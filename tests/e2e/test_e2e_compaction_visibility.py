"""An automatic compaction inside a turn stays visible in the real app.

The stubbed model round persists a real archive through the compaction archive
owner and emits the receipt pair the summarizer emits. The page, which already
listed zero archives for the conversation, must count the new archive without
a reload, and the transcript must still render the settled receipt after a
hard reload. Provider I/O stays stubbed; storage, the task loop, v4 sync, Vite
delivery and Chromium are real.
"""
from __future__ import annotations

import json

import pytest

import lib.tasks_pkg.llm_fallback._transport as _fallback_transport
from tests.e2e.test_e2e_journeys import _fresh_chat, _send_and_wait_done
from tests.e2e.test_e2e_smoke import (
    _disable_open_mode_rate_limit as _disable_open_mode_rate_limit,
    _install_llm_stubs as _install_llm_stubs,
    _wait_app_ready,
)

pytestmark = pytest.mark.visual

_COMPACT_MARKER = '__e2e_compact__'


@pytest.fixture
def compacting_reply(_install_llm_stubs, monkeypatch):
    from lib.agent_core.events import EventType, build_event
    from lib.tasks_pkg.compaction._archive import _archive_transcript
    from lib.tasks_pkg.manager import task_user_id
    from lib.tasks_pkg.manager._events import append_event

    original = _fallback_transport.stream_llm_response
    archives = []

    def stream(task, body, **kwargs):
        messages = body.get('messages', []) if isinstance(body, dict) else []
        if (_COMPACT_MARKER in json.dumps(messages[-1:], default=str)
                and not task.get('_e2e_compacted')):
            task['_e2e_compacted'] = True
            archive_id = _archive_transcript(
                task['convId'], messages, user_id=task_user_id(task),
                trigger='working_set', task=task, round_num=0,
                tokens_before=612_667, tokens_after=50_477,
                msgs_before=len(messages), msgs_after=2)
            assert archive_id, 'the archive owner must persist the transcript'
            append_event(task, build_event(
                EventType.COMPACTION_DONE, archiveId=str(archive_id),
                convId=task['convId'], trigger='working_set',
                tokensBefore=612_667, tokensAfter=50_477, tokenCountKind='estimated',
                msgsBefore=len(messages), msgsAfter=2, reductionPct=91.8, roundNum=0,
                receipt={'status': 'completed', 'trigger': 'working_set'}))
            archives.append(str(archive_id))
        return original(task, body, **kwargs)

    monkeypatch.setattr(_fallback_transport, 'stream_llm_response', stream)
    return archives


def _wait_compaction_count(page, expected):
    """Read the context details row the user reads, then close it again."""
    page.locator('#contextHealthBar').click()
    page.wait_for_function(
        "(expected) => document.querySelectorAll('#ctxBarPopover dd')[2]"
        "?.textContent === expected",
        arg=expected, timeout=15000)
    page.locator('#ctxBarPopover .ctx-pop-close').click()


def test_in_turn_compaction_reaches_the_open_count_and_survives_reload(
        compacting_reply, page, live_server, assert_no_js_errors):
    page.set_viewport_size({'width': 1280, 'height': 900})
    _fresh_chat(page)
    _send_and_wait_done(page, 'First question, nothing to compact yet')
    _wait_compaction_count(page, '0')

    _send_and_wait_done(page, f'{_COMPACT_MARKER} continue the long task',
                        expected_replies=2)
    assert len(compacting_reply) == 1
    archive_id = compacting_reply[0]
    _wait_compaction_count(page, '1')

    receipt = (f'#chatInner [data-conversation-action="open-compaction"]'
               f'[data-operation="{archive_id}"]')
    page.locator(receipt).wait_for(state='attached', timeout=15000)
    conversation_id = page.locator('.conv-item.active').get_attribute('data-conv-id')

    page.reload(wait_until='domcontentloaded')
    _wait_app_ready(page)
    assert page.locator('.conv-item.active').get_attribute('data-conv-id') == conversation_id
    page.locator(receipt).wait_for(state='attached', timeout=15000)
    _wait_compaction_count(page, '1')
    assert len(compacting_reply) == 1, 'reload must not execute another model request'
