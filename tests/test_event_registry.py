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

import glob
import os
import re

import pytest

from lib.agent_core.events import (
    EVENT_CONTRACT_VERSION,
    event_types,
    get_event_spec,
)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, '..'))
pytestmark = pytest.mark.unit

# ── Backend emission scan ──
# Files/PACKAGES that emit SSE events via append_event / emit_event.
#
# ⚠️ ENTRIES MAY BE FILES, DIRECTORIES, OR GLOBS. Every module that was a single file
# here has been split into a package at least once (orchestrator, tool_dispatch,
# executor, endpoint, manager, …). The previous form of this list named the
# monolith .py paths and the scanner did ``if not os.path.isfile: continue`` —
# so after each split the guard silently scanned FEWER files and still passed.
# Measured 2026-07-27: 10 of 21 listed paths no longer existed, i.e. HALF the
# emit surface went unverified while this test stayed green. (The comment that
# used to live here even documented an earlier instance of the same accident
# and only patched the two paths noticed at the time.)
#
# The durable fix is structural, not a longer list: name the PACKAGE (or a glob
# for a family of root-level modules), let ``_resolve_scan_targets`` expand it,
# and make a path that resolves to NOTHING a hard failure (see
# ``test_scan_targets_all_resolve``). A future split then changes the file set
# under a package without touching this list, and a genuine deletion/rename
# turns red instead of quietly shrinking the guard's reach.
_BACKEND_FILES = [
    'lib/tasks_pkg/orchestrator',
    'lib/tasks_pkg/tool_dispatch',
    'lib/tasks_pkg/executor',
    'lib/tasks_pkg/executor_image',
    'lib/orchestration_chat_*.py',
    'lib/tasks_pkg/llm_fallback',
    'lib/tasks_pkg/stream_handler',
    'lib/tasks_pkg/manager',
    'lib/tasks_pkg/autopilot.py',
    'lib/tasks_pkg/compaction/_archive.py',
    'lib/tasks_pkg/compaction/_layer1.py',
    'lib/memory/prefetch',
    'lib/scheduler/executor',
    'lib/artifacts/events.py',
    'lib/swarm/events.py',
    'lib/presence/registry.py',
]

# ``'type': 'X'`` strings that are NOT SSE events — message-content blocks
# (OpenAI/Anthropic shapes) and tool-call wrappers that happen to share the
# ``type`` key.  These live in the same files but never reach append_event.
_NOT_SSE_EVENT_TYPES = frozenset({
    'text', 'image_url', 'function', 'unserializable',
    # A tool-result CONTENT block passed to _finalize_tool_round as
    # ``[{'type': 'error', 'content': …}]`` (executor.py:714, search.py:449/610)
    # — nested inside a tool round's results, NOT a top-level SSE frame.
    'error',
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
    '@runtime:presence.js',
    # Current chat-event consumer; unlike the retired sse_pipeline section it
    # is present in the release manifest and branches on registered SSE types.
    '@runtime:ui/streaming_render.js',
]


def _read(rel: str) -> str:
    if rel.startswith('@runtime:'):
        from tests._runtime_sections import runtime_section
        return runtime_section(rel.removeprefix('@runtime:'))
    path = os.path.join(REPO, rel)
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def _resolve_scan_targets(entries: list[str]) -> tuple[list[str], list[str]]:
    """Expand files, package dirs, and globs to concrete scan targets.

    Returns ``(resolved, unresolved)``.

    A directory yields every ``*.py`` under it recursively, so a package split
    cannot shrink the guard's reach. An entry that is neither an existing file
    nor an existing directory lands in ``unresolved`` — the caller turns that
    into a FAILURE rather than skipping it, which is the whole point: a stale
    entry must be loud, not silently dropped. Globs provide the same protection
    for related root-level modules that do not live in a package directory.
    """
    resolved: list[str] = []
    unresolved: list[str] = []
    for rel in entries:
        if rel.startswith('@runtime:'):
            try:
                _read(rel)
            except AssertionError:
                unresolved.append(rel)
            else:
                resolved.append(rel)
            continue
        p = os.path.join(REPO, rel)
        if glob.has_magic(rel):
            matches = sorted(path for path in glob.glob(p) if os.path.isfile(path))
            if matches:
                resolved.extend(os.path.relpath(path, REPO) for path in matches)
            else:
                unresolved.append(rel)
            continue
        if os.path.isfile(p):
            resolved.append(rel)
        elif os.path.isdir(p):
            for dirpath, _dirs, names in os.walk(p):
                for nm in sorted(names):
                    if nm.endswith('.py'):
                        resolved.append(os.path.relpath(
                            os.path.join(dirpath, nm), REPO))
        else:
            unresolved.append(rel)
    return resolved, unresolved


def _backend_emitted_types() -> set[str]:
    from lib.agent_core.events import EventType
    found: set[str] = set()
    for rel in _resolve_scan_targets(_BACKEND_FILES)[0]:
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
    for rel in _resolve_scan_targets(_FRONTEND_FILES)[0]:
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


def test_scan_targets_all_resolve():
    """META: every scan-list entry must resolve, and the scan must reach a
    plausible number of files.

    This is the guard ON the guard. Both directions above are only as strong as
    the file set they read, and for months that set shrank silently every time a
    module was split into a package (measured: 10 of 21 entries had ceased to
    exist, so half the emit surface was unverified while every test here stayed
    green). Asserting the targets RESOLVE converts that failure mode from
    invisible to red.

    The floor counts are deliberately loose — they exist to catch "the scan
    collapsed to almost nothing", not to be re-tuned on every refactor.
    """
    back_ok, back_bad = _resolve_scan_targets(_BACKEND_FILES)
    front_ok, front_bad = _resolve_scan_targets(_FRONTEND_FILES)
    assert not back_bad, (
        'These _BACKEND_FILES entries resolve to NOTHING — the contract scan '
        'silently skipped them, so events emitted there are unverified:\n  '
        + '\n  '.join(back_bad)
        + '\n\nPoint each at the module/package that took over its role (a '
        'directory or glob entry is fine).')
    assert not front_bad, (
        'These _FRONTEND_FILES entries resolve to NOTHING:\n  '
        + '\n  '.join(front_bad))
    assert len(back_ok) >= 20, (
        f'backend emit scan collapsed to {len(back_ok)} file(s) — expected the '
        'orchestrator/dispatch/executor/endpoint/manager packages to contribute '
        'many more. The scan list has probably drifted off the real code.')
    assert len(front_ok) >= 2, f'frontend scan collapsed to {len(front_ok)} file(s)'


def test_terminal_and_interaction_specs_consistent():
    """done is terminal; interaction events require a response."""
    assert get_event_spec('done').terminal is True
    for t in ('human_guidance_request', 'write_approval_request',
              'stdin_request', 'approval_required'):
        assert get_event_spec(t).requires_response is True, t


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
