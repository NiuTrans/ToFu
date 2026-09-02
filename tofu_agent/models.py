"""Typed public values for the embeddable and headless Tofu runtime.

Provider credentials are deliberately represented by a redacting type. They
may be supplied in code, through the CLI, or once through environment
variables; no runtime result serializes the secret.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlsplit


_HTTP_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_FORBIDDEN_PROVIDER_HEADERS = frozenset({
    'authorization',
    'content-length',
    'cookie',
    'host',
    'proxy-authorization',
    'set-cookie',
    'transfer-encoding',
    'x-api-key',
})
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
class ProviderConfig:
    """The only model-provider configuration most embedders need.

    ``base_url`` must expose an OpenAI-compatible chat endpoint. An empty key
    is valid for local engines such as vLLM or Ollama. ``model`` is both the
    requested wire model and Tofu's routing identity.
    """

    base_url: str
    model: str
    api_key: str = field(default='', repr=False)
    extra_headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    thinking_format: str = ''
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        base_url = str(self.base_url or '').strip().rstrip('/')
        model = str(self.model or '').strip()
        if not base_url.startswith(('http://', 'https://')):
            raise AgentConfigurationError(
                'provider base_url must start with http:// or https://')
        if _contains_http_control(base_url):
            raise AgentConfigurationError(
                'provider base_url must not contain control characters')
        if len(base_url) > 2048:
            raise AgentConfigurationError(
                'provider base_url must be at most 2048 characters')
        parsed = urlsplit(base_url)
        if not parsed.hostname:
            raise AgentConfigurationError(
                'provider base_url must contain a hostname')
        if parsed.username is not None or parsed.password is not None:
            raise AgentConfigurationError(
                'provider base_url must not contain embedded credentials; '
                'use api_key or extra_headers')
        if parsed.query or parsed.fragment:
            raise AgentConfigurationError(
                'provider base_url must not contain a query or fragment')
        if not model:
            raise AgentConfigurationError('provider model is required')
        if _contains_http_control(model):
            raise AgentConfigurationError(
                'provider model must not contain control characters')
        if len(model) > 512:
            raise AgentConfigurationError(
                'provider model must be at most 512 characters')
        if not isinstance(self.extra_headers, Mapping):
            raise AgentConfigurationError(
                'provider extra_headers must be an object')
        headers: dict[str, str] = {}
        for key, value in dict(self.extra_headers or {}).items():
            name = str(key or '').strip()
            if not name:
                raise AgentConfigurationError(
                    'provider extra_headers contains an empty name')
            content = str(value)
            if len(name) > 256 or len(content) > 16384:
                raise AgentConfigurationError(
                    'provider extra header name or value is too long')
            if not _HTTP_HEADER_NAME.fullmatch(name):
                raise AgentConfigurationError(
                    'provider extra header names must be valid HTTP tokens')
            if name.lower() in _FORBIDDEN_PROVIDER_HEADERS:
                raise AgentConfigurationError(
                    f'provider extra header {name!r} is reserved')
            if _contains_http_control(content):
                raise AgentConfigurationError(
                    'provider extra header values must not contain control '
                    'characters or newlines')
            headers[name] = content
        if len(headers) > 64:
            raise AgentConfigurationError(
                'provider extra_headers accepts at most 64 entries')
        api_key = str(self.api_key or '').strip()
        if len(api_key) > 16384:
            raise AgentConfigurationError(
                'provider api_key must be at most 16384 characters')
        if _contains_http_control(api_key):
            raise AgentConfigurationError(
                'provider api_key must not contain control characters or '
                'newlines')
        object.__setattr__(self, 'base_url', base_url)
        object.__setattr__(self, 'model', model)
        object.__setattr__(self, 'api_key', api_key)
        object.__setattr__(self, 'extra_headers', headers)
        thinking_format = str(self.thinking_format or '').strip()
        if len(thinking_format) > 128:
            raise AgentConfigurationError(
                'provider thinking_format must be at most 128 characters')
        object.__setattr__(self, 'thinking_format', thinking_format)
        if isinstance(self.capabilities, (str, bytes, Mapping)) or not isinstance(
                self.capabilities, (list, tuple, set, frozenset)):
            raise AgentConfigurationError(
                'provider capabilities must be a list of names')
        capabilities = frozenset(
            str(value).strip() for value in self.capabilities
            if str(value).strip())
        if len(capabilities) > 64 or any(
                len(value) > 128 for value in capabilities):
            raise AgentConfigurationError(
                'provider capabilities contains too many or oversized names')
        object.__setattr__(self, 'capabilities', capabilities)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> 'ProviderConfig':
        """Accept the HTTP spelling plus the friendly ``endpoint`` alias."""
        if not isinstance(value, Mapping):
            raise AgentConfigurationError('provider must be an object')
        return cls(
            base_url=str(value.get('base_url') or value.get('endpoint') or ''),
            api_key=str(value.get('api_key') or ''),
            model=str(value.get('model') or value.get('model_id') or ''),
            extra_headers=(value['extra_headers']
                           if value.get('extra_headers') is not None else {}),
            thinking_format=str(value.get('thinking_format') or ''),
            capabilities=(value['capabilities']
                          if value.get('capabilities') is not None else ()),
        )

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        required: bool = False,
        default_model: str = '',
    ) -> 'ProviderConfig | None':
        """Load one default provider without importing application settings.

        New headless names win, then the concise aliases, then Tofu's existing
        ``LLM_*`` variables. If a key is supplied without a URL, the standard
        OpenAI endpoint is selected. A completely absent provider returns
        ``None`` unless ``required=True``.
        """
        source = os.environ if environ is None else environ
        base_url = _env_first(
            source,
            'TOFU_AGENT_PROVIDER_BASE_URL',
            'TOFU_PROVIDER_BASE_URL',
            'LLM_BASE_URL',
        )
        api_key = _env_first(
            source,
            'TOFU_AGENT_PROVIDER_API_KEY',
            'TOFU_PROVIDER_API_KEY',
            'LLM_API_KEY',
        )
        if not api_key:
            keys = _env_first(source, 'LLM_API_KEYS')
            api_key = next(
                (part.strip() for part in keys.split(',') if part.strip()), '')
        model = _env_first(
            source,
            'TOFU_AGENT_PROVIDER_MODEL',
            'TOFU_PROVIDER_MODEL',
            'TOFU_AGENT_MODEL',
            'LLM_MODEL',
        ) or str(default_model or '').strip()
        headers_raw = _env_first(
            source,
            'TOFU_AGENT_PROVIDER_EXTRA_HEADERS',
            'TOFU_PROVIDER_EXTRA_HEADERS',
        )
        thinking_format = _env_first(
            source,
            'TOFU_AGENT_PROVIDER_THINKING_FORMAT',
            'TOFU_PROVIDER_THINKING_FORMAT',
        )

        any_provider_value = bool(
            base_url or api_key or headers_raw or thinking_format)
        if not any_provider_value:
            if required:
                raise AgentConfigurationError(
                    'no provider configured; set base_url, api_key, and model')
            return None
        if not base_url and api_key:
            base_url = 'https://api.openai.com/v1'
        if not model:
            raise AgentConfigurationError(
                'provider endpoint/key is configured but its model is missing')
        headers: dict[str, str] = {}
        if headers_raw:
            try:
                decoded = json.loads(headers_raw)
            except json.JSONDecodeError as exc:
                raise AgentConfigurationError(
                    'provider extra headers must be a JSON object') from exc
            if not isinstance(decoded, dict):
                raise AgentConfigurationError(
                    'provider extra headers must be a JSON object')
            headers = {str(key): str(value)
                       for key, value in decoded.items()}
        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            extra_headers=headers,
            thinking_format=thinking_format,
        )

    def public_dict(self) -> dict[str, Any]:
        """Return diagnostics safe to log or expose over HTTP."""
        return {
            'base_url': self.base_url,
            'model': self.model,
            'has_api_key': bool(self.api_key),
            'extra_header_names': sorted(self.extra_headers),
            'thinking_format': self.thinking_format,
            'capabilities': sorted(self.capabilities),
        }


@dataclass(slots=True)
class AgentRequest:
    """One transport-neutral agent invocation."""

    messages: list[dict]
    model: str = ''
    provider: ProviderConfig | Mapping[str, Any] | None = None
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
        self.model = str(self.model or '').strip()
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
        if self.provider is not None and not isinstance(
                self.provider, (ProviderConfig, Mapping)):
            raise AgentConfigurationError('provider must be an object')
        if self.provider is not None and not isinstance(
                self.provider, ProviderConfig):
            provider_value = dict(self.provider)
            if self.model and not (
                    provider_value.get('model')
                    or provider_value.get('model_id')):
                provider_value['model'] = self.model
            self.provider = ProviderConfig.from_mapping(provider_value)


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
    'ProviderConfig',
]
