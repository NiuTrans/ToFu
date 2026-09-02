"""tests/test_local_control_browser_probe.py — the extension-install guidance
must be driven by a real browser PROBE, not by ``remote_addr``.

WHAT THIS GUARDS
----------------
The "帮我打开扩展管理页" button was a dead button, and the three failures that
made it dead compounded into a single silent 404 (observed live: three
``POST /api/v1/browser/open-extensions → 404`` in ``logs/app.log``, each
logging "no Chrome-family browser found on this machine"):

  1. **Wrong machine.** ``subprocess.Popen`` opens a window on the SERVER.
     That only helps a user who is physically at the server. The gate for
     "is the user at the server" was ``_remote_is_loopback()`` — a pure IP
     test that, under the standard same-host reverse-proxy / tunnel
     deployment, reports ``127.0.0.1`` for *every public request* (the
     ProxyFix seam is not wired; see docs/modules/integrations_api.md
     §3.2b, which calls this out as a defect written up as a feature). So
     the gate said "you are local" to a user who was three network hops
     away, and the button rendered for them.
  2. **No browser to open.** A headless server has no Chrome, so even a
     genuinely-local user got a 404 from a button that had already promised
     to act.
  3. **Chrome-only probe.** Edge is Chromium-family and runs this very
     extension unchanged, but the probe never looked for it.

THE INVARIANT PINNED HERE
-------------------------
**A control that cannot achieve what it claims must not invite the click.**
``static/js/local-control.js`` states this rule in its own header and
enforces it for the two capability switches; this route's button was the one
place that broke it. So the fix is not a nicer failure message — the button
must NOT EXIST when the probe cannot find a browser to drive.

And the probe is the RIGHT criterion, strictly stronger than the IP test: if
this machine has no browser at all, then nobody is looking at this UI *from*
this machine, which also makes the server-side ``extensionPath`` useless.
Both therefore hang off the probe.

DISCIPLINE
----------
These are BEHAVIOUR guards. They assert what a caller observes — the JSON a
real request returns, the DOM a real render produces — never that some
private helper exists or that a line of source looks a certain way. The
frontend halves splice the renderer out of the SHIPPED file and run it under
jsdom, so the guard cannot drift into asserting a world production left
behind.
"""
import json
import re
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
from tests._runtime_sections import runtime_section_path

LC_JS = Path(runtime_section_path('local-control.js'))
STYLES = ROOT / "static" / "styles.css"

# ══════════════════════════════════════════════════════════
#  Backend: the probe is the single source of truth
# ══════════════════════════════════════════════════════════

def _get_status(flask_client, monkeypatch, client_addr, *, probe):
    """Drive one real GET /api/v1/browser/status with a chosen peer + probe.

    ``scope_base={'client': ...}`` sets the ASGI socket peer the way
    Hypercorn does, so the REAL loopback predicate runs against it — the
    mechanism tests/test_proxy_trust.py established. Quart's test client
    otherwise reports ``'<local>'``, which ``auth.py`` treats as loopback and
    which would make every assertion below pass for the wrong reason.
    """
    from lib.api_keys import create_key
    from routes.api_v1 import browser as browser_routes

    monkeypatch.setattr(browser_routes, '_detect_local_browser',
                        lambda: probe)
    _row, token = create_key(owner_user_id=1, 
        name='browser-probe', scopes=['agents:browser'])
    resp = flask_client.get(
        '/api/v1/browser/status',
        headers={'Authorization': f'Bearer {token}'},
        scope_base={'client': client_addr})
    return resp.status_code, (resp.get_json(silent=True) or {})


_CHROME = {'binary': '/fake/chrome', 'family': 'chrome',
           'name': 'Chrome', 'extensionsUrl': 'chrome://extensions'}
_EDGE = {'binary': '/fake/msedge', 'family': 'edge',
         'name': 'Edge', 'extensionsUrl': 'edge://extensions'}


