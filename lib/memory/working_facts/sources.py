"""Fresh source bindings for one working-facts query; no persistent cache.

Artifact bodies are read only when admitting a quoted observation. Later
checks use immutable metadata or current review generations. These bindings
establish provenance, not the correctness of an arbitrary derived statement.
"""
import time

from lib.artifacts import ArtifactNotFoundError, get_artifact, get_artifact_meta
from lib.artifacts.review import read_criteria
from lib.storage import StorageError
from lib.turn_lifecycle import LifecycleNotFound
from .contract import limit


class SourceResolver:
    def __init__(self, conversation_id, principal, document, witnesses=None, tool_witnesses=None):
        self.conversation_id = conversation_id
        self.principal = principal
        self.owner = principal.require_owner(context='working fact source')
        self.deadline = time.monotonic() + limit('acquisitionSeconds')
        self.metadata = {}
        self.reviews = {}
        self.contents = {}
        from .file_sources import FileSources
        self.files = FileSources(conversation_id, principal, document, self.check_deadline, witnesses)
        from .tool_sources import ToolSources
        self.tools = ToolSources(conversation_id, principal, document, self.check_deadline, tool_witnesses)

    def check_deadline(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError('Working fact source acquisition expired')

    def resolve(self, source, *, capture=False):
        self.check_deadline()
        if source['kind'] == 'tool_result':
            return self.tools.resolve(source, capture=capture)
        if source['kind'] == 'file':
            return self.files.resolve(source, capture=capture)
        identifier = source['id']
        try:
            turn_criterion = source['kind'] == 'criterion' and source.get('targetKind') == 'turn'
            if not turn_criterion:
                if identifier not in self.metadata:
                    self.metadata[identifier] = get_artifact_meta(identifier, user_id=self.owner)
                meta = self.metadata[identifier]
                if meta['conv_id'] != self.conversation_id:
                    return 'unavailable', 'source_unavailable', ''
            if source['kind'] == 'criterion':
                key = (source.get('targetKind', 'artifact'), identifier)
                if key not in self.reviews:
                    if turn_criterion:
                        # The owned requirement-only port cannot recurse into
                        # decisions or source facts while resolving this graph.
                        from lib.acceptance.turns import read_requirements
                        self.reviews[key] = read_requirements(self.conversation_id, identifier,
                            principal=self.principal)
                    else:
                        self.reviews[key] = read_criteria(identifier, principal=self.principal)
                criterion = next((row for row in self.reviews[key]['criteria']
                                  if row['id'] == source['criterionId']), None)
                if criterion is None:
                    return 'stale', 'criterion_removed', ''
                if criterion['digest'] != source['version']:
                    return 'stale', 'source_version_changed', ''
                quoted = criterion['text']
                if capture and source['quote'] != quoted:
                    raise ValueError('A criterion source must quote its exact current text')
                return 'observed', '', quoted
            if meta['content_sha256'] != source['version']:
                return 'stale', 'source_version_changed', ''
            if capture:
                if identifier not in self.contents:
                    self.check_deadline()
                    artifact = get_artifact(identifier, user_id=self.owner)
                    self.contents[identifier] = artifact.get('content', '')
                if source['quote'] not in self.contents[identifier]:
                    raise ValueError('Artifact quote was not found in the exact source version')
            return 'observed', '', source['quote']
        except (ArtifactNotFoundError, LifecycleNotFound):
            return 'unavailable', 'source_unavailable', ''
        except StorageError as exc:
            if exc.code in {'database_forbidden', 'database_not_found'}:
                return 'unavailable', 'source_unavailable', ''
            raise
