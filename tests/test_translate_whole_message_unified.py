"""Whole-message / retro translate UNIFICATION (2026-07-10).

The live incremental worker (lib.translate.incremental) already streamed
per-round narration (partialByRound running frames) and carried segmentsByRound
on its done frame. The WHOLE-MESSAGE path (lib.translate.runtime._do_translate)
— which the retro / on-open / manual / frozen-OFF scenario runs — did NOT:

  • It pushed the ``done`` frame FIRST (deliverable only) and built the segment
    translation map AFTERWARDS, so the tool narration reached the client only
    via a later DB re-read — a SECOND render tick that repainted the narration
    EN→中文 separately (the reported "screen flickers, tool content replaced on
    a later tick").
  • It never emitted per-round progress, so a retro translation showed a bare
    spinner that hung, then a whole-bubble replace (no streaming).

This suite pins the unified behaviour with a deterministic fake translator
(offline). Each assertion is paired with a NEUTER that disables the mechanism.
"""

import time

import pytest

import lib.translate.runtime as rt

pytestmark = pytest.mark.unit


def _fake_translate(text, system_prompt, source='', target='', **kw):
    """Deterministic: prefix so output != input but derivable. Ignores cbs."""
    return ('ZH:' + text), {'_dispatch': {'model': 'fake-mt'}}


def _segments():
    """Two narration rounds + a deliverable — the shape _do_translate enriches."""
    return [
        {'type': 'text', 'text': 'First narration.', 'deliverable': False, 'llmRound': 0},
        {'type': 'tool_use', 'id': 't0', 'llmRound': 0},
        {'type': 'text', 'text': 'Second narration.', 'deliverable': False, 'llmRound': 1},
        {'type': 'tool_use', 'id': 't1', 'llmRound': 1},
        {'type': 'text', 'text': 'The final answer.', 'deliverable': True, 'terminal': True},
    ]


def _capture_push(monkeypatch):
    frames = []
    import lib.push as _push_mod
    monkeypatch.setattr(_push_mod, 'push_event',
                        lambda channel, tid, frame: frames.append((channel, tid, dict(frame))),
                        raising=False)
    return frames


def _run_do_translate(monkeypatch, *, segs, task_id='tw-1', content='The final answer.'):
    """Drive the real _do_translate with all network seams faked."""
    monkeypatch.setattr(rt, '_translate_freetext', _fake_translate)
    monkeypatch.setattr(rt, '_read_message_segments', lambda *a, **k: segs)
    committed = {}
    monkeypatch.setattr(rt, '_commit_translation_to_db',
                        lambda *a, **k: committed.update(args=a, kw=k))
    frames = _capture_push(monkeypatch)
    with rt._translate_tasks_lock:
        rt._translate_tasks[task_id] = {'id': task_id, 'status': 'running', 'model': None}
    rt._do_translate(task_id, content, 'Chinese', 'English',
                     'conv-1', 5, 'translatedContent', msg_id='m-1')
    return frames, committed


# ── B1: the done frame carries segmentsByRound (built BEFORE the push) ──

def test_done_frame_carries_segments_by_round(monkeypatch):
    """The whole-message done frame must carry segmentsByRound — {str(round):中文}
    — so the deliverable AND the per-round narration land in ONE render tick.
    (If the map were built AFTER the push, as before, the frame could not carry
    it — so this assertion IS the ordering proof.)"""
    frames, _ = _run_do_translate(monkeypatch, segs=_segments())
    done = [f for f in frames if f[2].get('status') == 'done']
    assert done, 'a done frame must be pushed'
    sbr = done[-1][2].get('segmentsByRound')
    assert sbr is not None, 'done frame must carry segmentsByRound (built before the push)'
    assert sbr.get('0') == 'ZH:First narration.'
    assert sbr.get('1') == 'ZH:Second narration.'
    # The deliverable/terminal segment is NOT in the narration map (it is the
    # translatedContent blob).
    assert 'ZH:The final answer.' not in sbr.values()


