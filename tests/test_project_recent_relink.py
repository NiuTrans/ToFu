"""tests/test_project_recent_relink.py — project.relink re-keys identity.

A directory rename/move does not change project identity. The ``project.relink``
command re-keys every owner-scoped aggregate in one transaction:

  * the recent entry keeps count/last_used (merging an entry the new path
    already owns — counts add, last_used takes the max);
  * the complete signal-driven Project Brain projection and event stream
    follow as one authority;
  * persisted conversation project pins follow without touching transcript
    revisions or unrelated settings;
  * a missing old entry / identical paths are hard protocol errors;
  * everything is scoped to the owning user.

Pins cover the sidecar op (real SQLite), the repository dispatch (op name +
payload keys), and the config facade wiring.
"""

from __future__ import annotations

import sqlite3

import pytest

from lib.storage.errors import StorageError
from lib.conversations.project_brain import deterministic_work_id
from lib.storage_sidecar.adapters.sqlite import SQLiteSession
from lib.storage_sidecar.schema import initialize_schema
from lib.storage_sidecar.operations_pkg._project_brain import (
    _project_brain_get,
    _project_brain_narrative_add,
    _project_brain_rebuild,
    _project_brain_work_start,
)
from lib.storage_sidecar.operations_pkg._project import (
    _conversation_project_relink,
    _project_recent_list,
    _project_recent_touch,
    _project_relink,
)
from lib.storage_sidecar.operations_pkg._common import _dump, _load

pytestmark = pytest.mark.unit


@pytest.fixture
def session():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    sess = SQLiteSession(connection)
    initialize_schema(sess)
    try:
        yield sess
    finally:
        connection.close()


def _touch(session, user_id, path, last_used, times=1):
    for i in range(times):
        _project_recent_touch(session, {
            "user_id": user_id,
            "project_path": path,
            "last_used": last_used + i,
        })


def _conversation(session, conv_id, user_id, settings, rev=0):
    session.execute(
        "INSERT INTO storage_conversations(id,user_id,settings_json,rev) "
        "VALUES(?,?,?,?)",
        (conv_id, user_id, _dump(settings), rev),
    )


