"""Worker activates acknowledged policy and never resets recovered usage."""
import pytest
from lib.research import engine, budget_binding as binding, model_attempt_journal as journal
from lib.storage.errors import StorageError
from tests.research.test_research_budget_binding import bound_source as bound_source, LIMITS
from tests.research.test_research_record_ack import research_task
from tests.research.test_research_workspace_evidence_guard import storage
pytestmark = pytest.mark.unit


def test_worker_enters_recipe_with_owned_physical_account(bound_source, monkeypatch):
    from lib.research import recipe, runtime
    task, _ = bound_source
    task['_research_record_enabled'] = True
    binding.bind_model_budget(task['task_id'], user_id=12, execution_limits=LIMITS)
    completed = []
    monkeypatch.setattr(runtime._research_runtime, 'mark_running', lambda tid: True)
    monkeypatch.setattr(runtime._research_runtime, 'finish', lambda *args, **kwargs: completed.append(kwargs))
    monkeypatch.setattr(engine, '_write_manifest', lambda *args: None)
    monkeypatch.setattr(engine, '_emit', lambda *args: None)
    monkeypatch.setattr('lib.research.records.persist_record', lambda *args, **kwargs: None)
    def graph(*args, model_budget_admit, **kwargs):
        lease = model_budget_admit(input_tokens=10, max_output_tokens=20)
        lease.mark_dispatched()
        assert journal.read_model_attempt(task['task_id'], 1, 'dispatch_intent', user_id=12)
        lease.settle({'prompt_tokens':8, 'completion_tokens':3})
        return {'accepted':[], 'rejected':[], 'corpus_size':0}
    monkeypatch.setattr(recipe, 'build_research_from_direction', graph)
    engine.run_research_task(task)
    assert completed and not completed[-1].get('error')
    assert task['_research_model_account']['_executionBudget'].consumption_snapshot() == (11,0,True)
    assert journal.read_model_attempt(task['task_id'], 1, 'terminal', user_id=12)['reported_tokens'] == 11
    with pytest.raises(StorageError, match='takeover refused'):
        engine._prepare_model_budget(task)


def test_recovered_policy_cannot_create_fresh_account(bound_source):
    task, _ = bound_source
    task.update(_research_record_enabled=True, _research_recovered=True)
    binding.bind_model_budget(task['task_id'], user_id=12, execution_limits=LIMITS)
    with pytest.raises(StorageError, match='fresh reset refused'):
        engine._prepare_model_budget(task)
    assert '_research_model_account' not in task
    assert journal.read_model_attempt(task['task_id'], 1, 'dispatch_intent', user_id=12) is None


def test_unbound_durable_task_retains_legacy_graph(bound_source):
    task, _ = bound_source
    task['_research_record_enabled'] = True
    assert engine._prepare_model_budget(task) is None
    assert '_research_model_account' not in task


def test_claim_loss_after_intent_ack_prevents_send_and_allows_void(bound_source, monkeypatch):
    from lib.research import discovery_owner
    from lib.agent_core.execution_budget import model_budget_admission
    task, _ = bound_source
    binding.bind_model_budget(task['task_id'], user_id=12, execution_limits=LIMITS)
    claim = discovery_owner.claim_discovery_execution(task['task_id'], user_id=12)
    carrier = {'id':task['task_id'], '_userId':12, 'config':{'executionLimits':LIMITS}}
    journal.install_model_attempt_journal(carrier, execution_claim=claim)
    original = journal._put
    def lose(key, value):
        original(key, value)
        if value['phase'] == 'dispatch_intent':
            monkeypatch.setattr(claim, 'assert_current', lambda: (_ for _ in ()).throw(
                StorageError('database_conflict', 'synthetic owner lost')))
    monkeypatch.setattr(journal, '_put', lose)
    lease = model_budget_admission(carrier)(input_tokens=1, max_output_tokens=2)
    with pytest.raises(StorageError, match='owner lost'):
        lease.mark_dispatched()
    assert not lease.dispatched
    lease.settle(unsent=True)
    assert journal.read_model_attempt(task['task_id'], 1, 'terminal', user_id=12)['phase'] == 'voided'
    assert carrier['_executionBudget'].consumption_snapshot() == (0,0,True)


@pytest.mark.parametrize('deduped', [False, True])
def test_creation_acknowledges_policy_before_spawn(bound_source, monkeypatch, deduped):
    from lib.research import runtime
    task, _ = bound_source
    monkeypatch.setattr(runtime, '_cleanup_stale_research_tasks', lambda: None)
    monkeypatch.setattr(runtime, '_research_task_id', lambda: task['task_id'])
    monkeypatch.setattr(runtime, '_claim_research_task', lambda *a, **k:
        (task, task['task_id'] if deduped else None))
    monkeypatch.setattr(engine, 'research_root', lambda: task['workdir'])
    dispatched = []
    def spawn(task_id, *args):
        policy = binding.read_model_budget(task_id, user_id=12)
        assert policy['execution_limits'] == LIMITS
        dispatched.append(task_id)
    monkeypatch.setattr(runtime._research_runtime, 'spawn', spawn)
    if deduped:
        binding.bind_model_budget(task['task_id'], user_id=12, execution_limits=LIMITS)
    result = engine.produce_research(task['direction'], user_id=12, execution_limits=LIMITS)
    assert result['deduped'] is deduped
    assert dispatched == ([] if deduped else [task['task_id']])


