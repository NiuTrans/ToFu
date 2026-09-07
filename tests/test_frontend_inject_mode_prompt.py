"""Integration contract for the typed chooser and send-mode prompt."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import native_module_path, runtime_section_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
DIALOG_BUNDLE = native_module_path(
    "dialog-controller.js",
    ROOT / "frontend/src/dialog-controller.ts",
)
SEND_PIPELINE = runtime_section_path("main/main_send_pipeline.js")
HAS_BROWSER_DEPS = bool(
    shutil.which("node")
    and (ROOT / "node_modules/jsdom/package.json").is_file()
)


_HARNESS = r"""
const fs = require('fs');
const {JSDOM} = require('jsdom');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
const sendSource = fs.readFileSync(process.argv[2], 'utf8');
const functionMatch = sendSource.match(
  /async function _promptInjectMode\([\s\S]*?\n\}\n/,
);
if (!functionMatch) throw new Error('_promptInjectMode not found');

const dom = new JSDOM('<!doctype html><body></body>', {
  url: 'http://tofu.test/',
});
const document = dom.window.document;
const schedule = {
  setTimeout: (callback, delayMs) => setTimeout(callback, delayMs),
  clearTimeout: (handle) => clearTimeout(handle),
  setInterval: (callback, delayMs) => setInterval(callback, delayMs),
  clearInterval: (handle) => clearInterval(handle),
  requestAnimationFrame: (callback) => setTimeout(
    () => callback(Date.now()), 0,
  ),
  cancelAnimationFrame: (handle) => clearTimeout(handle),
};
const appDialogController = createAppDialogController({
  document,
  schedule,
  copy: {
    confirm: () => 'Confirm',
    cancel: () => 'Cancel',
    ok: () => 'OK',
  },
  log: {warn: () => undefined},
});
let showChoice = appDialogController.showChoice;
const t = (key) => key;
const conversations = [];
const runtimeScope = {
  ConversationTurnRead: {
    activeMainAttemptId: (conversation) => conversation?.activeTaskId || null,
  },
};
eval(functionMatch[0]);

function clickNewestChoice(index) {
  setTimeout(() => {
    const overlays = [...document.querySelectorAll('.app-dialog-overlay')];
    const buttons = overlays.at(-1)?.querySelectorAll('.app-choice-btn');
    buttons?.[index]?.click();
  }, 15);
}

function key(value) {
  document.dispatchEvent(new dom.window.KeyboardEvent(
    'keydown', {key: value, bubbles: true, cancelable: true},
  ));
}

(async () => {
  const observed = {};

  conversations.push({id: 'c1', activeTaskId: 'attempt-1'});
  clickNewestChoice(0);
  observed.pickSteer = await _promptInjectMode('c1');
  clickNewestChoice(1);
  observed.pickQueue = await _promptInjectMode('c1');

  conversations.splice(0, conversations.length, {
    id: 'c2', activeTaskId: 'attempt-2',
  });
  const autoResolve = _promptInjectMode('c2');
  setTimeout(() => { conversations[0].activeTaskId = null; }, 30);
  observed.autoResolve = await Promise.race([
    autoResolve,
    new Promise((resolve) => setTimeout(() => resolve('__TIMEOUT__'), 2000)),
  ]);

  const navigation = showChoice({
    title: 'Navigation',
    options: [
      {value: 'steer', label: 'Steer', accent: true},
      {value: 'queue', label: 'Queue'},
    ],
    dismissValue: 'queue',
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  const overlay = [...document.querySelectorAll('.app-dialog-overlay')].at(-1);
  const buttons = [...overlay.querySelectorAll('.app-choice-btn')];
  observed.firstFocus = document.activeElement === buttons[0];
  key('ArrowDown');
  observed.downMoves = document.activeElement === buttons[1];
  key('ArrowDown');
  observed.downWraps = document.activeElement === buttons[0];
  key('ArrowUp');
  observed.upWraps = document.activeElement === buttons[1];
  buttons[1].click();
  observed.navigationResult = await navigation;

  const installedShowChoice = showChoice;
  showChoice = undefined;
  observed.withoutDialog = await _promptInjectMode('missing');
  showChoice = installedShowChoice;

  appDialogController.destroy();
  observed.overlaysAfterDestroy = document.querySelectorAll(
    '.app-dialog-overlay',
  ).length;
  console.log(JSON.stringify(observed));
})().catch((error) => {
  console.log(JSON.stringify({error: String(error?.stack || error)}));
  process.exitCode = 1;
});
"""


@pytest.fixture(scope="module")
def inject_mode_behavior() -> dict:
    if not HAS_BROWSER_DEPS:
        pytest.skip("node + jsdom dev dependencies are required")
    result = subprocess.run(
        ["node", "-e", _HARNESS, DIALOG_BUNDLE, SEND_PIPELINE],
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


def test_prompt_returns_the_selected_channel(inject_mode_behavior: dict):
    assert inject_mode_behavior["pickSteer"] == "steer"
    assert inject_mode_behavior["pickQueue"] == "queue"


def test_prompt_auto_resolves_when_the_turn_ends(inject_mode_behavior: dict):
    assert inject_mode_behavior["autoResolve"] == "queue"


def test_choice_keyboard_navigation_wraps(inject_mode_behavior: dict):
    assert inject_mode_behavior["firstFocus"] is True
    assert inject_mode_behavior["downMoves"] is True
    assert inject_mode_behavior["downWraps"] is True
    assert inject_mode_behavior["upWraps"] is True
    assert inject_mode_behavior["navigationResult"] == "queue"


def test_prompt_falls_back_without_dialog_service(inject_mode_behavior: dict):
    assert inject_mode_behavior["withoutDialog"] == "queue"
    assert inject_mode_behavior["overlaysAfterDestroy"] == 0
