"""Publish a running managed endpoint into model-routing v2.

The process launcher supplies only observed endpoint facts. Registration is
delegated to :mod:`lib.model_routing.local_provider`, the shared owner-aware
CAS service also used by well-known-port discovery. The managed-instance
ledger retains the deterministic Provider ID so teardown removes exactly that
aggregate without reconstructing a legacy provider row.
"""

from __future__ import annotations

from lib.identity import require_user_id
from lib.llm_dispatch.discovery import discover_models
from lib.log import audit_log, get_logger
from lib.model_routing import (
    ModelRoutingError,
    ModelRoutingRepository,
    OwnerBoundary,
    delete_local_provider,
    upsert_local_provider,
)
from lib.proxy import register_no_proxy_url


logger = get_logger(__name__)

__all__ = ["register_instance", "unregister_instance"]


def _provider_id(record: dict) -> str:
    return "managed_%s_%d" % (record["engine"], int(record["port"]))


def _boundary(record: dict) -> OwnerBoundary:
    return OwnerBoundary.create(
        require_user_id(
            record.get("owner_user_id"), context="managed local provider owner"),
        record.get("tenant_id") or "",
    )


def _rebuild_slots() -> None:
    from lib.llm_dispatch.health_local import _rebuild_dispatcher_slots

    _rebuild_dispatcher_slots()


def register_instance(
    record: dict,
    *,
    discover=None,
    rebuild=None,
    repository=None,
) -> dict:
    """Discover served models and upsert one owner-scoped v2 bundle."""

    base_url = record.get("base_url")
    if not base_url:
        return {"ok": False, "error": "实例缺少 base_url"}
    try:
        boundary = _boundary(record)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": "实例缺少有效 owner_user_id: %s" % exc}
    register_no_proxy_url(base_url)
    discover = discover or (
        lambda url: discover_models(
            url,
            "",
            timeout=5,
            return_effective=True,
            quiet_not_found=True,
        )
    )
    try:
        models, effective = discover(base_url)
    except Exception as exc:
        logger.debug(
            "[LocalServe] served-model discovery failed: %s", exc, exc_info=True)
        return {"ok": False, "error": "模型发现失败: %s" % exc}
    if not models:
        return {"ok": False, "error": "服务已就绪但未列出任何模型"}

    provider_id = _provider_id(record)
    authority_repository = repository or ModelRoutingRepository()
    try:
        mutation = upsert_local_provider(
            authority_repository,
            boundary,
            provider_id=provider_id,
            display_name="%s (本机托管)" % (
                record.get("served_name") or record["engine"]),
            base_url=effective,
            models=models,
        )
    except ModelRoutingError as exc:
        logger.error(
            "[LocalServe] model-routing registration failed owner=%s provider=%s: %s",
            boundary.owner_user_id,
            provider_id,
            exc,
            exc_info=True,
        )
        return {"ok": False, "error": "模型路由注册失败: %s" % exc}

    if mutation.changed:
        try:
            (rebuild or _rebuild_slots)()
        except Exception as exc:
            # The durable authority is already correct. A later dispatcher
            # initialization/rebuild reads it; do not tear down a healthy
            # local process because this best-effort cache refresh failed.
            logger.error(
                "[LocalServe] slot rebuild failed: %s", exc, exc_info=True)
    audit_log(
        "local_serve_registered",
        engine=record.get("engine"),
        endpoint=effective,
        n_models=len(models),
        instance=record.get("id"),
        owner_user_id=boundary.owner_user_id,
        model_routing_revision=mutation.authority.revision,
    )
    return {
        "ok": True,
        "provider_id": provider_id,
        "n_models": len(models),
        "effective_url": effective,
        "model_routing_revision": mutation.authority.revision,
        "changed": mutation.changed,
    }


def unregister_instance(
    record: dict,
    *,
    rebuild=None,
    repository=None,
) -> dict:
    """Remove exactly the managed instance's owner-scoped v2 provider."""

    try:
        boundary = _boundary(record)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": "实例缺少有效 owner_user_id: %s" % exc}
    provider_id = str(record.get("provider_id") or _provider_id(record))
    authority_repository = repository or ModelRoutingRepository()
    try:
        mutation = delete_local_provider(
            authority_repository,
            boundary,
            provider_id=provider_id,
        )
    except ModelRoutingError as exc:
        logger.error(
            "[LocalServe] model-routing removal failed owner=%s provider=%s: %s",
            boundary.owner_user_id,
            provider_id,
            exc,
            exc_info=True,
        )
        return {"ok": False, "error": str(exc)}
    if mutation.changed:
        try:
            (rebuild or _rebuild_slots)()
        except Exception as exc:
            logger.error(
                "[LocalServe] slot rebuild failed: %s", exc, exc_info=True)
        audit_log(
            "local_serve_unregistered",
            instance=record.get("id"),
            provider_id=provider_id,
            owner_user_id=boundary.owner_user_id,
            model_routing_revision=mutation.authority.revision,
        )
    return {
        "ok": mutation.changed,
        "provider_id": provider_id,
        "model_routing_revision": mutation.authority.revision,
    }
