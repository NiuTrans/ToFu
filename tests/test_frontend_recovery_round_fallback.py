#!/usr/bin/env python3
"""tests/test_frontend_recovery_round_fallback.py — a recovery-rebuilt tool
round (no query / no results) must NEVER render as an empty card.

SYMPTOM (2026-07-26, conv ms1auj3n — JOURNAL 续49)
--------------------------------------------------
After a server restart, tool cards rendered as: round number + icon +
「模型原文」button and NOTHING else. The rounds had been rebuilt from the
persisted segments by boot crash recovery (a wire-replay view:
toolCallId/toolName/toolArgs/toolContent/status/llmRound — no ``query``,
no ``results``). ``_renderUnifiedToolLine`` interpolates ``q`` (built from
``round.query || ""``) as the whole title of the generic line, so every
such round rendered blank.

FIX (epic pt_9409bf7133c049cb ①): ``_recoveryRoundFallbackTitle`` — when
``round.query`` is absent, fall back to the typed tool label (the same
``_getToolDisplay`` adapter live rounds use) plus a short first-string-arg
summary.

HARNESS: drives the real ``_renderUnifiedToolLine`` + ``_getToolDisplay`` +
``_recoveryRoundFallbackTitle`` from the retained test view. That view loads
the typed presentation owner first, matching the Vite prelude; the generic
path only builds a string, so no DOM is needed. Every other branch renderer is
stubbed to "" so the probe order still runs and the generic line is reached.

NEUTER: rewire the fallback call to '' (the pre-fix world) → the title span
comes out empty again, proving the fallback is load-bearing.

Skips cleanly when node isn't installed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
SRC = os.path.join(ROOT, 'static', 'js', 'ui', 'tool_rounds.js')


def _node_available() -> bool:
    return shutil.which('node') is not None


def _extract_function(src: str, name: str) -> str:
    """Extract ``function NAME(...) { ... }`` by brace matching (string/comment aware)."""
    marker = f'function {name}('
    start = src.find(marker)
    assert start >= 0, f'{name} not found in tool_rounds.js'
    i = src.find('{', start)
    depth = 0
    j = i
    in_s = in_d = in_t = in_line = in_block = False
    while j < len(src):
        c = src[j]
        nxt = src[j + 1] if j + 1 < len(src) else ''
        if in_line:
            if c == '\n':
                in_line = False
        elif in_block:
            if c == '*' and nxt == '/':
                in_block = False
                j += 1
        elif in_s:
            if c == '\\':
                j += 1
            elif c == "'":
                in_s = False
        elif in_d:
            if c == '\\':
                j += 1
            elif c == '"':
                in_d = False
        elif in_t:
            if c == '\\':
                j += 1
            elif c == '`':
                in_t = False
        else:
            if c == '/' and nxt == '/':
                in_line = True
                j += 1
            elif c == '/' and nxt == '*':
                in_block = True
                j += 1
            elif c == "'":
                in_s = True
            elif c == '"':
                in_d = True
            elif c == '`':
                in_t = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return src[start:j + 1]
        j += 1
    raise AssertionError(f'unbalanced braces extracting {name}')


def _build_driver(neuter: bool, round_json: str) -> str:
    src = open(SRC, encoding='utf-8').read()
    # The materialized prefix includes the typed presentation owner plus the
    # retained display-label adapter, matching the production ESM prelude.
    display_adapter = 'const _getToolDisplay = toolRoundDisplay;'
    prefix_end = src.find(display_adapter)
    assert prefix_end > 0
    prefix = src[:prefix_end + len(display_adapter)]

    fallback_fn = _extract_function(src, '_recoveryRoundFallbackTitle')
    render_fn = _extract_function(src, '_renderUnifiedToolLine')
    if neuter:
        render_fn = render_fn.replace('_recoveryRoundFallbackTitle(round, td)', "''")
        assert "_recoveryRoundFallbackTitle(round, td)" not in render_fn

    bootstrap = """
function Icon(name) { return '<svg data-icon="' + name + '"></svg>'; }
"""
    stubs = """
