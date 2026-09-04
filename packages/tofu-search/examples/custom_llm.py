#!/usr/bin/env python3
"""Search with a custom LLM callable.

Demonstrates how to provide your own LLM function for content filtering.
"""

from tofu_search import configure, search


def my_echo_llm(messages, **kwargs):
    """A dummy LLM that always returns [USEFUL] + the raw content.

    In practice, you'd call your own LLM API here. The function receives:
    - messages: list[dict] — OpenAI-format messages
    - kwargs may include: stop, temperature, timeout
    Must return a string.
    """
    # Just echo back — real implementation would call your LLM
    user_msg = messages[-1]['content'] if messages else ''
    # Find the raw content section
    marker = '--- Raw page content'
    idx = user_msg.find(marker)
    if idx >= 0:
        content_start = user_msg.find('---\n', idx) + 4
        return '[USEFUL]\n' + user_msg[content_start:]
    return '[USEFUL]\n' + user_msg


configure(llm_function=my_echo_llm)

results = search("Python web frameworks comparison", max_results=2)

print(f"Got {len(results)} results (with custom LLM filter):\n")
for i, r in enumerate(results, 1):
    print(f"  [{i}] {r['title']}: {r['url']}")
    if r.get('full_content'):
        print(f"      Content length: {len(r['full_content'])} chars")
    print()
