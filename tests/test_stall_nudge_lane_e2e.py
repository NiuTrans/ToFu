"""The stall-nudge lane, END TO END (epic pt_5303eb3c7afb44a8).

WHY THIS FILE EXISTS AT ALL
---------------------------
The first version of this lane registered ``_stallNudge`` in
``SYNTHETIC_INBOX_MARKERS`` and "verified" it with::

    assert is_synthetic_inbox_round({'_stallNudge': True, 'roundNum': 9_000_001})

That assertion passes for a marker with NO producer, NO sidecar and NO renderer
— it only proves the string is present in a constant. The marker was dead code
and the guard was green. Reviewer caught it; the fixture never touched a
production path.

So every test here drives a REAL seam:

  * the producer via the real :func:`analyse_stream_result` (never a
    hand-assembled ``task['_stallNudges']``),
  * the persistence lane via the real ``INBOX_INJECT_SIDECAR_FIELDS`` /
    ``_persist_inject_sidecars``,
  * the render path via the real shipped ``core.js`` + ``tool_rounds.js``
    evaluated in node (never a hand-built synthetic row).

If any layer is removed, something here goes red.

GROUND TRUTH the fixtures model — conv ms34yw0k74o2lq R17/R18: ``run_command``
blocked by a pre-execution hook, then a prose-only round with no tool calls.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from lib.tasks_pkg.segments import is_synthetic_inbox_round
from lib.tasks_pkg.stream_handler._intent_stall import NUDGE_TEXT
from lib.turn_lifecycle import _task_projection

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent


# ── Fixtures: the ground-truth stall shape ────────────────────────────

_STALL_TEXT = 'Let me use explicit paths only.'


def _blocked_round():
    """R17: run_command rejected by the pre-execution hook (never ran)."""
    return {
        'toolName': 'run_command',
        'roundNum': 17,
        'llmRound': 17,
        'status': 'blocked',
        'results': [{
            'badge': 'blocked',
            'notRun': True,
            'reason': 'Tool blocked by pre-execution hook',
        }],
    }


def _task(rounds, **extra):
    # A task dict the REAL append_event accepts: the analyser emits its phase
    # chip on the very branch that produces the sidecar record, so a fixture
    # that the emitter rejects cannot exercise the producer at all. The task is
    # not registered with the runtime, so append_event takes its documented
    # legacy fallback, which needs `events` + `events_lock`.
    t = {'id': 'task-stall-e2e', 'convId': 'ms34yw0k74o2lq',
         'events': [], 'events_lock': threading.Lock(),
         'toolRounds': list(rounds), 'aborted': False, 'content': _STALL_TEXT}
    t.update(extra)
    return t


def _analyse(task, content, messages):
    """Drive the REAL analyser — the only way the producer can be exercised."""
    from lib.tasks_pkg.stream_handler._analyse import analyse_stream_result
    return analyse_stream_result(
        assistant_msg={'content': content},
        last_finish_reason='stop',
        task=task,
        tid='ms34yw0k',
        model='yuju-claude-opus-5-evaDaily',
        round_num=18,
        _premature_retry_count=0,
        messages=messages,
        usage={},
    )


# ══════════════════════════════════════════════════════════════════
#  1. PRODUCER — the real analyser must emit the chip record
# ══════════════════════════════════════════════════════════════════

def test_the_analyser_produces_a_chip_record_when_it_nudges():
    """The nudge and its chip are one act: if the loop re-drives the model,
    the timeline must carry a record of it."""
    task = _task([_blocked_round()])
    decision = _analyse(task, _STALL_TEXT, [{'role': 'user', 'content': 'go'}])

    assert decision['action'] == 'continue', 'precondition: the nudge fired'
    recs = task.get('_stallNudges')
    assert recs, (
        'the analyser injected a nudge but produced NO chip record — the lane '
        'has no producer, so nothing can ever render'
    )
    assert len(recs) == 1


def test_the_record_carries_the_trigger_provenance():
    """A bare "the system nudged" line is not actionable.

    The record must name WHICH tool failed and carry the VERBATIM instruction,
    so the panel can show what actually happened instead of a paraphrase.
    """
    task = _task([_blocked_round()])
    _analyse(task, _STALL_TEXT, [{'role': 'user', 'content': 'go'}])
    rec = task['_stallNudges'][0]

    assert rec['tool'] == 'run_command', 'must name the tool that failed'
    assert rec['failedRound'] == 17
    assert rec['prompt'] == NUDGE_TEXT, (
        'the panel must show the EXACT text sent to the model, not a copy that '
        'can drift from NUDGE_TEXT'
    )
    assert rec['max'] == 1, 'the bound must be carried so the UI can state it'
    # round_num=18 was the prose-only round; the nudge it appended is consumed
    # by round 19, and the frontend anchors above `round - 1`. So 20 — NOT the
    # peer/steer +1, whose messages were injected before their round ran.
    assert rec['round'] == 20, (
        'the chip must be anchored to the round that CONSUMES the nudge; +1 '
        'anchors it to the prose-only round, which owns no tools, so the chip '
        'falls through to the tail of the panel'
    )


def test_a_normal_stop_produces_no_record():
    """Complement: no nudge → no chip.

    Without this, a producer that recorded on EVERY turn would satisfy the
    tests above while spamming the timeline.
    """
    ok_round = {'toolName': 'read_files', 'roundNum': 3, 'llmRound': 3,
                'status': 'done', 'results': [{'badge': '120L'}]}
    task = _task([ok_round], content='All done.')
    decision = _analyse(task, 'All done.', [{'role': 'user', 'content': 'go'}])

    assert decision['action'] == 'break'
    assert not task.get('_stallNudges')


def test_the_bound_holds_at_the_record_level_too():
    """The one-nudge cap must also cap the chips — no second record."""
    task = _task([_blocked_round()], _intent_stall_nudge_count=1)
    decision = _analyse(task, _STALL_TEXT, [{'role': 'user', 'content': 'go'}])

    assert decision['action'] == 'break'
    assert not task.get('_stallNudges')


# ══════════════════════════════════════════════════════════════════
#  2. PERSISTENCE — the real sidecar lane must carry it
# ══════════════════════════════════════════════════════════════════

def test_the_lane_is_registered_for_persistence():
    marker = {'prompt': 'continue'}
    projected = _task_projection({'_stallNudges': [marker]}, {})
    assert projected['_stallNudges'] == [{
        **marker, 'blockId': 'injection:stall-nudge:round-unknown',
    }]
    assert 'blockId' not in marker


def test_the_produced_record_survives_the_real_persist_seam():
    """End-to-end producer → persistence, with no hand-made dict anywhere."""
    task = _task([_blocked_round()])
    _analyse(task, _STALL_TEXT, [{'role': 'user', 'content': 'go'}])

    projection = _task_projection(task, {})

    projected_record = projection['_stallNudges'][0]
    assert {key: value for key, value in projected_record.items()
            if key != 'blockId'} == task['_stallNudges'][0]
    assert projected_record['blockId'] == (
        f"injection:stall-nudge:round-{projected_record['round']}")
    assert projected_record['tool'] == 'run_command'


# ══════════════════════════════════════════════════════════════════
#  3. WIRE PURITY — now load-bearing, because a row now EXISTS
# ══════════════════════════════════════════════════════════════════

def test_the_rehydrated_row_is_wire_excluded():
    """The synthetic row the FRONTEND builds must be filtered from replay.

    Driven off the row the real ``_rehydrateInjectRows`` produces (see the
    render section) rather than a hand-written dict — that is what makes this
    assertion about the shipped lane instead of about a constant.
    """
    row = _rehydrate_via_shipped_js(_sidecar_from_real_producer())[0]
    assert is_synthetic_inbox_round(row)


def test_a_real_tool_round_is_not_wire_excluded():
    """Complement: marking everything synthetic would empty the wire replay."""
    assert not is_synthetic_inbox_round({
        'roundNum': 3, 'toolName': 'read_files',
        'toolCallId': 'call_abc', 'toolContent': 'File: x.py', 'status': 'done',
    })


# ══════════════════════════════════════════════════════════════════
#  4. RENDER — the real shipped JS must produce a chip
# ══════════════════════════════════════════════════════════════════

def _node() -> str:
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available')
    return node


def _i18n_dict() -> dict:
    """Parse the SHIPPED dictionary so t() behaves as it does in production.

    A stubbed ``t`` that echoes a fallback would manufacture strings the real UI
    can never produce (the exact trap that shipped a raw `project.qrScan` key).
    """
    catalogs = {
        lang: json.loads(
            (_ROOT / f'frontend/src/i18n/locales/{lang}.json').read_text(
                encoding='utf-8'))
        for lang in ('zh', 'en')
    }
    return {
        key: {'zh': catalogs['zh'][key], 'en': catalogs['en'][key]}
        for key in catalogs['zh'].keys() & catalogs['en'].keys()
    }


def _sidecar_from_real_producer() -> list:
    """The sidecar list, produced by the REAL analyser (never hand-built)."""
    task = _task([_blocked_round()])
    _analyse(task, _STALL_TEXT, [{'role': 'user', 'content': 'go'}])
    return task['_stallNudges']


def _run_js(script: str) -> dict:
    """Evaluate the migrated core/tool-round owners and return the probe."""
    node = _node()
    from tests._runtime_sections import runtime_section
    core = runtime_section('core.js')
    rounds = runtime_section('ui/tool_rounds.js')
    harness = f"""
