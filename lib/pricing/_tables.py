"""
Pricing — static model pricing tables.

Holds the hardcoded fallback pricing constants:
    DEFAULT_USD_CNY_RATE  — USD→CNY conversion rate used across cost paths.
    DEFAULT_USD_DISPLAY_RATES — display-only currency units per USD.
    MODEL_PRICING         — {model_id: {input, output, cacheWriteMul, cacheReadMul, name}}
    QWEN_PRICING_CNY      — {model_id: {input: [(threshold, cny_price)], output: [...]}}

These are the single source of truth for the flat/tiered price tables; the
provider registry (._provider) and the online refresher (._refresh) both
read MODEL_PRICING from here by import.
"""

from types import MappingProxyType

from lib.log import get_logger
from lib.model_info._openai_gpt56 import gpt56_pricing_rows

logger = get_logger(__name__)



# ══════════════════════════════════════════════════════
#  Static Pricing Tables
# ══════════════════════════════════════════════════════

DEFAULT_USD_CNY_RATE = 7.24

# Offline display-only fallbacks for Settings price localization. Billing keeps
# using the declared model currency and DEFAULT_USD_CNY_RATE above; JPY/KRW are
# presentation pivots and are refreshed by the same bounded exchange-rate
# worker when online. Values are currency units per USD.
DEFAULT_USD_DISPLAY_RATES = MappingProxyType({
    'USD': 1.0,
    'CNY': DEFAULT_USD_CNY_RATE,
    'JPY': 159.0,
    'KRW': 1385.0,
})

# Source reference for the rounded JPY/KRW fallbacks (retrieved 2026-08-24):
# https://open.er-api.com/v6/latest/USD

