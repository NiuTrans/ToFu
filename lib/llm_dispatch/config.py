"""lib/llm_dispatch/config.py — Default slot configurations and model aliases.

Contains the static configuration tables that seed the slot pool before
benchmark data is loaded.  These are **reference tables** — they describe
known model capabilities / RPM / cost metadata so that *any* configured
model benefits from pre-seeded data.  They do NOT control which models
are "active" — that is driven entirely by the Settings UI providers
(server_config.json) or legacy env-var config.
"""

__all__ = [
    'DEFAULT_SLOT_CONFIGS',
    'MODEL_ALIASES',
    'MODEL_ALIAS_GROUPS',
    'CHEAP_BLENDED_THRESHOLD',
    'is_model_cheap',
]

# ── Auto-cheap pricing threshold ──────────────────────────────
# A model is 'cheap' if BOTH its input price AND output price are strictly
# below Sonnet's.  This prevents models that are expensive on one dimension
# (e.g. high output price) from being misclassified as cheap.
# Reference: Claude Sonnet 4.6 — input $3.0/1M, output $15.0/1M.
CHEAP_INPUT_THRESHOLD = 3.0   # USD per 1M tokens (= Sonnet input)
CHEAP_OUTPUT_THRESHOLD = 15.0  # USD per 1M tokens (= Sonnet output)

# Legacy blended threshold kept for fallback when only a single 'cost'
# number ($/1K blended) is available and we can't split input vs output.
CHEAP_BLENDED_THRESHOLD = 9.0  # USD per 1M tokens


def is_model_cheap(model_id: str, fallback_cost_per_1k: float = None,
                   input_price: float = None, output_price: float = None) -> bool:
    """Check whether *model_id* qualifies as 'cheap'.

    A model is cheap if its input price < Sonnet input ($3/1M) AND its output
    price < Sonnet output ($15/1M).

    Lookup order:
      1. Explicit *input_price* / *output_price* args (from discovery enrichment).
      2. ``MODEL_PRICING[model_id]`` → use stored input / output.
      3. *fallback_cost_per_1k* → assume symmetric pricing and compare blended.

    Args:
        model_id: Model identifier to look up in MODEL_PRICING.
        fallback_cost_per_1k: Optional simplified cost in $/1K tokens used
            when *model_id* has no entry in MODEL_PRICING.
        input_price: Explicit input price in $/1M tokens (overrides lookup).
        output_price: Explicit output price in $/1M tokens (overrides lookup).

    Returns:
        True if the model is cheaper than Sonnet on both input and output.
    """
    from lib import MODEL_PRICING

    inp = input_price
    out = output_price

    # Try MODEL_PRICING if explicit prices not given
    if inp is None or out is None:
        pricing = MODEL_PRICING.get(model_id)
        if pricing:
            inp = pricing.get('input', 0)
            out = pricing.get('output', 0)

    # If we have both input and output, do the proper two-sided check
    if inp is not None and out is not None:
        return inp < CHEAP_INPUT_THRESHOLD and out < CHEAP_OUTPUT_THRESHOLD

    # Fallback: only blended cost available — convert $/1K → $/1M
    if fallback_cost_per_1k is not None and fallback_cost_per_1k > 0:
        blended_1m = fallback_cost_per_1k * 1000.0
        return blended_1m <= CHEAP_BLENDED_THRESHOLD

    return False


