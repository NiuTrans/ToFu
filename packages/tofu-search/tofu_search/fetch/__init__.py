"""tofu_search.fetch — Content fetching, extraction, and caching package."""

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
