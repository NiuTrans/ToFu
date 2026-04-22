"""
Pricing — model pricing tables, exchange rate fetching, and background updater.

Consolidated from lib/__init__.py (static tables) and server.py (dynamic fetchers)
to keep all pricing data and logic in one place.

Public API (re-exported by lib/__init__):
    MODEL_PRICING          — {model_id: {input, output, cacheWriteMul, cacheReadMul, name}}
    QWEN_PRICING_CNY       — {model_id: {input: [(threshold, cny_price)], output: [...]}}
    DEFAULT_USD_CNY_RATE   — float
    get_pricing_data()     — thread-safe copy of live pricing state
    refresh_pricing_async() — trigger background pricing refresh
"""

import json
import re
import threading
import time

import requests  # noqa: E402  (lazy-loaded in functions for proxy/lib/db)

from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════
#  Static Pricing Tables
# ══════════════════════════════════════════════════════

DEFAULT_USD_CNY_RATE = 7.24

# ── Model pricing (USD per 1M tokens) — hardcoded fallback ──
# cacheWriteMul / cacheReadMul are multipliers of the base input price:
#   Anthropic Claude: write=1.25x, read=0.10x (5-min TTL)
#   OpenAI GPT:       write=1.00x, read=0.50x
#   DeepSeek:         write=1.00x, read=0.10x (disk cache)
MODEL_PRICING = {
    'aws.claude-opus-4.7':       {'input': 5.0,   'output': 25.0,  'cacheWriteMul': 1.25, 'cacheReadMul': 0.10, 'name': 'Claude Opus 4.7'},
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
    'deepseek-chat':             {'input': 0.27,  'output': 1.10,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'DeepSeek V3'},
    'deepseek-v3.2':             {'input': 0.28,  'output': 0.41,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'DeepSeek V3.2'},  # ¥2/¥3 per 1M
    # DeepSeek V3.2 mirrors on YourProvider gateway — tiered ¥2/¥4 input, ¥4/¥6 output at 32K (cheapest tier in USD)
    'deepseek-v3.2-tencent':     {'input': 0.28,  'output': 0.55,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'DeepSeek V3.2 (Tencent)'},  # ¥2/¥4 per 1M ≤32K
    'deepseek-v3.2-baidu':       {'input': 0.28,  'output': 0.55,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'DeepSeek V3.2 (Baidu)'},    # ¥2/¥4 per 1M ≤32K
    'deepseek-v3.2-huawei':      {'input': 0.28,  'output': 0.55,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'DeepSeek V3.2 (Huawei)'},   # ¥2/¥4 per 1M ≤32K
    'deepseek-v3.2-doubao':      {'input': 0.28,  'output': 0.55,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'DeepSeek V3.2 (Doubao)'},   # ¥2/¥4 per 1M ≤32K
    'deepseek-reasoner':         {'input': 0.55,  'output': 2.21,  'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'DeepSeek R1'},
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
    'gemini-3.1-flash-image-preview': {'input': 0.25, 'output': 1.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3.1 Flash Image'},
    'gemini-3-pro-image-preview':    {'input': 2.50, 'output': 12.0, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 3 Pro Image'},
    'gemini-2.5-flash-image':        {'input': 0.15, 'output': 0.60, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 2.5 Flash Image'},
    'gemini-2.0-flash-preview-image-generation': {'input': 0.10, 'output': 0.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.25, 'name': 'Gemini 2.0 Flash Image'},
    'gpt-image-1.5':                 {'input': 0.0,  'output': 0.0,  'cacheWriteMul': 0, 'cacheReadMul': 0, 'name': 'GPT Image 1.5'},
    'gpt-image-1':                   {'input': 0.0,  'output': 0.0,  'cacheWriteMul': 0, 'cacheReadMul': 0, 'name': 'GPT Image 1'},
    'gpt-image-1-mini':              {'input': 0.0,  'output': 0.0,  'cacheWriteMul': 0, 'cacheReadMul': 0, 'name': 'GPT Image 1 Mini'},
    'dall-e-3':                      {'input': 0.0,  'output': 0.0,  'cacheWriteMul': 0, 'cacheReadMul': 0, 'name': 'DALL-E 3'},
    # ── OpenAI (GPT-5.4 family — March 2026) ──
    'gpt-5.4':                   {'input': 2.50, 'output': 15.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5.4'},
    'gpt-5.4-pro':               {'input': 30.0, 'output': 180.0, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5.4 Pro'},
    'gpt-5.4-mini':              {'input': 0.75, 'output': 4.50, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5.4 Mini'},
    'gpt-5.4-nano':              {'input': 0.20, 'output': 1.25, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5.4 Nano'},
    # ── OpenAI (GPT-5 family) ──
    'gpt-5':                     {'input': 1.25, 'output': 10.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5'},
    'gpt-5.2':                   {'input': 1.75, 'output': 14.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5.2'},
    'gpt-5-mini':                {'input': 0.25, 'output': 2.00, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5 Mini'},
    'gpt-5-nano':                {'input': 0.05, 'output': 0.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GPT-5 Nano'},
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
    'MiniMax-M2':                {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2'},
    'MiniMax-M2.1':              {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.1'},
    'MiniMax-M2.1-highspeed':    {'input': 0.30, 'output': 2.40, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.1 HS'},
    'MiniMax-M2.5':              {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.5'},
    'MiniMax-M2.5-highspeed':    {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.5 HS'},
    'MiniMax-M2.7':              {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.7'},
    'MiniMax-M2.7-highspeed':    {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2.7 HS'},
    'M2-her':                    {'input': 0.30, 'output': 1.20, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'MiniMax M2-her'},
    # ── GLM (Zhipu AI) — converted from CNY at 7.24 ──
    'glm-5.1':                   {'input': 3.45, 'output': 13.81, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'GLM-5.1'},
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
    'kimi-k2.6':                 {'input': 0.60, 'output': 2.80, 'cacheWriteMul': 1.00, 'cacheReadMul': 0.10, 'name': 'Kimi K2.6'},
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
    # YourProvider mirrors: ¥2/¥4 input, ¥4/¥6 output, split at 32K context
    'deepseek-v3.2-tencent': {'input': [(32_000, 2.0), (1_000_000, 4.0)], 'output': [(32_000, 4.0), (1_000_000, 6.0)]},
    'deepseek-v3.2-baidu':   {'input': [(32_000, 2.0), (1_000_000, 4.0)], 'output': [(32_000, 4.0), (1_000_000, 6.0)]},
    'deepseek-v3.2-huawei':  {'input': [(32_000, 2.0), (1_000_000, 4.0)], 'output': [(32_000, 4.0), (1_000_000, 6.0)]},
    'deepseek-v3.2-doubao':  {'input': [(32_000, 2.0), (1_000_000, 4.0)], 'output': [(32_000, 4.0), (1_000_000, 6.0)]},
    'deepseek-r1':      {'input': [(1_000_000, 4.0)],  'output': [(1_000_000, 16.0)]},
    'glm-5v-turbo':     {'input': [(32_000, 5.0), (1_000_000, 7.0)],  'output': [(32_000, 22.0), (1_000_000, 26.0)]},
    '_default':         {'input': [(128_000, 0.8), (256_000, 2.0), (1_000_000, 4.0)], 'output': [(128_000, 4.8), (256_000, 12.0), (1_000_000, 24.0)]},
}

# ══════════════════════════════════════════════════════
#  Shared State
# ══════════════════════════════════════════════════════

_pricing_lock = threading.Lock()
_refresh_lock = threading.Lock()  # Guards refresh dedup — acquire(blocking=False) for non-blocking skip
_pricing_data = {
    'model': '', 'inputPrice': 15.0, 'outputPrice': 75.0,  # model populated at runtime
    'cacheWriteMul': 1.25, 'cacheReadMul': 0.10,
    'usdToCny': 7.24, 'exchangeRateUpdated': 0,  # DEFAULT_USD_CNY_RATE read at runtime
    'pricingUpdated': 0, 'pricingSource': 'default',
    'exchangeRateSource': 'none', 'onlineMatchedModel': None,
}

# ══════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════

def get_pricing_data():
    """Return a thread-safe copy of the current pricing data."""
    with _pricing_lock:
        return dict(_pricing_data)


def refresh_pricing_async():
    """Trigger a background pricing refresh. Non-blocking, deduped."""
    if not _refresh_lock.acquire(blocking=False):
        logger.debug('[Pricing] Refresh already in progress — skipping duplicate request')
        return
    try:
        threading.Thread(target=_update_pricing_locked, daemon=True).start()
    except Exception:
        logger.error('[Pricing] Failed to start pricing refresh thread', exc_info=True)
        _refresh_lock.release()
        raise

# ══════════════════════════════════════════════════════
#  Internal Fetchers
# ══════════════════════════════════════════════════════

def _fetch_exchange_rate():
    from lib.proxy import proxies_for as _proxies_for
    apis = [
        ('https://api.exchangerate-api.com/v4/latest/USD', lambda d: d.get('rates', {}).get('CNY')),
        ('https://open.er-api.com/v6/latest/USD', lambda d: d.get('rates', {}).get('CNY')),
        ('https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json', lambda d: d.get('usd', {}).get('cny')),
    ]
    for url, extract in apis:
        try:
            resp = requests.get(url, timeout=12, headers={'User-Agent': 'PricingBot/1.0'},
                               proxies=_proxies_for(url))
            if resp.ok:
                rate = extract(resp.json())
                if rate and float(rate) > 0:
                    return round(float(rate), 4)
        except Exception as e:
            logger.warning('[Pricing] exchange rate API %s failed: %s', url, e, exc_info=True)
    return None

def _fetch_model_pricing_online(model_name):
    from lib.proxy import proxies_for as _proxies_for
    try:
        norm = model_name.lower()
        for prefix in ('aws.', 'gcp.', 'azure.', 'bedrock.'):
            norm = norm.replace(prefix, '')
        norm = re.sub(r'\.\d+$', '', norm)
        resp = requests.get('https://openrouter.ai/api/v1/models', timeout=20,
                            headers={'User-Agent': 'PricingBot/1.0'},
                            proxies=_proxies_for('https://openrouter.ai/api/v1/models'))
        if not resp.ok:
            return None
        norm_parts = set(norm.replace('-', ' ').replace('.', ' ').split())
        best, best_score = None, 0
        for m in resp.json().get('data', []):
            mid = m.get('id', '').lower()
            mid_short = mid.split('/')[-1] if '/' in mid else mid
            overlap = len(norm_parts & set(mid_short.replace('-', ' ').replace('.', ' ').split()))
            if overlap < 2:
                continue
            pricing = m.get('pricing', {})
            pp = float(pricing.get('prompt', 0) or 0)
            cp = float(pricing.get('completion', 0) or 0)
            if pp <= 0 and cp <= 0:
                continue
            if overlap > best_score:
                best_score = overlap
                best = {
                    'input': round(pp * 1e6, 4),
                    'output': round(cp * 1e6, 4),
                    'matched': m.get('id', ''),
                }
        return best
    except Exception as e:
        logger.warning('[Pricing] OpenRouter model pricing fetch failed for %s: %s', model_name, e, exc_info=True)
        return None

def _update_pricing_locked():
    """Wrapper that owns _refresh_lock; used only by refresh_pricing_async."""
    try:
        _do_update_pricing()
    finally:
        _refresh_lock.release()

def _do_update_pricing():
    import lib as _lib  # deferred to avoid circular import
    now_ms = int(time.time() * 1000)
    rate = _fetch_exchange_rate()
    online = _fetch_model_pricing_online(_lib.LLM_MODEL)
    with _pricing_lock:
        if rate:
            _pricing_data['usdToCny'] = rate
            _pricing_data['exchangeRateUpdated'] = now_ms
            _pricing_data['exchangeRateSource'] = 'api'
        if online:
            _pricing_data.update(
                inputPrice=online['input'], outputPrice=online['output'],
                pricingSource='openrouter', onlineMatchedModel=online['matched'],
                pricingUpdated=now_ms,
            )
        elif _lib.LLM_MODEL in MODEL_PRICING:
            mp = MODEL_PRICING[_lib.LLM_MODEL]
            _pricing_data.update(
                inputPrice=mp['input'], outputPrice=mp['output'],
                pricingSource='known_table', pricingUpdated=now_ms,
            )
        data_copy = dict(_pricing_data)
    # Persist to DB
    db = None
    try:
        from lib.database import DOMAIN_SYSTEM, get_thread_db
        db = get_thread_db(DOMAIN_SYSTEM)
        db.execute(
            'INSERT OR REPLACE INTO pricing_cache (key, value, updated_at) VALUES (?, ?, ?)',
            ('pricing', json.dumps(data_copy), now_ms),
        )
        db.commit()
    except Exception as e:
        logger.warning('[Pricing] failed to persist pricing to DB: %s', e, exc_info=True)

# ══════════════════════════════════════════════════════
#  Background Worker
# ══════════════════════════════════════════════════════

