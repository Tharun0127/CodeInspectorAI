"""
Context Node - Fetches PR data from GitHub and builds rich context
for the review and security agents.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from app.github import (
    get_changed_files,
    get_commit_messages,
    get_pr,
    get_pr_diff,
    get_pr_metadata,
)
from graph.state import ChangedFile, PRMetadata, ReviewState
from graph.utils import (
    build_file_summaries,
    dominant_language,
    parse_changed_files,
)

logger = logging.getLogger(__name__)


def _infer_intent(metadata: PRMetadata, commit_messages: list) -> str:
    """
    Cheap, deterministic intent line: the PR description when the author wrote
    one, otherwise the commit messages. No LLM call needed for this.
    """
    description = (metadata.description or "").strip()
    if description and description.lower() != "no description provided.":
        return description[:1500]
    if commit_messages:
        return "Inferred from commits: " + "; ".join(commit_messages[:10])
    return metadata.title


def fetch_context(state: ReviewState) -> Dict[str, Any]:
    """Fetch PR data from GitHub and prepare context for the downstream nodes."""
    owner = state.get("owner", "")
    repo_name = state.get("repo_name", "")
    pr_number = state.get("pr_number")

    if not owner or not repo_name or not pr_number:
        return {
            "errors": ["context: owner, repo_name and pr_number are all required."],
            "review_status": "failed",
        }

    repo_full_name = "{owner}/{repo}".format(owner=owner, repo=repo_name)

    try:
        pr = get_pr(repo_full_name, pr_number)
        raw_diff = get_pr_diff(pr)
        changed_files_data = get_changed_files(pr)
        commit_messages = get_commit_messages(pr)
        pr_meta = get_pr_metadata(pr)
    except Exception as exc:  # network, auth, missing PR, ...
        logger.exception("Failed to fetch PR %s#%s", repo_full_name, pr_number)
        return {
            "errors": ["context: could not fetch PR - {exc}".format(exc=exc)],
            "review_status": "failed",
        }

    changed_files = [ChangedFile(**file) for file in changed_files_data]
    pr_metadata = PRMetadata(**pr_meta)

    parsed_diff = parse_changed_files(changed_files)

    logger.info(
        "Fetched %s: %s changed files (%s reviewable), %s commits",
        repo_full_name, len(changed_files), len(parsed_diff), len(commit_messages),
    )

    return {
        "raw_diff": raw_diff,
        "changed_files": changed_files,
        "commit_messages": commit_messages,
        "pr_metadata": pr_metadata,
        "pr_intent": _infer_intent(pr_metadata, commit_messages),
        "parsed_diff": parsed_diff,
        "file_summaries": build_file_summaries(parsed_diff),
        "language_detected": dominant_language(parsed_diff),
        "errors": [],
    }
