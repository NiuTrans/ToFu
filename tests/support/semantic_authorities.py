"""Function-scoped real authorities for semantic storage parity and recovery."""

from pathlib import Path
import os
import shutil
import pytest

pytest_plugins = ("tests.support.postgres_cluster",)


@pytest.fixture(params=["sqlite", "native", "postgres"])
def semantic_authority(request, tmp_path, monkeypatch):
    from lib.storage import StorageRuntime, StorageSupervisor, get_storage_client
    from lib.storage.service import install_runtime_for_test

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
        install_runtime_for_test(runtime)
        try:
            runtime.start()
            yield get_storage_client, 41, None
        finally:
            install_runtime_for_test(None)
            runtime.stop()
    else:
        from scripts.tofudb_perf_compare import TofuDbEngine, _OWNER_ID

        binary = os.environ.get("TOFUDB_BIN")
        if not binary:
            pytest.skip("Set TOFUDB_BIN to a source-matched native engine")
        engine = TofuDbEngine(Path(binary).resolve(), authority_dir_parent=tmp_path)
        engine.start()
        directories = list(engine._dirs)
        try:
            yield lambda **kwargs: engine._client, _OWNER_ID, engine
        finally:
            if engine._process is not None and engine._process.poll() is None:
                engine.stop()
            for directory in directories:
                shutil.rmtree(directory, ignore_errors=True)
