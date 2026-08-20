"""
Tests for the resilient structured-output helper.

Groq's open-weight models occasionally return a malformed tool call, so
`invoke_structured` retries and then falls back to JSON mode. These tests pin
that behaviour down without touching the network.
"""

import pytest
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda

import app.llm as llm_module
from graph.state import ReviewComment, ReviewResult, SecurityIssue

PROMPT = ChatPromptTemplate.from_messages(
    [("system", "review things"), ("user", "diff: {diff}")]
)
VARIABLES = {"diff": "+ x = 1"}

TOOL_CALL_ERROR = RuntimeError(
    "Error code: 400 - attempted to call tool 'json' which was not in request.tools"
)


class RecordingLLM:
    """
    Fake ChatGroq: fails `tool_failures` times on tool calling, and optionally
    succeeds in JSON mode.
    """

    def __init__(self, tool_failures=0, json_mode_works=True, result=None):
        self.tool_failures = tool_failures
        self.json_mode_works = json_mode_works
        self.result = result if result is not None else ReviewResult(code_quality_score=8.0)
        self.tool_calls = 0
        self.json_calls = 0
        self.last_json_variables = None

    def with_structured_output(self, _schema, method=None):
        if method == "json_mode":
            def _json(payload):
                self.json_calls += 1
                self.last_json_variables = payload
                if not self.json_mode_works:
                    raise RuntimeError("json mode failed too")
                return self.result

            return RunnableLambda(_json)

        def _tool(_payload):
            self.tool_calls += 1
            if self.tool_calls <= self.tool_failures:
                raise TOOL_CALL_ERROR
            return self.result

        return RunnableLambda(_tool)


@pytest.fixture
def use_llm(monkeypatch):
    def _install(fake):
        monkeypatch.setattr(llm_module, "get_llm", lambda **kwargs: fake)
        return fake

    return _install


class TestInvokeStructured:
    def test_happy_path_uses_tool_calling_once(self, use_llm):
        fake = use_llm(RecordingLLM())
        result = llm_module.invoke_structured(PROMPT, ReviewResult, VARIABLES)
        assert result.code_quality_score == 8.0
        assert fake.tool_calls == 1
        assert fake.json_calls == 0

    def test_retries_a_transient_tool_call_failure(self, use_llm):
        fake = use_llm(RecordingLLM(tool_failures=1))
        result = llm_module.invoke_structured(PROMPT, ReviewResult, VARIABLES)
        assert result.code_quality_score == 8.0
        assert fake.tool_calls == 2
        assert fake.json_calls == 0

    def test_falls_back_to_json_mode(self, use_llm):
        fake = use_llm(RecordingLLM(tool_failures=2))
        result = llm_module.invoke_structured(PROMPT, ReviewResult, VARIABLES)
        assert result.code_quality_score == 8.0
        assert fake.tool_calls == 2
        assert fake.json_calls == 1

    def test_json_fallback_renders_the_schema_into_the_prompt(self, use_llm):
        fake = use_llm(RecordingLLM(tool_failures=2))
        llm_module.invoke_structured(PROMPT, ReviewResult, VARIABLES)

        rendered = "\n".join(m.content for m in fake.last_json_variables.to_messages())
        assert "code_quality_score" in rendered  # the schema was spelled out
        assert "JSON object" in rendered
        assert "+ x = 1" in rendered  # the original variables survived

    def test_original_error_is_raised_when_everything_fails(self, use_llm):
        use_llm(RecordingLLM(tool_failures=2, json_mode_works=False))
        with pytest.raises(RuntimeError, match="attempted to call tool"):
            llm_module.invoke_structured(PROMPT, ReviewResult, VARIABLES)

    def test_caller_variables_are_not_mutated(self, use_llm):
        use_llm(RecordingLLM(tool_failures=2))
        variables = dict(VARIABLES)
        llm_module.invoke_structured(PROMPT, ReviewResult, variables)
        assert variables == VARIABLES


class TestSeverityNormalisation:
    """The two severity vocabularies get mixed up by the model constantly."""

    def test_review_comment_accepts_security_words(self):
        assert ReviewComment(file="a", severity="high", message="m").severity == "critical"
        assert ReviewComment(file="a", severity="medium", message="m").severity == "warning"
        assert ReviewComment(file="a", severity="low", message="m").severity == "info"

    def test_security_issue_accepts_review_words(self):
        def severity(value):
            return SecurityIssue(file="a", severity=value, issue="i",
                                 recommendation="r").severity

        assert severity("warning") == "medium"
        assert severity("info") == "low"
        assert severity("severe") == "critical"

    def test_case_and_whitespace_tolerated(self):
        assert ReviewComment(file="a", severity=" CRITICAL ", message="m").severity == "critical"

    def test_unknown_value_falls_back_to_least_severe(self):
        assert ReviewComment(file="a", severity="spicy", message="m").severity == "info"
        assert SecurityIssue(file="a", severity="spicy", issue="i",
                             recommendation="r").severity == "low"

    def test_valid_values_pass_through(self):
        assert ReviewComment(file="a", severity="warning", message="m").severity == "warning"
        assert SecurityIssue(file="a", severity="high", issue="i",
                             recommendation="r").severity == "high"
