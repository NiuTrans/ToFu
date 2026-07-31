#!/usr/bin/env python3
"""The NC-guard belt must heal ONLY genuine NC poison, never legit mid-run work.

WHY (production incident 2026-07-25 — the "phantom reverter")
-------------------------------------------------------------
``tests/conftest.py::_restore_nc_patched_sources`` snapshotted every guarded
source at SESSION START and, after EVERY test, rewrote any guarded file whose
bytes differed from that baseline. On a shared-HEAD tree where commits land
MID-RUN, a long suite (pid observed 1h17m) became a phantom reverter: a real
commit (lib/message_queue.py VU-preemption block, 83c7f1ed) was silently
unwritten from the working tree ~every 4-6 minutes (the suite's per-test
completion cadence) for over an hour — strace proved O_WRONLY|O_TRUNC from
the pytest pid. The belt could not distinguish "crashed-NC leftover" from
"legitimate new committed work".

FIX
---
Heal ONLY when the current bytes carry the NC poison signature: every on-disk
NC patch embeds an ``NC-WORD`` marker in its replacement text (project
convention: ``# NC-STORM``, ``pass  # NC-OBSERVE``, ``'nc-deny-forced'`` …).
A file that merely differs from baseline with NO marker is legit work
(committed mid-run or uncommitted sibling WIP) and must be left alone —
the belt's own session-start warning (``warn_on_nc_source_poison_at_session_start``)
already covers the "started on a poisoned baseline" case for humans.

NEUTER (manual A/B): revert the marker gate in conftest.py (restore the
unconditional "differs → rewrite" branch) and
test_legit_mid_run_work_is_never_rewritten goes red.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

_unit = pytest.mark.unit


def _load_conftest_belt(tmp_path, monkeypatch):
    """Load the belt's state + restore fn with a REDIRECTED root so the test
    never touches the real tree."""
    import tests.conftest as cf
    return cf


@_unit
def test_legit_mid_run_work_is_never_rewritten(tmp_path, monkeypatch):
    """THE BUG: baseline = OLD bytes (session start, pre-commit); the tree
    file then gains LEGITIMATE new content (a mid-run commit, NO NC marker).
    The belt must NOT rewrite it."""
    import tests.conftest as cf

    target = tmp_path / 'lib' / 'message_queue.py'
    target.parent.mkdir(parents=True)
    baseline = "def old():\n    return 1\n"
    legit_new = baseline + "\n# Owner-ratified preemption (2026-07-25)\ndef new():\n    return 2\n"
    target.write_text(legit_new, encoding='utf-8')

    monkeypatch.setitem(cf._nc_source_snapshots, str(target), baseline)
    try:
        healed = cf.restore_drifted_nc_sources()
    finally:
        cf._nc_source_snapshots.pop(str(target), None)

    assert str(target) not in [os.path.join(str(tmp_path), h) for h in healed]
    assert target.read_text(encoding='utf-8') == legit_new, (
        'the belt rewrote LEGITIMATE mid-run work back to its session-start '
        'baseline — this is the phantom reverter that un-wrote commit 83c7f1ed'
    )


@_unit
def test_nc_marked_leftover_is_healed(tmp_path, monkeypatch):
    """The belt's actual job must keep working: a file left in its NEUTERED
    state (carries the NC marker) by a crashed patch IS restored to baseline."""
    import tests.conftest as cf

    target = tmp_path / 'lib' / 'message_queue.py'
    target.parent.mkdir(parents=True)
    baseline = "def real():\n    return 'real'\n"
    poisoned = "pass  # NC-OBSERVE (marker propagation disabled)\n"
    target.write_text(poisoned, encoding='utf-8')

    monkeypatch.setitem(cf._nc_source_snapshots, str(target), baseline)
    try:
        cf.restore_drifted_nc_sources()
    finally:
        cf._nc_source_snapshots.pop(str(target), None)

    assert target.read_text(encoding='utf-8') == baseline, (
        'an NC-marked leftover must still be healed to the baseline'
    )


@_unit
def test_identical_file_untouched(tmp_path, monkeypatch):
    import tests.conftest as cf
    target = tmp_path / 'lib' / 'x.py'
    target.parent.mkdir(parents=True)
    target.write_text('same\n', encoding='utf-8')
    monkeypatch.setitem(cf._nc_source_snapshots, str(target), 'same\n')
    try:
        cf.restore_drifted_nc_sources()
    finally:
        cf._nc_source_snapshots.pop(str(target), None)
    assert target.read_text(encoding='utf-8') == 'same\n'
