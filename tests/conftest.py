"""Shared fixtures: fake GitHub objects and a fake LLM, so tests never hit the network."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from graph.state import ChangedFile, ParsedDiff, PRMetadata  # noqa: E402

SAMPLE_PATCH = """@@ -1,4 +1,8 @@
 import os
+
+def run(user_input):
+    return eval(user_input)
+
 def main():
     pass
"""


@pytest.fixture
def changed_files():
    return [
        ChangedFile(
            filename="app/handlers.py", status="modified",
            additions=4, deletions=0, changes=4, patch=SAMPLE_PATCH,
        ),
        ChangedFile(
            filename="package-lock.json", status="modified",
            additions=900, deletions=12, changes=912, patch="@@\n+noise",
        ),
        ChangedFile(
            filename="web/App.tsx", status="added",
            additions=2, deletions=0, changes=2, patch="@@ -0,0 +1,2 @@\n+const a = 1;\n+export default a;",
        ),
    ]


@pytest.fixture
def parsed_diff():
    return [
        ParsedDiff(
            filename="app/handlers.py", language="Python", diff=SAMPLE_PATCH,
            summary="modified", added_lines=4, removed_lines=0,
        )
    ]


@pytest.fixture
def pr_metadata():
    return PRMetadata(
        title="Add dynamic handler",
        description="Adds a handler that evaluates user input.",
        author="octocat",
        source_branch="feature/handler",
        target_branch="main",
        commit_sha="abc1234",
        url="https://github.com/acme/widgets/pull/7",
    )


@pytest.fixture
def base_state(parsed_diff, pr_metadata):
    return {
        "owner": "acme",
        "repo_name": "widgets",
        "pr_number": 7,
        "parsed_diff": parsed_diff,
        "pr_metadata": pr_metadata,
        "pr_intent": "Adds a handler that evaluates user input.",
        "language_detected": "Python",
        "file_summaries": ["app/handlers.py [Python] +4/-0"],
        "commit_messages": ["add handler"],
        "errors": [],
    }


class FakePR:
    """Minimal stand-in for github.PullRequest.PullRequest."""

    def __init__(self, patch=SAMPLE_PATCH):
        self.url = "https://api.github.com/repos/acme/widgets/pulls/7"
        self.html_url = "https://github.com/acme/widgets/pull/7"
        self.title = "Add dynamic handler"
        self.body = "Adds a handler that evaluates user input."
        self.user = SimpleNamespace(login="octocat")
        self.head = SimpleNamespace(ref="feature/handler", sha="abc1234")
        self.base = SimpleNamespace(ref="main")
        self._patch = patch
        self.posted = []

    def get_files(self):
        return [
            SimpleNamespace(
                filename="app/handlers.py", status="modified",
                additions=4, deletions=0, changes=4, patch=self._patch,
            )
        ]

    def get_commits(self):
        return [SimpleNamespace(commit=SimpleNamespace(message="add handler\n\ndetails"))]

    def get_issue_comments(self):
        return []

    def create_issue_comment(self, body):
        self.posted.append(body)
        return SimpleNamespace(
            html_url="https://github.com/acme/widgets/pull/7#issuecomment-1", body=body
        )


@pytest.fixture
def fake_pr():
    return FakePR()
