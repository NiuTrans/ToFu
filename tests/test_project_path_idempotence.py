"""Exact project-path reconciliation avoids duplicate browser and disk work."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
API_SOURCE = ROOT / "frontend/src/runtime/sections/api.js"


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_project_set_paths_coalesces_only_identical_inflight_requests() -> None:
    harness = r"""
const assert = require('node:assert/strict');
const fs = require('fs');

var runtimeScope = {};
var physical = [];
var release;
var gate = new Promise((resolve) => { release = resolve; });

class MockResponse {
  constructor(body) {
    this.bodyText = body;
    this.ok = true;
    this.status = 200;
  }
  clone() { return new MockResponse(this.bodyText); }
  async json() { return JSON.parse(this.bodyText); }
}

var requiredApiTransport = {
  ApiError: class ApiError extends Error {},
  resolvePath(path) { return path; },
  pageRequestId: 'project-singleflight-test',
  bindTaskAffinity() {},
  newIdempotencyKey() { return 'test-key'; },
  taskStartAffinityOptions(body, options) { return options || {}; },
  async request(path, options) {
    physical.push({ path, json: options.json });
    await gate;
    return new MockResponse(JSON.stringify({ path: options.json.paths[0] }));
  },
};

eval(fs.readFileSync(process.argv[1], 'utf8'));

(async () => {
  const first = runtimeScope.Api.project.setPaths(['/repo/a'], []);
  const duplicate = runtimeScope.Api.project.setPaths(['/repo/a'], []);
  const distinct = runtimeScope.Api.project.setPaths(['/repo/b'], []);
  const permissionChange = runtimeScope.Api.project.setPaths(
    ['/repo/a'], ['/repo/a'],
  );
  const recentIntent = runtimeScope.Api.project.setPaths(
    ['/repo/a'], [], ['/repo/a'],
  );
  const recentDuplicate = runtimeScope.Api.project.setPaths(
    ['/repo/a'], [], ['/repo/a'],
  );
  await Promise.resolve();
  assert.equal(physical.length, 4);
  release();
  const [firstResponse, duplicateResponse, distinctResponse, permissionResponse,
    recentResponse, recentDuplicateResponse] = await Promise.all([
    first, duplicate, distinct, permissionChange, recentIntent, recentDuplicate,
  ]);
  assert.notEqual(firstResponse, duplicateResponse);
  assert.deepEqual(await firstResponse.json(), { path: '/repo/a' });
  assert.deepEqual(await duplicateResponse.json(), { path: '/repo/a' });
  assert.deepEqual(await distinctResponse.json(), { path: '/repo/b' });
  assert.deepEqual(await permissionResponse.json(), { path: '/repo/a' });
  assert.deepEqual(await recentResponse.json(), { path: '/repo/a' });
  assert.deepEqual(await recentDuplicateResponse.json(), { path: '/repo/a' });
  assert.deepEqual(physical[3].json.recentPaths, ['/repo/a']);

  const overLimitPaths = Array.from(
    { length: 33 }, (_, index) => `/repo/many-${index}`,
  );
  await runtimeScope.Api.project.setPaths(
    overLimitPaths, [], overLimitPaths,
  );
  assert.equal(physical.length, 5);
  assert.equal(physical[4].json.paths.length, 33);
  assert.equal(physical[4].json.recentPaths.length, 32);

  await runtimeScope.Api.project.setPaths(['/repo/a'], []);
  assert.equal(physical.length, 6, 'settled calls are not cached');

  gate = new Promise((resolve) => { release = resolve; });
  const bounded = Array.from({ length: 17 }, (_, index) => (
    runtimeScope.Api.project.setPaths([`/repo/cap-${index}`], [])
  ));
  const retainedDuplicate = runtimeScope.Api.project.setPaths(['/repo/cap-0'], []);
  const overflowDuplicate = runtimeScope.Api.project.setPaths(['/repo/cap-16'], []);
  await Promise.resolve();
  assert.equal(
    physical.length,
    24,
    '16 keys are retained; the 17th key and its duplicate bypass the table',
  );
  release();
  await Promise.all([...bounded, retainedDuplicate, overflowDuplicate]);
  process.stdout.write(JSON.stringify({ physical: physical.length }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    result = subprocess.run(
        ["node", "-e", harness, str(API_SOURCE)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout) == {"physical": 24}


def test_identical_project_paths_preserve_session_state_and_skip_warm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lib.project_mod import config, scanner

    primary = tmp_path / "primary"
    extra = tmp_path / "extra"
    primary.mkdir()
    extra.mkdir()
    session_starts: list[str] = []
    warm_requests: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        scanner, "_start_new_session", lambda path: session_starts.append(path)
    )
    monkeypatch.setattr(
        scanner,
        "_warm_tree_indexes",
        lambda *paths: warm_requests.append(tuple(paths)),
    )
    scanner.clear_project()
    try:
        scanner.set_project_paths(
            [str(primary), str(extra)], readonly_paths=[str(extra)]
        )
        with config._lock:
            config._state["fileCount"] = 37
        first_session_count = len(session_starts)
        first_warm_count = len(warm_requests)

        state = scanner.set_project_paths(
            [str(primary), str(extra)], readonly_paths=[str(extra)]
        )
        assert state["fileCount"] == 37
        assert len(session_starts) == first_session_count
        assert len(warm_requests) == first_warm_count

        changed = scanner.set_project_paths(
            [str(primary), str(extra)], readonly_paths=[]
        )
        assert len(session_starts) == first_session_count + 1
        assert len(warm_requests) > first_warm_count
        assert changed["readOnly"] is False
        assert changed["extraRoots"][0]["readOnly"] is False
    finally:
        scanner.clear_project()
