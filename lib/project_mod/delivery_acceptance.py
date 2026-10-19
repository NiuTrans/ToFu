"""Version-bound gate evidence projected from the existing workspace authority.

Entry: gate_record, persist_gate_record, project_acceptance. Only the real Git
verification owner produces records. This module never runs a gate, publishes a
workspace, grants approval or treats an ordinary task verdict as acceptance.
Policy: docs/WORKSPACE_ACCEPTANCE.md.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path


def command_digest(command: str) -> str:
    return hashlib.sha256(command.encode('utf-8')).hexdigest()


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).resolve().parents[2] /
                         'contracts/workspace_acceptance_v1.schema.json').read_text())
    return Draft202012Validator(schema['$defs']['Record'])


def gate_record(*, base, target, tree, command, passed, observed_at, observations):
    """A check result for one immutable candidate, not correctness of the task."""
    record = {
        'version': 1, 'baseRevision': base, 'targetRevision': target,
        'targetTree': tree, 'checkpointRevision': target,
        'commandDigest': command_digest(command), 'observedAt': observed_at,
        'scope': 'configured_checks' if command else 'builtin_checks',
        'outcome': 'passed' if passed else 'failed',
        'checks': [
            {'id': 'builtin', 'outcome': observations.get('builtin', 'unknown')},
            {'id': 'project_command', 'outcome': observations.get('project_command', 'not_run')},
        ],
    }
    _validator().validate(record)
    return record


def persist_gate_record(row, record, *, checkpoint):
    """Join evidence to the exact owner/workspace before publication can proceed."""
    from lib.integration_state_repository import set_workspace_meta
    from time import time
    if not record:
        return  # An old/custom gate without a receipt cannot claim verification.
    value = dict(record, checkpointRevision=checkpoint)
    _validator().validate(value)
    origin = set_workspace_meta(user_id=int(row['user_id']),
        project_root=row['project_root'], task_id=row['task_id'],
        patch={'acceptanceRecord': value}, now=time())
    row['origin'] = origin


def project_acceptance(row, *, current_command=None):
    """Source revision changes invalidate old evidence without rewriting history."""
    record = (row.get('origin') or {}).get('acceptanceRecord')
    if not isinstance(record, dict) or not _validator().is_valid(record):
        return {'status': 'unverified', 'reason': 'no_versioned_evidence', 'record': None}
    # A workspace's package identity is its checkpoint, not its display title or
    # the current branch of a different writer. Old accepted versions stay facts.
    if record['checkpointRevision'] != (row.get('checkpoint_sha') or ''):
        status, reason = 'stale', 'checkpoint_changed'
    elif current_command is None:
        status, reason = 'unverified', 'current_policy_unavailable'
    elif record['commandDigest'] != command_digest(current_command):
        status, reason = 'stale', 'criteria_changed'
    elif record['outcome'] != 'passed':
        status, reason = 'failed', 'checks_failed'
    elif record['scope'] != 'configured_checks' or any(
            check['outcome'] != 'passed' for check in record['checks']):
        status, reason = 'unverified', 'project_checks_missing'
    else:
        status, reason = 'verified', 'exact_candidate_checked'
    return {'status': status, 'reason': reason, 'record': record}
