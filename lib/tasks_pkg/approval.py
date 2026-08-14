"""Write approval system — thread-safe user confirmation for file writes."""

import threading

from lib.log import get_logger

logger = get_logger(__name__)

_write_approvals = {}
_write_approvals_lock = threading.Lock()

def request_write_approval(approval_id, timeout=120):
    """Block until user approves/rejects. Returns True if approved."""
    logger.info('[Approval] Request %s waiting (timeout=%ds)', approval_id, timeout)
    evt = threading.Event()
    with _write_approvals_lock:
        _write_approvals[approval_id] = {
            'event': evt,
            'approved': False,
            'resolved': False,
        }
    signalled = evt.wait(timeout=timeout)
    with _write_approvals_lock:
        entry = _write_approvals.pop(approval_id, {})
    # The timeout and resolver can cross between Event.wait() returning and
    # this lock acquisition. The lock-owned `resolved` fence decides which
    # side won; an accepted answer must never be reported yet discarded.
    resolved = bool(entry.get('resolved'))
    approved = bool(entry.get('approved', False)) if resolved else False
    if signalled or resolved:
        logger.info('[Approval] Resolved %s → approved=%s', approval_id, approved)
    else:
        logger.warning('[Approval] Request %s timed out after %ds', approval_id, timeout)
    return approved

def resolve_write_approval(approval_id, approved):
    """Called by the API endpoint when user clicks Approve/Reject."""
    with _write_approvals_lock:
        entry = _write_approvals.get(approval_id)
        if not entry or entry.get('resolved'):
            logger.warning('[Approval] resolve called for unknown or already '
                           'resolved approval_id=%s', approval_id)
            return False
        entry['resolved'] = True
        entry['approved'] = approved
        entry['event'].set()
    logger.info('[Approval] User resolved %s → approved=%s', approval_id, approved)
    return True
