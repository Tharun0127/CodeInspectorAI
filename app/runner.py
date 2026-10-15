"""
Runs a review while recording its progress in the ReviewStore.

The graph is streamed rather than invoked, so every node's completion time is
written as it happens. The UI polls that to animate the pipeline live, and the
timings show the review and security agents genuinely overlapping.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Any, Dict, Optional

from pydantic import BaseModel

from app.config import DATA_DIR
from app.github import (
    app_configured,
    find_installation_id,
    get_client,
    get_installation_token,
    use_token,
)
from app.store import ReviewStore
from graph.graph import graph

logger = logging.getLogger(__name__)

PIPELINE = ["fetch_context", "code_review", "security_scan", "generate_summary", "post_review"]


@lru_cache(maxsize=1)
def get_store() -> ReviewStore:
    return ReviewStore(DATA_DIR / "reviews.db")


def is_public_repo(owner: str, repo: str) -> Optional[bool]:
    """True/False from GitHub, or None if the repo could not be looked up."""
    try:
        return not get_client().get_repo(f"{owner}/{repo}").private
    except Exception as exc:
        logger.info("Could not read visibility of %s/%s: %s", owner, repo, exc)
        return None


def resolve_token(owner: str, repo: str, installation_id: Optional[int] = None) -> Optional[str]:
    """
    The GitHub App installation token for this repo, if the app is installed there.

    None means "fall back to GITHUB_TOKEN (or anonymous)". Webhook deliveries name
    their installation; API and demo runs look it up from the repo.
    """
    if not app_configured():
        return None
    try:
        installation_id = installation_id or find_installation_id(owner, repo)
        return get_installation_token(installation_id) if installation_id else None
    except Exception as exc:
        logger.warning("Could not get an installation token for %s/%s: %s", owner, repo, exc)
        return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def serialise_result(state: Dict[str, Any]) -> Dict[str, Any]:
    """Keep what the UI shows; drop the raw diff and other bulky internals."""
    files = state.get("changed_files") or []
    return {
        "review_status": state.get("review_status", "failed"),
        "overall_verdict": state.get("overall_verdict"),
        "code_quality_score": state.get("code_quality_score"),
        "review_comments": _jsonable(state.get("review_comments") or []),
        "security_issues": _jsonable(state.get("security_issues") or []),
        "suggestions": state.get("suggestions") or [],
        "key_recommendations": state.get("key_recommendations") or [],
        "final_summary": state.get("final_summary", ""),
        "formatted_comment": state.get("formatted_comment", ""),
        "posted_comment_url": state.get("posted_comment_url", ""),
        "pr_metadata": _jsonable(state.get("pr_metadata")),
        "language": state.get("language_detected"),
        "files_changed": len(files),
        "additions": sum(getattr(f, "additions", 0) for f in files),
        "deletions": sum(getattr(f, "deletions", 0) for f in files),
        "errors": state.get("errors") or [],
    }


def run_tracked(review_id: str, initial_state: Dict[str, Any],
                token: Optional[str] = None) -> Dict[str, Any]:
    """
    Run the graph for an existing store record, recording every step.

    `initial_state` may carry `installation_id` (from a GitHub App webhook); it is
    used to pick the token and is not passed into the graph.
    """
    store = get_store()
    started = time.time()
    store.update(review_id, status="running", started_at=started)

    initial_state = dict(initial_state)
    installation_id = initial_state.pop("installation_id", None)
    if token is None:
        token = resolve_token(initial_state.get("owner", ""),
                              initial_state.get("repo_name", ""), installation_id)

    steps: Dict[str, Dict[str, float]] = {}
    state: Dict[str, Any] = {"review_status": "success", "errors": [], **initial_state}

    try:
        with use_token(token):
            stream = graph.stream(state, stream_mode=["updates", "values"])
            for mode, chunk in stream:
                if mode == "values":
                    state = chunk
                    continue
                for node in chunk or {}:
                    steps[node] = {"finished": round(time.time() - started, 2)}
                store.update(review_id, steps=steps)
    except Exception as exc:
        logger.exception("Review %s crashed", review_id)
        state["review_status"] = "failed"
        state["errors"] = list(state.get("errors") or []) + [f"crash: {exc}"]

    result = serialise_result(state)
    store.update(
        review_id,
        status="failed" if result["review_status"] == "failed" else "done",
        steps=steps,
        result=result,
        finished_at=time.time(),
    )
    return state
