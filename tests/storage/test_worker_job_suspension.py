"""Durable nonterminal suspension, cancellation wakeup, and old-fence refusal."""

import uuid
import json
from pathlib import Path
import threading

from tests.support.worker_job_time import wall_ms, wait_until_ms
import pytest

pytest_plugins = ("tests.support.semantic_authorities",)
pytestmark = pytest.mark.api


def start_claim(authority):
    factory, owner, engine = authority
    kind = "suspend-" + uuid.uuid4().hex
    task = uuid.uuid4().hex
    factory(write=True).command(
        "worker_job.enqueue",
        {
            "task_id": task,
            "user_id": owner,
            "tenant_id": "test-tenant",
            "task_kind": kind,
            "idempotency_key": task,
            "payload": {},
            "now_ms": 1000,
        },
        uuid.uuid4().hex,
    )
    return factory, owner, kind, claim(factory, kind, 1000)


def claim(factory, kind, now, worker="holder"):
    return factory(write=True).command(
        "worker_job.claim_next",
        {
            "worker_id": worker,
            "task_kinds": [kind],
            "now_ms": now,
            "lease_ms": 10000,
        },
        None,
    )


def due(job, value):
    return job['heartbeatAtMs'] + (value - 1000)


def defer(factory, job, *, now=2000, available=9000, observed=0):
    available = due(job, available)
    now = wall_ms()
    return factory(write=True).command(
        "worker_job.defer",
        {
            "task_id": job["taskId"],
            "worker_id": job["claimOwner"],
            "fencing_token": job["fencingToken"],
            "now_ms": now,
            "available_at_ms": available,
            "replay_cursor": 7,
            "observed_cancel_sequence": observed,
        },
        None,
    )


def cancel(factory, job, owner, now=3000):
    now = wall_ms()
    return factory(write=True).command(
        "worker_job.request_cancel",
        {
            "task_id": job["taskId"],
            "user_id": owner,
            "reason": "owner cancellation",
            "now_ms": now,
        },
        uuid.uuid4().hex,
    )


def test_suspension_releases_authority_and_replays_without_extending_wait(
    semantic_authority,
):
    factory, owner, kind, job = start_claim(semantic_authority)
    suspended = defer(factory, job)
    assert suspended["ok"] is True
    assert suspended["job"]["status"] == "suspended"
    assert suspended["job"]["leaseDeadlineMs"] == 0
    replay = defer(factory, job, now=2100)
    assert replay["replayed"] is True
    assert replay["job"] == suspended["job"]
    assert (
        factory().query(
            "worker_job.claim_state",
            {
                "task_id": job["taskId"],
                "worker_id": job["claimOwner"],
                "fencing_token": job["fencingToken"],
                "now_ms": 2100,
            },
        )["ok"]
        is False
    )
    assert claim(factory, kind, 8999) is None
    wait_until_ms(due(job, 9000))
    next_job = claim(factory, kind, 9000, worker="next")
    assert next_job["status"] == "running"
    assert next_job["fencingToken"] > job["fencingToken"]
    assert next_job["replayCursor"] == 7
    assert defer(factory, job, now=9100, available=10000)["ok"] is False


def test_cancellation_wakes_suspended_effect_for_domain_settlement(semantic_authority):
    factory, owner, kind, job = start_claim(semantic_authority)
    defer(factory, job)
    requested = cancel(factory, job, owner)
    assert requested["job"]["status"] == "suspended"
    assert requested["job"]["terminalAtMs"] == 0
    assert requested["job"]["availableAtMs"] == requested["job"]["cancelRequestedAtMs"]
    next_job = claim(factory, kind, 3000, worker="settler")
    assert next_job["cancelSequence"] == 1
    # Acknowledged cancellation can wait for an unknown external result;
    # repeated immediate claims would create a cancellation busy loop.
    waiting = defer(factory, next_job, now=3100, available=8000, observed=1)
    assert waiting["job"]["availableAtMs"] == due(next_job, 8000)
    assert claim(factory, kind, 7999) is None


def test_cancellation_racing_defer_cannot_be_hidden_by_future_availability(
    semantic_authority,
):
    factory, owner, kind, job = start_claim(semantic_authority)
    cancel(factory, job, owner, now=1900)
    deferred = defer(factory, job, now=2000, available=9000, observed=0)
    assert deferred["job"]["availableAtMs"] <= wall_ms()
    assert claim(factory, kind, 2000, worker="settler")["cancelSequence"] == 1


def test_expired_lease_cannot_defer_or_acknowledge_future_cancellation(
    semantic_authority,
):
    from lib.storage.errors import StorageError

    factory, owner, kind, job = start_claim(semantic_authority)
    with pytest.raises(StorageError) as error:
        defer(factory, job, now=2000, available=9000, observed=1)
    assert error.value.code == "database_protocol_error"
    wait_until_ms(job["leaseDeadlineMs"])
    assert defer(factory, job, now=11000, available=12000)["ok"] is False


