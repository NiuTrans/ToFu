"""Contracts for the required Vite feature-entry bridge."""

from __future__ import annotations

import os
import re

import pytest

from tests._jsdom import JS_DIR, run_harness


pytestmark = pytest.mark.unit
_BRIDGE = os.path.join(JS_DIR, 'feature-bridge.js')

_PROLOGUE = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const fs = require('fs');
const injected = [];
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [],
  globals: { toast: function(){} },
});
document.head.appendChild = function(node){ injected.push(node); return node; };
(0, eval)(fs.readFileSync(process.argv[4], 'utf8'));
'''


def _run(body: str, *, min_pass: int, label: str) -> None:
    run_harness(
        target_js=_BRIDGE,
        body_js=_PROLOGUE + 'exports.__unused=0; (async () => {\n' + body + '\n})();',
        extra_targets=[_BRIDGE],
        min_pass=min_pass,
        label=label,
    )


def test_first_call_waits_for_module_graph_then_routes_once():
    _run(r'''
      let received = null;
      window._wireConvSyncPush('boot');
      window.TofuModules = {
        canInvokeFeature: () => true,
        invokeFeature: (name, args, stub) => {
          received = { name, args, stub };
          return Promise.resolve();
        },
      };
      window.dispatchEvent(new window.CustomEvent('tofu:modules-ready'));
      await Promise.resolve();
      await Promise.resolve();
      check('queued entry routed', received && received.name === '_wireConvSyncPush');
      check('queued arguments preserved', received && received.args[0] === 'boot');
      check('identity guard preserved', received && typeof received.stub === 'function');
      check('wait path injects no scripts', injected.length === 0);
      report();
    ''', min_pass=4, label='feature-bridge-wait')


def test_owner_failure_is_terminal_and_does_not_load_another_implementation():
    _run(r'''
      window.TofuModules = {
        canInvokeFeature: () => true,
        invokeFeature: () => Promise.reject(new Error('chunk unavailable')),
      };
      window.openSettings();
      await Promise.resolve();
      await Promise.resolve();
      check('owner failure injects no scripts', injected.length === 0);
      check('entry remains the bridge stub', typeof window.openSettings === 'function');
      report();
    ''', min_pass=2, label='feature-bridge-terminal')


def test_existing_owner_is_never_clobbered():
    _run(r'''
      let calls = 0;
      const owner = function(){ calls += 1; };
      window.openTaskMode = owner;
      (0, eval)(fs.readFileSync(process.argv[4], 'utf8'));
      window.openTaskMode();
      check('existing owner identity preserved', window.openTaskMode === owner);
      check('existing owner remains callable', calls === 1);
      check('no script injection', injected.length === 0);
      report();
    ''', min_pass=3, label='feature-bridge-preserve')


def test_entry_points_match_bundler():
    source = open(_BRIDGE, encoding='utf-8').read()
    match = re.search(r'_FEATURE_ENTRY_POINTS\s*=\s*\[([^\]]*)\]', source)
    assert match, 'could not find _FEATURE_ENTRY_POINTS in feature-bridge.js'
    entries = re.findall(r"'([^']+)'", match.group(1))
    assert entries and len(entries) == len(set(entries)), (
        'feature-bridge Vite entry points must be non-empty and unique')
    assert {'openSettings', '_wireConvSyncPush', '_toggleCostPopover'} <= set(entries)
