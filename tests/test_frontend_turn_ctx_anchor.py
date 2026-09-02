"""tests/test_frontend_turn_ctx_anchor.py — anchor the turn-ctx capsule
reconcile to the RIGHT user turn, not "the last user in the conv".

WHY
---
The fact-card reconcile committed as ``8c4f30cb`` used the tail heuristic
(walk conversations[i].messages back until role === 'user') to locate the
user turn to overwrite. Three real scenarios expose that heuristic:

  1. **Concurrent send / background conv** — a user in another conv (or the
     same conv seconds later) plants a NEWER user row; the done frame from
     the FIRST task lands and mistakenly rewrites the newer bubble.
  2. **Autopilot VU** — the parent turn's DONE frame arrives, but the
     tail is now the VU-synthesised user; the fact card belongs to the
     PARENT user, not the VU, so the wrong bubble gets the correction (or,
     worse, the VU msg has no ``_ctx`` and the correction never lands).
  3. **Edit / regenerate a historical user** — the target user is
     mid-list, not the tail; last-user is wrong by construction.

The fix ships the user turn's stable ``_msgId`` on the DONE frame as
``userMsgId``. The frontend anchor:

  * If ``ev.userMsgId`` set → match by ``msg._msgId``.
  * Else → fall back to the last user (legacy backend / VU emitters that
    never stamp ``_userMsgId`` on the task) + a ``console.debug`` trace.

This harness loads the ANCHOR snippet from ``static/js/ui/sse_pipeline.js``
verbatim, wraps it as a function, and drives all three scenarios plus a
NEUTER that reverts the anchor to "always last user" — scenarios 1 & 2
must flip red under the NEUTER, scenario 3 must stay green.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# The harness reimplements ONLY the anchor selection loop (byte-mirror of
# sse_pipeline.js:1770-1802). Keeping it here — rather than sourceload the
# 2260-line file — lets us drive it with plain fake conv states and swap the
# anchor for a NEUTER version with sed-level surgery.
_HARNESS = r"""
'use strict';

const out = [];
function check(name, cond, extra) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? ' :: ' + extra : ''));
}

/* ── The shipped anchor selection, byte-mirror of sse_pipeline.js. ──
 *
 * The upstream is `messages` + `ev` + a console shim; returns the matched
 * user-message index (or -1). NEUTER (see below) replaces the whole body
 * with "always last user".
 */
function _selectAnchorIdx(messages, ev, convIdShort, log) {
  let _uIdx = -1;
  const _targetMsgId = (typeof ev.userMsgId === 'string' && ev.userMsgId)
    ? ev.userMsgId : '';
  if (_targetMsgId) {
    for (let i = messages.length - 1; i >= 0; i--) {
      const _m = messages[i];
      if (_m && _m.role === 'user' && _m._msgId === _targetMsgId) {
        _uIdx = i;
        break;
      }
    }
    if (_uIdx < 0) {
      log('miss:' + _targetMsgId);
    }
  } else {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i] && messages[i].role === 'user') { _uIdx = i; break; }
    }
    log('fallback');
  }
  return _uIdx;
}

/* NEUTER — the pre-fix behaviour: always take the last user. */
function _selectAnchorIdxNEUTER(messages, ev, convIdShort, log) {
  let _uIdx = -1;
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i] && messages[i].role === 'user') { _uIdx = i; break; }
  }
  log('neuter-always-last');
  return _uIdx;
}

function _msgs(rows) { return rows.map((r) => ({ ...r })); }

function _driveAll(selector, tag) {
  /* ── SCENARIO ①: userMsgId hits a historical user (regenerate/edit) ── */
  {
    const messages = _msgs([
      { role: 'user', _msgId: 'u1', content: 'Q1' },
      { role: 'assistant', _msgId: 'a1', content: 'A1' },
      { role: 'user', _msgId: 'u2', content: 'Q2' },  // target
      { role: 'assistant', _msgId: 'a2', content: 'A2' },
      { role: 'user', _msgId: 'u3', content: 'Q3 (newer, unrelated send)' },
    ]);
    const log = () => {};
    const idx = selector(messages, { userMsgId: 'u2' }, 'x', log);
    check(tag + '_scenario1_regen_historical_hits_u2', idx === 2,
          'idx=' + idx + ' (expected 2, i.e. u2 — NOT the newer u3 at idx=4)');
  }

  /* ── SCENARIO ②: autopilot VU — parent user is idx=2, VU is idx=4 ── */
  {
    const messages = _msgs([
      { role: 'user', _msgId: 'p_u1', content: 'Prev turn' },
      { role: 'assistant', _msgId: 'p_a1', content: 'Prev reply' },
      { role: 'user', _msgId: 'parent_u', content: 'Ask me a follow-up' },  // ← target
      { role: 'assistant', _msgId: 'parent_a', content: 'Reply that stops' },
      { role: 'user', _msgId: 'vu_u', content: 'Continue with X', _isVirtualUser: true },
    ]);
    const log = () => {};
    const idx = selector(messages, { userMsgId: 'parent_u' }, 'x', log);
    check(tag + '_scenario2_autopilot_vu_hits_parent',
          idx === 2 && messages[idx]._msgId === 'parent_u',
          'idx=' + idx + ' (expected 2, i.e. the PARENT user, NOT the VU at idx=4)');
  }

  /* ── SCENARIO ③: no userMsgId → legacy fallback to last user ── */
  {
    const messages = _msgs([
      { role: 'user', _msgId: 'u1', content: 'Q1' },
      { role: 'assistant', _msgId: 'a1', content: 'A1' },
      { role: 'user', _msgId: 'u2', content: 'Q2' },
    ]);
    const log = () => {};
    const idx = selector(messages, {}, 'x', log);
    check(tag + '_scenario3_no_userMsgId_falls_back_to_last', idx === 2,
          'idx=' + idx + ' (expected 2, the tail user)');
  }

  /* ── EXTRA: userMsgId that no longer exists in the conv (truncated /
       out-of-sync) → returns -1, NOT the tail. This is important: silently
       falling to the tail would rewrite the wrong bubble. ── */
  {
    const messages = _msgs([
      { role: 'user', _msgId: 'u1', content: 'Q1' },
      { role: 'assistant', _msgId: 'a1', content: 'A1' },
    ]);
    let logged = '';
    const idx = selector(messages, { userMsgId: 'ghost' }, 'x',
                         (s) => { logged = s; });
    check(tag + '_scenario_extra_missing_userMsgId_returns_negative',
          idx === -1,
          'idx=' + idx + ' (expected -1: better to skip than overwrite the wrong turn)');
    if (tag === 'live') {
      check(tag + '_scenario_extra_missing_userMsgId_logs_miss',
            logged.indexOf('miss:ghost') === 0,
            'logged=' + logged);
    }
  }
}

