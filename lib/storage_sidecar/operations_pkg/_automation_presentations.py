"""Publish owned time references in the existing run progress transaction.

Entry: publish_presentations. Depends on the canonical attempt read and record
CAS owners; no new message projection, command receipt or event queue.
"""
from lib.automations.presentation_index import completed_presentations, presentation_prefix, validate_target
from lib.automations.contract import STORAGE_NAMESPACES
from lib.automations.errors import AutomationError
from lib.storage.errors import StorageError
from ._turns_read import _attempt_get
from ._records import _record_get, _record_put_owned


def publish_presentations(session, prefix, previous, run):
    try:
        for pointer in completed_presentations(previous,run):
            attempt = _attempt_get(session,{'user_id':run['ownerId'],'attempt_id':pointer['attemptId']})
            validate_target(run['trigger'],attempt)
            key = presentation_prefix(prefix,pointer['conversationId'],pointer['attemptId']) + pointer['id']
            payload = {'namespace':STORAGE_NAMESPACES['presentation'],'key':key}
            existing = _record_get(session,payload)
            if existing:
                if existing['value'] != pointer:
                    raise ValueError('Presentation identity collision')
                continue
            _record_put_owned(session,{**payload,'value':pointer,'expected_version':0})
    except (ValueError,KeyError,AutomationError) as exc:
        raise StorageError('database_protocol_error',str(exc)) from exc
