"""lib/llm_dispatch/discovery.py — Model auto-discovery via /v1/models endpoint.

Auto-discovers available models from an OpenAI-compatible provider API,
infers capabilities from model name patterns + pricing data, and auto-tags
'cheap' for models whose input < Sonnet input ($3/1M) AND output < Sonnet
output ($15/1M).

Called automatically on first boot when endpoint is non-default, and via
the Settings UI "Discover Models" button.

Also provides ``probe_provider()`` — a one-shot probe that discovers models,
detects balance URL, infers brand/name, and suggests thinking format.
"""

import re
from urllib.parse import urlparse

import requests

from lib.log import get_logger, log_context
from lib.proxy import proxies_for as _proxies_for

logger = get_logger(__name__)

__all__ = [
    'discover_models',
    'enrich_models_with_pricing',
    'probe_provider',
]

# ── Discovery timeout (keep short — runs during startup) ─────
_DISCOVER_TIMEOUT = 10

# ══════════════════════════════════════════════════════
#  Capability Inference Patterns
# ══════════════════════════════════════════════════════

_EMBEDDING_PAT = re.compile(r'embed', re.I)
_IMAGE_GEN_PAT = re.compile(r'(dall-?e|[-_]image|image[-_])', re.I)

# Thinking / reasoning models
_THINKING_PAT = re.compile(
    r'(think|reason|\bo[1234]-|\bo[1234]\b|ernie-x)',
    re.I,
)

# Vision-capable families (permissive — most modern models support it)
_VISION_PAT = re.compile(
    r'(vision|vl\b|vlm'
    r'|gpt-4[.o]|gpt-5'                     # GPT-4o+, GPT-5+
    r'|claude.*(opus|sonnet|haiku)'          # All Claude 3+ have vision
    r'|gemini(?!.*lite)'                     # Gemini (except flash-lite)
    r'|qwen.*(vl|max|plus)'                 # Qwen VL/Max/Plus
    r'|ernie-5\.0'                           # ERNIE 5.0 is natively multimodal
    r'|kimi-k2\.[56]'                        # Kimi K2.5/K2.6 are natively multimodal
    r'|glm-5v'                               # GLM-5V (vision variant)
    r')',
    re.I,
)

# Cheap model name hints (fallback when no pricing data exists)
_CHEAP_HINT_PAT = re.compile(
    r'(mini|nano|lite|turbo|small|haiku|free)',
    re.I,
)


# ══════════════════════════════════════════════════════
#  Capability / RPM / Cost Inference
# ══════════════════════════════════════════════════════

def _infer_capabilities(model_id: str, model_meta: dict = None) -> set:
    """Infer model capabilities from its name and optional API metadata.

    Auto-tags 'cheap' if the model's input price < Sonnet input ($3/1M) AND
    output price < Sonnet output ($15/1M), using MODEL_PRICING.

    Args:
        model_id: The model identifier (e.g. 'gpt-5.4-mini').
        model_meta: Optional metadata dict from the /v1/models response.

    Returns:
        Set of capabilities like {'text', 'vision', 'thinking', 'cheap'}.
    """
    caps = set()

    # Some providers include capability info in model metadata
    if model_meta and isinstance(model_meta.get('capabilities'), list):
        for c in model_meta['capabilities']:
            if isinstance(c, str):
                caps.add(c.lower())

    mid_lower = model_id.lower()

    # ── Embedding models (not chat models) ──
    if _EMBEDDING_PAT.search(mid_lower):
        caps.add('embedding')
        return caps

    # ── Image generation models (not chat models) ──
    if _IMAGE_GEN_PAT.search(mid_lower):
        caps.add('image_gen')
        return caps

    # ── Chat models ──
    caps.add('text')

    if _THINKING_PAT.search(mid_lower):
        caps.add('thinking')

    if _VISION_PAT.search(mid_lower):
        caps.add('vision')

    # ── Pricing-based cheap tag ──
    from lib.llm_dispatch.config import is_model_cheap
    if is_model_cheap(model_id):
        caps.add('cheap')
    # Note: name-heuristic fallback (_CHEAP_HINT_PAT) is intentionally removed.
    # cheap tag should only come from real pricing data to avoid false positives.

    return caps


