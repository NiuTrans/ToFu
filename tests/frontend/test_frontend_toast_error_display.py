"""Public backend diagnostics remain visible text in the classic Toast DOM."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._jsdom import JS_DIR, run_harness
from tests._runtime_sections import shipped_source_text

pytestmark = pytest.mark.unit


def test_toast_escapes_backend_error_text_without_losing_detail():
    body = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="toastContainer"></div></body>',
  targets: [process.argv[2]],
  globals: { runtimeScope: {} },
});

const diagnostic = 'ContractViolation: <img src=x onerror=alert(1)> turn.status';
showToast(diagnostic, 'error');
const simple = document.querySelector('.toast-title');
check('simple toast keeps the full diagnostic', simple.textContent === diagnostic);
check('simple toast does not create backend markup', !document.querySelector('img'));

showToast('❌', 'Load failed', diagnostic, 6000);
const detail = document.querySelectorAll('.toast-detail')[0];
check('full toast keeps the full diagnostic', detail.textContent === diagnostic);
check('full toast does not create backend markup', !document.querySelector('img'));

escapeHtml = undefined;
showToast(diagnostic, 'error');
const fallback = document.querySelectorAll('.toast-title')[2];
check('fallback escaping keeps the diagnostic', fallback.textContent === diagnostic);
check('fallback escaping does not create backend markup', !document.querySelector('img'));
report();
'''
    run_harness(
        target_js=str(Path(JS_DIR) / 'core' / 'toast.js'),
        body_js=body,
        min_pass=6,
    )


def test_failure_toast_renders_complete_multiline_ids_as_escaped_text():
    body = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="toastContainer"></div></body>',
  targets: [process.argv[2]],
  globals: { runtimeScope: {} },
});
const message = 'Internal server error\nRequest ID: full-request-abcdef1234567890\nConversation ID: mufmucgu8c8ljb<unsafe>';
showToast(message, 'error');
const title = document.querySelector('.toast.t-error .toast-title');
check('full diagnostic stays in toast text', title.textContent === message);
check('untrusted conversation id is not markup', !document.querySelector('unsafe'));
report();
'''
    run_harness(
        target_js=str(Path(JS_DIR) / 'core' / 'toast.js'),
        body_js=body,
        min_pass=2,
    )
    styles = shipped_source_text(
        'frontend/src/styles/application/14-bridges-memory-branches.css'
    )
    assert '.toast.t-error .toast-title {' in styles
    assert 'white-space: pre-wrap;' in styles
    assert 'user-select: text;' in styles
