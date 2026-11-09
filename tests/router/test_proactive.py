"""Specs for the proactive question ledger, quiet-hours hold and digest.

docs/ROUTER.md R10: owner questions are indexed at the ask_human seam, answered
questions are detected from durable evidence, quiet-hours relays are held and
released as one message, and the digest is deterministic (no model call).
"""
from copy import deepcopy
import json
from types import SimpleNamespace
import threading

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True, scope='module')
def router_modules_stay_unpatched():
    """Each spec's fakes are scoped to it: later tests in this process see the real modules."""
    import lib.channels.outbox as outbox_module
    import lib.router.binding as binding_module
    import lib.router.projects as projects_module
    import lib.router.proactive.digest as digest_module
    import lib.router.proactive.questions as questions_module
    import lib.router.relay as relay_module
    import lib.router.watchdog.binding as watchdog_binding_module
    attributes = ((binding_module, 'global_router'), (relay_module, '_command_chat'),
                  (questions_module, 'open_questions'), (projects_module, 'router_projects'),
                  (watchdog_binding_module, 'router_watchdog'), (digest_module, 'assemble_digest'),
                  (outbox_module, 'enqueue'))
    real = [getattr(module, name) for module, name in attributes]
    yield
    leaked = [f'{module.__name__}.{name}' for (module, name), value in zip(attributes, real)
              if getattr(module, name) is not value]
    assert not leaked, f'a proactive spec leaked fakes into the rest of the test process: {leaked}'


class Store:
    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()

    def __call__(self, **kwargs):
        return self

    def query(self, operation, payload, **kwargs):
        if operation == 'record.get':
            return deepcopy(self.rows.get((payload['namespace'], payload['key'])))
        if operation == 'record.list':
            prefix = payload['prefix']
            after = payload.get('after_key', '')
            limit = payload.get('limit', 1000)
            matching = sorted(
                (key for (ns, key) in self.rows
                 if ns == payload['namespace'] and key.startswith(prefix) and key > after),
            )
            return [{'key': key, **deepcopy(self.rows[(payload['namespace'], key)])}
                    for key in matching[:limit]]
        raise ValueError(f'unknown operation {operation}')

    def command(self, operation, payload, command_id):
        identity = (payload['namespace'], payload['key'])
        with self.lock:
            old = self.rows.get(identity)
            expected = payload.get('expected_version', 0)
            if (old['version'] if old else 0) != expected:
                from lib.storage.errors import StorageError
                raise StorageError('database_conflict', 'CAS conflict')
            if operation == 'record.delete':
                self.rows.pop(identity, None)
                return {'deleted': True}
            self.rows[identity] = {'value': deepcopy(payload['value']), 'version': expected + 1}
            return {'version': expected + 1}


def test_record_and_read_open_questions():
    from lib.router.proactive.questions import record_question, open_questions
    store = Store()
    question = {'projectPath': '/project', 'coordinatorId': 'coordinator', 'turnId': 'turn-1',
                'toolCallId': 'tc-1', 'guidanceId': 'hg-1', 'blocking': True,
                'question': 'Should we restart?', 'options': []}
    record_question(7, question, client=store)
    # Idempotent: recording again returns the same record.
    record_question(7, question, client=store)
    open_qs = open_questions(7, client=store, turn_get=lambda *a, **kw: None)
    assert len(open_qs) == 1
    assert open_qs[0]['guidanceId'] == 'hg-1'
    assert open_qs[0]['blocking'] is True


def test_answered_blocking_question_is_not_open():
    from lib.router.proactive.questions import record_question, open_questions
    store = Store()
    question = {'projectPath': '/project', 'coordinatorId': 'coordinator', 'turnId': 'turn-1',
                'toolCallId': 'tc-1', 'guidanceId': 'hg-1', 'blocking': True,
                'question': 'Should we restart?', 'options': []}
    record_question(7, question, client=store)
    # The card has a response.
    turn = {'projection': {'toolRounds': [
        {'guidanceId': 'hg-1', 'guidanceResponse': 'yes', 'guidanceAnswerCommandId': 'cmd-1'}]}}
    open_qs = open_questions(7, client=store, turn_get=lambda *a, **kw: turn)
    assert open_qs == []


