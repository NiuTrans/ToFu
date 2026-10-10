"""Exact owner and retained-wiring contracts for command presentation."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._jsdom import JS_DIR, run_harness
from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
OWNER_JS = Path(native_module_path(
    '.native/tool-command-execution-presentation-contract.js',
    ROOT
    / 'frontend/src/conversation/presentation/'
    / 'tool-command-execution-presentation.ts',
))


_OWNER_HARNESS = r"""
eval(process.env.OWNER_SOURCE);

const checks = [];
function check(name, condition) {
  checks.push((condition ? 'PASS ' : 'FAIL ') + name);
}
function occurrences(value, fragment) {
  return String(value).split(fragment).length - 1;
}
const messages = {
  'toolCommandExecution.timeout': 'timeout-local',
  'toolCommandExecution.notRun': 'not-run-local',
  'toolCommandExecution.ok': 'ok-local',
  'toolCommandExecution.failed': 'failed-local',
  'toolCommandExecution.liveOutputElided': 'live-elided:{n}\n',
  'toolCommandExecution.argumentsLimit': 'args-limit:{n}',
  'toolCommandExecution.commandLimit': 'command-limit:{n}',
  'toolCommandExecution.descriptionLimit': 'description-limit:{n}',
  'toolCommandExecution.outputLimit': 'output-limit:{n}',
  'toolCommandExecution.qrLimit': 'qr-limit:{shown}/{total}',
  'toolCmd.finished': 'finished-local',
  'toolCmd.interruptedBadge': '⏸ interrupted-local',
  'toolCmd.foldCommand': 'fold-cmd-local',
  'toolCmd.foldOutput': 'fold-output-local',
  'project.qrScan': 'QR-local',
  'project.qrScanMulti': 'QRs-local',
};
function translate(key, params) {
  let value = messages[key] || key;
  if (!params || typeof params !== 'object') return value;
  return value.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name) => (
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name]) : token
  ));
}
const presentation = createToolCommandExecutionPresentation({ translate });
const header = Object.freeze({
  iconHtml: '<i data-slot="icon"></i>',
  rootPillHtml: '<b data-slot="root"></b>',
  timerHtml: '<time data-slot="timer"></time>',
  interruptHtml: '<button data-slot="interrupt"></button>',
  rightControlsHtml: '<span data-slot="controls"></span>',
});
const closed = Object.freeze({ bodyExpanded: false, outputExpanded: false });

const limits = TOOL_COMMAND_EXECUTION_PRESENTATION_LIMITS;
check('limits_and_public_port_are_frozen_and_narrow',
  Object.isFrozen(limits)
  && limits.serializedArgumentsUnits === 80000
  && limits.commandUnits === 65536
  && limits.descriptionUnits === 4096
  && limits.liveOutputUnits === 20000
  && limits.resultUnits === 120000
  && limits.legacyStatusTailUnits === 2048
  && limits.interactionKeyUnits === 512
  && limits.qrDescriptorsScanned === 64
  && limits.qrTiles === 16
  && limits.qrSourceUnits === 1000000
  && limits.qrCaptionUnits === 512
  && Object.isFrozen(presentation)
  && Object.keys(presentation).length === 2);
check('null_and_unrelated_rounds_fail_closed',
  presentation.renderRunningCommandHtml(null, header, closed) === ''
  && presentation.renderSettledCommandHtml(
    { toolName: 'browser_execute_js' }, {}, header, closed,
  ) === '');

