"""Public model-routing v2 facade with request-only dispatch bindings.

Domain, repository, migration, and route-compilation types are safe to load
while the application registers routes.  Slot minting is different: it owns
the legacy dispatcher bridge and therefore pulls in pool state.  Keep those
four exports lazy so importing the HTTP application does not instantiate the
execution transport before a request actually needs it.
"""

from importlib import import_module

from .domain import (
    CONTRACT_VERSION,
    MAX_COUNTS,
    MAX_DOCUMENT_BYTES,
    MAX_ROUTE_SNAPSHOT_BYTES,
    ModelRef,
    ModelRoutingError,
    NativeModelSelection,
    ProviderOfferingRef,
    empty_document,
    normalize_document,
    parse_native_model_selection,
    public_projection,
)
from .repository import (
    InMemoryModelRoutingRepository,
    ModelRoutingRepository,
    OwnerBoundary,
    RepositoryPort,
    StoredAuthority,
)
from .migration import (
    MigrationIssue,
    MigrationPlan,
    MigrationResult,
    execute_migration,
    plan_legacy_migration,
    validate_migration_plan,
)
from .health import HealthTarget, RouteHealthRegistry, classify_failure
from .routing import (
    RouteCandidate,
    RouteCandidateCompiler,
    RoutePolicy,
    RouteSnapshotBuilder,
    compile_candidates,
    compile_model_fallback_candidates,
    legacy_route_snapshot,
    resolve_compatible_model,
)
from .bootstrap import (
    bootstrap_owner_model_routing,
    bootstrap_personal_model_routing,
)
from .local_provider import (
    LocalProviderMutation,
    build_local_provider_bundle,
    connection_urls,
    delete_local_provider,
    upsert_local_provider,
)
from .discovered_provider import (
    build_discovered_provider_bundle,
    discovered_provider_id,
)

__all__ = [
    "CONTRACT_VERSION",
    "MAX_COUNTS",
    "MAX_DOCUMENT_BYTES",
    "MAX_ROUTE_SNAPSHOT_BYTES",
    "MAX_REQUEST_ROUTE_SLOTS",
    "ModelRef",
    "ModelRoutingError",
    "NativeModelSelection",
    "OPENAI_CHAT_COMPATIBLE_PROTOCOLS",
    "OPENAI_COMPATIBLE_PROTOCOLS",
    "InMemoryModelRoutingRepository",
    "LocalProviderMutation",
    "HealthTarget",
    "ModelRoutingRepository",
    "MigrationIssue",
    "MigrationPlan",
    "MigrationResult",
    "OwnerBoundary",
    "ProviderOfferingRef",
    "RepositoryPort",
    "RouteCandidate",
    "RouteCandidateCompiler",
    "RouteHealthRegistry",
    "RoutePolicy",
    "RouteSnapshotBuilder",
    "RoutedSlotGroup",
    "decode_credential_secret",
    "StoredAuthority",
    "empty_document",
    "classify_failure",
    "compile_candidates",
    "bootstrap_owner_model_routing",
    "bootstrap_personal_model_routing",
    "build_local_provider_bundle",
    "compile_model_fallback_candidates",
    "connection_urls",
    "build_discovered_provider_bundle",
    "CapabilityRoute",
    "discovered_provider_id",
    "delete_local_provider",
    "dispose_routed_slot_group",
    "execute_migration",
    "normalize_document",
    "parse_native_model_selection",
    "plan_legacy_migration",
    "public_projection",
    "legacy_route_snapshot",
    "mint_routed_slot_group",
    "list_capability_routes",
    "list_capability_route_groups",
    "mint_capability_slot_group",
    "resolve_compatible_model",
    "upsert_local_provider",
    "validate_migration_plan",
]

_LAZY_EXPORT_MODULES = {
    "MAX_REQUEST_ROUTE_SLOTS": "lib.model_routing.dispatch_adapter",
    "RoutedSlotGroup": "lib.model_routing.dispatch_adapter",
    "decode_credential_secret": "lib.model_routing.dispatch_adapter",
    "dispose_routed_slot_group": "lib.model_routing.dispatch_adapter",
    "mint_routed_slot_group": "lib.model_routing.dispatch_adapter",
    "CapabilityRoute": "lib.model_routing.capability_adapter",
    "OPENAI_CHAT_COMPATIBLE_PROTOCOLS": "lib.model_routing.capability_adapter",
    "OPENAI_COMPATIBLE_PROTOCOLS": "lib.model_routing.capability_adapter",
    "list_capability_routes": "lib.model_routing.capability_adapter",
    "list_capability_route_groups": "lib.model_routing.capability_adapter",
    "mint_capability_slot_group": "lib.model_routing.capability_adapter",
}


def __getattr__(name: str):
    module_name = _LAZY_EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
