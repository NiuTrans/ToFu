"""Typed cookie-capture completion subscription and production composition."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
OWNER = ROOT / 'frontend/src/core/cookie-capture-consent.ts'
OWNER_BUNDLE = native_module_path(
    '.native/cookie-capture-consent-contract.js',
    OWNER,
)


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_cookie_capture_public_behavior_and_lifecycle():
    script = r'''
const fs = require('fs');
eval(fs.readFileSync(OWNER_PATH, 'utf8'));
const toasts = [];
let frameHandler = null;
let unsubscribed = false;
const controller = createCookieCaptureConsentController({
  subscribe(channel, taskId, handler) {
    if (channel === 'cookie_capture' && taskId === 'consent') {
      frameHandler = handler;
    }
  },
  unsubscribe(channel, taskId, handler) {
    if (channel === 'cookie_capture' && taskId === 'consent'
        && handler === frameHandler) unsubscribed = true;
  },
  showToast(message, kind) { toasts.push({ message, kind }); },
  translate(key) { return key + ' {domain}'; },
});
const checks = [];
const check = (name, value) => checks.push((value ? 'PASS ' : 'FAIL ') + name);

check('controller_registers_one_typed_subscription',
  controller.source === 'typed' && typeof frameHandler === 'function');
frameHandler({ type: 'captured', domain: 'login.example.com' });
check('captured_frame_toasts_domain_and_success_kind',
  toasts.length === 1 && toasts[0].message === 'cc.captured login.example.com'
    && toasts[0].kind === 'success');
frameHandler({ type: 'request', domain: 'ignored.example.com' });
frameHandler(null);
frameHandler('captured');
check('non_completion_and_malformed_frames_are_ignored', toasts.length === 1);
controller.destroy();
controller.destroy();
controller.handleFrame({ type: 'captured', domain: 'after.destroy' });
check('destroy_is_idempotent_and_suppresses_delivery',
  unsubscribed && toasts.length === 1);
check('controller_port_is_immutable', Object.isFrozen(controller));

console.log(checks.join('\n'));
if (checks.some(line => line.startsWith('FAIL'))) process.exitCode = 1;
'''.replace('OWNER_PATH', json.dumps(OWNER_BUNDLE))
    proc = subprocess.run(
        ['node', '-e', script], cwd=ROOT,
        capture_output=True, text=True, timeout=20,
    )
    output = (proc.stdout or '') + (proc.stderr or '')
    assert proc.returncode == 0, output
    assert output.count('PASS') == 5, output
