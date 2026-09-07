"""Typed bounded-map behavior and API affinity resource-budget contracts."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
BOUNDED_MAP = ROOT / "frontend/src/core/bounded-map.ts"
TRANSPORT = ROOT / "frontend/src/api/transport.ts"


def _run_node(script: str, *paths: Path) -> dict:
    result = subprocess.run(
        ["node", "-e", script, *(str(path) for path in paths)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_bounded_map_uses_one_lru_order_for_reads_and_writes() -> None:
    bundle = native_module_path("bounded-map-owner.js", BOUNDED_MAP)
    output = _run_node(
        r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
const values = new BoundedMap(2);
values.set('a', 1).set('b', 2);
values.get('a');
values.set('c', 3);
values.set('a', 4);
let invalidCapacity = false;
try { new BoundedMap(0); } catch (error) { invalidCapacity = error instanceof RangeError; }
console.log(JSON.stringify({
  entries: Array.from(values.entries()),
  invalidCapacity,
  size: values.size,
}));
""",
        bundle,
    )
    assert output == {
        "entries": [["c", 3], ["a", 4]],
        "invalidCapacity": True,
        "size": 2,
    }


def test_transport_affinity_memory_storage_and_header_are_bounded() -> None:
    bundle = native_module_path("api-transport-bounded-affinity.js", TRANSPORT)
    output = _run_node(
        r"""
const fs = require('fs');
const stored = new Map();
stored.set('tofu_task_affinity_v1', JSON.stringify({
  tasks: Array.from({ length: 400 }, (_, index) => [`task-${index}`, `key-${index}`]),
  convs: Array.from({ length: 200 }, (_, index) => [`conv-${index}`, `conv-key-${index}`]),
}));
globalThis.window = globalThis;
globalThis.location = { pathname: '/' };
globalThis.document = { getElementById() { return null; } };
globalThis.sessionStorage = {
  getItem(key) { return stored.get(key) ?? null; },
  setItem(key, value) { stored.set(key, String(value)); },
};
let affinityHeader = '';
globalThis.fetch = async (_url, init) => {
  affinityHeader = init.headers['X-Tofu-Affinity-Key'] || '';
  return {
    ok: true,
    status: 204,
    headers: { get() { return null; } },
    async text() { return ''; },
  };
};
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

(async () => {
  const evictedTask = bindTaskAffinity('task-0');
  const retainedTask = bindTaskAffinity('task-399');
  const evictedConversation = bindTaskAffinity('probe-old', 'conv-0');
  const retainedConversation = bindTaskAffinity('probe-new', 'conv-199');
  for (let index = 0; index < 300; index += 1) {
    bindTaskAffinity(`bulk-${index}`, '', `bulk-key-${index}`);
  }
  for (let index = 0; index < 150; index += 1) {
    bindTaskAffinity(`conv-task-${index}`, `bulk-conv-${index}`, `conv-key-${index}`);
  }
  await request('/bounded-header', {
    parse: 'none',
    taskAffinityKey: 'x'.repeat(400),
  });
  const persisted = JSON.parse(stored.get('tofu_task_affinity_v1'));
  console.log(JSON.stringify({
    affinityHeaderLength: affinityHeader.length,
    evictedConversation,
    evictedTask,
    persistedConversations: persisted.convs.length,
    persistedTasks: persisted.tasks.length,
    retainedConversation,
    retainedTask,
    oldBulkTask: bindTaskAffinity('bulk-0'),
    recentBulkTask: bindTaskAffinity('bulk-299'),
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
""",
        bundle,
    )
    assert output == {
        "affinityHeaderLength": 256,
        "evictedConversation": "conv-conv-0",
        "evictedTask": "",
        "persistedConversations": 128,
        "persistedTasks": 256,
        "retainedConversation": "conv-key-199",
        "retainedTask": "key-399",
        "oldBulkTask": "",
        "recentBulkTask": "bulk-key-299",
    }
