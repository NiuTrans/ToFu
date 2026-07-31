"""lib/production — Production Substrate (docs/PRODUCTION_PIPELINE_DESIGN.md).

The horizontal layer under every "one sentence → finished product" capability
(video / podcast / long-form report / …): long-job lifecycle, the stage-graph
contract, and crash-resume. Each capability keeps its own thin **recipe** (the
300–600 lines of real business logic) on top.

What lives here, and why each piece earned its place:

  * ``stages`` — the stage-graph contract + checkpointed resumable runner.
    RELOCATED here verbatim from ``lib/motion_video/_stages.py`` (P6 slice 1);
    it was written knowing nothing about video/audio/LLMs precisely so that
    step could be a move, not a rewrite.
  * ``runtime`` — ``ProductionRuntime``: dedup index, create-with-field-shape,
    append+touch, stale sweep, id minting. Extracted because the P7
    measurement (§9) found the per-capability ``runtime.py`` was **67%
    byte-identical after renaming** across THREE samples.
  * ``jobs`` — job manifest + crash-resume rescan. Same story: the two halves
    were the same shape in motion (20 L / 55 L) and longform (16 L / 28 L).

**Deliberately NOT here: the binary ``deliverable`` channel.** The third
recipe (a markdown report) did not need it, so it is a video/podcast
commonality rather than a global one — abstracting it would be exactly the
"wrong shape from too few samples" mistake the design note's risk table warns
about. It stays in the capabilities until a sample actually demands it.

Also still outside: progress double-projection and the artifacts binary
format (same reason — no third-sample evidence yet).

``lib/motion_video/_stages.py`` remains a re-exporting shim so every historical
import keeps working.
"""

from __future__ import annotations

from lib.production.jobs import (
    MANIFEST_NAME,
    read_manifest,
    resume_running_jobs,
    write_manifest,
)
from lib.production.runtime import ProductionRuntime
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
    # stage graph
    'STATE_VERSION',
    'Stage',
    'StageAborted',
    'StageFailed',
    'load_state',
    'run_stages',
    'stage_artifact',
    'stage_is_done',
    # long-job runtime
    'ProductionRuntime',
    # job manifest + crash resume
    'MANIFEST_NAME',
    'read_manifest',
    'resume_running_jobs',
    'write_manifest',
]
