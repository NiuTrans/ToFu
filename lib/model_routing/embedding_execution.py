"""Owner-scoped, bounded execution service for compatible embeddings."""

from __future__ import annotations

from dataclasses import dataclass

from lib.agent_core.admission import controller
from lib.agent_core.execution_session import (
    ExecutionPhase,
    ExecutionSession,
    acquire_and_bind_admission,
    bind_model_route,
)
from lib.http_client import http_post
from lib.ids import short_id
from lib.log import get_logger
from lib.model_routing import (
    ModelRoutingRepository,
    OPENAI_COMPATIBLE_PROTOCOLS,
    OwnerBoundary,
    dispose_routed_slot_group,
    mint_capability_slot_group,
)


logger = get_logger(__name__)

EMBEDDING_MAX_INPUT_ITEMS = 256
EMBEDDING_MAX_INPUT_CHARACTERS = 1_000_000
EMBEDDING_TIMEOUT_SECONDS = 60


class EmbeddingCapacityError(RuntimeError):
    pass


class EmbeddingUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmbeddingUpstreamError(RuntimeError):
    status_code: int
    body_excerpt: str


def validate_embedding_inputs(inputs: list[str]) -> None:
    if len(inputs) > EMBEDDING_MAX_INPUT_ITEMS:
        raise ValueError(
            f"input supports at most {EMBEDDING_MAX_INPUT_ITEMS} items")
    total_characters = sum(len(value) for value in inputs)
    if total_characters > EMBEDDING_MAX_INPUT_CHARACTERS:
        raise ValueError(
            "embedding input exceeds the total character budget")


def execute_embeddings(
    inputs: list[str],
    *,
    model: str,
    owner_user_id: int,
    tenant_id: str | None,
    preferred_provider_id: str = "",
) -> dict:
    """Run one finite embedding request and settle every acquired resource."""
    validate_embedding_inputs(inputs)
    route_group = None
    route_bound = False
    session = ExecutionSession(
        execution_id=short_id("embedding-", 20),
        kind="embedding",
        owner_user_id=owner_user_id,
        deadline_seconds=EMBEDDING_TIMEOUT_SECONDS,
    )
    try:
        resolved_model, route_group = mint_capability_slot_group(
            ModelRoutingRepository(),
            OwnerBoundary.create(owner_user_id, tenant_id),
            "embedding",
            prefer_model=model,
            preferred_provider_id=preferred_provider_id,
            required_protocols=OPENAI_COMPATIBLE_PROTOCOLS,
            owner_tag=f"compat-embeddings:{owner_user_id}",
        )
        # bind_model_route owns rollback even when registration itself fails.
        route_bound = True
        bind_model_route(
            session,
            lambda: dispose_routed_slot_group(route_group),
        )
        admission_lease = acquire_and_bind_admission(session, controller)
        if admission_lease is None:
            raise EmbeddingCapacityError("embedding execution is at capacity")
        session.mark_dispatch_started()

        from lib.llm_dispatch import get_dispatcher
        from lib.llm_dispatch.provider_pin import provider_pin

        with provider_pin(route_group.pin_id):
            slot = get_dispatcher().pick_and_reserve(
                capability="embedding",
                prefer_model=resolved_model,
                strict_model=True,
            )
            if slot is None:
                raise EmbeddingUnavailableError(
                    "no embedding deployment is currently available")
            url = slot.base_url.rstrip("/") + "/embeddings"
            headers = dict(slot.extra_headers or {})
            if slot.api_key:
                headers["Authorization"] = f"Bearer {slot.api_key}"
            try:
                response = http_post(
                    url,
                    json={"model": slot.model, "input": inputs},
                    headers=headers,
                    timeout=EMBEDDING_TIMEOUT_SECONDS,
                )
            except Exception:
                slot.record_error()
                raise
            if not response.ok:
                slot.record_error(is_rate_limit=response.status_code == 429)
                raise EmbeddingUpstreamError(
                    status_code=int(response.status_code),
                    body_excerpt=str(response.text or "")[:500],
                )
            slot.record_success(latency_ms=0)
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("embedding provider returned a non-object")

        receipt = session.settle(ExecutionPhase.COMPLETED)
        if not receipt.invariants_satisfied:
            raise RuntimeError("embedding resource settlement failed")
        return payload
    except BaseException as exc:
        # A route may fail before its route group exists; the session still
        # produces one terminal receipt. Once bound, its release stack owns it.
        session.settle(ExecutionPhase.FAILED, cause=type(exc).__name__)
        if route_group is not None and not route_bound:
            dispose_routed_slot_group(route_group)
        raise


__all__ = [
    "EMBEDDING_MAX_INPUT_CHARACTERS",
    "EMBEDDING_MAX_INPUT_ITEMS",
    "EMBEDDING_TIMEOUT_SECONDS",
    "EmbeddingCapacityError",
    "EmbeddingUnavailableError",
    "EmbeddingUpstreamError",
    "execute_embeddings",
    "validate_embedding_inputs",
]
