"""MCP server surface for tofu-search.

★ SDK COUPLING IS DELIBERATELY CONFINED TO ONE IMPORT.

``from mcp.server import MCPServer`` is the ONLY way this package may reach
into the MCP SDK -- no submodule imports, no reaching for internals.  SDK v2
serves both the stateless 2026-07-28 protocol and legacy handshake clients, so
old ChatUI integrations and current MCP hosts use the same server.  A guard in
tests/test_plugin_contract.py fails the build if a second SDK import appears.

The tool surface is deliberately NARROW. ``tofu_search`` exports 20+ symbols,
but mapping them one-to-one onto tools would hand a model twenty near-identical
choices and degrade its selection accuracy. Three rules decided what became a
tool:

* Registration seams (``register_browser_provider``, ``register_reader``, ...)
  are for a HOST to call at start-up. A model has no implementation to supply.
* ``configure()`` mutates process-wide state. Exposing it would let one client
  change another client's search behaviour -- cross-tenant interference, not
  configuration. Per-call overrides go through the tool arguments instead.
* Functions that are one step of a single user-visible action (``format_results``,
  ``detect_vertical_intent``, ``parse_bibtex``) are folded into the tool that
  performs the whole action. A model never wants "parse, then verify".
"""

from __future__ import annotations

from mcp.server import MCPServer

from tofu_search import __version__
from tofu_search.config import get_config
from tofu_search.fetch.core import fetch_page_content, get_fetch_cache_stats
from tofu_search.log import get_logger
from tofu_search.mcp_server._bridge import get_limiter, run_blocking
from tofu_search.search.format import format_search_for_tool_response
from tofu_search.search.orchestrator import perform_web_search
from tofu_search.search.vertical import (
    describe_domains,
    detect_vertical_intent,
    list_domains,
    search_vertical,
    search_vertical_domain,
)
from tofu_search.verify import summarize, verify_bibtex, verify_references

logger = get_logger(__name__)

SERVER_NAME = 'tofu-search'

_READ_ONLY_OPEN_WORLD = {
    'readOnlyHint': True,
    'destructiveHint': False,
    'idempotentHint': True,
    'openWorldHint': True,
}

SERVER_INSTRUCTIONS = """\
Web research tools: multi-engine search with page content already fetched,
single-page reading, authoritative lookups for structured identifiers, and a
citation checker.

Prefer `web_search` as the entry point -- it already fetches and cleans page
content, and already enriches results when the query contains a structured
identifier. Reach for `fetch_page` to read one specific URL in depth, and for
`search_vertical` only when you want specialist data WITHOUT web results.
"""


def _vertical_description() -> str:
    """Build the search_vertical description from the live domain registry.

    ★ Generated, never hand-written. The registry is the only place that knows
    which domains exist and which of their handlers can actually run right now
    (some need a credential). A hand-maintained description in this file would
    have gone stale the moment a vertical was added -- and one was added while
    this server was being written. Generating it also lets the description tell
    the model the truth about UNAVAILABLE sub-handlers, so it does not spend a
    call on a domain that is guaranteed to come back empty.
    """
    lines = [
        'Look up a structured identifier in its authoritative source, '
        'bypassing web search. Supported domains:',
        '',
    ]
    for meta in describe_domains():
        domain = meta.get('domain', '')
        purpose = (meta.get('purpose') or '').rstrip('.')
        when = meta.get('when_to_use') or ''
        examples = ', '.join(meta.get('examples') or [])

        line = f'- `{domain}` — {purpose}. {when}'
        if examples:
            line += f' Examples: {examples}.'

        unavailable = meta.get('unavailable_types') or []
        if unavailable:
            # Each entry is {'type': ..., 'credential_env': ...} -- the env var
            # is per-handler, not per-domain, so a domain can have one handler
            # gated on a key while its siblings work fine.
            parts = []
            for item in unavailable:
                name = item.get('type', '?')
                env = item.get('credential_env') or meta.get('credential_env') or ''
                parts.append(f'{name} (needs {env})' if env else name)
            line += (f' NOTE: currently UNAVAILABLE here: {", ".join(parts)} '
                     '— do not expect results from those sub-lookups.')
        lines.append(line)

    lines += [
        '',
        'You usually do NOT need this tool: `web_search` auto-detects these '
        'identifiers and folds the same authoritative data into its results. '
        'Use `search_vertical` only when you want the specialist record ALONE, '
        'without web results, or when you want to force a specific domain.',
        '',
        'With `domain` set you may also pass a free-text topic rather than an '
        'identifier (e.g. domain="academic", query="state space models") to get '
        'a domain-scoped lookup.',
    ]
    return '\n'.join(lines)


