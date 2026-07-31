"""lib/production/jobs.py — job manifest + crash-resume rescan.

The second cluster the P7 measurement found duplicated
(docs/PRODUCTION_PIPELINE_DESIGN.md §9): motion's ``write_job_manifest`` /
``resume_interrupted_jobs`` (20 L / 55 L) and longform's ``_write_manifest`` /
``resume_interrupted_reports`` (16 L / 28 L) are the same shape.

Why a manifest at all: **crash-resume is a correctness contract** (owner
directive) — the stage-graph checkpoint lets a job resume mid-graph, but only
if something re-spawns the job after the process dies. The manifest is that
something: a tiny JSON next to the job's workdir recording the params needed
to re-create the task, plus its lifecycle state. On startup, every workdir
whose manifest still says ``running`` is re-spawned; the stage checkpoint then
skips the work that already finished.

Both halves are best-effort and never raise: a job that cannot be resumed must
not take down startup, and a manifest write that fails must not fail the job.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Iterable, Optional

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['MANIFEST_NAME', 'write_manifest', 'read_manifest',
           'resume_running_jobs']

MANIFEST_NAME = 'job.json'


def write_manifest(workdir: str, task: dict, *, fields: Iterable[str],
                   kind: str, state: str, log_label: str = 'Production') -> bool:
    """Write ``<workdir>/job.json`` with the params needed to re-spawn.

    Args:
        workdir: the job's directory. A falsy workdir is a no-op (returns False).
        task: the live task dict to harvest ``fields`` from.
        fields: task keys to persist. ``None`` values are skipped so a missing
            optional param doesn't overwrite a default with null on resume.
        kind: capability kind, recorded for diagnostics + the resume scanner.
        state: ``'running'`` | ``'done'`` | ``'error'`` — only ``running`` is
            re-spawned.
        log_label: log prefix.

    Returns True when the manifest was written.
    """
    if not workdir:
        return False
    from lib.json_store import write_json_atomic
    payload: dict[str, Any] = {k: task.get(k) for k in fields
                               if task.get(k) is not None}
    payload['kind'] = kind
    payload['state'] = state
    try:
        os.makedirs(workdir, exist_ok=True)
        write_json_atomic(os.path.join(workdir, MANIFEST_NAME), payload)
        return True
    except Exception as e:
        logger.warning('[%s] job manifest write failed (%s): %s', log_label,
                       task.get('task_id') or '?', e)
        return False


def read_manifest(workdir: str) -> Optional[dict]:
    """Read ``<workdir>/job.json``; None when absent or malformed."""
    from lib.json_store import read_json
    m = read_json(os.path.join(workdir, MANIFEST_NAME), default=None)
    return m if isinstance(m, dict) else None


def resume_running_jobs(jobs_dir: str, *, is_live: Callable[[str], bool],
                        respawn: Callable[[str, str, dict], None],
                        log_label: str = 'Production') -> int:
    """Re-spawn every job under ``jobs_dir`` whose manifest says ``running``.

    Args:
        jobs_dir: directory holding one sub-directory per job.
        is_live: ``fn(task_id) -> bool``; True means the task is already in the
            registry, so it must NOT be double-spawned (this makes the scan
            idempotent — safe to call more than once).
        respawn: ``fn(task_id, workdir, manifest) -> None``; the capability
            re-creates its task from the manifest and spawns its worker.
        log_label: log prefix.

    Returns the number of jobs re-spawned. Never raises — a single bad job is
    logged and skipped so the rest still resume.
    """
    if not os.path.isdir(jobs_dir):
        return 0
    resumed = 0
    try:
        names = sorted(os.listdir(jobs_dir))
    except OSError as e:
        logger.warning('[%s] cannot scan jobs dir %s: %s', log_label,
                       jobs_dir, e)
        return 0
    for name in names:
        workdir = os.path.join(jobs_dir, name)
        m = read_manifest(workdir)
        if not m or m.get('state') != 'running':
            continue
        task_id = m.get('task_id') or name
        try:
            if is_live(task_id):
                continue
            respawn(task_id, workdir, m)
            resumed += 1
            logger.info('[%s] resumed interrupted job %s (kind=%s)', log_label,
                        task_id, m.get('kind'))
        except Exception as e:
            logger.warning('[%s] failed to resume job %s: %s', log_label,
                           task_id, e, exc_info=True)
    if resumed:
        logger.info('[%s] resumed %d interrupted job(s) on startup', log_label,
                    resumed)
    return resumed
