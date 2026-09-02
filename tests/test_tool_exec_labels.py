#!/usr/bin/env python3
"""Tool-exec phase labels: emoji-free + honest verbs + structured tool names.

WHY
---
Owner report (2026-08-03): the stream phase text showed "✏️ Applying changes"
while the only visible tool card was a read_files — three defects, two of
which are backend-side:

  1. EMOJI: the ``_TOOL_EXEC_LABELS`` family carried emoji prefixes
     (✏️/🔍/📖/…) even though the frontend renders its own SVG icon system
     (``_phaseIcons`` is intentionally empty) — owner directive: no emoji in
     the phase text.
  2. WORDING: "Applying changes" for ``apply_diff`` is vaguer than the act
     itself — if the tool is patching, say patching ("Patching files").
  3. STRUCTURE: the round-open ``llm_thinking`` phase listed the previous
     round's tools as a pre-joined English ``toolContext`` string — an
     i18n client could not localize it. The backend now also ships the raw
     tool NAMES (``toolContextTools``) so the client composes the suffix in
     the UI language; the poll-lane phase snapshot forwards the same field.

The frontend half (the stale tool_exec phase-row gate + the localized
rendering) is pinned by the ConversationSurface and tool-round suites.

Failing-first (2026-08-03): every check below is RED against the pre-fix
source (labels carried emoji, "Applying changes", no toolContextTools).

Run DIRECTLY (env-guarded):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/test_tool_exec_labels.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _mk_task():
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    return chat_task_runtime.create(user_id=1)


@pytest.fixture()
def _no_persist(monkeypatch):
    """Keep the durable event-log write out of unit tests (DB hygiene)."""
    import lib.tasks_pkg.event_log as el
    monkeypatch.setattr(el, 'append_persistent_event',
                        lambda *a, **kw: None, raising=False)
    yield


# ── 1. The whole label family is emoji-free ─────────────────────────────
def test_labels_are_emoji_free():
    """FAILING-FIRST: every tool-exec phase label must be plain ASCII —
    the frontend owns iconography via its SVG system; an emoji in the label
    is a second, inconsistent icon source (owner directive 2026-08-03)."""
    from lib.tasks_pkg.tool_dispatch._labels import _TOOL_EXEC_LABELS
    offenders = {k: v for k, v in _TOOL_EXEC_LABELS.items() if not v.isascii()}
    assert not offenders, (
        f'tool-exec labels must be emoji-free ASCII (the phase row renders '
        f'no icon of its own); offenders: {offenders}')


def test_apply_diff_label_says_patching():
    """FAILING-FIRST: "Applying changes" → "Patching files". The user reads
    the phase text to learn WHICH tool is running; the honest verb is the
    one the tool actually performs."""
    from lib.tasks_pkg.tool_dispatch._labels import tool_label
    assert tool_label('apply_diff') == 'Patching files'
    assert tool_label('apply_diffs') == 'Patching files'


def test_mcp_fallback_label_emoji_free():
    """FAILING-FIRST: the MCP fallback ('server/tool') must also drop the
    🔌 prefix."""
    from lib.tasks_pkg.tool_dispatch._labels import tool_label
    assert tool_label('mcp__hope__login') == 'hope/login'


def test_emit_tool_exec_phase_english_fallback_and_tools(_no_persist):
    """The tool_exec phase event keeps an English ``detail`` fallback for
    headless clients AND ships the structured ``tools`` list the i18n
    client composes its localized label from."""
    from lib.tasks_pkg.tool_dispatch._labels import emit_tool_exec_phase
    task = _mk_task()
    parsed = [({'id': 'c1', 'function': {'name': 'apply_diff', 'arguments': '{}'}},
               'apply_diff', 'c1', {}, 1, {'roundNum': 1}, None)]
    emit_tool_exec_phase(task, parsed)
    ev = task['events'][-1]
    assert ev['type'] == 'phase' and ev['phase'] == 'tool_exec'
    assert ev['tools'] == ['apply_diff']
    assert ev['detail'] == 'Patching files', (
        f'English fallback detail must be the emoji-free honest label; '
        f'got {ev["detail"]!r}')
    assert ev['detail'].isascii()


def test_round_open_phase_ships_tool_context_tools(_no_persist):
    """FAILING-FIRST: the round-open llm_thinking phase must ship the
    previous round's raw tool NAMES as ``toolContextTools`` (structured,
    localizable) alongside the English ``toolContext`` fallback."""
    import lib.tasks_pkg.orchestrator._finalize as orch
    task = _mk_task()
    am = {'tool_calls': [
        {'function': {'name': 'read_files'}},
        {'function': {'name': 'apply_diff'}},
        {'function': {'name': 'apply_diff'}},  # duplicate → deduped
    ]}
    orch._emit_tool_round_phase(task, am, 2)
    ev = task['events'][-1]
    assert ev['phase'] == 'llm_thinking'
    assert ev.get('toolContextTools') == ['read_files', 'apply_diff'], (
        f'the client needs the raw tool names to localize the suffix; '
        f'got {ev.get("toolContextTools")!r}')
    # English fallback retained for headless clients — and emoji-free.
    assert ev['toolContext'] == 'Reading files, Patching files'
    assert ev['toolContext'].isascii()


def test_poll_lane_phase_snapshot_forwards_tool_context_tools(_no_persist):
    """FAILING-FIRST: the poll-fallback phase snapshot (manager/_events.py)
    must forward ``toolContextTools`` the same way it already forwards
    ``toolContext`` / ``tools`` — a poll-lane client localizes from the
    same structured field."""
    from lib.tasks_pkg.manager import append_event
    task = _mk_task()
    append_event(task, {'type': 'phase', 'phase': 'llm_thinking',
                        'detail': 'Analyzing…', 'toolContext': 'Reading files',
                        'toolContextTools': ['read_files'], 'roundNum': 2})
    snap = task['phase']
    assert snap and snap.get('toolContextTools') == ['read_files'], (
        f'poll-lane phase snapshot dropped toolContextTools: {snap!r}')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
