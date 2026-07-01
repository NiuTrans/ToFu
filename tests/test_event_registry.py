"""tests/test_event_registry.py — Streaming-event contract drift guard.

``lib/agent_core/events.py`` is the declared, versioned registry of every
SSE/push event the runtime emits.  This test keeps it honest in BOTH
directions, so the contract a foreign frontend reads from
``/api/v1/capabilities`` never silently drifts from reality:

  A. **Backend → registry**: every event ``type`` the orchestrator/tooling
     emits via ``append_event(task, {'type': ...})`` (and the swarm/memory
     translators) is registered.
  B. **Frontend → registry**: every ``ev.type === "..."`` the built-in
     frontend handles is registered.

If this fails
-------------
You added or renamed an event.  Update ``lib/agent_core/events.py`` (add an
``EventSpec`` + ``EventType`` constant) — and, on a *breaking* shape change,
bump ``EVENT_CONTRACT_VERSION``.  Do NOT just silence the test: the whole point
is that external frontends discover the vocabulary from the registry.
"""

from __future__ import annotations

import os
import re

import pytest

from lib.agent_core.events import (
    EVENT_CONTRACT_VERSION,
    TRANSPORT_TYPES,
    event_types,
    get_event_spec,
)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, '..'))

# ── Backend emission scan ──
# Files that emit SSE events via append_event / emit_event with {'type': ...}.
_BACKEND_FILES = [
    'lib/tasks_pkg/orchestrator.py',
    'lib/tasks_pkg/tool_dispatch.py',
    'lib/tasks_pkg/executor.py',
    'lib/tasks_pkg/executor_image.py',
    'lib/tasks_pkg/endpoint.py',
    'lib/tasks_pkg/llm_fallback.py',
    'lib/tasks_pkg/stream_handler.py',
    'lib/tasks_pkg/manager.py',
    'lib/tasks_pkg/autopilot.py',
    'lib/tasks_pkg/compaction/_archive.py',
    'lib/tasks_pkg/compaction/_layer1.py',
    'lib/memory/prefetch.py',
    'lib/scheduler/executor.py',
    'lib/artifacts/events.py',
    'lib/swarm/events.py',
    'lib/presence/registry.py',
    # The SSE stream route itself authors the lifecycle snapshot/terminal
    # events on (re)connect / cold replay (state, done, sse_timeout) — via the
    # typed build_event chokepoint, so the contract guard must scan it too.
    'routes/chat.py',
]

# ``'type': 'X'`` strings that are NOT SSE events — message-content blocks
# (OpenAI/Anthropic shapes) and tool-call wrappers that happen to share the
# ``type`` key.  These live in the same files but never reach append_event.
_NOT_SSE_EVENT_TYPES = frozenset({
    'text', 'image_url', 'function', 'unserializable',
})

# Match ``'type': 'value'`` or ``'type':'value'`` (single or double quotes).
_TYPE_RE = re.compile(r"""['"]type['"]\s*:\s*['"]([a-z_]+)['"]""")

# Match typed emissions: ``EventType.PHASE`` / ``build_event(EventType.DONE``.
# Resolved to the underlying string via the EventType class so typed call
# sites are scanned exactly like literal ones (item-2 unification).
_EVENTTYPE_RE = re.compile(r"""\bEventType\.([A-Z_][A-Z0-9_]*)\b""")

# Match ``ev.type === "value"`` / ``frame.type === 'value'`` in JS.
_JS_TYPE_RE = re.compile(r"""\.type\s*===\s*['"]([a-z_]+)['"]""")

_FRONTEND_FILES = [
    'static/js/ui/sse_pipeline.js',
    'static/js/branch.js',
    # Cross-conversation presence strip — handles the 'presence' push event
    # (.type === "presence"); proves the type is frontend-handled, not just
    # backend-emitted.
    'static/js/presence.js',
]


def _read(rel: str) -> str:
    path = os.path.join(REPO, rel)
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def _backend_emitted_types() -> set[str]:
    from lib.agent_core.events import EventType
    found: set[str] = set()
    for rel in _BACKEND_FILES:
        path = os.path.join(REPO, rel)
        if not os.path.isfile(path):
            continue
        src = _read(rel)
        # Literal ``'type': 'x'`` emissions.
        for m in _TYPE_RE.finditer(src):
            t = m.group(1)
            if t not in _NOT_SSE_EVENT_TYPES:
                found.add(t)
        # Typed ``EventType.CONST`` emissions (build_event / emit call sites).
        for m in _EVENTTYPE_RE.finditer(src):
            val = getattr(EventType, m.group(1), None)
            if isinstance(val, str):
                found.add(val)
    return found


def _frontend_handled_types() -> set[str]:
    found: set[str] = set()
    for rel in _FRONTEND_FILES:
        path = os.path.join(REPO, rel)
        if not os.path.isfile(path):
            continue
        for m in _JS_TYPE_RE.finditer(_read(rel)):
            found.add(m.group(1))
    return found


def test_contract_version_is_positive_int():
    assert isinstance(EVENT_CONTRACT_VERSION, int) and EVENT_CONTRACT_VERSION >= 1


def test_every_backend_emitted_type_is_registered():
    """Direction A: no event leaves the backend undeclared."""
    registered = event_types()
    emitted = _backend_emitted_types()
    missing = sorted(emitted - registered)
    assert not missing, (
        'Backend emits SSE event type(s) NOT declared in '
        'lib/agent_core/events.py:\n  ' + '\n  '.join(missing)
        + '\n\nAdd an EventSpec + EventType constant for each. If a match is a '
        'false positive (a message-content block, not an SSE event), add it to '
        '_NOT_SSE_EVENT_TYPES in this test.')


def test_every_frontend_handled_type_is_registered():
    """Direction B: the built-in frontend handles only declared events."""
    registered = event_types()
    handled = _frontend_handled_types()
    missing = sorted(handled - registered)
    assert not missing, (
        'Frontend handles event type(s) NOT declared in '
        'lib/agent_core/events.py:\n  ' + '\n  '.join(missing))


def test_registry_has_no_orphans_vs_known_surfaces():
    """Sanity: each registered type is either emitted by the backend, handled
    by the frontend, or an explicitly-exempt transport signal.

    Guards against the registry accumulating dead vocabulary that no code path
    actually produces or consumes.
    """
    registered = event_types()
    live = _backend_emitted_types() | _frontend_handled_types() | set(TRANSPORT_TYPES)
    orphans = sorted(registered - live)
    assert not orphans, (
        'Registered event type(s) neither emitted by the backend nor handled '
        'by the frontend (dead vocabulary?):\n  ' + '\n  '.join(orphans)
        + '\n\nEither wire them up or remove the EventSpec.')


def test_terminal_and_interaction_specs_consistent():
    """done is terminal; interaction events require a response."""
    assert get_event_spec('done').terminal is True
    for t in ('human_guidance_request', 'write_approval_request',
              'stdin_request', 'approval_required'):
        assert get_event_spec(t).requires_response is True, t


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
