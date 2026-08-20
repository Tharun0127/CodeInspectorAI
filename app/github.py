"""
GitHub integration helpers for the AI PR Reviewer.

Everything that touches the GitHub API lives here so the graph nodes stay
testable with a fake client.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Dict, List, Optional

import requests
from github import Auth, Github, GithubException
from github.PullRequest import PullRequest

from app.config import GITHUB_API_URL, GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET

logger = logging.getLogger(__name__)

# Marker used to recognise (and replace) our own previous review comment.
REVIEW_MARKER = "<!-- codeinspector-ai-review -->"


def get_client() -> Github:
    """
    Build a GitHub client.

    A token is optional for reading public repositories, but required to post
    the review back to the PR.
    """
    if GITHUB_TOKEN:
        return Github(auth=Auth.Token(GITHUB_TOKEN), base_url=GITHUB_API_URL)
    logger.warning("GITHUB_TOKEN not set - using unauthenticated access (60 req/h, read-only).")
    return Github(base_url=GITHUB_API_URL)


def get_pr(repo_full_name: str, pr_number: int) -> PullRequest:
    """Get a Pull Request object from GitHub."""
    return get_client().get_repo(repo_full_name).get_pull(pr_number)


def get_pr_diff(pr: PullRequest) -> str:
    """
    Fetch the unified diff for a PR.

    PyGithub exposes `diff_url` but not the diff body, so the raw REST endpoint
    is requested with the diff media type. If that fails (private repo without a
    token, or an oversized diff) the per-file patches are stitched together.
    """
    headers = {"Accept": "application/vnd.github.v3.diff"}
    if GITHUB_TOKEN:
        headers["Authorization"] = "Bearer " + GITHUB_TOKEN

    try:
        response = requests.get(pr.url, headers=headers, timeout=30)
        if response.ok and response.text.strip():
            return response.text
        logger.warning("Diff endpoint returned HTTP %s; falling back to file patches.",
                       response.status_code)
    except requests.RequestException as exc:
        logger.warning("Diff request failed (%s); falling back to file patches.", exc)

    return assemble_diff_from_files(get_changed_files(pr))


def assemble_diff_from_files(files: List[Dict[str, Any]]) -> str:
    """Rebuild a diff-like blob from individual file patches."""
    blocks = []
    for file in files:
        patch = file.get("patch")
        if patch:
            blocks.append("diff --git a/{name} b/{name}\n{patch}".format(
                name=file["filename"], patch=patch))
    return "\n".join(blocks)


def get_changed_files(pr: PullRequest) -> List[Dict[str, Any]]:
    """List of changed files with metadata."""
    files = []
    for file in pr.get_files():
        files.append(
            {
                "filename": file.filename,
                "status": file.status,
                "additions": file.additions,
                "deletions": file.deletions,
                "changes": file.changes,
                "patch": file.patch,
            }
        )
    return files


def get_commit_messages(pr: PullRequest, limit: int = 50) -> List[str]:
    """Commit messages on the PR branch, newest last."""
    messages = []
    try:
        for commit in pr.get_commits()[:limit]:
            message = (commit.commit.message or "").strip()
            if message:
                messages.append(message.splitlines()[0])
    except GithubException as exc:
        logger.warning("Could not read commits: %s", exc)
    return messages


def get_pr_metadata(pr: PullRequest) -> Dict[str, Any]:
    """Extract useful PR metadata."""
    return {
        "title": pr.title or "Untitled PR",
        "description": pr.body or "No description provided.",
        "author": pr.user.login if pr.user else "unknown",
        "source_branch": pr.head.ref if pr.head else "",
        "target_branch": pr.base.ref if pr.base else "",
        "commit_sha": pr.head.sha if pr.head else "",
        "url": pr.html_url or "",
    }


def post_pr_comment(pr: PullRequest, body: str, replace_previous: bool = True) -> str:
    """
    Post the review as an issue comment on the PR and return its URL.

    When `replace_previous` is set, an earlier comment from this bot (identified
    by REVIEW_MARKER) is edited in place instead of stacking a new one on every
    push to the branch.
    """
    body_with_marker = body if REVIEW_MARKER in body else REVIEW_MARKER + "\n" + body

    if replace_previous:
        existing = find_previous_review_comment(pr)
        if existing is not None:
            existing.edit(body_with_marker)
            return existing.html_url

    comment = pr.create_issue_comment(body_with_marker)
    return comment.html_url


def find_previous_review_comment(pr: PullRequest):
    """Return this bot's most recent review comment on the PR, if any."""
    try:
        for comment in reversed(list(pr.get_issue_comments())):
            if REVIEW_MARKER in (comment.body or ""):
                return comment
    except GithubException as exc:
        logger.warning("Could not list existing comments: %s", exc)
    return None


def verify_webhook_signature(payload: bytes, signature_header: Optional[str]) -> bool:
    """
    Validate the X-Hub-Signature-256 header against GITHUB_WEBHOOK_SECRET.

    Returns True when no secret is configured, so local testing works without
    one - set the secret in production.
    """
    if not GITHUB_WEBHOOK_SECRET:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header.split("=", 1)[1])
