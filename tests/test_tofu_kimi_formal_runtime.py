"""Formal Harbor contracts for production Tofu AgentRuntime × Kimi."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evaluations.swebench import harbor_runner
from evaluations.swebench.audit import audit_run
from evaluations.swebench.constants import BENCHMARKS
from evaluations.swebench.harbor_runner import (
    HarborRunSpec,
    _validated_resume_tofu_runtime,
    start_harbor_run,
)
from evaluations.swebench.rootless_qemu import RootlessQemuSettings
from evaluations.swebench.tofu_kimi_runtime import (
    TOFU_KIMI_AGENT,
    TofuKimiCandidateSettings,
    tofu_kimi_prompt_contract_sha256,
    tofu_kimi_tool_schema_sha256,
)
from evaluations.long_agent_release.run_store import (
    audit_release_attempts,
    initialize_release_run,
)
from lib.benchmark_contract import build_manifest_v2


pytestmark = pytest.mark.unit
_TASK = "swe-bench/django__django-11099"


def _executable(path: Path, body: str = "exit 0\n") -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fixture(
    tmp_path: Path,
) -> tuple[RootlessQemuSettings, TofuKimiCandidateSettings, Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    qemu = _executable(runtime / "qemu-system-x86_64")
    qemu_img = _executable(runtime / "qemu-img")
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
    settings = TofuKimiCandidateSettings(
        provider_face="meituan-chat",
        provider_slot_id="kimi-slot-fixture",
        agent_version="0.17.0",
        experiment_arm="prompt_lean_kimi",
        runtime_config={
            "responses": {"promptProfile": "lean"},
            "tools": {
                "schemaBudgetTokens": 4_000,
                "resultEnvelope": "v2",
            },
            "context": {"globalBudgetTokens": 96_000},
            "compaction": {"strategy": "adaptive"},
            "orchestration": {"policy": "v2"},
        },
    )
    harbor = _executable(
        tmp_path / "harbor-fixture", "printf '%s\\n' 'harbor fixture'\n")
    return (
        RootlessQemuSettings(
            base_disk=base,
            image_store=store,
            qemu_path=qemu,
            qemu_img_path=qemu_img,
        ),
        settings,
        harbor,
    )


def _spec(
    tmp_path: Path,
    rootless: RootlessQemuSettings,
    settings: TofuKimiCandidateSettings,
    harbor: Path,
) -> HarborRunSpec:
    return HarborRunSpec(
        agent=TOFU_KIMI_AGENT,
        models=("kimi-k3",),
        backend="rootless-qemu",
        output_root=tmp_path / "evals",
        task_ids=(_TASK,),
        concurrency=1,
        agent_concurrency=1,
        max_retries=0,
        reasoning_effort="high",
        agent_version=settings.agent_version,
        harbor_bin=str(harbor),
        rootless_qemu=rootless,
        tofu_kimi=settings,
    )


def test_formal_tofu_dry_run_is_secret_free_and_round_trips(
    tmp_path, monkeypatch,
):
    rootless, settings, harbor = _fixture(tmp_path)
    monkeypatch.setenv("KIMI_CHAT_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("KIMI_API_KEY", "formal-tofu-secret-never-persist")

    code, run_dir = start_harbor_run(
        _spec(tmp_path, rootless, settings, harbor), dry_run=True)

    assert code == 0
    config = json.loads((run_dir / "job-config.json").read_text())
    manifest = json.loads((run_dir / "manifest.json").read_text())
    serialized = json.dumps({"config": config, "manifest": manifest})
    assert "formal-tofu-secret-never-persist" not in serialized
    assert "https://models.example/v1" not in serialized
    assert manifest["host_only_secret_env_names"] == [
        "KIMI_CHAT_BASE_URL", "KIMI_API_KEY",
    ]
    assert manifest["tofu_kimi_runtime"]["credentialBoundary"] \
        == "harbor-host-only"
    assert manifest["tofu_kimi_runtime"]["guestCredentialValues"] is False
    assert manifest["experiment_arm"] == "prompt_lean_kimi"
    agent = config["agents"][0]
    assert agent["name"] == TOFU_KIMI_AGENT
    assert agent["model_name"] == "kimi-k3"
    assert "env" not in agent
    assert "extra_allowed_hosts" not in agent
    assert config["environment"]["kwargs"].get(
        "loopback_service_forwards", []) == []
    parsed_rootless = RootlessQemuSettings.from_environment_kwargs(
        config["environment"]["kwargs"])
    parsed = _validated_resume_tofu_runtime(
        manifest=manifest,
        config=config,
        rootless_qemu=parsed_rootless,
    )
    assert parsed is not None
    assert parsed.runtime_config_sha256 == settings.runtime_config_sha256
    checks = {row["name"]: row for row in audit_run(run_dir)["checks"]}
    assert checks["tofu_kimi_runtime_manifest"]["ok"] is True
    assert checks["tofu_kimi_production_agent_pin"]["ok"] is True
    assert checks["tofu_kimi_no_guest_control_route"]["ok"] is True
    assert checks["tofu_kimi_no_credential_persistence"]["ok"] is True


def test_formal_tofu_launcher_retains_secret_only_for_host_agent(
    tmp_path, monkeypatch,
):
    rootless, settings, harbor = _fixture(tmp_path)
    monkeypatch.setenv("KIMI_CHAT_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("KIMI_API_KEY", "host-agent-only-secret")
    monkeypatch.setattr(harbor_runner, "_git_dirty", lambda **_kwargs: False)
    monkeypatch.setattr(
        harbor_runner, "_git_revision", lambda **_kwargs: "clean-revision")
    observed: dict[str, object] = {}

    def fake_run_streaming(command, *, cwd, log_path, env, unset_env):
        observed.update({
            "command": list(command),
            "unset": tuple(unset_env),
            "config": json.loads((cwd / "job-config.json").read_text()),
        })
        return 17

    monkeypatch.setattr(harbor_runner, "run_streaming", fake_run_streaming)
    code, run_dir = start_harbor_run(
        _spec(tmp_path, rootless, settings, harbor))

    assert code == 17
    assert observed["unset"] == ()
    agent = observed["config"]["agents"][0]
    assert "env" not in agent
    serialized = (
        (run_dir / "job-config.json").read_text()
        + (run_dir / "manifest.json").read_text()
        + (run_dir / "command.json").read_text()
    )
    assert "host-agent-only-secret" not in serialized
    assert "https://models.example/v1" not in serialized


def test_formal_tofu_rejects_config_credentials_and_authority_drift(tmp_path):
    with pytest.raises(ValueError, match="credential-bearing"):
        TofuKimiCandidateSettings(
            provider_face="face",
            provider_slot_id="slot",
            agent_version="0.17.0",
            experiment_arm="arm",
            runtime_config={"api_key": "not-allowed"},
        )
    rootless, settings, harbor = _fixture(tmp_path)
    spec = _spec(tmp_path, rootless, settings, harbor)
    with pytest.raises(ValueError, match="internal retries"):
        HarborRunSpec(**{**spec.__dict__, "max_retries": 1}).validate()
    routed = RootlessQemuSettings(
        **{
            **rootless.__dict__,
            "loopback_services": (),
        }
    )
    assert routed.loopback_services == ()
    with pytest.raises(ValueError, match="agent version"):
        HarborRunSpec(**{
            **spec.__dict__, "agent_version": "drifted-version",
        }).validate()


def test_runtime_config_digest_is_canonical_and_manifest_bound(tmp_path):
    _, settings, _ = _fixture(tmp_path)
    reordered = TofuKimiCandidateSettings(
        provider_face=settings.provider_face,
        provider_slot_id=settings.provider_slot_id,
        agent_version=settings.agent_version,
        experiment_arm=settings.experiment_arm,
        runtime_config=dict(reversed(list(settings.runtime_config.items()))),
    )
    assert reordered.runtime_config_sha256 == settings.runtime_config_sha256
    record = settings.manifest_record()
    assert len(record["runtimeConfigSha256"]) == 64
    assert record["promptContractSha256"] == \
        tofu_kimi_prompt_contract_sha256(settings.runtime_config)
    drifted = {**record, "runtimeConfigSha256": hashlib.sha256(b"x").hexdigest()}
    with pytest.raises(ValueError, match="digest drifted"):
        TofuKimiCandidateSettings.from_manifest_record(drifted)


def test_formal_tofu_claims_release_attempt_before_dispatch(
    tmp_path, monkeypatch,
):
    rootless, settings, harbor = _fixture(tmp_path)
    monkeypatch.setenv("KIMI_CHAT_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("KIMI_API_KEY", "tracked-host-secret")
    monkeypatch.setattr(harbor_runner, "_git_dirty", lambda **_kwargs: False)
    monkeypatch.setattr(
        harbor_runner, "_git_revision", lambda **_kwargs: "tracked-revision")
    identity_spec = HarborRunSpec(**{
        **_spec(tmp_path, rootless, settings, harbor).__dict__,
        "run_id": "tofu-tracking-identity",
    })
    _, identity_dir = start_harbor_run(identity_spec, dry_run=True)
    identity = json.loads((identity_dir / "manifest.json").read_text())
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
    release_manifest = build_manifest_v2(
        run_id="release-candidate",
        harness=identity["harness_identity"],
        agent={
            "name": "tofu", "version": settings.agent_version,
            "commitSha256": digest("tofu-agent"),
        },
        provider_face=settings.provider_face,
        provider_slot_id=settings.provider_slot_id,
        thinking="high",
        experiment_arm=settings.experiment_arm,
        pair_id="pair-tracked",
        comparison_role="candidate",
        tool_permissions={"profile": "frozen-read-write"},
        prompt_digest=tofu_kimi_prompt_contract_sha256(
            settings.runtime_config),
        tool_schema_digest=tofu_kimi_tool_schema_sha256(),
        dataset_snapshot={
            "id": "tracked-pilot", "sha256": digest("dataset"),
            "frozen": True,
        },
        task_table=[{
            "taskId": "swe-bench-verified:django__django-11099",
            "family": "software_engineering",
            "dataset": "swe-bench-verified",
            "sourceTaskId": _TASK,
            "sourceSha256": digest("task"),
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
        environment={
            "gitCommit": "tracked-revision",
            "runtimeConfigSha256": settings.runtime_config_sha256,
        },
    )
    release_root = tmp_path / "release-store"
    initialize_release_run(release_root, release_manifest)
    observed: dict = {}

    def fake_run_streaming(*_args, cwd, **_kwargs):
        observed["attempts"] = audit_release_attempts(release_root)
        observed["manifest"] = json.loads(
            (cwd / "manifest.json").read_text())
        return 17

    monkeypatch.setattr(harbor_runner, "run_streaming", fake_run_streaming)
    tracked = HarborRunSpec(**{
        **_spec(tmp_path, rootless, settings, harbor).__dict__,
        "run_id": "tofu-tracked-dispatch",
        "release_run_root": release_root,
    })
    code, _ = start_harbor_run(tracked)

    assert code == 17
    assert observed["attempts"]["totalAttempts"] == 1
    assert observed["attempts"]["openAttempts"] == 1
    tracking = observed["manifest"]["release_attempt_tracking"]
    assert tracking["runnerKind"] == "harbor-tofu"
    assert tracking["taskCount"] == 1
    assert observed["manifest"]["release_evidence_eligible"] is True
