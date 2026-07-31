"""tests/test_finalize_actual_fact_card.py — the turn-ctx capsule fact card
is stamped onto the DONE SSE event.

WHY
---
The per-turn note in the message right-gutter (info-rail.js) used to be
frozen at send time — if the user paused and switched preset between send
and stream-start, or if the dispatcher fell back to a different provider,
the capsule silently lied. The frontend now RECONCILES the note against
the DONE frame's fact card (``reconcileTurnCtxCapsule``), so the wire
contract MUST ship these three server-authoritative fields on every done:

  * actualModel  — the model that actually answered (``_fallback_model``
    wins over the initial pick, same semantics as ``fallbackModel``).
  * actualDepth  — the thinking depth actually applied.
  * actualModes  — the run-mode list ({label, tone:'mode'}) live server-
    side; built from ``cfg['activeFlow'] / cfg['endpointMode'] /
    cfg['autopilot'] / cfg['swarmEnabled']``.

This test asserts the SOURCE contract at
``lib/tasks_pkg/orchestrator/_finalize.py::_finalize_and_emit_done``:

  1. All three names appear on ``done_evt`` in the file.
  2. ``actualModel = task.get('_fallback_model') or model`` — fallback
     wins, matching the existing ``fallbackModel`` semantics.
  3. All four mode sources are read.
  4. The block sits AFTER the ``toolsetDiff`` block (co-located per the
     wire-contract spec).

NEUTER: on a mutated string COPY (shipped file untouched), stripping the
``done_evt['actualModel'] = …`` line makes assertion (1) fail — proving
the assertion catches a regression that removes the field. Same technique
as ``tests/test_frontend_turn_ctx_fact_card.py`` for the JS half.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
FINALIZE_PATH = os.path.join(
    ROOT, 'lib', 'tasks_pkg', 'orchestrator', '_finalize.py')


def _read_source() -> str:
    with open(FINALIZE_PATH, encoding='utf-8') as f:
        return f.read()


def test_finalize_emits_actual_fact_card_fields():
    """All three ``actual*`` fields are stamped onto ``done_evt``. The
    frontend reconcile reads exactly these three names — a rename here
    would silently break the fact-card reconcile without triggering any
    other test."""
    src = _read_source()
    for field in ('actualModel', 'actualDepth', 'actualModes'):
        needle = f"done_evt['{field}']"
        assert needle in src, (
            f'{FINALIZE_PATH}: DONE event must stamp {needle} — the '
            f'frontend info-rail.js::reconcileTurnCtxCapsule reads this '
            f'exact key to overwrite the send-time snapshot. A silent '
            f'rename kills the fact-card contract with no other symptom.'
        )


def test_actual_model_prefers_fallback_over_initial():
    """``actualModel = task.get('_fallback_model') or model`` — matches
    the same fallback-precedence the existing ``fallbackModel`` field
    uses. Without this, a mid-turn dispatcher fallback would leave the
    capsule showing the initial (rejected) pick."""
    src = _read_source()
    assert "done_evt['actualModel'] = task.get('_fallback_model') or model" in src, (
        f'{FINALIZE_PATH}: actualModel must be '
        f'`task.get("_fallback_model") or model` so a mid-turn dispatcher '
        f'fallback correctly settles the capsule to what actually answered '
        f'(same semantics as the fallbackModel field one block down).'
    )


def test_actual_modes_reads_all_four_sources():
    """The mode list is built from the same four cfg keys the frontend
    ``_collectModes`` reads: activeFlow (supersedes the mode toggles),
    endpointMode, autopilot, swarmEnabled. Missing any source ⇒ a mode
    the user can trigger from the composer never round-trips."""
    src = _read_source()
    # Slice the file to the actualModes construction so we don't false-match
    # references elsewhere in the file.
    start = src.index("_actual_modes: list[dict[str, str]] = []")
    end = src.index("done_evt['actualModes']", start)
    block = src[start:end]
    for key in ("activeFlow", "endpointMode", "autopilot", "swarmEnabled"):
        assert f"cfg.get('{key}')" in block, (
            f'{FINALIZE_PATH}: the actualModes construction block must '
            f'read cfg.get({key!r}) — every composer mode toggle must '
            f'round-trip through the fact card. Missing: {key}.'
        )


def test_actual_block_is_co_located_with_toolsetdiff():
    """Per the wire-contract spec: place the fact-card fields BESIDE the
    tool-schema latch diff so both settlements ride the same block and
    remain reviewable together."""
    src = _read_source()
    diff_pos = src.index("done_evt['toolsetDiff']")
    actual_pos = src.index("done_evt['actualModel']")
    assert 0 < diff_pos < actual_pos, (
        f'{FINALIZE_PATH}: actualModel block (offset {actual_pos}) must '
        f'appear AFTER the toolsetDiff block (offset {diff_pos}) so both '
        f'the tool-schema-latch reconcile AND the fact-card overwrite are '
        f'clustered in one reviewable spot.'
    )
    # And close enough that they are visually one block, not scattered.
    gap = src[diff_pos:actual_pos]
    assert gap.count('\n') <= 25, (
        f'{FINALIZE_PATH}: toolsetDiff and actualModel drifted apart '
        f'({gap.count(chr(10))} lines between them). Keep them '
        f'co-located so a future reader sees the whole capsule-reconcile '
        f'contract at once.'
    )


def test_neuter_removing_actual_model_breaks_contract():
    """NEUTER: strip the ``done_evt['actualModel'] = …`` line from a
    string copy of the source (the shipped file stays untouched) and
    prove ``test_finalize_emits_actual_fact_card_fields`` would flip
    red. This tells a future engineer removing the line WHY it's
    load-bearing (`git blame` + this test)."""
    src = _read_source()
    needle = "done_evt['actualModel'] = task.get('_fallback_model') or model"
    assert needle in src, (
        f'NEUTER precondition failed: expected line not present in '
        f'{FINALIZE_PATH} — refresh the NEUTER needle.'
    )
    neutered = src.replace(needle, '# neutered')
    assert neutered != src, 'NEUTER did not mutate the source'
    # After neutering, the fact-card contract assertion must be violated
    # (this is what "reverse-proves" the guard).
    assert "done_evt['actualModel']" not in neutered, (
        'NEUTER succeeded but the guard still passes — the assertion is '
        'not actually testing the line it claims to test. Fix the '
        'assertion (the file-contract test above) so it flips red when '
        'this line is removed.'
    )
