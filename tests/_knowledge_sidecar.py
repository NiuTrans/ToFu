"""Real Sidecar and owner-scoped file roots for knowledge-domain tests."""

from __future__ import annotations

import uuid

import pytest


TEST_OWNER_USER_ID = 1


@pytest.fixture(scope="module", autouse=True)
def knowledge_storage(tmp_path_factory):
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.service import install_runtime_for_test

    project_root = tmp_path_factory.mktemp("knowledge-sidecar")
    runtime = StorageRuntime(
        StorageSupervisor(
            project_root=project_root, backend="sqlite", startup_timeout=60),
        auto_restart=False,
    )
    install_runtime_for_test(runtime)
    runtime.start()
    try:
        yield runtime
    finally:
        from lib.knowledge.enrichment import stop_visual_enrichment

        stop_visual_enrichment(timeout=3)
        runtime.stop()
        install_runtime_for_test(None)


@pytest.fixture()
def isolated_knowledge(tmp_path, monkeypatch, knowledge_storage):
    """Reset one owner's corpus while keeping Sidecar startup module-scoped."""
    from lib.knowledge import store
    from lib.knowledge.enrichment import stop_visual_enrichment
    from lib.knowledge.repository import KnowledgeRepository

    repository = KnowledgeRepository(TEST_OWNER_USER_ID)
    repository.clear_owner(command_id=f"knowledge.test.clear:{uuid.uuid4().hex}")
    monkeypatch.setattr(
        store, "_SOURCE_ROOT_OVERRIDE", str(tmp_path / "knowledge-files"))
    monkeypatch.setattr(
        store, "_ASSET_ROOT_OVERRIDE", str(tmp_path / "knowledge-files"))
    try:
        yield store
    finally:
        stop_visual_enrichment(timeout=3, user_id=TEST_OWNER_USER_ID)
        repository.clear_owner(
            command_id=f"knowledge.test.cleanup:{uuid.uuid4().hex}")


__all__ = ["TEST_OWNER_USER_ID", "isolated_knowledge", "knowledge_storage"]
