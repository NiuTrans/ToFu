"""Provider API v1, scoped identity, bounded JSON and canonical plan seals."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Any, Callable
import hashlib
import json

class EnvironmentError(RuntimeError):
    def __init__(self, code: str, message: str, *, uncertain: bool = False):
        self.code, self.uncertain = code, uncertain
        super().__init__(message)

@dataclass(frozen=True)
class Actor:
    owner_id: int
    tenant_id: str = ""
    project_root: str = ""
    def __post_init__(self):
        if type(self.owner_id) is not int or self.owner_id <= 0:
            raise EnvironmentError("owner_required", "Authenticated owner is required")
        if len(self.tenant_id) > 128:
            raise EnvironmentError("invalid_scope", "Tenant identity is too long")
    @property
    def scope(self) -> str:
        return hashlib.sha256(canonical([self.tenant_id, self.owner_id])).hexdigest()

@dataclass(frozen=True)
class Snapshot:
    state: str
    connection: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    def __post_init__(self):
        if self.state not in {"waiting", "ready", "failed"}:
            raise EnvironmentError("provider_protocol", "Invalid provider status")
        canonical(self.connection)
        if len(self.message) > 2048:
            raise EnvironmentError("provider_protocol", "Provider message exceeds bound")

class Provider(Protocol):
    """One provider instance belongs to one actor and explicit configuration.

    Mutators must return stable external identifiers; a timeout/lost response
    raises EnvironmentError(uncertain=True), never a replayable generic error.
    poll methods are read-only. Reconciliation must prove the exact operation.
    """
    provider_id: str
    def catalog(self, actor: Actor, project: str, query: str) -> dict: ...
    def validate(self, actor: Actor, spec: dict) -> dict: ...
    def create_image(self, actor: Actor, spec: dict, operation_id: str) -> str: ...
    def start_build(self, actor: Actor, spec: dict, image_id: str, operation_id: str, *, admission: dict) -> None: ...
    def image_status(self, actor: Actor, spec: dict, image_id: str, operation_id: str) -> Snapshot: ...
    def create_workspace(self, actor: Actor, spec: dict, image_id: str, operation_id: str) -> str: ...
    def activate_workspace(self, actor: Actor, spec: dict, workspace_id: str, operation_id: str) -> bool: ...
    def workspace_status(self, actor: Actor, spec: dict, workspace_id: str, operation_id: str) -> Snapshot: ...
    def reconcile(self, actor: Actor, record: dict) -> dict | None: ...

ProviderResolver = Callable[[Actor, str], Provider]

def canonical(value: Any) -> bytes:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as exc:
        raise EnvironmentError("invalid_json", "Finite JSON data is required") from exc
    if len(raw) > 262144:
        raise EnvironmentError("record_too_large", "Environment record exceeds 256 KiB")
    return raw

def seal(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()

def validate_spec(value: dict) -> dict:
    from jsonschema import Draft202012Validator
    schema = json.loads(Path(__file__).with_name("spec.schema.json").read_text())
    try:
        Draft202012Validator(schema).validate(value)
    except Exception as exc:
        raise EnvironmentError("invalid_spec", "Environment specification does not match API v1") from exc
    clean = json.loads(canonical(value))
    if clean["workspace"].get("gpu_count", 0) and not clean["workspace"].get("gpu_type"):
        raise EnvironmentError("invalid_spec", "GPU type is required when requesting GPUs")
    return clean
