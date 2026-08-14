"""tests/test_frontend_orch_warning_surface.py — validator-warning SURFACING.

Proves the last hop of the orchestration validator-warning path is not inert:
``validate_definition``'s warnings reach the browser (backend contract tested
in test_orchestrations.py), and the studio must render the warning **text** to
the author — not merely a count. Before this, ``_orchSave`` showed only
``Saved "X" (1 warning)`` so the parallel verdict-channel warning (and every
other validator warning) was effectively swallowed.

Loads the shipped ``orchestration-feedback.js`` controller and runs it under
jsdom. Skips when node+jsdom are absent.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
from tests._runtime_sections import orchestration_legacy_test_root

ROOT = orchestration_legacy_test_root()


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
global.window = dom.window; global.document = dom.window.document;
eval(fs.readFileSync(path.join(ROOT, 'static/js/orchestration-feedback.js'), 'utf8'));
const feedback = createOrchestrationFeedback({
  document,
  setTimeout: () => 0,
});

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// A parallel verdict-channel warning (the exact string family the validator
// now emits) must surface as READABLE TEXT, not just a count.
const warns = [
  "parallel 'p' region contains verdict-feeding producer(s) ['w1'] " +
  "(a verifier role or a shared-context producer) — the single-valued " +
  "feedback/directive channel is consumed order-dependently across " +
  "concurrent branches."];
document.querySelectorAll('.orch-toast').forEach(e => e.remove());
feedback.warn('Saved "Flow"', warns);
const toast = document.querySelector('.orch-toast');
check('toast_created', !!toast);
const txt = toast ? toast.textContent : '';
check('headline_has_prefix', txt.includes('Saved "Flow"'));
check('headline_has_count', txt.includes('1 warning'));
// The load-bearing assertion: the ACTUAL warning text is in the DOM.
check('detail_has_warning_text', txt.includes('verdict-feeding producer'));
check('detail_has_fix_hint', txt.includes('order-dependent'));
check('detail_node_named', txt.includes("['w1']"));
check('has_warn_class', !!(toast && toast.classList.contains('is-warn')));
check('detail_block_present', !!(toast && toast.querySelector('.orch-toast-detail')));

// No warnings → plain toast, no detail block, no warn styling.
document.querySelectorAll('.orch-toast').forEach(e => e.remove());
feedback.warn('Saved "Clean"', []);
const clean = document.querySelector('.orch-toast');
check('clean_no_detail', !!(clean && !clean.querySelector('.orch-toast-detail')));
check('clean_no_warn_class', !!(clean && !clean.classList.contains('is-warn')));
check('clean_headline', !!(clean && clean.textContent.includes('Saved "Clean"')));
check('clean_no_count', !!(clean && !/warning/.test(clean.textContent)));

document.querySelectorAll('.orch-toast').forEach(e => e.remove());
const localized = createOrchestrationFeedback({
  document,
  setTimeout: () => 0,
  translate: (key, params) => key === 'orch.feedback.warningCount'
    ? params.count + ' 条提醒' : key,
  issueMessages: values => values.map(value => value.message),
});
localized.warn('已保存', [{severity:'warning',message:'并行节点需要复核'}]);
const localizedText = document.querySelector('.orch-toast').textContent;
check('canonical_diagnostic_is_readable', localizedText.includes('并行节点需要复核'));
check('warning_count_is_localized', localizedText.includes('1 条提醒'));

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_orch_warning_text_surfaces():
    harness = os.path.join(HERE, '_orch_warning_surface_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ROOT],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'warning-surface failures:\n' + output
    assert output.count('PASS') >= 14, f'expected >=14 PASS, got:\n{output}'


def test_main_editor_delegates_feedback_to_shared_controller():
    editor = open(
        os.path.join(ROOT, 'static', 'js', 'orchestration.js'),
        encoding='utf-8',
    ).read()
    command_bridge = open(
        os.path.join(
            ROOT, 'static', 'js', 'orchestration-command-bridge.js',
        ),
        encoding='utf-8',
    ).read()
    feedback = open(
        os.path.join(ROOT, 'static', 'js', 'orchestration-feedback.js'),
        encoding='utf-8',
    ).read()
    studio_api = open(
        os.path.join(
            ROOT, 'frontend/src/features/orchestration/studio-api.ts'),
        encoding='utf-8',
    ).read()
    adapters = command_bridge[command_bridge.index('function _orchToast'):]
    assert 'createOrchestrationFeedback' in editor
    assert 'toast: _orchServices.toast' in editor
    assert '_orchStudioApi.toast' in adapters
    assert '_orchFeedback.warn' in adapters
    assert "call('toast', message, isError, toastOptions)" in studio_api
    assert "createElement('div')" not in adapters
    assert "createElement('div')" in feedback


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
