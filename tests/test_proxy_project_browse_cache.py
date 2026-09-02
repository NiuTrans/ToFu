"""Project picker cache-first paint and stale-request cancellation contract."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path


pytestmark = pytest.mark.unit


def test_project_browse_cache_is_bounded_and_late_results_cannot_repaint():
    if not shutil.which('node'):
        pytest.skip('node is required')
    project = runtime_section_path('project.js')
    coordinator = (
        Path(__file__).resolve().parents[1]
        / 'frontend/src/core/project-browse-coordinator.ts')
    harness = r"""
      const fs = require('fs');
      const ts = require('typescript');
      global.window = globalThis;
      global.runtimeScope = globalThis;
      const coordinatorSource = fs.readFileSync(COORDINATOR_PATH, 'utf8');
      const coordinatorModule = {exports:{}};
      const coordinatorJs = ts.transpileModule(coordinatorSource, {
        compilerOptions:{target:ts.ScriptTarget.ES2022,
          module:ts.ModuleKind.CommonJS, strict:true},
      }).outputText;
      new Function('module', 'exports', coordinatorJs)(
        coordinatorModule, coordinatorModule.exports);
      global.createProjectBrowseCoordinator =
        coordinatorModule.exports.createProjectBrowseCoordinator;
      const stored = new Map();
      global.sessionStorage = {
        getItem: (key) => stored.has(key) ? stored.get(key) : null,
        setItem: (key, value) => stored.set(key, String(value)),
        removeItem: (key) => stored.delete(key),
      };
      const elements = new Map();
      function element(id) {
        if (!elements.has(id)) elements.set(id, {
          id, innerHTML:'', textContent:'', value:'', disabled:false,
          hidden:false, style:{}, dataset:{}, scrollLeft:0, scrollWidth:0,
          classList:{add(){},remove(){},toggle(){},contains(){return false;}},
          querySelectorAll(){return [];}, querySelector(){return null;},
          addEventListener(){}, setAttribute(){}, focus(){},
        });
        return elements.get(id);
      }
      global.document = {
        getElementById: element,
        querySelector: () => null,
        querySelectorAll: () => [],
        createElement: (id) => element('created-' + id),
        addEventListener(){},
        body:{appendChild(){}},
      };
      global.escapeHtml = (value) => String(value == null ? '' : value);
      global.t = (key) => key;
      global.projectState = {path:'', extraRoots:[]};
      global.debugLog = () => {};
      const pending = [];
      const calls = [];
      global.Api = {project:{
        browse(path, showHidden, options) {
          return new Promise((resolve, reject) => {
            const call = {path, showHidden, options:options || {}, resolve, reject};
            calls.push(call); pending.push(call);
          });
        },
      }};
      eval(fs.readFileSync(PROJECT_PATH, 'utf8'));

      const data = (path, name) => ({
        path, parent:null, filesCount:1,
        dirs:[{path:path + '/' + name, name, itemCount:1, hasCode:false}],
      });
      const check = (condition, message) => {
        if (!condition) throw new Error(message);
      };

      (async () => {
        const first = browseDirectory('/root');
        pending.shift().resolve(data('/root', 'alpha'));
        await first;
        check(element('browseList').innerHTML.includes('alpha'),
          'initial server result did not paint');

        const cachedRefresh = browseDirectory('/root');
        const refreshCall = pending.shift();
        check(element('browseList').innerHTML.includes('alpha') &&
              !element('browseList').innerHTML.includes('Loading'),
          'cache did not paint synchronously before refresh');
        closeProjectModal();
        check(refreshCall.options.signal && refreshCall.options.signal.aborted,
          'closing the modal did not abort its browse request');
        refreshCall.resolve(data('/root', 'late-result'));
        await cachedRefresh;
        check(element('browseList').innerHTML.includes('alpha') &&
              !element('browseList').innerHTML.includes('late-result'),
          'late result repainted the closed modal');

        const slow = browseDirectory('/slow');
        const slowCall = pending.shift();
        const fast = browseDirectory('/fast');
        const fastCall = pending.shift();
        check(slowCall.options.signal && slowCall.options.signal.aborted,
          'new navigation did not abort the superseded request');
        fastCall.resolve(data('/fast', 'winner'));
        await fast;
        slowCall.resolve(data('/slow', 'stale'));
        await slow;
        check(element('browseList').innerHTML.includes('winner') &&
              !element('browseList').innerHTML.includes('stale'),
          'superseded result replaced newer navigation');

        for (let index = 0; index < 20; index += 1) {
          const path = '/bounded-' + index;
          const promise = browseDirectory(path);
          pending.shift().resolve(data(path, 'child-' + index));
          await promise;
        }
        const raw = stored.get('tofu_project_browse_cache_v1') || '';
        const persisted = JSON.parse(raw);
        check(persisted.entries.length <= 16,
          'session cache exceeded its entry budget');
        check(raw.length * 3 <= 256 * 1024,
          'session cache exceeded its conservative UTF-8 byte budget');
        console.log(JSON.stringify({
          cachedPaint:true, closeAborted:true, staleDropped:true,
          entries:persisted.entries.length, bytesUpperBound:raw.length * 3,
        }));
      })().catch((error) => { console.error(error); process.exitCode = 1; });
    """.replace('PROJECT_PATH', json.dumps(project)).replace(
        'COORDINATOR_PATH', json.dumps(str(coordinator)))
    completed = subprocess.run(
        ['node', '-e', harness],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result['cachedPaint'] is True
    assert result['closeAborted'] is True
    assert result['staleDropped'] is True
    assert result['entries'] <= 16
    assert result['bytesUpperBound'] <= 256 * 1024
