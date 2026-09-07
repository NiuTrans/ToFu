"""Drift-guard: browser-bridge 401 backoff + needs-repair state.

WHY
---
2026-08-01: the extension's fixed-cadence 401 retry (every 9s, forever)
produced ~400 auth warnings/hour on the server (3104 in a day) — a
wrong/missing bridge secret can never succeed, so a fixed retry is pure
log spam on BOTH ends. The fix:

  * exponential backoff per consecutive 401 (9s → … → 5 min cap),
  * after AUTH_GIVE_UP_AFTER consecutive 401s the bridge enters a
    needs-repair state (orange KEY badge + re-pair message) and parks at
    a 5-minute probe (self-healing if the secret is fixed server-side),
  * any success resets the backoff,
  * every next-poll schedule rides ONE cancelable timer
    (``_scheduleNextPoll``) so an adopted key (the auto-repair ladder, or
    the advanced popup field) reconnects instantly instead of waiting out
    a parked probe,
  * the state is exposed via getStatus (authFailures / needsRepair).

JS cannot run under pytest, so behavior is pinned by structural analysis
of the poll/branch bodies (same discipline as test_browser_tooling_fixes).
"""

import os
import re

import pytest

pytestmark = pytest.mark.unit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel='browser_extension/background.js'):
    with open(os.path.join(REPO, rel), encoding='utf-8') as f:
        return f.read()


def _extract_fn_body(src, fn_signature):
    start = src.index(fn_signature)
    brace = src.index('{', start)
    depth = 0
    i = brace
    while i < len(src):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return src[brace:i + 1]
        i += 1
    raise AssertionError('unbalanced braces for ' + fn_signature)


def _poll_body(src):
    return _extract_fn_body(src, 'async function poll(')


def _401_branch(body):
    """Slice the WHOLE 401 branch, anchored on its structural end.

    A fixed-width window (i401 + N chars) broke the moment the branch grew
    (2026-08-04: the auto-repair classification added ~600 chars and the
    backoff formula slid past the window) — anchor on the terminal
    `_scheduleNextPoll(delay);` instead, so the slice tracks the branch
    however long it gets."""
    i401 = body.index('resp.status === 401')
    i_end = body.index('_scheduleNextPoll(delay);', i401)
    return body[i401:i_end + len('_scheduleNextPoll(delay);')]


def test_401_backs_off_exponentially_with_cap():
    body = _poll_body(_src())
    branch = _401_branch(body)
    assert re.search(
        r'Math\.min\(\s*AUTH_RETRY_BASE_DELAY\s*\*\s*\(2\s*\*\*\s*\(authFailures\s*-\s*1\)\)\s*,\s*AUTH_RETRY_MAX_DELAY\)',
        branch), ('the 401 branch must back off exponentially per consecutive '
                  'failure, capped at the parked probe cadence — a fixed retry '
                  'is pure auth-log spam (measured 2026-08-01)')
    assert 'authFailures += 1' in branch, 'must count consecutive 401s'


def test_needs_repair_state_after_give_up_threshold():
    body = _poll_body(_src())
    branch = _401_branch(body)
    assert 'authFailures >= AUTH_GIVE_UP_AFTER' in branch, (
        'after N consecutive 401s the bridge must declare needs-repair '
        '(parked probe), not keep retrying eagerly')
    assert "updateBadge(needsRepair ? 'repair' : 'error')" in branch, (
        'needs-repair must surface on the badge (KEY), not just in logs')
    assert 're-pair' in branch, (
        'the error text must speak of re-pairing (2026-08-04: the ladder '
        'does it automatically — the copy promises automation, never a '
        'human chore)')


def test_success_resets_the_backoff():
    body = _poll_body(_src())
    i_ok = body.index('await resp.json()')
    i_reset = body.index('_resetAuthBackoff()', i_ok)
    i_badge = body.index("updateBadge('on')", i_ok)
    assert i_ok < i_reset < i_badge, (
        'a successful poll must reset the auth backoff before going green')


def test_single_pending_timer_invariant():
    src = _src()
    bare = re.findall(r'setTimeout\(poll,', src)
    assert bare == [], (
        f'{len(bare)} bare setTimeout(poll, …) call(s) remain — every '
        'next-poll schedule must ride _scheduleNextPoll so only one pending '
        'timer can exist and a user action can cancel a parked probe')
    body = _extract_fn_body(src, 'function _scheduleNextPoll(')
    assert 'clearTimeout(_retryTimer)' in body, (
        '_scheduleNextPoll must cancel any pending timer before arming a new one')


def test_secret_change_reconnects_immediately():
    body = _extract_fn_body(_src(), 'function setBridgeSecret(')
    assert '_resetAuthBackoff()' in body, (
        'a new secret must drop the backoff state')
    assert '_scheduleNextPoll(0)' in body, (
        'a new secret must cancel the parked 5-min probe and poll NOW — '
        'otherwise the user waits minutes to learn whether the fix worked')


def test_status_exposes_repair_state():
    src = _src()
    i = src.index("msg.type === 'getStatus'")
    block = src[i:i + 700]
    assert 'needsRepair' in block and 'authFailures' in block, (
        'getStatus must expose authFailures + needsRepair so the popup can '
        'render the re-pair state')


def test_neuter_bites_on_fixed_retry_regression():
    # The exact pre-fix regression: a fixed-cadence 401 retry. If someone
    # reverts to `setTimeout(poll, POLL_RETRY_DELAY * 3)` the exponential
    # formula AND the single-timer invariant both disappear.
    body = _poll_body(_src())
    neutered = re.sub(
        r'Math\.min\(\s*AUTH_RETRY_BASE_DELAY\s*\*\s*\(2\s*\*\*\s*\(authFailures\s*-\s*1\)\)\s*,\s*AUTH_RETRY_MAX_DELAY\)',
        'POLL_RETRY_DELAY * 3', body)
    assert not re.search(r'2\s*\*\*\s*\(authFailures', neutered), (
        'sanity: neuter removed the exponential formula (the guard above '
        'keys on it, so the regression is caught)')


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v', '-p', 'no:napari']))