def _infer_rpm(model_id: str, capabilities: set) -> int:
    """Guess a reasonable RPM limit from model type."""
    mid = model_id.lower()
    if 'embedding' in capabilities:
        return 60
    if 'image_gen' in capabilities:
        return 10
    if any(x in mid for x in ('nano', 'turbo', 'free')):
        return 200
    if any(x in mid for x in ('mini', 'small', 'haiku', 'lite')):
        return 120
    if any(x in mid for x in ('flash',)):
        return 100
    if any(x in mid for x in ('opus', 'large', 'max', '-pro')):
        return 30
    return 60


def _infer_cost(model_id: str, capabilities: set) -> float:
    """Get blended cost per 1K tokens for dispatch priority.

    Checks MODEL_PRICING first, falls back to name-based estimate.
    """
    from lib import MODEL_PRICING
    pricing = MODEL_PRICING.get(model_id)
    if pricing:
        return round((pricing['input'] + pricing['output']) / 2.0 / 1000.0, 4)

    mid = model_id.lower()
    if 'embedding' in capabilities:
        return 0.001
    if 'image_gen' in capabilities:
        return 0.02
    if any(x in mid for x in ('nano', 'free')):
        return 0.001
    if any(x in mid for x in ('mini', 'small', 'lite', 'haiku', 'turbo')):
        return 0.002
    if any(x in mid for x in ('flash',)):
        return 0.003
    if any(x in mid for x in ('opus', 'large', 'max')):
        return 0.02
    return 0.005


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
    # to appending /models.  Gateways like YourProvider may use non-standard
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

    logger.info('[Discovery] Fetching models from %s', models_url)

    try:
        resp = requests.get(
            models_url,
            headers={
                'Authorization': 'Bearer %s' % api_key,
                'User-Agent': 'Tofu/1.0',
            },
            timeout=timeout,
            proxies=_proxies_for(models_url),
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
        resp = requests.get(
            'https://openrouter.ai/api/v1/models',
            timeout=20,
            headers={'User-Agent': 'Tofu/1.0'},
            proxies=_proxies_for('https://openrouter.ai/api/v1/models'),
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

        from lib.llm_dispatch.config import is_model_cheap

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
                caps = set(model['capabilities'])
                cheap = is_model_cheap(model['model_id'],
                                       input_price=inp_1m, output_price=out_1m)
                if cheap and 'cheap' not in caps:
                    caps.add('cheap')
                    model['capabilities'] = sorted(caps)
                elif not cheap and 'cheap' in caps:
                    caps.discard('cheap')
                    model['capabilities'] = sorted(caps)
                updated += 1

        logger.info('[Discovery] Enriched %d/%d models with OpenRouter pricing',
                   updated, len(models))

    except Exception as e:
        logger.warning('[Discovery] OpenRouter pricing enrichment failed: %s', e)

    return models


# ══════════════════════════════════════════════════════
#  Provider Brand / Name Auto-Detection
# ══════════════════════════════════════════════════════

# Mapping: domain fragment → (provider_id, display_name)
# All public domains — no internal/corp domains.
_DOMAIN_BRAND_MAP = [
    ('api.deepseek.com',            'deepseek',    'DeepSeek'),
    ('dashscope.aliyuncs.com',      'qwen',        'Qwen (DashScope)'),
    ('ark.cn-beijing.volces.com',   'doubao',      'Doubao (Volcengine)'),
    ('api.minimax.io',              'minimax',      'MiniMax'),
    ('api.minimaxi.com',            'minimax',      'MiniMax'),
    ('api.minimax.chat',            'minimax',      'MiniMax'),
    ('open.bigmodel.cn',            'glm',          'GLM (Zhipu AI)'),
    ('openrouter.ai',               'openrouter',   'OpenRouter'),
    ('api.x.ai',                    'grok',         'xAI (Grok)'),
    ('api.mistral.ai',              'mistral',      'Mistral AI'),
    ('siliconflow.cn',              'siliconflow',  'SiliconFlow'),
    ('api.moonshot.cn',             'kimi',         'Moonshot (Kimi)'),
    ('api.moonshot.ai',             'kimi',         'Moonshot (Kimi)'),
    ('api.baichuan-ai.com',         'baichuan',     'Baichuan'),
    ('api.stepfun.com',             'stepfun',      'StepFun (阶跃星辰)'),
    ('api.lingyiwanwu.com',         'yi',           'Yi (零一万物)'),
    ('generativelanguage.googleapis.com', 'gemini', 'Google Gemini'),
    ('api.anthropic.com',           'claude',       'Anthropic'),
    ('api.openai.com',              'openai',       'OpenAI'),
    ('yeysai.com',                  'tsinghua',     'YeysAI (Tsinghua)'),
    ('api.together.xyz',            'together',     'Together AI'),
    ('api.groq.com',                'groq',         'Groq'),
    ('api.fireworks.ai',            'fireworks',    'Fireworks AI'),
    ('api.perplexity.ai',           'perplexity',   'Perplexity'),
    ('api.cohere.ai',               'cohere',       'Cohere'),
    ('api.sambanova.ai',            'sambanova',    'SambaNova'),
    ('api.infini-ai.com',           'infini',       'Infini AI'),
    ('api.siliconflow.com',         'siliconflow',  'SiliconFlow'),
]


def _detect_brand(base_url: str) -> tuple[str, str]:
    """Detect provider brand and display name from base URL hostname.

    Args:
        base_url: Provider API base URL.

    Returns:
        Tuple of (brand_id, display_name). Falls back to cleaned hostname
        if no known brand is matched.
    """
    if not base_url:
        return ('generic', 'Custom Provider')

    try:
        hostname = urlparse(base_url).hostname or ''
    except Exception as e:
        logger.debug('[BrandDetect] Failed to parse URL %s: %s', base_url, e)
        hostname = ''

    hostname_lower = hostname.lower()

    # Check known domain patterns
    for domain_frag, brand_id, display_name in _DOMAIN_BRAND_MAP:
        if domain_frag in hostname_lower:
            return (brand_id, display_name)

    # Fallback: extract a reasonable name from the hostname
    # e.g. "my-llm-proxy.example.com" → "My Llm Proxy"
    parts = hostname.replace('api.', '').replace('www.', '').split('.')
    if len(parts) >= 2:
        name_part = parts[0] if parts[0] not in ('com', 'org', 'io', 'ai', 'cn') else parts[-2]
    elif parts:
        name_part = parts[0]
    else:
        name_part = 'custom'

    display = name_part.replace('-', ' ').replace('_', ' ').title()
    return ('generic', display)


# ══════════════════════════════════════════════════════
#  Balance URL Auto-Detection
# ══════════════════════════════════════════════════════

# Common billing/balance endpoint paths to probe (order: most common first)
_BALANCE_PROBE_PATHS = [
    '/dashboard/billing/subscription',
    '/v1/dashboard/billing/subscription',
    '/billing/credit/grants',
    '/user/balance',
    '/balance',
]

_BALANCE_PROBE_TIMEOUT = 5  # seconds per path


def _probe_balance_url(base_url: str, api_key: str) -> str:
    """Try common balance/billing URL patterns and return the first working one.

    Only sends GET requests with short timeouts. Returns empty string if
    no working balance endpoint is found.

    Args:
        base_url: Provider API base URL.
        api_key: API key for authorization.

    Returns:
        Working balance URL, or empty string.
    """
    parsed = urlparse(base_url.rstrip('/'))
    origin = '%s://%s' % (parsed.scheme, parsed.netloc)
    headers = {
        'Authorization': 'Bearer %s' % api_key,
        'User-Agent': 'Tofu/1.0',
    }

    for path in _BALANCE_PROBE_PATHS:
        probe_url = origin + path
        try:
            resp = requests.get(
                probe_url,
                headers=headers,
                timeout=_BALANCE_PROBE_TIMEOUT,
                proxies=_proxies_for(probe_url),
            )
            if resp.ok:
                # Verify it returns JSON (not an HTML error page)
                ct = resp.headers.get('content-type', '')
                if 'json' in ct:
                    try:
                        resp.json()
                        logger.info('[BalanceProbe] Found working balance URL: %s', probe_url)
                        return probe_url
                    except (ValueError, TypeError) as e:
                        logger.debug('[BalanceProbe] %s returned non-JSON body: %s', probe_url, e)
            logger.debug('[BalanceProbe] %s returned HTTP %d (skipped)', probe_url, resp.status_code)
        except requests.Timeout:
            logger.debug('[BalanceProbe] %s timed out', probe_url)
        except requests.RequestException as e:
            logger.debug('[BalanceProbe] %s failed: %s', probe_url, e)

    logger.info('[BalanceProbe] No working balance endpoint found for %s', origin)
    return ''


# ══════════════════════════════════════════════════════
#  Thinking Format Detection
# ══════════════════════════════════════════════════════

# Model name patterns → thinking format hint
_THINKING_FORMAT_HINTS = [
    # Doubao / Claude-style: thinking.type = "enabled"
    (re.compile(r'claude|anthropic', re.I),       'thinking_type'),
    (re.compile(r'doubao|seed.*pro', re.I),       'thinking_type'),
    # Qwen / Gemini / LongCat style: enable_thinking = true
    (re.compile(r'qwen|qwq', re.I),              'enable_thinking'),
    (re.compile(r'gemini', re.I),                 'enable_thinking'),
    (re.compile(r'longcat', re.I),                'enable_thinking'),
    # GLM (Zhipu AI): thinking.type format
    (re.compile(r'glm', re.I),                    'thinking_type'),
    # DeepSeek V4 (Apr 2026) uses thinking.type = "enabled" (dual-mode API).
    (re.compile(r'deepseek-v4', re.I),            'thinking_type'),
    # DeepSeek V3 reasoner was a separate model — no thinking param needed.
    (re.compile(r'deepseek-reasoner', re.I),      'none'),
]


def _detect_thinking_format(models: list[dict], brand: str) -> str:
    """Suggest the thinking_format for a provider based on its models and brand.

    Args:
        models: List of discovered model dicts.
        brand: Detected brand ID.

    Returns:
        Suggested thinking_format string, or '' for auto-detect.
    """
    # Brand-level overrides
    brand_map = {
        'claude': 'thinking_type',
        'doubao': 'thinking_type',
        'glm': 'thinking_type',
        'qwen': 'enable_thinking',
        'gemini': 'enable_thinking',
    }
    if brand in brand_map:
        return brand_map[brand]

    # Check model names for hints
    format_votes = {}
    for m in models:
        mid = m.get('model_id', '')
        for pat, fmt in _THINKING_FORMAT_HINTS:
            if pat.search(mid):
                format_votes[fmt] = format_votes.get(fmt, 0) + 1
                break

    if format_votes:
        # Return the most common format
        winner = max(format_votes, key=format_votes.get)
        if winner != 'none':
            return winner

    return ''  # auto-detect


# ══════════════════════════════════════════════════════
#  Full Provider Probe (one-shot setup)
# ══════════════════════════════════════════════════════

def probe_provider(base_url: str, api_key: str,
                   models_path: str = '') -> dict:
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
    with log_context('probe_provider', logger=logger, url=base_url):
        # ── Step 1: Detect brand from URL ──
        brand, name = _detect_brand(base_url)
        logger.info('[Probe] Brand detected: %s (%s) from %s', brand, name, base_url)

        # ── Step 2: Discover models ──
        models = discover_models(base_url, api_key, models_path=models_path)
        if not models:
            return {
                'ok': False,
                'error': '在 %s 未发现任何模型。请检查 API 地址和密钥是否正确。' % base_url,
                'brand': brand,
                'name': name,
            }

        logger.info('[Probe] Discovered %d models, enriching with pricing…', len(models))

        # ── Step 3: Enrich with OpenRouter pricing ──
        models = enrich_models_with_pricing(models)

        # ── Step 4: Detect thinking format ──
        thinking_format = _detect_thinking_format(models, brand)
        logger.info('[Probe] Thinking format suggestion: %s',
                   thinking_format or '(auto-detect)')

        # ── Step 5: Probe balance URL ──
        balance_url = _probe_balance_url(base_url, api_key)

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
            'models': models,
            'balance_url': balance_url,
            'thinking_format': thinking_format,
            'summary': summary,
        }
