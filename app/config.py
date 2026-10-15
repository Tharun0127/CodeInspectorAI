"""
Central configuration for CodeInspectorAI.

Every setting is read from the environment (a local `.env` is loaded on import),
so nothing needs to be hardcoded in the nodes.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# Load the project-level .env explicitly so imports work regardless of cwd.
load_dotenv(BASE_DIR / ".env")

# Prompts are kept out of git, so deployments can mount them elsewhere.
PROMPTS_DIR = Path(os.getenv("PROMPTS_DIR", "").strip() or BASE_DIR / "prompts")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


# ---- Groq (LLM) ----
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()

# ---- GitHub ----
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_API_URL: str = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
GITHUB_WEBHOOK_SECRET: str = os.getenv("GITHUB_WEBHOOK_SECRET", "").strip()

# ---- GitHub App (optional, recommended) ----
# When set, every review authenticates as the app's installation on that repo,
# with a short-lived token, and comments appear as "<app>[bot]".
GITHUB_APP_ID: str = os.getenv("GITHUB_APP_ID", "").strip()
GITHUB_APP_SLUG: str = os.getenv("GITHUB_APP_SLUG", "").strip()


def _read_private_key() -> str:
    """PEM from GITHUB_APP_PRIVATE_KEY (literal or with \\n escapes) or a file path."""
    path = os.getenv("GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    # Many hosts only take single-line values, so accept a literal "\n" too.
    return os.getenv("GITHUB_APP_PRIVATE_KEY", "").strip().replace("\\n", "\n")


GITHUB_APP_PRIVATE_KEY: str = _read_private_key()

# ---- Behaviour ----
POST_COMMENTS: bool = _env_bool("POST_COMMENTS", True)
MAX_DIFF_CHARS: int = _env_int("MAX_DIFF_CHARS", 15_000)

# ---- Web UI / dashboard ----
# Review history lives in a SQLite file here. Mount a persistent disk in production.
DATA_DIR: Path = Path(os.getenv("DATA_DIR", "").strip() or BASE_DIR / "data")
# Unlocks private-repo reviews on the dashboard. Unset = public repos only.
DASHBOARD_TOKEN: str = os.getenv("DASHBOARD_TOKEN", "").strip()
# The landing-page "try it" box: always a dry run, public repos only.
DEMO_ENABLED: bool = _env_bool("DEMO_ENABLED", True)
DEMO_RATE_LIMIT: int = _env_int("DEMO_RATE_LIMIT", 5)  # per visitor per hour
REVIEW_CONCURRENCY: int = max(1, _env_int("REVIEW_CONCURRENCY", 2))

# PR events that should trigger a review.
TRIGGER_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}


def require_groq_key() -> str:
    """Return the Groq key or raise a clear error instead of failing deep in a node."""
    if not GROQ_API_KEY:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    return GROQ_API_KEY


def load_prompt(name: str, fallback: str = "") -> str:
    """Load a system prompt from prompts/<name>.md, falling back to a default."""
    path = PROMPTS_DIR / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return fallback
