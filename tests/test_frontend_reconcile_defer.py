"""Phase-3: the frontend Case-D reconcile must DEFER to the backend when the
server already reconciled the conversation (settings._reconciledAt →
conv._reconciledAt).

WHY
---
recover_stale_tasks_on_startup runs the SAME buried-ghost sweep + tail
classification server-side (lib/conversations/reconcile.py), persists it in one
commit, and stamps settings._reconciledAt. The frontend must then NOT re-infer
lifecycle state — that is the separation-of-concerns directive, and it is what
makes the resurrect + auto-fire regressions structurally impossible on the
crash-recovery path (no frontend pop / allowTruncate PUT happens).

This is a SOURCE-GUARD test (the Case-D gate lives inside the network-heavy
initActiveTasks; a full jsdom drive would be fragile). It asserts:
  1. main_init_tasks.js Case-D block is gated on ``!conv._reconciledAt``.
  2. conversations.js _applySettingsToConv maps ``settings._reconciledAt`` →
     ``conv._reconciledAt`` (so the marker actually reaches the guard).

Double-neuter (in a tmp COPY, real files untouched): removing ``!conv._reconciledAt``
from the gate makes assertion #1 fail.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _read(rel):
    with open(os.path.join(JS_DIR, rel), encoding='utf-8') as f:
        return f.read()


def test_case_d_gated_on_reconciled_marker():
    src = _read(os.path.join('main', 'main_init_tasks.js'))
    # The Case-D sweep+classify entry condition must include the deferral gate.
    m = re.search(r"if \(!conv\._needsLoad && !activeStreams\.has\(conv\.id\)([^)]*)\)\s*\{", src)
    assert m, 'Case-D entry condition not found in main_init_tasks.js'
    gate = m.group(1)
    assert '!conv._reconciledAt' in gate, (
        'Phase-3 regression: Case-D no longer defers to the backend '
        '_reconciledAt marker — the frontend would re-infer lifecycle state '
        'the backend already reconciled (separation-of-concerns violation, and '
        'the crash-recovery resurrect/auto-fire fixes rely on this deferral).')


def test_settings_maps_reconciled_marker():
    src = _read(os.path.join('core', 'conversations.js'))
    assert re.search(r"settings\._reconciledAt\b", src) and 'conv._reconciledAt = settings._reconciledAt' in src, (
        '_applySettingsToConv must map settings._reconciledAt → conv._reconciledAt '
        'or the Case-D gate never sees the backend marker.')


def test_double_neuter_gate_removal_detected():
    """Byte-revert control: with the gate removed, test #1 would fail."""
    src = _read(os.path.join('main', 'main_init_tasks.js'))
    neutered = src.replace(
        "!activeStreams.has(conv.id) && !conv._reconciledAt) {",
        "!activeStreams.has(conv.id)) {", 1)
    assert neutered != src, 'neuter target string not found (test brittle — fix the anchor)'
    m = re.search(r"if \(!conv\._needsLoad && !activeStreams\.has\(conv\.id\)([^)]*)\)\s*\{", neutered)
    assert m and '!conv._reconciledAt' not in m.group(1), (
        'neuter did not remove the gate — the guard test would not discriminate')


if __name__ == '__main__':
    test_case_d_gated_on_reconciled_marker()
    test_settings_maps_reconciled_marker()
    test_double_neuter_gate_removal_detected()
    print('PASS test_frontend_reconcile_defer')