def test_status_reports_no_local_browser_when_probe_finds_none(flask_client,
                                                              monkeypatch):
    """Headless server + loopback peer ⇒ the payload must say "no browser".

    This is the live-observed configuration. The frontend needs a POSITIVE
    signal to decide whether to render the button at all; without this field
    it can only find out by making the user click and collect a 404.
    """
    status, body = _get_status(flask_client, monkeypatch,
                               ('127.0.0.1', 5555), probe=None)
    assert status == 200, f"status endpoint broke: {status} {body}"
    assert 'localBrowser' in body, (
        "the payload carries no probe result, so the frontend cannot know "
        "whether opening a browser here is even possible — that is exactly "
        "how the dead button shipped")
    assert not body['localBrowser'], (
        f"probe found nothing, yet the payload advertises a browser: "
        f"{body['localBrowser']!r}")


def test_status_names_the_detected_browser(flask_client, monkeypatch):
    """A detected browser is reported BY NAME, so the UI can stop saying
    "Chrome" when it is really driving Edge."""
    status, body = _get_status(flask_client, monkeypatch,
                               ('127.0.0.1', 5555), probe=_EDGE)
    assert status == 200
    lb = body.get('localBrowser') or {}
    assert lb.get('name') == 'Edge', (
        f"expected the probe's browser name to reach the client, got {lb!r}")
    assert 'chrome' not in json.dumps(lb).lower() or lb.get('family') == 'edge', (
        f"Edge must not be reported as Chrome: {lb!r}")


def test_status_never_leaks_the_binary_path(flask_client, monkeypatch):
    """The absolute path of a local executable is server filesystem detail
    the browser has no use for. Name and family are what the UI renders."""
    status, body = _get_status(flask_client, monkeypatch,
                               ('127.0.0.1', 5555), probe=_CHROME)
    assert status == 200
    assert '/fake/chrome' not in json.dumps(body), (
        "the probe's binary path reached the client; expose only what the "
        "UI renders (name/family/extensionsUrl)")


def test_extension_path_is_withheld_when_no_browser_exists(flask_client,
                                                           monkeypatch):
    """No browser on this machine ⇒ nobody is viewing this UI from it ⇒ the
    server-side unpacked-extension path is useless and must be withheld.

    This is the ROOT fix for "wrong machine". The old gate was the IP test
    alone, which a same-host reverse proxy makes vacuously true for public
    traffic; the probe is a fact about the machine that a proxy cannot forge.
    Withholding the path is what makes the UI fall through to the
    download-the-ZIP instruction, which is the only actionable path for a
    user who is genuinely elsewhere.
    """
    status, body = _get_status(flask_client, monkeypatch,
                               ('127.0.0.1', 5555), probe=None)
    assert status == 200
    assert not body.get('extensionPath'), (
        f"a headless server still advertised its own on-disk extension "
        f"folder ({body.get('extensionPath')!r}) as if the user's browser "
        f"could load it")


def test_extension_path_still_served_to_a_real_local_user(flask_client,
                                                          monkeypatch):
    """The complement: loopback peer AND a real browser ⇒ the path IS served.

    Without this, "withhold when no browser" could be satisfied by
    withholding always, killing the same-machine install path entirely.
    """
    status, body = _get_status(flask_client, monkeypatch,
                               ('127.0.0.1', 5555), probe=_CHROME)
    assert status == 200
    assert body.get('extensionPath'), (
        "a genuinely local user with a real browser lost the on-disk "
        "install path")


def test_remote_peer_gets_no_extension_path_even_with_a_browser(flask_client,
                                                                monkeypatch):
    """Both conditions are required. A remote peer must not be handed a
    server-side path even when the server does happen to have a browser."""
    status, body = _get_status(flask_client, monkeypatch,
                               ('203.0.113.7', 5555), probe=_CHROME)
    assert status == 200
    assert not body.get('extensionPath'), (
        "a remote peer was handed the server's own folder path")


# ══════════════════════════════════════════════════════════
#  Backend: Edge is a first-class target, with ITS url
# ══════════════════════════════════════════════════════════

