"""Tests for GitHub App support: installation tokens, per-review identity and install events."""

import datetime as dt
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.github as github_module
import app.main as main_module
import app.runner as runner_module
import graph.nodes.context as context_node
import graph.nodes.post_review as post_node
from app.store import ReviewStore

from test_graph import wire_fakes

client = TestClient(main_module.app)


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = ReviewStore(tmp_path / "reviews.db")
    for module in (runner_module, main_module):
        monkeypatch.setattr(module, "get_store", lambda: s)
    return s


@pytest.fixture
def app_env(monkeypatch):
    """Pretend a GitHub App is configured and GitHub mints tokens on request."""
    monkeypatch.setattr(github_module.config, "GITHUB_APP_ID", "12345")
    monkeypatch.setattr(github_module.config, "GITHUB_APP_PRIVATE_KEY", "-----BEGIN KEY-----")
    monkeypatch.setattr(github_module, "GITHUB_TOKEN", "")
    github_module._token_cache.clear()
    minted = []

    class FakeIntegration:
        def get_access_token(self, installation_id):
            minted.append(installation_id)
            return SimpleNamespace(
                token="ghs_inst_%d_%d" % (installation_id, len(minted)),
                expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
            )

        def get_repo_installation(self, owner, repo):
            if repo == "not-installed":
                raise github_module.GithubException(404, {"message": "Not Found"}, None)
            return SimpleNamespace(id=77)

    monkeypatch.setattr(github_module, "_integration", lambda: FakeIntegration())
    yield minted
    github_module._token_cache.clear()


class TestTokens:
    def test_not_configured_means_no_app_token(self):
        assert github_module.app_configured() is False
        assert runner_module.resolve_token("acme", "widgets", 1) is None

    def test_tokens_are_cached_until_near_expiry(self, app_env):
        first = github_module.get_installation_token(42)
        assert github_module.get_installation_token(42) == first
        assert app_env == [42]

    def test_expiring_token_is_refreshed(self, app_env):
        github_module._token_cache[42] = ("ghs_old", 0)
        assert github_module.get_installation_token(42) != "ghs_old"

    def test_forget_installation_drops_the_cache(self, app_env):
        github_module.get_installation_token(42)
        github_module.forget_installation(42)
        github_module.get_installation_token(42)
        assert app_env == [42, 42]

    def test_installation_looked_up_from_repo(self, app_env):
        assert runner_module.resolve_token("acme", "widgets").startswith("ghs_inst_77")

    def test_repo_without_the_app_falls_back(self, app_env):
        assert runner_module.resolve_token("acme", "not-installed") is None

    def test_use_token_overrides_and_restores(self, monkeypatch):
        monkeypatch.setattr(github_module, "GITHUB_TOKEN", "ghp_global")
        with github_module.use_token("ghs_scoped"):
            assert github_module.current_token() == "ghs_scoped"
        assert github_module.current_token() == "ghp_global"

    def test_private_key_accepts_escaped_newlines(self, monkeypatch):
        import app.config as config_module
        monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "-----BEGIN-----\\nabc\\n-----END-----")
        assert config_module._read_private_key() == "-----BEGIN-----\nabc\n-----END-----"


class TestReviewRunsAsTheInstallation:
    def test_every_github_call_uses_the_installation_token(
        self, app_env, store, monkeypatch, fake_pr
    ):
        wire_fakes(monkeypatch, fake_pr)
        seen = {}

        def fetch(*_):
            seen["fetch"] = github_module.current_token()
            return fake_pr

        def post(pr, body):
            seen["post"] = github_module.current_token()
            return "https://github.com/acme/widgets/pull/7#issuecomment-1"

        monkeypatch.setattr(context_node, "get_pr", fetch)
        monkeypatch.setattr(post_node, "get_pr", lambda *_: fake_pr)
        monkeypatch.setattr(post_node, "post_pr_comment", post)

        rid = store.create(source="webhook", owner="acme", repo="widgets",
                           pr_number=7, is_public=True)
        runner_module.run_tracked(rid, {"owner": "acme", "repo_name": "widgets",
                                        "pr_number": 7, "post_comment": True,
                                        "installation_id": 42})

        assert seen["fetch"].startswith("ghs_inst_42")
        assert seen["post"] == seen["fetch"]
        assert store.get(rid)["result"]["posted_comment_url"].endswith("issuecomment-1")
        # The token is scoped to the review, not left behind for the next one.
        assert github_module.current_token() == ""


class TestPostCommentFallback:
    def test_uneditable_previous_comment_gets_a_new_one(self):
        class Locked:
            body = github_module.REVIEW_MARKER + " old"

            def edit(self, _):
                raise github_module.GithubException(403, {"message": "Forbidden"}, None)

        created = []
        pr = SimpleNamespace(
            get_issue_comments=lambda: [Locked()],
            create_issue_comment=lambda body: created.append(body) or SimpleNamespace(html_url="new"),
        )
        assert github_module.post_pr_comment(pr, "review") == "new"
        assert created


@pytest.fixture
def captured(monkeypatch):
    calls = []
    monkeypatch.setattr(main_module, "_run_review_task", lambda *args: calls.append(args))
    return calls


class TestWebhook:
    def _send(self, event, payload):
        return client.post("/webhook", json=payload, headers={"X-GitHub-Event": event})

    def test_pull_request_carries_its_installation(self, captured):
        res = self._send("pull_request", {
            "action": "opened",
            "installation": {"id": 42},
            "pull_request": {"number": 7, "draft": False},
            "repository": {"name": "widgets", "owner": {"login": "acme"}, "private": True},
        })
        assert res.json()["status"] == "queued"
        payload, source, is_public = captured[0]
        assert payload["installation_id"] == 42
        assert source == "webhook"
        assert is_public is False

    def test_install_and_uninstall_are_recorded(self, store, app_env):
        github_module.get_installation_token(42)
        install = {"id": 42, "account": {"login": "octo-org", "type": "Organization"},
                   "repository_selection": "selected"}

        assert self._send("installation", {"action": "created", "installation": install}).json()["state"] == "active"
        assert store.count_installations() == 1

        assert self._send("installation", {"action": "deleted", "installation": install}).json()["state"] == "removed"
        assert store.count_installations() == 0
        assert 42 not in github_module._token_cache

    def test_repo_changes_keep_the_install_active(self, store):
        res = self._send("installation_repositories", {
            "action": "added", "installation": {"id": 9, "account": {"login": "dev"}},
        })
        assert res.json()["state"] == "active"
        assert store.count_installations() == 1

    def test_installation_event_without_installation_is_400(self, store):
        assert self._send("installation", {"action": "created"}).status_code == 400


def test_app_jwt_is_signed_with_a_string_issuer(monkeypatch):
    """Regression: PyJWT >= 2.10 refuses an int "iss", which broke every token request."""
    import jwt as pyjwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption()).decode()
    monkeypatch.setattr(github_module.config, "GITHUB_APP_ID", "12345")
    monkeypatch.setattr(github_module.config, "GITHUB_APP_PRIVATE_KEY", pem)

    claims = pyjwt.decode(github_module._integration().auth.token, key.public_key(),
                          algorithms=["RS256"])
    assert claims["iss"] == "12345"
