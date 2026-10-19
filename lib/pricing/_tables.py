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

import re

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
    # ── Anthropic Fable 5.1 (GA 2026-09-01) — $10/$50 per 1M, same as Fable 5 list;
    # cache reads cut 75%: $0.25/1M → cacheReadMul 0.025. Official API id: claude-fable-5-1.
    'fable-5.1':                   {'input': 10.0,  'output': 50.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.025, 'name': 'Fable 5.1'},
    'aws.fable-5.1':               {'input': 10.0,  'output': 50.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.025, 'name': 'Fable 5.1'},
    'us.anthropic.fable-5-1-v1:0': {'input': 10.0,  'output': 50.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.025, 'name': 'Fable 5.1 (Bedrock)'},
    'claude-fable-5-1':            {'input': 10.0,  'output': 50.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.025, 'name': 'Fable 5.1'},
    # ── Logical Claude ids (the model-identity contract) ──
    # These are LOGICAL names: what presets target, the picker shows, and
    # conversations persist. The gateway spellings they dispatch to live in the
    # template's ``request_ids`` (yuju-…-evaDaily / aws.…). Both channels need a
    # row: cost keys on the WIRE id, the picker's display name on the LOGICAL
    # one — see lib/llm_dispatch/model_entry.py.
    # Opus 5.5 (GA 2026-09-22): $4/$20 per 1M; 5m cache write $5, 1h write $8,
    # cache read $0.20. Adaptive thinking is always on (cannot be disabled).
    'claude-opus-5.5':           {'input': 4.0,   'output': 20.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.05, 'name': 'Claude Opus 5.5'},
    'claude-opus-5-5':           {'input': 4.0,   'output': 20.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.05, 'name': 'Claude Opus 5.5'},
    'claude-opus-5':             {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 5'},
    # Sonnet 5 (2026-06-30, 1M ctx native): promo $2/$10 ended 2026-08-31 as
    # announced — standard $3.0/$15.0 applies since 2026-09-01.
    'claude-sonnet-5':           {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 5'},
    # Sonnet 5.5 (GA 2026-09-28): $2/$10 per 1M, 1M ctx; adaptive thinking
    # default ON (lowest rung between_tools), API default effort high.
    'claude-sonnet-5.5':         {'input': 2.0,   'output': 10.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 5.5'},
    'claude-sonnet-5-5':         {'input': 2.0,   'output': 10.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 5.5'},
    'claude-opus-4.8':           {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.8'},
    'claude-opus-4.7':           {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.7'},
    'claude-opus-4.6':           {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.6'},
    'claude-sonnet-4.6':         {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4.6'},
    'claude-opus-4.5':           {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.5'},
    'claude-sonnet-4.5':         {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4.5'},
    'claude-sonnet-4':           {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4'},
    'claude-haiku-4.5':          {'input': 1.0,   'output': 5.0,   'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Haiku 4.5'},
    # Haiku 5.5 (GA 2026-10-07): 1M ctx / 128K max out. Tiered by prompt length:
    # <=100K $0.10/$0.50; >100K $0.50/$2.50 (the only 4.6+ model without flat
    # 1M pricing). Adaptive thinking default ON; disable works only <= high.
    'claude-haiku-5.5':          {'input': 0.10,  'output': 0.50,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Haiku 5.5'},
    'claude-haiku-5-5':          {'input': 0.10,  'output': 0.50,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Haiku 5.5'},
    # Meituan-gateway aws./azure. spellings — wire channel of the split
    # identities above; cost keys on the raw gateway id (model_entry.py).
    'aws.claude-opus-4.5':       {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.5'},
    'aws.claude-sonnet-4.5':     {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4.5'},
    'aws.claude-sonnet-4':       {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4'},
    'aws.claude-haiku-4.5':      {'input': 1.0,   'output': 5.0,   'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Haiku 4.5'},
    'claude-fable-5.1':          {'input': 10.0,  'output': 50.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.025, 'name': 'Fable 5.1'},
    'azure.claude-fable-5.1':    {'input': 10.0,  'output': 50.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.025, 'name': 'Fable 5.1'},
    # qwen3.5 (Meituan plaza, baidu channel) — 广场价隐含 ¥2.8/¥5.6 per 1M。
    'qwen3.5':                   {'input': 0.39,  'output': 0.78,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen 3.5'},
    'qwen3.5-baidu':             {'input': 0.39,  'output': 0.78,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen 3.5'},
    # Meituan-gateway yuju evaDaily models — raw gateway IDs stay on the wire; .name is the display channel.
    'yuju-claude-opus-5-evaDaily':   {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 5'},
    'yuju-claude-opus-4.8-evaDaily': {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.8'},
    'yuju-claude-opus-4.7-evaDaily': {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.7'},
    'aws.claude-opus-4.8':       {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.8'},
    'claude-opus-4-8':           {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.8'},
    'us.anthropic.claude-opus-5-v1:0':          {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 5 (Bedrock)'},
    'us.anthropic.claude-sonnet-5-v1:0':        {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 5 (Bedrock)'},
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
    # gpt-oss-* are open weights — OpenAI sells no first-party API, so no official
    # card exists. Bare ids price at the OpenRouter cheapest-host list (2026-09-08):
    # 120B $0.037/$0.17, 20B $0.03/$0.13 per 1M; cache mul follows the Bedrock twins.
    'gpt-oss-120b':                           {'input': 0.037, 'output': 0.17,  'cacheWriteMul': 1.0,  'cacheReadMul': 0.50, 'name': 'GPT-OSS 120B'},
    'gpt-oss-20b':                            {'input': 0.03,  'output': 0.13,  'cacheWriteMul': 1.0,  'cacheReadMul': 0.50, 'name': 'GPT-OSS 20B'},
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
    # DeepSeek V4.1 Flash (2026-09-10, api-docs.deepseek.com/news/news260910) —
    # canonical API id 'deepseek-flash'. New CED architecture; native vision in
    # the BASE model; 1M ctx / 384K max output. The legacy ids
    # 'deepseek-v4-flash' / 'deepseek-v4-flash-vision-exp' are temporarily
    # routed to V4.1 Flash and billed at Flash rates, so their rows carry the
    # new prices. 'deepseek-v4-pro' is scheduled to route to V4.1 Flash from
    # 2026-09-14 12:00 Beijing time (until a V4.1 Pro release) — its row keeps
    # the V4 Pro price card until that routing lands, and the dated pin
    # deepseek-v4-pro-0813 folds onto it (lib/pricing.pricing_for_model).
    # Pricing (api-docs.deepseek.com/quick_start/pricing, 2026-09-10): off-peak
    # bases $0.15 in (cache hit $0.003 → cacheReadMul 0.02) / $0.60 out.
    # 'peak' (official API rows ONLY — not the gateway mirrors): 2x pricing on
    # ALL billing items during 01:00-04:00 + 06:00-10:00 UTC on WEEKDAYS, in
    # force since 2026-09-10 04:00 UTC (1786900800) — REPLACES the retired
    # 2026-08-16 Beijing-time schedule [(9,12),(14,18)], tz_offset 8. See
    # lib/pricing/_peak.py.
    'deepseek-flash':            {'input': 0.15,  'output': 0.60,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.02,   'name': 'DeepSeek V4.1 Flash',
                                  'peak': {'mul': 2.0, 'windows': [(1, 4), (6, 10)], 'tz_offset': 0, 'weekdays': [0, 1, 2, 3, 4], 'effective_from': 1786900800}},
    'deepseek-v4-pro':           {'input': 0.66,  'output': 1.98,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.0333, 'name': 'DeepSeek V4 Pro'},
    'deepseek-v4-flash':         {'input': 0.15,  'output': 0.60,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.02,   'name': 'DeepSeek V4.1 Flash',
                                  'peak': {'mul': 2.0, 'windows': [(1, 4), (6, 10)], 'tz_offset': 0, 'weekdays': [0, 1, 2, 3, 4], 'effective_from': 1786900800}},
    # Meituan gateway Huawei-cloud mirror for DeepSeek V4 Flash — same pricing.
    'deepseek-v4-flash-huawei':  {'input': 0.15,  'output': 0.60,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.02,   'name': 'DeepSeek V4.1 Flash (Huawei)'},
    # Meituan gateway Tencent/Meituan-cloud mirrors for DeepSeek V4 — same pricing.
    'deepseek-v4-flash-tencent': {'input': 0.15,  'output': 0.60,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.02,   'name': 'DeepSeek V4.1 Flash (Tencent)'},
    'deepseek-v4-flash-meituan': {'input': 0.15,  'output': 0.60,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.02,   'name': 'DeepSeek V4.1 Flash (Meituan)'},
    'deepseek-v4-pro-tencent':   {'input': 0.66,  'output': 1.98,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.0333, 'name': 'DeepSeek V4 Pro (Tencent)'},
    # Vision experimental variant of V4 Flash — superseded 2026-09-10: vision
    # moved into the base V4.1 Flash model; DeepSeek temporarily routes the
    # exp id to it at Flash rates.
    'deepseek-v4-flash-vision-exp': {'input': 0.15, 'output': 0.60, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.02, 'name': 'DeepSeek V4.1 Flash'},
    'deepseek-v3.2':             {'input': 0.28,  'output': 0.41,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'DeepSeek V3.2'},  # ¥2/¥3 per 1M
    'LongCat-Flash-Thinking-2601': {'input': 0.0, 'output': 0.0,  'cacheWriteMul': 0,    'cacheReadMul': 0,    'name': 'LongCat Flash'},
    'LongCat-Flash-Chat-2603':      {'input': 0.28,'output': 1.10, 'cacheWriteMul': 0,    'cacheReadMul': 0,    'name': 'LongCat Flash Chat'},  # ¥2/¥8 per 1M
    # ── Qwen (DashScope) — converted from CNY at 7.24 ──
    'qwen3.6-plus':              {'input': 0.28, 'output': 1.66, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen 3.6 Plus'},  # ¥2/¥12 per 1M (≤256K)
    'qwen3.5-plus':              {'input': 0.11, 'output': 0.66, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen 3.5 Plus'},  # ¥0.8/¥4.8 per 1M (≤128K)
    'qwen3.5-flash':             {'input': 0.03, 'output': 0.28, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen 3.5 Flash'},  # ¥0.2/¥2 per 1M (≤128K)
    'qwen3-max':                 {'input': 0.35, 'output': 1.38, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen3 Max'},  # ¥2.5/¥10 per 1M (≤32K)
    # qwen3-max-preview (2025-09 launch, Alibaba Cloud Model Studio tiered card):
    # ≤32K tier $0.861/$3.441 per 1M (32-128K $1.434/$5.735, 128-252K $2.151/$8.602).
    'qwen3-max-preview':         {'input': 0.861, 'output': 3.441, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen3 Max (Preview)'},
    # Qwen3.8 generation (2026-08, DashScope): native vision + thinking, 1M ctx.
    # 3.8-max ¥12/¥36 per 1M (cache hit ¥1.5 → cacheReadMul 0.125); 3.7-plus row uses the
    # >256K tier ¥6/¥24 (≤256K tier unverified); 3.8-flash ¥0.8/¥2.7 (read mul unverified —
    # family convention 0.20).
    'qwen3.8-max':               {'input': 1.66, 'output': 4.97, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.125, 'name': 'Qwen 3.8 Max'},
    'qwen3.7-plus':              {'input': 0.83, 'output': 3.31, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen 3.7 Plus'},
    'qwen3.8-flash':             {'input': 0.11, 'output': 0.37, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.20, 'name': 'Qwen 3.8 Flash'},
    # Qwen3.8-Omni-Flash (GA 2026-09-18, DashScope): ¥0.8/¥2.7 per 1M, implicit cache
    # hit ¥0.1 → cacheReadMul 0.125; native omni (text+image+audio+video in, text out).
    'qwen3.8-omni-flash':        {'input': 0.11, 'output': 0.37, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.125, 'name': 'Qwen 3.8 Omni Flash'},
    'qwen3-vl-plus':             {'input': 0.14, 'output': 1.38, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen3 VL Plus'},  # ¥1/¥10 per 1M (≤32K)
    # qwen-mt-plus (translation): Alibaba Cloud Model Studio international rate.
    'qwen-mt-plus':              {'input': 2.46, 'output': 7.37, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Qwen MT Plus'},
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
    'gemini-2.5-flash-lite':     {'input': 0.10, 'output': 0.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 2.5 Flash-Lite'},
    'gemini-2.0-flash-lite':     {'input': 0.075,'output': 0.30, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 2.0 Flash-Lite'},
    'gemini-3.1-flash-lite-preview': {'input': 0.25, 'output': 1.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.1 Flash-Lite'},
    # GA id (the Meituan gateway 404s the -preview spelling, serves this one).
    'gemini-3.1-flash-lite':     {'input': 0.25, 'output': 1.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.1 Flash-Lite'},
    'gemini-3.1-pro-preview':    {'input': 2.00, 'output': 12.0, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.1 Pro'},
    'gemini-3-flash-preview':    {'input': 0.15, 'output': 0.60, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3 Flash'},
    'gemini-3.5-flash':          {'input': 1.49, 'output': 8.95, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.5 Flash'},  # ¥10.80/¥64.80 per 1M
    'gemini-3.6-flash':          {'input': 1.49, 'output': 7.46, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.6 Flash'},  # ¥10.80/¥54.00 per 1M
    # Intro pricing $0.75/$3.75 through 2026-12-31, then $1.50/$7.50 standard.
    'gemini-3.7-flash':          {'input': 0.75, 'output': 3.75, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.7 Flash'},
    'gemini-3.8-flash':          {'input': 1.50, 'output': 7.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.8 Flash'},
    # Gemini 4 Argon intro pricing $2/$10 (+$0.10 cache-read) through 2026-11-30,
    # then $4/$20 +$0.20 (ai.google.dev/gemini-api/docs/pricing).
    'gemini-4-argon':            {'input': 2.00, 'output': 10.0, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.05, 'name': 'Gemini 4 Argon'},
    'gemini-3.5-flash-lite':     {'input': 0.30, 'output': 2.49, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.5 Flash-Lite'},  # ¥2.16/¥18 per 1M
    'gemini-3.1-flash-image-preview': {'input': 0.25, 'output': 1.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.1 Flash Image'},
    'gemini-3-pro-image-preview':    {'input': 2.50, 'output': 12.0, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3 Pro Image'},
    'gemini-2.5-flash-image':        {'input': 0.15, 'output': 0.60, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 2.5 Flash Image'},
    'gemini-2.0-flash-preview-image-generation': {'input': 0.10, 'output': 0.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 2.0 Flash Image'},
    'gpt-image-1.5':                 {'input': 0.0,  'output': 0.0,  'cacheWriteMul': 0, 'cacheReadMul': 0, 'name': 'GPT Image 1.5'},
    # GPT Image 2 — token-priced (Meituan): text in ¥36/M ($4.97), image in ¥57.6/M ($7.96), image out ¥216/M ($29.83).
    'gpt-image-2':                   {'input': 4.97, 'output': 29.83,'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT Image 2'},
    # GPT Image 2.5 Sunburst (2026-09-08) — official rates: text in $5/M (cached $1.25 →
    # 0.25), image in $8/M (cached $2), image out $30/M; "token rates match GPT Image 2".
    # developers.openai.com/api/docs/models/gpt-image-2.5-sunburst (checked 2026-09-20).
    'gpt-image-2.5-sunburst':        {'input': 5.0,  'output': 30.0, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'GPT Image 2.5 Sunburst'},
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
    'o3-pro':                    {'input': 20.0, 'output': 80.0, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.50, 'name': 'o3 Pro'},
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
    # Dated wire id of logical 'claude-sonnet-4.5' in anthropic.json — cost
    # keys on the wire id, so both channels need a row (same as haiku-4-5).
    'claude-sonnet-4-5-20250929': {'input': 3.0,  'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4.5'},
    # ── Anthropic (Claude 4 — legacy) ──
    'claude-opus-4-20250514':    {'input': 15.0,  'output': 75.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4'},
    'claude-sonnet-4-20250514':  {'input': 3.0,   'output': 15.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Sonnet 4'},
    # ── MiniMax ──
    # M3 (2026-06-01) — flagship: MSA sparse attn, 1M ctx, native multimodal (image+video in).
    # Launch 50%-off promo made permanent 2026-07: $0.30/$1.20 (≤512K; >512K tier $0.60/$2.40);
    # cache read $0.06 → cacheReadMul 0.20.
    'MiniMax-M3':                {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.20, 'name': 'MiniMax M3'},
    'MiniMax-M2':                {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2'},
    'MiniMax-M2.1':              {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.1'},
    'MiniMax-M2.1-highspeed':    {'input': 0.30, 'output': 2.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.1 HS'},
    'MiniMax-M2.5':              {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.5'},
    'MiniMax-M2.5-highspeed':    {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.5 HS'},
    'MiniMax-M2.7':              {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.7'},
    'MiniMax-M2.7-highspeed':    {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.7 HS'},
    'M2-her':                    {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2-her'},
    # ── Xiaomi MiMo (converted from CNY at 7.24) ──
    # MiMo V2.6 (GA 2026-09-22): Flash ¥1/¥2, Pro ¥3/¥6 per 1M; 99% cache-hit discount
    # (Flash ¥0.02, Pro ¥0.025) → cacheReadMul 0.02 / 0.0083. Native full-modal in → text.
    'mimo-v2.6-flash':           {'input': 0.14, 'output': 0.28, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.02, 'name': 'MiMo V2.6 Flash'},
    'mimo-v2.6-pro':             {'input': 0.41, 'output': 0.83, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.0083, 'name': 'MiMo V2.6 Pro'},
    # ── GLM (Zhipu AI) — converted from CNY at 7.24 ──
    'glm-5.3':                   {'input': 3.45, 'output': 13.81, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-5.3'},  # 1M context; registered billable rates, not a routing hint
    # glm-5.3-flash (2026-08): ¥0.8/¥2.8 per 1M; cache hit ¥0.23 → cacheReadMul ≈ 0.29.
    'glm-5.3-flash':             {'input': 0.11, 'output': 0.39, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.29, 'name': 'GLM-5.3 Flash'},
    'glm-5.2':                   {'input': 3.45, 'output': 13.81, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-5.2'},  # 1M context, 128K output, text-only thinking flagship (2026-06-13)
    'glm-5.1':                   {'input': 3.45, 'output': 13.81, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-5.1'},
    'glm-5.1-huawei':            {'input': 3.45, 'output': 13.81, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-5.1 (Huawei)'},
    'glm-5':                     {'input': 3.45, 'output': 13.81, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-5'},
    'glm-5v-turbo':              {'input': 0.69, 'output': 3.04, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-5V Turbo'},
    # glm-5-turbo: text-only turbo tier — anchored to glm-5v-turbo until Zhipu
    # publishes a separate card.
    'glm-5-turbo':               {'input': 0.69, 'output': 3.04, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-5 Turbo'},
    'glm-4.7':                   {'input': 0.69, 'output': 0.69, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-4.7'},
    # glm-4.7-flash: no Zhipu card yet; per OpenRouter z-ai listing (2026-09-08).
    'glm-4.7-flash':             {'input': 0.0605, 'output': 0.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-4.7 Flash'},
    'glm-4.5-air':               {'input': 0.28, 'output': 1.10, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-4.5 Air'},
    'glm-4.5-flash':             {'input': 0.0,  'output': 0.0,  'cacheWriteMul': 0,    'cacheReadMul': 0,    'name': 'GLM-4.5 Flash'},
    # ── Doubao (Volcengine) — converted from CNY at 7.24 ──
    'Doubao-Seed-2.0-pro':       {'input': 0.55, 'output': 2.21, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Doubao Seed 2.0 Pro'},
    # Seed 2.1 Pro (-260628 snapshot, current flagship): ¥8/¥20 per 1M → $1.10/$2.76 @ 7.24.
    'doubao-seed-2-1-pro-260628': {'input': 1.10, 'output': 2.76, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Doubao Seed 2.1 Pro'},
    'Doubao-Seed-2.0-lite':      {'input': 0.04, 'output': 0.14, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Doubao Seed 2.0 Lite'},
    'Doubao-Seed-2.0-mini':      {'input': 0.02, 'output': 0.06, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Doubao Seed 2.0 Mini'},
    'doubao-seed-1-6-vision-250815': {'input': 0.11, 'output': 1.10, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Doubao Seed 1.6 Vision'},  # ¥0.8/¥8 per 1M (≤32K)
    # ── Mistral AI ──
    # mistral-large-latest → Mistral Large 3 (25.12): $0.50/$1.50 per 1M.
    # Mistral Large 4 (Public Preview 2026-10-06, v26.10): list $1.36/$4.18,
    # cached $0.14 (current 50% promo $0.68/$2.09 NOT used — list price only).
    # 1T-param / 52B-active natively-multimodal MoE; reasoning_effort high/none.
    'mistral-large-4-0':         {'input': 1.36, 'output': 4.18, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Mistral Large 4'},
    'mistral-large-latest':      {'input': 0.50, 'output': 1.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Mistral Large 3'},
    # mistral-small-latest → Mistral Small 4 (26.03): $0.15/$0.60 per 1M.
    'mistral-small-latest':      {'input': 0.15, 'output': 0.60, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Mistral Small 4'},
    # codestral-latest: Codestral 25.01 is on Mistral's official deprecated list; the coding
    # role moved to mistral-medium-latest (Medium 3.5). Do NOT re-add.
    # Medium 3 matches la plateforme ($0.40/$2.00); Medium 3.5 has no first-party
    # card yet — priced per OpenRouter mistralai/mistral-medium-3-5 (2026-09-08).
    'mistral-medium-3.5-128b':   {'input': 1.50, 'output': 7.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Mistral Medium 3.5'},
    'mistral-medium-3-instruct': {'input': 0.40, 'output': 2.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Mistral Medium 3'},
    # ── xAI (Grok) ──
    'grok-3':                    {'input': 3.00, 'output': 15.0, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Grok 3'},
    'grok-3-mini':               {'input': 0.30, 'output': 0.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Grok 3 Mini'},
    # grok-4.7 (2026-09-21, current flagship): official xAI pricing $2/$0.50/$6 per 1M
    # (input/cached/output) below 200k prompt tokens; same shape as 4.6 → cacheReadMul 0.25.
    # OpenRouter's $1.60/$4.80 is a reseller discount; official xAI price wins.

    # xAI charges $4/$1/$12 above 200K prompt tokens; this flat table
    # records only the base tier, not an enforceable high-context price cap.
    'grok-4.7':                  {'input': 2.00, 'output': 6.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Grok 4.7'},
    # grok-4.6 (2026-08): $2/$6 per 1M, cached input $0.50 → cacheReadMul 0.25.
    'grok-4.6':                  {'input': 2.00, 'output': 6.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Grok 4.6'},
    'grok-4.20':                 {'input': 1.25, 'output': 2.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Grok 4.20'},
    'grok-4.1-fast':             {'input': 0.20, 'output': 0.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Grok 4.1 Fast'},
    # ── Meta (Muse Spark) — Meta Model API pricing (dev.meta.ai/docs/
    # pricing-rate-limits; unchanged since 1.1, listed for 1.3 on 2026-09-02) ──
    # Standard: $1.25/$4.25 per 1M, automatic prefix-cache read $0.15 → readMul 0.12.
    # Contributor SKU: $0.10/$0.20 (cache read $0.002 → readMul 0.02) in
    # exchange for Meta training on prompts/completions (vendor-documented).
    'muse-spark-1.3':              {'input': 1.25, 'output': 4.25, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.12, 'name': 'Muse Spark 1.3'},
    'muse-spark-1.3-contributor':  {'input': 0.10, 'output': 0.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.02, 'name': 'Muse Spark 1.3 (Contributor)'},
    # ── Moonshot (Kimi) — per OpenRouter (2026-04-20 release) ──
    # kimi-k3 (2026-07-17): ¥20/¥100 per 1M → $2.76/$13.81 @ 7.24; 1M context
    'kimi-k3':                   {'input': 2.76, 'output': 13.81,'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Kimi K3'},
    'kimi-k2.6':                 {'input': 0.60, 'output': 2.80, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Kimi K2.6'},
    # kimi-k2-instruct-0905 (K2 0905 checkpoint pin): $0.60/$2.50 per 1M — matches
    # OpenRouter moonshotai/kimi-k2-0905 (2026-09-08). Exact id wins over date-fold.
    'kimi-k2-instruct-0905':     {'input': 0.60, 'output': 2.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Kimi K2 0905'},
    # kimi-k2.7-code (official platform.kimi.ai, verified 2026-09-03):
    # $0.95/$4.00 per 1M, cache hit $0.19 → readMul 0.20; 262K context.
    # highspeed is a gateway SKU billing 2× output (MiniMax highspeed pattern).
    'kimi-k2.7-code':            {'input': 0.95, 'output': 4.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.20, 'name': 'Kimi K2.7 Code'},
    'kimi-k2.7-code-highspeed':  {'input': 0.95, 'output': 8.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.20, 'name': 'Kimi K2.7 Code (Highspeed)'},
    # ── Tencent Hunyuan ── hy4-preview (2026-08-28): ¥6/¥18 per 1M, cache hit ¥0.3 → $0.829/$2.486 @ 7.24
    'hy4-preview':               {'input': 0.829,'output': 2.486,'cacheWriteMul': 1.00, 'cacheReadMul': 0.05, 'name': 'Hunyuan HY4 Preview'},
    # ── Tencent Hunyuan ── cheapest tier (≤16K): ¥1.2/¥4 per 1M = $0.166/$0.553 @ 7.24
    'hy3-preview':               {'input': 0.166,'output': 0.553,'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Hunyuan HY3 Preview'},
    # -- BEGIN meituan_template_sync auto-added (regenerated wholesale) --
    # -- END meituan_template_sync auto-added --
}

# Dated-checkpoint pins (deepseek-v4-pro-0813, claude-opus-4-5-20251001) serve
# the same model as their rolling alias, and vendors do not reprice a pin. An
# exact miss therefore folds ONE trailing -MMDD / -YYYYMMDD stamp and retries
# against the alias row instead of silently booking zero. Stamps that are not
# dates (LongCat -2601-style iteration tags) fold to an id with no row and
# still miss — same outcome as before, never a wrong row.
_DATED_SUFFIX_RE = re.compile(r'-(?:\d{8}|\d{4})$')


def pricing_for_model(model_id):
    """Return the MODEL_PRICING row for *model_id*, or ``None``.

    Exact id first; on a miss, one dated-checkpoint suffix is folded so a
    pinned snapshot inherits the rolling alias's row.
    """
    if model_id in {"deepseek-v4.1-flash", "deepseek-v4-1-flash"}:
        model_id = "deepseek-flash"
    row = MODEL_PRICING.get(model_id)
    if row is None and isinstance(model_id, str):
        row = MODEL_PRICING.get(_DATED_SUFFIX_RE.sub('', model_id))
    return row


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
    # DeepSeek V4 gateway mirrors use provider-qualified model ids.  The plain
    # official ids live in MODEL_PRICING above; repeating them here would make
    # the legacy CNY compatibility layer overwrite the official USD rows.
    'deepseek-v4-flash-huawei': {'input': [(1_000_000, 1.1)],  'output': [(1_000_000, 4.4)]},
    # Tencent Hunyuan HY3 — tiered pricing on 16K / 32K / 256K context boundaries
    'hy3-preview':              {'input': [(16_000, 1.2), (32_000, 1.6), (256_000, 2.0)],
                                 'output': [(16_000, 4.0), (32_000, 6.4), (256_000, 8.0)]},
    # Tencent Hunyuan HY4 Preview — flat ¥6/¥18 per 1M regardless of context
    'hy4-preview':              {'input': [(1_000_000, 6.0)],  'output': [(1_000_000, 18.0)]},
    'deepseek-r1':      {'input': [(1_000_000, 4.0)],  'output': [(1_000_000, 16.0)]},
    'glm-5v-turbo':     {'input': [(32_000, 5.0), (1_000_000, 7.0)],  'output': [(32_000, 22.0), (1_000_000, 26.0)]},
    '_default':         {'input': [(128_000, 0.8), (256_000, 2.0), (1_000_000, 4.0)], 'output': [(128_000, 4.8), (256_000, 12.0), (1_000_000, 24.0)]},
}
