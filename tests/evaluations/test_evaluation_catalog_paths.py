"""Offline contracts for neutral benchmark identity and explicit host storage."""
from __future__ import annotations

import copy
import errno
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluations.catalog import BENCHMARKS, CATALOG_PATH, load_catalog
from evaluations.paths import runtime_environment, storage_record, validate_output_path, validate_storage_record
from evaluations.swebench import constants
from evaluations.swebench.artifacts import prepare_output_root
from evaluations.swebench.harbor_runner import HarborRunSpec, build_job_config, start_harbor_run
from rootless_vm import storage_probe

pytestmark = pytest.mark.unit


def _catalog(host_tmp_path):
    row = copy.deepcopy(json.loads(CATALOG_PATH.read_text())["benchmarks"][0])
    row.update(key="fixture-benchmark", task_count=1, task_digest_lock="tasks.json",
               environment_defaults={})
    (host_tmp_path / "tasks.json").write_text(json.dumps({"suite/task": "sha256:" + "a" * 64}))
    path = host_tmp_path / "catalog.json"
    payload = {"schema_version": 1, "benchmarks": [row]}
    path.write_text(json.dumps(payload))
    return path, payload


def test_new_catalog_entry_drives_existing_runner_without_benchmark_branches(host_tmp_path, monkeypatch):
    path, _ = _catalog(host_tmp_path)
    catalog = load_catalog(path)
    from evaluations.swebench import harbor_runner
    monkeypatch.setattr(harbor_runner, "BENCHMARKS", catalog)
    spec = HarborRunSpec(agent="codex", models=("fixture",), backend="modal",
                         output_root=host_tmp_path / "runs", benchmark="fixture-benchmark")
    config = build_job_config(spec, run_dir=host_tmp_path / "run", run_id="fixture")
    assert config["datasets"][0]["ref"] == catalog["fixture-benchmark"].dataset_source_revision
    assert catalog["fixture-benchmark"].task_digests() == {"suite/task": "sha256:" + "a" * 64}
    assert config["n_attempts"] == 1
    assert config["environment"]["type"] == "modal"


@pytest.mark.parametrize("mutation", [
    lambda p: p["benchmarks"].append(copy.deepcopy(p["benchmarks"][0])),
    lambda p: p["benchmarks"][0].update(dataset="suite@latest"),
    lambda p: p["benchmarks"][0].update(task_count=True),
    lambda p: p["benchmarks"][0].update(task_digest_lock="../escape.json"),
    lambda p: p["benchmarks"][0].update(source_commit="main"),
    lambda p: p["benchmarks"][0].update(source_url="https://user:secret@example.invalid/"),
    lambda p: p["benchmarks"][0].update(harness="unreviewed-import:Class"),
])
def test_catalog_rejects_ambiguous_unpinned_or_executable_inputs(host_tmp_path, mutation):
    path, payload = _catalog(host_tmp_path)
    mutation(payload)
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        load_catalog(path)


def test_catalog_task_digest_drift_and_surplus_fail_closed(host_tmp_path):
    path, _ = _catalog(host_tmp_path)
    (host_tmp_path / "tasks.json").write_text(json.dumps({"task": "sha256:not-a-digest"}))
    with pytest.raises(ValueError, match="task digest lock"):
        load_catalog(path)
    assert set(BENCHMARKS) == {"terminal-bench-2.1", "swebench-verified"}