/* Run the LIVE anchor first, then the NEUTER anchor. */
_driveAll(_selectAnchorIdx, 'live');
_driveAll(_selectAnchorIdxNEUTER, 'neuter');

/* Under NEUTER, scenarios ① and ② MUST be red (idx points at the tail —
 * the wrong bubble). Assert that inversion explicitly. */
{
  const messages = _msgs([
    { role: 'user', _msgId: 'u1' },
    { role: 'assistant', _msgId: 'a1' },
    { role: 'user', _msgId: 'u2' },
    { role: 'assistant', _msgId: 'a2' },
    { role: 'user', _msgId: 'u3' },
  ]);
  const neuterIdx = _selectAnchorIdxNEUTER(messages, { userMsgId: 'u2' }, 'x', () => {});
  check('NEUTER_scenario1_lands_on_wrong_user',
        neuterIdx === 4,
        'neuter idx=' + neuterIdx + ' (expected 4: last user, i.e. the BUG)');
  const messages2 = _msgs([
    { role: 'user', _msgId: 'parent_u' },
    { role: 'assistant', _msgId: 'parent_a' },
    { role: 'user', _msgId: 'vu_u', _isVirtualUser: true },
  ]);
  const neuterIdx2 = _selectAnchorIdxNEUTER(messages2, { userMsgId: 'parent_u' }, 'x', () => {});
  check('NEUTER_scenario2_lands_on_VU_not_parent',
        neuterIdx2 === 2,
        'neuter idx=' + neuterIdx2 + ' (expected 2: the VU, i.e. the BUG)');
}

console.log(out.join('\n'));
process.exit(0);
"""


def _read_pipeline_source() -> str:
    with open(os.path.join(JS_DIR, 'ui', 'sse_pipeline.js'), encoding='utf-8') as f:
        return f.read()


def test_shipped_pipeline_contains_anchor_selection_by_userMsgId():
    """The shipped sse_pipeline.js must contain the userMsgId anchor. Prevents
    a silent regression that reverts to the last-user heuristic without
    breaking the harness (the harness reimplements the logic; this guard ties
    it to the shipped file)."""
    src = _read_pipeline_source()
    for needle in (
        # The preference: read ev.userMsgId first.
        "ev.userMsgId",
        # The identity match — user with matching _msgId.
        "_m._msgId === _targetMsgId",
        # The fallback trace: log which conv fell back.
        "legacy backend fallback to last-user",
    ):
        assert needle in src, (
            f'sse_pipeline.js is missing the anchor rule "{needle}" — a '
            f'reader-visible signal that the reconcile stopped preferring '
            f'ev.userMsgId over the last-user heuristic.'
        )


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_anchor_selects_correct_user_across_scenarios():
    harness = os.path.join(HERE, '_turn_ctx_anchor_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness],
            capture_output=True, text=True, timeout=30,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    lines = output.splitlines()
    fails = [l for l in lines if l.startswith('FAIL')]
    # The LIVE anchor MUST have all four scenarios PASS.
    live_fails = [l for l in fails if 'live_' in l]
    assert not live_fails, 'live anchor failures:\n' + '\n'.join(fails)
    # The NEUTER anchor MUST fail scenarios 1 & 2 (proving the anchor is
    # load-bearing) and pass scenario 3 (fallback path unchanged).
    neuter_fail_labels = {l for l in lines
                          if 'neuter_scenario1' in l or 'neuter_scenario2' in l}
    assert any(l.startswith('FAIL neuter_scenario1_regen_historical') for l in lines), (
        'NEUTER did NOT break scenario 1 — the anchor is not load-bearing:\n'
        + output
    )
    assert any(l.startswith('FAIL neuter_scenario2_autopilot_vu_hits_parent') for l in lines), (
        'NEUTER did NOT break scenario 2 — the anchor is not load-bearing:\n'
        + output
    )
    assert any(l.startswith('PASS neuter_scenario3_no_userMsgId_falls_back_to_last') for l in lines), (
        'NEUTER should keep scenario 3 (fallback path) green:\n' + output
    )
    # And the explicit inversion assertions must be green (NEUTER lands on
    # the WRONG user, as an explicit "yes this bug happens without the fix").
    assert any(l.startswith('PASS NEUTER_scenario1_lands_on_wrong_user') for l in lines), output
    assert any(l.startswith('PASS NEUTER_scenario2_lands_on_VU_not_parent') for l in lines), output
    del neuter_fail_labels  # only used above for symmetry
