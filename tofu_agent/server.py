"""Database-free HTTP/SSE transport for :mod:`tofu_agent.runtime`.

The sidecar deliberately implements only the developer runtime boundary:
agent runs, in-memory task replay, abort, custom-tool handoff, health, and
capabilities, plus a small static model-routing setup control plane. It does not
initialize the full Tofu server, storage authority, billing, conversations,
or full Tofu application frontend.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
from importlib import resources
import json
import os
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

from quart import Quart, Response, jsonify, request

from tofu_agent.capabilities import runtime_capabilities
from tofu_agent.models import (
    AgentConfigurationError,
    AgentOverloadedError,
    AgentRequest,
    AgentTimeoutError,
    ModelRoutingConfig,
)
from tofu_agent.provider_setup import (
    ModelRoutingConfigurationLocked,
    ModelRoutingSetupService,
)
from tofu_agent.provider_store import (
    ModelRoutingSettingsStore,
    ModelRoutingStoreError,
)
from tofu_agent.runtime import AgentExecution, AgentRuntime


class _HeadlessQuart(Quart):
    """Minimal shell compatible with Quart 0.19 + Flask 3.1 installs."""

    default_config = {
        **Quart.default_config,
        'PROVIDE_AUTOMATIC_OPTIONS': True,
    }


def _is_loopback(value: str) -> bool:
    host = str(value or '').strip().split('%', 1)[0]
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _authority_hostname(value: str) -> str:
    """Extract a hostname from an HTTP Host authority, failing closed."""
    try:
        return str(urlparse(f'//{str(value or "").strip()}').hostname or '')
    except ValueError:
        return ''


@dataclass(frozen=True, slots=True)
class HeadlessServerConfig:
    """Security and resource policy for one headless server process."""

    bind_host: str = '127.0.0.1'
    token: str = field(default='', repr=False)
    auth_mode: str = 'auto'
    max_body_bytes: int = 2 * 1024 * 1024
    idempotency_ttl_s: float = 24 * 3600
    idempotency_max_entries: int = 4096
    setup_enabled: bool = True

    def __post_init__(self) -> None:
        mode = str(self.auth_mode or 'auto').strip().lower()
        if mode not in {'auto', 'token', 'open'}:
            raise AgentConfigurationError(
                'headless auth_mode must be auto, token, or open')
        if mode == 'token' and not str(self.token or '').strip():
            raise AgentConfigurationError(
                'headless token auth requires TOFU_AGENT_TOKEN')
        bind_host = str(self.bind_host or '').strip()
        if not bind_host:
            raise AgentConfigurationError(
                'headless bind_host must not be empty')
        object.__setattr__(self, 'auth_mode', mode)
        object.__setattr__(self, 'bind_host', bind_host)
        object.__setattr__(self, 'token', str(self.token or '').strip())
        object.__setattr__(self, 'max_body_bytes',
                           max(1024, int(self.max_body_bytes)))
        object.__setattr__(self, 'idempotency_ttl_s',
                           max(60.0, float(self.idempotency_ttl_s)))
        object.__setattr__(self, 'idempotency_max_entries',
                           max(16, int(self.idempotency_max_entries)))
        object.__setattr__(self, 'setup_enabled', bool(self.setup_enabled))

    @classmethod
    def from_env(cls, *, bind_host: str = '') -> 'HeadlessServerConfig':
        return cls(
            bind_host=(bind_host or os.environ.get(
                'TOFU_AGENT_HOST', '127.0.0.1')),
            token=os.environ.get('TOFU_AGENT_TOKEN', ''),
            auth_mode=os.environ.get('TOFU_AGENT_AUTH_MODE', 'auto'),
            max_body_bytes=int(os.environ.get(
                'TOFU_AGENT_MAX_BODY_BYTES', str(2 * 1024 * 1024))),
            idempotency_ttl_s=float(os.environ.get(
                'TOFU_AGENT_IDEMPOTENCY_TTL', str(24 * 3600))),
            idempotency_max_entries=int(os.environ.get(
                'TOFU_AGENT_IDEMPOTENCY_MAX', '4096')),
            setup_enabled=str(os.environ.get(
                'TOFU_AGENT_SETUP_ENABLED', '1')).strip().lower()
            not in {'0', 'false', 'no', 'off'},
        )

    @property
    def requires_token(self) -> bool:
        return self.auth_mode == 'token' or bool(self.token)

    def public_dict(self) -> dict[str, Any]:
        return {
            'bind_host': self.bind_host,
            'auth_mode': (
                'token' if self.requires_token else self.auth_mode),
            'token_configured': bool(self.token),
            'max_body_bytes': self.max_body_bytes,
            'idempotency_ttl_s': self.idempotency_ttl_s,
            'setup_enabled': self.setup_enabled,
        }


@dataclass(slots=True)
class _IdempotencyEntry:
    fingerprint: str
    created_at: float
    ready: threading.Event = field(default_factory=threading.Event)
    execution: AgentExecution | None = None


class _IdempotencyStore:
    """Bounded process-lifetime idempotency index with atomic reservations."""

    def __init__(self, *, ttl_s: float, max_entries: int) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._entries: dict[str, _IdempotencyEntry] = {}
        self._lock = threading.Lock()

    def _prune_locked(self, now: float) -> None:
        expired = [key for key, entry in self._entries.items()
                   if now - entry.created_at >= self.ttl_s]
        for key in expired:
            self._entries.pop(key, None)
        overflow = len(self._entries) - self.max_entries
        if overflow > 0:
            oldest = sorted(
                self._entries.items(), key=lambda pair: pair[1].created_at)
            for key, _entry in oldest[:overflow]:
                self._entries.pop(key, None)

    def claim(
        self, key: str, fingerprint: str,
    ) -> tuple[_IdempotencyEntry, bool, bool]:
        """Return ``(entry, creator, conflict)`` for one principal/key."""
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            existing = self._entries.get(key)
            if existing is not None:
                return existing, False, existing.fingerprint != fingerprint
            entry = _IdempotencyEntry(
                fingerprint=fingerprint, created_at=now)
            self._entries[key] = entry
            return entry, True, False

    def publish(self, key: str, entry: _IdempotencyEntry,
                execution: AgentExecution) -> None:
        with self._lock:
            if self._entries.get(key) is entry:
                entry.execution = execution
                entry.ready.set()

    def cancel(self, key: str, entry: _IdempotencyEntry) -> None:
        with self._lock:
            if self._entries.get(key) is entry:
                self._entries.pop(key, None)
            entry.ready.set()


def _json_error(kind: str, message: str, status: int, **extra):
    payload = {
        'ok': False,
        'error': {'kind': kind, 'message': str(message)},
    }
    payload.update(extra)
    return jsonify(payload), status


def _canonical_fingerprint(body: dict) -> str:
    encoded = json.dumps(
        body, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _bearer_token() -> str:
    authorization = str(request.headers.get('Authorization') or '')
    scheme, separator, credential = authorization.partition(' ')
    if separator and scheme.lower() == 'bearer':
        return credential.strip()
    return ''


def _setup_request_is_same_origin() -> bool:
    """Reject browser cross-site requests to the loopback setup authority."""
    fetch_site = str(request.headers.get('Sec-Fetch-Site') or '').lower()
    if fetch_site == 'cross-site':
        return False
    origin = str(request.headers.get('Origin') or '').strip()
    if not origin:
        return True
    parsed = urlparse(origin)
    return parsed.scheme in {'http', 'https'} and parsed.netloc == request.host


_SETUP_ASSETS = {
    'index.html': 'text/html; charset=utf-8',
    'setup.css': 'text/css; charset=utf-8',
    'setup.js': 'text/javascript; charset=utf-8',
}


@lru_cache(maxsize=len(_SETUP_ASSETS))
def _setup_asset(name: str) -> bytes:
    if name not in _SETUP_ASSETS:
        raise KeyError(name)
    return resources.files('tofu_agent').joinpath(
        'setup_ui', name).read_bytes()


def _query_cursor() -> int:
    raw = request.args.get('cursor')
    if raw not in (None, ''):
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0
    last_event_id = request.headers.get('Last-Event-ID')
    try:
        return max(0, int(last_event_id) + 1)
    except (TypeError, ValueError):
        return 0


def _request_boolean(body: dict, name: str) -> bool:
    """Read one JSON boolean without Python's truthy-string ambiguity."""
    value = body.get(name, False)
    if not isinstance(value, bool):
        raise AgentConfigurationError(f'{name} must be a boolean')
    return value


