"""Stable public API for context compaction.

Implementations remain in cohesive owner modules.  Application code imports
this module when it needs compaction services; experiments that extend the
step registry use the registry types exported here.  Private algorithms and
tunables are intentionally absent from this contract.
"""

from lib.tasks_pkg.compaction._advanced import advanced_compact
from lib.tasks_pkg.compaction._budget import (
    budget_tool_result,
    budget_tool_result_v2,
    clamp_tool_result_text,
    enforce_round_aggregate_budget,
    enforce_round_aggregate_budget_v2,
    mark_empty_result,
)
from lib.tasks_pkg.compaction._layer1 import micro_compact
from lib.tasks_pkg.compaction._layer2._compact import (
    execute_compact_tool,
    force_compact_if_needed,
)
from lib.tasks_pkg.compaction._pipeline import (
    recompose_context_after_compaction,
    run_compaction_pipeline,
)
from lib.tasks_pkg.compaction._reactive import reactive_compact
from lib.tasks_pkg.compaction._steps import (
    STEP_KIND_STRUCTURAL,
    STEP_KIND_TRANSFORM,
    CompactionContext,
    CompactionStep,
    MessageEditor,
    get_step,
    get_step_spec,
    list_steps,
    register_step,
)
from lib.tasks_pkg.compaction._tokens import (
    build_context_policy,
    resolve_model_context_limit,
    resolve_model_context_profile,
)

# Registration is intentional and centralized at the public extension seam.
import lib.tasks_pkg.compaction._builtin_steps  # noqa: F401, E402

__all__ = (
    'STEP_KIND_STRUCTURAL',
    'STEP_KIND_TRANSFORM',
    'CompactionContext',
    'CompactionStep',
    'MessageEditor',
    'advanced_compact',
    'budget_tool_result',
    'budget_tool_result_v2',
    'build_context_policy',
    'clamp_tool_result_text',
    'enforce_round_aggregate_budget',
    'enforce_round_aggregate_budget_v2',
    'execute_compact_tool',
    'force_compact_if_needed',
    'get_step',
    'get_step_spec',
    'list_steps',
    'mark_empty_result',
    'micro_compact',
    'reactive_compact',
    'recompose_context_after_compaction',
    'register_step',
    'resolve_model_context_limit',
    'resolve_model_context_profile',
    'run_compaction_pipeline',
)
