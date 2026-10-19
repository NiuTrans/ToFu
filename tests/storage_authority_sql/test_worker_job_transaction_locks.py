"""Real PostgreSQL handoff/cancellation races at canonical worker row locks."""

from concurrent.futures import ThreadPoolExecutor
import threading
import uuid
import time

import pytest

from lib.storage_authority_sql.adapters.postgres import PostgresSession
from lib.storage_authority_sql.operations_pkg import _worker_jobs as jobs
from lib.storage_authority_sql.schema import initialize_schema

pytest_plugins = ("tests.support.postgres_cluster",)
pytestmark = pytest.mark.api


@pytest.fixture
def queued_job(postgres_cluster, monkeypatch):
    import psycopg
    from psycopg.rows import dict_row

    from lib.storage_authority_sql.worker_lease_clock import WorkerLeaseClock
    source = {'elapsed': 0, 'wall': 1000}
    clock = WorkerLeaseClock(wall_ns=lambda: source['wall'] * 1_000_000,
                            monotonic_ns=lambda: source['elapsed'] * 1_000_000)
    raw_session = PostgresSession
    epoch = uuid.uuid4().hex

    class BackendSession(raw_session):
        def __init__(self, connection, *args, **kwargs):
            super().__init__(connection, event_read_epoch=epoch, worker_lease_clock=clock)

    monkeypatch.setattr(__import__(__name__, fromlist=['PostgresSession']),
                        'PostgresSession', BackendSession)

    def connect():
        from lib.storage_authority_sql.adapters.postgres import _PgPool

        connection = psycopg.connect(postgres_cluster, row_factory=dict_row)
        _PgPool._configure(connection)
        return connection

    with connect() as connection:
        session = PostgresSession(connection)
        initialize_schema(session)
        jobs._worker_job_enqueue(
            session,
            {
                "task_id": "owned-job",
                "user_id": 41,
                "tenant_id": "owned-tenant",
                "task_kind": "owned-kind",
                "payload": {},
                "idempotency_key": "owned-job",
                "now_ms": 1000,
            },
        )
    connect.advance_ms = lambda value: source.update(elapsed=value - 1000, wall=value)
    return connect


def claim(session, *, now=1000, worker="worker"):
    return jobs._worker_job_claim_next(
        session,
        {
            "worker_id": worker,
            "task_kinds": ["owned-kind"],
            "now_ms": now,
            "lease_ms": 10000,
        },
    )


def cancel(session):
    return jobs._worker_job_request_cancel(
        session,
        {
            "task_id": "owned-job",
            "user_id": 41,
            "reason": "owner cancellation",
            "now_ms": 2000,
        },
    )


def test_cancel_decision_excludes_claim_until_its_transaction_commits(queued_job):
    selected = threading.Event()
    release = threading.Event()

    class PausedCancel(PostgresSession):
        def fetch_one_for_update(self, sql, params=()):
            result = super().fetch_one_for_update(sql, params)
            selected.set()
            assert release.wait(10)
            return result

    def cancelling():
        with queued_job() as connection:
            return cancel(PausedCancel(connection))

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cancelling)
        try:
            assert selected.wait(10)
            with queued_job() as connection:
                assert claim(PostgresSession(connection)) is None
        finally:
            release.set()
        result = future.result(timeout=10)
    assert result["job"]["status"] == "cancelled"
    assert result["job"]["fencingToken"] == 0


def test_cancel_after_claim_waits_for_the_current_running_state(queued_job):
    selected = threading.Event()
    release = threading.Event()
    cancel_pid = []
    cancel_started = threading.Event()

    class PausedClaim(PostgresSession):
        def fetch_one_for_update_skip_locked(self, sql, params=()):
            result = super().fetch_one_for_update_skip_locked(sql, params)
            if result is not None:
                selected.set()
                assert release.wait(10)
            return result

    def claiming():
        with queued_job() as connection:
            return claim(PausedClaim(connection))

    def cancelling():
        with queued_job() as connection:
            cancel_pid.append(connection.info.backend_pid)
            cancel_started.set()
            return cancel(PostgresSession(connection))

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = pool.submit(claiming)
        try:
            assert selected.wait(10)
            cancelled = pool.submit(cancelling)
            assert cancel_started.wait(10)
            # Observe PostgreSQL's own lock wait, not a timing guess that a
            # thread probably reached its decision before claim publication.
            with queued_job() as connection:
                connection.autocommit = True
                deadline = time.monotonic() + 10
                while True:
                    row = connection.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                        (cancel_pid[0],),
                    ).fetchone()
                    if row and row["wait_event_type"] == "Lock":
                        break
                    assert time.monotonic() < deadline, (
                        "Cancellation did not reach its row lock"
                    )
                    time.sleep(0.01)
        finally:
            release.set()
        assert claimed.result(timeout=10)["status"] == "running"
        result = cancelled.result(timeout=10)
    assert result["job"]["status"] == "running"
    assert result["job"]["cancelSequence"] == 1
    assert result["job"]["terminalAtMs"] == 0


