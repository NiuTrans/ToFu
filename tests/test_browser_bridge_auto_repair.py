"""Drift-guard: browser-bridge ZERO-INPUT auto re-pair (extension 4.7.0).

WHY
---
2026-08-04 owner decree: the extension must NEVER ask the user for a
credential or a manual network step — pairing and recovery are entirely
the software's job. Two measured dead ends this kills:

  * a stale/revoked bridge key parked the bridge at "set the Bridge
    Secret in the popup" — a secret the user could only mint FROM the
    panel, by hand (measured 401 ×279 on the owner's machine);
  * behind an SSO-fronted proxy the poll never even reached Tofu (the
    extension fetch carried no browser cookies), so pasting a secret
    fixed nothing — the 401 came from the edge, not the bridge gate.

The fix, pinned here:

  1. the poll fetch carries ``credentials: 'include'`` — the browser's
     own SSO session passes the edge (host_permissions <all_urls> makes
     Chrome attach it on the cross-origin extension fetch);
  2. a 401 is CLASSIFIED by body: Tofu's own ``bridge_auth_required``
     envelope means the key is stale, anything else means an edge
     intercept — the two are not fixed the same way;
  3. every 401 kicks ``attemptAutoRepair``: mint a fresh agents:bridge
     key through the page context of an open Tofu tab (the panel's OWN
     mint call — the user's session authorizes it), falling back to a
     cooldown-bound hidden tab, and to a foreground tab only from the
     popup's user gesture;
  4. no string anywhere in the extension tells the user to paste a
     secret or open a tunnel; the manual key field survives only inside
     a collapsed <details> in the popup;
  5. both manifests moved to 4.7.0 together (the contract the server
     and the web-store build read).

background.js has no JS harness in this repo — pins are source-level,
the same convention as test_browser_bridge_auth_backoff.py.
"""

import json
import os
import re

import pytest

pytestmark = pytest.mark.unit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel='browser_extension/background.js'):
    with open(os.path.join(REPO, rel), encoding='utf-8') as f:
        return f.read()


def _extract_fn_body(src, fn_signature):
    start = src.index(fn_signature)
    brace = src.index('{', start)
    depth = 0
    i = brace
    while i < len(src):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return src[brace:i + 1]
        i += 1
    raise AssertionError('unbalanced braces for ' + fn_signature)


def _poll_body(src):
    return _extract_fn_body(src, 'async function poll(')


# ── 1. Transport: the browser's own session rides the poll ────────────

def test_poll_fetch_carries_browser_cookies():
    body = _poll_body(_src())
    i_fetch = body.index('fetch(')
    segment = body[i_fetch:i_fetch + 900]
    assert "credentials: 'include'" in segment, (
        'the poll fetch must carry the browser’s own cookies — behind an '
        'SSO-fronted proxy the bridge secret alone can never pass the '
        'edge, and the 401 that results is unfixable by any secret the '
        'user could paste (measured 2026-08-04)')


def test_401_is_classified_by_body():
    body = _poll_body(_src())
    i401 = body.index('resp.status === 401')
    branch = body[i401:i401 + 2200]
    assert 'resp.json()' in branch, (
        'the 401 branch must READ the body to classify the failure')
    assert "errBody.error === 'bridge_auth_required'" in branch, (
        "Tofu's own bridge 401 envelope ({error:'bridge_auth_required'}) "
        'is the only 401 a fresh key can fix — an edge intercept 401 '
        'needs the browser session, not a secret')


def test_every_401_kicks_the_auto_repair_ladder():
    body = _poll_body(_src())
    i401 = body.index('resp.status === 401')
    branch = body[i401:i401 + 2200]
    assert 'attemptAutoRepair()' in branch, (
        'a 401 must kick the silent re-pair ladder — parking at a probe '
        'and waiting for a human to paste a secret is the dead end this '
        'feature exists to remove')


# ── 2. The ladder itself ───────────────────────────────────────────────

def test_auto_repair_ladder_shape():
    src = _src()
    for fn in ('_tofuMintBridgeKey', '_mintKeyViaTab', 'attemptAutoRepair'):
        assert f'function {fn}' in src, f'{fn} missing — the ladder is gone'
    body = _extract_fn_body(src, 'async function attemptAutoRepair(')
    assert 'chrome.tabs.query({})' in body, (
        'the ladder must first look for an ALREADY-OPEN Tofu tab')
    assert '_isOwnServerTab(t)' in body and 'tab.url.startsWith(SERVER_URL)' in _src(), (
        'a tab only counts when it belongs to OUR server — minting '
        'against a different Tofu would pair the wrong account')
    assert '_mintKeyViaTab(' in body, (
        'the open-tab mint is the ladder’s first (invisible) rung')
    assert 'REPAIR_TAB_COOLDOWN' in body, (
        'opening a tab ourselves must be cooldown-bound — a permanently '
        'dead server may never flash a tab every five minutes')
    assert 'opts.forceTab' in body, (
        'a FOREGROUND tab is reserved for the popup button’s user gesture')


def test_page_mint_uses_the_apps_own_api_client():
    body = _extract_fn_body(_src(), 'function _tofuMintBridgeKey(')
    assert 'window.Api' in body and 'api.desktop.mintToken' in body, (
        'the page-context mint must ride the app’s OWN Api client — '
        'whatever auth the app carries (cookie session / SSO / bearer) '
        'then applies exactly as it does for the panel’s mint button')
    assert 'token' in body, 'the mint must return the fresh key material'


