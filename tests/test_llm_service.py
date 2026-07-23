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
