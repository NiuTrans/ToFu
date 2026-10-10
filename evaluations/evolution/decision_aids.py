"""Offline research prototypes for declared evidence edges and prerequisites.

CLI: python -m evaluations.evolution.decision_aids INPUT.json OUTPUT.json.
These pure local calculations have no task, store, model, verdict, or execution
port. Graph edges and event attestations are supplied assumptions, not inferred
or authenticated facts. Production never imports this evaluation module.
"""
from __future__ import annotations

import argparse
from collections import defaultdict, deque
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import time

MAX_NODES = 4096
MAX_EDGES = 16384
MAX_EVENTS = 16384
MAX_BYTES = 8 * 1024 * 1024
SHA = re.compile(r'[0-9a-f]{64}\Z')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def _scope(row, scope):
    if (not isinstance(scope, dict) or set(scope) != {'principal', 'namespace'}
            or not all(isinstance(v, str) and 0 < len(v) <= 256 for v in scope.values())
            or row.get('scope') != scope):
        raise ValueError('foreign or invalid local input scope')


def _id(value):
    if not isinstance(value, str) or not 0 < len(value) <= 256:
        raise ValueError('invalid identifier')
    return value


def _sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError('invalid generation digest')
    return value


def compile_graph(graph, *, scope):
    """Validate the declared DAG once. Missing semantic edges remain undetectable."""
    _scope(graph, scope)
    sources, claims = deepcopy(graph['sources']), deepcopy(graph['claims'])
    if not isinstance(sources, dict) or not isinstance(claims, dict) or not 0 < len(sources) + len(claims) <= MAX_NODES:
        raise ValueError('graph node bound exceeded')
    if set(sources) & set(claims):
        raise ValueError('source and claim identity collision')
    for key, row in sources.items():
        _id(key); _scope(row, scope); _sha(row['generation'])
    reverse = defaultdict(set)
    indegrees = dict.fromkeys(claims, 0)
    edges = 0
    for key, row in claims.items():
        _id(key); _scope(row, scope)
        dependencies = row['dependencies']
        if not isinstance(dependencies, list):
            raise ValueError('dependencies must be explicit')
        seen = set()
        for edge in dependencies:
            edges += 1
            if edges > MAX_EDGES or edge.get('role') not in {'support', 'contradiction', 'context'}:
                raise ValueError('invalid or excessive declared edges')
            dependency = _id(edge['id'])
            if dependency not in sources and dependency not in claims:
                raise ValueError('missing named dependency')
            if dependency in seen:
                raise ValueError('duplicate dependency')
            seen.add(dependency); reverse[dependency].add(key)
            indegrees[key] += dependency in claims
    pending = deque(key for key, count in indegrees.items() if count == 0)
    visited = 0
    while pending:
        key = pending.popleft(); visited += 1
        for child in reverse[key]:
            indegrees[child] -= 1
            if indegrees[child] == 0:
                pending.append(child)
    if visited != len(claims):
        raise ValueError('dependency cycle')
    return {'scope': deepcopy(scope), 'sources': sources, 'claims': claims,
            'reverse': dict(reverse), 'construction_edges': edges, 'fingerprint': digest(graph), 'claims_fingerprint': digest(claims)}


def _changed(index, event):
    _scope(event, index['scope'])
    source = event['source_id']
    if source not in index['sources'] or _sha(event['previous']) != index['sources'][source]['generation']:
        raise ValueError('unknown source or mismatched prior generation')
    _sha(event['current'])
    return source if event['previous'] != event['current'] else None


def _impact(index, affected, inspected):
    return {'suggested_review': sorted(affected), 'edges_inspected': inspected,
            'graph_fingerprint': index['fingerprint'], 'semantic_coverage': 'unverified',
            'verified_unaffected': [], 'execution_authorized': False,
            'historical_claims_sha256': index['claims_fingerprint']}


def indexed_impact(index, event):
    changed = _changed(index, event)
    pending = deque([changed] if changed else [])
    affected, inspected = set(), 0
    while pending:
        for claim in index['reverse'].get(pending.popleft(), ()):
            inspected += 1
            if claim not in affected:
                affected.add(claim); pending.append(claim)
    return _impact(index, affected, inspected)


def full_scan_impact(index, event):
    """Careful equivalent declared-graph comparator, not a production LLM run."""
    changed = _changed(index, event)
    if changed is None:
        return _impact(index, set(), 0)
    known, affected, inspected = {changed}, set(), 0
    while True:
        added = set()
        for key, row in index['claims'].items():
            for edge in row['dependencies']:
                inspected += 1
                if edge['id'] in known:
                    added.add(key)
        added -= affected
        if not added:
            return _impact(index, affected, inspected)
        affected |= added; known |= added


