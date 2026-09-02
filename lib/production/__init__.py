"""Shared execution contracts for long-running production capabilities.

This package owns checkpointed stage graphs, task/dedup lifecycle, restart
manifests, media-neutral content contracts, and shared research gates. Motion
video, slides, long-form, research, and podcast packages own their recipes,
quality policy, binary artifacts, and user-facing projections.

Architecture and verification routes: ``docs/modules/production.md``.
"""

from __future__ import annotations

from lib.production.jobs import (
    MANIFEST_NAME,
    read_manifest,
    resume_running_jobs,
    write_manifest,
)
from lib.production.contracts import (
    FINDING_SEVERITIES,
    normalise_asset_briefs,
    normalise_findings,
    normalise_narrative_core,
    normalise_source_ids,
)
from lib.production.research import (
    RESEARCH_RESUME_TTL_S,
    current_fact_errors,
    evidence_checkpoint_version,
    format_research_cards,
    gate_research_bundle,
    research_topic,
    summarise_current_signals,
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
    # shared content production contracts
    'FINDING_SEVERITIES',
    'normalise_asset_briefs',
    'normalise_findings',
    'normalise_narrative_core',
    'normalise_source_ids',
    # shared evidence production
    'RESEARCH_RESUME_TTL_S',
    'current_fact_errors',
    'evidence_checkpoint_version',
    'format_research_cards',
    'gate_research_bundle',
    'research_topic',
    'summarise_current_signals',
    # job manifest + crash resume
    'MANIFEST_NAME',
    'read_manifest',
    'resume_running_jobs',
    'write_manifest',
]