def _sse_response(generator, *, task_id: str) -> Response:
    return Response(
        generator,
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache, no-transform',
            'X-Accel-Buffering': 'no',
            'X-Tofu-Task-Id': task_id,
        },
    )


async def _native_task_stream(
    execution: AgentExecution,
    *,
    cursor: int,
):
    async for event in execution.events_async(cursor=cursor):
        sequence = event.get('seq')
        if sequence is not None:
            yield f'id: {int(sequence)}\n'
        yield f'data: {json.dumps(event, ensure_ascii=False)}\n\n'


async def _agent_run_stream(
    execution: AgentExecution,
    *,
    cursor: int,
):
    async for event in execution.events_async(cursor=cursor):
        event_type = str(event.get('type') or '')
        chunk: dict[str, Any] = {
            'id': execution.request_id,
            'object': 'agent.run.chunk',
            'created': int(time.time()),
            'model': execution.model,
            'task_id': execution.task_id,
            'event': event_type,
            'data': {key: value for key, value in event.items()
                     if key != 'type'},
        }
        if event_type == 'delta':
            delta = {'content': event.get('content') or ''}
            if event.get('thinking'):
                delta['reasoning_content'] = event['thinking']
            chunk['delta'] = delta
        sequence = event.get('seq')
        if sequence is not None:
            yield f'id: {int(sequence)}\n'
        yield f'data: {json.dumps(chunk, ensure_ascii=False)}\n\n'
        if event_type in {'done', 'error', 'aborted'}:
            yield 'data: [DONE]\n\n'
            return


