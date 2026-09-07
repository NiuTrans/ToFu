"""Task Mode run-list state coordination + bilingual localization (extraction-and-eval).

Two invariants, both proven by rendering the REAL extracted list view plus its
thin controller adapters under node with a stubbed ``document`` + ``Api``
AND the generated locale dictionaries consumed by the native i18n owner:

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

Request-client routing is exercised once through a shared native Vite graph;
this suite must not reconstruct the deleted per-file IIFE graph.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

from lib.orchestration.run_status import run_status_contract
from tests._runtime_sections import (
    native_module_graph,
    native_module_path,
    runtime_section_path,
)

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, '..'))
ORCHESTRATION_SOURCES = os.path.join(
    REPO_ROOT, 'frontend', 'src', 'features', 'orchestration')
I18N_LOCALE_FILES = {
    lang: os.path.join(REPO_ROOT, 'frontend', 'src', 'i18n', 'locales',
                       f'{lang}.json')
    for lang in ('zh', 'en')
}


def _orchestration_source(name: str) -> str:
    return _read(os.path.join(ORCHESTRATION_SOURCES, name))


def _native_orchestration_graph_path(
        logical_name: str, *owner_names: str) -> str:
    return native_module_graph([
        (
            f'.native/{logical_name}{"" if index == 0 else f"-{index}"}.js',
            os.path.join(ORCHESTRATION_SOURCES, owner_name),
        )
        for index, owner_name in enumerate(owner_names)
    ])


def _native_request_client_graph_path() -> str:
    logical_path = '.native/task-mode-request-clients.js'
    return native_module_graph([(
        logical_path,
        os.path.join(
            REPO_ROOT, 'frontend', 'src', 'features',
            'orchestration-core-owners.ts'),
    )])


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
    """Build a mutable-language harness from the production locale inputs.

    The classic ``static/js/i18n.js`` owner was deleted with the Vite cutover;
    keeping a fake copy just for tests would recreate the migration shadow.
    The proxy preserves this suite's load-bearing ``var entry = _i18n[key]``
    poison seam while sourcing every visible string from the real JSON files.
    """
    tables = {
        lang: json.loads(_read(path))
        for lang, path in I18N_LOCALE_FILES.items()
    }
    encoded = json.dumps(tables, ensure_ascii=False)
    return f'''
var _i18nLang = "zh";
var _i18nTables = {encoded};
var _i18n = new Proxy({{}}, {{get:function(_target,key){{
  var table = _i18nTables[_i18nLang] || _i18nTables.en || {{}};
  return table[key];
}}}});
function t(key, vars) {{
  var entry = _i18n[key];
  if (entry == null) return key;
  var text = String(entry);
  Object.keys(vars || {{}}).forEach(function(name) {{
    text = text.split('{{' + name + '}}').join(String(vars[name]));
  }});
  return text;
}}
'''


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

    request_runtime = _read(_native_orchestration_graph_path(
        'task-mode-list-state',
        'request-failure.ts',
        'task-mode-run-store.ts',
        'task-mode-list.ts',
        'task-mode-list-controller.ts',
    ))
    i18n_runtime = _extract_i18n_runtime()
    normalized_result = task_list_result
    if isinstance(task_list_result, dict) and task_list_result.get('ok') is True:
        page = task_list_result.get('page') or {}
        normalized_result = {
            'ok': True,
            'runs': task_list_result.get('runs') or [],
            'pageLimit': page.get('limit') or 0,
            'hasMore': page.get('has_more') is True,
            'nextLimit': page.get('next_limit'),
        }
    elif isinstance(task_list_result, dict):
        normalized_result = {
            **task_list_result,
            'ok': False,
            'reason': task_list_result.get('reason') or 'list-rejected',
        }

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
{request_runtime}
_i18nLang = {json.dumps(lang)};
function escapeHtml(s) {{ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){{
  return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]; }}); }}
var _lastHtml = '';
var _listEl = {{
  set innerHTML(v) {{ _lastHtml = v; }}, get innerHTML() {{ return _lastHtml; }},
  setAttribute: function() {{}},
  getAttribute: function() {{ return null; }},
  addEventListener: function() {{}},
  querySelector: function() {{ return null; }},
  querySelectorAll: function() {{ return []; }}
}};
var document = {{ getElementById: function(id) {{ return id === 'tmRunList' ? _listEl : null; }} }};
var store = createTaskModeRunStore();
var controller = null;
var view = createTaskModeRunListView({{
  document: document,
  hostId: 'tmRunList',
  listFocus: {{
    capture: function() {{ return null; }},
    restore: function() {{}},
    clear: function() {{}},
  }},
  translate: t,
  escape: escapeHtml,
  richCopy: function(value) {{ return String(value == null ? '' : value); }},
  icon: function(name) {{ return '<svg data-ico="' + name + '"></svg>'; }},
  isTerminal: function(value) {{ return !!(value && value.terminal === true); }},
  runContract: function() {{ return null; }},
  normalizeOutcome: function() {{ return null; }},
  outcomeMessage: function(_value, fallback) {{ return fallback; }},
  failureMessage: function(value, fallback) {{
    return orchestrationRequestFailureMessage(value, t, fallback || '');
  }},
  onOpen: function() {{}},
  onLoadMore: function() {{ return controller.loadMore(); }},
}});
var reader = {{
  read: async function() {{ return {json.dumps(normalized_result)}; }},
  report: function() {{ return false; }},
}};
controller = createTaskModeRunListController({{
  store: store,
  reader: reader,
  view: view,
  activeRunId: function() {{ return null; }},
  projectActionState: function() {{}},
  report: function() {{ return false; }},
}});

(async function() {{
  await controller.refresh();
  process.stdout.write(JSON.stringify({{
    html: _lastHtml,
    loadError: store.snapshot(null).loadError,
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
    src = _orchestration_source('task-mode.ts')
    fn = _extract_fn(src, '_tmIsTerminal')
    contract = _orchestration_source('contracts.ts')
    mutation_result = _orchestration_source('mutation-result.ts')
    run_status = _orchestration_source('run-status.ts')
    mutation = _read(os.path.join(
        REPO_ROOT, 'lib', 'orchestration', 'mutation_result.py'))
    assert 'orchestrationRunIsTerminal(' in fn
    assert '_orchAuthoring' not in fn
    assert "status === 'done'" not in fn
    assert "typeof run.terminal === 'boolean'" in run_status
    assert 'function isTerminalRunStatus(status)' not in contract
    assert "terminal: ['done', 'error', 'aborted']" not in contract
    assert 'resourceTerminal' in mutation_result
    assert 'mutation_payload_field_names()' in mutation
    assert "fields['resourceTerminal']: self.resource_terminal" in mutation


def test_task_mode_refreshes_the_backend_run_contract_on_open():
    src = _read(os.path.join(
        REPO_ROOT, 'frontend', 'src', 'features', 'orchestration',
        'task-mode.ts'))
    session = _read(os.path.join(
        REPO_ROOT, 'frontend', 'src', 'features', 'orchestration',
        'task-mode-contract-session.ts'))
    controller = _read(os.path.join(
        REPO_ROOT, 'frontend', 'src', 'features', 'orchestration',
        'task-mode-contract-controller.ts'))
    opened = _extract_fn(src, 'openTaskMode')
    refresh = _extract_fn(src, '_tmRefreshAuthoringContract')
    closed = _extract_fn(src, '_tmAfterClose')
    assert 'await _tmRefreshAuthoringContract()' in opened
    assert opened.index('await _tmRefreshAuthoringContract()') < \
        opened.index('const refresh = _tmRefreshRuns()')
    assert 'const openOwner = shell.captureOpen()' in opened
    assert 'shell.ownsOpen(openOwner)' in opened
    assert '_tmEnsureContractController().refresh()' in refresh
    assert 'await session.refresh(' in controller
    assert 'capability.refreshAuthoringContract as' in controller
    assert 'const contract = await' in controller
    assert 'owner !== generation' in session
    assert 'options.onAdopt?.(result.contracts)' in controller
    assert '_tmEnsureContractController().invalidate()' in closed
    assert 'runListController?.invalidate()' in closed


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

    inspector_graph = _read(_native_orchestration_graph_path(
        'task-mode-inspector', 'task-mode-inspector.ts'))
    i18n_runtime = _extract_i18n_runtime()
    if poison == 'i18n':
        i18n_runtime = i18n_runtime.replace('var entry = _i18n[key];', 'var entry = null;')

    harness = f'''
{i18n_runtime}
_i18nLang = {json.dumps(lang)};
function _tmIco(name) {{ return '<svg data-ico="' + name + '"></svg>'; }}
function escapeHtml(s) {{ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){{
  return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]; }}); }}
{inspector_graph}
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
    root_controller = _read(_native_orchestration_graph_path(
        'task-mode-root-controller', 'task-mode-root-controller.ts'))
    script = f'''
global.window=global;
{root_controller}
var reports=[];
var services={{
  reportError:function(){{reports.push(Array.from(arguments));}},
  api:function(){{}},studio:function(){{}},toast:function(){{}},
}};
var root=createTaskModeRootController({{
  services:function(){{return services;}},
  contractSession:{{}},session:{{}},runStore:{{}},
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
    orchestration_sources = os.path.join(
        REPO_ROOT, 'frontend', 'src', 'features', 'orchestration')
    def source(name: str) -> str:
        return _read(os.path.join(orchestration_sources, name))

    src = source('task-mode.ts')
    dialog = source('dialog.ts')
    task_shell = source('task-mode-shell.ts')
    task_shell_template = source('task-mode-shell-template.ts')
    run_list = source('task-mode-list.ts')
    run_title_view = source('task-mode-run-title-view.ts')
    run_view = source('task-mode-run-view.ts')
    node_presentation = source('task-mode-node-presentation.ts')
    timeline = source('task-mode-timeline.ts')
    graph_projection = source('task-mode-graph-projection.ts')
    graph = source('task-mode-graph.ts')
    inspector = source('task-mode-inspector.ts')
    gate_view = source('task-mode-gate-view.ts')
    gate_presentation = source('task-mode-gate-presentation.ts')
    gate_interaction = source('human-gate-interaction.ts')
    mutation_request = _read(os.path.join(
        orchestration_sources, 'mutation-request.ts'))
    task_actions = _read(os.path.join(
        orchestration_sources, 'task-mode-actions.ts'))
    task_commands = _read(os.path.join(
        orchestration_sources, 'task-mode-command-controller.ts'))
    task_commands += _read(os.path.join(
        orchestration_sources, 'task-mode-run-command-controller.ts'))
    list_controller = _read(os.path.join(
        REPO_ROOT, 'frontend', 'src', 'features', 'orchestration',
        'task-mode-list-controller.ts'))
    css = _read(os.path.join(
        REPO_ROOT, 'frontend', 'src', 'features', 'orchestration',
        'task-mode.css'))
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
    assert "target?.closest?.('[data-tm-action]')" in task_shell
    assert 'await Api.orchestrations' not in src
    assert "await reader.read('list', ['', '', requestedLimit])" \
        in list_controller
    assert 'return _tmEnsureRunListController().refresh()' in src
    assert '_tmApiCall' not in src
    assert 'createOrchestrationMutationRequestClient' in mutation_request
    assert "'approveGate', [requestId, approved]" in task_actions
    assert "'inputGate', [requestId, value]" in task_actions
    assert "'abortDurable', [runId]" in task_actions
    assert "'removeDurable', [runId]" in task_actions
    assert "invoke(_tmEnsureCommands(), 'approveGate', requestId, approved)" \
        in src
    assert "invoke(_tmEnsureCommands(), 'inputGate', requestId, input)" in src
    assert "invoke(_tmEnsureCommands(), 'abortRun', runId)" in src
    assert "invoke(_tmEnsureCommands(), 'deleteRun', runId)" in src
    assert "return gate('inputGate', requestId, value, '')" in task_commands
    assert "ports.call('reconcileRun', outcome.mutation, runId)" \
        in task_commands
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
    assert "host()?.setAttribute('aria-busy'" in timeline
    assert "overlay.addEventListener('keydown', keyDown as EventListener)" \
        in task_shell
    assert 'focusManager.trapTab(event, dialog)' in task_shell
    assert 'focusablePrevious.focus()' in dialog
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
    src = _orchestration_source('task-mode.ts')
    run_list = _orchestration_source('task-mode-list.ts')
    list_presentation = _orchestration_source(
        'task-mode-list-presentation.ts')
    i18n = '\n'.join(_read(path) for path in I18N_LOCALE_FILES.values())
    event_format = _orchestration_source('event-format.ts')
    run_time = _orchestration_source('task-mode-run-time.ts')
    for literal in (
        "' approved'", "' rejected'", "' answered'", "' agents, '",
        'Select a run to view its timeline.', "'(unnamed flow)'",
    ):
        assert literal not in src
    for key in (
        'tm.unnamedFlow', 'tm.ago.seconds', 'tm.ago.minutes',
        'tm.ago.hours', 'tm.ago.days', 'orch.ev.gateApproved',
        'orch.ev.gateRejected', 'orch.ev.gateAnswered',
        'orch.ev.completeSummary', 'tm.status.done',
    ):
        assert f'"{key}"' in i18n
        assert (key in src or key in run_list or key in list_presentation
                or key in run_time
                or key in event_format
                or key == 'tm.status.done')


def test_published_task_mode_status_vocabulary_has_bilingual_copy():
    contract = run_status_contract()
    required_keys = {
        *(f'tm.status.{status}' for status in contract['statuses']),
        'tm.status.incomplete',
        *(f'tm.statusCategory.{category}'
          for category in set(contract['categories'].values())),
    }
    for language, path in I18N_LOCALE_FILES.items():
        table = json.loads(_read(path))
        missing = sorted(key for key in required_keys if not table.get(key))
        assert not missing, (
            f'{language} Task Mode status copy missing for {missing}')


@pytest.mark.parametrize(
    ('action_result', 'gate_kept', 'toast_error'),
    [
        ({
            'ok': False,
            'message': 'transport unavailable',
        }, True, True),
        ({
            'mutation': {
                'ok': True,
                'reason': 'accepted',
                'targetExists': True,
            },
        }, False, False),
        ({
            'ok': False,
            'message': 'gate no longer exists',
            'mutation': {
                'ok': False,
                'reason': 'not_found',
                'targetExists': False,
            },
        }, False, True),
    ],
)
def test_gate_is_removed_only_after_backend_confirmation(
        action_result, gate_kept, toast_error):
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    controller_path = _native_orchestration_graph_path(
        'task-mode-command-controller', 'task-mode-command-controller.ts')
    harness = f'''
const fs=require('fs');
global.window=global;
eval(fs.readFileSync({json.dumps(controller_path)},'utf8'));
const gates={{gate_1:{{mode:'approve'}}}};
let renders=0;
const toasts=[];
const actionResult={json.dumps(action_result)};
const controller=createTaskModeCommandController({{
  actions:{{
    approveGate:async()=>actionResult,
    failureMessage:(result,fallback)=>result?.message||fallback,
  }},
  translate:key=>key,
  dismissGate:id=>{{
    if(!gates[id])return false;
    delete gates[id];
    renders+=1;
    return true;
  }},
  toast:(message,isError)=>toasts.push([message,isError]),
}});
(async()=>{{
  await controller.approveGate('gate_1',true);
  process.stdout.write(JSON.stringify({{
    kept:!!gates.gate_1,renders:renders,toasts:toasts,
  }}));
}})().catch(error=>{{console.error(error);process.exit(1);}});
'''
    proc = subprocess.run(
        [node, '-e', harness], cwd=REPO_ROOT,
        capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result['kept'] is gate_kept
    assert result['renders'] == (0 if gate_kept else 1)
    assert bool(result['toasts'][-1][1]) is toast_error


def test_task_request_clients_share_the_native_vite_contract_graph():
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    graph_path = _native_request_client_graph_path()
    script = r'''
const fs=require('fs');
global.window=global;
eval(fs.readFileSync(GRAPH_PATH,'utf8'));
const calls=[];
const response=(ok,status,data)=>({ok,status,json:async()=>data});
const inspection={
  format:'tofu.orchestration.inspection/v1',ok:true,
  errors:[],warnings:[],diagnostics:[],contract:{
    schema:'tofu.orchestration/v1',projection:'flow',initialPhase:'start',
    nodes:0,edges:0,
  },
};
const mutation=(ok,action,reason,targetId)=>({
  format:orchestrationWireFormat('mutation'),ok,action,reason,
  target_id:targetId,resource_status:'',resource_terminal:null,
  target_exists:reason==='not_found'?false:true,
  retryable:false,reconcile_required:!ok,
});
const api={
  taskListResult:async(status,orchestrationId,limit)=>{
    calls.push(['list',status,orchestrationId,limit]);
    return response(true,200,{ok:true,runs:[],page:{
      limit,has_more:false,next_limit:null,
    }});
  },
  taskGet:async id=>{
    calls.push(['get',id]);
    return response(false,404,{ok:false,error:'missing'});
  },
  taskCreate:async(definition,input,storedId,originId)=>{
    calls.push(['create',Boolean(definition),input,storedId||null,originId||null]);
    return response(true,201,{
      ok:true,run_id:'run-new',start:{
        format:orchestrationWireFormat('runtime-start'),
        kind:'durable',id:'run-new',
      },
      definitionSource:'inline',inspection,warnings:[],
      contract:inspection.contract,
    });
  },
  taskEventsResult:async(id,cursor)=>{
    calls.push(['events',id,cursor]);
    return response(true,200,{
      format:orchestrationWireFormat('task-replay'),ok:true,
      status:'running',events:[],next_cursor:cursor+1,done:false,
      caught_up:true,cursor:{requested:cursor,next:cursor+1,reset:false},
    });
  },
  taskAbort:async id=>{
    calls.push(['abort',id]);
    return response(true,200,{ok:true,mutation:
      mutation(true,'abort_run','accepted',id)});
  },
  taskRemove:async id=>{
    calls.push(['remove',id]);
    return response(false,409,{ok:false,error:'run is active',mutation:
      mutation(false,'delete_run','active',id)});
  },
};
(async()=>{
  const tasks=createOrchestrationTaskRequestClient({api:()=>api});
  const mutations=createOrchestrationMutationRequestClient({api:()=>api});
  const listed=await tasks.list('running','orch/one',25);
  const missing=await tasks.get('run/missing');
  const created=await tasks.create({nodes:[],edges:[]},'seed','orch/one');
  const events=await tasks.events('run/one',3);
  const aborted=await mutations.abortDurable('run/a');
  const removed=await mutations.removeDurable('run/b');
  process.stdout.write(JSON.stringify({
    calls,
    listed:{ok:listed.ok,pageLimit:listed.pageLimit,
      method:listed.requestMethod},
    missing:{ok:missing.ok,notFound:missing.notFound,status:missing.status},
    created:{ok:created.ok,runId:created.runId,method:created.requestMethod},
    events:{ok:events.ok,next:events.next_cursor,method:events.requestMethod},
    aborted:{ok:aborted.ok,action:aborted.mutation.action,
      targetId:aborted.mutation.targetId},
    removed:{ok:removed.ok,reason:removed.reason,error:removed.error},
  }));
})().catch(error=>{console.error(error);process.exit(1);});
'''.replace('GRAPH_PATH', json.dumps(graph_path))
    proc = subprocess.run(
        [node, '-e', script], cwd=REPO_ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (proc.stdout or '') + (proc.stderr or '')
    result = json.loads(proc.stdout)
    assert result['calls'] == [
        ['list', 'running', 'orch/one', 25],
        ['get', 'run/missing'],
        ['create', True, 'seed', None, 'orch/one'],
        ['events', 'run/one', 3],
        ['abort', 'run/a'],
        ['remove', 'run/b'],
    ]
    assert result['listed'] == {
        'ok': True, 'pageLimit': 25, 'method': 'taskListResult',
    }
    assert result['missing'] == {
        'ok': False, 'notFound': True, 'status': 404,
    }
    assert result['created'] == {
        'ok': True, 'runId': 'run-new', 'method': 'taskCreate',
    }
    assert result['events'] == {
        'ok': True, 'next': 4, 'method': 'taskEventsResult',
    }
    assert result['aborted'] == {
        'ok': True, 'action': 'abort_run', 'targetId': 'run/a',
    }
    assert result['removed'] == {
        'ok': False, 'reason': 'active', 'error': 'run is active',
    }


def test_api_task_reads_preserve_http_status_and_encode_filters():
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    api_sources = [native_module_path(
        '.native/http-result-for-task-mode.js',
        'frontend/src/core/http-result.ts',
    )] + [runtime_section_path(name) for name in (
        'api.js',
        'api/orchestration-http-contract.generated.js',
        'api/orchestration-response-contracts.js',
        'api/orchestration-client-methods.js',
        'api/orchestration-endpoint-transport.js',
        'api/orchestration-endpoints.js',
        'api/orchestrations.js',
    )]
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
eval(API_SOURCES.map(path=>fs.readFileSync(path,'utf8')).join('\n'));
(async()=>{
  const list=await Api.orchestrations.taskListResult('running','orch/one');
  const events=await Api.orchestrations.taskEventsResult('run/one',7);
  process.stdout.write(JSON.stringify({calls,list,events}));
})().catch(error=>{console.error(error);process.exit(1);});
'''.replace('API_SOURCES', json.dumps(api_sources))
    proc = subprocess.run(
        [node, '-e', script], cwd=REPO_ROOT,
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
