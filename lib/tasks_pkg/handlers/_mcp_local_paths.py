"""Route explicitly declared server-local MCP files through task workspace policy.

Entry: call_with_local_paths, shared by direct and progressive MCP calls.
Dependencies: MCP catalog/transport, project root admission and writer leases.
Remote/undeclared parameters remain opaque; no parameter-name heuristics run.
"""
from __future__ import annotations

from contextlib import ExitStack
from functools import lru_cache
import json
import os
from pathlib import Path

from lib.mcp.result_content import MCPToolResult
from lib.project_mod.workspace_lease import WorkspaceBusyError


@lru_cache(maxsize=1)
def path_contract():
    return json.loads((Path(__file__).resolve().parents[3] /
                       'contracts/mcp_local_paths_v1.schema.json').read_text())


def _declaration(info):
    contract = path_contract()
    metadata = info.get('meta') or {}
    key = contract['x-metadata-key']
    if key in metadata:
        from jsonschema import Draft202012Validator
        declaration = metadata[key]
        Draft202012Validator(contract).validate(declaration)
    else:
        fields = contract['x-legacy-adapters'].get(info.get('server_name'), {}).get(info.get('tool_name'))
        if fields is None:
            return None
        declaration = {'version': 1, 'filesystem': 'server-local', 'parameters': fields}
    properties = info.get('input_schema', {}).get('properties', {})
    if not properties:
        properties = info.get('openai_def', {}).get('function', {}).get('parameters', {}).get('properties', {})
    for field, policy in declaration['parameters'].items():
        if properties.get(field, {}).get('type') != 'string':
            raise ValueError(f'local path capability {field!r} requires a string schema; refresh MCP discovery')
        if policy.get('create_parents') and policy['access'] != 'write':
            raise ValueError('create_parents is only valid for output files')
    return declaration


