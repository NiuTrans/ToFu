"""Owner-scoped model-routing lifetime for background production jobs.

Production workers outlive the agent turn that created them.  They therefore
cannot borrow that turn's request-scoped provider pin: the owning user's
authorized chat routes must be minted for the job, pinned on every worker
thread that performs an LLM call, and disposed when the production stage graph
settles.  Callers without an explicit owner retain the legacy direct-library
behavior used by scripts and hermetic tests.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True, slots=True)
class OwnerChatRoute:
    """Thread-pin identity plus the model resolved by model-routing v2."""

    pin_id: str = ''
    routed_model: str = ''


@contextmanager
def owner_chat_route(
    owner_user_id: int | None,
    *,
    tenant_id: str | None = None,
    prefer_model: str = '',
    owner_tag: str = 'production',
) -> Iterator[OwnerChatRoute]:
    """Mint and pin one bounded owner-authorized text route group.

    The pin is thread-local.  Production code that fans out LLM work must pass
    ``route.pin_id`` to each worker and enter ``provider_pin`` there as well.
    """

    if owner_user_id is None:
        yield OwnerChatRoute()
        return

    from lib.llm_dispatch.provider_pin import provider_pin
    from lib.model_routing import (
        OPENAI_CHAT_COMPATIBLE_PROTOCOLS,
        ModelRoutingRepository,
        OwnerBoundary,
        dispose_routed_slot_group,
        mint_capability_slot_group,
    )

    route_group = None
    try:
        routed_model, route_group = mint_capability_slot_group(
            ModelRoutingRepository(),
            OwnerBoundary.create(owner_user_id, tenant_id),
            'text',
            prefer_model=prefer_model,
            required_protocols=OPENAI_CHAT_COMPATIBLE_PROTOCOLS,
            owner_tag=owner_tag,
        )
        route = OwnerChatRoute(
            pin_id=route_group.pin_id,
            routed_model=routed_model,
        )
        with provider_pin(route.pin_id):
            yield route
    finally:
        dispose_routed_slot_group(route_group)


__all__ = ['OwnerChatRoute', 'owner_chat_route']
