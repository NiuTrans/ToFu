"""tests/test_timer_resume_guardrails.py — guardrails added after the
2026-06-26 zombie-timer search storm.

Covers the four defense-in-depth fixes in ``lib/scheduler/timer.py``:

  1. ``_build_poll_tools`` does NOT grant web_search/fetch to a bare watcher
     (tools_config={}) — the ungrounded "is X done?" instruction made cheap
     poll models hallucinate web queries. Search/fetch only when explicitly
     enabled.
  2. ``resume_active_timers`` auto-expires over-age zombie timers (status →
     'expired') instead of re-spawning them forever.
  3. ``resume_active_timers`` caps how many timers a single boot re-spawns.
  4. ``_increment_poll_count`` is now called on the skipped-poll branch so a
     timer whose check_command output never changes still reaches max_polls
     and retires (verified indirectly via the helper here).

Durability assertions use a disposable real Sidecar runtime.
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


# ── Fix 2: age-sweep expires zombies on resume ──────────────────────────────

def test_resume_expires_overage_timer(monkeypatch):
    old = _new_timer(poll_interval=10, max_polls=120)
    monkeypatch.setattr(
        'lib.scheduler.timer._loop._resume_max_age_seconds',
        lambda _timer: -1)

    spawned: list[str] = []
    monkeypatch.setattr(timer_mod, 'start_timer_loop',
                        lambda tid, **_kwargs: spawned.append(tid))

    timer_mod.resume_active_timers()

    assert _status(old) == 'expired', 'over-age timer must be auto-expired'
    assert old not in spawned, 'expired timer must NOT be re-spawned'


def test_resume_keeps_fresh_timer(monkeypatch):
    fresh = _new_timer(poll_interval=10, max_polls=120)

    spawned: list[str] = []
    monkeypatch.setattr(timer_mod, 'start_timer_loop',
                        lambda tid, **_kwargs: spawned.append(tid))

    timer_mod.resume_active_timers()

    assert _status(fresh) == 'active'
    assert fresh in spawned, 'a fresh active timer must be resumed'


# ── Fix 3: resume concurrency cap ───────────────────────────────────────────

def test_resume_cap_limits_respawns(monkeypatch):
    ids = [_new_timer() for _ in range(5)]

    monkeypatch.setenv('TOFU_TIMER_RESUME_CAP', '2')
    spawned: list[str] = []
    monkeypatch.setattr(timer_mod, 'start_timer_loop',
                        lambda tid, **_kwargs: spawned.append(tid))

    # Only this module's fresh timers exist with these created_at values, but
    # other suites may leave active rows; assert the cap bounds OUR spawns.
    timer_mod.resume_active_timers()

    ours = [t for t in spawned if t in ids]
    assert len(ours) <= 2, f'cap=2 must bound respawns, got {len(ours)}'
    # The un-spawned survivors remain active (retried next boot), not expired.
    still_active = [t for t in ids if _status(t) == 'active']
    assert len(still_active) == 5


# ── Fix 4: skipped polls advance poll_count ─────────────────────────────────

def test_skipped_poll_increments_count():
    tid = _new_timer()
    before = timer_mod._get_timer_row(tid, user_id=1)['poll_count']
    timer_mod._increment_poll_count(
        tid, 'skipped', 'output unchanged', user_id=1)
    after = timer_mod._get_timer_row(tid, user_id=1)['poll_count']
    assert after == before + 1
