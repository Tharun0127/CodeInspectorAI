"""
Browser-facing routes: the landing page, the dashboard and their JSON API.

GET  /                    landing page with the live "try it" demo
GET  /dashboard           review history and stats
POST /api/reviews         start a demo review (dry run, public repos only)
GET  /api/reviews         recent reviews
GET  /api/reviews/{id}    one review, including live pipeline progress
GET  /api/stats           aggregate numbers for the dashboard

Private-repo reviews are only returned with a valid X-Dashboard-Token header.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import config
from app.runner import PIPELINE, get_store, is_public_repo, run_tracked

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_ACTIVE_DEMOS = 10

router = APIRouter()
executor = ThreadPoolExecutor(max_workers=config.REVIEW_CONCURRENCY,
                              thread_name_prefix="review")

_PR_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)"
    r"|^([\w.-]+)/([\w.-]+)#(\d+)$"
)


class DemoRequest(BaseModel):
    pr_url: str = Field(max_length=300, description="https://github.com/owner/repo/pull/123")


def parse_pr_reference(text: str) -> Optional[tuple]:
    match = _PR_URL.match(text.strip())
    if not match:
        return None
    groups = [g for g in match.groups() if g is not None]
    owner, repo, number = groups
    return owner, repo, int(number)


def is_admin(token: Optional[str]) -> bool:
    expected = config.DASHBOARD_TOKEN
    return bool(expected and token and hmac.compare_digest(token, expected))


def _client_hash(request: Request) -> str:
    host = request.client.host if request.client else "unknown"
    return hashlib.sha256(host.encode()).hexdigest()[:16]


def _brief(review: Dict[str, Any]) -> Dict[str, Any]:
    """List-view projection: drop the heavy markdown and per-finding detail."""
    result = review.get("result") or {}
    meta = result.get("pr_metadata") or {}
    security = result.get("security_issues") or []
    finished, started = review.get("finished_at"), review.get("started_at")
    return {
        "id": review["id"],
        "source": review["source"],
        "repo": f"{review['owner']}/{review['repo']}",
        "pr_number": review["pr_number"],
        "is_public": review["is_public"],
        "status": review["status"],
        "created_at": review["created_at"],
        "duration": round(finished - started, 1) if finished and started else None,
        "title": meta.get("title"),
        "author": meta.get("author"),
        "url": meta.get("url"),
        "verdict": result.get("overall_verdict"),
        "score": result.get("code_quality_score"),
        "comments": len(result.get("review_comments") or []),
        "security": len(security),
        "critical": sum(1 for i in security if i.get("severity") in ("high", "critical")),
        "language": result.get("language"),
        "additions": result.get("additions"),
        "deletions": result.get("deletions"),
    }


# ---------------------------------------------------------------- pages

@router.get("/", include_in_schema=False)
def landing() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@router.get("/dashboard", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


# ---------------------------------------------------------------- API

@router.get("/api")
def api_root() -> Dict[str, Any]:
    return {"service": "CodeInspectorAI", "status": "running", "docs": "/docs"}


@router.post("/api/reviews", status_code=202)
def start_demo_review(body: DemoRequest, request: Request) -> Dict[str, Any]:
    if not config.DEMO_ENABLED:
        raise HTTPException(403, "The live demo is disabled on this instance.")
    if not config.GROQ_API_KEY:
        raise HTTPException(503, "This instance has no GROQ_API_KEY configured.")

    target = parse_pr_reference(body.pr_url)
    if not target:
        raise HTTPException(422, "Paste a pull request URL like "
                                 "https://github.com/owner/repo/pull/123")
    owner, repo, number = target

    client = _client_hash(request)
    if get_store().count_recent(client, time.time() - 3600) >= config.DEMO_RATE_LIMIT:
        raise HTTPException(429, f"Demo limit reached ({config.DEMO_RATE_LIMIT} reviews "
                                 "per hour). Self-host it for unlimited reviews!")
    if get_store().count_active() >= MAX_ACTIVE_DEMOS:
        raise HTTPException(503, "The reviewer is busy right now. Try again in a minute.")

    visibility = is_public_repo(owner, repo)
    if visibility is None:
        raise HTTPException(404, f"Couldn't find {owner}/{repo} on GitHub.")
    if not visibility:
        raise HTTPException(403, "The public demo only reviews public repositories.")

    review_id = get_store().create(source="demo", owner=owner, repo=repo,
                                   pr_number=number, is_public=True, client_hash=client)
    # A demo never posts to GitHub: visitors don't own the PRs they paste.
    executor.submit(run_tracked, review_id,
                    {"owner": owner, "repo_name": repo, "pr_number": number,
                     "post_comment": False})
    return {"id": review_id, "status": "queued", "target": f"{owner}/{repo}#{number}"}


@router.get("/api/reviews")
def list_reviews(
    limit: int = Query(50, ge=1, le=200),
    x_dashboard_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    admin = is_admin(x_dashboard_token)
    reviews = get_store().list(limit=limit, include_private=admin)
    return {"admin": admin, "reviews": [_brief(r) for r in reviews]}


@router.get("/api/reviews/{review_id}")
def get_review(
    review_id: str,
    x_dashboard_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    review = get_store().get(review_id)
    if not review or (not review["is_public"] and not is_admin(x_dashboard_token)):
        raise HTTPException(404, "Review not found.")
    return {**_brief(review), "pipeline": PIPELINE, "steps": review["steps"],
            "result": review["result"]}


@router.get("/api/stats")
def stats(x_dashboard_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    admin = is_admin(x_dashboard_token)
    reviews = [_brief(r) for r in get_store().list(limit=1000, include_private=admin)]
    finished = [r for r in reviews if r["status"] == "done"]
    scores = [r["score"] for r in finished if r["score"] is not None]
    durations = [r["duration"] for r in finished if r["duration"]]

    day = 86400
    today = int(time.time() // day)
    per_day: List[int] = [0] * 14
    for r in reviews:
        offset = today - int(r["created_at"] // day)
        if 0 <= offset < 14:
            per_day[13 - offset] += 1

    return {
        "admin": admin,
        "total": len(reviews),
        "completed": len(finished),
        "failed": sum(1 for r in reviews if r["status"] == "failed"),
        "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
        "avg_duration": round(sum(durations) / len(durations), 1) if durations else None,
        "comments": sum(r["comments"] for r in finished),
        "security": sum(r["security"] for r in finished),
        "critical": sum(r["critical"] for r in finished),
        "verdicts": dict(Counter(r["verdict"] for r in finished if r["verdict"])),
        "languages": dict(Counter(r["language"] for r in finished
                                  if r["language"]).most_common(6)),
        "top_repos": Counter(r["repo"] for r in reviews).most_common(5),
        "per_day": per_day,
        "score_buckets": [sum(1 for s in scores if min(int(s // 2), 4) == b) for b in range(5)],
    }
