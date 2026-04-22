#!/usr/bin/env python3
"""Recover patches wrongly reported as empty due to the git-add-timeout bug.

Background (see .chatui/skills/swebench-git-add-timeout-bug.md):
  The previous run used `timeout=10` for `git add -A`; on FUSE/NFS under concurrent
  load those commands routinely exceeded 10s and the extractor silently returned ''.
  The surviving per-instance workspaces still contain the real edits, so we can
  re-extract patches with the fixed (120s) extractor and re-evaluate just those.

What this script does:
  1. Scans swebench_workdir/patches/*.diff for files that are empty/placeholder.
  2. For each, checks the matching workspace in swebench_workdir/workspaces/.
  3. Re-extracts the diff using the current (fixed) _extract_git_diff().
  4. Overwrites the patch file with the recovered diff (backs up the old one).
  5. Writes a JSON manifest of what was recovered vs genuinely empty.

This script does NOT do evaluation — run the runner with --reeval afterwards to
update the swebench_results.json with the recovered patches.

Logging goes to both stdout and swebench_workdir/recovery.log.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from debug.swebench_runner import _extract_git_diff  # reuse fixed extractor

WORKDIR = _PROJECT_ROOT / 'swebench_workdir'
PATCH_DIR = WORKDIR / 'patches'
WORKSPACE_DIR = WORKDIR / 'workspaces'
BACKUP_DIR = WORKDIR / 'patches_before_recovery'
LOG_FILE = WORKDIR / 'recovery.log'
MANIFEST_FILE = WORKDIR / 'recovery_manifest.json'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-5s %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.FileHandler(str(LOG_FILE), mode='a', encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger('recover')

_PLACEHOLDER = '# (empty'


def _is_empty_patch(text: str) -> bool:
    """Return True if this patch file is empty or just the placeholder comment."""
    stripped = text.strip()
    if not stripped:
        return True
    return stripped.startswith(_PLACEHOLDER)


def main() -> int:
    if not PATCH_DIR.exists():
        log.error('No patch directory at %s', PATCH_DIR)
        return 1

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    patch_files = sorted(PATCH_DIR.glob('*.diff'))
    log.info('[Recovery] Scanning %d patch files in %s', len(patch_files), PATCH_DIR)

    empty_files = []
    for pf in patch_files:
        try:
            text = pf.read_text()
        except Exception as e:
            log.warning('Failed to read %s: %s', pf.name, e)
            continue
        if _is_empty_patch(text):
            empty_files.append(pf)

    log.info('[Recovery] Found %d empty/placeholder patches', len(empty_files))

    results = {
        'recovered': [],       # had real edits
        'genuinely_empty': [], # workspace has no changes
        'missing_workspace': [], # workspace gone
        'extraction_failed': [], # fixed extractor still failed
    }

    t0 = time.time()
    for i, pf in enumerate(empty_files, 1):
        stem = pf.stem  # e.g. django__django-10097__tofu-glm
        ws = WORKSPACE_DIR / stem
        if not ws.exists() or not (ws / '.git').exists():
            log.warning('[%d/%d] %s — workspace missing', i, len(empty_files), stem)
            results['missing_workspace'].append(stem)
            continue

        try:
            diff = _extract_git_diff(ws)
        except Exception as e:
            log.error('[%d/%d] %s — extraction crashed: %s',
                      i, len(empty_files), stem, e, exc_info=True)
            results['extraction_failed'].append({'stem': stem, 'error': str(e)})
            continue

        if not diff or not diff.strip():
            # Truly empty: model produced no edits.
            log.info('[%d/%d] %s — genuinely empty (model produced no edits)',
                     i, len(empty_files), stem)
            results['genuinely_empty'].append(stem)
            continue

        # Back up the original placeholder and overwrite with real diff.
        backup_path = BACKUP_DIR / pf.name
        try:
            backup_path.write_text(pf.read_text())
        except Exception as e:
            log.warning('[%d/%d] %s — backup write failed: %s',
                        i, len(empty_files), stem, e)

        pf.write_text(diff)
        log.info('[%d/%d] %s — RECOVERED %d chars',
                 i, len(empty_files), stem, len(diff))
        results['recovered'].append({'stem': stem, 'size': len(diff)})

    elapsed = time.time() - t0
    log.info('[Recovery] Done in %.1fs — recovered=%d, empty=%d, missing=%d, failed=%d',
             elapsed,
             len(results['recovered']),
             len(results['genuinely_empty']),
             len(results['missing_workspace']),
             len(results['extraction_failed']))

    manifest = {
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'total_empty_patches': len(empty_files),
        'recovered_count': len(results['recovered']),
        'genuinely_empty_count': len(results['genuinely_empty']),
        'missing_workspace_count': len(results['missing_workspace']),
        'extraction_failed_count': len(results['extraction_failed']),
        'details': results,
    }
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=2))
    log.info('[Recovery] Manifest written: %s', MANIFEST_FILE)

    return 0


if __name__ == '__main__':
    sys.exit(main())
