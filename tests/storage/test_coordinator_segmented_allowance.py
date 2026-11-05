"""Replay the established allowance contracts through actual segmented SQL CAS."""
import uuid

import pytest

from tests.project_mod.test_project_dispatch_repository import context
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime
from tests.project_mod import test_coordinator_request_budget as contracts

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = [pytest.mark.api, pytest.mark.parametrize('semantic_authority', ['sqlite'], indirect=True)]


@pytest.fixture
def segmented_runtime(detached_semantic_authority, runtime):
    factory, _, _ = detached_semantic_authority
    r = runtime
    # Preserve the existing contract's frozen human/Turn/task evidence. Only
    # its in-memory persistence double is replaced by the selected authority;
    # conversation creation stays on the composed fixture's double.
    for (namespace, key), row in r.c.client.rows.items():
        factory(write=True).command('record.put', {'namespace': namespace,
            'key': key, 'expected_version': 0, 'value': row['value']}, uuid.uuid4().hex)
    r.c.repo.client = factory
    r.c.repo.storage_version = 2
    return r


SCENARIOS = [
    contracts.test_request_account_has_no_policy_and_coordinator_plus_workers_are_summed,
    contracts.test_spend_far_beyond_the_old_ceiling_is_never_refused,
    contracts.test_worker_missing_usage_receipt_never_blocks_its_next_call,
    contracts.test_coordinator_missing_usage_receipt_never_blocks_its_next_call,
    contracts.test_same_human_input_new_attempt_keeps_one_account,
    contracts.test_plan_counts_own_usage_but_never_delegates,
    contracts.test_long_running_records_stay_bounded_and_replays_are_not_double_counted,
]


@pytest.mark.parametrize('scenario', SCENARIOS, ids=lambda scenario: scenario.__name__)
def test_existing_conservation_contract_on_segmented_storage(segmented_runtime, scenario):
    scenario(segmented_runtime)


@pytest.mark.parametrize('scenario', [
    contracts.test_request_account_records_workers_without_limiting_concurrency,
    contracts.test_legacy_issued_worker_policy_does_not_limit_active_workers,
], ids=lambda scenario: scenario.__name__)
def test_unlimited_concurrency_on_segmented_storage(segmented_runtime, monkeypatch, scenario):
    scenario(segmented_runtime, monkeypatch)


def test_unknown_ack_remains_charged(segmented_runtime, monkeypatch):
    contracts.test_unknown_source_write_ack_never_rebinds(segmented_runtime, monkeypatch)


def test_live_original_and_worker_accounts_survive_cutover(segmented_runtime):
    r = segmented_runtime
    r.c.repo.storage_version = 1
    source = contracts.bind(r)
    contracts.spend(r.task, 100)
    worker, assignment = contracts._worker_task(r)
    before = contracts.usage(r, source)
    r.c.repo.storage_version = 2
    assert contracts.usage(r, source) == before
    contracts.spend(worker, 50)  # This existing physical account triggers cutover.
    r.attempts[assignment['execution']['attemptId']]['status'] = 'completed'
    after = contracts.usage(r, source)
    assert after['tokens'] == 150 and after['usageComplete']
    assert after['modelRequests'] == 2 and after['activeWorkers'] == 0
    contracts.spend(r.task, 20)
    assert contracts.usage(r, source)['tokens'] == 170


def test_one_request_can_delegate_beyond_the_old_project_lifetime_ceiling(segmented_runtime):
    r = segmented_runtime
    source = contracts.bind(r)
    conversations = []
    for number in range(132):
        r.args['idempotency_key'] = f'part-{number}'
        worker, assignment = contracts._worker_task(r)
        conversations.append(worker['convId'])
        # The fixture substitutes provider/Turn completion; ordinary account
        # binding records zero tool slots before unused capacity is released.
        r.attempts[assignment['execution']['attemptId']]['status'] = 'completed'
    assert len(set(conversations)) == len(r.created) == 132
    assert contracts.usage(r, source)['activeWorkers'] == 0
    assert contracts.usage(r, source)['tokens'] == 0


@pytest.mark.parametrize('group', ['source', 'budget'])
def test_missing_allowance_index_cannot_refund_a_worker(segmented_runtime, group):
    from lib.project_dispatch.segmented import SegmentedDispatch, key
    from lib.storage.errors import StorageError
    r = segmented_runtime
    source = contracts.bind(r)
    worker, assignment = contracts._worker_task(r)
    delegation = assignment['request']['delegation']
    retained = r.c.repo.read(assignment['id'])
    ident = delegation['attemptId'] if group == 'source' else delegation['policy']['requestBudgetId']
    store = SegmentedDispatch(r.c.repo)
    prefix = 'assignments:' + group + ':' + key(ident) + ':'
    index = store.query('list', collection='indexes', prefix=prefix, limit=1)[0]
    index_id = prefix + f"{index['value']['sequence']:020d}"
    head = store.head()
    store.command(head['version'], head['value']['state'], [{'collection': 'indexes',
        'id': index_id, 'expected_version': index['version'], 'delete': True}])
    with pytest.raises(StorageError) as error:
        r.c.repo._load(r.c.repo._selection(**({'sources': [ident]} if group == 'source' else {'budgets': [ident]})))
    assert error.value.code == 'database_integrity'
    assert r.c.repo.read(assignment['id']) == retained


def test_passive_history_does_not_load_original_request_account_history(segmented_runtime):
    r = segmented_runtime
    contracts.bind(r)
    _, assignment = contracts._worker_task(r)
    real = r.c.repo.client
    queries = []
    class Counted:
        def query(self, operation, payload, **kwargs):
            queries.append((operation, payload))
            return real().query(operation, payload, **kwargs)
        def command(self, *args, **kwargs): pytest.fail('History queries must remain passive')
    r.c.repo.client = lambda **kw: Counted()
    assert r.c.repo.history_page()['rows'][0]['id'] == assignment['id']
    assert not any(payload.get('collection') in ('requests', 'accounts', 'model_attempts', 'settlements') for _, payload in queries)