def test_creation_refuses_failed_policy_ack_without_spawn(bound_source, monkeypatch):
    from lib.research import runtime
    task, _ = bound_source
    monkeypatch.setattr(runtime, '_cleanup_stale_research_tasks', lambda: None)
    monkeypatch.setattr(runtime, '_research_task_id', lambda: task['task_id'])
    monkeypatch.setattr(runtime, '_claim_research_task', lambda *a, **k: (task, None))
    monkeypatch.setattr(engine, 'research_root', lambda: task['workdir'])
    monkeypatch.setattr(runtime._research_runtime, 'spawn', lambda *a: pytest.fail('unbound dispatch'))
    failures = []
    monkeypatch.setattr(runtime._research_runtime, 'finish', lambda *a, **k: failures.append(k))
    monkeypatch.setattr(binding, 'bind_model_budget', lambda *a, **k:
        (_ for _ in ()).throw(StorageError('database_unavailable', 'policy ACK unavailable')))
    with pytest.raises(StorageError, match='ACK unavailable'):
        engine.produce_research(task['direction'], user_id=12, execution_limits=LIMITS)
    assert len(failures) == 1


@pytest.mark.parametrize('policy', [{**LIMITS, 'max_tool_calls': 1}, {**LIMITS, 'max_tokens': True}])
def test_invalid_creation_policy_refuses_before_task_claim(monkeypatch, policy):
    from lib.research import runtime
    monkeypatch.setattr(runtime, '_claim_research_task', lambda *a, **k: pytest.fail('task allocated'))
    with pytest.raises(ValueError, match='execution limits'):
        engine.produce_research('topic', user_id=12, execution_limits=policy)


@pytest.mark.parametrize('prior', [None, {**LIMITS, 'max_tokens': 2000}])
def test_creation_cannot_retrofit_or_replace_deduped_budget(bound_source, monkeypatch, prior):
    from lib.research import runtime
    task, _ = bound_source
    monkeypatch.setattr(runtime, '_cleanup_stale_research_tasks', lambda: None)
    monkeypatch.setattr(runtime, '_claim_research_task', lambda *a, **k: (task, task['task_id']))
    monkeypatch.setattr(runtime._research_runtime, 'spawn', lambda *a: pytest.fail('duplicate dispatch'))
    if prior is not None:
        binding.bind_model_budget(task['task_id'], user_id=12, execution_limits=prior)
    with pytest.raises(ValueError, match='different model budget'):
        engine.produce_research(task['direction'], user_id=12, execution_limits=LIMITS)
    observed = binding.read_model_budget(task['task_id'], user_id=12)
    assert (observed['execution_limits'] if observed else None) == prior


@pytest.mark.parametrize('settled', [False, True])
def test_evolution_terminal_account_reads_durable_physical_attempts(bound_source, settled):
    from lib.evolution.bridge import terminal_actuals
    task, _ = bound_source
    binding.bind_model_budget(task['task_id'], user_id=12, execution_limits=LIMITS)
    task['_research_record_enabled'] = True
    admit = engine._prepare_model_budget(task)
    lease = admit(input_tokens=10, max_output_tokens=20)
    lease.mark_dispatched()
    if settled:
        lease.settle({'prompt_tokens': 8, 'completion_tokens': 3})
    task.update(kind='research', status='done', usage={}, finished_at=task['created_at'] + 1)
    actuals, complete = terminal_actuals(task)
    assert actuals['tokens'] == (11 if settled else 0)
    assert complete is settled


def test_worker_preserves_typed_budget_stop_for_evolution(bound_source, monkeypatch):
    from lib.research import recipe, runtime
    task, _ = bound_source
    task['_research_record_enabled'] = True
    binding.bind_model_budget(task['task_id'], user_id=12, execution_limits=LIMITS)
    completed = []
    monkeypatch.setattr(runtime._research_runtime, 'mark_running', lambda tid: True)
    monkeypatch.setattr(runtime._research_runtime, 'finish', lambda *a, **k: completed.append(k))
    monkeypatch.setattr(engine, '_write_manifest', lambda *a: None)
    monkeypatch.setattr(engine, '_emit', lambda *a: None)
    def graph(*args, model_budget_admit, **kwargs):
        model_budget_admit(input_tokens=1001, max_output_tokens=1)
        pytest.fail('over-budget model work admitted')
    monkeypatch.setattr(recipe, 'build_research_from_direction', graph)
    engine.run_research_task(task)
    assert task['_executionLimitReason'] == 'execution_token_budget_exhausted'
    assert completed and completed[-1]['error_context'] == 'research:engine'
    assert str(completed[-1]['error']) == 'execution_token_budget_exhausted'
    assert not task['abort_event'].is_set()
