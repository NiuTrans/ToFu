"""pt_a21cd6eb bug#2 — autopilot follow-up model re-resolution guards.

Measured 2026-08-01 (conv ms9ow2ttm0gnu0): the owner switched the
conversation to kimi-k3 at 16:58 (quota fallback had persisted it into
settings), yet the 18:20 autopilot follow-up still launched on the RUN's
launch-time model (claude-opus-5) and walked back into the same 429 wall —
`_start_followup_task` copied `cfg` from the parent task's config and never
re-read the conversation's current `settings.model`/`settings.preset`.

The fix re-resolves ONLY model/preset from the conv's live settings;
everything else in cfg stays inherited from the parent.
"""

import json

import pytest

import lib.tasks_pkg.autopilot_baton as baton

pytestmark = pytest.mark.unit


class _Row(dict):
    pass


class _FakeDB:
    def __init__(self, settings):
        self.settings = settings

    def execute(self, sql, params=()):
        return self

    def fetchone(self):
        if self.settings is None:
            return None
        return _Row(settings=json.dumps(self.settings))


def _run_followup(monkeypatch, *, parent_model='claude-opus-5',
                  parent_preset='claude-opus-5', settings=None):
    """Drive _start_followup_task with all side effects faked; return cfg."""
    captured = {}

    monkeypatch.setattr('lib.database.get_thread_db',
                        lambda domain: _FakeDB(settings))
    monkeypatch.setattr(
        'lib.tasks_pkg.conv_message_builder.build_api_messages_from_db',
        lambda conv_id, cfg: [{'role': 'user', 'content': 'vu turn'}])
    monkeypatch.setattr('lib.tasks_pkg.manager.abort_running_tasks_for_conv',
                        lambda conv_id: 0)

    def _fake_create(conv_id, messages, cfg):
        captured['cfg'] = dict(cfg)
        return {'id': 'new-task-1'}

    monkeypatch.setattr('lib.tasks_pkg.create_task', _fake_create)
    monkeypatch.setattr('lib.tasks_pkg.spawn_task', lambda task: None)
    monkeypatch.setattr('lib.conversations.set_conversation_settings',
                        lambda *a, **kw: None)
    monkeypatch.setattr('lib.conversations.notify_conv_changed',
                        lambda *a, **kw: None)

    task = {'id': 'parent-1', 'convId': 'c1',
            'config': {'model': parent_model, 'preset': parent_preset,
                       'thinkingDepth': 'max', 'autopilotRunId': 'ar-1'}}
    out = baton._start_followup_task(task, 'c1')
    assert out == 'new-task-1'
    return captured['cfg']


@pytest.mark.unit
class TestFollowupModelReresolve:

    def test_live_settings_model_wins(self, monkeypatch):
        cfg = _run_followup(
            monkeypatch,
            settings={'model': 'kimi-k3', 'preset': 'kimi-k3'})
        assert cfg['model'] == 'kimi-k3'
        assert cfg['preset'] == 'kimi-k3'
        # Turn-scoped state still inherited.
        assert cfg['thinkingDepth'] == 'max'
        assert cfg['autopilotRunId'] == 'ar-1'

    def test_same_model_is_noop(self, monkeypatch):
        cfg = _run_followup(
            monkeypatch,
            settings={'model': 'claude-opus-5', 'preset': 'claude-opus-5'})
        assert cfg['model'] == 'claude-opus-5'

    def test_missing_row_inherits_parent(self, monkeypatch):
        cfg = _run_followup(monkeypatch, settings=None)
        assert cfg['model'] == 'claude-opus-5'
        assert cfg['preset'] == 'claude-opus-5'

    def test_empty_settings_model_inherits_parent(self, monkeypatch):
        cfg = _run_followup(monkeypatch, settings={'model': ''})
        assert cfg['model'] == 'claude-opus-5'

    def test_model_swap_without_preset_keeps_inherited_preset(
            self, monkeypatch):
        cfg = _run_followup(monkeypatch, settings={'model': 'kimi-k3'})
        assert cfg['model'] == 'kimi-k3'
        assert cfg['preset'] == 'claude-opus-5'


def test_source_guard_reresolve_present():
    src = open(baton.__file__, encoding='utf-8').read()
    assert "SELECT settings FROM conversations WHERE id=? AND user_id=1" in src, (
        'regression: _start_followup_task no longer reads the conv settings — '
        'follow-ups are pinned to the launch-time model again.')
    assert "cfg['model'] = _lm" in src
