"""Proof bundle verification core.

Pure and offline-capable: structural checks (schema + content address) run
anywhere, with no server access. When a server-side evidence ``resolver``
is supplied, every evidence digest is also recomputed against the live
authorities — a mismatch is ``tampered``; an unreadable or expired item is
``unknown_evidence``.

Verdict precedence: ``tampered`` > ``unknown_evidence`` > ``partial`` >
``valid``. A bundle assembled with degraded coverage caps at ``partial``
and is never promoted to ``valid`` — the same honest-semantics rule the
Turn Trace coverage contract uses.
"""
from __future__ import annotations

from .contract import validate_bundle
from .digests import bundle_id
from .errors import ProofError

VERDICT_VALID = 'valid'
VERDICT_PARTIAL = 'partial'
VERDICT_TAMPERED = 'tampered'
VERDICT_UNKNOWN_EVIDENCE = 'unknown_evidence'


def _result(verdict: str, scope: str, checks: dict,
            reasons: list) -> dict:
    return {'verdict': verdict,
            'verificationScope': scope,
            'checks': checks,
            'reasons': reasons}


def verify_proof_bundle(bundle, *, resolver=None) -> dict:
    """Verify one submitted bundle.

    ``resolver`` maps one evidence item to its recomputed digest (or
    ``None`` when unreadable); ``lib.proof.evidence.server_evidence_resolver``
    is the server implementation. Without a resolver the result is
    structural only and says so (``verificationScope='structural'``).
    """
    scope = 'full' if resolver is not None else 'structural'
    checks = {'schema': False,
              'bundleId': False,
              'evidence': {'matched': 0, 'mismatched': [], 'unavailable': []}}
    if not isinstance(bundle, dict):
        return _result(VERDICT_TAMPERED, scope, checks, ['schema-invalid'])
    try:
        validate_bundle(bundle)
    except ProofError as exc:
        return _result(VERDICT_TAMPERED, scope, checks,
                       [f'schema-invalid:{exc.message}'])
    checks['schema'] = True

    expected_id = bundle_id({key: value for key, value in bundle.items()
                             if key != 'bundleId'})
    if expected_id != bundle.get('bundleId'):
        return _result(VERDICT_TAMPERED, scope, checks,
                       ['bundle-id-mismatch'])
    checks['bundleId'] = True

    if resolver is not None:
        evidence_checks = checks['evidence']
        for item in bundle['evidence']:
            recomputed = resolver(item)
            if recomputed is None:
                evidence_checks['unavailable'].append(item['ref'])
            elif recomputed != item['digest']:
                evidence_checks['mismatched'].append(item['ref'])
            else:
                evidence_checks['matched'] += 1
        if evidence_checks['mismatched']:
            return _result(VERDICT_TAMPERED, scope, checks,
                           ['evidence-mismatch'])
        if evidence_checks['unavailable']:
            return _result(VERDICT_UNKNOWN_EVIDENCE, scope, checks,
                           ['evidence-unavailable'])

    if bundle['coverage'] == 'partial':
        return _result(VERDICT_PARTIAL, scope, checks,
                       ['assembly-partial'])
    return _result(VERDICT_VALID, scope, checks, [])
