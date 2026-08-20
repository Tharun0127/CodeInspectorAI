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
PROMPTS_DIR = BASE_DIR / "prompts"

# Load the project-level .env explicitly so imports work regardless of cwd.
load_dotenv(BASE_DIR / ".env")


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

# ---- Behaviour ----
POST_COMMENTS: bool = _env_bool("POST_COMMENTS", True)
MAX_DIFF_CHARS: int = _env_int("MAX_DIFF_CHARS", 15_000)

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
