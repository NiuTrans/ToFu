"""Exact owner and retained-wiring contracts for browser JS presentation."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._jsdom import JS_DIR, run_harness
from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
OWNER_JS = Path(native_module_path(
    '.native/tool-browser-execution-presentation-contract.js',
    ROOT
    / 'frontend/src/conversation/presentation/'
    / 'tool-browser-execution-presentation.ts',
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
  'toolCmd.showResult': 'Show result-local',
  'toolBrowserExecution.executeJs': 'Execute JS-local',
  'toolBrowserExecution.ok': 'ok-local',
  'toolBrowserExecution.error': 'error-local',
  'toolBrowserExecution.argumentsLimit': 'args-limit:{n}',
  'toolBrowserExecution.codeLimit': 'code-limit:{n}',
  'toolBrowserExecution.descriptionLimit': 'description-limit:{n}',
  'toolBrowserExecution.resultLimit': 'result-limit:{n}',
};
function translate(key, params) {
  let value = messages[key] || key;
  if (!params || typeof params !== 'object') return value;
  return value.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name) => (
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name]) : token
  ));
}
const presentation = createToolBrowserExecutionPresentation({ translate });
const header = Object.freeze({
  iconHtml: '<i data-slot="icon"></i>',
  rootPillHtml: '<b data-slot="root"></b>',
  rightControlsHtml: '<span data-slot="controls"></span>',
});

check('limits_and_public_port_are_frozen_and_narrow',
  Object.isFrozen(TOOL_BROWSER_EXECUTION_PRESENTATION_LIMITS)
  && TOOL_BROWSER_EXECUTION_PRESENTATION_LIMITS.serializedArgumentsUnits === 80000
  && TOOL_BROWSER_EXECUTION_PRESENTATION_LIMITS.codeUnits === 65536
  && TOOL_BROWSER_EXECUTION_PRESENTATION_LIMITS.descriptionUnits === 4096
  && TOOL_BROWSER_EXECUTION_PRESENTATION_LIMITS.resultUnits === 120000
  && Object.isFrozen(presentation)
  && Object.keys(presentation).length === 1
  && typeof presentation.renderBrowserExecutionHtml === 'function');
check('null_and_unrelated_rounds_fail_closed',
  presentation.renderBrowserExecutionHtml(null, null, header) === ''
  && presentation.renderBrowserExecutionHtml(
    { toolName: 'browser_read_page' }, {}, header,
  ) === '');

const normalRound = Object.freeze({
  toolName: 'browser_execute_js', roundNum: 3, query: 'Execute math',
  toolArgs: '{"code":"1 + 1","description":"math"}',
  toolContent: '2',
});
const normalMetadata = Object.freeze({ badge: 'done' });
const normalBefore = JSON.stringify([normalRound, normalMetadata, header]);
const normalHtml = presentation.renderBrowserExecutionHtml(
  normalRound, normalMetadata, header,
);
check('normal_card_uses_explicit_trusted_header_slots',
  normalHtml.includes('ptool-cmd-block ptool-cmd-js ptool-cmd-ok')
  && normalHtml.includes('<i data-slot="icon"></i>')
  && normalHtml.includes('<b data-slot="root"></b>')
  && normalHtml.includes('data-slot="controls"'));
check('serialized_arguments_project_description_and_code',
  normalHtml.includes('<div class="ptool-cmd-desc">math</div>')
  && normalHtml.includes('<pre class="ptool-cmd-code"><code>1 + 1</code></pre>'));
check('result_toggle_action_and_trusted_chevron_are_exact',
  normalHtml.includes("_cmdOutputToggle(this,event,'result')")
  && normalHtml.includes('Show result-local')
  && normalHtml.includes('m6.5 5 3 3-3 3')
  && normalHtml.includes('<pre class="ptool-cmd-output"><code>2</code></pre>'));
check('success_status_is_localized',
  normalHtml.includes('ptool-cmd-status">ok-local</span>'));
const objectArgumentsHtml = presentation.renderBrowserExecutionHtml(
  {
    toolName: 'browser_execute_js', query: 'object',
    toolArgs: { code: 'document.title', description: 'read title' },
  },
  {},
  header,
);
check('object_arguments_follow_the_same_projection',
  objectArgumentsHtml.includes('document.title')
  && objectArgumentsHtml.includes('read title'));
const errorHtml = presentation.renderBrowserExecutionHtml(
  { toolName: 'browser_execute_js', query: 'bad', toolContent: 'failed' },
  { badge: 'error' },
  header,
);
check('error_metadata_selects_distinct_localized_status',
  errorHtml.includes('ptool-cmd-js ptool-cmd-err')
  && errorHtml.includes('ptool-cmd-status">error-local</span>'));

const hostileRound = Object.freeze({
  toolName: 'browser_execute_js',
  roundNum: '9" data-injected="<x>',
  query: '<query & unsafe>',
  toolArgs: Object.freeze({
    code: 'if (a < b) "x";', description: '<desc & unsafe>',
  }),
  toolContent: '<out & unsafe>',
});
const hostileBefore = JSON.stringify(hostileRound);
const hostileHtml = presentation.renderBrowserExecutionHtml(
  hostileRound, Object.freeze({}), header,
);
check('all_untrusted_fields_and_round_identity_are_escaped',
  hostileHtml.includes('data-rn="9&quot; data-injected=&quot;&lt;x&gt;"')
  && hostileHtml.includes('&lt;query &amp; unsafe&gt;')
  && hostileHtml.includes('if (a &lt; b) &quot;x&quot;;')
  && hostileHtml.includes('&lt;desc &amp; unsafe&gt;')
  && hostileHtml.includes('&lt;out &amp; unsafe&gt;')
  && !hostileHtml.includes('<query & unsafe>'));
check('presentation_never_mutates_projected_inputs',
  JSON.stringify(hostileRound) === hostileBefore
  && JSON.stringify([normalRound, normalMetadata, header]) === normalBefore);
const malformedHtml = presentation.renderBrowserExecutionHtml(
  {
    toolName: 'browser_execute_js', query: 'malformed',
    toolArgs: '{not-json', toolContent: 'result survives',
  },
  {},
  header,
);
check('malformed_arguments_fail_closed_without_hiding_result',
  !malformedHtml.includes('ptool-cmd-code')
  && !malformedHtml.includes('ptool-cmd-desc')
  && malformedHtml.includes('result survives'));
const oversizedArgumentsHtml = presentation.renderBrowserExecutionHtml(
  {
    toolName: 'browser_execute_js', query: 'large args',
    toolArgs: 'x'.repeat(80001),
  },
  {},
  header,
);
check('serialized_arguments_have_a_pre_parse_hard_bound',
  oversizedArgumentsHtml.includes('args-limit:80000')
  && !oversizedArgumentsHtml.includes('ptool-cmd-code'));

const longCodeHtml = presentation.renderBrowserExecutionHtml(
  {
    toolName: 'browser_execute_js', query: 'long code',
    toolArgs: { code: 'C'.repeat(65536) + 'CODE_TAIL' },
  },
  {},
  header,
);
check('code_display_has_a_visible_exact_bound',
  longCodeHtml.includes('code-limit:65536')
  && !longCodeHtml.includes('CODE_TAIL')
  && occurrences(longCodeHtml, 'C') >= 65536);
const longDescriptionHtml = presentation.renderBrowserExecutionHtml(
  {
    toolName: 'browser_execute_js', query: 'long description',
    toolArgs: { description: 'D'.repeat(4096) + 'DESCRIPTION_TAIL' },
  },
  {},
  header,
);
check('description_display_has_a_visible_exact_bound',
  longDescriptionHtml.includes('description-limit:4096')
  && !longDescriptionHtml.includes('DESCRIPTION_TAIL')
  && occurrences(longDescriptionHtml, 'D') >= 4096);
const longResultHtml = presentation.renderBrowserExecutionHtml(
  {
    toolName: 'browser_execute_js', query: 'long result',
    toolContent: 'R'.repeat(120000) + 'RESULT_TAIL',
  },
  {},
  header,
);
check('result_display_has_a_visible_exact_bound',
  longResultHtml.includes('result-limit:120000')
  && !longResultHtml.includes('RESULT_TAIL')
  && occurrences(longResultHtml, 'R') >= 120000);
const emptyResultHtml = presentation.renderBrowserExecutionHtml(
  {
    toolName: 'browser_execute_js', query: 'no result',
    toolArgs: { code: 'void 0' }, toolContent: { unexpected: true },
  },
  {},
  header,
);
check('missing_string_result_does_not_invent_a_toggle',
  emptyResultHtml.includes('void 0')
  && !emptyResultHtml.includes('ptool-cmd-output-wrap'));
const fallbackQueryHtml = presentation.renderBrowserExecutionHtml(
  { toolName: 'browser_execute_js' }, {}, header,
);
check('missing_query_uses_generated_localized_copy',
  fallbackQueryHtml.includes('Execute JS-local'));
const hostileArguments = {};
Object.defineProperty(hostileArguments, 'code', {
  get() { throw new Error('untrusted getter'); },
});
const hostileGetterHtml = presentation.renderBrowserExecutionHtml(
  { toolName: 'browser_execute_js', toolArgs: hostileArguments }, {}, header,
);
check('malformed_object_projection_fails_closed',
  hostileGetterHtml.includes('ptool-cmd-js')
  && !hostileGetterHtml.includes('ptool-cmd-code'));

console.log(checks.join('\n'));
"""


