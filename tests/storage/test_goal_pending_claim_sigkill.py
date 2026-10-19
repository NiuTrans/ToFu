"""Accepted continuation claims a killed Goal before preparing its shared journal.

Only task startup and provider usage are fixtures. Turn/Goal lineage, source
journal chunks, checkpoint retirement, wallet settlement and successor journal
all use the same private native authority; no model or tool is dispatched.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from tests.storage.test_goal_pending_actor_sigkill import WRITER
from tests.storage.test_native_checkpoint_frontier import actor_snapshot

pytestmark = pytest.mark.api


@pytest.mark.parametrize('lost_ack', [False, True])
def test_accepted_pending_claim_and_journal_preparation_after_sigkill(tmp_path, monkeypatch, lost_ack):
    from lib.agent_core.execution_budget import ModelBudgetExceeded
    from lib.billing import get_balance
    from lib.conversation_sync.command_service import ConversationTurnCommandService
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.goal_runs.execution_journal import create_goal_execution_journal
    from lib.goal_runs.pending_claim import claim_pending_goal_actors
    from lib.goal_runs.recovery import GoalRecoveryPorts, bind_goal_recovery_task, resume_chat_goal
    from lib.goal_runs.repository import SidecarGoalRunRepository
    from lib.goal_runs.service import GoalRunService, GoalRunServicePorts
    from lib.identity import PrincipalContext
    from lib.orchestration.execution_journal import RecoveryRefused
    from lib.orchestration.run_service import OrchestrationRunService
    from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
    from lib.orchestration.startup_recovery import retire_interrupted_orchestration_runs_detailed
    from lib.storage import service
    from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
    from lib.tasks_pkg.native_goal_handoff import NativeGoalHandoff
    from lib.turn_lifecycle import get_attempt, mark_task_started, recover_running_attempts
    from tofu_db.embedded import EmbeddedRuntime

    binary = os.environ.get('TOFU_CHECKPOINT_CABI_TEST_BINARY')
    library = os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY')
    source = os.environ.get('TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256')
    if not all((binary, library, source)) or sys.platform != 'linux':
        pytest.skip('explicit private native engine and Linux SIGKILL required')
    directory = tmp_path / 'authority'
    directory.mkdir()
    subprocess.run([binary, 'init', '--data-dir', str(directory)], check=True, capture_output=True)
    state = actor_snapshot()
    state['host_fee_projection']['tenant_id'] = ''
    fixture = tmp_path / 'snapshot.json'
    fixture.write_text(json.dumps(state))
    ready, errors = tmp_path / 'ready.json', tmp_path / 'writer.stderr'
    with errors.open('wb') as stderr:
        writer = subprocess.Popen([sys.executable, '-c', WRITER, library, str(directory),
            str(ready), source, 'claim', str(fixture)], stdout=subprocess.DEVNULL, stderr=stderr)
        try:
            expires = time.monotonic() + 30
            while not ready.exists():
                assert writer.poll() is None, errors.read_text()
                assert time.monotonic() < expires, errors.read_text()
                time.sleep(.02)
            writer.kill()
            assert writer.wait(10) == -signal.SIGKILL
        finally:
            if writer.poll() is None:
                writer.kill()
                writer.wait(10)
    original = json.loads(ready.read_text())
    with EmbeddedRuntime(Path(library), directory, owner_id=7, tenant_id=3, secret=b'x'*32) as runtime:
        assert runtime.build_identity()['source_sha256'] == source
        storage = EmbeddedStorageV2Client(runtime, b'x'*32, owner_id=7, tenant_id=3)
        handoffs = []
        try:
            provider = lambda **kwargs: storage
            monkeypatch.setattr(service, 'storage_runtime', lambda: SimpleNamespace(client=provider))
            monkeypatch.setattr('lib.storage.get_storage_client', provider)
            recover_running_attempts()
            retired = retire_interrupted_orchestration_runs_detailed(error={
                'kind': 'worker_lost', 'source': 'orchestration.startup_recovery'}, client=provider)
            assert retired.retired == 1
            runs = OrchestrationRunService(SidecarOrchestrationRunStore(7, client=provider))
            goals = SidecarGoalRunRepository(7, client=provider)
            conversations = SidecarConversationSyncRepository(client_factory=provider)
            started = []

            def start(conv, cfg, data, abort_after, registered, **kwargs):
                target = dict(id='target-root', convId=conv, _userId=7, _tenant_id='',
                    _turnId=cfg['_turnId'], _attemptId=cfg['_attemptId'], config=cfg,
                    messages=[], status='running', abort_event=threading.Event())
                registered(target['id'])
                assert mark_task_started(target['_attemptId'], target['id'], user_id=7)['status'] == 'running'
                started.append(target)
                return target['id'], None

            commands = ConversationTurnCommandService(build_user_message=lambda *args: None,
                was_aborted_after=lambda *args: False, start_task=start)
            ports = GoalRecoveryPorts(conversations, goals, get_attempt, commands)
            bounds = dict(header_bytes=4096, snapshot_bytes=1048576, identity_bytes=1024, io_timeout_ms=10000)
            assert resume_chat_goal(runs, original['run_id'], owner_user_id=7, ports=ports,
                pending_actor_bounds=bounds, storage=storage) == 'goal_target-root'
            assert len(started) == 1
            task = started[0]
            seed = bind_goal_recovery_task(task, runs=runs, conversation_repository=conversations,
                get_attempt=get_attempt, pending_actor_bounds=bounds, storage=storage)
            inputs = seed.inspect_pending_inputs(maximum_bytes=8*1048576,
                abort_check=task['abort_event'].is_set)
            assert set(inputs) == set(seed.pending_actors.actors)
            recovered = next(iter(inputs.values()))
            assert recovered.node['id'] == 'worker' and recovered.context == seed.launch().initial_context
            assert recovered.input_hash == seed.pending_actors.actors[recovered.key]['admission']['input_hash']
            assert recovered.worker_context == dict(messages=None, task_state={})
            assert '_executionBudget' not in task
            GoalRunService(ports=GoalRunServicePorts(lambda *args: goals)).start(task, seed.definition)
            assert seed.pending_actors.positions['launch_sequence'] > 1  # Real chunk reference, not logical ordinal.
            assert get_balance(original['wallet_id']) == 100000
            with pytest.raises(RecoveryRefused, match='atomic takeover'):
                create_goal_execution_journal(task, seed.definition, seed.launch(), runs=runs)
            assert '_executionBudget' not in task
            principal = PrincipalContext.user(subject_id=original['wallet_id'], owner_user_id=7)
            claim_calls = []
            control = storage.native_checkpoint_control

            def observe(operation, payload, **kwargs):
                result = control(operation, payload, **kwargs)
                if operation == 'goal_claim':
                    claim_calls.append(payload)
                    if lost_ack:
                        raise OSError('native claim committed but acknowledgement lost')
                return result

            monkeypatch.setattr(storage, 'native_checkpoint_control', observe)
            def claim_source():
                return claim_pending_goal_actors(task, seed, principal=principal, storage=storage,
                    wallet_user_id=original['wallet_id'], reserved_micro=100000,
                    header_bytes=4096, snapshot_bytes=1048576, evidence_bytes=8*1048576, io_timeout_ms=10000)
            if lost_ack:
                with pytest.raises(OSError, match='acknowledgement lost'):
                    claim_source()
                assert '_executionBudget' not in task
                assert not any(event['type'] == 'execution_journal_ready'
                    for event in runs.replay('goal_target-root', 0).events)
            else:
                claim = claim_source()
                assert claim.request['invoice']['componentsMicro']['totalMicro'] == 7593

                def factory(journal):
                    handoff = NativeGoalHandoff(principal=principal, task=task, storage=storage,
                        evidence_bytes=8*1048576, io_timeout_ms=10000, journal=journal)
                    handoffs.append(handoff)
                    return handoff

                journal = create_goal_execution_journal(task, seed.definition, seed.launch(), runs=runs,
                    native_claim=claim, account_handoff_factory=factory)
                budget = task['_executionBudget']
                assert budget.consumption_snapshot()[:2] == (10200, 0)
                with pytest.raises(ModelBudgetExceeded):
                    budget.reserve_model(task, input_tokens=0, max_output_tokens=1)
                assert budget.reserve_tool(task) == 'execution_account_frontier_unavailable'
                events = runs.replay('goal_target-root', 0).events
                assert any(event['type'] == 'execution_journal_ready' for event in events)
                assert not any(event['type'] == 'execution_effect_started' for event in events)
                journal.revoke()
            assert len(claim_calls) == 1
            assert get_balance(original['wallet_id']) == 192407
            claims = [event for event in runs.replay(original['run_id'],
                seed.pending_actors.positions['source_cursor']).events if event['type'] == 'execution_recovery_claim']
            assert len(claims) == 1 and claims[0]['successor_run_id'] == 'goal_target-root'
        finally:
            for handoff in handoffs:
                handoff.close()
            storage.close()
