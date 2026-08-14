"""Non-streaming DeepSeek tool turns retain required reasoning state."""

from __future__ import annotations

import importlib

import pytest


pytestmark = pytest.mark.unit


class _Response:
    status_code = 200
    headers: dict[str, str] = {}

    @staticmethod
    def json() -> dict:
        return {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "",
                        "reasoning_content": "private continuation state",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "run_command",
                                    "arguments": '{"command":"true"}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


def test_chat_exposes_reasoning_content_for_tool_replay(monkeypatch):
    chat_module = importlib.import_module("lib.llm.chat")
    monkeypatch.setattr(chat_module, "http_post", lambda *args, **kwargs: _Response())

    _, usage = chat_module.chat(
        [{"role": "user", "content": "use the tool"}],
        model="deepseek-v4-flash-meituan",
        api_key="test-only",
        base_url="https://public.example/v1",
        max_retries=0,
        extra={
            "thinking": {"type": "enabled"},
            "reasoning_effort": "max",
        },
    )

    assert usage["_reasoning_content"] == "private continuation state"
    assert usage["_tool_calls"][0]["function"]["name"] == "run_command"
