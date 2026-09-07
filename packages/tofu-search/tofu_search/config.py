"""tofu_search.config — Configuration management.

Replaces chatui's lib/__init__.py config constants with a standalone
dataclass-based configuration. Supports both global defaults (via
configure()) and per-call overrides.
"""

import contextvars
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Optional

__all__ = ['SearchConfig', 'get_config', 'configure', 'bind_call_config']


@dataclass
class SearchConfig:
    """Configuration for the tofu-search pipeline.

    All values have sane defaults matching chatui's production settings.
    Users can override via configure() or per-call kwargs.
    """

    # ── Fetch settings ──
    fetch_top_n: int = 6
    fetch_timeout: int = 15
    fetch_max_chars_search: int = 60_000
    fetch_max_chars_direct: int = 200_000
    fetch_max_chars_pdf: int = 0  # 0 = unlimited
    fetch_max_bytes: int = 20 * 1024 * 1024  # 20 MB
    # Host-level search profiles may enable one-hop link deepening.  Kept as a
    # plain boolean here so standalone callers can configure it directly.
    deepen_enabled: bool = False

    # ── Wall-clock deadlines (robustness against wedged/dead hosts) ──
    # Total budget for ONE perform_web_search() call. When exceeded, the
    # pipeline force-returns whatever it has gathered so far (partial results +
    # a ``_deadline_hit`` marker) instead of blocking on slow hosts: it caps the
    # fetch-wait loop and short-circuits the LLM-filter / deepen / rerank stages.
    # The ONLY prior caps were a 20s engine timeout and a 90s fetch timeout, and
    # the 90s only exits early once ``kept_ok >= target_ok`` — a count a
    # niche-domain query never reaches, so it hung the full 90s (and then some).
    # 0 disables the cap (legacy unbounded behaviour). Env: TOFU_SEARCH_DEADLINE_SECS.
    search_deadline_secs: int = 45
    # Total budget for ONE fetch_page_content() URL, bounding the whole fallback
    # chain (HTTP body-download + browser + Playwright) so a single dead host
    # can't stack per-hop timeouts (do_request body deadline is timeout*3=45s,
    # then a 15-25s browser fallback, then a 15s Playwright render) into 60s+.
    # Soft bound: it clamps the HTTP hop and SKIPS any further fallback once the
    # budget is blown, so worst case ≈ deadline + one in-flight hop.
    # 0 disables the cap. Env: TOFU_SEARCH_FETCH_URL_DEADLINE_SECS.
    fetch_url_deadline_secs: int = 25

    # ── Per-engine request throttle (self-inflicted rate-limit guard) ──
    # Minimum interval (ms) between two requests to the SAME search engine,
    # enforced process-globally in http_search_get just before the GET. Two
    # CONCURRENT search calls (e.g. two parallel recommend batches) that would
    # otherwise hit one engine within the same second — the cause of the
    # observed DDG-HTML ``202 (rate-limited)`` — serialize to >= this interval
    # instead. Per-engine (a busy engine never blocks a different one), with a
    # small upward jitter so two colliding threads desynchronize. The wait is
    # clamped to the request timeout, so it spends budget the caller already
    # has and never pushes a query past its deadline. 0 disables the throttle
    # (byte-identical to the old unthrottled path). Only wraps the HTML-engine
    # envelope — the arXiv/Semantic-Scholar JSON vertical path is NOT throttled.
    # Env: TOFU_SEARCH_MIN_REQUEST_INTERVAL_MS.
    min_request_interval_ms: int = 400

    # ── Proxy ──
    # Explicit proxy URL (e.g. 'http://10.0.0.1:8080'). A host that resolves a
    # proxy from its own Settings (e.g. chatui) injects it here; when empty the
    # standard https_proxy / http_proxy / all_proxy env vars are used instead.
    proxy_url: str = ''
    # When a proxy IS available, try BOTH network paths (proxied ↔ direct) per
    # engine and remember which one worked (see search/proxy_mode.py). This is
    # why "search works on one machine but not another" — a container behind a
    # proxy with no env var, or a host with a stale/dead proxy env var, or a
    # soft-blocked egress IP. With no proxy configured this is a no-op (single
    # direct attempt). Set False to force one attempt on the proxy path only.
    proxy_dual_attempt: bool = True
    # Extra proxy URLs forming a failover CHAIN behind ``proxy_url`` (ordered,
    # de-duped; e.g. ['http://bk-gw:8080']). A dead primary (allowlist gateway
    # refusing CONNECT with a tunnel 403, or a hard-down endpoint) no longer
    # empties search: the remaining proxies (then the direct path) are tried —
    # in PARALLEL when ``proxy_race`` is on. Falls back to the env var below
    # (comma/whitespace separated). Only consulted when ``proxy_dual_attempt``
    # is on; with it off the plan stays the single primary-proxy attempt.
    # Env: TOFU_SEARCH_PROXY_FALLBACK_URLS.
    proxy_fallback_urls: list = field(default_factory=list)
    # When the FIRST network path fails at connect level and TWO OR MORE
    # alternate paths remain, race them concurrently and take the first genuine
    # success (a dead primary usually fails in milliseconds, but a wedged one
    # would otherwise cost a full per-request timeout before the next path is
    # even tried — racing bounds the failover at ONE timeout instead of N).
    # Losers are abandoned in flight and never touch the per-engine learning.
    # Env: TOFU_SEARCH_PROXY_RACE.
    proxy_race: bool = True

    # ── Security ──
    # Block fetches whose host resolves to a private / loopback / link-local /
    # reserved address (SSRF guard). Applies to the initial URL *and* every
    # redirect hop. Leave on unless you deliberately fetch internal hosts.
    block_private_addresses: bool = True
    # When a TLS certificate cannot be verified, retry with verification
    # DISABLED. Off by default — enabling exposes those fetches to MITM.
    allow_insecure_ssl_fallback: bool = False
    # Hostnames exempted from the SSRF guard — the explicit way to say "I do
    # mean to fetch this internal host". Matched on the HOSTNAME (exact, or as
    # a parent suffix: 'example-corp.com' covers 'your-llm-gateway.example.com'), never on the
    # resolved IP: an internal load balancer rotates its address between
    # lookups, so an IP allowlist silently rots. Bare IPs are still checked
    # against the numeric guard, so a literal-IP URL cannot slip through by
    # naming a host here. Empty (default) = every private target stays blocked.
    allow_private_hosts: set = field(default_factory=set)

    # ── Domains to skip (media, social, etc.) ──
    skip_domains: set = field(default_factory=lambda: {
        'youtube.com', 'youtu.be', 'twitter.com', 'x.com',
        'facebook.com', 'instagram.com', 'tiktok.com',
        'linkedin.com', 'discord.com',
    })

    # ── SearXNG public instances (rotated to spread load / survive blocks) ──
    # A self-hosted instance is tried FIRST and is never shuffled away. It can
    # expose the JSON API and a much broader engine set without depending on
    # the policy/rate limits of public instances. Env: TOFU_SEARCH_SEARXNG_URL.
    searxng_url: str = field(
        default_factory=lambda: os.environ.get('TOFU_SEARCH_SEARXNG_URL', ''))
    # Empty means "respect the instance's enabled engines". Hard-coding only
    # google,bing,duckduckgo defeats SearXNG's useful free/vertical providers
    # (Mwmbl, OpenAlex, PubMed, GitHub, Marginalia, ...). Override with a
    # comma-separated list only when deterministic routing is required.
    # Env: TOFU_SEARCH_SEARXNG_ENGINES.
    searxng_engines: str = field(
        default_factory=lambda: os.environ.get('TOFU_SEARCH_SEARXNG_ENGINES', ''))
    # Public instances churn — override this list when the defaults go stale.
    # Pruned 2026-08-21 (probed via proxy egress): charliewhiskey (SSL error),
    # freestater (connect timeout), hbubli (Anubis PoW — a non-JS client can
    # never pass). The rest answer but often 429 datacenter IPs.
    searxng_instances: list = field(default_factory=lambda: [
        'https://search.indst.eu',
        'https://search.einfachzocken.eu',
        'https://priv.au',
        'https://paulgo.io',
        'https://search.catboy.house',
        'https://opnxng.com',
    ])

    # ── Travel vertical (RollingGo / 道旅 MCP) ──
    # Credential for the RollingGo hotel + flight MCP endpoints. Read from the
    # environment at dataclass-construction time — NOT only inside configure()
    # — because a host that never calls configure() would otherwise never pick
    # the key up. The hotel endpoint requires it; the flight endpoint currently
    # serves anonymous callers, so availability is decided per TYPE (see
    # search/vertical/travel_flight.py) rather than for the whole domain.
    rollinggo_api_key: str = field(
        default_factory=lambda: os.environ.get('ROLLINGGO_API_KEY', ''))
    rollinggo_hotel_endpoint: str = 'https://mcp.rollinggo.cn/mcp'
    rollinggo_flight_endpoint: str = 'https://mcp.rollinggo.cn/mcp/flight'
    # Travel lookups are multi-hop (airport resolve → flight search) against a
    # live inventory API, so they need a longer ceiling than the 10s the
    # metadata verticals use — but still well inside search_deadline_secs.
    vertical_travel_timeout: int = 25

    # ── Travel vertical (FlyAI / 飞猪 MCP) ──
    # The travel vertical's ANONYMOUS provider. The public flyai-cli npm
    # package ships a built-in trial credential, which travel_flyai.py embeds
    # — so an EMPTY value here means "use the bundled trial credential", NOT
    # "provider disabled". Set FLYAI_API_KEY (and FLYAI_SIGN_SECRET when the
    # signing secret rotates) to step off the shared trial quota.
    flyai_api_key: str = field(
        default_factory=lambda: os.environ.get('FLYAI_API_KEY', ''))
    flyai_sign_secret: str = field(
        default_factory=lambda: os.environ.get('FLYAI_SIGN_SECRET', ''))
    flyai_mcp_endpoint: str = 'https://flyai.open.fliggy.com/mcp'

    # ── Xiaohongshu engine account-risk guard ──
    # XHS polices automated access at the ACCOUNT level (限流/滑块/封号), and
    # this engine fires on every web search while connected — so the engine
    # paces itself instead of letting chat frequency set the request rate.
    # xhs_min_interval_s: minimum seconds between two real XHS page loads,
    #   plus a small random jitter (限 QPS + 随机延迟, the cross-project
    #   consensus for account safety). When the required wait would exceed
    #   the engine's latency budget the call is SKIPPED (returns []) rather
    #   than stalling the whole pipeline. Env: TOFU_XHS_MIN_INTERVAL_S.
    #   0 disables pacing.
    xhs_min_interval_s: float = 5.0
    # xhs_cache_ttl_s: same-keyword results are served from an in-memory TTL
    #   cache — a chat assistant re-asks near-identical queries constantly,
    #   and each repeat would be another logged-in hit (同关键词缓存, also
    #   consensus). Env: TOFU_XHS_CACHE_TTL_S. 0 disables the cache.
    xhs_cache_ttl_s: int = 600
    # xhs_backoff_cooldown_s: after several CONSECUTIVE empty scrapes (the
    #   signature of a risk-control wall or an expired cookie — a captcha /
    #   login-redirect page carries no note cards), the engine stops touching
    #   XHS for this long. Hammering a flagged account makes the flag worse.
    #   Env: TOFU_XHS_BACKOFF_COOLDOWN_S.
    xhs_backoff_cooldown_s: int = 1800

    # ── LLM configuration for content filter ──
    # Option A: OpenAI-compatible endpoint
    llm_api_key: str = ''
    llm_base_url: str = 'https://api.openai.com/v1'
    llm_model: str = 'gpt-4o-mini'
    llm_temperature: float = 0.0

    # Option B: Custom callable — takes (messages, **kwargs) -> str
    # If set, this overrides the OpenAI endpoint config.
    llm_function: Optional[Callable] = None

    # ── Content filter settings ──
    filter_enabled: bool = True
    # 'gate'    — relevance verdict ONLY: the LLM reads the page opening plus
    #             query-focused excerpts (capped at gate_input_max_chars) and answers [USEFUL] /
    #             §§IRRELEVANT§§ — a handful of output tokens. Useful pages keep
    #             their ORIGINAL extracted text. Fast (~1-3s/page).
    # 'rewrite' — the LLM regenerates the whole cleaned page verbatim (the
    #             pre-0.6 behaviour): highest cleaning quality, but output ≈
    #             page length, so generation dominates at 10-60s+ per page.
    filter_mode: str = 'gate'
    filter_min_chars: int = 3000
    # Per-LLM-call ceiling. 45s is generous for gate mode; in rewrite mode a
    # very long page may exceed it, in which case the raw text is served
    # (filtering is an enhancement, never a blocker). Raise it if you run
    # rewrite mode against big pages. Was 300 before 0.6.
    filter_timeout: int = 45
    # Gate mode judges relevance from bounded selected passages — the full body
    # is never sent to the LLM, which kills the prompt-processing cost too.
    # Rewrite mode always sends the full text.
    gate_input_max_chars: int = 12_000
    # Result cache for the LLM filter, keyed on (mode, url, query,
    # user_question, raw_text) — a repeated search of the same pages costs
    # zero LLM calls. 0 disables. Mirrors the fetch cache's TTL+LRU pattern.
    filter_cache_ttl: int = 600
    filter_cache_max_size: int = 500

    # Total extracted page characters included in the MCP web_search response.
    # The fetch/ranking pipeline still sees the full pages; only model-facing
    # output is reduced to query-focused passages. Env:
    # TOFU_SEARCH_MCP_CONTENT_BUDGET_CHARS.
    mcp_content_budget_chars: int = 18_000

    # ── Pre-fetch relevance gate ──
    # A cheap, no-LLM lexical check (title+snippet vs query terms) that runs
    # BEFORE a result is fetched, so obviously off-topic SERP junk (e.g. a
    # health page returned for an academic query) is never fetched. Fail-open
    # by design — see search/prefetch_gate.py.
    prefetch_gate_enabled: bool = True
    # Below this many meaningful query terms the gate is a no-op (fetch all).
    prefetch_gate_min_query_terms: int = 2
    # Always fetch at least this many leading candidates (recall floor).
    prefetch_gate_min_fetch: int = 3

    def has_llm(self) -> bool:
        """Return True if an LLM is configured (either callable or API key)."""
        return bool(self.llm_function) or bool(self.llm_api_key)

    def copy(self, **overrides) -> 'SearchConfig':
        """Create a copy with specific fields overridden."""
        import dataclasses
        return dataclasses.replace(self, **overrides)


