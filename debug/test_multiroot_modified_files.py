"""Integration test reproducing the EXACT cross-conversation bug the user
reported, exercising the REAL orchestrator code paths (not a mirror).

Scenario (matches the screenshots):
  • Conversation X runs in a MULTI-ROOT workspace: primary root `primary`
    plus an EXTRA root `chatui`.  Its task writes a NEW file
    `static/icons/mcp/longcat.svg` and patches `lib/mcp/registry.py` —
    both in the EXTRA `chatui` root.
  • CONCURRENTLY, conversation Y (a journalling session) patches
    `JOURNAL.md` in the PRIMARY root and stages it in file-history WITHOUT
    conversation X's task id.

Before the fix:
  • `_finalize_and_emit_done` derived `modifiedFileList` from the PRIMARY
    root only → empty for task X → the fh side-channel seeded it with
    conversation Y's `JOURNAL.md`.  The bar showed only `JOURNAL.md`, the
    real edits were invisible.

After the fix:
  • `derive_round_modified_files` unions task X's journalled writes across
    BOTH roots → returns longcat.svg + registry.py (root-tagged `chatui`).
  • The fh enrichment gate drops the concurrent unstamped `JOURNAL.md`
    because task X ran only tracked-edit tools (no opaque writer).

This drives the ACTUAL functions in lib/tasks_pkg/orchestrator.py and
lib/project_mod/modifications.py — the closest automated proxy for
"re-run that conversation" without a live browser/LLM/server.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault('TOFU_FILE_HISTORY', '1')

from lib import file_history as fh  # noqa: E402
from lib.project_mod import config as pm_config  # noqa: E402
from lib.project_mod.modifications import _record_modification  # noqa: E402
from lib.tasks_pkg.orchestrator import derive_round_modified_files  # noqa: E402


def _register_root(name: str, abs_path: str) -> None:
    """Register a workspace root in the global registry so
    `_record_modification` tags its mods with the right root name."""
    with pm_config._lock:
        pm_config._roots[name] = pm_config._make_root_state(abs_path)


def main() -> int:
    failed = 0
    with tempfile.TemporaryDirectory() as primary, \
         tempfile.TemporaryDirectory() as chatui:
        # Two registered roots: `primary` (primary) + `chatui` (extra).
        _register_root('primary', primary)
        _register_root('chatui', chatui)

        TASK_X = 'task-X-multiroot'
        CONV_X = 'conv-X'

        # ── Conversation X: real writes into the EXTRA `chatui` root ──
        # 1) NEW file longcat.svg (write_file, existed=False → "created")
        svg_rel = 'static/icons/mcp/longcat.svg'
        svg_abs = os.path.join(chatui, svg_rel)
        os.makedirs(os.path.dirname(svg_abs), exist_ok=True)
        with open(svg_abs, 'w') as f:
            f.write('<svg/>\n')
        _record_modification(chatui, 'write_file', svg_rel,
                             original_content=None,  # new file
                             conv_id=CONV_X, task_id=TASK_X)

        # 2) PATCH registry.py (apply_diff → "patched")
        reg_rel = 'lib/mcp/registry.py'
        reg_abs = os.path.join(chatui, reg_rel)
        os.makedirs(os.path.dirname(reg_abs), exist_ok=True)
        with open(reg_abs, 'w') as f:
            f.write('ICON = "longcat.svg"\n')
        _record_modification(chatui, 'apply_diff', reg_rel,
                             original_content='ICON = "old"\n',
                             reverse_patch={'search': 'longcat.svg', 'replace': 'old'},
                             conv_id=CONV_X, task_id=TASK_X)

        # ── Conversation Y: concurrent journalling write into PRIMARY root,
        #    NOT stamped with task X's id. ──
        journal_rel = 'JOURNAL.md'
        journal_abs = os.path.join(primary, journal_rel)
        with open(journal_abs, 'w') as f:
            f.write('# Journal\nentry\n')
        _record_modification(primary, 'apply_diff', journal_rel,
                             original_content='# Journal\n',
                             reverse_patch={'search': 'entry', 'replace': ''},
                             conv_id='conv-Y', task_id='task-Y-journal')

        # ── Exercise the REAL derivation helper ──
        task = {'id': TASK_X, 'convId': CONV_X, 'created_at': 0}
        project_paths = [primary, chatui]  # index 0 == primary
        file_list, n_mods, _ts = derive_round_modified_files(
            task, primary, project_paths)

        paths = {f['path'] for f in file_list}
        by_path = {f['path']: f for f in file_list}

        # ── Assertions ──
        # 1) Both real extra-root edits present.
        if svg_rel in paths and reg_rel in paths:
            print(f'PASS — both extra-root edits present: {sorted(paths)}')
        else:
            print(f'FAIL — missing extra-root edits (got {sorted(paths)})')
            failed += 1

        # 2) The concurrent JOURNAL.md is NOT misattributed to task X.
        if journal_rel not in paths:
            print('PASS — concurrent JOURNAL.md NOT misattributed to task X')
        else:
            print(f'FAIL — JOURNAL.md leaked into task X list (got {sorted(paths)})')
            failed += 1

        # 3) Files are root-tagged `chatui` so the bar shows the prefix.
        ok_root = all(by_path[p].get('root') == 'chatui'
                      for p in (svg_rel, reg_rel) if p in by_path)
        if ok_root:
            print("PASS — edits tagged with root 'chatui' (bar shows chatui: prefix)")
        else:
            print(f'FAIL — edits missing chatui root tag (got {file_list})')
            failed += 1

        # 4) Correct actions: created (new svg) + patched (apply_diff).
        if (by_path.get(svg_rel, {}).get('action') == 'created'
                and by_path.get(reg_rel, {}).get('action') == 'patched'):
            print('PASS — actions classified: svg=created, registry=patched')
        else:
            print(f'FAIL — wrong actions (got {file_list})')
            failed += 1

        # 5) Negative control: scanning the PRIMARY root alone (the OLD
        #    behaviour) would have returned NOTHING for task X — proving the
        #    bug was real and the multi-root union is what fixes it.
        primary_only_list, _, _ = derive_round_modified_files(
            task, primary, [primary])  # no extra root
        if not primary_only_list:
            print('PASS — negative control: primary-only scan finds nothing '
                  '(reproduces the original empty-list bug)')
        else:
            print(f'FAIL — primary-only scan unexpectedly found {primary_only_list}')
            failed += 1

        # Cleanup registry so we don't leak into other tests in-process.
        with pm_config._lock:
            pm_config._roots.pop('primary', None)
            pm_config._roots.pop('chatui', None)

    if failed:
        print(f'\n{failed} test(s) FAILED')
        return 1
    print('\nAll tests passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