def test_done_frame_and_commit_share_the_same_map(monkeypatch):
    """The commit gets the SAME segment_translations map carried on the done
    frame — one build, used for both the live stamp and the DB persist."""
    frames, committed = _run_do_translate(monkeypatch, segs=_segments())
    seg_trans = committed['kw'].get('segment_translations')
    assert seg_trans, 'commit must receive segment_translations'
    assert seg_trans.get(0) == 'ZH:First narration.'
    done = [f for f in frames if f[2].get('status') == 'done'][-1][2]
    # Frame keys are stringified; the commit map is int-keyed (in-process).
    assert {str(k): v for k, v in seg_trans.items()} == done['segmentsByRound']


def test_no_segments_done_frame_has_no_segmentsByRound(monkeypatch):
    """NEUTER (data): a message with no narration segments → the map is None,
    so the done frame carries NO segmentsByRound (proves the key only appears
    when there is narration to carry — it is not fabricated)."""
    frames, _ = _run_do_translate(monkeypatch, segs=None, task_id='tw-nosegs')
    done = [f for f in frames if f[2].get('status') == 'done']
    assert done, 'done frame still pushed for a segment-less message'
    assert 'segmentsByRound' not in done[-1][2], \
        'segmentsByRound must be absent when there are no narration segments'


# ── B2: _translate_segments_to_map streams partialByRound via progress_cb ──

def test_segments_to_map_progress_cb_streams_accumulating_map(monkeypatch):
    """The pure core calls progress_cb after EACH narration segment with the
    ACCUMULATED {str(round):中文} map — the lever that makes the retro path
    stream round-by-round instead of landing every round at the end."""
    monkeypatch.setattr(rt, '_translate_freetext', _fake_translate)
    snapshots = []
    rt._translate_segments_to_map(
        _segments(), 'sysprompt', 'English', 'Chinese',
        log_tag='t', progress_cb=lambda m: snapshots.append(dict(m)))
    assert len(snapshots) == 2, f'expected one snapshot per narration round, got {len(snapshots)}'
    # First snapshot: only round 0. Second: rounds 0 AND 1 (accumulating).
    assert snapshots[0] == {'0': 'ZH:First narration.'}
    assert snapshots[1] == {'0': 'ZH:First narration.', '1': 'ZH:Second narration.'}


def test_segments_to_map_pure_without_progress_cb(monkeypatch):
    """NEUTER (omit cb): with no progress_cb the function is pure — it still
    returns the full map but emits nothing (the backfill-migration contract is
    unchanged). Proves the streaming is opt-in and cb-driven, not incidental."""
    monkeypatch.setattr(rt, '_translate_freetext', _fake_translate)
    seg_map = rt._translate_segments_to_map(
        _segments(), 'sysprompt', 'English', 'Chinese', log_tag='t')
    assert seg_map == {0: 'ZH:First narration.', 1: 'ZH:Second narration.'}


def test_do_translate_emits_partialByRound_before_done(monkeypatch):
    """End-to-end: _do_translate pushes running frames carrying partialByRound
    (the per-round interleave map) BEFORE the terminal done frame — so a retro
    translation streams its narration round-by-round."""
    frames, _ = _run_do_translate(monkeypatch, segs=_segments(), task_id='tw-stream')
    running_by_round = [f for f in frames
                        if f[2].get('status') == 'running' and f[2].get('partialByRound')]
    assert running_by_round, 'expected running frames carrying partialByRound'
    # Every partialByRound frame precedes the done frame.
    done_idx = next(i for i, f in enumerate(frames) if f[2].get('status') == 'done')
    for i, f in enumerate(frames):
        if f[2].get('status') == 'running' and f[2].get('partialByRound'):
            assert i < done_idx, 'partialByRound running frame must precede the done frame'
    # The last partialByRound carries both rounds; a joined blob is present too.
    last = running_by_round[-1][2]
    assert last['partialByRound'].get('0') == 'ZH:First narration.'
    assert last['partialByRound'].get('1') == 'ZH:Second narration.'
    assert last.get('partial'), 'joined partial blob must accompany partialByRound for degrade'


