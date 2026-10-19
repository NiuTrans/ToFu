"""Storage backpressure parks the shared pipeline, never repeats paid/effect work."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.goals.test_goal_invocation_integration import runtime as runtime, TOOL, ANSWER  # noqa: F401
from tests.swarm.test_swarm_invocation import agent_for, scripted_provider, reply
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.persistence_wait._BASE_DELAY_SECONDS', 0.0)


@pytest.mark.parametrize('creator', ['human', 'agent'])
@pytest.mark.parametrize('blocked_phase', ['model_in_flight', 'tools_in_flight', 'ready'])
def test_same_pipeline_retries_checkpoint_without_model_or_tool_reexecution(runtime, monkeypatch, creator, blocked_phase):
    from lib.tasks_pkg.invocation import execute_invocation, ROOT_INVOCATION_POLICY
    import lib.tasks_pkg.orchestrator._run as runner
    snapshots, attempts = [], []
    def checkpoint(task, messages, rounds):
        phase = task['_invocationCheckpointPhase']
        if phase == blocked_phase and len(attempts) < 20:
            attempts.append((deepcopy(messages), rounds, len(runtime.calls), len(runtime.tool_calls)))
            raise StorageError('database_busy', 'checkpoint pressure')
        snapshots.append(phase)
    if creator == 'human':
        original = runner.assemble_round_tools
        def assemble(cfg, task, mcfg):
            if task is runtime.parent:
                task['_executable_tool_catalog'] = [TOOL]
                return [TOOL], True
            return original(cfg, task, mcfg)
        monkeypatch.setattr(runner, 'assemble_round_tools', assemble)
        runtime.parent['_invocationCheckpointSink'] = checkpoint
        execute_invocation(runtime.parent, policy=ROOT_INVOCATION_POLICY)
        assert runtime.parent['finishReason'] == 'stop', runtime.parent.get('error')
    else:
        agent = agent_for(runtime)
        original = agent._save_checkpoint
        def save(child, messages, rounds):
            checkpoint(child, messages, rounds)
            original(child, messages, rounds)
        agent._save_checkpoint = save
        result = agent.run()
        assert result.status == 'completed', result.error_message
        assert ANSWER in result.final_answer
    assert len(attempts) == 20
    assert all(entry == attempts[0] for entry in attempts)
    assert snapshots and len(runtime.calls) == 2 and len(runtime.tool_calls) == 1


@pytest.mark.parametrize('blocked_phase', ['model_in_flight', 'tools_in_flight', 'ready', 'final'])
def test_actual_swarm_writer_retains_same_receipt_and_terminal_answer(runtime, monkeypatch, blocked_phase):
    from lib.swarm import persistence
    # Use the actual swarm writer with a synthetic receipt-capable authority.
    # The model/tools are hermetic and parent conversation storage is not used.
    runtime.parent['_transientRuntime'] = False
    agent = agent_for(runtime)
    agent.swarm_key = 'hermetic-wave'
    attempts, receipts = [], {}
    def command(operation, payload, command_id):
        assert operation == 'swarm.agent.save'
        result = payload['result']
        selected = ((result.get('status') == 'completed') if blocked_phase == 'final'
                    else result.get('checkpoint_phase') == blocked_phase)
        if selected and len(attempts) < 20:
            attempts.append((deepcopy(payload), command_id, len(runtime.calls), len(runtime.tool_calls)))
            receipts.setdefault(command_id, deepcopy(payload))
            raise StorageError('database_busy', 'lost acknowledgement or pressure')
        receipts.setdefault(command_id, deepcopy(payload))
        return {'ok': True}
    monkeypatch.setattr(persistence, '_storage', lambda **kw: SimpleNamespace(command=command))
    result = agent.run()
    assert result.status == 'completed', result.error_message
    assert result.total_tokens == 140 and ANSWER in result.final_answer
    assert len(attempts) == 20 and all(row == attempts[0] for row in attempts)
    assert len(runtime.calls) == 2 and len(runtime.tool_calls) == 1
    final = list(receipts.values())[-1]
    assert final['status'] == 'completed' and ANSWER in final['result']['final_answer']
    assert not agent.messages and agent._child is None


def test_provider_retry_uses_same_invocation_policy_not_root_takeover(runtime, monkeypatch):
    import lib.tasks_pkg.turn_retry as policy
    monkeypatch.setattr(policy, 'auto_turn_backoff_seconds', lambda attempt: 0)
    runtime.parent['config']['autoTurnRetryMax'] = 1


    # A typed transport failure before effects may use the common retry owner.
    error = ConnectionError('Connection reset by peer')
    error._user_message = {'kind': 'network', 'retryable': True, 'detail': 'reset'}
    scripted_provider(monkeypatch, runtime, [error, reply('Verified after recovery')])
    result = agent_for(runtime).run()
    assert result.status == 'completed', result.error_message
    assert result.final_answer == 'Verified after recovery'
    assert len(runtime.children) == 1 and len(runtime.calls) == 2
    assert not runtime.tool_calls and not runtime.parent.get('aborted')
