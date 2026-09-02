"""Guard: heavy sync helpers never run on the event loop in route handlers.

WHY
---
2026-08-01: LoopWatch recorded 19 event-loop STALLs (~5s each) in one day.
The confirmed culprit class: Quart ``async def`` handlers calling heavy
SYNCHRONOUS work directly on the loop thread — sync handlers get Quart's
thread pool for free, async handlers do NOT. Measured evidence:

  * ``get_conv_count`` (routes/api_v1/daily_report.py) called
    ``_count_convs_for_date`` on-loop: a full-day conversations scan that
    fetches + ``json.loads``-es EVERY in-window conversation's messages
    blob (~300 MB for 2026-08-01) just to count. One stall dump caught
    ``daily_report/conversations.py:244`` in the act.
  * The full-routes/ AST audit the same day found three more of the same
    class: ``list_branches`` / ``create_branch`` parsed the full messages
    blob on-loop, and ``get_compaction`` parsed multi-MB archive payloads
    on-loop.

All were moved to ``await asyncio.to_thread(...)`` (the codebase's
established idiom — see ``_get_monthly_costs`` in daily_report.py).
This suite is the RATCHET so the next session cannot silently regress to
a bare on-loop call, in two parts:

1. AST rule — in ``routes/api_v1/daily_report.py``, every reference to a
   guarded heavy helper inside an ``async def`` must be the first
   positional argument of an ``asyncio.to_thread(...)`` call. A bare
   call anywhere else in an async handler convicts.
2. Token pins — the sibling-file offloads (branches / compaction viewer)
   are pinned by a source token + minimum occurrence count, in the
   spirit of ``_GRANDFATHERED`` in test_agent_loop_adoption_guard.py.

NEUTER evidence (manual, run before commit):
  * un-wrapping one ``asyncio.to_thread(_count_convs_for_date, ...)``
    back to a direct call turns test 1 red naming the line;
  * deleting the ``_branch_persist_payload`` offload turns test 2 red.
"""

from __future__ import annotations

import ast
import os
import unittest

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))

_DAILY_REPORT = 'routes/api_v1/daily_report.py'

# Helpers whose work scales with the whole messages table / a whole
# conversation blob. Inside an async def of _DAILY_REPORT they may ONLY be
# referenced as the fn argument of asyncio.to_thread(...).
_GUARDED_HELPERS = frozenset({
    '_count_convs_for_date',
    '_extract_convs_for_date',
    '_analyse_conversations',
    '_activity_counts_for_range',
    '_get_monthly_costs',
})

# File → (source token, min occurrences). Pins the offloads that live
# OUTSIDE daily_report.py so deleting one (back to an on-loop parse)
# turns this suite red.
_OFFLOAD_TOKENS = {
    'routes/conversations_compaction.py': [
        ('get_conversation_store().get_compaction_archive,', 1),
    ],
}

# Anti-empty-scan floor: daily_report.py must contain at least this many
# to_thread-wrapped references to guarded helpers (today: 9). A broken
# AST scan or a wholesale revert trips this even if no bare call exists.
_MIN_WRAPPED_REFS = 6


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


def _to_thread_spans(tree):
    """Line spans of every asyncio.to_thread(...) call in the module."""
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            label = ''
            if isinstance(func, ast.Attribute):
                base = func.value.id if isinstance(func.value, ast.Name) else ''
                label = f'{base}.{func.attr}'
            elif isinstance(func, ast.Name):
                label = func.id
            if label in ('asyncio.to_thread', 'to_thread'):
                spans.append((node.lineno, node.end_lineno or node.lineno,
                              node.args[0] if node.args else None))
    return spans


def _scan_daily_report(src):
    """Return (violations, wrapped_count) for the AST rule.

    violations: [lineno, ...] — a guarded helper referenced inside an
    async def OUTSIDE a to_thread first-arg position.
    wrapped_count: guarded helpers referenced as to_thread's first arg.
    """
    tree = ast.parse(src, filename=_DAILY_REPORT)
    violations = []
    wrapped = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        spans = _to_thread_spans(node)
        first_arg_lines = set()
        for a, b, first in spans:
            if isinstance(first, ast.Name) and first.id in _GUARDED_HELPERS:
                first_arg_lines.add(first.lineno)
                wrapped += 1
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id in _GUARDED_HELPERS:
                if sub.lineno in first_arg_lines:
                    continue
                in_span = any(a <= sub.lineno <= b for a, b, _ in spans)
                if not in_span:
                    violations.append(sub.lineno)
    return violations, wrapped


class TestLoopBlockingRoutesGuard(unittest.TestCase):

    def test_daily_report_helpers_always_offloaded(self):
        violations, _ = _scan_daily_report(_read(_DAILY_REPORT))
        self.assertEqual(
            violations, [],
            'guarded heavy helper(s) referenced on-loop (not as the fn '
            'arg of asyncio.to_thread) at '
            + ', '.join(f'{_DAILY_REPORT}:{n}' for n in violations)
            + ' — Quart async handlers get NO thread pool; a bare call '
            'here stalls the event loop (LoopWatch stalls of 2026-08-01)')

    def test_scan_actually_sees_the_offloads(self):
        _, wrapped = _scan_daily_report(_read(_DAILY_REPORT))
        self.assertGreaterEqual(
            wrapped, _MIN_WRAPPED_REFS,
            f'only {wrapped} to_thread-wrapped helper refs found in '
            f'{_DAILY_REPORT} (< {_MIN_WRAPPED_REFS}) — the offloads were '
            'removed wholesale, or the AST scan sees nothing (drift)')

    def test_sibling_offload_tokens(self):
        stale = []
        for rel, pins in _OFFLOAD_TOKENS.items():
            src = _read(rel)
            for token, min_count in pins:
                found = src.count(token)
                if found < min_count:
                    stale.append(
                        f'{rel}: offload token {token!r} found {found}× '
                        f'(< {min_count}) — an off-loop offload was '
                        'regressed to on-loop (or refactored: update the '
                        'pin deliberately)')
        self.assertEqual(stale, [], '\n'.join(stale))


if __name__ == '__main__':
    unittest.main(verbosity=2)
