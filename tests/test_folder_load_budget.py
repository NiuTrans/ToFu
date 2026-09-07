"""Folder startup reads preserve failure semantics without duplicate GETs."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
API_SOURCE = ROOT / 'frontend/src/runtime/sections/api.js'
FOLDER_SOURCE = ROOT / 'frontend/src/runtime/sections/core/folders.js'

HARNESS = r"""
const fs = require('fs');
global.window = globalThis;
global.runtimeScope = globalThis;
global.sessionStorage = {
  getItem() { return null; }, setItem() {}, removeItem() {},
};
const warnings = [];
global.console = {
  log() {}, info() {}, debug() {}, error() {},
  warn(...parts) { warnings.push(parts.map(String).join(' ')); },
};

let nextTimer = 0;
const timers = new Map();
global.setTimeout = (fn, delay) => {
  const id = ++nextTimer;
  timers.set(id, {fn, delay});
  return id;
};
global.clearTimeout = (id) => timers.delete(id);

class ApiError extends Error {
  constructor(message, detail = {}) {
    super(message);
    Object.assign(this, detail);
  }
}
const calls = [];
let mode = 'empty';
let releaseDeferred = null;
const response = () => {
  if (mode === 'empty') return {ok:true, items:[]};
  if (mode === 'one') return {ok:true, items:[{id:'folder-1', name:'One'}]};
  if (mode === 'bare') return [{id:'legacy', name:'Legacy'}];
  if (mode === 'malformed') return {ok:true};
  if (mode === 'failure') throw new ApiError('offline', {code:'network'});
  if (mode === 'deferred') {
    return new Promise((resolve) => { releaseDeferred = resolve; });
  }
  throw new Error('unknown mode');
};
global.requiredApiTransport = {
  ApiError,
  resolvePath:path => path,
  pageRequestId:'folder-budget',
  bindTaskAffinity() {},
  newIdempotencyKey:() => 'idem',
  taskStartAffinityOptions:(_body, options) => options,
  request:async (path, options) => {
    calls.push({path, options});
    if (path === '/api/v1/folders' || path === '/api/v1/paper-folders') {
      return await response();
    }
    return {};
  },
};

eval(fs.readFileSync(process.argv[1], 'utf8'));
const api = runtimeScope.Api;
let renders = 0;
global.renderConversationList = () => { renders += 1; };
global.conversations = [];
global.ConvCache = {put() {}};
eval(fs.readFileSync(process.argv[2], 'utf8') + `
  ;globalThis.__folderLoadTest = {
    loadFolders,
    seed(folders, loaded) { _folders = folders; _foldersLoaded = loaded; },
    state() {
      return {
        folders:_folders.map(row => row.id), loaded:_foldersLoaded,
        flight:Boolean(_folderLoadFlight), retryAttempt:_folderLoadRetryAttempt,
        retryTimer:Boolean(_folderLoadRetryTimer),
      };
    },
    clearRetry:_clearFolderLoadRetry,
  };
`);

const checks = [];
const check = (name, value) => checks.push((value ? 'PASS ' : 'FAIL ') + name);
const settle = async () => {
  for (let index = 0; index < 5; index += 1) await Promise.resolve();
};

(async () => {
  // Endpoint contract: one valid empty response, no ambiguity or fallback.
  mode = 'empty';
  let before = calls.length;
  const empty = await api.folders.list();
  check('api_empty_is_valid', Array.isArray(empty) && empty.length === 0);
  check('api_empty_is_one_throwing_get', calls.length === before + 1 &&
    calls.at(-1).options.onError === 'throw' && calls.at(-1).options.coalesce === true);

  mode = 'bare';
  const legacy = await api.folders.list();
  check('legacy_bare_array_remains_readable', legacy[0].id === 'legacy');

  mode = 'malformed';
  let malformed = null;
  try { await api.folders.list(); } catch (error) { malformed = error; }
  check('malformed_success_fails_closed', malformed instanceof ApiError && malformed.code === 'parse');

  mode = 'failure';
  let paperFailure = null;
  try { await api.paperFolders.list(); } catch (error) { paperFailure = error; }
  check('paper_folder_failure_is_not_empty_data', paperFailure?.code === 'network');

  // Loader contract: a genuine empty store costs exactly one request.
  __folderLoadTest.seed([{id:'stale'}], false);
  mode = 'empty';
  before = calls.length;
  await __folderLoadTest.loadFolders();
  check('empty_loader_uses_one_request', calls.length === before + 1);
  check('empty_loader_commits_loaded_state', __folderLoadTest.state().loaded &&
    __folderLoadTest.state().folders.length === 0);

  // Concurrent refreshes share the same bounded flight on direct/LAN too.
  __folderLoadTest.seed([], false);
  mode = 'deferred';
  before = calls.length;
  const first = __folderLoadTest.loadFolders();
  const second = __folderLoadTest.loadFolders();
  check('concurrent_loads_share_promise', first === second);
  check('concurrent_loads_issue_one_get', calls.length === before + 1);
  releaseDeferred({ok:true, items:[{id:'coalesced', name:'Coalesced'}]});
  await Promise.all([first, second]);
  check('shared_flight_commits_once', __folderLoadTest.state().folders.join(',') === 'coalesced');

  // First-load failure preserves state and retains the bounded recovery chain.
  __folderLoadTest.seed([{id:'preserved'}], false);
  mode = 'failure';
  before = calls.length;
  let firstLoadFailure = null;
  try { await __folderLoadTest.loadFolders(); } catch (error) { firstLoadFailure = error; }
  check('failure_is_one_get', calls.length === before + 1);
  check('failure_preserves_projection', __folderLoadTest.state().folders.join(',') === 'preserved' &&
    __folderLoadTest.state().loaded === false);
  check('failure_remains_observable', firstLoadFailure?.code === 'network');
  check('first_failure_schedules_one_retry', timers.size === 1 &&
    [...timers.values()][0].delay === 1500);

  // Any success cancels that reconstructible retry resource immediately.
  mode = 'one';
  await __folderLoadTest.loadFolders();
  await settle();
  check('success_releases_retry_and_flight', timers.size === 0 &&
    __folderLoadTest.state().retryAttempt === 0 &&
    __folderLoadTest.state().flight === false);

  // A later refresh failure never blanks an already-authoritative tree.
  mode = 'failure';
  let refreshFailure = null;
  try { await __folderLoadTest.loadFolders(); } catch (error) { refreshFailure = error; }
  check('refresh_failure_keeps_last_good_tree',
    __folderLoadTest.state().folders.join(',') === 'folder-1' &&
    __folderLoadTest.state().loaded === true && timers.size === 0 &&
    refreshFailure?.code === 'network');

  process.stdout.write(checks.join('\n') + '\n');
  if (checks.some(line => line.startsWith('FAIL'))) process.exitCode = 1;
})().catch((error) => { console.error(error); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node is required')
def test_folder_list_singleflight_failure_and_empty_budget():
    result = subprocess.run(
        ['node', '-e', HARNESS, str(API_SOURCE), str(FOLDER_SOURCE)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = (result.stdout or '') + (result.stderr or '')
    assert result.returncode == 0, output
    failures = [line for line in output.splitlines() if line.startswith('FAIL')]
    assert not failures, output
    assert output.count('PASS') == 16, output