for (const mode of ['running', 'settled']) {
  const round = { toolName: 'run_command', query: 'pwd', toolArgs: {working_dir: '/tmp/<unsafe> directory'} };
  const html = mode === 'running'
    ? presentation.renderRunningCommandHtml(round, header, closed)
    : presentation.renderSettledCommandHtml(round, {exitCode: '0'}, header, closed);
  check(mode + '_cwd_is_inside_command_code_and_escaped',
    html.includes('<code><span class="ptool-cmd-cwd">working_dir: /tmp/&lt;unsafe&gt; directory</span>\n$ pwd</code></pre>')
    && !html.includes('<unsafe>')
    && !html.includes('<div class="ptool-cmd-reason"><code>working_dir:'));
}
check('missing_cwd_is_not_invented', !presentation.renderRunningCommandHtml({toolName: 'run_command', query: 'pwd'}, header, closed).includes('working_dir:'));
for (const mode of ['running', 'settled']) {
  const round = {
    toolName: 'run_command', toolCallId: 'cwd-fold', query: 'echo ' + 'x'.repeat(101),
    toolArgs: { description: 'long command', working_dir: '<&' + 'a'.repeat(4098) },
  };
  const html = mode === 'running'
    ? presentation.renderRunningCommandHtml(round, header, closed)
    : presentation.renderSettledCommandHtml(round, {exitCode: 0}, header, closed);
  check(mode + '_cwd_folds_with_command_and_keeps_bound',
    html.includes('class="ptool-cmd-code ptool-cmd-collapsible"><code><span class="ptool-cmd-cwd">working_dir: &lt;&amp;')
    && html.includes('a'.repeat(4094) + '…</span>\n$ echo ')
    && !html.includes('a'.repeat(4095)));
}

const runningRound = Object.freeze({
  toolName: 'run_command', query: 'npm test', toolCallId: 'call-1',
  toolArgs: '{"description":"run tests"}', _partialOutput: 'line 1',
  grepSearchIntercepted: true,
});
const runningBefore = JSON.stringify([runningRound, header, closed]);
const runningHtml = presentation.renderRunningCommandHtml(
  runningRound, header, closed,
);
check('running_card_uses_explicit_trusted_slots_and_localized_copy',
  runningHtml.includes('ptool-cmd-running')
  && runningHtml.includes('data-slot="icon"')
  && runningHtml.includes('data-slot="root"')
  && runningHtml.includes('data-slot="timer"')
  && runningHtml.includes('data-slot="interrupt"'));
check('grep_interception_is_no_longer_surfaced',
  !runningHtml.includes('grepSearchIntercepted')
  && !runningHtml.includes('grep-local')
  && !runningHtml.includes('ptool-grep-intercept'));
check('plain_running_state_renders_no_status_label',
  !runningHtml.includes('ptool-cmd-label'));
const cancellingHtml = presentation.renderRunningCommandHtml({
  toolName: 'run_command', query: 'npm test', toolCallId: 'call-cancel',
  _partialOutputTerminalReason: 'cancelling',
}, header, closed);
check('non_running_progress_status_keeps_its_label',
  cancellingHtml.includes('data-progress-status="cancelling"')
  && cancellingHtml.includes('>Cancelling…</span>'));
check('running_command_description_and_live_output_are_projected',
  runningHtml.includes('run tests')
  && runningHtml.includes('$ npm test')
  && runningHtml.includes('ptool-cmd-output-live')
  && runningHtml.includes('line 1'));
check('owner_never_mutates_running_inputs',
  JSON.stringify([runningRound, header, closed]) === runningBefore);

const hostileHtml = presentation.renderRunningCommandHtml({
  toolName: 'code_exec', toolCallId: 'key" data-x="bad',
  query: '<cmd & unsafe>',
  toolArgs: { description: '<desc & unsafe>' },
  _partialOutput: '<output & unsafe>',
}, header, Object.freeze({ bodyExpanded: true, outputExpanded: false }));
check('every_untrusted_running_field_is_escaped',
  hostileHtml.includes('&lt;cmd &amp; unsafe&gt;')
  && hostileHtml.includes('&lt;desc &amp; unsafe&gt;')
  && hostileHtml.includes('&lt;output &amp; unsafe&gt;')
  && !hostileHtml.includes('<cmd & unsafe>'));

const longRunningHtml = presentation.renderRunningCommandHtml({
  toolName: 'run_command', toolCallId: 'long-1',
  query: 'C'.repeat(65536) + 'COMMAND_TAIL',
  toolArgs: { description: 'D'.repeat(4096) + 'DESCRIPTION_TAIL' },
  _partialOutput: 'LIVE_HEAD' + 'L'.repeat(20000) + 'LIVE_TAIL',
}, header, Object.freeze({ bodyExpanded: true, outputExpanded: false }));
check('command_and_description_have_visible_exact_bounds',
  longRunningHtml.includes('command-limit:65536')
  && longRunningHtml.includes('description-limit:4096')
  && !longRunningHtml.includes('COMMAND_TAIL')
  && !longRunningHtml.includes('DESCRIPTION_TAIL'));
