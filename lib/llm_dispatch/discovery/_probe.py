"""lib/llm_dispatch/discovery/_probe.py — Full one-shot provider probe."""

from lib.log import get_logger, log_context
from lib.proxy import (
    register_no_proxy_url as _register_no_proxy_url,
)

from ._balance import _probe_balance_url
from ._brand import _detect_brand
from ._discover import discover_models, enrich_models_with_pricing
from ._thinking import _detect_thinking_format
from ._url import is_local_endpoint, normalize_base_url

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════
#  Full Provider Probe (one-shot setup)
# ══════════════════════════════════════════════════════

def probe_provider(base_url: str, api_key: str,
                   models_path: str = '', *,
                   force_local: bool = False) -> dict:
    """One-shot provider probe: discover models, detect brand, find balance URL.

    Orchestrates all discovery steps into a single call suitable for
    the "Auto Setup" UI flow.

    Args:
        base_url: Provider API base URL (e.g. 'https://api.deepseek.com').
        api_key: API key for authentication.
        models_path: Optional custom models endpoint path.

    Returns:
        Dict with keys:
        - ok (bool): Whether model discovery succeeded.
        - error (str): Error message if ok=False.
        - brand (str): Detected brand ID (e.g. 'deepseek').
        - name (str): Suggested display name (e.g. 'DeepSeek').
        - models (list): Discovered and enriched model dicts.
        - balance_url (str): Detected balance URL, or ''.
        - thinking_format (str): Suggested thinking format, or ''.
        - summary (dict): Stats about discovered models.
    """
    # Be forgiving: users often paste a /chat/completions URL when they mean
    # the OpenAI-compatible base. Strip the suffix before doing anything else.
    base_url = normalize_base_url(base_url)

    logger.info('[Probe] Starting probe for %s', base_url)
    with log_context('probe_provider', logger=logger):
        # ── Step 1: Detect brand from URL ──
        brand, name = _detect_brand(base_url)
        is_local = force_local or brand == 'local' or is_local_endpoint(base_url)
        # Self-hosted endpoints frequently sit on private (or pseudo-private)
        # IPs that the corporate HTTP proxy can't reach. Register the host
        # for proxy bypass before we do any HTTP — otherwise even the
        # /v1/models GET below will time out via the corp proxy.
        if is_local:
            _register_no_proxy_url(base_url)
        if force_local and brand != 'local':
            # User explicitly added this URL via 'Bulk Add Local Endpoints',
            # so override the auto-detected brand. Keep the auto-detected
            # display name when it's already meaningful (host:port for IPs).
            brand = 'local'
            name = 'Local %s' % name if not name.lower().startswith('local') else name
        logger.info('[Probe] Brand detected: %s (%s) from %s', brand, name, base_url)

        # ── Step 2: Discover models ──
        models = discover_models(base_url, api_key, models_path=models_path)
        if not models:
            return {
                'ok': False,
                'error': '在 %s 未发现任何模型。请检查 API 地址和密钥是否正确。' % base_url,
                'brand': brand,
                'name': name,
                'base_url': base_url,
                'is_local': is_local,
            }

        logger.info('[Probe] Discovered %d models, enriching with pricing…', len(models))

        # ── Step 3: Enrich with OpenRouter pricing ──
        # Self-hosted models are not on OpenRouter — skip the upstream call so
        # local probes stay fast and don't leak host names to the public API.
        if not is_local:
            models = enrich_models_with_pricing(models)

        # ── Step 4: Detect thinking format ──
        thinking_format = _detect_thinking_format(models, brand)
        logger.info('[Probe] Thinking format suggestion: %s',
                   thinking_format or '(auto-detect)')

        # ── Step 5: Probe balance URL ──
        # Local OSS engines (vLLM / SGLang / Ollama) have no billing endpoint —
        # skip 5+ pointless 404s.
        balance_url = '' if is_local else _probe_balance_url(base_url, api_key)

        # ── Build summary ──
        n_text = sum(1 for m in models if 'text' in m.get('capabilities', []))
        n_thinking = sum(1 for m in models if 'thinking' in m.get('capabilities', []))
        n_vision = sum(1 for m in models if 'vision' in m.get('capabilities', []))
        n_cheap = sum(1 for m in models if 'cheap' in m.get('capabilities', []))
        n_img = sum(1 for m in models if 'image_gen' in m.get('capabilities', []))
        n_emb = sum(1 for m in models if 'embedding' in m.get('capabilities', []))

        summary = {
            'total': len(models),
            'text': n_text,
            'thinking': n_thinking,
            'vision': n_vision,
            'cheap': n_cheap,
            'image_gen': n_img,
            'embedding': n_emb,
        }

        logger.info('[Probe] Complete: %d models, balance=%s, brand=%s',
                   len(models), bool(balance_url), brand)

        return {
            'ok': True,
            'brand': brand,
            'name': name,
            'base_url': base_url,
            'is_local': is_local,
            'models': models,
            'balance_url': balance_url,
            'thinking_format': thinking_format,
            'summary': summary,
        }
