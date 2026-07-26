"""Tests for LLM response parsing robustness."""

from __future__ import annotations

import pytest

from backend.llm_service import _extract_content


def test_extract_content_happy_path():
    data = {"choices": [{"message": {"content": "{\"vehicle_id\": \"V1\"}"}}]}
    assert _extract_content(data) == '{"vehicle_id": "V1"}'


def test_extract_content_raises_on_missing_choices():
    with pytest.raises(ValueError):
        _extract_content({"choices": []})
    with pytest.raises(ValueError):
        _extract_content({})


def test_extract_content_raises_on_missing_message():
    with pytest.raises(ValueError):
        _extract_content({"choices": [{}]})


def test_extract_content_raises_on_missing_or_empty_content():
    with pytest.raises(ValueError):
        _extract_content({"choices": [{"message": {}}]})
    with pytest.raises(ValueError):
        _extract_content({"choices": [{"message": {"content": ""}}]})


def test_truncated_response_raises_a_clear_error():
    """A reply cut off by the token budget must say so explicitly.

    Regression test: a truncated reply otherwise surfaces as an inscrutable
    JSON "Expecting ',' delimiter" further down the stack, which points at
    the wrong problem. Reasoning models charge hidden reasoning tokens
    against max_tokens, so this is easy to trigger by accident.
    """
    truncated = {
        "choices": [{
            "finish_reason": "length",
            "message": {"content": '{"vehicle_id": "V1", "steps": [{"order": 1,'},
        }],
        "usage": {"completion_tokens": 4097},
    }
    with pytest.raises(ValueError, match="truncated"):
        _extract_content(truncated)


def test_max_tokens_is_not_sent_unless_configured():
    """Default config must not cap the reply, and must not send the key."""
    from backend.config import settings

    assert settings.llm_max_tokens is None