_WIRING_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const messages = {
  'toolCmd.showResult': '显示结果',
  'toolBrowserExecution.executeJs': '执行 JavaScript',
  'toolBrowserExecution.ok': '成功',
  'toolBrowserExecution.error': '错误',
  'toolBrowserExecution.argumentsLimit': '参数上限 {n}',
  'toolBrowserExecution.codeLimit': '代码上限 {n}',
  'toolBrowserExecution.descriptionLimit': '说明上限 {n}',
  'toolBrowserExecution.resultLimit': '结果上限 {n}',
};
function translate(key, params) {
  let value = messages[key] || key;
  if (!params) return value;
  return value.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name) => (
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name]) : token
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

check('retained_entry_is_exposed', typeof renderToolRoundsHTML === 'function');
const normalHtml = _renderUnifiedToolLine({
  roundNum: 1, status: 'done', toolName: 'browser_execute_js',
  query: '计算', toolArgs: { code: '1 + 1', description: '数学' },
  toolContent: '2', results: [{}],
}, false);
check('browser_execution_owner_reaches_retained_dispatch',
  normalHtml.includes('ptool-cmd-js ptool-cmd-ok')
  && normalHtml.includes('1 + 1')
  && normalHtml.includes('数学')
  && normalHtml.includes('显示结果')
  && normalHtml.includes('>成功</span>'));