check('body_interaction_snapshot_preserves_collapsible_state',
  longRunningHtml.includes('cmd-open')
  && longRunningHtml.includes('data-cmd-key="long-1"')
  && longRunningHtml.includes('ptool-cmd-fold-cmd')
  && longRunningHtml.includes('_cmdBodyToggle(this,event)')
  && longRunningHtml.includes('fold-cmd-local')
  && longRunningHtml.includes('aria-expanded="true"'));
check('running_card_without_collapsible_body_renders_no_fold_chip',
  !runningHtml.includes('ptool-cmd-fold')
  && !hostileHtml.includes('ptool-cmd-fold-output'));
check('live_output_is_a_localized_bounded_tail_window',
  longRunningHtml.includes('live-elided:18')
  && !longRunningHtml.includes('LIVE_HEAD')
  && longRunningHtml.includes('LIVE_TAIL'));
const oversizedArgumentsHtml = presentation.renderRunningCommandHtml({
  toolName: 'run_command', query: 'still visible',
  toolArgs: 'x'.repeat(80001),
}, header, closed);
check('serialized_arguments_are_rejected_before_parse',
  oversizedArgumentsHtml.includes('args-limit:80000')
  && oversizedArgumentsHtml.includes('still visible'));

const qrImages = Array.from({ length: 70 }, (_, index) => ({
  uri: `data:image/png;base64,${index}`,
  filename: `qr-${index}.png`,
}));
const qrHtml = presentation.renderRunningCommandHtml({
  toolName: 'run_command', query: 'login', qrImages,
}, header, closed);
check('qr_projection_has_scan_and_tile_bounds_with_visible_limit',
  occurrences(qrHtml, 'ptool-qr-tile') === 16
  && qrHtml.includes('qr-limit:16/70')
  && !qrHtml.includes('qr-16.png'));
const unsafeQrHtml = presentation.renderRunningCommandHtml({
  toolName: 'run_command', query: 'unsafe', qrImages: [
    { uri: 'javascript:alert(1)', filename: '<bad>' },
    { uri: '//evil.example/qr.png', filename: 'ambiguous' },
    { uri: 'data:text/html;base64,WA==', filename: 'active' },
  ],
}, header, closed);
check('qr_sources_use_the_shared_image_allowlist',
  !unsafeQrHtml.includes('ptool-qr-strip')
  && !unsafeQrHtml.includes('javascript:'));
const oversizedQrHtml = presentation.renderRunningCommandHtml({
  toolName: 'run_command', query: 'oversized qr', qrImages: [{
    uri: 'data:image/png;base64,' + 'A'.repeat(1000000),
    filename: 'large.png',
  }],
}, header, closed);
check('qr_source_text_has_an_explicit_hard_bound',
  !oversizedQrHtml.includes('ptool-qr-strip'));

const settledRound = Object.freeze({
  toolName: 'run_command', roundNum: 7, toolCallId: 'settled-1',
  query: 'fallback command', toolArgs: { description: 'fallback description' },
});
const settledMetadata = Object.freeze({
  command: 'printf ok', description: 'print', output: 'ok', exitCode: 0,
});
const settledBefore = JSON.stringify([settledRound, settledMetadata]);
const settledHtml = presentation.renderSettledCommandHtml(
  settledRound,
  settledMetadata,
  header,
  Object.freeze({ bodyExpanded: false, outputExpanded: true }),
);
check('settled_success_preserves_status_output_and_disclosure_contract',
  settledHtml.includes('ptool-cmd-ok')
  && !settledHtml.includes('finished-local')
  && settledHtml.includes('ptool-cmd-verdict')
  && settledHtml.indexOf('ptool-cmd-verdict') > settledHtml.indexOf('ptool-cmd-right')
  && settledHtml.includes('ok-local')
  && settledHtml.includes('ptool-cmd-hasoutput output-open')
  && settledHtml.includes('ptool-cmd-fold-output')
  && settledHtml.includes('_cmdOutputOpenToggle(this,event)')
  && settledHtml.includes('fold-output-local')
  && settledHtml.includes('<code>ok</code>'));
