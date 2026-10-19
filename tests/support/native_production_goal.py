"""Trusted actor checkpoint composition with an optional post-publication crash.

Storage, registered actor accounting, file proof and Goal handoff are production
owners. This fixture only observes checkpoints and waits at an explicit boundary.
"""
import asyncio
import json
from pathlib import Path
import uuid


def actor_checkpoint_factory(values, transport):
    directory = Path(values['directory'])

    def factory(request, principal, executor, configuration):
        from lib.storage.service import get_storage_client
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
        from lib.tasks_pkg.native_file_recovery import NativeFileToolRecovery

        task = request.task
        storage = get_storage_client(write=True)
        from lib.goal_runs.native_recovery import NativeGoalRecoveryConfiguration
        bounds = NativeGoalRecoveryConfiguration(**values.get('goal_checkpoint_bounds', {})).bounds(transport, storage=storage)
        async def restored(callback, state):
            (directory / ('restored-' + task['id'] + '.json')).write_text(json.dumps(state, indent=2))

        class ObservedCheckpoint(NativeCheckpointPolicy):
            async def complete(self, callback):
                stop_round = values.get('goal_checkpoint_round')
                if values.get('crash_pending') and (stop_round is None or len(task.get('apiRounds') or []) >= stop_round):
                    parent = chat_task_runtime.get(task['_parentTaskId'])
                    receipt = dict(task_id=parent['id'], conversation_id=parent['convId'],
                        turn_id=parent['_turnId'], attempt_id=parent['_attemptId'],
                        run_id=parent.get('_goalRunId'), billing_user=parent.get('_billingUserId'),
                        child_id=task['id'], account=parent['_executionBudget'].continuity_snapshot(),
                        tool_rounds=task.get('toolRounds'), fee_rounds=task.get('apiRounds'))
                    pending = directory / (f"goal-pending-{values['generation']}.json" if stop_round is not None else 'goal-pending.json')
                    temporary = pending.with_suffix('.tmp')
                    temporary.write_text(json.dumps(receipt, indent=2))
                    temporary.replace(pending)
                    await asyncio.wait_for(asyncio.Event().wait(), timeout=90)
                return await super().complete(callback)

        return ObservedCheckpoint(principal=principal, task=task, storage=storage,
            executor=executor, epoch=task['id'], binding='production-goal-actor-v1',
            token=uuid.uuid4().hex, **{key: value for key, value in bounds.items() if key != 'evidence_bytes'},
            restore=restored,
            registered_child=True, tool_recovery=NativeFileToolRecovery(
                principal=principal, task=task, executor=executor, registered_child=True,
                storage=storage, allow_unreconciled_batches=True))

    return factory
