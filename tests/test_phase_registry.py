"""tests/test_phase_registry.py — Phase (stream status-text) drift guard.

``lib/agent_core/events.py`` declares the PHASE sub-vocabulary: every
``phase`` value the runtime can push on the ``phase`` event (the stream's
status-text channel), each with its domain, purpose and payload fields —
constructed through the ONE typed pair ``build_phase`` / ``emit_phase``.

This test keeps the registry honest in BOTH directions, so the status pushes
stay perceivable from one place (and machine-discoverable via the
``/api/v1/capabilities`` ``phases`` block):

  A. **Backend → registry**: every ``phase='x'`` / ``'phase': 'x'`` literal
     and every ``Phase.X`` reference in ``lib/`` is a registered phase value
     (or a documented carve-out — a different channel that merely shares the
     field name, e.g. ``flow_iteration.phase``).
  B. **Frontend → registry**: every ``.phase === "x"`` the built-in chat
     frontend branches on is registered (or a documented client-local state).
  C. **Unified interface (the ratchet)**: no raw ``{'type': 'phase'`` dict
     literals in ``lib/`` — every status push is constructed via
     ``build_phase`` / ``emit_phase`` / ``build_event(EventType.PHASE, …)``,
     so a cross-cutting optimization lands at one chokepoint.
  D. **No dead vocabulary**: every registered phase is actually referenced by
     an emitter.

If this fails
-------------
You added or renamed a phase push.  Update ``lib/agent_core/events.py`` (add a
``Phase`` constant + ``PhaseSpec``) and emit it via ``emit_phase(task,
Phase.X, …)``.  If a match is a false positive (another channel's ``phase``
field, not the PHASE event), add it to ``_NON_PHASE_EVENT_VALUES`` with a
reason.  Do NOT just silence the test: the whole point is that the full set of
status pushes is perceivable from the registry.
"""

from __future__ import annotations

import json
import os
import re

import pytest

from lib.agent_core.events import (
    Phase,
    build_phase,
    emit_phase,
    get_phase_spec,
    is_registered_phase,
    phase_values,
    to_capabilities_dict,
)

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, '..'))


# ── Value-scan patterns ──
# ``phase='x'`` / ``phase='x'`` kwargs and ``'phase': 'x'`` dict literals.
_PHASE_KWARG_RE = re.compile(r"""\bphase\s*=\s*'([a-z_]+)'""")
_PHASE_DICT_RE = re.compile(r"""['"]phase['"]\s*:\s*['"]([a-z_]+)['"]""")
# Typed references: ``Phase.RETRYING`` — resolved against the Phase class.
_PHASE_CONST_RE = re.compile(r"""\bPhase\.([A-Z_][A-Z0-9_]*)\b""")
# Raw phase-event construction: ``{'type': 'phase'`` (either quote style).
_RAW_PHASE_EVENT_RE = re.compile(r"""['"]type['"]\s*:\s*['"]phase['"]""")
# Frontend branches over either an event property (``event.phase``) or the
# explicitly scoped local projection (``const phase = value.phase``).  Scan
# only the phase-owner files below so unrelated domain objects cannot widen
# the registry accidentally.
_JS_PHASE_RE = re.compile(
    r"""(?:\.phase|\bphase)\s*===\s*['"]([a-z_]+)['"]"""
)