def build_server() -> MCPServer:
    """Construct the MCP server with its tools and resources registered."""
    mcp = MCPServer(
        SERVER_NAME,
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
    )

    @mcp.tool(
        name='web_search',
        annotations=_READ_ONLY_OPEN_WORLD,
        description=(
            'Search the web across multiple engines (DuckDuckGo, Brave, Bing, '
            'SearXNG, Marginalia) in parallel and return results whose page '
            'content has ALREADY been fetched, cleaned and relevance-filtered '
            '— you do not need a follow-up call to read them.\n\n'
            'A query containing a structured identifier (CVE ID, arXiv ID, '
            'DOI, stock ticker, PyPI/npm package, GitHub repo, IP address) is '
            'automatically enriched with data from the matching authoritative '
            'API, so there is no need to call `search_vertical` as well.\n\n'
            'Recommended strategy: search once with a well-formed query, read '
            'the returned content, then use `fetch_page` on any URL that needs '
            'more depth. One call can take up to ~45 seconds; prefer a single '
            'specific query over several vague ones. Results are returned as '
            'formatted text, with each entry showing title, URL, engine '
            'consensus, an authority label, and query-focused page excerpts. '
            'The default shared excerpt budget is 18,000 Unicode characters '
            'across all sources (the exact token count depends on language and '
            'the host model); increase `content_budget_chars` only when the '
            'question genuinely needs more source detail. '
            '`max_results` is clamped to 1..12.'
        ),
    )
    async def web_search(
        query: str,
        max_results: int = 6,
        user_question: str = '',
        freshness: str = '',
        fetch_pages: bool = True,
        content_budget_chars: int = 0,
    ) -> str:
        """Search the web; see the registered description for model guidance."""
        max_results = max(1, min(int(max_results), 12))
        results = await run_blocking(
            perform_web_search,
            query,
            max_results=max_results,
            user_question=user_question or query,
            freshness=freshness,
            fetch_pages=fetch_pages,
        )
        diag = getattr(results, '_search_diag', None)
        budget = int(content_budget_chars or get_config().mcp_content_budget_chars)
        budget = max(4_000, min(budget, 60_000))
        return format_search_for_tool_response(
            results,
            search_diag=diag,
            query=query,
            max_total_content_chars=budget,
            fetch_tool_name='fetch_page',
        )

    @mcp.tool(
        name='fetch_page',
        annotations=_READ_ONLY_OPEN_WORLD,
        description=(
            'Fetch and read one web page (HTML, PDF or plain text), returning '
            'cleaned article text rather than raw markup.\n\n'
            'Use it when the user gives you a URL, or to read in depth a page '
            '`web_search` surfaced. The fetcher transparently handles '
            'JavaScript-rendered pages and bot-protected sites by falling back '
            'to a real browser, so a page that looks empty to a naive fetcher '
            'usually still comes back with content here.\n\n'
            'Returns the extracted text, truncated at `max_chars`. Returns an '
            'explicit failure note (not an exception) when the page cannot be '
            'read — treat that as "this page is unavailable", not as "this '
            'page is empty".'
        ),
    )
    async def fetch_page(url: str, max_chars: int = 200_000,
                         reason: str = '') -> str:
        """Read a single URL; see the registered description."""
        if reason:
            logger.info('[MCP] fetch_page %s (reason=%s)', url[:100], reason[:80])
        max_chars = max(1_000, min(int(max_chars), 500_000))
        content = await run_blocking(fetch_page_content, url, max_chars=max_chars)
        if not content:
            return (f'Could not read {url} — the fetch failed or the page had no '
                    'extractable text. This says nothing about whether the '
                    'information exists; try web_search, or another URL.')
        return content

    @mcp.tool(name='search_vertical', description=_vertical_description(),
              annotations=_READ_ONLY_OPEN_WORLD)
    async def search_vertical_tool(query: str, domain: str = 'auto') -> str:
        """Authoritative lookup; see the generated description."""
        if domain and domain != 'auto':
            # Explicit domain: fan out across that domain's available types.
            # Accepts free text, not just an identifier.
            result = await run_blocking(search_vertical_domain, domain, query)
        else:
            # ★ Auto: detection MUST come first. search_vertical()'s first
            # argument is a registered TYPE name ('arxiv', 'cve', ...), not a
            # query -- passing the query there looks plausible and silently
            # matches no handler, so every auto lookup would fall through to
            # the "nothing matched" branch below. detect_vertical_intent gives
            # the (type, identifier, params) triple the dispatcher wants.
            intent = detect_vertical_intent(query)
            if intent is None:
                result = None
            else:
                vtype, identifier, params = intent
                result = await run_blocking(search_vertical, vtype, identifier, params)

        if not result:
            known = ', '.join(list_domains())
            return (f'No authoritative source matched {query!r}. This means the '
                    'query carried no recognisable identifier — it does NOT mean '
                    'the subject does not exist. Use web_search instead. '
                    f'Domains available here: {known}.')
        if isinstance(result, dict):
            return result.get('content') or str(result)
        return str(result)

    @mcp.tool(
        name='verify_citations',
        annotations=_READ_ONLY_OPEN_WORLD,
        description=(
            'Check a bibliography for hallucinated or non-existent references '
            'against authoritative catalogues (CrossRef, arXiv, Semantic '
            'Scholar). No LLM calls — every verdict is backed by a lookup.\n\n'
            'Accepts BibTeX, a plain reference list, or prose containing '
            'citations; set `format` to force one, otherwise it is detected.\n\n'
            'Each entry gets one of THREE verdicts, and the distinction matters:\n'
            '- `verified`: an authoritative record matches the claim.\n'
            '- `suspicious`: a concrete DOI/arXiv ID that definitively does NOT '
            'resolve, or resolves to a different paper. This is real evidence '
            'of fabrication.\n'
            '- `unverifiable`: could neither confirm nor refute — no usable '
            'identifier, a catalogue coverage gap, a book/dataset, or a '
            'rate-limit. NEVER report these to the user as fake; the honest '
            'phrasing is "I could not check this one".\n\n'
            'Every verdict carries an `evidence` object with the exact '
            'catalogue URL checked, the matched title and a similarity score. '
            'Quote that evidence when explaining a verdict instead of asserting '
            'it bare.'
        ),
    )
    async def verify_citations_tool(text: str, format: str = 'auto') -> dict:
        """Verify a bibliography; see the registered description."""
        looks_like_bibtex = '@' in text and '{' in text
        use_bibtex = format == 'bibtex' or (format == 'auto' and looks_like_bibtex)

        verifier = verify_bibtex if use_bibtex else verify_references
        results = await run_blocking(verifier, text)
        summary = summarize(results)

        # summarize() repeats every suspicious entry in full under
        # summary['suspicious']. Returning both would send each one twice;
        # detail lives in `results`, the summary stays a tally.
        return {
            'summary': {
                'total': summary['total'],
                'counts': summary['counts'],
                'has_suspicious': summary['has_suspicious'],
            },
            'parsed_format': 'bibtex' if use_bibtex else 'references',
            'results': results,
        }

    @mcp.resource(
        'health://status',
        name='search-health',
        description=(
            'Operational state of the search stack: per-domain circuit '
            'breakers, fetch cache hit rates and the current concurrency '
            'limit. Read this to explain WHY searches are failing (an engine '
            'tripped its breaker) rather than to answer a user question.'
        ),
        mime_type='application/json',
    )
    def search_health() -> dict:
        """Diagnostics, exposed as a resource rather than a tool.

        A tool is an action a model takes to answer a question; this is
        operational telemetry. Registering it as a tool would add a fifth
        option to every tool-selection decision for something a model should
        almost never call.
        """
        limiter = get_limiter()
        return {
            'cache': get_fetch_cache_stats(),
            'concurrency': {
                'limit': limiter.total_tokens,
                'in_use': limiter.borrowed_tokens,
            },
            'vertical_domains': list_domains(),
        }

    return mcp
