"""Composition root for Orchestration Studio HTTP adapters.

Definition persistence, pure authoring and durable Task Mode routes are
registered by focused HTTP adapters alongside ephemeral runtime routes. This
module owns their shared blueprint, late-bound service providers and runtime.

Routes:
  GET    /api/v1/orchestrations                 — list metadata summaries
  GET    /api/v1/orchestrations/{id}            — fetch one
  POST   /api/v1/orchestrations                 — create
  PUT    /api/v1/orchestrations/{id}            — replace definition
  DELETE /api/v1/orchestrations/{id}            — remove
  POST   /api/v1/orchestrations/validate        — validate without saving
  POST   /api/v1/orchestrations/layout          — tidy node positions (pure)
  POST   /api/v1/orchestrations/compose         — LLM author/edit from NL
  GET    /api/v1/orchestrations/authoring-contract — Studio schema/catalogue
  POST   /api/v1/orchestrations/plan            — dry-run execution preview
  POST   /api/v1/orchestrations/run             — execute (background task)
  GET    /api/v1/orchestrations/run/poll/{id}   — poll a run's events
  POST   /api/v1/orchestrations/run/abort/{id}  — abort a run
  POST   /api/v1/orchestrations/run/human-approve — resolve an approval gate
  POST   /api/v1/orchestrations/run/human-input   — resolve an input gate
"""

from __future__ import annotations

from quart import Blueprint

from lib.orchestration.application_services import (
    OrchestrationApplicationServices,
)
from lib.orchestration.authoring_service import OrchestrationAuthoringService
from lib.orchestration.definition_service import (
    OrchestrationDefinitionService,
)
from lib.orchestration.run_service import OrchestrationRunService
from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
from lib.orchestration.human_gate_service import (
    OrchestrationHumanGateService,
)
from lib.agent_core.task_runtime import TaskRuntime

from .orchestration_definition_routes import (
    register_orchestration_definition_routes,
)
from .orchestration_authoring_routes import (
    register_orchestration_authoring_routes,
)
from .orchestration_runtime_routes import register_orchestration_runtime_routes
from .orchestration_task_routes import register_orchestration_task_routes
from .orchestration_mutation_routes import (
    register_orchestration_mutation_routes,
)
from .auth import current_auth

api_v1_orchestrations_bp = Blueprint('api_v1_orchestrations', __name__)

#: Background runtime for flow executions. Events stream to the
#: ``orchestration`` push channel; the frontend polls /run/poll/<id>.
orchestration_run_runtime = TaskRuntime('orchestration-run', ttl=3600,
                                        push_channel='orchestration')


def _definitions() -> OrchestrationDefinitionService:
    ctx = current_auth()
    if ctx is None or ctx.owner_user_id is None:
        raise RuntimeError('orchestration request has no repository owner')
    return OrchestrationDefinitionService.for_owner(
        ctx.owner_user_id, tenant_id=ctx.tenant_id)


def _run_instances() -> OrchestrationRunService:
    """Construct the framework-free durable-run application boundary."""
    ctx = current_auth()
    if ctx is None or ctx.owner_user_id is None:
        raise RuntimeError('orchestration request has no repository owner')
    return OrchestrationRunService(
        SidecarOrchestrationRunStore(
            ctx.owner_user_id, tenant_id=ctx.tenant_id),
        runtime_mutation=_services.runtime_mutations(ctx.owner_user_id))


def _authoring() -> OrchestrationAuthoringService:
    """Construct the stateless authoring application boundary."""
    return OrchestrationAuthoringService()


def _human_gates() -> OrchestrationHumanGateService:
    """Compose the shared chat/orchestration gate-resolution boundary."""
    return OrchestrationHumanGateService()


def _definition_provider() -> OrchestrationDefinitionService:
    """Resolve the current definition provider at request time."""
    return _definitions()


def _run_provider() -> OrchestrationRunService:
    """Resolve the current durable-run provider at request/worker time."""
    return _run_instances()


_services = OrchestrationApplicationServices(
    runtime=orchestration_run_runtime,
    definition_service=_definition_provider,
    run_service=_run_provider,
    authoring_service=_authoring,
    human_gate_service=_human_gates,
)


# ═══════════════════════════════════════════════════════════════════
#  Focused HTTP adapter registration
#
#  Providers stay late-bound so config reloads and route tests use the same
#  service seams as production. See the five orchestration_*_routes.py
#  adapters registered below.
# ═══════════════════════════════════════════════════════════════════

register_orchestration_definition_routes(
    api_v1_orchestrations_bp,
    definition_service=_services.definitions,
)

register_orchestration_authoring_routes(
    api_v1_orchestrations_bp,
    authoring_service=_services.authoring,
    resolve_definition=_services.resolve_definition,
)

register_orchestration_runtime_routes(
    api_v1_orchestrations_bp,
    orchestration_run_runtime,
    resolve_definition=_services.resolve_definition,
    authoring_service=_services.authoring,
    runtime_start_service=_services.runtime_starts,
)

register_orchestration_task_routes(
    api_v1_orchestrations_bp,
    resolve_definition=_services.resolve_definition,
    run_service=_services.runs,
    runtime_start_service=_services.runtime_starts,
)

register_orchestration_mutation_routes(
    api_v1_orchestrations_bp,
    orchestration_run_runtime,
    run_service=_services.runs,
    runtime_mutation_service=_services.runtime_mutations,
    human_gate_service=_services.human_gates,
)


__all__ = ['api_v1_orchestrations_bp', 'orchestration_run_runtime']
