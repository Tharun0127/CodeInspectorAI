"""
Shared Groq LLM access.

Nodes use `get_llm()` / `invoke_structured()` instead of constructing ChatGroq
themselves, so the model name lives in one place and every structured call gets
the same retry and fallback behaviour.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any, Dict, Optional, Type

from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq
from pydantic import BaseModel

from app.config import GROQ_MODEL, require_groq_key

logger = logging.getLogger(__name__)

SCHEMA_SLOT = "__schema_instructions__"

_JSON_MODE_INSTRUCTIONS = (
    "Respond with a single JSON object and nothing else. It must validate "
    "against this JSON schema:\n{schema}\n"
    "Use exactly these field names and only the allowed enum values."
)


@lru_cache(maxsize=8)
def get_llm(
    temperature: float = 0.2,
    max_tokens: Optional[int] = None,
    model: Optional[str] = None,
) -> ChatGroq:
    """Build (and cache) a ChatGroq client."""
    return ChatGroq(
        api_key=require_groq_key(),
        model=model or GROQ_MODEL,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=90,
        max_retries=2,
    )


def invoke_structured(
    prompt: ChatPromptTemplate,
    schema: Type[BaseModel],
    variables: Dict[str, Any],
    temperature: float = 0.2,
    max_tokens: Optional[int] = 4096,
) -> BaseModel:
    """
    Run `prompt` and parse the reply into `schema`.

    Tool calling is tried first because it is the most reliable, but Groq's
    open-weight models occasionally emit a malformed tool call (HTTP 400
    "attempted to call tool 'json'"). Rather than lose the whole node, the call
    is retried once and then repeated in JSON mode with the schema spelled out
    in the prompt. Only if all three attempts fail does the error propagate.
    """
    llm = get_llm(temperature=temperature, max_tokens=max_tokens)
    last_error: Optional[Exception] = None

    for attempt in (1, 2):
        try:
            return (prompt | llm.with_structured_output(schema)).invoke(variables)
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Structured call for %s failed (attempt %s/2): %s",
                schema.__name__, attempt, exc,
            )

    try:
        json_prompt = prompt + [("user", "{" + SCHEMA_SLOT + "}")]
        json_variables = dict(variables)
        json_variables[SCHEMA_SLOT] = _JSON_MODE_INSTRUCTIONS.format(
            schema=json.dumps(schema.model_json_schema(), indent=2)
        )
        result = (
            json_prompt | llm.with_structured_output(schema, method="json_mode")
        ).invoke(json_variables)
        logger.info("Recovered %s via JSON mode.", schema.__name__)
        return result
    except Exception as exc:
        logger.warning("JSON-mode fallback for %s also failed: %s", schema.__name__, exc)
        raise last_error or exc
