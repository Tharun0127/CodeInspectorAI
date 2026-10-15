"""
Post Review Node - Publishes the assembled review back to the Pull Request.

This is the final node in the graph. It is deliberately fail-soft: if posting
fails, the run is marked "partial" and the formatted comment is still returned
to the caller rather than being lost.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from app.config import POST_COMMENTS
from app.github import current_token, get_pr, post_pr_comment
from graph.state import ReviewState

logger = logging.getLogger(__name__)


def post_review(state: ReviewState) -> Dict[str, Any]:
    """Post the formatted review comment on the PR."""
    body = state.get("formatted_comment", "")
    prior_errors = state.get("errors") or []
    status = "partial" if prior_errors else "success"

    if not body:
        return {
            "posted_comment_url": "",
            "review_status": "failed",
            "errors": ["post_review: nothing to post - no formatted comment."],
        }

    # `post_comment` in the state overrides the global setting for one run.
    should_post = state.get("post_comment")
    if should_post is None:
        should_post = POST_COMMENTS

    if not should_post:
        logger.info("Dry run: skipping GitHub comment (POST_COMMENTS is off).")
        return {"posted_comment_url": "", "review_status": status, "errors": []}

    # An installation token (GitHub App) or GITHUB_TOKEN, whichever this run has.
    if not current_token():
        logger.warning("No GitHub credentials - review generated but not posted.")
        return {
            "posted_comment_url": "",
            "review_status": "partial",
            "errors": ["post_review: install the GitHub App on this repo or set "
                       "GITHUB_TOKEN to post the comment."],
        }

    repo_full_name = "{owner}/{repo}".format(
        owner=state.get("owner", ""), repo=state.get("repo_name", "")
    )
    try:
        pr = get_pr(repo_full_name, state["pr_number"])
        url = post_pr_comment(pr, body)
        logger.info("Posted review to %s", url)
        return {"posted_comment_url": url, "review_status": status, "errors": []}
    except Exception as exc:
        logger.exception("Failed to post review comment")
        return {
            "posted_comment_url": "",
            "review_status": "partial",
            "errors": ["post_review: {exc}".format(exc=exc)],
        }
