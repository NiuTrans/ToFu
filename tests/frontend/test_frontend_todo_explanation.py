"""Todo rationale is readable without expanding the checklist, on both paths."""
import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit


_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  targets: [process.argv[4], process.argv[2], process.argv[5]],
  globals: { _convRenderFingerprint: () => 0, conversations: [], activeConvId: null },
});
const explanation = 'Why <img src=x onerror=alert(1)> & now\nKeep this line.';
const make = (n, meta) => ({ roundNum: n, toolName: 'todo_write', status: 'done',
  toolArgs: { explanation: 'Unapplied argument must never replace accepted metadata' },
  toolCallId: `explanation-${n}`, results: [{
    todos: [{ content: 'Work', status: 'pending' }], ...meta,
  }] });
for (const [label, render] of [
  ['classic', rounds => renderToolRoundsHTML(rounds, false)],
  ['turn_native_slot', rounds => rounds.map(r => _renderToolSlot(r, rounds)).join('')],
]) {
  const rounds = [
    make(1, { todoExplanation: `  ${explanation}  ` }),
    make(2, { todoExplanation: explanation, todos: [] }),
    make(3, { todoExplanation: explanation, todoNoop: true }),
    make(4, { todoExplanation: explanation, todoRejected: true, todoRejectReason: 'Refused' }),
    make(5, {}),
    make(6, { todoExplanation: ' \n ' }),
    make(7, { todoExplanation: { text: 'Not a string' } }),
    make(8, { todoExplanation: '', todoRejected: true }),
    make(9, { todoExplanation: '', todoNoop: true }),
  ];
  const before = JSON.stringify(rounds);
  const container = document.createElement('div');
  container.innerHTML = render(rounds);
  const cards = [...container.querySelectorAll('.ptool-todo-block')];
  check(label + '_explanation_once_in_body_not_horizontal_header',
    cards.length === rounds.length && cards.slice(0, 4).every(card =>
      !card.open && card.querySelectorAll('.ptool-todo-explanation').length === 1
      && card.querySelector('.ptool-todo-body .ptool-todo-explanation')?.textContent === explanation
      && !card.querySelector('summary .ptool-todo-explanation')));
  check(label + '_missing_or_invalid_explanation_adds_no_row',
    cards.slice(4).every(card => !card.querySelector('.ptool-todo-explanation')));
  check(label + '_explanation_is_safe_and_preserves_audit_input',
    !container.querySelector('img, script') && JSON.stringify(rounds) === before);
}
report();
"""


def test_todo_explanation_is_accessible_in_card_body():
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),
        body_js=_BODY,
        extra_targets=[
            os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js'),
            os.path.join(JS_DIR, 'ui', 'tool_rounds_rich.js'),
        ],
        expect_pass=6,
        label='todo explanation visibility',
    )
