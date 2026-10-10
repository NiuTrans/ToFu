"""lib/llm_dispatch/discovery/ — Model auto-discovery via /v1/models endpoint.

Auto-discovers available models from an OpenAI-compatible provider API,
infers capabilities from model name patterns + pricing data, and auto-tags
'cheap' for models whose input < Sonnet input ($3/1M) AND output < Sonnet
output ($15/1M).

Called automatically on first boot when endpoint is non-default, and via
the Settings UI "Discover Models" button.

Also provides ``probe_provider()`` — a one-shot probe that discovers models,
detects balance URL, infers brand/name, and suggests thinking format.

Sub-modules:
  _url          — normalize_base_url + local-endpoint helpers + CIDR cache
  _capabilities — capability/RPM/cost inference + name-pattern regexes
  _discover     — discover_models + enrich_models_with_pricing
  _brand        — provider brand / name auto-detection
  _balance      — balance/billing URL probing
  _thinking     — thinking-format detection
  _probe        — probe_provider (top-level orchestrator)

The package layout preserves the original ``lib.llm_dispatch.discovery``
import path byte-for-byte: every public symbol (and the private symbols
imported by consumers/tests) is re-exported here.
"""

from lib.http_client import http_get  # noqa: F401  (re-exported so tests can patch discovery.http_get)
from lib.log import get_logger

from ._balance import (
    _BALANCE_PROBE_PATHS,
    _BALANCE_PROBE_TIMEOUT,
    _probe_balance_url,
)
from ._brand import (
    _DOMAIN_BRAND_MAP,
    _detect_brand,
)
from ._capabilities import (
    _CHEAP_HINT_PAT,
    _EMBEDDING_PAT,
    _IMAGE_GEN_PAT,
    _THINKING_PAT,
    _VISION_PAT,
    _infer_capabilities,
    _infer_cost,
    _infer_rpm,
)
from ._discover import (
    _DISCOVER_TIMEOUT,
    discover_models,
    enrich_models_with_pricing,
)
from ._probe import probe_provider
from ._thinking import (
    _CHAT_TEMPLATE_KWARGS_ENGINES,
    _THINKING_FORMAT_HINTS,
    _detect_thinking_format,
    _is_chat_template_kwargs_engine,
)
from ._url import (
    _CHAT_SUFFIXES,
    _LOCAL_CIDRS_CACHE,
    _local_cidrs,
    _parse_local_cidrs,
    is_local_endpoint,
    is_raw_ip_host,
    normalize_base_url,
    should_bypass_proxy,
)

logger = get_logger(__name__)

# ── Public API (verbatim from the original module) ──
# CRITICAL: lib/llm_dispatch/__init__.py does ``from .discovery import *`` —
# this list must remain identical to the pre-split module's __all__.
__all__ = [
    'discover_models',
    'enrich_models_with_pricing',
    'is_local_endpoint',
    'is_raw_ip_host',
    'should_bypass_proxy',
    'normalize_base_url',
    'probe_provider',
]
