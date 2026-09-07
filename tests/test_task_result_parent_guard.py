"""Owner and parent fences for durable executor checkpoints."""

from __future__ import annotations

import time

import pytest

from tests._seed import delete_conversation, seed_conversation


pytest_plugins = ("tests._chat_sidecar",)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("chat_sidecar")]


def _task(conversation_id: str, *, user_id=1, inline=False) -> dict:
    task = {
        "id": f"task-result-{time.time_ns()}",
        "convId": conversation_id,
        "_userId": user_id,
        "created_at": time.time(),
    }
    if inline:
        task["_inline_messages"] = True
    return task


def _write(task: dict) -> bool:
    from lib.tasks_pkg.manager._persist import _upsert_task_row

    return _upsert_task_row(
        task,
        task["convId"],
        content="checkpoint",
        thinking="",
        status="done",
        error_json=None,
        tr_json=None,
        meta_json=None,
    )


def _record(task_id: str):
    from lib.storage import get_storage_client

    return get_storage_client().query(
        "record.get", {"namespace": "task_results", "key": task_id}
    )


def test_conversation_backed_checkpoint_requires_its_owner_parent():
    conversation_id = f"task-parent-{time.time_ns()}"
    seed_conversation(conversation_id, user_id=7)

    owned = _task(conversation_id, user_id=7)
    foreign = _task(conversation_id, user_id=8)

    assert _write(owned) is True
    assert _record(owned["id"])["value"]["user_id"] == 7
    assert _write(foreign) is False
    assert _record(foreign["id"]) is None


def test_checkpoint_commits_staged_cache_facts_with_the_task_result(
        monkeypatch):
    from lib.storage import get_storage_client
    from lib.task_result_checkpoint_contract import (
        TASK_CACHE_PREFIX_HWM_CANDIDATE_FIELD,
        TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD,
        TASK_RESULT_CACHE_PREFIX_HWM_FIELD,
        TASK_RESULT_LAST_TURN_CACHE_READ_FIELD,
    )

    conversation_id = f"task-cache-facts-{time.time_ns()}"
    seed_conversation(
        conversation_id,
        user_id=7,
        settings={
            "cachePrefixHWM": 23,
            "lastTurnCacheRead": 1_000,
            "unrelated": "preserved",
        },
    )
    task = _task(conversation_id, user_id=7)
    task[TASK_CACHE_PREFIX_HWM_CANDIDATE_FIELD] = 19
    task[TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD] = 3_400
    # A new Sidecar capability echo must make both legacy writers unreachable.
    monkeypatch.setattr(
        "lib.tasks_pkg.cache_tracking._persist.advance_persisted_boundary",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "lib.tasks_pkg.cache_tracking._persist.write_last_turn_cache_read",
        lambda *_args, **_kwargs: False,
    )

    assert _write(task) is True

    value = _record(task["id"])["value"]
    assert value[TASK_RESULT_CACHE_PREFIX_HWM_FIELD] == 19
    assert value[TASK_RESULT_LAST_TURN_CACHE_READ_FIELD] == 3_400
    document = get_storage_client().query(
        "conversation.get", {
            "conv_id": conversation_id,
            "user_id": 7,
            "derive_messages": False,
        },
    )
    assert document["metadata"]["settings"] == {
        "cachePrefixHWM": 23,
        "lastTurnCacheRead": 3_400,
        "unrelated": "preserved",
    }
    assert TASK_CACHE_PREFIX_HWM_CANDIDATE_FIELD not in task
    assert TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD not in task

    # The checkpoint response also refreshes both process-local read caches;
    # no follow-up conversation query is needed on this process.
    from lib.tasks_pkg.cache_tracking import _persist as cache_persist
    monkeypatch.setattr(
        cache_persist,
        "_conversation_settings",
        lambda *_args, **_kwargs: pytest.fail("unexpected cache-fact read"),
    )
    assert cache_persist.read_persisted_boundary(
        conversation_id, user_id=7) == 23
    assert cache_persist.read_last_turn_cache_read(
        conversation_id, user_id=7) == 3_400


