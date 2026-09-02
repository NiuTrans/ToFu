"""No-code Provider setup service for the standalone Agent runtime.

Responsibility
--------------
Own the setup control-plane contract: templates, redacted snapshots, model
discovery, a real minimal completion probe, encrypted save/delete, and hot
application to new Agent runs.  HTTP routing and HTML rendering stay in
``tofu_agent.server`` and ``tofu_agent/setup_ui`` respectively.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlparse

from tofu_agent.models import AgentConfigurationError, ProviderConfig
from tofu_agent.provider_store import ProviderSettingsStore, secret_hint


PROVIDER_TEMPLATES: tuple[dict[str, str], ...] = (
    {
        'id': 'openai',
        'name': 'OpenAI',
        'base_url': 'https://api.openai.com/v1',
        'description': 'Official OpenAI-compatible API',
        'description_zh': 'OpenAI 官方兼容接口',
        'accent': 'lime',
    },
    {
        'id': 'openrouter',
        'name': 'OpenRouter',
        'base_url': 'https://openrouter.ai/api/v1',
        'description': 'One endpoint for many model providers',
        'description_zh': '一个端点接入多家模型',
        'accent': 'violet',
    },
    {
        'id': 'deepseek',
        'name': 'DeepSeek',
        'base_url': 'https://api.deepseek.com/v1',
        'description': 'DeepSeek OpenAI-compatible endpoint',
        'description_zh': 'DeepSeek 官方兼容接口',
        'accent': 'blue',
    },
    {
        'id': 'local',
        'name': 'Local model',
        'base_url': 'http://127.0.0.1:8000/v1',
        'description': 'vLLM, SGLang, Ollama, or another local engine',
        'description_zh': 'vLLM、SGLang、Ollama 等本地引擎',
        'accent': 'orange',
    },
    {
        'id': 'custom',
        'name': 'Custom',
        'base_url': '',
        'description': 'Any OpenAI-compatible endpoint',
        'description_zh': '任意 OpenAI-compatible 端点',
        'accent': 'ink',
    },
)


class ProviderConfigurationLocked(AgentConfigurationError):
    """The current Provider belongs to env/CLI configuration authority."""


class ProviderDiscoveryError(AgentConfigurationError):
    """The upstream model catalogue could not be read safely."""

    def __init__(self, verdict: str, message: str) -> None:
        super().__init__(message)
        self.verdict = verdict


@dataclass(frozen=True, slots=True)
class ProviderDraft:
    base_url: str
    api_key: str
    model: str
    extra_headers: Mapping[str, str]
    thinking_format: str

    def provider(self) -> ProviderConfig:
        return ProviderConfig(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            extra_headers=self.extra_headers,
            thinking_format=self.thinking_format,
        )


def _normalise_base_url(value: str) -> str:
    from lib.llm_dispatch.discovery import normalize_base_url
    return str(normalize_base_url(str(value or '').strip()) or '').rstrip('/')


def _safe_probe_detail(verdict: str, detail: str) -> str:
    match = re.search(r'HTTP\s+(\d{3})', str(detail or ''), re.IGNORECASE)
    status = f' (HTTP {match.group(1)})' if match else ''
    messages = {
        'ok': 'The provider returned a valid generated response.',
        'unauthorized': 'The provider rejected the API key or permissions.',
        'rate_limited': 'The provider is reachable but currently rate limited.',
        'not_found': 'The endpoint or selected model was not found.',
        'bad_request': 'The provider rejected the test request.',
        'unavailable': 'The provider is unavailable or could not be reached.',
        'invalid_response': 'The provider response was not OpenAI-compatible.',
        'error': 'The provider test failed.',
    }
    return messages.get(verdict, messages['error']) + status


class ProviderSetupService:
    """Coordinate one managed Provider and its no-code configuration UI."""

    def __init__(
        self,
        runtime,
        store: ProviderSettingsStore,
        *,
        source: str = 'none',
        editable: bool = True,
        load_error: str = '',
    ) -> None:
        self.runtime = runtime
        self.store = store
        self.source = str(source or 'none')
        self.editable = bool(editable)
        self.load_error = str(load_error or '')
        self._mutation_lock = threading.RLock()

    @property
    def provider(self) -> ProviderConfig | None:
        return getattr(self.runtime, 'provider', None)

    def snapshot(self) -> dict[str, Any]:
        provider = self.provider
        return {
            'configured': provider is not None,
            'ready': bool(provider and getattr(
                self.runtime, 'default_model', '')),
            'editable': self.editable,
            'source': self.source,
            'load_error': self.load_error,
            'provider': ({
                **provider.public_dict(),
                'api_key_hint': secret_hint(provider.api_key),
            } if provider is not None else None),
            'storage': {
                'kind': 'encrypted-file',
                'secret_values_returned': False,
            },
            'templates': [dict(template) for template in PROVIDER_TEMPLATES],
        }

    def _require_editable(self) -> None:
        if not self.editable:
            raise ProviderConfigurationLocked(
                'the active Provider is owned by environment variables or '
                'command-line arguments; remove that override and restart '
                'to manage it from /setup')

    def _draft(
        self,
        payload: Mapping[str, Any],
        *,
        require_model: bool,
    ) -> ProviderDraft:
        if not isinstance(payload, Mapping):
            raise AgentConfigurationError('provider payload must be an object')
        existing = self.provider
        supplied_base = payload.get('base_url', payload.get('endpoint'))
        base_url = _normalise_base_url(
            supplied_base if supplied_base is not None
            else (existing.base_url if existing else ''))
        if not base_url:
            raise AgentConfigurationError('provider endpoint is required')

        same_endpoint = bool(
            existing and existing.base_url.rstrip('/') == base_url)
        if 'api_key' in payload:
            if payload['api_key'] is not None \
                    and not isinstance(payload['api_key'], str):
                raise AgentConfigurationError('provider api_key must be a string')
            api_key = str(payload['api_key'] or '').strip()
        elif same_endpoint:
            api_key = existing.api_key
        elif existing is not None:
            raise AgentConfigurationError(
                're-enter the API key when changing the Provider endpoint')
        else:
            api_key = ''

        extra_headers_value = payload.get('extra_headers')
        if extra_headers_value is None and same_endpoint and existing:
            extra_headers: Mapping[str, str] = existing.extra_headers
        elif extra_headers_value is None:
            extra_headers = {}
        elif not isinstance(extra_headers_value, Mapping):
            raise AgentConfigurationError(
                'provider extra_headers must be an object')
        else:
            extra_headers = {
                str(key): str(value)
                for key, value in extra_headers_value.items()
            }

        model = str(
            payload.get('model') or payload.get('model_id')
            or (existing.model if same_endpoint and existing else '')
        ).strip()
        if require_model and not model:
            raise AgentConfigurationError('provider model is required')
        thinking_format = str(
            payload.get('thinking_format')
            if payload.get('thinking_format') is not None
            else (existing.thinking_format
                  if same_endpoint and existing else '')
        ).strip()
        # Reuse the public Provider value object for URL, secret-size, and
        # header-injection validation even when discovery has no model yet.
        validated = ProviderConfig(
            base_url=base_url,
            api_key=api_key,
            model=model or '__tofu_setup_discovery__',
            extra_headers=extra_headers,
            thinking_format=thinking_format,
        )
        return ProviderDraft(
            base_url=validated.base_url,
            api_key=validated.api_key,
            model=model,
            extra_headers=validated.extra_headers,
            thinking_format=validated.thinking_format,
        )

    @staticmethod
    def _validate_target(url: str) -> None:
        from lib.byo_egress import validate_egress_url
        validate_egress_url(url)

    @staticmethod
    def _models_url(base_url: str) -> str:
        return base_url.rstrip('/') + '/models'

    @staticmethod
    def _request_models(url: str, draft: ProviderDraft, timeout_s: float):
        from lib.http_client import http_get
        headers = {
            'Accept': 'application/json',
            'User-Agent': 'tofu-agent/provider-setup',
            **dict(draft.extra_headers),
        }
        if draft.api_key:
            headers['Authorization'] = f'Bearer {draft.api_key}'
        return http_get(url, headers=headers, timeout=timeout_s)

    @staticmethod
    def _catalogue_error(status: int) -> ProviderDiscoveryError:
        if status in {401, 403}:
            return ProviderDiscoveryError(
                'unauthorized',
                'the Provider rejected the API key or model-list permission')
        if status == 404:
            return ProviderDiscoveryError(
                'not_found', 'the Provider does not expose a /models endpoint')
        if status in {402, 429}:
            return ProviderDiscoveryError(
                'rate_limited', 'the Provider model catalogue is rate limited')
        if status >= 500:
            return ProviderDiscoveryError(
                'unavailable', f'the Provider returned HTTP {status}')
        return ProviderDiscoveryError(
            'http_error', f'the Provider model catalogue returned HTTP {status}')

    @staticmethod
    def _parse_models(response) -> list[dict[str, str]]:
        content = bytes(getattr(response, 'content', b'') or b'')
        if len(content) > 2 * 1024 * 1024:
            raise ProviderDiscoveryError(
                'invalid_response', 'the Provider model catalogue is too large')
        try:
            document = response.json()
        except (ValueError, TypeError) as exc:
            raise ProviderDiscoveryError(
                'invalid_response',
                'the Provider model catalogue is not valid JSON') from exc
        rows = document.get('data') if isinstance(document, dict) else None
        if not isinstance(rows, list):
            raise ProviderDiscoveryError(
                'invalid_response',
                'the Provider model catalogue is not OpenAI-compatible')
        models: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in rows[:1000]:
            if not isinstance(row, Mapping):
                continue
            model_id = str(row.get('id') or '').strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            models.append({
                'id': model_id,
                'owned_by': str(row.get('owned_by') or '').strip(),
            })
        if not models:
            raise ProviderDiscoveryError(
                'empty_catalogue', 'the Provider returned no usable model ids')
        return sorted(models, key=lambda value: value['id'].lower())

    def discover(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_s: float = 15.0,
    ) -> dict[str, Any]:
        """Discover model ids without persisting or returning credentials."""
        draft = self._draft(payload, require_model=False)
        models_url = self._models_url(draft.base_url)
        self._validate_target(models_url)
        try:
            response = self._request_models(models_url, draft, timeout_s)
        except Exception as exc:
            raise ProviderDiscoveryError(
                'unavailable', 'the Provider could not be reached') from exc
        status = int(getattr(response, 'status_code', 0) or 0)
        effective_base = draft.base_url
        if status == 404 and urlparse(draft.base_url).path in {'', '/'}:
            effective_base = draft.base_url.rstrip('/') + '/v1'
            models_url = self._models_url(effective_base)
            self._validate_target(models_url)
            try:
                response = self._request_models(models_url, draft, timeout_s)
            except Exception as exc:
                raise ProviderDiscoveryError(
                    'unavailable', 'the Provider could not be reached') from exc
            status = int(getattr(response, 'status_code', 0) or 0)
        if not 200 <= status < 300:
            raise self._catalogue_error(status)
        models = self._parse_models(response)
        return {
            'base_url': effective_base,
            'models': models,
            'count': len(models),
        }

    def test_connection(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_s: float = 20.0,
    ) -> dict[str, Any]:
        """Send one tiny real completion and return a secret-free verdict."""
        draft = self._draft(payload, require_model=True)
        self._validate_target(
            draft.base_url.rstrip('/') + '/chat/completions')
        from lib.provider_probe import probe_one_cell
        started = time.monotonic()
        verdict, detail = probe_one_cell(
            draft.base_url,
            draft.api_key,
            draft.model,
            dict(draft.extra_headers),
            timeout_s,
            protocol='openai',
        )
        return {
            'ok': verdict == 'ok',
            'verdict': verdict,
            'detail': _safe_probe_detail(verdict, detail),
            'latency_ms': max(0, round((time.monotonic() - started) * 1000)),
        }

    def save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Persist and hot-apply a Provider for all subsequently started runs."""
        with self._mutation_lock:
            self._require_editable()
            draft = self._draft(payload, require_model=True)
            self._validate_target(draft.base_url)
            provider = draft.provider()
            self.store.save(provider)
            self.runtime.configure_provider(provider, source='saved')
            self.source = 'saved'
            self.load_error = ''
            return self.snapshot()

    def delete(self) -> dict[str, Any]:
        """Remove the saved default; already-running tasks remain isolated."""
        with self._mutation_lock:
            self._require_editable()
            self.store.delete()
            self.runtime.configure_provider(None, source='none')
            self.source = 'none'
            self.load_error = ''
            return self.snapshot()


__all__ = [
    'PROVIDER_TEMPLATES',
    'ProviderConfigurationLocked',
    'ProviderDiscoveryError',
    'ProviderSetupService',
]
