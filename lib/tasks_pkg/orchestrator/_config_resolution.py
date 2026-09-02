"""Section 1 config resolution and first-dispatch model attribution.

Extracted 2026-07-31 from ``lib/tasks_pkg/orchestrator/_run.py``
run_task's Section 1, where the unit ran inline once per invocation.
Byte-identical behaviour.

Two steps:

1. ``mcfg = _resolve_model_config(cfg, task['id'])`` — resolves the
   per-model config (model, thinking, preset, token/temperature,
   feature flags, project scope).
2. Model seed: ``if model:
   task['model'] = model``. The loop tail re-stamps the model after
   each successful round, but a first-call DISPATCH failure
   (revoked-OAuth 401, all keys cooling, endpoint-unreachable
   exhaustion) raises BEFORE any round succeeds — the error row then
   persisted with metadata.model NULL (40 such rows in 14 days),
   invisible to per-model failure stats. The post-round stamp still
   tracks fallback swaps; this seed is the floor.

The 17-field unpack stays inline in run_task as local-variable
binding (owner DONE definition for : local binding is
spine-legitimate); only the resolve + seed branch moved here.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.model_config import _resolve_model_config


logger = get_logger(__name__)


def resolve_and_seed_model_config(cfg, task):
    """Resolve the per-model config and seed task['model'] immediately.

    ``cfg`` / ``task`` are positional carriers. Returns the resolved
    ``mcfg`` dict unchanged — run_task unpacks its fields inline.
    """
    mcfg = _resolve_model_config(cfg, task['id'])
    model = mcfg['model']
    # Seed the resolved model on the task immediately. The loop tail stamps it
    # again after each
    #   successful round, but a first-call DISPATCH failure (revoked-OAuth
    #   401, all keys cooling, endpoint-unreachable exhaustion) raises
    #   BEFORE any round succeeds — the error row then persisted with
    #   metadata.model NULL (40 such rows in 14 days), invisible to
    #   per-model failure stats. The post-round stamp still tracks
    #   fallback swaps; this seed is the floor.
    if model:
        task['model'] = model
    return mcfg
