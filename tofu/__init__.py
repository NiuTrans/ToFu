"""tofu — the supported in-process façade for embedders.

Run a Tofu agent turn **in the same Python process** — no HTTP hop, no SSE
re-parsing, no vendoring of ``lib/`` internals. This is the contract for
trusted same-process embedders (e.g. a Flask app that wants Tofu's tool
loop, fallback chain, compaction, MCP, and typed errors without standing up
or calling a second service).

    import tofu

    # Blocking turn — mirrors POST /api/v1/chat/completions (stream=false).
    res = tofu.chat(
        messages=[{"role": "user", "content": "Hi"}],
        model="claude-opus-4-7",
        response_format={"type": "json_object"},
        config={"thinkingDepth": "high", "tools": ["search"]},
    )
    print(res.content, res.usage)
    if not res.ok:
        print("failed:", res.error["kind"], res.error["message"])

    # Streaming — yields the SAME native event dicts the SSE/WS contract uses
    # (see GET /api/v1/capabilities → events).
    for ev in tofu.stream(messages=[{"role": "user", "content": "Hi"}],
                          model="claude-opus-4-7"):
        if ev["type"] == "delta" and ev.get("content"):
            print(ev["content"], end="", flush=True)

    # Self-describe — this deployment's models / tools / config schema.
    caps = tofu.capabilities()

Design boundary (intentional): this façade is the model/agent runtime only.
Multi-user **billing** and **BYO ephemeral providers** are HTTP-key-scoped
features and live exclusively behind ``/api/v1/*`` — they are out of scope
in-process. Use the HTTP API (or ``clients/python`` ``tofu-sdk``) when you
need those.

Stability: the request knobs mirror the HTTP chat body, streamed events
mirror the declared event contract, and errors are the typed envelope
(``lib.error_envelope``). Additive changes only; ``tofu.__api_version__``
tracks the contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ['chat', 'stream', 'capabilities', 'ChatResult', '__api_version__']

__api_version__ = 'v1'


# ChatResult is re-exported from the kernel so embedders import it from the
# stable top-level package, not from a lib.* internal path.
from lib.tasks_pkg.entry import ChatResult


def chat(
    messages: list[dict],
    *,
    model: str = '',
    config: dict | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    tools: list | None = None,
    response_format: dict | None = None,
    thinking_depth: str | None = None,
    user: str = '',
    timeout_s: float = 600.0,
) -> ChatResult:
    """Run one agent turn in-process and block until it finishes.

    Parameters mirror the ``POST /api/v1/chat/completions`` body. ``config``
    holds Tofu-specific keys (see :func:`capabilities` → ``config_schema``);
    explicit ``config`` values take precedence over the top-level knobs.

    Returns a :class:`ChatResult`. Inspect ``res.ok`` /
    ``res.error['kind']`` rather than trusting a non-empty ``content`` —
    a turn can finish with an empty body and a typed error envelope.

    Raises ``TimeoutError`` if the turn does not reach a terminal state
    within ``timeout_s``.
    """
    from lib.tasks_pkg.entry import run_chat_sync

    return run_chat_sync(
        messages, model=model, config=config, timeout_s=timeout_s,
        max_tokens=max_tokens, temperature=temperature, tools=tools,
        response_format=response_format, thinking_depth=thinking_depth,
        user=user,
    )


def stream(
    messages: list[dict],
    *,
    model: str = '',
    config: dict | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    tools: list | None = None,
    response_format: dict | None = None,
    thinking_depth: str | None = None,
    user: str = '',
    timeout_s: float = 600.0,
) -> Iterator[dict]:
    """Run one agent turn in-process, yielding native Tofu event dicts.

    Each item is an event (``delta`` / ``phase`` / ``tool_start`` /
    ``tool_result`` / … / terminal ``done``) matching the contract served
    at ``GET /api/v1/capabilities`` → ``events``. The generator stops after
    the terminal ``done`` event. The minimal consumer only needs ``delta``
    (append ``ev['content']`` / ``ev['thinking']``) and ``done`` (inspect
    ``ev.get('error')``).
    """
    from lib.tasks_pkg.entry import run_chat_stream

    yield from run_chat_stream(
        messages, model=model, config=config, timeout_s=timeout_s,
        max_tokens=max_tokens, temperature=temperature, tools=tools,
        response_format=response_format, thinking_depth=thinking_depth,
        user=user,
    )


def _ensure_flask_quart_shim() -> None:
    """Make ``import flask`` resolve to Quart, the same way ``server.py`` does.

    ``capabilities()`` reaches into ``routes`` (for the single source of
    truth on the schema), and importing ``routes`` triggers
    ``routes/push.py``'s ``@push_bp.websocket`` decorator — which only exists
    on a Quart ``Blueprint``. Outside the running server (e.g. an embedder
    calling ``tofu.capabilities()`` standalone) the shim isn't installed yet,
    so we install it on demand. No-op when Quart is absent or already shimmed.
    """
    import sys
    if getattr(sys.modules.get('flask'), '__name__', '') == 'quart':
        return
    try:
        import quart
    except ImportError:
        return
    sys.modules['flask'] = quart
    for attr in ('json', 'globals', 'helpers', 'wrappers', 'ctx'):
        mod = sys.modules.get(f'quart.{attr}')
        if mod is None:
            try:
                mod = __import__(f'quart.{attr}', fromlist=[attr])
            except ImportError:
                continue
        sys.modules[f'flask.{attr}'] = mod


def capabilities() -> dict:
    """Return this deployment's runtime registry (models / tools / agents /
    presets / backends / config schema / event contract).

    Same payload as ``GET /api/v1/capabilities`` (minus the HTTP envelope),
    built from the same helpers so there is no second source of truth.
    """
    _ensure_flask_quart_shim()
    from routes.api_v1.capabilities import (
        _agents_summary, _backends, _config_schema, _events_contract,
        _features, _models_summary, _presets, _tools_summary,
    )
    from lib.api_keys import ALL_SCOPES
    try:
        from lib.version import __version__ as ver
    except ImportError:
        ver = 'unknown'

    return {
        'tofu_version': ver,
        'api_version': 'v1',
        'features': _features(),
        'models': _models_summary(),
        'tools': _tools_summary(),
        'agents': _agents_summary(),
        'presets': _presets(),
        'backends': _backends(),
        'scopes': sorted(ALL_SCOPES),
        'config_schema': _config_schema(),
        'events': _events_contract(),
    }
