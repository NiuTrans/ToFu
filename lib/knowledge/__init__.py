"""Local, opt-in knowledge base.

The public surface is intentionally small: documents are ingested through
``store.add_document`` and the model gets a single read-only
``search_knowledge`` tool when (and only when) the corpus is enabled and has
at least one indexed document.
"""

from .search import search
from .store import (
    add_document,
    delete_document,
    get_asset,
    get_activity,
    get_document_content,
    get_status,
    list_documents,
    reindex_document,
    read_asset,
    set_enabled,
    set_visual_enrichment,
    tool_available,
    visual_enrichment_enabled,
)

__all__ = [
    'add_document', 'delete_document', 'get_activity', 'get_asset', 'get_document_content',
    'get_status', 'list_documents', 'read_asset', 'reindex_document', 'search',
    'set_enabled', 'set_visual_enrichment', 'tool_available',
    'visual_enrichment_enabled',
]
