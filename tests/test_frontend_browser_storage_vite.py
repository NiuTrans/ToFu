"""Compiled browser-storage capability boundary behavior."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "frontend/src/core/browser-storage.ts"


def test_browser_storage_capability_is_explicit_and_fail_soft():
    bundle = native_module_path("browser-storage.js", SOURCE)
    harness = r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

const storage = {
  getItem: () => null,
  setItem: () => undefined,
  removeItem: () => undefined,
  clear: () => undefined,
  key: () => null,
  length: 0,
};
const throwingHost = {};
Object.defineProperty(throwingHost, 'localStorage', {
  get() { throw new Error('storage denied'); },
});

Object.defineProperty(globalThis, 'localStorage', {
  configurable: true,
  value: storage,
});
const result = {
  explicit: resolveBrowserLocalStorage({ localStorage: storage }) === storage,
  defaultHost: resolveBrowserLocalStorage() === storage,
  missing: resolveBrowserLocalStorage({}) === undefined,
  denied: resolveBrowserLocalStorage(throwingHost) === undefined,
};
console.log(JSON.stringify(result));
"""
    result = subprocess.run(
        ["node", "-e", harness, bundle],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "explicit": True,
        "defaultHost": True,
        "missing": True,
        "denied": True,
    }
