"""Diagnostic counts never replace execution failures with benchmark scores."""
import json
from types import SimpleNamespace

import pytest

from evaluations.evidence import TrialObservation, summarize_trials
from evaluations.swebench.diagnostics import diagnose_run

pytestmark = pytest.mark.unit


def test_arbitrary_metrics_missing_attempts_and_errors_stay_distinct():
    trials = [TrialObservation("a", "task", "other-harness", {"distance": 12.5}),
              TrialObservation("b", "task", "other-harness", {}, "GuestChannelTimeoutError"),
              TrialObservation("c", "task", "other-harness", {"reward": 1}, "AgentTimeoutError")]
    report = summarize_trials(trials, expected={("task", "other-harness"): 4})
    assert report["observed_reward_histograms"] == {"distance": {"12.5": 1}, "reward": {"1": 1}}
    assert report["trials_without_rewards"] == 1
    assert report["missing_trials"] == [{"task": "task", "model": "other-harness", "count": 1}]
    assert report["formal_score"] is None
    assert report["exception_counts"] == {"AgentTimeoutError": 1, "GuestChannelTimeoutError": 1}


@pytest.mark.parametrize("value", [True, "1", float("nan"), float("inf"), None])
def test_invalid_rewards_never_become_scores(value):
    report = summarize_trials([TrialObservation("a", "t", "m", {"r": value})], expected={("t", "m"): 1})
    assert report["observed_reward_histograms"] == {}
    assert report["evidence_issue_counts"] == {"invalid_reward": 1}
    json.dumps(report, allow_nan=False)


def test_extra_attempts_cannot_displace_missing_tasks():
    trials = [TrialObservation("a", "easy", "m", {"reward": 1})] * 2
    report = summarize_trials(trials, expected={("easy", "m"): 1, ("hard", "m"): 1})
    assert report["evidence_issue_counts"]["duplicate_or_missing_trial_identity"] == 2
    assert report["missing_trials"][0]["task"] == "hard"
    assert report["excess_trials"][0]["task"] == "easy"
    assert report["formal_score"] is None


def test_equivalent_numeric_rewards_share_a_histogram_bucket():
    report = summarize_trials([TrialObservation("a", "t", "m", {"r": 0}),
                               TrialObservation("b", "t", "m", {"r": 0.0})],
                              expected={("t", "m"): 2})
    assert report["observed_reward_histograms"] == {"r": {"0": 2}}


def fixture_run(tmp_path, monkeypatch):
    from evaluations.swebench import diagnostics
    monkeypatch.setattr(diagnostics, "BENCHMARKS", {
        "arbitrary-suite": SimpleNamespace(task_digests=lambda: {"task": "sha256:pin"})})
    (tmp_path / "manifest.json").write_text(json.dumps({
        "run_id": "run", "benchmark": "arbitrary-suite", "models": ["model"], "status": "running"}))
    (tmp_path / "job-config.json").write_text(json.dumps({
        "job_name": "run", "datasets": [{}], "n_attempts": 2}))
    trial = tmp_path / "jobs/run/trial"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({
        "task_name": "task", "task_checksum": "different-content-digest",
        "config": {"agent": {"model_name": "model"}, "task": {"name": "task", "ref": "sha256:pin"}},
        "verifier_result": {"rewards": {"distance": 12.5}}, "finished_at": "2026-09-16T00:00:00Z"}))
    return trial


def test_harbor_adapter_preserves_nonbinary_metrics_and_truncation(tmp_path, monkeypatch):
    trial = fixture_run(tmp_path, monkeypatch)
    metrics = trial / "agent/opencode-evidence/proxy-metrics.jsonl"
    metrics.parent.mkdir(parents=True)
    metrics.write_text(json.dumps({"event": "chatCompletion", "status": "completed", "finishReasons": ["length"]}) + "\n")
    report = diagnose_run(tmp_path)
    assert report["evidence_issue_counts"] == {}
    assert report["observation_counts"]["last_model_response_truncated"] == 1
    assert report["observed_reward_histograms"] == {"distance": {"12.5": 1}}
    assert len(report["evidence_sha256"]) == 4
    assert report["missing_trials"][0]["count"] == 1
    assert report["snapshot_is_atomic"] is False