def test_tab_mint_executes_in_main_world_and_adopts_the_key():
    body = _extract_fn_body(_src(), 'async function _mintKeyViaTab(')
    assert "world: 'MAIN'" in body, (
        'the mint must run in the page’s MAIN world — the isolated world '
        'has no window.Api')
    assert 'func: _tofuMintBridgeKey' in body
    assert 'setBridgeSecret(r.token)' in body, (
        'an adopted key must go through setBridgeSecret — it resets the '
        'auth backoff and polls immediately, so the repair is visible '
        'within a beat')


def test_repair_is_single_flight_and_exposed():
    src = _src()
    assert '_repairInFlight' in src, (
        'overlapping repair ladders (one per backed-off 401) would mint '
        'a key per attempt — the flight guard must exist')
    i = src.index("msg.type === 'getStatus'")
    assert 'repairBusy' in src[i:i + 700], (
        'getStatus must expose the in-flight repair so the popup can '
        'show busy instead of inviting a second click')


def test_popup_button_rides_repair_now():
    src = _src()
    assert "msg.type === 'repairNow'" in src, (
        'the popup’s re-pair button needs its message handler')
    i = src.index("msg.type === 'repairNow'")
    block = src[i:i + 400]
    assert 'forceTab: true' in block, (
        'the popup click is a real user gesture — THAT is when a '
        'foreground Tofu tab is allowed')


# ── 3. The copy never asks for a credential or a tunnel ───────────────

def test_no_manual_secret_instruction_anywhere():
    src = _src()
    assert 'set the Bridge Secret' not in src, (
        'the old "set the Bridge Secret in the popup" instruction is the '
        'dead end this feature removes — it must never come back')
    assert 're-pair needed' not in src, (
        "'re-pair needed' implies a pending human chore — the ladder "
        're-pairs by itself, so the copy must say so')


def test_repairing_copy_is_honest_about_automation():
    body = _poll_body(_src())
    i401 = body.index('resp.status === 401')
    branch = body[i401:i401 + 2200]
    assert 're-pairing automatically' in branch, (
        'the 401 copy must say the bridge is healing itself — never '
        'imply a human chore is pending')


def test_popup_demotes_the_manual_key_field():
    html = _src('browser_extension/popup.html')
    i_secret = html.index("id=\"bridgeSecret\"")
    head = html[:i_secret]
    assert '<details' in head and (
        '</details>' not in head
        or head.rindex('<details') > head.rindex('</details>')), (
        'the manual bridge-key input must live INSIDE a collapsed '
        '<details> — a visible secret field invites the exact manual step '
        'the 2026-08-04 decree forbids')
    assert 'id="repairRow"' in html and 'id="repairBtn"' in html, (
        'the popup’s ONE visible remedy is the automatic re-pair row')


def test_popup_js_wires_the_repair_row():
    js = _src('browser_extension/popup.js')
    assert "repairRow.style.display" in js and 'needsRepair' in js, (
        'the repair row must appear exactly when the background declares '
        'the credential dead')
    assert "{ type: 'repairNow' }" in js, (
        'the repair button must trigger the ladder’s user-gesture variant')


def test_poll_reports_the_extension_version():
    """The stranded-fleet telemetry (2026-08-04): without a version on the
    wire, the server cannot tell 'installed but outdated' from 'current',
    and a 401-parked old install is indistinguishable from 'never
    installed'."""
    src = _src()
    body = _poll_body(src)
    assert 'extVersion: EXT_VERSION' in body, (
        'the poll body must carry the extension’s own version')
    assert 'chrome.runtime.getManifest()' in src, (
        'the reported version must come from the manifest itself — a '
        'hardcoded twin reads as "you didn’t update" (measured drift, '
        'the v4.3 badge incident)')


# ── 4. Version contract ────────────────────────────────────────────────

def test_manifests_moved_to_470_together():
    dev = json.loads(_src('browser_extension/manifest.json'))
    store = json.loads(_src('docs/chrome-web-store/manifest.store.json'))
    assert dev['version'] == store['version'], 'manifest skew'
    major, minor, _ = (int(x) for x in dev['version'].split('.'))
    assert (major, minor) >= (4, 7), (
        'auto re-pair landed without a version bump — installed bridges '
        'cannot be told apart from the manual-secret generation')


# ── 5. NEUTER — prove the pins bite ────────────────────────────────────

def test_NEUTER_dropping_the_cookie_ride_is_caught():
    src = _src()
    neutered = src.replace("credentials: 'include',", '')
    assert neutered != src, 'NEUTER did not remove the credentials ride'
    body = _poll_body(neutered)
    i_fetch = body.index('fetch(')
    assert "credentials: 'include'" not in body[i_fetch:i_fetch + 900], (
        'sanity: without the cookie ride the transport pin above goes red')


def test_NEUTER_dropping_the_repair_kick_is_caught():
    src = _src()
    neutered = src.replace('attemptAutoRepair().catch(() => {});', '')
    assert neutered != src, 'NEUTER did not remove the repair kick'
    body = _poll_body(neutered)
    i401 = body.index('resp.status === 401')
    assert 'attemptAutoRepair()' not in body[i401:i401 + 2200], (
        'sanity: without the kick the ladder pin above goes red')


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v', '-p', 'no:napari']))
