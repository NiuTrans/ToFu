"""Executable invariants for the shared server execution lifecycle."""

from __future__ import annotations

import threading

import pytest

from lib.agent_core.execution_session import (
    ExecutionPhase,
    ExecutionSession,
    ResourceDisposition,
    active_execution_snapshots,
    reconcile_overdue_execution_sessions,
)


pytestmark = pytest.mark.unit


def _session(execution_id: str = "exec-1", *, deadline_seconds=None):
    return ExecutionSession(
        execution_id=execution_id,
        kind="test",
        owner_user_id=7,
        deadline_seconds=deadline_seconds,
    )


def test_phases_are_monotonic_and_terminal_is_idempotent():
    session = _session()
    session.mark_routed()
    session.mark_admitted()
    session.mark_dispatch_started()
    with pytest.raises(ValueError, match="cannot regress"):
        session.mark_reserved()

    first = session.settle(ExecutionPhase.COMPLETED)
    second = session.settle(ExecutionPhase.FAILED, cause="late rewrite")
    assert second is first
    assert first.outcome is ExecutionPhase.COMPLETED
    assert first.invariants_satisfied is True


def test_concurrent_settlement_releases_each_resource_exactly_once():
    session = _session("exec-concurrent")
    calls = []
    calls_lock = threading.Lock()

    def release(context):
        with calls_lock:
            calls.append((context.execution_id, context.dispatch_started))

    session.hold_resource("route", release, release_order=20)
    session.mark_dispatch_started()
    barrier = threading.Barrier(20)
    receipts = []

    def worker():
        barrier.wait()
        receipts.append(session.settle(ExecutionPhase.COMPLETED))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == [("exec-concurrent", True)]
    assert len({id(receipt) for receipt in receipts}) == 1


def test_hard_cleanup_failure_refuses_success_but_recoverable_debt_defers():
    hard = _session("exec-hard")
    hard.hold_resource(
        "route", lambda _context: (_ for _ in ()).throw(RuntimeError("boom")))
    receipt = hard.settle(ExecutionPhase.COMPLETED)
    assert receipt.outcome is ExecutionPhase.FAILED
    assert receipt.invariants_satisfied is False
    assert receipt.resource_dispositions == (("route", ResourceDisposition.FAILED),)

    recoverable = _session("exec-recoverable")
    recoverable.hold_resource(
        "billing",
        lambda _context: (_ for _ in ()).throw(RuntimeError("sidecar down")),
        recoverable=True,
    )
    receipt = recoverable.settle(ExecutionPhase.COMPLETED)
    assert receipt.outcome is ExecutionPhase.COMPLETED
    assert receipt.invariants_satisfied is True
    assert receipt.resource_dispositions == (
        ("billing", ResourceDisposition.DEFERRED),
    )


def test_release_order_is_explicit_not_registration_order():
    session = _session("exec-order")
    released = []
    session.hold_resource(
        "admission", lambda _context: released.append("admission"),
        release_order=10,
    )
    session.hold_resource(
        "billing", lambda _context: released.append("billing"),
        release_order=30,
    )
    session.hold_resource(
        "route", lambda _context: released.append("route"),
        release_order=20,
    )
    session.settle(ExecutionPhase.COMPLETED)
    assert released == ["billing", "route", "admission"]


def test_deadline_reconciler_requests_cancel_without_releasing_live_resources(
        monkeypatch):
    import lib.agent_core.execution_session as execution

    now = [100.0]
    monkeypatch.setattr(execution.time, "monotonic", lambda: now[0])
    session = _session("exec-deadline", deadline_seconds=5)
    released = []
    session.hold_resource("admission", lambda _context: released.append(True))
    now[0] = 106.0

    assert reconcile_overdue_execution_sessions() == 1
    assert session.cancel_requested is True
    assert released == []
    assert reconcile_overdue_execution_sessions() == 0
    session.settle(ExecutionPhase.TIMED_OUT)
    assert released == [True]


def test_active_snapshot_is_content_free_and_terminal_session_disappears():
    session = _session("exec-snapshot")
    session.hold_resource("route", lambda _context: None)
    rows = active_execution_snapshots(limit=1024)
    row = next(item for item in rows if item["kind"] == "test"
               and "route" in item["held_resources"])
    assert "execution_id" not in row
    assert "owner_user_id" not in row
    assert "request_id" not in row
    session.settle(ExecutionPhase.CANCELLED)
    assert session.is_terminal is True


def test_task_runtime_owns_one_session_and_settles_before_terminal_event():
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime("execution-test", max_tasks=8, max_events=8)
    task = runtime.create(user_id=9, task_id="runtime-exec")
    session = task["_executionSession"]
    released = []
    session.hold_resource("route", lambda _context: released.append("route"))
    assert runtime.mark_running(task["id"]) is True
    assert session.phase is ExecutionPhase.DISPATCHING
    assert runtime.finish(task["id"], result={"ok": True}) is True
    assert released == ["route"]
    assert session.phase is ExecutionPhase.COMPLETED


def test_explicit_admission_lease_cannot_release_another_execution(monkeypatch):
    import lib.agent_core.admission as admission
    import lib.runtime_state_store as runtime_state_store

    runtime_state_store.reset_for_test()
    monkeypatch.setattr(
        admission, "_memory_pressure_allows_admission", lambda: True)
    controller = admission.AdmissionController(max_inflight=2)
    lease_a = controller.acquire()
    lease_b = controller.acquire()
    assert lease_a and lease_b and lease_a != lease_b
    assert controller.in_flight == 2

    assert controller.release(lease_a) is True
    assert controller.release(lease_a) is False
    assert controller.in_flight == 1
    assert controller.release(lease_b) is True
    assert controller.in_flight == 0
    controller.shutdown()
