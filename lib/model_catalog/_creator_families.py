"""Pure creator-family taxonomy retained for one-way v1-to-v2 migration.

The retired model-catalog enrichment stack used the same tables while fetching
third-party leaderboards.  Model-routing migration needs only deterministic,
offline identity attribution, so its complete authority lives here without
network, cache, or server-config dependencies.
"""

CREATOR_PROVIDERS: dict[str, tuple[str, ...]] = {
    'openai': ('openai', 'azure'),
    'anthropic': ('anthropic',),
    'google': ('google', 'google-vertex'),
    'deepseek': ('deepseek',),
    'moonshot': ('moonshotai', 'moonshotai-cn'),
    'zhipu': ('zai', 'zhipuai'),
    'minimax': ('minimax', 'minimax-cn'),
    'bytedance': ('volcengine',),
    'meituan': ('longcat',),
    'alibaba': ('alibaba', 'alibaba-cn'),
    'meta': ('meta', 'llama'),
    'mistral': ('mistral',),
    'xai': ('xai',),
    'stepfun': ('stepfun', 'stepfun-ai'),
    'cohere': ('cohere',),
    'nvidia': ('nvidia',),
    'microsoft': ('azure',),
    'amazon': ('nova', 'amazon-bedrock'),
    'perplexity': ('perplexity',),
    'xiaomi': ('xiaomi',),
    'bailing': ('bailing',),
}

FAMILY_BRAND_KEYS: dict[str, tuple[str, ...]] = {
    'openai': ('openai',),
    'anthropic': ('anthropic', 'claude'),
    'google': ('google', 'gemini', 'gemma'),
    'deepseek': ('deepseek',),
    'moonshot': ('moonshot', 'kimi'),
    'zhipu': ('zhipu', 'glm', 'zai'),
    'minimax': ('minimax', 'abab', 'hailuo'),
    'bytedance': ('bytedance', 'doubao', 'dola', 'seed'),
    'meituan': ('meituan', 'longcat'),
    'alibaba': ('alibaba', 'qwen', 'tongyi', 'qwq'),
    'meta': ('meta', 'llama'),
    'mistral': ('mistral', 'mixtral', 'ministral', 'codestral', 'pixtral',
                'magistral', 'voxtral'),
    'xai': ('xai', 'grok'),
    'stepfun': ('stepfun', 'step'),
    'cohere': ('cohere', 'command'),
    'nvidia': ('nvidia', 'nemotron'),
    'microsoft': ('microsoft', 'phi'),
    'amazon': ('amazon', 'nova'),
    'perplexity': ('perplexity', 'sonar'),
    'xiaomi': ('xiaomi', 'mimo'),
    'bailing': ('bailing',),
    'thinkingmachines': ('thinkingmachines', 'thinking machines', 'inkling'),
}

FAMILY_ID_PREFIXES: dict[str, tuple[str, ...]] = {
    'openai': ('gpt', 'o1', 'o3', 'o4', 'chatgpt', 'codex',
               'text-embedding-', 'deep-research-'),
    'anthropic': ('claude',),
    'google': ('gemini', 'gemma', 'lyria'),
    'deepseek': ('deepseek',),
    'moonshot': ('kimi', 'moonshot'),
    'zhipu': ('glm', 'chatglm', 'cog'),
    'minimax': ('minimax', 'abab', 'hailuo'),
    'bytedance': ('doubao', 'seed-', 'dola', 'skylark'),
    'meituan': ('longcat', 'meituan'),
    'alibaba': ('qwen', 'qwq', 'qvq', 'tongyi'),
    'meta': ('llama', 'meta-', 'muse-'),
    'mistral': ('mistral', 'mixtral', 'ministral', 'codestral', 'pixtral',
                'magistral', 'devstral', 'labs-devstral', 'voxtral'),
    'xai': ('grok',),
    'stepfun': ('step',),
    'cohere': ('command', 'cohere'),
    'nvidia': ('nemotron',),
    'microsoft': ('phi-',),
    'amazon': ('nova-', 'amazon'),
    'perplexity': ('sonar',),
    'xiaomi': ('mimo',),
    'bailing': ('ling-',),
    'thinkingmachines': ('thinkingmachines/', 'inkling'),
}


__all__ = [
    'CREATOR_PROVIDERS',
    'FAMILY_BRAND_KEYS',
    'FAMILY_ID_PREFIXES',
]
