"""Command-line entry point for the tofu-search MCP server.

Reachable two ways, both running this ``main()``:

    tofu-search-mcp              # console script from [project.scripts]
    python -m tofu_search.mcp_server
"""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import ExitStack
from dataclasses import replace

from tofu_search.config import bind_call_config, configure, get_config
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
        help='Legacy idle worker reuse hint; active calls start on demand.',
    )
    parser.add_argument('--workers', type=int, default=1,
                        help=argparse.SUPPRESS)
    parser.add_argument('--local-reranker-model-dir',
                        help='Enable optional offline CPU reranking with an already downloaded, '
                             'admitted Qwen3-Reranker-0.6B snapshot. No automatic download.')
    parser.add_argument('--local-reranker-python',
                        help='Python interpreter with the local-rerank extra installed '
                             '(defaults to this interpreter; requires --local-reranker-model-dir).')
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
            'Independent calls already scale through in-process demand threads.'
        )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.local_reranker_python and not args.local_reranker_model_dir:
        parser.error('--local-reranker-python requires --local-reranker-model-dir')

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

    from tofu_search.mcp_server._bridge import shutdown_bridge
    with ExitStack() as owners:
        # LIFO: stop/join model calls first, then join bridge carriers even if
        # model cleanup raises. Call contexts borrow this explicit owner.
        owners.callback(shutdown_bridge)
        if args.local_reranker_model_dir:
            from tofu_search.local_rerank import LocalReranker
            ranker = LocalReranker(args.local_reranker_model_dir,
                                   python_executable=args.local_reranker_python)
            owners.callback(ranker.close)
            owners.enter_context(bind_call_config(replace(get_config(), local_reranker=ranker)))
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
