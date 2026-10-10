"""Proof bundle assembly and verification semantics.

Readers (``lib.tasks_pkg.*``) are faked at the source modules, matching the
lazy-import seam documented in ``lib/proof/evidence.py``. No storage, no
 Quart app: these are pure application-layer specifications.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

OWNER = 42
TASK_ID = 'task-1'


def _snapshot(**overrides):
    base = dict(
        task_id=TASK_ID, user_id=OWNER, status='done', conv_id='conv-1',
        content='final answer', thinking='reasoning trace', error=None,
        metadata={'finishReason': 'stop', 'model': 'kimi-k3',
                  'usage': {'input_tokens': 10, 'output_tokens': 5},
                  'modifiedFiles': 1, 'modifiedFileList': ['src/a.py']},
        created_at_ms=1, updated_at_ms=2, completed_at_ms=3,
        event_replay={'base_cursor': 0, 'next_cursor': 0},
        terminal_payload_loaded=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _payload(round_num, turn):
    return {'taskId': TASK_ID, 'roundNum': round_num, 'kind': 'request',
            'turn': turn, 'model': 'kimi-k3', 'params': {'temperature': 0.3},
            'messages': [{'role': 'user', 'content': f'round {round_num}'}],
            'tools': [{'type': 'function', 'function': {'name': 'search'}}]}


@pytest.fixture
def readers(monkeypatch):
    """Fake the four evidence authorities; return the mutable state."""
    state = {
        'snapshot': _snapshot(),
        'trace': {'version': 1, 'taskId': TASK_ID, 'eventsAvailable': True,
                  'source': 'turn-snapshot', 'spans': [
                      {'spanId': 'r1', 'kind': 'llm', 'status': 'done'}]},
        'rounds': [{'roundNum': 1, 'turn': 'request:aa'},
                   {'roundNum': 2, 'turn': 'request:bb'}],
        'payloads': {(1, 'request:aa'): _payload(1, 'request:aa'),
                     (2, 'request:bb'): _payload(2, 'request:bb')},
    }
    monkeypatch.setattr(
        'lib.tasks_pkg.durable_chat_replay.load_durable_chat_task',
        lambda task_id, *, user_id: (
            state['snapshot'] if user_id == OWNER else None))
    monkeypatch.setattr(
        'lib.tasks_pkg.turn_trace.read_persisted_task_trace',
        lambda task_id, *, user_id: state['trace'])
    monkeypatch.setattr(
        'lib.tasks_pkg.request_inspector.fold_request_log',
        lambda task_id: {
            'taskId': task_id, 'requests': state['rounds'],
            'eventsAvailable': True,
            'requestCount': len(state['rounds']), 'coverage': 'full'})
    monkeypatch.setattr(
        'lib.tasks_pkg.request_inspector.get_request_payload',
        lambda task_id, round_num, turn='', kind='request', user_id=None:
            state['payloads'].get((round_num, turn)))
    return state


def _reseal(bundle: dict) -> dict:
    """Recompute a bundle's content address after editing it (attacker
    re-sealing is detected by evidence re-check, not by the address)."""
    from lib.proof.digests import bundle_id
    bundle['bundleId'] = bundle_id(
        {key: value for key, value in bundle.items() if key != 'bundleId'})
    return bundle


def _server_verify(bundle):
    from lib.proof.evidence import server_evidence_resolver
    from lib.proof.verify import verify_proof_bundle
    return verify_proof_bundle(
        bundle, resolver=server_evidence_resolver(OWNER))


def test_assemble_full_coverage_bundle_validates(readers):
    from lib.proof import assemble_task_proof
    from lib.proof.contract import validate_bundle
    bundle = assemble_task_proof(TASK_ID, owner_user_id=OWNER)
    assert bundle['coverage'] == 'full'
    assert bundle['coverageReasons'] == []
    assert [item['kind'] for item in bundle['evidence']] == [
        'turn_trace', 'request_round', 'request_round', 'file_receipt']
    assert bundle['subject']['identity']['taskId'] == TASK_ID
    assert bundle['verdicts']['task']['status'] == 'done'
    assert bundle['verdicts']['task']['usage'] == {
        'input_tokens': 10, 'output_tokens': 5}
    assert bundle['contentIncluded'] is False
    validate_bundle(bundle)
    # The content address covers every other byte of the bundle.
    from lib.proof.digests import bundle_id
    assert bundle['bundleId'] == bundle_id(
        {k: v for k, v in bundle.items() if k != 'bundleId'})


def test_assembly_evidence_digests_are_deterministic(readers):
    from lib.proof import assemble_task_proof
    first = assemble_task_proof(TASK_ID, owner_user_id=OWNER)
    second = assemble_task_proof(TASK_ID, owner_user_id=OWNER)
    assert [i['digest'] for i in first['evidence']] == \
           [i['digest'] for i in second['evidence']]
    assert first['subject']['contentDigest'] == \
        second['subject']['contentDigest']


def test_round_projection_is_content_free(readers):
    from lib.proof import assemble_task_proof
    bundle = assemble_task_proof(TASK_ID, owner_user_id=OWNER)
    serialized = str(bundle['evidence'])
    assert 'round 1' not in serialized  # message text never enters a bundle
    assert 'final answer' not in serialized


def test_server_verification_valid(readers):
    from lib.proof import assemble_task_proof
    bundle = assemble_task_proof(TASK_ID, owner_user_id=OWNER)
    result = _server_verify(bundle)
    assert result['verificationScope'] == 'full'
    assert result['verdict'] == 'valid'
    assert result['checks']['evidence']['matched'] == 4


def test_stale_bundle_id_is_tampered(readers):
    from lib.proof import assemble_task_proof
    bundle = assemble_task_proof(TASK_ID, owner_user_id=OWNER)
    bundle['verdicts']['task']['status'] = 'error'  # edited without resealing
    result = _server_verify(bundle)
    assert result['verdict'] == 'tampered'
    assert 'bundle-id-mismatch' in result['reasons']


def test_resealed_digest_edit_is_tampered(readers):
    """The attacker who re-seals after editing an evidence digest is caught
    by the live evidence re-check — content addressing alone is not the
    defense; the resolver is."""
    from lib.proof import assemble_task_proof
    bundle = assemble_task_proof(TASK_ID, owner_user_id=OWNER)
    forged = 'sha256:' + ('f' if bundle['evidence'][0]['digest'][7] != 'f'
                          else 'e') * 64
    bundle['evidence'][0]['digest'] = forged
    _reseal(bundle)
    result = _server_verify(bundle)
    assert result['verdict'] == 'tampered'
    assert result['checks']['evidence']['mismatched']


def test_evidence_content_change_is_tampered(readers):
    from lib.proof import assemble_task_proof
    bundle = assemble_task_proof(TASK_ID, owner_user_id=OWNER)
    readers['trace']['spans'][0]['status'] = 'error'  # authority changed
    result = _server_verify(bundle)
    assert result['verdict'] == 'tampered'


def test_missing_trace_degrades_to_partial(readers):
    from lib.proof import assemble_task_proof
    readers['trace'] = None
    bundle = assemble_task_proof(TASK_ID, owner_user_id=OWNER)
    assert bundle['coverage'] == 'partial'
    assert {'evidence': 'turn_trace', 'reason': 'unavailable'} in \
        bundle['coverageReasons']
    result = _server_verify(bundle)
    assert result['verdict'] == 'partial'


def test_unavailable_round_log_degrades_to_partial(readers, monkeypatch):
    from lib.proof import assemble_task_proof
    # eventsAvailable False is the honest "records cleaned up" empty state.
    monkeypatch.setattr(
        'lib.tasks_pkg.request_inspector.fold_request_log',
        lambda task_id: {'taskId': task_id, 'requests': [],
                         'eventsAvailable': False, 'requestCount': 0,
                         'coverage': 'full'})
    bundle = assemble_task_proof(TASK_ID, owner_user_id=OWNER)
    assert bundle['coverage'] == 'partial'
    assert {'evidence': 'request_round', 'reason': 'unavailable'} in \
        bundle['coverageReasons']


def test_empty_but_readable_round_log_is_a_fact(readers):
    from lib.proof import assemble_task_proof
    readers['rounds'] = []
    bundle = assemble_task_proof(TASK_ID, owner_user_id=OWNER)
    # A readable log with zero rounds is a fact: no round evidence, but
    # also no coverage gap (trace + file receipt remain).
    assert bundle['coverage'] == 'full'
    assert [i['kind'] for i in bundle['evidence']] == [
        'turn_trace', 'file_receipt']


def test_round_read_error_degrades_to_partial(readers, monkeypatch):
    from lib.proof import assemble_task_proof
    monkeypatch.setattr(
        'lib.tasks_pkg.request_inspector.get_request_payload',
        lambda *a, **kw: None)
    bundle = assemble_task_proof(TASK_ID, owner_user_id=OWNER)
    assert bundle['coverage'] == 'partial'
    assert {'evidence': 'request_round', 'reason': 'read-error'} in \
        bundle['coverageReasons']


def test_round_cap_is_explicit(readers):
    from lib.proof import assemble_task_proof
    readers['rounds'] = [{'roundNum': n, 'turn': f'request:{n}'}
                         for n in range(1, 6)]
    readers['payloads'] = {(n, f'request:{n}'): _payload(n, f'request:{n}')
                           for n in range(1, 6)}
    bundle = assemble_task_proof(TASK_ID, owner_user_id=OWNER, max_rounds=2)
    rounds = [i for i in bundle['evidence'] if i['kind'] == 'request_round']
    assert len(rounds) == 2
    assert {'evidence': 'request_round', 'reason': 'round-cap'} in \
        bundle['coverageReasons']
    assert bundle['coverage'] == 'partial'


def test_file_receipt_empty_list_is_a_fact_not_a_gap(readers):
    from lib.proof import assemble_task_proof
    readers['snapshot'] = _snapshot(metadata={
        'finishReason': 'stop', 'modifiedFiles': 0, 'modifiedFileList': []})
    bundle = assemble_task_proof(TASK_ID, owner_user_id=OWNER)
    assert bundle['coverage'] == 'full'
    assert any(i['kind'] == 'file_receipt' for i in bundle['evidence'])


def test_file_receipt_absent_lane_degrades_to_partial(readers):
    from lib.proof import assemble_task_proof
    readers['snapshot'] = _snapshot(metadata={'finishReason': 'stop'})
    bundle = assemble_task_proof(TASK_ID, owner_user_id=OWNER)
    assert bundle['coverage'] == 'partial'
    assert {'evidence': 'file_receipt', 'reason': 'unavailable'} in \
        bundle['coverageReasons']


def test_unknown_evidence_when_resolver_cannot_reread(readers):
    from lib.proof import assemble_task_proof
    bundle = assemble_task_proof(TASK_ID, owner_user_id=OWNER)
    readers['trace'] = None  # evidence expired after assembly
    result = _server_verify(bundle)
    assert result['verdict'] == 'unknown_evidence'
    assert result['checks']['evidence']['unavailable']


def test_structural_verify_without_resolver(readers):
    from lib.proof import assemble_task_proof
    from lib.proof.verify import verify_proof_bundle
    bundle = assemble_task_proof(TASK_ID, owner_user_id=OWNER)
    result = verify_proof_bundle(bundle)
    assert result['verificationScope'] == 'structural'
    assert result['verdict'] == 'valid'


def test_malformed_bundles_are_tampered():
    from lib.proof.verify import verify_proof_bundle
    assert verify_proof_bundle('not-a-dict')['verdict'] == 'tampered'
    assert verify_proof_bundle({'version': 1})['verdict'] == 'tampered'
    assert 'schema-invalid' in verify_proof_bundle(
        {'version': 1})['reasons'][0]


def test_foreign_task_is_invisible_to_assembly(readers):
    from lib.proof import assemble_task_proof
    assert assemble_task_proof(TASK_ID, owner_user_id=OWNER + 1) is None


def test_error_kind_is_extracted_from_envelope(readers):
    from lib.proof import assemble_task_proof
    readers['snapshot'] = _snapshot(
        status='error', error={'kind': 'tool_call_rejected',
                               'message': 'x'})
    bundle = assemble_task_proof(TASK_ID, owner_user_id=OWNER)
    assert bundle['verdicts']['task']['errorKind'] == 'tool_call_rejected'


def test_invalid_owner_rejected():
    from lib.proof import assemble_task_proof
    from lib.proof.errors import ProofError
    for bad in (0, -1, True, '42', None):
        with pytest.raises(ProofError):
            assemble_task_proof(TASK_ID, owner_user_id=bad)
