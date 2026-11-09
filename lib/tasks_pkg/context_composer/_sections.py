"""Composer facade for the shared, dependency-free context-section identity."""
from lib.llm_sanitize.context_sections import (  # noqa: F401
    ContextSection, _canonical_section, _strip_to_body, block_content_hash,
    context_sections, remove_managed_section,
)
