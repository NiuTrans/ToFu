"""lib/llm_dispatch/discovery/_discover.py — /v1/models discovery + pricing enrich.

``discover_models`` fetches an OpenAI-compatible /models endpoint and infers
capabilities/RPM/cost per model; ``enrich_models_with_pricing`` folds in
OpenRouter pricing.
"""

import re
import sys

import requests

from lib.http_client import http_get as _default_http_get
from lib.log import get_logger

from ._capabilities import _infer_capabilities, _infer_cost, _infer_rpm

logger = get_logger(__name__)


def http_get(*args, **kwargs):
    """Indirection so a monkeypatch of ``lib.llm_dispatch.discovery.http_get``
    (the package facade — the name the original single-module code exposed)
    is honored by discover_models / enrich_models_with_pricing.
    """
    pkg = sys.modules.get('lib.llm_dispatch.discovery')
    fn = getattr(pkg, 'http_get', None)
    # Avoid infinite recursion if the facade name still resolves back to us.
    if fn is not None and fn is not http_get:
        return fn(*args, **kwargs)
    return _default_http_get(*args, **kwargs)


# ── Discovery timeout (keep short — runs during startup) ─────
_DISCOVER_TIMEOUT = 10


# ══════════════════════════════════════════════════════
#  Model Discovery
# ══════════════════════════════════════════════════════

def discover_models(base_url: str, api_key: str,
                    timeout: int = _DISCOVER_TIMEOUT,
                    models_path: str = '') -> list[dict]:
    """Auto-discover models from an OpenAI-compatible /v1/models endpoint.

    Calls GET {models_url}, parses the response, infers capabilities,
    RPM, and cost for each model.

    Args:
        base_url: Provider base URL (e.g. 'https://yeysai.com/v1').
        api_key: API key for authentication.
        timeout: Request timeout in seconds.
        models_path: Optional custom path for the models endpoint.
            If empty (default), appends '/models' to base_url.
            Can be absolute ('/v1/models') or relative ('models').

    Returns:
        List of model dicts suitable for server_config providers.models:
        ``[{'model_id': str, 'aliases': [], 'capabilities': [...],
            'rpm': int, 'cost': float, 'thinking_default': bool}, ...]``
        Empty list on any failure.
    """
    # Normalize URL to /models endpoint
    # If the user specified a custom models_path, use it; otherwise default
    # to appending /models.  Gateways like Meituan may use non-standard
    # paths (e.g. /v1/openai/native/models).
    if models_path:
        # User-supplied path — join with base URL origin
        # models_path can be absolute (/v1/models) or relative (models)
        from urllib.parse import urlparse
        parsed = urlparse(base_url.rstrip('/'))
        origin = '%s://%s' % (parsed.scheme, parsed.netloc)
        if models_path.startswith('/'):
            models_url = origin + models_path
        else:
            models_url = base_url.rstrip('/') + '/' + models_path.lstrip('/')
    else:
        models_url = base_url.rstrip('/') + '/models'

    # Use-time SSRF egress guard (DNS can change since registration).
    from lib.byo_egress import EgressDenied, validate_egress_url
    try:
        validate_egress_url(models_url)
    except EgressDenied as e:
        logger.warning('[Discovery] blocked egress to %s: %s', models_url, e)
        return []

    logger.info('[Discovery] Fetching models from %s', models_url)

    headers = {'User-Agent': 'Tofu/1.0'}
    if api_key:
        headers['Authorization'] = 'Bearer %s' % api_key

    try:
        resp = http_get(
            models_url,
            headers=headers,
            timeout=timeout,
        )
        if not resp.ok:
            logger.warning('[Discovery] GET %s returned HTTP %d: %.500s',
                          models_url, resp.status_code, resp.text)
            return []

        data = resp.json()
        raw_models = data.get('data', [])
        if not isinstance(raw_models, list):
            logger.warning('[Discovery] Unexpected format: data is %s, not list',
                          type(raw_models).__name__)
            return []

        logger.info('[Discovery] Received %d models from API', len(raw_models))

    except requests.Timeout:
        logger.warning('[Discovery] Timeout after %ds: %s', timeout, models_url)
        return []
    except requests.RequestException as e:
        logger.warning('[Discovery] Request failed for %s: %s', models_url, e)
        return []
    except (ValueError, KeyError) as e:
        logger.warning('[Discovery] Invalid JSON response: %s', e)
        return []

    # ── Parse and enrich each model ──
    result = []
    for model_data in raw_models:
        model_id = model_data.get('id', '')
        if not model_id:
            continue
        # Skip internal / fine-tuned / system models
        if model_id.startswith(('system-', 'ft:', 'ft-')):
            continue

        caps = _infer_capabilities(model_id, model_data)
        rpm = _infer_rpm(model_id, caps)
        cost = _infer_cost(model_id, caps)

        entry = {
            'model_id': model_id,
            'aliases': [],
            'capabilities': sorted(caps),
            'rpm': rpm,
            'cost': cost,
            'thinking_default': 'thinking' in caps,
        }
        # Pass-through self-identification fields so downstream
        # heuristics (e.g. _detect_thinking_format) can branch on the
        # serving engine without re-querying /v1/models.
        owned_by = (model_data.get('owned_by') or '').strip()
        if owned_by:
            entry['owned_by'] = owned_by
        # If MODEL_PRICING has real input/output, include them
        from lib import MODEL_PRICING
        mp = MODEL_PRICING.get(model_id)
        if mp:
            entry['input_price'] = mp.get('input', 0)
            entry['output_price'] = mp.get('output', 0)
        result.append(entry)

    # Sort: text models first, then image_gen, then embedding
    def _sort_key(m):
        c = set(m['capabilities'])
        if 'embedding' in c:
            return (2, m['model_id'])
        if 'image_gen' in c:
            return (1, m['model_id'])
        return (0, m['model_id'])
    result.sort(key=_sort_key)

    n_text = sum(1 for m in result if 'text' in m['capabilities'])
    n_cheap = sum(1 for m in result if 'cheap' in m['capabilities'])
    n_img = sum(1 for m in result if 'image_gen' in m['capabilities'])
    n_emb = sum(1 for m in result if 'embedding' in m['capabilities'])
    logger.info('[Discovery] %d usable models: %d text (%d cheap), '
               '%d image_gen, %d embedding',
               len(result), n_text, n_cheap, n_img, n_emb)
    return result


