#!/usr/bin/env python3
"""Editing an assistant/critic/VU turn in place must keep the SEGMENT SoT
consistent with the edited deliverable.

WHY
---
A finished assistant turn stores its answer BOTH as ``content`` and as the
terminal ``deliverable:true`` ``text`` segment in ``segments`` — and the
segment list is the authoritative render/wire source (``deliverable_text`` /
``derive_content`` read it FIRST, and the next-turn wire rebuild in
conv_message_builder reads segments when present). The chatInner "Edit → Save"
affordance PATCHes only ``content``; without realignment the stored segments
keep the PRE-EDIT answer, so a segment-driven read (headless/compat, or the
next turn's context) resurfaces the stale text — an Edit button that saves to a
field the SoT ignores.

Covers:
  1. ``apply_edited_deliverable`` (pure): rewrites ONLY the terminal deliverable
     text; thinking / narration / tool_use segments untouched; no-op returns
     None (empty list, already-consistent); appends when no terminal exists and
     content is non-empty; empty content + no terminal → None.
  2. ``derive_content`` over the realigned list == the edited content (the
     inverse-projection round-trip).
  3. The REAL PATCH route (``_patch_message_blocking``) realigns the persisted
     segments when ``content`` is edited.

NEUTER: monkeypatch ``apply_edited_deliverable`` → identity (return input
unchanged / None) so the route's realign is dead → the persisted terminal
segment keeps the pre-edit text while ``content`` changed → divergence assert
fails. Proves the realign call is load-bearing.

Standalone runner (real DB) + pytest.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ── Pure-helper tests ──────────────────────────────────────────────────

def _segs_with_terminal():
    return [
        {'type': 'thinking', 'text': 'reasoning', 'deliverable': False, 'llmRound': 0},
        {'type': 'text', 'text': 'let me check', 'deliverable': False, 'llmRound': 0},
        {'type': 'tool_use', 'id': 'tc1', 'name': 'read_files', 'input': '{}',
         'llmRound': 0, 'result': {'content': 'file body', 'status': 'done'}},
        {'type': 'thinking', 'text': 'final reasoning', 'deliverable': False, 'terminal': True},
        {'type': 'text', 'text': 'ORIGINAL ANSWER', 'deliverable': True, 'terminal': True},
    ]


def test_pure_rewrites_only_terminal_deliverable():
    from lib.tasks_pkg.segments import apply_edited_deliverable, derive_content
    out = apply_edited_deliverable(_segs_with_terminal(), 'EDITED ANSWER')
    assert out is not None, 'expected a realigned list'
    # The terminal deliverable is the edited text; round-trips through derive.
    assert derive_content(out) == 'EDITED ANSWER'
    # Every non-terminal-deliverable segment survives untouched.
    kinds = [(s['type'], s.get('text'), s.get('deliverable'), s.get('terminal'))
             for s in out]
    assert ('thinking', 'reasoning', False, None) in kinds
    assert ('text', 'let me check', False, None) in kinds
    assert any(s['type'] == 'tool_use' and s['id'] == 'tc1' for s in out)
    assert ('thinking', 'final reasoning', False, True) in kinds
    # Exactly one terminal deliverable, carrying the new text.
    terms = [s for s in out if s.get('terminal') and s.get('deliverable')]
    assert len(terms) == 1 and terms[0]['text'] == 'EDITED ANSWER'
    _ok('pure helper rewrites ONLY the terminal deliverable; round-trips via derive_content')


def test_pure_noops_return_none():
    from lib.tasks_pkg.segments import apply_edited_deliverable
    # Empty / None input → None (nothing to keep consistent).
    assert apply_edited_deliverable(None, 'x') is None
    assert apply_edited_deliverable([], 'x') is None
    # Already-consistent → None (no write needed).
    assert apply_edited_deliverable(_segs_with_terminal(), 'ORIGINAL ANSWER') is None
    _ok('pure helper is a no-op (None) on empty input and when already consistent')


def test_pure_appends_when_no_terminal():
    from lib.tasks_pkg.segments import apply_edited_deliverable, derive_content
    tool_only = [
        {'type': 'text', 'text': 'narration', 'deliverable': False, 'llmRound': 0},
        {'type': 'tool_use', 'id': 'tc1', 'name': 'x', 'input': '{}', 'llmRound': 0,
         'result': {'content': 'r', 'status': 'done'}},
    ]
    out = apply_edited_deliverable(tool_only, 'NEW ANSWER')
    assert out is not None and derive_content(out) == 'NEW ANSWER'
    assert len(out) == 3 and out[-1]['terminal'] and out[-1]['deliverable']
    # No terminal + empty content → None (don't synthesize an empty segment).
    assert apply_edited_deliverable(tool_only, '') is None
    _ok('pure helper appends a terminal deliverable when none exists (and skips empty)')


# ── DB-backed PATCH-route tests ────────────────────────────────────────

def _seed(db, conv_id, messages):
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'edit-seg-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms, 'settings': '{}',
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings'], retry=True)
    db.commit()


def _read_msg(db, conv_id, idx):
    import json
    r = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                   (conv_id,)).fetchone()
    raw = r[0] if not isinstance(r, dict) else r['messages']
    return json.loads(raw or '[]')[idx]


def _cleanup(db, *conv_ids):
    from lib.database import db_execute_with_retry
    for cid in conv_ids:
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (cid,))
    db.commit()


def _seed_msgs():
    return [
        {'role': 'user', 'content': 'question', '_msgId': 'u0'},
        {'role': 'assistant', 'content': 'ORIGINAL ANSWER', '_msgId': 'a0',
         'toolRounds': [], 'segments': _segs_with_terminal()},
    ]


def test_patch_route_realigns_segment():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg.segments import derive_content
    from routes.conversations import _patch_message_blocking
    conv_id = 'cv-edit-seg'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _seed_msgs())
    try:
        _patch_message_blocking(db, conv_id, 1, {'content': 'EDITED ANSWER'})
        msg = _read_msg(db, conv_id, 1)
        assert msg['content'] == 'EDITED ANSWER'
        assert derive_content(msg['segments']) == 'EDITED ANSWER', \
            f'segment SoT stale: derive={derive_content(msg["segments"])!r}'
    finally:
        _cleanup(db, conv_id)
    _ok('PATCH route realigns the persisted terminal deliverable segment to the edit')


def _neuter_and_subrun():
    """NC: monkeypatch apply_edited_deliverable → always None (dead realign).
    The route then persists edited content but leaves segments stale →
    divergence. Proves the realign call is load-bearing.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg import segments as seg_mod
    import routes.conversations as rc
    from routes.conversations import _patch_message_blocking
    conv_id = 'cv-edit-seg-nc'
    db = get_thread_db(DOMAIN_CHAT)
    _orig = seg_mod.apply_edited_deliverable
    _seed(db, conv_id, _seed_msgs())
    try:
        # The route imports the symbol locally from lib.tasks_pkg.segments, so
        # patch it at the definition module.
        seg_mod.apply_edited_deliverable = lambda segs, content: None
        _patch_message_blocking(db, conv_id, 1, {'content': 'EDITED ANSWER'})
        msg = _read_msg(db, conv_id, 1)
        stale = (msg['content'] == 'EDITED ANSWER'
                 and seg_mod.derive_content(_orig(msg['segments'], msg['content']) or msg['segments'])
                 != 'EDITED ANSWER')
        # Simpler ground truth: with realign dead, the stored terminal segment
        # still holds the pre-edit text.
        term = [s for s in msg['segments'] if s.get('terminal') and s.get('deliverable')]
        stale = bool(term) and term[0]['text'] == 'ORIGINAL ANSWER' and msg['content'] == 'EDITED ANSWER'
        return stale, f"segment text={term[0]['text'] if term else None!r} content={msg['content']!r}"
    finally:
        seg_mod.apply_edited_deliverable = _orig
        _cleanup(db, conv_id)


_POSITIVE = [
    test_pure_rewrites_only_terminal_deliverable,
    test_pure_noops_return_none,
    test_pure_appends_when_no_terminal,
    test_patch_route_realigns_segment,
]


def _run(fn):
    try:
        fn()
        return True
    except AssertionError as e:
        print(' ', _color('✗', '31'), f'{fn.__name__}: {e}')
        return False
    except Exception:
        import traceback
        traceback.print_exc()
        return False


def main():
    print()
    print(_color('═══ Edit realigns segment SoT + neuter ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_edit_realigns_segments.__main__')

    print(_color('Baseline:', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('baseline failed')

    print()
    print(_color('NC — neuter apply_edited_deliverable, repeat the edit:', '36'))
    stale, out = _neuter_and_subrun()
    if not stale:
        _fail('NC did not confirm the realign is load-bearing:\n' + out)
    _ok('NC: with realign dead, segment keeps pre-edit answer while content changed (load-bearing)')

    print()
    print(_color('═══ ALL EDIT-SEGMENT-REALIGN TESTS + NEUTER PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
