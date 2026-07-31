"""lib/motion_video/_stages.py — Stage-graph contract for long productions.

The thin shell every "one sentence → finished product" recipe is built from
(docs/PRODUCTION_PIPELINE_DESIGN.md §2.1 ``stages.py``). It deliberately
knows NOTHING about video, audio or LLMs so P6 can MOVE it verbatim into
``lib/production/`` — that step must be a relocation, not a rewrite.

A stage is a 5-tuple:

    Stage(name, run, gate, retry, resumable)

  * ``run(ctx) -> artifact``      the work; artifact must be JSON-serializable
                                 (heavy binaries are referenced BY PATH, never
                                 inlined).
  * ``gate(ctx, artifact) -> []`` zero-LLM validation; a non-empty error list
                                 fails the stage (and triggers ``retry``).
  * ``retry``                     extra attempts on gate failure / exception.
  * ``resumable``                 when True a COMPLETED stage recorded in the
                                 state file is skipped on a later run.

**Crash-resume is a correctness contract, not a cost optimization** (owner
directive, 2026-07-25): each stage's artifact is committed to the state file
as soon as its gate passes, so a process killed mid-graph resumes at the
first unfinished stage and never redoes completed work. The state file is the
checkpoint; there is no in-memory-only progress.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['Stage', 'StageAborted', 'StageFailed', 'run_stages',
           'load_state', 'stage_is_done', 'stage_artifact']

#: Schema version of the state file — bump when the shape changes so a stale
#: checkpoint is ignored rather than mis-read.
STATE_VERSION = 1


class StageAborted(Exception):
    """The job's abort signal fired between (or inside) stages."""


class StageFailed(Exception):
    """A stage exhausted its attempts. Carries the stage name + gate errors."""

    def __init__(self, stage: str, detail: str, errors: Optional[list] = None):
        super().__init__(f'stage {stage!r} failed: {detail}')
        self.stage = stage
        self.detail = detail
        self.errors = list(errors or [])


@dataclass(frozen=True)
class Stage:
    """One step of a production recipe. See the module docstring."""

    name: str
    run: Callable[[dict], Any]
    gate: Optional[Callable[[dict, Any], list]] = None
    retry: int = 0
    resumable: bool = True


# ── State file (the checkpoint) ───────────────────────────

def load_state(state_path: str) -> dict:
    """Read the pipeline state file; a missing/corrupt file reads as empty."""
    from lib.json_store import read_json
    raw = read_json(state_path, default=None)
    if not isinstance(raw, dict) or raw.get('version') != STATE_VERSION:
        if raw is not None:
            logger.info('[Stages] ignoring incompatible state at %s', state_path)
        return {'version': STATE_VERSION, 'stages': {}}
    stages = raw.get('stages')
    if not isinstance(stages, dict):
        stages = {}
    return {'version': STATE_VERSION, 'stages': stages}


def stage_is_done(state: dict, name: str) -> bool:
    entry = (state.get('stages') or {}).get(name)
    return bool(isinstance(entry, dict) and entry.get('ok'))


def stage_artifact(state: dict, name: str) -> Any:
    entry = (state.get('stages') or {}).get(name)
    return entry.get('artifact') if isinstance(entry, dict) else None


def _commit(state_path: str, state: dict, name: str, artifact: Any) -> None:
    """Atomically record ``name`` as done. This IS the checkpoint."""
    from lib.json_store import write_json_atomic
    state.setdefault('stages', {})[name] = {
        'ok': True, 'artifact': artifact, 'at': round(time.time(), 3),
    }
    state['version'] = STATE_VERSION
    write_json_atomic(state_path, state)


# ── Runner ────────────────────────────────────────────────

def run_stages(stages: list, ctx: dict, *, state_path: str,
               emit: Optional[Callable[[dict], None]] = None,
               abort_check: Optional[Callable[[], bool]] = None) -> dict:
    """Run a stage graph with checkpointed resume. Returns ctx['artifacts'].

    Args:
        stages: ordered :class:`Stage` list.
        ctx: mutable job context; ``ctx['artifacts']`` accumulates results and
            is what later stages read.
        state_path: checkpoint file path (JSON, atomically written).
        emit: optional event sink — receives ``stage_started`` /
            ``stage_skipped`` / ``stage_done`` / ``stage_retry`` dicts.
        abort_check: optional zero-arg predicate; True → :class:`StageAborted`.

    Raises:
        StageAborted / StageFailed.
    """
    artifacts = ctx.setdefault('artifacts', {})
    state = load_state(state_path)
    total = len(stages)

    def _emit(event: dict) -> None:
        if emit is None:
            return
        try:
            emit(event)
        except Exception as e:  # an event sink must never break the pipeline
            logger.debug('[Stages] emit failed for %s: %s', event.get('type'), e)

    def _aborted() -> bool:
        return bool(abort_check is not None and abort_check())

    for index, stage in enumerate(stages, 1):
        if _aborted():
            raise StageAborted(f'aborted before stage {stage.name!r}')

        if stage.resumable and stage_is_done(state, stage.name):
            artifacts[stage.name] = stage_artifact(state, stage.name)
            logger.info('[Stages] %s: resumed from checkpoint (skipped)', stage.name)
            _emit({'type': 'stage_skipped', 'stage': stage.name,
                   'index': index, 'total': total, 'resumed': True})
            continue

        attempts = max(1, stage.retry + 1)
        last_detail = ''
        last_errors: list = []
        for attempt in range(1, attempts + 1):
            if _aborted():
                raise StageAborted(f'aborted in stage {stage.name!r}')
            _emit({'type': 'stage_started', 'stage': stage.name,
                   'index': index, 'total': total, 'attempt': attempt})
            started = time.time()
            try:
                artifact = stage.run(ctx)
            except StageAborted:
                raise
            except Exception as e:
                last_detail = f'{type(e).__name__}: {e}'
                last_errors = []
                logger.warning('[Stages] %s attempt %d/%d raised: %s',
                               stage.name, attempt, attempts, e, exc_info=True)
                continue

            errors = list(stage.gate(ctx, artifact) or []) if stage.gate else []
            if errors:
                last_detail = 'gate rejected the artifact'
                last_errors = errors
                logger.warning('[Stages] %s attempt %d/%d gate errors: %s',
                               stage.name, attempt, attempts, '; '.join(
                                   str(x) for x in errors[:4]))
                if attempt < attempts:
                    _emit({'type': 'stage_retry', 'stage': stage.name,
                           'attempt': attempt, 'errors': errors[:4]})
                continue

            artifacts[stage.name] = artifact
            _commit(state_path, state, stage.name, artifact)
            elapsed = round(time.time() - started, 2)
            logger.info('[Stages] %s done in %.2fs (%d/%d)',
                        stage.name, elapsed, index, total)
            _emit({'type': 'stage_done', 'stage': stage.name, 'index': index,
                   'total': total, 'elapsed_s': elapsed})
            break
        else:
            raise StageFailed(stage.name, last_detail or 'unknown failure',
                              last_errors)

    return artifacts
