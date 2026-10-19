"""Pinned inputs, verifier semantics, terminal attribution and reference drift."""
from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from evaluations.catalog import BENCHMARKS
from evaluations.native_failure import native_failure_receipt
from evaluations.reference_profiles import compare_glm53_reference
from evaluations.swebench.capabilities import require_harbor_capabilities
from evaluations.swebench.harbor_runner import HarborRunSpec, build_job_config
from evaluations.swebench.infrastructure_retry import classify_infrastructure_failure

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("key,count", [("deepswe-v1.1", 113), ("terminal-bench-3.0", 74),
                                      ("terminal-bench-4.0", 66)])
def test_new_pinned_suites_preserve_unknown_publication_rules(host_tmp_path, key, count):
    definition = BENCHMARKS[key]
    assert len(definition.task_digests()) == definition.task_count == count
    assert definition.official_min_attempts is None
    spec = HarborRunSpec(agent="mini-swe-agent", models=("glm-5.3",), backend="modal",
                         output_root=host_tmp_path / "runs", benchmark=key, agent_version="2.2.3")
    config = build_job_config(spec, run_dir=host_tmp_path / "run", run_id="readiness")
    assert config["datasets"][0]["ref"] == definition.dataset_source_revision
    assert config["n_attempts"] == 5


@pytest.mark.parametrize("backend", ["rootless-qemu", "singularity"])
def test_unsupported_local_backend_fails_before_workspace_creation(host_tmp_path, backend):
    spec = HarborRunSpec(agent="oracle", models=("oracle",), backend=backend,
                         output_root=host_tmp_path / "runs", benchmark="deepswe-v1.1")
    with pytest.raises(ValueError, match="separate-verifier"):
        spec.validate()
    assert not (host_tmp_path / "runs").exists()


@pytest.mark.parametrize("verifier", [{}, {"environment_mode": "separate", "collect": []}])
def test_engine_cannot_silently_ignore_verifier_contract(monkeypatch, verifier):
    module = ModuleType("harbor.models.task.config")
    module.TaskConfig = SimpleNamespace(model_validate=lambda value:
        SimpleNamespace(model_dump=lambda **kwargs: {"verifier": verifier}))
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with pytest.raises(ValueError, match="Harbor does not retain"):
        require_harbor_capabilities(BENCHMARKS["deepswe-v1.1"])


def test_engine_retains_separate_verifier_and_collect(monkeypatch):
    module = ModuleType("harbor.models.task.config")
    module.TaskConfig = SimpleNamespace(model_validate=lambda value:
        SimpleNamespace(model_dump=lambda **kwargs: value))
    monkeypatch.setitem(sys.modules, module.__name__, module)
    require_harbor_capabilities(BENCHMARKS["deepswe-v1.1"])


@pytest.mark.parametrize("kind,retryable", [("internal", False), ("prompt_too_long", False),
                                          ("network", False), ([], True)])
def test_context_and_ambiguous_failures_never_authorize_replacement(kind, retryable):
    result = {"exception_info": {"exception_type": "RuntimeError",
              "exception_message": "did not reach a successful terminal state"},
              "agent_result": {"metadata": {"tofuKimiEvidence": {
                  "runtimeFailure": {"kind": kind, "retryable": retryable}}}}}
    assert classify_infrastructure_failure(result) is None


def test_any_recorded_metric_blocks_infrastructure_replacement():
    result = {"verifier_result": {"rewards": {"pass_fraction": 0.2}},
              "exception_info": {"exception_type": "EnvironmentStartTimeoutError"}}
    assert classify_infrastructure_failure(result) is None


def test_native_failure_receipt_does_not_export_raw_provider_data():
    receipt = native_failure_receipt({"kind": "internal", "retryable": False,
        "raw": "summary_budget_exhausted secret-url-and-key", "message": "private prompt"})
    assert receipt == {"kind": "internal", "retryable": False,
                       "reason": "context_compaction_budget_exhausted"}
    assert "secret" not in json.dumps(receipt)


def test_reference_comparison_never_infers_missing_defaults_or_score():
    report = {"observed_request_parameters": [{"parameters": {
        "temperature": 1.0, "max_tokens": 128000, "top_p": None}, "count": 4}]}
    config = {"agents": [{"name": "tofu", "kwargs": {"version": "0.17.4"},
                          "override_timeout_sec": 21600}]}
    comparison = compare_glm53_reference(config, report)
    assert comparison["dimensions"]["agent"]["status"] == "mismatch"
    assert comparison["dimensions"]["max_tokens"]["status"] == "mismatch"
    assert comparison["dimensions"]["top_p"]["status"] == "unknown"
    assert comparison["dimensions"]["temperature"]["status"] == "match"
    assert comparison["reference_reproduced"] is False


def test_candidate_cli_freezes_production_surface_and_wire_receipts(host_tmp_path):
    from evaluations.swebench.cli import build_parser, _tofu_kimi_settings
    path = host_tmp_path / "runtime.json"
    path.write_text('{"maxTokens": 65536}')
    args = build_parser().parse_args(["run", "--agent", "tofu-kimi", "--model", "glm-5.3-m17",
        "--tofu-runtime-config", str(path), "--tofu-experiment-arm", "reference-budget",
        "--kimi-provider-face", "zai", "--kimi-slot-id", "m17",
        "--tofu-tool-surface", "production", "--tofu-wire-proxy"])
    _, _, settings = _tofu_kimi_settings(args, agent="tofu-kimi", agent_version="0.17.4")
    assert settings.tool_surface == "production"
    assert settings.wire_proxy is True
    assert settings.runtime_config == {"maxTokens": 65536}


def test_reference_unknown_fields_do_not_echo_credentials():
    comparison = compare_glm53_reference({"agents": [{
        "name": "private-provider-key", "kwargs": {"version": "private-provider-key"},
        "override_timeout_sec": "private-provider-key"}]}, {})
    assert "private-provider-key" not in json.dumps(comparison)
    assert comparison["dimensions"]["agent"]["status"] == "unknown"


def test_agent_timeout_cannot_be_promoted_to_transient_runtime_failure():
    result = {"exception_info": {"exception_type": "AgentTimeoutError",
              "exception_message": "did not reach a successful terminal state"},
              "agent_result": {"metadata": {"tofuKimiEvidence": {
                  "runtimeFailure": {"kind": "network", "retryable": True}}}}}
    assert classify_infrastructure_failure(result) is None
