"""lib/llm_dispatch/config/_slots.py — Default slot configuration table.

``DEFAULT_SLOT_CONFIGS`` is a comprehensive reference table (model ->
{caps, rpm, latency, cost}) that seeds the slot pool before benchmark
data is loaded.  These are **reference tables** — any model configured
via the Settings UI benefits from having a pre-seeded entry here.  They
are overridden by benchmark data at runtime.
"""

from lib.log import get_logger
from lib.model_info._openai_gpt56 import gpt56_slot_configs

logger = get_logger(__name__)

# ══════════════════════════════════════════════════════════════
#  Default slot configs: model → {caps, rpm, latency, cost}
#  Comprehensive reference table — any model that might be configured
#  via the Settings UI benefits from having a pre-seeded entry here.
#  These are overridden by benchmark data at runtime.
# ══════════════════════════════════════════════════════════════
DEFAULT_SLOT_CONFIGS = {
    # ── Anthropic Fable 5 (creative-flagship line, May 2026) ──
    'fable-5':                       {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    'aws.fable-5':                   {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    'us.anthropic.fable-5-v1:0':     {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    # Meituan-gateway name for Fable 5 (Jul 2026 marketplace) — pricier tier
    # than the public API (¥72/¥360 per 1M ≈ $9.94/$49.72).
    'claude-fable-5':                {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.030},
    # ── Claude (Anthropic — 5th gen: Sonnet 5, Jun 2026) ──
    # Promo price $2/$10 until 2026-08-31, then $3/$15 — at $3 input the
    # derived 'cheap' tag no longer applies; drop it when the price row moves.
    'claude-sonnet-5':               {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 50,  'latency': 2000, 'cost': 0.009},
    # ── Claude (Anthropic — current gen: 4.8 flagship, May 2026) ──
    'claude-opus-4-8':               {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    # ── Claude (Anthropic — 4.7 family, Apr 2026) ──
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
    'aws.claude-opus-4.8':           {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    'aws.claude-opus-4.7':           {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
    # ── Claude (Amazon Bedrock native model IDs, inference-profile form) ──
    'us.anthropic.claude-opus-4-8-v1:0':         {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 5000, 'cost': 0.015},
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

    # ── OpenAI GPT-5.6 public family ──
    # Sol is the flagship (and the gpt-5.6 alias target), Terra balances
    # quality/cost, and Luna serves cost-sensitive high-volume work. Pro is a
    # Responses reasoning mode, not a model slug. Values come from the shared
    # lib/model_info/data/openai.json contract.
    **gpt56_slot_configs(),
    # ── OpenAI (GPT-5.4 family — March 2026; kept as the cost tier) ──
    'gpt-5.4':                       {'caps': {'text', 'vision', 'thinking'},      'rpm': 30,  'latency': 3000, 'cost': 0.015},
    'gpt-5.4-pro':                   {'caps': {'text', 'vision', 'thinking'},      'rpm': 10,  'latency': 10000,'cost': 0.180},
    'gpt-5.4-mini':                  {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 60,  'latency': 2000, 'cost': 0.005},
    'gpt-5.4-nano':                  {'caps': {'text', 'vision', 'cheap'},         'rpm': 200, 'latency': 1000, 'cost': 0.001},
    # NOTE: The original GPT-5 family (gpt-5, gpt-5.2, gpt-5-mini, gpt-5-nano)
    # has been retired — 5.4 and 5.6 fully cover its capabilities and the
    # OpenAI gateway no longer routes to them. Codex-branded snapshots
    # (gpt-5.2-codex / gpt-5.1-codex-mini) remain in the Codex block below
    # since Codex is a separate lineup on its own cadence.
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
    # V4 family (Apr 2026) — 1M ctx, dual Thinking / Non-Thinking; pro=1.6T/49B, flash=284B/13B.
    'deepseek-v4-pro':               {'caps': {'text', 'thinking', 'cheap'},      'rpm': 30,  'latency': 3000, 'cost': 0.001},
    'deepseek-v4-flash':             {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.0002},
    'deepseek-v4-flash-huawei':      {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.0002},
    # RETIRED 2026-07-24 15:59 UTC (api-docs.deepseek.com/updates): the legacy
    # aliases 'deepseek-chat' and 'deepseek-reasoner' now ERROR on the official
    # API — their rows were removed. Successors: deepseek-v4-flash (chat →
    # non-thinking, reasoner → thinking) / deepseek-v4-pro. Do NOT re-add.
    'deepseek-v3.2':                 {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'deepseek-v3.2-tencent':         {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'deepseek-v3.2-baidu':           {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'deepseek-v3.2-huawei':          {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'deepseek-v3.2-doubao':          {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.001},

    # ── Gemini ──
    'gemini-2.5-pro':                {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 100, 'latency': 2000, 'cost': 0.005},
    'gemini-2.5-flash':              {'caps': {'text', 'vision', 'cheap'},         'rpm': 200, 'latency': 1500, 'cost': 0.001},
    'gemini-2.0-flash-lite':         {'caps': {'text', 'cheap'},                   'rpm': 200, 'latency': 1000, 'cost': 0.001},
    'gemini-3.1-flash-lite-preview': {'caps': {'text', 'vision', 'cheap'},         'rpm': 30,  'latency': 1500, 'cost': 0.001},
    'gemini-3.1-pro-preview':        {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 5,   'latency': 3000, 'cost': 0.006},
    'gemini-3-flash-preview':        {'caps': {'text', 'vision', 'thinking', 'cheap', 'audio_chat'}, 'rpm': 60,  'latency': 1500, 'cost': 0.001},
    # Omni chat model: audio arrives inline as an input_audio content-part via
    # /chat/completions (audio_chat), NOT the /audio/transcriptions endpoint.
    'LongCat-Flash-Omni-2603':       {'caps': {'text', 'vision', 'audio_chat'}, 'rpm': 60,  'latency': 2000, 'cost': 0.0},
    'gemini-3.5-flash':              {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 30,  'latency': 2000, 'cost': 0.005},
    # Jul 2026 marketplace additions — marketplace default quota is 20 RPM each.
    'gemini-3.6-flash':              {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 20,  'latency': 2000, 'cost': 0.005},
    # Aug 2026 marketplace addition (released 2026-08-13, 1M ctx / 64K out) —
    # marketplace default quota 20 RPM.
    'gemini-3.7-flash':              {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 20,  'latency': 2000, 'cost': 0.005},
    'gemini-3.5-flash-lite':         {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 20,  'latency': 1500, 'cost': 0.001},

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
    # M3 (2026-06-01) — flagship: MSA sparse attn, 1M ctx, native multimodal (image+video in).
    'MiniMax-M3':                    {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 60, 'latency': 2000, 'cost': 0.002},
    'MiniMax-M2':                    {'caps': {'text', 'vision', 'cheap'},        'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'MiniMax-M2.1':                  {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'MiniMax-M2.1-highspeed':        {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 1500, 'cost': 0.002},
    'MiniMax-M2.5':                  {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'MiniMax-M2.5-highspeed':        {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'MiniMax-M2.7':                  {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'MiniMax-M2.7-highspeed':        {'caps': {'text', 'thinking', 'cheap'},      'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'M2-her':                        {'caps': {'text', 'cheap'},                  'rpm': 60,  'latency': 2000, 'cost': 0.001},

    # ── Doubao (Volcengine) ──
    # NOTE: the Doubao-Seed-ASR-2.0 speech-to-text model is a pure 'transcription'
    # (non-chat) model — its slot config lives in the Speech-to-text block below,
    # NOT here with the Doubao chat models.
    'Doubao-Seed-2.0-pro':           {'caps': {'text', 'vision', 'thinking', 'cheap'}, 'rpm': 60, 'latency': 2000, 'cost': 0.002},
    'Doubao-Seed-2.0-lite':          {'caps': {'text', 'cheap'},                   'rpm': 120, 'latency': 1500, 'cost': 0.001},
    'Doubao-Seed-2.0-mini':          {'caps': {'text', 'cheap'},                   'rpm': 200, 'latency': 1000, 'cost': 0.001},

    # ── GLM (Zhipu AI) ──
    'glm-5.3':                       {'caps': {'text', 'thinking'},                'rpm': 60,  'latency': 3000},
    'glm-5.2':                       {'caps': {'text', 'thinking'},                'rpm': 60,  'latency': 3000, 'cost': 0.004},
    'glm-5.1':                       {'caps': {'text', 'thinking'},                'rpm': 60,  'latency': 3000, 'cost': 0.004},
    'glm-5.1-huawei':                {'caps': {'text', 'thinking'},                'rpm': 60,  'latency': 3000, 'cost': 0.004},
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

    # ── Tencent Hunyuan ──
    # hy4-preview (2026-08-28): 770B-A49B MoE, 1M context, high/no_think modes
    'hy4-preview':                   {'caps': {'text', 'thinking'},                'rpm': 30,  'latency': 3000, 'cost': 0.003},
    'hy3-preview':                   {'caps': {'text', 'thinking', 'cheap'},       'rpm': 30,  'latency': 3000, 'cost': 0.002},
    'hunyuan-2.0-thinking-20251109': {'caps': {'text', 'thinking', 'cheap'},       'rpm': 30,  'latency': 3000, 'cost': 0.003},
    'hunyuan-2.0-instruct-20251111': {'caps': {'text', 'cheap'},                   'rpm': 30,  'latency': 2500, 'cost': 0.002},
    'hunyuan-role-latest':           {'caps': {'text', 'cheap'},                   'rpm': 30,  'latency': 2500, 'cost': 0.002},

    # ── DeepSeek (additional snapshots served by Tencent TokenHub) ──
    'deepseek-v3.1-terminus':        {'caps': {'text', 'cheap'},                   'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'deepseek-r1-0528':              {'caps': {'text', 'thinking', 'cheap'},       'rpm': 30,  'latency': 3000, 'cost': 0.002},
    'deepseek-v3-0324':              {'caps': {'text', 'cheap'},                   'rpm': 60,  'latency': 2000, 'cost': 0.001},

    # ── GLM Turbo / Kimi older / MiniMax older — TokenHub catalog ──
    'glm-5-turbo':                   {'caps': {'text', 'cheap'},                   'rpm': 60,  'latency': 2000, 'cost': 0.002},
    # kimi-k3 (2026-07-17): native vision + native VIDEO understanding,
    # always-on thinking mode. See platform.kimi.com/docs/guide/use-kimi-vision-model.
    'kimi-k3':                       {'caps': {'text', 'vision', 'video', 'thinking', 'cheap'}, 'rpm': 60,  'latency': 3000, 'cost': 0.0083},
    'kimi-k2.6':                     {'caps': {'text', 'cheap'},                   'rpm': 30,  'latency': 3000, 'cost': 0.003},
    'kimi-k2.5':                     {'caps': {'text', 'cheap'},                   'rpm': 30,  'latency': 3000, 'cost': 0.002},
    'minimax-m2.5':                  {'caps': {'text', 'cheap'},                   'rpm': 60,  'latency': 2000, 'cost': 0.001},
    'minimax-m2.7':                  {'caps': {'text', 'cheap'},                   'rpm': 60,  'latency': 2000, 'cost': 0.001},

    # ── LongCat (internal, free) ──
    'LongCat-Flash-Thinking-2601':   {'caps': {'text', 'thinking', 'cheap'},       'rpm': 60,  'latency': 2000, 'cost': 0.0},
    'LongCat-Flash-Chat-2603':       {'caps': {'text', 'cheap'},                   'rpm': 60,  'latency': 1500, 'cost': 0.001},

    # ── OpenAI Codex (ChatGPT Plus subscription) ──
    'gpt-5.2-codex':                 {'caps': {'text', 'vision', 'thinking'},      'rpm': 10,  'latency': 5000, 'cost': 0.0},
    'gpt-5.1-codex-mini':            {'caps': {'text', 'vision', 'thinking'},      'rpm': 20,  'latency': 3000, 'cost': 0.0},
    'codex-mini':                    {'caps': {'text', 'vision', 'cheap'},         'rpm': 20,  'latency': 2000, 'cost': 0.0},

    # ── Image generation ──
    'gpt-image-1.5':                         {'caps': {'image_gen'},               'rpm': 10,  'latency': 30000, 'cost': 0.043},
    'gpt-image-2':                           {'caps': {'image_gen'},               'rpm': 10,  'latency': 30000, 'cost': 0.065},
    'gpt-image-1':                           {'caps': {'image_gen'},               'rpm': 10,  'latency': 30000, 'cost': 0.040},
    'gpt-image-1-mini':                      {'caps': {'image_gen'},               'rpm': 15,  'latency': 20000, 'cost': 0.015},
    'dall-e-3':                              {'caps': {'image_gen'},               'rpm': 5,   'latency': 30000, 'cost': 0.040},
    'gemini-3.1-flash-image-preview':        {'caps': {'image_gen'},               'rpm': 10,  'latency': 30000, 'cost': 0.015},
    'gemini-3-pro-image-preview':            {'caps': {'image_gen'},               'rpm': 10,  'latency': 30000, 'cost': 0.020},
    'gemini-2.5-flash-image':                {'caps': {'image_gen'},               'rpm': 10,  'latency': 30000, 'cost': 0.015},
    'gemini-2.0-flash-preview-image-generation': {'caps': {'image_gen'},           'rpm': 10,  'latency': 30000, 'cost': 0.010},

    # ── Speech-to-text (transcription) ──
    # Reference targets for the voice-input feature. Any provider exposing the
    # standard POST /v1/audio/transcriptions endpoint works once a slot carries
    # the 'transcription' capability — these are just pre-seeded metadata so a
    # configured model routes without a hand-written entry. Selected directly by
    # lib/transcription.py (NOT the chat picker); 'transcription' is a non-chat
    # cap (see dispatcher._NON_CHAT_CAPS).
    'gpt-4o-transcribe':             {'caps': {'transcription'},                   'rpm': 60,  'latency': 4000, 'cost': 0.006},
    'gpt-4o-mini-transcribe':        {'caps': {'transcription'},                   'rpm': 60,  'latency': 3000, 'cost': 0.003},
    'whisper-1':                     {'caps': {'transcription'},                   'rpm': 60,  'latency': 4000, 'cost': 0.006},
    'whisper-large-v3-turbo':        {'caps': {'transcription'},                   'rpm': 120, 'latency': 2000, 'cost': 0.0004},
    'whisper-large-v3':              {'caps': {'transcription'},                   'rpm': 120, 'latency': 3000, 'cost': 0.0004},
    # Doubao-Seed-ASR-2.0 (Volcengine Seed-ASR 2.0) — served on the Meituan
    # gateway's OpenAI-native multipart /audio/transcriptions surface.
    'Doubao-Seed-ASR-2.0':           {'caps': {'transcription'},                   'rpm': 60,  'latency': 3000, 'cost': 0.001},

    # ── Text-to-speech (tts) ──
    # Reference targets for spoken-audio output (paper podcast, read-aloud).
    # Any provider exposing the OpenAI-compatible POST /v1/audio/speech
    # endpoint works once a slot carries the 'tts' capability — these are
    # pre-seeded metadata for well-known PUBLIC model names only (same
    # pattern as the whisper-1 transcription block above). A deployment's
    # actual TTS model is registered via server config with
    # capabilities: ['tts']; the podcast code never hardcodes a model/voice.
    'tts-1':                         {'caps': {'tts'},                            'rpm': 60,  'latency': 3000, 'cost': 0.015},
    'tts-1-hd':                      {'caps': {'tts'},                            'rpm': 60,  'latency': 5000, 'cost': 0.030},
    'gpt-4o-mini-tts':               {'caps': {'tts'},                            'rpm': 60,  'latency': 3000, 'cost': 0.012},

    # ── Embeddings ──
    'text-embedding-v4':             {'caps': {'embedding'},                       'rpm': 100, 'latency': 500,  'cost': 0.001},
    'text-embedding-3-small':        {'caps': {'embedding'},                       'rpm': 60,  'latency': 500,  'cost': 0.001},
    'text-embedding-3-large':        {'caps': {'embedding'},                       'rpm': 60,  'latency': 500,  'cost': 0.001},
}
