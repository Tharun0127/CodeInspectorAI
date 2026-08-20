"""
Main LangGraph workflow for the AI PR Reviewer.

    START -> fetch_context -+-> code_review ---+-> generate_summary -> post_review -> END
                            |                  |
                            +-> security_scan -+
                            |
                            +-> (abort on fetch failure) -> END
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Union

from langgraph.graph import END, StateGraph

from graph.nodes.context import fetch_context
from graph.nodes.post_review import post_review
from graph.nodes.review import code_review
from graph.nodes.security import security_scan
from graph.nodes.summary import generate_summary
from graph.state import ReviewState

logger = logging.getLogger(__name__)


def route_after_context(state: ReviewState) -> Union[str, List[str]]:
    """
    Fan out to the two analysis nodes, unless the PR could not be fetched.

    Without this guard a failed fetch would still burn two LLM calls on an
    empty diff and post an empty review.
    """
    if state.get("review_status") == "failed":
        logger.warning("Aborting review: %s", state.get("errors"))
        return END
    return ["code_review", "security_scan"]


def build_graph():
    """Build and compile the LangGraph workflow."""
    workflow = StateGraph(ReviewState)

    workflow.add_node("fetch_context", fetch_context)
    workflow.add_node("code_review", code_review)
    workflow.add_node("security_scan", security_scan)
    workflow.add_node("generate_summary", generate_summary)
    workflow.add_node("post_review", post_review)

    workflow.set_entry_point("fetch_context")

    # Parallel: review + security run in the same superstep after context.
    workflow.add_conditional_edges(
        "fetch_context",
        route_after_context,
        ["code_review", "security_scan", END],
    )

    # Both branches converge on the summary, which waits for each of them.
    workflow.add_edge("code_review", "generate_summary")
    workflow.add_edge("security_scan", "generate_summary")

    workflow.add_edge("generate_summary", "post_review")
    workflow.add_edge("post_review", END)

    return workflow.compile()


graph = build_graph()


def run_pr_review(initial_state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run the full PR review graph.

    `initial_state` needs `owner`, `repo_name` and `pr_number`; pass
    `post_comment=False` to generate the review without publishing it.
    """
    state: Dict[str, Any] = {
        "review_status": "success",
        "errors": [],
        **initial_state,
    }
    return graph.invoke(state)
