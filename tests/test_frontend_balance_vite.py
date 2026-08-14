"""Behavior contracts for the native TypeScript provider-balance owner."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests._esm_feature_harness import compile_feature_owner


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'frontend/src/features/settings/balance.ts'
ESBUILD = ROOT / 'node_modules/.bin/esbuild'


def _node() -> str:
    executable = shutil.which('node')
    if not executable:
        pytest.skip('node not available')
    return executable


def _has_jsdom() -> bool:
    return subprocess.run(
        [_node(), '-e', "require('jsdom')"], cwd=ROOT,
        capture_output=True,
    ).returncode == 0


@pytest.mark.skipif(not ESBUILD.is_file() or not _has_jsdom(),
                    reason='jsdom + esbuild not installed')
def test_native_balance_actions_stale_guard_and_owned_polling(tmp_path):
    built = tmp_path / 'balance.js'
    compiled = compile_feature_owner(ESBUILD, MODULE, built, tmp_path)
    assert compiled.returncode == 0, compiled.stderr

    harness = textwrap.dedent(
        """
        const { JSDOM } = require('jsdom');
        const dom = new JSDOM('<!doctype html><body>' +
          '<div class="stg-provider-card" data-prov-idx="0">' +
          '<div class="stg-provider-badges"></div></div>' +
          '<div id="stgBalanceResult_0"></div></body>');
        global.window = dom.window;
        global.document = dom.window.document;
        for (const name of ['HTMLElement', 'AbortController', 'AbortSignal']) {
          global[name] = dom.window[name];
        }
        window._stgProviders = [{
          id: 'first', base_url: 'https://provider.test/v1',
          balance_url: '', api_keys: ['secret'], enabled: true,
        }];
        window._guessBalanceUrl = () => 'https://provider.test/balance';
        let providerRenders = 0;
        window._renderProvidersTab = () => { providerRenders += 1; };
        window.t = (key, values = {}) => `${key}:${values.error ?? ''}`;
        window.Icon = (name) => `<svg data-icon="${name}"></svg>`;
        window.escapeHtml = (value) => String(value).replaceAll('&', '&amp;')
          .replaceAll('<', '&lt;').replaceAll('>', '&gt;');
        const calls = [];
        window.Api = { providers: {
          balance: async (body) => {
            calls.push(body);
            return { ok: true, balance: { balance_usd: 12.5 } };
          },
        }};
        const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
        require(BUILT_PATH);
        window.__setFeatureService('_stgProviders', window._stgProviders);

        (async () => {
          window._checkProviderBalance(0);
          await tick(); await tick();
          const resultText = document.getElementById('stgBalanceResult_0').textContent;
          const badgeText = document.querySelector('.stg-badge-balance').textContent;
          document.querySelector('.stg-badge-balance').click();
          await tick(); await tick();

          let resolveStale;
          window.Api.providers.balance = (body) => {
            calls.push(body);
            return new Promise((resolve) => { resolveStale = resolve; });
          };
          const result = document.getElementById('stgBalanceResult_0');
          window._checkProviderBalance(0);
          window._stgProviders[0] = {
            id: 'replacement', balance_url: 'https://new.test/balance',
            api_keys: ['new-key'], enabled: true,
          };
          result.textContent = 'REPLACEMENT';
          resolveStale({ ok: true, balance: { balance_usd: 999 } });
          await tick(); await tick();
          const staleResult = result.textContent;

          const scheduled = [];
          const clearedTimeouts = [];
          const clearedIntervals = [];
          let nextTimer = 1;
          window.setTimeout = (callback, delay) => {
            const id = nextTimer++;
            scheduled.push({ id, callback, delay });
            return id;
          };
          window.clearTimeout = (id) => { clearedTimeouts.push(id); };
          window.setInterval = (callback, delay) => {
            const id = nextTimer++;
            scheduled.push({ id, callback, delay, interval: true });
            return id;
          };
          window.clearInterval = (id) => { clearedIntervals.push(id); };
          delete window._balanceCache[0];
          window._stgProviders = [
            { id: 'a', balance_url: 'https://a.test', api_keys: ['a'] },
            { id: 'b', balance_url: 'https://b.test', api_keys: ['b'] },
          ];
          window.__setFeatureService('_stgProviders', window._stgProviders);
          const callsBeforePolling = calls.length;
          window._startBalancePolling();
          const timeoutRows = scheduled.filter((row) => !row.interval);
          const intervalRows = scheduled.filter((row) => row.interval);
          window._stopBalancePolling();
          for (const row of timeoutRows) row.callback();
          await tick();

          const unsafe = window._renderBalanceInfo({
            balance_usd: 2,
            currency: '<img src=x onerror=bad()>',
            balance_local: 3,
          });
          console.log(JSON.stringify({
            resultText, badgeText, calls, providerRenders, staleResult,
            timeoutCount: timeoutRows.length,
            intervalCount: intervalRows.length,
            clearedTimeouts: clearedTimeouts.length,
            clearedIntervals: clearedIntervals.length,
            callsAfterStoppedCallbacks: calls.length - callsBeforePolling,
            escapedCurrency: unsafe.includes('&lt;img'),
          }));
        })().catch((error) => {
          console.error(error);
          process.exitCode = 1;
        });
        """
    ).replace('BUILT_PATH', json.dumps(str(built)))
    run = subprocess.run(
        [_node(), '-e', harness], cwd=ROOT, capture_output=True, text=True,
    )
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout.strip().splitlines()[-1])

    assert '$12.50' in result['resultText']
    assert '$12.50' in result['badgeText']
    assert result['calls'][0] == {
        'balance_url': 'https://provider.test/balance',
        'api_key': 'secret',
    }
    assert result['providerRenders'] == 1
    assert result['staleResult'] == 'REPLACEMENT'
    assert result['timeoutCount'] == result['clearedTimeouts'] == 2
    assert result['intervalCount'] == result['clearedIntervals'] == 1
    assert result['callsAfterStoppedCallbacks'] == 0
    assert result['escapedCurrency'] is True
