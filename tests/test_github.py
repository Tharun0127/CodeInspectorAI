"""Tests for the GitHub helper layer (no network calls)."""

from types import SimpleNamespace

import requests

import app.github as gh


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code
        self.ok = 200 <= status_code < 300


class TestMetadata:
    def test_extracts_fields(self, fake_pr):
        meta = gh.get_pr_metadata(fake_pr)
        assert meta["title"] == "Add dynamic handler"
        assert meta["author"] == "octocat"
        assert meta["source_branch"] == "feature/handler"
        assert meta["target_branch"] == "main"
        assert meta["commit_sha"] == "abc1234"

    def test_handles_empty_body_and_missing_user(self, fake_pr):
        fake_pr.body = None
        fake_pr.user = None
        meta = gh.get_pr_metadata(fake_pr)
        assert meta["description"] == "No description provided."
        assert meta["author"] == "unknown"


class TestCommitMessages:
    def test_uses_first_line_only(self, fake_pr):
        assert gh.get_commit_messages(fake_pr) == ["add handler"]

    def test_skips_empty_messages(self, fake_pr):
        fake_pr.get_commits = lambda: [
            SimpleNamespace(commit=SimpleNamespace(message="   ")),
            SimpleNamespace(commit=SimpleNamespace(message="real commit")),
        ]
        assert gh.get_commit_messages(fake_pr) == ["real commit"]


class TestDiffFetching:
    def test_uses_the_rest_diff_endpoint(self, monkeypatch, fake_pr):
        monkeypatch.setattr(
            gh.requests, "get", lambda *a, **k: FakeResponse("diff --git a/x b/x\n+1")
        )
        assert gh.get_pr_diff(fake_pr).startswith("diff --git")

    def test_falls_back_to_file_patches_on_error(self, monkeypatch, fake_pr):
        monkeypatch.setattr(gh.requests, "get", lambda *a, **k: FakeResponse("", 404))
        diff = gh.get_pr_diff(fake_pr)
        assert "diff --git a/app/handlers.py b/app/handlers.py" in diff
        assert "eval(user_input)" in diff

    def test_falls_back_when_the_request_raises(self, monkeypatch, fake_pr):
        def boom(*_a, **_k):
            raise requests.RequestException("timeout")

        monkeypatch.setattr(gh.requests, "get", boom)
        assert "app/handlers.py" in gh.get_pr_diff(fake_pr)

    def test_assemble_skips_files_without_a_patch(self):
        files = [
            {"filename": "logo.png", "patch": None},
            {"filename": "a.py", "patch": "@@\n+x = 1"},
        ]
        assembled = gh.assemble_diff_from_files(files)
        assert "logo.png" not in assembled
        assert "a.py" in assembled


class TestCommentPosting:
    def test_adds_the_marker(self, fake_pr):
        url = gh.post_pr_comment(fake_pr, "body text")
        assert url.endswith("issuecomment-1")
        assert gh.REVIEW_MARKER in fake_pr.posted[0]

    def test_edits_the_previous_review_instead_of_stacking(self, fake_pr):
        edited = {}

        class OldComment:
            body = gh.REVIEW_MARKER + "\nold review"
            html_url = "https://github.com/acme/widgets/pull/7#issuecomment-old"

            def edit(self, new_body):
                edited["body"] = new_body

        fake_pr.get_issue_comments = lambda: [OldComment()]

        url = gh.post_pr_comment(fake_pr, "new review")

        assert url.endswith("issuecomment-old")
        assert "new review" in edited["body"]
        assert fake_pr.posted == []  # nothing new was created

    def test_ignores_comments_from_other_authors(self, fake_pr):
        fake_pr.get_issue_comments = lambda: [
            SimpleNamespace(body="a human comment", html_url="x")
        ]
        gh.post_pr_comment(fake_pr, "new review")
        assert len(fake_pr.posted) == 1
