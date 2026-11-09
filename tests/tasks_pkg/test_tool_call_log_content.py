"""Tool-call logs show a declared content argument by its size and digest, never its value.

docs/LOGGING.md: log sizes, counts, hashes and safe identifiers, never payloads; docs/CHANNEL_PROTOCOL.md
P14: no message content in any log line. A handler declares its content arguments when it registers
its tool names (``ToolRegistry`` ``content_args``); both tool-call log lines (the call itself, and a
handler exception) project them, and every other tool keeps its arguments in the log as before.
"""
import hashlib
import logging

import pytest

from lib.tasks_pkg.executor import ToolRegistry
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations
from tests.tasks_pkg.test_tool_timeout_recovery import recovery_task

pytestmark = pytest.mark.unit

TEXT = '下周一的名单在附件里'


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def test_declared_content_arguments_are_logged_by_size_and_digest_only():
    registry = ToolRegistry()
    registry.register_set(frozenset({'post_note', 'read_notes'}), lambda *args: None,
                          content_args=frozenset({'text', 'files'}))
    registry.register('search_notes', lambda *args: None)
    files = [{'path': 'notes/plan.md'}, {'path': 'notes/budget.xlsx'}]
    logged = registry.log_arguments('post_note', {'to': 'ann', 'text': TEXT, 'files': files})
    assert logged['to'] == 'ann'
    assert logged['text'] == {'bytes': len(TEXT.encode('utf-8')), 'sha256': _digest(TEXT)}
    assert logged['files']['items'] == 2 and 'plan.md' not in str(logged)
    assert registry.log_arguments('read_notes', {'folder': 'notes'}) == {'folder': 'notes'}
    assert registry.log_arguments('search_notes', {'text': TEXT}) == {'text': TEXT}
    assert registry.log_arguments('unregistered', {'text': TEXT}) == {'text': TEXT}


def test_the_call_log_of_channel_send_carries_no_message_text(monkeypatch, caplog):
    from lib.tasks_pkg.handlers import _adapter
    monkeypatch.setattr(_adapter, '_build_simple_meta', lambda *args, **kwargs: {})
    monkeypatch.setattr(_adapter, '_finalize_tool_round', lambda *args, **kwargs: None)
    caplog.set_level(logging.INFO, logger=_adapter.logger.name)
    for name in ('channel_send', 'mcp__notes__append'):
        _adapter.simple_call({'id': 'task-1'}, name, {'thread': 'dx:gid:900', 'text': TEXT}, 0, {}, 'call-1',
                             executor=lambda *args, **kwargs: 'ok', source='Test')
    send, other = [record.getMessage() for record in caplog.records if 'called with args=' in record.getMessage()]
    assert TEXT not in send and _digest(TEXT) in send and "'thread': 'dx:gid:900'" in send
    assert f"'text': '{TEXT}'" in other  # a tool that declares no content argument logs it as before


def test_a_handler_exception_logs_declared_content_by_digest_and_other_arguments_verbatim(observations, caplog):
    from lib.tasks_pkg.executor import _execute
    caplog.set_level(logging.DEBUG, logger=_execute.logger.name)
    _execute._handler_error_result(recovery_task(None, 'read_only'), 'channel_send', 'call-1',
                                   {'thread': 'dx:gid:900', 'text': TEXT, 'idempotency_key': 'k-1'}, 1, {},
                                   RuntimeError('outbox store unavailable'))
    _execute._handler_error_result(recovery_task(None, 'read_only'), 'channel_send', 'call-2',
                                   {'thread': 'dx:gid:900', 'text': TEXT}, 1, {}, ValueError('bad thread'))
    assert TEXT not in caplog.text and caplog.text.count(_digest(TEXT)) == 2
    assert 'dx:gid:900' in caplog.text
    caplog.clear()
    _execute._handler_error_result(recovery_task(None, 'read_only'), 'fetch_url', 'call-3',
                                   {'url': 'https://example.test/report'}, 1, {}, RuntimeError('reader crashed'))
    assert 'https://example.test/report' in caplog.text
