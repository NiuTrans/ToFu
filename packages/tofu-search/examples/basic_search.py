#!/usr/bin/env python3
"""Basic search example — no LLM required.

Just searches, fetches pages, and reranks by BM25.
The LLM content filter is automatically skipped when no LLM is configured.
"""

from tofu_search import format_results, search  # noqa: F401  (format_results shown below)

results = search("Python asyncio tutorial", max_results=3)

print(f"Got {len(results)} results:\n")
for i, r in enumerate(results, 1):
    has_content = bool(r.get('full_content'))
    content_len = len(r['full_content']) if has_content else 0
    print(f"  [{i}] {r['title']}")
    print(f"      URL: {r['url']}")
    print(f"      Source: {r['source']}")
    print(f"      Content: {'%d chars' % content_len if has_content else 'N/A'}")
    print()

# You can also get the formatted text output:
# text = format_results(results)
# print(text)
