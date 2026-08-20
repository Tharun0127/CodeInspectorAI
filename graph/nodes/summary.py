"""
Summary Node - Combines all findings into the final review comment.

The narrative paragraph comes from the LLM; the structure (verdict, tables,
counts) is assembled in code so the posted comment is always well-formed even
when the model misbehaves.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Sequence

from langchain_core.prompts import ChatPromptTemplate

from app.config import load_prompt
from app.llm import get_llm
from graph.state import ReviewComment, ReviewState, SecurityIssue
from graph.utils import decide_verdict

logger = logging.getLogger(__name__)

FALLBACK_PROMPT = (
    "You are an expert technical reviewer. Summarise the review findings clearly."
)

USER_TEMPLATE = """Here is the analysis of a pull request.

PR Title: {title}
PR Intent: {intent}
Computed verdict: {verdict}
Code quality score: {score}/10

=== CODE REVIEW FINDINGS ===
{review_comments}

=== SECURITY FINDINGS ===
{security_issues}

Write 2-4 short paragraphs of plain prose for the PR author: what this change
does, the most important things to fix (security first), and anything done
well. Do not repeat the findings verbatim as a list - they are rendered
separately below your text. Do not add a heading or a verdict line."""

VERDICT_BADGE = {
    "approved": "✅ **Approved**",
    "approved_with_comments": "💬 **Approved with comments**",
    "changes_requested": "🔴 **Changes requested**",
}

SEVERITY_ICON = {
    "critical": "🔴", "high": "🟠", "warning": "🟡",
    "medium": "🟡", "low": "🔵", "info": "ℹ️",
}


def _cell(text: str, limit: int = 300) -> str:
    """Make a string safe to put inside a markdown table cell."""
    flat = " ".join((text or "").split()).replace("|", r"\|")
    return flat[:limit] + ("..." if len(flat) > limit else "")


def _location(file: str, line) -> str:
    return "`{f}`{l}".format(f=file, l=":" + str(line) if line else "")


def _render_security_table(issues: Sequence[SecurityIssue]) -> str:
    if not issues:
        return "_No security issues detected in this diff._\n"
    rows = ["| | Severity | Location | Issue | Recommendation |",
            "|---|---|---|---|---|"]
    for issue in issues:
        rows.append("| {icon} | {sev} | {loc} | {issue} | {rec} |".format(
            icon=SEVERITY_ICON.get(issue.severity, ""),
            sev=issue.severity.capitalize(),
            loc=_location(issue.file, issue.line),
            issue=_cell(issue.issue),
            rec=_cell(issue.recommendation),
        ))
    return "\n".join(rows) + "\n"


def _render_review_section(comments: Sequence[ReviewComment]) -> str:
    if not comments:
        return "_No review comments._\n"
    lines = []
    for comment in comments:
        lines.append("- {icon} **{sev}** {loc} - {msg}".format(
            icon=SEVERITY_ICON.get(comment.severity, ""),
            sev=comment.severity.capitalize(),
            loc=_location(comment.file, comment.line),
            msg=_cell(comment.message, 600),
        ))
        if comment.suggestion:
            lines.append("  - _Suggestion:_ " + _cell(comment.suggestion, 600))
    return "\n".join(lines) + "\n"


def _findings_digest(comments: Sequence[ReviewComment], issues: Sequence[SecurityIssue]) -> tuple:
    """Compact text of the findings, for the LLM prompt."""
    review_text = "\n".join(
        "[{sev}] {file}: {msg}".format(sev=c.severity, file=c.file, msg=c.message)
        for c in comments
    ) or "none"
    security_text = "\n".join(
        "[{sev}] {file}: {issue}".format(sev=i.severity, file=i.file, issue=i.issue)
        for i in issues
    ) or "none"
    return review_text, security_text


def _build_comment(
    verdict: str,
    narrative: str,
    comments: Sequence[ReviewComment],
    issues: Sequence[SecurityIssue],
    suggestions: Sequence[str],
    score: float,
    files_reviewed: int,
    errors: Sequence[str],
) -> str:
    """Assemble the final markdown comment posted to GitHub."""
    critical = sum(1 for i in issues if i.severity in ("high", "critical"))
    parts: List[str] = [
        "## 🤖 CodeInspectorAI Review",
        "",
        "{badge} &nbsp;•&nbsp; Quality score **{score}/10** &nbsp;•&nbsp; "
        "{files} file(s) reviewed &nbsp;•&nbsp; {sec} security issue(s)"
        "{crit}".format(
            badge=VERDICT_BADGE.get(verdict, verdict),
            score=score,
            files=files_reviewed,
            sec=len(issues),
            crit=" (**{n} high/critical**)".format(n=critical) if critical else "",
        ),
        "",
        narrative.strip(),
        "",
        "### 🔐 Security",
        "",
        _render_security_table(issues),
        "",
        "### 📝 Code review",
        "",
        _render_review_section(comments),
    ]

    if suggestions:
        parts += ["", "### 💡 Suggestions", ""]
        parts += ["- " + _cell(s, 400) for s in suggestions[:8]]

    if errors:
        parts += [
            "", "<details><summary>⚠️ This review ran with errors</summary>", "",
        ]
        parts += ["- `" + _cell(e, 300) + "`" for e in errors]
        parts += ["", "</details>"]

    parts += [
        "",
        "---",
        "<sub>Generated by CodeInspectorAI - an automated reviewer. "
        "Treat its findings as suggestions, not gospel.</sub>",
    ]
    return "\n".join(parts)


def generate_summary(state: ReviewState) -> Dict[str, Any]:
    """Generate the final summary and the formatted GitHub comment."""
    comments = state.get("review_comments") or []
    issues = state.get("security_issues") or []
    score = state.get("code_quality_score", 0.0) or 0.0
    metadata = state.get("pr_metadata")
    errors: List[str] = []

    verdict = decide_verdict(comments, issues, score)
    review_text, security_text = _findings_digest(comments, issues)

    narrative = ""
    prompt = ChatPromptTemplate.from_messages(
        [("system", load_prompt("summary", FALLBACK_PROMPT)), ("user", USER_TEMPLATE)]
    )
    try:
        chain = prompt | get_llm(temperature=0.4, max_tokens=1500)
        narrative = chain.invoke(
            {
                "title": metadata.title if metadata else "Untitled PR",
                "intent": state.get("pr_intent", "") or "not stated",
                "verdict": verdict,
                "score": score,
                "review_comments": review_text,
                "security_issues": security_text,
            }
        ).content.strip()
    except Exception as exc:
        logger.exception("Summary node failed")
        errors.append("summary: {exc}".format(exc=exc))
        narrative = (
            "The automated narrative could not be generated, but the findings "
            "below are complete."
        )

    formatted = _build_comment(
        verdict=verdict,
        narrative=narrative,
        comments=comments,
        issues=issues,
        suggestions=state.get("suggestions") or [],
        score=score,
        files_reviewed=len(state.get("parsed_diff") or []),
        errors=list(state.get("errors") or []) + errors,
    )

    key_recommendations = [i.recommendation for i in issues[:3]]
    key_recommendations += [
        c.suggestion or c.message for c in comments[: max(0, 3 - len(key_recommendations))]
    ]

    return {
        "final_summary": narrative,
        "overall_verdict": verdict,
        "formatted_comment": formatted,
        "key_recommendations": key_recommendations,
        "errors": errors,
    }
