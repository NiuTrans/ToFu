"""Native prepared occurrences query the real canonical file publication owner."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import threading
from types import SimpleNamespace

import pytest

from lib.agent_core.execution_budget import ExecutionBudget
from lib.agent_core.native_process import (
    ApprovalRequest,
    CheckpointRequest,
    ProgressLimits,
    ProgressRequest,
    RoundRequest,
    RoundSettlement,
    ToolRequest,
)
from lib.identity import PrincipalContext
from lib.tasks_pkg.handlers.project import _handle_project_tool
from lib.tasks_pkg.native_file_recovery import NativeFileToolRecovery
from lib.tasks_pkg.native_gateway import NativeGateway
from tests.turn_lifecycle import test_file_effect_inspection as fixtures

storage, turn_service, owner, project = (
    fixtures.storage,
    fixtures.turn_service,
    fixtures.owner,
    fixtures.project,
)
_task, _discard = fixtures._task, fixtures._discard

pytestmark = pytest.mark.unit


def _catalog():
    import copy
    from lib.tools.project import PROJECT_TOOL_WRITE_FILE

    return [copy.deepcopy(PROJECT_TOOL_WRITE_FILE)]


def _gateway(task, principal, executor, base):
    gateway = NativeGateway(
        principal=principal,
        task=task,
        executor=executor,
        configuration={"autoApply": True},
        tool_list=_catalog(),
        permissions={"write_file": "write"},
        project_path=str(base),
        project_enabled=True,
        model="fixture",
        round_source=lambda: 0,
        decision_policy=None,
        pending_calls=4,
        pending_bytes=65536,
    )
    gateway.enable_progress(ProgressLimits(3, 3), 16384)
    return gateway


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "changed_file",
        "uncommitted_changed",
        "foreign_producer",
        "missing_operation",
    ],
)
def test_native_file_batch_keeps_ids_and_restores_only_proven_spent_tools(
    storage, turn_service, owner, project, monkeypatch, fault
):
    from lib.conversation_sync import repository

    repository_type = repository.SidecarConversationSyncRepository
    monkeypatch.setattr(
        repository,
        "SidecarConversationSyncRepository",
        lambda: repository_type(client_factory=lambda write=False: storage.client),
    )
    _, task = _task(turn_service, storage, "native-file-batch")
    principal = PrincipalContext.user(subject_id="native-file-batch", owner_user_id=1)
    task["config"].update(
        projectPath=str(project),
        executionLimits=dict(max_tokens=1000, max_tool_calls=3, deadline=None),
    )
    task["_tool_env"] = SimpleNamespace(resolve=lambda name: _handle_project_tool)
    task["_executable_tool_catalog"] = _catalog()
    task["_executionBudget"] = ExecutionBudget(task)
    profile = dict(
        encoding="cl100k_base", output_field="max_tokens", wire_model="fixture"
    )
    saved_account = task["_executionBudget"].capture_shared_account(
        owner=1, task=task, counter_policy=profile
    )
    arguments = dict(
        description="Append one result",
        path="result.txt",
        content="once\n",
        append=True,
    )
    calls = [
        dict(
            id="write-" + str(index),
            type="function",
            function=dict(name="write_file", arguments=json.dumps(arguments)),
        )
        for index in range(2)
    ]
    if fault == "uncommitted_changed":
        from lib.project_mod.write_tools import _ops

        original = _ops._record_modification_checked
        committed = []

        def lose_second_ack(*args, **kwargs):
            committed.append(True)
            if len(committed) == 2:
                raise OSError("publication happened but undo acknowledgement was lost")
            return original(*args, **kwargs)

        monkeypatch.setattr(_ops, "_record_modification_checked", lose_second_ack)
    captured = None

    async def execute(executor):
        nonlocal captured
        gateway = _gateway(task, principal, executor, project)
        adapter = NativeFileToolRecovery(
            principal=principal, task=task, executor=executor
        )
        adapter.bind_gateway(gateway)
        try:
            gateway.prepare_response(
                RoundSettlement(
                    RoundRequest(principal, task["id"], "round", 0, 0),
                    {"kind": "returned", "model_calls": calls},
                )
            )
            request = CheckpointRequest(
                principal,
                task["id"],
                "pending",
                0,
                "epoch",
                "binding",
                threading.Event(),
                state=dict(
                    tools_pending=True,
                    shared_model_account=saved_account,
                    messages=[dict(role="assistant", tool_calls=calls)],
                ),
            )
            captured = await adapter.capture(request)
            # Two tools in one model round retain distinct producer counters.
            assert [row['tool_round'] for row in captured['tool_recovery']['calls']] == [1, 2]
            expected = [
                row["inspection"]["operationId"]
                for row in captured["tool_recovery"]["calls"]
            ]
            assert len(set(expected)) == 2 and not (project / "result.txt").exists()
            assert not adapter.batch.armed
            await adapter.committed(replace(request, state=captured))
            for call in calls:
                approval = ApprovalRequest(
                    principal,
                    task["id"],
                    "approve-" + call["id"],
                    call["id"],
                    "write_file",
                    "epoch",
                    json.dumps(arguments).encode(),
                    b"null",
                    asyncio.Event(),
                    5,
                )
                assert await gateway.decide(approval) == "auto_write_mode"
                await gateway.execute(
                    ToolRequest(
                        principal,
                        task["id"],
                        "execute-" + call["id"],
                        "write_file",
                        arguments,
                        call["id"],
                        model_round=0,
                    )
                )
            assert [
                row["executionEffect"]["operationId"] for row in task["toolRounds"]
            ] == expected
            assert (project / "result.txt").read_text() == "once\nonce\n"
            assert task["_executionBudget"].consumption_snapshot()[1] == 2
        finally:
            gateway.close()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            asyncio.run(execute(executor))
        recovery_task = dict(task)
        recovery_task.pop("_executionBudget")
        recovery_task.pop("_executionToolsAdmitted", None)
        account = ExecutionBudget(recovery_task)
        recovery_task["_executionBudget"] = account
        account.restore_shared_account(
            saved_account, owner=1, task=recovery_task, counter_policy=profile
        )
        if fault in {"changed_file", "uncommitted_changed"}:
            (project / "result.txt").write_text("new human content\n")
        elif fault == "foreign_producer":
            captured["tool_recovery"]["producer_task_id"] = "other-executor"
        elif fault == "missing_operation":
            captured["tool_recovery"]["calls"][0]["inspection"]["operationId"] = (
                "f" * 32
            )

        async def recover(executor):
            gateway = _gateway(recovery_task, principal, executor, project)
            adapter = NativeFileToolRecovery(
                principal=principal, task=recovery_task, executor=executor
            )
            adapter.bind_gateway(gateway)
            try:
                request = CheckpointRequest(
                    principal,
                    task["id"],
                    "reconcile",
                    0,
                    "epoch",
                    "binding",
                    threading.Event(),
                    state=captured,
                )
                if fault in {"none", "changed_file"}:
                    observed = await adapter.reconcile(request)
                    assert [item["call_id"] for item in observed] == [
                        call["id"] for call in calls
                    ]
                    assert all(
                        json.loads(row["content"])["outcome"] == "file_published"
                        for row in observed
                    )
                    progress = await gateway._progress.observe(
                        ProgressRequest(principal, task["id"], 0, 2, threading.Event())
                    )
                    assert progress["resultEvidenceComplete"]
                    assert account.consumption_snapshot()[1] == 2
                    with pytest.raises(ValueError):
                        await adapter.reconcile(request)
                    assert account.reserve_tool(recovery_task) is None
                    assert (
                        account.reserve_tool(recovery_task)
                        == "execution_tool_budget_exhausted"
                    )
                else:
                    with pytest.raises(Exception):
                        await adapter.reconcile(request)
                    assert account.consumption_snapshot()[1] == 0
                    assert account.consumption_snapshot()[2] is False
                    assert gateway._progress.batch is None
            finally:
                gateway.close()

        with ThreadPoolExecutor(max_workers=1) as executor:
            asyncio.run(recover(executor))
        assert (project / "result.txt").read_text() == (
            "new human content\n"
            if fault in {"changed_file", "uncommitted_changed"}
            else "once\nonce\n"
        )
    finally:
        _discard(task)


@pytest.mark.parametrize('allow_unreconciled', [False, True])
@pytest.mark.parametrize('batch', ['ordinary', 'mixed', 'duplicate'])
def test_unreconciled_batch_keeps_paid_state_but_cannot_grant_replay(
    storage, turn_service, owner, project, allow_unreconciled, batch
):
    _, task = _task(turn_service, storage, 'unreconciled-batch')
    principal = PrincipalContext.user(subject_id='unreconciled-batch', owner_user_id=1)
    task['config'].update(projectPath=str(project),
        executionLimits=dict(max_tokens=1000, max_tool_calls=3, deadline=None))
    task['_executionBudget'] = ExecutionBudget(task)
    profile = dict(encoding='cl100k_base', output_field='max_tokens', wire_model='fixture')
    saved = task['_executionBudget'].capture_shared_account(owner=1, task=task, counter_policy=profile)
    calls = [dict(id='inspect', type='function',
        function=dict(name='read_files', arguments=json.dumps({'path': 'result.txt'})))]
    if batch == 'mixed':
        calls.append(dict(id='publish', type='function', function=dict(name='write_file',
            arguments=json.dumps(dict(path='result.txt', content='must not replay')))))
    if batch == 'duplicate':
        calls.append(dict(calls[0]))
    state = dict(tools_pending=True, shared_model_account=saved,
        messages=[dict(role='assistant', tool_calls=calls)])
    original = json.dumps(state, sort_keys=True)

    async def verify(executor):
        gateway = _gateway(task, principal, executor, project)
        adapter = NativeFileToolRecovery(principal=principal, task=task, executor=executor,
            allow_unreconciled_batches=allow_unreconciled)
        adapter.bind_gateway(gateway)
        request = CheckpointRequest(principal, task['id'], 'pending', 0, 'epoch', 'binding',
            threading.Event(), state=state)
        try:
            if not allow_unreconciled or batch == 'duplicate':
                with pytest.raises(ValueError):
                    await adapter.capture(request)
                assert adapter.capture_digest is None
                return
            captured = await adapter.capture(request)
            assert json.dumps(captured, sort_keys=True) == original
            assert 'tool_recovery' not in captured
            changed = json.loads(json.dumps(captured))
            changed['messages'][-1]['tool_calls'][0]['function']['name'] = 'write_file'
            with pytest.raises(ValueError, match='binding changed'):
                await adapter.committed(replace(request, state=changed))
            await adapter.committed(replace(request, state=captured))
            with pytest.raises(ValueError, match='binding changed'):
                await adapter.committed(replace(request, state=captured))
        finally:
            gateway.close()
        recovered = dict(task)
        recovered.pop('_executionBudget')
        account = ExecutionBudget(recovered)
        recovered['_executionBudget'] = account
        account.restore_shared_account(saved, owner=1, task=recovered, counter_policy=profile)
        gateway = _gateway(recovered, principal, executor, project)
        adapter = NativeFileToolRecovery(principal=principal, task=recovered, executor=executor,
            allow_unreconciled_batches=True)
        adapter.bind_gateway(gateway)
        try:
            with pytest.raises(ValueError, match='outcome is unknown; automatic replay is not permitted'):
                await adapter.reconcile(replace(request, state=captured))
            assert account.consumption_snapshot() == (0, 0, False)
            assert not (project / 'result.txt').exists()
        finally:
            gateway.close()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            asyncio.run(verify(executor))
        assert json.dumps(state, sort_keys=True) == original
    finally:
        _discard(task)