globalThis.window = globalThis;
globalThis.document = {{
  addEventListener(){{}}, getElementById(){{return null;}},
  querySelectorAll(){{return [];}}, createElement(){{return {{style:{{}}}};}},
  head:{{appendChild(){{}}}},
}};
globalThis.localStorage = {{getItem(){{return null;}}, setItem(){{}}, removeItem(){{}}}};
globalThis.sessionStorage = {{getItem(){{return null;}}, setItem(){{}}}};
globalThis.navigator = {{}};
globalThis.location = {{pathname:'/'}};
globalThis.matchMedia = () => ({{matches:false, addEventListener(){{}}}});
globalThis.requestAnimationFrame = (f) => f();
globalThis.setInterval = () => 0;
globalThis.crypto = {{ randomUUID: () => 'x' }};
const _I18N = {json.dumps(_i18n_dict())};
globalThis.t = (k, params) => {{
  const e = _I18N[k];
  let s = e ? e.zh : k;                    // missing key → key, as production does
  if (params) for (const p of Object.keys(params))
    s = s.split('{{'+p+'}}').join(params[p]);
  return s;
}};
globalThis.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;');
globalThis.renderMarkdown = (s) => String(s == null ? '' : s);
globalThis.Icon = () => '';
globalThis._featureFlags = {{ debug_mode: false }};
{core}
{rounds}
{script}
"""
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as fh:
        fh.write(harness)
        path = fh.name
    try:
        res = subprocess.run([node, path], capture_output=True, text=True,
                             timeout=180)
        if res.returncode != 0:
            pytest.fail(f'node failed: {res.stderr[-2000:]}')
        return json.loads(res.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(path)


def _rehydrate_via_shipped_js(sidecar: list) -> list:
    """Rebuild the synthetic rows using the REAL ``_rehydrateInjectRows``."""
    probe = _run_js(
        'const msg = {_stallNudges: ' + json.dumps(sidecar) + '};\n'
        'const rows = getToolRoundsFromMsg(msg);\n'
        'console.log(JSON.stringify(rows.filter(r => r._stallNudge)));'
    )
    return probe


def test_the_sidecar_rehydrates_into_a_synthetic_row():
    """Reload path: the persisted sidecar must rebuild the in-timeline row."""
    rows = _rehydrate_via_shipped_js(_sidecar_from_real_producer())
    assert len(rows) == 1, 'the chip must survive a reload'
    row = rows[0]
    assert row['_stallNudge'] is True
    assert row['stallTool'] == 'run_command'
    assert row['stallPrompt'].startswith('[SYSTEM: TOOL CALL DID NOT RUN]')


def test_rehydration_is_idempotent():
    """A second pass must not duplicate the chip (sync/poll/reload all call it)."""
    sidecar = _sidecar_from_real_producer()
    probe = _run_js(
        'const msg = {_stallNudges: ' + json.dumps(sidecar) + '};\n'
        'const once = getToolRoundsFromMsg(msg);\n'
        'const twice = _rehydrateInjectRows(msg, once);\n'
        'console.log(JSON.stringify({'
        'once: once.filter(r=>r._stallNudge).length,'
        'twice: twice.filter(r=>r._stallNudge).length}));'
    )
    assert probe == {'once': 1, 'twice': 1}


def test_the_real_renderer_emits_a_chip_with_provenance_and_the_bound():
    """The shipped renderer must produce DOM a human can act on."""
    sidecar = _sidecar_from_real_producer()
    probe = _run_js(
        'const msg = {_stallNudges: ' + json.dumps(sidecar) + '};\n'
        'const rows = getToolRoundsFromMsg(msg);\n'
        'const html = renderToolRoundsHTML(rows, false);\n'
        'console.log(JSON.stringify({html}));'
    )
    html = probe['html']
    assert 'sw-stall-row' in html, 'the stall-nudge chip did not render'
    assert 'run_command' in html, 'the chip must name the tool that failed'
    assert 'TOOL CALL DID NOT RUN' in html, 'the verbatim prompt must be shown'
    # The bound must be stated — otherwise "the system re-drove my agent" reads
    # as an unbounded spend risk.
    assert 'sw-stall-bound' in html
    # And it must not be rendered as a user-authored bubble.
    assert 'sw-steer-row' not in html


def test_the_chip_is_anchored_between_the_failure_and_the_retry():
    """The chip must sit AFTER the failed tool and BEFORE the resumed one.

    This is the whole point of the feature: show that the loop intervened *at
    this moment*. Rendered after the resumed tool it reads as though the nudge
    happened once the model had already recovered — the one position that
    inverts the causality it exists to display.

    WHY THIS NEEDS TWO REAL TOOL ROUNDS. Every other test in this file uses
    zero or one, and at that size the bug is invisible: with nothing after the
    anchor, ``_spliceInjectRow``'s tail fallback and the correct slot are the
    SAME position. An off-by-one in the round stamp only becomes observable
    once a later round exists for the chip to be wrongly pushed past. That
    generalises — any inject lane's ordering is untested below two rounds.

    Ground truth shape (conv ms34yw0k74o2lq): R17 blocked, R18 prose-only (the
    nudge fires here), R19 the model resumes. R18 owns no tool rounds, which is
    exactly why anchoring to it silently degrades to the tail.

    Asserts RELATIVE order, never an index — the panel's markup may change.
    """
    sidecar = _sidecar_from_real_producer()
    failed = _blocked_round() | {'toolCallId': 'call_17'}
    resumed = {
        'toolName': 'read_files', 'roundNum': 19, 'llmRound': 19,
        'toolCallId': 'call_19', 'toolContent': 'File: x.py',
        'status': 'done', 'results': [{'badge': '120L'}],
    }
    msg = {'_stallNudges': sidecar, 'toolRounds': [failed, resumed]}
    segs = [
        {'type': 'tool_use', 'id': 'call_17', 'llmRound': 17,
         'content': 'blocked', 'status': 'blocked'},
        {'type': 'tool_use', 'id': 'call_19', 'llmRound': 19,
         'content': 'File: x.py', 'status': 'done'},
    ]
    probe = _run_js(
        'const msg = ' + json.dumps(msg) + ';\n'
        'const segs = ' + json.dumps(segs) + ';\n'
        'const rows = getToolRoundsFromMsg(msg);\n'
        # Row order: what every consumer of the rehydrated list sees.
        'const rowSeq = rows.map(r => r._stallNudge ? "NUDGE" : "R" + r.llmRound);\n'
        'const html = renderSegmentTimelineHTML(segs, msg, 0);\n'
        # Document order via the markers the panel actually emits.
        'const at = {NUDGE: html.indexOf("sw-stall-row"),\n'
        '            R17: html.indexOf(\'data-llm-round="L17"\'),\n'
        '            R19: html.indexOf(\'data-llm-round="L19"\')};\n'
        'const segSeq = Object.keys(at).filter(k => at[k] >= 0)\n'
        '  .sort((a, b) => at[a] - at[b]);\n'
        'console.log(JSON.stringify({rowSeq, segSeq, html: html.length}));'
    )

    assert probe['html'], 'segment path bailed out — fixture no longer pairs'
    expected = ['R17', 'NUDGE', 'R19']
    assert probe['rowSeq'] == expected, (
        f'rehydrated row order is {probe["rowSeq"]}, expected {expected}. '
        'The chip is anchored to a round that owns no tools, so it fell '
        'through to the tail — it now renders after the tool it caused.'
    )
    assert probe['segSeq'] == expected, (
        f'segment-timeline document order is {probe["segSeq"]}, expected '
        f'{expected} — the chip renders on the wrong side of the resumed tool.'
    )


def test_the_chip_copy_is_translated_not_a_raw_key():
    """Every string must resolve through the real dictionary.

    Driven with production ``t()`` semantics (missing key → the key itself), so
    an unregistered key surfaces as a literal `stall.…` in the DOM.
    """
    sidecar = _sidecar_from_real_producer()
    probe = _run_js(
        'const msg = {_stallNudges: ' + json.dumps(sidecar) + '};\n'
        'const html = renderToolRoundsHTML(getToolRoundsFromMsg(msg), false);\n'
        'console.log(JSON.stringify({html}));'
    )
    assert 'stall.' not in probe['html'], (
        'a raw i18n key leaked into the rendered chip — the key is referenced '
        'but not defined in the locale chunks'
    )


def test_the_chip_survives_the_segment_timeline_path():
    """The OTHER render path must carry the lane too.

    A settled turn with segments renders through ``renderSegmentTimelineHTML``,
    which drives its walk from ``segments`` — and ``assemble_segments`` skips
    synthetic rounds, so a lane missing from that function's extraction set is
    DROPPED entirely once the turn settles (the chip shows while streaming, then
    vanishes on reload — the worst possible shape for a "leave a trace"
    feature). The three older lanes are extracted there by name; this asserts
    the stall lane is too.
    """
    sidecar = _sidecar_from_real_producer()
    # A real tool round paired to a tool_use segment by id — without the pair
    # the function bails to the legacy path and would pass vacuously.
    msg = {
        '_stallNudges': sidecar,
        'toolRounds': [{
            'roundNum': 17, 'llmRound': 17, 'toolName': 'run_command',
            'toolCallId': 'call_x', 'toolContent': 'blocked',
            'status': 'blocked', 'results': [{'badge': 'blocked'}],
        }],
    }
    segs = [{'type': 'tool_use', 'id': 'call_x', 'llmRound': 17,
             'content': 'blocked', 'status': 'blocked'}]
    probe = _run_js(
        'const msg = ' + json.dumps(msg) + ';\n'
        'const segs = ' + json.dumps(segs) + ';\n'
        'const html = renderSegmentTimelineHTML(segs, msg, 0);\n'
        'console.log(JSON.stringify({html}));'
    )
    html = probe['html']
    assert html, 'the segment path bailed out — fixture no longer pairs (fix the test)'
    assert 'sw-stall-row' in html, (
        'the chip is dropped by the segment-timeline path: a settled turn loses '
        'it on reload'
    )
    assert 'run_command' in html
    # The synthetic row must not be counted as a tool in the panel header.
    assert 'data-full-count="1"' in html


def test_the_grouped_panel_header_never_counts_the_chip():
    """The GROUPED fallback path (segment-less / trimmed turns) must count
    REAL tools in the panel header, exactly like the segment timeline does.

    Ground truth (conv msg0cop6qf64ee msg[1]): a trimmed turn whose 32 real
    rounds were stripped for transport rendered its lone rehydrated stall
    chip under a 「使用了 1 个工具」 header — the chip is not a tool the model
    called, and the count made the hidden history look like a single-call
    turn. The grouped `_renderUnifiedGroup` counted `allRounds` (chips
    included) while the timeline path counted realRounds only; this pins the
    two paths to the SAME rule.
    """
    sidecar = _sidecar_from_real_producer()
    real = {
        'roundNum': 17, 'llmRound': 17, 'toolName': 'run_command',
        'toolCallId': 'call_17', 'toolContent': 'blocked',
        'status': 'blocked', 'results': [{'badge': 'blocked'}],
    }
    probe = _run_js(
        'const msg1 = {_stallNudges: ' + json.dumps(sidecar)
        + ', toolRounds: [' + json.dumps(real) + ']};\n'
        'const html1 = renderToolRoundsHTML(getToolRoundsFromMsg(msg1), false);\n'
        'const msg2 = {_stallNudges: ' + json.dumps(sidecar) + '};\n'
        'const html2 = renderToolRoundsHTML(getToolRoundsFromMsg(msg2), false);\n'
        'console.log(JSON.stringify({html1, html2}));'
    )
    # 1 real + 1 chip: the header claims ONE tool — the chip must not inflate it.
    assert '使用了 1 个工具' in probe['html1'], (
        'grouped header lost the real-round count:\n' + probe['html1'][:400])
    assert '使用了 2' not in probe['html1'], (
        'the synthetic stall chip inflated the grouped header tool count')
    assert 'data-full-count="1"' in probe['html1']
    assert 'sw-stall-row' in probe['html1'], 'the chip must still render'
    # Chip-only (the trimmed view): no tool-count claim at all — the real
    # count lives on the typed "Load tool activity" affordance.
    assert 'sw-stall-row' in probe['html2']
    assert '使用了' not in probe['html2'], (
        'a chip-only panel must not claim any tool count')
    assert 'data-full-count="0"' in probe['html2']
