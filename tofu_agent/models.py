"""Typed public values for the embeddable and headless Tofu runtime.

The standalone runtime consumes the same ``tofu.model-routing/v2`` aggregate
as the full Tofu application. Credential values live in a separate, redacting secret map and are
never serialized by a result or public diagnostic projection.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping
CUSTOM_TOOLS_MODES = frozenset({'augment', 'exclusive'})


def _contains_http_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127
               for character in value)


class AgentRuntimeError(RuntimeError):
    """Base class for public runtime failures."""


class AgentConfigurationError(AgentRuntimeError, ValueError):
    """The runtime or request is missing required configuration."""


class AgentOverloadedError(AgentRuntimeError):
    """The local runtime reached its configured in-flight limit."""


class AgentTimeoutError(AgentRuntimeError, TimeoutError):
    """A run did not become terminal within the caller's deadline."""


class AgentClosedError(AgentRuntimeError):
    """A new run was submitted after :meth:`AgentRuntime.close`."""


def _env_first(environ: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = str(environ.get(name) or '').strip()
        if value:
            return value
    return ''


@dataclass(frozen=True, slots=True)
class ModelRoutingConfig:
    """One complete v2 access aggregate plus independently supplied secrets."""

    document: Mapping[str, Any]
    model: Mapping[str, str]
    routing: Mapping[str, Any] = field(default_factory=dict)
    credential_secrets: Mapping[str, str] = field(
        default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        from lib.model_routing import (
            ModelRoutingError, normalize_document, parse_native_model_selection,
        )
        try:
            document = normalize_document(self.document)
            model = dict(self.model)
            routing = dict(self.routing or {})
            parse_native_model_selection({'model': model, 'routing': routing})
        except (ModelRoutingError, TypeError, ValueError) as exc:
            raise AgentConfigurationError(str(exc)) from exc
        if not isinstance(self.credential_secrets, Mapping):
            raise AgentConfigurationError(
                'credential_secrets must be an object keyed by secret_reference')
        secrets: dict[str, str] = {}
        for reference, raw_value in self.credential_secrets.items():
            key = str(reference or '').strip()
            value = str(raw_value or '').strip()
            if not key or len(key) > 256:
                raise AgentConfigurationError(
                    'credential_secrets contains an invalid secret_reference')
            if len(value.encode('utf-8')) > 8192 or _contains_http_control(value):
                raise AgentConfigurationError(
                    'credential secret is oversized or contains control characters')
            secrets[key] = value
        required = {
            str(row.get('secret_reference') or '')
            for row in document['credentials']
            if row.get('enabled') and row.get('kind') != 'local_identity'
        }
        missing = sorted(reference for reference in required if reference not in secrets)
        if missing:
            raise AgentConfigurationError(
                'credential_secrets is missing enabled references: '
                + ', '.join(missing))
        unknown = sorted(set(secrets) - {
            str(row.get('secret_reference') or '')
            for row in document['credentials']
        })
        if unknown:
            raise AgentConfigurationError(
                'credential_secrets contains unknown references: '
                + ', '.join(unknown))
        object.__setattr__(self, 'document', document)
        object.__setattr__(self, 'model', model)
        object.__setattr__(self, 'routing', routing)
        object.__setattr__(self, 'credential_secrets', secrets)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> 'ModelRoutingConfig':
        if not isinstance(value, Mapping):
            raise AgentConfigurationError('model_routing must be an object')
        document = value.get('model_routing', value.get('document'))
        if document is None and value.get('contract_version'):
            document = value
        if not isinstance(document, Mapping):
            raise AgentConfigurationError(
                'model_routing must contain a full tofu.model-routing/v2 document')
        model = value.get('model')
        if not isinstance(model, Mapping):
            raise AgentConfigurationError('model must be a structured object')
        return cls(
            document=document,
            model=model,
            routing=(value.get('routing') or {}),
            credential_secrets=(value.get('credential_secrets') or {}),
        )

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        required: bool = False,
    ) -> 'ModelRoutingConfig | None':
        """Load the exact v2 access envelope from one JSON environment value."""
        source = os.environ if environ is None else environ
        raw = _env_first(source, 'TOFU_AGENT_MODEL_ROUTING')
        if not raw:
            if required:
                raise AgentConfigurationError(
                    'TOFU_AGENT_MODEL_ROUTING is required')
            return None
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AgentConfigurationError(
                'TOFU_AGENT_MODEL_ROUTING must be valid JSON') from exc
        if not isinstance(decoded, Mapping):
            raise AgentConfigurationError(
                'TOFU_AGENT_MODEL_ROUTING must be a JSON object')
        return cls.from_mapping(decoded)

    @property
    def model_id(self) -> str:
        return str(self.model.get('model_id') or self.model.get('offering_id') or '')

    def public_dict(self) -> dict[str, Any]:
        from lib.model_routing import public_projection
        return {
            'model_routing': public_projection(self.document),
            'model': dict(self.model),
            'routing': dict(self.routing),
            'credential_secret_hints': {
                reference: ('configured' if value else 'empty')
                for reference, value in self.credential_secrets.items()
            },
        }


@dataclass(slots=True)
class AgentRequest:
    """One transport-neutral agent invocation."""

    messages: list[dict]
    model: Mapping[str, str] | None = None
    routing: dict = field(default_factory=dict)
    model_routing: ModelRoutingConfig | Mapping[str, Any] | None = None
    config: dict = field(default_factory=dict)
    capabilities: dict = field(default_factory=dict)
    custom_tools: list[dict] = field(default_factory=list)
    custom_tools_mode: str = 'augment'
    trajectory: str | None = None
    conversation_id: str = ''
    request_id: str = ''
    timeout_s: float = 600.0

    def __post_init__(self) -> None:
        if not isinstance(self.messages, list) or not self.messages:
            raise AgentConfigurationError('messages must be a non-empty list')
        for index, message in enumerate(self.messages):
            if not isinstance(message, dict):
                raise AgentConfigurationError(
                    f'messages[{index}] must be an object')
            if not str(message.get('role') or '').strip():
                raise AgentConfigurationError(
                    f'messages[{index}].role is required')
        if self.model is not None and not isinstance(self.model, Mapping):
            raise AgentConfigurationError('model must be a structured object')
        self.model = dict(self.model) if self.model is not None else None
        if not isinstance(self.routing, Mapping):
            raise AgentConfigurationError('routing must be an object')
        self.routing = dict(self.routing)
        if not isinstance(self.config, Mapping):
            raise AgentConfigurationError('config must be an object')
        if not isinstance(self.capabilities, Mapping):
            raise AgentConfigurationError('capabilities must be an object')
        if not isinstance(self.custom_tools, list):
            raise AgentConfigurationError('tools must be a list')
        if any(not isinstance(tool, Mapping) for tool in self.custom_tools):
            raise AgentConfigurationError('every tool must be an object')
        self.custom_tools_mode = str(
            self.custom_tools_mode or 'augment').strip().lower()
        if self.custom_tools_mode not in CUSTOM_TOOLS_MODES:
            raise AgentConfigurationError(
                'custom_tools_mode must be `augment` or `exclusive`')
        if self.custom_tools_mode == 'exclusive' and not self.custom_tools:
            raise AgentConfigurationError(
                'custom_tools_mode=`exclusive` requires at least one tool')
        self.config = dict(self.config)
        self.capabilities = dict(self.capabilities)
        self.custom_tools = list(self.custom_tools)
        self.trajectory = str(self.trajectory or '').strip() or None
        self.conversation_id = str(self.conversation_id or '').strip()
        self.request_id = str(self.request_id or '').strip()
        try:
            self.timeout_s = float(self.timeout_s)
        except (TypeError, ValueError) as exc:
            raise AgentConfigurationError('timeout_s must be numeric') from exc
        if self.timeout_s <= 0:
            raise AgentConfigurationError('timeout_s must be positive')
        if self.model_routing is not None and not isinstance(
                self.model_routing, (ModelRoutingConfig, Mapping)):
            raise AgentConfigurationError('model_routing must be an object')
        if self.model_routing is not None and not isinstance(
                self.model_routing, ModelRoutingConfig):
            envelope = dict(self.model_routing)
            if self.model is not None:
                envelope['model'] = self.model
            if self.routing:
                envelope['routing'] = self.routing
            self.model_routing = ModelRoutingConfig.from_mapping(envelope)


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Stable typed view of an ``agent.run`` terminal payload."""

    id: str
    task_id: str
    model: str
    status: str
    finish_reason: str
    content: str
    thinking: str
    usage: Mapping[str, Any]
    n_tool_rounds: int
    error: Mapping[str, Any] | None = None
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    provider_id: str = ''
    trajectory_format: str = ''
    trajectory: Any = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> 'AgentResult':
        return cls(
            id=str(payload.get('id') or ''),
            task_id=str(payload.get('task_id') or ''),
            model=str(payload.get('model') or ''),
            status=str(payload.get('status') or ''),
            finish_reason=str(payload.get('finish_reason') or 'stop'),
            content=str(payload.get('content') or ''),
            thinking=str(payload.get('thinking') or ''),
            usage=dict(payload.get('usage') or {}),
            n_tool_rounds=int(payload.get('n_tool_rounds') or 0),
            error=(dict(payload['error'])
                   if isinstance(payload.get('error'), Mapping) else None),
            tool_calls=tuple(payload.get('tool_calls') or ()),
            provider_id=str(payload.get('provider_id') or ''),
            trajectory_format=str(payload.get('trajectory_format') or ''),
            trajectory=payload.get('trajectory'),
            raw=dict(payload),
        )

    @property
    def ok(self) -> bool:
        return (self.status == 'done' and self.error is None
                and self.finish_reason != 'incomplete')

    def to_dict(self) -> dict[str, Any]:
        return dict(self.raw)


__all__ = [
    'AgentClosedError',
    'AgentConfigurationError',
    'AgentOverloadedError',
    'AgentRequest',
    'AgentResult',
    'AgentRuntimeError',
    'AgentTimeoutError',
    'CUSTOM_TOOLS_MODES',
    'ModelRoutingConfig',
]
