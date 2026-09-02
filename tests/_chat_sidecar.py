"""Shared real Sidecar authority for chat-domain storage tests.

The runtime is module-scoped and rooted in a disposable system directory,
while activation is explicitly requested by each test module. This keeps
storage state isolated from the repository database and makes persistence
tests exercise the same client/service contract as production.

Usage in a test module::

    pytest_plugins = ('tests._chat_sidecar',)
"""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import uuid

import pytest


@pytest.fixture(scope='module')
def _chat_sidecar_runtime():
    """Start one real SQLite-backed Sidecar for an opted-in test module."""
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.service import install_runtime_for_test
    project_root = Path(tempfile.mkdtemp(prefix='chat-sidecar-')) / (
        uuid.uuid4().hex[:8])
    project_root.mkdir(parents=True, exist_ok=True)
    runtime = StorageRuntime(
        StorageSupervisor(
            project_root=project_root, backend='sqlite', startup_timeout=60),
        auto_restart=False,
    )
    install_runtime_for_test(runtime)
    runtime.start()
    try:
        yield runtime
    finally:
        install_runtime_for_test(None)
        try:
            runtime.stop()
        except Exception:
            pass
        shutil.rmtree(project_root, ignore_errors=True)


@pytest.fixture
def chat_sidecar(_chat_sidecar_runtime, monkeypatch):
    """Expose the module's Sidecar authority to one test.

    The environment marker is temporary test-harness isolation while the
    repository-wide fixture stops exporting the retired legacy mode; the
    production storage modules exercised here do not branch on it.
    """
    yield _chat_sidecar_runtime
