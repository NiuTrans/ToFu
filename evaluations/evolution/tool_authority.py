"""Offline admission for paper tool-surface comparisons; never calls a provider.

Compare exact executable schemas and their authoritative contract documents.
Wire projection is the independent variable. Equal counts or wire schemas alone
cannot establish equal execution authority. CLI builds the existing frozen arms.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def _catalog(epoch):
    schemas = epoch.executable_schemas
    if epoch.degraded_reason or not 1 <= len(schemas) <= 512:
        raise ValueError('tool authority requires a nonempty, nondegraded epoch')
    rows = {}
    for schema in schemas:
        name = schema.get('function', {}).get('name')
        if not isinstance(name, str) or not 1 <= len(name) <= 240 or name in rows:
            raise ValueError('tool authority has an invalid or duplicate name')
        document = epoch.contract_documents_by_name.get(name)
        if not isinstance(document, dict) or not document:
            raise ValueError('tool authority requires every executable contract document')
        rows[name] = {'schema_sha256': _hash(schema), 'contract_sha256': _hash(document)}
    return rows


def compare_tool_authority(control, candidate):
    """Return a bounded preflight receipt; no quality or activation authority."""
    left, right = _catalog(control), _catalog(candidate)
    common = left.keys() & right.keys()
    changed = sorted(name for name in common if left[name] != right[name])
    added, removed = sorted(right.keys() - left.keys()), sorted(left.keys() - right.keys())
    matched = not (added or removed or changed)
    return {'contract_version': 'tofu-tool-authority-preflight/v1',
            'provider_called': False, 'activation_eligible': False,
            'authority_equal': matched, 'comparison_admitted': matched,
            'reason': 'matched_execution_contracts' if matched else 'execution_authority_mismatch',
            'control': {'catalog': left, 'catalog_sha256': _hash(left), 'epoch_hash': control.epoch_hash},
            'candidate': {'catalog': right, 'catalog_sha256': _hash(right), 'epoch_hash': candidate.epoch_hash},
            'added_names': added, 'removed_names': removed, 'changed_names': changed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner-id', required=True, type=int)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.owner_id <= 0:
        parser.error('--owner-id must be positive')
    from .paired import reserve_report, write_report
    reserve_report(args.output, {'evaluation_state': 'preparing', 'provider_called': False,
                                 'comparison_admitted': False, 'activation_eligible': False})
    from lib.paper.tools import build_paper_full_tool_epoch
    from lib.experiments.long_agent_strategy import LONG_AGENT_POLICIES
    policies = {name: LONG_AGENT_POLICIES[name] for name in ('control', 'tool_surface_v2')}
    def epoch(arm):
        policy = policies[arm]
        return build_paper_full_tool_epoch(owner_user_id=args.owner_id, model=args.model,
            cfg={'tools': {'schemaBudgetTokens': policy['schemaBudgetTokens'],
                           'resultEnvelope': policy['resultEnvelope']}})
    result = compare_tool_authority(epoch('control'), epoch('tool_surface_v2'))
    result.update(evaluation_state='completed', model=args.model, policies=policies,
                  policies_sha256=_hash(policies), harness_sha256=_hash(Path(__file__).read_text()))
    write_report(args.output, result)
    return 0 if result['comparison_admitted'] else 2



if __name__ == '__main__':
    raise SystemExit(main())
