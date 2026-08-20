"""
FastAPI service for CodeInspectorAI.

Endpoints
---------
GET  /              service banner
GET  /health        readiness probe (reports which credentials are configured)
POST /webhook       GitHub pull_request webhook -> queues a review
POST /review        manual trigger, useful for testing and re-runs
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import (
    GITHUB_TOKEN,
    GITHUB_WEBHOOK_SECRET,
    GROQ_API_KEY,
    GROQ_MODEL,
    POST_COMMENTS,
    TRIGGER_ACTIONS,
)
from app.github import verify_webhook_signature
from graph.graph import run_pr_review

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


def _run_review_task(payload: Dict[str, Any]) -> None:
    """Background worker so GitHub is not kept waiting for the LLM calls."""
    label = "{o}/{r}#{n}".format(
        o=payload.get("owner"), r=payload.get("repo_name"), n=payload.get("pr_number")
    )
    try:
        result = run_pr_review(payload)
        logger.info("Review finished for %s: %s", label, _summarise(result))
    except Exception:
        logger.exception("Review crashed for %s", label)


@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "service": "CodeInspectorAI",
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "model": GROQ_MODEL,
        "groq_key_configured": bool(GROQ_API_KEY),
        "github_token_configured": bool(GITHUB_TOKEN),
        "webhook_secret_configured": bool(GITHUB_WEBHOOK_SECRET),
        "post_comments": POST_COMMENTS,
    }


@app.post("/review")
def review(request: ReviewRequest, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    """Manually trigger a review. Runs in the background; check the logs or the PR."""
    if not GROQ_API_KEY:
        raise HTTPException(status_code=503, detail="GROQ_API_KEY is not configured.")

    background_tasks.add_task(_run_review_task, request.model_dump(exclude_none=True))
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
    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": "event " + (x_github_event or "unknown")}

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed JSON payload.")

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

    background_tasks.add_task(
        _run_review_task,
        {"owner": owner, "repo_name": repo_name, "pr_number": pr_number},
    )
    logger.info("Queued review for %s/%s#%s (%s)", owner, repo_name, pr_number, action)
    return {
        "status": "queued",
        "target": "{o}/{r}#{n}".format(o=owner, r=repo_name, n=pr_number),
    }
