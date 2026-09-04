"""Command-line entry point for the standalone Tofu agent runtime."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
import json
import os
from pathlib import Path

from tofu_agent.models import (
    AgentConfigurationError,
    AgentTimeoutError,
    ModelRoutingConfig,
)
from tofu_agent.provider_setup import ModelRoutingSetupService
from lib.trajectory import AVAILABLE_FORMATS
from tofu_agent.provider_store import (
    ModelRoutingSettingsStore,
    ModelRoutingStoreError,
)
from tofu_agent.runtime import AgentRuntime
from tofu_agent.server import HeadlessServerConfig, create_app


def _load_dotenv(path: str) -> None:
    try:
        from tofu_dotenv import load_dotenv_file
    except ImportError:
        return
    load_dotenv_file(Path(path))


def _model_routing_from_args(args) -> ModelRoutingConfig | None:
    if args.model_routing_json and args.model_routing_file:
        raise AgentConfigurationError(
            'pass only one of --model-routing-json and --model-routing-file')
    raw = str(args.model_routing_json or '').strip()
    if args.model_routing_file:
        try:
            raw = Path(args.model_routing_file).read_text(encoding='utf-8')
        except OSError as exc:
            raise AgentConfigurationError(
                'model-routing file could not be read') from exc
    if not raw:
        return ModelRoutingConfig.from_env()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AgentConfigurationError(
            'model-routing input must be valid JSON') from exc
    return ModelRoutingConfig.from_mapping(decoded)


def _add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        '--model-routing-json', default='',
        help='Complete v2 access envelope JSON; prefer the environment variable.',
    )
    parser.add_argument(
        '--model-routing-file', default=os.environ.get(
            'TOFU_AGENT_MODEL_ROUTING_FILE', ''),
        help='Read a complete v2 access envelope from this file.',
    )
    parser.add_argument('--max-inflight', type=int, default=int(
        os.environ.get('TOFU_AGENT_MAX_INFLIGHT', '4')))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='tofu-agent',
        description=(
            'Embed or serve Tofu Agent without the database or full Tofu '
            'application frontend.'),
    )
    parser.add_argument('--env-file', default=os.environ.get(
        'TOFU_AGENT_ENV_FILE', '.env'))
    subparsers = parser.add_subparsers(dest='command', required=True)

    serve_parser = subparsers.add_parser('serve', help='Run the headless API.')
    _add_runtime_arguments(serve_parser)
    serve_parser.add_argument('--host', default=os.environ.get(
        'TOFU_AGENT_HOST', '127.0.0.1'))
    serve_parser.add_argument('--port', type=int, default=int(os.environ.get(
        'TOFU_AGENT_PORT', '15001')))
    serve_parser.add_argument('--token', default=os.environ.get(
        'TOFU_AGENT_TOKEN', ''),
        help='Bearer token; prefer TOFU_AGENT_TOKEN to avoid process-list leaks.')
    serve_parser.add_argument(
        '--allow-unauthenticated', action='store_true',
        help='Explicitly allow remote requests without a token (unsafe).',
    )
    serve_parser.add_argument('--log-level', default=os.environ.get(
        'TOFU_AGENT_LOG_LEVEL', 'info'))
    setup_enabled = str(os.environ.get(
        'TOFU_AGENT_SETUP_ENABLED', '1')).strip().lower() \
        not in {'0', 'false', 'no', 'off'}
    serve_parser.add_argument(
        '--no-setup', action='store_false', dest='setup_enabled',
        default=setup_enabled,
        help='Disable the built-in /setup model-routing control panel.',
    )

    doctor_parser = subparsers.add_parser(
        'doctor', help='Validate and print redacted runtime configuration.')
    _add_runtime_arguments(doctor_parser)

    run_parser = subparsers.add_parser(
        'run',
        help='Run one task to completion and print the JSON result.',
        description=(
            'Single-shot agent invocation: one process, one task, one JSON '
            'result. Intended for harness/eval integrations that cannot or '
            'should not manage a long-lived serve process. Exit codes: '
            '0=done, 2=usage, 3=run timeout, 4=permanent error, '
            '5=aborted, 6=retriable upstream error (the error envelope in '
            'the JSON result carries kind/retryable for attribution).'),
    )
    _add_runtime_arguments(run_parser)
    run_parser.add_argument(
        '--task', default=os.environ.get('TOFU_AGENT_RUN_TASK', ''),
        help='Task instruction text.')
    run_parser.add_argument(
        '--task-file', default=os.environ.get('TOFU_AGENT_RUN_TASK_FILE', ''),
        help='Read the task instruction from this file (wins over --task).')
    run_parser.add_argument(
        '--cwd', default=os.environ.get('TOFU_AGENT_RUN_CWD', ''),
        help='Project root exposed to the agent tools (run_command and file '
             'tools resolve against it). Defaults to the process cwd.')
    run_parser.add_argument(
        '--timeout-s', type=float, default=float(os.environ.get(
            'TOFU_AGENT_RUN_TIMEOUT', '600')),
        help='Wall-clock budget for the whole run (default 600s).')
    run_parser.add_argument(
        '--tools', default=os.environ.get('TOFU_AGENT_RUN_TOOLS', ''),
        help="Comma-separated tool tags (see README) or '*'. Default keeps "
             'the storage-free runtime policy.')
    run_parser.add_argument(
        '--trajectory', default=os.environ.get('TOFU_AGENT_RUN_TRAJECTORY', ''),
        choices=['', *AVAILABLE_FORMATS],
        help='Embed a flattened trajectory of this run in the JSON result.')
    run_parser.add_argument(
        '--output', default=os.environ.get('TOFU_AGENT_RUN_OUTPUT', ''),
        help='Write the JSON result to this path instead of stdout.')
    return parser


def _is_loopback_host(host: str) -> bool:
    from tofu_agent.server import _is_loopback
    return _is_loopback(host)


def _runtime_and_setup(
    args,
) -> tuple[AgentRuntime, ModelRoutingSetupService]:
    store = ModelRoutingSettingsStore()
    access = _model_routing_from_args(args)
    explicit = bool(args.model_routing_json or args.model_routing_file)
    if explicit:
        source = 'arguments'
    elif access is not None:
        source = 'environment'
    else:
        source = 'none'
    load_error = ''
    if access is None:
        try:
            access = store.load()
        except ModelRoutingStoreError as exc:
            load_error = str(exc)
        if access is not None:
            source = 'saved'
    runtime = AgentRuntime.local(
        model_routing=access,
        model_routing_source=source,
        max_inflight=args.max_inflight,
    )
    setup = ModelRoutingSetupService(
        runtime,
        store,
        source=source,
        editable=source in {'none', 'saved'},
        load_error=load_error,
    )
    return runtime, setup


def _runtime(args) -> AgentRuntime:
    """Compatibility helper retained for focused CLI tests/importers."""
    runtime, _setup = _runtime_and_setup(args)
    return runtime


def _run_task(args) -> int:
    task_text = ''
    if args.task_file:
        task_text = Path(args.task_file).read_text(encoding='utf-8')
    if not task_text.strip():
        task_text = args.task
    if not task_text.strip():
        raise AgentConfigurationError(
            'no task given; pass --task or --task-file')

    config: dict = {}
    if args.cwd:
        config['project'] = args.cwd
    if args.tools:
        tags = [tag.strip() for tag in args.tools.split(',') if tag.strip()]
        config['tools'] = tags[0] if len(tags) == 1 else tags

    runtime = _runtime(args)
    try:
        result = runtime.run(
            [{'role': 'user', 'content': task_text}],
            config=config,
            trajectory=args.trajectory or None,
            timeout_s=args.timeout_s,
        )
    except AgentTimeoutError:
        runtime.close(abort=True)
        _emit({
            'ok': False,
            'status': 'timeout',
            'error': {'kind': 'timeout',
                      'message': f'run exceeded {args.timeout_s}s'},
        }, args.output)
        return 3
    finally:
        runtime.close(abort=False)

    document = {
        'ok': result.status == 'done',
        'id': result.id,
        'task_id': result.task_id,
        'model': result.model,
        'status': result.status,
        'finish_reason': result.finish_reason,
        'content': result.content,
        'thinking': result.thinking,
        'usage': dict(result.usage),
        'n_tool_rounds': result.n_tool_rounds,
        'error': result.error,
        'provider_id': result.provider_id,
    }
    if result.trajectory_format:
        document['trajectory_format'] = result.trajectory_format
        document['trajectory'] = result.trajectory

    _emit(document, args.output)

    if result.status == 'done':
        return 0
    if result.status == 'aborted':
        return 5
    if isinstance(result.error, Mapping) \
            and result.error.get('retryable') is True:
        # Transient upstream failure (ratelimit / upstream_error / network …)
        # after the in-run retry budgets: the harness may rerun this trial
        # and plausibly succeed, unlike a permanent payload/permission error.
        return 6
    return 4


def _emit(document: dict, output: str) -> None:
    payload = json.dumps(document, ensure_ascii=False, indent=2)
    if output:
        Path(output).write_text(payload + '\n', encoding='utf-8')
    else:
        print(payload)


async def _serve(args) -> None:
    if (not _is_loopback_host(args.host) and not args.token
            and not args.allow_unauthenticated):
        raise AgentConfigurationError(
            'refusing a non-loopback bind without authentication; set '
            'TOFU_AGENT_TOKEN or explicitly pass --allow-unauthenticated')
    runtime, model_routing_setup = _runtime_and_setup(args)
    auth_mode = 'open' if args.allow_unauthenticated else 'auto'
    config = HeadlessServerConfig(
        bind_host=args.host,
        token=args.token,
        auth_mode=auth_mode,
        setup_enabled=args.setup_enabled,
    )
    app = create_app(
        runtime=runtime, config=config,
        model_routing_setup=model_routing_setup)
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    hypercorn = Config()
    hypercorn.bind = [f'{args.host}:{args.port}']
    hypercorn.accesslog = '-'
    hypercorn.errorlog = '-'
    hypercorn.loglevel = args.log_level
    if args.setup_enabled:
        browser_host = args.host if _is_loopback_host(args.host) else '127.0.0.1'
        print(
            f'Model-routing setup: http://{browser_host}:{args.port}/setup',
            flush=True,
        )
    try:
        await serve(app, hypercorn)
    finally:
        await asyncio.to_thread(runtime.close)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    preliminary, _unknown = parser.parse_known_args(argv)
    _load_dotenv(preliminary.env_file)
    # Rebuild after dotenv loading so argparse defaults see the newly loaded
    # environment. Explicit command-line values still win normally.
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == 'doctor':
            runtime, model_routing_setup = _runtime_and_setup(args)
            try:
                print(json.dumps({
                    'ok': True,
                    'ready': bool(runtime.default_model),
                    'model': runtime.default_model,
                    'model_routing': (runtime.model_routing.public_dict()
                                      if runtime.model_routing else None),
                    'capacity': runtime.capacity,
                    'database': False,
                    'frontend': False,
                    'model_routing_setup_ui': True,
                    'model_routing_setup': {
                        'url_path': '/setup',
                        'source': model_routing_setup.source,
                        'editable': model_routing_setup.editable,
                        'load_error': model_routing_setup.load_error,
                    },
                }, ensure_ascii=False, indent=2))
            finally:
                runtime.close(abort=False)
            return 0
        if args.command == 'run':
            return _run_task(args)
        asyncio.run(_serve(args))
        return 0
    except (AgentConfigurationError, ValueError) as exc:
        parser.error(str(exc))
    return 2


__all__ = ['main']
