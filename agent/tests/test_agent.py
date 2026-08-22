from types import SimpleNamespace

import pytest

from respan_integration_agent.agent import (
    DEFAULT_RESPAN_BASE_URL,
    AgentRunError,
    _validate_terminal_result,
    _validated_respan_base_url,
)


def _result(**overrides):
    values = {
        "is_error": False,
        "subtype": "success",
        "terminal_reason": "completed",
        "permission_denials": [],
        "errors": [],
        "api_error_status": None,
        "deferred_tool_use": None,
        "result": "Applied the pinned integration.",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_accepts_completed_terminal_result():
    result = _result()
    assert _validate_terminal_result(result) is result


@pytest.mark.parametrize(
    "overrides",
    [
        {"is_error": True, "result": "API Error"},
        {"subtype": "error_max_turns"},
        {"terminal_reason": "max_turns"},
        {"terminal_reason": "aborted_streaming"},
        {"permission_denials": [{"tool": "Edit"}]},
        {"errors": ["transport failed"]},
        {"api_error_status": 429},
        {"deferred_tool_use": {"id": "tool-1", "name": "Read", "input": {}}},
        {"result": "   "},
    ],
)
def test_rejects_non_success_terminal_states(overrides):
    with pytest.raises(AgentRunError):
        _validate_terminal_result(_result(**overrides))


def test_rejects_missing_terminal_result():
    with pytest.raises(AgentRunError, match="without a terminal"):
        _validate_terminal_result(None)


def test_v0_agent_endpoint_is_pinned_and_ambient_independent(monkeypatch):
    monkeypatch.setenv("RESPAN_BASE_URL", "https://attacker.invalid/api")

    assert _validated_respan_base_url(DEFAULT_RESPAN_BASE_URL + "/") == (
        DEFAULT_RESPAN_BASE_URL
    )
    with pytest.raises(AgentRunError, match="official Respan API endpoint"):
        _validated_respan_base_url("https://attacker.invalid/api")
