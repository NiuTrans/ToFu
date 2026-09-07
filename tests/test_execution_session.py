"""Executable invariants for the shared server execution lifecycle."""

from __future__ import annotations

import threading

import pytest

from lib.agent_core.execution_session import (
    ExecutionPhase,
    ExecutionSession,
    ResourceDisposition,
    acquire_and_bind_admission,
    active_execution_snapshots,
    bind_model_route,
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


@pytest.mark.parametrize(
    "prepare",
    [
        lambda session: None,
        lambda session: session.mark_routed(),
        lambda session: (
            session.mark_routed(), session.mark_reserved()),
        lambda session: (
            session.mark_routed(), session.mark_admitted()),
        lambda session: session.mark_dispatch_started(),
    ],
    ids=["created", "routed", "reserved", "admitted", "dispatching"],
)
def test_cancellation_from_every_live_phase_releases_all_resources_once(prepare):
    execution_id = f"cancel-{id(prepare)}"
    session = _session(execution_id)
    released = []
    session.hold_resource(
        "billing", lambda _context: released.append("billing"),
        release_order=30, recoverable=True,
    )
    session.hold_resource(
        "route", lambda _context: released.append("route"),
        release_order=20,
    )
    session.hold_resource(
        "admission", lambda _context: released.append("admission"),
        release_order=10, recoverable=True,
    )
    prepare(session)

    receipt = session.settle(ExecutionPhase.CANCELLED, cause="fault_matrix")
    assert receipt.outcome is ExecutionPhase.CANCELLED
    assert receipt.invariants_satisfied is True
    assert released == ["billing", "route", "admission"]
    session.settle(ExecutionPhase.COMPLETED)
    assert released == ["billing", "route", "admission"]


def test_task_runtime_refuses_success_when_hard_resource_cleanup_fails():
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime("execution-failure-test", max_tasks=8, max_events=8)
    task = runtime.create(user_id=9, task_id="runtime-cleanup-failure")
    task["_executionSession"].hold_resource(
        "route",
        lambda _context: (_ for _ in ()).throw(RuntimeError("dispose failed")),
    )
    assert runtime.mark_running(task["id"]) is True
    assert runtime.finish(task["id"], result={"unsafe": "success"}) is True

    assert task["status"] == "error"
    assert task["error"]["kind"] == "generic"
    assert task["_executionSession"].phase is ExecutionPhase.FAILED
    terminal = task["events"][-1]
    assert terminal["type"] == "error"
    assert "result" not in terminal


def test_dispatch_cannot_start_after_terminal_settlement():
    session = _session("dispatch-after-terminal")
    session.settle(ExecutionPhase.CANCELLED)

    assert session.mark_dispatch_started() is False
    assert session.dispatch_started is False
    assert session.phase is ExecutionPhase.CANCELLED


def test_task_runtime_does_not_publish_running_after_session_is_terminal():
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime("execution-preterminal", max_tasks=8, max_events=8)
    task = runtime.create(user_id=9, task_id="runtime-preterminal")
    task["_executionSession"].settle(ExecutionPhase.FAILED)

    assert runtime.mark_running(task["id"]) is False
    assert task["status"] == "pending"


def test_task_runtime_cannot_publish_success_after_prior_execution_failure():
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime("execution-prior-failure", max_tasks=8, max_events=8)
    task = runtime.create(user_id=9, task_id="runtime-prior-failure")
    task["_executionSession"].settle(
        ExecutionPhase.FAILED, cause="preflight_failed")

    assert runtime.finish(task["id"], result={"unsafe": "success"}) is True
    assert task["status"] == "error"
    assert task["result"] == {"unsafe": "success"}
    assert task["events"][-1]["type"] == "error"
    assert "result" not in task["events"][-1]


def test_abort_cannot_rewrite_success_after_terminalization_begins():
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime("execution-terminal-fence", max_tasks=8, max_events=8)
    task = runtime.create(user_id=9, task_id="runtime-terminal-fence")
    assert runtime.mark_running(task["id"]) is True
    release_entered = threading.Event()
    allow_release = threading.Event()

    def release(_context):
        release_entered.set()
        assert allow_release.wait(timeout=2)

    task["_executionSession"].hold_resource("route", release)
    finished = []
    thread = threading.Thread(
        target=lambda: finished.append(
            runtime.finish(task["id"], result={"ok": True})
        )
    )
    thread.start()
    assert release_entered.wait(timeout=2)

    assert runtime.abort(task["id"]) is False
    assert runtime.remove_owned(task["id"], user_id=9) is False
    assert runtime.discard(task["id"]) is None
    assert runtime.get(task["id"]) is task
    allow_release.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert finished == [True]
    assert task["status"] == "done"
    assert task["abort_event"].is_set() is False


def test_terminal_task_cannot_be_removed_before_its_final_event():
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime(
        "execution-terminal-publish", ttl=0, max_tasks=8, max_events=8)
    task = runtime.create(user_id=9, task_id="runtime-terminal-publish")
    assert runtime.mark_running(task["id"]) is True
    append_entered = threading.Event()
    allow_append = threading.Event()
    original_append_event = runtime.append_event

    def blocked_append(task_id, event, **kwargs):
        append_entered.set()
        assert allow_append.wait(timeout=2)
        return original_append_event(task_id, event, **kwargs)

    runtime.append_event = blocked_append
    finished = []
    thread = threading.Thread(
        target=lambda: finished.append(runtime.finish(task["id"]))
    )
    thread.start()
    assert append_entered.wait(timeout=2)

    assert task["status"] == "done"
    assert runtime.remove_owned(task["id"], user_id=9) is False
    assert runtime.discard(task["id"]) is None
    assert runtime.cleanup_stale(max_age=0) == 0
    assert runtime.get(task["id"]) is task

    allow_append.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert finished == [True]
    assert task["events"][-1]["type"] == "done"
    assert runtime.remove_owned(task["id"], user_id=9) is True


def test_admission_store_failure_settles_earlier_resources():
    session = _session("admission-store-failure")
    released = []
    session.hold_resource(
        "model_route", lambda _context: released.append("model_route"))

    class FailingController:
        @staticmethod
        def acquire():
            raise RuntimeError("lease store unavailable")

    with pytest.raises(RuntimeError, match="lease store unavailable"):
        acquire_and_bind_admission(session, FailingController())

    assert released == ["model_route"]
    assert session.phase is ExecutionPhase.FAILED


def test_admission_bind_failure_releases_new_lease_and_existing_stack():
    session = _session("admission-bind-failure")
    released = []
    session.hold_resource(
        "admission", lambda _context: released.append("existing"),
        recoverable=True,
    )

    class Controller:
        @staticmethod
        def acquire():
            return "new-lease"

        @staticmethod
        def release(lease_id):
            released.append(lease_id)
            return True

    with pytest.raises(ValueError, match="already held"):
        acquire_and_bind_admission(session, Controller())

    assert released == ["new-lease", "existing"]
    assert session.phase is ExecutionPhase.FAILED


def test_route_bind_failure_rolls_back_new_and_existing_routes_once():
    session = _session("route-bind-failure")
    released = []
    session.hold_resource(
        "model_route", lambda _context: released.append("existing"))

    with pytest.raises(ValueError, match="already held"):
        bind_model_route(session, lambda: released.append("new"))

    assert released == ["new", "existing"]
    assert session.phase is ExecutionPhase.FAILED


def test_chat_discard_delegates_bound_route_cleanup_to_session_once():
    from lib.tasks_pkg.manager import discard_task
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    task = chat_task_runtime.create(
        user_id=9, task_id="execution-discard-route-once")
    route_group = object()
    task["_model_routing_group"] = route_group
    released = []
    bind_model_route(
        task["_executionSession"], lambda: released.append(route_group))

    discard_task(task["id"])

    assert released == [route_group]
    assert chat_task_runtime.get(task["id"]) is None


def test_execution_ids_and_resource_metric_labels_are_bounded():
    first = _session("duplicate-active-id")
    try:
        with pytest.raises(ValueError, match="active execution id"):
            _session("duplicate-active-id")
        with pytest.raises(ValueError, match="unsupported execution resource"):
            first.hold_resource("user-controlled-label", lambda _context: None)
    finally:
        first.settle(ExecutionPhase.CANCELLED)


def test_owner_removal_settles_private_execution_before_id_reuse():
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime("execution-remove", max_tasks=8, max_events=8)
    task = runtime.create(user_id=9, task_id="runtime-remove")
    released = []
    task["_executionSession"].hold_resource(
        "route", lambda _context: released.append("route"))

    assert runtime.remove_owned(task["id"], user_id=9) is True
    assert released == ["route"]
    assert task["_executionSession"].phase is ExecutionPhase.CANCELLED
    replacement = runtime.create(user_id=9, task_id="runtime-remove")
    runtime.remove_owned(replacement["id"], user_id=9)


def test_owner_removal_never_releases_under_live_dispatch():
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime("execution-live-remove", max_tasks=8, max_events=8)
    task = runtime.create(user_id=9, task_id="runtime-live-remove")
    released = []
    task["_executionSession"].hold_resource(
        "route", lambda _context: released.append("route"))
    assert runtime.mark_running(task["id"]) is True

    assert runtime.remove_owned(task["id"], user_id=9) is False
    assert task["abort_event"].is_set() is True
    assert runtime.get(task["id"]) is task
    assert released == []

    assert runtime.finish(task["id"]) is True
    assert released == ["route"]
    assert runtime.remove_owned(task["id"], user_id=9) is True