function _getToolSvg() { return 'SVG'; }
function _linkifyMcpLabels(s) { return s; }
function escapeHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function t(k, d) { return d || k; }
const _EMPTY = () => '';
const _FALSE = () => false;
function _rowModelViewBtn() { return 'BTN'; }
// Command interaction state is lifecycle-owned (Sets keyed by toolCallId). The
// generic-line render only READS `_cmdBodyExpanded.has(...)`/
// `_cmdOutputExpanded.has(...)`, which the auto-stub regex (`_name(`) cannot
// see, so declare the real empty Sets here (same initial state as the shipped
// tool_rounds.js) and let `_cmdInteractionKey` fall through to its auto-stub.
const _cmdBodyExpanded = new Set();
const _cmdOutputExpanded = new Set();
"""
    # ── Auto-derived collaborator stubs ────────────────────────────────
    # The list of `_renderX(...)` helpers this driver must stub used to be
    # hand-written. That is a rot generator: a586787c added _rowRightControls
    # (5 call sites inside _renderUnifiedToolLine) and the driver died with
    # `ReferenceError: _rowRightControls is not defined` — a harness fault
    # that reads exactly like a product regression. Derive it instead: every
    # `_name(` the extracted functions call, minus whatever the prefix/stubs
    # already define, gets a no-op stub. A helper added tomorrow is covered
    # automatically.
    _extracted = fallback_fn + '\n' + render_fn
    _called = set(re.findall(r'\b(_[A-Za-z_]\w*)\s*\(', _extracted))
    _already = set(re.findall(
        r'\bfunction\s+(_[A-Za-z_]\w*)\s*\(', bootstrap + prefix + stubs))
    _already |= set(re.findall(
        r'\b(?:const|let|var)\s+(_[A-Za-z_]\w*)\s*=',
        bootstrap + prefix + stubs,
    ))
    # The two functions under test are real, never stubbed.
    _already |= {'_recoveryRoundFallbackTitle', '_renderUnifiedToolLine'}
    _missing = sorted(_called - _already)
    if _missing:
        stubs += ('var ' + ' = _EMPTY, '.join(_missing) + ' = _EMPTY;\n')

    call = f"\nconst ROUND = {round_json};\nconsole.log(_renderUnifiedToolLine(ROUND, false));\n"
    return (bootstrap + prefix + '\n' + fallback_fn + '\n' + render_fn
            + '\n' + stubs + call)


def _render(round_obj: dict, neuter: bool = False) -> str:
    driver = _build_driver(neuter, json.dumps(round_obj, ensure_ascii=False))
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
        f.write(driver)
        path = f.name
    try:
        proc = subprocess.run(['node', path], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise AssertionError(f'node driver failed: {proc.stderr[:800]}')
        return proc.stdout.strip()
    finally:
        os.unlink(path)


_QUERYLESS = {
    'toolName': 'read_files',
    'toolArgs': '{"path": "src/revealController.ts"}',
    'toolCallId': 'toolu_01X', 'status': 'done',
    'toolContent': 'file body', 'llmRound': 0,
    # NOTE: no query, no results — the recovery-rebuilt shape
}

_QUERYLESS_BRAIN = {
    'toolName': 'project_board_read',
    'toolArgs': '{}',
    'toolCallId': 'project_board_read_16', 'status': 'done',
    'toolContent': '[PROJECT BOARD] …', 'llmRound': 0,
}

_WITH_QUERY = dict(_QUERYLESS, query='src/revealController.ts')


@pytest.mark.skipif(not _node_available(), reason='node not installed')
class TestRecoveryRoundFallback:

    def test_queryless_round_renders_label_and_args_summary(self):
        """The empty-card regression: a recovery-rebuilt round (no query) must
        still render the tool label + a short args summary."""
        html = _render(_QUERYLESS)
        assert 'Project' in html, f'tool label missing from card: {html!r}'
        assert 'src/revealController.ts' in html, \
            f'args summary missing from card: {html!r}'
        assert '<span class="ptool-text"></span>' not in html, \
            f'empty title span — the blank card is back: {html!r}'

    def test_queryless_brain_round_renders_generic_label(self):
        """Tools outside _TOOL_DISPLAY fall back to the humanized tool name."""
        html = _render(_QUERYLESS_BRAIN)
        assert 'Project board read' in html, f'generic label missing: {html!r}'
        assert '<span class="ptool-text"></span>' not in html

    def test_query_present_round_unchanged(self):
        """Behaviour preservation: a round WITH query renders its query, not
        the fallback."""
        html = _render(_WITH_QUERY)
        assert 'src/revealController.ts' in html
        # The fallback would have prefixed the label; the live path does not.
        assert 'Project — ' not in html, \
            f'fallback leaked into a query-present round: {html!r}'

    def test_neuter_fallback_removed_blank_card_returns(self):
        """NEUTER: pre-fix shape (fallback rewired to '') → the title span is
        empty again. Proves the fallback is load-bearing."""
        html = _render(_QUERYLESS, neuter=True)
        assert '<span class="ptool-text"></span>' in html, \
            f'NEUTER failed: without the fallback the card is NOT blank — ' \
            f'the fallback is not load-bearing: {html!r}'