def _validate_events(events, scope):
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValueError('event trace bound exceeded')
    ids, versions = {}, {}
    for event in events:
        _scope(event, scope); _id(event['event_id']); _id(event['key']); _sha(event['generation'])
        if (type(event['version']) is not int or not 1 <= event['version'] <= 2**63-1
                or event['status'] not in {'available', 'revoked', 'unknown'}
                or type(event['complete']) is not bool or type(event['value']) is not bool):
            raise ValueError('invalid prerequisite observation')
        fingerprint = digest(event)
        if event['event_id'] in ids and ids[event['event_id']] != fingerprint:
            raise ValueError('conflicting duplicate event identity')
        ids[event['event_id']] = fingerprint
        payload = digest({k: v for k, v in event.items() if k != 'event_id'})
        key = (event['key'], event['version'])
        if key in versions and versions[key] != payload:
            raise ValueError('conflicting prerequisite version')
        versions[key] = payload


def _readiness(branch, latest, *, scope, now):
    _scope(branch, scope)
    requirements = branch['requirements']
    if (not isinstance(requirements, dict) or not requirements or len(requirements) > MAX_NODES
            or type(branch['cancelled']) is not bool
            or type(now) not in {int, float} or type(branch['expires_at']) not in {int, float}):
        raise ValueError('invalid bounded local branch')
    # Also refuse NaN/Infinity instead of treating an invalid expiry as current.
    digest([now, branch['expires_at']])
    missing = []
    for key, generation in requirements.items():
        _id(key); _sha(generation)
        observation = latest.get(key)
        if (not observation or observation['status'] != 'available'
                or not observation['complete'] or not observation['value']
                or observation['generation'] != generation):
            missing.append(key)
    return {'ready_for_review': not missing and not branch['cancelled'] and now < branch['expires_at'],
            'missing': sorted(missing), 'cancelled': branch['cancelled'],
            'expired': now >= branch['expires_at'], 'execution_authorized': False,
            'event_attestations': 'unverified local input',
            'hypothesis_assessment': deepcopy(branch.get('hypothesis_assessment')),
            'branch_fingerprint': digest(branch)}


def prerequisite_fold(branch, events, *, scope, now):
    _validate_events(events, scope)
    latest = {}
    for event in events:
        if event['key'] not in latest or event['version'] > latest[event['key']]['version']:
            latest[event['key']] = event
    result = _readiness(branch, latest, scope=scope, now=now)
    return result | {'observations_inspected': len(events)}


def prerequisite_full_scan(branch, events, *, scope, now):
    """Same latest-version rule, implemented as a careful complete trace scan."""
    _validate_events(events, scope)
    latest, inspected = {}, 0
    for key in branch['requirements']:
        for event in events:
            inspected += 1
            if event['key'] == key and (key not in latest or event['version'] > latest[key]['version']):
                latest[key] = event
    return _readiness(branch, latest, scope=scope, now=now) | {'observations_inspected': inspected}


def evaluate(payload):
    """One offline matched-input comparison; all setup costs are explicit."""
    scope = payload['scope']
    start = time.perf_counter_ns(); index = compile_graph(payload['graph'], scope=scope)
    construction = time.perf_counter_ns() - start
    results = {'input_sha256': digest(payload), 'graph_construction_ns': construction,
               'construction_edges': index['construction_edges'], 'impact': [], 'prerequisites': []}
    if not isinstance(payload['changes'], list) or len(payload['changes']) > 32:
        raise ValueError('change batch bound exceeded')
    for event in payload['changes']:
        pair = {}
        for name, invoke in [('full_scan', full_scan_impact), ('indexed', indexed_impact)]:
            start = time.perf_counter_ns(); result = invoke(index, event)
            pair[name] = result | {'processing_ns': time.perf_counter_ns() - start}
        results['impact'].append(pair)
    for name, invoke in [('full_scan', prerequisite_full_scan), ('fold', prerequisite_fold)]:
        start = time.perf_counter_ns()
        results['prerequisites'].append({'arm': name, **invoke(payload['branch'], payload['events'], scope=scope, now=payload['now']),
                                         'processing_ns': time.perf_counter_ns()-start})
    results['limits'] = {'mapping_effort': None, 'expert_coverage': None, 'human_time': None,
                         'adoption': None, 'net_value': None, 'novelty': None,
                         'scope': 'Declared graphs and locally supplied events; not full studio or scientific validation'}
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path); parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.input.is_symlink() or not args.input.is_file() or args.input.stat().st_size > MAX_BYTES:
        raise ValueError('input must be a bounded regular JSON file')
    if args.output.resolve() == args.input.resolve():
        raise ValueError('preserve original input')
    payload = json.loads(args.input.read_text()); output = json.dumps(evaluate(payload), ensure_ascii=False, indent=2)
    if len(output.encode()) > MAX_BYTES:
        raise ValueError('result exceeds artifact bound')
    with args.output.open('x') as handle:
        handle.write(output + '\n')


if __name__ == '__main__':
    main()
