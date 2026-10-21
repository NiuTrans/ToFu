"""Offline contract for the pinned OpenCode formal benchmark arm."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from evaluations.codex_kimi_proxy.codex_contract import (
    CodexContractError,
    benchmark_trial_token,
    write_trial_proxy_metrics,
)
from evaluations.opencode_contract import (
    OPENCODE_PROVIDER_NPM_VERSION,
    OPENCODE_VERSION,
)
from evaluations.swebench.audit import (
    _opencode_trial_evidence_checks,
    audit_run,
)
from evaluations.swebench.constants import BENCHMARKS
from evaluations.swebench.harbor_runner import (
    HarborRunSpec,
    _validated_resume_opencode_runtime,
    start_harbor_run,
)
from evaluations.swebench.opencode_runtime import (
    OPENCODE_AGENT,
    OpenCodeBaselineSettings,
)
from evaluations.swebench.rootless_qemu import RootlessQemuSettings

pytestmark = pytest.mark.unit
_TASK = "swe-bench/django__django-11099"
_MODEL = "glm-5.3-m17"


def _executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _opencode_fixture(host_tmp_path: Path) -> tuple[Path, str, Path]:
    binary = _executable(
        host_tmp_path / "opencode-fixture",
        f"printf '%s\\n' 'opencode {OPENCODE_VERSION}'\n",
    )
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    seed = host_tmp_path / "cache-seed"
    marker = seed / "node_modules" / "@ai-sdk" / "openai-compatible"
    marker.mkdir(parents=True, mode=0o700)
    (marker / "package.json").write_text(json.dumps({
        "name": "@ai-sdk/openai-compatible",
        "version": OPENCODE_PROVIDER_NPM_VERSION,
    }), encoding="utf-8")
    return binary, digest, seed


def _settings(host_tmp_path: Path) -> OpenCodeBaselineSettings:
    binary, digest, seed = _opencode_fixture(host_tmp_path)
    return OpenCodeBaselineSettings(
        opencode_binary=binary,
        opencode_sha256=digest,
        opencode_cache_seed=seed,
        model=_MODEL,
        provider_face="yourprovider-chat",
        provider_slot_id="glm-slot-fixture",
    )


def _runtime_fixture(
    host_tmp_path: Path,
) -> tuple[RootlessQemuSettings, OpenCodeBaselineSettings]:
    runtime = host_tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    qemu = _executable(runtime / "qemu-system-x86_64", "exit 0\n")
    qemu_img = _executable(runtime / "qemu-img", "exit 0\n")
    base = host_tmp_path / "base.qcow2"
    base.write_bytes(b"trusted-base")
    store = host_tmp_path / "images"
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
    return (
        RootlessQemuSettings(
            base_disk=base,
            image_store=store,
            qemu_path=qemu,
            qemu_img_path=qemu_img,
        ),
        _settings(host_tmp_path),
    )


def _spec(
    host_tmp_path: Path,
    rootless: RootlessQemuSettings,
    opencode: OpenCodeBaselineSettings,
    **overrides: object,
) -> HarborRunSpec:
    harbor = _executable(
        host_tmp_path / "harbor-fixture",
        "test -z \"${GLM_API_KEY+x}\" || exit 92\n"
        "printf '%s\\n' 'harbor fixture'\n",
    )
    fields: dict[str, object] = {
        "agent": OPENCODE_AGENT,
        "models": (_MODEL,),
        "backend": "rootless-qemu",
        "output_root": host_tmp_path / "evals",
        "task_ids": (_TASK,),
        "concurrency": 1,
        "agent_concurrency": 1,
        "max_retries": 0,
        "agent_version": OPENCODE_VERSION,
        "harbor_bin": str(harbor),
        "rootless_qemu": rootless,
        "opencode": opencode,
    }
    fields.update(overrides)
    return HarborRunSpec(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize('deadline', [None, 21600])
def test_formal_dry_run_is_secret_free_and_round_trips_runtime(
    host_tmp_path, monkeypatch, deadline,
):
    rootless, opencode = _runtime_fixture(host_tmp_path)
    if deadline is not None:
        opencode = replace(opencode, agent_timeout_seconds=deadline, output_tokens=65536)
    monkeypatch.setenv("GLM_CHAT_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("GLM_API_KEY", "formal-secret-value-never-persist")

    code, run_dir = start_harbor_run(
        _spec(host_tmp_path, rootless, opencode,
              agent_timeout_override_seconds=deadline), dry_run=True)

    assert code == 0
    config = json.loads((run_dir / "job-config.json").read_text())
    manifest = json.loads((run_dir / "manifest.json").read_text())
    serialized = json.dumps({"config": config, "manifest": manifest})
    assert "formal-secret-value-never-persist" not in serialized
    assert "http://127.0.0.1:9" not in serialized
    assert manifest["host_only_secret_env_names"] == [
        "GLM_CHAT_BASE_URL", "GLM_API_KEY"
    ]
    runtime = manifest["opencode_runtime"]
    assert runtime["credentialBoundary"] == "launcher-host-only"
    assert runtime["model"] == _MODEL
    assert runtime["opencodeVersion"] == OPENCODE_VERSION
    assert runtime["opencodeCacheSeed"]["version"] \
        == OPENCODE_PROVIDER_NPM_VERSION
    assert manifest["provider_face"] == "yourprovider-chat"
    assert manifest["provider_slot_id"] == "glm-slot-fixture"
    assert len(manifest["harbor_binary_sha256"]) == 64
    agent = config["agents"][0]
    assert agent.get('override_timeout_sec') == deadline
    assert manifest['agent_timeout_override_seconds'] == deadline
    assert runtime['outputTokens'] == opencode.output_tokens
    assert agent["name"] == OPENCODE_AGENT
    assert agent["model_name"] == _MODEL
    assert agent["kwargs"]["version"] == OPENCODE_VERSION
    assert "reasoning_effort" not in agent["kwargs"]
    assert "env" not in agent
    routes = config["environment"]["kwargs"]["loopback_service_forwards"]
    assert len(routes) == 1
    assert routes[0]["guest_host"] == "10.0.2.101"
    parsed = RootlessQemuSettings.from_environment_kwargs(
        config["environment"]["kwargs"]
    )
    resolved = _validated_resume_opencode_runtime(
        run_dir=run_dir,
        manifest=manifest,
        config=config,
        rootless_qemu=parsed,
    )
    assert resolved is not None
    assert resolved[1] == routes[0]["host_port"]
    audit = audit_run(run_dir)
    checks = {row["name"]: row for row in audit["checks"]}
    assert checks["opencode_runtime_manifest"]["ok"] is True
    assert checks["opencode_agent_pin"]["ok"] is True
    assert checks["opencode_single_control_route"]["ok"] is True
    assert checks["opencode_no_credential_persistence"]["ok"] is True
    assert checks["opencode_private_metrics_repository"]["ok"] is True
    assert checks["job_result"]["ok"] is False

    for key in ('override_timeout_sec', 'max_timeout_sec'):
        drifted = deepcopy(config)
        drifted['agents'][0][key] = 120
        with pytest.raises(ValueError, match='config drifted'):
            _validated_resume_opencode_runtime(run_dir=run_dir, manifest=manifest,
                config=drifted, rootless_qemu=parsed)

    leaked = deepcopy(config)
    leaked["credentialProbe"] = "formal-secret-value-never-persist"
    with pytest.raises(ValueError, match="leaked"):
        _validated_resume_opencode_runtime(
            run_dir=run_dir,
            manifest=manifest,
            config=leaked,
            rootless_qemu=parsed,
        )


def test_spec_rejects_kimi_only_knobs_and_drift(host_tmp_path):
    rootless, opencode = _runtime_fixture(host_tmp_path)
    with pytest.raises(ValueError, match='budgets must match'):
        _spec(host_tmp_path, rootless, opencode, agent_timeout_override_seconds=21600).validate()
    with pytest.raises(ValueError, match='only supported for local GLM'):
        _spec(host_tmp_path, rootless, opencode, agent_timeout_override_seconds=3600,
              release_run_root=host_tmp_path / 'release').validate()
    with pytest.raises(ValueError, match="reasoning_effort"):
        _spec(host_tmp_path, rootless, opencode,
              reasoning_effort="high").validate()
    with pytest.raises(ValueError, match="pinned model"):
        _spec(host_tmp_path, rootless, opencode, models=("kimi-k3",)).validate()
    with pytest.raises(ValueError, match="agent version"):
        _spec(host_tmp_path, rootless, opencode, agent_version="0.0.0").validate()
    with pytest.raises(ValueError, match="timeout_multiplier"):
        _spec(host_tmp_path, rootless, opencode,
              timeout_multiplier=2.0).validate()
    with pytest.raises(ValueError, match="maxInfrastructureRetries"):
        _spec(host_tmp_path, rootless, opencode, max_retries=1).validate()


def test_settings_validation_rejects_bad_pins(host_tmp_path):
    binary, digest, seed = _opencode_fixture(host_tmp_path)
    base = {
        "opencode_binary": binary,
        "opencode_sha256": digest,
        "opencode_cache_seed": seed,
        "model": _MODEL,
        "provider_face": "yourprovider-chat",
        "provider_slot_id": "glm-slot-fixture",
    }
    with pytest.raises(ValueError, match="SHA-256"):
        OpenCodeBaselineSettings(
            **{**base, "opencode_sha256": "not-hex"}).validate(
            verify_binary=False)
    with pytest.raises(CodexContractError, match="SHA-256"):
        OpenCodeBaselineSettings(
            **{**base, "opencode_sha256": "0" * 64}).validate(
            verify_binary=True)
    with pytest.raises(ValueError, match="different variables"):
        OpenCodeBaselineSettings(
            **{**base, "upstream_api_key_env": "GLM_CHAT_BASE_URL"}).validate(
            verify_binary=False)
    with pytest.raises(ValueError, match="model"):
        OpenCodeBaselineSettings(**{**base, "model": "bad model"}).validate(
            verify_binary=False)


def _synthetic_trial(run_dir: Path, token: str) -> Path:
    global_metrics = run_dir / "proxy-metrics.jsonl"
    row = {
        "trialToken": token,
        "event": "chatCompletion",
        "status": "completed",
        "model": _MODEL,
        "upstreamCalls": 1,
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 7,
            "total_tokens": 19,
            "completion_tokens_details": {"reasoning_tokens": 2},
            "prompt_tokens_details": {"cached_tokens": 3},
        },
        "proxyCpuNs": 1000,
        "rawWallNs": 2000,
        "upstreamWallNs": 3000,
    }
    global_metrics.write_text(json.dumps(row) + "\n", encoding="utf-8")
    trial_dir = run_dir / "trial-0"
    evidence = trial_dir / "agent" / "opencode-evidence"
    evidence.mkdir(parents=True, mode=0o700)
    write_trial_proxy_metrics(
        str(global_metrics), str(evidence / "proxy-metrics.jsonl"),
        trial_token=token)
    events = [
        {"part": {"type": "step-finish", "tokens": {
            "input": 9, "output": 5, "reasoning": 2,
            "cache": {"read": 3},
        }}},
        {"part": {"type": "tool", "callID": "call-1", "tool": "bash",
                  "state": {"status": "completed",
                            "input": {"command": "true"},
                            "output": "ok"}}},
        {"part": {"type": "text", "text": "done"}},
    ]
    (evidence / "opencode-events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8")
    return trial_dir


def test_trial_evidence_reconciles_and_detects_tampering(host_tmp_path):
    run_dir = host_tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    token = benchmark_trial_token("run", _TASK, "1")
    trial_dir = _synthetic_trial(run_dir, token)
    sha = "ab" * 32
    trajectory = {
        "schema_version": "ATIF-v1.7",
        "agent": {
            "name": "opencode-guest",
            "version": OPENCODE_VERSION,
            "model_name": _MODEL,
            "extra": {"binary_sha256": sha, "trial_token": token},
        },
        "steps": [{"step_id": 1, "source": "agent", "message": "done"}],
    }
    trajectory_path = trial_dir / "agent" / "trajectory.json"
    trajectory_path.write_text(json.dumps(trajectory), encoding="utf-8")
    manifest = {
        "agent": OPENCODE_AGENT,
        "opencode_runtime": {"opencodeSha256": sha},
    }

    checks = _opencode_trial_evidence_checks(manifest, [trial_dir], 1)
    assert checks[0].ok is True

    # The proxy/CLI token usage no longer reconciles after tampering.
    events_path = trial_dir / "agent" / "opencode-evidence" \
        / "opencode-events.jsonl"
    events_path.write_text(
        json.dumps({"part": {"type": "step-finish", "tokens": {
            "input": 10, "output": 5, "reasoning": 2,
            "cache": {"read": 3},
        }}}) + "\n",
        encoding="utf-8")
    checks = _opencode_trial_evidence_checks(manifest, [trial_dir], 1)
    assert checks[0].ok is False

    # A foreign trial token finds no tagged proxy rows.
    events_path.write_text(
        json.dumps({"part": {"type": "step-finish", "tokens": {
            "input": 9, "output": 5, "reasoning": 2,
            "cache": {"read": 3},
        }}}) + "\n",
        encoding="utf-8")
    tampered = json.loads(trajectory_path.read_text())
    tampered["agent"]["extra"]["trial_token"] = "cd" * 32
    trajectory_path.write_text(json.dumps(tampered), encoding="utf-8")
    checks = _opencode_trial_evidence_checks(manifest, [trial_dir], 1)
    assert checks[0].ok is False


def test_real_trajectory_export_preserves_trial_identity_and_redacts_credentials(host_tmp_path):
    pytest.importorskip('harbor')
    from rootless_vm.harbor_opencode_agent import _opencode_atif
    from rootless_vm.trajectory import write_collected_trajectory
    from evaluations.opencode_proxy.projection import project_opencode_trial

    run_dir = host_tmp_path / 'run'
    run_dir.mkdir(mode=0o700)
    token = benchmark_trial_token('run', _TASK, '1')
    trial = _synthetic_trial(run_dir, token)
    evidence = trial / 'agent' / 'opencode-evidence'
    projection = project_opencode_trial(
        raw_events=evidence / 'opencode-events.jsonl',
        proxy_metrics=evidence / 'proxy-metrics.jsonl', trial_token=token)
    document = _opencode_atif(instruction='task', projection=projection,
        binary_sha256='ab' * 32, trial_token=token, model=_MODEL)
    document['agent']['extra']['api_key'] = 'private-provider-credential'
    path = trial / 'agent' / 'trajectory.json'
    write_collected_trajectory(path, document)
    exported = json.loads(path.read_text())
    assert exported['agent']['extra']['trial_id_sha256'] == token
    assert 'private-provider-credential' not in path.read_text()
    manifest = {'agent': OPENCODE_AGENT,
                'opencode_runtime': {'opencodeSha256': 'ab' * 32}}
    assert _opencode_trial_evidence_checks(manifest, [trial], 1)[0].ok
    exported['agent']['extra']['trial_id_sha256'] = 'cd' * 32
    path.write_text(json.dumps(exported))
    assert not _opencode_trial_evidence_checks(manifest, [trial], 1)[0].ok
    exported['agent']['extra']['trial_id_sha256'] = token
    exported['agent']['extra']['trial_token'] = 'cd' * 32
    path.write_text(json.dumps(exported))
    assert not _opencode_trial_evidence_checks(manifest, [trial], 1)[0].ok
    exported['agent']['extra']['trial_token'] = '<redacted>'
    path.write_text(json.dumps(exported))
    assert _opencode_trial_evidence_checks(manifest, [trial], 1)[0].ok
