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
SUMMARY_FAILURE_CODES = frozenset({
    SUMMARY_FAILURE_CODE, 'summary_rate_limited', 'summary_provider_unavailable',
    'summary_permission_denied', 'summary_route_unavailable', 'summary_incomplete',
    'summary_empty', 'summary_cancelled', 'summary_dispatch_failed',
    'summary_route_revalidation_failed',
})
# JSON consumers represent receipt counts as JavaScript safe integers.
MAX_SUMMARY_DIAGNOSTIC_COUNT = 2**53 - 1
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
    """Detach safe diagnostics; only the strict credential shape proves unsent auth."""
    if type(value) is not dict or not _allowlisted(value.get('code'), SUMMARY_FAILURE_CODES):
        return None
    if value['code'] == SUMMARY_FAILURE_CODE:
        if (value.keys() != _FAILURE_FIELDS
                or not _allowlisted(value.get('authIssue'), SUMMARY_AUTH_ISSUES)
                or not _allowlisted(value.get('credentialSource'), SUMMARY_CREDENTIAL_SOURCES)
                or value.get('providerReached') is not False):
            return None
        return {key: value[key] for key in (
            'code', 'authIssue', 'credentialSource', 'providerReached')}
    if not value.keys() <= {'code', 'statusCode', 'attempts', 'limit'}:
        return None
    if ('statusCode' in value and (type(value['statusCode']) is not int
                                  or not 100 <= value['statusCode'] <= 599)):
        return None
    for key in ('attempts', 'limit'):
        if key in value and (type(value[key]) is not int
                or not (1 if key == 'limit' else 0) <= value[key] <= MAX_SUMMARY_DIAGNOSTIC_COUNT):
            return None
    if 'limit' in value and ('attempts' not in value or value['attempts'] > value['limit']):
        return None
    return {key: value[key] for key in ('code', 'statusCode', 'attempts', 'limit') if key in value}


def classify_summary_failure(error: BaseException, *, provider_completed=False) -> dict:
    """Classify existing typed failures without inspecting text or arbitrary attributes.

    Diagnostic facts do not create a retry allowance, route, or credential grant.
    Actual 429 counts come only from the dispatcher's typed caller-owned budget.
    A local failure after a completed transport is not another provider outage.
    """
    from lib.llm.llm_errors import PermissionError_, RateLimitError, RetryableAPIError
    from lib.llm.stream_result import UnverifiedProviderStreamError
    from lib.llm_dispatch._api_errors import DispatchNoAdmissibleSlot, DispatchRateLimitBudgetExceeded
    from lib.model_routing.domain import ModelRoutingError
    from lib.tasks_pkg.compaction._layer2._summary_transport import IncompleteSummaryError

    result = {'code': 'summary_dispatch_failed'}
    if isinstance(error, (IncompleteSummaryError, UnverifiedProviderStreamError)):
        return {'code': 'summary_incomplete'}
    if provider_completed:
        return result
    if isinstance(error, DispatchNoAdmissibleSlot):
        return {'code': 'summary_route_unavailable', 'attempts': 0}
    if isinstance(error, ModelRoutingError):
        return {'code': 'summary_route_revalidation_failed'
                if error.kind == 'model_route_changed' else 'summary_route_unavailable'}
    if isinstance(error, (PermissionError_, RateLimitError, RetryableAPIError)):
        status = error.status_code
        if type(status) is int and 100 <= status <= 599:
            result['statusCode'] = status
        if isinstance(error, PermissionError_):
            if status in (0, 401, 403):
                result['code'] = 'summary_permission_denied'
        elif isinstance(error, RetryableAPIError):
            if status == 0 or type(status) is int and 500 <= status <= 599:
                result['code'] = 'summary_provider_unavailable'
        elif error.is_gateway or type(status) is int and 500 <= status <= 599:
            result['code'] = 'summary_provider_unavailable'
        elif not error.is_quota and status in (0, 429):
            result['code'] = 'summary_rate_limited'
    if isinstance(error, DispatchRateLimitBudgetExceeded):
        counted = {**result, 'attempts': error.attempts, 'limit': error.limit}
        if validate_summary_failure(counted) is not None:
            result = counted
    return result


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
    """Only credential recovery needs whole-attempt proof of unsent ingress."""
    if failure['code'] != SUMMARY_FAILURE_CODE:
        return True  # Display diagnostics never reconstruct credential authority.
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
    if (failure is None or failure.get('code') != SUMMARY_FAILURE_CODE
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
    'MAX_SUMMARY_ATTEMPTS', 'SUMMARY_FAILURE_CODE', 'SUMMARY_FAILURE_CODES',
    'MAX_SUMMARY_DIAGNOSTIC_COUNT', 'classify_summary_failure', 'SUMMARY_AUTH_ISSUES',
    'SUMMARY_CREDENTIAL_SOURCES', 'validate_summary_failure',
    'validate_summary_attempts', 'reconstructed_summary_failure',
]
