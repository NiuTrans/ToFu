"""Lazy provider aliases for orchestration application-service ports."""

from __future__ import annotations

from collections.abc import Callable

from lib.orchestration.application_result_ports import ResolvedDefinitionPort
from lib.orchestration.application_service_ports import (
    AuthoringServicePort,
    DefinitionServicePort,
    HumanGateServicePort,
    RunServicePort,
    RuntimeMutationServicePort,
    RuntimeStartServicePort,
)


AuthoringServiceProvider = Callable[[], AuthoringServicePort]
DefinitionResolver = Callable[[dict], ResolvedDefinitionPort]
DefinitionServiceProvider = Callable[[], DefinitionServicePort]
RunServiceProvider = Callable[[], RunServicePort]
RuntimeStartServiceProvider = Callable[[], RuntimeStartServicePort]
RuntimeMutationServiceProvider = Callable[[], RuntimeMutationServicePort]
HumanGateServiceProvider = Callable[[], HumanGateServicePort]


__all__ = [
    'AuthoringServiceProvider',
    'DefinitionResolver',
    'DefinitionServiceProvider',
    'RunServiceProvider',
    'RuntimeStartServiceProvider',
    'RuntimeMutationServiceProvider',
    'HumanGateServiceProvider',
]
