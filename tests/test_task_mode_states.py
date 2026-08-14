"""Task Mode run-list state coordination + bilingual localization (extraction-and-eval).

Two invariants, both proven by rendering the REAL extracted list view plus its
thin controller adapters under node with a stubbed ``document`` + ``Api``
AND the REAL i18n runtime (the ``_i18n`` table + ``t()`` extracted from
static/js/i18n.js):

  A. STATE DISTINCTION — the run list must tell apart three states that used to
     collapse into a misleading "No runs yet":
       1. LOAD ERROR — ``taskList()`` resolves ``null`` (its ``onError:'null'``
          contract) on a network/5xx failure → error card + Retry.
       2. GENUINELY EMPTY — ``{ok:true, runs:[]}`` → onboarding CTA → Studio.
       3. HAS RUNS — rows with a localized status chip + relative-time + duration.

  B. LOCALIZATION — Task Mode is opened from a topbar button that already renders
     ``任务`` (zh). Its whole operating room must speak the same language. We
     render every state card under BOTH ``zh`` and ``en`` and assert the
     language-appropriate text appears, PLUS a leak guard that the zh render
     carries none of the old bare-English literals.

Poisoned-fixture NCs prove both the error-branch and the localization are
load-bearing (not tautologies).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
TM_JS = os.path.join(ROOT, 'static', 'js', 'task-mode.js')
TM_LIST_JS = os.path.join(ROOT, 'static', 'js', 'task-mode-list.js')
I18N_JS = os.path.join(ROOT, 'static', 'js', 'i18n.js')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _brace_match(src: str, open_pos: int) -> int:
    """Return the index just past the '}' that closes the brace at open_pos."""
    depth = 0
    j = open_pos
    while j < len(src):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise AssertionError('unbalanced braces')


def _extract_fn(src: str, fn_name: str) -> str:
    """Grab `function <name>(...) { ... }` by brace-matching from its header."""
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(fn_name) + r'\s*\(', src)
    assert m, f'{fn_name} not found'
    i = src.find('{', m.end())
    return src[m.start():_brace_match(src, i)]


def _extract_i18n_runtime() -> str:
    """Extract the real `_i18n` table + `t()` from i18n.js, plus a MUTABLE
    `_i18nLang` the harness can flip between renders."""
    src = _read(I18N_JS)
    m = re.search(r'var\s+_i18n\s*=\s*', src)
    assert m, '_i18n table not found in i18n.js'
    brace = src.find('{', m.end())
    table = src[m.start():_brace_match(src, brace)]      # `var _i18n = {...}`
    t_fn = _extract_fn(src, 't')
    # A settable language global (the real file reads it from localStorage).
    return 'var _i18nLang = "zh";\n' + table + ';\n' + t_fn


def _task_list_success(runs: list[dict]) -> dict:
    return {
        'ok': True,
        'runs': runs,
        'page': {'limit': 50, 'has_more': False, 'next_limit': None},
    }


def _run(*, task_list_result, lang: str = 'en', poison: str = '') -> dict:
    """Eval the real run-list functions with a stubbed DOM + Api + real i18n.

    ``lang`` sets the render language ('zh'|'en'). ``poison`` selects a
    load-bearing neuter: 'error_branch' collapses the null→error path; 'i18n'
    makes t() echo the key so localized text can't appear.
    """
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for extraction-and-eval')

    src = _read(TM_JS)
    list_view = _read(TM_LIST_JS)
    request_runtime = '\n'.join(_read(os.path.join(
        ROOT, 'static', 'js', name)) for name in (
            'api/http-result.js',
            'api/orchestration-http-contract.generated.js',
            'api/orchestration-response-contracts.js',
            'api/orchestration-client-methods.js',
            'api/orchestration-endpoint-transport.js',
            'api/orchestration-endpoints.js',
            'orchestration-wire-formats.generated.js',
            'orchestration-compatibility-defaults.generated.js',
            'orchestration-compatibility-contracts.js',
            'orchestration-wire-contract.js',
            'orchestration-run-status.js',
            'orchestration-run-filter.js',
            'orchestration-diagnostic-report.js',
            'orchestration-result.js',
            'orchestration-read-core.js',
            'orchestration-runtime-read.js',
            'orchestration-durable-run-snapshot.js',
            'orchestration-durable-list-read.js',
            'orchestration-durable-read.js',
            'orchestration-replay-read.js',
            'orchestration-http-read.js',
            'orchestration-request-failure.js',
            'orchestration-api-request.js',
            'orchestration-request-contract.js',
            'orchestration-task-request.js',
            'orchestration-run-session.js',
            'orchestration-roving-items.js',
            'task-mode-services.js',
            'task-mode-run-store.js',
            'task-mode-run-time.js',
            'task-mode-list-focus.js',
            'task-mode-list-paging.js',
            'task-mode-list-error.js',
            'task-mode-run-status-presentation.js',
            'task-mode-list-presentation.js',
            'orchestration-request-reader.js',
            'task-mode-list-controller.js',
            'task-mode-controller-hub.js',
            'task-mode-root-controller.js',
        ))
    fns = [
        '_tmApiClient', '_tmTaskClient', '_tmReportTaskFailure',
        '_tmEnsureControllerHub',
        '_tmT', '_tmEsc', '_tmIsTerminal', '_tmEnsureRunListController',
        '_tmEnsureRunListView',
        '_tmSetRunListBusy', '_tmRenderRunList', '_tmRefreshRuns',
    ]
    extracted = '\n'.join(_extract_fn(src, f) for f in fns)
    i18n_runtime = _extract_i18n_runtime()

    if poison == 'error_branch':
        request_runtime = request_runtime.replace(
            "if (!result || result.ok !== true || !Array.isArray(result.runs)) {",
            "if (!result || result.ok !== true || !Array.isArray(result.runs)) { loadError = false; runs = []; return true; } if (false) {")
        assert 'loadError = false; runs = []' in request_runtime, \
            'poison did not apply'
    if poison == 'i18n':
        # Force t() to echo the key — simulates a NON-localized render (the bug
        # this whole change fixes). Localized text assertions must then fail.
        i18n_runtime = i18n_runtime.replace(
            'var entry = _i18n[key];',
            'var entry = null;')

    harness = f'''
{i18n_runtime}
_i18nLang = {json.dumps(lang)};
// ── module-level state the extracted fns read ──
var _tmRunSession = createOrchestrationRunSession();
var _tmRunStore = createTaskModeRunStore();
var _tmRunListController = null;
var _tmControllerHub = null;
var _tmContracts = {{snapshot: function() {{ return {{runContract: null}}; }}}};
// stubs
function _tmIco(name) {{ return '<svg data-ico="' + name + '"></svg>'; }}
function _tmOpenRun() {{}}
function _tmToast() {{}}
function _tmReconcileRunMutation() {{}}
function _tmProjectRunTransition() {{}}
function _tmRenderGraph() {{}}
function _tmRenderInspector() {{}}
function _tmRenderTimelineEvent() {{}}
function _tmSelectPanel() {{}}
function _tmSyncChip() {{}}
function orchestrationMutationMessage(_value,_translate,fallback) {{ return fallback; }}
function _tmEnsureRunController() {{ return {{ id: function() {{
  return _tmRunSession.id();
}} }}; }}
function escapeHtml(s) {{ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){{
  return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]; }}); }}
var _lastHtml = '';
var _listEl = {{
  set innerHTML(v) {{ _lastHtml = v; }}, get innerHTML() {{ return _lastHtml; }},
  setAttribute: function() {{}},
  querySelectorAll: function() {{ return []; }}
}};
var document = {{ getElementById: function(id) {{ return id === 'tmRunList' ? _listEl : null; }} }};
var Api = {{ orchestrations: {{ taskList: async function() {{ return {json.dumps(task_list_result)}; }} }} }};
globalThis.Api = Api;

{list_view}
{request_runtime}
var _tmRootController=createTaskModeRootController({{
  services:function(){{return _tmServices;}},
  contractSession:_tmContracts,session:_tmRunSession,runStore:_tmRunStore,
  mutationMessage:orchestrationMutationMessage,
  resultError:orchestrationResultError,
  translate:_tmT,reconcileRun:_tmReconcileRunMutation,
  refreshRuns:_tmRefreshRuns,openRun:_tmOpenRun,
  renderRunList:function(){{return _tmRenderRunList();}},
  isTerminal:_tmIsTerminal,projectTransition:_tmProjectRunTransition,
  renderGraph:_tmRenderGraph,renderInspector:_tmRenderInspector,
  renderTimelineEvent:_tmRenderTimelineEvent,syncChip:_tmSyncChip,
}});
{extracted}

(async function() {{
  await _tmRefreshRuns();
  process.stdout.write(JSON.stringify({{
    html: _lastHtml,
    loadError: _tmRunStore.snapshot(_tmRunSession.id()).loadError,
  }}));
}})();
'''
    with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False) as f:
        f.write(harness)
        tmp = f.name
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True, timeout=20)
        assert out.returncode == 0, f'node eval failed: {out.stderr}'
        return json.loads(out.stdout)
    finally:
        os.unlink(tmp)


# ─────────────────────────── state distinction ───────────────────────────

def test_load_failure_shows_error_not_empty():
    """A null taskList() (failed request) renders the error card with a Retry
    button — NOT the misleading empty state."""
    r = _run(task_list_result=None, lang='en')
    assert r['loadError'] is True
    assert 'tm-state-err' in r['html']
    assert 'Retry' in r['html']
    assert 'No task runs yet' not in r['html']


def test_explicit_failed_envelope_shows_retry_state():
    r = _run(task_list_result={'ok': False, 'error': 'backend unavailable'},
             lang='en')
    assert r['loadError'] is True
    assert 'tm-state-err' in r['html']
    assert 'No task runs yet' not in r['html']
    assert 'The backend did not accept the operation' in r['html']
    assert 'backend unavailable' not in r['html']


def test_empty_account_shows_studio_cta():
    """A genuinely-empty account renders the onboarding CTA that bridges to the
    Studio — the actionable empty state."""
    r = _run(task_list_result=_task_list_success([]), lang='en')
    assert r['loadError'] is False
    assert 'No task runs yet' in r['html']
    assert 'data-tm-action="open-studio"' in r['html']
    assert 'tm-state-err' not in r['html']


def test_runs_render_with_chip_and_duration():
    """Runs render as rows with a status chip and a duration label."""
    now = 1_000_000_000_000
    runs = [
        {'id': 'run_a', 'name': 'Résumé screen', 'status': 'done', 'terminal': True,
         'created_at': now, 'finished_at': now + 42_000, 'updated_at': now + 42_000},
        {'id': 'run_b', 'name': 'Live job', 'status': 'running', 'terminal': False,
         'created_at': now, 'updated_at': now},
    ]
    r = _run(task_list_result=_task_list_success(runs), lang='en')
    assert r['loadError'] is False
    assert 'Résumé screen' in r['html']
    assert 'tm-chip-done' in r['html']
    assert 'tm-chip-running' in r['html']
    assert '42s' in r['html']
    assert 'tm-run-live' in r['html']   # the running row is flagged live
    assert 'data-tm-run-index="0"' in r['html']
    assert '_tmOpenRun(' not in r['html']


def test_server_declared_terminal_status_drives_row_lifecycle():
    """New status names can roll out without first teaching old JS literals."""
    now = 1_000_000_000_000
    r = _run(task_list_result=_task_list_success([{
        'id': 'archived', 'name': 'Archived run', 'status': 'archived',
        'terminal': True, 'created_at': now,
        'finished_at': now + 2_000, 'updated_at': now + 2_000,
    }]), lang='en')
    assert 'tm-run-live' not in r['html']
    assert '2s' in r['html']


def test_terminal_lifecycle_is_server_projected_without_status_literals():
    src = _read(TM_JS)
    fn = _extract_fn(src, '_tmIsTerminal')
    contract = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-contract.js'))
    mutation_result = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-mutation-result.js'))
    run_status = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-run-status.js'))
    mutation = _read(os.path.join(
        ROOT, 'lib', 'orchestration', 'mutation_result.py'))
    assert 'orchestrationRunIsTerminal(' in fn
    assert '_orchAuthoring' not in fn
    assert "status === 'done'" not in fn
    assert 'typeof runOrStatus.terminal' in run_status
    assert 'function isTerminalRunStatus(status)' not in contract
    assert "terminal: ['done', 'error', 'aborted']" not in contract
    assert 'resourceTerminal' in mutation_result
    assert 'mutation_payload_field_names()' in mutation
    assert "fields['resourceTerminal']: self.resource_terminal" in mutation


def test_task_mode_refreshes_the_backend_run_contract_on_open():
    src = _read(TM_JS)
    session = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-contract-session.js'))
    controller = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-contract-controller.js'))
    opened = _extract_fn(src, 'openTaskMode')
    refresh = _extract_fn(src, '_tmRefreshAuthoringContract')
    closed = _extract_fn(src, '_tmAfterClose')
    assert 'await _tmRefreshAuthoringContract()' in opened
    assert opened.index('await _tmRefreshAuthoringContract()') < \
        opened.index('var refresh = _tmRefreshRuns()')
    assert 'var openOwner = shell.captureOpen()' in opened
    assert 'shell.ownsOpen(openOwner)' in opened
    assert '_tmEnsureContractController().refresh()' in refresh
    assert 'await session.refresh(' in controller
    assert 'capability.refreshAuthoringContract()' in controller
    assert 'owner !== generation' in session
    assert 'options.onAdopt(result.contracts)' in controller
    assert '_tmEnsureContractController().invalidate()' in closed
    assert '_tmRunListController.invalidate()' in closed


# ─────────────────── B. bilingual render ground truth ───────────────────

# Expected localized text per state, per language — the ground truth the
# operating room must render. zh strings come straight from the i18n table.
_EXPECT = {
    'error': {'zh': '无法加载运行', 'en': "Couldn't load runs"},
    'empty': {'zh': '还没有任务运行', 'en': 'No task runs yet'},
    'retry': {'zh': '重试', 'en': 'Retry'},
    'openStudio': {'zh': '打开编排台', 'en': 'Open Studio'},
    'statusDone': {'zh': '完成', 'en': 'Done'},
    'statusRunning': {'zh': '运行中', 'en': 'Running'},
}

# Old bare-English literals that must NOT leak into a zh render.
_ENGLISH_LEAKS = [
    "Couldn't load runs", 'No task runs yet', 'Open Studio', 'Retry',
    "The server didn't respond",
]


@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_error_card_localized(lang):
    r = _run(task_list_result=None, lang=lang)
    assert r['loadError'] is True
    assert _EXPECT['error'][lang] in r['html'], f'{lang} error title missing'
    assert _EXPECT['retry'][lang] in r['html'], f'{lang} retry label missing'


@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_empty_cta_localized(lang):
    r = _run(task_list_result=_task_list_success([]), lang=lang)
    assert _EXPECT['empty'][lang] in r['html'], f'{lang} empty title missing'
    assert _EXPECT['openStudio'][lang] in r['html'], f'{lang} CTA label missing'


@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_status_chip_localized(lang):
    now = 1_000_000_000_000
    runs = [
        {'id': 'a', 'name': 'x', 'status': 'done', 'terminal': True,
         'created_at': now, 'finished_at': now + 1000, 'updated_at': now + 1000},
        {'id': 'b', 'name': 'y', 'status': 'running', 'terminal': False,
         'created_at': now, 'updated_at': now},
    ]
    r = _run(task_list_result=_task_list_success(runs), lang=lang)
    # status shown as a LOCALIZED label; raw status stays as the CSS class.
    assert _EXPECT['statusDone'][lang] in r['html']
    assert _EXPECT['statusRunning'][lang] in r['html']
    assert 'tm-chip-done' in r['html'] and 'tm-chip-running' in r['html']


def test_zh_render_has_no_english_leak():
    """The zh render of every state card must carry NONE of the old bare-English
    literals — proving nothing was left un-localized."""
    for result in (None, _task_list_success([])):
        r = _run(task_list_result=result, lang='zh')
        for leak in _ENGLISH_LEAKS:
            assert leak not in r['html'], f'English leak in zh render: {leak!r}'


def test_zh_run_metadata_localizes_relative_time_and_unnamed_flow():
    r = _run(task_list_result=_task_list_success([{
        'id': 'run', 'name': '', 'status': 'done', 'terminal': True,
        'created_at': 1_000_000_000_000,
        'finished_at': 1_000_000_001_000,
        'updated_at': 1_000_000_001_000,
    }]), lang='zh')
    assert '（未命名流程）' in r['html']
    assert '前' in r['html']
    assert not re.search(r'\d+[smhd] ago', r['html'])


# ─────────────────────────── poisoned-fixture NCs ───────────────────────────

def test_nc_poisoned_error_branch_regresses_to_empty():
    """Neuter the failure branch → a null result is treated as empty. Proves the
    null→error branch is load-bearing."""
    r = _run(task_list_result=None, poison='error_branch', lang='en')
    assert r['loadError'] is False, 'poison did not neuter the error branch'
    assert 'No task runs yet' in r['html']
    assert 'tm-state-err' not in r['html']


def test_nc_poisoned_i18n_drops_localized_text():
    """Neuter t() (echo the key) → the zh localized title CANNOT appear, and the
    render carries the raw key instead. Proves the localized strings actually
    flow through t() and aren't hardcoded zh literals."""
    r = _run(task_list_result=_task_list_success([]),
             poison='i18n', lang='zh')
    assert _EXPECT['empty']['zh'] not in r['html'], 'zh text survived a neutered t()'
    assert 'tm.empty.title' in r['html'], 'expected the raw i18n key to leak through'