check('settled_header_is_inert_and_short_command_has_no_cmd_fold',
  !settledHtml.includes('role="button"')
  && !settledHtml.includes('ptool-cmd-fold-cmd')
  && !settledHtml.includes('ptool-cmd-desc-toggle'));
check('settled_metadata_precedes_args_and_query',
  settledHtml.includes('$ printf ok')
  && settledHtml.includes('>print</span>')
  && !settledHtml.includes('fallback command'));
check('settled_owner_preserves_trusted_controls_and_inputs',
  settledHtml.includes('data-slot="controls"')
  && JSON.stringify([settledRound, settledMetadata]) === settledBefore);

const backgroundHtml = presentation.renderSettledCommandHtml({
  toolName: 'run_command', query: 'slow upload',
}, { exitCode: 'background', backgrounded: true }, header, closed);
check('background_acceptance_is_neither_failure_nor_command_completion',
  backgroundHtml.includes('ptool-cmd-background')
  && backgroundHtml.includes('bgCommand.moved')
  && !backgroundHtml.includes('ptool-cmd-err')
  && !backgroundHtml.includes('finished-local')
  && !backgroundHtml.includes('ok-local')
  && !backgroundHtml.includes('failed-local'));
const errorHtml = presentation.renderSettledCommandHtml({
  toolName: 'run_command', query: 'bad',
}, { exitCode: 2, output: 'boom' }, header, closed);
const timeoutHtml = presentation.renderSettledCommandHtml({
  toolName: 'run_command', query: 'slow',
}, { timedOut: true, output: 'partial' }, header, closed);
const interruptedHtml = presentation.renderSettledCommandHtml({
  toolName: 'code_exec', query: 'stop',
}, { exitCode: -1, interrupted: true, output: 'partial' }, header, closed);
const notRunHtml = presentation.renderSettledCommandHtml({
  toolName: 'run_command', query: 'blocked',
}, { notRun: true, reason: '<denied>', badge: 'policy denied' }, header, closed);
check('terminal_verdicts_have_distinct_localized_states',
  errorHtml.includes('ptool-cmd-err') && errorHtml.includes('failed-local')
  && timeoutHtml.includes('ptool-cmd-timeout')
  && timeoutHtml.includes('timeout-local')
  && interruptedHtml.includes('ptool-cmd-interrupted')
  && interruptedHtml.includes('interrupted-local')
  && !interruptedHtml.includes('failed-local')
  && notRunHtml.includes('ptool-cmd-notrun')
  && notRunHtml.includes('policy denied'));
check('not_run_reason_is_visible_and_escaped',
  notRunHtml.includes('&lt;denied&gt;')
  && !notRunHtml.includes('<denied>'));

const legacyHtml = presentation.renderSettledCommandHtml({
  toolName: 'run_command', roundNum: 8, query: 'echo ok',
  toolContent: '$ echo ok\nlegacy output\n[exit code: 0]',
}, {}, header, closed);
check('legacy_content_recovers_query_output_and_exit_without_full_scan',
  legacyHtml.includes('$ echo ok')
  && legacyHtml.includes('legacy output')
  && legacyHtml.includes('ok-local')
  && !legacyHtml.includes('[exit code: 0]'));

const longOutputHtml = presentation.renderSettledCommandHtml({
  toolName: 'run_command', query: 'large',
}, { exitCode: 0, output: 'R'.repeat(120000) + 'RESULT_TAIL' }, header, closed);
check('settled_output_has_a_visible_exact_bound',
  longOutputHtml.includes('output-limit:120000')
  && !longOutputHtml.includes('RESULT_TAIL')
  && occurrences(longOutputHtml, 'R') >= 120000);

const smallHtml = presentation.renderSettledCommandHtml({toolName:'run_command',query:'echo ok'},
  {exitCode:0,output:'ok'},header,closed);
check('normal_output_has_no_limit_boilerplate', !smallHtml.includes('ptool-result-trunc'));
const serverShortened = presentation.renderSettledCommandHtml({toolName:'run_command',query:'echo ok'},
  {exitCode:0,output:'ok',outputTruncated:true},header,closed);
check('server_shortening_never_claims_a_fictitious_120000_character_bound',
  !serverShortened.includes('output-limit:120000') && serverShortened.includes('ptool-result-trunc'));

