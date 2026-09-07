"""Android native-shell bridge contract: visibility folding, reauth gate,
gateway-auth discrimination.

The Android WebView shell and the SPA talk through two narrow seams owned by
frontend/src/core/native-bridge.ts:

- ``tofu:native-visibility`` — a WebView never flips document.visibilityState
  when the app backgrounds, so budget layers must fold the shell signal with
  the document state (OR semantics) or they keep hammering the vscode proxy
  tunnel from the user's pocket.
- ``TofuNative.requestReauth`` — when the outer gateway's session cookie dies
  mid-page, only the shell can re-login headlessly; the gate rate-limits so a
  burst of 401ing polls cannot storm the login endpoint.

Plus the wire discrimination shared with the Android probe: only a bare-string
edge 401/403 (no Tofu envelope, no problem detail) may trigger reauth.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
NATIVE_BRIDGE = ROOT / "frontend/src/core/native-bridge.ts"


def _run_node(script: str, *paths: Path) -> dict:
    result = subprocess.run(
        ["node", "-e", script, *(str(path) for path in paths)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_visibility_folds_shell_and_document_with_or_semantics() -> None:
    bundle = native_module_path("native-bridge-owner.js", NATIVE_BRIDGE)
    output = _run_node(
        r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

let docHidden = false;
let nativeListener = null;
const events = [];
const visibility = createNativeVisibility({
  subscribeNativeVisibility(listener) { nativeListener = listener; },
  documentHidden() { return docHidden; },
  native: { requestReauth() {} },
});
const unsubscribe = visibility.subscribe((hidden) => events.push(hidden));

const checks = {
  initiallyVisible: !visibility.isEffectivelyHidden(),
  shellDetected: visibility.isNativeShell(),
};
nativeListener(true);
// NEUTER: drop the nativeHidden half of the OR and this flips false — every
// budget layer would keep polling a backgrounded app.
checks.hiddenByShell = visibility.isEffectivelyHidden() && visibility.isHidden();
nativeListener(true);  // repeat — no flip, no event
docHidden = true;
nativeListener(false); // shell foregrounds but the document is hidden
checks.documentKeepsHidden = visibility.isEffectivelyHidden() && !visibility.isHidden();
docHidden = false;
nativeListener(true);
nativeListener(false); // both visible now — one flip to visible
checks.visibleAgain = !visibility.isEffectivelyHidden();
unsubscribe();
nativeListener(true);  // after unsubscribe: no further events
checks.events = events;
console.log(JSON.stringify(checks));
""",
        bundle,
    )
    assert output == {
        "initiallyVisible": True,
        "shellDetected": True,
        "hiddenByShell": True,
        "documentKeepsHidden": True,
        "visibleAgain": True,
        # Six listener invocations, only three effective flips: background,
        # background-again (document already hid us), foreground.
        "events": [True, True, False],
    }


def test_reauth_gate_rate_limits_normalizes_and_never_consumes_on_throw() -> None:
    bundle = native_module_path("native-bridge-owner.js", NATIVE_BRIDGE)
    output = _run_node(
        r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

const forwarded = [];
let now = 100000;
const gate = createNativeReauthGate({
  native: { requestReauth(reason) { forwarded.push(reason); } },
  now() { return now; },
});
const results = {
  available: gate.available(),
  first: gate.requestReauth('  api 401 GET /v4/meta  '),
  // NEUTER: drop the min-interval check and this forwards — a 401ing poll
  // burst would storm the gateway login endpoint.
  repeatInsideWindow: gate.requestReauth('again'),
};
now += 30000;
results.afterWindow = gate.requestReauth('x'.repeat(200));
now += 30000;
results.blankReason = gate.requestReauth('   ');
results.forwarded = forwarded;

const headless = createNativeReauthGate({ now() { return now; } });
results.headlessAvailable = headless.available();
results.headlessCall = headless.requestReauth('x');

const errors = [];
const failing = createNativeReauthGate({
  native: { requestReauth() { throw new Error('dead bridge'); } },
  now() { return now; },
  onError(error) { errors.push(String(error)); },
});
results.throwResult = failing.requestReauth('boom');
// A failed forward must NOT consume the rate budget: the immediate retry
// reaches the shell again (and fails again), proving lastForwardedAt stayed
// unset.
results.throwRetry = failing.requestReauth('boom again');
results.errorReports = errors.length;
console.log(JSON.stringify(results));
""",
        bundle,
    )
    assert output == {
        "available": True,
        "first": True,
        "repeatInsideWindow": False,
        "afterWindow": True,
        "blankReason": True,
        "forwarded": ["api 401 GET /v4/meta", "x" * 120, "unknown"],
        "headlessAvailable": False,
        "headlessCall": False,
        "throwResult": False,
        "throwRetry": False,
        "errorReports": 2,
    }


def test_gateway_auth_rejection_only_fires_on_bare_edge_401_403() -> None:
    bundle = native_module_path("native-bridge-owner.js", NATIVE_BRIDGE)
    output = _run_node(
        r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
console.log(JSON.stringify({
  // The code-server edge answers {"error":"Unauthorized"} — parsed by the
  // transport as no structured envelope. NEUTER: drop the envelope/problem
  // conjuncts and Tofu's own 401s would trigger a shell re-login loop.
  bare401: isGatewayAuthRejection(401, null, null),
  bare403: isGatewayAuthRejection(403, undefined, undefined),
  tofuEnvelope401: isGatewayAuthRejection(401, { ok: false, error: { code: 'auth' } }, null),
  problem401: isGatewayAuthRejection(401, null, { title: 'unauthorized' }),
  ok200: isGatewayAuthRejection(200, null, null),
}));
""",
        bundle,
    )
    assert output == {
        "bare401": True,
        "bare403": True,
        "tofuEnvelope401": False,
        "problem401": False,
        "ok200": False,
    }
