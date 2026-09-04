"""Behavior and resource-bound contract for skill-package installation."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
OWNER = ROOT / 'frontend/src/features/skills/package-installer.ts'
OWNER_BUNDLE = native_module_path('.native/skill-package-installer.js', OWNER)


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_installer_bounds_uploads_and_drop_listeners():
    harness = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[1], 'utf8'));
const checks = [];
const check = (name, value) => checks.push((value ? 'PASS ' : 'FAIL ') + name);
class TestFormData {
  constructor() { this.values = new Map(); }
  append(name, value) { this.values.set(name, value); }
  get(name) { return this.values.get(name); }
}
global.FormData = TestFormData;

const calls = { installs: [], invalid: 0, installing: 0, rejected: [],
  installed: [], errors: [] };
let resolveUpload;
let installImplementation = (form) => new Promise((resolve) => {
  calls.installs.push(form); resolveUpload = resolve;
});
let pickScopeImplementation = () => 'project';
const installer = createSkillPackageInstaller({
  install: (form) => installImplementation(form),
  pickScope: () => pickScopeImplementation(),
  showInvalidFile: () => { calls.invalid += 1; },
  showInstalling: () => { calls.installing += 1; },
  showRejected: (detail) => calls.rejected.push(detail),
  showInstalled: (body) => calls.installed.push(body),
  showError: (error) => calls.errors.push(error),
});

(async () => {
  const first = installer.install({ name: 'one.zip', type: 'application/zip' });
  await Promise.resolve();
  const overlapping = installer.install({ name: 'two.zip', type: 'application/zip' });
  await Promise.resolve();
  check('only_one_upload_can_be_active',
    calls.installs.length === 1 && calls.installing === 1);
  resolveUpload({ ok: true, json: async () => ({ memory: { name: 'One' } }) });
  await Promise.all([first, overlapping]);
  check('form_uses_selected_scope_and_file',
    calls.installs[0].get('scope') === 'project'
      && calls.installs[0].get('file').name === 'one.zip');
  check('success_is_reported_once', calls.installed.length === 1);

  await installer.install({ name: 'plain.txt', type: 'text/plain' });
  check('non_zip_input_is_rejected_without_transport',
    calls.invalid === 1 && calls.installs.length === 1);

  installImplementation = async () => ({
    ok: false, statusText: 'Rejected', json: async () => ({ error: 'unsafe' }),
  });
  await installer.install({ name: 'bad.zip', type: '' });
  check('http_rejection_preserves_typed_detail', calls.rejected[0] === 'unsafe');
  installImplementation = async () => { throw new Error('offline'); };
  await installer.install({ name: 'offline.zip', type: '' });
  check('transport_error_is_visible', calls.errors[0]?.message === 'offline');

  const installsBeforeCancel = calls.installs.length;
  const toastsBeforeCancel = calls.installing;
  pickScopeImplementation = () => null;
  await installer.install({ name: 'cancelled.zip', type: 'application/zip' });
  check('cancelled_scope_choice_aborts_before_transport',
    calls.installs.length === installsBeforeCancel
      && calls.installing === toastsBeforeCancel);
  pickScopeImplementation = () => 'project';

  const listeners = new Map();
  const listenElement = { addEventListener(type, listener) {
    const rows = listeners.get(type) || []; rows.push(listener); listeners.set(type, rows);
  } };
  const classes = new Set();
  const highlightElement = { classList: {
    add: (name) => classes.add(name), remove: (name) => classes.delete(name),
  } };
  installer.attachDropZone(listenElement, highlightElement);
  installer.attachDropZone(listenElement, highlightElement);
  check('drop_zone_has_one_fixed_listener_set',
    ['dragenter','dragover','dragleave','drop'].every(
      (type) => listeners.get(type)?.length === 1));

  let prevented = 0;
  listeners.get('dragenter')[0]({
    dataTransfer: { types: ['Files'] }, preventDefault: () => { prevented += 1; },
  });
  check('file_drag_is_highlighted', prevented === 1 && classes.has('is-dragging'));
  listeners.get('drop')[0]({
    dataTransfer: { types: ['Files'], files: [{ name: 'bad.txt', type: '' }] },
    preventDefault: () => { prevented += 1; },
  });
  check('rejected_drop_clears_highlight',
    calls.invalid === 2 && !classes.has('is-dragging') && prevented === 2);

  const input = { files: [{ name: 'input.zip', type: '' }], value: 'selected' };
  installImplementation = async () => ({ ok: true, json: async () => ({}) });
  installer.installFromInput(input);
  await Promise.resolve(); await Promise.resolve();
  check('file_input_can_reselect_the_same_package', input.value === '');

  console.log(checks.join('\n'));
  if (checks.some((line) => line.startsWith('FAIL'))) process.exitCode = 1;
})().catch((error) => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        ['node', '-e', harness, OWNER_BUNDLE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = (result.stdout or '') + (result.stderr or '')
    assert result.returncode == 0, output
    assert output.count('PASS') == 11, output
