"""Stdin Handler — thread-safe blocking wait for user input to subprocess stdin.

When a subprocess (run_command) appears to be waiting for interactive input
(stdin), the backend emits a ``stdin_request`` SSE event.  The backend thread
blocks until the user submits a response via the ``/api/chat/stdin_response``
endpoint, which calls ``resolve_stdin()``.

This mirrors the human_guidance pattern but is specifically for subprocess
stdin interaction.
"""

import threading

from lib.log import get_logger

logger = get_logger(__name__)

_stdin_requests = {}
_stdin_lock = threading.Lock()

# How often (seconds) the blocking wait wakes up to check for task abort.
_ABORT_POLL_INTERVAL = 1.0


def request_stdin(stdin_id, task=None):
    """Block the current thread until the user provides stdin input.

    Args:
        stdin_id: Unique identifier for this stdin request.
        task: Optional task dict — if provided, abort is checked periodically.

    Returns:
        The user's input string (with trailing newline), or None if aborted.
    """
    logger.info('[StdinHandler] Request %s blocking (abort_poll=%.1fs, task=%s)',
                stdin_id, _ABORT_POLL_INTERVAL,
                task.get('id', '?')[:8] if task else 'none')
    evt = threading.Event()
    with _stdin_lock:
        _stdin_requests[stdin_id] = {
            'event': evt,
            'response': None,
        }

    resolved = False
    while not resolved:
        resolved = evt.wait(timeout=_ABORT_POLL_INTERVAL)
        if resolved:
            break
        if task and task.get('aborted'):
            logger.info('[StdinHandler] Request %s — task aborted, unblocking', stdin_id)
            with _stdin_lock:
                _stdin_requests.pop(stdin_id, None)
            return None

    with _stdin_lock:
        entry = _stdin_requests.pop(stdin_id, {})
        response = entry.get('response')
    logger.info('[StdinHandler] Resolved %s → response_len=%d, preview=%.100s',
                stdin_id, len(response) if response else 0, response or '')
    return response


def resolve_stdin(stdin_id, input_text):
    """Called by the API endpoint when user submits stdin input.

    Args:
        stdin_id: The stdin request ID to resolve.
        input_text: The user's input string.

    Returns:
        True if the request was found and resolved, False otherwise.
    """
    with _stdin_lock:
        entry = _stdin_requests.get(stdin_id)
        if not entry:
            logger.warning('[StdinHandler] resolve called for unknown stdin_id=%s', stdin_id)
            return False
        entry['response'] = input_text
        entry['event'].set()
    logger.info('[StdinHandler] User resolved %s → input_len=%d', stdin_id, len(input_text))
    return True


def cancel_stdin(stdin_id):
    """Cancel a pending stdin request, unblocking the waiting thread."""
    with _stdin_lock:
        entry = _stdin_requests.get(stdin_id)
        if not entry:
            return False
        entry['response'] = None
        entry['event'].set()
    logger.info('[StdinHandler] Cancelled stdin_id=%s', stdin_id)
    return True
