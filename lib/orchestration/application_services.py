"""Unified application-service composition for orchestration adapters.

Delivery layers receive one late-bound container instead of assembling the
definition, authoring, run, runtime-start and human-gate seams independently.
Providers remain lazy so configuration reloads and focused test replacements
are observed without caching repositories or database handles.
"""

from __future__ import annotations

from dataclasses import dataclass

from lib.orchestration.application_provider_ports import (
    AuthoringServiceProvider,
    DefinitionServiceProvider,
    HumanGateServiceProvider,
    RunServiceProvider,
)
from lib.orchestration.application_result_ports import ResolvedDefinitionPort
from lib.orchestration.application_service_ports import (
    AuthoringServicePort,
    DefinitionServicePort,
    HumanGateServicePort,
    RunServicePort,
    RuntimeMutationServicePort,
    RuntimeStartServicePort,
)
from lib.orchestration.definition_selection_contract import (
    definition_selection_values,
)
from lib.orchestration.runtime_mutation_service import (
    OrchestrationRuntimeMutationService,
)
from lib.orchestration.runtime_ports import OrchestrationRuntimePort
from lib.orchestration.runtime_start_service import (
    OrchestrationRuntimeStartService,
)


@dataclass(frozen=True)
class OrchestrationApplicationServices:
    """Late-bound service port shared by all orchestration delivery adapters."""

    runtime: OrchestrationRuntimePort
    definition_service: DefinitionServiceProvider
    run_service: RunServiceProvider
    authoring_service: AuthoringServiceProvider
    human_gate_service: HumanGateServiceProvider

    def definitions(self) -> DefinitionServicePort:
        return self.definition_service()

    def runs(self) -> RunServicePort:
        return self.run_service()

    def authoring(self) -> AuthoringServicePort:
        return self.authoring_service()

    def human_gates(self) -> HumanGateServicePort:
        return self.human_gate_service()

    def runtime_starts(self) -> RuntimeStartServicePort:
        return OrchestrationRuntimeStartService(
            self.runtime,
            definition_service=self.definitions,
            run_service=self.runs,
        )

    def runtime_mutations(
        self,
        owner_user_id: int,
    ) -> RuntimeMutationServicePort:
        return OrchestrationRuntimeMutationService(
            self.runtime, owner_user_id)

    def resolve_definition(self, body: dict) -> ResolvedDefinitionPort:
        inline, stored_id = definition_selection_values(body)
        return self.definitions().resolve(
            inline=inline,
            stored_id=stored_id,
        )


__all__ = ['OrchestrationApplicationServices']
