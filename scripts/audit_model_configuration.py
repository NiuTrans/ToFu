#!/usr/bin/env python3
"""Read-only audit of an explicitly exported model-routing authority.

Usage: python3 scripts/audit_model_configuration.py --authority export.json
       [--plan-metadata-migration]
Accepts a public authority GET response or the aggregate itself. Never reads
credentials, connects to a server, edits authority, or performs paid probes.
Reports declared compiler consistency, not live provider compatibility.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.llm.reasoning import compile_reasoning
from lib.model_routing import normalize_document
from lib.model_routing.controls import _choices, reasoning_controls
from lib.model_routing.inference import candidate_inference_metadata
from lib.model_routing.metadata_migration import reconcile_offering_metadata


def audit_configuration(document, *, plan_migration=False):
    document = normalize_document(deepcopy(document))
    from lib.provider_template_recipes import load_provider_templates
    changes = (reconcile_offering_metadata(document, provider_templates=load_provider_templates())
               if plan_migration else [])
    models = {(m['creator_id'], m['model_id']): m for m in document['models']}
    offerings = {o['offering_id']: o for o in document['offerings']}
    connections = {c['connection_id']: c for c in document['connections']}
    states, fields = Counter(), Counter()
    failures, checked = [], 0
    for offering in offerings.values():
        if offering['identity_state'] == 'pending_identity':
            fields['pending_identity'] += 1
            continue
        for field in ('capabilities', 'context_window'):
            kind = ('legacy_unmarked' if 'inherited_model_fields' not in offering else
                    'inherited' if field in offering['inherited_model_fields'] else 'override')
            fields[f'{field}.{kind}'] += 1
    for deployment in document['deployments']:
        offering = offerings[deployment['offering_id']]
        ref = offering.get('model', {})
        candidate = SimpleNamespace(
            model=models.get((ref.get('creator_id'), ref.get('model_id')), {}),
            offering=offering, connection=connections[deployment['connection_id']],
            deployment=deployment)
        profile = candidate_inference_metadata(candidate)['inference_profile']
        support, _, choices = _choices(profile)
        states[support] += 1
        for depth in sorted(choices):
            mode = depth if depth in {'default', 'off', 'lowest', 'enabled'} else 'preset'
            try:
                body = {'_reasoning_intent': {'version': 1, 'mode': mode,
                                              'value': depth, 'strict': True}}
                compile_reasoning(body, 'opaque-audit-wire', inference_profile=profile)
                checked += 1
            except ValueError as error:
                failures.append({'deployment_id': deployment['deployment_id'],
                                 'depth': depth, 'kind': type(error).__name__})
    controls = [r for r in reasoning_controls(document) if not r['provider_id']]
    coverage = Counter()
    attention = []
    for row in controls:
        coverage[row['support']] += 1
        if row['availability'] == 'no_route' or row['support'] == 'unknown':
            attention.append({key: row[key] for key in ('creator_id', 'model_id', 'support', 'availability')})
    return {'revision': document['revision'], 'models': len(models),
            'offerings': len(offerings), 'deployments': len(document['deployments']),
            'metadata': dict(fields), 'reasoning': dict(states),
            'compiled_choices': checked, 'failures': failures,
            'composer_coverage': dict(coverage), 'requires_attention': attention,
            'migration_plan': changes,
            'migration_receipt': 'not included in public export; startup also consults owner cutover receipt',
            'verification': 'offline declared contracts; no live inference or context-capacity probe'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authority', required=True, type=Path)
    parser.add_argument('--plan-metadata-migration', action='store_true')
    args = parser.parse_args()
    with args.authority.open('rb') as source:
        raw = source.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        parser.error('authority export exceeds the 16 MiB audit budget')
    payload = json.loads(raw)
    report = audit_configuration(payload.get('model_routing', payload),
                                 plan_migration=args.plan_metadata_migration)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report['failures'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
