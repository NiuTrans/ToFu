"""Reservation estimates cover client tools and prior tool arguments; no repricing."""
import pytest
from lib.billing.request_flow import estimate_prompt_tokens

pytestmark = pytest.mark.unit


def test_large_tool_schema_and_prior_arguments_are_not_free_prompt():
    messages = [{'role': 'user', 'content': 'do it'}]
    tool = {'type': 'function', 'function': {'name': 'client', 'description': 'x' * 16000}}
    assert estimate_prompt_tokens(messages) == 1
    assert estimate_prompt_tokens(messages, tools=[tool]) >= 4000
    prior = messages + [{'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'prior', 'type': 'function', 'function': {'name': 'client', 'arguments': 'x' * 8000}}]}]
    assert estimate_prompt_tokens(prior, tools=[tool]) >= 6000
    assert estimate_prompt_tokens([], tools=[]) == 1