def call_with_local_paths(bridge, name, arguments, *, task, invoke):
    """Validate all paths before side effects, preserving typed transport results."""
    info = bridge.get_tool_info(name)
    if not isinstance(info, dict):
        return invoke(arguments)
    invocation_started = False
    try:
        declaration = _declaration(info)
        if declaration is None:
            invocation_started = True
            return invoke(arguments)
        fields = {key: policy for key, policy in declaration['parameters'].items()
                  if key in arguments and arguments[key] is not None}
        if not fields:
            invocation_started = True
            return invoke(arguments)
        servers = bridge.list_servers()
        server = next((row for row in servers if row.get('name') == info['server_name']), {})
        if server.get('transport') != 'stdio':
            raise ValueError('server-local file routing requires a local stdio MCP server; remote paths are not host files')
        if (task.get('config') or {}).get('project_remote'):
            raise ValueError('desktop project files are not server-local MCP files')
        if not task.get('id') or type(task.get('_userId')) is not int or task['_userId'] <= 0:
            raise ValueError('local file routing requires an owned task')
        from lib.tasks_pkg.workspace_delivery import (
            ensure_execution_roots, execution_root_key, route_project_tool_args)
        from lib.project_mod.config import build_root_policy, execution_root_scope
        config = task.get('config') or {}
        policy = ensure_execution_roots(task) or task.get('_workspaceRootPolicy')
        if policy is None:
            if not config.get('projectPath'):
                raise ValueError('local MCP files require an attached workspace')
            policy = build_root_policy(config['projectPath'], config.get('projectPaths'), config.get('readOnlyPaths'))
        key = execution_root_key(task)
        from lib.project_mod.path_resolution import _resolve_base
        from lib.project_mod.tool_admission import Cancellation, admission_timeout, tool_admission_scope
        from lib.project_mod.write_tools._paths import _resolve_write_path
        from lib.project_mod.workspace_lease import workspace_write_lease, workspace_read_lease
        routed = dict(arguments)
        plans = []
        limits = path_contract()['x-limits']
        cancellation = Cancellation(task)
        with execution_root_scope(key, policy), tool_admission_scope(task), ExitStack() as stack:
            for field, field_policy in fields.items():
                value = arguments[field]
                if not isinstance(value, str) or not value.strip() or len(value) > limits['path_chars'] or '\0' in value:
                    raise ValueError(f'{field}: expected a nonempty bounded local file path')
                value = route_project_tool_args(task, name, {'path': value})['path']
                base, relative = _resolve_base(policy['primaryPath'], value, conv_id=key)
                target = Path(os.path.abspath(os.path.expanduser(os.path.join(base, relative))))
                if len(target.parts) > limits['path_components']:
                    raise ValueError(f'{field}: path exceeds component budget')
                matching = [state for state in policy['roots'].values()
                            if target.is_relative_to(state['path'])
                            and target.resolve().is_relative_to(Path(state['path']).resolve())]
                if not matching:
                    raise ValueError(f'{field}: target is outside this task workspace or crosses an escaping symlink')
                owner = max(matching, key=lambda state: len(state['path']))
                if field_policy['access'] == 'write':
                    _resolve_write_path(policy['primaryPath'], str(target), conv_id=key)
                    if target.exists() and not target.is_file():
                        raise ValueError(f'{field}: output must be a file')
                    if not field_policy.get('create_parents') and not target.parent.is_dir():
                        raise ValueError(f'{field}: output parent does not exist; create it explicitly')
                elif not target.is_file():
                    raise ValueError(f'{field}: input file does not exist at {str(target)!r}; use the file write receipt')
                routed[field] = str(target)
                plans.append((target, field_policy, owner['path']))
            leases = {}
            for _, field_policy, root in plans:
                leases[root] = leases.get(root, False) or field_policy['access'] == 'write'
            for root, write in sorted(leases.items()):
                factory = workspace_write_lease if write else workspace_read_lease
                stack.enter_context(factory(root, timeout=admission_timeout(), cancel_event=cancellation))
            if cancellation.is_set():
                raise ValueError('task cancelled before local MCP file preparation')
            for target, field_policy, root in plans:
                if not target.resolve().is_relative_to(Path(root).resolve()):
                    raise ValueError('local path changed root while waiting for admission')
                if field_policy['access'] == 'write':
                    # Check after admission and again after directory preparation.
                    _resolve_write_path(policy['primaryPath'], str(target), conv_id=key)
                    if field_policy.get('create_parents'):
                        target.parent.mkdir(parents=True, exist_ok=True)
                    _resolve_write_path(policy['primaryPath'], str(target), conv_id=key)
                    if not target.resolve().is_relative_to(Path(root).resolve()):
                        raise ValueError('output path changed root during directory preparation')
                elif not target.is_file():
                    raise ValueError('input file disappeared while waiting for admission')
            if cancellation.is_set():
                raise ValueError('task cancelled before MCP invocation')
            invocation_started = True
            result = invoke(routed, local_cancel=cancellation)
            if isinstance(result, str) and not getattr(result, 'is_error', False) and not result.lstrip().startswith(('MCP Error', 'MCP tool error', 'Error', '❌')):
                receipt = '\nResolved local MCP files: ' + json.dumps(
                    {field: routed[field] for field in fields}, ensure_ascii=False)
                if isinstance(result, MCPToolResult):
                    # Keep media and canonical structured JSON intact.
                    decorated = MCPToolResult(str(result) + receipt)
                    decorated.__dict__.update(result.__dict__)
                    result = decorated
                else:
                    result = str(result) + receipt
            return result
    except (ValueError, OSError, WorkspaceBusyError) as error:
        if invocation_started:
            raise
        return MCPToolResult(f'MCP Error: Local path preparation refused: {error}. Command/tool was not started.', is_error=True)
    except Exception as error:
        # Invalid untrusted metadata is a capability refusal, never a fallback.
        from jsonschema import ValidationError
        if isinstance(error, ValidationError):
            return MCPToolResult('MCP Error: Invalid local-path declaration; refresh MCP discovery. Tool was not started.', is_error=True)
        raise