# ───────────── C. the INTERACTIVE surface: human-gate card ─────────────
#
# The gate card is the one place the operator must ACT while a run is live
# (approve / reject a control gate, or type an input answer). If it renders in
# English under a `任务` (zh) button, a Chinese operator can't act in their own
# language. `_tmGateCard(ev)` is a pure function of the gate event, so we render
# it directly under both languages.

def _run_gate(*, ev: dict, lang: str = 'en', poison: str = '') -> str:
    """Eval the REAL _tmGateCard(ev) under node with the real i18n runtime.
    Returns the rendered gate-card HTML. poison='i18n' neuters t()."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for extraction-and-eval')

    inspector = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-inspector.js'))
    disclosure_state = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-disclosure-state.js'))
    bounded_state = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-bounded-state.js'))
    inspector_presentation = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-inspector-presentation.js'))
    gate_view = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-gate-view.js'))
    gate_presentation = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-gate-presentation.js'))
    request_limits = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-request-limits.js'))
    action_lock = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-action-lock.js'))
    i18n_runtime = _extract_i18n_runtime()
    if poison == 'i18n':
        i18n_runtime = i18n_runtime.replace('var entry = _i18n[key];', 'var entry = null;')

    harness = f'''
{i18n_runtime}
_i18nLang = {json.dumps(lang)};
function _tmIco(name) {{ return '<svg data-ico="' + name + '"></svg>'; }}
function escapeHtml(s) {{ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){{
  return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]; }}); }}
{request_limits}
{action_lock}
{gate_presentation}
{gate_view}
{inspector_presentation}
{bounded_state}
{disclosure_state}
{inspector}
var view=createTaskModeInspectorView({{
  translate:t,escape:escapeHtml,icon:_tmIco,
  scrollState:{{capture:function(){{}},restore:function(){{}}}},
  draftState:{{clear:function(){{}},bind:function(){{return function(){{}};}}}}
}});
process.stdout.write(view.gateCard({json.dumps(ev)}));
'''
    with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False) as f:
        f.write(harness)
        tmp = f.name
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True, timeout=20)
        assert out.returncode == 0, f'node eval failed: {out.stderr}'
        return out.stdout
    finally:
        os.unlink(tmp)


_GATE_EXPECT = {
    'tag': {'zh': '人工确认', 'en': 'Human gate'},
    'approve': {'zh': '批准', 'en': 'Approve'},
    'reject': {'zh': '拒绝', 'en': 'Reject'},
    'send': {'zh': '发送', 'en': 'Send'},
    'approvePrompt': {'zh': '批准后继续？', 'en': 'Approve to continue?'},
    'inputPrompt': {'zh': '请输入回复', 'en': 'Enter a response.'},
}


@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_gate_card_approve_localized(lang):
    """The APPROVE gate — the operator's act surface — renders its tag, prompt,
    and both buttons (批准/Approve, 拒绝/Reject) in the active language."""
    html = _run_gate(ev={'request_id': 'r1', 'mode': 'approve'}, lang=lang)
    assert _GATE_EXPECT['tag'][lang] in html, f'{lang} gate tag missing'
    assert _GATE_EXPECT['approve'][lang] in html, f'{lang} Approve missing'
    assert _GATE_EXPECT['reject'][lang] in html, f'{lang} Reject missing'
    # default prompt (no ev.prompt supplied) is localized too
    assert _GATE_EXPECT['approvePrompt'][lang] in html
    # Fixed action markers are bound to opaque request IDs after injection.
    assert 'data-tm-gate-decision="approve"' in html
    assert 'data-tm-gate-decision="reject"' in html
    assert 'onclick=' not in html


@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_gate_card_input_localized(lang):
    """The INPUT gate renders its Send button + placeholder + default prompt
    localized."""
    html = _run_gate(ev={'request_id': 'r2', 'mode': 'input'}, lang=lang)
    assert _GATE_EXPECT['send'][lang] in html, f'{lang} Send missing'
    assert _GATE_EXPECT['inputPrompt'][lang] in html
    assert 'data-tm-gate-input' in html
    assert 'data-tm-gate-send' in html
    assert 'onclick=' not in html


def test_gate_card_zh_no_english_leak():
    """The zh gate card carries none of the old bare-English gate literals in its
    VISIBLE text. Strip non-text attributes so the guard checks the rendered
    labels rather than fixed data-action names."""
    attr = re.compile(r'\b(?:onclick|id|class|data-ico)="[^"]*"')
    for mode in ('approve', 'input'):
        html = _run_gate(ev={'request_id': 'r', 'mode': mode}, lang='zh')
        visible = attr.sub('', html)
        for leak in ('Human gate', 'Approve', 'Reject', 'Send',
                     'Approve to continue?', 'Your input?', 'Type your answer'):
            assert leak not in visible, f'English leak in zh {mode} gate: {leak!r}'


def test_gate_card_keeps_backend_request_id_out_of_markup():
    hostile = "gate-'\"><script>bad()</script>"
    html = _run_gate(
        ev={'request_id': hostile, 'mode': 'approve'}, lang='en',
    )
    assert hostile not in html
    assert '<script>' not in html
    assert 'onclick=' not in html
    assert 'data-tm-gate-index="0"' in html


def test_nc_poisoned_i18n_drops_gate_labels():
    """POISONED-NC for the interactive surface: neuter t() → the zh gate labels
    CANNOT appear and the raw keys leak. Proves the gate buttons the operator
    clicks flow through t(), not hardcoded strings."""
    html = _run_gate(ev={'request_id': 'r', 'mode': 'approve'}, lang='zh', poison='i18n')
    assert _GATE_EXPECT['approve']['zh'] not in html, 'zh Approve survived a neutered t()'
    assert _GATE_EXPECT['reject']['zh'] not in html, 'zh Reject survived a neutered t()'
    assert 'orch.gate.approve' in html, 'expected the raw gate key to leak through'


def test_task_failure_reporter_keeps_envelope_failures_diagnosable():
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    root_controller = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-root-controller.js'))
    script = f'''
var reports=[];
var services={{
  reportError:function(){{reports.push(Array.from(arguments));}},
  api:function(){{}},studio:function(){{}},toast:function(){{}},
}};
function orchestrationResultError(value){{
  return value&&value.error&&value.error.message||'';
}}
function createTaskModeControllerHub(){{throw new Error('not used');}}
{root_controller}
var root=createTaskModeRootController({{
  services:function(){{return services;}},resultError:orchestrationResultError,
}});
var success=root.reportTaskFailure('list',{{ok:true,status:200}});
var envelope=root.reportTaskFailure('list',{{ok:false,status:503,
  reason:'server-failed',error:{{message:'store offline'}}}});
var cause=new Error('network down');
var transport=root.reportTaskFailure('events',{{ok:false,cause:cause}});
process.stdout.write(JSON.stringify({{
  success:success,envelope:envelope,transport:transport,
  reports:reports.map(function(row){{return [row[0],row[1],{{
    status:row[2].status||0,reason:row[2].reason||'',
    error:row[2].error||'',message:row[2].message||'',
  }}];}}),
}}));
'''
    proc = subprocess.run(
        [node, '-e', script], capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        'success': False,
        'envelope': True,
        'transport': True,
        'reports': [
            ['TaskMode', 'list', {
                'status': 503, 'reason': 'server-failed',
                'error': 'store offline', 'message': '',
            }],
            ['TaskMode', 'events', {
                'status': 0, 'reason': '', 'error': '',
                'message': 'network down',
            }],
        ],
    }


def test_dynamic_task_and_graph_ids_stay_out_of_inline_handlers():
    src = _read(TM_JS)
    dialog = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-dialog.js'))
    task_shell = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-shell.js'))
    task_shell_template = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-shell-template.js'))
    run_list = _read(TM_LIST_JS)
    run_title_view = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-run-title-view.js'))
    run_view = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-run-view.js'))
    node_presentation = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-node-presentation.js'))
    timeline = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-timeline.js'))
    graph_projection = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-graph-projection.js'))
    graph = _read(os.path.join(ROOT, 'static', 'js', 'task-mode-graph.js'))
    inspector = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-inspector.js'))
    gate_view = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-gate-view.js'))
    gate_presentation = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-gate-presentation.js'))
    gate_interaction = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-human-gate-interaction.js'))
    mutation_request = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-mutation-request.js'))
    task_actions = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-actions.js'))
    task_commands = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-command-controller.js'))
    task_commands += _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-run-command-controller.js'))
    list_controller = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-list-controller.js'))
    css = _read(os.path.join(ROOT, 'static', 'styles.css'))
    assert "onclick=\"_tmOpenRun(" not in src
    assert "onclick=\"_tmDeleteRun(" not in src
    assert "onclick=\"_tmAbortRun(" not in src
    assert "onclick=\"_tmSelectNode(" not in src
    assert 'onclick=' not in src
    assert 'onclick=' not in task_shell
    assert 'onclick=' not in task_shell_template
    assert 'onclick=' not in run_list
    assert 'onclick=' not in run_view
    assert 'onclick=' not in run_title_view
    assert 'onclick=' not in inspector
    assert 'onclick=' not in gate_view
    assert 'console.warn' not in gate_view
    assert "report('gate action', error)" in gate_view
    assert 'oninput=' not in src
    assert 'onchange=' not in src
    assert 'onkeydown=' not in src
    assert 'onerror=' not in src
    assert 'data-tm-action="open-studio"' in task_shell_template
    assert 'data-tm-action="refresh-runs"' in task_shell_template
    assert "event.target.closest('[data-tm-action]')" in task_shell
    assert 'await Api.orchestrations' not in src
    assert "await reader.read(\n      'list', ['', '', requestedLimit])" \
        in list_controller
    assert 'return _tmEnsureRunListController().refresh()' in src
    assert '_tmApiCall' not in src
    assert 'createOrchestrationMutationRequestClient' in mutation_request
    assert "'approveGate', [requestId, approved]" in task_actions
    assert "'inputGate', [requestId, value]" in task_actions
    assert "'abortDurable', [runId]" in task_actions
    assert "'removeDurable', [runId]" in task_actions
    assert '_tmEnsureCommands().approveGate(rid, approved)' in src
    assert '_tmEnsureCommands().inputGate(rid, input)' in src
    assert '_tmEnsureCommands().abortRun(runId)' in src
    assert '_tmEnsureCommands().deleteRun(runId)' in src
    assert "_gate('inputGate', requestId, value" in task_commands
    assert "_call('reconcileRun', outcome.mutation, runId)" in task_commands
    assert "_tmApiCall('humanApprove'" not in src
    assert "_tmApiCall('humanInput'" not in src
    assert "_tmApiCall('taskAbort'" not in src
    assert "_tmApiCall('taskRemove'" not in src
    assert 'data-tm-avatar' in node_presentation
    assert "avatar.addEventListener('error'" in node_presentation
    assert 'role="dialog" aria-modal="true" tabindex="-1"' \
        in task_shell_template
    assert 'role="log"' in task_shell_template
    assert 'aria-live="off"' in task_shell_template
    assert 'aria-relevant="additions"' in task_shell_template
    assert "timeline.setAttribute('aria-busy'" in timeline
    assert "overlay.addEventListener('keydown', keyDown)" in task_shell
    assert 'focusManager.trapTab(event, dialog)' in task_shell
    assert 'previousFocus.focus()' in dialog
    assert 'data-tm-run-index' in run_list
    assert "button.addEventListener('click'" in run_list
    assert 'data-tm-title-delete' in run_title_view
    assert 'data-tm-title-abort' in run_title_view
    assert 'data-tm-node-index' in graph_projection
    assert 'role="button" tabindex="0" aria-pressed=' in graph_projection
    assert 'role="button" tabindex="0" aria-pressed=' not in graph
    assert "getAttribute('data-tm-node-index')" in graph
    assert "card.addEventListener('keydown'" in graph
    assert 'onclick=' not in graph
    assert 'data-tm-gate-index' in gate_presentation
    assert 'data-tm-gate-decision' in gate_presentation
    assert 'interaction.bindClick(button' in gate_view
    assert "control.addEventListener('click'" in gate_interaction
    assert 'function _tmInjectStyles' not in src
    assert "createElement('style')" not in src
    assert '.tm-overlay{' in css
    assert '.tm-final-pre.tm-final-error{' in css
    assert 'height:var(--vh100,100dvh)' in css
    assert '.tm-mobile-tabs{display:grid' in css
    assert '.tm-body>[aria-hidden="true"]{display:none}' in css
    assert '.tm-main{flex:1;min-height:0}' in css
    assert '.tm-top-actions .tm-btn span{display:none}' in css
    assert '.tm-title-row .tm-btn span{display:none}' in css
    assert '.tm-run:focus-visible{' in css
    assert '.tm-gnode:focus-visible{' in css
    assert 'justify-content:safe center' in css
    assert '@media(prefers-reduced-motion:reduce)' in css


def test_visible_task_mode_status_and_event_copy_is_localized():
    src = _read(TM_JS)
    run_list = _read(TM_LIST_JS)
    list_presentation = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-list-presentation.js'))
    i18n = _read(I18N_JS)
    event_format = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-event-format.js'))
    run_time = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-run-time.js'))
    for literal in (
        "' approved'", "' rejected'", "' answered'", "' agents, '",
        'Select a run to view its timeline.', "'(unnamed flow)'",
    ):
        assert literal not in src
    for key in (
        'tm.unnamedFlow', 'tm.ago.seconds', 'tm.ago.minutes',
        'tm.ago.hours', 'tm.ago.days', 'orch.ev.gateApproved',
        'orch.ev.gateRejected', 'orch.ev.gateAnswered',
        'orch.ev.completeSummary', 'tm.status.completed',
    ):
        assert f"'{key}'" in i18n
        assert (key in src or key in run_list or key in list_presentation
                or key in run_time
                or key in event_format
                or key == 'tm.status.completed')


@pytest.mark.parametrize(
    ('api_result', 'gate_kept', 'toast_error'),
    [
        (None, True, True),
        ({
            'ok': True,
            'mutation': {
                'format': 'tofu.orchestration.mutation/v1',
                'ok': True,
                'action': 'approve_gate',
                'reason': 'accepted',
                'target_id': 'gate_1',
                'resource_status': '',
                'resource_terminal': None,
                'target_exists': False,
                'retryable': False,
                'reconcile_required': False,
            },
        }, False, False),
        ({
            'ok': False,
            'status': 404,
            'data': {
                'ok': False,
                'mutation': {
                    'format': 'tofu.orchestration.mutation/v1',
                    'ok': False,
                    'action': 'approve_gate',
                    'reason': 'not_found',
                    'target_id': 'gate_1',
                    'resource_status': '',
                    'resource_terminal': None,
                    'target_exists': False,
                    'retryable': False,
                    'reconcile_required': True,
                },
            },
        }, False, True),
    ],
)
def test_gate_is_removed_only_after_backend_confirmation(
        api_result, gate_kept, toast_error):
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    src = _read(TM_JS)
    wire_formats_src = _read(os.path.join(
        ROOT, 'static', 'js',
        'orchestration-wire-formats.generated.js'))
    compatibility_defaults_src = _read(os.path.join(
        ROOT, 'static', 'js',
        'orchestration-compatibility-defaults.generated.js'))
    compatibility_contracts_src = _read(os.path.join(
        ROOT, 'static', 'js',
        'orchestration-compatibility-contracts.js'))
    wire_contract_src = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-wire-contract.js'))
    result_src = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-result.js'))
    mutation_payload_src = _read(os.path.join(
        ROOT, 'static', 'js',
        'orchestration-mutation-payload-contract.js'))
    mutation_result_src = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-mutation-result.js'))
    read_core_src = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-read-core.js'))
    runtime_read_src = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-runtime-read.js'))
    http_read_src = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-http-read.js'))
    api_result_src = _read(os.path.join(
        ROOT, 'static', 'js', 'api', 'http-result.js'))
    http_contract_src = _read(os.path.join(
        ROOT, 'static', 'js', 'api',
        'orchestration-http-contract.generated.js'))
    response_contracts_src = _read(os.path.join(
        ROOT, 'static', 'js', 'api',
        'orchestration-response-contracts.js'))
    client_methods_src = _read(os.path.join(
        ROOT, 'static', 'js', 'api',
        'orchestration-client-methods.js'))
    endpoint_transport_src = _read(os.path.join(
        ROOT, 'static', 'js', 'api',
        'orchestration-endpoint-transport.js'))
    endpoint_registry_src = _read(os.path.join(
        ROOT, 'static', 'js', 'api', 'orchestration-endpoints.js'))
    api_request_src = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-api-request.js'))
    request_failure_src = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-request-failure.js'))
    request_contract_src = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-request-contract.js'))
    mutation_request_src = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-mutation-request.js'))
    mutation_command_src = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-mutation-command.js'))
    diagnostic_report_src = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-diagnostic-report.js'))
    single_flight_src = _read(os.path.join(
        ROOT, 'static', 'js', 'orchestration-single-flight.js'))
    action_controller_src = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-actions.js'))
    command_controller_src = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-command-controller.js'))
    run_command_controller_src = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-run-command-controller.js'))
    controller_hub_src = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-controller-hub.js'))
    root_controller_src = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-root-controller.js'))
    task_services_src = _read(os.path.join(
        ROOT, 'static', 'js', 'task-mode-services.js'))
    extracted = '\n'.join(
        _extract_fn(src, name)
        for name in (
            '_tmApiClient', '_tmStudioClient', '_tmToast',
            '_tmTaskClient', '_tmReportTaskFailure',
            '_tmEnsureControllerHub', '_tmEnsureActions',
            '_tmEnsureCommands', '_tmHumanApprove',
        )
    )
    harness = f'''
var _tmGates={{gate_1:{{mode:'approve'}}}};var renders=0;var toasts=[];
var _tmControllerHub=null;
var _tmRunSession={{}};
var _tmContracts={{snapshot:function(){{return {{}};}}}};
var Api={{orchestrations:{{humanApprove:async function(){{return {json.dumps(api_result)};}}}}}};
globalThis.Api=Api;
function _tmRenderInspector(){{renders++;}}
var _tmEventController={{dismissGate:function(rid){{
  if(!_tmGates[rid])return false;delete _tmGates[rid];_tmRenderInspector();return true;
}}}};
function createTaskModeEventController(){{return _tmEventController;}}
function _tmEnsureEventController(){{return _tmEventController;}}
function _tmReconcileRunMutation(){{}}
function _tmRefreshRuns(){{}}
function _tmOpenRun(){{}}
function _tmProjectRunTransition(){{}}
function _tmRenderGraph(){{}}
function _tmRenderTimelineEvent(){{}}
function _tmSelectPanel(){{}}
function _tmSyncChip(){{}}
function _tmIsTerminal(){{return false;}}
function _tmEnsureRunController(){{return{{id:function(){{return null;}},reset:function(){{}}}};}}
function _tmT(key){{return key;}}
var _orchStudioApi={{toast:function(message,isError){{toasts.push([message,isError]);}}}};
{task_services_src}
{wire_formats_src}
{compatibility_defaults_src}
{compatibility_contracts_src}
{wire_contract_src}
{result_src}
{mutation_payload_src}
{mutation_result_src}
{read_core_src}
{runtime_read_src}
{http_read_src}
{api_result_src}
{http_contract_src}
{response_contracts_src}
{client_methods_src}
{endpoint_transport_src}
{endpoint_registry_src}
{request_failure_src}
{api_request_src}
{request_contract_src}
{mutation_request_src}
{diagnostic_report_src}
{mutation_command_src}
{single_flight_src}
{action_controller_src}
{run_command_controller_src}
{command_controller_src}
{controller_hub_src}
{root_controller_src}
var _tmRootController=createTaskModeRootController({{
  services:function(){{return _tmServices;}},
  contractSession:_tmContracts,session:_tmRunSession,runStore:{{discard:function(){{}}}},
  mutationMessage:orchestrationMutationMessage,
  resultError:orchestrationResultError,translate:_tmT,
  reconcileRun:_tmReconcileRunMutation,refreshRuns:_tmRefreshRuns,
  openRun:_tmOpenRun,renderRunList:function(){{}},isTerminal:_tmIsTerminal,
  projectTransition:_tmProjectRunTransition,renderGraph:_tmRenderGraph,
  renderInspector:_tmRenderInspector,
  renderTimelineEvent:_tmRenderTimelineEvent,syncChip:_tmSyncChip,
}});
{extracted}
(async function(){{await _tmHumanApprove('gate_1',true);process.stdout.write(JSON.stringify({{
 kept:!!_tmGates.gate_1,renders:renders,toasts:toasts
}}));}})();
'''
    with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False) as handle:
        handle.write(harness)
        tmp = handle.name
    try:
        proc = subprocess.run(
            [node, tmp], capture_output=True, text=True, timeout=20,
        )
    finally:
        os.unlink(tmp)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result['kept'] is gate_kept
    assert result['renders'] == (0 if gate_kept else 1)
    assert bool(result['toasts'][-1][1]) is toast_error


def test_shared_mutation_client_maps_durable_actions():
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    script = r'''
const fs=require('fs');
global.window=global;
for(const file of [
  'api/http-result.js','api/orchestration-http-contract.generated.js',
  'api/orchestration-response-contracts.js',
  'api/orchestration-client-methods.js',
  'api/orchestration-endpoint-transport.js',
  'api/orchestration-endpoints.js',
  'orchestration-wire-formats.generated.js',
  'orchestration-compatibility-defaults.generated.js',
  'orchestration-compatibility-contracts.js',
  'orchestration-wire-contract.js','orchestration-result.js',
  'orchestration-mutation-payload-contract.js',
  'orchestration-mutation-result.js',
  'orchestration-read-core.js',
  'orchestration-runtime-read.js',
  'orchestration-durable-run-snapshot.js',
  'orchestration-durable-list-read.js',
  'orchestration-durable-read.js',
  'orchestration-replay-read.js',
  'orchestration-http-read.js',
  'orchestration-request-failure.js','orchestration-api-request.js',
  'orchestration-request-contract.js',
  'orchestration-mutation-request.js',
])eval(fs.readFileSync('static/js/'+file,'utf8'));
const calls=[];
const api={
  taskAbort:async id=>{calls.push(['abort',id]);return{ok:true,status:200,
    data:{ok:true,mutation:{format:'tofu.orchestration.mutation/v1',ok:true,
      action:'abort_run',reason:'accepted',target_id:id,resource_status:'',
      resource_terminal:null,target_exists:true,retryable:false,
      reconcile_required:false}}};},
  taskRemove:async id=>{calls.push(['remove',id]);return{ok:false,status:409,
    data:{ok:false,error:'run is active',mutation:{
      format:'tofu.orchestration.mutation/v1',ok:false,
      action:'delete_run',reason:'active',target_id:id,resource_status:'',
      resource_terminal:null,target_exists:true,retryable:false,
      reconcile_required:true}}};},
};
(async()=>{
  const client=createOrchestrationMutationRequestClient({api:()=>api});
  const aborted=await client.abortDurable('run-a');
  const removed=await client.removeDurable('run-b');
  process.stdout.write(JSON.stringify({calls,aborted:{ok:aborted.ok,
    action:aborted.mutation.action},removed:{ok:removed.ok,
    reason:removed.reason,error:removed.error}}));
})().catch(error=>{console.error(error);process.exit(1);});
'''
    proc = subprocess.run(
        [node, '-e', script], cwd=ROOT,
        capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, (proc.stdout or '') + (proc.stderr or '')
    assert json.loads(proc.stdout) == {
        'calls': [['abort', 'run-a'], ['remove', 'run-b']],
        'aborted': {'ok': True, 'action': 'abort_run'},
        'removed': {
            'ok': False, 'reason': 'active', 'error': 'run is active',
        },
    }


def test_shared_task_request_client_unifies_durable_reads_and_creation():
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    script = r'''
const fs=require('fs');
global.window=global;
eval([
  'api/http-result.js','api/orchestration-http-contract.generated.js',
  'api/orchestration-response-contracts.js',
  'api/orchestration-client-methods.js',
  'api/orchestration-endpoint-transport.js',
  'api/orchestration-endpoints.js',
  'orchestration-wire-formats.generated.js',
  'orchestration-compatibility-defaults.generated.js',
  'orchestration-compatibility-contracts.js',
  'orchestration-wire-contract.js','orchestration-result.js',
  'orchestration-read-core.js',
  'orchestration-runtime-read.js',
  'orchestration-durable-run-snapshot.js',
  'orchestration-durable-list-read.js',
  'orchestration-durable-read.js',
  'orchestration-replay-read.js',
  'orchestration-http-read.js',
  'orchestration-request-failure.js','orchestration-api-request.js',
  'orchestration-request-contract.js',
  'orchestration-task-request.js',
].map(file=>fs.readFileSync('static/js/'+file,'utf8')).join('\n'));
const calls=[];
const api={
  taskListResult:async(status,orchId)=>{
    calls.push(['listResult',status,orchId]);
    return {ok:true,status:200,data:{ok:true,runs:[{id:'new'}],page:{
      limit:50,has_more:false,next_limit:null}}};
  },
  taskList:async()=>{calls.push(['listDirect']);return {ok:true,runs:[]};},
  taskGet:async id=>{
    calls.push(['get',id]);
    return {ok:false,status:404,data:{ok:false,error:'missing'}};
  },
  taskCreate:async()=>{
    calls.push(['create']);
    const error=new Error('network down');error.status=0;throw error;
  },
  taskEventsResult:async(id,cursor)=>{
    calls.push(['eventsResult',id,cursor]);
    return {ok:true,status:200,data:{
      format:'tofu.task-replay/v1',ok:true,status:'running',
      events:[{seq:3,type:'progress'}],next_cursor:4,done:false,
      caught_up:true,cursor:{requested:3,next:4,reset:false},
    }};
  },
};
(async()=>{
  const client=createOrchestrationTaskRequestClient({api:()=>api});
  const listed=await client.list('running','orch/one');
  const missing=await client.get('run/missing');
  const created=await client.create({nodes:[],edges:[]},'seed','orch/one');
  const events=await client.events('run/one',3);
  const legacyCalls=[];
  const legacy=createOrchestrationTaskRequestClient({api:()=>({
    taskList:async()=>{legacyCalls.push('list');return{
      ok:true,runs:[{id:'legacy'}],page:{
        limit:50,has_more:false,next_limit:null}};},
  })});
  const legacyList=await legacy.list();
  process.stdout.write(JSON.stringify({
    calls,legacyCalls,
    listed:{ok:listed.ok,ids:listed.runs.map(run=>run.id),
      method:listed.requestMethod,usedResult:listed.usedResultMethod},
    missing:{ok:missing.ok,notFound:missing.notFound,
      reason:missing.reason,status:missing.status},
    created:{ok:created.ok,reason:created.reason,
      cause:created.cause&&created.cause.message},
    events:{ok:events.ok,status:events.status,httpStatus:events.httpStatus,
      reason:events.reason,next:events.next_cursor,count:events.events.length,
      method:events.requestMethod,usedResult:events.usedResultMethod},
    legacy:{ok:legacyList.ok,ids:legacyList.runs.map(run=>run.id),
      method:legacyList.requestMethod,usedResult:legacyList.usedResultMethod},
  }));
})().catch(error=>{console.error(error);process.exit(1);});
'''
    proc = subprocess.run(
        [node, '-e', script], cwd=ROOT,
        capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, (proc.stdout or '') + (proc.stderr or '')
    assert json.loads(proc.stdout) == {
        'calls': [
            ['listResult', 'running', 'orch/one'],
            ['get', 'run/missing'],
            ['create'],
            ['eventsResult', 'run/one', 3],
        ],
        'legacyCalls': ['list'],
        'listed': {
            'ok': True, 'ids': ['new'], 'method': 'taskListResult',
            'usedResult': True,
        },
        'missing': {
            'ok': False, 'notFound': True, 'reason': 'not-found',
            'status': 404,
        },
        'created': {
            'ok': False, 'reason': 'transport-failed',
            'cause': 'network down',
        },
        'events': {
            'ok': True, 'status': 'running', 'httpStatus': 200,
            'reason': 'accepted', 'next': 4, 'count': 1,
            'method': 'taskEventsResult', 'usedResult': True,
        },
        'legacy': {
            'ok': True, 'ids': ['legacy'], 'method': 'taskList',
            'usedResult': False,
        },
    }


def test_api_task_reads_preserve_http_status_and_encode_filters():
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    script = r'''
const fs=require('fs');
global.window=global;
global.location={pathname:'/',protocol:'http:',host:'localhost'};
const calls=[];
function response(ok,status,body){return{
  ok,status,headers:{get:()=> 'application/json'},
  json:async()=>body,text:async()=>JSON.stringify(body),
};}
global.fetch=async(url,init)=>{
  calls.push({url,method:init.method});
  if(calls.length===1)return response(false,503,{
    ok:false,error:{message:'store offline'}});
  return response(false,404,{
    format:'tofu.task-replay/v1',ok:false,status:'',events:[],
    next_cursor:7,done:true,cursor:{requested:7,next:7,reset:false},
    error:'not_found',
  });
};
eval(fs.readFileSync('static/js/api.js','utf8'));
eval(fs.readFileSync('static/js/api/http-result.js','utf8'));
eval(fs.readFileSync('static/js/api/orchestration-http-contract.generated.js','utf8'));
eval(fs.readFileSync('static/js/api/orchestration-response-contracts.js','utf8'));
eval(fs.readFileSync('static/js/api/orchestration-client-methods.js','utf8'));
eval(fs.readFileSync('static/js/api/orchestration-endpoint-transport.js','utf8'));
eval(fs.readFileSync('static/js/api/orchestration-endpoints.js','utf8'));
eval(fs.readFileSync('static/js/api/orchestrations.js','utf8'));
(async()=>{
  const list=await Api.orchestrations.taskListResult('running','orch/one');
  const events=await Api.orchestrations.taskEventsResult('run/one',7);
  process.stdout.write(JSON.stringify({calls,list,events}));
})().catch(error=>{console.error(error);process.exit(1);});
'''
    proc = subprocess.run(
        [node, '-e', script], cwd=ROOT,
        capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, (proc.stdout or '') + (proc.stderr or '')
    output = json.loads(proc.stdout)
    assert output['calls'] == [
        {
            'url': '/api/v1/orchestrations/tasks?status=running&orch_id=orch%2Fone',
            'method': 'GET',
        },
        {
            'url': '/api/v1/orchestrations/tasks/run%2Fone/events?cursor=7',
            'method': 'GET',
        },
    ]
    assert output['list'] == {
        'ok': False, 'status': 503,
        'data': {'ok': False, 'error': {'message': 'store offline'}},
    }
    assert output['events']['ok'] is False
    assert output['events']['status'] == 404
    assert output['events']['data']['format'] == 'tofu.task-replay/v1'
    assert output['events']['data']['next_cursor'] == 7


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
