"""Principal and ownership contract shared by every execution boundary.

``PrincipalContext`` is the sole structured identity carried from HTTP auth
into services, tasks, repositories, and maintenance processes. Personal mode
may select its bootstrap owner only at composition; lower layers still receive
an explicit context or positive owner and never consult a module-global user.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

PERSONAL_USER_ID = 1

PrincipalKind = Literal["user", "system"]


def require_user_id(value: Any, *, context: str = "operation") -> int:
    """Return a positive owner id or fail closed.

    Core application and storage code calls this at ownership boundaries.
    Personal-user defaults belong only in request/bootstrap composition, never
    in a repository, task, or background callback.
    """
    if isinstance(value, bool):
        raise ValueError(f"{context} requires a numeric user_id")
    try:
        user_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{context} requires a numeric user_id") from exc
    if user_id <= 0:
        raise ValueError(f"{context} requires a positive user_id")
    return user_id


def _normalized_identifier(value: Any, *, field: str) -> str:
    identifier = str(value or "").strip()
    if not identifier or len(identifier) > 256:
        raise ValueError(f"principal {field} must be 1..256 characters")
    return identifier


def _normalized_scopes(values: Iterable[Any]) -> frozenset[str]:
    scopes: set[str] = set()
    for value in values:
        scope = str(value or "").strip()
        if not scope or len(scope) > 128:
            raise ValueError("principal scopes must be 1..128 characters")
        scopes.add(scope)
    return frozenset(scopes)


@dataclass(frozen=True, slots=True)
class PrincipalContext:
    """Immutable identity passed explicitly across request/task boundaries."""

    kind: PrincipalKind
    subject_id: str
    owner_user_id: int | None
    tenant_id: str | None
    scopes: frozenset[str]

    def __post_init__(self) -> None:
        if self.kind not in {"user", "system"}:
            raise ValueError("principal kind must be user or system")
        object.__setattr__(
            self, "subject_id",
            _normalized_identifier(self.subject_id, field="subject_id"),
        )
        if self.kind == "user" and self.owner_user_id is None:
            raise ValueError("user principal requires owner_user_id")
        if self.owner_user_id is not None:
            object.__setattr__(
                self,
                "owner_user_id",
                require_user_id(
                    self.owner_user_id, context="principal owner_user_id"),
            )
        tenant_id = str(self.tenant_id or "").strip() or None
        if tenant_id is not None and len(tenant_id) > 256:
            raise ValueError("principal tenant_id must be at most 256 characters")
        object.__setattr__(self, "tenant_id", tenant_id)
        object.__setattr__(self, "scopes", _normalized_scopes(self.scopes))

    @classmethod
    def user(
        cls,
        *,
        subject_id: str,
        owner_user_id: Any,
        tenant_id: str | None = None,
        scopes: Iterable[Any] = (),
    ) -> "PrincipalContext":
        return cls(
            kind="user",
            subject_id=subject_id,
            owner_user_id=require_user_id(
                owner_user_id, context="user principal"),
            tenant_id=tenant_id,
            scopes=_normalized_scopes(scopes),
        )

    @classmethod
    def system(
        cls,
        *,
        subject_id: str,
        scopes: Iterable[Any],
        tenant_id: str | None = None,
        owner_user_id: Any | None = None,
    ) -> "PrincipalContext":
        return cls(
            kind="system",
            subject_id=subject_id,
            owner_user_id=(
                None if owner_user_id is None
                else require_user_id(owner_user_id, context="system principal")
            ),
            tenant_id=tenant_id,
            scopes=_normalized_scopes(scopes),
        )

    def require_owner(self, *, context: str = "operation") -> int:
        if self.owner_user_id is None:
            raise PermissionError(f"{context} requires an owning user principal")
        return require_user_id(self.owner_user_id, context=context)

    def has_scope(self, scope: str) -> bool:
        requested = str(scope or "").strip()
        return bool(requested) and (
            "admin" in self.scopes or requested in self.scopes)

    def require_scope(self, scope: str) -> None:
        if not self.has_scope(scope):
            raise PermissionError(
                f"principal {self.subject_id!r} lacks scope {scope!r}")

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subject_id": self.subject_id,
            "owner_user_id": self.owner_user_id,
            "tenant_id": self.tenant_id,
            "scopes": sorted(self.scopes),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PrincipalContext":
        return cls(
            kind=str(payload.get("kind") or ""),  # type: ignore[arg-type]
            subject_id=str(payload.get("subject_id") or ""),
            owner_user_id=payload.get("owner_user_id"),
            tenant_id=payload.get("tenant_id"),
            scopes=_normalized_scopes(payload.get("scopes") or ()),
        )


def principal_from_auth_context(
    auth_context: Any,
    *,
    allow_personal_owner: bool,
) -> PrincipalContext:
    """Translate the auth adapter once; reject missing enterprise owners."""
    if auth_context is None:
        raise PermissionError("request has no authenticated principal")
    raw_owner = getattr(auth_context, "owner_user_id", None)
    if raw_owner in (None, ""):
        if not allow_personal_owner:
            raise PermissionError(
                "authenticated principal has no owner_user_id")
        raw_owner = PERSONAL_USER_ID
    owner_user_id = require_user_id(raw_owner, context="request principal")
    subject_id = str(
        getattr(auth_context, "key_id", "") or f"user:{owner_user_id}")
    return PrincipalContext.user(
        subject_id=subject_id,
        owner_user_id=owner_user_id,
        tenant_id=getattr(auth_context, "tenant_id", None),
        scopes=getattr(auth_context, "scopes", ()) or (),
    )


__all__ = [
    "PERSONAL_USER_ID",
    "PrincipalContext",
    "PrincipalKind",
    "principal_from_auth_context",
    "require_user_id",
]
