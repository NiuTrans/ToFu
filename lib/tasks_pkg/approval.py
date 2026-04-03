"""Write approval system — thread-safe user confirmation for file writes."""

import logging
import threading

logger = logging.getLogger(__name__)

_write_approvals = {}
_write_approvals_lock = threading.Lock()

def request_write_approval(approval_id, timeout=120):
    """Block until user approves/rejects. Returns True if approved."""
    logger.info('[Approval] Request %s waiting (timeout=%ds)', approval_id, timeout)
    evt = threading.Event()
    with _write_approvals_lock:
        _write_approvals[approval_id] = {'event': evt, 'approved': False}
    approved = False
    if evt.wait(timeout=timeout):
        with _write_approvals_lock:
            entry = _write_approvals.pop(approval_id, {})
            approved = entry.get('approved', False)
        logger.info('[Approval] Resolved %s → approved=%s', approval_id, approved)
    else:
        with _write_approvals_lock:
            _write_approvals.pop(approval_id, None)
        logger.warning('[Approval] Request %s timed out after %ds', approval_id, timeout)
    return approved

def resolve_write_approval(approval_id, approved):
    """Called by the API endpoint when user clicks Approve/Reject."""
    with _write_approvals_lock:
        entry = _write_approvals.get(approval_id)
        if not entry:
            logger.warning('[Approval] resolve called for unknown approval_id=%s', approval_id)
            return False
        entry['approved'] = approved
        entry['event'].set()
    logger.info('[Approval] User resolved %s → approved=%s', approval_id, approved)
    return True