# ── Canonical model pricing (USD per 1M tokens) — hardcoded fallback ──
# cacheWriteMul / cacheReadMul are multipliers of the base input price:
#   Anthropic Claude: write=1.25x, read=0.10x (5-min TTL)
#   OpenAI GPT-5.6:   write=1.25x, read=0.10x
#   DeepSeek:         write=1.00x, read=0.10x (disk cache)
MODEL_PRICING = {
    # ── Anthropic Fable 5 (creative flagship, May 2026) — Opus-tier pricing ──
    'fable-5':                   {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Fable 5'},
    'aws.fable-5':               {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Fable 5'},
    'us.anthropic.fable-5-v1:0': {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Fable 5 (Bedrock)'},
    # Meituan-gateway Fable 5 (Jul 2026 marketplace) — ¥72/¥360 per 1M.
    'claude-fable-5':            {'input': 9.94,  'output': 49.72, 'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Fable 5'},
    # ── Logical Claude ids (the model-identity contract) ──
    # These are LOGICAL names: what presets target, the picker shows, and
    # conversations persist. The gateway spellings they dispatch to live in the
    # template's ``request_ids`` (yuju-…-evaDaily / aws.…). Both channels need a
    # row: cost keys on the WIRE id, the picker's display name on the LOGICAL
    # one — see lib/llm_dispatch/model_entry.py.
    'claude-opus-5':             {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 5'},
    # Sonnet 5 (2026-06-30, 1M ctx native): PROMO $2/$10 until 2026-08-31,
    # then $3.0/$15.0 — update this row on 09-01.
    'claude-sonnet-5':           {'input': 2.0,   'output': 10.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 5'},
    'claude-opus-4.8':           {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.8'},
    'claude-opus-4.7':           {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.7'},
    'claude-opus-4.6':           {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.6'},
    'claude-sonnet-4.6':         {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4.6'},
    # Meituan-gateway yuju evaDaily models — raw gateway IDs stay on the wire; .name is the display channel.
    'yuju-claude-opus-5-evaDaily':   {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 5'},
    'yuju-claude-opus-4.8-evaDaily': {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.8'},
    'yuju-claude-opus-4.7-evaDaily': {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.7'},
    'aws.claude-opus-4.8':       {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.8'},
    'claude-opus-4-8':           {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.8'},
    'us.anthropic.claude-opus-4-8-v1:0':        {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.8 (Bedrock)'},
    'aws.claude-opus-4.7':       {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.7'},
    # Nova-04 deployment of the same model — an interchangeable member of the
    # claude-opus-4.7 wire pool, so it must price identically or cost books at $0.
    'aws.claude-opus-4.7-nova04': {'input': 5.0,  'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.7'},
    'claude-opus-4-7':           {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.7'},
    'us.anthropic.claude-opus-4-7-v1:0':        {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.7 (Bedrock)'},
    'us.anthropic.claude-opus-4-6-v1:0':        {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.6 (Bedrock)'},
    'us.anthropic.claude-sonnet-4-6-v1:0':      {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4.6 (Bedrock)'},
    'us.anthropic.claude-sonnet-4-5-v1:0':      {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4.5 (Bedrock)'},
    'us.anthropic.claude-opus-4-5-v1:0':        {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.5 (Bedrock)'},
    'us.anthropic.claude-haiku-4-5-v1:0':       {'input': 1.0,   'output': 5.0,   'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Haiku 4.5 (Bedrock)'},
    'us.anthropic.claude-sonnet-4-20250514-v1:0': {'input': 3.0, 'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4 (Bedrock)'},
    'openai.gpt-oss-120b-1:0':                  {'input': 0.15,  'output': 0.60,  'cacheWriteMul': 1.0,  'cacheReadMul': 0.50, 'name': 'GPT-OSS 120B (Bedrock)'},
    'openai.gpt-oss-20b-1:0':                   {'input': 0.07,  'output': 0.30,  'cacheWriteMul': 1.0,  'cacheReadMul': 0.50, 'name': 'GPT-OSS 20B (Bedrock)'},
    'aws.claude-opus-4.6':       {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.6'},
    'aws.claude-opus-4.6-b':    {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.6 (B)'},
    'vertex.claude-opus-4.6':   {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.6 (Vertex)'},
    'claude-opus-4-6-20250514':  {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.6'},
    'aws.claude-sonnet-4.6':     {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4.6'},
    'claude-sonnet-4-6-20250514':{'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4.6'},
    'claude-3-5-sonnet-20241022':{'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude 3.5 Sonnet'},
    'claude-3-opus-20240229':    {'input': 15.0,  'output': 75.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude 3 Opus'},
    'claude-3-5-haiku-20241022': {'input': 0.8,   'output': 4.0,   'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude 3.5 Haiku'},
    'gpt-4o':                    {'input': 2.5,   'output': 10.0,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.50, 'name': 'GPT-4o'},
    'gpt-4o-mini':               {'input': 0.15,  'output': 0.6,   'cacheWriteMul': 1.00, 'cacheReadMul': 0.50, 'name': 'GPT-4o Mini'},
    'gpt-4-turbo':               {'input': 10.0,  'output': 30.0,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.50, 'name': 'GPT-4 Turbo'},
    # RETIRED 2026-07-24 15:59 UTC (api-docs.deepseek.com/updates, 2026-04-24):
    # the legacy aliases 'deepseek-chat' (V3 non-thinking) and 'deepseek-reasoner'
    # (V3 thinking) now ERROR on the official API — their rows were removed.
    # Successors: deepseek-v4-flash / deepseek-v4-pro below. Do NOT re-add.
    # DeepSeek V4 (2026-04-24) — both models have 1M ctx, dual Thinking / Non-Thinking modes.
    # Pro: 75% price cut made permanent 2026-05-22 ($1.74/$3.48 → $0.435/$0.87); cached input $0.003625 → cacheReadMul ≈ 0.0083.
    # Flash cacheReadMul from disclosed cache-hit pricing: $0.028 / $0.14 = 0.20.
    # 'peak' (official API rows ONLY — not the gateway mirrors): DeepSeek announced
    # 2x pricing on ALL billing items during 09:00-12:00 + 14:00-18:00 Beijing time,
    # effective date TBA (pricing page, 2026-07-31). Ships INERT: set
    # effective_from (UTC epoch) when the date is announced. See lib/pricing/_peak.py.
    'deepseek-v4-pro':           {'input': 0.435, 'output': 0.87,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.0083, 'name': 'DeepSeek V4 Pro',
                                  'peak': {'mul': 2.0, 'windows': [(9, 12), (14, 18)], 'tz_offset': 8, 'effective_from': None}},
    'deepseek-v4-flash':         {'input': 0.14,  'output': 0.28,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.20,  'name': 'DeepSeek V4 Flash',
                                  'peak': {'mul': 2.0, 'windows': [(9, 12), (14, 18)], 'tz_offset': 8, 'effective_from': None}},
    # Meituan gateway Huawei-cloud mirror for DeepSeek V4 Flash — same pricing.
    'deepseek-v4-flash-huawei':  {'input': 0.14,  'output': 0.28,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.20,  'name': 'DeepSeek V4 Flash (Huawei)'},
    'deepseek-v3.2':             {'input': 0.28,  'output': 0.41,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'DeepSeek V3.2'},  # ¥2/¥3 per 1M
    # DeepSeek V3.2 mirrors on Meituan gateway — tiered ¥2/¥4 input, ¥4/¥6 output at 32K (cheapest tier in USD)
    'deepseek-v3.2-tencent':     {'input': 0.28,  'output': 0.55,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'DeepSeek V3.2 (Tencent)'},  # ¥2/¥4 per 1M ≤32K
    'deepseek-v3.2-baidu':       {'input': 0.28,  'output': 0.55,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'DeepSeek V3.2 (Baidu)'},    # ¥2/¥4 per 1M ≤32K
    'deepseek-v3.2-huawei':      {'input': 0.28,  'output': 0.55,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'DeepSeek V3.2 (Huawei)'},   # ¥2/¥4 per 1M ≤32K
    'deepseek-v3.2-doubao':      {'input': 0.28,  'output': 0.55,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'DeepSeek V3.2 (Doubao)'},   # ¥2/¥4 per 1M ≤32K
    'LongCat-Flash-Thinking-2601': {'input': 0.0, 'output': 0.0,  'cacheWriteMul': 0,    'cacheReadMul': 0,    'name': 'LongCat Flash'},
    'LongCat-Flash-Chat-2603':      {'input': 0.28,'output': 1.10, 'cacheWriteMul': 0,    'cacheReadMul': 0,    'name': 'LongCat Flash Chat'},  # ¥2/¥8 per 1M
    # ── Qwen (DashScope) — converted from CNY at 7.24 ──
    'qwen3.6-plus':              {'input': 0.28, 'output': 1.66, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen 3.6 Plus'},  # ¥2/¥12 per 1M (≤256K)
    'qwen3.5-plus':              {'input': 0.11, 'output': 0.66, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen 3.5 Plus'},  # ¥0.8/¥4.8 per 1M (≤128K)
    'qwen3.5-flash':             {'input': 0.03, 'output': 0.28, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen 3.5 Flash'},  # ¥0.2/¥2 per 1M (≤128K)
    'qwen3-max':                 {'input': 0.35, 'output': 1.38, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen3 Max'},  # ¥2.5/¥10 per 1M (≤32K)
    'qwen3-vl-plus':             {'input': 0.14, 'output': 1.38, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen3 VL Plus'},  # ¥1/¥10 per 1M (≤32K)
    'qwen3-vl-flash':            {'input': 0.02, 'output': 0.21, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen3 VL Flash'},  # ¥0.15/¥1.5 per 1M (≤32K)
    'qwen3-coder-plus':          {'input': 0.55, 'output': 2.21, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen3 Coder Plus'},  # ¥4/¥16 per 1M (≤32K)
    'qwen3-coder-flash':         {'input': 0.14, 'output': 0.55, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen3 Coder Flash'},  # ¥1/¥4 per 1M (≤32K)
    'qwen-plus':                 {'input': 0.11, 'output': 0.28, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen Plus'},  # ¥0.8/¥2 non-think, ¥8 think per 1M (≤128K)
    'qwen-max':                  {'input': 0.33, 'output': 1.33, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen Max'},  # ¥2.4/¥9.6 per 1M
    'qwen-flash':                {'input': 0.02, 'output': 0.21, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen Flash'},  # ¥0.15/¥1.5 per 1M (≤128K)
    'qwq-plus':                  {'input': 0.22, 'output': 0.55, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'QwQ Plus'},  # ¥1.6/¥4 per 1M
    'qvq-max':                   {'input': 1.10, 'output': 4.42, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'QVQ Max'},  # ¥8/¥32 per 1M
    'qvq-plus':                  {'input': 0.28, 'output': 0.69, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'QVQ Plus'},  # ¥2/¥5 per 1M
    'qwen-vl-max':               {'input': 0.22, 'output': 0.55, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen VL Max'},  # ¥1.6/¥4 per 1M
    'qwen-vl-plus':              {'input': 0.11, 'output': 0.28, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen VL Plus'},  # ¥0.8/¥2 per 1M
    'qwen-turbo':                {'input': 0.04, 'output': 0.08, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen Turbo'},  # ¥0.3/¥0.6 non-think, ¥3 think per 1M
    'qwen-long':                 {'input': 0.07, 'output': 0.28, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen Long'},  # ¥0.5/¥2 per 1M
    # ── Gemini ──
    'gemini-2.5-pro':            {'input': 1.25, 'output': 10.0, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 2.5 Pro'},
    'gemini-2.5-flash':          {'input': 0.15, 'output': 0.60, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 2.5 Flash'},
    'gemini-2.0-flash-lite':     {'input': 0.075,'output': 0.30, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 2.0 Flash-Lite'},
    'gemini-3.1-flash-lite-preview': {'input': 0.25, 'output': 1.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.1 Flash-Lite'},
    'gemini-3.1-pro-preview':    {'input': 2.00, 'output': 12.0, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.1 Pro'},
    'gemini-3-flash-preview':    {'input': 0.15, 'output': 0.60, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3 Flash'},
    'gemini-3.5-flash':          {'input': 1.49, 'output': 8.95, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.5 Flash'},  # ¥10.80/¥64.80 per 1M
    'gemini-3.6-flash':          {'input': 1.49, 'output': 7.46, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.6 Flash'},  # ¥10.80/¥54.00 per 1M
    # Intro pricing $0.75/$3.75 through 2026-12-31, then $1.50/$7.50 standard.
    'gemini-3.7-flash':          {'input': 0.75, 'output': 3.75, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.7 Flash'},
    'gemini-3.5-flash-lite':     {'input': 0.30, 'output': 2.49, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.5 Flash-Lite'},  # ¥2.16/¥18 per 1M
    'gemini-3.1-flash-image-preview': {'input': 0.25, 'output': 1.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.1 Flash Image'},
    'gemini-3-pro-image-preview':    {'input': 2.50, 'output': 12.0, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3 Pro Image'},
    'gemini-2.5-flash-image':        {'input': 0.15, 'output': 0.60, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 2.5 Flash Image'},
    'gemini-2.0-flash-preview-image-generation': {'input': 0.10, 'output': 0.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 2.0 Flash Image'},
    'gpt-image-1.5':                 {'input': 0.0,  'output': 0.0,  'cacheWriteMul': 0, 'cacheReadMul': 0, 'name': 'GPT Image 1.5'},
    # GPT Image 2 — token-priced (Meituan): text in ¥36/M ($4.97), image in ¥57.6/M ($7.96), image out ¥216/M ($29.83).
    'gpt-image-2':                   {'input': 4.97, 'output': 29.83,'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT Image 2'},
    'gpt-image-1':                   {'input': 0.0,  'output': 0.0,  'cacheWriteMul': 0, 'cacheReadMul': 0, 'name': 'GPT Image 1'},
    'gpt-image-1-mini':              {'input': 0.0,  'output': 0.0,  'cacheWriteMul': 0, 'cacheReadMul': 0, 'name': 'GPT Image 1 Mini'},
    'dall-e-3':                      {'input': 0.0,  'output': 0.0,  'cacheWriteMul': 0, 'cacheReadMul': 0, 'name': 'DALL-E 3'},
    # ── OpenAI GPT-5.6 public family ──
    # Includes the official >272K full-request pricing tier. Gateway-specific
    # prices (for example Meituan) remain provider overrides and win at runtime.
    **gpt56_pricing_rows(),
    # ── OpenAI (GPT-5.4 family — March 2026; kept as the cost tier) ──
    'gpt-5.4':                   {'input': 2.50, 'output': 15.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5.4'},
    'gpt-5.4-pro':               {'input': 30.0, 'output': 180.0, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5.4 Pro'},
    'gpt-5.4-mini':              {'input': 0.75, 'output': 4.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5.4 Mini'},
    'gpt-5.4-nano':              {'input': 0.20, 'output': 1.25, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5.4 Nano'},
    # Original GPT-5 family retired — see _slots.py comment.
    # ── OpenAI (o-series reasoning) ──
    'o3':                        {'input': 2.00, 'output': 8.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.50, 'name': 'o3'},
    'o4-mini':                   {'input': 1.10, 'output': 4.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.50, 'name': 'o4-mini'},
    'o3-mini':                   {'input': 1.10, 'output': 4.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.50, 'name': 'o3-mini'},
    # ── OpenAI (GPT-4 family — previous gen) ──
    'gpt-4.1':                   {'input': 2.00, 'output': 8.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.50, 'name': 'GPT-4.1'},
    'gpt-4.1-mini':              {'input': 0.40, 'output': 1.60, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'GPT-4.1 Mini'},
    'gpt-4.1-nano':              {'input': 0.10, 'output': 0.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'GPT-4.1 Nano'},
    # ── Anthropic (Claude 4.6 family — Feb 2026) ──
    'claude-opus-4-6':           {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.6'},
    'claude-sonnet-4-6':         {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4.6'},
    'claude-haiku-4-5':          {'input': 1.0,   'output': 5.0,   'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Haiku 4.5'},
    'claude-haiku-4-5-20251001': {'input': 1.0,   'output': 5.0,   'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Haiku 4.5'},
    # ── Anthropic (Claude 4.5 family) ──
    'claude-opus-4-5':           {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.5'},
    'claude-sonnet-4-5':         {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4.5'},
    # ── Anthropic (Claude 4 — legacy) ──
    'claude-opus-4-20250514':    {'input': 15.0,  'output': 75.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4'},
    'claude-sonnet-4-20250514':  {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4'},
    # ── MiniMax ──
    # M3 (2026-06-01) — new flagship: MSA sparse attn, 1M ctx, native multimodal (image+video in).
    # Standard rate $0.60/$2.40; launch promo halves it to $0.30/$1.20 (temporary — not stored).
    'MiniMax-M3':                {'input': 0.60, 'output': 2.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M3'},
    'MiniMax-M2':                {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2'},
    'MiniMax-M2.1':              {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.1'},
    'MiniMax-M2.1-highspeed':    {'input': 0.30, 'output': 2.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.1 HS'},
    'MiniMax-M2.5':              {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.5'},
    'MiniMax-M2.5-highspeed':    {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.5 HS'},
    'MiniMax-M2.7':              {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.7'},
    'MiniMax-M2.7-highspeed':    {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.7 HS'},
    'M2-her':                    {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2-her'},
    # ── GLM (Zhipu AI) — converted from CNY at 7.24 ──
    'glm-5.3':                   {'input': 3.45, 'output': 13.81, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-5.3'},  # 1M context; registered billable rates, not a routing hint
    'glm-5.2':                   {'input': 3.45, 'output': 13.81, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-5.2'},  # 1M context, 128K output, text-only thinking flagship (2026-06-13)
    'glm-5.1':                   {'input': 3.45, 'output': 13.81, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-5.1'},
    'glm-5.1-huawei':            {'input': 3.45, 'output': 13.81, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-5.1 (Huawei)'},
    'glm-5':                     {'input': 3.45, 'output': 13.81, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-5'},
    'glm-5v-turbo':              {'input': 0.69, 'output': 3.04, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-5V Turbo'},
    'glm-4.7':                   {'input': 0.69, 'output': 0.69, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-4.7'},
    'glm-4.5-air':               {'input': 0.28, 'output': 1.10, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-4.5 Air'},
    'glm-4.5-flash':             {'input': 0.0,  'output': 0.0,  'cacheWriteMul': 0,    'cacheReadMul': 0,    'name': 'GLM-4.5 Flash'},
    # ── Doubao (Volcengine) — converted from CNY at 7.24 ──
    'Doubao-Seed-2.0-pro':       {'input': 0.55, 'output': 2.21, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Doubao Seed 2.0 Pro'},
    'Doubao-Seed-2.0-lite':      {'input': 0.04, 'output': 0.14, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Doubao Seed 2.0 Lite'},
    'Doubao-Seed-2.0-mini':      {'input': 0.02, 'output': 0.06, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Doubao Seed 2.0 Mini'},
    # ── Mistral AI ──
    'mistral-large-latest':      {'input': 2.00, 'output': 6.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Mistral Large'},
    'mistral-small-latest':      {'input': 0.10, 'output': 0.30, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Mistral Small'},
    'codestral-latest':          {'input': 0.30, 'output': 0.90, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Codestral'},
    # ── xAI (Grok) ──
    'grok-3':                    {'input': 3.00, 'output': 15.0, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Grok 3'},
    'grok-3-mini':               {'input': 0.30, 'output': 0.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Grok 3 Mini'},
    # ── Moonshot (Kimi) — per OpenRouter (2026-04-20 release) ──
    # kimi-k3 (2026-07-17): ¥20/¥100 per 1M → $2.76/$13.81 @ 7.24; 1M context
    'kimi-k3':                   {'input': 2.76, 'output': 13.81,'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Kimi K3'},
    'kimi-k2.6':                 {'input': 0.60, 'output': 2.80, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Kimi K2.6'},
    # ── Tencent Hunyuan ── hy4-preview (2026-08-28): ¥6/¥18 per 1M, cache hit ¥0.3 → $0.829/$2.486 @ 7.24
    'hy4-preview':               {'input': 0.829,'output': 2.486,'cacheWriteMul': 1.00, 'cacheReadMul': 0.05, 'name': 'Hunyuan HY4 Preview'},
    # ── Tencent Hunyuan ── cheapest tier (≤16K): ¥1.2/¥4 per 1M = $0.166/$0.553 @ 7.24
    'hy3-preview':               {'input': 0.166,'output': 0.553,'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Hunyuan HY3 Preview'},
}

# ── Qwen tiered pricing (CNY per 1M tokens) ──
# The MODEL_PRICING above uses the cheapest tier converted to USD.
# For precise CNY cost, use these per-model tiers directly:
QWEN_PRICING_CNY = {
    'qwen3.6-plus':     {'input': [(256_000, 2.0), (1_000_000, 8.0)],       'output': [(256_000, 12.0), (1_000_000, 48.0)]},
    'qwen3.5-plus':     {'input': [(128_000, 0.8), (256_000, 2.0), (1_000_000, 4.0)], 'output': [(128_000, 4.8), (256_000, 12.0), (1_000_000, 24.0)]},
    'qwen3.5-flash':    {'input': [(128_000, 0.2), (256_000, 0.8), (1_000_000, 1.2)], 'output': [(128_000, 2.0), (256_000, 8.0), (1_000_000, 12.0)]},
    'qwen3-max':        {'input': [(32_000, 2.5), (128_000, 4.0), (252_000, 7.0)],    'output': [(32_000, 10.0), (128_000, 16.0), (252_000, 28.0)]},
    'qwen-plus':        {'input': [(128_000, 0.8), (256_000, 2.4), (1_000_000, 4.8)], 'output': [(128_000, 2.0), (256_000, 20.0), (1_000_000, 48.0)]},
    'qwen-flash':       {'input': [(128_000, 0.15), (256_000, 0.6), (1_000_000, 1.2)], 'output': [(128_000, 1.5), (256_000, 6.0), (1_000_000, 12.0)]},
    'qwen3-vl-plus':    {'input': [(32_000, 1.0), (128_000, 1.5), (256_000, 3.0)],    'output': [(32_000, 10.0), (128_000, 15.0), (256_000, 30.0)]},
    'qwen3-vl-flash':   {'input': [(32_000, 0.15), (128_000, 0.3), (256_000, 0.6)],   'output': [(32_000, 1.5), (128_000, 3.0), (256_000, 6.0)]},
    'qwen3-coder-plus': {'input': [(32_000, 4.0), (128_000, 6.0), (256_000, 10.0), (1_000_000, 20.0)], 'output': [(32_000, 16.0), (128_000, 24.0), (256_000, 40.0), (1_000_000, 200.0)]},
    'qwen3-coder-flash':{'input': [(32_000, 1.0), (128_000, 1.5), (256_000, 2.5), (1_000_000, 5.0)],  'output': [(32_000, 4.0), (128_000, 6.0), (256_000, 10.0), (1_000_000, 25.0)]},
    'qwq-plus':         {'input': [(1_000_000, 1.6)],  'output': [(1_000_000, 4.0)]},
    'qvq-max':          {'input': [(1_000_000, 8.0)],  'output': [(1_000_000, 32.0)]},
    'qvq-plus':         {'input': [(1_000_000, 2.0)],  'output': [(1_000_000, 5.0)]},
    'qwen-max':         {'input': [(1_000_000, 2.4)],  'output': [(1_000_000, 9.6)]},
    'qwen-turbo':       {'input': [(1_000_000, 0.3)],  'output': [(1_000_000, 0.6)]},
    'qwen-long':        {'input': [(1_000_000, 0.5)],  'output': [(1_000_000, 2.0)]},
    'qwen-vl-max':      {'input': [(1_000_000, 1.6)],  'output': [(1_000_000, 4.0)]},
    'qwen-vl-plus':     {'input': [(1_000_000, 0.8)],  'output': [(1_000_000, 2.0)]},
    'deepseek-v3.2':    {'input': [(1_000_000, 2.0)],  'output': [(1_000_000, 3.0)]},
    # Meituan mirrors: ¥2/¥4 input, ¥4/¥6 output, split at 32K context
    'deepseek-v3.2-tencent': {'input': [(32_000, 2.0), (1_000_000, 4.0)], 'output': [(32_000, 4.0), (1_000_000, 6.0)]},
    'deepseek-v3.2-baidu':   {'input': [(32_000, 2.0), (1_000_000, 4.0)], 'output': [(32_000, 4.0), (1_000_000, 6.0)]},
    'deepseek-v3.2-huawei':  {'input': [(32_000, 2.0), (1_000_000, 4.0)], 'output': [(32_000, 4.0), (1_000_000, 6.0)]},
    'deepseek-v3.2-doubao':  {'input': [(32_000, 2.0), (1_000_000, 4.0)], 'output': [(32_000, 4.0), (1_000_000, 6.0)]},
    # DeepSeek V4 gateway mirrors use provider-qualified model ids.  The plain
    # official ids live in MODEL_PRICING above; repeating them here would make
    # the legacy CNY compatibility layer overwrite the official USD rows.
    'deepseek-v4-flash-huawei': {'input': [(1_000_000, 1.0)],  'output': [(1_000_000, 2.0)]},
    # Tencent Hunyuan HY3 — tiered pricing on 16K / 32K / 256K context boundaries
    'hy3-preview':              {'input': [(16_000, 1.2), (32_000, 1.6), (256_000, 2.0)],
                                 'output': [(16_000, 4.0), (32_000, 6.4), (256_000, 8.0)]},
    # Tencent Hunyuan HY4 Preview — flat ¥6/¥18 per 1M regardless of context
    'hy4-preview':              {'input': [(1_000_000, 6.0)],  'output': [(1_000_000, 18.0)]},
    'deepseek-r1':      {'input': [(1_000_000, 4.0)],  'output': [(1_000_000, 16.0)]},
    'glm-5v-turbo':     {'input': [(32_000, 5.0), (1_000_000, 7.0)],  'output': [(32_000, 22.0), (1_000_000, 26.0)]},
    '_default':         {'input': [(128_000, 0.8), (256_000, 2.0), (1_000_000, 4.0)], 'output': [(128_000, 4.8), (256_000, 12.0), (1_000_000, 24.0)]},
}
