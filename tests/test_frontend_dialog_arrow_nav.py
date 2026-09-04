"""Behavior contract for the typed application-dialog controller."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import native_module_path, runtime_section_names


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
DIALOG_SOURCE = ROOT / "frontend/src/dialog-controller.ts"
DIALOG_BUNDLE = native_module_path("dialog-controller.js", DIALOG_SOURCE)
LAZY_DIALOG_BUNDLE = native_module_path(
    "lazy-dialog-controller.js",
    ROOT / "frontend/src/lazy-dialog-controller.ts",
)
HAS_BROWSER_DEPS = bool(
    shutil.which("node")
    and (ROOT / "node_modules/jsdom/package.json").is_file()
)


_HARNESS = r"""
const fs = require('fs');
const {JSDOM} = require('jsdom');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
(0, eval)(fs.readFileSync(process.argv[2], 'utf8'));

function createScheduler() {
  let nextHandle = 1;
  const jobs = new Map();
  const create = (kind, callback, delayMs = 0) => {
    const handle = nextHandle++;
    jobs.set(handle, {kind, callback, delayMs, active: true});
    return handle;
  };
  const clear = (handle) => {
    const job = jobs.get(handle);
    if (job) job.active = false;
  };
  const fire = (kind) => {
    for (const job of jobs.values()) {
      if (!job.active || job.kind !== kind) continue;
      if (kind !== 'interval') job.active = false;
      job.callback();
    }
  };
  return {
    port: {
      setTimeout: (callback, delayMs) => create('timeout', callback, delayMs),
      clearTimeout: clear,
      setInterval: (callback, delayMs) => create('interval', callback, delayMs),
      clearInterval: clear,
      requestAnimationFrame: (callback) => create('frame', callback),
      cancelAnimationFrame: clear,
    },
    fire,
    active: (kind) => [...jobs.values()].filter(
      (job) => job.active && job.kind === kind,
    ).length,
  };
}

function createEnvironment() {
  const dom = new JSDOM(
    '<!doctype html><body><button id="before">before</button></body>',
    {url: 'http://tofu.test/'},
  );
  const document = dom.window.document;
  const scheduler = createScheduler();
  const keyListeners = new Set();
  const addEventListener = document.addEventListener.bind(document);
  const removeEventListener = document.removeEventListener.bind(document);
  document.addEventListener = (type, listener, options) => {
    if (type === 'keydown') keyListeners.add(listener);
    return addEventListener(type, listener, options);
  };
  document.removeEventListener = (type, listener, options) => {
    if (type === 'keydown') keyListeners.delete(listener);
    return removeEventListener(type, listener, options);
  };
  const ports = {
    document,
    schedule: scheduler.port,
    copy: {
      confirm: () => 'Confirm',
      cancel: () => 'Cancel',
      ok: () => 'OK',
    },
    log: {warn: () => undefined},
  };
  const controller = createAppDialogController(ports);
  const key = (value) => document.dispatchEvent(new dom.window.KeyboardEvent(
    'keydown', {key: value, bubbles: true, cancelable: true},
  ));
  return {dom, document, scheduler, keyListeners, ports, controller, key};
}

