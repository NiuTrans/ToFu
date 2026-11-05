"""Validate bounded summary failure evidence without retaining request material.

The additive receipt fields are owned by compaction_receipt_v1.schema.json.
``validate_summary_failure`` and ``validate_summary_attempts`` return detached
allowlisted JSON only. ``reconstructed_summary_failure`` additionally requires
an issued, current SummaryBudget and its current in-process usage projection;
configuration, copied task state and older attempts cannot mint an auth cause.
No credential resolution, exception-message parsing or provider I/O occurs here.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lib.llm.llm_errors import SubscriptionCredentialUnavailableError

SUMMARY_FAILURE_CODE = 'summary_credentials_unavailable'
MAX_SUMMARY_ATTEMPTS = 3
MAX_SUMMARY_ROUTE_ID_LENGTH = 200
SUMMARY_AUTH_ISSUES = frozenset({
    'codex_command_timeout', 'codex_command_unavailable',
    'codex_home_storage_full', 'codex_home_unavailable',
    'desktop_login_stale', 'desktop_login_unavailable', 'desktop_disconnected',
    'credential_source_unavailable', 'codex_credentials_unreadable',
    'credential_store_unsupported', 'invalid_codex_home', 'credential_unavailable',
})
SUMMARY_CREDENTIAL_SOURCES = frozenset({'local', 'desktop', 'tofu_oauth'})
# Match the existing typed source owners, not arbitrary exception attributes or
# configuration. Missing/unsupported/unreadable source formats are not declared
# transient merely because a new attempt might eventually succeed.
_TRANSIENT_AUTH_ISSUES = frozenset({
    'codex_command_timeout', 'codex_command_unavailable',
    'codex_home_storage_full', 'codex_home_unavailable',
    'desktop_login_stale', 'desktop_login_unavailable', 'desktop_disconnected',
    'credential_source_unavailable',
})
_FAILURE_FIELDS = frozenset({'code', 'authIssue', 'credentialSource', 'providerReached'})
_ATTEMPT_REQUIRED_FIELDS = frozenset({'modelId', 'providerId', 'status'})
_ATTEMPT_FIELDS = _ATTEMPT_REQUIRED_FIELDS | frozenset({
    'providerReached', 'authIssue', 'credentialSource'})


def _allowlisted(value, allowed) -> bool:
    return type(value) is str and value in allowed


def validate_summary_failure(value) -> dict | None:
    """Reject unknown fields/reasons outright; never clip or stringify them."""
    if (type(value) is not dict or len(value) != len(_FAILURE_FIELDS)
            or value.keys() != _FAILURE_FIELDS
            or not _allowlisted(value.get('code'), {SUMMARY_FAILURE_CODE})
            or not _allowlisted(value.get('authIssue'), SUMMARY_AUTH_ISSUES)
            or not _allowlisted(value.get('credentialSource'), SUMMARY_CREDENTIAL_SOURCES)
            or value.get('providerReached') is not False):
        return None
    return {key: value[key] for key in (
        'code', 'authIssue', 'credentialSource', 'providerReached')}


def _valid_route_id(value) -> bool:
    return (type(value) is str and 0 < len(value) <= MAX_SUMMARY_ROUTE_ID_LENGTH
            and all(ord(char) >= 32 and ord(char) != 127 for char in value))


def validate_summary_attempts(value) -> list[dict]:
    """Return at most three complete valid rows, or nothing for invalid input.

    Do not truncate an oversized list or filter invalid rows: doing so could
    hide later ingress/success and falsely certify an unsent credential outage.
    Provider ingress may stay unknown for generic failed attempts. Only verified
    success and typed credential-unavailable evidence require a boolean fact.
    """
    if type(value) is not list or not 0 < len(value) <= MAX_SUMMARY_ATTEMPTS:
        return []
    result = []
    for row in value:
        if (type(row) is not dict
                or not len(_ATTEMPT_REQUIRED_FIELDS) <= len(row) <= len(_ATTEMPT_FIELDS)
                or not _ATTEMPT_REQUIRED_FIELDS <= row.keys() <= _ATTEMPT_FIELDS
                or not _valid_route_id(row.get('modelId'))
                or not _valid_route_id(row.get('providerId'))
                or not _allowlisted(row.get('status'), {'failed', 'succeeded'})):
            return []
        if 'providerReached' in row and type(row['providerReached']) is not bool:
            return []
        if row['status'] == 'succeeded' and row.get('providerReached') is not True:
            return []
        if 'authIssue' in row or 'credentialSource' in row:
            if (not _allowlisted(row.get('authIssue'), SUMMARY_AUTH_ISSUES)
                    or not _allowlisted(row.get('credentialSource'), SUMMARY_CREDENTIAL_SOURCES)
                    or row['status'] != 'failed' or row.get('providerReached') is not False):
                return []
        result.append(dict(row))
    return result


def _failure_matches_attempts(failure: dict, attempts: list[dict]) -> bool:
    """An unsent overall failure cannot override recorded or unknown ingress."""
    return (all(row['status'] == 'failed' and row.get('providerReached') is False
                for row in attempts)
            and (not attempts or all(attempts[-1].get(key) == failure[key]
                                     for key in ('authIssue', 'credentialSource'))))


def reconstructed_summary_failure(
    task, *, summary_usage: Mapping | None = None,
) -> SubscriptionCredentialUnavailableError | None:
    """Rebuild the existing typed error only for current issued failure evidence.

    The request digest alone is not a trust token. A current usage projection
    carries the exact detached failure object issued by ``budget.project_usage``;
    shallow metadata transport preserves it, while config/deserialized/copy
    forgeries and earlier projections cannot reacquire that authority.
    """
    if (not isinstance(task, dict) or task.get('aborted')
            or not isinstance(summary_usage, Mapping)):
        return None
    from lib.tasks_pkg.compaction._summary_budget import _SummaryAttemptState

    state = task.get('_summaryAttemptBudget')
    if (type(state) is not _SummaryAttemptState or not state.is_current(task)
            or state.get('_summaryBudgetFailure') != SUMMARY_FAILURE_CODE
            or summary_usage.get('_summaryRequestKey') != state.get('requestKey')):
        return None
    failure, attempts = state.validated_metadata()
    projected_attempts = validate_summary_attempts(summary_usage.get('_summaryAttempts'))
    if (failure is None
            or summary_usage.get('_summaryFailure') is not state.projected_failure
            or validate_summary_failure(summary_usage.get('_summaryFailure')) != failure
            or ('_summaryAttempts' in summary_usage and not projected_attempts)
            or projected_attempts != attempts):
        return None
    from lib.llm.llm_errors import SubscriptionCredentialUnavailableError

    return SubscriptionCredentialUnavailableError(
        reason=failure['authIssue'], credential_source=failure['credentialSource'],
        transient=failure['authIssue'] in _TRANSIENT_AUTH_ISSUES)


__all__ = [
    'MAX_SUMMARY_ATTEMPTS', 'SUMMARY_FAILURE_CODE', 'SUMMARY_AUTH_ISSUES',
    'SUMMARY_CREDENTIAL_SOURCES', 'validate_summary_failure',
    'validate_summary_attempts', 'reconstructed_summary_failure',
]