const errorHtml = _renderUnifiedToolLine({
  roundNum: 2, status: 'done', toolName: 'browser_execute_js',
  query: '失败', toolArgs: { code: 'x()' }, toolContent: 'ReferenceError',
  results: [{ badge: 'error' }],
}, false);
check('error_state_reaches_typed_owner',
  errorHtml.includes('ptool-cmd-js ptool-cmd-err')
  && errorHtml.includes('>错误</span>'));
const boundedHtml = _renderUnifiedToolLine({
  roundNum: 3, status: 'done', toolName: 'browser_execute_js',
  query: 'large', toolArgs: { code: 'x'.repeat(65537) }, results: [{}],
}, false);
check('owner_bound_reaches_retained_dispatch',
  boundedHtml.includes('代码上限 65536')
  && !boundedHtml.includes('x'.repeat(65537)));
const genericHtml = _renderUnifiedToolLine({
  roundNum: 4, status: 'done', toolName: 'list_dir', query: 'list',
  results: [{ badge: 'done', text: 'entry.txt' }],
}, false);
check('unrelated_tools_still_fall_through_to_generic_renderer',
  genericHtml.includes('ptool-line')
  && !genericHtml.includes('ptool-cmd-js'));
report();
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_tool_browser_execution_presentation_owner_contract() -> None:
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
    assert len(passes) == 18, process.stdout


def test_retained_dispatch_wires_browser_execution_owner() -> None:
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),
        body_js=_WIRING_HARNESS,
        expect_pass=5,
        label='tool-browser-execution retained wiring',
    )
