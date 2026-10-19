"""Pure effect-recovery policy over request-owned ToolContractV2 documents.

Entry: effect_recovery_policy. Missing/contradictory declarations reconcile;
provider names, rendered text and equal arguments never establish idempotency.
Dependencies: none. Policy does not grant execution or approval authority.
"""
from collections.abc import Mapping


def effect_recovery_policy(documents: Mapping, tool_name: str) -> str:
    document = documents.get(tool_name) if isinstance(documents, Mapping) else None
    if not isinstance(document, Mapping) or document.get('name') != tool_name:
        return 'reconcile'
    policy = document.get('effectRecovery')
    if (policy == 'read_only' and document.get('permission') == 'read'
            and document.get('idempotency') == 'read_only'):
        return 'read_only'
    if policy == 'idempotent' and document.get('idempotency') == 'idempotent':
        return 'idempotent'
    return 'reconcile'
