"""Formal launcher contracts for the paired Codex 0.149.1 × Kimi arm."""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import sys
import threading
import urllib.request
from copy import deepcopy
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from evaluations.codex_kimi_proxy.server import ProxyConfig
from evaluations.codex_kimi_proxy.codex_contract import benchmark_trial_token
from evaluations.codex_kimi_proxy.supervisor import CodexKimiProxySupervisor
from evaluations.long_agent_release.run_store import (
    audit_release_attempts,
    initialize_release_run,
)
from evaluations.swebench import harbor_runner
from evaluations.swebench.codex_kimi_runtime import (
    CODEX_KIMI_AGENT,
    CodexKimiBaselineSettings,
)
from evaluations.swebench.constants import BENCHMARKS
from evaluations.swebench.audit import audit_run
from evaluations.swebench.harbor_runner import (
    HarborRunSpec,
    _validated_resume_codex_runtime,
    resume_harbor_run,
    start_harbor_run,
)
from evaluations.swebench.process import run_streaming
from evaluations.swebench.rootless_qemu import RootlessQemuSettings
from lib.benchmark_contract import build_manifest_v2


pytestmark = pytest.mark.unit
_TASK = "swe-bench/django__django-11099"


def _executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _runtime_fixture(
    tmp_path: Path,
) -> tuple[RootlessQemuSettings, CodexKimiBaselineSettings]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    qemu = _executable(runtime / "qemu-system-x86_64", "exit 0\n")
    qemu_img = _executable(runtime / "qemu-img", "exit 0\n")
    base = tmp_path / "base.qcow2"
    base.write_bytes(b"trusted-base")
    store = tmp_path / "images"
    store.mkdir(mode=0o700)
    definition = BENCHMARKS["swebench-verified"]
    (store / "index.json").write_text(json.dumps({
        "schema": 1,
        "benchmark": definition.key,
        "dataset": definition.dataset,
        "dataset_source_revision": definition.dataset_source_revision,
        "task_count": definition.task_count,
        "images": {"fixture-image": {"tasks": [_TASK]}},
    }), encoding="utf-8")
    codex = _executable(
        tmp_path / "codex-0.149.1",
        "test -z \"${KIMI_API_KEY+x}\" || exit 91\n"
        "printf '%s\\n' 'codex-cli 0.149.1'\n",
    )
    codex_digest = hashlib.sha256(codex.read_bytes()).hexdigest()
    return (
        RootlessQemuSettings(
            base_disk=base,
            image_store=store,
            qemu_path=qemu,
            qemu_img_path=qemu_img,
        ),
        CodexKimiBaselineSettings(
            codex_binary=codex,
            codex_sha256=codex_digest,
            provider_face="meituan-chat",
            provider_slot_id="kimi-slot-fixture",
        ),
    )


def _spec(
    tmp_path: Path,
    rootless: RootlessQemuSettings,
    codex: CodexKimiBaselineSettings,
) -> HarborRunSpec:
    harbor = _executable(
        tmp_path / "harbor-fixture",
        "test -z \"${KIMI_API_KEY+x}\" || exit 92\n"
        "printf '%s\\n' 'harbor fixture'\n",
    )
    return HarborRunSpec(
        agent=CODEX_KIMI_AGENT,
        models=("kimi-k3",),
        backend="rootless-qemu",
        output_root=tmp_path / "evals",
        task_ids=(_TASK,),
        concurrency=1,
        agent_concurrency=1,
        max_retries=0,
        reasoning_effort="high",
        agent_version="0.149.1",
        harbor_bin=str(harbor),
        rootless_qemu=rootless,
        codex_kimi=codex,
    )


