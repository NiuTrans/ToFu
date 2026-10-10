"""HTTP request adapters for the sole model-routing v2 authority."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from lib.model_routing import (
    ModelRoutingError,
    ModelRoutingRepository,
    NativeModelSelection,
    OwnerBoundary,
    RoutePolicy,
    parse_native_model_selection,
    resolve_compatible_model,
)

if TYPE_CHECKING:
    from lib.model_routing import RoutedSlotGroup


def _policy(body: Mapping[str, Any], *, protocol: str = "") -> RoutePolicy:
    routing = body.get("routing")
    routing = routing if isinstance(routing, Mapping) else {}
    config = body.get("config")
    config = config if isinstance(config, Mapping) else {}
    required = {"text"}
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            content = message.get("content") if isinstance(message, Mapping) else None
            blocks = content if isinstance(content, list) else []
            if any(
                isinstance(block, Mapping)
                and str(block.get("type") or "") in {"image", "image_url"}
                for block in blocks
            ):
                required.add("vision")
                break
    if body.get("thinking_depth") or body.get("thinkingDepth") or body.get("reasoning_effort"):
        required.add("thinking")
    context = routing.get("required_context") or config.get("requiredContext") or 1
    try:
        context = max(1, int(context))
    except (TypeError, ValueError):
        raise ModelRoutingError(
            "routing.required_context must be a positive integer",
            field="routing.required_context",
        )
    price_budget = routing.get("price_budget") or {}
    if not isinstance(price_budget, Mapping):
        raise ModelRoutingError(
            "routing.price_budget must be an object",
            field="routing.price_budget",
        )

    def price(name: str) -> float | None:
        value = price_budget.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ModelRoutingError(
                f"routing.price_budget.{name} must be non-negative",
                field=f"routing.price_budget.{name}",
            )
        return float(value)

    return RoutePolicy(
        required_capabilities=frozenset(required),
        required_context=context,
        required_protocols=frozenset({protocol}) if protocol else frozenset(),
        max_input_price=price("max_input"),
        max_output_price=price("max_output"),
        price_currency=str(price_budget.get("currency") or "USD"),
        cache_affinity_connection_id=str(
            routing.get("cache_affinity_connection_id") or ""),
    )


def _logical_model(
    selection: NativeModelSelection, group: "RoutedSlotGroup",
) -> str:
    if selection.model is not None:
        return selection.model.model_id
    offering = group.candidates[0].offering
    pending_model_id = str(offering.get("pending_model_id") or "")
    if pending_model_id:
        return pending_model_id
    official_model = offering.get("model")
    if isinstance(official_model, Mapping):
        return str(official_model.get("model_id") or "")
    raise ModelRoutingError(
        "selected offering has no model identity",
        kind="model_route_integrity_error",
        field="model",
    )


def mint_native_request_route(
    body: Mapping[str, Any],
    *,
    owner_user_id: int,
    tenant_id: str | None,
    owner_tag: str,
) -> tuple[str, NativeModelSelection, RoutedSlotGroup]:
    from lib.model_routing import mint_routed_slot_group

    selection = parse_native_model_selection(body)
    group = mint_routed_slot_group(
        ModelRoutingRepository(),
        OwnerBoundary.create(owner_user_id, tenant_id),
        selection,
        policy=_policy(body),
        owner_tag=owner_tag,
    )
    return _logical_model(selection, group), selection, group


def _compat_extensions(body: Mapping[str, Any]) -> tuple[str, str]:
    tofu = body.get("tofu")
    tofu = tofu if isinstance(tofu, Mapping) else {}
    metadata = body.get("metadata")
    if isinstance(metadata, Mapping) and isinstance(metadata.get("tofu"), Mapping):
        tofu = {**metadata["tofu"], **tofu}
    creator_id = str(
        tofu.get("creator_id") or body.get("tofu_creator_id") or "").strip()
    provider_id = str(
        tofu.get("preferred_provider_id")
        or body.get("tofu_preferred_provider_id") or "").strip()
    return creator_id, provider_id


def mint_compatible_request_route(
    body: Mapping[str, Any],
    *,
    model_id: str,
    owner_user_id: int,
    tenant_id: str | None,
    owner_tag: str,
    protocol: str,
) -> tuple[str, NativeModelSelection, RoutedSlotGroup]:
    from lib.model_routing import mint_routed_slot_group

    repository = ModelRoutingRepository()
    boundary = OwnerBoundary.create(owner_user_id, tenant_id)
    creator_id, preferred_provider_id = _compat_extensions(body)
    selection = resolve_compatible_model(
        repository.get(boundary).document,
        model_id,
        creator_id=creator_id,
        preferred_provider_id=preferred_provider_id,
    )
    group = mint_routed_slot_group(
        repository,
        boundary,
        selection,
        policy=_policy(body, protocol=protocol),
        owner_tag=owner_tag,
    )
    return _logical_model(selection, group), selection, group


def routing_error_fields(exc: ModelRoutingError) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "error_kind": exc.kind,
    }
    if exc.field:
        fields["field"] = exc.field
    if exc.candidates:
        fields["candidates"] = exc.candidates
    return fields


def dispose_routed_slot_group(group: "RoutedSlotGroup | None") -> bool:
    """Dispose a request route without loading dispatch during app import."""
    from lib.model_routing import dispose_routed_slot_group as dispose

    return dispose(group)


__all__ = [
    "dispose_routed_slot_group",
    "mint_compatible_request_route",
    "mint_native_request_route",
    "routing_error_fields",
]
