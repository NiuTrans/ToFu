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
  'toolCommandExecution.ok': 'ok',
  'toolCommandExecution.failed': 'failed',
  'toolCommandExecution.liveOutputElided': 'elided {n}\n',
  'toolCommandExecution.argumentsLimit': 'args {n}',
  'toolCommandExecution.commandLimit': 'command {n}',
  'toolCommandExecution.descriptionLimit': 'description {n}',
  'toolCommandExecution.outputLimit': 'output {n}',
  'toolCommandExecution.qrLimit': 'qr {shown}/{total}',
  'toolCmd.finished': 'finished',
  'toolCmd.interruptedBadge': '⏸ interrupted',
  'toolCmd.interrupt': 'Interrupt',
  'toolCmd.interrupting': 'Interrupting…',
  'toolCmd.interruptTip': 'Stop this command only — the task continues with the partial output',
  'toolCmd.interruptNone': 'Nothing to interrupt — the command already finished',

  'toolCmd.background': 'Move to background',
  'toolCmd.backgrounding': 'Moving…',
  'toolCmd.backgroundTip': 'Keep this command running in the background',
  'toolCmd.backgroundUnavailable': 'Command unavailable',
  'toolCmd.backgroundStdin': 'Answer stdin first',
  'toolCmd.backgroundCapacity': 'Background capacity full',
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
  roundNum: 3, toolCallId: 'call-3', tStart: Date.now() - 60000, _taskId: 'task-abc123',
}, true);
check('running_command_has_interrupt_button',
  runningHtml.includes('ptool-cmd-interrupt'));

check('running_command_has_background_and_interrupt_controls',
  runningHtml.includes('ptool-cmd-handoff')
  && runningHtml.indexOf('ptool-cmd-handoff') < runningHtml.indexOf('ptool-cmd-interrupt'));
check('background_button_targets_this_round_and_call_only',
  runningHtml.includes('data-cmd-round="3"')
  && runningHtml.includes('data-cmd-call="call-3"')
  && runningHtml.includes('_cmdBackgroundClick(this,event)'));
check('button_carries_escaped_resolvable_task_id',
  runningHtml.includes('data-cmd-task="task-abc123"'));
check('button_uses_named_restricted_action',
  runningHtml.includes('_cmdInterruptClick(this,event)'));
check('button_has_localized_label_and_tip',
  runningHtml.includes('>Interrupt</button>')
  && runningHtml.includes('task continues with the partial output'));

const codeExecHtml = _renderUnifiedToolLine({
  status: 'searching', toolName: 'code_exec', query: 'sleep 30',
  roundNum: 4, toolCallId: 'code-call', _taskId: 'task-code',
}, true);
check('code_exec_uses_the_same_interrupt_authority',
  codeExecHtml.includes('data-cmd-task="task-code"'));

check('code_exec_also_gets_background_action',
  codeExecHtml.includes('ptool-cmd-handoff'));
const orphanHtml = _renderUnifiedToolLine({
  status: 'searching', toolName: 'run_command', query: 'sleep 30',
  roundNum: 5,
}, true);
check('unresolvable_task_id_has_no_interrupt_button',
  !orphanHtml.includes('ptool-cmd-interrupt'));

check('unresolvable_task_or_call_has_no_background_action',
  !orphanHtml.includes('ptool-cmd-handoff')
  && !_renderUnifiedToolLine({status:'searching',toolName:'run_command',
    roundNum:5,_taskId:'task',query:'sleep 30'},true).includes('ptool-cmd-handoff'));
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

  const backgroundOk = {
    disabled: false, textContent: '',
    getAttribute(name) {
      return {'data-cmd-task':'task-abc123','data-cmd-round':'3',
        'data-cmd-call':'call-3'}[name] || '';
    },
  };
  let backgroundRequest = [];
  global.Api = {chat: {backgroundCommand: async (...args) => {
    backgroundRequest = args;
    return {backgrounded:true};
  }}};
  await _cmdBackgroundClick(backgroundOk, {stopPropagation() {stopped += 1;}});
  check('background_action_posts_exact_command_identity',
    backgroundRequest.join(':') === 'task-abc123:3:call-3' && stopped === 2);
  check('background_success_waits_for_authoritative_settlement',
    backgroundOk.disabled === true && backgroundOk.textContent === 'Moving…');
  const backgroundRefused = {...backgroundOk, disabled:false, textContent:''};
  global.Api = {chat: {backgroundCommand: async () => ({
    backgrounded:false,reason:'capacity'})}};
  await _cmdBackgroundClick(backgroundRefused, {stopPropagation() {}});
  check('background_refusal_recovers_button_with_reason',
    backgroundRefused.disabled === false
    && backgroundRefused.textContent === 'Move to background'
    && toasted === 'Background capacity full');

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
        expect_pass=18,
        label='command interrupt lifecycle',
    )
