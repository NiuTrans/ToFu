"""tests/test_conv_provider_id_stamp.py — the terminal-persist provider stamp.

WHY
---
The context gauge resolves its window via ``per_model['provider::model']``
(server-config context policy) and reads the provider from
``conv.provider_id``. Legacy conversations NEVER persisted ``provider_id``
in their settings (fleet-wide NULL — verified 2026-08-20 on data/tofu.db),
so the gauge's three-step lookup (scoped key → bare key → default_limit)
fell through to ``unknown`` and the chip showed "—" forever.

The authoritative provider is only known AFTER dispatch (fallback chains can
land on a different slot than requested), so ``persist_task_result`` — the
terminal chokepoint every conv-backed task passes — is the earliest honest
writer. ``_stamp_conv_provider_id`` runs there.

These tests spy on ``lib.conversations.update_conversation_settings`` (the real
settings gate is covered by tests/test_settings_store.py) and assert:

  1. a task with provider_id + convId stamps the settings, with
     ``notify=False`` (invisible metadata must not push to peer tabs);
  2. the mutate is VALUE-ONLY — an unchanged provider returns False (no
     UPDATE, no cache invalidation churn on every persisted task);
  3. a changed provider overwrites (fallback to another slot updates the
     conv's identity);
  4. no provider_id / no convId / inline-message task → the gate is never
     touched;
  5. NEUTER proof: dropping the stamp call from persist_task_result removes
     the behaviour (the wiring assertion is load-bearing).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

import lib.tasks_pkg.manager._persist as _persist  # noqa: E402


@pytest.fixture
def spy_gate(monkeypatch):
    """Replace the settings gate with a recorder; returns (calls, settings)."""
    import lib.conversations as conv_pkg
    calls = []
    stored = {'model': 'kimi-k3'}  # a realistic pre-existing settings blob

    def _fake_set(conv_id, mutate, **kwargs):
        result = mutate(stored)
        calls.append({'conv_id': conv_id, 'kwargs': kwargs,
                      'mutate_result': result})
        return None if result is False else stored

    monkeypatch.setattr(conv_pkg, 'update_conversation_settings', _fake_set)
    return calls, stored


def _task(**over):
    t = {'id': 'task-12345678', 'convId': 'conv-abc',
         'provider_id': 'sankuai', 'model': 'kimi-k3', '_userId': 1}
    t.update(over)
    return t


def test_stamp_writes_provider_id(spy_gate):
    calls, stored = spy_gate
    _persist._stamp_conv_provider_id(_task())
    assert len(calls) == 1
    assert calls[0]['conv_id'] == 'conv-abc'
    # Invisible metadata — must NOT emit the cross-device conv_changed push.
    assert calls[0]['kwargs'].get('notify') is False
    assert stored['provider_id'] == 'sankuai'
    # Pre-existing keys survive the merge.
    assert stored['model'] == 'kimi-k3'


def test_stamp_is_value_only(spy_gate):
    """An unchanged provider short-circuits the write (mutate → False)."""
    calls, stored = spy_gate
    stored['provider_id'] = 'sankuai'
    _persist._stamp_conv_provider_id(_task())
    assert len(calls) == 1, 'gate is consulted (it owns freshness)'
    # The mutate must signal "nothing changed" so the REAL gate skips the
    # UPDATE + cache invalidation (covered end-to-end in
    # tests/test_settings_store.py::test_mutate_false_skips_write).
    assert calls[0]['mutate_result'] is False
    assert set(stored) == {'model', 'provider_id'}


def test_stamp_updates_on_provider_change(spy_gate):
    calls, stored = spy_gate
    stored['provider_id'] = 'sankuai'
    _persist._stamp_conv_provider_id(_task(provider_id='oauth_codex'))
    assert stored['provider_id'] == 'oauth_codex'


@pytest.mark.parametrize('kwargs', [
    {'provider_id': None},                 # dispatch never stamped one
    {'convId': ''},                        # carrier task, no conversation
    {'_inline_messages': True},            # eval-harness / API task
])
def test_stamp_skips_when_not_applicable(spy_gate, kwargs):
    calls, _ = spy_gate
    _persist._stamp_conv_provider_id(_task(**kwargs))
    assert calls == []


def test_stamp_is_wired_into_persist_task_result():
    """NEUTER guard: the helper must actually be CALLED from the terminal
    persist path — delete the call site and this fails."""
    import inspect
    src = inspect.getsource(_persist.persist_task_result)
    assert '_stamp_conv_provider_id(task)' in src, (
        'persist_task_result no longer stamps the conv provider — the '
        'context gauge loses its provider::model anchor again')


def test_stamp_failure_never_breaks_persist(spy_gate, monkeypatch):
    """A throwing gate must be swallowed (logged), never raised."""
    import lib.conversations as conv_pkg

    def _boom(*_a, **_kw):
        raise RuntimeError('db down')

    monkeypatch.setattr(conv_pkg, 'update_conversation_settings', _boom)
    _persist._stamp_conv_provider_id(_task())  # must not raise
