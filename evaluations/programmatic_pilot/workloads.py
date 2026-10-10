"""Frozen diagnostic workloads and read-only world; no model or host I/O.

Entry points: workloads(), tool_schemas(), read_world(), oracle(). The pilot
compares identical task inputs; it never asks a model to adopt a particular
lane. This small synthetic pack is not a population or release benchmark.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class Workload:
    task_id: str
    family: str
    instruction: str
    world_json: str
    expected_json: str

    @property
    def fingerprint(self) -> str:
        return digest(self.__dict__)


def workloads() -> tuple[Workload, ...]:
    rows = [{'id': f'r{i:03}', 'region': 'east' if i % 3 == 0 else 'west',
             'active': i % 5 != 0, 'amount': i * 17 + 3,
             'note': 'Archived supporting material. ' * 12}
            for i in range(240)]
    chosen = [r for r in rows if r['region'] == 'east'
              and r['active'] and r['amount'] >= 3500]
    shards = {f'part-{j}': [{'id': f'{j}-{i}', 'amount': (j + 1) * (i + 2),
                            'note': 'Supporting detail. ' * 8}
                           for i in range(45)] for j in range(3)}
    clauses = [
        {'id': 'alpha', 'fee': 1200, 'clause':
         'Renewal requires written acceptance. Silence is not acceptance.'},
        {'id': 'beta', 'fee': 900, 'clause':
         'Renews automatically unless notice is delivered thirty days '
         'before the anniversary. The anniversary is in ten days.'},
        {'id': 'gamma', 'fee': 3000, 'clause':
         'Already cancelled in writing; service ends on the anniversary.'},
    ]
    definitions = [
        ('bulk-filter', 'data_reduction',
         'Read ledger. Return {"ids":[...],"total":number} for active east '
         'records with amount >= 3500, IDs in original order, total=sum(amount).',
         {'ledger': rows},
         {'ids': [r['id'] for r in chosen],
          'total': sum(r['amount'] for r in chosen)}),
        ('dependent-fanout', 'data_composition',
         'Discover every dataset using list_datasets, then read all of them. '
         'Return {"count":number,"total":number} across all records.',
         shards, {'count': 135, 'total': sum(r['amount']
                    for shard in shards.values() for r in shard)}),
        ('clause-judgment', 'semantic_judgment',
         'Read renewals. Which contract poses the greatest risk of an '
         'unwanted automatic renewal? Interpret the clauses, rather than '
         'ranking fees. Return only {"id":"..."}.',
         {'renewals': clauses}, {'id': 'beta'}),
        ('small-lookup', 'small_result',
         'Read status. Return only {"state":"..."} using its state field.',
         {'status': {'state': 'ready', 'revision': 17}}, {'state': 'ready'}),
    ]
    return tuple(Workload(t, f, i, canonical(w), canonical(e))
                 for t, f, i, w, e in definitions)


def workload_pack(pack: str = 'baseline') -> tuple[Workload, ...]:
    """Select a frozen pack; the original baseline identities stay unchanged."""
    if pack == 'baseline':
        return workloads()
    if pack == 'adaptive':
        from .adaptive_workloads import adaptive_workloads
        controls = tuple(w for w in workloads() if w.family in {'semantic_judgment', 'small_result'})
        return adaptive_workloads() + controls
    if pack == 'declared':
        from .declared_workloads import declared_workloads
        controls = tuple(w for w in workloads() if w.family in {'semantic_judgment', 'small_result'})
        return declared_workloads() + controls
    raise ValueError('Unknown workload pack')


def tool_schemas(workload: Workload | None = None) -> list[dict]:
    if workload is not None:
        from .declared_workloads import DECLARED_FAMILIES, declared_tool_schemas
        if workload.family in DECLARED_FAMILIES:
            return declared_tool_schemas()
    def tool(name, description, properties, required):
        return {'type': 'function', 'function': {
            'name': 'custom__' + name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties,
                           'required': required, 'additionalProperties': False}},
            'execution': {'mode': 'client', 'timeout_s': 30}, 'write': False}
    return [tool('list_datasets', 'List every available dataset name as JSON.',
                 {}, []),
            tool('read_dataset', 'Read a dataset as JSON. For arrays, optional '
                 'offset/limit page the rows; an empty array marks the end.',
                 {'dataset': {'type': 'string'},
                  'offset': {'type': 'integer', 'minimum': 0},
                  'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000}},
                 ['dataset'])]


def read_world(workload: Workload, name: str, arguments: dict) -> str:
    from .declared_workloads import DECLARED_FAMILIES, read_declared_world
    if workload.family in DECLARED_FAMILIES:
        return read_declared_world(workload, name, arguments)
    world = json.loads(workload.world_json)
    if name == 'custom__list_datasets' and arguments == {}:
        return canonical(sorted(world))
    if name == 'custom__read_dataset' and 'dataset' in arguments and set(arguments) <= {'dataset', 'offset', 'limit'}:
        dataset = arguments['dataset']
        if isinstance(dataset, str) and dataset in world:
            data = world[dataset]
            if 'offset' not in arguments and 'limit' not in arguments:
                return canonical(data)
            offset, limit = arguments.get('offset', 0), arguments.get('limit', 1000)
            if isinstance(data, list) and type(offset) is int and offset >= 0 \
                    and type(limit) is int and 1 <= limit <= 1000:
                return canonical(data[offset:offset + limit])
    raise ValueError('Unknown tool, arguments, or dataset')


def oracle(workload: Workload, content: str) -> bool:
    try:
        # JSON booleans are not interchangeable with numeric oracle fields.
        return canonical(json.loads(content)) == workload.expected_json
    except (ValueError, TypeError):
        return False
