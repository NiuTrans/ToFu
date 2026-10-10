"""Single workflow authority: approval, CAS claims, external IDs and recovery."""
from __future__ import annotations
import json
import time
import logging
_LOG = logging.getLogger("tofu.environments")
from .contracts import Actor, EnvironmentError, canonical, seal, validate_spec

_INFLIGHT = {"creating_image", "starting_build", "creating_workspace", "activating_workspace"}
class EnvironmentService:
    def __init__(self, repository, resolve_provider):
        self.repository, self.resolve_provider = repository, resolve_provider
    def catalog(self, actor: Actor, provider_id: str, project: str, query: str = "") -> dict:
        if not isinstance(project, str) or not project or len(project) > 128 or not isinstance(query, str) or len(query) > 256:
            raise EnvironmentError("invalid_catalog_query", "Bounded project and search query required")
        provider = self.resolve_provider(actor, provider_id)
        return json.loads(canonical(provider.catalog(actor, project, query)))
    def plan(self, actor: Actor, provider_id: str, spec: dict, request_id: str) -> dict:
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise EnvironmentError("invalid_request", "A bounded request identifier is required")
        if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 64:
            raise EnvironmentError("invalid_provider", "A bounded provider identifier is required")
        clean = validate_spec(spec)
        provider = self.resolve_provider(actor, provider_id)
        evidence = provider.validate(actor, clean)
        approved = {"provider": provider_id, "spec": clean, "evidence": evidence,
                    "project_root": actor.project_root, "scope": actor.scope}
        digest = seal(approved)
        operation_id = seal([actor.scope, request_id])[:32]
        old = self.repository.get(actor, operation_id)
        if old:
            if old["value"]["plan_hash"] != digest:
                raise EnvironmentError("request_conflict", "Request ID already names a different plan")
            return self._view(old["value"])
        record = {"operation_id": operation_id, "plan_hash": digest, "plan": approved,
                  "phase": "planned", "image_id": "", "workspace_id": "", "error": ""}
        try:
            self.repository.put(actor, operation_id, record, 0)
        except EnvironmentError as exc:
            if exc.code != "conflict": raise
            old = self.repository.get(actor, operation_id)
            if old is None or old["value"]["plan_hash"] != digest: raise
            record = old["value"]
        return self._view(record)
    def _load(self, actor, operation_id):
        row = self.repository.get(actor, operation_id)
        if not row:
            raise EnvironmentError("not_found", "No operation in this owner scope")
        record = json.loads(canonical(row["value"]))
        if record["plan"]["scope"] != actor.scope or record["plan"]["project_root"] != actor.project_root:
            raise EnvironmentError("scope_changed", "Operation belongs to a different project scope")
        if seal(record["plan"]) != record["plan_hash"]:
            raise EnvironmentError("plan_corrupt", "Immutable plan seal does not match")
        return row["version"], record, self.resolve_provider(actor, record["plan"]["provider"])
    def apply(self, actor, operation_id, plan_hash, authorize, *, approved_plan=None) -> dict:
        version, record, provider = self._load(actor, operation_id)
        if approved_plan is not None and seal(approved_plan) != record["plan_hash"]:
            raise EnvironmentError("approval_scope_changed", "Presented approval plan does not match immutable scope")
        if record["plan_hash"] != plan_hash or not authorize(actor, record["plan_hash"]):
            raise EnvironmentError("approval_required", "Exact plan authorization is required")
        phase = record["phase"]
        if phase in _INFLIGHT or phase == "uncertain":
            return self._view(record)
        actions = {"planned": ("creating_image", "image_created"),
                   "image_created": ("starting_build", "building"),
                   "image_ready": ("creating_workspace", "workspace_created"),
                   "workspace_created": ("activating_workspace", "activating")}
        if phase not in actions:
            return self._view(record)
        inflight, done = actions[phase]
        spec = record["plan"]["spec"]
        # Revalidate resource/credential binding before side effects. Any changed
        # material evidence requires a fresh plan and approval.
        if provider.validate(actor, spec) != record["plan"]["evidence"]:
            raise EnvironmentError("provider_changed", "Provider admission changed; create a new plan")
        record["phase"] = inflight
        record["claim_until"] = time.time() + max(1, min(float(getattr(provider, "operation_timeout", 600)), 3600)) + 10
        claimed = self.repository.put(actor, operation_id, record, version)
        _LOG.info("environment stage claimed operation=%s owner_scope=%s provider=%s phase=%s", operation_id, actor.scope, record["plan"]["provider"], inflight)
        record = json.loads(canonical(record))
        try:
            if phase == "planned":
                record["image_id"] = provider.create_image(actor, spec, operation_id)
            elif phase == "image_created":
                provider.start_build(actor, spec, record["image_id"], operation_id, admission=record["plan"]["evidence"])
            elif phase == "image_ready":
                record["workspace_id"] = provider.create_workspace(actor, spec, record["image_id"], operation_id)
            elif phase == "workspace_created":
                if not provider.activate_workspace(actor, spec, record["workspace_id"], operation_id):
                    done = "workspace_created"
            if phase in {"planned", "image_ready"} and not isinstance(record["image_id"] if phase == "planned" else record["workspace_id"], str):
                raise EnvironmentError("provider_protocol", "External identifier must be a string", uncertain=True)
            if phase in {"planned", "image_ready"} and not (record["image_id"] if phase == "planned" else record["workspace_id"]):
                raise EnvironmentError("provider_protocol", "External identifier is missing", uncertain=True)
            for name in ("image_id", "workspace_id"):
                if len(record[name]) > 512:
                    raise EnvironmentError("provider_protocol", "External identifier exceeds bound", uncertain=True)
            record["phase"] = done
        except Exception as exc:
            record["phase"] = "failed" if isinstance(exc, EnvironmentError) and not exc.uncertain else "uncertain"
            record["uncertain_phase"] = inflight
            record["error"] = getattr(exc, "code", "external_outcome_unknown")
        # If this checkpoint fails, the durable inflight claim remains. Another
        # process never retries it blindly; status/reconcile is the only exit.
        self.repository.put(actor, operation_id, record, claimed["version"])
        _LOG.info("environment stage settled operation=%s phase=%s error_code=%s", operation_id, record["phase"], record["error"])
        return self._view(record)
    def status(self, actor, operation_id) -> dict:
        version, record, provider = self._load(actor, operation_id)
        original = json.loads(canonical(record))
        spec = record["plan"]["spec"]
        phase = record["phase"]
        if phase in _INFLIGHT and time.time() < record.get("claim_until", 0):
            return self._view(record)
        if phase in _INFLIGHT or phase == "uncertain":
            reconciled = provider.reconcile(actor, json.loads(canonical(record)))
            if reconciled is not None:
                if set(reconciled) - {"phase", "image_id", "workspace_id"}:
                    raise EnvironmentError("provider_protocol", "Invalid reconciliation evidence")
                allowed = {"creating_image": {"image_created"},
                           "starting_build": {"building", "image_ready"},
                           "creating_workspace": {"workspace_created", "activating"},
                           "activating_workspace": {"workspace_created", "activating"}}
                origin = record.get("uncertain_phase", phase)
                if reconciled.get("phase") not in allowed.get(origin, set()):
                    raise EnvironmentError("provider_protocol", "Invalid reconciliation phase")
                for name in ("image_id", "workspace_id"):
                    if name in reconciled and (not isinstance(reconciled[name], str) or not reconciled[name] or len(reconciled[name]) > 512):
                        raise EnvironmentError("provider_protocol", "Invalid reconciled resource identifier")
                record.update(reconciled)
                record["error"] = ""
            else:
                record["phase"] = "uncertain"
        elif phase in {"building", "activating"}:
            snapshot = (provider.image_status(actor, spec, record["image_id"], operation_id) if phase == "building"
                        else provider.workspace_status(actor, spec, record["workspace_id"], operation_id))
            if snapshot.state == "ready":
                record["phase"] = "image_ready" if phase == "building" else "ready"
                if phase == "activating": record["connection"] = snapshot.connection
            elif snapshot.state == "failed":
                record["phase"], record["error"] = "failed", "provider_failed"
        if record != original:
            self.repository.put(actor, operation_id, record, version)
            _LOG.info("environment status changed operation=%s phase=%s", operation_id, record["phase"])
        return self._view(record)
    def _view(self, record):
        return {**json.loads(canonical(record)), "durable": self.repository.durable,
                "next_action": "apply" if record["phase"] in {"planned", "image_created", "image_ready", "workspace_created"} else "status"}
