"""The retained runtime evaluates cleanly under real ESM global semantics."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from lib.vite_assets import VITE_OUT_DIR, validate_vite_artifact


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "frontend/src/runtime/app-runtime.js"


def _runtime_source() -> str:
    return RUNTIME.read_text(encoding="utf-8")


def _runtime_scope_seam_symbols(source: str) -> list[str]:
    """Find runtimeScope services still called through guarded bare names."""
    registered = set(re.findall(
        r"runtimeScope\.([A-Za-z_$][A-Za-z0-9_$]*)\s*=", source,
    ))
    guarded = set(re.findall(
        r"typeof\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*===?\s*['\"]function['\"]",
        source,
    ))
    declared = set(re.findall(
        r"^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)",
        source,
        re.M,
    ))
    declared |= set(re.findall(
        r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)",
        source,
        re.M,
    ))
    return sorted((registered & guarded) - declared)


def test_runtime_tail_publishes_only_live_service_and_action_bridges():
    source = _runtime_source()
    assert "const _globalPublishTarget" in source
    assert "if (_name in _globalPublishTarget) continue;" in source
    assert "globalThis)[name] = value;" in source
    assert "[\"renderChat\", renderChat]" not in source
    assert "runtimeSourceCount" not in source
    assert _runtime_scope_seam_symbols(source)


def _main_asset() -> Path:
    # Resolve through the same strict manifest/path validator used by the
    # server, so this exercises the shipped artifact boundary rather than
    # pinning a second test-only manifest reader.
    manifest = validate_vite_artifact(("main",))
    relative = manifest.get("frontend/src/main.ts", {}).get("file")
    assert relative, "Vite manifest has no frontend/src/main.ts entry"
    return Path(VITE_OUT_DIR, *relative.split("/"))


_LOADER = r"""
import { readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
registerHooks({
  load(url, context, nextLoad) {
    if (url.includes('/static/vite/assets/') && url.endsWith('.js')) {
      return {format:'module', source:readFileSync(new URL(url), 'utf8'),
        shortCircuit:true};
    }
    return nextLoad(url, context);
  },
});
"""


_HARNESS = r"""
'use strict';
const path = require('path');
const {pathToFileURL} = require('url');
const root = process.argv[3];
const symbols = JSON.parse(process.env.SEAM_SYMBOLS || '[]');
process.on('unhandledRejection', () => {});
process.on('uncaughtException', () => {});
const {JSDOM} = require(path.join(root, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!doctype html><body><main id="chatInner"></main></body>',
  {url:'http://localhost/'});
const win = dom.window;
global.window = win;
global.document = win.document;
global.self = win;
const media = () => ({matches:false, media:'', addEventListener(){},
  removeEventListener(){}, addListener(){}, removeListener(){}});
win.matchMedia = media;
global.matchMedia = media;
global.IntersectionObserver = win.IntersectionObserver = class {
  observe() {} unobserve() {} disconnect() {}
};
global.ResizeObserver = win.ResizeObserver = class {
  observe() {} unobserve() {} disconnect() {}
};
global.requestIdleCallback = win.requestIdleCallback = () => 0;
global.requestAnimationFrame = win.requestAnimationFrame = () => 0;
global.localStorage = win.localStorage;
global.sessionStorage = win.sessionStorage;
global.CustomEvent = win.CustomEvent;
global.Event = win.Event;
global.MutationObserver = win.MutationObserver;
global.getComputedStyle = (element, pseudo) => win.getComputedStyle(element, pseudo);
global.alert = () => {};
global.confirm = () => false;
global.prompt = () => null;
global.HTMLElement = win.HTMLElement;
global.CSS = win.CSS || (win.CSS = {escape:(value) => String(value)});
for (const key of Object.getOwnPropertyNames(win)) {
  if (key in globalThis) continue;
  try { globalThis[key] = win[key]; } catch (_) {}
}
for (const key of ['AbortController', 'AbortSignal']) {
  if (win[key]) globalThis[key] = win[key];
}
for (const key of ['navigator', 'location']) {
  try { Object.defineProperty(globalThis, key, {value:win[key],
    configurable:true, writable:true}); } catch (_) {}
}
(async () => {
  let importError = '';
  try { await import(pathToFileURL(process.argv[2]).href); }
  catch (error) { importError = String(error?.stack || error); }
  await new Promise((resolve) => setTimeout(resolve, 100));
  const unresolved = symbols.filter((name) => typeof globalThis[name] !== 'function');
  console.log('__RESULT__ ' + JSON.stringify({importError, unresolved}));
  process.exit(0);
})();
"""


@pytest.mark.skipif(
    not shutil.which("node") or not (ROOT / "node_modules/jsdom").is_dir(),
    reason="node + jsdom are required",
)
def test_built_runtime_imports_without_undefined_retired_owner():
    harness = ROOT / "tests/_runtime_scope_smoke.cjs"
    loader = ROOT / "tests/_runtime_scope_loader.mjs"
    harness.write_text(_HARNESS, encoding="utf-8")
    loader.write_text(_LOADER, encoding="utf-8")
    try:
        result = subprocess.run(
            [
                shutil.which("node"), "--import", str(loader), str(harness),
                str(_main_asset()), str(ROOT),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            env={
                **os.environ,
                "SEAM_SYMBOLS": json.dumps(
                    _runtime_scope_seam_symbols(_runtime_source()),
                ),
            },
        )
    finally:
        harness.unlink(missing_ok=True)
        loader.unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr
    line = next(
        (item for item in result.stdout.splitlines()
         if item.startswith("__RESULT__ ")),
        None,
    )
    assert line, result.stdout
    payload = json.loads(line.removeprefix("__RESULT__ "))
    assert not payload["importError"], payload["importError"]
    assert payload["unresolved"] == []