(async () => {
  const observed = {};

  {
    const env = createEnvironment();
    const previous = env.document.getElementById('before');
    previous.focus();
    const promise = env.controller.showConfirm(
      '<img src=x onerror=bad>\nsecond line',
      {okText: 'Clean', cancelText: 'Keep'},
    );
    env.scheduler.fire('frame');
    const ok = env.document.querySelector('.app-dialog-ok');
    const cancel = env.document.querySelector('.app-dialog-cancel');
    const message = env.document.querySelector('.app-dialog-message');
    observed.defaultFocus = env.document.activeElement === ok;
    observed.safeMessage = message.textContent === '<img src=x onerror=bad>second line'
      && message.querySelectorAll('img').length === 0
      && message.querySelectorAll('br').length === 1;
    env.key('ArrowLeft');
    observed.leftFocus = env.document.activeElement === cancel;
    env.key('ArrowRight');
    observed.rightFocus = env.document.activeElement === ok;
    env.key('ArrowLeft');
    env.key('Enter');
    observed.focusedResult = await promise;
    observed.focusRestored = env.document.activeElement === previous;
    observed.listenerReleased = env.keyListeners.size === 0;
    env.scheduler.fire('timeout');
    observed.overlayRemoved = env.document.querySelector('.app-dialog-overlay') === null;
  }

  {
    const env = createEnvironment();
    const promise = env.controller.showPrompt('Name', {defaultValue: 'before'});
    env.scheduler.fire('frame');
    const input = env.document.querySelector('.app-dialog-input');
    input.value = 'programmatic update';
    env.key('Enter');
    observed.promptValue = await promise;
    env.scheduler.fire('timeout');
    const cancelled = env.controller.showPrompt('Cancel me');
    env.scheduler.fire('frame');
    env.key('Escape');
    observed.promptCancel = await cancelled;
  }

  {
    const env = createEnvironment();
    const first = env.controller.showConfirm('first');
    const second = env.controller.showPrompt('second');
    observed.replacedResult = await first;
    observed.singleOverlay = env.document.querySelectorAll(
      '.app-dialog-overlay',
    ).length === 1;
    observed.frameBeforeDestroy = env.scheduler.active('frame');
    env.controller.destroy();
    observed.destroyedResult = await second;
    observed.destroyRemovedOverlay = env.document.querySelector(
      '.app-dialog-overlay',
    ) === null;
    observed.destroyReleasedResources = env.keyListeners.size === 0
      && env.scheduler.active('frame') === 0
      && env.scheduler.active('interval') === 0
      && env.scheduler.active('timeout') === 0;
    observed.afterDestroy = await env.controller.showAlert('ignored');
    observed.afterDestroyHasOverlay = !!env.document.querySelector(
      '.app-dialog-overlay',
    );
  }

  {
    const env = createEnvironment();
    let loadCalls = 0;
    let createCalls = 0;
    let releaseModule;
    const lazyController = createDialogServices(env.ports, () => {
      loadCalls += 1;
      return new Promise((resolve) => { releaseModule = resolve; });
    });
    const pendingConfirm = lazyController.showConfirm('pending');
    const pendingPrompt = lazyController.showPrompt('pending');
    observed.lazyLoadCalls = loadCalls;
    lazyController.destroy();
    releaseModule({
      createAppDialogController: () => {
        createCalls += 1;
        return env.controller;
      },
    });
    observed.lazyConfirmAfterDestroy = await pendingConfirm;
    observed.lazyPromptAfterDestroy = await pendingPrompt;
    observed.lazyCreateCalls = createCalls;
  }

  console.log(JSON.stringify(observed));
})().catch((error) => {
  console.log(JSON.stringify({error: String(error?.stack || error)}));
  process.exitCode = 1;
});
"""


@pytest.fixture(scope="module")
def dialog_behavior() -> dict:
    if not HAS_BROWSER_DEPS:
        pytest.skip("node + jsdom dev dependencies are required")
    result = subprocess.run(
        ["node", "-e", _HARNESS, DIALOG_BUNDLE, LAZY_DIALOG_BUNDLE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert "error" not in payload, payload.get("error")
    return payload


def test_confirm_keyboard_navigation_and_safe_rendering(dialog_behavior: dict):
    assert dialog_behavior["defaultFocus"] is True
    assert dialog_behavior["leftFocus"] is True
    assert dialog_behavior["rightFocus"] is True
    assert dialog_behavior["focusedResult"] is False
    assert dialog_behavior["safeMessage"] is True
    assert dialog_behavior["focusRestored"] is True
    assert dialog_behavior["listenerReleased"] is True
    assert dialog_behavior["overlayRemoved"] is True


def test_prompt_reads_the_input_at_settlement(dialog_behavior: dict):
    assert dialog_behavior["promptValue"] == "programmatic update"
    assert dialog_behavior["promptCancel"] is None


def test_replacement_and_destroy_are_bounded(dialog_behavior: dict):
    assert dialog_behavior["replacedResult"] is False
    assert dialog_behavior["singleOverlay"] is True
    assert dialog_behavior["frameBeforeDestroy"] == 1
    assert dialog_behavior["destroyedResult"] is None
    assert dialog_behavior["destroyRemovedOverlay"] is True
    assert dialog_behavior["destroyReleasedResources"] is True
    assert dialog_behavior["afterDestroy"] is False
    assert dialog_behavior["afterDestroyHasOverlay"] is False


def test_lazy_facade_coalesces_and_fails_safe_on_destroy(dialog_behavior: dict):
    assert dialog_behavior["lazyLoadCalls"] == 1
    assert dialog_behavior["lazyConfirmAfterDestroy"] is False
    assert dialog_behavior["lazyPromptAfterDestroy"] is None
    assert dialog_behavior["lazyCreateCalls"] == 0


def test_legacy_dialog_section_is_retired():
    assert "core/dialog.js" not in runtime_section_names()