def _settings(session, conv_id, user_id):
    row = session.fetch_one(
        "SELECT settings_json,rev FROM storage_conversations "
        "WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    return _load(row["settings_json"]), int(row["rev"])


def _trashed_conversation(session, conv_id, user_id, settings, rev=0):
    session.execute(
        "INSERT INTO storage_conversation_trash"
        "(conversation_id,user_id,settings_json,rev,deleted_at_ms) "
        "VALUES(?,?,?,?,?)",
        (conv_id, user_id, _dump(settings), rev, 1),
    )


def _trashed_settings(session, conv_id, user_id):
    row = session.fetch_one(
        "SELECT settings_json,rev FROM storage_conversation_trash "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    return _load(row["settings_json"]), int(row["rev"])


def test_relink_rekeys_recent_and_merges_existing_entry(session):
    _touch(session, 7, "/old/p", 1000, times=2)
    _touch(session, 7, "/new/p", 1005)
    out = _project_relink(
        session, {"user_id": 7, "old_path": "/old/p", "new_path": "/new/p"})
    assert out["project"]["path"] == "/new/p"
    assert out["project"]["count"] == 3          # 2 touches + merged 1
    assert out["project"]["last_used"] == 1005   # max wins
    paths = [p["path"] for p in _project_recent_list(session, {"user_id": 7})]
    assert paths == ["/new/p"]


def test_relink_moves_project_brain_projection_and_events(session):
    _touch(session, 7, "/x", 5)
    _project_brain_work_start(session, {
        'owner_user_id': 7,
        'project_key': '/x',
        'work_item': {
            'id': deterministic_work_id('task-relink'),
            'taskId': 'task-relink',
            'conversationId': 'conv-original', 'title': 'Relink me',
            'trigger': 'file_write', 'status': 'active',
            'changedPaths': [], 'artifacts': [], 'resultSummary': '',
            'startedAt': 1, 'finishedAt': None,
            '_titlePriority': 100, '_titleRefined': False,
        },
        'timestamp': 1,
    })
    out = _project_relink(
        session, {"user_id": 7, "old_path": "/x", "new_path": "/y"})
    assert out['projectBrainMoved'] is True
    assert _project_brain_get(session, {
        'owner_user_id': 7, 'project_key': '/x'})['workItems'] == []
    moved = _project_brain_get(session, {
        'owner_user_id': 7, 'project_key': '/y'})
    assert moved['projectKey'] == '/y'
    assert moved['workItems'][0]['conversationId'] == 'conv-original'
    event = session.fetch_one(
        'SELECT task_id,project_key FROM storage_events '
        "WHERE stream_kind='project_brain'")
    assert event['project_key'] == '/y'
    assert event['task_id'] == 'project-brain:7:/y'


def test_relink_merges_both_project_brain_authorities_into_checkpoint(session):
    _touch(session, 7, "/x", 5)
    for project_key, task_id, timestamp in (
        ("/y", "task-new-path", 10),
        ("/x", "task-stale-tab", 20),
    ):
        _project_brain_work_start(session, {
            "owner_user_id": 7,
            "project_key": project_key,
            "work_item": {
                "id": deterministic_work_id(task_id),
                "taskId": task_id,
                "conversationId": "conv-" + task_id,
                "title": task_id,
                "trigger": "file_write",
                "status": "active",
                "changedPaths": [],
                "artifacts": [],
                "resultSummary": "",
                "startedAt": timestamp,
                "finishedAt": None,
                "_titlePriority": 100,
                "_titleRefined": False,
            },
            "timestamp": timestamp,
        })
        _project_brain_narrative_add(session, {
            "owner_user_id": 7,
            "project_key": project_key,
            "kind": "note",
            "text": "narrative " + task_id,
            "conversation_id": "conv-" + task_id,
            "timestamp": timestamp + 1,
        })

    out = _project_relink(
        session, {"user_id": 7, "old_path": "/x", "new_path": "/y"})

    assert out["projectBrainMoved"] is True
    assert _project_brain_get(session, {
        "owner_user_id": 7, "project_key": "/x",
    })["workItems"] == []
    merged = _project_brain_get(session, {
        "owner_user_id": 7, "project_key": "/y",
    })
    assert {item["taskId"] for item in merged["workItems"]} == {
        "task-new-path", "task-stale-tab",
    }
    assert [item["text"] for item in merged["narratives"]] == [
        "narrative task-new-path", "narrative task-stale-tab",
    ]
    assert session.fetch_one(
        "SELECT 1 AS present FROM storage_events "
        "WHERE owner_user_id=7 AND project_key='/x'"
    ) is None
    checkpoint = session.fetch_one(
        "SELECT event_kind FROM storage_events "
        "WHERE owner_user_id=7 AND project_key='/y' "
        "ORDER BY project_sequence DESC LIMIT 1"
    )
    assert checkpoint["event_kind"] == "projection_checkpoint"

    rebuilt = _project_brain_rebuild(session, {
        "owner_user_id": 7, "project_key": "/y",
    })
    assert {item["taskId"] for item in rebuilt["projection"]["workItems"]} == {
        "task-new-path", "task-stale-tab",
    }
    assert [item["text"] for item in rebuilt["projection"]["narratives"]] == [
        "narrative task-new-path", "narrative task-stale-tab",
    ]


def test_relink_without_project_brain_projection_reports_false(session):
    _touch(session, 7, "/old/p", 1)
    out = _project_relink(
        session, {"user_id": 7, "old_path": "/old/p", "new_path": "/new/p"})
    assert out['projectBrainMoved'] is False


def test_relink_moves_conversation_project_settings_without_advancing_rev(session):
    _touch(session, 7, "/old/p", 1)
    _conversation(session, "conv-a", 7, {
        "projectPath": "/old/p",
        "projectPaths": ["/old/p", "/keep", "/new/p"],
        "readOnlyPaths": ["/old/p", "/new/p"],
        "note": "leave /old/p embedded here",
    }, rev=9)
    _conversation(session, "conv-other-owner", 8, {
        "projectPath": "/old/p",
    })

    out = _project_relink(
        session, {"user_id": 7, "old_path": "/old/p", "new_path": "/new/p"})

    assert out["conversationsMoved"] == 1
    settings, rev = _settings(session, "conv-a", 7)
    assert settings == {
        "projectPath": "/new/p",
        "projectPaths": ["/new/p", "/keep"],
        "readOnlyPaths": ["/new/p"],
        "note": "leave /old/p embedded here",
    }
    assert rev == 9
    other, _ = _settings(session, "conv-other-owner", 8)
    assert other["projectPath"] == "/old/p"


def test_relink_ignores_old_path_outside_project_settings_plane(session):
    _touch(session, 7, "/old/p", 1)
    _conversation(session, "conv-note", 7, {
        "projectPath": "/somewhere-else",
        "note": "/old/p",
    })
    out = _project_relink(
        session, {"user_id": 7, "old_path": "/old/p", "new_path": "/new/p"})
    assert out["conversationsMoved"] == 0
    settings, _ = _settings(session, "conv-note", 7)
    assert settings["note"] == "/old/p"


def test_relink_moves_recoverable_trashed_conversation_pin(session):
    _touch(session, 7, "/old/p", 1)
    _trashed_conversation(session, "trashed-a", 7, {
        "projectPath": "/old/p",
        "projectPaths": ["/old/p"],
    }, rev=4)
    out = _project_relink(
        session, {"user_id": 7, "old_path": "/old/p", "new_path": "/new/p"})
    assert out["conversationsMoved"] == 0
    assert out["trashedConversationsMoved"] == 1
    settings, rev = _trashed_settings(session, "trashed-a", 7)
    assert settings["projectPath"] == "/new/p"
    assert settings["projectPaths"] == ["/new/p"]
    assert rev == 4


def test_conversation_pin_relink_fails_before_writes_above_bound(
    session, monkeypatch
):
    from lib.storage_sidecar.operations_pkg import _project as project_ops

    monkeypatch.setattr(project_ops, "_PROJECT_RELINK_CONVERSATION_LIMIT", 1)
    _conversation(session, "active-a", 7, {"projectPath": "/old/p"})
    _trashed_conversation(session, "trashed-a", 7, {"projectPath": "/old/p"})

    with pytest.raises(StorageError) as raised:
        project_ops._conversation_project_relink(
            session, 7, "/old/p", "/new/p"
        )

    assert raised.value.code == "database_conflict"
    active, _ = _settings(session, "active-a", 7)
    trashed, _ = _trashed_settings(session, "trashed-a", 7)
    assert active["projectPath"] == "/old/p"
    assert trashed["projectPath"] == "/old/p"


def test_conversation_pin_relink_uses_one_bounded_backend_batch(session):
    class RecordingSession:
        backend = session.backend

        def __init__(self):
            self.batch_sizes = []

        def fetch_all(self, sql, params=()):
            return session.fetch_all(sql, params)

        def execute_many_exact(self, sql, params):
            self.batch_sizes.append((sql, len(params)))
            return session.execute_many_exact(sql, params)

    for index in range(512):
        _conversation(
            session,
            f"batch-{index:04d}",
            7,
            {"projectPath": "/old/p"},
        )
    recording = RecordingSession()

    moved, trashed = _conversation_project_relink(
        recording, 7, "/old/p", "/new/p"
    )

    assert (moved, trashed) == (512, 0)
    assert len(recording.batch_sizes) == 1
    statement, batch_size = recording.batch_sizes[0]
    assert statement.startswith("UPDATE storage_conversations")
    assert batch_size == 512
    settings, _ = _settings(session, "batch-0511", 7)
    assert settings["projectPath"] == "/new/p"


def test_relink_requires_an_existing_old_entry(session):
    with pytest.raises(StorageError) as raised:
        _project_relink(
            session, {"user_id": 7, "old_path": "/gone", "new_path": "/new"})
    assert raised.value.code == "database_not_found"


def test_relink_rejects_identical_paths(session):
    with pytest.raises(StorageError) as raised:
        _project_relink(
            session, {"user_id": 7, "old_path": "/p", "new_path": "/p"})
    assert raised.value.code == "database_protocol_error"


def test_relink_is_owner_scoped(session):
    _touch(session, 8, "/old/p", 1)
    with pytest.raises(StorageError) as raised:
        _project_relink(
            session, {"user_id": 7, "old_path": "/old/p", "new_path": "/n"})
    assert raised.value.code == "database_not_found"


class _Client:
    def __init__(self):
        self.calls = []

    def query(self, operation, payload):
        self.calls.append(("query", operation, payload))
        return []

    def command(
        self,
        operation,
        payload,
        command_id,
        priority="user",
        deadline=None,
    ):
        self.calls.append(
            ("command", operation, payload, command_id, priority, deadline)
        )
        return {"project": {"path": payload.get("new_path")}}


def test_repository_relink_dispatches_project_relink():
    from lib.project_mod.recent_repository import RecentProjectRepository

    client = _Client()
    repo = RecentProjectRepository(
        19, client_factory=lambda *, write=False: client)
    assert repo.relink("/a", "/b") == {"project": {"path": "/b"}}
    kind, operation, payload, _cid, priority, deadline = client.calls[0]
    assert kind == "command"
    assert operation == "project.relink"
    assert payload == {"user_id": 19, "old_path": "/a", "new_path": "/b"}
    assert priority == "maintenance"
    assert deadline == 120.0


def test_relink_operation_declares_only_its_own_extended_transaction_budget():
    from lib.storage_sidecar.operations import resolve_operation_contract

    receipt_required, _execute, transaction_timeout_s = (
        resolve_operation_contract(
            "project.relink",
            "command",
            {"user_id": 19, "old_path": "/a", "new_path": "/b"},
        )
    )

    assert receipt_required is True
    assert transaction_timeout_s == 120.0
    _receipt, _execute, ordinary_timeout_s = resolve_operation_contract(
        "project.recent.touch",
        "command",
        {"user_id": 19, "project_path": "/a", "last_used": 1},
    )
    assert ordinary_timeout_s is None


def test_config_facade_uses_storage_client(monkeypatch):
    client = _Client()
    monkeypatch.setattr(
        "lib.storage.get_storage_client", lambda *, write=False: client)
    from lib.project_mod.config import relink_project_path

    relink_project_path("/a", "/b", user_id=19)
    assert client.calls[0][1] == "project.relink"
