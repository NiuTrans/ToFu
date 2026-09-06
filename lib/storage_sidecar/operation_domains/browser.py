"""Declarative browser-observation storage operations."""

from lib.storage_sidecar import operations as ops


OPERATIONS = {
    "browser.site_observation.get": ops.OperationSpec(
        "query", False, ops._browser_site_observation_get),
    # Reconstructible counters deliberately avoid permanent command receipts;
    # ambiguous dispatched writes are surfaced and never retried by the client.
    "browser.site_observation.record": ops.OperationSpec(
        "command", False, ops._browser_site_observation_record),
}


__all__ = ["OPERATIONS"]
