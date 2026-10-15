"""
End-to-end tests for the compiled LangGraph workflow.

The graph is exercised for real - only the GitHub API and the LLM are faked -
so the wiring, the parallel fan-out and the state reducers are all covered.

To run a review against a real PR instead, use the CLI:
    python -m app.cli owner/repo 42 --dry-run
"""

import graph.graph as graph_module
import graph.nodes.context as context_node
import graph.nodes.post_review as post_node
import graph.nodes.review as review_node
import graph.nodes.security as security_node
import graph.nodes.summary as summary_node
from graph.state import ReviewComment, ReviewResult, SecurityIssue, SecurityResult

from test_nodes import FakeMessage, canned, stub_structured


def wire_fakes(monkeypatch, fake_pr, review_result=None, security_result=None):
    """Patch every external dependency the graph reaches for."""
    monkeypatch.setattr(context_node, "get_pr", lambda *_: fake_pr)
    monkeypatch.setattr(context_node, "get_pr_diff", lambda _: "raw diff")

    stub_structured(monkeypatch, review_node, review_result or ReviewResult())
    stub_structured(monkeypatch, security_node, security_result or SecurityResult())
    monkeypatch.setattr(
        summary_node, "get_llm",
        lambda **kwargs: canned(FakeMessage("Narrative summary of the change.")),
    )


class TestGraphStructure:
    def test_compiles_with_all_five_nodes(self):
        nodes = set(graph_module.graph.get_graph().nodes)
        for name in (
            "fetch_context", "code_review", "security_scan",
            "generate_summary", "post_review",
        ):
            assert name in nodes

    def test_router_fans_out_on_success(self):
        assert graph_module.route_after_context({"review_status": "success"}) == [
            "code_review", "security_scan",
        ]

    def test_router_aborts_on_fetch_failure(self):
        assert graph_module.route_after_context(
            {"review_status": "failed", "errors": ["boom"]}
        ) == "__end__"


class TestFullRun:
    def test_clean_pr_end_to_end(self, monkeypatch, fake_pr):
        wire_fakes(monkeypatch, fake_pr, ReviewResult(code_quality_score=9.0))

        result = graph_module.run_pr_review(
            {"owner": "acme", "repo_name": "widgets", "pr_number": 7,
             "post_comment": False}
        )

        assert result["review_status"] == "success"
        assert result["overall_verdict"] == "approved"
        assert result["code_quality_score"] == 9.0
        assert result["language_detected"] == "Python"
        assert "CodeInspectorAI Review" in result["formatted_comment"]
        assert result["errors"] == []

    def test_problem_pr_requests_changes(self, monkeypatch, fake_pr):
        wire_fakes(
            monkeypatch, fake_pr,
            ReviewResult(
                comments=[ReviewComment(file="app/handlers.py", line=4,
                                        severity="critical",
                                        message="eval on untrusted input")],
                code_quality_score=3.0,
                suggestions=["Never call eval on request data"],
            ),
            SecurityResult(
                issues=[SecurityIssue(file="app/handlers.py", line=4,
                                      severity="critical",
                                      issue="Remote code execution",
                                      recommendation="Use ast.literal_eval")]
            ),
        )

        result = graph_module.run_pr_review(
            {"owner": "acme", "repo_name": "widgets", "pr_number": 7,
             "post_comment": False}
        )

        assert result["overall_verdict"] == "changes_requested"
        assert len(result["review_comments"]) == 1
        assert len(result["security_issues"]) == 1
        assert "Remote code execution" in result["formatted_comment"]
        assert result["key_recommendations"]

    def test_both_branches_contribute_to_state(self, monkeypatch, fake_pr):
        """Review and security run in the same superstep; neither may clobber the other."""
        wire_fakes(
            monkeypatch, fake_pr,
            ReviewResult(comments=[ReviewComment(file="a.py", severity="warning",
                                                 message="nit")],
                         code_quality_score=7.0),
            SecurityResult(issues=[SecurityIssue(file="a.py", severity="low",
                                                 issue="weak hash",
                                                 recommendation="use sha256")]),
        )

        result = graph_module.run_pr_review(
            {"owner": "acme", "repo_name": "widgets", "pr_number": 7,
             "post_comment": False}
        )

        assert result["review_comments"] and result["security_issues"]
        assert result["overall_verdict"] == "approved_with_comments"

    def test_posts_comment_when_enabled(self, monkeypatch, fake_pr):
        wire_fakes(monkeypatch, fake_pr, ReviewResult(code_quality_score=8.0))
        monkeypatch.setattr(post_node, "current_token", lambda: "ghp_test")
        monkeypatch.setattr(post_node, "get_pr", lambda *_: fake_pr)

        result = graph_module.run_pr_review(
            {"owner": "acme", "repo_name": "widgets", "pr_number": 7,
             "post_comment": True}
        )

        assert result["posted_comment_url"].endswith("issuecomment-1")
        assert len(fake_pr.posted) == 1
        assert "CodeInspectorAI Review" in fake_pr.posted[0]


class TestFailureHandling:
    def test_fetch_failure_short_circuits_the_llm_nodes(self, monkeypatch):
        def boom(*_args):
            raise RuntimeError("repo not found")

        monkeypatch.setattr(context_node, "get_pr", boom)

        def never(*_args, **_kwargs):
            raise AssertionError("LLM must not run after a failed fetch")

        monkeypatch.setattr(review_node, "invoke_structured", never)
        monkeypatch.setattr(security_node, "invoke_structured", never)

        result = graph_module.run_pr_review(
            {"owner": "acme", "repo_name": "ghost", "pr_number": 1}
        )

        assert result["review_status"] == "failed"
        assert "repo not found" in result["errors"][0]
        assert not result.get("formatted_comment")

    def test_llm_failure_still_produces_a_review(self, monkeypatch, fake_pr):
        wire_fakes(monkeypatch, fake_pr)
        stub_structured(monkeypatch, review_node, RuntimeError("groq 503"))

        result = graph_module.run_pr_review(
            {"owner": "acme", "repo_name": "widgets", "pr_number": 7,
             "post_comment": False}
        )

        assert result["formatted_comment"]
        assert any("groq 503" in error for error in result["errors"])
        assert result["review_status"] == "partial"

    def test_missing_inputs_do_not_crash(self):
        result = graph_module.run_pr_review({"owner": "acme"})
        assert result["review_status"] == "failed"
