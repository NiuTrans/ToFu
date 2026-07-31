"""lib/motion_video/_stages.py — Compatibility shim.

The stage-graph contract moved to :mod:`lib.production.stages` in the P6
strangler step (docs/PRODUCTION_PIPELINE_DESIGN.md): it is the horizontal
substrate every production recipe shares, not a motion-video detail. The move
was a verbatim relocation — no behaviour changed.

This shim preserves the historical import path so existing call sites keep
working unchanged. Prefer the new home in new code::

    from lib.production.stages import Stage, run_stages
    # or via the facade:
    from lib.production import Stage, run_stages
"""

from __future__ import annotations

from lib.production.stages import (
    STATE_VERSION,
    Stage,
    StageAborted,
    StageFailed,
    load_state,
    run_stages,
    stage_artifact,
    stage_is_done,
)

__all__ = [
    'STATE_VERSION',
    'Stage',
    'StageAborted',
    'StageFailed',
    'load_state',
    'run_stages',
    'stage_artifact',
    'stage_is_done',
]
