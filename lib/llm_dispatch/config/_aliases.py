"""lib/llm_dispatch/config/_aliases.py — Model alias groups & lookup map.

``MODEL_ALIAS_GROUPS`` lists sets of interchangeable model IDs (e.g. a
Claude model reachable through multiple gateway prefixes).  When
``prefer_model`` is one of these, any model in the same group is treated
as "preferred".  ``MODEL_ALIASES`` is the flattened member -> group map.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# ══════════════════════════════════════════════════════════════
#  Model alias groups: models within the same group are interchangeable
#  When prefer_model is one of these, any model in the same group is "preferred".
#  This benefits anyone routing Claude through multiple gateway prefixes.
# ══════════════════════════════════════════════════════════════
MODEL_ALIAS_GROUPS = [
    # Anthropic Fable 5 — aws gateway + direct API + Bedrock-native + YourProvider-gateway IDs are interchangeable
    {'aws.fable-5', 'fable-5', 'us.anthropic.fable-5-v1:0', 'claude-fable-5'},
    # Anthropic Fable 5.1 — aws gateway + direct API + Bedrock-native IDs are interchangeable
    {'aws.fable-5.1', 'fable-5.1', 'us.anthropic.fable-5-1-v1:0', 'claude-fable-5-1'},
    # Claude Opus 5 — logical id + the YourProvider evaDaily gateway spelling
    {'claude-opus-5', 'yuju-claude-opus-5-evaDaily'},
    # Claude Opus 4.8 — logical id + aws gateway + direct API + Bedrock-native IDs are interchangeable
    {'claude-opus-4.8', 'aws.claude-opus-4.8', 'claude-opus-4-8',
     'us.anthropic.claude-opus-4-8-v1:0', 'yuju-claude-opus-4.8-evaDaily'},
    # Claude Opus 4.7 — logical id + aws gateway + direct API + Bedrock-native IDs are interchangeable
    {'claude-opus-4.7', 'aws.claude-opus-4.7', 'claude-opus-4-7',
     'us.anthropic.claude-opus-4-7-v1:0', 'yuju-claude-opus-4.7-evaDaily'},
    # Claude Opus 4.6 — aws, vertex, direct API, Bedrock-native names are interchangeable
    {'claude-opus-4.6', 'aws.claude-opus-4.6', 'aws.claude-opus-4.6-b', 'vertex.claude-opus-4.6',
     'claude-opus-4-20250514', 'claude-opus-4-6-20250514', 'claude-opus-4-6',
     'us.anthropic.claude-opus-4-6-v1:0'},
    # Claude Sonnet 4.6 — aws gateway vs direct API name vs Bedrock-native
    {'claude-sonnet-4.6', 'aws.claude-sonnet-4.6', 'claude-sonnet-4-20250514',
     'claude-sonnet-4-6-20250514', 'claude-sonnet-4-6',
     'us.anthropic.claude-sonnet-4-6-v1:0'},
    # DeepSeek V4.1 Flash (2026-09-10) — canonical API id 'deepseek-flash';
    # the legacy v4-flash spellings route to it. Direct API + gateway mirrors.
    {'deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-huawei', 'deepseek-v4-flash-tencent',
     'deepseek-v4-flash-yourprovider'},
    # DeepSeek V4 Pro — direct DeepSeek API + YourProvider gateway Tencent-cloud mirror
    {'deepseek-v4-pro', 'deepseek-v4-pro-tencent', 'deepseek-v4-pro-0813'},
    # GLM-5.1 — YourProvider gateway default + Huawei-cloud mirror
    {'glm-5.1', 'glm-5.1-huawei'},
]

MODEL_ALIASES: dict[str, set[str]] = {}
for _group in MODEL_ALIAS_GROUPS:
    for _m in _group:
        MODEL_ALIASES[_m] = _group
