"""Task Mode run-list state coordination + bilingual localization (extraction-and-eval).

Two invariants, both proven by rendering the REAL functions (brace-extracted
from static/js/task-mode.js) under node with a stubbed ``document`` + ``Api``
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
    fns = [
        '_tmT', '_tmEsc', '_tmAgo', '_tmIsTerminal', '_tmDuration',
        '_tmStatusChip', '_tmRenderRunList', '_tmRefreshRuns',
    ]
    extracted = '\n'.join(_extract_fn(src, f) for f in fns)
    i18n_runtime = _extract_i18n_runtime()

    if poison == 'error_branch':
        extracted = extracted.replace(
            'if (res === null) { _tmLoadError = true; _tmRenderRunList(); return; }',
            'if (res === null) { _tmLoadError = false; _tmRuns = []; _tmRenderRunList(); return; }')
        assert '_tmLoadError = false; _tmRuns = []' in extracted, 'poison did not apply'
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
var _tmRuns = [];
var _tmRunId = null;
var _tmLoadError = false;
// stubs
function _tmIco(name) {{ return '<svg data-ico="' + name + '"></svg>'; }}
function escapeHtml(s) {{ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){{
  return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]; }}); }}
var _lastHtml = '';
var _listEl = {{ set innerHTML(v) {{ _lastHtml = v; }}, get innerHTML() {{ return _lastHtml; }} }};
var document = {{ getElementById: function(id) {{ return id === 'tmRunList' ? _listEl : null; }} }};
var Api = {{ orchestrations: {{ taskList: async function() {{ return {json.dumps(task_list_result)}; }} }} }};

{extracted}

(async function() {{
  await _tmRefreshRuns();
  process.stdout.write(JSON.stringify({{ html: _lastHtml, loadError: _tmLoadError }}));
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


def test_empty_account_shows_studio_cta():
    """A genuinely-empty account renders the onboarding CTA that bridges to the
    Studio — the actionable empty state."""
    r = _run(task_list_result={'ok': True, 'runs': []}, lang='en')
    assert r['loadError'] is False
    assert 'No task runs yet' in r['html']
    assert '_tmOpenStudio()' in r['html']
    assert 'tm-state-err' not in r['html']


def test_runs_render_with_chip_and_duration():
    """Runs render as rows with a status chip and a duration label."""
    now = 1_000_000_000_000
    runs = [
        {'id': 'run_a', 'name': 'Résumé screen', 'status': 'done',
         'created_at': now, 'finished_at': now + 42_000, 'updated_at': now + 42_000},
        {'id': 'run_b', 'name': 'Live job', 'status': 'running',
         'created_at': now, 'updated_at': now},
    ]
    r = _run(task_list_result={'ok': True, 'runs': runs}, lang='en')
    assert r['loadError'] is False
    assert 'Résumé screen' in r['html']
    assert 'tm-chip-done' in r['html']
    assert 'tm-chip-running' in r['html']
    assert '42s' in r['html']
    assert 'tm-run-live' in r['html']   # the running row is flagged live


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
    r = _run(task_list_result={'ok': True, 'runs': []}, lang=lang)
    assert _EXPECT['empty'][lang] in r['html'], f'{lang} empty title missing'
    assert _EXPECT['openStudio'][lang] in r['html'], f'{lang} CTA label missing'


@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_status_chip_localized(lang):
    now = 1_000_000_000_000
    runs = [
        {'id': 'a', 'name': 'x', 'status': 'done',
         'created_at': now, 'finished_at': now + 1000, 'updated_at': now + 1000},
        {'id': 'b', 'name': 'y', 'status': 'running', 'created_at': now, 'updated_at': now},
    ]
    r = _run(task_list_result={'ok': True, 'runs': runs}, lang=lang)
    # status shown as a LOCALIZED label; raw status stays as the CSS class.
    assert _EXPECT['statusDone'][lang] in r['html']
    assert _EXPECT['statusRunning'][lang] in r['html']
    assert 'tm-chip-done' in r['html'] and 'tm-chip-running' in r['html']


def test_zh_render_has_no_english_leak():
    """The zh render of every state card must carry NONE of the old bare-English
    literals — proving nothing was left un-localized."""
    for result in (None, {'ok': True, 'runs': []}):
        r = _run(task_list_result=result, lang='zh')
        for leak in _ENGLISH_LEAKS:
            assert leak not in r['html'], f'English leak in zh render: {leak!r}'


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
    r = _run(task_list_result={'ok': True, 'runs': []}, poison='i18n', lang='zh')
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

    src = _read(TM_JS)
    extracted = '\n'.join(_extract_fn(src, f) for f in ('_tmT', '_tmEsc', '_tmGateCard'))
    i18n_runtime = _extract_i18n_runtime()
    if poison == 'i18n':
        i18n_runtime = i18n_runtime.replace('var entry = _i18n[key];', 'var entry = null;')

    harness = f'''
{i18n_runtime}
_i18nLang = {json.dumps(lang)};
function _tmIco(name) {{ return '<svg data-ico="' + name + '"></svg>'; }}
function escapeHtml(s) {{ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){{
  return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]; }}); }}
{extracted}
process.stdout.write(_tmGateCard({json.dumps(ev)}));
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
    'approvePrompt': {'zh': '批准以继续？', 'en': 'Approve to continue?'},
    'inputPrompt': {'zh': '请输入？', 'en': 'Your input?'},
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
    # wiring preserved regardless of language
    assert '_tmHumanApprove(' in html


@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_gate_card_input_localized(lang):
    """The INPUT gate renders its Send button + placeholder + default prompt
    localized."""
    html = _run_gate(ev={'request_id': 'r2', 'mode': 'input'}, lang=lang)
    assert _GATE_EXPECT['send'][lang] in html, f'{lang} Send missing'
    assert _GATE_EXPECT['inputPrompt'][lang] in html
    assert '_tmHumanInput(' in html


def test_gate_card_zh_no_english_leak():
    """The zh gate card carries none of the old bare-English gate literals in its
    VISIBLE text. (JS handler names like ``_tmHumanApprove`` legitimately contain
    'Approve' inside onclick= attributes — strip attribute values first so the
    guard checks rendered text, not code.)"""
    attr = re.compile(r'\b(?:onclick|id|class|data-ico)="[^"]*"')
    for mode in ('approve', 'input'):
        html = _run_gate(ev={'request_id': 'r', 'mode': mode}, lang='zh')
        visible = attr.sub('', html)
        for leak in ('Human gate', 'Approve', 'Reject', 'Send',
                     'Approve to continue?', 'Your input?', 'Type your answer'):
            assert leak not in visible, f'English leak in zh {mode} gate: {leak!r}'


def test_nc_poisoned_i18n_drops_gate_labels():
    """POISONED-NC for the interactive surface: neuter t() → the zh gate labels
    CANNOT appear and the raw keys leak. Proves the gate buttons the operator
    clicks flow through t(), not hardcoded strings."""
    html = _run_gate(ev={'request_id': 'r', 'mode': 'approve'}, lang='zh', poison='i18n')
    assert _GATE_EXPECT['approve']['zh'] not in html, 'zh Approve survived a neutered t()'
    assert _GATE_EXPECT['reject']['zh'] not in html, 'zh Reject survived a neutered t()'
    assert 'tm.gate.approve' in html, 'expected the raw gate key to leak through'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