def test_probe_finds_edge_when_only_edge_is_installed(monkeypatch):
    """Edge is Chromium-family and runs this extension unchanged, so a
    machine with only Edge must not report "no browser".

    Driven through the real probe with a fake PATH resolver, so the platform
    branch actually executes rather than being asserted about.
    """
    import sys as _sys

    from routes.api_v1 import browser as browser_routes

    if _sys.platform not in ('linux', 'linux2'):
        pytest.skip('POSIX name-resolution branch')

    monkeypatch.setattr(browser_routes.shutil, 'which',
                        lambda n: '/usr/bin/' + n if 'edge' in n else None)
    got = browser_routes._probe_local_browser()
    assert got, ("only Edge installed and the probe reported nothing — Edge "
                 "runs this extension unchanged")
    assert got['family'] == 'edge', f"misidentified Edge: {got!r}"


def test_edge_gets_its_own_extensions_url(monkeypatch):
    """``chrome://extensions`` is not Edge's extensions page. Handing a
    browser another vendor's internal URL is the same class of bug as the
    dead button: an action that looks taken and lands nowhere useful."""
    import sys as _sys

    from routes.api_v1 import browser as browser_routes

    if _sys.platform not in ('linux', 'linux2'):
        pytest.skip('POSIX name-resolution branch')

    monkeypatch.setattr(browser_routes.shutil, 'which',
                        lambda n: '/usr/bin/' + n if 'edge' in n else None)
    edge = browser_routes._probe_local_browser()
    assert edge['extensionsUrl'].startswith('edge://'), (
        f"Edge was given {edge['extensionsUrl']!r}")

    monkeypatch.setattr(browser_routes.shutil, 'which',
                        lambda n: '/usr/bin/' + n if 'chrom' in n else None)
    chrome = browser_routes._probe_local_browser()
    assert chrome['extensionsUrl'].startswith('chrome://'), (
        f"Chrome was given {chrome['extensionsUrl']!r}")


def test_open_extensions_launches_the_probes_own_url(flask_client,
                                                     monkeypatch):
    """The route must spawn the URL the PROBE chose — not a hardcoded one.

    Two hardcoded copies of "which page is the extensions page" is how the
    Edge case silently regresses to chrome://.
    """
    import subprocess as _sp

    from routes.api_v1 import browser as browser_routes

    calls = []
    monkeypatch.setattr(_sp, 'Popen',
                        lambda argv, **kw: calls.append(argv) or object())
    monkeypatch.setattr(browser_routes, '_detect_local_browser',
                        lambda: _EDGE)
    from lib.api_keys import create_key
    _row, token = create_key(owner_user_id=1, 
        name='browser-open-extensions', scopes=['agents:browser'])
    resp = flask_client.post(
        '/api/v1/browser/open-extensions',
        headers={'Authorization': f'Bearer {token}'},
        scope_base={'client': ('127.0.0.1', 5555)})
    assert resp.status_code == 200, (
        f"Edge launch failed: {resp.status_code} {resp.get_json(silent=True)}")
    assert len(calls) == 1, f"expected one spawn, got {calls}"
    assert calls[0][0] == '/fake/msedge', f"wrong binary: {calls[0]!r}"
    assert 'edge://extensions' in calls[0], (
        f"the route ignored the probe's URL and launched {calls[0]!r}")


# ══════════════════════════════════════════════════════════
#  Frontend: no probe ⇒ no button (jsdom, shipped source)
# ══════════════════════════════════════════════════════════

def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not available")
    return exe


def _has_jsdom() -> bool:
    try:
        subprocess.run([_node(), "-e", "require('jsdom')"], cwd=ROOT,
                       capture_output=True, timeout=30, check=True)
        return True
    except Exception:
        return False


_SETUP_HTML = (
    '<div id="lcBrowserStatus"><span class="browser-status-dot"></span>'
    '<span class="lc-status-text"></span></div>'
    '<div id="lcBrowserAbout"></div>'
    '<div id="lcBrowserSetup"></div>'
    '<button id="lcBrowserSwitch"><span class="lc-switch-knob"></span></button>'
    '<div id="localControlBadge"></div><div id="localControlToggle"></div>'
)