const hostileSettledHtml = presentation.renderSettledCommandHtml({
  toolName: 'run_command', roundNum: '9" data-injected="x',
  query: '<query>', toolCallId: 'id" bad="x',
}, { exitCode: 1, output: '<out>' }, header, closed);
check('settled_round_identity_command_key_and_output_are_escaped',
  hostileSettledHtml.includes('data-rn="9&quot; data-injected=&quot;x"')
  && hostileSettledHtml.includes('&lt;query&gt;')
  && hostileSettledHtml.includes('&lt;out&gt;')
  && !hostileSettledHtml.includes('<out>'));

const hostileMetadata = {};
Object.defineProperty(hostileMetadata, 'command', {
  get() { throw new Error('untrusted getter'); },
});
const getterHtml = presentation.renderSettledCommandHtml({
  toolName: 'run_command', query: 'safe fallback',
}, hostileMetadata, header, closed);
check('hostile_metadata_getters_fail_closed_to_safe_fallbacks',
  getterHtml.includes('safe fallback')
  && getterHtml.includes('ptool-cmd-block'));

const longInteractionKey = 'c'.repeat(600);
const truncatedInteractionKey = 'c'.repeat(512);
const longKeyHtml = presentation.renderSettledCommandHtml({
  toolName: 'run_command', query: 'long key command',
  toolCallId: longInteractionKey, toolArgs: { description: 'long key' },
}, { command: 'printf ' + 'x'.repeat(150), output: 'ok', exitCode: 0 },
   header, closed);
check('interaction_key_is_truncated_to_512_code_units',
  longKeyHtml.includes('data-cmd-key="' + truncatedInteractionKey + '"')
  && !longKeyHtml.includes('data-cmd-key="' + longInteractionKey + '"'));
check('collapsible_settled_command_renders_both_fold_controls_closed',
  longKeyHtml.includes('ptool-cmd-fold-cmd')
  && longKeyHtml.includes('ptool-cmd-fold-output')
  && longKeyHtml.includes('fold-cmd-local')
  && longKeyHtml.includes('fold-output-local')
  && !longKeyHtml.includes('aria-expanded="true"'));
