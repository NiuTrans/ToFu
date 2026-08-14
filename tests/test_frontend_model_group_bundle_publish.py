#!/usr/bin/env python3
"""Prod-parity guard: bundle-world visibility of the model-group helpers
(bug class: harness/prod global-publish divergence).

WHY
---
``core/model_group.js`` publishes ``modelGroupKey`` / ``modelGroupLabel`` /
``modelGroupBrandNames`` onto ``runtimeScope``. In the jsdom TEST harness the
materialized sections are classic scripts and ``runtimeScope IS window``, so
the publishes create real globals — every consumer's bare
``typeof modelGroupKey === 'function'`` guard evaluates TRUE.

In the production ESM bundle ``runtimeScope`` is a module-private
``Object.create(null)`` (frontend/src/runtime/app-runtime.js, "Former
file-scope exports live here instead of leaking onto window"). A
runtimeScope-only publish therefore leaves those free-variable guards FALSE
in production: ``settings/visibility_defaults.js`` fell back to grouping by
``provider.brand`` VERBATIM, so the ChatGPT Codex subscription (brand
'oauth' — a credential kind, not a vendor) rendered its Settings-preset
group header as the grey generic smiley (2026-08-14). The 2026-08-10 fix
(2d800a5d) was correct but only ever exercised in the harness world.

The fix publishes the family on ``globalThis`` as well. This test loads the
REAL sections in a simulated BUNDLE world — a private ``runtimeScope``
object that is NOT the global — and asserts:

  1. the bare guards resolve: ``typeof modelGroupKey === 'function'`` (and
     Label / BrandNames) — the exact expressions the consumers use;
  2. the concrete case: the Codex subscription provider resolves to the
     'openai' vendor group, never the literal 'oauth' credential kind;
  3. NEUTER — stripping the globalThis publish block makes (1) fail, proving
     the guard bites and a revert cannot silently re-ship the smiley.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

import pytest

from tests._runtime_sections import (
    runtime_section as _section_source,
    runtime_section_path,
)

pytestmark = pytest.mark.unit

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..'))
NODE = os.environ.get('NODE_BIN', 'node')


def _node_available() -> bool:
    return subprocess.run(
        [NODE, '--version'], capture_output=True).returncode == 0


_NODE_BODY = r'''
const fs = require('fs');
const vm = require('vm');

// ── BUNDLE-WORLD simulation ──
// runtimeScope is PRIVATE (module-scope Object.create(null)), exactly like
// the ESM bundle — deliberately NOT the vm context global.
const ctx = { console };
// NOTE: do NOT pre-assign ctx.globalThis — the contextified sandbox mints
// its own globalThis; overwriting it would send the publishes to the OUTER
// object and bare lookups would miss them (a false negative).
vm.createContext(ctx);

vm.runInContext('const runtimeScope = Object.create(null);', ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);  // branding
vm.runInContext(fs.readFileSync(process.argv[3], 'utf8'), ctx);  // model_group

let pass = 0, fail = 0;
const check = (name, ok) => {
  if (ok) { pass++; console.log('PASS ' + name); }
  else { fail++; console.log('FAIL ' + name); }
};

// 1. The bare guards consumers actually write resolve in bundle world.
check('bare_modelGroupKey_is_function',
      vm.runInContext('typeof modelGroupKey === "function"', ctx));
check('bare_modelGroupLabel_is_function',
      vm.runInContext('typeof modelGroupLabel === "function"', ctx));
check('bare_modelGroupBrandNames_is_function',
      vm.runInContext('typeof modelGroupBrandNames === "function"', ctx));

// 2. The concrete case: the Codex subscription resolves to its VENDOR group.
//    try/catch per check so a ReferenceError (the NC world) cannot kill the
//    remaining checks — the neuter must still observe the runtimeScope path.
const evalOr = (expr) => { try { return vm.runInContext(expr, ctx); } catch (_e) { return undefined; } };
const codexKey = '"use strict"; (typeof modelGroupKey === "function") ? ' +
  'modelGroupKey({brand: "oauth", name: "ChatGPT (Codex subscription)"},' +
  ' {model_id: "gpt-5.6-sol"}) : undefined';
check('codex_subscription_is_openai', evalOr(codexKey) === 'openai');
check('codex_never_literal_oauth', evalOr(codexKey) !== 'oauth');

// 3. runtimeScope publish still present (the ESM/TofuModules read path).
check('runtimescope_publish_intact',
      vm.runInContext(
        'typeof runtimeScope.modelGroupKey === "function"', ctx));

console.log('__RESULT__ ' + pass + ' ' + fail);
process.exit(fail ? 1 : 0);
'''


def _run_bundle_world(model_group_src: str) -> tuple[str, int]:
    branding = runtime_section_path('settings/branding.js', scope_prelude=False)
    with tempfile.NamedTemporaryFile(
            'w', suffix='.js', delete=False, encoding='utf-8') as fh:
        mg_path = fh.name
        fh.write(model_group_src)
    with tempfile.NamedTemporaryFile(
            'w', suffix='.js', delete=False, encoding='utf-8') as fh:
        body_path = fh.name
        fh.write(_NODE_BODY)
    try:
        proc = subprocess.run(
            [NODE, body_path, branding, mg_path],
            capture_output=True, text=True, timeout=60, cwd=ROOT)
        # A nonzero exit is EXPECTED for the neuter case (its node body ends
        # process.exit(fail ? 1 : 0)) — surface both, let callers assert.
        if proc.returncode not in (0, 1) or not proc.stdout:
            raise AssertionError(
                f'node failed: {proc.stderr}\n{proc.stdout}')
        return proc.stdout, proc.returncode
    finally:
        os.remove(mg_path)
        os.remove(body_path)


def test_bundle_world_global_publish():
    """The model-group family must be reachable as BARE GLOBALS in the
    bundle world (private runtimeScope) — the exact shape every consumer's
    typeof guard needs in production."""
    src = _section_source('core/model_group.js', scope_prelude=False)
    out, rc = _run_bundle_world(src)
    assert rc == 0 and 'FAIL ' not in out, out
    assert out.count('PASS ') == 6, out


def test_NC_stripping_global_publish_reintroduces_degradation():
    """NEUTER: remove the globalThis publish (the pre-fix module) and the
    bare-guard assertion must FAIL — proving the guard has teeth."""
    src = _section_source('core/model_group.js', scope_prelude=False)
    marker = 'if (typeof globalThis !== \'undefined\') {'
    assert marker in src, 'NC anchor missing — test is stale'
    start = src.index(marker)
    end = src.index('}', src.index('globalThis.modelGroupBrandNames', start)) + 2
    bad = src[:start] + src[end:]
    assert bad != src
    out, rc = _run_bundle_world(bad)
    assert rc == 1, out
    assert 'FAIL bare_modelGroupKey_is_function' in out, out
    assert 'PASS runtimescope_publish_intact' in out, out


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