# ── Global singleton ──
_lock = threading.Lock()
_global_config = SearchConfig()

# ── Per-call override (ContextVar) ──
# perform_web_search binds the caller's config override here for the call's
# duration; worker threads inherit it through submit_with_provider_context's
# copy_context. Without this, a per-call override (search(**kwargs) /
# perform_web_search(config=...)) never reached code paths that read the
# global config — e.g. the engine proxy plan in search/_common.py silently
# ignored a per-call proxy_url and went direct.
_call_config_override: contextvars.ContextVar = contextvars.ContextVar(
    'tofu_search_call_config', default=None)


def get_config() -> SearchConfig:
    """Get the effective configuration (thread- and context-safe).

    A per-call override bound via :func:`bind_call_config` wins over the
    global config for the duration of that call.
    """
    override = _call_config_override.get()
    if override is not None:
        return override
    with _lock:
        return _global_config


@contextmanager
def bind_call_config(config: SearchConfig):
    """Bind *config* as the effective config for this context.

    Worker threads spawned via ``submit_with_provider_context``
    (``contextvars.copy_context``) inherit the binding; the global config is
    restored on exit, so concurrent calls with different overrides never
    stomp on each other.
    """
    token = _call_config_override.set(config)
    try:
        yield config
    finally:
        _call_config_override.reset(token)


