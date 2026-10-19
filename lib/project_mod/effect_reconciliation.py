"""Bridge one tracked local write occurrence to existing file-publication evidence.

Entry points: bind_prepared_write (before replace), inspect_prepared_write (read).
The undo index and write-intent journal remain the only file authorities. Queries
never recover, delete, replay, or overwrite a file. They prove publication, not
current content, tool completion, downstream effects, or task acceptance.
"""
import os

from lib.project_mod import modifications
from lib.project_mod.session_lock import locked_session, SessionLockBusy
from lib.project_mod.write_intents import IntentStore, classify


def reference_for(record):
    return {'kind': 'project_file_write', **{key: record[key] for key in (
        'changeId', 'basePath', 'path', 'taskId', 'rootIdentity', 'postDigest', 'postBytes')}}


def bind_prepared_write(record):
    from lib.project_mod.tool_admission import cancellation, journal_conv_id
    control = cancellation()
    context = control.runtime_context if control is not None else None
    if context is None or control.tool_name != 'write_file':
        return
    from lib.tasks_pkg.executor._effects import bind_tool_effect_reconciliation
    from lib.tasks_pkg.tool_runtime.context import ToolCancelled
    expected_conversation = journal_conv_id(context.task.get('id'))
    if expected_conversation != record['convId'] or context.task.get('id') != record['taskId']:
        raise ValueError('Prepared write attribution differs from its tool occurrence')
    bind_tool_effect_reconciliation(context, reference_for(record))
    if context.cancellation_requested:
        raise ToolCancelled('Cancelled before file publication')


def _matches(record, reference, conversation_id, *, committed=False):
    if record.get('convId') != conversation_id:
        return False
    if any(record.get(key) != reference[key] for key in ('changeId', 'basePath', 'path', 'taskId')):
        return False
    if committed:
        return record.get('type') == 'write_file' and record.get('postImage') == {
            'kind': 'content', 'bytes': reference['postBytes'],
            'digest': reference['postDigest'], 'root': reference['rootIdentity'],
        }
    return all(record.get(key) == reference[key] for key in ('rootIdentity', 'postDigest', 'postBytes'))


def inspect_prepared_write(reference, *, conversation_id):
    """Caller first resolves the exact occurrence through an owner-scoped Turn.

    A committed undo receipt proves historical publication even if a user later
    edits or undoes the file. Prepared-only evidence must still pass the existing
    root/inode/content classifier. Missing evidence is never proof of no effect.
    """
    session_dir = modifications._get_session_dir(reference['basePath'], create=False)
    if not session_dir or not os.path.isdir(session_dir):
        return 'unknown', 'missing_evidence'
    try:
        with locked_session(session_dir) as state:
            if state.get('writing') or state.get('recovering'):
                return 'unknown', 'writer_active'
            rows = [row for row in modifications._load_from_disk(session_dir)
                    if row.get('changeId') == reference['changeId']]
            if rows:
                if len(rows) == 1 and _matches(rows[0], reference, conversation_id, committed=True):
                    return 'file_published', 'undo_committed'
                return 'unknown', 'evidence_conflict'
            directory = os.path.join(session_dir, 'write-intents-v1')
            if not os.path.isdir(directory):
                return 'unknown', 'missing_evidence'
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                records = [record for record in IntentStore(directory, descriptor).read()
                           if record['changeId'] == reference['changeId']]
            finally:
                os.close(descriptor)
            if not records:
                return 'unknown', 'missing_evidence'
            record = records[0]
            if len(records) != 1 or not _matches(record, reference, conversation_id):
                return 'unknown', 'evidence_conflict'
            status = classify(record)
            if status == 'applied':
                return 'file_published', 'prepared_file_applied'
            if status == 'unpublished':
                return 'file_not_published', 'prepared_file_unpublished'
            return 'unknown', 'evidence_conflict'
    except SessionLockBusy:
        return 'unknown', 'writer_active'
    except (OSError, ValueError):
        return 'unknown', 'evidence_unavailable'