def create_app(
    *,
    runtime: AgentRuntime | None = None,
    config: HeadlessServerConfig | None = None,
    model_routing_setup: ModelRoutingSetupService | None = None,
) -> Quart:
    """Create the isolated headless ASGI application."""
    server_config = config or HeadlessServerConfig.from_env()
    owns_runtime = runtime is None
    if runtime is None:
        store = ModelRoutingSettingsStore()
        environment_access = ModelRoutingConfig.from_env()
        source = 'environment' if environment_access is not None else 'none'
        load_error = ''
        saved_access = None
        if environment_access is None:
            try:
                saved_access = store.load()
            except ModelRoutingStoreError as exc:
                load_error = str(exc)
            if saved_access is not None:
                source = 'saved'
        agent_runtime = AgentRuntime.local(
            model_routing=(environment_access or saved_access),
            model_routing_source=source,
        )
        model_routing_setup = ModelRoutingSetupService(
            agent_runtime,
            store,
            source=source,
            editable=environment_access is None,
            load_error=load_error,
        )
    else:
        agent_runtime = runtime
    if model_routing_setup is None:
        source = str(getattr(
            agent_runtime, 'model_routing_source', 'runtime') or 'runtime')
        model_routing_setup = ModelRoutingSetupService(
            agent_runtime,
            ModelRoutingSettingsStore(),
            source=source,
            editable=source in {'none', 'saved'},
        )
    idempotency = _IdempotencyStore(
        ttl_s=server_config.idempotency_ttl_s,
        max_entries=server_config.idempotency_max_entries,
    )

    app = _HeadlessQuart('tofu-agent', static_folder=None)
    app.config['MAX_CONTENT_LENGTH'] = server_config.max_body_bytes
    app.extensions['tofu_agent_runtime'] = agent_runtime
    app.extensions['tofu_agent_server_config'] = server_config
    app.extensions['tofu_agent_model_routing_setup'] = model_routing_setup

    @app.before_request
    async def _authenticate():
        public_setup_asset = (
            request.path == '/setup'
            or request.path.startswith('/setup/assets/'))
        if request.path in {'/', '/health/live', '/health/ready'} \
                or public_setup_asset:
            return None
        if request.path.startswith('/api/v1/setup/') \
                and not _setup_request_is_same_origin():
            return _json_error(
                'cross_site_request',
                'Model-routing setup accepts same-origin browser requests only.',
                403,
            )
        if server_config.auth_mode == 'open':
            return None
        if server_config.requires_token:
            supplied = _bearer_token()
            if not supplied or not hmac.compare_digest(
                    supplied, server_config.token):
                return _json_error(
                    'unauthorized', 'A valid Bearer token is required.', 401)
            return None
        # Auto mode without a token is a localhost-only development policy.
        if (not _is_loopback(server_config.bind_host)
                or not _is_loopback(str(request.remote_addr or ''))
                or not _is_loopback(_authority_hostname(request.host))):
            return _json_error(
                'unauthorized',
                'Tokenless mode accepts loopback peers and Host authorities '
                'only. Set TOFU_AGENT_TOKEN before exposing this server.',
                401,
            )
        return None

    @app.after_request
    async def _security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('Referrer-Policy', 'no-referrer')
        response.headers.setdefault(
            'Permissions-Policy',
            'camera=(), microphone=(), geolocation=(), payment=()')
        if request.path == '/setup' \
                or request.path.startswith('/setup/assets/'):
            response.headers['Cache-Control'] = 'no-store'
            response.headers.setdefault(
                'Content-Security-Policy',
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        return response

    @app.get('/')
    async def _root():
        return jsonify({
            'service': 'tofu-agent',
            'api_version': 'v1',
            'health': '/health/ready',
            'capabilities': '/api/v1/capabilities',
            'setup': '/setup' if server_config.setup_enabled else None,
        })

    @app.get('/setup')
    async def _model_routing_setup_page():
        if not server_config.setup_enabled:
            return _json_error('not_found', 'Model-routing setup is disabled.', 404)
        return Response(
            _setup_asset('index.html'),
            content_type=_SETUP_ASSETS['index.html'],
        )

    @app.get('/setup/assets/<name>')
    async def _model_routing_setup_asset(name: str):
        if not server_config.setup_enabled or name not in _SETUP_ASSETS \
                or name == 'index.html':
            return _json_error('not_found', 'Setup asset not found.', 404)
        return Response(_setup_asset(name), content_type=_SETUP_ASSETS[name])

    @app.get('/health/live')
    async def _live():
        return jsonify({'status': 'ok', 'service': 'tofu-agent'})

    @app.get('/health/ready')
    async def _ready():
        ready = not agent_runtime.closed and bool(agent_runtime.default_model)
        return jsonify({
            'status': 'ready' if ready else 'not_ready',
            'ready': ready,
            'setup_required': not ready,
            'setup': '/setup' if server_config.setup_enabled else None,
            'in_flight': agent_runtime.in_flight,
            'capacity': agent_runtime.capacity,
        }), (200 if ready else 503)

    @app.get('/api/v1/capabilities')
    async def _capabilities():
        return jsonify({'ok': True, **runtime_capabilities(agent_runtime)})

    def _setup_disabled():
        if server_config.setup_enabled:
            return None
        return _json_error('not_found', 'Model-routing setup is disabled.', 404)

    async def _setup_json_body():
        body = await request.get_json(silent=True)
        if not isinstance(body, dict):
            raise AgentConfigurationError('JSON object body required')
        return body

    @app.get('/api/v1/setup/model-routing')
    async def _get_model_routing_setup():
        disabled = _setup_disabled()
        if disabled:
            return disabled
        return jsonify({'ok': True, **model_routing_setup.snapshot()})

    @app.post('/api/v1/setup/model-routing/test')
    async def _test_model_routing_connection():
        disabled = _setup_disabled()
        if disabled:
            return disabled
        try:
            body = await _setup_json_body()
            result = await asyncio.to_thread(
                model_routing_setup.test_connection, body)
            return jsonify(result)
        except (AgentConfigurationError, ValueError) as exc:
            return _json_error('invalid_request', str(exc), 400)

    @app.put('/api/v1/setup/model-routing')
    async def _save_model_routing_setup():
        disabled = _setup_disabled()
        if disabled:
            return disabled
        try:
            body = await _setup_json_body()
            result = await asyncio.to_thread(model_routing_setup.save, body)
            return jsonify({'ok': True, **result})
        except ModelRoutingConfigurationLocked as exc:
            return _json_error('configuration_locked', str(exc), 409)
        except (AgentConfigurationError, ValueError) as exc:
            return _json_error('invalid_request', str(exc), 400)
        except Exception:
            app.logger.exception('model-routing settings save failed')
            return _json_error(
                'internal_error', 'Model-routing settings could not be saved.', 500)

    @app.delete('/api/v1/setup/model-routing')
    async def _delete_model_routing_setup():
        disabled = _setup_disabled()
        if disabled:
            return disabled
        try:
            result = await asyncio.to_thread(model_routing_setup.delete)
            return jsonify({'ok': True, **result})
        except ModelRoutingConfigurationLocked as exc:
            return _json_error('configuration_locked', str(exc), 409)
        except AgentConfigurationError as exc:
            return _json_error('invalid_request', str(exc), 400)
        except Exception:
            app.logger.exception('model-routing settings delete failed')
            return _json_error(
                'internal_error', 'Model-routing settings could not be removed.', 500)

    async def _execution_from_body(body: dict) -> AgentExecution:
        if 'provider' in body:
            raise AgentConfigurationError(
                'inline provider blocks were removed; configure '
                'tofu.model-routing/v2 access')
        request_value = AgentRequest(
            messages=body.get('messages'),
            model=body.get('model'),
            routing=(body['routing'] if body.get('routing') is not None else {}),
            model_routing=body.get('model_routing'),
            config=(body['config']
                    if body.get('config') is not None else {}),
            capabilities=(body['capabilities']
                          if body.get('capabilities') is not None else {}),
            custom_tools=(body['tools']
                          if body.get('tools') is not None else []),
            custom_tools_mode=body.get('custom_tools_mode') or 'augment',
            trajectory=body.get('trajectory'),
            conversation_id=body.get('conversation_id') or '',
            request_id=body.get('id') or '',
            timeout_s=(body['timeout_s']
                       if body.get('timeout_s') is not None else 600.0),
        )
        return await asyncio.to_thread(agent_runtime.start, request_value)

    @app.post('/api/v1/agent/run')
    async def _run_agent():
        body = await request.get_json(silent=True)
        if not isinstance(body, dict):
            return _json_error(
                'invalid_request', 'JSON object body required.', 400)
        try:
            stream_response = _request_boolean(body, 'stream')
            async_response = _request_boolean(body, 'async')
        except AgentConfigurationError as exc:
            return _json_error('invalid_request', str(exc), 400)

        idempotency_key = str(
            request.headers.get('Idempotency-Key')
            or body.get('idempotency_key') or '').strip()
        entry = None
        store_key = ''
        creator = True
        if idempotency_key:
            store_key = (
                f'{agent_runtime.principal.subject_id}:{idempotency_key}')
            entry, creator, conflict = idempotency.claim(
                store_key, _canonical_fingerprint(body))
            if conflict:
                return _json_error(
                    'idempotency_conflict',
                    'Idempotency-Key was already used with a different body.',
                    409,
                )

        try:
            if entry is not None and not creator:
                ready = await asyncio.to_thread(entry.ready.wait, 30.0)
                if not ready or entry.execution is None:
                    return _json_error(
                        'idempotency_pending',
                        'The original request is still being admitted; retry.',
                        409,
                        retry_after=1,
                    )
                execution = entry.execution
            else:
                execution = await _execution_from_body(body)
                if entry is not None:
                    idempotency.publish(store_key, entry, execution)
        except AgentConfigurationError as exc:
            if entry is not None and creator:
                idempotency.cancel(store_key, entry)
            return _json_error('invalid_request', str(exc), 400)
        except AgentOverloadedError as exc:
            if entry is not None and creator:
                idempotency.cancel(store_key, entry)
            return _json_error('overloaded', str(exc), 503, retry_after=5)
        except Exception as exc:
            if entry is not None and creator:
                idempotency.cancel(store_key, entry)
            app.logger.exception('agent submission failed')
            return _json_error('internal_error', str(exc), 500)

        if stream_response:
            return _sse_response(
                _agent_run_stream(execution, cursor=_query_cursor()),
                task_id=execution.task_id,
            )
        if async_response or 'respond-async' in str(
                request.headers.get('Prefer') or '').lower():
            response = jsonify({
                'ok': True,
                'id': execution.request_id,
                'object': 'agent.run',
                'task_id': execution.task_id,
                'status': execution.status,
            })
            response.status_code = 202
            response.headers['Location'] = f'/api/v1/tasks/{execution.task_id}'
            response.headers['X-Tofu-Task-Id'] = execution.task_id
            return response
        try:
            result = await execution.result_async(execution.timeout_s)
        except AgentTimeoutError as exc:
            return _json_error(
                'timeout', str(exc), 504, task_id=execution.task_id)
        response = jsonify({'ok': True, **result.to_dict()})
        response.headers['X-Tofu-Task-Id'] = execution.task_id
        return response

    def _owned_execution(task_id: str):
        execution = agent_runtime.get(task_id)
        if execution is None:
            return None, _json_error(
                'not_found', 'Task not found.', 404)
        return execution, None

    @app.get('/api/v1/tasks/<task_id>')
    async def _get_task(task_id: str):
        execution, error = _owned_execution(task_id)
        if error:
            return error
        return jsonify({'ok': True, **execution.snapshot()})

    @app.get('/api/v1/tasks/<task_id>/events')
    async def _get_events(task_id: str):
        execution, error = _owned_execution(task_id)
        if error:
            return error
        return jsonify(execution.event_page(cursor=_query_cursor()))

    @app.get('/api/v1/tasks/<task_id>/stream')
    async def _stream_task(task_id: str):
        execution, error = _owned_execution(task_id)
        if error:
            return error
        return _sse_response(
            _native_task_stream(execution, cursor=_query_cursor()),
            task_id=execution.task_id,
        )

    @app.post('/api/v1/tasks/<task_id>/abort')
    async def _abort_task(task_id: str):
        execution, error = _owned_execution(task_id)
        if error:
            return error
        accepted = execution.abort()
        return jsonify({
            'ok': True,
            'task_id': task_id,
            'status': execution.status,
            'abort_requested': accepted,
        })

    @app.post('/api/v1/tasks/<task_id>/tools/<call_id>/result')
    async def _resolve_tool(task_id: str, call_id: str):
        execution, error = _owned_execution(task_id)
        if error:
            return error
        body = await request.get_json(silent=True)
        if not isinstance(body, dict) or 'content' not in body:
            return _json_error(
                'invalid_request', '`content` is required.', 400)
        from lib.tools.tool_env import resolve_client_tool_result
        resolved = resolve_client_tool_result(
            call_id,
            str(body.get('content') or ''),
            task_id=execution.task_id,
            user_id=agent_runtime.principal.require_owner(
                context='custom tool result'),
            is_error=bool(body.get('is_error')),
        )
        if not resolved:
            return _json_error(
                'not_found', 'Tool call is absent, expired, or already resolved.',
                404,
            )
        return jsonify({'ok': True, 'task_id': task_id, 'call_id': call_id})

    if owns_runtime:
        @app.after_serving
        async def _close_runtime():
            await asyncio.to_thread(agent_runtime.close)

    return app


__all__ = ['HeadlessServerConfig', 'create_app']
