"""Shared execution contracts for long-running production capabilities.

This package owns checkpointed stage graphs, task/dedup lifecycle, restart
manifests, media-neutral content contracts, and shared research gates. Motion
video, slides, long-form, research, and podcast packages own their recipes,
quality policy, binary artifacts, and user-facing projections.

Architecture and verification routes: ``docs/modules/production.md``.
"""

from __future__ import annotations

from importlib import import_module

__all__ = [
    # stage graph
    'STATE_VERSION',
    'Stage',
    'StageAborted',
    'StageFailed',
    'load_state',
    'run_independent_stages',
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
    # shared background model transport policy
    'OPTIONAL_LLM_MAX_429_ATTEMPTS',
    'PRODUCTION_LLM_HARD_ERROR_ATTEMPTS',
    'PRODUCTION_LLM_MAX_429_ATTEMPTS',
    'abort_check_from_event',
    'optional_llm_dispatch_kwargs',
    'optional_llm_max_429_attempts',
    'production_llm_dispatch_kwargs',
    'production_llm_max_429_attempts',
    # shared background image transport policy
    'PRODUCTION_IMAGE_HARD_ERROR_ATTEMPTS',
    'PRODUCTION_IMAGE_MAX_FANOUT',
    'PRODUCTION_IMAGE_MAX_429_ATTEMPTS',
    'production_image_dispatch_kwargs',
    'production_image_fanout',
    'production_image_max_429_attempts',
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


# Capability runtimes import ``lib.production.runtime`` during server boot.
# Keep that narrow dependency from initializing checkpoint, research, and
# restart-manifest owners until a production recipe actually needs them.
_EXPORT_MODULES = {
    # Stage graph.
    'STATE_VERSION': 'lib.production.stages',
    'Stage': 'lib.production.stages',
    'StageAborted': 'lib.production.stages',
    'StageFailed': 'lib.production.stages',
    'load_state': 'lib.production.stages',
    'run_independent_stages': 'lib.production.stages',
    'run_stages': 'lib.production.stages',
    'stage_artifact': 'lib.production.stages',
    'stage_is_done': 'lib.production.stages',
    # Long-job runtime.
    'ProductionRuntime': 'lib.production.runtime',
    # Shared content contracts.
    'FINDING_SEVERITIES': 'lib.production.contracts',
    'normalise_asset_briefs': 'lib.production.contracts',
    'normalise_findings': 'lib.production.contracts',
    'normalise_narrative_core': 'lib.production.contracts',
    'normalise_source_ids': 'lib.production.contracts',
    # Background model transport policy.
    'OPTIONAL_LLM_MAX_429_ATTEMPTS': 'lib.production.llm_policy',
    'PRODUCTION_LLM_HARD_ERROR_ATTEMPTS': 'lib.production.llm_policy',
    'PRODUCTION_LLM_MAX_429_ATTEMPTS': 'lib.production.llm_policy',
    'abort_check_from_event': 'lib.production.llm_policy',
    'optional_llm_dispatch_kwargs': 'lib.production.llm_policy',
    'optional_llm_max_429_attempts': 'lib.production.llm_policy',
    'production_llm_dispatch_kwargs': 'lib.production.llm_policy',
    'production_llm_max_429_attempts': 'lib.production.llm_policy',
    # Background image transport policy.
    'PRODUCTION_IMAGE_HARD_ERROR_ATTEMPTS': 'lib.production.image_policy',
    'PRODUCTION_IMAGE_MAX_FANOUT': 'lib.production.image_policy',
    'PRODUCTION_IMAGE_MAX_429_ATTEMPTS': 'lib.production.image_policy',
    'production_image_dispatch_kwargs': 'lib.production.image_policy',
    'production_image_fanout': 'lib.production.image_policy',
    'production_image_max_429_attempts': 'lib.production.image_policy',
    # Evidence production.
    'RESEARCH_RESUME_TTL_S': 'lib.production.research',
    'current_fact_errors': 'lib.production.research',
    'evidence_checkpoint_version': 'lib.production.research',
    'format_research_cards': 'lib.production.research',
    'gate_research_bundle': 'lib.production.research',
    'research_topic': 'lib.production.research',
    'summarise_current_signals': 'lib.production.research',
    # Crash-resume manifests.
    'MANIFEST_NAME': 'lib.production.jobs',
    'read_manifest': 'lib.production.jobs',
    'resume_running_jobs': 'lib.production.jobs',
    'write_manifest': 'lib.production.jobs',
}

_CHILD_MODULES = {
    'contracts', 'heartbeat', 'image_policy', 'jobs', 'llm_policy', 'research',
    'runtime', 'stages',
}


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None and name in _CHILD_MODULES:
        module_name = f'lib.production.{name}'
    if module_name is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    module = import_module(module_name)
    value = module if name in _CHILD_MODULES else getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__) | _CHILD_MODULES)
