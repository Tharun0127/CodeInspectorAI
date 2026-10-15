"""Node-level tests. Every LLM and GitHub call is monkeypatched."""

import pytest
from langchain_core.runnables import RunnableLambda

import graph.nodes.context as context_node
import graph.nodes.post_review as post_node
import graph.nodes.review as review_node
import graph.nodes.security as security_node
import graph.nodes.summary as summary_node
from graph.state import (
    ParsedDiff,
    ReviewComment,
    ReviewResult,
    SecurityIssue,
    SecurityResult,
)


def canned(result):
    """A Runnable that returns `result`, or raises it when it is an exception."""

    def _invoke(_payload):
        if isinstance(result, Exception):
            raise result
        return result

    return RunnableLambda(_invoke)


def stub_structured(monkeypatch, module, result):
    """Replace `invoke_structured` in a node module with a canned answer."""

    def _fake(_prompt, _schema, _variables, **_kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(module, "invoke_structured", _fake)


# ------------------------------------------------------------
# Context node
# ------------------------------------------------------------
class TestContextNode:
    def test_missing_inputs_fail_fast(self):
        result = context_node.fetch_context({"owner": "acme"})
        assert result["review_status"] == "failed"
        assert result["errors"]

    def test_builds_context_from_pr(self, monkeypatch, fake_pr):
        monkeypatch.setattr(context_node, "get_pr", lambda *_: fake_pr)
        monkeypatch.setattr(context_node, "get_pr_diff", lambda _: "raw diff")

        result = context_node.fetch_context(
            {"owner": "acme", "repo_name": "widgets", "pr_number": 7}
        )

        assert result["raw_diff"] == "raw diff"
        assert result["language_detected"] == "Python"
        assert [f.filename for f in result["changed_files"]] == ["app/handlers.py"]
        assert result["commit_messages"] == ["add handler"]
        assert result["pr_metadata"].author == "octocat"
        assert result["file_summaries"][0].startswith("app/handlers.py")

    def test_intent_falls_back_to_commits(self, monkeypatch, fake_pr):
        fake_pr.body = None  # GitHub returns None for an empty description
        monkeypatch.setattr(context_node, "get_pr", lambda *_: fake_pr)
        monkeypatch.setattr(context_node, "get_pr_diff", lambda _: "")

        result = context_node.fetch_context(
            {"owner": "acme", "repo_name": "widgets", "pr_number": 7}
        )
        assert result["pr_intent"].startswith("Inferred from commits:")

    def test_github_failure_is_reported_not_raised(self, monkeypatch):
        def boom(*_args):
            raise RuntimeError("404 Not Found")

        monkeypatch.setattr(context_node, "get_pr", boom)
        result = context_node.fetch_context(
            {"owner": "acme", "repo_name": "widgets", "pr_number": 999}
        )
        assert result["review_status"] == "failed"
        assert "404" in result["errors"][0]


# ------------------------------------------------------------
# Review node
# ------------------------------------------------------------
class TestReviewNode:
    def test_returns_sorted_comments(self, monkeypatch, base_state):
        stub_structured(
            monkeypatch,
            review_node,
            ReviewResult(
                comments=[
                    ReviewComment(file="app/handlers.py", severity="info", message="nit"),
                    ReviewComment(
                        file="app/handlers.py",
                        severity="critical",
                        message="eval on user input",
                    ),
                ],
                code_quality_score=4.25,
                suggestions=["Add tests"],
            ),
        )

        result = review_node.code_review(base_state)

        assert result["review_comments"][0].severity == "critical"
        assert result["code_quality_score"] == 4.2  # rounded to 1dp
        assert result["suggestions"] == ["Add tests"]
        assert result["errors"] == []

    def test_empty_diff_skips_llm(self, monkeypatch, base_state):
        def explode(*_args, **_kwargs):
            raise AssertionError("LLM should not be called for an empty diff")

        monkeypatch.setattr(review_node, "invoke_structured", explode)
        result = review_node.code_review({**base_state, "parsed_diff": []})
        assert result["review_comments"] == []

    def test_llm_failure_is_captured(self, monkeypatch, base_state):
        stub_structured(monkeypatch, review_node, RuntimeError("groq down"))
        result = review_node.code_review(base_state)
        assert result["review_comments"] == []
        assert "groq down" in result["errors"][0]


# ------------------------------------------------------------
# Security node
# ------------------------------------------------------------
SECRET_ADDED = (
    "@@ -0,0 +1 @@\n"
    '+AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMIK7MDENGbPxRfiCY"'
)
SECRET_REMOVED = (
    "@@ -1 +1 @@\n"
    '-AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMIK7MDENGbPxRfiCY"\n'
    "+x = 1"
)
TOKEN_ADDED = (
    "@@ -0,0 +1 @@\n"
    '+github_token = "ghp_abcdefghijklmnopqrstuvwxyz0123"'
)


class TestSecurityNode:
    def test_regex_catches_hardcoded_secret(self):
        findings = security_node.scan_for_secrets(
            [ParsedDiff(filename="config.py", diff=SECRET_ADDED)]
        )
        assert len(findings) == 1
        assert findings[0].severity == "critical"

    @pytest.mark.parametrize(
        "line",
        [
            '+DB_PASSWORD = "SuperSecret123!"',       # underscore prefix
            '+apiKey: "abcdefghijklmnop"',            # camelCase
            "+password = 'hunter2hunter2'",           # single quotes
            '+AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMIK7MDENGbPxRfiCY"',
            '+token = "ghp_abcdefghijklmnopqrstuvwxyz0123"',
        ],
    )
    def test_regex_catches_credential_shapes(self, line):
        findings = security_node.scan_for_secrets(
            [ParsedDiff(filename="config.py", diff="@@ -0,0 +1 @@\n" + line)]
        )
        assert len(findings) == 1, "missed: " + line

    @pytest.mark.parametrize(
        "line",
        [
            '+password = os.environ["DB_PASSWORD"]',  # read from the environment
            '+token = ""',                            # empty placeholder
            "+# password = ...",                      # not an assignment
        ],
    )
    def test_regex_ignores_safe_lines(self, line):
        findings = security_node.scan_for_secrets(
            [ParsedDiff(filename="config.py", diff="@@ -0,0 +1 @@\n" + line)]
        )
        assert findings == [], "false positive: " + line

    def test_regex_ignores_unchanged_lines(self):
        findings = security_node.scan_for_secrets(
            [ParsedDiff(filename="config.py", diff=SECRET_REMOVED)]
        )
        assert findings == []

    def test_merges_llm_and_regex_findings(self, monkeypatch, base_state):
        stub_structured(
            monkeypatch,
            security_node,
            SecurityResult(
                issues=[
                    SecurityIssue(
                        file="app/handlers.py",
                        line=4,
                        severity="critical",
                        issue="Arbitrary code execution via eval",
                        recommendation="Use ast.literal_eval",
                    )
                ]
            ),
        )

        result = security_node.security_scan(base_state)

        assert len(result["security_issues"]) == 1
        assert result["severity_levels"] == ["critical"]
        assert result["vulnerable_lines"] == [4]

    def test_deduplicates_repeats(self, monkeypatch, base_state):
        duplicate = SecurityIssue(
            file="a.py", severity="high", issue="SQL injection",
            recommendation="parameterise",
        )
        stub_structured(monkeypatch, security_node, SecurityResult(issues=[duplicate, duplicate]))
        result = security_node.security_scan(base_state)
        assert len(result["security_issues"]) == 1

    def test_llm_failure_keeps_regex_findings(self, monkeypatch, base_state):
        state = {
            **base_state,
            "parsed_diff": [ParsedDiff(filename="config.py", diff=TOKEN_ADDED)],
        }
        stub_structured(monkeypatch, security_node, RuntimeError("groq down"))

        result = security_node.security_scan(state)
        assert len(result["security_issues"]) == 1
        assert "groq down" in result["errors"][0]


# ------------------------------------------------------------
# Summary node
# ------------------------------------------------------------
class FakeMessage:
    def __init__(self, content):
        self.content = content


class TestSummaryNode:
    def _stub(self, monkeypatch, text):
        """The summary node pipes straight into the LLM, so return a message."""
        result = text if isinstance(text, Exception) else FakeMessage(text)
        monkeypatch.setattr(summary_node, "get_llm", lambda **kwargs: canned(result))

    def test_builds_formatted_comment(self, monkeypatch, base_state):
        self._stub(monkeypatch, "This PR adds a handler.")
        state = {
            **base_state,
            "review_comments": [
                ReviewComment(
                    file="app/handlers.py", line=4, severity="critical",
                    message="eval is unsafe", suggestion="use ast.literal_eval",
                )
            ],
            "security_issues": [
                SecurityIssue(
                    file="app/handlers.py", line=4, severity="critical",
                    issue="RCE", recommendation="remove eval",
                )
            ],
            "code_quality_score": 4.0,
            "suggestions": ["Add tests"],
        }

        result = summary_node.generate_summary(state)

        assert result["overall_verdict"] == "changes_requested"
        assert "CodeInspectorAI Review" in result["formatted_comment"]
        assert "Changes requested" in result["formatted_comment"]
        assert "eval is unsafe" in result["formatted_comment"]
        assert "RCE" in result["formatted_comment"]
        assert result["key_recommendations"]

    def test_clean_pr_is_approved(self, monkeypatch, base_state):
        self._stub(monkeypatch, "Looks good.")
        result = summary_node.generate_summary(
            {
                **base_state,
                "review_comments": [],
                "security_issues": [],
                "code_quality_score": 9.0,
            }
        )
        assert result["overall_verdict"] == "approved"
        assert "No security issues detected" in result["formatted_comment"]

    def test_pipe_characters_are_escaped_in_tables(self, monkeypatch, base_state):
        self._stub(monkeypatch, "n/a")
        state = {
            **base_state,
            "security_issues": [
                SecurityIssue(
                    file="a.py", severity="low",
                    issue="uses a | b pattern", recommendation="fix",
                )
            ],
            "code_quality_score": 8.0,
        }
        comment = summary_node.generate_summary(state)["formatted_comment"]
        assert "a \\| b" in comment

    def test_llm_failure_still_produces_comment(self, monkeypatch, base_state):
        self._stub(monkeypatch, RuntimeError("groq down"))
        result = summary_node.generate_summary({**base_state, "code_quality_score": 8.0})
        assert result["formatted_comment"]
        assert "groq down" in result["errors"][0]


# ------------------------------------------------------------
# Post review node
# ------------------------------------------------------------
class TestPostReviewNode:
    def test_dry_run_does_not_post(self, monkeypatch, base_state):
        monkeypatch.setattr(post_node, "get_pr", lambda *_: pytest.fail("should not post"))
        result = post_node.post_review(
            {**base_state, "formatted_comment": "hi", "post_comment": False}
        )
        assert result["posted_comment_url"] == ""
        assert result["review_status"] == "success"

    def test_missing_comment_fails(self, base_state):
        result = post_node.post_review({**base_state, "formatted_comment": ""})
        assert result["review_status"] == "failed"

    def test_posts_and_returns_url(self, monkeypatch, base_state, fake_pr):
        monkeypatch.setattr(post_node, "current_token", lambda: "ghp_test")
        monkeypatch.setattr(post_node, "get_pr", lambda *_: fake_pr)
        monkeypatch.setattr(post_node, "post_pr_comment", lambda pr, body: "https://x/1")

        result = post_node.post_review(
            {**base_state, "formatted_comment": "review body", "post_comment": True}
        )
        assert result["posted_comment_url"] == "https://x/1"
        assert result["review_status"] == "success"

    def test_missing_token_is_partial_not_crash(self, monkeypatch, base_state):
        monkeypatch.setattr(post_node, "current_token", lambda: "")
        result = post_node.post_review(
            {**base_state, "formatted_comment": "body", "post_comment": True}
        )
        assert result["review_status"] == "partial"
        assert "GITHUB_TOKEN" in result["errors"][0]

    def test_prior_errors_downgrade_status(self, monkeypatch, base_state):
        monkeypatch.setattr(post_node, "current_token", lambda: "ghp_test")
        monkeypatch.setattr(post_node, "get_pr", lambda *_: object())
        monkeypatch.setattr(post_node, "post_pr_comment", lambda pr, body: "https://x/1")

        result = post_node.post_review(
            {
                **base_state,
                "formatted_comment": "body",
                "post_comment": True,
                "errors": ["review: groq down"],
            }
        )
        assert result["review_status"] == "partial"
