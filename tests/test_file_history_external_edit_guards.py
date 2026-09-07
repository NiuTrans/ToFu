"""Guards for ``lib.file_history.detect_external_edits``.

Regression suite for the two defects the external-edit probe had (both
confirmed against live store data before the fix):

  1. PHANTOM FIRES — the probe compared only mtime/size, and a Tofu write
     tool leaves a ``mtime: 0`` sentinel in ``tracked.json`` (see
     ``store._stage_explicit``).  Every subsequent probe then saw
     ``real_mtime != 0`` and staged a redundant, byte-identical backup +
     fired a false ``project_external_edit`` toast — even though nothing
     changed on disk out-of-band.  Observed live: five byte-identical
     backup blobs and 750 external snapshots on one root.

  2. MISATTRIBUTION — the store is shared across every conversation on a
     project root.  A concurrent Tofu conversation's write drifted the
     file relative to *this* task's round boundary, and the probe reported
     it as an "edited outside Tofu (IDE)" edit.  The tracked entry already
     carries ``last_writer_task_id``; a drift attributed to a known Tofu
     task is a sibling conversation, not an IDE edit.

Each guard has a NEUTER control proving it is load-bearing: disable the
guard and the false positive returns.

Run::

    python -m pytest tests/test_file_history_external_edit_guards.py -v
"""
from __future__ import annotations

_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/turn_builder.py'}

import os
import tempfile

import pytest

from lib import file_history as fh
from lib.file_history import api as fh_api
from lib.file_history.store import load_tracked

pytestmark = pytest.mark.unit


@pytest.fixture()
def base():
    """Fresh isolated project root with file-history enabled."""
    prev = os.environ.get('TOFU_FILE_HISTORY')
    prev_probe = os.environ.get('TOFU_FILE_HISTORY_PROBE')
    os.environ['TOFU_FILE_HISTORY'] = '1'
    os.environ['TOFU_FILE_HISTORY_PROBE'] = '1'
    with tempfile.TemporaryDirectory() as d:
        yield d
    if prev is None:
        os.environ.pop('TOFU_FILE_HISTORY', None)
    else:
        os.environ['TOFU_FILE_HISTORY'] = prev
    if prev_probe is None:
        os.environ.pop('TOFU_FILE_HISTORY_PROBE', None)
    else:
        os.environ['TOFU_FILE_HISTORY_PROBE'] = prev_probe


def _write(base_path: str, rel: str, content: str) -> None:
    p = os.path.join(base_path, rel)
    os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(content)


def _latest_version(base_path: str, rel: str) -> int:
    return int((load_tracked(base_path).get(rel) or {}).get('latest_version') or 0)


def _simulate_tofu_net_zero_write(base_path: str, rel: str, *,
                                  task_id: str | None = None) -> None:
    """Reproduce the exact state a Tofu write tool leaves behind.

    ``_record_modification`` calls ``track_edit(pre_content=<pre-image>)``
    AFTER the write, which routes to ``store._stage_explicit`` — that
    bumps the version, stores the given bytes as the blob, and writes the
    poisonous ``mtime: 0`` sentinel + ``last_writer_task_id``.  For a
    net-zero content change the pre-image equals the on-disk bytes, so the
    latest backup blob is byte-identical to disk yet the index mtime is 0.
    """
    with open(os.path.join(base_path, rel), encoding='utf-8') as f:
        cur = f.read()
    fh.track_edit(base_path, rel, pre_content=cur, task_id=task_id)


def test_restore_write_failure_preserves_last_good_file(base, monkeypatch):
    target = os.path.join(base, 'document.txt')
    with open(target, 'wb') as handle:
        handle.write(b'last-good')
    monkeypatch.setattr(fh_api, 'read_blob',
                        lambda _base, _rel, _version: b'restored')
    monkeypatch.setattr(
        'lib.json_store.os.replace',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError('injected replace failure')))

    ok, detail = fh_api._restore_one(base, 'document.txt', 1)

    assert ok is False and 'injected replace failure' in detail
    with open(target, 'rb') as handle:
        assert handle.read() == b'last-good'
    assert not [name for name in os.listdir(base)
                if name.startswith('.binstore-')]


# ═══════════════════════════════════════════════════════════════════
#  Guard 1 — CONTENT: a byte-identical rewrite is NOT drift
# ═══════════════════════════════════════════════════════════════════

def test_content_guard_suppresses_phantom_identical_rewrite(base):
    """The mtime:0 sentinel + identical content must NOT fire or back up."""
    _write(base, 'lib/turn_builder.py', 'X' * 100)
    fh.track_edit(base, 'lib/turn_builder.py')          # v1, real mtime/size
    # A Tofu write that produced identical bytes leaves mtime:0 behind.
    _simulate_tofu_net_zero_write(base, 'lib/turn_builder.py')
    v_before = _latest_version(base, 'lib/turn_builder.py')
    assert v_before >= 1

    # Sanity: the poison is present (mtime sentinel), so a naive mtime probe
    # WOULD have flagged drift — the content guard is what saves us.
    entry = load_tracked(base)['lib/turn_builder.py']
    assert float(entry.get('mtime') or 0) == 0.0, 'setup: mtime:0 sentinel present'

    res = fh.detect_external_edits(base, known_task_ids=set())
    assert res['committed'] is False, 'identical content must not commit a snapshot'
    assert res['files'] == [], 'identical content must not be reported as drift'
    assert res['siblingFiles'] == []
    assert _latest_version(base, 'lib/turn_builder.py') == v_before, \
        'no redundant backup version staged for identical content'
    # The stale sentinel mtime is reconciled so future probes short-circuit.
    reconciled = float(load_tracked(base)['lib/turn_builder.py'].get('mtime') or 0)
    assert reconciled != 0.0, 'stale mtime:0 reconciled to real mtime'


