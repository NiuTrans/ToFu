"""Animation scheduling degrades safely in non-visual and older webviews."""

from __future__ import annotations

import json
import subprocess

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit


def test_animation_frame_scheduler_uses_native_frame_and_timer_fallback():
    bundle = native_module_path(
        "animation-frame-scheduler.js",
        "frontend/src/conversation/ui/animation-frame-scheduler.ts",
    )
    harness = r"""
require(process.argv[1]);
let nativeCancelled = 0;
let timerCancelled = 0;
let nativeValue = null;
let timerValue = null;
const nativeCancel = scheduleAnimationFrame({
  requestAnimationFrame(callback) { callback(42); return 7; },
  cancelAnimationFrame(handle) { nativeCancelled = handle; },
  setTimeout() { throw new Error('native path must not use timer'); },
  clearTimeout() {},
}, (value) => { nativeValue = value; });
nativeCancel();
const timerCancel = scheduleAnimationFrame({
  setTimeout(callback, delay) { callback(); return delay; },
  clearTimeout(handle) { timerCancelled = handle; },
}, (value) => { timerValue = value; });
timerCancel();
let throwingNativeValue = null;
let throwingTimerCancelled = 0;
const throwingCancel = scheduleAnimationFrame({
  requestAnimationFrame() { throw new Error('native denied'); },
  cancelAnimationFrame() { throw new Error('cancel denied'); },
  setTimeout(callback, delay) { callback(); return delay + 1; },
  clearTimeout(handle) { throwingTimerCancelled = handle; },
}, (value) => { throwingNativeValue = value; });
throwingCancel();
let cancelThrowEscaped = false;
const nativeThrowingCancel = scheduleAnimationFrame({
  requestAnimationFrame() { return 9; },
  cancelAnimationFrame() { throw new Error('cancel denied'); },
  setTimeout() { throw new Error('timer unused'); },
  clearTimeout() {},
}, () => {});
try { nativeThrowingCancel(); } catch { cancelThrowEscaped = true; }
let getterFallbackValue = null;
let getterTimerCancelled = 0;
const getterCancel = scheduleAnimationFrame({
  get requestAnimationFrame() { throw new Error('frame property denied'); },
  get cancelAnimationFrame() { throw new Error('cancel property denied'); },
  setTimeout(callback, delay) { callback(); return delay + 2; },
  clearTimeout(handle) { getterTimerCancelled = handle; },
}, (value) => { getterFallbackValue = value; });
getterCancel();
let cancelGetterEscaped = false;
const nativeThrowingCancelGetter = scheduleAnimationFrame({
  requestAnimationFrame() { return 10; },
  get cancelAnimationFrame() { throw new Error('cancel property denied'); },
  setTimeout() { throw new Error('timer unused'); },
  clearTimeout() {},
}, () => {});
try { nativeThrowingCancelGetter(); } catch { cancelGetterEscaped = true; }
console.log(JSON.stringify({
  nativeValue,
  nativeCancelled,
  timerValueIsNumber: typeof timerValue === 'number',
  timerCancelled,
  throwingNativeValueIsNumber: typeof throwingNativeValue === 'number',
  throwingTimerCancelled,
  cancelThrowEscaped,

  getterFallbackValueIsNumber: typeof getterFallbackValue === 'number',
  getterTimerCancelled,
  cancelGetterEscaped,
}));
"""
    completed = subprocess.run(
        ["node", "-e", harness, bundle],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "nativeValue": 42,
        "nativeCancelled": 7,
        "timerValueIsNumber": True,
        "timerCancelled": 16,
        "throwingNativeValueIsNumber": True,
        "throwingTimerCancelled": 17,
        "cancelThrowEscaped": False,

        "getterFallbackValueIsNumber": True,
        "getterTimerCancelled": 18,
        "cancelGetterEscaped": False,
    }
