#!/usr/bin/env python3
"""Regression: a WARM ``Last-Event-ID`` SSE resume must reassert the FULL
in-memory ``toolRounds`` snapshot BEFORE replaying the missed deltas.

WHY
---
Root cause of the reported "the first round of tool calls displayed is already
the tenth round" bug (conv ``mrbf9px2g5mct3``, 2026-07-08). The warm-resume
branch in ``routes/chat.py::chat_stream`` (``if resume_from is not None:``)
replayed ONLY the events *after* the client's Last-Event-ID cursor and NEVER
re-emitted a ``state`` snapshot — it trusted the client's cached ``toolRounds``
to already hold rounds 1..N-1. But a reconnect that landed on a fresh empty
assistant placeholder (``initActiveTasks`` Case A stale-tail /
``connectToTask`` stale-turn guard, ``toolRounds: []``) has NO cached rounds,
so the delta-only replay rendered starting at whatever round the first missed
``tool_start`` carried (round 10) — the earlier nine rounds were stranded.

Verified before this fix (both against the code):
  * only the warm-resume branch fired for this conv's tasks — no
    ``full-snapshot resync`` line, ruling out the cold / served-from-DB paths;
  * ``task['toolRounds']`` is reset ONLY at turn-start (orchestrator) and
    mutated SOLELY by append (executor) — no pop/slice/cap → the live list is
    COMPLETE, never lossy.

THE FIX
-------
Emit a leading ``state`` event carrying the complete ``task['toolRounds']``
(+ content/thinking), read under ``task['events_lock']``, with NO ``id:``
field (mirroring the fresh-connection snapshot so it can't collide with the
first replayed event's cursor). The frontend's ``_snapshotLongerRounds``
keep-longer guard ADOPTS it when the client cache is short (fixes the strand)
and harmlessly IGNORES it when the client is already equal/longer — so a
shorter buffer can never collapse a longer one.

TESTS
-----
  1. BEHAVIORAL (end-to-end, drives the REAL ``chat_stream`` via a Quart test
     client with a ``Last-Event-ID`` header): the FIRST ``data:`` frame is a
     ``state`` event whose ``toolRounds`` holds ALL rounds 1..N, and it is
     emitted BEFORE the replayed post-cursor deltas.
  2. SOURCE NEUTER: the warm-resume block must build ``resume_state`` with the
     full ``task['toolRounds']`` and yield it before the replay loop. Removing
     the ``resume_state`` build/emit reverts to the delta-only strand.
"""

import ast
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Install Flask→Quart shim before importing routes (mirrors conftest).
import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

_CHAT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'routes', 'chat.py')


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _parse_sse_frames(raw: str):
    """Split a raw SSE body into (id, json-payload-dict) frames.

    Comment/keepalive lines (``:``-prefixed) are skipped. A frame with no
    ``id:`` line yields id=None (the synthetic snapshot shape)."""
    import json
    frames = []
    for block in raw.split('\n\n'):
        block = block.strip('\n')
        if not block:
            continue
        eid = None
        data_lines = []
        is_comment = False
        for line in block.split('\n'):
            if line.startswith(':'):
                is_comment = True
                continue
            if line.startswith('id:'):
                eid = line[3:].strip()
            elif line.startswith('data:'):
                data_lines.append(line[5:].strip())
        if is_comment and not data_lines:
            continue
        if not data_lines:
            continue
        try:
            payload = json.loads('\n'.join(data_lines))
        except Exception:
            payload = None
        frames.append((eid, payload))
    return frames


