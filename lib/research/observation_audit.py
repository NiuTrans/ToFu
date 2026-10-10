"""Deterministic consistency checks over reported scientific observations.

Entry point: verify_observations. These checks recompute statistics, seed
coverage and partition disjointness; they do not establish benchmark integrity
or independently reproduce an experiment. No storage, model or tool dispatch.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from copy import deepcopy
from functools import lru_cache

AUDIT_VERSION = 7
FORMAT = 'tofu.scientific-observations/v1'
IDENTIFIED_FORMAT = 'tofu.scientific-observations/v2'
CHECKS = ('baseline_fairness', 'uncertainty', 'seeds', 'validation_split')


def normalize_observation_format(value):
    if value != IDENTIFIED_FORMAT:
        raise ValueError('required observation format must be identified paired v2')
    return value


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def normalize_decision_rule(rule):
    """Validate an explicit owner-frozen rule; never guess metric direction."""
    if (not isinstance(rule, Mapping)
            or set(rule) != {'direction', 'minimum_effect', 'uncertainty_multiplier'}
            or rule.get('direction') not in ('higher', 'lower')
            or not _number(rule.get('minimum_effect'))
            or not _number(rule.get('uncertainty_multiplier'))
            or rule['uncertainty_multiplier'] <= 0):
        raise ValueError('Invalid scientific decision rule')
    return {'direction': rule['direction'],
            'minimum_effect': float(rule['minimum_effect']) if rule['minimum_effect'] else 0.0,
            'uncertainty_multiplier': float(rule['uncertainty_multiplier'])}


def normalize_measurement_scope(scope):
    """Freeze exact measurement identities and budget, without inventing evidence."""
    keys={'dataset_digest','split_digest','budget_unit','budget'}
    generations={'candidate_digest','baseline_digest'}
    if (not isinstance(scope, Mapping) or set(scope) not in (keys,keys|generations)
            or any(not isinstance(scope.get(key),str) or len(scope[key])!=64
                   or any(char not in '0123456789abcdef' for char in scope[key])
                   for key in ('dataset_digest','split_digest','candidate_digest','baseline_digest') if key in scope)
            or not isinstance(scope.get('budget_unit'),str) or not scope['budget_unit'].strip()
            or len(scope['budget_unit'])>2000
            or not _number(scope.get('budget')) or scope['budget']<=0):
        raise ValueError('Invalid frozen scientific measurement scope')
    return dict(scope)


def scientific_inference_instruction(protocol):
    """Explain the exact statistical scope shared by executor and reviewer."""
    if not isinstance(protocol, Mapping) or 'decision_rule' not in protocol:
        return ''
    return ('The frozen decision rule compares a direction-adjusted paired mean '
            'and a chosen multiple of its sample standard error against minimum_effect. '
            'Supported/refuted refer to that rule. This SE margin alone does not establish '
            'a confidence level or significance test. Claims about confidence levels '
            'or statistical significance require separately inspected calculations, '
            'their method and assumptions; missing evidence remains unknown.\n')


def measured_outcome(mean, standard_error, rule):
    """Apply a frozen SE margin, not a confidence interval/significance test."""
    try:
        rule = normalize_decision_rule(rule)
        if not _number(mean) or not _number(standard_error) or standard_error < 0:
            return 'inconclusive'
        effect = mean if rule['direction'] == 'higher' else -mean
        margin = rule['uncertainty_multiplier'] * standard_error
        lower, upper = effect - margin, effect + margin
        if not all(_number(value) for value in (margin, lower, upper)):
            return 'inconclusive'
        if lower > rule['minimum_effect']:
            return 'supported'
        if upper < rule['minimum_effect']:
            return 'refuted'
    except (ValueError, OverflowError):
        pass
    return 'inconclusive'


def partition_digest(partitions):
    return hashlib.sha256(json.dumps(partitions, sort_keys=True,
        ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def _partition_valid(partitions):
    if not isinstance(partitions, Mapping):
        return False
    size = partitions.get('dataset_size')
    if type(size) is not int or size < 3:
        return False
    intervals = []
    for name in ('train', 'validation', 'test'):
        ranges = partitions.get(name)
        if not isinstance(ranges, list) or not ranges:
            return False
        for pair in ranges:
            if (not isinstance(pair, list) or len(pair) != 2
                    or any(type(value) is not int for value in pair)
                    or not 0 <= pair[0] < pair[1] <= size):
                return False
            intervals.append(tuple(pair))
    cursor = 0
    for start, end in sorted(intervals):
        if start != cursor:
            return False
        cursor = end
    return cursor == size


def paired_statistics(method_values, baseline_values):
    """Compute aligned paired mean/SE; caller retains seed identity authority."""
    limits = _observation_schema()['properties']['samples']['properties']['seeds']
    if (not isinstance(method_values, list) or not isinstance(baseline_values, list)
            or len(method_values) != len(baseline_values)
            or not limits['minItems'] <= len(method_values) <= limits['maxItems']
            or not all(_number(value) for value in method_values + baseline_values)):
        raise ValueError('paired statistics require equally sized finite sample lists')
    try:
        differences = [left - right for left, right in zip(method_values, baseline_values)]
        if not all(_number(value) for value in differences):
            raise ValueError('paired differences must be finite')
        try:
            mean = math.fsum(differences) / len(differences)
        except OverflowError:
            # The mean can be representable even when the intermediate sum is not.
            mean = math.fsum(value / len(differences) for value in differences)
        scale = math.sqrt(len(differences) * (len(differences) - 1))
        standard_error = math.hypot(*(
            (value - mean) / scale if _number(value - mean)
            else value / scale - mean / scale for value in differences))
    except (OverflowError, ValueError) as exc:
        raise ValueError('paired statistics are not finite') from exc
    if not _number(mean) or not _number(standard_error):
        raise ValueError('paired statistics are not finite')
    return mean, standard_error


def verify_observations(payload, protocol):
    """Never promote a claimed ``verified`` flag without computable evidence."""
    checks = dict.fromkeys(CHECKS, False)
    if not isinstance(payload, Mapping) or not isinstance(protocol, Mapping):
        return checks
    evidence = payload.get('scientific_observations')
    if not isinstance(evidence, Mapping) or evidence.get('format') not in (FORMAT, IDENTIFIED_FORMAT):
        return checks
    if protocol.get('observation_format') is not None and evidence['format'] != protocol['observation_format']:
        return checks
    partitions = evidence.get('partitions')
    partition_valid = _partition_valid(partitions)
    comparison = evidence.get('comparison')
    comparison = comparison if isinstance(comparison, Mapping) else {}
    method = comparison.get('method')
    baseline = comparison.get('baseline')
    contexts = (method, baseline)
    context_valid = all(isinstance(value, Mapping) for value in contexts)
    if context_valid:
        context_valid = bool(protocol.get('dataset') and protocol.get('primary_metric') and protocol.get('baseline')) and all(
            value.get('dataset') == protocol.get('dataset')
            and value.get('metric') == protocol.get('primary_metric')
            and isinstance(value.get('dataset_digest'), str)
            and len(value['dataset_digest']) == 64
            and all(char in '0123456789abcdef' for char in value['dataset_digest'])
            and value.get('evaluation_partition') == 'test' for value in contexts)
    if context_valid:
        context_valid = all('system_digest' not in value or (
            isinstance(value['system_digest'],str) and len(value['system_digest'])==64
            and all(char in '0123456789abcdef' for char in value['system_digest'])) for value in contexts)
    if context_valid:
        context_valid = (baseline.get('system') == protocol['baseline']
                         and all(isinstance(value.get('system'),str) and bool(value['system'].strip())
                                 for value in contexts))
    if context_valid:
        checks['validation_split'] = partition_valid and all(
            value.get('split_digest') == partition_digest(partitions) for value in contexts)
        unit = method.get('budget_unit')
        checks['baseline_fairness'] = bool(isinstance(unit,str) and unit.strip()) and (
            method['dataset_digest'] == baseline['dataset_digest']
            and method.get('split_digest') == baseline.get('split_digest')
            and checks['validation_split']
            and baseline.get('budget_unit') == unit
            and all(_number(value.get('budget')) and value['budget'] > 0 for value in contexts)
            and method['budget'] == baseline['budget'])
    if 'measurement_scope' in protocol:
        try:
            scope=normalize_measurement_scope(protocol['measurement_scope'])
            checks['validation_split'] &= context_valid and all(
                value.get('dataset_digest')==scope['dataset_digest']
                and value.get('split_digest')==scope['split_digest'] for value in contexts)
            checks['baseline_fairness'] &= checks['validation_split'] and all(
                value.get(key)==scope[key] for value in contexts for key in ('budget_unit','budget'))
            if 'candidate_digest' in scope:
                checks['baseline_fairness'] &= (context_valid and method.get('system_digest')==scope['candidate_digest']
                    and baseline.get('system_digest')==scope['baseline_digest'])
        except ValueError:
            checks['validation_split']=checks['baseline_fairness']=False
    samples = evidence.get('samples')
    if not isinstance(samples, Mapping):
        return checks
    seeds = samples.get('seeds')
    planned = protocol.get('random_seeds')
    seed_contract = _observation_schema()['properties']['samples']['properties']['seeds']
    if (not isinstance(seeds, list) or not isinstance(planned, list)
            or not seed_contract['minItems'] <= len(seeds) <= seed_contract['maxItems']
            or not seed_contract['minItems'] <= len(planned) <= seed_contract['maxItems']
            or any(type(value) is not int for value in seeds + planned)):
        return checks
    checks['seeds'] = (len(set(seeds)) == len(seeds) and len(set(planned)) == len(planned)
                      and set(seeds) == set(planned))
    method_values, baseline_values = samples.get('method_values'), samples.get('baseline_values')
    if (not isinstance(method_values, list) or not isinstance(baseline_values, list)
            or len(method_values) != len(seeds) or len(baseline_values) != len(seeds)
            or not all(_number(value) for value in method_values + baseline_values)):
        return checks
    if evidence['format'] == IDENTIFIED_FORMAT:
        aligned = []
        for field, values in (('method_seeds', method_values), ('baseline_seeds', baseline_values)):
            identities = samples.get(field)
            if (not isinstance(identities, list) or len(identities) != len(values)
                    or any(type(value) is not int for value in identities)
                    or len(set(identities)) != len(identities) or set(identities) != set(seeds)):
                checks['seeds'] = False
                return checks
            by_seed = dict(zip(identities, values))
            aligned.append([by_seed[seed] for seed in seeds])
        method_values, baseline_values = aligned
    elif any(field in samples for field in ('method_seeds', 'baseline_seeds')):
        checks['seeds'] = False
        return checks
    try:
        mean, standard_error = paired_statistics(method_values, baseline_values)
    except ValueError:
        return checks
    checks['uncertainty'] = checks['seeds'] and all(
        _number(reported) and math.isclose(reported, computed, rel_tol=1e-6, abs_tol=0.0)
        for reported, computed in [(samples.get('mean_difference'), mean),
                                   (samples.get('standard_error'), standard_error)])
    if 'decision_rule' in protocol:
        try:
            rule = normalize_decision_rule(protocol['decision_rule'])
            checks['uncertainty'] &= payload.get('scientific_outcome') == measured_outcome(mean, standard_error, rule)
        except ValueError:
            checks['uncertainty'] = False
    return checks


@lru_cache(maxsize=1)
def _observation_schema():
    """One process-lifetime canonical schema; callers receive copies, never this template."""
    from pathlib import Path
    authority = Path(__file__).resolve().parents[2] / 'contracts/research_program_v1.schema.json'
    return json.loads(authority.read_text(encoding='utf-8'))['$defs']['scientificObservations']


def observation_contract(protocol=None):
    """Return the retained machine-readable input contract for agent/tool authors."""
    contract=deepcopy(_observation_schema())
    if isinstance(protocol, Mapping) and 'observation_format' in protocol:
        contract['properties']['format'] = {'const': normalize_observation_format(protocol['observation_format'])}
    scope=protocol.get('measurement_scope') if isinstance(protocol,Mapping) else None
    if isinstance(scope, Mapping):
        for system, generation in (('method', 'candidate_digest'), ('baseline', 'baseline_digest')):
            context = contract['properties']['comparison']['properties'][system]
            for field in ('dataset_digest', 'split_digest', 'budget_unit', 'budget'):
                if field in scope:
                    context['properties'][field]['const'] = deepcopy(scope[field])
            if all(key in scope for key in ('candidate_digest', 'baseline_digest')):
                context['required'].append('system_digest')
                context['properties']['system_digest']['const'] = scope[generation]
    planned = protocol.get('random_seeds') if isinstance(protocol, Mapping) else None
    if isinstance(planned, list):
        samples = contract['properties']['samples']['properties']
        limits = samples['seeds']
        if (not limits['minItems'] <= len(planned) <= limits['maxItems']
                or any(type(seed) is not int for seed in planned)
                or len(set(planned)) != len(planned)):
            if isinstance(protocol, Mapping) and 'decision_rule' in protocol:
                raise ValueError('frozen seeds must satisfy the canonical population contract')
            return contract  # Exploratory drafts have not frozen a scientific protocol.
        for field in ('seeds', 'method_seeds', 'baseline_seeds'):
            samples[field].update(minItems=len(planned), maxItems=len(planned), uniqueItems=True)
            samples[field]['items']['enum'] = deepcopy(planned)
        for field in ('method_values', 'baseline_values'):
            samples[field].update(minItems=len(planned), maxItems=len(planned))
    return contract


def decision_rule_contract():
    """Expose the canonical numeric proposal contract without executing a model."""
    from pathlib import Path
    authority = Path(__file__).resolve().parents[2] / 'contracts/research_program_v1.schema.json'
    return json.loads(authority.read_text(encoding='utf-8'))['$defs']['scientificDecisionRule']


def decode_observation_json(text):
    """Decode unambiguous scientific originals; duplicate keys never select a verdict."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate scientific JSON key')
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=unique_object)
    except RecursionError as exc:
        raise ValueError('scientific JSON exceeds decoder nesting capacity') from exc
