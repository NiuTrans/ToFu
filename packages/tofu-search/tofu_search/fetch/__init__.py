"""tofu_search.fetch — Content fetching, extraction, and caching package.

The package also loads ``content_filter``, which the ``tofu_search`` facade
needs: a thread that first imports that module while this package is absent
must complete it here, before CPython makes it wait for the facade
(``tests/test_import_graph.py``).
"""

from importlib import import_module

from tofu_search.fetch.core import (
    extract_urls_from_text,
    fetch_contents_for_results,
    fetch_page_content,
    fetch_urls,
    get_publish_date_from_url,
)
from tofu_search.fetch.readers import (
    SiteReader,
    get_reader,
    register_reader,
)

import_module('tofu_search.fetch.content_filter')

__all__ = [
    'fetch_page_content',
    'get_publish_date_from_url',
    'fetch_contents_for_results',
    'fetch_urls',
    'extract_urls_from_text',
    'SiteReader',
    'register_reader',
    'get_reader',
]