_HARNESS = textwrap.dedent(r"""
(async () => {
  const { JSDOM } = require('jsdom');
  const fs = require('fs');
  const dom = new JSDOM('<!doctype html><body>' + %(html)s + '</body>');
  global.window = dom.window; global.document = dom.window.document;
  global.navigator = dom.window.navigator;
  global.browserEnabled = false; global.desktopEnabled = false;
  global.escapeHtml = (s) => String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;');
  global.t = (k) => k;                 // fall back to the inline defaults
  global._safeClipboardWrite = () => Promise.resolve();
  global.Api = { browser: { openExtensions: () => Promise.resolve(null) } };

  // Splice the SHIPPED renderer in, so the guard tracks production.
  const src = fs.readFileSync(%(lc)s, 'utf8');
  eval(src.replace(/\bif \(typeof window !== 'undefined'\) \{[\s\S]*$/, ''));

  const out = {};
  for (const [name, payload] of Object.entries(%(cases)s)) {
    document.getElementById('lcBrowserSetup').innerHTML = '';
    _lcBrowserUpgradeRequested = payload.forceBrowserUpgrade === true;
    _lcRenderBrowser(payload);
    const el = document.getElementById('lcBrowserSetup');
    out[name] = {
      hasOpenBtn: !!el.querySelector('#lcExtOpenBtn'),
      hasDownloadBtn: !!el.querySelector('#lcExtDownloadBtn'),
      hasPath: el.textContent.includes('/srv/tofu/browser_extension'),
      text: el.textContent.trim(),
      statusText: document.querySelector('#lcBrowserStatus .lc-status-text').textContent,
      state: _lcBrowserSetupState(payload),
    };
  }
  console.log(JSON.stringify(out));
})();
""")


