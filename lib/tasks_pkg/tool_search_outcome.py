"""Bounded local search evidence for audit aggregation and request attribution.

Entry point: audit_search_outcome; task handlers supply catalog/visibility facts.
Depends on the shared audit sink; diagnostics never change search or authority.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from lib.log import audit_log, get_logger
from lib.tools.gateway import GATEWAY_TOOL_NAMES

logger = get_logger(__name__)


def audit_search_outcome(
        task: dict[str, Any], arguments: dict, result: dict | None, *,
        source: str, call_id: str, llm_round: int, catalog_names,
        visible_names=(), disclosed_names=(), exception: Exception | None = None,
        duration_ms: float = 0) -> None:
    """Emit one observation per actual search, including successes as denominator.

    Query text is deliberately retained (512 chars); transcript/schema bodies and
    exception messages are omitted. Identifiers link to existing request evidence.
    A missing catalog name is evidence of absence, not proof of contamination.
    """
    try:
        result = result or {}
        query = str(arguments.get('query') or '')
        name = query.strip().removeprefix('+').lower()
        names = {str(value) for value in catalog_names
                 if value and value not in GATEWAY_TOOL_NAMES}
        visible_lower = {str(value).strip().lower() for value in visible_names}
        disclosed_lower = {str(value).strip().lower() for value in disclosed_names}
        visible_count = disclosed_count = 0
        name_in_catalog = False
        for catalog_name in names:
            lower_name = catalog_name.lower()
            name_in_catalog |= lower_name == name
            if lower_name in visible_lower:
                visible_count += 1
            elif lower_name in disclosed_lower:
                disclosed_count += 1
        hidden_count = len(names) - visible_count - disclosed_count
        exact = name_in_catalog or (
            '_' in name and bool(re.fullmatch(r'[a-z0-9_.:-]+', name)))
        outcome = 'matched'
        reason = 'matched'
        if exception is not None or result.get('fail_open'):
            outcome, reason = 'error', 'retrieval_exception'
        elif result.get('status') != 'ok':
            outcome = 'error'
            reason = str((result.get('error') or {}).get('code') or 'search_error')
        elif not result.get('items'):
            outcome = 'empty'
            availability = result.get('capability_availability') or []
            if availability:
                reason = str(availability[0].get('reason_code') or 'capability_unavailable')
            elif result.get('missing_name'):
                reason = 'exact_name_absent'
            elif result.get('already_visible'):
                reason = 'already_visible'
            elif result.get('ambiguous_names'):
                reason = 'ambiguous_name'
            elif result.get('available_namespaces'):
                reason = 'namespace_absent'
            elif result.get('total'):
                reason = 'page_exhausted'
            elif name_in_catalog and arguments.get('namespace'):
                reason = 'namespace_filtered_name'
            elif not names:
                reason = 'catalog_empty'
            elif not hidden_count:
                reason = 'all_tools_disclosed'
            else:
                reason = 'no_match'
        detail = {
            'schema_version': 1, 'backend': 'local', 'source': source,
            'outcome': outcome, 'reason': reason,
            'query': query[:512], 'query_truncated': len(query) > 512,
            'namespace': str(arguments.get('namespace') or '')[:128],
            'has_cursor': bool(arguments.get('cursor')),
            'query_is_tool_name': exact,
            'query_name_in_catalog': name_in_catalog,
            'catalog_count': len(names), 'visible_count': visible_count,
            'disclosed_count': disclosed_count, 'hidden_count': hidden_count,
            'catalog_names_sha256': hashlib.sha256(
                '\x00'.join(sorted(names)).encode()).hexdigest(),
            'result_count': len(result.get('items') or []),
            'total': int(result.get('total') or 0),
            'fail_open': bool(result.get('fail_open')),
            'duration_ms': round(duration_ms, 3),
            'task_id': str(task.get('id') or '')[:128],
            'conv_id': str(task.get('convId') or '')[:128],
            'user_id': str(task.get('_userId') or '')[:128],
            'attempt_id': str(task.get('_attemptId') or task.get('attemptId') or '')[:128],
            'model': str(task.get('model') or '')[:128],
            'call_id': str(call_id)[:128], 'llm_round': llm_round,
        }
        resolved = (result.get('resolved_name') or result.get('already_visible')
                    or result.get('already_disclosed'))
        if resolved:
            detail['resolved_name'] = str(resolved)[:128]
        if result.get('repeated_query'):
            detail['repeated_query'] = int(result['repeated_query'])
        if exception is not None:
            detail['exception_type'] = type(exception).__name__[:128]
        audit_log('tool_search_outcome', **detail)
    except Exception as exc:
        logger.debug('[tool_search_outcome] audit skipped: %s', type(exc).__name__)