def test_answered_nonblocking_question_is_not_open():
    from lib.router.proactive.questions import record_question, open_questions
    store = Store()
    question = {'projectPath': '/project', 'coordinatorId': 'coordinator', 'turnId': 'turn-1',
                'toolCallId': 'tc-1', 'guidanceId': 'hg-1', 'blocking': False,
                'question': 'Should we restart?', 'options': []}
    record_question(7, question, client=store)
    # A human Turn after the question's Turn answers it.
    turn = {'ordinal': 5, 'projection': {'toolRounds': []}}
    later = {'turns': [{'actor': 'human', 'ordinal': 6}]}
    open_qs = open_questions(7, client=store, turn_get=lambda *a, **kw: turn,
                             turn_lister=lambda *a, **kw: later)
    assert open_qs == []


def test_quiet_hours_detection():
    from datetime import datetime
    from lib.router.proactive.held import quiet_hours_active, quiet_end
    assert quiet_hours_active(datetime(2026, 11, 8, 2, 0)) is True
    assert quiet_hours_active(datetime(2026, 11, 8, 7, 59)) is True
    assert quiet_hours_active(datetime(2026, 11, 8, 8, 0)) is False
    assert quiet_hours_active(datetime(2026, 11, 8, 12, 0)) is False
    end = quiet_end(datetime(2026, 11, 8, 2, 0))
    assert end.hour == 8 and end.minute == 0 and end.day == 8
    end = quiet_end(datetime(2026, 11, 8, 9, 0))
    assert end.day == 9


def test_hold_and_release_relays(monkeypatch):
    from lib.router.proactive.held import hold_relay, held_relays, release_held
    store = Store()
    payload = {'kind': 'progress', 'title': 'Restart done', 'message': 'Verified.'}
    hold_relay(7, 'report', 'cmd-1', payload, client=store)
    # Idempotent: holding again returns the same record.
    _, created = hold_relay(7, 'report', 'cmd-1', payload, client=store)
    assert created is False
    rows = held_relays(7, client=store)
    assert len(rows) == 1
    assert rows[0]['payload']['title'] == 'Restart done'
    # Release: held records are deleted, a Router notice is dispatched.
    sent = []
    dispatched = []
    # The release imports from lib.router.relay; patch it there.
    monkeypatch.setattr('lib.router.relay._command_chat', lambda owner, client: None)
    monkeypatch.setattr('lib.router.binding.global_router', lambda owner, **kw: {
        'availability': 'available', 'conversationId': 'router-conv', 'revision': 1})
    def fake_dispatch(conv, message, tools, **kwargs):
        dispatched.append((conv, message, kwargs))
        return SimpleNamespace(disposition='started')
    count, summary = release_held(7, client=store, dispatch=fake_dispatch)
    assert count == 1
    assert '1 report' in summary or '1 held' in summary
    assert held_relays(7, client=store) == []
    assert len(dispatched) == 1
    assert dispatched[0][0] == 'router-conv'
    assert dispatched[0][2]['inject_mode'] == 'steer'


def test_digest_assembles_open_decisions_and_skips_when_empty(monkeypatch):
    from lib.router.proactive.digest import assemble_digest
    store = Store()
    # Empty: no questions, no commitments, no faults.
    monkeypatch.setattr('lib.router.proactive.questions.open_questions', lambda owner, **kw: [])
    monkeypatch.setattr('lib.router.projects.router_projects',
                        lambda owner, args, **kw: {'projects': [], 'next_cursor': None})
    monkeypatch.setattr('lib.router.watchdog.binding.router_watchdog',
                        lambda owner, **kw: {'availability': 'unavailable'})
    text, counts = assemble_digest(7, client=store)
    assert text is None
    # With one open question:
    monkeypatch.setattr('lib.router.proactive.questions.open_questions', lambda owner, **kw: [
        {'guidanceId': 'hg-1', 'question': 'Should we restart?', 'blocking': True,
         'createdAt': 0, 'default': ''}])
    text, counts = assemble_digest(7, client=store)
    assert text is not None
    assert 'Should we restart?' in text
    assert counts['questions'] == 1
    assert 'blocking' in text.lower() or '⏸' in text


def test_digest_delivery_through_outbox(monkeypatch):
    from lib.router.proactive.digest import run_digest
    monkeypatch.setattr('lib.router.proactive.digest.assemble_digest',
                        lambda owner, **kw: ('Test digest text', {'questions': 1}))
    sent = []
    monkeypatch.setattr('lib.router.relay._command_chat', lambda owner, client: 'dx:gid:12345')
    monkeypatch.setattr('lib.channels.outbox.enqueue',
                        lambda owner, thread, text, **kw: sent.append((thread, text, kw)))
    ok, result = run_digest({'user_id': 7})
    assert ok, result
    assert len(sent) == 1
    assert sent[0][0] == 'dx:gid:12345'
    assert sent[0][1] == 'Test digest text'
    assert sent[0][2]['idempotency_key'].startswith('digest:7:')
