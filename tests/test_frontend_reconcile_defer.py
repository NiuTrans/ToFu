#!/usr/bin/env python3
"""The frontend must NOT re-infer settled lifecycle state the backend already
reconciled — guarded in its POST-retirement form.

WHY (two-layer history)
-----------------------
Phase-3 made ``recover_stale_tasks_on_startup`` +
``routes/conversations.py::_reconcile_conv_on_get_blocking`` run the
buried-ghost sweep + tail classification SERVER-side
(lib/conversations/reconcile.py), persist it in one commit, and stamp
settings._reconciledAt. The frontend's own init-time Case-D sweep was first
GATED on ``!conv._reconciledAt``, then RETIRED outright (2026-07-11, see the
"Case D: RETIRED" comment block in main_init_tasks.js): the verdict now lives
ONLY on the backend, applied on EVERY render path (single-conv GET, the
?meta=1&prefetch= path, startup recovery), proven byte-equivalent by
tests/test_reconcile_js_backend_equivalence.py.

GUARD-STALE FAMILY, case #6 (2026-07-27, epic pt_3a0cdc233c19408f): this suite
previously scanned main_init_tasks.js for the PRE-retirement gated block
(``if (!conv._needsLoad && !activeStreams.has(conv.id) && !conv._reconciledAt)``)
and core/conversations.js for the settings mapping. Both anchors died of
DELIBERATE refactors, not regressions:
  1. the Case-D retirement removed the gated block entirely (the property is
     now held MORE strongly — there is no frontend init-time classifier);
  2. fc0d8d60 (pt_3879f00e slice 5) extracted _applySettingsToConv into
     core/conv_apply_settings.js, taking the mapping with it.
The suite is therefore re-pointed at the CURRENT invariant:

  1. main_init_tasks.js contains NO init-time Case-D sweep entry shape and NO
     ``_classifyGhostTail(`` call. (The stream-finalize ``_classifyGhostTailJS``
     in ui/chat_render.js is a DIFFERENT, sanctioned mechanism — a faithful 1:1
     port of reconcile.classify_ghost_tail consumed by stream_lifecycle.js;
     it is NOT lifecycle inference at conv-load time and is out of scope here.)
  2. core/conv_apply_settings.js still maps settings._reconciledAt →
     conv._reconciledAt, so any FUTURE defer gate can see the backend marker.

NEUTER (in a tmp COPY, real files untouched): re-injecting the old Case-D
block into main_init_tasks.js makes assertion #1 fail; stripping the mapping
from conv_apply_settings.js makes assertion #2 fail.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')

# The retired init-time Case-D sweep entry shape (pre-2026-07-11). Its return
# would re-introduce frontend lifecycle inference at conv-load time.
_CASE_D_ENTRY_RE = re.compile(r"if \(!conv\._needsLoad && !activeStreams\.has\(conv\.id\)")

# Injection used by the NEUTER control: a minimal re-animated Case-D block.
_CASE_D_RESURRECTION = (
    "\n      if (!conv._needsLoad && !activeStreams.has(conv.id)"
    " && !conv._reconciledAt) {\n"
    "        _classifyGhostTail(conv);\n"
    "      }\n"
)


def _read(rel):
    with open(os.path.join(JS_DIR, rel), encoding='utf-8') as f:
        return f.read()


def _assert_no_init_time_ghost_sweep(src: str) -> None:
    assert 'Case D: RETIRED' in src, (
        'the Case-D retirement marker comment vanished from main_init_tasks.js '
        '— if the retirement itself was reverted, the frontend init-time ghost '
        'verdict is back and lifecycle state is inferred in TWO places again '
        '(separation-of-concerns violation; the backend reconcile in '
        'lib/conversations/reconcile.py is the ONLY sanctioned home).')
    m = _CASE_D_ENTRY_RE.search(src)
    assert not m, (
        'REGRESSION: an init-time Case-D sweep entry reappeared in '
        'main_init_tasks.js — the frontend would re-infer settled lifecycle '
        'state at conv-load time, which the backend reconcile already owns '
        '(retired 2026-07-11; equivalence pinned by '
        'tests/test_reconcile_js_backend_equivalence.py).')
    assert '_classifyGhostTail(' not in src, (
        'REGRESSION: main_init_tasks.js calls _classifyGhostTail( — the '
        'init-time ghost-tail classifier was deliberately removed; the '
        'stream-finalize _classifyGhostTailJS (ui/chat_render.js) is the only '
        'sanctioned JS port and belongs to a different path.')


def test_no_frontend_init_time_ghost_sweep():
    _assert_no_init_time_ghost_sweep(_read(os.path.join('main', 'main_init_tasks.js')))


def test_settings_maps_reconciled_marker():
    # Real home since fc0d8d60 (pt_3879f00e slice 5 extraction); the pre-slice
    # anchor core/conversations.js no longer carries the mapping.
    src = _read(os.path.join('core', 'conv_apply_settings.js'))
    assert re.search(r"settings\._reconciledAt\b", src) and 'conv._reconciledAt = settings._reconciledAt' in src, (
        'conv_apply_settings.js must map settings._reconciledAt → '
        'conv._reconciledAt or no future frontend defer gate can see the '
        'backend reconcile marker.')


def test_double_neuter_gate_removal_detected():
    """Byte-revert control: with the retirement undone / the mapping stripped,
    assertions #1 and #2 would fail — proving both discriminate."""
    src = _read(os.path.join('main', 'main_init_tasks.js'))
    resurrected = src + _CASE_D_RESURRECTION
    assert resurrected != src, 'neuter setup failed (no-op concatenation)'
    with pytest.raises(AssertionError):
        _assert_no_init_time_ghost_sweep(resurrected)

    mapping_src = _read(os.path.join('core', 'conv_apply_settings.js'))
    stripped = mapping_src.replace(
        'conv._reconciledAt = settings._reconciledAt',
        'conv._reconciledAt = undefined', 1)
    assert stripped != mapping_src, 'neuter target string not found (test brittle — fix the anchor)'
    assert not (re.search(r"settings\._reconciledAt\b", stripped)
                and 'conv._reconciledAt = settings._reconciledAt' in stripped), (
        'neuter did not strip the mapping — the guard test would not discriminate')


if __name__ == '__main__':
    test_no_frontend_init_time_ghost_sweep()
    test_settings_maps_reconciled_marker()
    test_double_neuter_gate_removal_detected()
    print('PASS test_frontend_reconcile_defer')