# ══════════════════════════════════════════════════════
#  OpenRouter Pricing Enrichment
# ══════════════════════════════════════════════════════

def enrich_models_with_pricing(models: list[dict]) -> list[dict]:
    """Fetch pricing from OpenRouter and update cost + cheap tags.

    Intended to be called in a background thread (or synchronously for
    the Settings UI discover button).  Modifies models in-place.

    Args:
        models: List of model dicts (same format as discover_models output).

    Returns:
        The same list with updated cost values and 'cheap' tags.
    """
    try:
        resp = http_get(
            'https://openrouter.ai/api/v1/models',
            timeout=20,
            headers={'User-Agent': 'Tofu/1.0'},
        )
        if not resp.ok:
            logger.debug('[Discovery] OpenRouter pricing fetch failed: HTTP %d',
                        resp.status_code)
            return models

        or_models = resp.json().get('data', [])
        if not isinstance(or_models, list):
            return models

        # Build lookup: {normalized_name → {input_1m, output_1m}}
        or_lookup = {}
        for m in or_models:
            mid = m.get('id', '')
            pricing = m.get('pricing', {})
            pp = float(pricing.get('prompt', 0) or 0)
            cp = float(pricing.get('completion', 0) or 0)
            if pp <= 0 and cp <= 0:
                continue
            data = {
                'input_1m': round(pp * 1e6, 4),
                'output_1m': round(cp * 1e6, 4),
            }
            # Index by short name for matching
            short = mid.split('/')[-1] if '/' in mid else mid
            or_lookup[short.lower()] = data
            or_lookup[mid.lower()] = data

        from lib.llm_dispatch.config import reevaluate_pricing_tags

        updated = 0
        for model in models:
            mid_norm = model['model_id'].lower()
            # Strip provider prefixes
            for prefix in ('aws.', 'vertex.', 'gcp.', 'azure.', 'bedrock.'):
                mid_norm = mid_norm.replace(prefix, '')

            # Try exact match
            match = or_lookup.get(mid_norm)

            # Fuzzy match: shared word tokens (same approach as pricing.py)
            if not match:
                parts = set(re.split(r'[-_.\s/]', mid_norm))
                parts.discard('')
                best_score = 0
                for or_key, or_val in or_lookup.items():
                    or_parts = set(re.split(r'[-_.\s/]', or_key))
                    or_parts.discard('')
                    overlap = len(parts & or_parts)
                    if overlap >= 2 and overlap > best_score:
                        best_score = overlap
                        match = or_val

            if match:
                inp_1m = match['input_1m']
                out_1m = match['output_1m']
                blended_1m = (inp_1m + out_1m) / 2.0
                model['cost'] = round(blended_1m / 1000.0, 4)
                # Preserve real input/output pricing ($/1M tokens)
                model['input_price'] = round(inp_1m, 4)
                model['output_price'] = round(out_1m, 4)
                updated += 1

        # Re-evaluate pricing-tier tags in one pass using the enriched
        # input_price / output_price / cost fields.  Covers 'cheap' today
        # and any future tier added to PRICING_TIERS.
        reevaluate_pricing_tags(models, log_prefix='openrouter-enrich')

        logger.info('[Discovery] Enriched %d/%d models with OpenRouter pricing',
                   updated, len(models))

    except Exception as e:
        logger.warning('[Discovery] OpenRouter pricing enrichment failed: %s', e)

    return models
