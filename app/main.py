"""
FastAPI service for CodeInspectorAI.

Endpoints
---------
GET  /health        readiness probe (reports which credentials are configured)
POST /webhook       GitHub webhook: pull_request queues a review; installation
                    events keep track of who has the GitHub App installed
POST /review        manual trigger, useful for testing and re-runs

The landing page, dashboard and their JSON API live in app/web.py.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import (
    GITHUB_TOKEN,
    GITHUB_WEBHOOK_SECRET,
    GROQ_API_KEY,
    GROQ_MODEL,
    POST_COMMENTS,
    TRIGGER_ACTIONS,
)
from app.github import app_configured, forget_installation, use_token, verify_webhook_signature
from app.runner import get_store, is_public_repo, resolve_token, run_tracked
from app.web import STATIC_DIR, router as web_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("codeinspector")

app = FastAPI(
    title="CodeInspectorAI",
    description="AI pull request reviewer built on LangGraph + Groq.",
    version="1.0.0",
)
app.include_router(web_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ReviewRequest(BaseModel):
    owner: str = Field(description="Repository owner or organisation.")
    repo_name: str = Field(description="Repository name.")
    pr_number: int = Field(gt=0, description="Pull request number.")
    post_comment: Optional[bool] = Field(
        default=None,
        description="Override POST_COMMENTS for this run. False = dry run.",
    )


def _summarise(result: Dict[str, Any]) -> Dict[str, Any]:
    """Trim a graph result down to a JSON-friendly response."""
    return {
        "review_status": result.get("review_status", "failed"),
        "overall_verdict": result.get("overall_verdict"),
        "code_quality_score": result.get("code_quality_score"),
        "review_comments": len(result.get("review_comments") or []),
        "security_issues": len(result.get("security_issues") or []),
        "posted_comment_url": result.get("posted_comment_url", ""),
        "final_summary": result.get("final_summary", ""),
        "errors": result.get("errors") or [],
    }


def _run_review_task(payload: Dict[str, Any], source: str = "webhook",
                     is_public: Optional[bool] = None) -> None:
    """Background worker so GitHub is not kept waiting for the LLM calls."""
    owner, repo, number = payload.get("owner"), payload.get("repo_name"), payload.get("pr_number")
    label = "{o}/{r}#{n}".format(o=owner, r=repo, n=number)
    try:
        token = resolve_token(owner, repo, payload.get("installation_id"))
        if is_public is None:
            with use_token(token):
                # Unknown visibility is treated as private so the dashboard never leaks it.
                is_public = bool(is_public_repo(owner, repo))
        review_id = get_store().create(
            source=source, owner=owner, repo=repo, pr_number=number, is_public=is_public,
        )
        result = run_tracked(review_id, payload, token=token)
        logger.info("Review finished for %s: %s", label, _summarise(result))
    except Exception:
        logger.exception("Review crashed for %s", label)


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "model": GROQ_MODEL,
        "groq_key_configured": bool(GROQ_API_KEY),
        "github_token_configured": bool(GITHUB_TOKEN),
        "github_app_configured": app_configured(),
        "webhook_secret_configured": bool(GITHUB_WEBHOOK_SECRET),
        "post_comments": POST_COMMENTS,
    }


@app.post("/review")
def review(request: ReviewRequest, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    """Manually trigger a review. Runs in the background; check the logs or the PR."""
    if not GROQ_API_KEY:
        raise HTTPException(status_code=503, detail="GROQ_API_KEY is not configured.")

    background_tasks.add_task(
        _run_review_task, request.model_dump(exclude_none=True), "api"
    )
    return {
        "status": "queued",
        "target": "{o}/{r}#{n}".format(
            o=request.owner, r=request.repo_name, n=request.pr_number
        ),
    }


@app.post("/webhook")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str = Header(default=""),
    x_hub_signature_256: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Receive GitHub `pull_request` events and queue a review."""
    body = await request.body()

    if not verify_webhook_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    if x_github_event == "ping":
        return {"status": "pong"}
    if x_github_event not in ("pull_request", "installation", "installation_repositories"):
        return {"status": "ignored", "reason": "event " + (x_github_event or "unknown")}

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed JSON payload.")

    if x_github_event != "pull_request":
        return _record_installation(payload)

    action = payload.get("action", "")
    if action not in TRIGGER_ACTIONS:
        return {"status": "ignored", "reason": "action " + action}

    pull_request = payload.get("pull_request") or {}
    if pull_request.get("draft"):
        return {"status": "ignored", "reason": "draft PR"}

    repository = payload.get("repository") or {}
    owner = (repository.get("owner") or {}).get("login")
    repo_name = repository.get("name")
    pr_number = pull_request.get("number")

    if not (owner and repo_name and pr_number):
        raise HTTPException(status_code=400, detail="Payload is missing repo/PR fields.")

    task = {"owner": owner, "repo_name": repo_name, "pr_number": pr_number}
    installation_id = (payload.get("installation") or {}).get("id")
    if installation_id:
        task["installation_id"] = installation_id
    is_public = None if "private" not in repository else not repository["private"]
    background_tasks.add_task(_run_review_task, task, "webhook", is_public)
    logger.info("Queued review for %s/%s#%s (%s)", owner, repo_name, pr_number, action)
    return {
        "status": "queued",
        "target": "{o}/{r}#{n}".format(o=owner, r=repo_name, n=pr_number),
    }


_INSTALLATION_STATUS = {
    "created": "active", "unsuspend": "active", "new_permissions_accepted": "active",
    "deleted": "removed", "suspend": "suspended",
}


def _record_installation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the installations table in step with installs, uninstalls and repo changes."""
    installation = payload.get("installation") or {}
    installation_id = installation.get("id")
    if not installation_id:
        raise HTTPException(status_code=400, detail="Payload is missing the installation.")

    action = payload.get("action", "")
    # installation_repositories (added/removed) means the install is still live.
    status = _INSTALLATION_STATUS.get(action, "active")
    account = installation.get("account") or {}
    get_store().save_installation(
        installation_id,
        account=account.get("login", "unknown"),
        account_type=account.get("type", ""),
        repo_selection=installation.get("repository_selection", ""),
        status=status,
    )
    if status != "active":
        # GitHub revokes the access itself; just stop reusing a cached token.
        forget_installation(installation_id)
    logger.info("Installation %s on %s: %s", installation_id, account.get("login"), action)
    return {"status": "recorded", "installation": installation_id, "state": status}
