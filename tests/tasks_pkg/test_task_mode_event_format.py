"""Task Mode timeline event-format coverage for goal-run envelope events.

Goal runs emit two envelope lifecycle events — ``goal_run_started`` and
``goal_run_transition`` (pinned server-side in tests/goals/test_goal_run_sidecar.py).
The shared timeline formatter used to fall through to the generic
``orch.ev.unknown`` warning for both, so a perfectly healthy goal run showed
「未识别的运行事件」 error lines in the Task Mode timeline. These pins prove
the two envelope types render as localized status lines (zh + en) while
genuinely unknown types still hit the warning path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import tempfile

import pytest

from tests._runtime_sections import native_module_graph

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
ORCHESTRATION_SOURCES = os.path.join(
    REPO_ROOT, 'frontend', 'src', 'features', 'orchestration')
I18N_LOCALE_FILES = {
    lang: os.path.join(REPO_ROOT, 'frontend', 'src', 'i18n', 'locales',
                       f'{lang}.json')
    for lang in ('zh', 'en')
}
EVENT_FORMAT_OWNERS = [
    'registry.ts',
    'contracts.ts',
    'result.ts',
    'outcome-result.ts',
    'event-policy.ts',
    'event-format.ts',
]


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _format_lines(events: list[dict], lang: str) -> list[list[dict]]:
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for extraction-and-eval')
    runtime = _read(native_module_graph([
        (
            f'.native/task-mode-event-format{"" if index == 0 else f"-{index}"}.js',
            os.path.join(ORCHESTRATION_SOURCES, owner_name),
        )
        for index, owner_name in enumerate(EVENT_FORMAT_OWNERS)
    ]))
    tables = {
        lang_key: json.loads(_read(path))
        for lang_key, path in I18N_LOCALE_FILES.items()
    }
    harness = f'''
var _i18nTables = {json.dumps(tables, ensure_ascii=False)};
var _i18nLang = {json.dumps(lang)};
function t(key, vars) {{
  var entry = (_i18nTables[_i18nLang] || {{}})[key];
  if (entry == null) return key;
  var text = String(entry);
  Object.keys(vars || {{}}).forEach(function(name) {{
    text = text.split('{{' + name + '}}').join(String(vars[name]));
  }});
  return text;
}}
{runtime}
function escapeHtml(s) {{ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){{
  return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]; }}); }}
var events = {json.dumps(events, ensure_ascii=False)};
var out = events.map(function(ev) {{
  return formatOrchestrationEventLines(ev, {{
    escape: escapeHtml,
    translate: t,
    icon: function(name) {{ return '<i data-ico="' + name + '"></i>'; }},
  }});
}});
console.log(JSON.stringify(out));
'''
    harness_path = None
    try:
        with tempfile.NamedTemporaryFile(
                'w', suffix='.js', delete=False, encoding='utf-8') as f:
            f.write(harness)
            harness_path = f.name
        proc = subprocess.run(
            [node, harness_path], capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)
    finally:
        if harness_path:
            os.unlink(harness_path)


def test_goal_run_envelope_events_render_localized_timeline_lines_zh():
    (started, completed, failed, cancelled, mystery) = _format_lines([
        {'type': 'goal_run_started', 'status': 'active', 'reason': 'started',
         'objective': '全力看看能不能加速tofu-db上线'},
        {'type': 'goal_run_transition', 'status': 'completed',
         'reason': 'completed'},
        {'type': 'goal_run_transition', 'status': 'failed', 'reason': 'failed'},
        {'type': 'goal_run_transition', 'status': 'cancelled'},
        {'type': 'mystery_event'},
    ], 'zh')

    assert len(started) == 1
    assert '目标运行已开始' in started[0]['html']
    assert '全力看看能不能加速tofu-db上线' in started[0]['html']
    assert started[0]['className'] == ''

    assert len(completed) == 1
    assert '运行结束：完成' in completed[0]['html']
    assert completed[0]['className'] == 'is-done'

    assert len(failed) == 1
    assert '运行结束：失败' in failed[0]['html']
    assert failed[0]['className'] == 'is-err'

    assert len(cancelled) == 1
    assert '运行结束：已中止' in cancelled[0]['html']
    assert cancelled[0]['className'] == 'is-warn'

    # Genuinely unknown types must still surface the warning path.
    assert len(mystery) == 1
    assert '未识别的运行事件' in mystery[0]['html']
    assert mystery[0]['className'] == 'is-err'


def test_goal_run_transition_renders_english_copy():
    (completed,) = _format_lines([
        {'type': 'goal_run_transition', 'status': 'completed'},
    ], 'en')
    assert len(completed) == 1
    assert 'Run ended: Completed' in completed[0]['html']
    assert completed[0]['className'] == 'is-done'


def test_goal_run_event_keys_exist_in_both_locales():
    for path in I18N_LOCALE_FILES.values():
        table = json.loads(_read(path))
        assert 'orch.ev.goalStarted' in table
        assert 'orch.ev.goalTransition' in table
