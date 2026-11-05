"""Reconcile prepared project writes with the committed undo journal.

Entry points: recover_pending_writes and recover_locked. Recovery never restores
user files. It records proven published writes, reclaims proven private aborted
stages, and preserves conflicting evidence with a visible failure. Every caller
uses the same per-session lock as journal writers before touching journal cache;
without intent evidence (has_pending_intents) there is nothing to lock for.
"""
import base64
import os

from lib.project_mod.session_lock import locked_session
from lib.project_mod.snapshot_content import anchored_parent
from lib.project_mod.write_intents import IntentError, classify, locked_intents


def post_image(record):
    return {'kind': 'content', 'bytes': record['postBytes'],
            'digest': record['postDigest'], 'root': record['rootIdentity']}


def recover_locked(base_path, store):
    from lib.project_mod import modifications as mods
    expected_base = os.path.abspath(base_path)
    pending = store.read()
    if not pending:
        return 0
    session_dir = os.path.dirname(store.directory)
    rows = mods._load_from_disk(session_dir)
    # Intent evidence marks a crashed or failed writer, whose packed payloads
    # may now be unreferenced.
    mods._sweep_crash_artifacts(session_dir, rows, crash_evidence=True)
    committed = {row.get('changeId') for row in rows}
    recovered = 0
    for record in pending:
        if record['basePath'] != expected_base:
            raise IntentError('Write recovery root attribution mismatch')
        if record['state'] == 'aborted':
            store.discard_aborted(record)
            continue
        already_committed = record['changeId'] in committed
        status = 'applied' if already_committed else classify(record)
        if status == 'unpublished':
            store.discard_aborted(store.abort(record))
            continue
        if status != 'applied':
            raise IntentError('write-recovery-conflict: file or project changed; '
                              'original bytes preserved for explicit recovery')
        if not already_committed:
            # A previous replacement's parent flush may have failed. Confirm
            # the published name before confirming its journal transition.
            with anchored_parent(record['rootPath'], record['path'],
                                 tuple(record['rootIdentity'])) as (parent, _):
                os.fsync(parent)
        original = (None if record['original'] is None else
                    base64.b64decode(record['original'], validate=True))
        if mods._record_modification(record['basePath'], 'write_file', record['path'],
                original_content=original, conv_id=record['convId'], task_id=record['taskId'],
                change_id=record['changeId'], post_image=post_image(record)) is not True:
            raise IntentError('Prepared write journal recovery was not confirmed')
        store.acknowledge(record)
        recovered += 1
    return recovered


def has_pending_intents(session_dir):
    """Whether prepared-write evidence exists, listed without admission.

    Evidence that a crashed writer left behind is stable, so it is always seen.
    An intent created while the listing runs belongs to a writer that is still
    live and acknowledges it itself; missing it never skips a needed recovery.
    """
    try:
        with os.scandir(os.path.join(session_dir, 'write-intents-v1')) as entries:
            return any(entry.name.endswith('.json') for entry in entries)
    except (FileNotFoundError, NotADirectoryError):
        return False


def recover_pending_writes(base_path):
    from lib.project_mod import modifications as mods
    session_dir = mods._get_session_dir(base_path, create=False)
    if not session_dir or not has_pending_intents(session_dir):
        return 0
    with locked_session(session_dir) as state:
        # Journal listeners may request a projection within an active writer or
        # recovery. They must not consume that operation's unacknowledged intent.
        if state.get('writing') or state.get('recovering'):
            return 0
        state['recovering'] = True
        try:
            with locked_intents(session_dir) as store:
                return recover_locked(base_path, store)
        finally:
            state.pop('recovering', None)