def test_deferral_excludes_expired_claim_takeover_until_publication(queued_job):
    with queued_job() as connection:
        original = claim(PostgresSession(connection))
    selected = threading.Event()
    release = threading.Event()

    class PausedDeferral(PostgresSession):
        def execute(self, sql, params=()):
            result = super().execute(sql, params)
            if sql.startswith('UPDATE storage_worker_jobs SET status='):
                # Proof and publication are complete but uncommitted. A now
                # expired rival must skip the row held through this commit.
                selected.set()
                assert release.wait(10)
            return result

    def deferring():
        with queued_job() as connection:
            return jobs._worker_job_defer(
                PausedDeferral(connection),
                {
                    "task_id": "owned-job",
                    "worker_id": "worker",
                    "fencing_token": 1,
                    "now_ms": 2000,
                    "available_at_ms": 12000,
                    "replay_cursor": 7,
                    "observed_cancel_sequence": 0,
                },
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(deferring)
        try:
            assert selected.wait(10)
            queued_job.advance_ms(11000)
            with queued_job() as connection:
                assert (
                    claim(PostgresSession(connection), now=11000, worker="next") is None
                )
        finally:
            release.set()
        assert future.result(timeout=10)["job"]["status"] == "suspended"
    queued_job.advance_ms(12000)
    with queued_job() as connection:
        resumed = claim(PostgresSession(connection), now=12000, worker="next")
    assert resumed["fencingToken"] > original["fencingToken"]
    assert resumed["replayCursor"] == 7


def test_automation_progress_holds_its_claim_until_run_publication(queued_job):
    from lib.automations.definitions import prepare_definition
    from lib.automations.execution_repository import AutomationExecutionRepository
    from lib.automations.transitions import ExecutionFence, bind_fence
    from lib.identity import PrincipalContext
    from lib.storage_authority_sql.operation_registry import build_registry
    from tests.support.automation_fixtures import clock_definition, trigger

    registry = build_registry()

    class Port:
        def __init__(self, session_type=PostgresSession):
            self.session_type = session_type

        def query(self, operation, payload):
            with queued_job() as connection:
                return registry[operation].handler(
                    self.session_type(connection), payload
                )

        def command(self, operation, payload, command_id):
            return self.query(operation, payload)

    principal = PrincipalContext.user(
        subject_id="publisher",
        owner_user_id=41,
        tenant_id="owned-tenant",
        scopes={"automations:read", "automations:manage", "automations:execute"},
    )
    repo = AutomationExecutionRepository(
        principal, client_factory=lambda **kwargs: Port()
    )
    definition = repo.publish(
        prepare_definition(clock_definition()), expected_version=0, now=100
    )
    admitted = repo.admit(definition.id, trigger(), now=100)
    queued_job.advance_ms(100000)
    with queued_job() as connection:
        job = jobs._worker_job_claim_next(
            PostgresSession(connection),
            {
                "worker_id": "progress-worker",
                "task_kinds": ["automation-run"],
                "now_ms": 100000,
                "lease_ms": 10000,
            },
        )
    fence = ExecutionFence(job["taskId"], job["claimOwner"], job["fencingToken"])
    progress = bind_fence(admitted.value, fence, now=101)
    selected = threading.Event()
    release = threading.Event()

    class PausedProgress(PostgresSession):
        def execute(self, sql, params=()):
            result = super().execute(sql, params)
            if 'storage_records' in sql and sql.lstrip().startswith(('INSERT', 'UPDATE')):
                selected.set()
                assert release.wait(10)
            return result

    executor = AutomationExecutionRepository(
        principal, client_factory=lambda **kwargs: Port(PausedProgress)
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(executor.advance, admitted, progress, fence, now=101)
        try:
            assert selected.wait(10)
            queued_job.advance_ms(110000)
            with queued_job() as connection:
                takeover = jobs._worker_job_claim_next(
                    PostgresSession(connection),
                    {
                        "worker_id": "takeover-worker",
                        "task_kinds": ["automation-run"],
                        "now_ms": 110000,
                        "lease_ms": 10000,
                    },
                )
                assert takeover is None
        finally:
            release.set()
        assert future.result(timeout=10).value == progress
    with queued_job() as connection:
        takeover = jobs._worker_job_claim_next(
            PostgresSession(connection),
            {
                "worker_id": "takeover-worker",
                "task_kinds": ["automation-run"],
                "now_ms": 110000,
                "lease_ms": 10000,
            },
        )
    assert takeover["fencingToken"] > fence.token
