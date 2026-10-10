"""Bounded, evidence-preserving infrastructure retry (launcher level).

Pin the classified failure set from tb21 v11 evidence, the never-retry
boundaries (scored trials, agent timeouts, unclassified exceptions), the
whole-directory archival contract, and the resume-dispatch cycle receipts.

Run: pytest tests/test_infrastructure_retry.py -m unit
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from evaluations.swebench.infrastructure_retry import (
    ENVIRONMENT_START_TIMEOUT,
    OPENCODE_CLI_NONZERO_EXIT,
    OPENCODE_EVIDENCE_RECONCILIATION,
    OPENCODE_PROXY_METRICS_SETTLE,
    TOFU_RUNTIME_NO_SUCCESSFUL_TERMINAL,
    archive_infrastructure_failures,
    classify_infrastructure_failure,
    infrastructure_retry_cycle,
    scan_infrastructure_failures,
)


def _exception(exc_type: str, message: str) -> dict:
    return {
        "exception_info": {
            "exception_type": exc_type,
            "exception_message": message,
        }
    }


@pytest.mark.unit
class TestClassifyInfrastructureFailure:
    def test_v11_observed_classes(self):
        assert classify_infrastructure_failure(_exception(
            "EnvironmentStartTimeoutError",
            "Environment start timed out after 600.0 seconds",
        )) == ENVIRONMENT_START_TIMEOUT
        assert classify_infrastructure_failure(_exception(
            "RuntimeError",
            "OpenCode trial evidence could not be reconciled; "
            "retry as infrastructure",
        )) == OPENCODE_EVIDENCE_RECONCILIATION
        assert classify_infrastructure_failure(_exception(
            "RuntimeError",
            "tagged OpenCode proxy metrics did not settle",
        )) == OPENCODE_PROXY_METRICS_SETTLE
        assert classify_infrastructure_failure(_exception(
            "RuntimeError",
            "OpenCode CLI exited nonzero; the trial cannot be "
            "projected as complete",
        )) == OPENCODE_CLI_NONZERO_EXIT
        assert classify_infrastructure_failure(_exception(
            "RuntimeError",
            "production AgentRuntime did not reach a successful "
            "terminal state",
        )) == TOFU_RUNTIME_NO_SUCCESSFUL_TERMINAL

    def test_agent_outcomes_are_never_infrastructure(self):
        assert classify_infrastructure_failure(_exception(
            "AgentTimeoutError", "Agent execution timed out")) is None
        assert classify_infrastructure_failure(_exception(
            "VerifierTimeoutError", "Verifier timed out")) is None
        assert classify_infrastructure_failure(_exception(
            "RuntimeError", "some unclassified failure")) is None
        assert classify_infrastructure_failure({}) is None
        assert classify_infrastructure_failure(
            {"exception_info": None}) is None


def _write_trial(job_dir, name, *, reward="missing", exc=None):
    trial = job_dir / name
    trial.mkdir(mode=0o700)
    result = {
        "task_name": f"terminal-bench/{name.split('__')[0]}",
        "trial_name": name,
    }
    if reward != "missing":
        result["verifier_result"] = {"rewards": {"reward": reward}}
    if exc is not None:
        result.update(exc)
    (trial / "result.json").write_text(json.dumps(result), encoding="utf-8")
    (trial / "exception.txt").write_text("trace", encoding="utf-8")
    return trial


def _make_run(tmp_path, *, trials):
    run_dir = tmp_path / "run"
    job_dir = run_dir / "jobs" / "run-x"
    job_dir.mkdir(mode=0o700, parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps({"run_id": "run-x"}), encoding="utf-8")
    for name, kwargs in trials:
        _write_trial(job_dir, name, **kwargs)
    return run_dir, job_dir


@pytest.mark.unit
class TestScanInfrastructureFailures:
    def test_scan_classifies_only_unscored_infra_trials(self, tmp_path):
        env_fail = _exception("EnvironmentStartTimeoutError", "timed out")
        run_dir, job_dir = _make_run(tmp_path, trials=[
            ("env-fail__a1", {"exc": env_fail}),
            ("scored-pass__b2", {"reward": 1}),
            ("scored-fail__c3", {"reward": 0}),
            ("agent-timeout__d4", {"exc": _exception(
                "AgentTimeoutError", "Agent execution timed out")}),
        ])
        # A trial without result.json stays for harbor's native resume.
        (job_dir / "crashed__e5").mkdir(mode=0o700)

        failures = scan_infrastructure_failures(job_dir)
        assert [f.task_name for f in failures] == [
            "terminal-bench/env-fail"]
        assert failures[0].failure_class == ENVIRONMENT_START_TIMEOUT
        assert failures[0].exception_type == "EnvironmentStartTimeoutError"


@pytest.mark.unit
class TestArchiveInfrastructureFailures:
    def test_archive_moves_directory_whole_and_receipts(self, tmp_path):
        run_dir, job_dir = _make_run(tmp_path, trials=[
            ("env-fail__a1", {"exc": _exception(
                "EnvironmentStartTimeoutError", "timed out")}),
        ])
        failures = scan_infrastructure_failures(job_dir)
        receipts = archive_infrastructure_failures(
            failures,
            archive_root=run_dir / "infrastructure-attempts",
            attempt_index=1)
        assert not (job_dir / "env-fail__a1").exists()
        archived = run_dir / "infrastructure-attempts" / "attempt-1" \
            / "env-fail__a1"
        assert (archived / "result.json").is_file()
        assert (archived / "exception.txt").is_file()
        assert receipts[0]["taskName"] == "terminal-bench/env-fail"
        assert receipts[0]["failureClass"] == ENVIRONMENT_START_TIMEOUT
        assert receipts[0]["archivedTo"] == str(archived)

    def test_archive_refuses_to_repeat_attempt_index(self, tmp_path):
        run_dir, job_dir = _make_run(tmp_path, trials=[
            ("env-fail__a1", {"exc": _exception(
                "EnvironmentStartTimeoutError", "timed out")}),
        ])
        failures = scan_infrastructure_failures(job_dir)
        archive_infrastructure_failures(
            failures,
            archive_root=run_dir / "infrastructure-attempts",
            attempt_index=1)
        with pytest.raises(FileExistsError):
            archive_infrastructure_failures(
                failures,
                archive_root=run_dir / "infrastructure-attempts",
                attempt_index=1)


@pytest.mark.unit
class TestInfrastructureRetryCycle:
    def test_cycle_archives_then_dispatches_with_task_names(self, host_tmp_path):
        run_dir, job_dir = _make_run(host_tmp_path, trials=[
            ("env-fail__a1", {"exc": _exception(
                "EnvironmentStartTimeoutError", "timed out")}),
            ("scored-pass__b2", {"reward": 1}),
        ])
        dispatched = []

        def _resume(path, tasks):
            dispatched.append((path, tasks))
            return 0

        receipt = infrastructure_retry_cycle(
            run_dir,
            attempt_index=1,
            max_infrastructure_retries=2,
            resume=_resume)
        assert receipt["dispatched"] is True
        assert receipt["exitCode"] == 0
        assert len(receipt["archived"]) == 1
        assert dispatched == [(run_dir, ("terminal-bench/env-fail",))]
        # No infra failures remain → a second scan finds nothing to archive.
        receipt2 = infrastructure_retry_cycle(
            run_dir,
            attempt_index=2,
            max_infrastructure_retries=2,
            resume=_resume)
        assert receipt2["dispatched"] is False
        assert (run_dir / "infrastructure-retry-1.json").is_file()
        assert (run_dir / "infrastructure-retry-2.json").is_file()

    def test_cycle_enforces_preregistered_allowance(self, tmp_path):
        run_dir, _job_dir = _make_run(tmp_path, trials=[])
        with pytest.raises(ValueError, match="allowance"):
            infrastructure_retry_cycle(
                run_dir,
                attempt_index=3,
                max_infrastructure_retries=2,
                resume=lambda *_a: 0)
        with pytest.raises(ValueError, match="negative"):
            infrastructure_retry_cycle(
                run_dir,
                attempt_index=1,
                max_infrastructure_retries=-1,
                resume=lambda *_a: 0)


@pytest.mark.unit
class TestResumeArchiveAssertion:
    def test_unarchived_retry_task_is_refused(self, tmp_path):
        from evaluations.swebench.harbor_runner import (
            _assert_infrastructure_retry_tasks_archived)
        _run_dir, job_dir = _make_run(tmp_path, trials=[
            ("env-fail__a1", {"exc": _exception(
                "EnvironmentStartTimeoutError", "timed out")}),
        ])
        with pytest.raises(ValueError, match="not archived"):
            _assert_infrastructure_retry_tasks_archived(
                job_dir, ("terminal-bench/env-fail",))

    def test_archived_retry_task_passes(self, tmp_path):
        from evaluations.swebench.harbor_runner import (
            _assert_infrastructure_retry_tasks_archived)
        _run_dir, job_dir = _make_run(tmp_path, trials=[
            ("scored-pass__b2", {"reward": 1}),
        ])
        _assert_infrastructure_retry_tasks_archived(
            job_dir, ("terminal-bench/env-fail",))

    def test_empty_task_set_is_refused(self, tmp_path):
        from evaluations.swebench.harbor_runner import (
            _assert_infrastructure_retry_tasks_archived)
        _run_dir, job_dir = _make_run(tmp_path, trials=[])
        with pytest.raises(ValueError, match="must not be empty"):
            _assert_infrastructure_retry_tasks_archived(job_dir, ())
