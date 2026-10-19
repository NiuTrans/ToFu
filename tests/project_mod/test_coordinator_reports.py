"""Reports survive lost acknowledgements without duplicate messages or unread reset."""
from copy import deepcopy
from types import SimpleNamespace
import threading

import pytest

from lib.conversations.coordinator_reports import CoordinatorReports, POLICY, recover_pending_reports
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


class Store:
    def __init__(self):
        self.rows = {}; self.lock = threading.Lock(); self.fail_head = False; self.lost_ack = None

    def __call__(self, **kwargs):
        return self

    def query(self, operation, payload, **kwargs):
        if operation == 'record.list':
            return [dict(deepcopy(row), key=key) for (ns, key), row in sorted(self.rows.items())
                    if ns == payload['namespace'] and key > payload['after_key']][:payload['limit']]
        assert operation == 'record.get'
        return deepcopy(self.rows.get((payload['namespace'], payload['key'])))

    def command(self, operation, payload, command_id):
        identity = (payload['namespace'], payload['key'])
        with self.lock:
            old = self.rows.get(identity)
            if (old['version'] if old else 0) != payload['expected_version']:
                if operation == 'record.delete' and old is None:
                    return {'deleted': False}
                raise StorageError('database_conflict', 'CAS conflict')
            if operation == 'record.delete':
                self.rows.pop(identity, None)
                return {'deleted': old is not None}
            assert operation == 'record.put'
            if self.fail_head and payload['namespace'] == POLICY['headNamespace']:
                raise StorageError('database_unavailable', 'head is offline')
            self.rows[identity] = {'value': deepcopy(payload['value']), 'version': payload['expected_version'] + 1}
            if self.lost_ack == payload['namespace']:
                self.lost_ack = None
                raise StorageError('database_unavailable', 'ACK lost', retryable=True)
            return {'version': payload['expected_version'] + 1}


@pytest.fixture
def reports():
    store = Store(); turns = {}; lock = threading.Lock(); notices = []
    binding = {'availability': 'available', 'conversationId': 'coordinator', 'revision': 1}
    bindings = SimpleNamespace(read=lambda path: deepcopy(binding))
    def append(conv, *, command_id, actor, kind, projection, user_id):
        with lock:
            if command_id not in turns:
                turns[command_id] = {'turnId': 'turn-' + str(len(turns)), 'ordinal': len(turns), 'createdAt': 1000,
                    'conversationId': conv, 'actor': actor, 'kind': kind, 'projection': deepcopy(projection), 'owner': user_id}
            return {'turn': deepcopy(turns[command_id])}
    def get(conv, turn_id, *, user_id):
        return next((deepcopy(t) for t in turns.values() if t['turnId'] == turn_id
                     and t['conversationId'] == conv and t['owner'] == user_id), None)
    def factory(owner=7, path='/project'):
        return CoordinatorReports(owner, path, client=store, bindings=bindings,
                                  append_turn=append, get_turn=get, notify=notices.append)
    scope = {'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator', 'bindingRevision': 1}
    request = {'title': 'Decision needed', 'message': 'Choose the migration window.', 'kind': 'decision', 'idempotency_key': 'one'}
    return SimpleNamespace(store=store, turns=turns, binding=binding, factory=factory,
                           repo=factory(), scope=scope, request=request, notices=notices)


def read_request(r, turn_id):
    return {'path': '/project', 'conversationId': 'coordinator', 'bindingRevision': 1, 'turnId': turn_id}


def test_report_is_an_assistant_turn_with_durable_unread_and_explicit_read(reports):
    r = reports
    assert r.repo.read()['latest'] is None
    sent = r.repo.publish(r.scope, r.request)
    state = r.factory().read()
    assert state['unread'] and state['latest']['turnId'] == sent['turnId']
    assert state['latest']['content'] == 'Decision needed\n\nChoose the migration window.'
    assert next(iter(r.turns.values()))['projection']['origin']['initiator'] == 'brain'
    assert next(iter(r.turns.values()))['actor'] == 'assistant'
    assert not any(ns == POLICY['pendingNamespace'] for ns, _ in r.store.rows)
    assert r.repo.mark_read(read_request(r, sent['turnId']))['unread'] is False
    assert r.factory().read()['unread'] is False
    assert len(r.notices) == 2


def test_retry_and_old_ack_do_not_duplicate_messages_or_hide_newer_report(reports):
    r = reports
    first = r.repo.publish(r.scope, r.request)
    r.repo.mark_read(read_request(r, first['turnId']))
    assert r.repo.publish(r.scope, r.request) == first
    assert not r.repo.read()['unread'] and len(r.turns) == 1
    second = r.repo.publish(r.scope, {**r.request, 'idempotency_key': 'two', 'message': 'New result'})
    state = r.repo.mark_read(read_request(r, first['turnId']))
    assert state['unread'] and state['latest']['turnId'] == second['turnId']
    r.repo.publish(r.scope, r.request)
    assert r.repo.read()['latest']['turnId'] == second['turnId']
    assert len(r.turns) == 2


