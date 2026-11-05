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
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))

_DAILY_REPORT = 'routes/api_v1/daily_report.py'

# Helpers whose work scales with the whole messages table / a whole
# conversation blob, plus the report-file helpers (locked JSON read /
# read-modify-write on a network filesystem — the 2026-09-06 status-poll
# stall caught by LoopWatch at 5.5s mid-request). Inside an async def of
# _DAILY_REPORT they may ONLY be referenced as the fn argument of
# asyncio.to_thread(...).
_GUARDED_HELPERS = frozenset({
    '_count_convs_for_date',
    '_extract_convs_for_date',
    '_analyse_conversations',
    '_activity_counts_for_range',
    '_get_monthly_costs',
    '_load_report',
    '_get_today_inherited_todos',
    '_update_report',
    '_save_generated_report',
})

# File → (source token, min occurrences). Pins the offloads that live
# OUTSIDE daily_report.py so deleting one (back to an on-loop parse)
# turns this suite red.
_OFFLOAD_TOKENS = {
    'routes/conversations_compaction.py': [
        ('get_conversation_store().get_compaction_archive,', 1),
    ],
}

# Request-route minting reads the routing document from storage, opens
# secret envelopes on disk, resolves DNS for egress checks and takes the
# dispatcher lock. 2026-11-02: OpenAI-compatible gateway traffic ran it
# on-loop in compat_openai.chat_completions; py-spy caught the loop thread
# there in 30/40 samples, /api/health hung >10s and the browser showed the
# "server unreachable" banner every few seconds. Inside any async def under
# routes/ these may ONLY be referenced within asyncio.to_thread(...).
_ROUTE_MINT_HELPERS = frozenset({
    'mint_compatible_request_route',
    'mint_native_request_route',
})

# Anti-empty-scan floor: daily_report.py must contain at least this many
# to_thread-wrapped references to guarded helpers (today: 25). A broken
# AST scan or a wholesale revert trips this even if no bare call exists.
_MIN_WRAPPED_REFS = 20


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


def _guarded_helper_name(node):
    """Return the guarded helper referenced by a Name/Attribute node.

    The helpers migrated behind the lazy ``_daily_report`` seam, so the
    route references them as ``_daily_report.<helper>`` (an Attribute) rather
    than as a bare Name. Both shapes pin the same offload.
    """
    if isinstance(node, ast.Name):
        return node.id if node.id in _GUARDED_HELPERS else ''
    if isinstance(node, ast.Attribute):
        return node.attr if node.attr in _GUARDED_HELPERS else ''
    return ''


def _scan_async_refs(src, filename, guarded):
    """Return (violations, wrapped_count) for names in ``guarded``.

    A reference inside an async def is accepted only when it lies within an
    asyncio.to_thread(...) call span.
    """
    tree = ast.parse(src, filename=filename)
    violations = []
    wrapped = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        spans = _to_thread_spans(node)
        for sub in ast.walk(node):
            name = (sub.id if isinstance(sub, ast.Name)
                    else sub.attr if isinstance(sub, ast.Attribute) else '')
            if name not in guarded:
                continue
            if any(a <= sub.lineno <= b for a, b, _ in spans):
                wrapped += 1
            else:
                violations.append(sub.lineno)
    return violations, wrapped


def _route_sources():
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, 'routes')):
        for name in sorted(files):
            if name.endswith('.py'):
                rel = os.path.relpath(os.path.join(dirpath, name), ROOT)
                yield rel, _read(rel)


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
            if _guarded_helper_name(first):
                first_arg_lines.add(first.lineno)
                wrapped += 1
        for sub in ast.walk(node):
            if not _guarded_helper_name(sub):
                continue
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

    def test_route_minting_always_offloaded(self):
        violations = []
        wrapped_total = 0
        for rel, src in _route_sources():
            found, wrapped = _scan_async_refs(src, rel, _ROUTE_MINT_HELPERS)
            violations.extend(f'{rel}:{n}' for n in found)
            wrapped_total += wrapped
        self.assertEqual(
            violations, [],
            'request-route minting referenced on-loop (outside '
            'asyncio.to_thread) at ' + ', '.join(violations)
            + ' — it does storage, secret-file and DNS I/O and stalls the '
            'event loop, freezing /api/health and the push socket')
        self.assertGreater(
            wrapped_total, 0,
            'no offloaded route-mint references under routes/ — the scan '
            'sees nothing (drift)')

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
