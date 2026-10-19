"""Working-fact evidence adapter for the shared owner review authority.

Reads one owned graph for the selected bindings. Stored text is the admitted
snapshot, while live dependency status comes only from the working-fact owner.
No acceptance, retry, or task execution is performed here.
"""
from lib.memory.working_facts.dependencies import assess_dependency


def current_facts(conversation_id, principal, bindings):
    if not bindings:
        return {}
    from lib.memory.working_facts.service import query_dependency_facts
    view = query_dependency_facts(conversation_id, principal=principal,
                                  ids=[item['id'] for item in bindings])
    return {item['id']: item for item in view['facts']}


def project_dependencies(bindings, facts):
    projected = []
    for binding in bindings:
        fact = facts.get(binding['id'])
        status, reasons = assess_dependency(fact, binding['revision'], binding['revision'])
        projected.append({'id': binding['id'], 'boundRevision': binding['revision'],
            'currentRevision': fact['revision'] if fact else None, 'status': status, 'reasons': reasons})
    return projected


def capture_bindings(conversation_id, principal, bindings):
    from .review import ReviewConflict
    if len({item['id'] for item in bindings}) != len(bindings):
        raise ValueError('Review evidence requires unique fact IDs')
    facts = current_facts(conversation_id, principal, bindings)
    if any(row['status'] != 'current' for row in project_dependencies(bindings, facts)):
        raise ReviewConflict('Supporting facts changed or need review; reload before deciding')
    return [{**item, 'text': facts[item['id']]['text'], 'statusAtReview': facts[item['id']]['status']}
            for item in bindings]