def test_formal_dry_run_is_secret_free_and_round_trips_runtime(
    tmp_path, monkeypatch
):
    rootless, codex = _runtime_fixture(tmp_path)
    monkeypatch.setenv("KIMI_CHAT_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("KIMI_API_KEY", "formal-secret-value-never-persist")

    code, run_dir = start_harbor_run(_spec(tmp_path, rootless, codex), dry_run=True)

    assert code == 0
    config = json.loads((run_dir / "job-config.json").read_text())
    manifest = json.loads((run_dir / "manifest.json").read_text())
    serialized = json.dumps({"config": config, "manifest": manifest})
    assert "formal-secret-value-never-persist" not in serialized
    assert "http://127.0.0.1:9" not in serialized
    assert manifest["host_only_secret_env_names"] == [
        "KIMI_CHAT_BASE_URL", "KIMI_API_KEY"
    ]
    assert manifest["codex_kimi_runtime"]["credentialBoundary"] \
        == "launcher-host-only"
    assert manifest["provider_face"] == "meituan-chat"
    assert manifest["provider_slot_id"] == "kimi-slot-fixture"
    assert len(manifest["harbor_binary_sha256"]) == 64
    agent = config["agents"][0]
    assert agent["name"] == CODEX_KIMI_AGENT
    assert agent["model_name"] == "kimi-k3"
    assert "env" not in agent
    routes = config["environment"]["kwargs"]["loopback_service_forwards"]
    assert len(routes) == 1
    assert routes[0]["guest_host"] == "10.0.2.101"
    assert routes[0]["guest_port"] == 8765
    parsed = RootlessQemuSettings.from_environment_kwargs(
        config["environment"]["kwargs"]
    )
    resolved = _validated_resume_codex_runtime(
        run_dir=run_dir,
        manifest=manifest,
        config=config,
        rootless_qemu=parsed,
    )
    assert resolved is not None
    assert resolved[1] == routes[0]["host_port"]
    audit = audit_run(run_dir)
    checks = {row["name"]: row for row in audit["checks"]}
    assert checks["codex_kimi_runtime_manifest"]["ok"] is True
    assert checks["codex_kimi_agent_pin"]["ok"] is True
    assert checks["codex_kimi_single_control_route"]["ok"] is True
    assert checks["codex_kimi_no_credential_persistence"]["ok"] is True
    assert checks["codex_kimi_private_metrics_repository"]["ok"] is True
    assert checks["job_result"]["ok"] is False

    leaked = deepcopy(config)
    leaked["credentialProbe"] = "formal-secret-value-never-persist"
    with pytest.raises(ValueError, match="leaked"):
        _validated_resume_codex_runtime(
            run_dir=run_dir,
            manifest=manifest,
            config=leaked,
            rootless_qemu=parsed,
        )


def test_formal_launcher_owns_live_proxy_and_excludes_credentials(
    tmp_path, monkeypatch
):
    rootless, codex = _runtime_fixture(tmp_path)
    monkeypatch.setenv("KIMI_CHAT_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("KIMI_API_KEY", "live-secret-value-never-child")
    monkeypatch.setattr(harbor_runner, "_git_dirty", lambda **_kwargs: False)
    monkeypatch.setattr(
        harbor_runner, "_git_revision", lambda **_kwargs: "clean-fixture-revision"
    )
    observed: dict[str, object] = {}

    def fake_run_streaming(command, *, cwd, log_path, env, unset_env):
        config = json.loads((cwd / "job-config.json").read_text())
        route = config["environment"]["kwargs"]["loopback_service_forwards"][0]
        port = int(route["host_port"])
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/v1/models", timeout=2
        ) as response:
            assert response.status == 200
            assert json.loads(response.read())["data"][0]["id"] == "kimi-k3"
        observed.update({
            "port": port,
            "unset": tuple(unset_env),
            "command": list(command),
        })
        return 17

    monkeypatch.setattr(harbor_runner, "run_streaming", fake_run_streaming)
    code, run_dir = start_harbor_run(_spec(tmp_path, rootless, codex))

    assert code == 17
    assert observed["unset"] == ("KIMI_CHAT_BASE_URL", "KIMI_API_KEY")
    assert socket.socket().connect_ex(("127.0.0.1", int(observed["port"]))) != 0
    serialized = (run_dir / "job-config.json").read_text() \
        + (run_dir / "manifest.json").read_text() \
        + (run_dir / "command.json").read_text()
    assert "live-secret-value-never-child" not in serialized
    assert "http://127.0.0.1:9" not in serialized
    assert repr(ProxyConfig(
        upstream_base_url="http://127.0.0.1:9",
        upstream_api_key="repr-secret",
    )).find("repr-secret") == -1


def test_formal_release_claim_exists_before_harbor_dispatch(
    tmp_path, monkeypatch,
):
    rootless, codex = _runtime_fixture(tmp_path)
    monkeypatch.setenv("KIMI_CHAT_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("KIMI_API_KEY", "tracked-secret-never-child")
    monkeypatch.setattr(harbor_runner, "_git_dirty", lambda **_kwargs: False)
    monkeypatch.setattr(
        harbor_runner, "_git_revision", lambda **_kwargs: "tracked-revision"
    )
    identity_spec = HarborRunSpec(**{
        **_spec(tmp_path, rootless, codex).__dict__,
        "run_id": "tracking-identity",
    })
    _, identity_dir = start_harbor_run(identity_spec, dry_run=True)
    identity = json.loads((identity_dir / "manifest.json").read_text())
    release_manifest = build_manifest_v2(
        run_id="release-baseline", harness=identity["harness_identity"],
        agent={
            "name": "codex", "version": "0.149.1",
            "binarySha256": codex.codex_sha256,
        },
        provider_face="meituan-chat",
        provider_slot_id="kimi-slot-fixture", thinking="high",
        experiment_arm="codex_0_149_1", pair_id="pair-tracked",
        comparison_role="baseline",
        tool_permissions={"profile": "frozen-read-write"},
        prompt_digest=hashlib.sha256(b"prompt").hexdigest(),
        tool_schema_digest=hashlib.sha256(b"tools").hexdigest(),
        dataset_snapshot={
            "id": "tracked-pilot",
            "sha256": hashlib.sha256(b"dataset").hexdigest(),
            "frozen": True,
        },
        task_table=[{
            "taskId": "swe-bench-verified:django__django-11099",
            "family": "software_engineering",
            "dataset": "swe-bench-verified",
            "sourceTaskId": _TASK,
            "sourceSha256": hashlib.sha256(b"task").hexdigest(),
            "trialIndex": 1,
        }],
        sandbox=identity["sandbox_identity"],
        retry_rule={
            "maxInfrastructureRetries": 1,
            "retryableFailureClasses": ["infrastructure"],
        },
        artifact_limits={
            "maximumArtifactBytes": 1_000_000,
            "maximumTaskArtifactBytes": 2_000_000,
            "maximumRunArtifactBytes": 10_000_000,
        },
        timeout_seconds=3600,
        maximum_infrastructure_failure_rate=0.02,
    )
    release_root = tmp_path / "release-store"
    initialize_release_run(release_root, release_manifest)
    observed = {}

    def fake_run_streaming(*_args, cwd, **_kwargs):
        attempt = audit_release_attempts(release_root)
        manifest = json.loads((cwd / "manifest.json").read_text())
        observed.update({"attempt": attempt, "manifest": manifest})
        return 17

    monkeypatch.setattr(harbor_runner, "run_streaming", fake_run_streaming)
    tracked_spec = HarborRunSpec(**{
        **_spec(tmp_path, rootless, codex).__dict__,
        "run_id": "tracked-dispatch",
        "release_run_root": release_root,
    })
    code, tracked_run_dir = start_harbor_run(tracked_spec)

    assert code == 17
    assert observed["attempt"]["totalAttempts"] == 1
    assert observed["attempt"]["openAttempts"] == 1
    assert observed["manifest"]["release_evidence_eligible"] is True
    assert observed["manifest"]["release_attempt_tracking"]["taskCount"] == 1
    with pytest.raises(ValueError, match="requires --release-run-root"):
        resume_harbor_run(
            tracked_run_dir,
            harbor_bin=observed["manifest"]["harbor_binary"],
        )


def test_run_streaming_removes_explicit_host_only_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "must-not-reach-child")
    log = tmp_path / "child.log"

    code = run_streaming(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('KIMI_API_KEY', 'absent'))",
        ],
        cwd=tmp_path,
        log_path=log,
        unset_env=("KIMI_API_KEY",),
    )

    assert code == 0
    assert log.read_text().strip() == "absent"


