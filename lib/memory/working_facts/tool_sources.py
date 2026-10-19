"""Selected historical tool observations sealed with the fact aggregate CAS.

Only the common execution adapter can supply VerifiedToolQuote. JSON metadata,
advisory artifact origins and arbitrary historical tool messages grant nothing.
The seal retains the selected quote's provenance, not a second result archive.
"""
from dataclasses import dataclass

from .repository import digest


@dataclass(frozen=True)
class VerifiedToolQuote:
    scope: tuple
    source_key: str
    task_id: str
    result_id: str


def prune_tool_seals(document):
    if '_toolSources' not in document:
        return
    retained = {digest(source) for fact in document['facts'].values()
                for source in fact['sources'] if source['kind'] == 'tool_result'}
    document['_toolSources'] = {key: value for key, value in document['_toolSources'].items() if key in retained}


class ToolSources:
    def __init__(self, conversation_id, principal, document, check, verify=None):
        self.scope = (principal.require_owner(context='working tool source'), principal.tenant_id, conversation_id)
        self.document, self.check, self.verify = document, check, verify

    def resolve(self, source, *, capture=False):
        key = digest(source)
        if key not in self.document.get('_toolSources', {}):
            witness = self.verify(source, self.check) if capture and self.verify else None
            if not isinstance(witness, VerifiedToolQuote) or witness.scope != self.scope or witness.source_key != key:
                return 'unavailable', 'source_unavailable', ''
            self.document.setdefault('_toolSources', {})[key] = {
                'taskId': witness.task_id, 'resultId': witness.result_id}
        # A returned result is immutable historical evidence. It cannot certify
        # the latest filesystem/network state, even when retention was complete.
        return 'observed', 'historical_tool_observation', source['quote']