def _render_cases(cases: dict) -> dict:
    if not _has_jsdom():
        pytest.skip("jsdom not installed")
    script = _HARNESS % {
        'html': json.dumps(_SETUP_HTML),
        'lc': json.dumps(str(LC_JS)),
        'cases': json.dumps(cases),
    }
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, (
        f"harness failed:\nSTDOUT {proc.stdout}\nSTDERR {proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


_LOCAL = {'connected': False,
          'extensionPath': '/srv/tofu/browser_extension',
          'localBrowser': {'family': 'edge', 'name': 'Edge',
                           'extensionsUrl': 'edge://extensions'}}
_NO_BROWSER = {'connected': False,
               'extensionPath': '/srv/tofu/browser_extension',
               'localBrowser': None}
_REMOTE = {'connected': False, 'localBrowser': None}


def test_button_absent_when_no_browser_can_be_driven():
    """THE headline invariant: probe found nothing ⇒ the button does not exist.

    A rendered-but-doomed button is what produced the live 404s. Note the
    payload here deliberately still carries ``extensionPath`` — the guard
    must fail if the frontend keys the button off the path alone, which is
    precisely what it used to do.
    """
    out = _render_cases({'no_browser': _NO_BROWSER})['no_browser']
    assert not out['hasOpenBtn'], (
        "the open-extensions button rendered on a machine with no browser to "
        "open — clicking it can only ever produce a 404. A control that "
        "cannot achieve what it claims must not invite the click.")


def test_a_real_local_user_still_gets_the_button():
    """The complement — otherwise "never render it" would pass test 1 while
    deleting the feature."""
    out = _render_cases({'local': _LOCAL})['local']
    assert out['hasOpenBtn'], (
        "a local user with a real browser lost the one-click path")
    assert out['hasPath'], "the folder path to paste went missing"


def test_no_browser_falls_through_to_an_actionable_instruction():
    """Removing the button must leave something the user CAN do, not a void.

    Whatever the surface offers here, it has to be actionable from where the
    user actually is: the ZIP download.
    """
    out = _render_cases({'no_browser': _NO_BROWSER})['no_browser']
    assert out['text'], (
        "removing the dead button left an EMPTY panel — the user is now told "
        "nothing at all, which is worse than a wrong instruction")
    assert out['hasDownloadBtn'], (
        f"expected the download-the-ZIP path to take over, got: "
        f"{out['text'][:200]!r}")


def test_the_instruction_names_the_detected_browser():
    """With Edge detected, the guidance must not order the user into Chrome."""
    out = _render_cases({'local': _LOCAL})['local']
    assert 'Edge' in out['text'], (
        f"Edge was detected but the instruction never names it: "
        f"{out['text'][:300]!r}")


def test_capability_upgrade_request_keeps_a_download_action_without_version_data():
    """Legacy protocol clients often report no manifest version.

    Settings already has authoritative evidence that such a client lacks a
    required adapter capability.  Opening Local Control from that warning must
    not let the ordinary "connected" branch erase every setup action merely
    because version comparison has no left-hand value.
    """
    out = _render_cases({
        'capability_upgrade': {
            'connected': True,
            'clients': [{
                'client_id': 'legacy', 'ext_version': '',
                'protocol_version': 1,
            }],
            'servedExtVersion': '2.4.0',
            'forceBrowserUpgrade': True,
        },
    })['capability_upgrade']
    assert out['hasDownloadBtn'], (
        f"capability-triggered upgrade opened an actionless connected card: "
        f"{out['text']!r}")
    assert '站点适配器所需能力' in out['text']


def test_connected_status_line_names_the_extension_version():
    """The dot's label must say WHICH extension binary is connected.

    "已连接" alone hides the one fact that tells a stale side-load apart at a
    glance.  The suffix appears only on fleet-wide agreement: mixed versions
    stay unnamed (the upgrade nudge owns that story), and a legacy client
    reporting no manifest version adds nothing.
    """
    out = _render_cases({
        'single': {
            'connected': True, 'secondsAgo': 3.2,
            'clients': [{'client_id': 'a', 'ext_version': '5.4.1'}],
            'servedExtVersion': '5.4.1',
        },
        'fleet_agreeing': {
            'connected': True,
            'clients': [{'client_id': 'a', 'ext_version': '5.4.1'},
                        {'client_id': 'b', 'ext_version': '5.4.1'}],
            'servedExtVersion': '5.4.1',
        },
        'fleet_mixed': {
            'connected': True,
            'clients': [{'client_id': 'a', 'ext_version': '5.4.0'},
                        {'client_id': 'b', 'ext_version': '5.4.1'}],
            'servedExtVersion': '5.4.1',
        },
        'legacy_versionless': {
            'connected': True, 'secondsAgo': 2,
            'clients': [{'client_id': 'a', 'ext_version': ''}],
            'servedExtVersion': '5.4.1',
        },
    })
    single = out['single']['statusText']
    assert single == '已连接 · v5.4.1 · 3.2s', (
        f"connected card never named the extension version: {single!r}")
    fleet = out['fleet_agreeing']['statusText']
    assert fleet.endswith(' · v5.4.1'), (
        f"an agreeing fleet should still name its one version: {fleet!r}")
    mixed = out['fleet_mixed']['statusText']
    assert '· v' not in mixed, (
        f"mixed versions must stay unnamed in the status line: {mixed!r}")
    legacy = out['legacy_versionless']['statusText']
    assert legacy == '已连接 · 2s', (
        f"a versionless legacy client must not invent a suffix: {legacy!r}")


# ══════════════════════════════════════════════════════════
#  CSS rendering: .lc-substep must carry its own typography
# ══════════════════════════════════════════════════════════

def _decl(selector: str) -> dict:
    """Effective declarations for ``selector``, merged across ALL rules.

    A single-rule lookup would be asserting the SHAPE of the stylesheet
    ("this property must appear in this literal rule"), which is exactly the
    kind of implementation-coupled guard that goes red when someone factors
    two rules into a grouped selector without changing a single rendered
    pixel. What matters is the value that actually lands, so this merges every
    rule whose selector list contains ``selector`` in document order.

    All the selectors involved here are single classes (specificity 0,1,0), so
    document order alone decides the winner — no specificity model needed.
    """
    css = STYLES.read_text(encoding='utf-8')
    # Strip comments FIRST. Without this, the selector capture below swallows
    # any preceding /* ... */ block, so ".lc-step" ends up glued to the comment
    # text and no longer compares equal to a bare selector part.
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    out: dict[str, str] = {}
    hits = 0
    for sel_list, body in re.findall(r'([^{}]+)\{([^}]*)\}', css):
        parts = [s.strip() for s in sel_list.split(',')]
        if selector not in parts:
            continue
        hits += 1
        for decl in body.split(';'):
            if ':' in decl:
                k, v = decl.split(':', 1)
                out[k.strip()] = v.strip()
    assert hits, f"selector {selector} not found in styles.css"
    return out


def test_lc_substep_carries_its_own_typography():
    """``.lc-substep`` declared ONLY ``margin``, so a bare-text node inside it
    inherited the modal's default size and rendered visibly larger than the
    ``.lc-step`` lines beside it.

    It looked fine everywhere else purely by accident: the other two
    ``.lc-substep`` uses wrap an ``<a class="lc-dl-link">``, and *that* class
    sets its own font-size — a child was silently covering for the parent.
    """
    sub = _decl('.lc-substep')
    missing = [p for p in ('font-size', 'color', 'line-height')
               if p not in sub]
    assert not missing, (
        f".lc-substep is missing {missing} — a bare text node inside it "
        f"renders at the modal's default size instead of matching the "
        f".lc-step lines beside it. Declared: {sub}")


def test_substep_and_step_share_one_font_size():
    """Both are secondary instruction text in the same panel; two different
    literals here is the drift that let this diverge unnoticed."""
    assert _decl('.lc-substep')['font-size'] == _decl('.lc-step')['font-size'], (
        "sibling instruction lines disagree on font-size")


def test_the_dl_link_form_is_not_regressed():
    """The two ``.lc-dl-link`` uses must still look right after the parent
    gains a size — this is the form that was accidentally already correct,
    so it is the one a fix could quietly break."""
    assert 'font-size' in _decl('.lc-dl-link'), (
        ".lc-dl-link lost its own font-size; it is nested in .lc-substep and "
        "both forms must render correctly")


# ══════════════════════════════════════════════════════════
#  The probe is CACHED — and the cache must not blind it
# ══════════════════════════════════════════════════════════
#
# `_detect_local_browser` hangs off GET /status, which the Local Control modal
# polls every 3s (`_LC_POLL_MS`). Uncached, the miss path is the expensive one:
# with no browser installed, every candidate name misses and each miss walks
# the WHOLE PATH. Measured on this host — 51 PATH entries, ~408 stat() calls
# and ~6ms per probe, and that is a local disk; on the FUSE/dolphinfs mounts
# this project actually deploys to, stat is markedly dearer.
#
# But a cache is itself a way to reinvent the bug this module was written to
# kill. "Tofu can't find my browser" is the ORIGINAL complaint; a cache with
# no expiry turns a user who installs Chrome mid-session into exactly that
# report, and this time the probe would be right and the cache lying. So the
# TTL is not a tuning knob — it is the guarantee that a newly-installed
# browser becomes visible on its own, and it is pinned as a behaviour.

def _cache():
    from routes.api_v1 import browser as browser_routes
    return browser_routes._BROWSER_PROBE_CACHE


def test_repeated_status_calls_do_not_re_probe_the_filesystem(monkeypatch):
    """The 3s poll must not pay for a full PATH walk every time.

    Asserts the OBSERVABLE consequence — how many times the underlying probe
    actually ran — rather than that some cache object exists.
    """
    from routes.api_v1 import browser as browser_routes

    _cache().clear()
    calls = []
    monkeypatch.setattr(browser_routes, '_probe_local_browser',
                        lambda: calls.append(1) or None)
    for _ in range(20):
        browser_routes._detect_local_browser()
    assert len(calls) == 1, (
        f"20 status polls triggered {len(calls)} filesystem probes — the "
        f"modal polls every 3s and each miss walks the entire PATH, so this "
        f"is a hot loop paid on every open modal")


def test_a_newly_installed_browser_is_found_once_the_ttl_lapses(monkeypatch):
    """THE anti-regression for the cache itself.

    A user who installs Chrome while Tofu is running must NOT be stuck with
    "no browser" forever — that is the original complaint wearing a cache as
    a disguise. Drives real wall-clock expiry through the cache's own TTL
    rather than reaching into its internals.
    """
    from routes.api_v1 import browser as browser_routes

    _cache().clear()
    monkeypatch.setattr(_cache(), 'ttl', 0.05)   # keep the test fast

    state = {'installed': False}
    monkeypatch.setattr(
        browser_routes, '_probe_local_browser',
        lambda: (_CHROME if state['installed'] else None))

    assert browser_routes._detect_local_browser() is None
    state['installed'] = True                     # user installs Chrome now
    time.sleep(0.08)                              # TTL lapses
    got = browser_routes._detect_local_browser()
    assert got is not None, (
        "a browser installed while Tofu was running stayed invisible after "
        "the TTL lapsed — the cache has recreated the very 'Tofu can't find "
        "my browser' bug this module exists to prevent")
    assert got['family'] == 'chrome'


def test_the_ttl_is_short_enough_to_self_heal():
    """A 'cache' whose TTL outlives the session is a permanent wrong answer.

    Bounded rather than pinned to one literal: the point is self-healing
    within a human's patience, not a specific number.
    """
    ttl = _cache().ttl
    assert 0 < ttl <= 120, (
        f"probe cache TTL is {ttl}s — outside the self-healing window. Too "
        f"long (or unbounded) means a browser installed mid-session may never "
        f"be noticed; <=0 disables expiry entirely in TTLCache.")


def test_the_cache_is_the_shared_one_not_a_hand_rolled_dict():
    """Reuse `lib/ttl_cache.TTLCache` — it already solves per-key compute
    serialisation, LRU bounds, and registration with the cgroup pressure
    relief pass (`clear_all_caches`). A bespoke dict here would silently opt
    out of memory-pressure reclaim."""
    from lib.ttl_cache import TTLCache
    assert isinstance(_cache(), TTLCache), (
        f"probe cache is {type(_cache()).__name__}, not the shared TTLCache — "
        f"a hand-rolled cache misses cgroup memory-pressure reclaim")


def test_concurrent_missers_probe_the_filesystem_once(monkeypatch):
    """Opening several tabs at once must not fan out into N PATH walks.

    TTLCache.get_or_compute serialises per key; this pins that we actually
    use that entry point instead of a get/set pair with a race between them.
    """
    import threading

    from routes.api_v1 import browser as browser_routes

    _cache().clear()
    calls = []

    def _slow_probe():
        calls.append(1)
        time.sleep(0.05)
        return _CHROME

    monkeypatch.setattr(browser_routes, '_probe_local_browser', _slow_probe)
    threads = [threading.Thread(target=browser_routes._detect_local_browser)
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1, (
        f"8 concurrent pollers ran {len(calls)} probes — get/set with a gap "
        f"between them lets every misser stampede the filesystem")


# ══════════════════════════════════════════════════════════
#  Docs: the user-facing install path must not be Chrome-only
# ══════════════════════════════════════════════════════════
#
# The backend can drive Edge now, but that only reaches a user through the
# docs they actually read. A README that says "go to chrome://extensions" is
# not merely stale for an Edge user — typing chrome:// into Edge does not
# open anything, so the documented path is a dead end in exactly the way the
# dead button was. CLAUDE.md makes README.md/README_CN.md the user-facing
# product docs and requires the pair stay in sync.

_READMES = ('README.md', 'README_CN.md')


def _readme_extension_section(name: str) -> str:
    """The browser-extension section of a README, located by heading."""
    txt = (ROOT / name).read_text(encoding='utf-8')
    m = re.search(r'\n#{2,4} [^\n]*(?:Browser Extension|浏览器插件)[^\n]*\n',
                  txt)
    assert m, f"{name}: browser-extension section heading not found"
    rest = txt[m.end():]
    nxt = re.search(r'\n#{2,4} ', rest)
    return rest[:nxt.start()] if nxt else rest


def test_readmes_do_not_hardcode_one_vendors_extensions_url():
    """`chrome://extensions` typed into Edge opens nothing, so it must never
    be the ONLY address the reader is given.

    Note what this does *not* forbid: naming both schemes side by side
    ("`chrome://extensions` in Chrome, `edge://extensions` in Edge") is the
    correct fix, and is strictly more useful than a vague "open the
    extensions page". A guard that banned the substring outright would push
    the docs toward that vaguer wording — so the defect pinned here is
    'chrome:// with no edge:// anywhere near it', which is what actually
    dead-ends an Edge user.
    """
    bad = {}
    for n in _READMES:
        sec = _readme_extension_section(n)
        if 'chrome://extensions' in sec and 'edge://extensions' not in sec:
            bad[n] = sec
    assert not bad, (
        f"{sorted(bad)} instruct users to open `chrome://extensions` without "
        f"ever naming Edge's own `edge://extensions`. An Edge user cannot "
        f"follow that — edge:// is its own scheme — so the documented install "
        f"path dead-ends for every non-Chrome user the backend now supports.")


def test_readmes_tell_the_user_edge_works():
    """Edge support that no user is told about has not shipped."""
    for n in _READMES:
        sec = _readme_extension_section(n)
        assert 'Edge' in sec, (
            f"{n}: the browser-extension section never mentions Edge, so a "
            f"user has no way to learn it is supported")


def test_the_two_readmes_stay_in_sync_on_browser_support():
    """CLAUDE.md requires the Chinese README track the English one. Drift here
    means one language's users get worse instructions than the other's."""
    en, zh = (_readme_extension_section(n) for n in _READMES)
    for browser in ('Chrome', 'Edge'):
        assert (browser in en) == (browser in zh), (
            f"{browser} is documented in only one README — the pair must "
            f"stay in sync")


def test_no_tracked_doc_teaches_a_script_that_does_not_exist():
    """No doc that actually SHIPS may hand the reader a command that fails.

    ``docs/README_EXTENSION.md`` walked developers through three helper
    scripts (``dev_extension.sh`` / ``install_extension.sh`` /
    ``install_extension.bat``) that do not exist in the repo. It is now
    gitignored and untracked (commit 160b6796 deliberately excluded the
    internal dev summaries from both git and the opensource export), so it
    reaches nobody and a guard aimed at that one path would pass VACUOUSLY on
    a clean checkout — the exact shape of guard rot this suite exists to
    avoid.

    So the invariant is generalised to the population that can actually reach
    a reader: every TRACKED markdown doc. A doc telling you to run a script
    that was deleted costs you the time to discover it is gone, and that is
    the same failure mode as the dead button — an instruction that looks
    actionable and lands nowhere.
    """
    tracked = subprocess.run(
        ['git', 'ls-files', '*.md'], cwd=ROOT,
        capture_output=True, text=True, timeout=60).stdout.split()
    assert tracked, "git ls-files returned nothing — scan surface is empty"

    ghost_scripts = ('dev_extension.sh', 'install_extension.sh',
                     'install_extension.bat')
    # Only an IMPERATIVE mention counts — "run `./dev_extension.sh`". A doc
    # that NAMES a script in order to report it is missing (as
    # docs/modules/integrations_api.md §2.4 does, auditing exactly this
    # rot) is doing the right thing, and a guard that punished it would push
    # the repo to delete its own findings. The distinguishing mark is the
    # invocation form: a leading `./` or a shell-prompt `$`.
    invocation = re.compile(
        r'(?:\./|\$\s+)(' + '|'.join(re.escape(s) for s in ghost_scripts) + r')\b')
    offenders = {}
    for rel in tracked:
        p = ROOT / rel
        if not p.exists():
            continue
        try:
            txt = p.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        missing = sorted({m for m in invocation.findall(txt)
                          if not (ROOT / m).exists()})
        if missing:
            offenders[rel] = missing
    assert not offenders, (
        f"tracked docs instruct the reader to run scripts that do not exist: "
        f"{offenders}. Either restore the scripts or fix the docs — a command "
        f"that cannot run is the documentation form of a dead button.")
