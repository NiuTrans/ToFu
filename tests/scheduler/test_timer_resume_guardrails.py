"""Timer restart preserves durable active work, owner identity and poll policy.

Polling tools still require explicit capability configuration, and skipped
polls retain actual poll-count accounting. Restart has no age-based expiry
or batch-count gate.
"""

from __future__ import annotations

import pytest

import lib.scheduler.timer as timer_mod

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

_CONV = 'conv-timer-test-guardrails'
_CREATED: list[str] = []


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    for timer_id in _CREATED:
        timer_mod.cancel_timer(timer_id, user_id=1)
    _CREATED.clear()


def _new_timer(tools_config=None, check_command='', poll_interval=10,
               max_polls=120):
    t = timer_mod.create_timer(
        user_id=1,
        conv_id=_CONV,
        check_instruction='Is the run finished?',
        continuation_message='done',
        poll_interval=poll_interval,
        max_polls=max_polls,
        check_command=check_command,
        tools_config=tools_config if tools_config is not None else {},
        source_task_id='task-guardrail',
    )
    _CREATED.append(t['id'])
    return t['id']


def _status(timer_id):
    return timer_mod._get_timer_row(timer_id, user_id=1)['status']


# ── Fix 1: bare watcher gets NO web_search / fetch ──────────────────────────

def test_bare_watcher_has_no_search_or_fetch():
    tools = timer_mod._build_poll_tools({}, owner_user_id=1) or []
    names = {t.get('function', {}).get('name') for t in tools}
    assert 'web_search' not in names, 'bare watcher must NOT get web_search'
    assert 'fetch_url' not in names, 'bare watcher must NOT get fetch_url'
    # read_files is always available for grounding.
    assert 'read_files' in names


def test_search_added_only_when_explicitly_enabled():
    tools = timer_mod._build_poll_tools(
        {'searchMode': 'multi'}, owner_user_id=1) or []
    names = {t.get('function', {}).get('name') for t in tools}
    assert 'web_search' in names
    assert 'fetch_url' in names


def test_fetch_added_when_fetch_enabled_alone():
    tools = timer_mod._build_poll_tools(
        {'fetchEnabled': True}, owner_user_id=1) or []
    names = {t.get('function', {}).get('name') for t in tools}
    assert 'fetch_url' in names
    assert 'web_search' not in names


# ── Restart retains old active work ──────────────────────────────

@pytest.mark.parametrize('max_polls', [0, 120])
@pytest.mark.parametrize('created_at', ['2000-01-01T00:00:00', 'unknown'])
def test_resume_keeps_old_active_work_without_storage_mutation(monkeypatch, max_polls, created_at):
    from lib.scheduler.timer import _loop
    row = {'id': 'old-active', 'user_id': 7, 'status': 'active',
           'poll_interval': 10, 'max_polls': max_polls, 'poll_count': 0,
           'created_at': created_at}
    class ReadOnlyClient:
        def query(self, operation, payload):
            assert operation == 'timer.active.list_all'
            assert payload == {'limit': 0}
            return [row]
    monkeypatch.setenv('TOFU_TIMER_MAX_AGE_HOURS', '0')
    monkeypatch.setattr(_loop, '_timer_client', lambda: ReadOnlyClient())
    spawned = []
    monkeypatch.setattr(timer_mod, 'start_timer_loop',
                        lambda tid, *, user_id: spawned.append((tid, user_id)) or True)
    assert timer_mod.resume_active_timers() == 1
    assert row['status'] == 'active'
    assert spawned == [('old-active', 7)]


def test_resume_keeps_fresh_timer(monkeypatch):
    fresh = _new_timer(poll_interval=10, max_polls=120)

    spawned: list[str] = []
    monkeypatch.setattr(timer_mod, 'start_timer_loop',
                        lambda tid, **_kwargs: spawned.append(tid))

    timer_mod.resume_active_timers()

    assert _status(fresh) == 'active'
    assert fresh in spawned, 'a fresh active timer must be resumed'


# ── Resume every eligible identity ───────────────────────────────────────────

def test_retired_resume_cap_does_not_limit_respawns(monkeypatch):
    ids = [_new_timer() for _ in range(5)]

    monkeypatch.setenv('TOFU_TIMER_RESUME_CAP', '2')
    spawned: list[str] = []
    monkeypatch.setattr(timer_mod, 'start_timer_loop',
                        lambda tid, **_kwargs: spawned.append(tid))

    # Only this module's fresh timers exist with these created_at values, but
    # other suites may leave active rows; assert the cap bounds OUR spawns.
    timer_mod.resume_active_timers()

    ours = [t for t in spawned if t in ids]
    assert set(ours) == set(ids)
    # The un-spawned survivors remain active (retried next boot), not expired.
    still_active = [t for t in ids if _status(t) == 'active']
    assert len(still_active) == 5


def test_durable_create_has_no_owner_active_capacity(monkeypatch):
    monkeypatch.setenv('TOFU_TIMER_LIVE_CAP', '1')
    admitted = [_new_timer() for _ in range(65)]
    assert all(_status(timer_id) == 'active' for timer_id in admitted)


# ── Fix 4: skipped polls advance poll_count ─────────────────────────────────

def test_skipped_poll_increments_count():
    tid = _new_timer()
    before = timer_mod._get_timer_row(tid, user_id=1)['poll_count']
    timer_mod._increment_poll_count(
        tid, 'skipped', 'output unchanged', user_id=1)
    after = timer_mod._get_timer_row(tid, user_id=1)['poll_count']
    assert after == before + 1


def test_resume_reads_beyond_old_two_hundred_row_feed(monkeypatch):
    ids = [_new_timer() for _ in range(265)]
    monkeypatch.setenv('TOFU_TIMER_RESUME_CAP', '1')
    monkeypatch.setenv('TOFU_TIMER_LIVE_CAP', '1')
    spawned = []
    monkeypatch.setattr(timer_mod, 'start_timer_loop', lambda timer_id, **kwargs: spawned.append(timer_id))
    timer_mod.resume_active_timers()
    assert set(ids).issubset(spawned)
    assert all(_status(timer_id) == 'active' for timer_id in ids)
