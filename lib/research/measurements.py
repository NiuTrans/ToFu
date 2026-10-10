"""Construct reported paired observations from measured samples and explicit identities.

Entry: build_paired_observation. Reuses observation_audit's numeric and contract
checks; no model, command, storage or digest discovery. Output is consistency
checked reported data, never proof of execution or independent replication.
"""
from copy import deepcopy
from .observation_audit import (FORMAT, IDENTIFIED_FORMAT, observation_contract,
                                paired_statistics, measured_outcome, verify_observations)


def build_paired_observation(protocol, *, comparison, partitions, method_samples, baseline_samples):
    """Align {seed,value} records to frozen seeds and produce the exact contract.

    Identity/context fields must come from the caller's measured experiment;
    this function neither invents them nor copies expected scope into results.
    It does not add verified audit flags or artifact/source references.
    """
    if not isinstance(protocol, dict):
        raise ValueError('a frozen protocol object is required')
    protocol = deepcopy(protocol)
    seeds = protocol.get('random_seeds')
    if (not isinstance(seeds, list) or any(type(seed) is not int for seed in seeds)
            or len(set(seeds)) != len(seeds)):
        raise ValueError('frozen seeds must be distinct integers')
    aligned = []
    for records in (method_samples, baseline_samples):
        if not isinstance(records, list) or len(records) != len(seeds):
            raise ValueError('every frozen seed requires one measured sample')
        values = {}
        for row in records:
            if (not isinstance(row, dict) or set(row) != {'seed', 'value'}
                    or type(row['seed']) is not int or row['seed'] in values):
                raise ValueError('sample identities must be unique integer seeds')
            values[row['seed']] = row['value']
        if set(values) != set(seeds):
            raise ValueError('sample identities must match the frozen seeds')
        aligned.append([values[seed] for seed in seeds])
    mean, error = paired_statistics(*aligned)
    fmt = protocol.get('observation_format', FORMAT)
    if fmt not in (FORMAT, IDENTIFIED_FORMAT):
        raise ValueError('unsupported observation format')
    samples = {'seeds': deepcopy(seeds), 'method_values': aligned[0], 'baseline_values': aligned[1],
               'mean_difference': mean, 'standard_error': error}
    if fmt == IDENTIFIED_FORMAT:
        samples.update(method_seeds=deepcopy(seeds), baseline_seeds=deepcopy(seeds))
    payload = {'scientific_observations': {'format': fmt, 'comparison': deepcopy(comparison),
               'partitions': deepcopy(partitions), 'samples': samples}}
    if 'decision_rule' in protocol:
        payload['scientific_outcome'] = measured_outcome(mean, error, protocol['decision_rule'])
    import jsonschema
    try:
        jsonschema.validate(payload['scientific_observations'], observation_contract(protocol))
    except jsonschema.ValidationError as exc:
        raise ValueError('measured observation violates its schema') from exc
    if not all(verify_observations(payload, protocol).values()):
        raise ValueError('measured observation does not match its frozen protocol')
    return payload
