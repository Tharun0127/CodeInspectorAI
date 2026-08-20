"""
Review Node - Performs a comprehensive code review with Groq.

Runs in parallel with the security node.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from langchain_core.prompts import ChatPromptTemplate

from app.config import MAX_DIFF_CHARS, load_prompt
from app.llm import invoke_structured
from graph.state import ReviewResult, ReviewState
from graph.utils import build_diff_context, sort_review_comments

logger = logging.getLogger(__name__)

FALLBACK_PROMPT = (
    "You are an expert senior software engineer performing a thorough code review."
)

USER_TEMPLATE = """Review the following pull request.

PR Title: {title}
PR Intent: {intent}
Primary language: {language}

Changed files:
{files}

Recent commit messages:
{commits}

Unified diff (only the changed hunks are shown):
{diff}

Rules:
- Only comment on lines that actually appear in the diff.
- Use the exact file paths listed above.
- Prefer a few high-signal comments over many trivial ones; return an empty
  list of comments if the change genuinely looks fine.
- Score code_quality_score from 0 (unacceptable) to 10 (excellent)."""


def code_review(state: ReviewState) -> Dict[str, Any]:
    """Main code review node."""
    parsed_diff = state.get("parsed_diff") or []
    if not parsed_diff:
        return {
            "review_comments": [],
            "code_quality_score": 0.0,
            "suggestions": [],
            "errors": [],
        }

    metadata = state.get("pr_metadata")
    prompt = ChatPromptTemplate.from_messages(
        [("system", load_prompt("review", FALLBACK_PROMPT)), ("user", USER_TEMPLATE)]
    )

    try:
        result = invoke_structured(
            prompt,
            ReviewResult,
            {
                "title": metadata.title if metadata else "Untitled PR",
                "intent": state.get("pr_intent", "") or "not stated",
                "language": state.get("language_detected", "") or "mixed/unknown",
                "files": "\n".join(state.get("file_summaries") or []),
                "commits": "\n".join(state.get("commit_messages") or []) or "none",
                "diff": build_diff_context(parsed_diff, MAX_DIFF_CHARS),
            },
            temperature=0.2,
        )
    except Exception as exc:
        logger.exception("Code review node failed")
        return {
            "review_comments": [],
            "code_quality_score": 0.0,
            "suggestions": [],
            "errors": ["review: {exc}".format(exc=exc)],
        }

    comments = sort_review_comments(result.comments)
    logger.info("Review produced %s comment(s), score %.1f",
                len(comments), result.code_quality_score)

    return {
        "review_comments": comments,
        "code_quality_score": round(float(result.code_quality_score), 1),
        "suggestions": result.suggestions,
        "errors": [],
    }
