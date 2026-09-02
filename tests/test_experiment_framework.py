"""Executable contract for the pluginized experiment capability boundary."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from lib.experiments import (
    ExperimentContractError,
    analyze_experiment,
    apply_experiment,
    assign_experiment,
    compile_experiment_application,
    compile_metric_extractor,
    extract_metric_values,
    resolve_experiment_spec,
)
from lib.experiments.builtin_context_cost import context_cost_spec
from lib.experiments.contracts import document_digest, validate_resolved_spec
from lib.experiments.registry import (
    AnalyzerProvider,
    ExperimentPlugin,
    ExperimentPluginError,
    ExperimentRegistry,
    MetricProvider,
    StrategyProvider,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _plugin(*, digest: str = "a" * 64, fail_apply: bool = False,
            version: str = "1.0.0"):
    def apply(config, policy):
        if fail_apply:
            raise RuntimeError("injected strategy failure")
        return {**config, "variant": policy["variant"]}

    strategy = StrategyProvider(
        "test.experiment", "variant", version, digest, "test strategy",
        {"type": "object"},
        lambda raw: {"variant": str(raw.get("variant") or "control")},
        lambda config: "manual_override" if config.get("manual") else None,
        apply,
    )
    metric = MetricProvider(
        "test.experiment", "score", version, digest, "test score",
        "points", "increase", lambda outcome: outcome.get("score"),
    )
    analyzer = AnalyzerProvider(
        "test.experiment", "identity", version, digest, "test analyzer",
        lambda payload: {
            "contractVersion": "test.decision/v1",
            "specDigest": payload["spec"]["specDigest"],
            "payload": payload.get("value"),
        },
    )
    return ExperimentPlugin(
        "test.experiment", version, "test plugin",
        strategies=(strategy,), metrics=(metric,), analyzers=(analyzer,),
    )


def _resolved(registry: ExperimentRegistry, *, version: str | None = None) -> dict:
    version_ref = {"pluginVersion": version} if version else {}
    return resolve_experiment_spec({
        "experimentId": "plugin-contract-v1",
        "assignmentUnit": "conversation",
        "enrollmentBps": 10_000,
        "arms": [
            {
                "id": "control", "allocationBps": 5_000,
                "strategy": {
                    "pluginId": "test.experiment", "strategyId": "variant",
                    **version_ref,
                    "config": {"variant": "control"},
                },
            },
            {
                "id": "candidate", "allocationBps": 5_000,
                "strategy": {
                    "pluginId": "test.experiment", "strategyId": "variant",
                    **version_ref,
                    "config": {"variant": "candidate"},
                },
            },
        ],
        "metrics": [{"pluginId": "test.experiment", "metricId": "score",
                     **version_ref}],
        "primaryMetric": {
            "pluginId": "test.experiment", "metricId": "score",
        },
        "analyzer": {
            "pluginId": "test.experiment", "analyzerId": "identity",
            **version_ref,
        },
        "analysis": {"minimumSampleSizePerArm": 2},
    }, provider_registry=registry)


def test_registry_mount_is_atomic_disposable_and_callback_free():
    providers = ExperimentRegistry()
    dispose = providers.register(_plugin())
    assert providers.generation == 1
    assert providers.require_strategy("test.experiment", "variant")
    assert providers.catalog()["plugins"][0]["pluginId"] == "test.experiment"
    assert "apply" not in str(providers.catalog())
    with pytest.raises(ExperimentPluginError, match="already registered"):
        providers.register(_plugin())
    # The failed duplicate registration did not partially replace the bundle.
    assert providers.require_metric("test.experiment", "score")
    dispose()
    dispose()  # idempotent rollback owner
    assert providers.generation == 2
    with pytest.raises(ExperimentPluginError, match="unavailable"):
        providers.require_strategy("test.experiment", "variant")


def test_registry_keeps_historical_versions_and_rejects_ambiguous_resolution():
    providers = ExperimentRegistry()
    providers.register(_plugin(version="1.0.0", digest="a" * 64))
    historical = _resolved(providers, version="1.0.0")
    providers.register(_plugin(version="2.0.0", digest="b" * 64))

    with pytest.raises(ExperimentPluginError, match="version is ambiguous"):
        _resolved(providers)
    assert extract_metric_values(
        historical, {"score": 7}, provider_registry=providers
    ) == {"test.experiment/score": 7.0}
    current = _resolved(providers, version="2.0.0")
    assert current["metrics"][0]["pluginVersion"] == "2.0.0"


def test_assignment_is_owner_scoped_deterministic_and_digest_verified():
    providers = ExperimentRegistry()
    providers.register(_plugin())
    spec = _resolved(providers)
    first = assign_experiment(spec, owner_id=1, unit_id="same-conversation")
    repeated = assign_experiment(spec, owner_id=1, unit_id="same-conversation")
    other_owner = assign_experiment(spec, owner_id=2, unit_id="same-conversation")
    assert first == repeated
    assert first["subjectDigest"] != other_owner["subjectDigest"]
    assert len(first["subjectDigest"]) == 64
    assert set(first["subjectDigest"]) <= set("0123456789abcdef")
    with pytest.raises(ValueError, match="owner_id"):
        assign_experiment(spec, owner_id="", unit_id="conversation")

    tampered = {**spec, "enrollmentBps": 0}
    with pytest.raises(ExperimentContractError, match="specDigest mismatch"):
        assign_experiment(tampered, owner_id=1, unit_id="conversation")

    self_consistent_but_invalid = copy.deepcopy(spec)
    self_consistent_but_invalid["arms"][0]["allocationBps"] = 4_000
    self_consistent_but_invalid.pop("specDigest")
    self_consistent_but_invalid["specDigest"] = document_digest(
        self_consistent_but_invalid)
    with pytest.raises(ExperimentContractError, match="must total 10000"):
        validate_resolved_spec(self_consistent_but_invalid)


def test_strategy_conflict_and_provider_drift_preserve_original_request():
    providers = ExperimentRegistry()
    providers.register(_plugin())
    spec = _resolved(providers)
    request = {"manual": True}
    unchanged, assignment = apply_experiment(
        spec, owner_id=1, unit_id="conversation", request_config=request,
        provider_registry=providers,
    )
    assert unchanged is request
    assert assignment["status"] == "excluded"
    assert assignment["exposureStatus"] == "not_applied"

    drifted = ExperimentRegistry()
    drifted.register(_plugin(digest="b" * 64))
    unchanged, assignment = apply_experiment(
        spec, owner_id=1, unit_id="conversation-2", request_config=request,
        provider_registry=drifted,
    )
    assert unchanged is request
    assert assignment["status"] == "application_failed"
    assert assignment["exposureStatus"] == "failed"


def test_metric_and_analyzer_execution_are_pinned_to_the_resolved_provider():
    providers = ExperimentRegistry()
    providers.register(_plugin())
    spec = _resolved(providers)
    assert extract_metric_values(
        spec, {"score": 7}, provider_registry=providers
    ) == {"test.experiment/score": 7.0}
    extractor = compile_metric_extractor(spec, provider_registry=providers)
    assert extractor({"score": 8}) == {"test.experiment/score": 8.0}
    application = compile_experiment_application(
        spec, provider_registry=providers
    )
    updated, assignment = application(
        owner_id=1, unit_id="compiled-hot-path", request_config={}
    )
    assert updated["variant"] == assignment["strategy"]["config"]["variant"]
    decision = analyze_experiment(
        spec, {"value": "evidence"}, provider_registry=providers
    )
    assert decision["specDigest"] == spec["specDigest"]
    assert decision["payload"] == "evidence"


def test_builtin_decision_requires_complete_cost_quality_latency_and_source():
    spec = context_cost_spec(
        experiment_id="context-cost-statistics-v1",
        traffic_percent=100,
        treatment_percent=50,
        minimum_sample_size=10,
    )
    payload = {
        "arms": {
            "control": {
                "assignedUnits": 20,
                "fullyPricedCosts": [0.20] * 20,
                "pricingCoverage": 1.0,
                "qualityByUnit": [1.0] * 20,
                "latencyByUnit": [100.0] * 20,
            },
            "optimized": {
                "assignedUnits": 20,
                "fullyPricedCosts": [0.10] * 20,
                "pricingCoverage": 1.0,
                "qualityByUnit": [1.0] * 20,
                "latencyByUnit": [105.0] * 20,
            },
        },
        "observedSpecDigests": [spec["specDigest"]],
        "analysisClosed": True,
        "analysisStartVerified": True,
        "analysisSealVerified": True,
        "fixedHorizonReached": True,
    }
    promoted = analyze_experiment(spec, payload)
    assert promoted["status"] == "promote"
    assert promoted["promotionEligible"] is True
    assert promoted["costDifferenceUsd"]["upper"] < 0

    incomplete = {
        **payload,
        "arms": {
            **payload["arms"],
            "optimized": {
                **payload["arms"]["optimized"], "pricingCoverage": 0.95,
            },
        },
    }
    refused = analyze_experiment(spec, incomplete)
    assert refused["promotionEligible"] is False
    assert "incomplete_pricing" in refused["blockers"]

    truncated = analyze_experiment(spec, {**payload, "truncated": True})
    assert truncated["dataValid"] is False
    assert "truncated_source" in truncated["blockers"]


def test_resolved_builtin_spec_conforms_to_the_machine_readable_contract():
    schema = json.loads(
        (ROOT / "contracts/experiments_v1.schema.json").read_text()
    )
    Draft202012Validator.check_schema(schema)
    spec = context_cost_spec(
        experiment_id="schema-contract-v1",
        traffic_percent=10,
        treatment_percent=50,
        minimum_sample_size=20,
    )
    Draft202012Validator(schema).validate(spec)
