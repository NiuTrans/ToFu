"""lib/tools/tool_env.py — Per-request custom tool environment.

A :class:`ToolEnvironment` carries the tool *schemas* AND *handlers* that a
single headless ``/api/v1/agent/run`` request brings with it, scoped to exactly
one task. It is attached to the task as ``task['_tool_env']`` and disposed when
the task reaches a terminal state — so nothing a request defines ever persists
into the process-global ``tool_registry`` or leaks into another user's task.

See ``docs/CUSTOM_TOOLS.md`` for the full design. The short version:

* **Inverted lookup** — handler resolution becomes per-request. The executor
  consults ``task['_tool_env'].resolve(name)`` *before* the global registry.
* **Lifecycle** — mirrors ``lib/llm_dispatch/ephemeral.py``: mint per request,
  bounded handle table, idempotent disposal, dispose-on-terminal.
* **Three backends** selected per-tool by ``execution.mode``:
    - ``client``  — zero-trust handoff; server emits an event and blocks for
      the client to POST the result back. Never runs user code.
    - ``webhook`` — server POSTs args to a URL (SSRF-guarded at mint + call).
    - ``sandbox`` — server runs a command (RCE; operator opt-in only).

Isolation contract (enforced by ``tests/test_custom_tool_isolation.py``):
custom tool names MUST match ``custom__<ident>`` and must not collide with any
built-in tool; minting + disposing an env leaves ``tool_registry`` byte-identical.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from lib.log import audit_log, get_logger
from runtime_guards import resolve_resource_budget

logger = get_logger(__name__)

__all__ = [
    'CUSTOM_TOOL_PREFIX',
    'MAX_CUSTOM_TOOL_CALL_ID_CHARS',
    'MAX_CUSTOM_TOOL_RESULT_CHARS',
    'ToolLimits',
    'ToolEnvironment',
    'CustomToolError',
    'mint_tool_env',
    'dispose_tool_env',
    'dispose_tool_env_after_terminal',
    'count_tool_envs',
    'request_client_tool_result',
    'resolve_client_tool_result',
]

# ── Namespace contract ──────────────────────────────────────────────
# Every custom tool name lives under this prefix. No built-in tool and no
# `mcp__` tool uses it, so collisions are structurally impossible; the prefix
# also passes the existing parse_tool_calls name guards (alphanumeric + `_`).
CUSTOM_TOOL_PREFIX = 'custom__'
_NAME_RE = re.compile(r'^custom__[A-Za-z0-9_]{1,56}$')

# These are protocol ceilings, not merely UI hints.  A client result waits in
# process memory until the blocked tool thread consumes it, so accepting an
# arbitrarily large callback would let one slow task retain an arbitrary body.
MAX_CUSTOM_TOOL_CALL_ID_CHARS = 128
MAX_CUSTOM_TOOL_RESULT_CHARS = 200_000
_MAX_CUSTOM_TOOL_TASK_ID_CHARS = 256

_VALID_MODES = ('client', 'webhook', 'sandbox')

# Per-process ceiling on live envs (mirrors _MAX_EPHEMERAL_SLOTS). Each env is
# cheap, but a caller leaking handles must not grow the table unboundedly.
_MAX_TOOL_ENVS = 1024

_lock = threading.Lock()
_envs: dict[str, 'ToolEnvironment'] = {}


class CustomToolError(ValueError):
    """Raised when a custom-tool definition is invalid (maps to HTTP 400)."""


# ══════════════════════════════════════════════════════════
#  Limits
# ══════════════════════════════════════════════════════════

@dataclass
class ToolLimits:
    """Per-request resource caps for a custom tool environment."""
    max_tools: int = 32
    max_total_schema_bytes: int = 256 * 1024
    per_call_timeout_s: float = 120.0
    max_result_chars: int = MAX_CUSTOM_TOOL_RESULT_CHARS


# ══════════════════════════════════════════════════════════
#  One custom tool
# ══════════════════════════════════════════════════════════

@dataclass
class _CustomTool:
    name: str
    schema: dict              # clean {type, function} — what the LLM sees
    mode: str                 # 'client' | 'webhook' | 'sandbox'
    write: bool               # runs in the serial write phase
    idempotent: bool          # dedup-cached within the task
    execution: dict           # backend-specific config (url/auth/command/…)


# ══════════════════════════════════════════════════════════
#  Client-handoff result registry (mirrors approval.py / human_guidance.py)
# ══════════════════════════════════════════════════════════

# A provider/tool call id is only unique inside its owning task.  Keying this
# process-wide registry by the bare call id lets two concurrent tasks that both
# use a positional id such as ``call_0`` overwrite each other's wait handle
# before the owner check in ``resolve_client_tool_result`` can run.
_client_results: dict[tuple[str, str], dict] = {}
_client_results_lock = threading.Lock()
_CLIENT_ABORT_POLL_INTERVAL = 1.0


def _client_result_capacity() -> int:
    """Bound waiters from the same launch-time budget as task admission.

    One admitted task can have at most ``ToolLimits.max_tools`` distinct
    client calls in a parallel tool round.  The absolute ceiling keeps an
    aggressively raised task budget from turning 200k-character callbacks
    into an accidental multi-gigabyte process cache.
    """
    active_tasks = resolve_resource_budget(
        'TOFU_MAX_INFLIGHT_TASKS', minimum=1, maximum=256)
    return max(32, min(512, active_tasks * ToolLimits().max_tools))


_MAX_CLIENT_RESULTS = _client_result_capacity()


def _require_handoff_id(value: object, *, field: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f'{field} must be a non-empty string')
    if len(value) > max_chars:
        raise ValueError(f'{field} too long (max {max_chars} chars)')
    return value


def _bound_result_text(content: Any, max_chars: int) -> str:
    text = content if isinstance(content, str) else json.dumps(
        content, ensure_ascii=False) if content is not None else ''
    if len(text) > max_chars:
        return text[:max_chars] + '\n… [custom tool result truncated]'
    return text


def request_client_tool_result(call_id: str, *, task: dict | None = None,
                               timeout: float = 120.0) -> tuple[str, bool]:
    """Block until the client POSTs a result for *call_id* (or timeout/abort).

    Returns ``(content, is_error)``. On timeout or abort, ``is_error`` is True
    and ``content`` explains why — surfaced to the LLM as the tool result.
    """
    task_id = _require_handoff_id(
        str((task or {}).get('id') or ''), field='task id',
        max_chars=_MAX_CUSTOM_TOOL_TASK_ID_CHARS)
    call_id = _require_handoff_id(
        call_id, field='custom tool call id',
        max_chars=MAX_CUSTOM_TOOL_CALL_ID_CHARS)
    try:
        owner_user_id = int((task or {}).get('_userId') or 0)
    except (TypeError, ValueError):
        owner_user_id = 0
    if owner_user_id < 1:
        raise ValueError(
            'Client tool handoff requires an owned task with a stable id')
    registry_key = (task_id, call_id)
    tool_env = (task or {}).get('_tool_env')
    env_handle = str(getattr(tool_env, 'handle_id', '') or '')
    evt = threading.Event()
    with _client_results_lock:
        if registry_key in _client_results:
            logger.error('[CustomTool] duplicate pending client handoff '
                         'task=%s call_id=%s', task_id[:8], call_id)
            return (
                f'Custom tool call {call_id} is already pending for this task.',
                True,
            )
        if len(_client_results) >= _MAX_CLIENT_RESULTS:
            logger.warning(
                '[CustomTool] client handoff capacity exhausted (%d)',
                _MAX_CLIENT_RESULTS)
            return (
                'Custom tool client-handoff capacity is temporarily full; '
                'retry the tool call later.',
                True,
            )
        _client_results[registry_key] = {
            'event': evt,
            'content': None,
            'is_error': False,
            'resolved': False,
            'task_id': task_id,
            'user_id': owner_user_id,
            'env_handle': env_handle,
            'max_result_chars': min(
                MAX_CUSTOM_TOOL_RESULT_CHARS,
                max(1, int(getattr(
                    getattr(tool_env, 'limits', None),
                    'max_result_chars', MAX_CUSTOM_TOOL_RESULT_CHARS))),
            ),
        }
    logger.info('[CustomTool] client handoff %s waiting (timeout=%.0fs)',
                call_id, timeout)
    deadline = time.time() + max(1.0, timeout)
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            with _client_results_lock:
                entry = _client_results.pop(registry_key, {})
            if entry.get('resolved'):
                return (entry.get('content') or '',
                        bool(entry.get('is_error')))
            logger.warning('[CustomTool] client handoff %s timed out', call_id)
            return (f'Custom tool call {call_id} timed out after {timeout:.0f}s '
                    'waiting for the client to return a result.', True)
        if evt.wait(timeout=min(remaining, _CLIENT_ABORT_POLL_INTERVAL)):
            with _client_results_lock:
                entry = _client_results.pop(registry_key, {})
            if not entry.get('resolved'):
                return ('Custom tool handoff ended without a terminal result.',
                        True)
            return (entry.get('content') or '', bool(entry.get('is_error')))
        if task is not None and task.get('aborted'):
            with _client_results_lock:
                entry = _client_results.pop(registry_key, {})
            if entry.get('resolved'):
                return (entry.get('content') or '',
                        bool(entry.get('is_error')))
            logger.info('[CustomTool] client handoff %s — task aborted', call_id)
            return ('Task aborted by user before the client returned a result.',
                    True)


def resolve_client_tool_result(
    call_id: str,
    content: str,
    *,
    task_id: str,
    user_id: int,
    is_error: bool = False,
) -> bool:
    """Resolve only a handoff owned by the authenticated task principal."""
    try:
        task_id = _require_handoff_id(
            task_id, field='task id',
            max_chars=_MAX_CUSTOM_TOOL_TASK_ID_CHARS)
        call_id = _require_handoff_id(
            call_id, field='custom tool call id',
            max_chars=MAX_CUSTOM_TOOL_CALL_ID_CHARS)
    except ValueError:
        return False
    if not isinstance(content, str):
        raise TypeError('custom tool result content must be a string')
    registry_key = (task_id, call_id)
    with _client_results_lock:
        entry = _client_results.get(registry_key)
        if not entry or entry.get('resolved'):
            logger.warning('[CustomTool] resolve for unknown task=%s call_id=%s '
                           '(expired or already resolved)', task_id[:8], call_id)
            return False
        if (entry.get('task_id') != task_id
                or int(entry.get('user_id') or 0) != int(user_id)):
            logger.warning(
                '[CustomTool] rejected foreign resolve call_id=%s task=%s',
                call_id, task_id[:8])
            return False
        # Bound before retaining the callback in the process-wide table.  The
        # downstream dispatch cap remains as defense in depth for webhook and
        # sandbox backends, which do not pass through this registry.
        entry['content'] = _bound_result_text(
            content, int(entry['max_result_chars']))
        entry['is_error'] = is_error
        entry['resolved'] = True
        entry['event'].set()
    logger.info('[CustomTool] client resolved %s (is_error=%s, len=%d)',
                call_id, is_error, len(entry['content']))
    return True


# ══════════════════════════════════════════════════════════
#  ToolEnvironment
# ══════════════════════════════════════════════════════════

@dataclass
class ToolEnvironment:
    """Request-scoped collection of custom tools + their handlers.

    Created by :func:`mint_tool_env`; attached to a task as
    ``task['_tool_env']``; disposed by :func:`dispose_tool_env`.
    """
    handle_id: str
    owner: str
    tools: list[_CustomTool]
    limits: ToolLimits
    minted_at: float = field(default_factory=time.time)
    disposed: bool = False

    # ── Derived views ──
    @property
    def schemas(self) -> list[dict]:
        """Clean LLM-facing schemas (no execution/write/idempotent keys)."""
        return [t.schema for t in self.tools]

    @property
    def write_names(self) -> frozenset[str]:
        return frozenset(t.name for t in self.tools if t.write)

    @property
    def idempotent_names(self) -> frozenset[str]:
        return frozenset(t.name for t in self.tools if t.idempotent)

    def _get(self, fn_name: str) -> _CustomTool | None:
        for t in self.tools:
            if t.name == fn_name:
                return t
        return None

    # ── The inverted lookup ──
    def resolve(self, fn_name: str) -> Callable | None:
        """Return a ToolHandler-shaped callable for *fn_name*, or None.

        Consulted by ``_execute_tool_one`` BEFORE the global ``tool_registry``.
        """
        tool = self._get(fn_name)
        if tool is None:
            return None

        def _handler(task, tc, name, tc_id, fn_args, rn, round_entry,
                     cfg, project_path, project_enabled, all_tools=None):
            return self._dispatch(tool, task, tc_id, fn_args, rn, round_entry)
        return _handler

    # ── Backend dispatch ──
    def _dispatch(self, tool: _CustomTool, task, tc_id, fn_args, rn,
                  round_entry) -> tuple[str, str, bool]:
        from lib.tasks_pkg.executor import _finalize_tool_round
        t0 = time.time()
        tid = (task.get('id', '?') or '?')[:8]
        logger.info('[Task %s] [CustomTool:%s] mode=%s called', tid,
                    tool.name, tool.mode)
        try:
            if tool.mode == 'client':
                content, is_err = self._run_client(tool, task, tc_id, fn_args,
                                                   rn, round_entry)
            elif tool.mode == 'webhook':
                content, is_err = self._run_webhook(tool, fn_args)
            elif tool.mode == 'sandbox':
                content, is_err = self._run_sandbox(tool, fn_args)
            else:  # pragma: no cover — validated at mint time
                content, is_err = (f'Unknown execution mode {tool.mode!r}', True)
        except Exception as e:
            logger.error('[Task %s] [CustomTool:%s] dispatch failed: %s',
                         tid, tool.name, e, exc_info=True)
            content, is_err = (f'Custom tool {tool.name} failed: '
                               f'{type(e).__name__}: {e}', True)

        content = self._cap(content)
        elapsed = time.time() - t0
        logger.info('[Task %s] [CustomTool:%s] done in %.1fs (len=%d, err=%s)',
                    tid, tool.name, elapsed, len(content), is_err)
        # Finalize the round so the UI doesn't show a dangling "searching…".
        if round_entry is not None and round_entry.get('status') != 'done':
            badge = '❌ custom' if is_err else '🧩 custom'
            meta = {
                'toolName': tool.name,
                'title': f'🧩 {tool.name}',
                'snippet': content[:120].replace('\n', ' '),
                'source': f'custom:{tool.mode}',
                'fetched': not is_err,
                'fetchedChars': len(content),
                'badge': badge,
            }
            try:
                _finalize_tool_round(task, rn, round_entry, [meta],
                                     query_override=round_entry.get('query', tool.name))
            except Exception as e:
                logger.debug('[CustomTool] finalize failed for %s: %s',
                             tool.name, e)
        return tc_id, content, False

    def _cap(self, content: Any) -> str:
        return _bound_result_text(content, self.limits.max_result_chars)

    # ── client: zero-trust handoff ──
    def _run_client(self, tool, task, tc_id, fn_args, rn,
                    round_entry) -> tuple[str, bool]:
        from lib.tasks_pkg.manager import append_event
        call_id = f'ctool_{secrets.token_hex(8)}'
        round_entry['status'] = 'awaiting_client_tool'
        round_entry['customCallId'] = call_id
        try:
            append_event(task, {
                'type': 'custom_tool_call',
                'roundNum': rn,
                'toolCallId': round_entry.get('toolCallId', tc_id),
                'callId': call_id,
                'toolName': tool.name,
                'arguments': fn_args,
            })
        except Exception as e:
            logger.warning('[CustomTool] custom_tool_call emit failed for %s: %s',
                           tool.name, e)
        return request_client_tool_result(
            call_id, task=task, timeout=self.limits.per_call_timeout_s)

    # ── webhook: SSRF-guarded remote function ──
    def _run_webhook(self, tool, fn_args) -> tuple[str, bool]:
        from lib.byo_egress import EgressDenied, validate_egress_url
        from lib.http_client import http_post
        url = tool.execution.get('url', '')
        try:
            validate_egress_url(url)   # use-time check defeats DNS rebind
        except EgressDenied as e:
            logger.debug('[CustomTool] webhook egress blocked for %s: %s', tool.name, e)
            return (f'Webhook blocked: {e}', True)
        headers = dict(tool.execution.get('headers') or {})
        auth = tool.execution.get('auth')
        if auth:
            headers.setdefault('Authorization', auth)
        timeout = float(tool.execution.get('timeout_s')
                        or self.limits.per_call_timeout_s)
        try:
            resp = http_post(url, json={'tool': tool.name, 'arguments': fn_args},
                             headers=headers, timeout=timeout,
                             allow_redirects=False)
        except Exception as e:
            logger.debug('[CustomTool] webhook request failed for %s: %s: %s',
                         tool.name, type(e).__name__, e)
            return (f'Webhook request failed: {type(e).__name__}: {e}', True)
        if resp.status_code >= 400:
            return (f'Webhook returned HTTP {resp.status_code}: '
                    f'{resp.text[:500]}', True)
        return (resp.text or '', False)

    # ── sandbox: operator-gated code execution ──
    def _run_sandbox(self, tool, fn_args) -> tuple[str, bool]:
        from lib.project_mod import execute_standalone_command
        command = tool.execution.get('command', '')
        env_args = json.dumps(fn_args, ensure_ascii=False)
        prev = os.environ.get('TOFU_TOOL_ARGS')
        os.environ['TOFU_TOOL_ARGS'] = env_args
        try:
            out = execute_standalone_command(
                'run_command', {'command': command,
                                'timeout': tool.execution.get('timeout_s')})
        except Exception as e:
            logger.warning('[CustomTool] sandbox command failed for %s: %s: %s',
                           tool.name, type(e).__name__, e)
            return (f'Sandbox command failed: {type(e).__name__}: {e}', True)
        finally:
            if prev is None:
                os.environ.pop('TOFU_TOOL_ARGS', None)
            else:
                os.environ['TOFU_TOOL_ARGS'] = prev
        return (out or '', False)


# ══════════════════════════════════════════════════════════
#  Validation + mint/dispose
# ══════════════════════════════════════════════════════════

def _builtin_tool_names() -> set[str]:
    """All tool names contributed by registered (non-custom) ToolSpecs."""
    names: set[str] = set()
    try:
        from lib.tools.registry import all_specs
        for spec in all_specs():
            if spec.key == 'custom':
                continue
            names |= set(spec.provides)
    except Exception as e:
        logger.debug('[CustomTool] builtin-name collection failed: %s', e)
    return names


def _validate_one(raw: dict, idx: int, builtins: set[str],
                  allow_sandbox: bool) -> _CustomTool:
    if not isinstance(raw, dict):
        raise CustomToolError(f'tools[{idx}] must be an object')
    fn = raw.get('function')
    if not isinstance(fn, dict):
        raise CustomToolError(f'tools[{idx}] missing function object')
    name = fn.get('name')
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise CustomToolError(
            f'tools[{idx}] name {name!r} invalid — custom tool names must '
            f"match '{CUSTOM_TOOL_PREFIX}<ident>' (e.g. custom__get_weather)")
    if name in builtins:
        raise CustomToolError(
            f'tools[{idx}] name {name!r} collides with a built-in tool')

    execution = raw.get('execution') or {}
    if not isinstance(execution, dict):
        raise CustomToolError(f'tools[{idx}] execution must be an object')
    mode = (execution.get('mode') or 'client').lower()
    if mode not in _VALID_MODES:
        raise CustomToolError(
            f'tools[{idx}] execution.mode must be one of {_VALID_MODES}')
    if mode == 'webhook':
        url = execution.get('url', '')
        if not isinstance(url, str) or not url.strip():
            raise CustomToolError(f'tools[{idx}] webhook mode requires execution.url')
        from lib.byo_egress import EgressDenied, validate_egress_url
        try:
            validate_egress_url(url)
        except EgressDenied as e:
            raise CustomToolError(f'tools[{idx}] webhook url rejected: {e}') from e
    if mode == 'sandbox':
        if not allow_sandbox:
            raise CustomToolError(
                f'tools[{idx}] sandbox mode is disabled on this server '
                '(operator must set TOFU_CUSTOM_TOOLS_ALLOW_SANDBOX=1)')
        if not (execution.get('command') or '').strip():
            raise CustomToolError(f'tools[{idx}] sandbox mode requires execution.command')

    # Clean schema — strip server-only keys so the LLM sees only {type, function}.
    clean = {'type': 'function', 'function': fn}
    return _CustomTool(
        name=name, schema=clean, mode=mode,
        write=bool(raw.get('write', False)),
        idempotent=bool(raw.get('idempotent', False)),
        execution=execution,
    )


def _sandbox_allowed() -> bool:
    return os.environ.get('TOFU_CUSTOM_TOOLS_ALLOW_SANDBOX', '').strip().lower() \
        in ('1', 'true', 'yes', 'on')


def mint_tool_env(*, tools: list, owner: str = '',
                  limits: ToolLimits | None = None) -> ToolEnvironment:
    """Validate *tools* and inject a request-scoped :class:`ToolEnvironment`.

    Args:
        tools: list of OpenAI-style function specs, each optionally carrying
            ``execution`` / ``write`` / ``idempotent`` keys.
        owner: opaque audit tag (typically the API key id).
        limits: optional :class:`ToolLimits` override.

    Returns:
        The minted :class:`ToolEnvironment` — pass to :func:`dispose_tool_env`.

    Raises:
        CustomToolError: any tool definition is invalid, or caps exceeded.
        RuntimeError: per-process env ceiling reached.
    """
    if not isinstance(tools, list) or not tools:
        raise CustomToolError('tools must be a non-empty list')
    lim = limits or ToolLimits()
    if len(tools) > lim.max_tools:
        raise CustomToolError(
            f'too many custom tools ({len(tools)} > {lim.max_tools})')

    try:
        total_bytes = len(json.dumps(tools, ensure_ascii=False).encode('utf-8'))
    except (TypeError, ValueError) as e:
        raise CustomToolError(f'tools not JSON-serializable: {e}') from e
    if total_bytes > lim.max_total_schema_bytes:
        raise CustomToolError(
            f'custom tool schemas too large ({total_bytes} bytes > '
            f'{lim.max_total_schema_bytes})')

    builtins = _builtin_tool_names()
    allow_sandbox = _sandbox_allowed()
    parsed: list[_CustomTool] = []
    seen: set[str] = set()
    for i, raw in enumerate(tools):
        t = _validate_one(raw, i, builtins, allow_sandbox)
        if t.name in seen:
            raise CustomToolError(f'duplicate custom tool name {t.name!r}')
        seen.add(t.name)
        parsed.append(t)

    with _lock:
        if len(_envs) >= _MAX_TOOL_ENVS:
            raise RuntimeError(
                f'Custom tool env pool full ({_MAX_TOOL_ENVS}); '
                'a caller is likely leaking handles')
        handle_id = f'custom_env_{secrets.token_hex(8)}'
        env = ToolEnvironment(handle_id=handle_id, owner=str(owner or ''),
                              tools=parsed, limits=lim)
        _envs[handle_id] = env

    audit_log('custom_tool_env_mint', handle=handle_id, owner=str(owner or ''),
              n_tools=len(parsed),
              modes=','.join(sorted({t.mode for t in parsed})),
              names=','.join(t.name for t in parsed))
    logger.info('[CustomTool] mint env=%s owner=%s n_tools=%d modes=%s',
                handle_id, owner or '?', len(parsed),
                ','.join(sorted({t.mode for t in parsed})))
    return env


def dispose_tool_env(env: ToolEnvironment | None) -> bool:
    """Remove an env from the table. Idempotent (False on second call)."""
    if env is None or not isinstance(env, ToolEnvironment):
        return False
    with _lock:
        if env.disposed or env.handle_id not in _envs:
            env.disposed = True
            _envs.pop(env.handle_id, None)
            logger.debug('[CustomTool] dispose noop env=%s', env.handle_id)
            return False
        env.disposed = True
        _envs.pop(env.handle_id, None)
    # Unblock only this environment's in-flight client handoffs.  The waiter
    # owns removal so it can still consume the terminal error atomically.
    with _client_results_lock:
        pending = [
            entry for entry in _client_results.values()
            if entry.get('env_handle') == env.handle_id
        ]
        for entry in pending:
            entry['content'] = (
                'Custom tool environment was disposed before the client '
                'returned a result.'
            )
            entry['is_error'] = True
            entry['resolved'] = True
            entry['event'].set()
    audit_log('custom_tool_env_dispose', handle=env.handle_id, owner=env.owner,
              lifetime_ms=int((time.time() - env.minted_at) * 1000))
    logger.info('[CustomTool] dispose env=%s owner=%s lifetime=%.1fs',
                env.handle_id, env.owner or '?', time.time() - env.minted_at)
    return True


def dispose_tool_env_after_terminal(task: dict, env: ToolEnvironment) -> None:
    """Dispose *env* once *task* reaches a terminal state (daemon-thread body).

    Mirrors ``lib.byo_resolve.dispose_after_terminal`` — bounded by a 1-hour
    ceiling so a wedged task can't leak the env forever.
    """
    deadline = time.time() + 3600
    while task.get('status') not in ('done', 'error', 'aborted'):
        if time.time() >= deadline:
            logger.warning('[CustomTool] env %s: task %s still running after 1h, '
                           'force-disposing', env.handle_id,
                           (task.get('id', '?') or '?')[:8])
            break
        time.sleep(0.5)
    dispose_tool_env(env)


def count_tool_envs() -> int:
    """Number of currently-live custom tool envs (for /capabilities + tests)."""
    with _lock:
        return len(_envs)
