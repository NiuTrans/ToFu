"""Behavior contract for typed lazy My Day task mutations."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
OWNER = ROOT / 'frontend/src/features/myday/task-actions.ts'
OWNER_BUNDLE = native_module_path('.native/myday-task-actions.js', OWNER)
LAUNCHER = ROOT / 'frontend/src/features/myday/quick-action-launcher.ts'
LAUNCHER_BUNDLE = native_module_path('.native/myday-quick-action-launcher.js', LAUNCHER)


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_task_mutations_preserve_authority_rollback_and_cache_boundaries():
    harness = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[1], 'utf8'));
const checks = [];
const check = (name, value) => checks.push((value ? 'PASS ' : 'FAIL ') + name);
const report = {
  today_todos: [{ id: 'inherited', done: false }, { id: 'remove-inherited', done: false }],
  tomorrow: [{ id: 'todo', done: false }, { id: 'remove', done: false }],
  streams: [{ id: 'stream', status: 'in_progress', remaining: 'two steps' }],
};
const calls = { render: 0, calendar: 0, persist: [], accepted: [], warnings: [], api: [] };
const input = { value: '  write tests  ' };
let selectedReport = report;
let behavior = {};
const response = (ok, body = {}, status = ok ? 200 : 500) => ({
  ok, status, json: async () => body,
});
const api = {};
for (const name of ['inheritedTodoToggle', 'inheritedTodoDelete', 'todoToggle',
  'taskDelete', 'taskStatus', 'taskCreate']) {
  api[name] = async (payload) => {
    calls.api.push([name, payload]);
    const value = behavior[name];
    if (value instanceof Error) throw value;
    return typeof value === 'function' ? value(payload) : value;
  };
}
const actions = createMyDayTaskActions({
  api,
  selectedReport: () => ({ date: '2026-08-28', report: selectedReport }),
  acceptAuthoritativeReport: (date, value) => {
    calls.accepted.push([date, value]); selectedReport = value;
  },
  persistReport: (date, value) => calls.persist.push([date, value]),
  renderReport: () => { calls.render += 1; },
  renderCalendar: () => { calls.calendar += 1; },
  taskInput: () => input,
  warn: (message, detail) => calls.warnings.push([message, detail]),
});

(async () => {
  let resolveInherited;
  behavior.inheritedTodoToggle = () => new Promise((resolve) => {
    resolveInherited = resolve;
  });
  const inherited = actions.toggleInheritedTodo('inherited', '2026-08-27');
  await Promise.resolve();
  check('inherited_todo_updates_optimistically',
    report.today_todos[0].done === true && calls.render === 1);
  resolveInherited(response(false));
  await inherited;
  check('inherited_todo_rolls_back_on_rejection',
    report.today_todos[0].done === false && calls.render === 2);

  behavior.todoToggle = response(true);
  await actions.toggleTodo('todo');
  check('todo_success_persists_selected_report',
    report.tomorrow[0].done === true && calls.persist.length === 1
      && calls.api.at(-1)[1].date === '2026-08-28');

  behavior.taskStatus = response(true, { ok: true, status: 'done' });
  await actions.toggleStreamStatus('stream');
  check('server_owns_stream_cycle_result',
    report.streams[0].status === 'done' && report.streams[0].remaining === null
      && report.streams[0]._manual === true && calls.persist.length === 2);

  behavior.taskDelete = response(false);
  await actions.deleteTodo('remove');
  check('delete_rejection_restores_original_position',
    report.tomorrow[1].id === 'remove');

  behavior.inheritedTodoDelete = response(true);
  await actions.deleteInheritedTodo('remove-inherited', '2026-08-26');
  check('accepted_inherited_delete_stays_removed',
    report.today_todos.every((item) => item.id !== 'remove-inherited'));

  const authoritative = { tomorrow: [{ id: 'server-task', done: false }] };
  behavior.taskCreate = response(true, { report: authoritative });
  await actions.addTodo();
  check('add_uses_trimmed_text_and_clears_input',
    input.value === '' && calls.api.at(-1)[1].task === 'write tests');
  check('add_adopts_authoritative_report',
    calls.accepted.length === 1 && selectedReport === authoritative);

  behavior.todoToggle = new Error('offline');
  selectedReport = report;
  await actions.toggleTodo('todo');
  check('transport_failure_rolls_back_and_is_visible',
    report.tomorrow[0].done === true
      && calls.warnings.at(-1)[1].message === 'offline');
  check('calendar_reconciliation_is_bounded_to_calendar_mutations',
    calls.calendar === 4);
  check('all_mutation_payloads_keep_explicit_period_identity',
    calls.api.filter((row) => ['todoToggle','taskDelete','taskStatus','taskCreate']
      .includes(row[0])).every((row) => row[1].date === '2026-08-28'));

  console.log(checks.join('\n'));
  if (checks.some((line) => line.startsWith('FAIL'))) process.exitCode = 1;
})().catch((error) => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        ['node', '-e', harness, OWNER_BUNDLE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = (result.stdout or '') + (result.stderr or '')
    assert result.returncode == 0, output
    assert output.count('PASS') == 11, output


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_quick_action_launcher_preserves_prefill_order_and_tool_intent():
    harness = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[1], 'utf8'));
const checks = [];
const check = (name, value) => checks.push((value ? 'PASS ' : 'FAIL ') + name);
const composer = {
  value: '', scrollHeight: 42, style: { height: '' }, focused: 0,
  focus() { this.focused += 1; },
};
const report = {
  tomorrow: [{ id: 'todo', text: 'fallback', quick_action: {
    prefill: 'PREFILLED', searchMode: 'multi', fetchEnabled: true,
    codeExecEnabled: true, browserEnabled: false,
  }}],
  today_todos: [{ id: 'inherited', text: 'INHERITED', quick_action: {} }],
  unfinished: [{ id: 'unfinished', text: 'UNFINISHED', quick_action: {
    searchMode: 'off',
  }}],
};
const calls = [];
const launcher = createMyDayQuickActionLauncher({
  selectedReport: () => ({ date: '2026-08-28', report }),
  composerInput: () => composer,
  closeReport: () => calls.push(['close']),
  createConversation: () => calls.push(['newChat', composer.value]),
  applySearchMode: (value) => calls.push(['search', value]),
  applyFetchEnabled: (value) => calls.push(['fetch', value]),
  applyCodeExecEnabled: (value) => calls.push(['code', value]),
  applyBrowserEnabled: (value) => calls.push(['browser', value]),
  updateSendButton: () => calls.push(['send']),
});

launcher.startTodo('todo');
check('prefill_happens_before_new_chat',
  calls[0][0] === 'close' && calls[1][0] === 'newChat'
    && calls[1][1] === 'PREFILLED');
check('tool_intent_is_forwarded_explicitly',
  JSON.stringify(calls.slice(2, 6)) === JSON.stringify([
    ['search','multi'], ['fetch',true], ['code',true], ['browser',false],
  ]));
check('composer_is_resized_focused_and_reconciled',
  composer.style.height === '42px' && composer.focused === 1
    && calls.at(-1)[0] === 'send');

calls.length = 0; composer.value = '';
launcher.startInheritedTodo('inherited', '2026-08-27');
check('missing_prefill_falls_back_to_task_text',
  calls[1][1] === 'INHERITED' && calls[2][1] === 'off');

calls.length = 0; composer.value = '';
launcher.startUnfinishedTodo(0);
check('unfinished_selection_uses_stable_indexed_item',
  calls[1][1] === 'UNFINISHED');

calls.length = 0;
launcher.startTodo('missing');
launcher.startUnfinishedTodo(-1);
check('missing_or_invalid_selection_is_a_noop', calls.length === 0);

console.log(checks.join('\n'));
if (checks.some((line) => line.startsWith('FAIL'))) process.exitCode = 1;
"""
    result = subprocess.run(
        ['node', '-e', harness, LAUNCHER_BUNDLE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = (result.stdout or '') + (result.stderr or '')
    assert result.returncode == 0, output
    assert output.count('PASS') == 6, output