def configure(**kwargs) -> SearchConfig:
    """Set global configuration values.

    Accepts any SearchConfig field name as a keyword argument.
    Returns the updated config.

    Example::

        from tofu_search import configure
        configure(
            llm_api_key='sk-...',
            llm_base_url='https://api.openai.com/v1',
            llm_model='gpt-4o-mini',
            fetch_top_n=10,
        )
    """
    global _global_config
    with _lock:
        def _as_bool(v: str) -> bool:
            return v.strip().lower() in ('1', 'true', 'yes', 'on')

        def _as_csv(v: str) -> list:
            return [t for t in (tok.strip() for tok in v.replace(',', ' ').split())
                    if t]

        # Also support env var overrides
        env_mapping = {
            'FETCH_TOP_N': ('fetch_top_n', int),
            'FETCH_TIMEOUT': ('fetch_timeout', int),
            'FETCH_MAX_CHARS_SEARCH': ('fetch_max_chars_search', int),
            'FETCH_MAX_CHARS_DIRECT': ('fetch_max_chars_direct', int),
            'FETCH_MAX_CHARS_PDF': ('fetch_max_chars_pdf', int),
            'FETCH_MAX_BYTES': ('fetch_max_bytes', int),
            'TOFU_SEARCH_DEADLINE_SECS': ('search_deadline_secs', int),
            'TOFU_SEARCH_FETCH_URL_DEADLINE_SECS': ('fetch_url_deadline_secs', int),
            'TOFU_SEARCH_PROXY_URL': ('proxy_url', str),
            'TOFU_SEARCH_PROXY_DUAL_ATTEMPT': ('proxy_dual_attempt', _as_bool),
            'TOFU_SEARCH_PROXY_FALLBACK_URLS': ('proxy_fallback_urls', _as_csv),
            'TOFU_SEARCH_PROXY_RACE': ('proxy_race', _as_bool),
            'TOFU_SEARCH_MIN_REQUEST_INTERVAL_MS': ('min_request_interval_ms', int),
            'TOFU_SEARCH_SEARXNG_URL': ('searxng_url', str),
            'TOFU_SEARCH_SEARXNG_ENGINES': ('searxng_engines', str),
            'TOFU_SEARCH_MCP_CONTENT_BUDGET_CHARS': ('mcp_content_budget_chars', int),
            'ROLLINGGO_API_KEY': ('rollinggo_api_key', str),
            'TOFU_XHS_MIN_INTERVAL_S': ('xhs_min_interval_s', float),
            'TOFU_XHS_CACHE_TTL_S': ('xhs_cache_ttl_s', int),
            'TOFU_XHS_BACKOFF_COOLDOWN_S': ('xhs_backoff_cooldown_s', int),
        }

        # Apply env var defaults (only for fields not explicitly set by user)
        for env_key, (field_name, cast) in env_mapping.items():
            if field_name not in kwargs:
                env_val = os.environ.get(env_key)
                if env_val is not None:
                    kwargs[field_name] = cast(env_val)

        _global_config = _global_config.copy(**kwargs)
        return _global_config
