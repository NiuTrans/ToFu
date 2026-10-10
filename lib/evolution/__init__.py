"""Self-evolution module: goal intake, confirmed plans, episode settlement.

Design authority: ``docs/EVOLUTION_DESIGN.md``. Entry points: ``admit_goal``,
``save_plan``, ``open_episode``, ``settle_episode`` and the get/list readers.
The package never imports routes or opens an LLM/tool loop. The goal-only
application service in runs.py delegates execution to the task engine and
owns the durable stage receipts alongside goal/plan/episode learning records.
"""

from .bridge import (
    derive_actuals,
    open_for_task,
    record_terminal_task,
    settle_from_task,
)
from .checkers import (
    evaluate_goal,
)
from .contracts import (
    EvolutionContractError,
    EvolutionStateError,
    validate_episode,
    validate_goal,
    validate_observation,
    validate_plan,
)
from .cost_model import (
    estimate_cost,
    list_priors,
    recompute_priors,
)
from .crystallize import (
    propose_macro_crystallizations,
)
from .curriculum import (
    frontier_digest,
    propose_probes,
)
from .distill import (
    build_trajectory_dataset,
    propose_distilled_model,
)
from .gate_policy import (
    apply_gate_policy,
    get_thresholds,
    meta_report,
)
from .domains import (
    research_goal_payload,
    verdict_from_research_record,
)
from .episodes import (
    get_episode,
    list_episodes,
    open_episode,
    settle_episode,
)
from .friction import (
    mine_friction,
)
from .gate import (
    activate_mutation,
    collect_capability_gaps,
    confirm_mutation,
    list_capability_gaps,
    list_mutations,
    get_mutation,
    propose_mutation,
    reject_mutation,
    rollback_mutation,
)
from .goals import (
    admit_goal,
    get_goal,
    get_plan,
    list_goals,
    list_plans,
    save_plan,
)
from .holdout import (
    build_holdout_suite,
    latest_regression,
    list_holdout,
    monitor_active_mutations,
    run_regression,
)
from .self_model import (
    author_hypotheses,
    list_hypotheses,
    path_features_for_candidate,
    predict_success,
    propose_hypotheses,
    validate_hypotheses,
)
from .skill_forge import (
    forge_draft,
    forge_policy,
    get_skill,
    install_skill,
    list_forged_skills,
    propose_skill,
    sandbox_run,
)
from .indexers import (
    AcademicIndexer,
    HistoryIndexer,
    ProjectIndexer,
    index_all,
)
from .awareness import (
    system_snapshot,
)
from .maintenance import (
    run_maintenance_cycle,
)
from .observations import (
    cluster_observations,
    list_observations,
    promote_observation,
    propose_frame_changes,
    record_observation,
    set_observation_status,
)
from .planner import (
    build_plan,
    load_active_policy,
    suggest_candidates,
)
from .refetch import (
    REINDEX_TOOL_DEFINITION,
    reindex_for_run,
    reindex_maintenance,
)
from .report import (
    render_goal_report,
)
from .strategies import (
    distill_strategies,
    list_anti_patterns,
    list_strategies,
    retrieve_strategies,
)
from .teach import (
    TEACH_TOOL_DEFINITION,
    teach,
)

__all__ = [
    "AcademicIndexer",
    "EvolutionContractError",
    "EvolutionStateError",
    "HistoryIndexer",
    "ProjectIndexer",
    "REINDEX_TOOL_DEFINITION",
    "TEACH_TOOL_DEFINITION",
    "activate_mutation",
    "admit_goal",
    "apply_gate_policy",
    "author_hypotheses",
    "build_holdout_suite",
    "build_trajectory_dataset",
    "build_plan",
    "collect_capability_gaps",
    "cluster_observations",
    "confirm_mutation",
    "derive_actuals",
    "distill_strategies",
    "estimate_cost",
    "evaluate_goal",
    "forge_draft",
    "forge_policy",
    "frontier_digest",
    "get_episode",
    "get_goal",
    "get_plan",
    "get_skill",
    "get_thresholds",
    "index_all",
    "install_skill",
    "latest_regression",
    "list_anti_patterns",
    "list_capability_gaps",
    "list_episodes",
    "list_forged_skills",
    "list_goals",
    "list_holdout",
    "list_hypotheses",
    "list_mutations",
    "get_mutation",
    "list_observations",
    "list_plans",
    "list_priors",
    "list_strategies",
    "load_active_policy",
    "meta_report",
    "mine_friction",
    "monitor_active_mutations",
    "open_episode",
    "open_for_task",
    "path_features_for_candidate",
    "predict_success",
    "propose_distilled_model",
    "propose_frame_changes",
    "propose_hypotheses",
    "propose_macro_crystallizations",
    "propose_mutation",
    "propose_probes",
    "promote_observation",
    "record_observation",
    "record_terminal_task",
    "recompute_priors",
    "reindex_for_run",
    "reindex_maintenance",
    "reject_mutation",
    "render_goal_report",
    "research_goal_payload",
    "retrieve_strategies",
    "rollback_mutation",
    "run_maintenance_cycle",
    "run_regression",
    "sandbox_run",
    "save_plan",
    "set_observation_status",
    "settle_episode",
    "settle_from_task",
    "suggest_candidates",
    "system_snapshot",
    "teach",
    "validate_episode",
    "validate_goal",
    "validate_hypotheses",
    "validate_observation",
    "validate_plan",
    "verdict_from_research_record",
]
