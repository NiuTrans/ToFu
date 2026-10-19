"""Optional stdio MCP server; bootstrap supplies one trusted actor-bound SDK binding.

Entry points: create_server(binding), main(--factory module:function). No host
imports, implicit credentials, storage selection, login or resource allocation.
"""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager
import importlib
from importlib.metadata import version, PackageNotFoundError
import json
import logging
from .contracts import EnvironmentError
from .mcp_adapter import MCPBinding, EXTERNAL_WRITES


def create_server(binding: MCPBinding):
    from mcp.server import Server
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations
    import anyio

    async def list_tools(_context, _params):
        return ListToolsResult(tools=[Tool(
            name=row['function']['name'], description=row['function']['description'],
            input_schema=row['function']['parameters'],
            annotations=ToolAnnotations(
                read_only_hint=row['function']['name'].endswith(('_catalog', '_status')),
                destructive_hint=row['function']['name'] in EXTERNAL_WRITES,
                idempotent_hint=False, open_world_hint=True),
        ) for row in binding.schemas()])

    async def call_tool(_context, params):
        try:
            # Borrow MCP's worker boundary. Cancellation joins the operation;
            # it must never turn a lost acknowledgement into a replay.
            result = await anyio.to_thread.run_sync(binding.invoke, params.name, params.arguments or {})
            return CallToolResult(content=[TextContent(type='text', text=json.dumps(result, ensure_ascii=False))])
        except EnvironmentError as exc:
            payload = {'ok': False, 'code': exc.code, 'uncertain': exc.uncertain}
        except Exception:
            payload = {'ok': False, 'code': 'operation_failed', 'uncertain': params.name in EXTERNAL_WRITES}
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(payload))], is_error=True)

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield {}
        finally:
            if binding.close is not None:
                await anyio.to_thread.run_sync(binding.close)

    try:
        package_version = version('tofu-environments')
    except PackageNotFoundError:
        package_version = 'development'
    return Server('tofu-environments-mcp', version=package_version, lifespan=lifespan,
                  on_list_tools=list_tools, on_call_tool=call_tool)


def main():
    parser = argparse.ArgumentParser(description='Actor-bound environment/compute MCP')
    parser.add_argument('--factory', required=True, help='Trusted deployment module:function returning MCPBinding')
    args = parser.parse_args()
    module, separator, function = args.factory.partition(':')
    if not separator or not module or not function:
        parser.error('--factory must be module:function')
    binding = getattr(importlib.import_module(module), function)()
    if not isinstance(binding, MCPBinding):
        parser.error('factory must return an actor-bound MCPBinding')
    logging.basicConfig(level=logging.INFO)

    async def run():
        from mcp.server.stdio import stdio_server
        server = create_server(binding)
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    asyncio.run(run())


if __name__ == '__main__':
    main()