def test_deleted_or_missing_parent_cannot_be_resurrected_by_late_checkpoint():
    task = _task(f"missing-parent-{time.time_ns()}")

    assert _write(task) is False
    assert _record(task["id"]) is None


def test_guarded_followup_rechecks_parent_inside_the_checkpoint_transaction():
    conversation_id = f"task-parent-late-delete-{time.time_ns()}"
    seed_conversation(conversation_id, user_id=7)
    task = _task(conversation_id, user_id=7)

    assert _write(task) is True
    before = _record(task["id"])
    assert task["_taskResultCheckpointGuard"].endswith("/v1")
    delete_conversation(conversation_id, user_id=7)

    assert _write(task) is False
    after = _record(task["id"])
    assert after["version"] == before["version"]


def test_inline_checkpoint_has_no_conversation_parent():
    task = _task("", user_id=9, inline=True)

    assert _write(task) is True
    assert _record(task["id"])["value"]["user_id"] == 9


def test_checkpoint_without_owner_fails_closed():
    task = _task("", inline=True)
    task.pop("_userId")

    with pytest.raises(ValueError, match="task result checkpoint"):
        _write(task)


def test_confirmed_guard_collapses_steady_checkpoint_to_one_sidecar_rpc(
        monkeypatch):
    from lib.task_result_checkpoint_contract import (
        TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
    )
    from lib.tasks_pkg.manager._persist import _upsert_task_row

    class FakeClient:
        def __init__(self):
            self.queries = []
            self.commands = []
            self.version = 0

        def query(self, operation, payload):
            self.queries.append((operation, payload))
            if operation == "conversation.get":
                return {"id": payload["conv_id"]}
            assert operation == "record.get"
            return None

        def command(
            self,
            operation,
            payload,
            command_id,
            priority="user",
            deadline=None,
        ):
            self.commands.append((operation, payload, priority, deadline))
            self.version += 1
            return {
                "key": payload["key"],
                "version": self.version,
                "updated_at_ms": self.version,
                "owned": True,
                "guard_contract": TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
            }

    client = FakeClient()
    monkeypatch.setattr(
        "lib.storage.get_storage_client", lambda write=False: client,
    )
    task = _task("guarded-parent", user_id=7)

    assert _upsert_task_row(
        task,
        task["convId"],
        content="",
        thinking="",
        status="pending",
        error_json=None,
        tr_json=None,
        meta_json=None,
    ) is True
    assert [operation for operation, _payload in client.queries] == [
        "conversation.get", "record.get",
    ]
    assert client.commands[0][2:] == ("user", None)

    assert _upsert_task_row(
        task,
        task["convId"],
        content="checkpoint",
        thinking="",
        status="running",
        error_json=None,
        tr_json=None,
        meta_json=None,
    ) is True
    assert len(client.queries) == 2
    assert len(client.commands) == 2
    assert client.commands[1][1]["expected_version"] == 1
    assert client.commands[1][1]["require_parent"] is True
    assert client.commands[1][2:] == ("maintenance", 0.5)
    assert task["_taskResultVersion"] == 2


def test_old_sidecar_response_retains_legacy_preflight_reads(monkeypatch):
    from lib.tasks_pkg.manager._persist import _upsert_task_row

    class OldSidecarClient:
        def __init__(self):
            self.queries = []
            self.version = 0

        def query(self, operation, payload):
            self.queries.append(operation)
            if operation == "conversation.get":
                return {"id": payload["conv_id"]}
            return {
                "value": {"status": "pending", "user_id": 7},
                "version": self.version,
            } if self.version else None

        def command(self, _operation, _payload, _command_id, **_options):
            self.version += 1
            # An older peer ignores additive request members and cannot echo
            # the guard contract, so the manager must not trust cached state.
            return {"key": "old-peer", "version": self.version}

    client = OldSidecarClient()
    monkeypatch.setattr(
        "lib.storage.get_storage_client", lambda write=False: client,
    )
    task = _task("old-peer-parent", user_id=7)
    common = dict(
        content="checkpoint",
        thinking="",
        status="pending",
        error_json=None,
        tr_json=None,
        meta_json=None,
    )

    assert _upsert_task_row(task, task["convId"], **common) is True
    assert _upsert_task_row(task, task["convId"], **common) is True
    assert client.queries == [
        "conversation.get", "record.get",
        "conversation.get", "record.get",
    ]
    assert "_taskResultCheckpointGuard" not in task