def test_native_crash_retains_suspension_and_exact_ready_order(semantic_authority):
    factory, owner, kind, job = start_claim(semantic_authority)
    suspended = defer(factory, job)
    engine = semantic_authority[2]
    if engine is not None:
        engine.crash_cycle()
    saved = factory().query(
        "worker_job.get", {"task_id": job["taskId"], "user_id": owner}
    )
    assert saved == suspended["job"]
    assert claim(factory, kind, 8999) is None
    wait_until_ms(due(job, 9000))
    assert claim(factory, kind, 9000)["fencingToken"] > job["fencingToken"]


def runner_job(
    authority, *, handler, cancel_handler=None, failure_handler=None, store_type=None
):
    from lib.durable_worker import (
        StorageWorkerJobStore,
        SynchronousWorkerRunner,
        WorkerKindRegistration,
        WorkerKindSafety,
    )
    from lib.identity import PrincipalContext

    factory, owner, engine = authority
    kind = "runner-" + uuid.uuid4().hex
    task = uuid.uuid4().hex
    principal = PrincipalContext.system(
        subject_id="test-worker",
        owner_user_id=owner,
        tenant_id="test-tenant",
        scopes={"test:execute"},
    )
    factory(write=True).command(
        "worker_job.enqueue",
        {
            "task_id": task,
            "user_id": owner,
            "tenant_id": "test-tenant",
            "task_kind": kind,
            "idempotency_key": task,
            "payload": {
                "contract": "test.worker/v1",
                "principal": principal.to_payload(),
            },
            "now_ms": 1000,
        },
        uuid.uuid4().hex,
    )

    class AuthorityStore(store_type or StorageWorkerJobStore):
        def _client(self, *, write=False):
            return factory(write=write)

    clock = {"now": wall_ms()}
    registration = WorkerKindRegistration(
        kind,
        handler,
        WorkerKindSafety(True, True, True, True),
        cancel_handler=cancel_handler,
        failure_handler=failure_handler,
    )
    runner = SynchronousWorkerRunner(
        worker_id="test-runner",
        store=AuthorityStore(),
        registrations=[registration],
        lease_ms=10000,
        heartbeat_interval_ms=10,
        now_ms=lambda: clock["now"],
    )
    return runner, factory, owner, kind, task, clock


def test_canonical_runner_suspends_and_resumes_one_job(semantic_authority):
    from lib.durable_worker import WorkerJobOutcome, WorkerJobSuspension

    calls = []

    def handler(context, payload):
        calls.append(context.fencing_token)
        if len(calls) == 1:
            return WorkerJobSuspension(clock["now"] + 2000, 7)
        assert context.replay_cursor == 7
        return WorkerJobOutcome(
            status="succeeded", result_ref="test:durable-settlement", replay_cursor=8
        )

    runner, factory, owner, kind, task, clock = runner_job(
        semantic_authority, handler=handler
    )
    assert runner.run_once().state == "suspended"
    heartbeat_name = f'durable-worker-heartbeat:{task[:32]}'
    assert all(thread.name != heartbeat_name for thread in threading.enumerate())
    assert (
        factory().query("worker_job.get", {"task_id": task, "user_id": owner})["status"]
        == "suspended"
    )
    assert runner.run_once().state == "idle"
    wait_until_ms(clock["now"] + 2000)
    clock["now"] = wall_ms()
    assert runner.run_once().state == "succeeded"
    assert calls == [1, 2]


def test_registered_cancel_handler_waits_for_external_settlement(semantic_authority):
    from lib.durable_worker import WorkerJobOutcome, WorkerJobSuspension

    external_settled = threading.Event()
    normal_calls = []
    cancellation_calls = []

    def cancel_handler(context, payload):
        cancellation_calls.append(context.cancellation_sequence)
        context.checkpoint(allow_cancelled=True)
        if not external_settled.is_set():
            return WorkerJobSuspension(clock["now"] + 2000, 7)
        return WorkerJobOutcome(
            status="cancelled", result_ref="test:settled-cancellation", replay_cursor=8
        )

    runner, factory, owner, kind, task, clock = runner_job(
        semantic_authority,
        handler=lambda context, payload: normal_calls.append(True),
        cancel_handler=cancel_handler,
    )
    first = claim(factory, kind, 1000, worker="old")
    cancel(factory, first, owner, now=1900)
    wait_until_ms(first["leaseDeadlineMs"])
    clock["now"] = wall_ms()
    assert runner.run_once().state == "suspended"
    assert normal_calls == []
    saved = factory().query("worker_job.get", {"task_id": task, "user_id": owner})
    assert saved["status"] == "suspended"
    assert saved["terminalAtMs"] == 0
    assert saved["availableAtMs"] == clock["now"] + 2000
    assert runner.run_once().state == "idle"
    external_settled.set()
    wait_until_ms(saved["availableAtMs"])
    clock["now"] = wall_ms()
    assert runner.run_once().state == "cancelled"
    assert cancellation_calls == [1, 1]


