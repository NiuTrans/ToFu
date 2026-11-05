"""Prompt admission measures the actual local discovery surface."""
from copy import deepcopy

import pytest

from lib.llm._sse_core import prepare_request
from lib.tasks_pkg.compaction._prompt_admission import enforce_dispatch_prompt_limit
from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
from lib.tasks_pkg.orchestrator._tool_request_metadata import attach_tool_request_metadata
from lib.tools.gateway import tool_schema_tokens

pytestmark = pytest.mark.unit


def test_small_window_admits_local_projection_not_deferred_catalog(monkeypatch):
    monkeypatch.setattr(
        'lib.conversations.project_coordination.ProjectCoordinationRepository.read',
        lambda self, path: {'availability': 'unavailable'})
    config = {'model': 'kimi-k3.1-pro', 'projectPath': '/tmp/small-window',
              'searchMode': 'multi', 'maxTokens': 8192}
    task = {'id': 'small-window', 'convId': 'small-window', '_userId': 7,
            'config': config,
            'messages': [{'role': 'user', 'content': 'Explain this.'}]}
    candidates, has_tools = assemble_round_tools(config, task, {
        'project_path': '', 'project_enabled': False,
        'search_mode': 'multi', 'search_enabled': True, 'fetch_enabled': True,
        'code_exec_enabled': False, 'browser_enabled': False,
        'desktop_enabled': False, 'image_gen_enabled': False,
    })
    assert has_tools
    body = {'model': config['model'], 'messages': deepcopy(task['messages']),
            'tools': deepcopy(candidates)}
    attach_tool_request_metadata(task, body, body['messages'], candidates,
                                 model=config['model'], round_num=0, tid=task['id'])
    plan = prepare_request(body, base_url='https://api.moonshot.cn/v1',
                           api_protocol='openai')
    measurement = enforce_dispatch_prompt_limit(
        task['messages'], plan.body['tools'], task, round_num=0,
        model=config['model'])
    assert task['_lastPromptAdmission']['action'] == 'admit'
    assert len(plan.body['tools']) < len(candidates)
    # Admission measured the local projection, not the deferred catalog.
    assert measurement['toolSchemaTokens'] < tool_schema_tokens(candidates, model=config['model'])
