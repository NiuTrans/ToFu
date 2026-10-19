"""Offline coding-tool projection comparison through the production owners.

Build one synthetic Evolution catalog without MCP or personal memory, then vary
only the explicit schema budget at the shared provider boundary. Token counts
are tokenizer estimates, not provider usage or a model-quality evaluation.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from evaluations.evolution.tool_authority import compare_tool_authority


def compare_schema_cost(*, owner_id: int, model: str, budget: int = 4000) -> dict:
    from lib.llm.tool_wire import project_tool_wire
    from lib.tools.gateway import search_executable_catalog, tool_schema_tokens
    from lib.tools.registry import ToolContext, assemble_tool_list
    if type(owner_id) is not int or owner_id <= 0 or type(budget) is not int or not 0 <= budget <= 128000:
        raise ValueError('explicit owner and bounded schema budget required')
    cfg = {'mcpEnabled': False, 'plugins': [], 'memoryEnabled': False,
           'projectEnabled': True, 'evolution': {'goal_id': 'schema-probe',
               'run_id': 'schema-probe', 'stage': 'executing',
               'project_path': '/evaluation/project'}}
    ctx = ToolContext(cfg=cfg, task_id='schema-probe', owner_user_id=owner_id,
        project_path='/evaluation/project', project_enabled=True, search_mode='multi',
        search_enabled=True, fetch_enabled=True, code_exec_enabled=True,
        browser_enabled=False, desktop_enabled=False, enabled_plugins=set())
    surface, _ = assemble_tool_list(ctx)
    original = deepcopy(ctx.executable_tool_catalog)
    bodies, summaries, epochs = [], [], []
    for limit in (0, budget):
        body = {'model': model, 'tools': deepcopy(surface),
                '_tool_wire_catalog': deepcopy(surface),
                '_executable_tool_catalog': deepcopy(original),
                '_required_direct_tool_names': sorted(ctx.direct_wire_tool_names),
                '_tool_discovery_policy_by_name': deepcopy(ctx.discovery_policy_by_name),
                '_tool_search_mode': 'local', '_tool_schema_budget_tokens': limit}
        project_tool_wire(body, api_protocol='openai')
        names = {tool['function']['name'] for tool in body['tools']}
        hidden = {tool['function']['name'] for tool in original} - names
        missing = []
        for name in sorted(hidden):
            discovered = search_executable_catalog(body['_executable_tool_catalog'], name,
                visible_names=names, contract_documents_by_name=ctx.tool_contract_documents_by_name)
            if name not in {tool.get('name') for tool in discovered.get('items', [])}:
                missing.append(name)
        summaries.append({'schema_budget_tokens': limit,
            'estimated_schema_tokens': tool_schema_tokens(body['tools'], model=model),
            'visible_tools': len(names), 'hidden_tools': len(hidden),
            'missing_discovery': missing, 'visible_names': sorted(names)})
        bodies.append(body)
        epochs.append(SimpleNamespace(executable_schemas=body['_executable_tool_catalog'],
            contract_documents_by_name=ctx.tool_contract_documents_by_name,
            degraded_reason='', epoch_hash=''))
    authority = compare_tool_authority(*epochs)
    return {'scope': 'offline synthetic coding catalog; MCP excluded; no provider called',
            'model': model, 'owner_id': owner_id, 'budget_is_soft': True,
            'catalog_config': cfg, 'protocol': 'openai', 'search_backend': 'local',
            'source_sha256': {name: hashlib.sha256((Path(__file__).resolve().parents[2] / name).read_bytes()).hexdigest()
                for name in ('lib/llm/tool_wire.py', 'lib/tools/gateway.py',
                             'lib/tools/registry/_build.py', 'lib/tools/registry/_spec.py')},
            'authority_equal': authority['authority_equal'],
            'catalog_unchanged': all(body['_executable_tool_catalog'] == original for body in bodies),
            'direct_protocols_preserved': all(
                next(tool for tool in bodies[0]['tools'] if tool['function']['name'] == name)
                == next((tool for tool in bodies[1]['tools'] if tool['function']['name'] == name), None)
                for name in ctx.direct_wire_tool_names),
            'authority_sha256': authority['control']['catalog_sha256'],
            'control': summaries[0], 'candidate': summaries[1],
            'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner-id', type=int, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--schema-budget-tokens', type=int, default=4000)
    args = parser.parse_args(argv)
    report = compare_schema_cost(owner_id=args.owner_id, model=args.model,
                                 budget=args.schema_budget_tokens)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if (report['authority_equal'] and report['catalog_unchanged'] and report['direct_protocols_preserved']
                 and not report['candidate']['missing_discovery']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
