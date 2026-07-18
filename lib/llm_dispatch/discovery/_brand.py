"""lib/llm_dispatch/discovery/_brand.py — Provider brand / name auto-detection."""

import ipaddress
from urllib.parse import urlparse

from lib.log import get_logger

from ._url import is_local_endpoint

logger = get_logger(__name__)


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
        parsed = urlparse(base_url)
        hostname = parsed.hostname or ''
        port = parsed.port
    except Exception as e:
        logger.debug('[BrandDetect] Failed to parse URL %s: %s', base_url, e)
        hostname = ''
        port = None

    hostname_lower = hostname.lower()

    # Self-hosted (vLLM / SGLang / Ollama / proxy) — keyed by host:port so
    # multiple machines hosting different models stay distinguishable.
    if is_local_endpoint(base_url):
        label = '%s:%d' % (hostname, port) if port else hostname
        return ('local', 'Local %s' % label)

    # Check known domain patterns
    for domain_frag, brand_id, display_name in _DOMAIN_BRAND_MAP:
        if domain_frag in hostname_lower:
            return (brand_id, display_name)

    # Raw IP fallback — keep the full address so users can tell endpoints apart.
    try:
        ipaddress.ip_address(hostname)
        is_ip = True
    except ValueError as _e_audit:
        logger.debug('[discovery] _detect_brand caught %s: %s', type(_e_audit).__name__, _e_audit)
        is_ip = False
    if is_ip:
        label = '%s:%d' % (hostname, port) if port else hostname
        return ('generic', label)

    # Fallback: extract a reasonable name from the hostname
    # e.g. "my-llm-proxy.example.com" → "My Llm Proxy"
    parts = hostname.replace('api.', '').replace('www.', '').split('.')
    if len(parts) >= 2:
        name_part = parts[0] if parts[0] not in ('com', 'org', 'io', 'ai', 'cn') else parts[-2]
    else:
        name_part = 'custom'

    display = name_part.replace('-', ' ').replace('_', ' ').title()
    return ('generic', display)
