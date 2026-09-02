"""lib/llm_dispatch/discovery/_discover.py — /v1/models discovery + pricing enrich.

``discover_models`` fetches an OpenAI-compatible /models endpoint and infers
capabilities/RPM/cost per model; ``enrich_models_with_pricing`` folds in
OpenRouter pricing.
"""

import re
import sys
import time

import requests

from lib.http_client import http_get as _default_http_get
from lib.log import get_logger

from ._capabilities import _infer_capabilities, _infer_rpm

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


def _fetch_models_json(models_url: str, headers: dict, timeout: int,
                       *, quiet_not_found: bool = False):
    """GET {models_url} once. Returns ``(data_dict, None)`` or ``(None, err)`.

    ``err`` is one of ``'http-<status>'`` / ``'timeout'`` / ``'conn'`` /
    ``'bad-json'`` — callers branch on the exact failure class (only a 404
    on a bare origin justifies the /v1 fallback retry; a timeout means the
    box is down and retrying would just double the wait).
    """
    routine_log = logger.debug if quiet_not_found else logger.info
    routine_log('[Discovery] Fetching models from %s', models_url)
    try:
        resp = http_get(
            models_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.Timeout:
        logger.warning('[Discovery] Timeout after %ds: %s', timeout, models_url)
        return None, 'timeout'
    except requests.RequestException as e:
        logger.warning('[Discovery] Request failed for %s: %s', models_url, e)
        return None, 'conn'

    if not resp.ok:
        # A periodic well-known-port sweep first proves that TCP is open, but
        # the listener can legitimately be an unrelated local service. Its
        # /models + /v1/models 404 pair means "not an LLM engine", not an
        # operational incident. Interactive/provider-config discovery still
        # defaults to WARNING so a user's bad URL remains visible; only the
        # explicit background-sweep context opts into DEBUG for clean 404s.
        log = logger.debug if quiet_not_found and resp.status_code == 404 \
            else logger.warning
        log('[Discovery] GET %s returned HTTP %d: %.500s',
            models_url, resp.status_code, resp.text)
        return None, 'http-%d' % resp.status_code

    try:
        data = resp.json()
    except (ValueError, KeyError) as e:
        logger.warning('[Discovery] Invalid JSON response from %s: %s', models_url, e)
        return None, 'bad-json'
    if not isinstance(data, dict) or not isinstance(data.get('data'), list):
        logger.warning('[Discovery] Unexpected format from %s: data is %s, not list',
                      models_url,
                      type(data.get('data')).__name__ if isinstance(data, dict)
                      else type(data).__name__)
        return None, 'bad-json'
    routine_log('[Discovery] Received %d models from API', len(data['data']))
    return data, None


# ══════════════════════════════════════════════════════
#  Model Discovery
# ══════════════════════════════════════════════════════

def discover_models(base_url: str, api_key: str,
                    timeout: int = _DISCOVER_TIMEOUT,
                    models_path: str = '',
                    return_effective: bool = False,
                    quiet_not_found: bool = False):
    """Auto-discover models from an OpenAI-compatible /v1/models endpoint.

    Calls GET {models_url}, parses the response, infers capabilities,
    RPM, and cost for each model.

    Bare-origin URLs (e.g. Ollama's habitual ``http://host:11434`` with no
    path) that answer /models with HTTP 404 are retried once under
    ``/v1`` — that is the single most common self-hosted mis-paste, and
    without the retry the probe just looks broken.

    Args:
        base_url: Provider base URL (e.g. 'https://yeysai.com/v1').
        api_key: API key for authentication.
        timeout: Request timeout in seconds.
        models_path: Optional custom path for the models endpoint.
            If empty (default), appends '/models' to base_url.
            Can be absolute ('/v1/models') or relative ('models').
        return_effective: When True, return ``(models, effective_base_url)``
            so the caller can persist the URL that ACTUALLY worked (the
            /v1 fallback variant) instead of the raw user input.
        quiet_not_found: Log HTTP 404 and routine request/success details at
            DEBUG instead of WARNING/INFO. Reserved for periodic best-effort
            probes where an open well-known port may belong to an unrelated
            service. All other failures stay WARNING.

    Returns:
        List of model dicts suitable for server_config providers.models:
        ``[{'model_id': str, 'aliases': [], 'capabilities': [...],
            'rpm': int, 'cost': float, 'thinking_default': bool}, ...]``
        Empty list on any failure. With ``return_effective=True`` a
        ``(list, str)`` tuple instead.
    """
    def _ret(models, effective):
        return (models, effective) if return_effective else models
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
        return _ret([], base_url.rstrip('/'))

    headers = {'User-Agent': 'Tofu/1.0'}
    if api_key:
        headers['Authorization'] = 'Bearer %s' % api_key

    effective_base = base_url.rstrip('/')
    data, err = _fetch_models_json(
        models_url, headers, timeout, quiet_not_found=quiet_not_found)

    # ── /v1 fallback: bare-origin URL + plain 404 → retry under /v1 ──
    # Gated on a CUSTOM models_path being absent (an explicit path is
    # authoritative) and on the error being a clean 404 (not a down box).
    if data is None and err == 'http-404' and not models_path:
        from urllib.parse import urlparse as _urlparse
        if _urlparse(effective_base).path in ('', '/'):
            alt_base = effective_base + '/v1'
            alt_url = alt_base + '/models'
            try:
                validate_egress_url(alt_url)
            except EgressDenied as e:
                logger.warning('[Discovery] /v1 fallback blocked egress to %s: %s',
                              alt_url, e)
                alt_url = ''
            if alt_url:
                data, err = _fetch_models_json(
                    alt_url, headers, timeout,
                    quiet_not_found=quiet_not_found)
                if data is not None:
                    effective_base = alt_base
                    fallback_log = logger.debug if quiet_not_found else logger.info
                    fallback_log('[Discovery] bare-origin /models 404 — '
                                 'fell back to %s', alt_url)

    if data is None:
        return _ret([], effective_base)
    raw_models = data.get('data', [])

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
        entry = {
            'model_id': model_id,
            'aliases': [],
            'capabilities': sorted(caps),
            'rpm': rpm,
            'thinking_default': 'thinking' in caps,
        }
        # Preserve a supplier-declared profile when present; otherwise the
        # evidence registry may recognize a documented product hierarchy from
        # the wire id (for example Sol > Terra > Luna). Unknown names remain
        # explicitly unknown and are never promoted into swarm routing.
        from lib.model_profiles import build_model_profile
        declared_profile = model_data.get('capability_profile')
        if isinstance(declared_profile, dict):
            declared_profile = dict(declared_profile)
            declared_profile['evidence'] = 'provider_catalog'
        profile = build_model_profile(
            model_id, model_entry={
                'capability_profile': declared_profile
            } if declared_profile is not None else None)
        entry['capability_profile'] = {
            'family': profile['family'],
            'quality': profile['quality'],
            'roles': profile['roles'],
            'evidence': profile['evidence'],
            'confidence': profile['confidence'],
            'updated_at': int(time.time()),
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
    result_log = logger.debug if quiet_not_found else logger.info
    result_log('[Discovery] %d usable models: %d text (%d cheap), '
               '%d image_gen, %d embedding',
               len(result), n_text, n_cheap, n_img, n_emb)
    return _ret(result, effective_base)


# ══════════════════════════════════════════════════════
#  OpenRouter Pricing Enrichment
# ══════════════════════════════════════════════════════

def enrich_models_with_pricing(models: list[dict]) -> list[dict]:
    """Fetch pricing from OpenRouter and update billable prices + tier tags.

    Intended to be called in a background thread (or synchronously for
    the Settings UI discover button).  Modifies models in-place.

    Args:
        models: List of model dicts (same format as discover_models output).

    Returns:
        The same list with canonical ``pricing`` rows and updated tier tags.
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
                model['pricing'] = {
                    'input': round(inp_1m, 4),
                    'output': round(out_1m, 4),
                    'currency': 'USD',
                    'unit': 'per_million_tokens',
                }
                model.pop('cost', None)
                model.pop('input_price', None)
                model.pop('output_price', None)
                updated += 1

        # Re-evaluate pricing-tier tags in one pass using the enriched
        # canonical pricing fields. Covers 'cheap' today and any future tier
        # added to PRICING_TIERS.
        reevaluate_pricing_tags(models, log_prefix='openrouter-enrich')

        logger.info('[Discovery] Enriched %d/%d models with OpenRouter pricing',
                   updated, len(models))

    except Exception as e:
        logger.warning('[Discovery] OpenRouter pricing enrichment failed: %s', e)

    return models
