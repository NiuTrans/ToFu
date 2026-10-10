"""CAS operation repository and declarative host storage manifest."""
from __future__ import annotations
import json
from threading import RLock
from .contracts import Actor, EnvironmentError, canonical

MANIFEST = {"namespace": "tofu.environments", "version": 1,
 "tables": [{"name": "operations", "columns": [
   {"name": "key", "type": "string", "required": True},
   {"name": "value", "type": "json", "required": True}], "primary_key": ["key"]}],
 "operations": [{"name": "get", "kind": "query", "action": "get", "table": "operations"},
                {"name": "put", "kind": "command", "action": "put", "table": "operations"}]}

def key(actor: Actor, operation_id: str) -> str:
    if not operation_id or len(operation_id) > 128:
        raise EnvironmentError("invalid_operation", "Invalid operation identifier")
    return actor.scope + ":" + operation_id

class MemoryRepository:
    """Bounded transient repository for embedding/tests; never promises recovery."""
    durable = False
    def __init__(self, maximum_operations: int = 256):
        self._rows, self._lock = {}, RLock()
        self.maximum_operations = maximum_operations
    def get(self, actor, operation_id):
        with self._lock:
            row = self._rows.get(key(actor, operation_id))
            return json.loads(canonical(row)) if row else None
    def put(self, actor, operation_id, value, expected_version):
        with self._lock:
            identity = key(actor, operation_id)
            old = self._rows.get(identity)
            if (old["version"] if old else 0) != expected_version:
                raise EnvironmentError("conflict", "Operation changed concurrently")
            if not old and len(self._rows) >= self.maximum_operations:
                raise EnvironmentError("capacity", "Transient operation capacity reached")
            result = {"version": expected_version + 1, "value": json.loads(canonical(value))}
            self._rows[identity] = result
            return json.loads(canonical(result))

class HostRepository:
    """Selected Tofu storage only; all keys carry owner and tenant identity."""
    durable = True
    def __init__(self, client_factory):
        self.client_factory = client_factory
    def get(self, actor, operation_id):
        row = self.client_factory().query("plugin.tofu.environments.get", {"key": key(actor, operation_id)}, deadline=15)
        return {"version": row["version"], "value": row["document"]["value"]} if row else None
    def put(self, actor, operation_id, value, expected_version):
        import uuid
        try:
            result = self.client_factory(write=True).command("plugin.tofu.environments.put", {
                "document": {"key": key(actor, operation_id), "value": json.loads(canonical(value))},
                "expected_version": expected_version}, "environment-cas:" + uuid.uuid4().hex, deadline=15)
        except Exception as exc:
            if getattr(exc, "code", "") == "database_conflict":
                raise EnvironmentError("conflict", "Operation changed concurrently") from exc
            raise
        return {"version": result["version"], "value": value}
