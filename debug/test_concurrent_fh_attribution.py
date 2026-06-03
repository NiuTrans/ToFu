"""Reproduces the cross-conversation modifiedFileList bug and verifies
the per-task attribution + atomic-commit fixes.

Two simulated tasks (Task-A and Task-B) operate on the SAME project
root concurrently.  Task-B writes a real edit to ``b_only.py``.
Task-A writes nothing.

Without the fixes:
* Task-A's commit thread captures prev_snap before Task-B's snapshot,
  then Task-A's make_snapshot pins tracked.json (which already
  includes Task-B's edit), and diff_name_status reports b_only.py
  as a Task-A change.

With Fix 2 + Fix 3:
* Task-A's fh side-channel sees b_only.py's last_writer_task_id =
  Task-B and drops it.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lib import file_history as fh  # noqa: E402
from lib.file_history.store import _project_lock as fh_project_lock  # noqa: E402
from lib.file_history.store import load_tracked  # noqa: E402

os.environ.setdefault('TOFU_FILE_HISTORY', '1')


def _commit_round_simulating_orchestrator(base, *, task_id, conv_id, mods):
    """Mirror lib/tasks_pkg/orchestrator.py:_run_commit_round_async.

    Returns the side-channel-attributed file list this task would have
    reported (after the new attribution filter).
    """
    rel_paths = [m['path'] for m in mods if m.get('path')]
    tool_names = [m.get('type') or '' for m in mods]

    fh_changes = []
    tracked_index = {}
    with fh_project_lock(base):
        prev_snap = fh.get_last_snapshot_id(base)
        snap_id = fh.make_snapshot(
            base, task_id=task_id, conv_id=conv_id,
            tool_names=tool_names or None, summary='', rel_paths=rel_paths or None,
        )
        if snap_id:
            fh_changes = fh.diff_name_status(base, prev_snap, snap_id) or []
            tracked_index = load_tracked(base) or {}

    # Apply the new attribution filter (Fix 2).
    own = task_id
    filtered = []
    for entry in fh_changes:
        writer = (tracked_index.get(entry['path'], {})
                  .get('last_writer_task_id') or '')
        if not writer or writer == own:
            filtered.append(entry)
    return [e['path'] for e in filtered]


def _simulate_writer_task(base, *, task_id, conv_id, paths, content_template):
    """Simulate a task that calls track_edit(... pre_content=...) for each
    path then commits.  Returns (own_attributed_paths)."""
    mods = []
    for rel in paths:
        abs_p = os.path.join(base, rel)
        os.makedirs(os.path.dirname(abs_p), exist_ok=True)
        # Pre-image: previous on-disk content (or empty if new).
        pre = b''
        if os.path.exists(abs_p):
            with open(abs_p, 'rb') as f:
                pre = f.read()
        # Write new content.
        with open(abs_p, 'wb') as f:
            f.write((content_template % task_id).encode())
        # Pre-write hook (modifications._record_modification path).
        fh.track_edit(base, rel, message_id=task_id, pre_content=pre,
                      task_id=task_id)
        mods.append({'path': rel, 'type': 'write_file'})
    return _commit_round_simulating_orchestrator(
        base, task_id=task_id, conv_id=conv_id, mods=mods)


def _simulate_no_op_task(base, *, task_id, conv_id):
    """Simulate a task that did NOT touch any file but still commits."""
    return _commit_round_simulating_orchestrator(
        base, task_id=task_id, conv_id=conv_id, mods=[])


def main() -> int:
    failed = 0
    with tempfile.TemporaryDirectory() as base:
        # Sequence-level baseline: warm up tracked.json.
        with open(os.path.join(base, 'seed.txt'), 'w') as f:
            f.write('seed\n')
        fh.track_edit(base, 'seed.txt', task_id='task-init')
        fh.make_snapshot(base, task_id='task-init', conv_id='conv-init')

        # Two tasks operating on the same project.  Task-B writes
        # b_only.py; Task-A writes nothing.  We run them serially in
        # this test (the lock would funnel them anyway) but still
        # produces the same misattribution risk because Task-A's
        # commit happens AFTER Task-B's writes have updated
        # tracked.json.
        b_results = _simulate_writer_task(
            base, task_id='task-B', conv_id='conv-B',
            paths=['b_only.py'],
            content_template='# written by %s\n',
        )
        a_results = _simulate_no_op_task(
            base, task_id='task-A', conv_id='conv-A',
        )

        # Task-B should have its own file in its side-channel diff.
        if 'b_only.py' in b_results:
            print(f'PASS — Task-B fh side-channel reports b_only.py (got {b_results})')
        else:
            print(f'FAIL — Task-B did NOT report b_only.py (got {b_results})')
            failed += 1

        # Task-A should NOT see b_only.py in its side-channel diff.
        if 'b_only.py' not in a_results:
            print(f'PASS — Task-A fh side-channel does NOT misattribute '
                  f"b_only.py (got {a_results or '[]'})")
        else:
            print(f'FAIL — Task-A still misattributes b_only.py (got {a_results})')
            failed += 1

        # Now repeat with TRUE concurrency: two threads commit at
        # roughly the same time.  Even when racing, attribution must
        # still hold.
        events = {'go': threading.Event()}
        results: dict[str, list] = {}

        def _runC():
            events['go'].wait()
            results['C'] = _simulate_writer_task(
                base, task_id='task-C', conv_id='conv-C',
                paths=['c_only.py'],
                content_template='# written by %s\n',
            )

        def _runD():
            events['go'].wait()
            results['D'] = _simulate_no_op_task(
                base, task_id='task-D', conv_id='conv-D',
            )

        tC = threading.Thread(target=_runC, name='task-C')
        tD = threading.Thread(target=_runD, name='task-D')
        tC.start(); tD.start()
        time.sleep(0.05)
        events['go'].set()
        tC.join(); tD.join()

        if 'c_only.py' in results['C']:
            print(f"PASS — Task-C reports c_only.py (got {results['C']})")
        else:
            print(f"FAIL — Task-C missing c_only.py (got {results['C']})")
            failed += 1

        if 'c_only.py' not in results['D']:
            print(f"PASS — Task-D does NOT misattribute c_only.py "
                  f"(got {results['D'] or '[]'})")
        else:
            print(f"FAIL — Task-D misattributed c_only.py (got {results['D']})")
            failed += 1

    if failed:
        print(f'\n{failed} test(s) FAILED')
        return 1
    print('\nAll tests passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