console.log(checks.join('\n'));
"""


_WIRING_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const messages = {
  'toolCommandExecution.timeout': '超时',
  'toolCommandExecution.notRun': '未运行',
  'toolCommandExecution.ok': '成功',
  'toolCommandExecution.failed': '失败',
  'toolCommandExecution.liveOutputElided': '省略 {n}\n',
  'toolCommandExecution.argumentsLimit': '参数上限 {n}',
  'toolCommandExecution.commandLimit': '命令上限 {n}',
  'toolCommandExecution.descriptionLimit': '说明上限 {n}',
  'toolCommandExecution.outputLimit': '输出上限 {n}',
  'toolCommandExecution.qrLimit': '二维码 {shown}/{total}',
  'toolCmd.finished': '已结束',
  'toolCmd.interruptedBadge': '⏸ 已中断',
  'toolCmd.interrupt': '中断',
  'toolCmd.interruptTip': '只中断命令',
  'toolCmd.foldCommand': '命令',
  'toolCmd.foldOutput': '输出',
  'project.qrScan': '二维码',
  'project.qrScanMulti': '个二维码',
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
  html: '<!DOCTYPE html><body><div id="chatInner"></div></body>',
  targets: [process.argv[2]],
  globals: {
    t: translate,
    _featureFlags: { debug_mode: false },
    projectState: { extraRoots: [] },
  },
});

const runningHtml = _renderUnifiedToolLine({
  roundNum: 1, status: 'searching', toolName: 'run_command',
  query: 'npm test', toolCallId: 'cmd-1', _taskId: 'task-1',
  toolArgs: { description: '测试', working_dir: '/work/project' }, _partialOutput: 'line', results: [],
}, true);
check('running_owner_reaches_retained_dispatch',
  runningHtml.includes('ptool-cmd-running')
  && runningHtml.includes('npm test')
  && runningHtml.includes('测试')
  && !runningHtml.includes('ptool-cmd-label')
  && runningHtml.includes('ptool-cmd-interrupt'));

check('running_dispatch_puts_directory_inside_code_tray',
  runningHtml.includes('<code><span class="ptool-cmd-cwd">working_dir: /work/project</span>\n$ npm test</code></pre>'));
const settledRound = {
  roundNum: 2, status: 'done', toolName: 'code_exec',
  query: 'fallback', toolCallId: 'cmd-2',
  toolArgs: { working_dir: '/work/project' },
  results: [{ command: 'printf ok', output: 'ok', exitCode: 0 }],
};
const settledHtml = _renderUnifiedToolLine(settledRound, false);
check('settled_owner_reaches_retained_dispatch',
  settledHtml.includes('ptool-cmd-ok')
  && settledHtml.includes('printf ok')
  && !settledHtml.includes('已结束')
  && settledHtml.includes('ptool-cmd-verdict')
  && settledHtml.includes('成功'));

check('settled_dispatch_puts_directory_inside_code_tray',
  settledHtml.includes('<code><span class="ptool-cmd-cwd">working_dir: /work/project</span>\n$ printf ok</code></pre>'));
const host = document.createElement('div');
host.innerHTML = settledHtml;
const outputFold = host.querySelector('.ptool-cmd-fold-output');
_cmdOutputOpenToggle(outputFold, {
  stopPropagation() {},
  target: outputFold,
});
const expandedOutputHtml = _renderUnifiedToolLine(settledRound, false);
check('retained_output_state_is_supplied_as_a_boolean_snapshot',
  expandedOutputHtml.includes('output-open')
  && expandedOutputHtml.includes('aria-expanded="true"'));

const longRound = {
  roundNum: 3, status: 'done', toolName: 'run_command',
  query: 'long', results: [{ exitCode: 0, output: 'x'.repeat(120001) }],
};
const boundedHtml = _renderUnifiedToolLine(longRound, false);
check('owner_output_bound_reaches_retained_dispatch',
  boundedHtml.includes('输出上限 120000')
  && !boundedHtml.includes('x'.repeat(120001)));

const genericHtml = _renderUnifiedToolLine({
  roundNum: 4, status: 'done', toolName: 'list_dir', query: 'list',
  results: [{ badge: 'done', text: 'entry.txt' }],
}, false);
check('unrelated_tools_still_fall_through_to_generic_renderer',
  genericHtml.includes('ptool-line')
  && !genericHtml.includes('ptool-cmd-block'));
const longKeyRound = {
  roundNum: 6, status: 'done', toolName: 'run_command',
  query: 'long key command', toolCallId: 'c'.repeat(600),
  toolArgs: { description: 'long key' },
  results: [{ command: 'printf ' + 'x'.repeat(150), output: 'ok', exitCode: 0 }],
};
const longKeyHost = document.createElement('div');
longKeyHost.innerHTML = _renderUnifiedToolLine(longKeyRound, false);
const longKeyBlock = longKeyHost.querySelector('.ptool-cmd-block');
check('retained_interaction_key_matches_presenter_truncation',
  Boolean(longKeyBlock)
  && longKeyBlock.getAttribute('data-cmd-key') === 'c'.repeat(512));
_cmdBodyToggle(longKeyBlock, { stopPropagation() {}, target: longKeyBlock });
const reopenedLongKeyHtml = _renderUnifiedToolLine(longKeyRound, false);
check('long_call_id_body_state_survives_truncated_key_roundtrip',
  reopenedLongKeyHtml.includes('cmd-open'));
report();
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_tool_command_execution_presentation_owner_contract() -> None:
    process = subprocess.run(
        [shutil.which('node'), '-e', _OWNER_HARNESS],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            'OWNER_SOURCE': OWNER_JS.read_text(encoding='utf-8'),
        },
    )
    assert process.returncode == 0, process.stderr
    failures = [
        line for line in process.stdout.splitlines()
        if line.startswith('FAIL ')
    ]
    assert not failures, process.stdout
    passes = [
        line for line in process.stdout.splitlines()
        if line.startswith('PASS ')
    ]
    assert len(passes) == 37, process.stdout


def test_retained_dispatch_wires_command_execution_owner() -> None:
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),
        body_js=_WIRING_HARNESS,
        expect_pass=9,
        label='tool-command-execution retained wiring',
    )
