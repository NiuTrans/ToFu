"""Function-scoped real authorities for semantic storage parity and recovery."""

from pathlib import Path
import os
import shutil
import pytest

pytest_plugins = ("tests.support.postgres_cluster",)


@pytest.fixture(params=["sqlite", "native", "postgres"])
def semantic_authority(request, tmp_path, monkeypatch):
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage import service as storage_service
    from lib.resource_control.adapters import bind_storage

    if request.param in {"sqlite", "postgres"}:
        if request.param == "postgres":
            dsn = request.getfixturevalue("postgres_cluster")
            from tests.support.postgres_worker_clock import install_worker_clock
            install_worker_clock(dsn, tmp_path / 'server-clock')
            dsn_file = tmp_path / "postgres-dsn"
            dsn_file.write_text(dsn)
            dsn_file.chmod(0o600)
            monkeypatch.setenv("TOFU_STORAGE_TEST_POSTGRES_DSN_FILE", str(dsn_file))
            monkeypatch.setenv("TOFU_STORAGE_PG_READ_POOL", "2")
            monkeypatch.setenv("TOFU_STORAGE_PG_WRITE_POOL", "2")
        runtime = StorageRuntime(
            StorageSupervisor(project_root=tmp_path, backend=request.param),
            auto_restart=False,
        )
        # Install through monkeypatch so fixtures that overlay the process-global
        # runtime with a double unwind in LIFO order instead of this teardown
        # stopping their double; the factory stays bound to this authority.
        monkeypatch.setattr(storage_service, '_runtime', runtime)

        def authority_client(*, write=False):
            client = runtime.client(write=write)
            bind_storage(client)
            return client

        try:
            runtime.start()
            yield authority_client, 41, None
        finally:
            runtime.stop()
    else:
        from scripts.tofudb_perf_compare import TofuDbEngine, _OWNER_ID

        from serverctl_pkg.native_engine import verify_current
        from tests.support.native_engine_prerequisite import inherited_engine_refusal_skips

        binary = os.environ.get("TOFUDB_BIN")
        if not binary:
            pytest.skip("Set TOFUDB_BIN to a source-matched native engine")
        binary = Path(binary).resolve()
        explicit = os.environ.get("TOFU_TEST_TOFUDB_BIN")
        expected_sha256 = ""
        if explicit and Path(explicit).expanduser().resolve() == binary:
            # tests/conftest.py verified this artifact when the process started
            # and recorded its digest; pin those bytes instead of re-running
            # the source fingerprint and identity probe for every test.
            expected_sha256 = os.environ["TOFUDB_EXPECTED_SHA256"]
        else:
            with inherited_engine_refusal_skips():
                verify_current(binary, Path(__file__).resolve().parents[2])
        engine = TofuDbEngine(binary, authority_dir_parent=tmp_path,
                              expected_sha256=expected_sha256)
        engine.start()
        directories = list(engine._dirs)
        try:
            yield lambda **kwargs: engine._client, _OWNER_ID, engine
        finally:
            if engine._process is not None and engine._process.poll() is None:
                engine.stop()
            for directory in directories:
                shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def detached_semantic_authority(semantic_authority):
    """``semantic_authority`` held outside the process storage slot.

    Specs that compose a fixture installing its own process-runtime double
    (the coordinator dispatch-tools conversation-creation double) would
    otherwise displace the SQL authority from that slot: repository calls
    then reach the double, and the authority's teardown stops the double
    instead of itself. This fixture yields the authority's own client
    factory, empties the slot before such fixtures run, and returns the
    authority to it before ``semantic_authority`` stops it, so the slot ends
    empty. The native authority never occupies the slot.
    """
    factory, owner, engine = semantic_authority
    if engine is not None:
        yield factory, owner, engine
        return
    from lib.storage import service as storage_service

    authority = storage_service.storage_runtime()
    with storage_service._lock:
        storage_service._runtime = None
    try:
        yield authority.client, owner, engine
    finally:
        with storage_service._lock:
            storage_service._runtime = authority
