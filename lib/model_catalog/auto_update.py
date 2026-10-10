"""Scheduled one-pass model intelligence update: registry, prices, scores.

The Models page facts rot one vendor announcement behind reality because each
enrichment today refreshes on a different trigger: the world catalogue syncs
only at startup, OpenRouter pricing fills only on an explicit POST, and
Artificial Analysis scores only when a browser read finds a stale cache.
This module is the single automatic pass the durable scheduler runs daily:

1. **Templates + registry** — refresh the allowlisted provider-owned public
   catalogs, then re-run world catalogue sync so template changes land without
   a restart. A failed feed keeps the authored/last-good template projection.
2. **Prices** — force-fetch the public OpenRouter catalogue once and CAS-write
   policy-approved ``list_pricing`` fills, exactly as the manual
   ``POST /api/v1/model-intelligence/openrouter-pricing/refresh`` route does:
   officially priced/covered Models are never rewritten and all-zero rows
   never become a $0 list price.
3. **Registration** — bounded official-source research fills complete draft
   proposals through the same repository admission boundary.
4. **Scores** — force-refresh the Artificial Analysis dataset once when the
   owner has an effective key (settings secret → legacy config → env), so the
   24-hour cache the browser reads is always warm.  Without a key the phase
   reports ``no_key`` and costs no network call.

Safety rails
------------
* **Fail-closed per phase.** One failing source is recorded in the summary
  and never masks the other phases; only principal/authority errors abort
  the run.
* **Bounded.** Both datasets carry their own row/page/time budgets; the
  summary is counts-only (plus a truncated applied-key sample) so the
  scheduler history row stays small.  A deadline guards the whole pass.
* **Owner-explicit.** Runs under the least-privilege system principal the
  scheduler mints for the task row owner; no ambient user state.
"""

from __future__ import annotations

import time

from lib.identity import PrincipalContext
from lib.log import audit_log, get_logger
from lib.model_routing import ModelRoutingRepository, OwnerBoundary

from .aa import refresh_scores
from .aa_key import resolve_effective_key
from .openrouter import fill_document_list_pricing, refresh_pricing_block

logger = get_logger(__name__)

__all__ = [
    "MODEL_INTELLIGENCE_UPDATE_MAX_RUNTIME",
    "run_model_intelligence_update",
]

MODEL_INTELLIGENCE_UPDATE_MAX_RUNTIME = 300
_MAX_APPLIED_KEYS_IN_SUMMARY = 32


def _pricing_phase(repository, boundary) -> dict:
    models = [dict(row) for row in repository.get(boundary).document["models"]]
    block = refresh_pricing_block(models)
    applied: list[str] = []
    if block.get("status") == "ok" and block.get("fillable"):
        holder: dict[str, list[str]] = {"applied": []}

        def mutation(working: dict) -> dict:
            holder["applied"] = fill_document_list_pricing(
                working, block["fillable"])
            return working

        repository.mutate(boundary, mutation)
        applied = holder["applied"]
    report = {
        "status": block.get("status"),
        "applied": len(applied),
        "applied_sample": applied[:_MAX_APPLIED_KEYS_IN_SUMMARY],
    }
    summary = block.get("summary")
    if isinstance(summary, dict):
        report.update(summary)
    return report


def _scores_phase(repository, boundary) -> dict:
    api_key, key_source, key_hint = resolve_effective_key(repository, boundary)
    if not api_key:
        return {"status": "no_key", "scores": 0}
    models = [dict(row) for row in repository.get(boundary).document["models"]]
    block = refresh_scores(
        models,
        api_key=api_key,
        key_source=key_source,
        key_hint=key_hint,
    )
    return {
        "status": block.get("status"),
        "key_source": key_source,
        "scores": len(block.get("scores") or {}),
    }


def run_model_intelligence_update(
    *,
    principal: PrincipalContext,
    max_runtime: int = MODEL_INTELLIGENCE_UPDATE_MAX_RUNTIME,
    repository: ModelRoutingRepository | None = None,
) -> dict:
    """Run one registry + pricing + scores update pass for the task owner."""
    if not isinstance(principal, PrincipalContext) or "providers" not in principal.scopes:
        raise PermissionError("model intelligence update requires providers scope")
    boundary = OwnerBoundary.create(
        principal.require_owner(context="model intelligence update"))
    repository = repository or ModelRoutingRepository()
    deadline = time.monotonic() + min(
        MODEL_INTELLIGENCE_UPDATE_MAX_RUNTIME, max(1, max_runtime))

    from lib.model_routing.world_catalog import sync_world_model_catalog

    summary: dict = {"ok": True}
    from lib.provider_template_feed import refresh_now
    try:
        summary["templates"] = refresh_now(max_runtime=max(1, deadline - time.monotonic()))
    except Exception as exc:
        logger.warning("[ModelIntelligence] template refresh failed (%s)", type(exc).__name__)
        summary["templates"] = {"ok": False, "status": "unavailable"}
    try:
        catalog = sync_world_model_catalog(boundary=boundary, repository=repository)
        summary["catalog"] = {
            "added": catalog.get("added", 0),
            "creators_added": catalog.get("creators_added", 0),
            "pruned": catalog.get("pruned", 0),
        }
    except Exception as exc:
        logger.warning("[ModelIntelligence] auto-update catalogue sync failed: %s", exc)
        summary["catalog"] = {"status": "error", "error": str(exc)[:200]}

    from .registration_research import run_registration_research
    def registration_phase(repository, boundary):
        return run_registration_research(repository, boundary, deadline=deadline)

    for name, phase in (("pricing", _pricing_phase), ("registration", registration_phase), ("scores", _scores_phase)):
        if time.monotonic() >= deadline:
            summary[name] = {"status": "skipped", "reason": "deadline"}
            continue
        try:
            summary[name] = phase(repository, boundary)
        except Exception as exc:
            logger.warning("[ModelIntelligence] auto-update %s failed: %s", name, exc)
            summary[name] = {"status": "error", "error": str(exc)[:200]}

    audit_log(
        "model_intelligence_auto_update",
        catalog=summary.get("catalog"),
        pricing_status=(summary.get("pricing") or {}).get("status"),
        pricing_applied=(summary.get("pricing") or {}).get("applied"),
        scores_status=(summary.get("scores") or {}).get("status"),
    )
    return summary
