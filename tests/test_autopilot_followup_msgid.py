"""tests/test_autopilot_followup_msgid.py — Autopilot follow-up _msgId isolation.

Root cause guarded here (conv mrmuh47m3h2a3p — 16 assistant rows sharing ONE
`_msgId`):

  ``_start_followup_task`` copies the PARENT task's config
  (``dict(task['config'])``) to seed the follow-up. That config still carried
  ``assistantMsgId`` — the CLIENT-minted stable id of the ORIGINAL turn's
  assistant bubble. ``create_task`` stamps it as the follow-up's
  ``_assistantMsgId`` and ``_new_assistant_slot`` reuses it as the committed
  row's ``_msgId``. So EVERY follow-up in an autopilot run commits with the
  SAME ``_msgId``. The frontend keys/dedups DOM nodes by ``_msgId`` → N
  colliding assistant rows collapse into ONE bubble, so the Agent replies
  between VU (user-role) turns become invisible and the transcript degenerates
  to a wall of VU turns.

Fix: strip ``assistantMsgId`` / ``msgId`` from the copied cfg in
``_start_followup_task`` (and in the kick config template), so each follow-up
gets a UNIQUE server-minted UUID.

The tests monkeypatch spawn / message-build so no live LLM / orchestrator runs.
"""

import pytest

pytestmark = pytest.mark.unit


def _parent_task(tid='parent-1', conv_id='conv-FU', assistant_msg_id='tmp_client_abc'):
    """A finished parent worker task whose config carries assistantMsgId
    (exactly what the browser ships in the send POST)."""
    return {
        'id': tid,
        'convId': conv_id,
        'status': 'done',
        'config': {
            'model': 'm',
            'autopilot': True,
            'assistantMsgId': assistant_msg_id,
        },
    }


@pytest.fixture()
def _patched_spawn(monkeypatch):
    """Patch the follow-up spawn seam: capture the config create_task received
    WITHOUT running a real task/thread. Returns a dict the test reads."""
    captured = {}

    import lib.tasks_pkg.autopilot as ap
    import lib.tasks_pkg.conv_message_builder as cmb
    import lib.tasks_pkg as pkg
    import lib.tasks_pkg.manager as mgr

    monkeypatch.setattr(cmb, 'build_api_messages_from_db',
                        lambda cid, cfg: [{'role': 'user', 'content': 'go on'}])
    # abort sweep is a DB/registry side effect — no-op it.
    monkeypatch.setattr(mgr, 'abort_running_tasks_for_conv',
                        lambda *a, **k: 0, raising=False)

    real_create_task = pkg.create_task

    def _capturing_create_task(conv_id, messages, config, **kw):
        captured['config'] = config
        t = real_create_task(conv_id, messages, config, **kw)
        captured['task'] = t
        return t

    monkeypatch.setattr(pkg, 'create_task', _capturing_create_task)
    monkeypatch.setattr(pkg, 'spawn_task', lambda t: captured.update(spawned=t))
    # settings / notify side effects — no-op.
    import lib.conversations as convs
    monkeypatch.setattr(convs, 'set_conversation_settings',
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(convs, 'notify_conv_changed',
                        lambda *a, **k: None, raising=False)

    return captured


def test_followup_strips_parent_assistant_msg_id(_patched_spawn):
    """The follow-up task's config MUST NOT carry the parent's assistantMsgId,
    and the resulting task's _assistantMsgId must be empty (→ a fresh UUID is
    minted per follow-up at commit time)."""
    from lib.tasks_pkg.autopilot import _start_followup_task

    parent = _parent_task(assistant_msg_id='tmp_client_abc')
    new_id = _start_followup_task(parent, parent['convId'])

    assert new_id, 'follow-up should spawn'
    cfg = _patched_spawn['config']
    assert 'assistantMsgId' not in cfg, (
        'follow-up cfg still carries the parent assistantMsgId — this is the '
        'collision root cause')
    assert 'msgId' not in cfg
    # create_task reads config.assistantMsgId → task['_assistantMsgId'].
    # Empty means _new_assistant_slot falls through to a fresh server UUID.
    assert (_patched_spawn['task'].get('_assistantMsgId') or '') == ''


def test_followup_preserves_other_config(_patched_spawn):
    """Stripping the id must not disturb unrelated config the follow-up needs."""
    from lib.tasks_pkg.autopilot import _start_followup_task

    parent = _parent_task()
    parent['config']['model'] = 'claude-x'
    parent['config']['autopilot'] = True
    _start_followup_task(parent, parent['convId'])

    cfg = _patched_spawn['config']
    assert cfg['model'] == 'claude-x'
    assert cfg['autopilot'] is True


def test_NEUTER_without_strip_ids_collide(_patched_spawn, monkeypatch):
    """NEUTER: prove the id is load-bearing. If assistantMsgId is NOT stripped
    (simulated by seeding it back), two consecutive follow-ups minted from the
    SAME parent config commit assistant slots with the IDENTICAL _msgId — the
    exact collision that hides Agent turns."""
    from lib.tasks_pkg.manager._events import _new_assistant_slot

    # Simulate the pre-fix behaviour: cfg still carries assistantMsgId → the
    # task's _assistantMsgId is that client id → _new_assistant_slot reuses it.
    parent_client_id = 'tmp_client_abc'
    task_a = {'_assistantMsgId': parent_client_id}
    task_b = {'_assistantMsgId': parent_client_id}
    slot_a = _new_assistant_slot(task_a)
    slot_b = _new_assistant_slot(task_b)
    assert slot_a['_msgId'] == slot_b['_msgId'] == parent_client_id, (
        'NEUTER: reusing assistantMsgId across follow-ups collides the '
        'committed _msgId (this is what the fix prevents)')

    # And the fixed path (empty _assistantMsgId) yields NO stamped id, so
    # _assign_message_ids later mints a unique UUID per row.
    slot_fixed_a = _new_assistant_slot({'_assistantMsgId': ''})
    slot_fixed_b = _new_assistant_slot({'_assistantMsgId': ''})
    assert '_msgId' not in slot_fixed_a
    assert '_msgId' not in slot_fixed_b
