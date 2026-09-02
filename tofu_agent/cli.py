"""Command-line entry point for the standalone Tofu agent runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from tofu_agent.models import AgentConfigurationError, ProviderConfig
from tofu_agent.provider_setup import ProviderSetupService
from tofu_agent.provider_store import ProviderSettingsStore, ProviderStoreError
from tofu_agent.runtime import AgentRuntime
from tofu_agent.server import HeadlessServerConfig, create_app


def _load_dotenv(path: str) -> None:
    try:
        from tofu_dotenv import load_dotenv_file
    except ImportError:
        return
    load_dotenv_file(Path(path))


def _headers(values: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for value in values:
        name, separator, content = value.partition('=')
        if not separator or not name.strip():
            raise AgentConfigurationError(
                f'invalid --provider-header {value!r}; expected NAME=VALUE')
        headers[name.strip()] = content
    return headers


def _provider_from_args(args) -> ProviderConfig | None:
    environment_provider = ProviderConfig.from_env()
    explicit = bool(
        args.provider_base_url or args.provider_api_key
        or args.provider_model or args.provider_header)
    if not explicit:
        return environment_provider
    return ProviderConfig(
        base_url=(args.provider_base_url
                  or (environment_provider.base_url
                      if environment_provider else '')),
        api_key=(args.provider_api_key
                 or (environment_provider.api_key
                     if environment_provider else '')),
        model=(args.provider_model
               or (environment_provider.model if environment_provider else '')
               or args.model),
        extra_headers={
            **(dict(environment_provider.extra_headers)
               if environment_provider else {}),
            **_headers(args.provider_header),
        },
        thinking_format=(environment_provider.thinking_format
                         if environment_provider else ''),
    )


def _has_explicit_provider_arguments(args) -> bool:
    return bool(
        args.provider_base_url or args.provider_api_key
        or args.provider_model or args.provider_header)


def _add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--model', default=os.environ.get(
        'TOFU_AGENT_MODEL', ''), help='Managed/default model id.')
    parser.add_argument('--provider-base-url', default='',
                        help='OpenAI-compatible provider endpoint.')
    parser.add_argument('--provider-api-key', default='',
                        help='Provider key; prefer the environment variable.')
    parser.add_argument('--provider-model', default='',
                        help='Provider model id (defaults to --model).')
    parser.add_argument(
        '--provider-header', action='append', default=[], metavar='NAME=VALUE',
        help='Extra provider header; may be repeated.',
    )
    parser.add_argument('--max-inflight', type=int, default=int(
        os.environ.get('TOFU_AGENT_MAX_INFLIGHT', '4')))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='tofu-agent',
        description=(
            'Embed or serve Tofu Agent without the database or ChatUI '
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
        help='Disable the built-in /setup Provider control panel.',
    )

    doctor_parser = subparsers.add_parser(
        'doctor', help='Validate and print redacted runtime configuration.')
    _add_runtime_arguments(doctor_parser)
    return parser


def _is_loopback_host(host: str) -> bool:
    from tofu_agent.server import _is_loopback
    return _is_loopback(host)


def _runtime_and_setup(
    args,
) -> tuple[AgentRuntime, ProviderSetupService]:
    store = ProviderSettingsStore()
    provider = _provider_from_args(args)
    explicit = _has_explicit_provider_arguments(args)
    if explicit:
        source = 'arguments'
    elif provider is not None:
        source = 'environment'
    else:
        source = 'none'
    load_error = ''
    if provider is None:
        try:
            provider = store.load()
        except ProviderStoreError as exc:
            load_error = str(exc)
        if provider is not None:
            source = 'saved'
    runtime = AgentRuntime.local(
        provider=provider,
        provider_source=source,
        default_model=(args.model or (provider.model if provider else '')),
        max_inflight=args.max_inflight,
    )
    setup = ProviderSetupService(
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


async def _serve(args) -> None:
    if (not _is_loopback_host(args.host) and not args.token
            and not args.allow_unauthenticated):
        raise AgentConfigurationError(
            'refusing a non-loopback bind without authentication; set '
            'TOFU_AGENT_TOKEN or explicitly pass --allow-unauthenticated')
    runtime, provider_setup = _runtime_and_setup(args)
    auth_mode = 'open' if args.allow_unauthenticated else 'auto'
    config = HeadlessServerConfig(
        bind_host=args.host,
        token=args.token,
        auth_mode=auth_mode,
        setup_enabled=args.setup_enabled,
    )
    app = create_app(
        runtime=runtime, config=config, provider_setup=provider_setup)
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
            f'Provider setup: http://{browser_host}:{args.port}/setup',
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
            runtime, provider_setup = _runtime_and_setup(args)
            try:
                print(json.dumps({
                    'ok': True,
                    'ready': bool(runtime.default_model),
                    'model': runtime.default_model,
                    'provider': (runtime.provider.public_dict()
                                 if runtime.provider else None),
                    'capacity': runtime.capacity,
                    'database': False,
                    'frontend': False,
                    'provider_setup_ui': True,
                    'provider_setup': {
                        'url_path': '/setup',
                        'source': provider_setup.source,
                        'editable': provider_setup.editable,
                        'load_error': provider_setup.load_error,
                    },
                }, ensure_ascii=False, indent=2))
            finally:
                runtime.close(abort=False)
            return 0
        asyncio.run(_serve(args))
        return 0
    except (AgentConfigurationError, ValueError) as exc:
        parser.error(str(exc))
    return 2


__all__ = ['main']
