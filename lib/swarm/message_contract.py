"""Load the sole run-message schema authority; no handwritten wire copies."""
import json
from copy import deepcopy
from pathlib import Path

CONTRACT = json.loads((Path(__file__).resolve().parents[2] /
                       'contracts/swarm_messages_v1.json').read_text())
SEND_AGENT_MESSAGE_TOOL, GET_AGENT_MESSAGES_TOOL = CONTRACT['tools']
LIMITS = CONTRACT['limits']
NAMESPACE = CONTRACT['namespace']


def worker_message_tool(tool: dict) -> dict:
    """Narrow an inherited tool to the contract's same-team worker view.

    Field constraints still come from the inherited schema. A detached copy
    keeps root/peer catalogs immutable; runtime ownership checks remain required.
    """
    function = tool.get('function') or {}
    view = CONTRACT['teamMessaging']['toolViews'].get(function.get('name'))
    if view is None:
        return tool
    scoped = deepcopy(tool)
    function = scoped['function']
    function['description'] = view['description']
    parameters = function['parameters']
    allowed = set(view['fields'])
    if set(parameters.get('required', [])) - allowed:
        raise ValueError('Inherited message contract requires fields unavailable to workers')
    parameters['properties'] = {key: value for key, value in parameters['properties'].items()
                                if key in allowed}
    parameters['required'] = list(dict.fromkeys([
        *parameters.get('required', []),
        *view['required'],
    ]))
    # Root oneOf target forms: workers only ever address exact runs, so drop
    # any branch that requires a field the worker view does not expose. If the
    # inherited contract ever stops offering an exact-run branch the worker
    # tool would become uncallable; fail fast instead of silently keeping a
    # stable-only schema in a worker catalog.
    one_of = parameters.get('oneOf')
    if isinstance(one_of, list):
        branches = [branch for branch in one_of
                    if isinstance(branch, dict)
                    and set(branch.get('required', [])) <= allowed]
        if not branches:
            raise ValueError(
                'Inherited message contract has no worker-compatible target form')
        parameters['oneOf'] = branches
    parameters['additionalProperties'] = False
    return scoped


def worker_message_contracts(documents: dict) -> dict:
    """Keep inherited execution/discovery contracts as narrow as worker wire."""
    scoped = dict(documents)
    for name in CONTRACT['teamMessaging']['toolViews']:
        document = scoped.get(name)
        if not isinstance(document, dict) or document.get('name') != name:
            continue  # Preserve malformed authority for the common validator.
        schema = document.get('arguments_schema')
        if not isinstance(schema, dict):
            continue
        function = worker_message_tool({'function': {
            'name': name, 'parameters': schema,
        }})['function']
        scoped[name] = {**document, 'arguments_schema': function['parameters'],
                        'description': function['description']}
    return scoped
