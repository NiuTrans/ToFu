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
    # Claude Opus 4.8 — aws gateway + direct API + Bedrock-native IDs are interchangeable
    {'aws.claude-opus-4.8', 'claude-opus-4-8', 'us.anthropic.claude-opus-4-8-v1:0'},
    # Claude Opus 4.7 — aws gateway + direct API + Bedrock-native IDs are interchangeable
    {'aws.claude-opus-4.7', 'claude-opus-4-7', 'us.anthropic.claude-opus-4-7-v1:0'},
    # Claude Opus 4.6 — aws, vertex, direct API, Bedrock-native names are interchangeable
    {'aws.claude-opus-4.6', 'aws.claude-opus-4.6-b', 'vertex.claude-opus-4.6',
     'claude-opus-4-20250514', 'claude-opus-4-6-20250514', 'claude-opus-4-6',
     'us.anthropic.claude-opus-4-6-v1:0'},
    # Claude Sonnet 4.6 — aws gateway vs direct API name vs Bedrock-native
    {'aws.claude-sonnet-4.6', 'claude-sonnet-4-20250514', 'claude-sonnet-4-6-20250514',
     'claude-sonnet-4-6', 'us.anthropic.claude-sonnet-4-6-v1:0'},
    # DeepSeek V3.2 — Meituan gateway mirrors across Tencent/Baidu/Huawei/Doubao clouds
    {'deepseek-v3.2-tencent', 'deepseek-v3.2-baidu', 'deepseek-v3.2-huawei', 'deepseek-v3.2-doubao'},
    # DeepSeek V4 Flash — direct DeepSeek API + Meituan gateway Huawei-cloud mirror
    {'deepseek-v4-flash', 'deepseek-v4-flash-huawei'},
    # GLM-5.1 — Meituan gateway default + Huawei-cloud mirror
    {'glm-5.1', 'glm-5.1-huawei'},
]

MODEL_ALIASES: dict[str, set[str]] = {}
for _group in MODEL_ALIAS_GROUPS:
    for _m in _group:
        MODEL_ALIASES[_m] = _group
