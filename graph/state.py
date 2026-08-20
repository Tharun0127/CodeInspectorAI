"""
LangGraph state definition for AI PR Reviewer (CodeInspectorAI).

This file defines the shared state that flows between every node in the graph,
plus the structured-output models the LLM nodes are bound to.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator
from typing_extensions import TypedDict

# ============================================================
# Structured Output Models
# ============================================================

# LLMs mix the two severity vocabularies (a review comment coming back as
# "high", a security issue as "warning"). Normalising is much friendlier than
# throwing away an otherwise good finding on a validation error.
_REVIEW_SEVERITY_ALIASES = {
    "low": "info", "minor": "info", "nit": "info", "suggestion": "info",
    "note": "info", "medium": "warning", "moderate": "warning", "major": "warning",
    "warn": "warning", "high": "critical", "blocker": "critical", "error": "critical",
}
_SECURITY_SEVERITY_ALIASES = {
    "info": "low", "informational": "low", "minor": "low", "nit": "low",
    "warning": "medium", "moderate": "medium", "major": "high",
    "severe": "critical", "blocker": "critical",
}


def _normalise(value: Any, aliases: dict, allowed: set, default: str) -> Any:
    """Map a loose severity string onto the values the schema allows."""
    if not isinstance(value, str):
        return value
    cleaned = value.strip().lower()
    if cleaned in allowed:
        return cleaned
    return aliases.get(cleaned, default)


class PRMetadata(BaseModel):
    """Basic metadata about the Pull Request."""

    title: str
    description: str = ""
    author: str = "unknown"
    source_branch: str = ""
    target_branch: str = ""
    commit_sha: str = ""
    url: str = ""


class ChangedFile(BaseModel):
    """Represents one file changed in the PR."""

    filename: str
    status: str = "modified"
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    patch: Optional[str] = None


class ParsedDiff(BaseModel):
    """Processed, per-file representation of the PR diff."""

    filename: str
    language: Optional[str] = None
    diff: str = ""
    summary: Optional[str] = None
    added_lines: int = 0
    removed_lines: int = 0


class ReviewComment(BaseModel):
    """A single piece of code review feedback."""

    file: str = Field(description="Path of the file the comment applies to.")
    line: Optional[int] = Field(
        default=None, description="Line number in the new file, if identifiable."
    )
    severity: Literal["info", "warning", "critical"] = Field(
        default="info", description="How important this comment is."
    )
    message: str = Field(description="What the problem is.")
    suggestion: Optional[str] = Field(
        default=None, description="A concrete way to fix or improve it."
    )

    @field_validator("severity", mode="before")
    @classmethod
    def _coerce_severity(cls, value: Any) -> Any:
        return _normalise(
            value, _REVIEW_SEVERITY_ALIASES, {"info", "warning", "critical"}, "info"
        )


class SecurityIssue(BaseModel):
    """A single security vulnerability detected in the diff."""

    file: str = Field(description="Path of the file containing the issue.")
    line: Optional[int] = Field(default=None, description="Line number, if identifiable.")
    severity: Literal["low", "medium", "high", "critical"] = Field(
        default="low", description="Risk level of the issue."
    )
    issue: str = Field(description="Description of the vulnerability.")
    recommendation: str = Field(description="How to remediate it.")

    @field_validator("severity", mode="before")
    @classmethod
    def _coerce_severity(cls, value: Any) -> Any:
        return _normalise(
            value,
            _SECURITY_SEVERITY_ALIASES,
            {"low", "medium", "high", "critical"},
            "low",
        )


class ReviewResult(BaseModel):
    """Full structured response expected from the review node."""

    comments: List[ReviewComment] = Field(
        default_factory=list, description="Review comments, most important first."
    )
    code_quality_score: float = Field(
        default=7.0, ge=0.0, le=10.0, description="Overall code quality, 0 (poor) to 10 (excellent)."
    )
    suggestions: List[str] = Field(
        default_factory=list, description="Short, high-level improvement suggestions."
    )


class SecurityResult(BaseModel):
    """Full structured response expected from the security node."""

    issues: List[SecurityIssue] = Field(
        default_factory=list, description="Security issues found. Empty list if none."
    )


# ============================================================
# LangGraph State
# ============================================================
# NOTE: `review` and `security` run in the same superstep, so any key both of
# them can write needs a reducer -- hence the Annotated[..., operator.add] on
# `errors`. Everything else is written by exactly one node.


class ReviewState(TypedDict, total=False):
    # --------------------------------------------------------
    # Initial Inputs (from webhook / CLI / test)
    # --------------------------------------------------------
    owner: str
    repo_name: str
    pr_number: int
    post_comment: bool  # overrides config.POST_COMMENTS for a single run

    # --------------------------------------------------------
    # Fetch PR / Context Node
    # --------------------------------------------------------
    raw_diff: str
    changed_files: List[ChangedFile]
    commit_messages: List[str]
    pr_metadata: PRMetadata
    pr_intent: str

    # --------------------------------------------------------
    # Context Enhancements
    # --------------------------------------------------------
    parsed_diff: List[ParsedDiff]
    file_summaries: List[str]
    language_detected: str

    # --------------------------------------------------------
    # Review & Security Nodes (Parallel)
    # --------------------------------------------------------
    review_comments: List[ReviewComment]
    code_quality_score: float
    suggestions: List[str]

    security_issues: List[SecurityIssue]
    severity_levels: List[str]
    vulnerable_lines: List[int]

    # --------------------------------------------------------
    # Summary Node
    # --------------------------------------------------------
    final_summary: str
    overall_verdict: Literal["approved", "approved_with_comments", "changes_requested"]
    formatted_comment: str
    key_recommendations: List[str]

    # --------------------------------------------------------
    # Post Review Node / Final Output
    # --------------------------------------------------------
    posted_comment_url: str
    review_status: Literal["success", "partial", "failed"]

    # --------------------------------------------------------
    # Control Fields
    # --------------------------------------------------------
    errors: Annotated[List[str], operator.add]
