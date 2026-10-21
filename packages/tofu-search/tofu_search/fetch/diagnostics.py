"""Safe public fetch-failure observations for delivery adapters.

Entry point: fetch_failure_observation. Core diagnostics may contain private
exception details and headers; this projection admits only known categories
and status values encoded in those categories. No I/O or optional dependency.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

_FETCH_FAILURE_NOTES = {
    'bad_scheme': 'Only HTTP(S) URLs can be read.',
    'bad_url': 'The URL has no valid host.',
    'ssrf_blocked': 'The URL was refused by the network-address policy.',
    'skip_domain': 'This domain is excluded by the configured fetch policy.',
    'binary_media': 'The URL points to binary media rather than readable text.',
    'circuit_open': 'The source is temporarily paused after repeated failures.',
    'guard_error': 'The URL could not be admitted by the fetch guard.',
    'refused': 'The URL was refused before a request was made.',
    'auth_replay_rejected': 'The available authenticated access did not return readable content.',
    'timeout': 'The page request timed out.',
    'connection_error': 'A connection to the source could not be established.',
    'pdf_extraction_error': 'PDF text extraction was unavailable or failed.',
    'bot_wall': 'The source returned an access challenge or sign-in page.',
    'spa_shell': 'The page returned only an application shell.',
    'empty_extract': 'No readable text could be extracted.',
    'deadline': 'The fetch time budget was exhausted.',
}


def fetch_failure_observation(diagnostics: Mapping[str, object] | None) -> dict:
    """Return static recovery facts without granting retry or access authority.

    Unknown causes retain a generic message. A separate raw HTTP status never
    overrides the reason, so conflicting or untrusted fields cannot fabricate
    another cause. The input remains untouched for its diagnostic owner.
    """
    cause = diagnostics.get('reason') if diagnostics is not None else None
    note = _FETCH_FAILURE_NOTES.get(cause) if isinstance(cause, str) else None
    status = None
    if isinstance(cause, str) and re.fullmatch(r'http_[1-5][0-9]{2}', cause):
        status = int(cause[5:])
        note = f'The source returned HTTP {status}.'
    if not note:
        return {'message': 'The fetch failed or the page had no extractable text.'}
    result = {'reason': cause, 'message': note}
    if status is not None:
        result['http_status'] = status
    return result
