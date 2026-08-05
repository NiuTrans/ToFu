#!/usr/bin/env python3
"""Same-role merge observability — designed seams silent, producers alarm.

WHY (epic pt_99eeedbd40424fe6)
------------------------------
``_merge_consecutive_same_role`` (lib/llm_sanitize/_messages.py) merged ANY
consecutive user/assistant pair and logged it at INFO — ~2,000 lines/day in
production (measured 2026-08-03). Offline reconstruction showed 99.9% of those
pairs are the *designed* synthetic-context seams that deliberately place a user
message adjacent to another user message:

  * the CLAUDE.md ``_isMeta`` carrier at index 1
    (``system_context._insert_user_context_message`` — A/B-measured cache win,
    see build_user_context_reminder);
  * the preference-profile blocks riding that carrier;
  * the per-turn "Recently Modified Files" attachment
    (``attachments.inject_attachments``);
  * the coalesced swarm/peer/steer inbox user message
    (``orchestrator._swarm_inbox.drain_and_inject_inbox``).

For those, the merge IS the designated final assembly step — the model-visible
wire is exactly what the seams intended, so the INFO line was pure noise that
also *looked* like a bug being re-patched forever.

The remaining 0.1% are genuine producers (send-race duplicate user rows,
error-ghost adjacency from the DB, endpoint leaks). Those must ALARM — at
WARNING, with the pair's location + a preview — so the source is one grep away
instead of invisible.

THE CONTRACT THIS SUITE PINS
----------------------------
  1. A pair where EITHER side is a synthetic-context message (content starts
     with ``<system-reminder>`` / ``<swarm-update>`` or carries a known marker)
     merges with NO INFO/WARNING (debug only).
  2. Any other pair merges (the provider-safety behavior is unchanged) AND
     fires ONE WARNING carrying the merged-away index ``#i/role`` + a preview.
  3. The accumulator is NOT a laundering channel: after a designed pair fuses,
     a following REAL duplicate user row must still alarm (the fused message
     is no longer "purely synthetic").
  4. The merged CONTENT is byte-identical to the legacy merge (this change is
     observability-only).

NEUTER (standalone main): forcing ``_is_synthetic_context_msg`` to always
return False flips the designed seams into WARNING — tests 1/3/4 FAIL while the
unexpected-pair tests still pass, proving the classifier is load-bearing.

Standalone runner; also importable as pytest functions (no fixtures — the log
capture is a self-contained handler so both runners drive the same code).
"""

import logging
import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

_LOGGER_NAME = 'lib.llm_sanitize._messages'


def _merge_consecutive_same_role(messages):
    """Call-time resolution through ``sys.modules`` — NEVER a top-level
    ``from ... import`` (which would pin the ORIGINAL function object and
    make the in-memory neuter a no-op). The NC harness rebinds the parent
    package's ``_messages`` attribute, so a fresh import here sees the
    neutered module."""
    from lib.llm_sanitize import _messages as _m
    return _m._merge_consecutive_same_role(messages)

_CARRIER_BODY = ('<system-reminder>\n[PROJECT CO-PILOT MODE]\n'
                 'Project: /repo/chatui rules…\n</system-reminder>')
_ATTACHMENT_BODY = ('<system-reminder>\n## Recently Modified Files\n'
                    'Files that were modified earlier…\n</system-reminder>')
_SWARM_BODY = '<swarm-update>\n  <agent-id>a1</agent-id>\n</swarm-update>'


@contextmanager
def _capture(level=logging.DEBUG):
    """Capture records emitted by the sanitize logger (caplog-free so the
    standalone runner drives the exact same assertions)."""
    logger = logging.getLogger(_LOGGER_NAME)
    records = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(r)
    old_level = logger.level
    logger.setLevel(level)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def _carrier():
    """The CLAUDE.md _isMeta context carrier (designed seam, index 1)."""
    return {'role': 'user', '_isMeta': True, 'content': _CARRIER_BODY}


def _user(text):
    return {'role': 'user', 'content': text}


def _at(records, levelno):
    return [r for r in records if r.levelno == levelno]


# ───────────────────────── positive tests ─────────────────────────

def test_designed_carrier_pair_merges_silently():
    """[system, carrier, user] — the 99.9% production case: merges, NO
    INFO/WARNING, and the merged content is byte-identical to the legacy
    string-concat merge (carrier + '\\n\\n' + user)."""
    with _capture() as recs:
        msgs = [{'role': 'system', 'content': 'sys'}, _carrier(), _user('真实用户消息')]
        out = _merge_consecutive_same_role(msgs)
    assert len(out) == 2, f'carrier pair must still merge (got {len(out)})'
    assert out[1]['content'] == _CARRIER_BODY + '\n\n' + '真实用户消息', (
        'merged content drifted from the legacy byte shape')
    assert not _at(recs, logging.INFO), (
        f'designed seam must not log INFO: {[r.getMessage() for r in _at(recs, logging.INFO)]}')
    assert not _at(recs, logging.WARNING), (
        f'designed seam must not log WARNING: {[r.getMessage() for r in _at(recs, logging.WARNING)]}')


def test_unexpected_user_pair_warns_with_location_and_preview():
    """Two plain adjacent user rows (send-race dup / ghost-created adjacency):
    merge still happens (provider safety) AND one WARNING names the merged-away
    index ``#1/user`` + a content preview."""
    with _capture() as recs:
        out = _merge_consecutive_same_role([_user('第一句'), _user('第二句')])
    assert len(out) == 1
    assert out[0]['content'] == '第一句\n\n第二句'
    warns = _at(recs, logging.WARNING)
    assert len(warns) == 1, (
        f'unexpected pair must fire exactly one WARNING, got '
        f'{[r.getMessage() for r in recs]}')
    text = warns[0].getMessage()
    assert '#1/user' in text, f'location token #1/user missing: {text}'
    assert '第二句' in text, f'merged-away preview missing: {text}'


