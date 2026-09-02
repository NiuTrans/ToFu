"""Paper library/report HTTP adapters preserve the authenticated owner."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.unit
OWNER = 73


class _StorageClient:
    def __init__(self) -> None:
        self.queries: list[tuple[str, dict]] = []
        self.commands: list[tuple[str, dict, str]] = []

    def query(self, operation, payload):
        self.queries.append((operation, dict(payload)))
        return []

    def command(self, operation, payload, command_id):
        self.commands.append((operation, dict(payload), command_id))
        if operation == "paper.library.delete":
            return {"deleted": True}
        return {"saved": True}


def _sidecar_library(monkeypatch):
    import lib.storage
    import routes.paper_pkg._library as library

    client = _StorageClient()
    monkeypatch.setattr(library, "request_user_id", lambda: OWNER)
    monkeypatch.setattr(lib.storage, "get_storage_client", lambda **_kw: client)
    monkeypatch.setattr(library, "api_ok", lambda value=None: value or {"ok": True})
    return library, client


def test_library_routes_scope_every_sidecar_operation_to_request_owner(monkeypatch):
    library, client = _sidecar_library(monkeypatch)
    monkeypatch.setattr(library, "async_parse_body", lambda: _async_value({"title": "T"}))

    asyncio.run(library.list_library())
    asyncio.run(library.upsert_library_entry("paper-1"))
    asyncio.run(library.delete_library_entry("paper-1"))
    asyncio.run(library.prune_broken_library_rows())

    payloads = [payload for _operation, payload in client.queries]
    payloads.extend(payload for _operation, payload, _command_id in client.commands)
    assert payloads
    assert {payload["user_id"] for payload in payloads} == {OWNER}
    assert all(
        f":{OWNER}:" in command_id
        for _operation, _payload, command_id in client.commands
    )


async def _async_value(value):
    return value


def test_ingest_persistence_rejects_an_implicit_owner(monkeypatch):
    import routes.paper_pkg._library as library

    with pytest.raises(ValueError, match="numeric user_id"):
        library._persist_ingested_library_row(
            "paper-1",
            user_id=None,
            title="T",
            pdf_url="/api/paper/pdf/p.pdf",
            pdf_filename="p.pdf",
            arxiv_id="",
            paper_hash="hash",
            parsed_text="text",
            images=[],
            page_count=1,
        )


def test_paper_research_services_require_an_explicit_owner():
    import lib.paper.harvest as harvest
    import lib.paper.survey as survey
    import lib.research.recipe as recipe

    owner_scoped_entrypoints = (
        harvest.harvest_arxiv_id,
        harvest.harvest_arxiv_batch,
        survey._load_paper_inputs,
        survey._library_id_set,
        survey._verify_against_library,
        survey.build_survey,
        recipe.build_research_from_direction,
    )
    for entrypoint in owner_scoped_entrypoints:
        parameter = inspect.signature(entrypoint).parameters["user_id"]
        assert parameter.default is inspect.Parameter.empty, entrypoint.__qualname__

    with pytest.raises(ValueError, match="numeric user_id"):
        harvest.harvest_arxiv_id("2501.00001", user_id=None)
    with pytest.raises(ValueError, match="numeric user_id"):
        survey._verify_against_library({}, user_id=None, lib_ids=set())
    with pytest.raises(ValueError, match="numeric user_id"):
        recipe.build_research_from_direction("direction", "unused", user_id=None)


def test_report_export_title_lookup_uses_request_owner(monkeypatch):
    import routes.paper_pkg._report as report

    calls = []

    class _Artifacts:
        def __init__(self, user_id):
            calls.append(("artifact.owner", user_id))

        def get_report(self, paper_hash, lang):
            calls.append(("artifact.get", paper_hash, lang))
            return SimpleNamespace(report="# Report")

    class _Library:
        def __init__(self, user_id):
            calls.append(("library.owner", user_id))

        def identity(self, paper_hash):
            calls.append(("library.identity", paper_hash))
            return SimpleNamespace(title="Owned title", arxiv_id="")

    monkeypatch.setattr(report, "request_user_id", lambda: OWNER)
    monkeypatch.setattr(
        report,
        "request",
        SimpleNamespace(args={"paper_hash": "abc123", "lang": "en", "format": "md"}),
    )
    monkeypatch.setattr(report, "PaperArtifactRepository", _Artifacts)
    monkeypatch.setattr(report, "PaperLibraryRepository", _Library)
    monkeypatch.setattr(report, "_safe_hash_dir", lambda _value: True)
    monkeypatch.setattr(report, "load_image_manifest", lambda _value: [])
    monkeypatch.setattr(report, "inject_images_into_report", lambda body, *_a, **_k: body)
    monkeypatch.setattr(
        report,
        "ensure_title_heading",
        lambda body, _hash, *, user_id: body,
    )

    response = asyncio.run(report.export_report())

    assert response.status_code == 200
    assert ("artifact.owner", OWNER) in calls
    assert ("artifact.get", "abc123", "en") in calls
    assert ("library.owner", OWNER) in calls
    assert ("library.identity", "abc123") in calls