def test_resume_restarts_exact_proxy_without_exposing_host_credentials(
    tmp_path, monkeypatch
):
    rootless, codex = _runtime_fixture(tmp_path)
    monkeypatch.setenv("KIMI_CHAT_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("KIMI_API_KEY", "resume-secret-never-child")
    monkeypatch.setattr(harbor_runner, "_git_dirty", lambda **_kwargs: False)
    monkeypatch.setattr(
        harbor_runner, "_git_revision", lambda **_kwargs: "clean-fixture-revision"
    )
    _, run_dir = start_harbor_run(_spec(tmp_path, rootless, codex), dry_run=True)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    (run_dir / "jobs" / manifest["run_id"]).mkdir(parents=True)
    observed: dict[str, object] = {}

    from evaluations.swebench import preflight

    monkeypatch.setattr(preflight, "harbor_checks", lambda **_kwargs: [])

    def fake_resume(command, *, cwd, log_path, env, unset_env):
        port = manifest["codex_kimi_runtime"]["listenPort"]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/models", timeout=2
        ) as response:
            assert response.status == 200
        observed["unset"] = tuple(unset_env)
        return 19

    monkeypatch.setattr(harbor_runner, "run_streaming", fake_resume)

    assert resume_harbor_run(
        run_dir, harbor_bin=manifest["harbor_binary"]
    ) == 19
    assert observed["unset"] == ("KIMI_CHAT_BASE_URL", "KIMI_API_KEY")
    assert "resume-secret-never-child" not in (
        (run_dir / "manifest.json").read_text()
        + (run_dir / "job-config.json").read_text()
    )


