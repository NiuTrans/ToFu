"""My Day month overview: one in-flight calendar load, never a request storm.

Measured 2026-11-08 17:39-18:00 ([DB-MEASURE-LOAD]): 249 concurrent
``/daily-report/calendar`` cost scans left the API host mostly unresponsive for
about 20 minutes. Cause in the retained panel: ``_mydayRenderCalendar`` always
started ``_mydayFetchMonthOverview``, whose IndexedDB instant paint rendered the
calendar again before its own network request, and the 15 s freshness stamp
was only written after the first response. While the server was slow, every
IndexedDB read issued one more cold scan.

Client contract: one in-flight month load per month and page, rendering never
fetches. The server side (concurrent cold loads of one owner's month share one
computation) is owned by lib/daily_report/calendar_overview.py and its
tests/daily_report/test_daily_report_calendar_flight.py.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

_HARNESS = r"""
const fs = require('fs');
global.window = global;
const out = [];
const check = (name, cond) => out.push((cond ? 'PASS ' : 'FAIL ') + name);
const els = {};
const element = () => {
  const el = { _html: '', textContent: '', style: {}, children: [],
    classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} },
    querySelectorAll: () => [], querySelector: () => null, appendChild() {}, replaceChildren() {},
    addEventListener() {}, setAttribute() {}, removeAttribute() {} };
  Object.defineProperty(el, 'innerHTML', { get() { return el._html; }, set(v) { el._html = v; } });
  return el;
};
global.document = {
  getElementById: (id) => (els[id] = els[id] || element()),
  querySelectorAll: () => [], createElement: () => element(), addEventListener() {},
  readyState: 'complete',
};
global.requestAnimationFrame = (fn) => { fn(); return 0; };
global.t = (key) => key;
global.escapeHtml = (value) => String(value == null ? '' : value);
global.MYDAY_PRESENTATION_ASSETS = new Proxy({}, { get: () => '' });
let calendarCalls = 0;
const pending = [];
global.Api = { daily: {
  calendar: () => {
    calendarCalls += 1;
    if (calendarCalls > 20) {  // a storm never yields to the checks below
      console.log('FAIL request_storm calls=' + calendarCalls);
      process.exit(0);
    }
    return new Promise((resolve) => pending.push(resolve));
  },
  status: () => new Promise(() => {}),
  convCount: async () => ({ count: 0 }),
  goals: () => new Promise(() => {}),
} };
// The section binds runtimeScope to window; the typed repository is a registry port.
// IndexedDB completes in a later task, as in a browser.
global.MyDayReportRepository = {
  readMonth: () => new Promise((resolve) => setTimeout(() => resolve(
    { cost_days: { 1: { cost: 1.5, conversations: 2 } }, conv_days: { 1: 2 } }), 1)),
  storeMonth: async () => {},
  readReport: async () => null,
  storeReport: async () => {},
};
eval(fs.readFileSync(process.argv[2], 'utf8'));
const tick = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
(async () => {
  const now = new Date();
  // Opening the panel: the calendar renders and its month load starts.
  _mydayRenderCalendar();
  _mydayFetchMonthOverview(now.getFullYear(), now.getMonth());
  await tick(200);  // the server is slow: no response yet
  check('one_month_load_in_flight', calendarCalls === 1);
  _mydayRenderCalendar();
  _mydayRenderCalendar();
  _mydayFetchMonthOverview(now.getFullYear(), now.getMonth());  // a second open joins
  await tick(50);
  check('rendering_and_reopening_never_start_another_load', calendarCalls === 1);
  for (const resolve of pending.splice(0)) resolve({ days: {}, conv_days: { 1: 3 }, cost_days: {} });
  await tick(50);
  _mydayFetchMonthOverview(now.getFullYear(), now.getMonth());
  check('settled_load_is_fresh', calendarCalls === 1);
  console.log(out.join('\n'));
  console.log('CALLS ' + calendarCalls);
})().catch((error) => { console.log('FAIL exception ' + (error && error.stack)); console.log(out.join('\n')); });
"""


def test_calendar_render_keeps_one_month_load_in_flight(tmp_path):
    from tests._jsdom import require_node_modules
    require_node_modules()
    harness = tmp_path / 'calendar_harness.js'
    harness.write_text(_HARNESS, encoding='utf-8')
    proc = subprocess.run(['node', str(harness), os.path.join(runtime_sections_dir(), 'myday.js')],
                          capture_output=True, text=True, timeout=60)
    output = proc.stdout.strip()
    assert proc.returncode == 0, proc.stderr
    assert 'FAIL' not in output and output.count('PASS') == 3, output