def test_swarm_inbox_pair_is_by_design():
    """The coalesced <swarm-update> inbox user message after the real user
    tail — a designed seam; merges silently."""
    with _capture() as recs:
        out = _merge_consecutive_same_role([_user('真实用户消息'), _user(_SWARM_BODY)])
    assert len(out) == 1
    assert not _at(recs, logging.INFO)
    assert not _at(recs, logging.WARNING)


def test_attachment_reminder_pair_is_by_design():
    """The per-turn 'Recently Modified Files' attachment appended after the
    real user tail — designed seam; merges silently."""
    with _capture() as recs:
        out = _merge_consecutive_same_role([_user('真实用户消息'), _user(_ATTACHMENT_BODY)])
    assert len(out) == 1
    assert not _at(recs, logging.INFO)
    assert not _at(recs, logging.WARNING)


def test_fused_accumulator_does_not_launder_a_real_dup():
    """[system, carrier, user1, user1-dup]: pair 1 (carrier+user1) is designed
    and silent, but pair 2 (fused-accumulator + a REAL duplicate user row)
    must still alarm — the fused message is no longer purely synthetic, so a
    genuine bad-data producer can never hide behind the carrier."""
    with _capture() as recs:
        msgs = [{'role': 'system', 'content': 'sys'}, _carrier(),
                _user('真实用户消息'), _user('真实用户消息')]
        out = _merge_consecutive_same_role(msgs)
    assert len(out) == 2, f'both pairs merge (got {len(out)})'
    warns = _at(recs, logging.WARNING)
    assert len(warns) == 1, (
        f'the real dup must fire exactly one WARNING, got '
        f'{[r.getMessage() for r in recs]}')
    assert '#3/user' in warns[0].getMessage(), (
        f'location of the merged-away dup missing: {warns[0].getMessage()}')


def test_unexpected_assistant_pair_warns():
    """Adjacent plain assistant rows (endpoint leak / DB drift) alarm too."""
    with _capture() as recs:
        msgs = [{'role': 'assistant', 'content': '甲'},
                {'role': 'assistant', 'content': '乙'}]
        out = _merge_consecutive_same_role(msgs)
    assert len(out) == 1
    assert out[0]['content'] == '甲\n\n乙'
    warns = _at(recs, logging.WARNING)
    assert len(warns) == 1
    assert '#1/assistant' in warns[0].getMessage()


_POSITIVE = [
    test_designed_carrier_pair_merges_silently,
    test_unexpected_user_pair_warns_with_location_and_preview,
    test_swarm_inbox_pair_is_by_design,
    test_attachment_reminder_pair_is_by_design,
    test_fused_accumulator_does_not_launder_a_real_dup,
    test_unexpected_assistant_pair_warns,
]


# ───────────────────────── on-disk-free neuter ─────────────────────────

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TARGET = os.path.join(_ROOT, 'lib', 'llm_sanitize', '_messages.py')

# Anchor: the synthetic-context predicate's prefix classification. Neutering
# it to always-False makes every pair "unexpected", so the designed seams
# flip into WARNING and the silent-tests fail.
_NC_FIND = '    if head.startswith(_SYNTHETIC_PAIR_PREFIXES):\n'
_NC_REPL = ('    return False  # NC: neuter synthetic-context classification\n'
            '    if head.startswith(_SYNTHETIC_PAIR_PREFIXES):\n')


def _run(fn):
    try:
        fn()
        print(' ', '\033[32m✓\033[0m', fn.__name__)
        return True
    except AssertionError as e:
        print(' ', f'\033[31m✗\033[0m {fn.__name__}: {e}')
        return False
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(' ', f'\033[31m✗\033[0m {fn.__name__}: unexpected {type(e).__name__}: {e}')
        return False


def main():
    print()
    print('\033[36m═══ same-role merge classification + neuter ═══\033[0m')
    print()
    print('\033[36mBaseline (shipped classifier):\033[0m')
    if not all([_run(fn) for fn in _POSITIVE]):
        sys.exit('baseline failed — fix the classifier before neutering')

    print()
    print('\033[36mNC — classifier is load-bearing (always-False):\033[0m')
    from tests._nc_harness import neutered_source
    with neutered_source(_TARGET, _NC_FIND, _NC_REPL):
        silent_ok = _run(test_designed_carrier_pair_merges_silently)
        swarm_ok = _run(test_swarm_inbox_pair_is_by_design)
        attach_ok = _run(test_attachment_reminder_pair_is_by_design)
        warn_ok = _run(test_unexpected_user_pair_warns_with_location_and_preview)
    if silent_ok or swarm_ok or attach_ok:
        sys.exit('NC: a designed-seam test PASSED with the classifier neutered — '
                 'classification is not load-bearing!')
    if not warn_ok:
        sys.exit('NC: unexpected-pair control failed — neuter had unintended blast radius')
    print(' ', '\033[32m✓\033[0m NC: designed-seam tests FAIL with classifier off; '
          'unexpected-pair control still passes')

    print()
    print('\033[36mPost-restore baseline:\033[0m')
    if not all([_run(fn) for fn in _POSITIVE]):
        sys.exit('post-restore baseline failed — module not restored correctly')
    print()
    print('\033[32m═══ ALL MERGE-CLASSIFICATION TESTS + NEUTER PASSED ═══\033[0m')
    print()


if __name__ == '__main__':
    main()
