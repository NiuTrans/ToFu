"""Platform bridge boundaries: Git provenance, admission, failure evidence and CAS.

Only expensive Harbor execution is replaced. Git export, source validation,
payload preparation, SDK job lifecycle and output interpretation run for real.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from evaluations.platform_jobs.collection import inspect_collected_outputs
from evaluations.platform_jobs.contract import canonical, compile_request, digest, validate_request
from evaluations.platform_jobs.payload import export_committed_sources, plan_on_platform, prepare_platform_job
from evaluations.platform_jobs.worker import WORKER_OUTPUT_DIRECTORY, run_worker
from evaluations.source_identity import REQUIRED_SOURCES, verify_source_receipt
from tofu_environments import Actor
from tofu_environments.contracts import EnvironmentError
from tofu_environments.jobs import JobService
from tofu_environments.repository import MemoryRepository

pytestmark = pytest.mark.unit
ARGUMENTS = ["--benchmark", "terminal-bench-2.1", "--agent", "oracle", "--model", "oracle",
             "--backend", "docker", "--attempts", "1"]


def git(root, *arguments):
    return subprocess.run(["git", "-C", str(root), *arguments], check=True,
                          capture_output=True).stdout.decode().strip()


@pytest.fixture
def committed_source(host_tmp_path):
    repository = host_tmp_path / "repository"
    repository.mkdir()
    for name in (*REQUIRED_SOURCES, "evaluations/platform_jobs/worker.py",
                 "packages/tofu-environments/tofu_environments/job_runner.py"):
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('"""Retained fixture source."""\n')
    private = repository / "data" / "credentials.json"
    private.parent.mkdir()
    private.write_text('{"secret":"excluded-user-data"}')
    git(repository, "init")
    git(repository, "add", ".")
    git(repository, "-c", "user.name=Evaluation fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-m", "fixture")
    return repository, git(repository, "rev-parse", "HEAD")


def export_worker(host_tmp_path, committed_source):
    tmp_repository, revision = committed_source
    files, proof = export_committed_sources(tmp_repository, revision, byte_limit=1048576, file_limit=128)
    worker = host_tmp_path / "worker"
    worker.mkdir()
    for name, raw in files.items():
        path = worker / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    (worker / "payload-source.json").write_bytes(canonical(proof))
    request = compile_request(ARGUMENTS, run_id="platform-fixture", runner_revision=revision,
                              runtime_seconds=60, result_bytes=1048576, console_bytes=65536)
    (worker / "platform-evaluation.json").write_bytes(canonical(request))
    return worker, request


def prepare(host_tmp_path, committed_source):
    tmp_repository, revision = committed_source
    actor = Actor(owner_id=12, tenant_id="fixture", project_root=str(host_tmp_path))
    registration = prepare_platform_job(actor, repository=tmp_repository, revision=revision,
        arguments=ARGUMENTS, run_id="prepared-fixture", output_root=host_tmp_path / "prepared",
        project="test-project", resources={"cpu": 2, "memory_gib": 4, "gpu_count": 0},
        provider_options={"queue": "test-queue"}, runtime_seconds=60, result_bytes=1048576,
        source_byte_limit=1048576, source_file_limit=128)
    return actor, registration


def test_payload_uses_committed_sources_and_sdk_without_user_data(host_tmp_path, committed_source):
    actor, registration = prepare(host_tmp_path, committed_source)
    assert registration["submission"] == "not_submitted"
    assert registration["scope"] == actor.scope
    archive = host_tmp_path / "prepared/prepared-fixture/worker-source.zip"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == registration["spec"]["source"]["sha256"]
    with zipfile.ZipFile(archive) as bundle:
        assert "tofu_environments/job_runner.py" in bundle.namelist()
        assert not any(name.startswith("data/") for name in bundle.namelist())
    with pytest.raises(FileExistsError):
        prepare(host_tmp_path, committed_source)


def test_source_proof_works_without_git_directory(host_tmp_path, committed_source):
    worker, request = export_worker(host_tmp_path, committed_source)
    identity = verify_source_receipt(worker, worker / "payload-source.json",
                                     expected_revision=request["runner_revision"])
    assert identity["revision"] == committed_source[1]
    assert identity["file_count"] == 6
    assert not (worker / ".git").exists()
    from evaluations.swebench.artifacts import output_guard_status
    output = worker / WORKER_OUTPUT_DIRECTORY
    assert output_guard_status(output, worker)[0] is True
    output.mkdir(parents=True)
    (output / "trial.json").write_text('{"reward":0}')
    assert verify_source_receipt(worker, worker / "payload-source.json") == identity


@pytest.mark.parametrize("mutation", ["source", "unexpected", "symlink", "tree", "revision", "mapping"])
def test_source_proof_rejects_drift(host_tmp_path, committed_source, mutation):
    worker, request = export_worker(host_tmp_path, committed_source)
    receipt = worker / "payload-source.json"
    if mutation == "source":
        (worker / REQUIRED_SOURCES[0]).write_text("changed")
    elif mutation == "unexpected":
        (worker / "injected.py").write_text("changed")
    elif mutation == "symlink":
        (worker / "linked.py").symlink_to(worker / REQUIRED_SOURCES[0])
    else:
        proof = json.loads(receipt.read_text())
        if mutation == "tree":
            proof["trees"][next(iter(proof["trees"]))] = "Y2hhbmdlZA=="
        elif mutation == "revision":
            proof["revision"] = "0" * 40
        else:
            proof["files"][0]["repository_path"] = "../private.py"
        receipt.write_bytes(canonical(proof))
    with pytest.raises((ValueError, KeyError)):
        verify_source_receipt(worker, receipt, expected_revision=request["runner_revision"])


@pytest.mark.parametrize("extra", [["--max-retries", "1"], ["--run-id", "other"],
    ["--harbor-bin", "/untrusted"], ["--task", "unknown-task"], ["--limit", "1"],
    ["--model", "https://private.example/key"], ["--model", "oracle"], ["--attempts", "0"]])
def test_request_rejects_invalid_or_attempt_erasing_arguments(extra):
    with pytest.raises(ValueError):
        compile_request(ARGUMENTS + extra, run_id="fixture", runner_revision="1" * 40,
                        runtime_seconds=60, result_bytes=1048576, console_bytes=65536)


def test_request_rechecks_pinned_task_and_engine_identity():
    request = compile_request(ARGUMENTS, run_id="fixture", runner_revision="1" * 40,
                              runtime_seconds=60, result_bytes=1048576, console_bytes=65536)
    assert request["arguments"][-2:] == ["--max-retries", "0"]
    for field in ("task_lock_sha256", "harbor_commit"):
        changed = {**request, field: "0" * len(request[field])}
        with pytest.raises(ValueError, match="drifted"):
            validate_request(changed)
    changed = {**request, "credential_env_names": ["UNDECLARED_SECRET"]}
    with pytest.raises(ValueError, match="drifted"):
        validate_request(changed)


def test_new_suites_cannot_claim_rootless_separate_verifier():
    args = ["--benchmark", "deepswe-v1.1", "--agent", "oracle", "--model", "oracle",
            "--backend", "rootless-qemu"]
    with pytest.raises(ValueError, match="separate-verifier"):
        compile_request(args, run_id="fixture", runner_revision="1" * 40,
                        runtime_seconds=60, result_bytes=1048576, console_bytes=65536)


class PlatformProvider:
    def __init__(self):
        self.submissions = 0

    def validate_job(self, actor, spec, *, operation_id):
        return {"queue": spec["provider_options"]["queue"], "scope": actor.scope}

    def submit_job(self, actor, spec, operation_id, admission):
        self.submissions += 1
        raise EnvironmentError("transport_timeout", "lost acknowledgement", uncertain=True)

    def reconcile_job(self, actor, record):
        return None


def test_platform_reuses_exact_approval_and_uncertain_submission_cas(host_tmp_path, committed_source):
    actor, registration = prepare(host_tmp_path, committed_source)
    provider = PlatformProvider()
    service = JobService(MemoryRepository(), lambda actor, name: provider)
    plan = plan_on_platform(service, actor, registration, provider="fixture-platform")
    assert provider.submissions == 0
    with pytest.raises(EnvironmentError, match="authorization"):
        service.submit(actor, plan["operation_id"], plan["plan_hash"], lambda *_: False)
    first = service.submit(actor, plan["operation_id"], plan["plan_hash"], lambda *_: True)
    assert first["phase"] == "uncertain"
    service.submit(actor, plan["operation_id"], plan["plan_hash"], lambda *_: True)
    service.status(actor, plan["operation_id"])
    assert provider.submissions == 1
    other = Actor(owner_id=13, tenant_id="fixture", project_root=str(host_tmp_path))
    with pytest.raises(ValueError, match="another actor"):
        plan_on_platform(service, other, registration, provider="fixture-platform")
    registration["spec"]["resources"]["cpu"] = 8
    with pytest.raises(ValueError, match="specification changed"):
        plan_on_platform(service, actor, registration, provider="fixture-platform")


@pytest.mark.parametrize("doctor_code,benchmark_code", [(1, 0), (0, 1), (0, 0)])
def test_worker_preserves_failed_evidence_without_inventing_score(host_tmp_path, committed_source,
                                                               doctor_code, benchmark_code):
    from tofu_environments.job_runner import run_job
    worker, request = export_worker(host_tmp_path, committed_source)
    calls = []

    def dispatch(command, workspace, seconds, output_bytes):
        phase = "doctor" if "doctor" in command else "run"
        calls.append(phase)
        code = doctor_code if phase == "doctor" else benchmark_code
        script = ("from pathlib import Path; import sys; "
                  "Path('.eval-runs/benchmarks').mkdir(parents=True, exist_ok=True); "
                  f"Path('.eval-runs/benchmarks/{phase}.json').write_text('{{\"phase\":\"{phase}\"}}'); "
                  f"print('retained {phase} diagnostics'); sys.exit({code})")
        return run_job({"command": [sys.executable, "-c", script], "outputs": [],
                        "limits": {"runtime_seconds": seconds, "result_bytes": output_bytes}}, workspace)

    report = run_worker(request, worker, dispatch=dispatch,
                        engine_identity=lambda expected: {"version": "0.21.0", "source_commit": expected})
    expected = "preflight_failed" if doctor_code else "benchmark_failed" if benchmark_code else "completed"
    assert report["benchmark_state"] == expected
    assert calls == (["doctor"] if doctor_code else ["doctor", "run"])
    assert report["evidence_complete"] is True
    assert report["formal_score"] is None
    with zipfile.ZipFile(worker / "platform-evidence.zip") as bundle:
        assert ".eval-runs/benchmarks/doctor.json" in bundle.namelist()
        if not doctor_code:
            assert ".eval-runs/benchmarks/run.json" in bundle.namelist()
    registration = {"request": request}
    interpreted = inspect_collected_outputs(worker, registration)
    assert interpreted["benchmark_state"] == expected
    assert interpreted["native_audit_required"] is True
    assert interpreted["formal_score"] is None
    with (worker / "platform-evidence.zip").open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError, match="size mismatch"):
        inspect_collected_outputs(worker, registration)


def test_worker_rejects_source_drift_before_preflight_or_model(host_tmp_path, committed_source):
    worker, request = export_worker(host_tmp_path, committed_source)
    (worker / REQUIRED_SOURCES[0]).write_text("changed")
    calls = []
    report = run_worker(request, worker, dispatch=lambda *args: calls.append(args))
    assert calls == []
    assert report["benchmark_state"] == "preflight_failed"
    assert report["failure_type"] == "ValueError"


@pytest.mark.parametrize("failure", ["secret", "size", "symlink"])
def test_worker_does_not_publish_unsafe_or_truncated_evidence(host_tmp_path, committed_source, monkeypatch, failure):
    worker, request = export_worker(host_tmp_path, committed_source)
    request["arguments"] += ["--secret-env", "FIXTURE_API_KEY"]
    request["credential_env_names"] = ["FIXTURE_API_KEY"]
    monkeypatch.setenv("FIXTURE_API_KEY", "credential-must-never-publish")
    output = worker / WORKER_OUTPUT_DIRECTORY
    output.mkdir(parents=True)
    if failure == "secret":
        (output / "trace.json").write_text("credential-must-never-publish")
    elif failure == "size":
        (output / "trace.json").write_bytes(b"x" * (request["limits"]["result_bytes"] + 1))
    else:
        (output / "trace.json").symlink_to(worker / REQUIRED_SOURCES[0])
    report = run_worker(request, worker, dispatch=lambda *args: {"state": "failed", "exit_code": 1},
                        engine_identity=lambda expected: {"source_commit": expected})
    assert report["benchmark_state"] == "evidence_failed"
    assert report["evidence_complete"] is False
    with zipfile.ZipFile(worker / "platform-evidence.zip") as bundle:
        assert bundle.namelist() == []
    assert b"credential-must-never-publish" not in (worker / "platform-result.json").read_bytes()


def test_runner_resume_reverifies_exported_source(host_tmp_path, committed_source, monkeypatch):
    from evaluations.swebench import harbor_runner
    tmp_path_worker, request = export_worker(host_tmp_path, committed_source)
    monkeypatch.setattr(harbor_runner, "PROJECT_ROOT", tmp_path_worker)
    identity = verify_source_receipt(tmp_path_worker, tmp_path_worker / "payload-source.json")
    manifest = {"source_receipt": str(tmp_path_worker / "payload-source.json"), "source_identity": identity,
                "project_revision": request["runner_revision"]}
    assert harbor_runner._resume_source_matches(manifest, exclusions=()) is True
    (tmp_path_worker / "lib/version.py").write_text("changed")
    with pytest.raises(ValueError, match="committed blob"):
        harbor_runner._resume_source_matches(manifest, exclusions=())


def test_source_budget_is_enforced_before_blob_export(committed_source):
    tmp_repository, revision = committed_source
    for kwargs in ({"byte_limit": 1, "file_limit": 128}, {"byte_limit": 1048576, "file_limit": 1}):
        with pytest.raises(ValueError, match="budget"):
            export_committed_sources(tmp_repository, revision, **kwargs)


def test_worker_deadline_exhaustion_never_dispatches_model(host_tmp_path, committed_source, monkeypatch):
    from evaluations.platform_jobs import worker as module
    worker, request = export_worker(host_tmp_path, committed_source)
    calls = []
    ticks = iter([100.0, 160.0, 160.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks, 160.0))
    report = run_worker(request, worker, dispatch=lambda *args: calls.append(args),
                        engine_identity=lambda expected: {"source_commit": expected})
    assert calls == []
    assert report["benchmark_state"] == "preflight_failed"
    assert report["failure_type"] == "TimeoutError"


def test_collection_rejects_another_request_even_with_valid_zip(host_tmp_path, committed_source):
    worker, request = export_worker(host_tmp_path, committed_source)
    run_worker(request, worker, dispatch=lambda *args: {"state": "failed", "exit_code": 1},
               engine_identity=lambda expected: {"source_commit": expected})
    other = {**request, "run_id": "other-fixture"}
    with pytest.raises(ValueError, match="registered benchmark"):
        inspect_collected_outputs(worker, {"request": other})


def test_failed_doctor_without_created_output_directory_still_publishes_logs(host_tmp_path, committed_source):
    worker, request = export_worker(host_tmp_path, committed_source)
    report = run_worker(request, worker,
        dispatch=lambda *args: {"state": "failed", "exit_code": 2, "stdout": "missing backend"},
        engine_identity=lambda expected: {"source_commit": expected})
    assert report["preflight_exit_code"] == 2
    assert report["failure_type"] is None
    assert report["evidence_complete"] is True
    with zipfile.ZipFile(worker / "platform-evidence.zip") as bundle:
        assert bundle.read(WORKER_OUTPUT_DIRECTORY + "/platform-doctor.stdout") == b"missing backend"
