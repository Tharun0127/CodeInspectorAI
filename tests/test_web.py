"""Tests for the web UI backend: review store, tracked runner and the /api routes."""

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.runner as runner_module
import app.web as web_module
from app.store import ReviewStore
from graph.state import ReviewResult, SecurityIssue, SecurityResult

from test_graph import wire_fakes

client = TestClient(main_module.app)


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = ReviewStore(tmp_path / "reviews.db")
    for module in (runner_module, web_module, main_module):
        monkeypatch.setattr(module, "get_store", lambda: s)
    return s


@pytest.fixture
def demo_ready(monkeypatch, store):
    """Groq key set, every repo public, and jobs recorded instead of executed."""
    monkeypatch.setattr(web_module.config, "GROQ_API_KEY", "gsk_test")
    monkeypatch.setattr(web_module, "is_public_repo", lambda *_: True)
    submitted = []
    monkeypatch.setattr(web_module.executor, "submit",
                        lambda fn, *args: submitted.append(args))
    return submitted


class TestParsePrReference:
    @pytest.mark.parametrize("text", [
        "https://github.com/acme/widgets/pull/7",
        "github.com/acme/widgets/pull/7/files",
        "  https://www.github.com/acme/widgets/pull/7  ",
        "acme/widgets#7",
    ])
    def test_accepts_common_forms(self, text):
        assert web_module.parse_pr_reference(text) == ("acme", "widgets", 7)

    @pytest.mark.parametrize("text", [
        "https://gitlab.com/acme/widgets/pull/7",
        "https://github.com/acme/widgets/issues/7",
        "not a url",
    ])
    def test_rejects_everything_else(self, text):
        assert web_module.parse_pr_reference(text) is None


class TestStore:
    def test_round_trip(self, store):
        rid = store.create(source="demo", owner="acme", repo="widgets",
                           pr_number=7, is_public=True, client_hash="abc")
        store.update(rid, status="done", steps={"fetch_context": {"finished": 1.0}},
                     result={"overall_verdict": "approved"})
        row = store.get(rid)
        assert row["status"] == "done"
        assert row["steps"]["fetch_context"]["finished"] == 1.0
        assert row["result"]["overall_verdict"] == "approved"
        assert "client_hash" not in row

    def test_private_rows_hidden_by_default(self, store):
        store.create(source="webhook", owner="a", repo="secret", pr_number=1, is_public=False)
        store.create(source="webhook", owner="a", repo="open", pr_number=2, is_public=True)
        assert [r["repo"] for r in store.list()] == ["open"]
        assert len(store.list(include_private=True)) == 2

    def test_restart_fails_in_flight_reviews(self, tmp_path):
        path = tmp_path / "db.sqlite"
        rid = ReviewStore(path).create(source="api", owner="a", repo="b",
                                       pr_number=1, is_public=True)
        assert ReviewStore(path).get(rid)["status"] == "failed"


class TestRunner:
    def test_records_every_step_and_the_result(self, monkeypatch, fake_pr, store):
        wire_fakes(
            monkeypatch, fake_pr, ReviewResult(code_quality_score=6.5),
            SecurityResult(issues=[SecurityIssue(
                file="app/handlers.py", line=4, severity="critical",
                issue="eval on user input", recommendation="use ast.literal_eval")]),
        )
        rid = store.create(source="demo", owner="acme", repo="widgets",
                           pr_number=7, is_public=True)
        runner_module.run_tracked(rid, {"owner": "acme", "repo_name": "widgets",
                                        "pr_number": 7, "post_comment": False})

        row = store.get(rid)
        assert row["status"] == "done"
        assert set(row["steps"]) == set(runner_module.PIPELINE)
        result = row["result"]
        assert result["overall_verdict"] == "changes_requested"
        assert result["security_issues"][0]["issue"] == "eval on user input"
        assert result["code_quality_score"] == 6.5
        assert result["files_changed"] == len(fake_pr.get_files())
        assert "raw_diff" not in result

    def test_fetch_failure_is_recorded_as_failed(self, monkeypatch, store):
        import graph.nodes.context as context_node

        def boom(*_):
            raise RuntimeError("404 Not Found")

        monkeypatch.setattr(context_node, "get_pr", boom)
        rid = store.create(source="api", owner="a", repo="b", pr_number=1, is_public=True)
        runner_module.run_tracked(rid, {"owner": "a", "repo_name": "b", "pr_number": 1})

        row = store.get(rid)
        assert row["status"] == "failed"
        assert list(row["steps"]) == ["fetch_context"]
        assert row["result"]["errors"]


