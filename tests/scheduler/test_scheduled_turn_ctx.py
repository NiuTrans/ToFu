"""Scheduler-owned turns (timer / proactive / swarm auto-continue) must carry
a per-turn context note like human turns do.

Root cause (2026-09-08 user screenshot): a 定时代理 input turn rendered with
NO turn-ctx rail — human turns get ``user_msg._ctx`` from the browser at
send time (info-rail.js ``buildTurnCtxSnapshot`` → ``payload['ctx']``), but
scheduler turns are dispatched server-side via
``dispatch_scheduled_turn`` and never touch a browser, so the projection
carried no ``_ctx`` and the rail/fold had nothing to render.

The fix derives the same snapshot shape from the merged task config at
dispatch time; ``normalize_projection_document`` then maps ``_ctx`` →
``contextSnapshot`` exactly like the human path.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from lib.scheduler._shared import build_task_config, build_turn_ctx_snapshot
from lib.turn_projection_patch import normalize_projection_document


def _full_config() -> dict:
    return build_task_config(
        {
            'searchMode': 'multi',
            'codeExecEnabled': True,
            'browserEnabled': True,
            'imageGenEnabled': True,
            'autoTranslate': True,
            'projectPath': '/mnt/data/workspace/proj',
        },
        {'model': 'kimi-k3'},
    )


def test_snapshot_shape_matches_frontend_contract():
    snap = build_turn_ctx_snapshot(_full_config())
    assert snap is not None
    assert snap['model'] == 'kimi-k3'
    # depth/modes stay empty: the done frame's fact card settles them.
    assert snap['depth'] == ''
    assert snap['modes'] == []
    labels = {tool['label']: tool['tone'] for tool in snap['tools']}
    assert labels == {
        'Search ×N': 'search',
        'Fetch': 'net',
        'Browser': 'net',
        'Code Exec': 'code',
        'Memory': 'ai',
        'Image Gen': 'ai',
        'Translate': 'ai',
    }
    assert snap['roots'] == [
        {'short': 'workspace/proj', 'path': '/mnt/data/workspace/proj',
         'ro': False},
    ]


def test_search_mode_chip_variants():
    base = build_task_config({}, {'model': 'm'})
    single = build_turn_ctx_snapshot({**base, 'searchMode': 'single'})
    assert {'label': 'Search', 'tone': 'search'} in single['tools']
    off = build_turn_ctx_snapshot({**base, 'searchMode': 'off'})
    assert all(tool['tone'] != 'search' for tool in off['tools'])


def test_short_project_path_kept_verbatim():
    snap = build_turn_ctx_snapshot({'model': 'm', 'projectPath': '/proj'})
    assert snap['roots'] == [{'short': '/proj', 'path': '/proj', 'ro': False}]


def test_empty_config_yields_no_snapshot():
    assert build_turn_ctx_snapshot({}) is None
    assert build_turn_ctx_snapshot({'searchMode': 'off'}) is None


def test_snapshot_survives_projection_normalization():
    """The persisted/read projection path maps ``_ctx`` → ``contextSnapshot``
    — pin that the scheduler snapshot makes that trip intact."""
    snap = build_turn_ctx_snapshot(_full_config())
    normalized = normalize_projection_document(
        {'role': 'user', 'content': 'run', '_ctx': snap})
    block = normalized.get('contextSnapshot')
    assert block is not None
    assert block['blockId'] == 'turn-context'
    assert block['snapshot'] == snap
    assert '_ctx' not in normalized


# ── Dispatch seam ────────────────────────────────────────────────────────────

class _FakeCommands:
    def __init__(self):
        self.calls: list[dict] = []

    def create_turn(self, conv_id, owner_id, payload, *, request_started_at):
        self.calls.append(payload)

        class _Outcome:
            value = {'attempt': {'taskId': 'task-123'}}

        return _Outcome()


class _FakeStorage:
    def query(self, operation, payload):
        assert operation == 'conversation.get'
        return {'metadata': {'settings': {'model': 'kimi-k3'}}}


def _dispatch(monkeypatch, user_message):
    import lib.scheduler.conversation_dispatch as dispatch_module

    commands = _FakeCommands()
    monkeypatch.setattr(dispatch_module, 'get_storage_client',
                        lambda: _FakeStorage())
    monkeypatch.setattr(dispatch_module, 'conversation_turn_commands', commands)
    result = dispatch_module.dispatch_scheduled_turn(
        'conv-1',
        user_message,
        {},
        user_id=7,
        command_id='proactive:t1:1',
    )
    return result, commands


def test_dispatch_attaches_ctx_to_input_turn(monkeypatch):
    result, commands = _dispatch(
        monkeypatch,
        {'role': 'user', 'content': '⏰ poll', '_proactive': True},
    )
    assert result.disposition == 'started'
    input_turn = commands.calls[0]['inputTurn']
    ctx = input_turn.get('_ctx')
    assert ctx is not None
    assert ctx['model'] == 'kimi-k3'
    # build_task_config defaults: fetch on, search multi, memory on.
    labels = {tool['label'] for tool in ctx['tools']}
    assert {'Search ×N', 'Fetch', 'Memory'} <= labels


def test_dispatch_preserves_caller_supplied_ctx(monkeypatch):
    caller_ctx = {'model': 'caller-model', 'tools': [], 'roots': [],
                  'modes': [], 'depth': ''}
    _, commands = _dispatch(
        monkeypatch,
        {'role': 'user', 'content': 'x', '_timer': True, '_ctx': caller_ctx},
    )
    assert commands.calls[0]['inputTurn']['_ctx'] is caller_ctx