# ``phase='x'`` values that are NOT the PHASE event's status push — other
# channels that merely share the field name. Each carries its reason; the
# scan fails on any value not in this set and not in the registry, so a NEW
# out-of-channel use must be justified here.
_NON_PHASE_EVENT_VALUES: dict[str, str] = {
    # flow_iteration / flow_new_turn events' phase field (a different type).
    'planning': 'flow_iteration.phase',
    'reviewing': 'flow_iteration.phase',
    'planner': 'endpoint next_phase marker / agent_verdict decision',
    'worker': 'agent_verdict decision / endpoint next_phase marker',
    'stop': 'agent_verdict decision / endpoint next_phase marker',
    # presence heartbeats + swarm agent rows (lib/presence channel and
    # swarm_agent_phase event — neither is the PHASE event).
    'generating': 'presence heartbeat phase',
    'tool_use': 'presence / swarm_agent_phase',
    'stalled': 'swarm agent row phase',
    'timeout': 'swarm agent row phase',
    'no_progress': 'swarm agent row phase',
    'running': 'swarm_agent_phase / swarm agent row',
    'done': 'swarm agent row / video_analysis store record',
    # swarm_phase top-level orchestration event.
    'spawning': 'swarm_phase event',
    'complete': 'swarm_phase event',
    'spawn_more': 'swarm_phase event',
    # orchestration_engine sub-task dict field (not an event).
    'tool': 'orchestration_engine task dict',
    # video_analysis store record field (DB row, not an event).
    'probe': 'video_analysis store record',
    # tools registry build phases (ToolSpec assembly order).
    'base': 'tools registry build phase',
    'capability': 'tools registry build phase',
    # routes/api_v1/agents.py progress events (headless agent-run channel).
    'started': 'api_v1 agents progress event',
    'finished': 'api_v1 agents progress event',
    # Server boot lock lifecycle and swarm-agent failure rows are separate
    # state machines; neither value is emitted on EventType.PHASE.
    'booting': 'server_boot lock state',
    'serving': 'server_boot lock state',
    'error': 'swarm agent row / event phase',
    'begin': 'SQLite writer transaction state',
    # File-transfer and storage-copy records expose their own lifecycle field
    # named ``phase``; none of these dictionaries rides EventType.PHASE.
    'completed': 'browser file-transfer record state',
    'failed': 'media-attachment processing record state',
    'copying': 'storage fastpath copy state',
    'copying_database': 'storage shipper copy state',
    'database_complete': 'storage shipper copy state',
}

# Frontend ``.phase === "x"`` values that are NOT pushed PHASE values.
_FRONTEND_PHASE_EXEMPTIONS: dict[str, str] = {
    # Derived CLIENT-side from thinking deltas (sse_pipeline.js /
    # streaming_render.js → setStreamPhase) — never pushed by the backend.
    'thinking_active': 'client-local derived phase state',
    # Autopilot VU presentation states and the no-frame fallback are created
    # by the typed conversation projection, not pushed by the backend.
    'warming': 'client-local autopilot virtual-user presentation',
    'responding': 'client-local autopilot virtual-user presentation',
    'waiting': 'client-local missing-phase fallback presentation',
    # The durable Turn/Attempt join derives these dispatch states when no
    # transient worker phase exists. ``executor_queued`` also has a canonical
    # backend phase now, so its no-frame fallback is covered by the registry.
    'executor_preparing': 'client-derived pending attempt without task binding',
    'worker_starting': 'client-derived running attempt without worker phase',
    'storage_wedged': 'client presentation of the storage-wedge status flag',
    # flow_iteration branches in the typed conversation projection.
    'planning': 'flow_iteration.phase branch',
    'reviewing': 'flow_iteration.phase branch',
}

# Frontend files whose ``.phase ===`` branches consume the chat PHASE event.
_FRONTEND_FILES = [
    'frontend/src/conversation/ui/classic-conversation-renderers.ts',
]


