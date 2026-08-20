"""Unit tests for the pure diff/verdict helpers."""

from graph.state import ReviewComment, SecurityIssue
from graph.utils import (
    build_diff_context,
    build_file_summaries,
    count_patch_lines,
    decide_verdict,
    detect_language,
    dominant_language,
    is_reviewable,
    parse_changed_files,
    sort_review_comments,
    sort_security_issues,
)


class TestLanguageDetection:
    def test_known_extensions(self):
        assert detect_language("app/main.py") == "Python"
        assert detect_language("web/App.tsx") == "TypeScript"
        assert detect_language("cmd/server.go") == "Go"

    def test_special_filenames(self):
        assert detect_language("Dockerfile") == "Dockerfile"
        assert detect_language("build/Dockerfile.prod") == "Dockerfile"
        assert detect_language("Makefile") == "Makefile"

    def test_unknown_returns_empty(self):
        assert detect_language("LICENSE") == ""
        assert detect_language("data.xyz") == ""
        assert detect_language("") == ""


class TestReviewableFilter:
    def test_lockfiles_and_binaries_skipped(self):
        assert not is_reviewable("package-lock.json")
        assert not is_reviewable("frontend/yarn.lock")
        assert not is_reviewable("dist/bundle.min.js")
        assert not is_reviewable("docs/logo.png")

    def test_source_files_kept(self):
        assert is_reviewable("app/main.py")
        assert is_reviewable("src/index.ts")


class TestPatchParsing:
    def test_counts_ignore_headers(self):
        patch = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n+extra\n context"
        assert count_patch_lines(patch) == (2, 1)

    def test_empty_patch(self):
        assert count_patch_lines("") == (0, 0)
        assert count_patch_lines(None) == (0, 0)

    def test_parse_drops_lockfiles(self, changed_files):
        parsed = parse_changed_files(changed_files)
        names = [item.filename for item in parsed]
        assert names == ["app/handlers.py", "web/App.tsx"]

    def test_parse_sets_language(self, changed_files):
        parsed = parse_changed_files(changed_files)
        assert parsed[0].language == "Python"
        assert parsed[1].language == "TypeScript"

    def test_dominant_language(self, changed_files):
        assert dominant_language(parse_changed_files(changed_files)) == "Python"

    def test_dominant_language_empty(self):
        assert dominant_language([]) == ""

    def test_file_summaries(self, changed_files):
        summaries = build_file_summaries(parse_changed_files(changed_files))
        assert summaries[0].startswith("app/handlers.py [Python]")


class TestDiffContext:
    def test_includes_file_headers(self, parsed_diff):
        context = build_diff_context(parsed_diff, 10_000)
        assert "--- FILE: app/handlers.py (Python) ---" in context
        assert "eval(user_input)" in context

    def test_respects_budget_and_reports_omissions(self, changed_files):
        parsed = parse_changed_files(changed_files)
        context = build_diff_context(parsed, 80)
        assert "NOT SHOWN IN THIS DIFF" in context

    def test_zero_budget(self, parsed_diff):
        assert build_diff_context(parsed_diff, 0) == ""

    def test_no_files(self):
        assert build_diff_context([], 1000) == ""


class TestVerdict:
    def test_clean_pr_is_approved(self):
        assert decide_verdict([], [], 9.0) == "approved"

    def test_high_security_issue_blocks(self):
        issue = SecurityIssue(file="a.py", severity="high", issue="x", recommendation="y")
        assert decide_verdict([], [issue], 9.5) == "changes_requested"

    def test_critical_review_comment_blocks(self):
        comment = ReviewComment(file="a.py", severity="critical", message="broken")
        assert decide_verdict([comment], [], 9.5) == "changes_requested"

    def test_low_score_blocks(self):
        comment = ReviewComment(file="a.py", severity="info", message="nit")
        assert decide_verdict([comment], [], 3.0) == "changes_requested"

    def test_minor_findings_approve_with_comments(self):
        comment = ReviewComment(file="a.py", severity="warning", message="nit")
        issue = SecurityIssue(file="a.py", severity="low", issue="x", recommendation="y")
        assert decide_verdict([comment], [issue], 8.0) == "approved_with_comments"


class TestSorting:
    def test_review_comments_most_severe_first(self):
        comments = [
            ReviewComment(file="a", severity="info", message="1"),
            ReviewComment(file="b", severity="critical", message="2"),
            ReviewComment(file="c", severity="warning", message="3"),
        ]
        assert [c.severity for c in sort_review_comments(comments)] == [
            "critical", "warning", "info",
        ]

    def test_security_issues_most_severe_first(self):
        issues = [
            SecurityIssue(file="a", severity="low", issue="1", recommendation="r"),
            SecurityIssue(file="b", severity="critical", issue="2", recommendation="r"),
            SecurityIssue(file="c", severity="medium", issue="3", recommendation="r"),
        ]
        assert [i.severity for i in sort_security_issues(issues)] == [
            "critical", "medium", "low",
        ]
