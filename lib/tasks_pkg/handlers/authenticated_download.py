"""Safe fallback for model-authored cookie-bearing shell file downloads.

The canonical capability is ``browser_download_url_to_server``. This adapter only
recognizes a narrow legacy model behavior before ``run_command`` spawns: a
single GET-like curl/wget file download carrying cookies.  It discards the
cookie material, reuses the task's explicit browser identity, and invokes the
same acquisition owner as the canonical tool.  It never becomes a general
shell parser or a second transfer implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
import json

from lib.browser.log_safety import text_for_log, url_for_log
from lib.log import get_logger
from lib.project_mod.download_intent import (
    parse_authenticated_download_command,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class AuthenticatedDownloadRedirect:
    tool_content: str
    display_command: str
    badge: str
    ok: bool
    receipt: dict | None = None


def _safe_requested_output(value: str) -> str:
    return text_for_log(value or '', max_chars=240) or '[not specified]'


def maybe_redirect_authenticated_download(
    *,
    task: dict,
    cfg: dict,
    command: str,
) -> AuthenticatedDownloadRedirect | None:
    """Redirect one recognized cookie download, block ambiguity, else ``None``."""
    intent = parse_authenticated_download_command(command)
    if intent is None:
        return None

    display_command = (
        f'[cookie-authenticated {intent.downloader} file download intercepted]')
    if intent.block_reason:
        reason = text_for_log(intent.block_reason, max_chars=260)
        tool_content = (
            f'$ {display_command}\n'
            'The command was blocked before subprocess spawn: browser cookie '
            f'material may not be replayed through shell HTTP. Reason: {reason}.\n'
            'Call browser_download_url_to_server({"url":"<exact file URL>"}) instead; '
            'it automatically selects server HTTP or the logged-in browser.\n'
            '[exit code: 2]'
        )
        logger.warning('[DownloadRedirect] blocked ambiguous cookie-bearing %s '
                       'download before shell spawn: %s',
                       intent.downloader, reason)
        return AuthenticatedDownloadRedirect(
            tool_content=tool_content,
            display_command=display_command,
            badge='cookie download blocked',
            ok=False,
        )

    from lib.search_bridge import bind_search_browser
    from lib.tasks_pkg.handlers.search import _core as search_core

    with bind_search_browser(
            user_id=task.get('_userId', '') or '',
            client_id=(cfg or {}).get('browserClientId') or '',
            required_capabilities=('file_export',)):
        result = search_core.download_url_to_server(
            intent.url, owner_user_id=task.get('_userId', '') or '')

    requested_output = _safe_requested_output(intent.requested_output)
    if result.get('error_code'):
        error = {
            'code': result['error_code'],
            'message': result.get('error_msg') or 'Download failed.',
            'retryable': bool(result.get('retryable')),
            'nextAction': result.get('next_action') or (
                'Use browser_download_url_to_server; do not retry with cookie replay.'),
        }
        tool_content = (
            f'$ {display_command}\n'
            'The cookie-bearing shell command was not executed. Its file '
            'acquisition was automatically routed to browser_download_url_to_server, '
            'which failed with:\n'
            f'{json.dumps(error, ensure_ascii=False, sort_keys=True)}\n'
            '[exit code: 1]'
        )
        logger.info('[DownloadRedirect] canonical acquisition failed for %s: %s',
                    url_for_log(intent.url), result['error_code'])
        return AuthenticatedDownloadRedirect(
            tool_content=tool_content,
            display_command=display_command,
            badge=result['error_code'],
            ok=False,
        )

    receipt = {
        'location': 'server_staging',
        'path': result['saved_path'],
        'sizeBytes': int(result.get('size_bytes') or 0),
        'sha256': result.get('sha256') or '',
        'contentType': result.get('content_type') or '',
        'transport': result.get('transport') or '',
        'temporary': True,
        'requestedDestination': requested_output,
        'destinationWritten': False,
        'nextAction': (
            'The original -o/-O destination was intentionally not written by '
            'the transport redirect. If it is the user-approved final path, '
            'copy/move this staging file there with an authorized filesystem '
            'operation and verify the destination.'
        ),
    }
    tool_content = (
        f'$ {display_command}\n'
        'The cookie-bearing shell command was not executed. The file was '
        'automatically fetched through the canonical server download path:\n'
        f'{json.dumps(receipt, ensure_ascii=False, sort_keys=True)}\n'
        '[exit code: 0]'
    )
    logger.info('[DownloadRedirect] safely staged %d bytes via %s for %s',
                receipt['sizeBytes'], receipt['transport'],
                url_for_log(intent.url))
    return AuthenticatedDownloadRedirect(
        tool_content=tool_content,
        display_command=display_command,
        badge='server staged',
        ok=True,
        receipt=receipt,
    )


__all__ = [
    'AuthenticatedDownloadRedirect', 'maybe_redirect_authenticated_download',
]
