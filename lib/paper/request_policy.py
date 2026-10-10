"""Paper request-policy identity and cache-isolation decisions.

Responsibility: derive a stable, owner-independent execution fingerprint from
one request's model/config and identify long-agent experiment controls that
must never reuse or overwrite the product's canonical paper-result cache.
The module owns no task state and performs no storage access.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


_LONG_AGENT_POLICY_FIELDS = (
    ('responses', 'promptProfile'),
    ('tools', 'schemaBudgetTokens'),
    ('tools', 'resultEnvelope'),
    ('context', 'globalBudgetTokens'),
    ('compaction', 'strategy'),
    ('orchestration', 'policy'),
)
_MISSING = object()


def _explicit_value(config: Mapping[str, Any], owner: str, field: str) -> Any:
    nested = config.get(owner)
    if isinstance(nested, Mapping) and field in nested:
        return nested[field]
    dotted = f'{owner}.{field}'
    return config[dotted] if dotted in config else _MISSING


def paper_runtime_policy_projection(config: Any) -> dict[str, Any]:
    """Return only explicitly supplied long-agent experiment policy fields."""
    if not isinstance(config, Mapping):
        return {}
    projection: dict[str, Any] = {}
    for owner, field in _LONG_AGENT_POLICY_FIELDS:
        value = _explicit_value(config, owner, field)
        if value is not _MISSING:
            projection[f'{owner}.{field}'] = value
    return projection


def paper_runtime_policy_isolated(config: Any) -> bool:
    """Whether this request must bypass canonical result reads and writes."""
    return bool(paper_runtime_policy_projection(config))


def paper_execution_fingerprint(*, model: Any, config: Any) -> str:
    """Hash every output-affecting request value for exact in-flight dedup.

    Request bodies are JSON, but ``default=str`` keeps direct internal callers
    fail-safe instead of turning a diagnostic/non-JSON config into a 500.  Only
    the digest is retained or logged; raw config values are never copied into
    index keys.
    """
    payload = {
        'model': str(model or ''),
        'config': dict(config) if isinstance(config, Mapping) else {},
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def paper_request_policy_telemetry(*, model: Any, config: Any) -> dict[str, Any]:
    """Return the bounded task/benchmark projection for one request policy."""
    projection = paper_runtime_policy_projection(config)
    return {
        'contractVersion': 'tofu.paper-request-policy/v1',
        'executionFingerprint': paper_execution_fingerprint(
            model=model, config=config),
        'cacheMode': 'request_local' if projection else 'shared',
        'explicitPolicyFields': sorted(projection),
    }


__all__ = [
    'paper_execution_fingerprint',
    'paper_request_policy_telemetry',
    'paper_runtime_policy_isolated',
    'paper_runtime_policy_projection',
]
