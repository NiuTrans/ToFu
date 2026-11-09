"""Executable specification: stored edit_file insert rounds survive replace-only.

Conversations recorded before the owner decision of 2026-11-06 hold edit_file
calls with ``operation: "insert_after"`` and retired ``insert_contents`` calls.
They must still summarize, replay to providers verbatim, feed compaction and
export trajectories; only new calls are refused. Browser rendering of the same
rounds is pinned by tests/frontend/test_frontend_tool_rounds_render.py.
"""

from __future__ import annotations

import json

import pytest

from lib.tools.project import PROJECT_TOOL_EDIT_FILE

pytestmark = pytest.mark.unit

STORED_ARGS = {'path': 'src/a.py', 'edits': [
    {'operation': 'insert_after', 'anchor': 'def a():', 'content': '    pass'},
    {'operation': 'replace', 'anchor': 'x = 1', 'content': 'x = 2'}]}
STORED_RESULT = ('Applied 2/2 edits\n'
                 '[1] OK src/a.py [insert_after]: 1 lines inserted after anchor at L1\n'
                 '[2] OK src/a.py [replace]: 1 lines changed (3L → 3L)')


def _messages():
    return [
        {'role': 'user', 'content': 'add a body'},
        {'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': 'call_1', 'type': 'function',
            'function': {'name': 'edit_file', 'arguments': json.dumps(STORED_ARGS)}}]},
        {'role': 'tool', 'tool_call_id': 'call_1', 'content': STORED_RESULT},
    ]


def test_stored_insert_round_still_summarizes():
    from lib.tools.meta import build_project_tool_meta
    meta = build_project_tool_meta('edit_file', STORED_ARGS, STORED_RESULT)
    assert meta['writeOk'] is True
    assert meta['editOperations'] == ['insert_after', 'replace']
    legacy = build_project_tool_meta(
        'insert_contents', {'edits': [{'path': 'a.py', 'anchor': 'A', 'content': 'B'}]},
        'Inserted 1/1 edits\n[1] OK a.py: 1 lines inserted after anchor at L1')
    assert legacy['writeOk'] is True


def test_stored_insert_round_replays_verbatim_to_providers():
    from lib.llm.anthropic_outbound import openai_body_to_anthropic
    from lib.tools.gateway import preflight_wire_tool_body

    body = {'model': 'claude-opus-5.5', 'messages': _messages(),
            'tools': [PROJECT_TOOL_EDIT_FILE]}
    preflight_wire_tool_body(body)
    wire = openai_body_to_anthropic(body)
    uses = [block for message in wire['messages'] if isinstance(message.get('content'), list)
            for block in message['content'] if block.get('type') == 'tool_use']
    assert [use['input'] for use in uses] == [STORED_ARGS]


def test_stored_insert_round_feeds_compaction_with_its_batch_path():
    from lib.tasks_pkg.compaction._layer2._anchor import _extract_recently_accessed_files
    assert _extract_recently_accessed_files(_messages()) == ['src/a.py']


@pytest.mark.parametrize('fmt', ['atif', 'anthropic'])
def test_stored_insert_round_exports_its_arguments(fmt):
    from lib.trajectory import flatten
    task = {'id': 't', 'messages': _messages(), 'content': 'done', 'toolRounds': [{
        'tool_calls': _messages()[1]['tool_calls'],
        'results': [{'tool_call_id': 'call_1', 'name': 'edit_file', 'result': STORED_RESULT}]}]}
    text = json.dumps(flatten(task, fmt))
    assert '"operation": "insert_after"' in text
    assert 'def a():' in text
