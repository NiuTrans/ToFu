"""Durable swarm-session status vocabulary shared with the Storage Sidecar.

This module is dependency-free on purpose. The swarm runtime and the Sidecar
operation catalog both import it, while neither becomes an owner of the
other's implementation details.
"""

from __future__ import annotations


SWARM_SESSION_STATUS_RUNNING = "running"
SWARM_SESSION_STATUS_TERMINATED = "terminated"
SWARM_SESSION_STATUS_QUARANTINED_OWNERLESS = "quarantined:ownerless"

# Only ordinary lifecycle code may write these states. Quarantine has a
# dedicated semantic operation so a caller cannot bypass its owner check.
SWARM_SESSION_WRITABLE_STATUSES = frozenset({
    SWARM_SESSION_STATUS_RUNNING,
    SWARM_SESSION_STATUS_TERMINATED,
})

SWARM_SESSION_TERMINAL_STATUSES = frozenset({
    SWARM_SESSION_STATUS_TERMINATED,
    SWARM_SESSION_STATUS_QUARANTINED_OWNERLESS,
})

# ``swarm.agent.checkpoint.mark`` overlays only these compact result fields on
# an agent's last full checkpoint. Values are JSON scalars and strings stay
# within the text bound, so a marker never grows with the transcript.
# contracts/tofudb_ir_v1.json declares the same vocabulary for native TofuDB.
SWARM_CHECKPOINT_MARKER_RESULT_FIELDS = (
    "actuals_complete",
    "checkpoint_phase",
    "completion_tokens",
    "cost_usd",
    "elapsed_seconds",
    "prompt_tokens",
    "rounds_used",
    "total_tokens",
)
SWARM_CHECKPOINT_MARKER_TEXT_CHARACTERS = 64

# Durable per-run agent outcomes. The resumable session/agent rows above are
# keyed by the reusable conversation swarm key and are reclaimed at TTL
# eviction and new-wave replacement; these records are user state keyed by
# (owner, swarm_run_id, agent_id) and outlive both. They are portable generic
# records, so SQL and native TofuDB authorities store them identically.
SWARM_RUN_RESULT_NAMESPACE = "swarm.run_results.v1"
SWARM_RUN_RESULT_FORMAT = "tofu.swarm-run-result/v1"
# ``SubAgentResult`` fields a run record retains: the outcome, the final
# answer and its failure evidence, and usage. Transcripts stay resumable state.
SWARM_RUN_RESULT_FIELDS = (
    "status",
    "final_answer",
    "error_message",
    "contract_error",
    "recovery_reason",
    "elapsed_seconds",
    "rounds_used",
    "tool_calls_made",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cost_usd",
)


__all__ = [
    "SWARM_SESSION_STATUS_RUNNING",
    "SWARM_SESSION_STATUS_TERMINATED",
    "SWARM_SESSION_STATUS_QUARANTINED_OWNERLESS",
    "SWARM_SESSION_WRITABLE_STATUSES",
    "SWARM_SESSION_TERMINAL_STATUSES",
    "SWARM_CHECKPOINT_MARKER_RESULT_FIELDS",
    "SWARM_CHECKPOINT_MARKER_TEXT_CHARACTERS",
    "SWARM_RUN_RESULT_NAMESPACE",
    "SWARM_RUN_RESULT_FORMAT",
    "SWARM_RUN_RESULT_FIELDS",
]