def test_content_guard_repeated_probes_stay_silent(base):
    """Five consecutive probes on unchanged content produce ZERO snapshots.

    This is the exact live symptom (five identical blobs / snapshot spam);
    with the guard, unchanged content yields no external snapshots at all.
    """
    _write(base, 'a.py', 'same-bytes\n')
    fh.track_edit(base, 'a.py')
    _simulate_tofu_net_zero_write(base, 'a.py')
    v0 = _latest_version(base, 'a.py')
    for _ in range(5):
        res = fh.detect_external_edits(base, known_task_ids=set())
        assert res['committed'] is False
        assert res['files'] == []
    assert _latest_version(base, 'a.py') == v0, 'no version churn across 5 probes'


def test_content_guard_neutered_reproduces_phantom_fire(base, monkeypatch):
    """NEUTER: disable the content check → the phantom fire returns.

    Proves the guard is load-bearing, not a tautology.  With
    ``_disk_matches_last_backup`` forced to 'not identical', the same
    identical-content + mtime:0 state fires exactly as the old code did.
    """
    _write(base, 'lib/turn_builder.py', 'X' * 100)
    fh.track_edit(base, 'lib/turn_builder.py')
    _simulate_tofu_net_zero_write(base, 'lib/turn_builder.py')

    # Revert Guard 1: pretend disk never matches the last backup.
    monkeypatch.setattr(fh_api, '_disk_matches_last_backup',
                        lambda *a, **k: False)

    res = fh.detect_external_edits(base, known_task_ids=set())
    assert res['committed'] is True, 'neutered guard fires on identical content'
    assert 'lib/turn_builder.py' in res['files']


def test_genuine_external_edit_still_fires(base):
    """CONTROL: a real out-of-band content change is still reported."""
    _write(base, 'lib/turn_builder.py', 'original\n')
    fh.track_edit(base, 'lib/turn_builder.py')
    _simulate_tofu_net_zero_write(base, 'lib/turn_builder.py')  # mtime:0 poison
    # Now a true IDE save changes the bytes.
    _write(base, 'lib/turn_builder.py', 'the IDE genuinely changed this\n')

    res = fh.detect_external_edits(base, known_task_ids=set())
    assert res['committed'] is True, 'genuine content change must fire'
    assert 'lib/turn_builder.py' in res['files']
    assert res['siblingFiles'] == []


# ═══════════════════════════════════════════════════════════════════
#  Guard 2 — ATTRIBUTION: a sibling Tofu write is not an "IDE" edit
# ═══════════════════════════════════════════════════════════════════

def test_attribution_guard_excludes_sibling_conversation_write(base):
    """A genuine drift stamped by a KNOWN Tofu task is a sibling, not IDE."""
    _write(base, 'lib/turn_builder.py', 'v1\n')
    fh.track_edit(base, 'lib/turn_builder.py')
    # A concurrent conversation's write: it stamps its own task id and (via
    # _stage_explicit) leaves mtime:0 while disk holds its NEW content.
    _write(base, 'lib/turn_builder.py', 'sibling conversation wrote this\n')
    _simulate_tofu_net_zero_write(base, 'lib/turn_builder.py',
                                  task_id='sibling-task-123')
    # Its post-image differs from the pre-image blob → genuine content change.
    _write(base, 'lib/turn_builder.py', 'sibling conversation wrote this v2\n')

    res = fh.detect_external_edits(base, known_task_ids={'sibling-task-123'})
    assert res['committed'] is False, 'sibling write must not commit an external snapshot'
    assert res['files'] == [], 'sibling write must not be reported as external/IDE'
    assert res['siblingFiles'], 'sibling write surfaced under siblingFiles'
    entry = res['siblingFiles'][0]
    assert entry['path'] == 'lib/turn_builder.py'
    assert entry['taskId'] == 'sibling-task-123'


def test_attribution_guard_neutered_misattributes_as_external(base):
    """NEUTER: with the writer NOT in known_task_ids the guard can't bite.

    Same task-stamped drift, but the probe isn't told the task is a live
    Tofu task → it (wrongly, as the old code did) reports it as external.
    Proves the ``known_task_ids`` attribution check is load-bearing.
    """
    _write(base, 'lib/turn_builder.py', 'v1\n')
    fh.track_edit(base, 'lib/turn_builder.py')
    _write(base, 'lib/turn_builder.py', 'sibling wrote this\n')
    _simulate_tofu_net_zero_write(base, 'lib/turn_builder.py',
                                  task_id='sibling-task-123')
    _write(base, 'lib/turn_builder.py', 'sibling wrote this v2\n')

    # Guard neutered: the known-task set does not contain the writer.
    res = fh.detect_external_edits(base, known_task_ids=set())
    assert res['committed'] is True, 'without attribution the drift fires as external'
    assert 'lib/turn_builder.py' in res['files']
    assert res['siblingFiles'] == []


def test_unattributed_genuine_edit_is_external(base):
    """A genuine drift with NO writer stamp is a real external edit.

    The attribution guard must only suppress writes attributable to a
    KNOWN Tofu task — an empty ``last_writer_task_id`` (true out-of-band
    IDE edit) stays external even when known_task_ids is populated.
    """
    _write(base, 'notes.md', 'draft\n')
    fh.track_edit(base, 'notes.md')                    # no task_id → writer ''
    _write(base, 'notes.md', 'human typed this in the editor\n')

    res = fh.detect_external_edits(base, known_task_ids={'some-other-task'})
    assert res['committed'] is True
    assert 'notes.md' in res['files']
    assert res['siblingFiles'] == []
