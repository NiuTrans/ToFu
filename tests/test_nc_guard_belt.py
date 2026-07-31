#!/usr/bin/env python3
"""Regression: the conftest nc-guard belt heals a shipped source that a crashed
on-disk negative-control (``_patch_restore``) left in its NEUTERED state.

WHY
---
A family of NC tests physically byte-patch a shipped ``lib/`` source, run the
neutered assertion, and restore it in a ``finally``. If the test is KILLED
mid-patch (per-test timeout, xdist worker crash, KeyboardInterrupt) the
``finally`` never runs and the file stays neutered — cascading into EVERY later
importer for the rest of the session (this is exactly how the ``_persist.py``
vertical-relocation line and the ``project_peer.py`` feed branch were found
poisoned in the working tree). The autouse ``_restore_nc_patched_sources`` belt
in ``tests/conftest.py`` is the backstop: it snapshots each guarded source once
and, after EVERY test, rewrites any that drifted from the snapshot.

This test exercises the belt's real machinery (``_snapshot_nc_sources`` +
``_restore_nc_patched_sources``) against a REAL guarded source, simulating the
crash by writing a neutered variant WITHOUT restoring it (the missed
``finally``), then asserting the belt puts it back byte-identical.

It also guards the two structural properties the belt depends on:
  * every guarded path exists on disk (a stale entry is a silent no-op), and
  * every guarded source is genuinely a target of an on-disk NC writer OR is a
    module an NC neuters — i.e. the list tracks the audit, not guesswork.

Run:  python3 tests/test_nc_guard_belt.py
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))

_PRISTINE = "CANONICAL = 1\ndef load_bearing():\n    return 'real'\n"
# ⚠️ The poisoned variant MUST carry a real ``NC-WORD`` marker.
# ``restore_drifted_nc_sources`` heals ONLY marker-bearing drift (the marker
# gate added in 0910e72e after the "phantom reverter" incident, where the belt
# silently un-wrote a legitimate mid-run commit). This fixture previously used
# a bare ``__NC_POISONED_`` prefix, which the gate's ``\bNC-[A-Z0-9]…`` pattern
# does NOT match — no word boundary before ``NC`` inside ``__NC_``, and no
# hyphen. So the belt correctly declined to heal it and the test went red while
# the belt was working exactly as designed: the fixture, not the belt, had
# fallen out of date with the contract.
#
# Using the SAME marker convention as the real NC writers (``# NC-STORM``,
# ``pass  # NC-OBSERVE``, ``'nc-deny-forced'``) is what makes this test
# exercise the production heal path rather than a shape nothing ever produces.
_POISONED = ("CANONICAL = 1\ndef load_bearing():\n"
             "    return 'neutered'  # NC-BELT-SIM\n")


@contextlib.contextmanager
def _belt_guarding_a_temp_file():
    """Register a throwaway temp file into the belt's snapshot dict, primed to
    its pristine bytes. Restores the real snapshot dict on exit.

    Why a temp file, NOT a real ``lib/`` source: the belt's ``restore_*`` pass
    simply iterates ``_nc_source_snapshots`` and rewrites any path that drifted,
    so a temp path exercises the IDENTICAL logic. Writing a shared shipped
    source on disk here would itself be the xdist hazard this whole change
    exists to kill (a parallel worker importing it mid-poison would crash) — the
    test must not reintroduce the disease it cures.
    """
    import tests.conftest as ct
    fd, tmp = tempfile.mkstemp(prefix='nc_belt_', suffix='.py')
    os.close(fd)
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(_PRISTINE)
    saved = dict(ct._nc_source_snapshots)
    ct._nc_source_snapshots.clear()
    ct._nc_source_snapshots[tmp] = _PRISTINE
    try:
        yield ct, tmp
    finally:
        ct._nc_source_snapshots.clear()
        ct._nc_source_snapshots.update(saved)
        with contextlib.suppress(OSError):
            os.remove(tmp)


def test_all_guarded_sources_exist():
    """A stale entry in _NC_GUARDED_SOURCES is a silent no-op (the snapshot
    skips a missing file), so the list must only name files that exist."""
    import tests.conftest as ct
    missing = [rel for rel in ct._NC_GUARDED_SOURCES
               if not os.path.isfile(os.path.join(ct._ROOT_DIR, rel))]
    assert not missing, f'_NC_GUARDED_SOURCES names non-existent files: {missing}'


def test_persist_and_conversation_are_guarded():
    """Regression pin: the sources whose on-disk NCs poisoned the tree (the
    audit gap) must stay in the guarded set.

    ``_persist`` is named by its POST-PACKAGING path. The compaction
    ``_persist.py`` module became a ``_persist/`` package, and the NC actually
    writes ``_persist/_splitters.py``; this assertion kept naming the old
    single-file path and so went red while the belt itself was fine and
    conftest already tracked the right file. A pin that names a path nobody
    writes any more tests the layout of a past refactor, not the protection.
    """
    import tests.conftest as ct
    for rel in ('lib/tasks_pkg/compaction/_persist/_splitters.py',
                'lib/tools/conversation.py',
                'lib/conversations/project_brain_influence.py'):
        assert rel in ct._NC_GUARDED_SOURCES, \
            f'{rel} must be guarded (it is written on disk by an NC)'


def test_belt_heals_interrupted_neuter():
    """Simulate a crashed _patch_restore: poison a belt-guarded file and DO NOT
    restore it (the missed ``finally``). The belt's restore pass must rewrite it
    byte-identical to the session snapshot and report having healed it."""
    with _belt_guarding_a_temp_file() as (ct, tmp):
        # ── Simulate the crash: poison the file, skip the restore. ──
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(_POISONED)
        with open(tmp, encoding='utf-8') as f:
            assert f.read() == _POISONED, 'precondition: file is poisoned'

        # ── The belt's after-every-test restore pass runs. ──
        # restore_drifted_nc_sources() reports paths RELATIVE to the repo root
        # (a temp file resolves to a ../.. chain), so match by basename; the
        # authoritative signal is that the file content is restored.
        healed = ct.restore_drifted_nc_sources()
        assert any(os.path.basename(h) == os.path.basename(tmp) for h in healed), \
            f'the belt must report healing the drifted file; healed={healed}'

        with open(tmp, encoding='utf-8') as f:
            assert f.read() == _PRISTINE, \
                'belt must restore the poisoned file byte-identical to the snapshot'


def test_belt_noop_when_clean():
    """A clean tree drifts nothing → the belt heals nothing (no spurious writes
    that would churn the working tree every test)."""
    with _belt_guarding_a_temp_file() as (ct, tmp):
        healed = ct.restore_drifted_nc_sources()
        assert healed == [], 'belt must not rewrite a file that never drifted'


def test_NC_belt_without_restore_leaves_poison():
    """Negative control: WITHOUT the belt's restore pass, the simulated crash
    leaves the file poisoned — proving the belt's restore is what heals it (not
    some other cleanup)."""
    with _belt_guarding_a_temp_file() as (_ct, tmp):
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(_POISONED)
        # NO belt restore invoked here.
        with open(tmp, encoding='utf-8') as f:
            still = f.read()
        assert still == _POISONED and still != _PRISTINE, \
            'without the belt the poison persists (belt is load-bearing)'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:napari']))
