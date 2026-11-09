"""review_project_work and cancel_project_work answer with compact effect receipts.

A receipt names what changed (assignment, decision, revisions, delivery) and the
next step. It never echoes the brief, the granted tool list or the Worker's
transcript: those stay with get_project_assignment and get_conversation, and
the old full view cost the Coordinator thousands of tokens on every call.
"""
import json

import pytest

from tests.project_mod.test_coordinator_continuation import (
    activate, delegate, end, ready, run as run)
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context

__all__ = ('runtime', 'base_runtime', 'context')

pytestmark = pytest.mark.unit

RECEIPT = {'assignment_id', 'decision', 'grant_revision', 'revoked', 'result_revision',
           'execution_status', 'checkpoint_sha', 'delivery', 'canonical_delivery', 'next_step'}


def _worker_turn_reads(r, monkeypatch):
    reads = []
    original = r.service.turn_get

    def turn_get(conversation, turn_id, **kwargs):
        reads.append((turn_id, kwargs.get('include_projection', True)))
        return original(conversation, turn_id, **kwargs)
    monkeypatch.setattr(r.service, 'turn_get', turn_get)
    monkeypatch.setattr(r.c.repo, 'turn_get', turn_get)
    return reads


def _echoes_view(r, worker, receipt):
    text = json.dumps(receipt, ensure_ascii=False)
    return any(value in text for value in (
        worker['request']['brief'], 'write_file', 'verified evidence', '"request"'))


@pytest.mark.parametrize('decision', ['accept', 'rework'])
def test_review_receipt_names_decision_revisions_and_delivery_only(run, monkeypatch, decision):
    r = run
    worker = ready(r)
    activate(r)
    reads = _worker_turn_reads(r, monkeypatch)
    receipt = r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                     decision=decision, note='Verified the exact evidence')
    assert set(receipt) == RECEIPT, receipt
    assert receipt['assignment_id'] == worker['id'] and receipt['decision'] == decision
    assert (receipt['grant_revision'], receipt['result_revision']) == (worker['grant']['revision'], 1)
    assert receipt['revoked'] is False and receipt['execution_status'] == 'completed'
    assert receipt['checkpoint_sha'] is None and receipt['canonical_delivery'] is True
    assert receipt['delivery']['status'] == 'delivered'
    assert ('rework_of' in receipt['next_step']) is (decision == 'rework')
    assert not _echoes_view(r, worker, receipt)
    # The receipt reads the Worker Turn's metadata; its projection can be megabytes.
    turn = worker['execution']['turnId']
    assert reads and all(projection is False for turn_id, projection in reads if turn_id == turn), reads
    assert r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                  decision=decision, note='Verified the exact evidence') == receipt


def test_cancel_receipt_names_the_withdrawn_grant_and_replays_identically(run, monkeypatch):
    r = run
    done = delegate(r)
    active = delegate(r, 'active')
    end(r, done)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']
    activate(r)
    reads = _worker_turn_reads(r, monkeypatch)
    receipt = r.call('cancel_project_work', assignment_id=active['id'])
    assert set(receipt) == RECEIPT, receipt
    assert receipt['assignment_id'] == active['id'] and receipt['decision'] == 'cancelled'
    assert receipt['revoked'] is True and receipt['grant_revision'] == active['grant']['revision'] + 1
    assert receipt['execution_status'] == 'interrupted'
    assert not _echoes_view(r, active, receipt)
    turn = active['execution']['turnId']
    assert all(projection is False for turn_id, projection in reads if turn_id == turn), reads
    assert r.call('cancel_project_work', assignment_id=active['id']) == receipt
