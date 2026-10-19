"""Identical request money scenarios against concurrent PostgreSQL authority."""
import pytest
from tests.storage import test_native_billing_admission_postgres as postgres_contract
from tests.billing.test_direct_usage_recovery import (
    test_unknown_or_partial_usage_is_never_a_zero_invoice,
    test_cold_recovery_sums_attempts_and_never_reprices_or_double_charges,
    test_explicit_zero_is_complete_and_private_diagnostics_are_not_stored,
    test_refund_and_admission_have_only_one_winner,
    test_lost_admission_ack_never_grants_provider_entry,
    test_raw_ledger_append_cannot_forge_request_evidence,
    test_foreign_owner_and_changed_reservation_are_rejected,
    test_proven_unsent_attempt_refunds_but_unverified_counts_do_not,
    test_known_counters_from_failed_transport_cannot_settle,
    test_missing_price_retains_hold,
    test_oversized_counter_evidence_retains_hold,
)

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.api
application_storage = postgres_contract.application_storage
__all__ = [name for name in globals() if name.startswith('test_')]