def test_unknown_cleanup_never_commits_a_false_terminal_job(semantic_authority):
    def handler(context, payload):
        raise RuntimeError("action result unknown")

    def failure_handler(context, payload, error):
        raise RuntimeError("durable settlement unavailable")

    runner, factory, owner, kind, task, clock = runner_job(
        semantic_authority, handler=handler, failure_handler=failure_handler
    )
    assert runner.run_once().state == "settlement_unconfirmed"
    saved = factory().query("worker_job.get", {"task_id": task, "user_id": owner})
    assert saved["status"] == "running"
    assert saved["terminalAtMs"] == 0


def test_pending_heartbeat_reaches_quiescence_before_suspension(semantic_authority):
    from lib.durable_worker import StorageWorkerJobStore, WorkerJobSuspension

    entered = threading.Event()
    released = threading.Event()
    exited = threading.Event()
    handler_returning = threading.Event()
    finished = threading.Event()

    class BlockingHeartbeatStore(StorageWorkerJobStore):
        def heartbeat(self, *args, **kwargs):
            entered.set()
            assert released.wait(10)
            result = super().heartbeat(*args, **kwargs)
            exited.set()
            return result

        def defer(self, *args, **kwargs):
            assert exited.is_set()
            return super().defer(*args, **kwargs)

    def handler(context, payload):
        assert entered.wait(10)
        handler_returning.set()
        return WorkerJobSuspension(clock["now"] + 2000)

    runner, factory, owner, kind, task, clock = runner_job(
        semantic_authority, handler=handler, store_type=BlockingHeartbeatStore
    )
    results = []

    def run():
        try:
            results.append(runner.run_once())
        finally:
            finished.set()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert handler_returning.wait(10)
        assert not finished.is_set()
        released.set()
        assert finished.wait(10)
        assert results[0].state == "suspended"
    finally:
        released.set()
        thread.join(10)
    assert not thread.is_alive()


def test_cancellation_arriving_during_teardown_keeps_cleanup_supervised(
    semantic_authority,
):
    from lib.durable_worker import (
        StorageWorkerJobStore,
        WorkerJobOutcome,
        WorkerJobSuspension,
    )

    first_heartbeat = threading.Event()
    normal_validated = threading.Event()
    cleanup_heartbeat = threading.Event()
    factory, owner, _ = semantic_authority

    class LateCancelStore(StorageWorkerJobStore):
        def heartbeat(self, claim, **kwargs):
            if not first_heartbeat.is_set():
                first_heartbeat.set()
                assert normal_validated.wait(10)
                factory(write=True).command(
                    "worker_job.request_cancel",
                    {
                        "task_id": claim.task_id,
                        "user_id": owner,
                        "reason": "cancel during teardown",
                        "now_ms": kwargs["now_ms"],
                    },
                    uuid.uuid4().hex,
                )
            else:
                cleanup_heartbeat.set()
            return super().heartbeat(claim, **kwargs)

    def handler(context, payload):
        assert first_heartbeat.wait(10)
        return WorkerJobOutcome(status="succeeded")

    def cancel_handler(context, payload):
        assert cleanup_heartbeat.wait(10)
        context.checkpoint(allow_cancelled=True)
        return WorkerJobSuspension(clock["now"] + 2000)

    runner, factory, owner, kind, task, clock = runner_job(
        semantic_authority,
        handler=handler,
        cancel_handler=cancel_handler,
        store_type=LateCancelStore,
    )
    validate = runner._validate_handler_outcome

    def release_after_validation(context, outcome, **kwargs):
        result = validate(context, outcome, **kwargs)
        normal_validated.set()
        return result

    runner._validate_handler_outcome = release_after_validation
    assert runner.run_once().state == "suspended"
    saved = factory().query("worker_job.get", {"task_id": task, "user_id": owner})
    assert saved["status"] == "suspended"
    assert saved["cancelSequence"] == 1
    assert saved["availableAtMs"] == clock["now"] + 2000


def test_lifecycle_consumers_match_the_canonical_states():
    from lib.storage.generated_worker_job_contract import (
        WORKER_JOB_STATES,
        WORKER_TERMINAL_STATES,
    )

    source = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "contracts/worker_job_lifecycle_v1.json"
        ).read_text()
    )
    assert WORKER_JOB_STATES == frozenset(source["states"])
    assert WORKER_TERMINAL_STATES == frozenset(source["terminalStates"])
