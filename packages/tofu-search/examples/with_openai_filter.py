#!/usr/bin/env python3
"""Search with OpenAI-compatible LLM content filter.

Set your API key before running:
    export OPENAI_API_KEY=sk-...
    python examples/with_openai_filter.py
"""

import os
import sys

from tofu_search import configure, search

api_key = os.environ.get('OPENAI_API_KEY', '')
if not api_key:
    print("Set OPENAI_API_KEY env var first:")
    print("  export OPENAI_API_KEY=sk-...")
    sys.exit(1)

configure(
    llm_api_key=api_key,
    llm_base_url="https://api.openai.com/v1",
    llm_model="gpt-4o-mini",
)

results = search("latest Python 3.13 features", max_results=3)

print(f"Got {len(results)} results (with LLM filtering):\n")
for i, r in enumerate(results, 1):
    has_content = bool(r.get('full_content'))
    print(f"  [{i}] {r['title']}")
    print(f"      URL: {r['url']}")
    if has_content:
        preview = r['full_content'][:200].replace('\n', ' ')
        print(f"      Content: {preview}...")
    print()