# ══════════════════════════════════════════════════════════════
#  Default slot configs: model → {caps, rpm, latency, cost}
#  Comprehensive reference table — any model that might be configured
#  via the Settings UI benefits from having a pre-seeded entry here.
#  These are overridden by benchmark data at runtime.
# ══════════════════════════════════════════════════════════════
DEFAULT_SLOT_CONFIGS = {
    # ── Claude (Anthropic — current gen: 4.7 flagship, Apr 2026) ──
    'claude-opus-4-7':               {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    # ── Claude (Anthropic — 4.6 family, Feb 2026) ──
    'claude-opus-4-6':               {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    'claude-sonnet-4-6':             {'caps': {'text', 'vision', 'thinking'},      'rpm': 50,  'latency': 2000, 'cost': 0.015},
    'claude-haiku-4-5':              {'caps': {'text', 'vision', 'cheap'},         'rpm': 100, 'latency': 1500, 'cost': 0.005},
    'claude-haiku-4-5-20251001':     {'caps': {'text', 'vision', 'cheap'},         'rpm': 100, 'latency': 1500, 'cost': 0.005},
    # ── Claude (Anthropic — 4.5 family) ──
    'claude-opus-4-5':               {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.025},
    'claude-sonnet-4-5':             {'caps': {'text', 'vision', 'thinking'},      'rpm': 50,  'latency': 2000, 'cost': 0.015},
    # ── Claude (Anthropic — legacy: 4.0 and earlier) ──
    'claude-opus-4-20250514':        {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.045},
    'claude-sonnet-4-20250514':      {'caps': {'text', 'vision', 'thinking'},      'rpm': 50,  'latency': 2000, 'cost': 0.009},
    'claude-3-5-sonnet-20241022':    {'caps': {'text', 'vision'},                  'rpm': 50,  'latency': 2000, 'cost': 0.009},
    'claude-3-opus-20240229':        {'caps': {'text', 'vision'},                  'rpm': 30,  'latency': 5000, 'cost': 0.045},
    'claude-3-5-haiku-20241022':     {'caps': {'text', 'cheap'},                   'rpm': 100, 'latency': 1500, 'cost': 0.003},

    # ── Claude (AWS / Vertex gateway-prefixed names) ──
    'aws.claude-opus-4.7':           {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    # ── Claude (Amazon Bedrock native model IDs, inference-profile form) ──
    'us.anthropic.claude-opus-4-7-v1:0':         {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    'us.anthropic.claude-opus-4-6-v1:0':         {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    'us.anthropic.claude-sonnet-4-6-v1:0':       {'caps': {'text', 'vision', 'thinking'},      'rpm': 50,  'latency': 2000, 'cost': 0.009},
    'us.anthropic.claude-sonnet-4-5-v1:0':       {'caps': {'text', 'vision', 'thinking'},      'rpm': 50,  'latency': 2000, 'cost': 0.009},
    'us.anthropic.claude-opus-4-5-v1:0':         {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    'us.anthropic.claude-haiku-4-5-v1:0':        {'caps': {'text', 'vision', 'cheap'},         'rpm': 100, 'latency': 1500, 'cost': 0.003},
    'us.anthropic.claude-sonnet-4-20250514-v1:0':{'caps': {'text', 'vision', 'thinking'},      'rpm': 50,  'latency': 2000, 'cost': 0.009},
    'openai.gpt-oss-120b-1:0':                   {'caps': {'text', 'thinking', 'cheap'},       'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'openai.gpt-oss-20b-1:0':                    {'caps': {'text', 'cheap'},                   'rpm': 120, 'latency': 1500, 'cost': 0.0005},
    'aws.claude-opus-4.6':           {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.025},
    'aws.claude-opus-4.6-b':         {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.025},
    'vertex.claude-opus-4.6':        {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.025},
    'aws.claude-sonnet-4.6':         {'caps': {'text', 'vision', 'thinking'},      'rpm': 50,  'latency': 2000, 'cost': 0.015},
    'vertex.claude-sonnet-4.6':      {'caps': {'text', 'vision', 'thinking'},      'rpm': 50,  'latency': 2000, 'cost': 0.015},

    # ── OpenAI (GPT-5.4 family — March 2026) ──
    'gpt-5.4':                       {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 3000, 'cost': 0.015},
    'gpt-5.4-pro':                   {'caps': {'text', 'vision', 'thinking'},      'rpm': 10,  'latency': 10000,'cost': 0.180},
    'gpt-5.4-mini':                  {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 60,  'latency': 2000, 'cost': 0.005},
    'gpt-5.4-nano':                  {'caps': {'text', 'vision', 'cheap'},         'rpm': 200, 'latency': 1000, 'cost': 0.001},
    # ── OpenAI (GPT-5 family) ──
    'gpt-5':                         {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 30,  'latency': 3000, 'cost': 0.010},
    'gpt-5.2':                       {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 30,  'latency': 3000, 'cost': 0.014},
    'gpt-5-mini':                    {'caps': {'text', 'vision', 'cheap'},         'rpm': 60,  'latency': 2000, 'cost': 0.002},
    'gpt-5-nano':                    {'caps': {'text', 'cheap'},                   'rpm': 200, 'latency': 1000, 'cost': 0.001},
    # ── OpenAI (o-series reasoning) ──
    'o3':                            {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 30,  'latency': 5000, 'cost': 0.010},
    'o4-mini':                       {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 30,  'latency': 3000, 'cost': 0.005},
    'o3-mini':                       {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 30,  'latency': 3000, 'cost': 0.005},
    # ── OpenAI (GPT-4 family — previous gen) ──
    'gpt-4o':                        {'caps': {'text', 'vision', 'cheap'},         'rpm': 60,  'latency': 2000, 'cost': 0.005},
    'gpt-4o-mini':                   {'caps': {'text', 'vision', 'cheap'},         'rpm': 200, 'latency': 1500, 'cost': 0.001},
    'gpt-4-turbo':                   {'caps': {'text', 'vision'},                  'rpm': 30,  'latency': 3000, 'cost': 0.020},
    'gpt-4.1':                       {'caps': {'text', 'vision', 'cheap'},         'rpm': 30,  'latency': 3000, 'cost': 0.010},
    'gpt-4.1-mini':                  {'caps': {'text', 'vision', 'cheap'},         'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'gpt-4.1-nano':                  {'caps': {'text', 'cheap'},                   'rpm': 200, 'latency': 1000, 'cost': 0.001},

    # ── DeepSeek ──
    'deepseek-chat':                 {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'deepseek-v3.2':                 {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'deepseek-reasoner':             {'caps': {'text', 'thinking', 'cheap'},      'rpm': 30,  'latency': 3000, 'cost': 0.002, 'stream_only': True},

    # ── Gemini ──
    'gemini-2.5-pro':                {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 100, 'latency': 2000, 'cost': 0.005},
    'gemini-2.5-flash':              {'caps': {'text', 'vision', 'cheap'},         'rpm': 200, 'latency': 1500, 'cost': 0.001},
    'gemini-2.0-flash-lite':         {'caps': {'text', 'cheap'},                   'rpm': 200, 'latency': 1000, 'cost': 0.001},
    'gemini-3.1-flash-lite-preview': {'caps': {'text', 'vision', 'cheap'},         'rpm': 30,  'latency': 1500, 'cost': 0.001},
    'gemini-3.1-pro-preview':        {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 5,   'latency': 3000, 'cost': 0.006},
    'gemini-3-flash-preview':        {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 60,  'latency': 1500, 'cost': 0.001},

    # ── Qwen (DashScope) ──
    'qwen3.6-plus':                  {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 60, 'latency': 2000, 'cost': 0.002},
    'qwen3.5-plus':                  {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 60, 'latency': 2000, 'cost': 0.001},
    'qwen3.5-flash':                 {'caps': {'text', 'thinking', 'cheap'},       'rpm': 120, 'latency': 1500, 'cost': 0.001},
    'qwen3-max':                     {'caps': {'text', 'thinking', 'cheap'},      'rpm': 30,  'latency': 3000, 'cost': 0.004},
    'qwen3-vl-plus':                 {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 30, 'latency': 3000, 'cost': 0.002},
    'qwen3-vl-flash':                {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 60, 'latency': 2000, 'cost': 0.001},
    'qwen3-coder-plus':              {'caps': {'text', 'thinking', 'cheap'},      'rpm': 30,  'latency': 3000, 'cost': 0.004},
    'qwen3-coder-flash':             {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.002},
    'qwen-plus':                     {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.002},
    'qwen-max':                      {'caps': {'text', 'cheap'},                  'rpm': 30,  'latency': 3000, 'cost': 0.004},
    'qwen-flash':                    {'caps': {'text', 'thinking', 'cheap'},       'rpm': 120, 'latency': 1500, 'cost': 0.001},
    'qwq-plus':                      {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.002, 'stream_only': True},
    'qvq-max':                       {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 30, 'latency': 3000, 'cost': 0.006},
    'qvq-plus':                      {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 60, 'latency': 2000, 'cost': 0.002},
    'qwen-vl-max':                   {'caps': {'text', 'vision', 'cheap'},        'rpm': 30,  'latency': 3000, 'cost': 0.002},
    'qwen-vl-plus':                  {'caps': {'text', 'vision', 'cheap'},         'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'qwen-turbo':                    {'caps': {'text', 'cheap'},                   'rpm': 200, 'latency': 1000, 'cost': 0.001},
    'qwen-long':                     {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.001},

    # ── MiniMax ──
    'MiniMax-M2':                    {'caps': {'text', 'vision', 'cheap'},        'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'MiniMax-M2.1':                  {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'MiniMax-M2.1-highspeed':        {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 1500, 'cost': 0.002},
    'MiniMax-M2.5':                  {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'MiniMax-M2.5-highspeed':        {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'MiniMax-M2.7':                  {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'MiniMax-M2.7-highspeed':        {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'M2-her':                        {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.001},

    # ── Doubao (Volcengine) ──
    'Doubao-Seed-2.0-pro':           {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 60, 'latency': 2000, 'cost': 0.002},
    'Doubao-Seed-2.0-lite':          {'caps': {'text', 'cheap'},                   'rpm': 120, 'latency': 1500, 'cost': 0.001},
    'Doubao-Seed-2.0-mini':          {'caps': {'text', 'cheap'},                   'rpm': 200, 'latency': 1000, 'cost': 0.001},

    # ── GLM (Zhipu AI) ──
    'glm-5.1':                       {'caps': {'text', 'thinking'},                'rpm': 60,  'latency': 3000, 'cost': 0.004},
    'glm-5':                         {'caps': {'text', 'thinking'},                'rpm': 60,  'latency': 3000, 'cost': 0.004},
    'glm-4.7':                       {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.002},
    'glm-4.5-air':                   {'caps': {'text', 'cheap'},                   'rpm': 120, 'latency': 1500, 'cost': 0.001},
    'glm-4.5-flash':                 {'caps': {'text', 'cheap'},                   'rpm': 200, 'latency': 1000, 'cost': 0.0},
    'glm-5v-turbo':                  {'caps': {'text', 'vision', 'cheap'},         'rpm': 60,  'latency': 2000, 'cost': 0.002},

    # ── Mistral AI ──
    'mistral-large-latest':          {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 30, 'latency': 3000, 'cost': 0.008},
    'mistral-small-latest':          {'caps': {'text', 'cheap'},                   'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'codestral-latest':              {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.003},

    # ── xAI (Grok) ──
    'grok-3':                        {'caps': {'text', 'thinking'},                'rpm': 30,  'latency': 3000, 'cost': 0.010},
    'grok-3-mini':                   {'caps': {'text', 'thinking', 'cheap'},       'rpm': 60,  'latency': 2000, 'cost': 0.003},

    # ── LongCat (internal, free) ──
    'LongCat-Flash-Thinking-2601':   {'caps': {'text', 'thinking', 'cheap'},       'rpm': 60,  'latency': 2000, 'cost': 0.0},
    'LongCat-Flash-Chat-2603':       {'caps': {'text', 'cheap'},                   'rpm': 60,  'latency': 1500, 'cost': 0.001},
    'longcat-pro-0403':              {'caps': {'text', 'thinking', 'cheap'},       'rpm': 60,  'latency': 2000, 'cost': 0.0},

    # ── OpenAI Codex (ChatGPT Plus subscription) ──
    'gpt-5.2-codex':                 {'caps': {'text', 'vision', 'thinking'},      'rpm': 10,  'latency': 5000, 'cost': 0.0},
    'gpt-5.1-codex-mini':            {'caps': {'text', 'vision', 'thinking'},      'rpm': 20,  'latency': 3000, 'cost': 0.0},
    'codex-mini':                    {'caps': {'text', 'vision', 'cheap'},         'rpm': 20,  'latency': 2000, 'cost': 0.0},

    # ── Image generation ──
    'gpt-image-1.5':                         {'caps': {'image_gen'},               'rpm': 10,  'latency': 30000, 'cost': 0.043},
    'gpt-image-1':                           {'caps': {'image_gen'},               'rpm': 10,  'latency': 30000, 'cost': 0.040},
    'gpt-image-1-mini':                      {'caps': {'image_gen'},               'rpm': 15,  'latency': 20000, 'cost': 0.015},
    'dall-e-3':                              {'caps': {'image_gen'},               'rpm': 5,   'latency': 30000, 'cost': 0.040},
    'gemini-3.1-flash-image-preview':        {'caps': {'image_gen'},               'rpm': 10,  'latency': 30000, 'cost': 0.015},
    'gemini-3-pro-image-preview':            {'caps': {'image_gen'},               'rpm': 10,  'latency': 30000, 'cost': 0.020},
    'gemini-2.5-flash-image':                {'caps': {'image_gen'},               'rpm': 10,  'latency': 30000, 'cost': 0.015},
    'gemini-2.0-flash-preview-image-generation': {'caps': {'image_gen'},           'rpm': 10,  'latency': 30000, 'cost': 0.010},

    # ── Embeddings ──
    'text-embedding-v4':             {'caps': {'embedding'},                       'rpm': 100, 'latency': 500,  'cost': 0.001},
    'text-embedding-3-small':        {'caps': {'embedding'},                       'rpm': 60,  'latency': 500,  'cost': 0.001},
    'text-embedding-3-large':        {'caps': {'embedding'},                       'rpm': 60,  'latency': 500,  'cost': 0.001},
}


# ══════════════════════════════════════════════════════════════
#  Model alias groups: models within the same group are interchangeable
#  When prefer_model is one of these, any model in the same group is "preferred".
#  This benefits anyone routing Claude through multiple gateway prefixes.
# ══════════════════════════════════════════════════════════════
MODEL_ALIAS_GROUPS = [
    # Claude Opus 4.7 — aws gateway + direct API + Bedrock-native IDs are interchangeable
    {'aws.claude-opus-4.7', 'claude-opus-4-7', 'us.anthropic.claude-opus-4-7-v1:0'},
    # Claude Opus 4.6 — aws, vertex, direct API, Bedrock-native names are interchangeable
    {'aws.claude-opus-4.6', 'aws.claude-opus-4.6-b', 'vertex.claude-opus-4.6',
     'claude-opus-4-20250514', 'claude-opus-4-6-20250514', 'claude-opus-4-6',
     'us.anthropic.claude-opus-4-6-v1:0'},
    # Claude Sonnet 4.6 — aws gateway vs direct API name vs Bedrock-native
    {'aws.claude-sonnet-4.6', 'claude-sonnet-4-20250514', 'claude-sonnet-4-6-20250514',
     'claude-sonnet-4-6', 'us.anthropic.claude-sonnet-4-6-v1:0'},
]

MODEL_ALIASES: dict[str, set[str]] = {}
for _group in MODEL_ALIAS_GROUPS:
    for _m in _group:
        MODEL_ALIASES[_m] = _group
