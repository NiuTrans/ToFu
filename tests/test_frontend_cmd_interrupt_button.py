"""Lifecycle contract for the retained per-command interrupt action.

Command/status HTML belongs to the typed command-execution presenter and is
covered by ``test_tool_command_execution_presentation.py``. This narrow jsdom
fixture keeps only the stateful boundary: task-id resolution and interrupt I/O.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, run_harness


pytestmark = pytest.mark.unit


_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const messages = {
  'toolCommandExecution.running': 'Running...',
  'toolCommandExecution.timeout': 'timeout',
  'toolCommandExecution.notRun': 'not run',
  'toolCommandExecution.exitCode': 'exit {code}',
  'toolCommandExecution.liveOutputElided': 'elided {n}\n',
  'toolCommandExecution.argumentsLimit': 'args {n}',
  'toolCommandExecution.commandLimit': 'command {n}',
  'toolCommandExecution.descriptionLimit': 'description {n}',
  'toolCommandExecution.outputLimit': 'output {n}',
  'toolCommandExecution.qrLimit': 'qr {shown}/{total}',
  'toolCmd.finished': 'finished',
  'toolCmd.grepSearchIntercepted': 'grep_search takeover',
  'toolCmd.interruptedBadge': '⏸ interrupted',
  'toolCmd.interrupt': 'Interrupt',
  'toolCmd.interrupting': 'Interrupting…',
  'toolCmd.interruptTip': 'Stop this command only — the task continues with the partial output',
  'toolCmd.interruptNone': 'Nothing to interrupt — the command already finished',
  'project.qrScan': 'Scannable QR code',
  'project.qrScanMulti': 'scannable QR codes',
};
function translate(key, paramsOrFallback) {
  if (typeof paramsOrFallback === 'string') {
    return messages[key] || paramsOrFallback;
  }
  let value = messages[key] || key;
  if (!paramsOrFallback) return value;
  return value.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name) => (
    Object.prototype.hasOwnProperty.call(paramsOrFallback, name)
      ? String(paramsOrFallback[name]) : token
  ));
}
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [process.argv[2]],
  globals: {
    t: translate,
    _featureFlags: { debug_mode: false },
    projectState: { extraRoots: [] },
  },
});

const runningHtml = _renderUnifiedToolLine({
  status: 'searching', toolName: 'run_command', query: 'find .',
  roundNum: 3, tStart: Date.now() - 60000, _taskId: 'task-abc123',
}, true);
check('running_command_has_interrupt_button',
  runningHtml.includes('ptool-cmd-interrupt'));
check('button_carries_escaped_resolvable_task_id',
  runningHtml.includes('data-cmd-task="task-abc123"'));
check('button_uses_named_restricted_action',
  runningHtml.includes('_cmdInterruptClick(this,event)'));
check('button_has_localized_label_and_tip',
  runningHtml.includes('>Interrupt</button>')
  && runningHtml.includes('task continues with the partial output'));

const codeExecHtml = _renderUnifiedToolLine({
  status: 'searching', toolName: 'code_exec', query: 'sleep 30',
  roundNum: 4, _taskId: 'task-code',
}, true);
check('code_exec_uses_the_same_interrupt_authority',
  codeExecHtml.includes('data-cmd-task="task-code"'));

const orphanHtml = _renderUnifiedToolLine({
  status: 'searching', toolName: 'run_command', query: 'sleep 30',
  roundNum: 5,
}, true);
check('unresolvable_task_id_has_no_interrupt_button',
  !orphanHtml.includes('ptool-cmd-interrupt'));

(async () => {
  let stopped = 0;
  const event = { stopPropagation() { stopped += 1; } };
  const buttonOk = {
    disabled: false,
    textContent: '',
    getAttribute: () => 'task-abc123',
  };
  let postedTaskId = '';
  global.Api = { chat: { interruptCommand: async (taskId) => {
    postedTaskId = taskId;
    return { interrupted: true, pid: 7 };
  } } };
  await _cmdInterruptClick(buttonOk, event);
  check('success_posts_exact_task_id_and_stops_bubbling',
    postedTaskId === 'task-abc123' && stopped === 1);
  check('success_stays_disabled_until_authoritative_sse_settlement',
    buttonOk.disabled === true && buttonOk.textContent === 'Interrupting…');

  const buttonRefused = {
    disabled: false,
    textContent: '',
    getAttribute: () => 'task-abc123',
  };
  global.Api = { chat: { interruptCommand: async () => ({
    interrupted: false,
    reason: 'no_active_command',
  }) } };
  let toasted = '';
  global.showToast = (message) => { toasted = message; };
  await _cmdInterruptClick(buttonRefused, { stopPropagation() {} });
  check('refusal_restores_button_and_surfaces_reason',
    buttonRefused.disabled === false
    && buttonRefused.textContent === 'Interrupt'
    && toasted === 'Nothing to interrupt — the command already finished');

  const buttonNetwork = {
    disabled: false,
    textContent: '',
    getAttribute: () => 'task-abc123',
  };
  global.Api = { chat: { interruptCommand: async () => null } };
  await _cmdInterruptClick(buttonNetwork, { stopPropagation() {} });
  check('network_failure_restores_button_without_throwing',
    buttonNetwork.disabled === false
    && buttonNetwork.textContent === 'Interrupt');

  const missingTaskButton = {
    disabled: false,
    textContent: '',
    getAttribute: () => '',
  };
  await _cmdInterruptClick(missingTaskButton, { stopPropagation() {} });
  check('missing_task_id_performs_no_io_or_state_change',
    missingTaskButton.disabled === false
    && missingTaskButton.textContent === '');
  report();
})();
"""


def test_cmd_interrupt_lifecycle_contract() -> None:
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),
        body_js=_HARNESS,
        expect_pass=11,
        label='command interrupt lifecycle',
    )
