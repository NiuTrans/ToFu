"""Guard: deliverable_proof_v1 golden instances and typed negatives.

The wire authority is ``contracts/deliverable_proof_v1.schema.json``; the
lib validator is ``lib.proof.contract.validate_bundle``. Registered in
``docs/catalog.json``.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _golden_bundle() -> dict:
    from lib.proof.digests import bundle_id
    bundle = {
        'version': 1,
        'owner': 12,
        'assembledAt': 1727000000000,
        'subject': {
            'kind': 'turn',
            'identity': {'taskId': 'task-1', 'turnId': None,
                         'attemptId': 'att-1'},
            'contentDigest': 'sha256:' + '0' * 64,
        },
        'evidence': [
            {'kind': 'turn_trace', 'ref': {'taskId': 'task-1'},
             'digest': 'sha256:' + '1' * 64},
            {'kind': 'request_round',
             'ref': {'taskId': 'task-1', 'roundNum': 1, 'turn': 'request:ab'},
             'digest': 'sha256:' + '2' * 64},
            {'kind': 'file_receipt', 'ref': {'taskId': 'task-1'},
             'digest': 'sha256:' + '3' * 64},
        ],
        'verdicts': {'task': {'status': 'done', 'finishReason': 'stop',
                              'errorKind': None, 'model': 'kimi-k3',
                              'usage': {'input_tokens': 1}}},
        'coverage': 'full',
        'coverageReasons': [],
        'contentIncluded': False,
    }
    bundle['bundleId'] = bundle_id(bundle)
    return bundle


def test_golden_bundle_validates():
    from lib.proof.contract import validate_bundle
    assert validate_bundle(_golden_bundle())['version'] == 1


def test_golden_partial_bundle_validates():
    from lib.proof.contract import validate_bundle
    bundle = _golden_bundle()
    bundle['coverage'] = 'partial'
    bundle['coverageReasons'] = [
        {'evidence': 'turn_trace', 'reason': 'unavailable'}]
    assert validate_bundle(bundle)['coverage'] == 'partial'


def test_resource_limits_are_declared():
    from lib.proof.contract import limit
    assert limit('maxRounds') == 32
    assert limit('maxEvidenceItems') == 40
    assert limit('maxBundleBytes') == 1048576


def _mutations():
    def set_field(key, value):
        return lambda b: b.__setitem__(key, value)
    return [
        ('unknown top-level field', set_field('surprise', True)),
        ('bool owner is not an integer', set_field('owner', True)),
        ('owner below minimum', set_field('owner', 0)),
        ('owner as string', set_field('owner', '12')),
        ('bad digest pattern', lambda b: b['evidence'][0].__setitem__(
            'digest', 'md5:' + '1' * 32)),
        ('unknown evidence kind', lambda b: b['evidence'][0].__setitem__(
            'kind', 'vibes')),
        ('round number below one', lambda b: b['evidence'][1]['ref']
            .__setitem__('roundNum', 0)),
        ('extra ref field', lambda b: b['evidence'][2]['ref']
            .__setitem__('path', 'a.py')),
        ('unknown coverage reason', lambda b: b.__setitem__(
            'coverageReasons', [{'evidence': 'turn_trace',
                                 'reason': 'mystery'}])),
        ('coverage value outside enum', set_field('coverage', 'mostly')),
        ('contentIncluded not boolean', set_field('contentIncluded', 'no')),
        ('missing verdicts.task', lambda b: b.__setitem__('verdicts', {})),
        ('evidence above max items', lambda b: b.__setitem__(
            'evidence', b['evidence'] * 14)),
    ]


@pytest.mark.parametrize('label,mutate', _mutations(),
                         ids=[label for label, _ in _mutations()])
def test_typed_negatives_raise(label, mutate):
    from lib.proof.contract import validate_bundle
    from lib.proof.errors import ProofError
    bundle = _golden_bundle()
    mutate(bundle)
    with pytest.raises(ProofError) as excinfo:
        validate_bundle(bundle)
    assert excinfo.value.code == 'proof_contract', label