def test_malformed_result_is_visible_and_not_zero(tmp_path, monkeypatch):
    trial = fixture_run(tmp_path, monkeypatch)
    (trial / "result.json").write_text('{"secret": "sensitive",')
    report = diagnose_run(tmp_path)
    assert report["evidence_issue_counts"]["unreadable_or_invalid_evidence"] == 1
    assert report["trials_without_rewards"] == 1
    assert "sensitive" not in json.dumps(report)


def test_symlink_trial_is_rejected(tmp_path, monkeypatch):
    trial = fixture_run(tmp_path, monkeypatch)
    (trial.parent / "alias").symlink_to(trial, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        diagnose_run(tmp_path)


def test_byte_budget_failure_is_visible(tmp_path, monkeypatch):
    from evaluations.swebench import diagnostics
    trial = fixture_run(tmp_path, monkeypatch)
    (trial / "result.json").write_text(" " * 600)
    monkeypatch.setattr(diagnostics, "MAX_FILE_BYTES", 512)
    report = diagnose_run(tmp_path)
    assert report["evidence_issue_counts"]["unreadable_or_invalid_evidence"] == 1


def test_cli_reports_registered_coverage_without_loading_a_harness(tmp_path, monkeypatch, capsys):
    from evaluations.__main__ import main
    fixture_run(tmp_path, monkeypatch)
    assert main(["harbor", "diagnose", str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["expected_trials"] == 2
    assert report["formal_score"] is None


def test_registered_coverage_is_bounded_before_allocating_trials(tmp_path, monkeypatch):
    from evaluations.swebench import diagnostics
    fixture_run(tmp_path, monkeypatch)
    monkeypatch.setattr(diagnostics, "MAX_TRIALS", 1)
    with pytest.raises(ValueError, match="coverage exceeds"):
        diagnose_run(tmp_path)


def test_native_compaction_failure_and_actual_main_request_are_visible(tmp_path, monkeypatch):
    trial = fixture_run(tmp_path, monkeypatch)
    native = trial / "agent/tofu-kimi-evidence"
    native.mkdir(parents=True)
    (native / "runtime-evidence.json").write_text(json.dumps({"status": "error", "apiRounds": [
        {"kind": "main", "usage": {"_request_parameters": {
            "max_tokens": 128000, "temperature": 1.0}}},
        {"kind": "compaction", "usage": {"_request_parameters": {"max_tokens": 2048}}},
    ]}))
    # The original transcript can exceed the per-file budget; only the bounded
    # terminal slice is inspected, with its own byte offset and checksum.
    events = native / "events.jsonl"
    events.write_text(json.dumps({"event": {"type": "content", "text": "private" * 15000}})
        + "\n" + json.dumps({"event": {"type": "done", "error": {
            "kind": "internal", "retryable": False,
            "raw": "summary_budget_exhausted private-provider-key"}}}) + "\n")
    report = diagnose_run(tmp_path)
    assert report["native_failure_counts"] == {"context_compaction_budget_exhausted": 1}
    assert report["observed_request_parameters"][0]["parameters"]["max_tokens"] == 128000
    assert len(report["observed_request_parameters"]) == 1
    receipt = report["evidence_slices"]["jobs/run/trial/agent/tofu-kimi-evidence/events.jsonl"]
    assert receipt["offset"] > 0 and receipt["bytes"] == 65536
    assert "private-provider-key" not in json.dumps(report)
    assert report["formal_score"] is None


def test_native_tail_symlink_cannot_escape_run_root(tmp_path, monkeypatch):
    trial = fixture_run(tmp_path, monkeypatch)
    native = trial / "agent/tofu-kimi-evidence"
    native.mkdir(parents=True)
    (native / "runtime-evidence.json").write_text('{"status":"error"}')
    (native / "events.jsonl").symlink_to(tmp_path / "manifest.json")
    report = diagnose_run(tmp_path)
    assert report["evidence_issue_counts"]["unreadable_or_invalid_evidence"] == 1
    assert report["native_failure_counts"] == {}
