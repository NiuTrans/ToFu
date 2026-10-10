"""Offline paired benchmark of current observation-contract loading strategies.

Run with python -m benchmarks.research_observation_contract --output PATH.
Uses the production projector unchanged; swaps only its private schema-loader
seam in this isolated, single-threaded benchmark process and always restores it.
Synthetic protocol identities measure projection cost, not experiment quality.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import time
from pathlib import Path


def measure(*, trials=7, iterations=200):
    if (type(trials) is not int or not 3 <= trials <= 31
            or type(iterations) is not int or not 1 <= iterations <= 2000):
        raise ValueError('trials must be 3..31 and iterations 1..2000')
    from lib.research import observation_audit as audit
    from lib.research.program import normalize_program_fields
    from runtime_guards import probe_system_resources
    resources = probe_system_resources().as_dict()
    root = Path(__file__).resolve().parents[1]
    authority = root / 'contracts/research_program_v1.schema.json'
    cached_loader = audit._observation_schema
    protocol = normalize_program_fields({'protocol': {
        'dataset':'synthetic-pinned-queries', 'baseline':'control', 'primary_metric':'comparisons',
        'random_seeds':[11,23,37], 'observation_format':audit.IDENTIFIED_FORMAT,
        'decision_rule':{'direction':'lower','minimum_effect':0,'uncertainty_multiplier':2},
        'measurement_scope':{'dataset_digest':'d'*64,'split_digest':'e'*64,
                             'budget_unit':'queries','budget':200,
                             'candidate_digest':'a'*64,'baseline_digest':'b'*64},
    }})['protocol']
    def fresh_loader():
        return json.loads(authority.read_text())['$defs']['scientificObservations']
    cached_loader.cache_clear()
    started = time.perf_counter()
    reference = audit.observation_contract(protocol)
    first_projection_seconds = time.perf_counter() - started
    pairs = []
    try:
        for trial in range(trials):
            order = ['fresh','cached'] if trial % 2 == 0 else ['cached','fresh']
            row = {'order':order}
            for arm in order:
                audit._observation_schema = fresh_loader if arm == 'fresh' else cached_loader
                if audit.observation_contract(protocol) != reference:
                    raise RuntimeError('projection changed between benchmark arms')
                started = time.perf_counter()
                for _ in range(iterations):
                    audit.observation_contract(protocol)
                row[arm+'_seconds'] = time.perf_counter() - started
            pairs.append(row)
    finally:
        audit._observation_schema = cached_loader
    fresh = statistics.median(row['fresh_seconds'] for row in pairs)
    cached = statistics.median(row['cached_seconds'] for row in pairs)
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return {'harness_version':3, 'scope':'same current projector; warm filesystem; one isolated process; synthetic protocol',
            'launch_resources':resources, 'python':platform.python_version(), 'platform':platform.platform(),
            'harness_sha256':sha(Path(__file__)), 'source_sha256':sha(Path(audit.__file__)),
            'normalizer_sha256':sha(root/'lib/research/program.py'), 'contract_sha256':sha(authority),
            'trials':trials, 'iterations':iterations, 'protocol':protocol,
            'protocol_sha256':hashlib.sha256(json.dumps(protocol,sort_keys=True).encode()).hexdigest(),
            'first_projection_seconds':first_projection_seconds,
            'first_projection_scope':'empty Python schema cache; imports and filesystem-cache control excluded',
            'equivalent':True, 'pairs':pairs, 'median_fresh_seconds':fresh,
            'median_cached_seconds':cached, 'median_ratio':fresh/cached if cached else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--trials', type=int, default=7)
    parser.add_argument('--iterations', type=int, default=200)
    args = parser.parse_args(argv)
    report = measure(trials=args.trials, iterations=args.iterations)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({key:report[key] for key in ('equivalent','median_fresh_seconds','median_cached_seconds','median_ratio')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
