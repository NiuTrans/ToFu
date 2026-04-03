"""Human Guidance system — thread-safe blocking wait for user input.

The LLM can call the ``ask_human`` tool to pose a question to the user.
The backend thread blocks **indefinitely** until the user responds or the
task is aborted, similar to the write-approval flow in ``approval.py``.

Two response modes are supported:
- **free_text**: user types a free-form answer
- **choice**: user picks from a list of options provided by the LLM
"""

import threading

from lib.log import get_logger

logger = get_logger(__name__)

_human_guidance_requests = {}
_human_guidance_lock = threading.Lock()

# How often (seconds) the blocking wait wakes up to check for task abort.
_ABORT_POLL_INTERVAL = 2.0


def request_human_guidance(guidance_id, task=None):
    """Block the current thread indefinitely until the user responds.

    The wait has **no timeout** — it blocks until one of:
    1. The user submits a response (via ``resolve_human_guidance``).
    2. The parent *task* is aborted (``task['aborted']`` becomes truthy).
    3. ``cancel_human_guidance`` is called externally.

    To avoid zombie threads on task abort, the wait polls every
    ``_ABORT_POLL_INTERVAL`` seconds and checks ``task['aborted']``.

    Args:
        guidance_id: Unique identifier for this guidance request.
        task: Optional task dict — if provided, abort is checked
              periodically so the thread doesn't block forever.

    Returns:
        The user's response string, or None if task was aborted.
    """
    logger.info('[HumanGuidance] Request %s blocking (no timeout, '
                'abort_poll=%.1fs, task=%s)', guidance_id,
                _ABORT_POLL_INTERVAL,
                task.get('id', '?')[:8] if task else 'none')
    evt = threading.Event()
    with _human_guidance_lock:
        _human_guidance_requests[guidance_id] = {
            'event': evt,
            'response': None,
        }

    # Poll loop: wait _ABORT_POLL_INTERVAL at a time, checking for abort
    resolved = False
    while not resolved:
        resolved = evt.wait(timeout=_ABORT_POLL_INTERVAL)
        if resolved:
            break
        # Check task abort
        if task and task.get('aborted'):
            logger.info('[HumanGuidance] Request %s — task aborted, '
                        'unblocking thread', guidance_id)
            with _human_guidance_lock:
                _human_guidance_requests.pop(guidance_id, None)
            return None

    # Event was set — user responded
    with _human_guidance_lock:
        entry = _human_guidance_requests.pop(guidance_id, {})
        response = entry.get('response')
    logger.info('[HumanGuidance] Resolved %s → response_len=%d, '
                'preview=%.100s', guidance_id,
                len(response) if response else 0, response or '')
    return response


def cancel_human_guidance(guidance_id):
    """Cancel a pending guidance request, unblocking the waiting thread.

    Called when the task is externally aborted or cleaned up.

    Args:
        guidance_id: The guidance request ID to cancel.

    Returns:
        True if the request was found and cancelled, False otherwise.
    """
    with _human_guidance_lock:
        entry = _human_guidance_requests.get(guidance_id)
        if not entry:
            logger.debug('[HumanGuidance] cancel called for unknown '
                         'guidance_id=%s (already resolved?)', guidance_id)
            return False
        entry['response'] = None
        entry['event'].set()
    logger.info('[HumanGuidance] Cancelled guidance_id=%s', guidance_id)
    return True


def resolve_human_guidance(guidance_id, response_text):
    """Called by the API endpoint when user submits their answer.

    Args:
        guidance_id: The guidance request ID to resolve.
        response_text: The user's response string.

    Returns:
        True if the request was found and resolved, False otherwise.
    """
    with _human_guidance_lock:
        entry = _human_guidance_requests.get(guidance_id)
        if not entry:
            logger.warning('[HumanGuidance] resolve called for unknown '
                           'guidance_id=%s (expired or already resolved)',
                           guidance_id)
            return False
        entry['response'] = response_text
        entry['event'].set()
    logger.info('[HumanGuidance] User resolved %s → response_len=%d, '
                'preview=%.100s', guidance_id,
                len(response_text) if response_text else 0,
                response_text or '')
    return True