def test_old_sidecar_response_falls_back_per_cache_fact(monkeypatch):
    from lib.task_result_checkpoint_contract import (
        TASK_CACHE_PREFIX_HWM_CANDIDATE_FIELD,
        TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD,
        TASK_RESULT_CACHE_PREFIX_HWM_FIELD,
        TASK_RESULT_CACHE_SETTINGS_CONTRACT,
        TASK_RESULT_LAST_TURN_CACHE_READ_FIELD,
    )
    from lib.tasks_pkg.manager._persist import _upsert_task_row

    class OldSidecarClient:
        def __init__(self):
            self.payload = None

        def query(self, operation, payload):
            if operation == "conversation.get":
                return {"id": payload["conv_id"]}
            assert operation == "record.get"
            return None

        def command(self, _operation, payload, _command_id, **_options):
            self.payload = payload
            return {"key": payload["key"], "version": 1}

    legacy_calls = []
    monkeypatch.setattr(
        "lib.tasks_pkg.cache_tracking._persist.advance_persisted_boundary",
        lambda conv_id, value, *, user_id: legacy_calls.append(
            ("hwm", conv_id, value, user_id)) or False,
    )
    monkeypatch.setattr(
        "lib.tasks_pkg.cache_tracking._persist.write_last_turn_cache_read",
        lambda conv_id, value, *, user_id: legacy_calls.append(
            ("last", conv_id, value, user_id)) or True,
    )
    client = OldSidecarClient()
    monkeypatch.setattr(
        "lib.storage.get_storage_client", lambda write=False: client,
    )
    task = _task("old-peer-cache-parent", user_id=7)
    task[TASK_CACHE_PREFIX_HWM_CANDIDATE_FIELD] = 19
    task[TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD] = 3_400

    assert _upsert_task_row(
        task,
        task["convId"],
        content="checkpoint",
        thinking="",
        status="done",
        error_json=None,
        tr_json=None,
        meta_json=None,
    ) is True

    assert client.payload["cache_settings_contract"] == (
        TASK_RESULT_CACHE_SETTINGS_CONTRACT
    )
    assert client.payload["value"][TASK_RESULT_CACHE_PREFIX_HWM_FIELD] == 19
    assert (
        client.payload["value"][TASK_RESULT_LAST_TURN_CACHE_READ_FIELD]
        == 3_400
    )
    assert legacy_calls == [
        ("hwm", "old-peer-cache-parent", 19, 7),
        ("last", "old-peer-cache-parent", 3_400, 7),
    ]
    assert task[TASK_CACHE_PREFIX_HWM_CANDIDATE_FIELD] == 19
    assert TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD not in task