# ══════════════════════════════════════════════════════════════════════
#  1. BEHAVIORAL — real chat_stream, warm resume, first frame carries all rounds
# ══════════════════════════════════════════════════════════════════════
def test_warm_resume_first_frame_is_full_toolrounds_snapshot():
    import importlib.util
    from lib import auth_mode as _auth_mode

    _prev_mode_env = os.environ.pop('TOFU_AUTH_MODE', None)
    _auth_mode.reset_for_tests()
    _auth_mode.set_mode('open', set_by='warm-resume-test')
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)), 'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    app = mod.app

    from lib.tasks_pkg.manager import create_task, append_event
    from lib.agent_core.events import EventType, build_event

    async def _t():
        task = create_task('cv-warm-resume', [{'role': 'user', 'content': 'q'}], {})
        # Simulate a turn that reached round 10: ten completed tool rounds live
        # in task['toolRounds'], and a matching stream of tool events.
        rounds = []
        for rn in range(1, 11):
            entry = {'roundNum': rn, 'query': f'search {rn}', 'status': 'done',
                     'toolName': 'grep_search', 'results': [{'title': f'r{rn}'}]}
            rounds.append(entry)
            append_event(task, build_event(EventType.TOOL_START, roundNum=rn,
                                            query=f'search {rn}', toolName='grep_search'))
            append_event(task, build_event(EventType.TOOL_RESULT, roundNum=rn,
                                            query=f'search {rn}', results=entry['results']))
        task['toolRounds'] = rounds
        task['content'] = 'partial answer so far'

        # The client's last-received event id (mid-turn — e.g. it saw events
        # up to just before round 10's tool_start). We resume from there.
        resume_cursor = len(task['events']) - 3  # a few missed events remain

        # Mark the task terminal (append a done event + flip status) BEFORE
        # consuming the body, so the warm-resume replay hits the done frame and
        # the generator RETURNS instead of blocking on the live streaming loop.
        # The reasserted snapshot is emitted before the replay regardless, so
        # this does not affect the invariant under test.
        append_event(task, build_event(EventType.DONE, finishReason='stop'))
        task['status'] = 'done'
        task['finishReason'] = 'stop'

        async with app.test_client() as client:
            resp = await client.get(
                f'/api/chat/stream/{task["id"]}',
                headers={'Last-Event-ID': str(resume_cursor)},
            )
            assert resp.status_code == 200, f'got {resp.status_code}'
            raw = await resp.get_data(as_text=True)

        frames = _parse_sse_frames(raw)
        assert frames, f'no SSE frames parsed from body:\n{raw[:500]}'

        # THE INVARIANT: the FIRST data frame is a synthetic `state` snapshot
        # (no id:) carrying the FULL toolRounds (all 10 rounds), emitted BEFORE
        # any replayed post-cursor delta/tool event.
        first_eid, first_payload = frames[0]
        assert first_payload is not None, f'first frame unparseable: {frames[0]}'
        assert first_payload.get('type') == 'state', (
            f'first warm-resume frame must be a state snapshot, got '
            f'type={first_payload.get("type")!r}')
        assert first_eid is None, (
            'the reasserted state snapshot must carry NO id: (synthetic, like '
            f'the fresh path) — got id={first_eid!r}')
        tr = first_payload.get('toolRounds')
        assert tr is not None, 'state snapshot omitted toolRounds entirely'
        assert len(tr) == 10, (
            f'warm-resume state snapshot must carry ALL 10 rounds so keep-longer '
            f'adopts them, got {len(tr)} — client would strand at a later round')
        nums = [r.get('roundNum') for r in tr]
        assert nums == list(range(1, 11)), f'rounds out of order / missing: {nums}'
        assert first_payload.get('content') == 'partial answer so far'

    try:
        asyncio.run(_t())
    finally:
        _auth_mode.reset_for_tests()
        if _prev_mode_env is not None:
            os.environ['TOFU_AUTH_MODE'] = _prev_mode_env
        else:
            os.environ['TOFU_AUTH_MODE'] = 'private'
        _auth_mode.reset_for_tests()
    _ok('warm resume emits a leading full-toolRounds state snapshot (all 10 rounds, no id:)')


# ══════════════════════════════════════════════════════════════════════
#  2. SOURCE NEUTER — the warm-resume block must build+emit resume_state
# ══════════════════════════════════════════════════════════════════════
def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def test_source_warm_resume_builds_and_emits_full_snapshot():
    src = open(_CHAT_PY, encoding='utf-8').read()
    tree = ast.parse(src)
    fn = _find_func(tree, 'chat_stream')
    assert fn is not None, 'chat_stream not found in routes/chat.py'
    scoped = ast.get_source_segment(src, fn)

    # The warm-resume branch must BUILD a resume_state snapshot ...
    assert 'resume_state = build_event(' in scoped, (
        'warm-resume branch no longer builds a resume_state snapshot — the '
        'delta-only replay would strand a placeholder-reset client at a later '
        'round (the round-10 bug).')
    # ... from the FULL in-memory toolRounds ...
    assert "resume_state['toolRounds'] = task['toolRounds']" in scoped, (
        "resume_state must carry the COMPLETE task['toolRounds'] so "
        '_snapshotLongerRounds adopts it when the client cache is short.')
    # ... and EMIT it (no id:) before the replay loop.
    assert '_resume_state_payload' in scoped and 'resume_state' in scoped, (
        'the built resume_state is never yielded — snapshot emission missing.')
    # The emitted snapshot frame must NOT carry an id: (synthetic, like fresh).
    assert "yield f'data: {_resume_state_payload}\\n\\n'" in scoped, (
        'the resume_state must be yielded as an id-less data frame (like the '
        'fresh-connection snapshot) to avoid a cursor collision.')
    _ok('source: warm-resume branch builds full-toolRounds resume_state and yields it id-less')


def main():
    print()
    print(_color('═══ Warm-resume reasserts toolRounds Tests ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_warm_resume_reasserts_toolrounds.__main__')
    tests = [
        test_source_warm_resume_builds_and_emits_full_snapshot,
        test_warm_resume_first_frame_is_full_toolrounds_snapshot,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
