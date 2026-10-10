"""Read verified original scientific packets for receipts too large for previews.

original_science_loader binds journal lookup to explicit owner/direction/language
and the run's frozen scientific dependencies. One original is parsed at a time;
no complete history is loaded. Missing legacy bindings stay unverified, while
conflicting manifests, ambiguous calls and corrupt originals fail closed.
Dependencies: ActionObservations, program and the shared producer-envelope parser.
This reader never executes a tool, model, or stored command.
"""
from collections.abc import Mapping

from lib.storage.errors import StorageError
from .action_observations import ActionObservations
from .program import dependency_digests


def original_science_loader(client, *, user_id, paper_hash, lang, run):
    dependencies = {key: run[key] for key in ('hypothesis_digest', 'protocol_digest')}

    def load(receipt):
        task_id = receipt.get('source_task_id'); call_id = receipt.get('call_id')
        if not task_id or not call_id:
            return None
        repository = ActionObservations.open(client, user_id=user_id, task_id=task_id)
        if repository is None:
            return None
        binding = repository.binding
        basis = repository.load_basis()
        if binding['direction_hash'] != paper_hash or binding['lang'] != lang:
            raise StorageError('database_integrity', 'Research original scientific basis does not match run')
        if basis is None:
            return None  # legacy missing snapshots are never filled from current state
        if dependency_digests(basis) != dependencies:
            raise StorageError('database_integrity', 'Research original scientific basis does not match run')
        match = None; cursor = ''
        while True:
            page = repository.page(limit=16, cursor=cursor)
            for manifest in page['observations']:
                if manifest['call_id'] != call_id:
                    continue
                if (match is not None or manifest['tool'] != receipt.get('tool')
                        or manifest['result_digest'] != receipt.get('result_digest')
                        or manifest['receipt'].get('status') != receipt.get('status')):
                    raise StorageError('database_integrity', 'Research original scientific source is ambiguous or changed')
                match = manifest
            cursor = page['next_cursor']
            if not cursor:
                break
        if match is None:
            raise StorageError('database_integrity', 'Research original scientific source manifest is missing')
        exported = repository.prepare_original_export(match['ordinal'])
        if exported is None:
            raise StorageError('database_integrity', 'Research original scientific source disappeared')
        manifest, spool, _ = exported
        try:
            if manifest != match:
                raise StorageError('database_integrity', 'Research original scientific manifest changed')
            # Reuse the original capture parser, including command exit envelopes.
            from .action import _observed_tool_payload
            payload, command_state = _observed_tool_payload(
                {'toolName': manifest['tool'], 'status': manifest['receipt']['status']},
                spool.read().decode('utf-8'))
            if command_state in {'error', 'pending'} or not isinstance(payload, Mapping):
                return None
            keys = ('scientific_observations', 'scientific_audit', 'scientific_outcome')
            return {key: payload[key] for key in keys if key in payload} if any(key in payload for key in keys) else None
        finally:
            spool.close()
    return load