def _lib_py_files() -> list[str]:
    """Tracked ``lib/**/*.py`` via the git index.

    ``os.walk`` on this repo's FUSE mount measured ~300s in a bad window (the
    same class that made the census suite read the git index instead,
    294s→0.2s) — the index answers instantly and untracked scratch files
    should not be scanned anyway. Falls back to os.walk outside a git
    checkout (e.g. the exported tree ships without .git).
    """
    import subprocess
    try:
        out = subprocess.run(
            ['git', 'ls-files', 'lib/*.py'], cwd=REPO,
            capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            return [
                rel for rel in out.stdout.splitlines()
                if rel.endswith('.py') and os.path.isfile(os.path.join(REPO, rel))
            ]
    except Exception:
        pass
    out = []
    lib_root = os.path.join(REPO, 'lib')
    for dirpath, _dirs, names in os.walk(lib_root):
        for nm in sorted(names):
            if nm.endswith('.py'):
                out.append(os.path.relpath(os.path.join(dirpath, nm), REPO))
    return out


def _read(rel: str) -> str:
    if rel.startswith('@runtime:'):
        from tests._runtime_sections import runtime_section
        return runtime_section(rel.removeprefix('@runtime:'))
    with open(os.path.join(REPO, rel), 'r', encoding='utf-8') as f:
        return f.read()


def _emitted_phase_literals() -> dict[str, set[str]]:
    """Map phase value → {files} for every phase literal found in lib/.

    The registry module itself is excluded: its docstrings carry
    ``phase='x'`` syntax examples that are documentation, not emissions.
    """
    found: dict[str, set[str]] = {}
    for rel in _lib_py_files():
        if rel == os.path.join('lib', 'agent_core', 'events.py'):
            continue
        src = _read(rel)
        for m in _PHASE_KWARG_RE.finditer(src):
            found.setdefault(m.group(1), set()).add(rel)
        for m in _PHASE_DICT_RE.finditer(src):
            found.setdefault(m.group(1), set()).add(rel)
    return found


def _typed_phase_refs() -> dict[str, set[str]]:
    """Map phase value → {files} for every ``Phase.X`` reference in lib/
    (outside the registry module itself)."""
    found: dict[str, set[str]] = {}
    for rel in _lib_py_files():
        if rel == os.path.join('lib', 'agent_core', 'events.py'):
            continue
        for m in _PHASE_CONST_RE.finditer(_read(rel)):
            val = getattr(Phase, m.group(1), None)
            if isinstance(val, str):
                found.setdefault(val, set()).add(rel)
    return found


# ── A. backend → registry ──

def test_every_emitted_phase_value_is_registered():
    registered = phase_values()
    carved = set(_NON_PHASE_EVENT_VALUES)
    unknown: dict[str, set[str]] = {}
    for value, files in _emitted_phase_literals().items():
        if value not in registered and value not in carved:
            unknown[value] = files
    assert not unknown, (
        'phase value(s) used in lib/ but NOT declared in the Phase registry '
        '(lib/agent_core/events.py):\n  '
        + '\n  '.join(f'{v!r}  ←  {sorted(fs)}' for v, fs in unknown.items())
        + '\n\nAdd a Phase constant + PhaseSpec (and emit it via emit_phase). '
        'If the match is another channel that merely shares the field name, '
        'add it to _NON_PHASE_EVENT_VALUES with a reason.')


def test_every_typed_phase_reference_is_registered():
    """A ``Phase.X`` constant deleted from the registry but still referenced
    (or vice versa) must turn red."""
    registered = phase_values()
    missing = sorted(v for v in _typed_phase_refs() if v not in registered)
    assert not missing, (
        'Phase constant(s) referenced in lib/ but missing from the registry: '
        + ', '.join(missing))


# ── B. frontend → registry ──

def test_every_frontend_handled_phase_is_registered():
    registered = phase_values()
    exempt = set(_FRONTEND_PHASE_EXEMPTIONS)
    handled: dict[str, str] = {}
    for rel in _FRONTEND_FILES:
        for m in _JS_PHASE_RE.finditer(_read(rel)):
            handled.setdefault(m.group(1), rel)
    missing = sorted(v for v in handled
                     if v not in registered and v not in exempt)
    assert not missing, (
        'Frontend branches on phase value(s) NOT declared in the Phase '
        'registry:\n  '
        + '\n  '.join(f'{v!r}  ←  {handled[v]}' for v in missing)
        + '\n\nRegister it (Phase constant + PhaseSpec) or — if the client '
        'derives it locally and the backend never pushes it — add it to '
        '_FRONTEND_PHASE_EXEMPTIONS with a reason.')


# ── C. the unified-interface ratchet ──

def test_no_raw_phase_event_literals_in_lib():
    """Every PHASE event is constructed via the typed interface.

    The ONLY place a raw ``{'type': 'phase'`` substring may appear is
    ``lib/agent_core/events.py`` docstrings (they document the byte-parity).
    A raw literal anywhere else reintroduces the scattered, unperceivable
    construction this registry removed.
    """
    offenders = []
    for rel in _lib_py_files():
        if rel == os.path.join('lib', 'agent_core', 'events.py'):
            continue
        for lineno, line in enumerate(_read(rel).splitlines(), 1):
            if _RAW_PHASE_EVENT_RE.search(line):
                offenders.append(f'{rel}:{lineno}: {line.strip()[:100]}')
    assert not offenders, (
        'Raw {\'type\': \'phase\'} literal(s) outside the typed interface — '
        'construct via emit_phase(task, Phase.X, …) / build_phase instead:\n  '
        + '\n  '.join(offenders))


# ── D. no dead vocabulary ──

def test_every_registered_phase_is_referenced():
    """Each registered phase must be referenced by at least one emitter in
    lib/ (as a ``Phase.X`` reference or a literal) — the registry must not
    accumulate dead vocabulary nothing pushes."""
    refs = _typed_phase_refs()
    lits = _emitted_phase_literals()
    dead = sorted(p for p in phase_values()
                  if p not in refs and p not in lits)
    assert not dead, (
        'Registered phase(s) never referenced by any emitter (dead '
        'vocabulary?):\n  ' + '\n  '.join(dead)
        + '\n\nEither wire them up or remove the Phase constant + PhaseSpec.')


# ── Registry self-consistency + typed constructor parity ──

def test_phase_constants_match_registry_one_to_one():
    consts = {k: v for k, v in vars(Phase).items() if k.isupper()}
    assert set(consts.values()) == set(phase_values())
    # And every constant resolves to a spec of the same value.
    for name, value in consts.items():
        spec = get_phase_spec(value)
        assert spec is not None and spec.phase == value, name


def test_build_phase_byte_identity_with_the_old_literal():
    got = build_phase(Phase.LLM_THINKING, detail='Generating response…',
                      detailKey='stream.phase.generatingResponse', roundNum=1)
    literal = {'type': 'phase', 'phase': 'llm_thinking',
               'detail': 'Generating response…',
               'detailKey': 'stream.phase.generatingResponse', 'roundNum': 1}
    assert got == literal
    assert list(got.keys()) == list(literal.keys())
    assert json.dumps(got, ensure_ascii=False) == json.dumps(
        literal, ensure_ascii=False)


def test_emit_phase_delivers_through_append_event():
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    task = chat_task_runtime.create(user_id=1)
    emit_phase(task, Phase.WORKING, detail='go')
    last = task['events'][-1]
    assert last['type'] == 'phase'
    assert last['phase'] == 'working'
    assert last['detail'] == 'go'


def test_unregistered_phase_still_builds_but_is_flagged():
    # Forward-compat: the wire stays permissive (the drift tests are the
    # enforcement), and the helper answers the membership question.
    e = build_phase('some_future_phase', detail='x')
    assert e['phase'] == 'some_future_phase'
    assert not is_registered_phase('some_future_phase')


def test_capabilities_exposes_the_phase_vocabulary():
    phases = to_capabilities_dict()['phases']
    assert 'chat' in phases, 'the chat-domain phases must be discoverable'
    chat_values = {p['phase'] for p in phases['chat']}
    assert {'llm_thinking', 'tool_exec', 'retrying', 'waiting_model',
            'working'} <= chat_values
    # Every registered phase appears under at least one domain.
    flattened = {p['phase'] for entries in phases.values() for p in entries}
    assert flattened == set(phase_values())


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
