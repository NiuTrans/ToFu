"""Reloaded simulation history must distinguish failed/running/completed runs."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
JSDOM = ROOT / 'node_modules/jsdom'
SOURCE = Path(__file__).resolve().parents[1] / 'tofu_trading/static/js/trading/simulator.js'

HARNESS = r'''
const fs = require('fs');
const assert = require('node:assert/strict');
const {JSDOM} = require(process.env.JSDOM);
const status = process.env.SIM_STATUS;
const ids = ['simSessionList', 'simResHero', 'simResHeroLabel', 'simResReturn',
  'simResDrawdown', 'simResSharpe', 'simResWinRate', 'simResFees', 'simResTrades',
  'simResSharpeHint', 'simResBenchmark', 'simResCTA', 'simResJournal'];
const dom = new JSDOM(ids.map(id => `<div id="${id}"></div>`).join(''),
  {url:'http://localhost/', runScripts:'outside-only'});
const w = dom.window;
const metrics = {total_return_pct: 12, max_drawdown_pct: 3, sharpe_ratio: 1.2, win_rate: 60};
const session = {session_id:'fixture', status, start_date:'2024-01-01', end_date:'2024-01-04'};
const toasts = [];
const calls = [];
w.TradingApp = {
  _$: id => w.document.getElementById(id), _state: {},
  api: async path => {
    calls.push(path);
    if (path.startsWith('/sim/sessions')) return {sessions:[{...session, metrics:status === 'completed' ? metrics : {}}]};
    if (path === '/sim/session/fixture') return {session, metrics:status === 'completed' ? metrics : {}, trade_count:1};
    if (path.startsWith('/sim/journal/')) return {journal:[]};
    return {};
  },
  toast:(text, kind) => toasts.push({text, kind}),
  fmtNum:(value, digits=2) => Number(value).toFixed(digits),
  fmtPct:value => String(value), pnlClass:value => value >= 0 ? 'up' : 'down',
  escHtml:value => String(value), renderMarkdown:value => String(value),
};
w.eval(fs.readFileSync(process.env.SIM_SOURCE, 'utf8'));
(async () => {
  const tick = () => new Promise(resolve => setImmediate(resolve));
  w.TradingApp.loadSimulator();
  await tick();
  const list = w.document.getElementById('simSessionList').textContent;
  assert.ok(list.includes(status === 'completed' ? '已完成' : status === 'failed' ? '失败' : '进行中'));
  w.TradingApp.viewSimSession('fixture');
  await tick(); await tick();
  assert.ok(calls.some(path => path.startsWith('/sim/journal/fixture')));
  const text = id => w.document.getElementById(id).textContent;
  if (status === 'completed') {
    assert.equal(text('simResHero'), '+12.00%');
    assert.ok(toasts.some(toast => toast.kind === 'success'));
  } else {
    assert.equal(text('simResHero'), '—');
    assert.equal(text('simResReturn'), '—');
    assert.equal(text('simResBenchmark'), '');
    assert.equal(text('simResCTA'), '');
    assert.ok(text('simResHeroLabel').includes(status === 'failed' ? '失败' : '进行中'));
    assert.ok(!toasts.some(toast => toast.kind === 'success' || toast.text.includes('模拟完成')));
  }
  dom.window.close();
})().catch(error => {console.error(error); dom.window.close(); process.exitCode=1;});
'''


@pytest.mark.parametrize('status', ['failed', 'running', 'completed'])
def test_history_presents_only_committed_final_results(status):
    if not shutil.which('node') or not JSDOM.is_dir():
        pytest.skip('node and workspace jsdom are required')
    completed = subprocess.run(['node', '-e', HARNESS], capture_output=True, text=True,
                               timeout=30, env={**os.environ, 'SIM_STATUS': status,
                                                'JSDOM': str(JSDOM), 'SIM_SOURCE': str(SOURCE)})
    assert completed.returncode == 0, completed.stdout + completed.stderr
