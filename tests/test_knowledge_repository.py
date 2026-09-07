"""Knowledge repository always binds operations to one explicit owner."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.identity import PrincipalContext
from lib.knowledge.repository import KnowledgeRepository
from lib.storage.errors import StorageError


pytestmark = pytest.mark.unit


class _Client:
    def __init__(self):
        self.calls = []

    def query(self, operation, payload):
        self.calls.append(("query", operation, payload))
        if operation == "knowledge.document.list":
            return []
        if operation == "knowledge.document.content":
            return {
                "document": {"id": payload["document_id"]},
                "chunks": [],
                "pagination": {
                    "offset": payload["offset"], "limit": payload["limit"]},
            }
        if operation == "knowledge.settings.get":
            return {"enabled": True, "visual_enrichment": False}
        if operation == "knowledge.enrichment.activity":
            return {"pending": 0}
        if operation == "knowledge.availability":
            return {"available": True}
        if operation == "knowledge.catalog":
            return {"documents": [], "totals": {"documents": 0}}
        if operation == "knowledge.search.candidates":
            return []
        return None

    def command(self, operation, payload, command_id):
        self.calls.append(("command", operation, payload, command_id))
        if operation == "knowledge.document.create":
            return {"created": True, "document": payload["document"]}
        if operation == "knowledge.settings.patch":
            return {"enabled": True, "visual_enrichment": False}
        if operation == "knowledge.document.delete":
            return {"deleted": False, "document": None}
        if operation == "knowledge.assets.mark_no_vision":
            return {"changed": 0}
        if operation == "knowledge.owner.clear":
            return {"deleted_documents": 0}
        return {"updated": True}


def test_repository_injects_owner_into_every_corpus_operation():
    client = _Client()
    repo = KnowledgeRepository(
        23, client_factory=lambda *, write=False: client)
    document = {"id": "doc", "sha256": "abc", "chunks": [], "assets": []}

    assert repo.documents() == []
    assert repo.document_content("doc", offset=40, limit=20)["pagination"] == {
        "offset": 40, "limit": 20}
    assert repo.document_by_digest("a" * 64) is None
    assert repo.settings()["enabled"] is True
    assert repo.available() is True
    assert repo.catalog()["documents"] == []
    assert repo.search_candidates(["evidence"]) == []
    assert repo.create_document(
        document, command_id="create-doc") == (document, True)
    assert repo.patch_settings(
        enabled=True, command_id="enable-knowledge")["enabled"] is True
    assert repo.delete_document(
        "missing", command_id="delete-missing") is None
    assert repo.mark_pending_assets_no_vision(
        command_id="mark-no-vision") == 0
    assert repo.clear_owner(command_id="clear-owner") == 0
    assert all(call[2]["user_id"] == 23 for call in client.calls)


def test_repository_replays_ambiguous_command_with_the_same_receipt():
    class LostAckClient(_Client):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def command(self, operation, payload, command_id):
            self.calls.append(("command", operation, payload, command_id))
            self.attempts += 1
            if self.attempts == 1:
                raise StorageError(
                    "database_timeout", "lost acknowledgement", retryable=True)
            return {"id": "asset-1", "enrichment_status": "running"}

    client = LostAckClient()
    repo = KnowledgeRepository(
        23, client_factory=lambda *, write=False: client)

    assert repo.claim_pending_asset(command_id="worker-claim-7") == {
        "id": "asset-1", "enrichment_status": "running"}
    assert [call[3] for call in client.calls] == [
        "worker-claim-7", "worker-claim-7"]


def test_owner_inventory_requires_restricted_system_principal():
    from lib.knowledge.repository import visual_enrichment_owner_ids

    client = _Client()
    factory = lambda *, write=False: client
    with pytest.raises(PermissionError):
        visual_enrichment_owner_ids(
            principal=PrincipalContext.user(
                subject_id="user", owner_user_id=23,
                scopes={"knowledge:maintain"}),
            client_factory=factory,
        )
    with pytest.raises(PermissionError):
        visual_enrichment_owner_ids(
            principal=PrincipalContext.system(
                subject_id="unscoped", scopes=set()),
            client_factory=factory,
        )
    assert client.calls == []


def test_owner_inventory_passes_a_bounded_storage_limit():
    from lib.knowledge.repository import visual_enrichment_owner_ids

    class InventoryClient(_Client):
        def query(self, operation, payload):
            self.calls.append(("query", operation, payload))
            return [7, 9]

    client = InventoryClient()
    principal = PrincipalContext.system(
        subject_id="knowledge-maintainer", scopes={"knowledge:maintain"})

    assert visual_enrichment_owner_ids(
        principal=principal,
        limit=11,
        client_factory=lambda *, write=False: client,
    ) == [7, 9]
    assert client.calls == [
        ("query", "knowledge.enrichment.owners", {"limit": 11})]

    with pytest.raises(ValueError, match="1..512"):
        visual_enrichment_owner_ids(
            principal=principal,
            limit=513,
            client_factory=lambda *, write=False: client,
        )
