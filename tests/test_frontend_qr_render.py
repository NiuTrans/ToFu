"""Public renderer contract for bounded, scannable command QR images.

The typed command owner carries exact projection/security tests. This jsdom
fixture pins only user-visible placement through the retained dispatcher: a QR
must be visible outside collapsed terminal output while the command is live or
settled.
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
  'toolCommandExecution.qrLimit': 'Showing {shown}/{total}',
  'toolCmd.finished': 'finished',
  'toolCmd.grepSearchIntercepted': 'grep_search takeover',
  'toolCmd.interruptedBadge': '⏸ interrupted',
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

const firstUri = 'data:image/png;base64,QQQQ';
const secondUri = 'data:image/png;base64,WWWW';
const runningHtml = _renderUnifiedToolLine({
  roundNum: 1, status: 'searching', toolName: 'run_command',
  query: 'gh auth login', _partialOutput: 'Waiting for scan...',
  qrImages: [{ uri: firstUri, filename: 'qr.png' }], results: [],
}, true);
check('running_command_renders_scannable_qr',
  runningHtml.includes('ptool-cmd-running')
  && runningHtml.includes('ptool-qr-strip')
  && runningHtml.includes(`src="${firstUri}"`));
check('single_qr_label_is_localized_prose',
  runningHtml.includes('Scannable QR code')
  && !runningHtml.includes('project.qrScan'));
check('running_qr_precedes_and_stays_outside_live_output',
  runningHtml.indexOf('ptool-qr-strip')
    < runningHtml.indexOf('ptool-cmd-output-live')
  && !runningHtml.slice(runningHtml.indexOf('ptool-cmd-output-live'))
    .includes('ptool-qr'));

const earlyQrHtml = _renderUnifiedToolLine({
  roundNum: 2, status: 'searching', toolName: 'run_command',
  query: 'login', _partialOutput: '',
  qrImages: [{ uri: firstUri, filename: 'qr.png' }], results: [],
}, true);
check('qr_does_not_depend_on_live_output_existing',
  earlyQrHtml.includes('ptool-qr-strip')
  && !earlyQrHtml.includes('ptool-cmd-output-live'));

const settledHtml = _renderUnifiedToolLine({
  roundNum: 3, status: 'done', toolName: 'run_command', query: 'login',
  results: [{
    command: 'login', output: 'scan', exitCode: 1,
    qrImages: [
      { uri: firstUri, filename: 'qr.png' },
      { uri: secondUri, filename: 'qr-2.png' },
    ],
  }],
}, false);
check('settled_failure_still_renders_all_qr_images',
  settledHtml.includes('ptool-cmd-err')
  && settledHtml.split('ptool-qr-tile').length - 1 === 2);
check('multi_qr_label_is_localized_prose',
  settledHtml.includes('2 scannable QR codes')
  && !settledHtml.includes('project.qrScanMulti'));
check('settled_qr_precedes_collapsed_output',
  settledHtml.indexOf('ptool-qr-strip')
    < settledHtml.indexOf('ptool-cmd-output-wrap')
  && !settledHtml.slice(settledHtml.indexOf('ptool-cmd-output-wrap'))
    .includes('ptool-qr'));

const absentHtml = _renderUnifiedToolLine({
  roundNum: 4, status: 'done', toolName: 'run_command', query: 'plain',
  results: [{ command: 'plain', output: 'ok', exitCode: 0 }],
}, false);
check('absent_qr_does_not_perturb_command_block',
  absentHtml.includes('ptool-cmd-block')
  && !absentHtml.includes('ptool-qr'));

const strippedHtml = _renderUnifiedToolLine({
  roundNum: 5, status: 'done', toolName: 'run_command', query: 'cached',
  results: [{
    command: 'cached', output: 'ok', exitCode: 0,
    qrImages: [{ filename: 'qr.png' }],
  }],
}, false);
check('descriptor_without_uri_degrades_without_broken_image',
  !strippedHtml.includes('ptool-qr') && !strippedHtml.includes('src=""'));

const unsafeHtml = _renderUnifiedToolLine({
  roundNum: 6, status: 'done', toolName: 'run_command', query: 'unsafe',
  results: [{
    command: 'unsafe', output: 'x', exitCode: 0,
    qrImages: [{ uri: 'data:image/png;base64,A"><script>x</script>' }],
  }],
}, false);
check('malformed_data_uri_cannot_inject_markup',
  !unsafeHtml.includes('<script>') && !unsafeHtml.includes('ptool-qr-strip'));
report();
"""


def test_command_qr_public_renderer_contract() -> None:
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),
        body_js=_HARNESS,
        expect_pass=10,
        label='command QR public renderer',
    )
