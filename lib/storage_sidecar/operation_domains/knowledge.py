"""Knowledge corpus operation registrations."""

from lib.storage_sidecar import operations as ops


OPERATIONS = {
    "knowledge.document.list": ops.OperationSpec(
        "query", False, ops._knowledge_document_list),
    "knowledge.document.get": ops.OperationSpec(
        "query", False, ops._knowledge_document_get),
    "knowledge.document.metadata": ops.OperationSpec(
        "query", False, ops._knowledge_document_metadata),
    "knowledge.document.assets": ops.OperationSpec(
        "query", False, ops._knowledge_document_assets),
    "knowledge.document.content": ops.OperationSpec(
        "query", False, ops._knowledge_document_content),
    "knowledge.document.find_digest": ops.OperationSpec(
        "query", False, ops._knowledge_document_find_digest),
    "knowledge.document.create": ops.OperationSpec(
        "command", True, ops._knowledge_document_create),
    "knowledge.document.replace": ops.OperationSpec(
        "command", True, ops._knowledge_document_replace),
    "knowledge.document.patch": ops.OperationSpec(
        "command", True, ops._knowledge_document_patch),
    "knowledge.document.delete": ops.OperationSpec(
        "command", True, ops._knowledge_document_delete),
    "knowledge.settings.get": ops.OperationSpec(
        "query", False, ops._knowledge_settings_get),
    "knowledge.settings.patch": ops.OperationSpec(
        "command", True, ops._knowledge_settings_patch),
    "knowledge.availability": ops.OperationSpec(
        "query", False, ops._knowledge_availability),
    "knowledge.catalog": ops.OperationSpec(
        "query", False, ops._knowledge_catalog),
    "knowledge.search.candidates": ops.OperationSpec(
        "query", False, ops._knowledge_search_candidates),
    "knowledge.asset.get": ops.OperationSpec(
        "query", False, ops._knowledge_asset_get),
    "knowledge.enrichment.activity": ops.OperationSpec(
        "query", False, ops._knowledge_enrichment_activity),
    "knowledge.enrichment.owners": ops.OperationSpec(
        "query", False, ops._knowledge_enrichment_owners),
    "knowledge.asset.claim": ops.OperationSpec(
        "command", True, ops._knowledge_asset_claim),
    "knowledge.asset.update": ops.OperationSpec(
        "command", True, ops._knowledge_asset_update),
    "knowledge.assets.mark_no_vision": ops.OperationSpec(
        "command", True, ops._knowledge_assets_mark_no_vision),
    "knowledge.owner.clear": ops.OperationSpec(
        "command", True, ops._knowledge_owner_clear),
}


__all__ = ["OPERATIONS"]
