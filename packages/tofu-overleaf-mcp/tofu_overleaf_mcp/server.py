"""MCP stdio composition: upstream project tools plus typed artifact export.

create_server is the transport-neutral entry point for authenticated deployments.
The caller owns transport authentication and project authorization. Artifact tool
metadata describes a capability; it grants no authority to execute it.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, PaginatedRequestParams, ListToolsResult, TextContent, Tool, ToolAnnotations

from . import __version__
from .contract import ExportError, definition, schema
from .export import ExportStore
from .transport import OverleafTransport

logger = logging.getLogger(__name__)


def export_tools() -> list[Tool]:
    return [Tool(name=row['name'], description=row['description'],
                 inputSchema=definition(row['input']), outputSchema=definition(row['output']),
                 annotations=ToolAnnotations(readOnlyHint=role == 'read',
                                             destructiveHint=False, idempotentHint=role != 'build'),
                 _meta={'tofu/paper-build': {'schema_version': 1, 'role': role,
                         'version_verified': False, 'remote_cancel': False}})
            for role, row in schema()['x-mcp-tools'].items()]


def create_server(store: ExportStore, upstream, *, writes=None, git_operations=None) -> Server:
    from .git_operations import GitOperations, git_tools
    if writes is None and upstream is not None:
        from .writes import upstream_writes
        writes = upstream_writes(upstream)
    if git_operations is None:
        git_operations = getattr(upstream, '_tofu_git_operations', None) or GitOperations()
        if upstream is not None:
            upstream._tofu_git_operations = git_operations
    roles = {row['name']: role for role, row in schema()['x-mcp-tools'].items()}

    async def list_tools(ctx, params):
        legacy = await upstream.handle_list_tools(ctx, params)
        if any(tool.name in roles for tool in legacy.tools):
            raise ExportError('export_tool_collision')
        project_tools = writes.tools(legacy.tools) if writes is not None else legacy.tools
        return ListToolsResult(tools=[*project_tools, *export_tools()])

    async def call_tool(ctx, params):
        role = roles.get(params.name)
        from .writes import WriteError, contract as write_contract
        file_write = writes is not None and params.name in write_contract()['x-mcp-write-tools']
        git_operation = params.name in git_tools()
        if role is None and not git_operation:
            legacy = await upstream.handle_list_tools(ctx, PaginatedRequestParams())
            if not any(tool.name == params.name for tool in legacy.tools):
                return CallToolResult(content=[TextContent(
                    type='text', text='Unknown tool: ' + params.name)], is_error=True)
            return await upstream.handle_call_tool(ctx, params)
        try:
            args = params.arguments or {}
            if git_operation:
                result = await git_operations.invoke(params.name, args)
                if not file_write:
                    return CallToolResult(content=[TextContent(type='text', text=result)])
            elif role == 'build':
                result = await store.build(args)
            elif role == 'read':
                result = store.read(args)
            else:
                result = store.release(args)
            return CallToolResult(content=[TextContent(type='text', text=result['output'] if file_write else 'Paper artifact result')],
                                  structured_content=result)
        except (ExportError, WriteError) as exc:
            code = str(exc)
        except TimeoutError:
            code = 'write_timeout' if file_write else 'git_timeout' if git_operation else 'export_timeout'
        except Exception as exc:
            kind = 'write' if file_write else 'Git read' if git_operation else 'export'
            logger.warning('Overleaf %s failed (%s)', kind, type(exc).__name__)
            code = 'write_unconfirmed' if file_write else 'git_operation_unconfirmed' if git_operation else 'export_unavailable'
        # CancelledError deliberately escapes so SDK cancellation retires HTTP.
        recovery = (' Read remote state before retrying.' if file_write and code in {
            'write_unconfirmed', 'write_timeout', 'git_operation_unconfirmed',
            'git_worker_failed', 'git_memory_limit', 'git_workspace_limit', 'git_response_limit'} else '')
        return CallToolResult(content=[TextContent(type='text', text='Error: ' + code + recovery)], is_error=True)

    @asynccontextmanager
    async def lifespan(_server):
        async with git_operations.session():
            yield {}

    return Server('tofu-overleaf-mcp', version=__version__, lifespan=lifespan,
                  on_list_tools=list_tools, on_call_tool=call_tool)


def main():
    from overleaf_mcp import server as upstream
    from overleaf_mcp.compile import _headers
    from overleaf_mcp.config import OVERLEAF_BASE_URL

    logging.basicConfig(level=logging.INFO)
    # Additional origins are deployment configuration, never compile response data.
    origins = tuple(value.strip() for value in os.environ.get(
        'TOFU_OVERLEAF_OUTPUT_ORIGINS', 'https://compiles.overleaf.com').split(',') if value.strip())

    async def run():
        from .git_operations import GitOperations
        from .git_resources import probe_budget
        budget = probe_budget()
        logger.info('Overleaf Git launch budget: %s', budget.report())
        async with ExportStore(OverleafTransport(OVERLEAF_BASE_URL, _headers,
                                               output_origins=origins)) as store:
            operations = getattr(upstream, '_tofu_git_operations', None) or GitOperations(budget=budget)
            server = create_server(store, upstream, git_operations=operations)
            async with stdio_server() as (reader, writer):
                await server.run(reader, writer, server.create_initialization_options())

    asyncio.run(run())


if __name__ == '__main__':
    main()
