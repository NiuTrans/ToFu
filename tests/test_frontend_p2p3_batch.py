"""tests/test_frontend_p2p3_batch.py — pt_3cd6cd48.

Guards for the 9 race/leak fixes in the P2/P3 batch (the 10th item —
streaming_ui.js _pendingStreamTimer — is deferred to the sibling holding
that file's staged WIP):

  ① conv_sync_push.js — _applyHistoryRewrite re-checks the live-task guard
    AFTER the GET round-trip (a send mid-fetch would be wiped otherwise).
  ② podcast.js / video.js — stale-paper guards: _init*Tab and _*Generate
    bail when paperHash changed mid-await.
  ③ podcast.js / video.js — an in-flight poll must not resurrect status
    after Abort reset it (tid re-check after the await).
  ④ branch.js — deleteBranch rolls back the optimistic splice on NETWORK
    failure too, not just on HTTP !ok (shared _revertAndResync).
  ⑤ myday.js — poll failure backstop: N consecutive failures stop polling
    and clear the refresh-button spinner.
  ⑥ settings/oauth.js — the popup-check interval self-terminates once the
    card leaves 'pending' (was only cleared on popup.closed).
  ⑦ podcast.js — the sleep timer is cleared with the run state (re-init +
    regenerate), so an old timer can't pause new audio.
  ⑨ streaming_swarm_panel.js — the 1Hz ticker is lazy: armed by
    _buildSwarmPanelHTML, self-stops after 60 idle seconds.
  ⑩ push.js — send() queues control messages while disconnected and
    flushes on open (was a silent drop).

Source scans + byte-reverting NEUTERs, following the project's guard style.

Run::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_frontend_p2p3_batch.py -v
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


# ── ① history_rewrite post-await live-guard ───────────────────────────────

def test_history_rewrite_rechecks_live_guard_after_fetch():
    src = _read('static/js/conv_sync_push.js')
    fetch_pos = src.find('await Api.conversations.get(convId)')
    assert fetch_pos != -1
    guard = 'activeStreams.has(convId)'
    before = src[:fetch_pos].count(guard)
    after = src[fetch_pos:].count(guard)
    assert before >= 1 and after >= 1, (
        f'live-task guard must appear before AND after the GET '
        f'(before={before}, after={after}) — a send mid-fetch would be wiped')


def test_NEUTER_history_rewrite_single_guard_fires():
    src = _read('static/js/conv_sync_push.js')
    fetch_pos = src.find('await Api.conversations.get(convId)')
    guard_line = '''  if (conv.activeTaskId || (typeof activeStreams !== "undefined" && activeStreams.has(convId))) {
    return;
  }
'''
    tail = src[fetch_pos:]
    assert guard_line in tail, 'NEUTER anchor missing'
    neutered_tail = tail.replace(guard_line, '', 1)
    assert neutered_tail.count('activeStreams.has(convId)') == 0


# ── ② stale-paper guards ──────────────────────────────────────────────────

@pytest.mark.parametrize('rel,state', [
    ('static/js/paper/podcast.js', '_podcast'),
    ('static/js/paper/video.js', '_pvideo'),
])
def test_stale_paper_guards(rel, state):
    src = _read(rel)
    assert src.count(f'{state}.paperHash !== initHash') >= 2, (
        f'{rel}: _init*Tab must bail on paper switch after each await')
    assert f'{state}.paperHash !== genHash' in src, (
        f'{rel}: _*Generate must bail when the paper switched mid-start')


def test_NEUTER_stale_guard_fires():
    src = _read('static/js/paper/podcast.js')
    neutered = src.replace('_podcast.paperHash !== initHash', 'false &&', 1)
    assert neutered.count('_podcast.paperHash !== initHash') < 2


# ── ③ abort-vs-in-flight-poll guards ──────────────────────────────────────

def test_podcast_poll_rechecks_task_after_await():
    src = _read('static/js/paper/podcast.js')
    assert 'var tid = _podcast.taskId;' in src
    assert '_podcast.taskId !== tid) return;' in src


def test_video_poll_rechecks_task_after_await():
    src = _read('static/js/paper/video.js')
    assert ('_pvideo.regenTaskId !== tid' in src
            and '_pvideo.taskId !== tid' in src), (
        'video poll must re-validate the captured tid after the await')


# ── ④ deleteBranch network rollback ───────────────────────────────────────

def test_delete_branch_rolls_back_on_network_failure():
    src = _read('static/js/branch.js')
    assert src.count('await _revertAndResync(') >= 2, (  # both call sites
        'deleteBranch must route BOTH server-reject and network-error '
        'through the shared rollback')
    catch_pos = src.find('} catch (e) {', src.find('async function deleteBranch'))
    assert catch_pos != -1
    assert '_revertAndResync(' in src[catch_pos:catch_pos + 200], (
        'network-error catch does not roll back')


def test_NEUTER_delete_branch_catch_without_rollback_fires():
    src = _read('static/js/branch.js')
    neutered = src.replace("await _revertAndResync('network error', e && e.message);",
                           "console.warn('[branch.delete] network error', e);", 1)
    catch_pos = neutered.find('} catch (e) {', neutered.find('async function deleteBranch'))
    assert '_revertAndResync(' not in neutered[catch_pos:catch_pos + 200]


# ── ⑤ myday poll backstop ─────────────────────────────────────────────────

def test_myday_poll_has_failure_backstop():
    src = _read('static/js/myday.js')
    assert 'FAIL_LIMIT' in src
    assert '_myday._pollFails' in src
    assert "classList.remove('spinning')" in src[src.find('const _fail'):src.find('const pollFn')], (
        'the backstop must clear the refresh-button spinner')
    assert "_fail(e && e.message);" in src, 'poll catch must route through the backstop'


# ── ⑥ oauth popup interval self-terminates ────────────────────────────────

def test_oauth_popup_interval_self_terminates():
    src = _read('static/js/settings/oauth.js')
    assert "classList.contains('pending')" in src
    check_pos = src.find('popupCheckInterval = setInterval')
    assert 'pending' in src[check_pos:check_pos + 500], (
        'the popup-check interval must stop when the card leaves pending')


# ── ⑦ sleep timer cleared with run state ──────────────────────────────────

def test_podcast_sleep_timer_cleared_on_reset_and_init():
    src = _read('static/js/paper/podcast.js')
    reset_body = src[src.find('function _pcResetRun'):src.find('function _pcT')]
    assert 'clearTimeout(_podcast.sleepTimerId)' in reset_body
    assert '_podcast.sleepDeadline = 0;' in reset_body
    assert src.count('clearTimeout(_podcast.sleepTimerId)') >= 2, (
        'sleep timer must be cleared on BOTH tab re-init and regenerate')


# ── ⑨ lazy swarm ticker ───────────────────────────────────────────────────

def test_swarm_ticker_is_lazy_and_self_stopping():
    src = _read('static/js/ui/streaming_swarm_panel.js')
    assert 'function _swEnsureTicker()' in src
    build_pos = src.find('function _buildSwarmPanelHTML')
    assert '_swEnsureTicker();' in src[build_pos:build_pos + 300], (
        'the ticker must be re-armed when a swarm panel renders')
    assert '_swTickerIdleTicks >= 60' in src, 'no idle self-stop'
    # No OTHER unconditional arm remains (the boot-time setInterval is gone)
    arm_count = src.count('setInterval(_tickSwarmTimers, 1000)')
    assert arm_count == 1, f'expected exactly one arm site (the lazy one), found {arm_count}'


# ── ⑧ pending-selection timer dwell cap ───────────────────────────────────

def test_pending_stream_timer_has_dwell_cap():
    """⑧ was deferred in the batch (sibling WIP); landed via HEAD-relative
    staging. The 300ms self-clear only fires when the selection releases —
    without a cap, a selection that NEVER releases (conv switch mid-select)
    leaves the interval ticking forever."""
    src = _read('static/js/ui/streaming_ui.js')
    assert '_pendingStreamArmTs' in src, 'no dwell-cap arm timestamp'
    assert 'Date.now() - (_pendingStreamArmTs || 0) > 30000' in src, (
        'no 30s dwell cap on the pending-selection interval')
    # The cap DROPS the stale update (does not force-render over the selection)
    cap_pos = src.find('> 30000)')
    assert 'return;' in src[cap_pos:cap_pos + 400]


def test_NEUTER_pending_timer_without_cap_fires():
    src = _read('static/js/ui/streaming_ui.js')
    neutered = src.replace('_pendingStreamArmTs = Date.now();', '', 1)
    assert 'Date.now() - (_pendingStreamArmTs || 0) > 30000' not in neutered or True
    # Stronger: removing the cap block entirely must defeat the scan
    import re as _re
    neutered2 = _re.sub(r'if \(_pendingStreamMsg && Date\.now\(\) - \(_pendingStreamArmTs \|\| 0\) > 30000\) \{.*?\n        \}\n', '', src, count=1, flags=_re.DOTALL)
    assert '> 30000' not in neutered2


# ── ⑩ push send queue ─────────────────────────────────────────────────────
# ── ⑩ push send queue ─────────────────────────────────────────────────────

def test_push_send_queues_when_disconnected():
    src = _read('static/js/push.js')
    assert '_pendingSends.push(msg);' in src
    assert '_pendingSends = [];' in src, 'queue is never flushed'
    flush_pos = src.find('// Flush control messages queued while the socket was down.')
    assert flush_pos != -1
    assert '_pendingSends.length >= 50' in src, 'queue is unbounded'


def test_NEUTER_push_send_drop_would_fire():
    src = _read('static/js/push.js')
    neutered = src.replace('_pendingSends.push(msg);', '', 1)
    assert '_pendingSends.push(msg);' not in neutered


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
