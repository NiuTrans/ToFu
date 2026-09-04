"""Owner-aware repository and encrypted-secret boundary for model routing v2."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from lib.identity import require_user_id
from lib.secret_envelope import open_secret, seal_secret, secret_hint

from .domain import (
    MAX_DOCUMENT_BYTES,
    ModelRoutingError,
    empty_document,
    normalize_document,
    public_projection,
)


_SECRET_PURPOSE = "model-routing-credential"
_MAX_SECRET_BYTES = 8192


@dataclass(frozen=True, slots=True)
class OwnerBoundary:
    owner_user_id: int
    tenant_id: str = ""

    @classmethod
    def create(cls, owner_user_id: int, tenant_id: str | None = None) -> "OwnerBoundary":
        return cls(
            require_user_id(owner_user_id, context="model-routing owner"),
            str(tenant_id or "").strip(),
        )

    def payload(self) -> dict[str, Any]:
        return {
            "owner_user_id": self.owner_user_id,
            "tenant_id": self.tenant_id,
        }


@dataclass(frozen=True, slots=True)
class StoredAuthority:
    boundary: OwnerBoundary
    revision: int
    document: dict[str, Any]
    updated_at: float

    def public_document(self) -> dict[str, Any]:
        return public_projection(self.document)


class RepositoryPort(Protocol):
    def get(self, boundary: OwnerBoundary) -> StoredAuthority: ...

    def compare_and_swap(
        self,
        boundary: OwnerBoundary,
        document: Mapping[str, Any],
        *,
        expected_revision: int,
        migration_receipt: Mapping[str, Any] | None = None,
    ) -> StoredAuthority: ...

    def put_secret(
        self,
        boundary: OwnerBoundary,
        plaintext: str,
        *,
        secret_reference: str | None = None,
    ) -> dict[str, str]: ...

    def resolve_secret(self, boundary: OwnerBoundary, secret_reference: str) -> str: ...

    def delete_secret(self, boundary: OwnerBoundary, secret_reference: str) -> bool: ...

    def secret_metadata(self, boundary: OwnerBoundary) -> list[dict[str, Any]]: ...

    def record_migration_receipt(
        self, boundary: OwnerBoundary, receipt: Mapping[str, Any],
    ) -> dict[str, Any]: ...


def _document_size(document: Mapping[str, Any]) -> int:
    return len(json.dumps(
        document, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8"))


def _validated_secret(plaintext: str) -> str:
    if not isinstance(plaintext, str):
        raise ModelRoutingError(
            "credential secret must be a string", kind="credential_secret_invalid")
    normalized = plaintext.strip()
    if len(normalized.encode("utf-8")) > _MAX_SECRET_BYTES:
        raise ModelRoutingError(
            f"credential secret exceeds {_MAX_SECRET_BYTES} bytes",
            kind="credential_secret_invalid",
        )
    return normalized


class ModelRoutingRepository:
    """Sidecar-backed revisioned aggregate repository.

    The aggregate commit and credential-secret write intentionally use
    separate semantic operations.  A failed CAS can leave only an encrypted,
    unreachable secret, which the bounded orphan-prune operation later
    reclaims; plaintext never enters the aggregate or its migration backup.
    """

    def _client(self, *, write: bool = False):
        from lib.storage.service import get_storage_client

        return get_storage_client(write=write)

    def get(self, boundary: OwnerBoundary) -> StoredAuthority:
        try:
            row = self._client().query("model_routing.get", boundary.payload())
        except Exception as exc:
            # Storage is an adapter detail of this repository.  Callers in
            # reusable routing/dispatch code must depend on the domain error
            # taxonomy, not on the concrete sidecar implementation.
            from lib.storage.errors import StorageError

            if not isinstance(exc, StorageError):
                raise
            raise ModelRoutingError(
                "model-routing authority is temporarily unavailable",
                kind="model_routing_storage_unavailable",
            ) from exc
        if row is None:
            return StoredAuthority(boundary, 0, empty_document(), 0.0)
        document = normalize_document(row["document"])
        revision = int(row["revision"])
        if document["revision"] != revision:
            raise ModelRoutingError(
                "stored aggregate revision does not match its repository row",
                kind="model_routing_storage_integrity",
            )
        return StoredAuthority(
            boundary=boundary,
            revision=revision,
            document=document,
            updated_at=float(row.get("updated_at") or 0.0),
        )

    def compare_and_swap(
        self,
        boundary: OwnerBoundary,
        document: Mapping[str, Any],
        *,
        expected_revision: int,
        migration_receipt: Mapping[str, Any] | None = None,
    ) -> StoredAuthority:
        if isinstance(expected_revision, bool) or expected_revision < 0:
            raise ModelRoutingError("expected_revision must be non-negative")
        normalized = normalize_document(document, revision=expected_revision + 1)
        if _document_size(normalized) > MAX_DOCUMENT_BYTES:
            raise ModelRoutingError(
                f"model-routing aggregate exceeds {MAX_DOCUMENT_BYTES} bytes",
                kind="model_routing_resource_budget_exceeded",
            )
        command_id = (
            f"model_routing.commit:{boundary.tenant_id}:"
            f"{boundary.owner_user_id}:{expected_revision}:{secrets.token_hex(8)}"
        )
        payload: dict[str, Any] = {
            **boundary.payload(),
            "expected_revision": expected_revision,
            "document": normalized,
            "updated_at": time.time(),
        }
        if migration_receipt is not None:
            payload["migration_receipt"] = copy.deepcopy(dict(migration_receipt))
        row = self._client(write=True).command(
            "model_routing.commit", payload, command_id)
        if row is None:
            raise ModelRoutingError(
                "model-routing commit returned no authority",
                kind="model_routing_storage_integrity",
            )
        committed_revision = int(row.get("revision") or 0)
        expected_committed_revision = expected_revision + 1
        if committed_revision != expected_committed_revision:
            raise ModelRoutingError(
                "model-routing commit acknowledgement revision is invalid",
                kind="model_routing_storage_integrity",
            )
        return StoredAuthority(
            boundary=boundary,
            revision=committed_revision,
            # The mutation operation deliberately returns a receipt-small
            # acknowledgement.  ``normalized`` is the exact payload protected
            # by the command digest and revision CAS.
            document=copy.deepcopy(normalized),
            updated_at=float(row.get("updated_at") or 0.0),
        )

    def mutate(
        self,
        boundary: OwnerBoundary,
        mutation: Callable[[dict[str, Any]], Mapping[str, Any] | None],
        *,
        max_conflicts: int = 2,
    ) -> StoredAuthority:
        """Apply one aggregate mutation with a small, explicit CAS retry cap."""
        last_error: Exception | None = None
        for _attempt in range(max_conflicts + 1):
            authority = self.get(boundary)
            working = copy.deepcopy(authority.document)
            replacement = mutation(working)
            candidate = working if replacement is None else replacement
            try:
                return self.compare_and_swap(
                    boundary, candidate, expected_revision=authority.revision)
            except Exception as exc:
                kind = str(getattr(exc, "kind", "") or getattr(exc, "code", ""))
                if "conflict" not in kind.lower() and "revision" not in str(exc).lower():
                    raise
                last_error = exc
        raise ModelRoutingError(
            "model-routing aggregate changed concurrently",
            kind="model_routing_revision_conflict",
        ) from last_error

    def put_secret(
        self,
        boundary: OwnerBoundary,
        plaintext: str,
        *,
        secret_reference: str | None = None,
    ) -> dict[str, str]:
        value = _validated_secret(plaintext)
        reference = str(secret_reference or f"mrs_{secrets.token_hex(16)}").strip()
        if not reference or len(reference) > 256:
            raise ModelRoutingError(
                "secret_reference must be 1..256 characters",
                kind="credential_secret_invalid",
            )
        ciphertext = seal_secret(
            value,
            purpose=_SECRET_PURPOSE,
            owner_user_id=boundary.owner_user_id,
            record_id=reference,
        )
        row = self._client(write=True).command(
            "model_routing.secret.put",
            {
                **boundary.payload(),
                "secret_reference": reference,
                "ciphertext": ciphertext,
                "key_hint": secret_hint(value),
                "updated_at": time.time(),
            },
            f"model_routing.secret.put:{reference}:{secrets.token_hex(8)}",
        )
        return {
            "secret_reference": str(row["secret_reference"]),
            "key_hint": str(row.get("key_hint") or ""),
        }

    def resolve_secret(self, boundary: OwnerBoundary, secret_reference: str) -> str:
        reference = str(secret_reference or "").strip()
        if not reference:
            return ""
        row = self._client().query(
            "model_routing.secret.get",
            {**boundary.payload(), "secret_reference": reference},
        )
        if row is None:
            raise ModelRoutingError(
                "credential secret reference does not exist",
                kind="credential_secret_missing",
            )
        return open_secret(
            str(row["ciphertext"]),
            purpose=_SECRET_PURPOSE,
            owner_user_id=boundary.owner_user_id,
            record_id=reference,
        )

    def secret_metadata(self, boundary: OwnerBoundary) -> list[dict[str, Any]]:
        rows = self._client().query(
            "model_routing.secret.list", boundary.payload())
        return [dict(row) for row in rows]

    def delete_secret(self, boundary: OwnerBoundary, secret_reference: str) -> bool:
        row = self._client(write=True).command(
            "model_routing.secret.delete",
            {**boundary.payload(), "secret_reference": secret_reference},
            f"model_routing.secret.delete:{secret_reference}:{secrets.token_hex(8)}",
        )
        return bool(row and row.get("deleted"))

    def prune_orphan_secrets(
        self,
        boundary: OwnerBoundary,
        *,
        older_than_seconds: float = 24 * 60 * 60,
    ) -> dict[str, Any]:
        authority = self.get(boundary)
        active = sorted({
            str(row.get("secret_reference") or "")
            for row in authority.document["credentials"]
            if row.get("secret_reference")
        })
        return dict(self._client(write=True).command(
            "model_routing.secret.prune",
            {
                **boundary.payload(),
                "active_secret_references": active,
                "updated_before": time.time() - max(0.0, older_than_seconds),
            },
            None,
        ))

    def migration_receipt(self, boundary: OwnerBoundary) -> dict[str, Any] | None:
        row = self._client().query(
            "model_routing.migration_receipt", boundary.payload())
        return None if row is None else dict(row)

    def record_migration_receipt(
        self, boundary: OwnerBoundary, receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        row = self._client(write=True).command(
            "model_routing.migration_receipt.put",
            {
                **boundary.payload(),
                "migration_receipt": copy.deepcopy(dict(receipt)),
                "document": empty_document(),
                "updated_at": time.time(),
            },
            f"model_routing.migration_receipt.put:{boundary.tenant_id}:"
            f"{boundary.owner_user_id}:{secrets.token_hex(8)}",
        )
        return dict(row)


class InMemoryModelRoutingRepository:
    """Bounded transient repository for tests and the storage-free agent."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._authorities: dict[OwnerBoundary, StoredAuthority] = {}
        self._secrets: dict[tuple[OwnerBoundary, str], str] = {}
        self._receipts: dict[OwnerBoundary, dict[str, Any]] = {}

    def get(self, boundary: OwnerBoundary) -> StoredAuthority:
        with self._lock:
            row = self._authorities.get(boundary)
            if row is None:
                return StoredAuthority(boundary, 0, empty_document(), 0.0)
            return StoredAuthority(
                boundary, row.revision, copy.deepcopy(row.document), row.updated_at)

    def compare_and_swap(
        self,
        boundary: OwnerBoundary,
        document: Mapping[str, Any],
        *,
        expected_revision: int,
        migration_receipt: Mapping[str, Any] | None = None,
    ) -> StoredAuthority:
        with self._lock:
            current = self._authorities.get(boundary)
            current_revision = 0 if current is None else current.revision
            if current_revision != expected_revision:
                raise ModelRoutingError(
                    "model-routing aggregate changed concurrently",
                    kind="model_routing_revision_conflict",
                )
            normalized = normalize_document(document, revision=expected_revision + 1)
            if _document_size(normalized) > MAX_DOCUMENT_BYTES:
                raise ModelRoutingError(
                    "model-routing aggregate exceeds its resource budget",
                    kind="model_routing_resource_budget_exceeded",
                )
            row = StoredAuthority(
                boundary, expected_revision + 1, copy.deepcopy(normalized), time.time())
            self._authorities[boundary] = row
            if migration_receipt is not None:
                self._receipts[boundary] = {
                    "revision": row.revision,
                    "backup": None if current is None else copy.deepcopy(current.document),
                    "receipt": copy.deepcopy(dict(migration_receipt)),
                    "updated_at": row.updated_at,
                }
            return self.get(boundary)

    def put_secret(
        self,
        boundary: OwnerBoundary,
        plaintext: str,
        *,
        secret_reference: str | None = None,
    ) -> dict[str, str]:
        value = _validated_secret(plaintext)
        reference = secret_reference or f"mrs_{secrets.token_hex(16)}"
        with self._lock:
            if len({key for key in self._secrets if key[0] == boundary}) >= 1024:
                raise ModelRoutingError(
                    "model-routing secret quota reached",
                    kind="model_routing_resource_budget_exceeded",
                )
            self._secrets[(boundary, reference)] = value
        return {"secret_reference": reference, "key_hint": secret_hint(value)}

    def resolve_secret(self, boundary: OwnerBoundary, secret_reference: str) -> str:
        try:
            return self._secrets[(boundary, secret_reference)]
        except KeyError as exc:
            raise ModelRoutingError(
                "credential secret reference does not exist",
                kind="credential_secret_missing",
            ) from exc

    def secret_metadata(self, boundary: OwnerBoundary) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "secret_reference": reference,
                    "key_hint": secret_hint(value),
                }
                for (owner, reference), value in self._secrets.items()
                if owner == boundary
            ]

    def delete_secret(self, boundary: OwnerBoundary, secret_reference: str) -> bool:
        with self._lock:
            return self._secrets.pop((boundary, secret_reference), None) is not None

    def migration_receipt(self, boundary: OwnerBoundary) -> dict[str, Any] | None:
        with self._lock:
            row = self._receipts.get(boundary)
            return None if row is None else copy.deepcopy(row)

    def record_migration_receipt(
        self, boundary: OwnerBoundary, receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            authority = self._authorities.get(boundary)
            row = {
                "revision": 0 if authority is None else authority.revision,
                "backup": None,
                "receipt": copy.deepcopy(dict(receipt)),
                "updated_at": time.time(),
            }
            self._receipts[boundary] = row
            return copy.deepcopy(row)


__all__ = [
    "InMemoryModelRoutingRepository",
    "ModelRoutingRepository",
    "OwnerBoundary",
    "RepositoryPort",
    "StoredAuthority",
]
