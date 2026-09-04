"""Typed owner-bound access to the durable knowledge corpus."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from lib.identity import PrincipalContext, require_user_id
from lib.storage.errors import StorageError


def _required_command_id(value: str, *, operation: str) -> str:
    command_id = str(value or "").strip()
    if not command_id or len(command_id) > 200:
        raise ValueError(f"{operation} requires a 1..200 character command_id")
    return command_id


class KnowledgeRepository:
    """Keep owner injection and semantic storage commands in one place."""

    def __init__(
        self,
        owner_user_id: int,
        *,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.owner_user_id = require_user_id(
            owner_user_id, context="knowledge corpus owner")
        self._client_factory = client_factory

    def _client(self, *, write: bool = False):
        if self._client_factory is not None:
            return self._client_factory(write=write)
        from lib.storage import get_storage_client

        return get_storage_client(write=write)

    def _payload(self, **values) -> dict:
        return {"user_id": self.owner_user_id, **values}

    def _command(self, operation: str, payload: Mapping, command_id: str):
        """Replay one receipted command once after an ambiguous transport loss."""
        stable_command_id = _required_command_id(
            command_id, operation=operation)
        try:
            return self._client(write=True).command(
                operation, dict(payload), stable_command_id)
        except StorageError as exc:
            if not exc.retryable:
                raise
            return self._client(write=True).command(
                operation, dict(payload), stable_command_id)

    def documents(self) -> list[dict]:
        rows = self._client().query(
            "knowledge.document.list", self._payload())
        return [dict(row) for row in rows] if isinstance(rows, list) else []

    def document(self, document_id: str) -> dict | None:
        row = self._client().query(
            "knowledge.document.get",
            self._payload(document_id=str(document_id or "")),
        )
        return dict(row) if isinstance(row, Mapping) else None

    def document_metadata(self, document_id: str) -> dict | None:
        row = self._client().query(
            "knowledge.document.metadata",
            self._payload(document_id=str(document_id or "")),
        )
        return dict(row) if isinstance(row, Mapping) else None

    def document_assets(
        self, document_id: str, *, offset: int = 0, limit: int = 80,
    ) -> list[dict] | None:
        rows = self._client().query(
            "knowledge.document.assets",
            self._payload(
                document_id=str(document_id or ""),
                offset=int(offset), limit=int(limit)),
        )
        if rows is None:
            return None
        return [dict(row) for row in rows] if isinstance(rows, list) else []

    def document_content(
        self, document_id: str, *, offset: int = 0, limit: int = 80,
    ) -> dict | None:
        row = self._client().query(
            "knowledge.document.content",
            self._payload(
                document_id=str(document_id or ""),
                offset=int(offset),
                limit=int(limit),
            ),
        )
        return dict(row) if isinstance(row, Mapping) else None

    def document_by_digest(self, sha256: str) -> dict | None:
        row = self._client().query(
            "knowledge.document.find_digest",
            self._payload(sha256=str(sha256 or "")),
        )
        return dict(row) if isinstance(row, Mapping) else None

    def create_document(
        self, document: Mapping, *, command_id: str,
    ) -> tuple[dict, bool]:
        document_id = str(document.get("id") or "")
        result = self._command(
            "knowledge.document.create",
            self._payload(document_id=document_id, document=dict(document)),
            command_id,
        )
        return dict(result["document"]), bool(result["created"])

    def replace_document(
        self, document: Mapping, *, command_id: str,
    ) -> dict | None:
        document_id = str(document.get("id") or "")
        result = self._command(
            "knowledge.document.replace",
            self._payload(document_id=document_id, document=dict(document)),
            command_id,
        )
        return dict(result) if isinstance(result, Mapping) else None

    def patch_document(
        self, document_id: str, *, updates: Mapping, command_id: str,
    ) -> dict | None:
        result = self._command(
            "knowledge.document.patch",
            self._payload(
                document_id=str(document_id or ""), updates=dict(updates)),
            command_id,
        )
        return dict(result) if isinstance(result, Mapping) else None

    def delete_document(
        self, document_id: str, *, command_id: str,
    ) -> dict | None:
        result = self._command(
            "knowledge.document.delete",
            self._payload(document_id=str(document_id or "")),
            command_id,
        )
        document = (result or {}).get("document")
        return dict(document) if isinstance(document, Mapping) else None

    def settings(self) -> dict:
        result = self._client().query(
            "knowledge.settings.get", self._payload())
        return dict(result) if isinstance(result, Mapping) else {
            "enabled": False, "visual_enrichment": False}

    def available(self) -> bool:
        result = self._client().query(
            "knowledge.availability", self._payload())
        return bool((result or {}).get("available"))

    def catalog(
        self,
        *,
        page: int = 1,
        page_size: int = 30,
        query: str = "",
        category: str = "all",
        sort: str = "updated_desc",
    ) -> dict:
        result = self._client().query(
            "knowledge.catalog",
            self._payload(
                page=int(page), page_size=int(page_size), query=str(query),
                category=str(category), sort=str(sort)),
        )
        return dict(result) if isinstance(result, Mapping) else {}

    def search_candidates(
        self, tokens: list[str], *, limit: int = 80,
        document_id: str = "",
    ) -> list[dict]:
        result = self._client().query(
            "knowledge.search.candidates",
            self._payload(
                tokens=list(tokens), limit=int(limit),
                document_id=str(document_id or "")),
        )
        return [dict(row) for row in result] if isinstance(result, list) else []

    def patch_settings(self, *, command_id: str, **updates: bool) -> dict:
        result = self._command(
            "knowledge.settings.patch",
            self._payload(**updates),
            command_id,
        )
        return dict(result) if isinstance(result, Mapping) else {}

    def asset(self, asset_id: str) -> dict | None:
        result = self._client().query(
            "knowledge.asset.get",
            self._payload(asset_id=str(asset_id or "")),
        )
        return dict(result) if isinstance(result, Mapping) else None

    def enrichment_activity(self) -> dict:
        result = self._client().query(
            "knowledge.enrichment.activity", self._payload())
        return dict(result) if isinstance(result, Mapping) else {}

    def claim_pending_asset(self, *, command_id: str) -> dict | None:
        result = self._command(
            "knowledge.asset.claim", self._payload(),
            command_id)
        return dict(result) if isinstance(result, Mapping) else None

    def update_asset(
        self,
        asset_id: str,
        *,
        updates: Mapping,
        command_id: str,
        chunk_content: str | None = None,
        chunk_search_text: str | None = None,
    ) -> bool:
        payload = self._payload(
            asset_id=str(asset_id or ""), updates=dict(updates))
        if chunk_content is not None:
            payload["chunk_content"] = chunk_content
        if chunk_search_text is not None:
            payload["chunk_search_text"] = chunk_search_text
        result = self._command(
            "knowledge.asset.update", payload,
            command_id)
        return bool((result or {}).get("updated"))

    def mark_pending_assets_no_vision(self, *, command_id: str) -> int:
        result = self._command(
            "knowledge.assets.mark_no_vision", self._payload(),
            command_id)
        return int((result or {}).get("changed") or 0)

    def clear_owner(self, *, command_id: str) -> int:
        result = self._command(
            "knowledge.owner.clear", self._payload(), command_id)
        return int((result or {}).get("deleted_documents") or 0)


__all__ = ["KnowledgeRepository"]


def visual_enrichment_owner_ids(
    *, principal: PrincipalContext,
    limit: int = 512,
    client_factory: Callable[..., Any] | None = None,
) -> list[int]:
    """Bounded owners with authorized, unfinished visual evidence."""
    if not isinstance(principal, PrincipalContext) or principal.kind != "system":
        raise PermissionError(
            "knowledge owner inventory requires a system principal")
    principal.require_scope("knowledge:maintain")
    if (not isinstance(limit, int) or isinstance(limit, bool)
            or limit <= 0 or limit > 512):
        raise ValueError("knowledge enrichment owner limit must be 1..512")
    if client_factory is None:
        from lib.storage import get_storage_client

        client_factory = get_storage_client

    rows = client_factory(write=False).query(
        "knowledge.enrichment.owners", {"limit": limit})
    return [int(owner_id) for owner_id in rows] if isinstance(rows, list) else []


__all__.append("visual_enrichment_owner_ids")
