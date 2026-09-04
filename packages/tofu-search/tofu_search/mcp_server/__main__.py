"""Command-line entry point for the tofu-search MCP server.

Reachable two ways, both running this ``main()``:

    tofu-search-mcp              # console script from [project.scripts]
    python -m tofu_search.mcp_server
"""

from __future__ import annotations

import argparse
import os
import sys

from tofu_search.config import configure
from tofu_search.log import get_logger

logger = get_logger(__name__)

#: Env vars read once at start-up. Configuration is applied to the process
#: ONCE here -- never per request. configure() mutates global state, so a
#: per-request call would let one client change another client's search
#: behaviour. Per-request variation belongs in tool arguments.
_ENV_CONFIG_MAP = {
    'TOFU_SEARCH_LLM_API_KEY': 'llm_api_key',
    'TOFU_SEARCH_LLM_BASE_URL': 'llm_base_url',
    'TOFU_SEARCH_LLM_MODEL': 'llm_model',
    'TOFU_SEARCH_DEADLINE_SECS': 'search_deadline_secs',
    'TOFU_SEARCH_FETCH_TOP_N': 'fetch_top_n',
    'TOFU_SEARCH_PROXY': 'proxy_url',
}

_INT_FIELDS = {'search_deadline_secs', 'fetch_top_n'}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='tofu-search-mcp',
        description='Serve tofu-search over the Model Context Protocol.',
    )
    parser.add_argument(
        '--transport', choices=('stdio', 'http'), default='stdio',
        help='stdio (default) for a local plugin; http for Streamable HTTP. '
             'The deprecated SSE transport is deliberately not offered.',
    )
    parser.add_argument('--host', default='127.0.0.1',
                        help='Bind address for --transport http (default: 127.0.0.1).')
    parser.add_argument('--port', type=int, default=8000,
                        help='Port for --transport http (default: 8000).')
    parser.add_argument(
        '--max-concurrency', type=int, default=None,
        help='Concurrent blocking pipeline calls (default 4). Each one fans '
             'out into ~22 threads inside the library, so raise it with care.',
    )
    parser.add_argument('--workers', type=int, default=1,
                        help=argparse.SUPPRESS)
    return parser


def _apply_env_config() -> None:
    """Apply TOFU_SEARCH_* environment variables to the global config, once."""
    overrides: dict[str, object] = {}
    for env_name, field in _ENV_CONFIG_MAP.items():
        raw = os.environ.get(env_name, '').strip()
        if not raw:
            continue
        if field in _INT_FIELDS:
            try:
                overrides[field] = int(raw)
            except ValueError:
                logger.warning('[MCP] %s=%r is not an integer, ignoring', env_name, raw)
                continue
        else:
            overrides[field] = raw
    if overrides:
        configure(**overrides)
        logger.info('[MCP] applied start-up config from env: %s',
                    ', '.join(sorted(overrides)))


def _reject_multiprocess(workers: int) -> None:
    """Refuse to start more than one worker process.

    ★ This is a correctness guard, not a performance preference.

    The per-engine request throttle, the engine and per-domain circuit
    breakers, the fetch cache and the Playwright pool are all module-level
    singletons created at import time. They are thread-safe but NOT shared
    across processes. With N workers each process independently believes it is
    honouring ``min_request_interval_ms``, so the real request rate to every
    search engine is multiplied by N -- which is precisely how this project
    earned a batch of empty ``202 (rate-limited)`` responses from DuckDuckGo
    once already (fixed in 0.5.1 by adding the throttle those workers would
    now defeat).

    Concurrency comes from the in-process thread pools instead; see
    --max-concurrency. Genuine horizontal scaling requires externalising the
    throttle state first, which is not implemented.
    """
    if workers > 1:
        raise SystemExit(
            f'tofu-search-mcp refuses to start with --workers={workers}.\n'
            '\n'
            'Its rate-limit throttle and circuit-breaker state are per-process '
            'singletons. Running multiple workers multiplies this server\'s '
            'request rate to every search engine while each worker still '
            'believes it is respecting the configured interval -- the result is '
            'engine-side rate-limiting that empties whole result batches.\n'
            '\n'
            'Scale with --max-concurrency (in-process threads) instead.'
        )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    _reject_multiprocess(args.workers)

    if args.max_concurrency is not None:
        if args.max_concurrency < 1:
            raise SystemExit('--max-concurrency must be at least 1')
        os.environ['TOFU_SEARCH_MCP_MAX_CONCURRENCY'] = str(args.max_concurrency)

    _apply_env_config()

    try:
        from tofu_search.mcp_server.server import build_server
    except ImportError as e:
        raise SystemExit(
            f'The MCP server dependencies are not installed ({e}).\n'
            'Install them with:  pip install "tofu-search[mcp]"'
        ) from e

    server = build_server()

    if args.transport == 'http':
        logger.info('[MCP] serving Streamable HTTP on %s:%d', args.host, args.port)
        # MCP SDK v2 owns transport configuration at run time.  The same
        # endpoint accepts both modern stateless requests and legacy sessions.
        server.run(
            transport='streamable-http',
            host=args.host,
            port=args.port,
        )
    else:
        # stdout belongs to the JSON-RPC stream from here on; all diagnostics
        # go to stderr (tofu_search.log never writes to stdout).
        logger.info('[MCP] serving over stdio')
        server.run(transport='stdio')
    return 0


if __name__ == '__main__':
    sys.exit(main())
