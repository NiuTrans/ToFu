"""Behavior contracts for the native Memory modal owner."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests._esm_feature_harness import compile_feature_owner
from tests._jsdom import require_node_modules

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'frontend/src/features/memory/panel.ts'
ESBUILD = ROOT / 'scripts' / 'vite_test_bundle.mjs'


def _node() -> str:
    value = shutil.which('node')
    require_node_modules('jsdom', 'vite')
    return value


def test_native_memory_modal_actions_rollback_and_request_ownership(tmp_path):
    built = tmp_path / 'memory.js'
    compiled = compile_feature_owner(ESBUILD, MODULE, built, tmp_path)
    assert compiled.returncode == 0, compiled.stderr
    harness = textwrap.dedent(
        """
        const { JSDOM } = require('jsdom');
        const dom = new JSDOM(`<!doctype html><body>
          <div id="memoryModal"><div class="memory-modal"><div id="memoryStats"></div>
            <button class="memory-tab active" data-scope="all"></button>
            <input id="memorySearchInput"><div id="memoryList"></div>
            <div id="memoryAddSection" style="display:none"></div>
            <input id="memoryNewName"><input id="memoryNewDesc">
            <textarea id="memoryNewBody"></textarea><input id="memoryNewTags">
            <select id="memoryNewScope"><option value="project">p</option></select>
            <div id="memoryModalStatus"></div><button id="memoryModalToggleBtn"></button>
            <input id="memoryInstallInput" type="file">
          </div></div><div id="prefsMemoryList"></div>
        </body>`, { url: 'http://localhost/' });
        global.window = dom.window; global.document = dom.window.document;
        for (const name of ['Element','HTMLElement','HTMLButtonElement','HTMLInputElement',
          'HTMLTextAreaElement','HTMLSelectElement','File','FormData']) {
          global[name] = dom.window[name];
        }
        global.memoryEnabled = true;
        const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
        const response = (body = {}) => ({ ok: true, status: 200, json: async () => body });
        const rows = [{ id: 'm1', name: 'Memory One', description: 'desc', body: '**body**',
          scope: 'project', tags: ['one'], enabled: true }];
        const calls = [];
        let toggleReject, deleteReject;
        const packageInstallResolvers = [];
        window.t = (key) => key; window.escapeHtml = (v) => String(v ?? '')
          .replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
        window.debugLog = (...args) => calls.push(['debug', ...args]);
        window.showConfirm = async () => true;
        window.showChoice = async (choice) => {
          calls.push(['destinationChoices', choice.options.map((option) => option.value)]);
          return 'cancel';
        };
        window._applyMemoryUI = (enabled) => {
          global.memoryEnabled = enabled; calls.push(['applyMemory', enabled]);
        };
        window.captureActiveConversationSettings = () => calls.push(['saveState']);
        window.updateSubmenuCounts = () => calls.push(['counts']);
        window._ephemeralToast = () => null;
        window.Api = { memory: {
          list: async () => ({ memories: rows.map((row) => ({ ...row })) }),
          toggle: (id) => new Promise((_, reject) => { calls.push(['toggle', id]); toggleReject = reject; }),
          remove: (id) => new Promise((_, reject) => { calls.push(['delete', id]); deleteReject = reject; }),
          create: async (item) => { calls.push(['create', item]); return response({ memory: {
            id: 'm2', ...item, enabled: true,
          }}); },
        }, project: {
          recentList: async () => ({ projects: [{ path: '/projects/memory-test', exists: true }] }),
        }, skills: {
          install: (form, projectPath) => new Promise((resolve) => {
            calls.push(['packageInstall', form.get('scope'), form.get('file')?.name, projectPath]);
            packageInstallResolvers.push(resolve);
          }),
        }};
        require(BUILT_PATH);
        (async () => {
          window.openMemoryModal(); await tick(); await tick();
          const list = document.getElementById('memoryList');
          const noInlineHandlers = list.querySelectorAll('[onclick],[oninput]').length === 0;
          const initialRendered = list.textContent.includes('Memory One');
          const packageInput = document.getElementById('memoryInstallInput');
          Object.defineProperty(packageInput, 'files', { value: [
            new File(['zip'], 'memory.zip', { type: 'application/zip' }),
          ] });
          window.installSkillFromFileInput(packageInput);
          await tick(); await tick(); await tick();
          const packageInstallCalls = calls.filter((row) => row[0] === 'packageInstall');
          window.installSkillFromFileInput(packageInput);
          await tick(); await tick(); await tick();
          const concurrentInstallIgnored = calls.filter((row) => row[0] === 'packageInstall').length === 1;
          for (const resolve of packageInstallResolvers) {
            resolve(response({ memory: { name: 'Uploaded', scope: 'global' } }));
          }
          await tick(); await tick(); await tick();
          list.querySelector('[data-memory-action="toggle"]').click(); await tick();
          const toggledInstantly = list.querySelector('.memory-card').classList.contains('is-disabled');
          toggleReject(new Error('down')); await tick(); await tick();
          const toggleRolledBack = !list.querySelector('.memory-card').classList.contains('is-disabled');
          list.querySelector('[data-memory-action="delete"]').click(); await tick(); await tick();
          const hiddenInstantly = list.querySelector('.memory-card').style.opacity === '0';
          deleteReject(new Error('down')); await tick(); await tick();
          const deleteRolledBack = list.querySelector('.memory-card').style.opacity === '1';
          document.getElementById('memoryNewName').value = 'Created';
          document.getElementById('memoryNewBody').value = 'Body';
          await window.createMemoryFromModal();
          const createRendered = list.textContent.includes('Created');

          const resolvers = [];
          let deferredListCalls = 0;
          window.Api.memory.list = () => new Promise((resolve) => {
            deferredListCalls++; resolvers.push(resolve);
          });
          const oldRequest = window.refreshMemoryList();
          window.closeMemoryModal();
          const newRequest = window.refreshMemoryList();
          resolvers[0]({ memories: [{ id: 'old', name: 'OLD', enabled: true }] });
          await tick(); await tick();
          const oldIgnored = !list.textContent.includes('OLD');
          resolvers[1]({ memories: [{ id: 'new', name: 'NEW', enabled: true }] });
          await Promise.all([oldRequest, newRequest]);
          const reopenedApplied = list.textContent.includes('NEW');

          const sameEpochResolvers = [];
          window.Api.memory.list = () => new Promise((resolve) => sameEpochResolvers.push(resolve));
          const allRequest = window.refreshMemoryList('all');
          const projectRequest = window.refreshMemoryList('project');
          const sameEpochSingleFlight = sameEpochResolvers.length === 1;
          sameEpochResolvers[0]({ memories: [
            { id: 'global', name: 'GLOBAL', scope: 'global', enabled: true },
            { id: 'project', name: 'PROJECT', scope: 'project', enabled: true },
          ] });
          await Promise.all([allRequest, projectRequest]);
          const latestScopeApplied = list.textContent.includes('PROJECT')
            && !list.textContent.includes('GLOBAL');
          window.toggleMemoryFromModal();
          console.log(JSON.stringify({ noInlineHandlers, initialRendered, toggledInstantly,
            toggleRolledBack, hiddenInstantly, deleteRolledBack, createRendered,
            oldIgnored, reopenedApplied, sameEpochSingleFlight, latestScopeApplied,
            deferredListCalls, packageInstallCalls, concurrentInstallIgnored,
            destinationChoiceCalls: calls.filter((row) => row[0] === 'destinationChoices').length,
            toggleCalls: calls.filter(x => x[0] === 'toggle').length,
            deleteCalls: calls.filter(x => x[0] === 'delete').length,
            createCalls: calls.filter(x => x[0] === 'create').length,
            settingsCaptures: calls.filter(x => x[0] === 'saveState').length,
            memoryApply: calls.findLast(x => x[0] === 'applyMemory')?.[1] }));
        })().catch((error) => { console.error(error); process.exitCode = 1; });
        """
    ).replace('BUILT_PATH', json.dumps(str(built)))
    run = subprocess.run([_node(), '-e', harness], cwd=ROOT,
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout.strip().splitlines()[-1])
    behavioral_results = {key: result[key] for key in (
        'noInlineHandlers', 'initialRendered', 'toggledInstantly',
        'toggleRolledBack', 'hiddenInstantly', 'deleteRolledBack',
        'createRendered', 'oldIgnored', 'reopenedApplied',
        'sameEpochSingleFlight', 'latestScopeApplied',
        'concurrentInstallIgnored',
    )}
    assert all(value is True for value in behavioral_results.values()), behavioral_results
    assert result['deferredListCalls'] == 2
    assert result['toggleCalls'] == result['deleteCalls'] == result['createCalls'] == 1
    assert result['settingsCaptures'] == 1
    assert result['memoryApply'] is False
    assert result['packageInstallCalls'] == [
        ['packageInstall', 'global', 'memory.zip', ''],
    ]
    assert result['destinationChoiceCalls'] == 0