def test_checkpoint_does_not_clear_newer_staged_cache_facts(monkeypatch):
    from lib.task_result_checkpoint_contract import (
        TASK_CACHE_PREFIX_HWM_CANDIDATE_FIELD,
        TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD,
        TASK_RESULT_CACHE_PREFIX_HWM_FIELD,
        TASK_RESULT_CACHE_SETTINGS_CONTRACT,
        TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
        TASK_RESULT_LAST_TURN_CACHE_READ_FIELD,
    )
    from lib.tasks_pkg.manager._persist import _upsert_task_row

    task = _task("overlap-cache-parent", user_id=7)
    task[TASK_CACHE_PREFIX_HWM_CANDIDATE_FIELD] = 10
    task[TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD] = 3_000

    class OverlapClient:
        def query(self, operation, payload):
            if operation == "conversation.get":
                return {"id": payload["conv_id"]}
            assert operation == "record.get"
            return None

        def command(self, _operation, payload, _command_id, **_options):
            assert payload["value"][TASK_RESULT_CACHE_PREFIX_HWM_FIELD] == 10
            assert (
                payload["value"][TASK_RESULT_LAST_TURN_CACHE_READ_FIELD]
                == 3_000
            )
            task[TASK_CACHE_PREFIX_HWM_CANDIDATE_FIELD] = 20
            task[TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD] = 4_000
            return {
                "key": payload["key"],
                "version": 1,
                "updated_at_ms": 1,
                "owned": True,
                "guard_contract": TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
                "cache_settings_contract": TASK_RESULT_CACHE_SETTINGS_CONTRACT,
                "cache_settings_committed": True,
                TASK_RESULT_CACHE_PREFIX_HWM_FIELD: 10,
                TASK_RESULT_LAST_TURN_CACHE_READ_FIELD: 3_000,
            }

    monkeypatch.setattr(
        "lib.storage.get_storage_client", lambda write=False: OverlapClient(),
    )

    assert _upsert_task_row(
        task,
        task["convId"],
        content="checkpoint",
        thinking="",
        status="running",
        error_json=None,
        tr_json=None,
        meta_json=None,
    ) is True
    assert task[TASK_CACHE_PREFIX_HWM_CANDIDATE_FIELD] == 20
    assert task[TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD] == 4_000


def test_incomplete_guard_echo_does_not_authorize_cached_fast_path(monkeypatch):
    from lib.task_result_checkpoint_contract import (
        TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
    )
    from lib.tasks_pkg.manager._persist import _upsert_task_row

    class IncompleteGuardClient:
        def __init__(self):
            self.queries = 0
            self.version = 0

        def query(self, operation, payload):
            self.queries += 1
            if operation == "conversation.get":
                return {"id": payload["conv_id"]}
            return None

        def command(self, _operation, _payload, _command_id, **_options):
            self.version += 1
            return {
                "guard_contract": TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
                "version": self.version,
                # Missing the contract's boolean ``owned`` witness.
            }

    client = IncompleteGuardClient()
    monkeypatch.setattr(
        "lib.storage.get_storage_client", lambda write=False: client,
    )
    task = _task("incomplete-guard-parent", user_id=7)
    common = dict(
        content="checkpoint",
        thinking="",
        status="pending",
        error_json=None,
        tr_json=None,
        meta_json=None,
    )

    assert _upsert_task_row(task, task["convId"], **common) is True
    assert _upsert_task_row(task, task["convId"], **common) is True
    assert client.queries == 4
    assert "_taskResultCheckpointGuard" not in task


def test_cas_exhaustion_is_pressure_not_an_ownership_fence(monkeypatch):
    from lib.storage import StorageError
    from lib.task_result_checkpoint_contract import (
        TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
    )
    from lib.tasks_pkg.manager._persist import _upsert_task_row

    class ContendedClient:
        def __init__(self):
            self.commands = 0
            self.version = 1

        def query(self, operation, payload):
            assert operation == "record.get"
            assert payload["key"] == task["id"]
            self.version += 1
            return {
                "version": self.version,
                "value": {"status": "running", "user_id": 7},
            }

        def command(self, _operation, _payload, _command_id, **_options):
            self.commands += 1
            raise StorageError(
                "database_conflict", "concurrent checkpoint advanced",
            )

    task = _task("", user_id=7, inline=True)
    task["_taskResultCheckpointGuard"] = (
        TASK_RESULT_CHECKPOINT_GUARD_CONTRACT
    )
    task["_taskResultVersion"] = 1
    client = ContendedClient()
    monkeypatch.setattr(
        "lib.storage.get_storage_client", lambda write=False: client,
    )

    with pytest.raises(StorageError) as raised:
        _upsert_task_row(
            task,
            "",
            content="checkpoint",
            thinking="",
            status="running",
            error_json=None,
            tr_json=None,
            meta_json=None,
        )
    assert raised.value.code == "database_conflict"
    assert client.commands == 2