# ── SEAM: the client-scheduled frozen-OFF+toggle-ON path is UNIFORM ──
#
# _startAutoTranslateForMsg → _runTranslationPipeline(field:'translatedContent')
# → _startTranslateTask → POST /api/v1/translate/start, which forwards
# (convId, msgIdx, msgId, field) VERBATIM into _do_translate
# (routes/api_v1/translate.py::translate_start_v1). These tests pin that the
# EXACT tuple the client sends on that path drives the per-round narration
# build — i.e. the frozen-OFF+toggle-ON case translates tool narration too, not
# just the deliverable — and that the gate is load-bearing (a non-deliverable
# field / absent conv_id does NOT build the map, so it can't fire spuriously).

def _run_do_translate_args(monkeypatch, *, conv_id, msg_idx, field, msg_id,
                           task_id='tw-seam', segs=None):
    """Drive _do_translate with an EXPLICIT arg tuple (mirrors what the route
    forwards from the client body) so we can vary the gate inputs precisely."""
    if segs is None:
        segs = _segments()
    monkeypatch.setattr(rt, '_translate_freetext', _fake_translate)
    monkeypatch.setattr(rt, '_read_message_segments', lambda *a, **k: segs)
    committed = {}
    monkeypatch.setattr(rt, '_commit_translation_to_db',
                        lambda *a, **k: committed.update(args=a, kw=k))
    frames = _capture_push(monkeypatch)
    with rt._translate_tasks_lock:
        rt._translate_tasks[task_id] = {'id': task_id, 'status': 'running', 'model': None}
    rt._do_translate(task_id, 'The final answer.', 'Chinese', 'English',
                     conv_id, msg_idx, field, msg_id=msg_id)
    return frames, committed


def test_client_path_tuple_translates_narration(monkeypatch):
    """The frozen-OFF+toggle-ON client path sends exactly
    (convId set, msgIdx set, msgId set, field='translatedContent'). That tuple
    MUST build + stream the per-round narration — proving tool content is
    translated on this path, not left English for a later retro pass. This is
    the acceptance-closing uniformity check."""
    frames, committed = _run_do_translate_args(
        monkeypatch, conv_id='conv-x', msg_idx=7, field='translatedContent',
        msg_id='m-x', task_id='tw-seam-ok')
    # narration streamed round-by-round …
    running_by_round = [f for f in frames
                        if f[2].get('status') == 'running' and f[2].get('partialByRound')]
    assert running_by_round, 'client-path tuple must stream partialByRound narration'
    # … carried on the done frame …
    done = [f for f in frames if f[2].get('status') == 'done'][-1][2]
    assert done.get('segmentsByRound', {}).get('1') == 'ZH:Second narration.'
    # … and persisted with the deliverable in one commit.
    assert committed['kw'].get('segment_translations'), \
        'narration map must be committed alongside the deliverable'


def test_gate_non_deliverable_field_skips_narration(monkeypatch):
    """NEUTER (gate input): a field OTHER than 'translatedContent' (e.g. the
    edit-original 'content' field) must NOT build the narration map — proving
    field=='translatedContent' is the load-bearing gate the client path
    satisfies, and the build can't fire on an unrelated field."""
    frames, committed = _run_do_translate_args(
        monkeypatch, conv_id='conv-x', msg_idx=7, field='content',
        msg_id='m-x', task_id='tw-seam-field')
    assert not any(f[2].get('partialByRound') for f in frames), \
        'non-deliverable field must not stream narration'
    done = [f for f in frames if f[2].get('status') == 'done'][-1][2]
    assert 'segmentsByRound' not in done, \
        'non-deliverable field must not carry segmentsByRound'
    assert not committed['kw'].get('segment_translations')


def test_gate_missing_conv_id_skips_narration(monkeypatch):
    """NEUTER (gate input): with no conv_id (a context-free translate) there is
    no message to enrich, so the narration map is not built — the gate's
    conv_id requirement is load-bearing."""
    frames, _ = _run_do_translate_args(
        monkeypatch, conv_id='', msg_idx=None, field='translatedContent',
        msg_id=None, task_id='tw-seam-noconv')
    assert not any(f[2].get('partialByRound') for f in frames), \
        'no conv_id must not stream narration'
    done = [f for f in frames if f[2].get('status') == 'done'][-1][2]
    assert 'segmentsByRound' not in done