def test_default_storage_stays_on_checkout_volume_not_home_or_tmp(monkeypatch):
    monkeypatch.delenv("TOFU_EVAL_ROOT", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/ambient-state")
    assert constants.default_output_root() == constants.PROJECT_ROOT / ".eval-runs" / "benchmarks"


@pytest.mark.parametrize("path", ["/tmp/eval", "/var/tmp/eval", "/dev/shm/eval"])
def test_forbidden_host_output_rejected_before_creation(path):
    with pytest.raises(ValueError, match="forbidden"):
        prepare_output_root(Path(path))


def test_host_path_rejects_symlink_and_off_dolphin_volume(host_tmp_path, monkeypatch):
    target = host_tmp_path / "target"
    target.mkdir()
    (host_tmp_path / "link").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        validate_output_path(host_tmp_path / "link" / "new")
    from evaluations import paths
    monkeypatch.setattr(paths, "PROJECT_ROOT", Path("/mnt/your-fs"))
    monkeypatch.setattr(paths, "validate_host_path", lambda path: path)
    with pytest.raises(ValueError, match="dolphinfs"):
        validate_output_path(Path("/opt/other-volume/new-eval"))


def test_runtime_environment_is_per_run_private_and_not_process_global(host_tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", "/tmp/ambient")
    monkeypatch.setenv("UV_CACHE_DIR", "/tmp/cache")
    monkeypatch.setenv("SECRET_SENTINEL", "never-persist")
    first = runtime_environment(host_tmp_path / "one")
    second = runtime_environment(host_tmp_path / "two")
    assert first["TMPDIR"] != second["TMPDIR"]
    assert first["ROOTLESS_VM_TMPDIR"] == first["TMPDIR"]
    assert all(Path(value).is_relative_to(host_tmp_path / "one") for value in first.values())
    assert Path(first["TMPDIR"]).stat().st_mode & 0o777 == 0o700
    assert os.environ["TMPDIR"] == "/tmp/ambient"
    assert "never-persist" not in json.dumps(first)
    assert Path(first["HOME"]).stat().st_mode & 0o777 == 0o700
    assert first["HOME"] != os.environ.get("HOME")
    before = sorted(p.name for p in (host_tmp_path / "one" / ".runtime").iterdir())
    assert runtime_environment(host_tmp_path / "one") == first
    assert sorted(p.name for p in (host_tmp_path / "one" / ".runtime").iterdir()) == before


def test_storage_manifest_rejects_missing_and_changed_paths(host_tmp_path):
    run = host_tmp_path / "run"
    record = storage_record(run)
    validate_storage_record(run, record)
    record["environment"]["TMPDIR"] = "/tmp/drift"
    for invalid in (None, record):
        with pytest.raises(ValueError, match="storage policy"):
            validate_storage_record(run, invalid)


def test_storage_probe_reports_enosys_and_cleans_owned_named_files(host_tmp_path, monkeypatch):
    class UnsupportedSocket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def settimeout(self, value):
            pass
        def bind(self, address):
            assert address.startswith("/proc/self/fd/")
            raise OSError(errno.ENOSYS, "Function not implemented")
    monkeypatch.setattr(storage_probe.socket, "socket", lambda *a: UnsupportedSocket())
    root = host_tmp_path / "state"
    report = storage_probe.probe_storage(root)
    assert report["ok"] is False
    assert report["checks"]["pathname_unix_socket"] == {"ok": False, "errno": errno.ENOSYS}
    assert report["checks"]["named_files_atomic_replace_flock"]["ok"] is True
    assert list((root / ".runtime" / "tmp").iterdir()) == []
    with pytest.raises(ValueError, match="no host /tmp fallback"):
        storage_probe.require_storage(root)


def test_unsupported_storage_blocks_before_harbor_subprocess(host_tmp_path, monkeypatch):
    from evaluations.swebench import harbor_runner as runner
    settings = SimpleNamespace(preflight_storage=lambda path: (_ for _ in ()).throw(
        ValueError("storage unavailable")))
    spec = HarborRunSpec(agent="oracle", models=("oracle",), backend="rootless-qemu",
                         output_root=host_tmp_path / "runs", rootless_qemu=settings)
    monkeypatch.setattr(runner, "_git_revision", lambda **k: "a" * 40)
    monkeypatch.setattr(runner, "_git_dirty", lambda **k: False)
    monkeypatch.setattr(runner, "resolve_executable", lambda *a: pytest.fail("must not dispatch"))
    with pytest.raises(ValueError, match="storage unavailable"):
        start_harbor_run(spec)


def test_neutral_cli_delegates_help_to_selected_adapter(monkeypatch):
    from evaluations.__main__ import main
    from evaluations.swebench import cli
    received = []
    monkeypatch.setattr(cli, "main", lambda argv: received.append(argv) or 0)
    assert main(["harbor", "run", "--help"]) == 0
    assert received == [["run", "--help"]]


def test_output_root_rejects_symlink_marker_and_ignore_files(host_tmp_path):
    external = host_tmp_path / "existing"
    external.write_text("must remain unchanged")
    for name in (".tofu-swebench-eval-root", ".gitignore", ".ignore"):
        root = prepare_output_root(host_tmp_path / name.lstrip("."))
        (root / name).unlink()
        (root / name).symlink_to(external)
        with pytest.raises(ValueError, match="symlink"):
            prepare_output_root(root)
        assert external.read_text() == "must remain unchanged"


def test_child_runtime_rejects_insecure_parent_without_chmod(host_tmp_path):
    parent = host_tmp_path / "run" / ".runtime"
    parent.mkdir(parents=True, mode=0o755)
    parent.chmod(0o755)
    with pytest.raises(PermissionError, match="runtime root"):
        runtime_environment(host_tmp_path / "run")
    assert parent.stat().st_mode & 0o777 == 0o755


def test_resume_rejects_symlink_before_opening_manifest(host_tmp_path):
    from evaluations.swebench.harbor_runner import resume_harbor_run
    (host_tmp_path / "alias").symlink_to(host_tmp_path / "not-a-run", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        resume_harbor_run(host_tmp_path / "alias")


def test_real_child_cannot_restore_ambient_tmp_or_home(host_tmp_path, monkeypatch):
    import sys
    from evaluations.swebench.process import run_streaming
    # HOME isolation protects tools which ignore XDG (including pinned Harbor).
    monkeypatch.setenv("TMPDIR", "/tmp/ambient")
    parent_home = os.environ.get("HOME")
    run = host_tmp_path / "child"
    expected = runtime_environment(run)
    output = run / "child-paths.json"
    code = (
        "import json,os,tempfile; from pathlib import Path; "
        "p=Path(tempfile.mkdtemp()); "
        "Path('child-paths.json').write_text(json.dumps({'tmp':str(p),"
        "'home':str(Path.home()),'cache':os.environ['UV_CACHE_DIR']})); p.rmdir()"
    )
    assert run_streaming([sys.executable, "-c", code], cwd=run, log_path=run / "launch.log",
                         env={"TMPDIR": "/tmp/override"}) == 0
    observed = json.loads(output.read_text())
    assert Path(observed["tmp"]).parent == Path(expected["TMPDIR"])
    assert observed["home"] == expected["HOME"]
    assert observed["cache"] == expected["UV_CACHE_DIR"]
    assert os.environ["TMPDIR"] == "/tmp/ambient"
    assert os.environ.get("HOME") == parent_home


def test_backend_specific_cache_overrides_cannot_escape_child(host_tmp_path, monkeypatch):
    import sys
    from evaluations.swebench.process import run_streaming
    overrides = ("HF_HUB_CACHE", "HF_ASSETS_CACHE", "HF_MODULES_CACHE",
                 "TRANSFORMERS_CACHE", "TORCH_HOME", "APPTAINER_TMPDIR",
                 "SINGULARITY_TMPDIR", "APPTAINER_CACHEDIR", "SINGULARITY_CACHEDIR")
    for name in overrides:
        monkeypatch.setenv(name, "/tmp/ambient-escape")
    run = host_tmp_path / "backend-child"
    code = ("import json,os; from pathlib import Path; "
            "Path('backend-paths.json').write_text(json.dumps({k:os.environ[k] for k in "
            + repr(overrides) + "}))")
    assert run_streaming([sys.executable, "-c", code], cwd=run,
                         log_path=run / "launcher.log") == 0
    observed = json.loads((run / "backend-paths.json").read_text())
    assert all(Path(value).is_relative_to(run / ".runtime") for value in observed.values())
    assert all(os.environ[name] == "/tmp/ambient-escape" for name in overrides)


def test_log_and_json_writes_reject_host_tmp_before_creating_files(host_tmp_path, monkeypatch):
    from evaluations.swebench import process
    from evaluations.swebench.artifacts import atomic_write_json
    monkeypatch.setattr(process.subprocess, "Popen", lambda *a, **k: pytest.fail("must not dispatch"))
    with pytest.raises(ValueError, match="forbidden"):
        process.run_streaming(["unused"], cwd=host_tmp_path, log_path=Path("/tmp/must-not-write.log"))
    with pytest.raises(ValueError, match="forbidden"):
        atomic_write_json(Path("/tmp/must-not-write.json"), {})


def test_preflight_version_binary_receives_private_home_and_tmp(host_tmp_path, monkeypatch):
    import sys
    from evaluations.swebench import preflight
    binary = host_tmp_path / "version-probe"
    output = host_tmp_path / "probe-paths.json"
    binary.write_text("#!" + sys.executable + "\nimport json,os,tempfile\nfrom pathlib import Path\n"
                      "with tempfile.TemporaryDirectory() as d:\n"
                      "    Path(" + repr(str(output)) + ").write_text(json.dumps({'tmp':d,'home':str(Path.home())}))\n"
                      "print('fixture 1.2.3')\n")
    binary.chmod(0o700)
    monkeypatch.setenv("ROOTLESS_VM_TMPDIR", str(host_tmp_path / "owned"))
    monkeypatch.setenv("TMPDIR", "/tmp/ambient")
    monkeypatch.setenv("HOME", "/tmp/ambient-home")
    assert preflight._tool_version(str(binary))[0] == (1, 2, 3)
    paths = json.loads(output.read_text())
    assert all(Path(value).is_relative_to(host_tmp_path) for value in paths.values())
    assert os.environ["TMPDIR"] == "/tmp/ambient"


def test_low_level_state_cache_and_script_roots_share_storage_policy():
    from rootless_vm.session import _private_state_root
    from rootless_vm.image_cache import _private_dir as cache_root
    from rootless_vm.base_disk import _private_dir as base_root
    from scripts.rootless_terminal_bench_21 import _private_dir as legacy_root
    for check in (_private_state_root, cache_root, legacy_root,
                  lambda path: base_root(path, "base")):
        with pytest.raises(ValueError, match="forbidden"):
            check(Path("/tmp/not-created-by-test"))


def test_all_host_scratch_stays_on_dolphinfs_when_checkout_does(monkeypatch):
    from rootless_vm import host_paths
    monkeypatch.setattr(host_paths, "__file__", "/mnt/your-fs")
    with pytest.raises(ValueError, match="dolphinfs"):
        host_paths.private_temporary_root(owner_root=Path("/opt/off-volume"))


def test_root_execution_is_rejected_before_qemu_probe(monkeypatch, host_tmp_path):
    from rootless_vm.qemu import QemuRuntime, QemuUnavailableError
    from rootless_vm.session import SandboxSpec
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    runtime = QemuRuntime(qemu=Path("/bin/false"))
    monkeypatch.setattr(QemuRuntime, "version", lambda self: pytest.fail("must not start QEMU"))
    with pytest.raises(QemuUnavailableError, match="host root execution"):
        runtime.preflight()
    with pytest.raises(PermissionError, match="host root execution"):
        SandboxSpec(runtime=runtime, state_root=host_tmp_path / "state").validate()
    assert not (host_tmp_path / "state").exists()


def test_legacy_config_stops_before_harbor_on_bad_storage(host_tmp_path, monkeypatch):
    from argparse import Namespace
    from scripts import rootless_terminal_bench_21 as legacy
    config = host_tmp_path / "job-config.json"
    config.write_text(json.dumps({"environment": {
        "import_path": "rootless_vm.harbor_environment:RootlessQemuEnvironment",
        "kwargs": {"state_root": str(host_tmp_path / "state")},
    }}))
    monkeypatch.setattr(legacy, "_validate_config_tasks", lambda path: None)
    def refuse(root):
        raise ValueError("storage unavailable before dispatch")
    monkeypatch.setattr(storage_probe, "require_storage", refuse)
    monkeypatch.setattr(legacy.subprocess, "Popen", lambda *a, **k: pytest.fail("must not dispatch"))
    with pytest.raises(ValueError, match="storage unavailable"):
        legacy.run_config(Namespace(harbor="/bin/false", config=str(config)))
