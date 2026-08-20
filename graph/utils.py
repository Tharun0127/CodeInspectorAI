"""
Pure helpers used by the graph nodes: language detection, diff parsing and
budget-aware truncation. No network and no LLM calls live here, which keeps
this module trivially unit-testable.
"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

from graph.state import ChangedFile, ParsedDiff, ReviewComment, SecurityIssue

# Extension -> human readable language name.
LANGUAGE_BY_EXTENSION = {
    "py": "Python", "pyi": "Python",
    "js": "JavaScript", "jsx": "JavaScript", "mjs": "JavaScript", "cjs": "JavaScript",
    "ts": "TypeScript", "tsx": "TypeScript",
    "java": "Java", "kt": "Kotlin", "kts": "Kotlin", "scala": "Scala",
    "go": "Go", "rs": "Rust", "rb": "Ruby", "php": "PHP",
    "c": "C", "h": "C", "cc": "C++", "cpp": "C++", "cxx": "C++", "hpp": "C++",
    "cs": "C#", "swift": "Swift", "m": "Objective-C", "mm": "Objective-C",
    "sh": "Shell", "bash": "Shell", "zsh": "Shell", "ps1": "PowerShell",
    "sql": "SQL", "html": "HTML", "htm": "HTML",
    "css": "CSS", "scss": "SCSS", "sass": "SCSS", "less": "Less",
    "vue": "Vue", "svelte": "Svelte", "dart": "Dart", "lua": "Lua", "r": "R",
    "yml": "YAML", "yaml": "YAML", "json": "JSON", "toml": "TOML", "ini": "INI",
    "xml": "XML", "md": "Markdown", "rst": "reStructuredText",
    "tf": "Terraform", "gradle": "Gradle",
}

# Files whose diffs are noise for a code review.
_IGNORED_SUFFIXES = (
    ".lock", ".min.js", ".min.css", ".map", ".snap",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf",
    ".woff", ".woff2", ".ttf", ".eot", ".zip", ".gz",
)
_IGNORED_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Pipfile.lock", "go.sum", "Cargo.lock",
}


def detect_language(filename: str) -> str:
    """Best-effort language name for a path. Returns an empty string when unknown."""
    if not filename:
        return ""
    name = filename.rsplit("/", 1)[-1]
    lowered = name.lower()
    if lowered.startswith("dockerfile"):
        return "Dockerfile"
    if lowered == "makefile":
        return "Makefile"
    if "." not in name:
        return ""
    return LANGUAGE_BY_EXTENSION.get(name.rsplit(".", 1)[-1].lower(), "")


def is_reviewable(filename: str) -> bool:
    """False for lockfiles, minified bundles and binary assets."""
    name = filename.rsplit("/", 1)[-1]
    if name in _IGNORED_NAMES:
        return False
    return not filename.lower().endswith(_IGNORED_SUFFIXES)


def count_patch_lines(patch: str) -> Tuple[int, int]:
    """Count added/removed lines in a unified diff patch, ignoring file headers."""
    added = removed = 0
    for line in (patch or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def parse_changed_files(changed_files: Sequence[ChangedFile]) -> List[ParsedDiff]:
    """Turn raw GitHub file entries into per-file ParsedDiff objects."""
    parsed: List[ParsedDiff] = []
    for file in changed_files:
        if not is_reviewable(file.filename):
            continue
        patch = file.patch or ""
        added, removed = count_patch_lines(patch)
        parsed.append(
            ParsedDiff(
                filename=file.filename,
                language=detect_language(file.filename) or None,
                diff=patch,
                summary="{status}: +{add}/-{rem} ({chg} changes)".format(
                    status=file.status, add=file.additions,
                    rem=file.deletions, chg=file.changes,
                ),
                added_lines=added or file.additions,
                removed_lines=removed or file.deletions,
            )
        )
    return parsed


def dominant_language(parsed: Iterable[ParsedDiff]) -> str:
    """The language accounting for the most changed lines across the PR."""
    weights = {}
    for item in parsed:
        if not item.language:
            continue
        weights[item.language] = (
            weights.get(item.language, 0) + item.added_lines + item.removed_lines
        )
    if not weights:
        return ""
    return max(weights.items(), key=lambda kv: (kv[1], kv[0]))[0]


def build_file_summaries(parsed: Sequence[ParsedDiff]) -> List[str]:
    """One human-readable line per changed file, used as prompt context."""
    return [
        "{name} [{lang}] +{add}/-{rem}".format(
            name=item.filename, lang=item.language or "unknown",
            add=item.added_lines, rem=item.removed_lines,
        )
        for item in parsed
    ]


def build_diff_context(parsed: Sequence[ParsedDiff], max_chars: int) -> str:
    """
    Assemble a diff string that fits inside max_chars.

    Anything that does not fit is listed explicitly, so the model knows its view
    of the PR is partial instead of silently reviewing a truncated diff.
    """
    if max_chars <= 0 or not parsed:
        return ""

    chunks: List[str] = []
    skipped: List[str] = []
    used = 0

    for item in parsed:
        header = "--- FILE: {name} ({lang}) ---\n".format(
            name=item.filename, lang=item.language or "unknown"
        )
        body = item.diff.strip()
        if not body:
            skipped.append(item.filename + " (no textual patch available)")
            continue

        block = header + body + "\n"
        if used + len(block) > max_chars:
            remaining = max_chars - used - len(header)
            if remaining > 400:  # only worth including if a useful slice fits
                block = header + body[:remaining] + "\n... [file truncated]\n"
                chunks.append(block)
                used += len(block)
            skipped.append(item.filename + " (too large for the context budget)")
            continue

        chunks.append(block)
        used += len(block)

    if skipped:
        chunks.append(
            "\n--- NOT SHOWN IN THIS DIFF ---\n"
            + "\n".join("- " + entry for entry in skipped)
            + "\n"
        )
    return "".join(chunks)


# ------------------------------------------------------------
# Verdict / scoring helpers
# ------------------------------------------------------------

_REVIEW_SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}
_SECURITY_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def sort_review_comments(comments: Sequence[ReviewComment]) -> List[ReviewComment]:
    """Most severe comments first."""
    return sorted(comments, key=lambda c: -_REVIEW_SEVERITY_ORDER.get(c.severity, 0))


def sort_security_issues(issues: Sequence[SecurityIssue]) -> List[SecurityIssue]:
    """Most severe issues first."""
    return sorted(issues, key=lambda i: -_SECURITY_SEVERITY_ORDER.get(i.severity, 0))


def decide_verdict(
    review_comments: Sequence[ReviewComment],
    security_issues: Sequence[SecurityIssue],
    code_quality_score: float,
) -> str:
    """
    Deterministic verdict, so the outcome never depends on LLM phrasing.

    - changes_requested: any high/critical security issue, any critical review
      comment, or a very low quality score.
    - approved: nothing worth mentioning at all.
    - approved_with_comments: everything in between.
    """
    if any(issue.severity in ("high", "critical") for issue in security_issues):
        return "changes_requested"
    if any(comment.severity == "critical" for comment in review_comments):
        return "changes_requested"
    if code_quality_score and code_quality_score < 5.0:
        return "changes_requested"
    if not review_comments and not security_issues:
        return "approved"
    return "approved_with_comments"
