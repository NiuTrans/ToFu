"""File provenance admission and live checks, never a path-based read grant.

Only host-issued FileWitness objects can introduce a source. Exact selected
quotes are sealed in the fact aggregate CAS; subsequent queries stream a digest
through the shared descriptor guard. No file body or independent store lives here.
"""
from dataclasses import dataclass

from lib.artifacts.file_snapshot import read_project_snapshot
from .repository import digest


@dataclass(frozen=True)
class FileWitness:
    scope: tuple
    identifier: str
    version: str
    path: str
    root: str
    root_identity: tuple[int, int]
    task_id: str
    call_id: str


def source_key(source):
    return digest(source)


def prune_file_seals(document):
    retained = {source_key(source) for fact in document['facts'].values()
                for source in fact['sources'] if source['kind'] == 'file'}
    seals = document.get('_fileSources', {})
    document['_fileSources'] = {key: value for key, value in seals.items() if key in retained}


class FileSources:
    def __init__(self, conversation_id, principal, document, check, witnesses=None):
        self.scope = (principal.require_owner(context='working file source'),
                      principal.tenant_id, conversation_id)
        self.document, self.check, self.witnesses = document, check, witnesses
        self.snapshots = {}

    def resolve(self, source, *, capture=False):
        key = source_key(source)
        seal = self.document.get('_fileSources', {}).get(key)
        if seal is None:
            witness = self.witnesses(source) if capture and self.witnesses else None
            if (not isinstance(witness, FileWitness) or witness.scope != self.scope
                    or (witness.identifier, witness.version, witness.path) !=
                       (source['id'], source['version'], source['path'])):
                return 'unavailable', 'source_unavailable', ''
            seal = {'root': witness.root, 'rootIdentity': list(witness.root_identity),
                    'path': witness.path, 'taskId': witness.task_id, 'callId': witness.call_id}
        try:
            # Cache only within this query. Capturing a new exact quote must
            # inspect it even if another quote from this file was already checked.
            cache_key = (seal['root'], seal['path'], tuple(seal['rootIdentity']))
            unsealed = key not in self.document.get('_fileSources', {})
            if unsealed or cache_key not in self.snapshots:
                snapshot = read_project_snapshot(seal['path'], [seal['root']],
                    check=self.check, quotes=[source['quote']] if unsealed else (),
                    expected_root_identity=seal['rootIdentity'])
                self.snapshots[cache_key] = snapshot
            else:
                snapshot = self.snapshots[cache_key]
        except TimeoutError:
            raise
        except (OSError, ValueError):
            return 'unavailable', 'source_unavailable', ''
        if snapshot.root_identity != tuple(seal['rootIdentity']):
            return 'unavailable', 'source_unavailable', ''
        if snapshot.sha256 != source['version']:
            return 'stale', 'source_version_changed', ''
        if unsealed:
            if source['quote'] not in snapshot.found_quotes:
                raise ValueError('File quote was not found in the exact source version')
            self.document.setdefault('_fileSources', {})[key] = seal
        return 'observed', '', source['quote']
