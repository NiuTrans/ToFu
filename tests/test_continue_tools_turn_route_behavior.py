#!/usr/bin/env python3
"""tests/test_continue_tools_turn_route_behavior.py — determine the ACTUAL
current /api/chat/continue behaviour for a capable TOOLS turn, to scope the
P5 flip honestly (epic pt_c11c3a9272274848, owner answered "A — FLIP").

The epic's premise is "today checkpoint wins and DROPS the trailing prose".
But the checkpoint branch (routes/chat.py) computes _resume_prefill BEFORE the
rollback and, when set, ships cfg['resumePrefill'] = the tail — i.e. the
case-2 composition (replay tool batch + prefill tail) that
test_case2_prefill_after_tool_batch_no_double_count PROVES is lossless on the
wire. So the route may ALREADY be lossless for a capable tools turn, in which
case the P5 "flip" is really about the VERDICT honestly reporting
resume.mode=prefill (lossless) instead of checkpoint (lossy) — not a route
behaviour change.

This probe drives the REAL /api/chat/continue for a capable tools turn
(completed tool batch + terminal deliverable tail + resumable finishReason +
segments), stubs _start_task_for_conv to capture the cfg the route builds, and
asserts WHICH path the route takes:

  * cfg['resumePrefill'] set   → case-2 prefill (LOSSLESS) — route already
                                  prefers prefill; the flip is verdict-only.
  * cfg['resumePrefill'] absent → checkpoint regenerate (LOSSY) — the flip
                                  must ALSO change the route.

Run:  pytest tests/test_continue_tools_turn_route_behavior.py -q
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/tools_turn_route_probe.db')

TAIL = 'Based on the results, the fix is:'
BATCH_PROSE = 'Let me search first.'


def _require_db():
    from lib.database import init_db
    try:
        init_db()
    except Exception as e:
        pytest.skip(f'DB bootstrap unavailable ({type(e).__name__}: {e})')


def _seed_tools_turn_conv(flask_client, conv_id, model='gpt-4o'):
    """Seed a conv whose last assistant turn is a TOOLS turn: one completed
    tool batch (with batch prose) + a terminal deliverable tail + a resumable
    finishReason, with the segment timeline persisted (as recover-on-startup
    / persist_task_result would stamp)."""
    import time as _time
    now = int(_time.time() * 1000)
    rounds = [{
        'roundNum': 1, 'llmRound': 0, 'toolCallId': 'tc_1',
        'toolName': 'web_search', 'toolArgs': '{"query":"x"}',
        'toolContent': 'search hit', 'status': 'done',
        'assistantContent': BATCH_PROSE,
    }]
    segs = [
        {'type': 'text', 'text': BATCH_PROSE, 'deliverable': False, 'llmRound': 0},
        {'type': 'tool_use', 'id': 'tc_1', 'name': 'web_search',
         'input': '{"query":"x"}', 'llmRound': 0,
         'result': {'content': 'search hit', 'status': 'done'}},
        {'type': 'text', 'text': TAIL, 'deliverable': True, 'terminal': True},
    ]
    r = flask_client.put(f"/api/v1/conversations/{conv_id}", json={
        "title": "tools-turn-probe", "messages": [
            {"role": "user", "content": "resume please", "timestamp": now},
            {"role": "assistant", "content": TAIL, "thinking": "",
             "toolRounds": rounds, "segments": segs,
             "finishReason": "aborted", "timestamp": now + 1},
        ], "createdAt": now, "updatedAt": now})
    assert r.status_code == 200, r.get_data(as_text=True)


@pytest.mark.api
def test_route_behavior_for_capable_tools_turn(flask_client, monkeypatch):
    """Probe which path /api/chat/continue takes for a capable tools turn."""
    _require_db()
    import routes.chat as chatmod

    captured = {}

    def _capture_start(conv_id, cfg, data=None, **kw):
        captured['cfg'] = cfg
        return ('stub-task-id', None)

    monkeypatch.setattr(chatmod, '_start_task_for_conv', _capture_start)

    conv_id = f"cv-probe-tools-{int(__import__('time').time()*1000)}"
    _seed_tools_turn_conv(flask_client, conv_id)
    try:
        resp = flask_client.post("/api/v1/chat/continue", json={
            "convId": conv_id, "config": {"model": "gpt-4o"}})
        data = resp.get_json()
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert data.get('fallback') != 'regenerate', \
            f'a capable tools turn with a resumable tail must NOT regenerate: {data}'
        cfg = captured.get('cfg') or {}
        # THE PROBE: is resumePrefill set (case-2 prefill, lossless) or not
        # (checkpoint regenerate, lossy)?
        print(f"\n[PROBE] resumeMode={data.get('checkpoint', {}).get('resumeMode')!r} "
              f"cfg.resumePrefill={'SET(%d chars)' % len(cfg.get('resumePrefill') or '') if cfg.get('resumePrefill') else 'ABSENT'} "
              f"cfg.toolHistory={'SET' if cfg.get('toolHistory') else 'ABSENT'} "
              f"cfg.contentPrefix_len={len(cfg.get('contentPrefix') or '')} "
              f"priorContent={data.get('checkpoint', {}).get('priorContent')!r}")
        # The wire is LOSSLESS when contentPrefix == the FULL prior content
        # (tail included) — the tail is continued, not dropped. priorContent
        # must be EMPTY (nothing relegated to a display-only block).
        assert cfg.get('contentPrefix') == TAIL, \
            f'contentPrefix should be the full prior content (tail), got {cfg.get("contentPrefix")!r}'
        assert data.get('checkpoint', {}).get('priorContent') == '', \
            'a lossless prefill resume has NO priorContent (the tail is continued, not dropped)'
        assert cfg.get('resumePrefill') == TAIL, \
            f'route did NOT ship resumePrefill for a capable tools turn — it is LOSSY (drops the tail). cfg={ {k: (len(v) if isinstance(v,str) else v) for k,v in cfg.items()} }'
        assert cfg.get('toolHistory'), 'route must replay the tool batch alongside the prefill'
    finally:
        flask_client.delete(f"/api/v1/conversations/{conv_id}")


@pytest.mark.api
def test_route_behavior_claude_tools_turn_no_prefill(flask_client, monkeypatch):
    """Claude must fail closed: a tools turn on Claude ships NO resumePrefill
    (checkpoint regenerate — the only safe option, since Claude rejects a
    trailing assistant prefill)."""
    _require_db()
    import routes.chat as chatmod

    captured = {}

    def _capture_start(conv_id, cfg, data=None, **kw):
        captured['cfg'] = cfg
        return ('stub-task-id', None)

    monkeypatch.setattr(chatmod, '_start_task_for_conv', _capture_start)

    conv_id = f"cv-probe-claude-{int(__import__('time').time()*1000)}"
    _seed_tools_turn_conv(flask_client, conv_id, model='claude-sonnet-4-5')
    try:
        resp = flask_client.post("/api/v1/chat/continue", json={
            "convId": conv_id, "config": {"model": "claude-sonnet-4-5"}})
        data = resp.get_json()
        assert resp.status_code == 200, resp.get_data(as_text=True)
        cfg = captured.get('cfg') or {}
        assert not cfg.get('resumePrefill'), \
            'Claude must fail closed — no resumePrefill on the wire'
        assert cfg.get('toolHistory'), 'Claude still replays the tool batch (checkpoint)'
    finally:
        flask_client.delete(f"/api/v1/conversations/{conv_id}")


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