def test_formal_agent_fails_closed_without_complete_pins(tmp_path):
    rootless, codex = _runtime_fixture(tmp_path)
    missing = _spec(tmp_path, rootless, codex)
    missing = HarborRunSpec(**{
        **missing.__dict__,
        "codex_kimi": None,
    })
    with pytest.raises(ValueError, match="pinned runtime"):
        missing.validate()

    wrong_model = HarborRunSpec(**{
        **_spec(tmp_path, rootless, codex).__dict__,
        "models": ("not-kimi",),
    })
    with pytest.raises(ValueError, match="exactly model"):
        wrong_model.validate()

    hidden_retries = HarborRunSpec(**{
        **_spec(tmp_path, rootless, codex).__dict__,
        "max_retries": 1,
    })
    with pytest.raises(ValueError, match="evidence-erasing"):
        hidden_retries.validate()


def test_paid_formal_run_rejects_dirty_runner_before_creating_artifacts(
    tmp_path, monkeypatch,
):
    rootless, codex = _runtime_fixture(tmp_path)
    spec = _spec(tmp_path, rootless, codex)
    monkeypatch.setenv("KIMI_CHAT_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("KIMI_API_KEY", "host-only-fixture")
    monkeypatch.setattr(harbor_runner, "_git_dirty", lambda **_kwargs: True)

    with pytest.raises(ValueError, match="clean pinned"):
        start_harbor_run(spec)

    assert not spec.output_root.exists()


def test_launcher_exception_closes_proxy_and_records_nonterminal_failure(
    tmp_path, monkeypatch,
):
    rootless, codex = _runtime_fixture(tmp_path)
    base = _spec(tmp_path, rootless, codex)
    spec = HarborRunSpec(**{**base.__dict__, "run_id": "launcher-failure"})
    monkeypatch.setenv("KIMI_CHAT_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("KIMI_API_KEY", "launcher-failure-secret")
    monkeypatch.setattr(harbor_runner, "_git_dirty", lambda **_kwargs: False)
    monkeypatch.setattr(
        harbor_runner, "_git_revision", lambda **_kwargs: "clean-fixture-revision"
    )

    def fail_streaming(*_args, **_kwargs):
        raise RuntimeError("synthetic launcher failure")

    monkeypatch.setattr(harbor_runner, "run_streaming", fail_streaming)
    with pytest.raises(RuntimeError, match="synthetic launcher failure"):
        start_harbor_run(spec)

    run_dir = spec.output_root / "launcher-failure"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    port = manifest["codex_kimi_runtime"]["listenPort"]
    assert manifest["status"] == "launcher_failed"
    assert manifest["failure_type"] == "RuntimeError"
    assert manifest["exit_code"] is None
    assert socket.socket().connect_ex(("127.0.0.1", port)) != 0
    assert "launcher-failure-secret" not in json.dumps(manifest)


def test_resume_proxy_start_failure_is_recoverable_and_not_running(
    tmp_path, monkeypatch,
):
    rootless, codex = _runtime_fixture(tmp_path)
    monkeypatch.setenv("KIMI_CHAT_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("KIMI_API_KEY", "resume-start-failure-secret")
    monkeypatch.setattr(harbor_runner, "_git_dirty", lambda **_kwargs: False)
    monkeypatch.setattr(
        harbor_runner, "_git_revision", lambda **_kwargs: "clean-fixture-revision"
    )
    _, run_dir = start_harbor_run(
        _spec(tmp_path, rootless, codex), dry_run=True
    )
    manifest = json.loads((run_dir / "manifest.json").read_text())
    (run_dir / "jobs" / manifest["run_id"]).mkdir(parents=True)
    from evaluations.swebench import preflight

    monkeypatch.setattr(preflight, "harbor_checks", lambda **_kwargs: [])

    def fail_proxy_start(self):
        raise RuntimeError("synthetic bind failure")

    monkeypatch.setattr(CodexKimiProxySupervisor, "start", fail_proxy_start)
    with pytest.raises(RuntimeError, match="synthetic bind failure"):
        resume_harbor_run(
            run_dir, harbor_bin=manifest["harbor_binary"]
        )

    failed = json.loads((run_dir / "manifest.json").read_text())
    assert failed["status"] == "resume_failed"
    assert failed["failure_type"] == "RuntimeError"
    assert failed["exit_code"] is None
    assert "resume-start-failure-secret" not in json.dumps(failed)


def test_proxy_ignores_ambient_http_proxy_and_redacts_upstream_key(
    tmp_path, monkeypatch
):
    key = "upstream-key-must-never-return"

    class ErrorUpstream(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            payload = f"upstream echoed {key}".encode()
            self.send_response(401)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), ErrorUpstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    metrics = tmp_path / "metrics"
    metrics.mkdir(mode=0o700)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    config = ProxyConfig(
        upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
        upstream_api_key=key,
        trial_metrics_dir=str(metrics),
        require_trial_header=True,
    )
    try:
        with CodexKimiProxySupervisor(config) as proxy:
            connection = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=3)
            body = json.dumps({"model": "kimi-k3", "input": "hello"})
            connection.request(
                "POST",
                "/v1/responses",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Tofu-Benchmark-Trial": benchmark_trial_token("redaction"),
                },
            )
            response = connection.getresponse()
            payload = response.read().decode()
            connection.close()
            assert response.status == 401
            assert key not in payload
            assert "redacted-benchmark-credential" in payload
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)
