"""Local, opt-in knowledge base.

The public surface is intentionally small: documents are ingested through
``store.add_document`` and the model gets a single read-only
``search_knowledge`` tool when (and only when) the corpus is enabled and has
at least one indexed document.
"""

from .search import search
from .store import (
    add_document,
    create_media_source,
    delete_document,
    get_asset,
    get_activity,
    get_document_content,
    get_document_metadata,
    get_status,
    list_documents,
    list_document_assets,
    patch_media_metadata,
    reindex_document,
    read_asset,
    read_source_path,
    remove_library_document,
    replace_media_evidence,
    search_document_candidates,
    set_document_scope,
    set_enabled,
    set_visual_enrichment,
    tool_available,
    visual_enrichment_enabled,
)

__all__ = [
    'add_document', 'create_media_source', 'delete_document', 'get_activity',
    'get_asset', 'get_document_content', 'get_document_metadata', 'get_status',
    'list_document_assets', 'list_documents', 'patch_media_metadata',
    'read_asset', 'read_source_path', 'reindex_document',
    'remove_library_document', 'replace_media_evidence', 'search',
    'search_document_candidates', 'set_document_scope',
    'set_enabled', 'set_visual_enrichment', 'tool_available',
    'visual_enrichment_enabled',
]
