#!/usr/bin/env python3
"""Paper-panel tool-round render checks for the FULL tool set (2026-07-28).

The paper report / Q&A panels replay engine events into chat-shaped rounds
and render them through the SAME ``renderToolRoundsHTML`` the chat bubble
uses (static/js/ui/tool_rounds.js). Now that the paper engines ship the full
chat-tier tool set, rounds whose toolName is read_files / code_exec /
todo_write (previously impossible in a paper panel) must render with the
same line + badge + block the chat shows — not fall over or render blank.

This harness drives those exact replay shapes through the REAL shipped
renderer under jsdom and asserts the DOM, mirroring
``test_frontend_tool_rounds_render.py`` (the chat characterization twin).
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit


_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatInner"></div></body>',
  targets: [process.argv[4], process.argv[2]],
  globals: {
    _convRenderFingerprint: () => 0,
    conversations: [],
    activeConvId: null,
  },
});

function frag(html) {
  const d = document.createElement('div');
  d.innerHTML = html;
  return d;
}

if (typeof renderToolRoundsHTML !== 'function') {
  console.log('FAIL entry_exposed renderToolRoundsHTML missing');
  report();
  return;
}
check('entry_exposed', true);

// ── 1. paper read_files round (project-meta result, as finalized by the
//       shared dispatch) renders a normal line with the file label ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, llmRound: 0, toolName: 'read_files', status: 'done',
      query: 'Read staged_asset.txt',
      results: [{ badge: '165B', fetched: true, fetchedChars: 165,
        snippet: 'staged_asset.txt  3 lines', title: 'staged_asset.txt',
        source: 'Project', url: '' }] },
  ], false);
  const d = frag(html);
  check('read_line_rendered', !!d.querySelector('.ptool-line'));
  check('read_file_label_present', html.includes('staged_asset.txt'));
  check('read_not_blank', html.length > 0);
  check('read_no_rejected_class', !d.querySelector('.ptool-rejected'));
}

// ── 2. paper code_exec round (run_command flipped to code_exec in a
//       project-less engine) renders the command block with the exit code ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 2, llmRound: 1, toolName: 'code_exec', status: 'done',
      query: '$ echo paper-full-tools-routing-ok',
      results: [{ toolName: 'code_exec',
        command: 'echo paper-full-tools-routing-ok',
        output: 'paper-full-tools-routing-ok', exitCode: '0',
        timedOut: false }] },
  ], false);
  const d = frag(html);
  // code_exec renders as an inline TERMINAL block (not a .ptool-line) —
  // chat parity: _renderCmdDoneBlock with the ok status class.
  check('exec_cmd_block_rendered', !!d.querySelector('.ptool-cmd-block'));
  check('exec_cmd_block_ok', !!d.querySelector('.ptool-cmd-ok'));
  check('exec_command_present', html.includes('echo paper-full-tools-routing-ok'));
  check('exec_output_present', html.includes('paper-full-tools-routing-ok'));
  // A successful exit must NOT render as interrupted / not-run.
  check('exec_no_interrupted', !d.querySelector('.ptool-interrupted'));
}

// ── 3. paper todo_write round renders the checklist badge, not the generic
//       "unregistered tool" fallback ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 3, llmRound: 2, toolName: 'todo_write', status: 'done',
      query: 'Planning: 2 steps (1 done)',
      results: [{ toolName: 'todo_write', title: 'Checklist · 1/2 done',
        snippet: '2 items', source: 'Checklist', badge: '1/2',
        fetched: true, fetchedChars: 40 }] },
  ], false);
  const d = frag(html);
  check('todo_line_rendered', !!d.querySelector('.ptool-line'));
  check('todo_badge_present', html.includes('1/2'));
}

// ── 4. mixed paper timeline (search + read + exec across llmRounds) keeps
//       chronological order and renders every line ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, llmRound: 0, toolName: 'web_search', status: 'done',
      query: 'first', results: [{ title: 'r1' }] },
    { roundNum: 2, llmRound: 1, toolName: 'read_files', status: 'done',
      query: 'second', results: [{ fetched: true, title: 'a.txt',
        snippet: 'a.txt', source: 'Project', badge: '1K' }] },
    { roundNum: 3, llmRound: 2, toolName: 'code_exec', status: 'done',
      query: 'third', results: [{ toolName: 'code_exec', command: 'true',
        output: '', exitCode: '0' }] },
  ], false);
  const d = frag(html);
  const slots = [...d.querySelectorAll('[data-prn]')];
  check('mixed_three_slots', slots.length === 3);
  check('mixed_chrono', slots[0].getAttribute('data-prn') === '1' &&
    slots[1].getAttribute('data-prn') === '2' &&
    slots[2].getAttribute('data-prn') === '3');
  check('mixed_search_read_lines', d.querySelectorAll('.ptool-line').length === 2);
  check('mixed_exec_cmd_block', !!d.querySelector('.ptool-cmd-block'));
}

report();
"""


def test_paper_full_tool_rounds_render():
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),
        body_js=_BODY,
        extra_targets=[os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js')],
        min_pass=15,
        # FUSE-mounted checkouts load jsdom + the 2283L renderer slowly
        # (~80s wall here, almost pure I/O wait) — the 60s default times out.
        timeout=240,
        label='paper full tool rounds render',
    )
