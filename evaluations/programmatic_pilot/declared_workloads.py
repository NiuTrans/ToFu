"""Frozen declared-return pagination/fanout diagnostics; synthetic leaf I/O only.

Both arms receive identical producer schemas and source data. Instructions name
the required result, never a program/helper/lane. Production runtime owns control
flow. Complete source coverage includes every dataset's terminal cursor.
"""
import hashlib
import json

from .workloads import Workload, canonical

DECLARED_FAMILIES = frozenset({'declared_pagination', 'declared_composition'})


def declared_workloads() -> tuple[Workload, ...]:
    paged = {'batch-r4': [{'amount': 17 * (i + 1) + 3,
             'note': 'Auxiliary record context.'} for i in range(197)]}
    fanout = {f'batch-k{j + 3}': [{'amount': (j + 1) * (i + 2),
              'note': 'Auxiliary record context.'} for i in range(45)] for j in range(3)}
    tasks = []
    for task_id, family, data, size in [
            ('declared-pagination', 'declared_pagination', paged, 32),
            ('declared-fanout', 'declared_composition', fanout, 16)]:
        world = {'manifest': {'datasets': list(data), 'pageSize': size}, 'datasets': data}
        expected = {'count': sum(len(rows) for rows in data.values()),
                    'total': sum(row['amount'] for rows in data.values() for row in rows)}
        tasks.append(Workload(task_id, family,
            'For the current batch, return only {"count":number,"total":number}. '
            'Count is the number of records across every dataset in the current batch; '
            'total is the sum of their amount fields. Include all pages.',
            canonical(world), canonical(expected)))
    return tuple(tasks)


def declared_tool_schemas() -> list[dict]:
    def tool(name, description, parameters, result):
        return {'type': 'function', 'function': {'name': 'custom__' + name,
                'description': description, 'parameters': parameters},
                'decoded_result_schema': result, 'execution': {'mode': 'client', 'timeout_s': 30},
                'write': False}
    manifest = {'type': 'object', 'properties': {
        'datasets': {'type': 'array', 'items': {'type': 'string'}},
        'pageSize': {'type': 'integer', 'minimum': 1, 'maximum': 64}},
        'required': ['datasets', 'pageSize'], 'additionalProperties': False}
    row = {'type': 'object', 'properties': {'amount': {'type': 'integer'},
           'note': {'type': 'string'}}, 'required': ['amount', 'note'], 'additionalProperties': False}
    page = {'type': 'object', 'properties': {
        'records': {'type': 'array', 'items': row},
        'nextOffset': {'type': ['integer', 'null'], 'minimum': 0}},
        'required': ['records', 'nextOffset'], 'additionalProperties': False}
    return [tool('current_batch', 'Return the current batch manifest as JSON.',
            {'type': 'object', 'properties': {}, 'additionalProperties': False}, manifest),
            tool('read_batch_page', 'Return one JSON page of the named dataset. Start at offset 0; '
                 'nextOffset is the next page position or null when this dataset ends. '
                 'The manifest pageSize is the server page bound.',
                 {'type': 'object', 'properties': {'dataset': {'type': 'string'},
                  'offset': {'type': 'integer', 'minimum': 0, 'default': 0}},
                  'required': ['dataset'], 'additionalProperties': False}, page)]


def read_declared_world(workload: Workload, name: str, args: dict) -> str:
    world = json.loads(workload.world_json)
    if name == 'custom__current_batch' and args == {}:
        return canonical(world['manifest'])
    if name == 'custom__read_batch_page' and set(args) <= {'dataset', 'offset'}:
        dataset, offset = args.get('dataset'), args.get('offset', 0)
        if (isinstance(dataset, str) and dataset in world['datasets']
                and type(offset) is int and offset >= 0):
            rows = world['datasets'][dataset]
            records = rows[offset:offset + world['manifest']['pageSize']]
            end = offset + len(records)
            return canonical({'records': records, 'nextOffset': end if end < len(rows) else None})
    raise ValueError('Unknown tool, arguments, or dataset')


def declared_reads_complete(workload: Workload, calls: list[dict]) -> bool:
    """Require accepted real manifest/pages, all row indices, and terminal cursors."""
    world = json.loads(workload.world_json)
    observed_manifest, terminal = False, set()
    coverage = {name: set() for name in world['datasets']}
    for call in calls:
        if call.get('isError') or call.get('accepted') is not True:
            continue
        try:
            output = read_declared_world(workload, call['name'], call['arguments'])
        except (ValueError, KeyError, TypeError):
            continue
        # Bind the counted rows to the leaf payload actually delivered.
        if call.get('outputSha256') != hashlib.sha256(output.encode()).hexdigest():
            continue
        if call['name'] == 'custom__current_batch':
            observed_manifest = True
        else:
            page = json.loads(output)
            dataset = call['arguments']['dataset']
            offset = call['arguments'].get('offset', 0)
            coverage[dataset].update(range(offset, offset + len(page['records'])))
            if page['nextOffset'] is None:
                terminal.add(dataset)
    return observed_manifest and all(name in terminal and coverage[name] == set(range(len(rows)))
        for name, rows in world['datasets'].items())
