"""Deterministic real comparison-count experiment; no model-authored measurements.

Linear and binary candidates swap against each other on identical held-out
queries and seeds, producing real negative and positive comparison-count results. This is an integration probe, not a claim
about research novelty or competitive system quality.
"""
import hashlib
import json
import math
from pathlib import Path
import random

SEEDS = [11, 23, 37]
PARTITIONS = {'dataset_size': 1000, 'train': [[0, 600]], 'validation': [[600, 800]], 'test': [[800, 1000]]}


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def linear(target):
    for index in range(256):
        if index == target:
            return index, index + 1
    raise AssertionError('query missing')


def binary(target):
    low, high, comparisons = 0, 255, 0
    while low <= high:
        middle = (low + high) // 2
        comparisons += 1
        if middle == target:
            return middle, comparisons
        if middle < target:
            low = middle + 1
        else:
            high = middle - 1
    raise AssertionError('query missing')


def measure(candidate_algorithm="linear"):
    if candidate_algorithm not in ("linear", "binary"):
        raise ValueError("candidate algorithm must be linear or binary")
    candidate_search, control_search = (linear, binary) if candidate_algorithm == "linear" else (binary, linear)
    datasets = {}
    for seed in SEEDS:
        generator = random.Random(seed)
        datasets[str(seed)] = [generator.randrange(256) for _ in range(1000)]
    def evaluate(search, seeds):
        values = []
        for seed in seeds:
            counts = []
            for target in datasets[str(seed)][800:1000]:
                found, count = search(target)
                assert found == target
                counts.append(count)
            values.append(math.fsum(counts) / len(counts))
        return values
    method_seeds = list(SEEDS)
    baseline_seeds = list(reversed(SEEDS))
    method_values = evaluate(candidate_search, method_seeds)
    baseline_values = evaluate(control_search, baseline_seeds)
    baseline_by_seed = dict(zip(baseline_seeds, baseline_values))
    differences = [value - baseline_by_seed[seed] for seed, value in zip(method_seeds, method_values)]
    mean = math.fsum(differences) / len(differences)
    se = math.sqrt(math.fsum((value - mean)**2 for value in differences) / (len(differences) - 1) / len(differences))
    split_digest = hashlib.sha256(encoded(PARTITIONS)).hexdigest()
    source_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    context = {'system_digest': source_digest, 'dataset': 'seeded-search-queries', 'metric': 'comparisons',
        'dataset_digest': hashlib.sha256(encoded(datasets)).hexdigest(), 'split_digest': split_digest,
        'evaluation_partition': 'test', 'budget_unit': 'evaluated_queries', 'budget': 200}
    outcome = 'supported' if -mean - 2 * se > 0 else 'refuted' if -mean + 2 * se < 0 else 'inconclusive'
    return {'scientific_outcome': outcome, 'scientific_observations': {
        'format': 'tofu.scientific-observations/v2', 'partitions': PARTITIONS,
        'comparison': {'method': dict(context, system=candidate_algorithm+'-candidate'),
                       'baseline': dict(context, system=('binary' if candidate_algorithm == 'linear' else 'linear')+'-control')},
        'samples': {'seeds': SEEDS, 'method_seeds': method_seeds, 'baseline_seeds': baseline_seeds, 'method_values': method_values, 'baseline_values': baseline_values,
                    'mean_difference': mean, 'standard_error': se}}}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", choices=("linear", "binary"), default="linear")
    print(json.dumps(measure(parser.parse_args().candidate), sort_keys=True))
