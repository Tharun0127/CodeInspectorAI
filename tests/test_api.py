"""Tests for the FastAPI surface: health, manual trigger and the GitHub webhook."""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import app.github as github_module
import app.main as main_module

client = TestClient(main_module.app)


@pytest.fixture(autouse=True)
def captured_reviews(monkeypatch):
    """Replace the background worker so no review actually runs."""
    calls = []
    monkeypatch.setattr(main_module, "_run_review_task",
                        lambda payload, *args: calls.append(payload))
    return calls


def pr_payload(action="opened", draft=False, number=7):
    return {
        "action": action,
        "pull_request": {"number": number, "draft": draft, "title": "Add handler"},
        "repository": {"name": "widgets", "owner": {"login": "acme"}},
    }


class TestBasics:
    def test_root(self):
        assert client.get("/api").json()["service"] == "CodeInspectorAI"

    def test_health_reports_configuration(self):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert "model" in body
        assert isinstance(body["groq_key_configured"], bool)


class TestManualReview:
    def test_queues_a_review(self, monkeypatch, captured_reviews):
        monkeypatch.setattr(main_module, "GROQ_API_KEY", "gsk_test")
        response = client.post(
            "/review",
            json={"owner": "acme", "repo_name": "widgets", "pr_number": 7,
                  "post_comment": False},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "queued"
        assert captured_reviews == [
            {"owner": "acme", "repo_name": "widgets", "pr_number": 7,
             "post_comment": False}
        ]

    def test_rejects_invalid_pr_number(self, monkeypatch):
        monkeypatch.setattr(main_module, "GROQ_API_KEY", "gsk_test")
        response = client.post(
            "/review", json={"owner": "acme", "repo_name": "widgets", "pr_number": 0}
        )
        assert response.status_code == 422

    def test_503_without_a_groq_key(self, monkeypatch):
        monkeypatch.setattr(main_module, "GROQ_API_KEY", "")
        response = client.post(
            "/review", json={"owner": "acme", "repo_name": "widgets", "pr_number": 7}
        )
        assert response.status_code == 503


class TestWebhook:
    def test_ping_is_answered(self):
        response = client.post("/webhook", json={"zen": "hi"},
                               headers={"X-GitHub-Event": "ping"})
        assert response.json()["status"] == "pong"

    def test_pull_request_opened_queues_review(self, captured_reviews):
        response = client.post("/webhook", json=pr_payload(),
                               headers={"X-GitHub-Event": "pull_request"})
        assert response.json()["status"] == "queued"
        assert captured_reviews == [
            {"owner": "acme", "repo_name": "widgets", "pr_number": 7}
        ]

    def test_synchronize_also_triggers(self, captured_reviews):
        client.post("/webhook", json=pr_payload(action="synchronize"),
                    headers={"X-GitHub-Event": "pull_request"})
        assert len(captured_reviews) == 1

    def test_irrelevant_action_ignored(self, captured_reviews):
        response = client.post("/webhook", json=pr_payload(action="labeled"),
                               headers={"X-GitHub-Event": "pull_request"})
        assert response.json()["status"] == "ignored"
        assert captured_reviews == []

    def test_draft_pr_ignored(self, captured_reviews):
        response = client.post("/webhook", json=pr_payload(draft=True),
                               headers={"X-GitHub-Event": "pull_request"})
        assert response.json()["status"] == "ignored"
        assert captured_reviews == []

    def test_other_events_ignored(self, captured_reviews):
        response = client.post("/webhook", json={"action": "created"},
                               headers={"X-GitHub-Event": "issue_comment"})
        assert response.json()["status"] == "ignored"
        assert captured_reviews == []

    def test_incomplete_payload_is_400(self):
        response = client.post("/webhook", json={"action": "opened"},
                               headers={"X-GitHub-Event": "pull_request"})
        assert response.status_code == 400

    def test_bad_signature_is_rejected(self, monkeypatch, captured_reviews):
        monkeypatch.setattr(main_module, "verify_webhook_signature", lambda *_: False)
        response = client.post("/webhook", json=pr_payload(),
                               headers={"X-GitHub-Event": "pull_request",
                                        "X-Hub-Signature-256": "sha256=wrong"})
        assert response.status_code == 401
        assert captured_reviews == []

    def test_valid_signature_is_accepted(self, monkeypatch, captured_reviews):
        secret = "s3cr3t"
        monkeypatch.setattr(github_module, "GITHUB_WEBHOOK_SECRET", secret)
        body = json.dumps(pr_payload()).encode()
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        response = client.post(
            "/webhook", content=body,
            headers={"X-GitHub-Event": "pull_request",
                     "Content-Type": "application/json",
                     "X-Hub-Signature-256": "sha256=" + digest},
        )
        assert response.status_code == 200
        assert len(captured_reviews) == 1


class TestSignatureVerification:
    def test_no_secret_configured_allows_everything(self, monkeypatch):
        monkeypatch.setattr(github_module, "GITHUB_WEBHOOK_SECRET", "")
        assert github_module.verify_webhook_signature(b"anything", None)

    def test_missing_header_rejected_when_secret_set(self, monkeypatch):
        monkeypatch.setattr(github_module, "GITHUB_WEBHOOK_SECRET", "s3cr3t")
        assert not github_module.verify_webhook_signature(b"body", None)
        assert not github_module.verify_webhook_signature(b"body", "sha1=abc")

    def test_correct_digest_accepted(self, monkeypatch):
        secret, body = "s3cr3t", b"body"
        monkeypatch.setattr(github_module, "GITHUB_WEBHOOK_SECRET", secret)
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert github_module.verify_webhook_signature(body, "sha256=" + digest)