class TestDemoEndpoint:
    def test_queues_a_dry_run(self, demo_ready, store):
        res = client.post("/api/reviews", json={"pr_url": "https://github.com/acme/widgets/pull/7"})
        assert res.status_code == 202
        review_id, state = demo_ready[0]
        assert review_id == res.json()["id"]
        assert state["post_comment"] is False
        assert store.get(review_id)["source"] == "demo"

    def test_bad_url_is_422(self, demo_ready):
        assert client.post("/api/reviews", json={"pr_url": "hello"}).status_code == 422

    def test_private_repo_is_refused(self, demo_ready, monkeypatch):
        monkeypatch.setattr(web_module, "is_public_repo", lambda *_: False)
        res = client.post("/api/reviews", json={"pr_url": "acme/secret#1"})
        assert res.status_code == 403
        assert not demo_ready

    def test_unknown_repo_is_404(self, demo_ready, monkeypatch):
        monkeypatch.setattr(web_module, "is_public_repo", lambda *_: None)
        assert client.post("/api/reviews", json={"pr_url": "acme/nope#1"}).status_code == 404

    def test_rate_limited_per_visitor(self, demo_ready, monkeypatch):
        monkeypatch.setattr(web_module.config, "DEMO_RATE_LIMIT", 2)
        codes = [client.post("/api/reviews", json={"pr_url": "acme/widgets#%d" % n}).status_code
                 for n in range(1, 4)]
        assert codes == [202, 202, 429]

    def test_disabled_demo_is_403(self, demo_ready, monkeypatch):
        monkeypatch.setattr(web_module.config, "DEMO_ENABLED", False)
        assert client.post("/api/reviews", json={"pr_url": "acme/widgets#1"}).status_code == 403

    def test_needs_a_groq_key(self, demo_ready, monkeypatch):
        monkeypatch.setattr(web_module.config, "GROQ_API_KEY", "")
        assert client.post("/api/reviews", json={"pr_url": "acme/widgets#1"}).status_code == 503


class TestDashboardApi:
    @pytest.fixture
    def seeded(self, store):
        public = store.create(source="webhook", owner="acme", repo="open", pr_number=1, is_public=True)
        private = store.create(source="webhook", owner="acme", repo="secret", pr_number=2, is_public=False)
        for rid, verdict, score in ((public, "approved", 9.0), (private, "changes_requested", 3.0)):
            store.update(rid, status="done", started_at=100.0, finished_at=110.0,
                         result={"overall_verdict": verdict, "code_quality_score": score,
                                 "review_comments": [{}], "security_issues": [],
                                 "language": "Python"})
        return public, private

    def test_list_hides_private_without_token(self, seeded):
        body = client.get("/api/reviews").json()
        assert body["admin"] is False
        assert [r["repo"] for r in body["reviews"]] == ["acme/open"]

    def test_token_unlocks_private(self, seeded, monkeypatch):
        monkeypatch.setattr(web_module.config, "DASHBOARD_TOKEN", "s3cret")
        body = client.get("/api/reviews", headers={"X-Dashboard-Token": "s3cret"}).json()
        assert body["admin"] is True
        assert len(body["reviews"]) == 2

    def test_wrong_token_does_not_unlock(self, seeded, monkeypatch):
        monkeypatch.setattr(web_module.config, "DASHBOARD_TOKEN", "s3cret")
        body = client.get("/api/reviews", headers={"X-Dashboard-Token": "nope"}).json()
        assert body["admin"] is False

    def test_private_review_detail_is_404(self, seeded):
        assert client.get("/api/reviews/" + seeded[1]).status_code == 404
        assert client.get("/api/reviews/" + seeded[0]).status_code == 200

    def test_stats_only_count_visible_reviews(self, seeded):
        stats = client.get("/api/stats").json()
        assert stats["total"] == 1
        assert stats["avg_score"] == 9.0
        assert stats["avg_duration"] == 10.0
        assert stats["verdicts"] == {"approved": 1}
        assert stats["score_buckets"] == [0, 0, 0, 0, 1]
        assert stats["per_day"][-1] == 1


class TestPages:
    def test_landing_and_dashboard_are_html(self):
        for path in ("/", "/dashboard"):
            res = client.get(path)
            assert res.status_code == 200
            assert "text/html" in res.headers["content-type"]

    def test_static_assets_are_served(self):
        for name in ("styles.css", "common.js", "landing.js", "dashboard.js"):
            assert client.get("/static/" + name).status_code == 200


class TestPublicConfig:
    def test_no_app_means_no_install_link(self, monkeypatch):
        monkeypatch.setattr(web_module.config, "GITHUB_APP_SLUG", "")
        body = client.get("/api/config").json()
        assert body["install_url"] == "" and body["bot_login"] == ""

    def test_app_slug_gives_install_link_and_bot_name(self, monkeypatch):
        monkeypatch.setattr(web_module.config, "GITHUB_APP_SLUG", "codeinspectorai")
        body = client.get("/api/config").json()
        assert body["install_url"] == "https://github.com/apps/codeinspectorai/installations/new"
        assert body["bot_login"] == "codeinspectorai[bot]"