def test_pending_intent_recovers_after_turn_commit_but_failed_attention_write(reports):
    r = reports; r.store.fail_head = True
    with pytest.raises(StorageError):
        r.repo.publish(r.scope, r.request)
    assert len(r.turns) == 1
    assert any(ns == POLICY['pendingNamespace'] for ns, _ in r.store.rows)
    r.store.fail_head = False
    assert recover_pending_reports(client=r.store, repository_factory=r.factory) == 1
    assert r.repo.read()['unread'] and len(r.turns) == 1
    assert recover_pending_reports(client=r.store, repository_factory=r.factory) == 0


@pytest.mark.parametrize('namespace', [POLICY['headNamespace'], POLICY['pendingNamespace']])
def test_lost_cas_ack_is_recovered_by_exact_readback(reports, namespace):
    r = reports; r.store.lost_ack = namespace
    r.repo.publish(r.scope, r.request)
    assert r.repo.read()['unread'] and len(r.turns) == 1


def test_binding_replacement_withdraws_pending_transport_and_keeps_old_message(reports):
    r = reports; r.store.fail_head = True
    with pytest.raises(StorageError): r.repo.publish(r.scope, r.request)
    r.binding.update(conversationId='replacement', revision=2); r.store.fail_head = False
    assert recover_pending_reports(client=r.store, repository_factory=r.factory) == 0
    assert len(r.turns) == 1 and r.repo.read()['latest'] is None
    with pytest.raises(PermissionError): r.repo.publish(r.scope, r.request)
    with pytest.raises(PermissionError): r.repo.mark_read(read_request(r, 'turn-0'))


def test_reads_spend_nothing_and_report_owner_and_scope_are_exact(reports):
    r = reports; r.repo.publish(r.scope, r.request)
    before = deepcopy(r.store.rows)
    r.factory().read(); r.factory().read()
    assert r.store.rows == before and len(r.turns) == 1
    assert r.factory(8).read()['latest'] is None
    with pytest.raises(PermissionError):
        r.factory(8).publish(r.scope, r.request)
    with pytest.raises(PermissionError):
        r.factory(path='/other').publish(r.scope, r.request)


def test_concurrent_identical_reports_commit_one_turn_and_one_attention_position(reports):
    from concurrent.futures import ThreadPoolExecutor
    r = reports
    with ThreadPoolExecutor(max_workers=8) as executor:
        values = list(executor.map(lambda _: r.factory().publish(r.scope, r.request), range(8)))
    assert len({value['turnId'] for value in values}) == 1
    assert len(r.turns) == 1 and r.repo.read()['unread']


def test_changed_idempotent_request_is_rejected_and_never_changes_original(reports):
    r = reports; r.repo.publish(r.scope, r.request)
    with pytest.raises(ValueError, match='conflicts'):
        r.repo.publish(r.scope, {**r.request, 'message': 'Changed body'})
    assert 'Changed body' not in r.repo.read()['latest']['content']
    assert not any(ns == POLICY['pendingNamespace'] for ns, _ in r.store.rows)
    assert r.repo.publish(r.scope, r.request)['published']
    assert len(r.turns) == 1


def test_new_report_racing_read_cannot_be_marked_read_by_stale_tab(reports, monkeypatch):
    r = reports; first = r.repo.publish(r.scope, r.request)
    put = r.repo._put; raced = False
    def race(namespace, key, value, revision):
        nonlocal raced
        if not raced and namespace == POLICY['headNamespace']:
            raced = True
            r.factory().publish(r.scope, {**r.request, 'idempotency_key': 'new', 'message': 'New report'})
        return put(namespace, key, value, revision)
    monkeypatch.setattr(r.repo, '_put', race)
    result = r.repo.mark_read(read_request(r, first['turnId']))
    assert result['unread'] and result['latest']['turnId'] != first['turnId']


@pytest.mark.parametrize('patch', [
    {'title': ' '}, {'title': 'a\nb'}, {'message': ' '}, {'message': 'x' * 4001},
    {'idempotency_key': ' '}, {'message': '\ud800'}, {'kind': 'urgent'}, {'owner': 8},
])
def test_invalid_reports_have_no_message_or_attention_effect(reports, patch):
    with pytest.raises((ValueError, UnicodeError)):
        reports.repo.publish(reports.scope, {**reports.request, **patch})
    assert not reports.turns and not reports.store.rows


def test_recovery_pages_through_all_intents_and_does_not_retain_completed_transport(reports):
    r = reports; r.store.fail_head = True
    for index in range(POLICY['recoveryPageSize'] + 1):
        with pytest.raises(StorageError):
            r.repo.publish(r.scope, {**r.request, 'idempotency_key': str(index)})
    r.store.fail_head = False
    assert recover_pending_reports(client=r.store, repository_factory=r.factory) == POLICY['recoveryPageSize'] + 1
    assert len(r.store.rows) == 1 and r.repo.read()['unread']
    assert len(r.turns) == POLICY['recoveryPageSize'] + 1
