"""Provider attempt logs must follow typed stream completion authority."""

import logging

import pytest

from lib.llm.stream_result import ProviderStreamResult
from lib.tasks_pkg.llm_fallback._call import _log_stream_attempt_outcome


pytestmark = pytest.mark.unit


def _log(result, caplog):
    caplog.set_level(logging.INFO)
    _log_stream_attempt_outcome(
        {'convId': 'conv-log'},
        tid='task-log',
        round_num=3,
        model='kimi-k3',
        stream_result=result,
    )
    return caplog.text


def test_no_actionable_attempt_is_never_logged_as_ok(caplog):
    result = ProviderStreamResult.from_legacy(
        {'role': 'assistant', 'content': '', 'reasoning_content': 'thinking'},
        'stop',
        {
            '_no_actionable_timeout': True,
            '_missing_done': True,
            'stream_elapsed_ms': 300_000,
            'trace_id': 'trace-no-output',
        },
    )

    logged = _log(result, caplog)

    assert '⚠ LLM round 3 UNUSABLE' in logged
    assert 'stream_state=no_actionable_output' in logged
    assert '✓ LLM round 3 OK' not in logged


def test_verified_provider_finish_keeps_success_log(caplog):
    result = ProviderStreamResult.from_legacy(
        {'role': 'assistant', 'content': 'done'},
        'stop',
        {'stream_elapsed_ms': 1250, 'trace_id': 'trace-done'},
    )

    logged = _log(result, caplog)

    assert '✓ LLM round 3 OK' in logged
    assert 'stream_state=provider_finished' in logged
    assert 'UNUSABLE' not in logged
