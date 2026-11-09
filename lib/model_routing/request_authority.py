"""Carry one run's request-scoped model-routing authority to its descendants.

Responsibility: the storage-free composition (``tofu_agent.AgentRuntime``)
admits each run against an ``InMemoryModelRoutingRepository`` built from the
request envelope. A delegated child keeps only the authorized selection
(``modelRef``) and mints its own route group when it is created
(``lib/tasks_pkg/manager/_registry.py``), so it must mint from the authority
that admitted its parent. Without a carrier it fell back to the
process-default durable ``ModelRoutingRepository``: a storage-free process
runs none ("model-routing authority is temporarily unavailable"), and where
one runs it holds the owner's durable table instead of the run's envelope.

``RequestRoutingAuthority`` is a live reference to that same repository,
never a copy of the table: copy/deepcopy share it, and it stays out of task
config, snapshots and persistence. The composing process executes the root and
every delegated child, so the reference is complete there. The full
application's API and execution hosts carry no request authority; both resolve
through the durable repository, which each reaches through storage. The carrier
binds the owner boundary it admitted and refuses a child of another owner or
tenant.

Entry points: ``bind_request_routing_authority`` (the composition that minted
the root route), ``share_request_routing_authority`` (detached parent carriers
and created children), ``request_routing_repository`` (child creation).
Dependencies: ``domain`` and ``repository`` only.
"""

from __future__ import annotations

from .domain import ModelRoutingError
from .repository import OwnerBoundary, RepositoryPort

KEY = "_requestRoutingAuthority"


class RequestRoutingAuthority:
    """The repository and owner boundary that admitted one run's routes."""

    __slots__ = ("repository", "boundary")

    def __init__(self, repository: RepositoryPort, boundary: OwnerBoundary) -> None:
        self.repository = repository
        self.boundary = boundary

    def __copy__(self) -> "RequestRoutingAuthority":
        return self

    def __deepcopy__(self, memo) -> "RequestRoutingAuthority":
        return self


def bind_request_routing_authority(
    task: dict, repository: RepositoryPort, boundary: OwnerBoundary,
) -> None:
    """Declare the authority that minted ``task``'s own route group."""
    task[KEY] = RequestRoutingAuthority(repository, boundary)


def share_request_routing_authority(parent: dict, carrier: dict) -> None:
    """Let a detached parent carrier or a created child borrow ``parent``'s."""
    authority = parent.get(KEY)
    if authority is not None:
        carrier[KEY] = authority


def request_routing_repository(
    carrier: dict, boundary: OwnerBoundary,
) -> RepositoryPort | None:
    """Return the repository a child of ``carrier`` owned by ``boundary`` mints from.

    ``None`` means the carrier holds no request authority, so the durable owner
    repository stays authoritative.
    """
    authority = carrier.get(KEY)
    if authority is None:
        return None
    if authority.boundary != boundary:
        raise ModelRoutingError(
            "child principal is outside its run's model-routing authority",
            kind="model_route_unavailable",
        )
    return authority.repository


__all__ = [
    "RequestRoutingAuthority",
    "bind_request_routing_authority",
    "request_routing_repository",
    "share_request_routing_authority",
]
