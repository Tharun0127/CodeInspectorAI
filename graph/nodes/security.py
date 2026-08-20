"""
Security Node - Scans the diff for security vulnerabilities.

Runs in parallel with the review node.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from langchain_core.prompts import ChatPromptTemplate

from app.config import MAX_DIFF_CHARS, load_prompt
from app.llm import invoke_structured
from graph.state import ReviewState, SecurityIssue, SecurityResult
from graph.utils import build_diff_context, sort_security_issues

logger = logging.getLogger(__name__)

FALLBACK_PROMPT = (
    "You are a security expert. Find real vulnerabilities in the code changes."
)

USER_TEMPLATE = """Analyse this pull request for security vulnerabilities.

PR Title: {title}
Primary language: {language}

Changed files:
{files}

Unified diff (only the changed hunks are shown):
{diff}

Rules:
- Report only issues that are actually introduced or made worse by this diff.
- Do not speculate about code you cannot see.
- Return an empty list if the change introduces no security risk.
- Every issue needs a concrete, actionable recommendation."""

# Cheap regex pre-screen. These never replace the LLM pass - they are appended
# so that an obvious hardcoded secret is never missed because of a bad sample.
SECRET_PATTERNS = [
    (r"(?i)\b(?:aws_secret_access_key|aws_access_key_id)\b\s*[=:]\s*\S+", "Hardcoded AWS credential"),
    (r"\bAKIA[0-9A-Z]{16}\b", "Hardcoded AWS access key ID"),
    (r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "Hardcoded GitHub token"),
    (r"\bsk-[A-Za-z0-9_\-]{20,}\b", "Hardcoded API key (OpenAI-style)"),
    (r"\bgsk_[A-Za-z0-9]{20,}\b", "Hardcoded API key (Groq-style)"),
    (r"(?i)-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----", "Private key committed to the repository"),
    # No leading \b: names like DB_PASSWORD / apiKey must match too.
    (r"(?i)(?:password|passwd|secret|api_?key|token|credential)s?\s*[=:]\s*[\"'][^\"'\s]{8,}[\"']", "Possible hardcoded credential"),
]


def scan_for_secrets(parsed_diff) -> List[SecurityIssue]:
    """Regex sweep over added lines only, so pre-existing code is not re-reported."""
    findings: List[SecurityIssue] = []
    for item in parsed_diff:
        for offset, line in enumerate(item.diff.splitlines()):
            if not line.startswith("+") or line.startswith("+++"):
                continue
            for pattern, label in SECRET_PATTERNS:
                if re.search(pattern, line):
                    findings.append(
                        SecurityIssue(
                            file=item.filename,
                            line=offset,
                            severity="critical",
                            issue="{label} detected in an added line.".format(label=label),
                            recommendation=(
                                "Remove the value from the diff, move it to an "
                                "environment variable or secret manager, and rotate "
                                "the credential - it must be treated as compromised."
                            ),
                        )
                    )
                    break  # one finding per line is enough
    return findings


def _deduplicate(issues: List[SecurityIssue]) -> List[SecurityIssue]:
    """Drop issues that repeat the same file + description."""
    seen = set()
    unique = []
    for issue in issues:
        key = (issue.file, issue.issue.strip().lower()[:80])
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return unique


def security_scan(state: ReviewState) -> Dict[str, Any]:
    """Security analysis node."""
    parsed_diff = state.get("parsed_diff") or []
    if not parsed_diff:
        return {
            "security_issues": [],
            "severity_levels": [],
            "vulnerable_lines": [],
            "errors": [],
        }

    metadata = state.get("pr_metadata")
    issues: List[SecurityIssue] = scan_for_secrets(parsed_diff)
    errors: List[str] = []

    prompt = ChatPromptTemplate.from_messages(
        [("system", load_prompt("security", FALLBACK_PROMPT)), ("user", USER_TEMPLATE)]
    )

    try:
        result = invoke_structured(
            prompt,
            SecurityResult,
            {
                "title": metadata.title if metadata else "Untitled PR",
                "language": state.get("language_detected", "") or "mixed/unknown",
                "files": "\n".join(state.get("file_summaries") or []),
                "diff": build_diff_context(parsed_diff, MAX_DIFF_CHARS),
            },
            temperature=0.1,
        )
        issues.extend(result.issues)
    except Exception as exc:
        logger.exception("Security node failed")
        errors.append("security: {exc}".format(exc=exc))

    issues = sort_security_issues(_deduplicate(issues))
    logger.info("Security scan produced %s issue(s)", len(issues))

    return {
        "security_issues": issues,
        "severity_levels": [issue.severity for issue in issues],
        "vulnerable_lines": [issue.line for issue in issues if issue.line is not None],
        "errors": errors,
    }
