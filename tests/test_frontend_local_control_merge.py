"""tests/test_frontend_local_control_merge.py — the merged "Local Control" surface.

WHAT THIS GUARDS
----------------
Desktop control and the browser bridge used to be two toolbar rows, two
mental models, and two very different levels of care: the browser row opened
a setup modal with a live status dot, while ``toggleDesktop`` was a blind
three-line flag flip with NO status check at all. Since
``lib/tools/registry/_build.py::_build_desktop`` ships ZERO desktop tools when
no agent is connected, a user could switch desktop control "on" and get an
enabled-looking toggle whose tools never existed.

They are now ONE entry (``#localControlToggle``) opening ONE modal
(``#localControlModal``) with two capability rows. The two backing wire flags
stay separate — only the surface merged.

The load-bearing behaviour, and what each test below pins:

  1. Exactly ONE install instruction is rendered per capability, chosen by
     DETECTED state — never a menu of every possible path. This is the whole
     point of the merge: minimum cognitive load.
  2. The desktop instruction comes from the BACKEND's ``setup_state``, because
     only the server process can see ``sys.frozen``. The three states render
     three DIFFERENT instructions, and only the remote one offers a token.
  3. A connected capability renders NO instruction at all.

DISCIPLINE (charter: "禁止在测试 harness 里手抄生产判据")
--------------------------------------------------------
The renderers are spliced OUT OF THE SHIPPED SOURCE at run time and evaluated
under jsdom — no predicate is hand-copied into this file, so the guard cannot
drift into asserting a world that production left behind. Symbols are located
by SEARCHING for their definition, not by hardcoding a line range, and the
three failure modes are separately diagnosable (deleted / duplicated / found).
"""
import functools
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "static" / "js"
INDEX_HTML = ROOT / "index.html"


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not available")
    return exe


@functools.lru_cache(maxsize=1)
def _has_jsdom() -> bool:
    """Probe for jsdom ONCE per session.

    Cached because every ``@pytest.mark.skipif`` argument is evaluated at
    IMPORT time, and `require('jsdom')` costs ~5s on this FUSE mount (measured:
    0.12s for bare node, 5.2s once jsdom's module tree is pulled in). With one
    probe per decorator that was ~145s of collection before a single test body
    ran — enough to look like a hang and to blow a suite timeout.
    """
    try:
        subprocess.run([_node(), "-e", "require('jsdom')"], cwd=ROOT,
                       capture_output=True, check=True)
        return True
    except Exception:
        return False


def _find_defining_file(symbol: str) -> Path:
    """Locate the ONE file defining ``function <symbol>(``.

    Anchored on the SYMBOL, not a hardcoded path, so a legitimate module split
    re-points the guard instead of producing an unreadable false red. All three
    outcomes are separately diagnosable.
    """
    needle = f"function {symbol}("
    hits = [p for p in sorted(JS_DIR.rglob("*.js"))
            # Dotfiles too: the bundler's atomic-rename temp files are
            # `.bundle-<hash>.<rand>.js` — visible mid-build, and scanning
            # one reads a HALF-WRITTEN copy of every symbol (false red,
            # measured 2026-08-04 on a live bundle rebuild).
            if not p.name.startswith((".", "bundle-", "feature-"))
            and needle in p.read_text(encoding="utf-8")]
    if not hits:
        raise AssertionError(
            f"IMPLEMENTATION GONE: no file defines `{needle}`. If the merged "
            f"Local Control surface was removed, this guard is protecting "
            f"nothing — decide whether the protection should still exist "
            f"before repointing it.")
    if len(hits) > 1:
        raise AssertionError(
            f"SINGLE SOURCE COPIED: `{needle}` is defined in "
            f"{[str(p.relative_to(ROOT)) for p in hits]} — the merged surface "
            f"must have exactly one renderer per capability.")
    return hits[0]


def _slice_fn(symbol: str) -> str:
    """Brace-match the named function out of whichever file defines it."""
    src = _find_defining_file(symbol).read_text(encoding="utf-8")
    start = src.index(f"function {symbol}(")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"could not brace-match {symbol}")


# Every helper the two renderers call, spliced from the same shipped source.
_SHIPPED_SYMBOLS = (
    "_lcT", "_lcEsc", "_lcSetStatus", "_lcSetSwitch", "_lcSetAbout",
    "_lcBrowserSetupState", "_lcRenderBrowser", "_lcRenderDesktop",
    "_lcMintToken", "_lcConnectLine", "_lcUpdateBadge",
    "_lcOpenExtensionsPage",
    # The download instruction is authored ONCE and reached from three places
    # (the pre-detection floor, a failed status call, the detected `download`
    # state), so the renderers cannot be evaluated without it.
    "_lcBrowserDownload", "_lcPaintFloor",
    # The desktop download LINKS are likewise authored once and reached from
    # both install branches (local_source / remote) via the renderers — this
    # splice drifted when the helper was extracted and the whole suite went
    # red on ReferenceError, which is exactly the drift it exists to catch.
    "_lcDownloadLinks", "_lcFmtSize",
    # Re-bases the backend's absolute download URL onto the live BASE_PATH
    # (cloud-IDE proxy prefix) — _lcDownloadLinks calls it, so the splice
    # needs it or every test here goes red on ReferenceError.
    "_lcResolveDlUrl",
    # The poll-signature gate (2026-08-03): _lcRenderDesktop calls it to
    # decide whether a repaint is warranted at all.
    "_lcDesktopSignature",
    # The awaiting-agent hint is prepended in both attach branches, and
    # the pair block / connect-line details / wiring are authored once and
    # reached from both — the splice needs all of them or every test goes
    # ReferenceError. (2026-08-04 owner decree: the proxy-SSH warning was
    # DELETED — pairing codes are address-free, so no branch may send the
    # user to open a tunnel by hand.)
    "_lcAwaitingAgentHtml", "_lcPairBlockHtml", "_lcConnectDetailsHtml",
    "_lcWireAttach", "_lcPairCode",
    # The download button is authored once and shared by the install /
    # upgrade-nudge / stranded-rescue branches (2026-08-04 fleet tri-state);
    # the browser renderer calls both, so the splice needs them.
    "_lcExtDownloadAction", "_lcWireExtDownload",
)


def _module_state() -> str:
    """Splice the module-level `var` declarations the renderers depend on.

    Taken from the shipped source rather than re-declared here: the renderers
    write `_lcReach` (last CONFIRMED reachability per capability), so a
    hand-written stub in this file would be exactly the copied-predicate
    pattern the charter forbids — and a rename in production would leave the
    stub silently satisfying the reference.
    """
    src = _find_defining_file("_lcRenderDesktop").read_text(encoding="utf-8")
    decls = re.findall(r"^var _lc[A-Za-z]+ = .*?;$", src, re.M)
    assert decls, (
        "no module-level `var _lc… =` declarations found — the renderers' "
        "shared state moved or was renamed; re-point this splice before "
        "trusting any test below")
    return "\n".join(decls)


def _shipped(extra_neuter=None) -> str:
    """Concatenate the shipped renderers; optionally apply a NEUTER rewrite."""
    body = _module_state() + "\n" + "\n".join(
        _slice_fn(s) for s in _SHIPPED_SYMBOLS)
    if extra_neuter:
        neutered = extra_neuter(body)
        assert neutered != body, "NEUTER substitution did not apply"
        body = neutered
    return body


# Markup mirroring the shipped modal's two capability rows.
MODAL_HTML = """
<div id="localControlModal">
  <div class="lc-cap">
    <span class="lc-cap-status" id="lcBrowserStatus">
      <span class="browser-status-dot disconnected"></span>
      <span class="lc-status-text">checking</span>
    </span>
    <button class="lc-switch" id="lcBrowserSwitch" aria-checked="false"></button>
    <p class="lc-cap-about" id="lcBrowserAbout"></p>
    <div class="lc-cap-setup" id="lcBrowserSetup"></div>
  </div>
  <div class="lc-cap">
    <span class="lc-cap-status" id="lcDesktopStatus">
      <span class="browser-status-dot disconnected"></span>
      <span class="lc-status-text">checking</span>
    </span>
    <button class="lc-switch" id="lcDesktopSwitch" aria-checked="false"></button>
    <p class="lc-cap-about" id="lcDesktopAbout"></p>
    <div class="lc-cap-setup" id="lcDesktopSetup"></div>
  </div>
  <p class="lc-perm-note" id="lcPermNote" style="display:none"></p>
</div>
"""

HARNESS = textwrap.dedent("""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM(`<!DOCTYPE html><body>{html}</body>`);
    global.document = dom.window.document;
    global.window = dom.window;
    global.browserEnabled = false;
    global.desktopEnabled = false;
    global.t = (k) => k;                    // i18n absent -> fallback strings
    global.escapeHtml = (s) => String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    global._rec = {{ openExt: 0, clip: null }};
    global.Api = {{
      desktop: {{ mintToken: () => Promise.resolve({{token:'T'}}) }},
      browser: {{ openExtensions: () => {{
        global._rec.openExt++;
        return Promise.resolve({{ok: true}});
      }} }},
    }};
    global.showToast = () => {{}};
    global._safeClipboardWrite = (s) => {{
      global._rec.clip = s;
      return Promise.resolve();
    }};
    global.downloadBrowserExtension = () => {{}};
    global._applyBrowserLnaWarning = () => {{}};

    {shipped}

    (async () => {{

    // Render each DESKTOP setup_state into a fresh slot and capture what the
    // user would actually see. The two URL fields are what the backend sends
    // alongside setup_state (routes/api_v1/desktop.py).
    const DL = 'https://github.com/rangehow/ToFu/releases/latest';
    const SRV = 'https://tofu.example.com';
    // What the backend's `downloads` looks like once the server hosts the
    // artifact itself: an ABSOLUTE same-origin URL (built from the request's
    // own host), plus size + a hosted marker the UI turns into a badge.
    const DLS = [{{ os: 'windows', arch: 'x86_64', label: 'Windows installer',
                   filename: 'Tofu-Setup-0.15.2-win64.exe',
                   url: SRV + '/api/v1/desktop/download/Tofu-Setup-0.15.2-win64.exe',
                   hosted: 'server', size: 115822886 }}];
    const desktopStates = ['connected', 'tray', 'local_source', 'remote'];
    const desktop = {{}};
    for (const st of desktopStates) {{
      document.getElementById('lcDesktopSetup').innerHTML = '';
      _lcRenderDesktop({{ connected: st === 'connected', setup_state: st,
                         download_url: DL, server_url: SRV, downloads: DLS }});
      const el = document.getElementById('lcDesktopSetup');
      const dlA = el.querySelector('a[href]');
      const pageA = el.querySelector('a#lcDesktopDownload');
      const dsw = document.getElementById('lcDesktopSwitch');
      const hostedEl = el.querySelector('.lc-dl-hosted');
      desktop[st] = {{
        steps: el.querySelectorAll('.lc-step').length,
        text: el.textContent.trim(),
        hasMintButton: !!el.querySelector('#lcMintBtn'),
        // The connect line survives ONLY inside the collapsed advanced
        // details — never as a top-level action (2026-08-04 decree).
        mintInsideDetails: !!el.querySelector('.lc-details #lcMintBtn'),
        // Pairing is the ONE primary attach action in every branch that
        // attaches anything; the ids are branch-unique (lcPairBtn).
        hasPairButton: !!el.querySelector('#lcPairBtn'),
        pairInAgentRole: !!el.querySelector('.lc-role #lcPairBtn'),
        // Owner decree 2026-08-04: no surface may send the user to open an
        // ssh tunnel by hand.
        mentionsManualTunnel: /隧道地址|ssh 隧道|ssh-tunnel/i.test(el.textContent),
        // A real, clickable link the user can follow — not prose.
        downloadHref: dlA ? dlA.getAttribute('href') : '',
        // The releases-page escape hatch — external, must never be re-based.
        pageHref: pageA ? pageA.getAttribute('href') : '',
        // The per-platform direct links (vs the releases-page escape hatch).
        directCount: el.querySelectorAll('a.lc-dl-direct').length,
        hostedText: hostedEl ? hostedEl.textContent.trim() : '',
        dotConnected: !!document.querySelector(
          '#lcDesktopStatus .browser-status-dot.connected'),
        // Can the user flip this on right now, and does the row explain
        // what flipping it actually grants?
        switchUsable: !dsw.disabled,
        switchReason: dsw.getAttribute('title') || '',
        about: document.getElementById('lcDesktopAbout').textContent.trim(),
        permNoteShown:
          document.getElementById('lcPermNote').style.display !== 'none',
      }};
    }}

    // Drive the REAL mint handler and read what actually lands in the box.
    // A bare secret is unusable: the line must carry the server address too.
    document.getElementById('lcDesktopSetup').innerHTML = '';
    _lcRenderDesktop({{ connected: false, setup_state: 'remote',
                       download_url: DL, server_url: SRV }});
    document.getElementById('lcMintBtn').onclick();
    const mintedLine = await new Promise((res) => setTimeout(() => {{
      const b = document.getElementById('lcTokenBox');
      res(b ? b.textContent : '');
    }}, 0));

    // Same for the BROWSER row: connected / on-disk folder / remote download.
    const browser = {{}};
    const bcases = {{
      connected:     {{ connected: true, clients: [{{client_id:'abcdef123'}}], secondsAgo: 2 }},
      // 'load_unpacked' now needs BOTH the on-disk path AND the browser probe:
      // a machine with no drivable browser must not render the open button
      // (see tests/test_local_control_browser_probe.py).
      load_unpacked: {{ connected: false, extensionPath: '/srv/tofu/browser_extension',
                        localBrowser: {{ family:'chrome', name:'Chrome',
                                        extensionsUrl:'chrome://extensions' }} }},
      download:      {{ connected: false, localBrowser: null }},
      // Fleet tri-state (2026-08-04): a stale-but-working install gets the
      // upgrade nudge; a locked-out install (dead credential, cannot poll)
      // must never render as 'not installed'.
      outdated:      {{ connected: true, secondsAgo: 2,
                        clients: [{{client_id:'abcdef123', ext_version:'4.6.0'}}],
                        servedExtVersion: '4.7.0' }},
      stranded:      {{ connected: false, localBrowser: null,
                        lockedOutClients: [{{client_id:'dead1',
                          ext_version:'4.3.0', seconds_ago: 42}}] }},
    }};
    for (const k of Object.keys(bcases)) {{
      document.getElementById('lcBrowserSetup').innerHTML = '';
      _lcRenderBrowser(bcases[k]);
      const el = document.getElementById('lcBrowserSetup');
      const bsw = document.getElementById('lcBrowserSwitch');
      browser[k] = {{
        steps: el.querySelectorAll('.lc-step').length,
        text: el.textContent.trim(),
        hasPath: el.textContent.includes('/srv/tofu/browser_extension'),
        hasDownloadBtn: !!el.querySelector('#lcExtDownloadBtn'),
        hasOpenBtn: !!el.querySelector('#lcExtOpenBtn'),
        switchUsable: !bsw.disabled,
        about: document.getElementById('lcBrowserAbout').textContent.trim(),
        statusText: (document.querySelector(
          '#lcBrowserStatus .lc-status-text') || {{textContent: ''}})
          .textContent.trim(),
      }};
    }}

    // Drive the ONE primary action of the on-disk case: the button must BOTH
    // copy the folder path AND ask the server to open the extensions page —
    // a page with no path, or a path with no page, is half an action.
    document.getElementById('lcBrowserSetup').innerHTML = '';
    _lcRenderBrowser({{ connected: false, extensionPath: '/srv/tofu/browser_extension',
                       localBrowser: {{ family:'chrome', name:'Chrome',
                                       extensionsUrl:'chrome://extensions' }} }});
    const openBtn = document.getElementById('lcExtOpenBtn');
    if (openBtn) openBtn.onclick();
    const openClick = await new Promise((res) => setTimeout(() => res({{
      present: !!openBtn,
      routeCalls: global._rec.openExt,
      clip: global._rec.clip,
      note: (document.getElementById('lcExtOpenNote') || {{textContent: ''}}).textContent,
    }}), 0));

    console.log(JSON.stringify({{ desktop, browser, mintedLine, openClick }}));
    }})();
""")


def _run(shipped: str) -> dict:
    script = HARNESS.format(shipped=shipped,
                            html=MODAL_HTML.replace("`", "\\`"))
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_proxied(shipped: str) -> dict:
    """``_run`` with a live base path: ``apiUrl()`` rebases onto
    ``/proxy/15000``, simulating a path-prefixed cloud-IDE proxy deployment
    (``BASE_PATH`` = the page's own prefix, exactly what core.js computes)."""
    script = HARNESS.format(shipped=shipped,
                            html=MODAL_HTML.replace("`", "\\`"))
    anchor = "global.window = dom.window;"
    assert script.count(anchor) == 1, "harness window-anchor drifted"
    script = script.replace(
        anchor, anchor + "\nglobal.apiUrl = (p) => '/proxy/15000' + p;")
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── Scan-surface report ────────────────────────────────────────────────
# charter: "扫描类守卫必须先验证扫描面" — print what was actually spliced
# before asserting anything about it.

def test_scan_surface_report(capsys):
    """Print which symbols were located and where, before any assertion."""
    located = {s: str(_find_defining_file(s).relative_to(ROOT))
               for s in _SHIPPED_SYMBOLS}
    with capsys.disabled():
        print("\n[scan surface] shipped renderers spliced from:")
        for sym, path in located.items():
            print(f"  {sym:<24} <- {path}")
    assert len(located) == len(_SHIPPED_SYMBOLS)


# ── 1. The merge itself: ONE entry, not two ────────────────────────────

def test_the_two_toolbar_rows_became_one():
    """#browserToggle / #desktopToggle are gone; one merged row replaces them.

    Asserts the RESULT the user sees (a single entry), not any implementation
    detail of how it is wired.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="localControlToggle"' in html, "merged toolbar entry missing"
    assert 'id="browserToggle"' not in html, (
        "#browserToggle still present — the rows did not merge, they "
        "multiplied")
    assert 'id="desktopToggle"' not in html, (
        "#desktopToggle still present — the rows did not merge")
    # Mobile must not keep a second, unmerged pair.
    assert 'id="mobileLocalControl"' in html, "merged mobile row missing"
    assert 'id="mobileBrowser"' not in html and 'id="mobileDesktop"' not in html, (
        "mobile sheet still carries the two separate rows")


def test_there_is_exactly_one_setup_modal():
    """The old standalone #browserModal must not survive alongside the merged one."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="localControlModal"' in html
    assert 'id="browserModal"' not in html, (
        "#browserModal still exists — two setup modals is the duplication "
        "this merge exists to remove")


# ── 2. ONE instruction per state (the cognitive-load contract) ─────────

@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_each_desktop_state_renders_exactly_one_instruction():
    out = _run(_shipped())["desktop"]

    # Connected: nothing to install. An instruction here would be noise.
    assert out["connected"]["steps"] == 0, (
        "a connected agent must show NO install instruction")
    assert out["connected"]["text"] == ""

    # Each unconnected state: exactly one, never a menu.
    for st in ("tray", "local_source", "remote"):
        assert out[st]["steps"] == 1, (
            f"setup_state={st} rendered {out[st]['steps']} instructions — "
            f"must be exactly ONE next action")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_the_three_states_give_three_different_instructions():
    """If they all said the same thing, the detection would be decorative."""
    out = _run(_shipped())["desktop"]
    texts = {st: out[st]["text"] for st in ("tray", "local_source", "remote")}
    assert len(set(texts.values())) == 3, (
        f"states must differ, got {texts}")
    # The packaged-app case is the one-click path: tray, no token.
    assert "tray" in texts["tray"].lower() or "托盘" in texts["tray"]


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_pairing_is_the_primary_attach_in_every_branch():
    """2026-08-04 owner decree: the pairing code is the ONE primary attach
    action wherever a machine can be attached — it carries NO address, so
    it works from every reachability class (the agent discovers the route
    itself, building its own tunnel when needed). The minted connect line
    is demoted to the collapsed advanced details, and NO branch may send
    the user to open an ssh tunnel by hand."""
    out = _run(_shipped())["desktop"]
    for st in ("local_source", "remote"):
        assert out[st]["hasPairButton"] is True, (
            f"setup_state={st} lost its pairing button — the only "
            f"address-free attach path")
        assert out[st]["mintInsideDetails"] is True, (
            f"setup_state={st}: the connect line must live inside the "
            f"collapsed advanced details, never as a top-level action")
    for st in ("connected", "tray"):
        assert out[st]["hasPairButton"] is False, (
            f"setup_state={st} attaches nothing — no pair button allowed")
    # The documented tunnel blind spot (ssh -L presents as loopback):
    # local_source's AGENT role block carries the pair action visibly.
    assert out["local_source"]["pairInAgentRole"] is True, (
        "the agent role block lost its pairing button — the office-machine "
        "case has no connect flow again")
    for st in ("tray", "local_source", "remote"):
        assert out[st]["mentionsManualTunnel"] is False, (
            f"setup_state={st} still instructs a manual ssh tunnel — the "
            f"2026-08-04 decree forbids it on every surface")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_the_deprecated_cli_flow_is_never_taught():
    """`python -m lib.desktop_agent` is the SUPERSEDED path.

    desktop/launcher.py's own docstring says the in-process tray agent
    "Replaces the old 'install a second program and run python -m
    lib.desktop_agent' flow". Teaching a four-flag CLI invocation in a setup
    dialog is the opposite of minimising cognitive load.
    """
    out = _run(_shipped())["desktop"]
    for st, info in out.items():
        assert "lib.desktop_agent" not in info["text"], (
            f"setup_state={st} teaches the deprecated CLI flow")
        for flag in ("--allow-write", "--allow-exec", "--allow-gui",
                     "--allow-all", "--bridge-secret"):
            assert flag not in info["text"], (
                f"setup_state={st} exposes the raw CLI flag {flag}")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_browser_row_picks_the_actionable_instruction():
    """On-disk folder when the backend says so; download otherwise."""
    out = _run(_shipped())["browser"]

    assert out["connected"]["steps"] == 0, (
        "a connected extension must show NO install instruction")

    # extensionPath is sent only when the peer is loopback AND this machine has
    # a browser the server can drive — the folder is then genuinely reachable
    # from the user's own browser. The probe half matters because the loopback
    # half is a pure IP test that a same-host reverse proxy makes vacuously
    # true (routes/api_v1/browser.py::browser_status).
    assert out["load_unpacked"]["steps"] == 1
    assert out["load_unpacked"]["hasPath"] is True, (
        "on-disk case must show the folder to load")
    assert out["load_unpacked"]["hasDownloadBtn"] is False, (
        "a download button here would be a second, redundant path")

    # No path (or no drivable browser here) -> the user's browser cannot load a
    # server-side folder, so download-then-load is the only actionable path.
    assert out["download"]["steps"] == 1
    assert out["download"]["hasDownloadBtn"] is True
    assert out["download"]["hasPath"] is False


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_browser_row_distinguishes_the_install_states():
    """2026-08-04 stranded-fleet fix: 'never installed' / 'installed &
    current' / 'installed but outdated' / 'installed but LOCKED OUT' are
    four different situations and the row must say which. The locked-out
    case is load-bearing: a side-loaded extension with a dead credential
    cannot heal itself (no update channel, cannot poll), so the ONLY cure
    is the preseeded re-download — rendering it as 尚未安装 sends the user
    down an entirely wrong path."""
    out = _run(_shipped())["browser"]
    # Healthy + current: no nudge, no setup content.
    assert out["connected"]["steps"] == 0
    assert out["connected"]["hasDownloadBtn"] is False
    # Outdated but working: dot stays green, setup carries the upgrade.
    assert out["outdated"]["hasDownloadBtn"] is True, (
        "an outdated-but-working extension must get the one-click upgrade")
    assert "4.6.0" in out["outdated"]["text"] and "4.7.0" in out["outdated"]["text"], (
        "the nudge must name BOTH versions so the user can see what changes")
    # Locked out: the status must not claim 'not installed'.
    assert "失效" in out["stranded"]["statusText"], (
        f"a locked-out extension rendered as "
        f"{out['stranded']['statusText']!r} — the lie of omission this fix "
        f"exists to kill")
    assert out["stranded"]["hasDownloadBtn"] is True, (
        "the stranded state must offer the preseeded re-download")
    # Plain never-installed keeps the honest 'not installed'.
    assert "尚未安装" in out["download"]["statusText"]


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_load_unpacked_offers_the_one_click_open_button():
    """The on-disk case's ONE action: open the page + copy the path.

    What remains (Developer mode → Load unpacked → paste) lives inside
    Chrome's sandbox — no web page may do it — so the residual text must
    say so rather than implying the button finishes the install.
    """
    out = _run(_shipped())["browser"]
    assert out["load_unpacked"]["hasOpenBtn"] is True, (
        "the on-disk case must offer the one-click open-page button")
    assert "开发者模式" in out["load_unpacked"]["text"], (
        "the residual steps must be stated honestly — Chrome's sandbox "
        "makes them un-automatable")
    assert out["download"]["hasOpenBtn"] is False, (
        "a REMOTE user gets no page-open button — the window would open on "
        "the server, not on their machine")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_the_open_button_copies_the_path_and_calls_the_route():
    """One click does BOTH halves, and reports what happened."""
    click = _run(_shipped())["openClick"]
    assert click["present"] is True, "no open button to drive"
    assert click["routeCalls"] == 1, (
        f"the click must call the server route exactly once, got "
        f"{click['routeCalls']}")
    assert click["clip"] == "/srv/tofu/browser_extension", (
        f"the click must copy the extension path, got {click['clip']!r}")
    assert click["note"].strip(), (
        "the click must report the outcome — a silent button is a dead one")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_connected_state_lights_the_dot():
    """Complement: the merge must not lose the live status signal itself."""
    out = _run(_shipped())["desktop"]
    assert out["connected"]["dotConnected"] is True, (
        "a connected agent must light the status dot")


# ── 3. NEUTER — prove the detection is load-bearing ────────────────────

@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_ignoring_setup_state_collapses_the_three_paths():
    """Make the desktop renderer ignore the backend's verdict.

    If the guard stays green after this, it was never testing the detection —
    only that *something* renders.
    """
    out = _run(_shipped(
        lambda s: s.replace("switch (d.setup_state) {", "switch ('remote') {")
    ))["desktop"]
    texts = {st: out[st]["text"] for st in ("tray", "local_source", "remote")}
    assert len(set(texts.values())) == 1, (
        "NEUTER should have collapsed every state to one instruction")
    # And the specific consequence: a tray user gets told to mint a token.
    assert out["tray"]["hasMintButton"] is True


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_showing_every_path_at_once_breaks_the_one_action_rule():
    """A menu of all three instructions must be rejected, not tolerated."""
    out = _run(_shipped(lambda s: s.replace(
        "case 'tray':\n      // Packaged desktop app: the agent runs IN-PROCESS. One click, no token,\n      // no second program to install.\n      setup.innerHTML = '<p class=\"lc-step\">'",
        "case 'tray':\n      setup.innerHTML = '<p class=\"lc-step\">x</p><p class=\"lc-step\">y</p>' + '<p class=\"lc-step\">'",
    )))["desktop"]
    assert out["tray"]["steps"] > 1, "NEUTER did not inject extra instructions"


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_rendering_an_instruction_while_connected_is_noise():
    """Complement to the 'exactly one' rule: connected must render ZERO.

    Without this, deleting the whole instruction block would keep every other
    test green while making the modal useless.
    """
    out = _run(_shipped(
        lambda s: s.replace("case 'connected':\n      setup.innerHTML = '';",
                            "case 'connected':\n      setup.innerHTML = '<p class=\"lc-step\">z</p>';")
    ))["desktop"]
    assert out["connected"]["steps"] == 1, (
        "NEUTER did not inject an instruction into the connected state")


# ── 4. The remote case must be ACTIONABLE, not just present ────────────
# "Install the desktop app" with no link, and a bare secret with no address,
# are both dead ends: the user is told to do something they cannot do from
# here. These assert the two halves that make the instruction followable.

@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_remote_state_offers_a_real_download_link():
    """The remote user does not have the app — give them a way to get it.

    The link target comes from the backend (derived from the ONE
    ``UPDATE_REPO`` constant), so a fork points at its own releases. Asserting
    "an anchor with an http(s) href" rather than a specific URL keeps this
    green when the slug legitimately changes and red when the link vanishes.
    """
    out = _run(_shipped())["desktop"]
    href = out["remote"]["downloadHref"]
    assert href.startswith(("http://", "https://")), (
        f"remote case must render a real, followable download link; got "
        f"{href!r}. Telling a user to install something with no link is not "
        f"an actionable instruction.")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_only_the_states_that_need_the_app_show_a_download_link():
    """A download link belongs exactly where the user does NOT have the app.

    tray + connected already have it — a link there competes with the one
    real action. local_source + remote do not — a sentence saying "install
    the app" with no way to GET it is the dead end review caught in the
    remote branch; local_source shipped the same shape and is fixed to the
    same standard.
    """
    out = _run(_shipped())["desktop"]
    for st in ("connected", "tray"):
        assert out[st]["downloadHref"] == "", (
            f"setup_state={st} must not offer a desktop-app download — the "
            f"app is already on this machine")
    for st in ("local_source", "remote"):
        assert out[st]["downloadHref"].startswith(("http://", "https://")), (
            f"setup_state={st} tells the user to install the desktop app "
            f"but gives no way to get it")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_the_minted_line_carries_the_server_address():
    """One copy must be enough — a naked token is unusable.

    Nothing on the user's machine knows which server to poll, so the token
    alone leaves them holding a secret with nowhere to put it. Drives the REAL
    mint handler and reads what actually lands in the box.
    """
    line = _run(_shipped())["mintedLine"]
    assert "T" in line, "the minted token must appear in the line"
    assert "tofu.example.com" in line, (
        f"the connect line must carry the server address the agent has to "
        f"reach, not just the secret; got {line!r}")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_a_server_hosted_entry_shows_where_the_file_comes_from():
    """A download served by THIS server must say so, and name its size.

    Server-hosting exists so the install does not depend on the public
    GitHub network. Silently rendering it identically to a GitHub link
    would hide the one fact that explains why it is fast and reliable —
    and a 115 MB installer with no size shown is its own bad surprise.
    """
    out = _run(_shipped())["desktop"]
    assert out["remote"]["directCount"] >= 1, (
        "a downloads payload must render per-platform direct links")
    assert out["remote"]["hostedText"], (
        "a server-hosted artifact renders no provenance badge — the user "
        "cannot tell it downloads from this server rather than GitHub")
    assert "MB" in out["remote"]["text"], (
        "the artifact size must appear next to the download")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_the_direct_link_is_rebased_onto_the_live_proxy_prefix():
    """The reported 404: the backend builds downloads[].url from
    request.host_url, which under a path-prefixed cloud-IDE proxy
    (…/proxy/15000/) LACKS the prefix — the click died on the gateway's
    default route and never reached Tofu (zero /desktop/download hits in
    access.log). With a live base path the rendered href must carry it; the
    releases-page escape hatch (no /api/ marker) must pass through
    untouched."""
    out = _run_proxied(_shipped())["desktop"]
    href = out["remote"]["downloadHref"]
    assert href == ('/proxy/15000/api/v1/desktop/download/'
                    'Tofu-Setup-0.15.2-win64.exe'), href
    assert out["remote"]["pageHref"] == (
        'https://github.com/rangehow/ToFu/releases/latest'), (
        'the escape hatch is external — rebasing it would break it')


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_rendering_the_server_url_verbatim_is_caught():
    """Strip the rebase (render p.url verbatim) → the prefix-less absolute
    URL goes straight into the href: the exact reported 404 shape."""
    out = _run_proxied(_shipped(
        lambda s: s.replace("_lcResolveDlUrl(p.url)", "p.url")
    ))["desktop"]
    href = out["remote"]["downloadHref"]
    assert not href.startswith('/proxy/15000/'), (
        'NEUTER did not remove the rebase — the href still carries the '
        'live prefix')


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_stripping_the_download_link_is_caught():
    """No links helper output → told to install with no way to get the app."""
    out = _run(_shipped(
        lambda s: s.replace(
            "function _lcDownloadLinks(d, kind, suppressPage) {\n  kind = kind || 'full';",
            "function _lcDownloadLinks(d, kind, suppressPage) {\n  return '';\n  kind = kind || 'full';")
    ))["desktop"]
    assert out["remote"]["downloadHref"] == "", (
        "NEUTER did not remove the followable link")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_dropping_the_platform_matched_links_is_caught():
    """Lose the direct row → only the five-asset releases page remains.

    The whole point of `downloads` is one click on the file THIS machine
    takes; a regression that empties the row while keeping the page anchor
    must not pass silently — the UI would still LOOK like it offers help.
    """
    out = _run(_shipped(
        lambda s: s.replace("if (picks.length) {", "if (false && picks.length) {")
    ))["desktop"]
    assert out["local_source"]["directCount"] == 0, (
        "NEUTER did not remove the platform-matched direct links")
    assert out["local_source"]["downloadHref"].startswith(("http://", "https://")), (
        "the escape-hatch page link must survive — the NEUTER targets the "
        "direct row, not the whole instruction")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_reverting_to_a_bare_token_is_caught():
    """Make the connect line the token alone → the address is gone.

    This is exactly the shape that shipped first: 32 characters of secret and
    no indication of where it goes.
    """
    out = _run(_shipped(
        lambda s: s.replace("return srv ? (srv + '  ' + token) : token;",
                            "return token;")
    ))
    assert "tofu.example.com" not in out["mintedLine"], (
        "NEUTER did not reduce the line to a bare token")


# ── 5. Usability: never invite a click that grants nothing ─────────────
# The original bug was a toggle that lit up while the tool registry shipped
# ZERO tools. Moving it into a nicer modal does not fix that — a switch the
# user CAN flip while nothing is connected reproduces the same dead end.

@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_the_switch_is_inert_until_the_capability_can_actually_work():
    """Disconnected → the switch cannot be flipped, and says why.

    ``lib/tools/registry/_build.py`` returns [] for an unconnected bridge, so
    turning it on would give the AI nothing. Asserting the RESULT (not
    operable + a reason) rather than any CSS class.
    """
    out = _run(_shipped())
    for st in ("tray", "local_source", "remote"):
        assert out["desktop"][st]["switchUsable"] is False, (
            f"setup_state={st}: nothing is connected, yet the user can switch "
            f"desktop control ON — that grants zero tools and reproduces the "
            f"exact silent failure this merge set out to remove")
        assert out["desktop"][st]["switchReason"].strip(), (
            f"setup_state={st}: the switch is inert but gives no reason")
    for k in ("load_unpacked", "download"):
        assert out["browser"][k]["switchUsable"] is False, (
            f"browser {k}: no extension connected, switch must not be flippable")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_a_connected_capability_is_switchable():
    """Complement — otherwise 'disable everything' would pass the test above."""
    out = _run(_shipped())
    assert out["desktop"]["connected"]["switchUsable"] is True, (
        "a connected agent MUST be switchable, or the feature is unreachable")
    assert out["browser"]["connected"]["switchUsable"] is True, (
        "a connected extension MUST be switchable")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_each_row_says_what_the_ai_actually_gets():
    """Granting access to your browser session and machine needs informed consent.

    "Browser tabs" / "This computer" alone does not let a user judge the risk.
    Asserted as a non-empty, DISTINCT line per row — not a fixed string, so
    the wording can be improved without a false red.
    """
    out = _run(_shipped())
    b = out["browser"]["connected"]["about"]
    d = out["desktop"]["connected"]["about"]
    assert b, "browser row must say what enabling it grants"
    assert d, "computer row must say what enabling it grants"
    assert b != d, (
        "the two rows describe very different powers (reading a tab vs "
        "running a shell command); one shared sentence hides that")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_the_permissions_note_only_appears_where_it_can_be_followed():
    """It explains the TRAY menu — useless to someone with nothing installed.

    Showing it in the remote/source cases puts a second, unfollowable
    instruction next to the one real next action.
    """
    out = _run(_shipped())["desktop"]
    assert out["tray"]["permNoteShown"] is True, (
        "the tray user CAN open that menu — the note belongs here")
    assert out["connected"]["permNoteShown"] is True, (
        "a running agent's permissions are adjustable — keep the note")
    for st in ("local_source", "remote"):
        assert out[st]["permNoteShown"] is False, (
            f"setup_state={st} has no tray to open; the note is an "
            f"instruction the user cannot follow")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_allowing_the_switch_while_disconnected_is_caught():
    """Ungate the switch → the dead-end toggle is back."""
    out = _run(_shipped(
        lambda s: s.replace("sw.disabled = !can;", "sw.disabled = false;")
    ))
    assert out["desktop"]["tray"]["switchUsable"] is True, (
        "NEUTER did not re-enable the switch")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_blanking_the_capability_line_is_caught():
    """Remove the description → the user grants access without being told what."""
    out = _run(_shipped(
        lambda s: s.replace("host.textContent = text;", "host.textContent = '';")
    ))
    assert out["desktop"]["connected"]["about"] == "", (
        "NEUTER did not blank the capability line")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_showing_the_permissions_note_everywhere_is_caught():
    """Always-on note → an unfollowable instruction in the remote case."""
    out = _run(_shipped(
        lambda s: s.replace("perm.style.display = trayReachable ? '' : 'none';",
                            "perm.style.display = '';")
    ))["desktop"]
    assert out["remote"]["permNoteShown"] is True, (
        "NEUTER did not force the note into the remote case")


# ── 5b. Revoking access must never be harder than granting it ─────────
#
# Gating the switch on "is it connected" is right for turning something ON
# (an unconnected bridge ships zero tools). Applying the SAME gate to turning
# it OFF produced a worse failure than the one it fixed: a capability enabled
# while the agent was up became UNREVOKABLE the moment that agent dropped —
# the wire flag stayed ON and kept travelling to the server, the switch showed
# ON and greyed out, and the one action a worried user wants (withdraw access
# to their own machine) was the one action the UI refused.
#
# These drive the WHOLE shipped file — the toggle handlers, the reachability
# bookkeeping and the badge are all part of the behaviour under test, so
# slicing individual renderers would miss the interaction between them.

LC_FILE_HARNESS = textwrap.dedent("""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM(`<!DOCTYPE html><body>{html}
      <div id="localControlToggle"></div>
      <span id="localControlBadge"></span></body>`);
    global.document = dom.window.document;
    global.window = dom.window;
    global.t = (k) => k;
    global.escapeHtml = (s) => String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    global.Api = {{ desktop: {{ mintToken: () => Promise.resolve({{token:'T'}}) }} }};
    global.showToast = () => {{}};
    global._safeClipboardWrite = () => Promise.resolve();
    global.downloadBrowserExtension = () => {{}};
    global._applyBrowserLnaWarning = () => {{}};
    // The real wire flags + their real painters live in main.js; the modal
    // only ever reaches them through these two setters.
    global.browserEnabled = false;
    global.desktopEnabled = false;
    global._applyBrowserUI = (v) => {{ global.browserEnabled = !!v; }};
    global._applyDesktopUI = (v) => {{ global.desktopEnabled = !!v; }};
    global._saveConvToolState = () => {{}};
    global.updateSubmenuCounts = () => {{}};

    {shipped}

    const dsw = () => document.getElementById('lcDesktopSwitch');
    const badge = () => document.getElementById('localControlBadge');
    const snap = () => ({{
      operable: !dsw().disabled,
      shownOn: dsw().classList.contains('on'),
      flaggedStale: dsw().classList.contains('lc-switch-stale'),
      badgeStale: badge().classList.contains('lc-badge-stale'),
      badgeText: badge().textContent,
      reason: dsw().getAttribute('title') || '',
    }});

    const out = {{}};

    // (a) Enabled while the agent was up, agent then drops.
    global.desktopEnabled = true;
    _lcRenderDesktop({{ connected: false, setup_state: 'tray' }});
    out.enabledThenDropped = snap();
    toggleDesktopFromLocalModal();          // the user tries to revoke
    out.afterRevoke = {{ flag: global.desktopEnabled, ...snap() }};

    // (b) Complement: OFF + disconnected must still refuse to turn ON.
    global.desktopEnabled = false;
    _lcRenderDesktop({{ connected: false, setup_state: 'tray' }});
    out.offAndDisconnected = snap();
    toggleDesktopFromLocalModal();
    out.offFlipAttempt = {{ flag: global.desktopEnabled }};

    // (c) Complement: a connected capability behaves exactly as before.
    global.desktopEnabled = false;
    _lcRenderDesktop({{ connected: true, setup_state: 'connected' }});
    out.connectedBefore = snap();
    toggleDesktopFromLocalModal();
    out.connectedAfterOn = {{ flag: global.desktopEnabled, ...snap() }};

    // (d) Never probed: the modal was never opened this session. An
    //     unverified capability must not be presented as broken.
    global.desktopEnabled = true;
    global.browserEnabled = false;
    _lcReach.desktop = null; _lcReach.browser = null;
    _lcUpdateBadge();
    out.neverProbed = snap();

    console.log(JSON.stringify(out));
""")


def _run_file(neuter=None) -> dict:
    """Eval the ENTIRE shipped local-control.js — handlers, state and badge."""
    path = _find_defining_file("toggleDesktopFromLocalModal")
    body = path.read_text(encoding="utf-8")
    if neuter:
        rewritten = neuter(body)
        assert rewritten != body, (
            "NEUTER substitution did not apply — the anchor text is gone, so "
            "this run proves nothing about whether the guard bites")
        body = rewritten
    script = LC_FILE_HARNESS.format(shipped=body,
                                    html=MODAL_HTML.replace("`", "\\`"))
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_an_enabled_capability_can_always_be_switched_off():
    """The agent dropping must not trap the user with access they can't revoke."""
    out = _run_file()
    assert out["enabledThenDropped"]["operable"] is True, (
        "desktop control is ON but the agent is gone, and the switch is "
        "inert — the user cannot withdraw access to their own machine. A "
        "safety control must never be harder to turn off than on.")
    assert out["afterRevoke"]["flag"] is False, (
        "clicking the switch did not actually clear the wire flag; it is "
        "still being sent to the server")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_an_enabled_but_unreachable_capability_does_not_look_healthy():
    """ON + disconnected ⇒ the AI gets ZERO tools. Saying otherwise is the lie
    this whole merge exists to stop telling.

    ``_build_desktop`` returns [] for an unconnected agent, so a badge that
    counts it identically to a live one sends the user away believing the AI
    can reach their machine.
    """
    out = _run_file()
    assert out["enabledThenDropped"]["flaggedStale"] is True, (
        "the switch shows ON with no agent connected and is not marked — it "
        "reads as a working capability that ships no tools")
    assert out["enabledThenDropped"]["badgeStale"] is True, (
        "the merged badge counts an unreachable capability as live; the user "
        "closes the modal and sees a confident badge for tools that do not "
        "exist")
    assert out["enabledThenDropped"]["reason"].strip(), (
        "flagged as stale but with no explanation of what is wrong")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_turning_it_on_while_disconnected_is_still_refused():
    """Complement: the one-way gate must not become no gate at all."""
    out = _run_file()
    assert out["offAndDisconnected"]["operable"] is False, (
        "OFF + disconnected must stay inert — enabling it grants zero tools")
    assert out["offFlipAttempt"]["flag"] is False, (
        "the flip was not actually refused")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_a_connected_capability_is_unaffected():
    """Complement: none of the above may degrade the working path."""
    out = _run_file()
    assert out["connectedBefore"]["operable"] is True
    assert out["connectedBefore"]["flaggedStale"] is False, (
        "a connected capability must not be flagged as stale")
    assert out["connectedAfterOn"]["flag"] is True, "connected → must turn on"
    assert out["connectedAfterOn"]["badgeStale"] is False, (
        "a live capability must not warn")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_an_unprobed_capability_is_not_reported_as_broken():
    """Complement: 'never checked' ≠ 'unreachable'.

    The badge paints on conversation restore, long before the modal is ever
    opened. Treating unprobed as broken would warn about a perfectly healthy
    setup — its own kind of false alarm.
    """
    out = _run_file()
    assert out["neverProbed"]["badgeStale"] is False, (
        "a capability that was never probed is being reported as broken")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_gating_both_directions_traps_the_user():
    """Restore the symmetric gate → revoking becomes impossible again."""
    out = _run_file(lambda s: s.replace(
        "var can = canEnable || !!on;   // already on ⇒ always revocable",
        "var can = canEnable;"))
    assert out["enabledThenDropped"]["operable"] is False, (
        "NEUTER did not restore the symmetric gate")
    assert out["afterRevoke"]["flag"] is True, (
        "NEUTER should have made the wire flag unrevokable")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_counting_unreachable_capabilities_as_live_is_caught():
    """Drop the staleness split → the badge overstates what the AI has."""
    out = _run_file(lambda s: s.replace(
        "var stale = ((bOn && _lcReach.browser === false) ? 1 : 0)\n"
        "            + ((dOn && _lcReach.desktop === false) ? 1 : 0);",
        "var stale = 0;"))
    assert out["enabledThenDropped"]["badgeStale"] is False, (
        "NEUTER did not collapse the badge back to a plain count")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_letting_a_toggle_assert_reachability_is_caught():
    """The toggle must report what the POLL observed, not assume success.

    Passing a hardcoded `true` here re-paints a disconnected capability as
    healthy the instant the user touches it — the stale marking would vanish
    on click while the tools still do not exist.
    """
    out = _run_file(lambda s: s.replace(
        "_lcSetSwitch('lcDesktopSwitch', desktopEnabled, _lcReach.desktop !== false);",
        "_lcSetSwitch('lcDesktopSwitch', desktopEnabled, true);"))
    # Turning it ON from the connected state, then the flag stays on: with the
    # NEUTER the switch no longer consults observed reachability at all.
    assert out["afterRevoke"]["flaggedStale"] is False


# ── 5c. Granting machine access must not leak into a new conversation ──

def test_a_new_chat_does_not_inherit_computer_control():
    """Desktop was the ONE tool flag `_resetToolsToDefaults` never cleared.

    `_restoreConvToolState` sets it per-conversation and nothing reset it, so
    a brand-new chat silently inherited computer control — shell, file writes,
    GUI — from whatever conversation came before, and the merged badge then
    reported it as active on a conversation the user never granted it on.

    Asserts the RESULT (the reset path clears both local-control flags) by
    locating the function via symbol search, not a hardcoded path or line.
    """
    reset = _slice_fn("_resetToolsToDefaults")
    assert "_applyBrowserUI(false)" in reset, (
        "browser access is no longer reset on a new chat")
    assert "_applyDesktopUI(false)" in reset, (
        "a new chat does not reset desktop control, so it is inherited from "
        "the previous conversation — the highest-risk capability here "
        "(shell / file writes / GUI) must not carry over by omission")


def test_the_reset_path_covers_every_local_control_flag():
    """Complement: pin the pair together so the next capability isn't forgotten.

    The bug was an omission, not a wrong value — one setter simply missing
    from a long list. Asserting both flags reset in the same place makes the
    omission visible rather than silent.
    """
    reset = _slice_fn("_resetToolsToDefaults")
    restored = _slice_fn("_restoreConvToolState")
    for setter in ("_applyBrowserUI", "_applyDesktopUI"):
        assert setter in restored, (
            f"{setter} is not restored per-conversation — the guard below "
            f"would then be pinning a flag nothing sets")
        assert setter in reset, (
            f"{setter} is restored per-conversation but never reset for a new "
            f"chat, so its value leaks across conversations")


# ── 6. No dead i18n strings on this surface ────────────────────────────

def test_no_orphaned_local_control_strings():
    """A declared-but-never-rendered string is a promise nobody keeps.

    Four ``browser.*`` keys described what the bridge could do and had ZERO
    render points anywhere — the guidance existed only in the dictionary.
    Scans the whole surface (html + non-i18n js) rather than one file.
    """
    i18n = (JS_DIR / "i18n.js").read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    js_sources = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(JS_DIR.rglob("*.js"))
        if "i18n" not in p.name
        and not p.name.startswith((".", "bundle-", "feature-")))
    declared = set(re.findall(r"^\s*'((?:local|browser)\.[A-Za-z0-9_]+)':",
                              i18n, re.M))
    assert declared, "scan surface empty — the regex stopped matching"
    orphans = sorted(k for k in declared
                     if k not in html and k not in js_sources)
    assert not orphans, (
        f"these keys are declared but rendered nowhere: {orphans}. Either "
        f"render them or delete them — a dictionary entry no user can see is "
        f"a maintenance trap that reads like shipped guidance.")


# ── 7. Live polling — the 15s connection window ────────────────────────

def test_the_modal_polls_while_open():
    """`is_desktop_agent_connected()` is a 15s window, and a user enabling the
    tray agent while looking at this dialog must see the dot flip WITHOUT
    reopening it. The old _checkBrowserStatus was one-shot-on-open; that
    limitation must not be inherited.

    Asserts the RESULT (a repeating refresh that is torn down on close),
    located by symbol rather than by line.
    """
    src = _find_defining_file("openLocalControlModal").read_text(encoding="utf-8")
    open_fn = _slice_fn("openLocalControlModal")
    close_fn = _slice_fn("closeLocalControlModal")
    assert "setInterval" in open_fn, (
        "opening the modal must start a repeating refresh — a one-shot check "
        "cannot show the dot flipping within the 15s connection window")
    assert "clearInterval" in close_fn, (
        "closing the modal must stop the poll, or a background tab keeps "
        "hitting the server forever")
    m = re.search(r"_LC_POLL_MS\s*=\s*(\d+)", src)
    assert m, "poll cadence must be a named constant"
    assert int(m.group(1)) < 15000, (
        f"poll cadence {m.group(1)}ms is not shorter than the 15s connection "
        f"window — a state change could be missed entirely")


# ── 8. The open-extensions route: loopback-gated, shell-free ─────────
#
# The button above lands on POST /api/v1/browser/open-extensions. Two facts
# are load-bearing: a REMOTE peer must never make the server spawn a browser
# on its own (headless) machine, and the spawn must be a list-form argv —
# no shell for a future argument to travel through.

_ROUTE_TOKEN = '_lc_route_test_token__'


def _post_open_extensions(flask_client, monkeypatch, client_addr, *,
                          chrome='/fake/chrome'):
    """Drive ONE real POST through the full stack with a chosen socket peer.

    ``scope_base={'client': ...}`` sets the ASGI peer the way Hypercorn
    would (the mechanism tests/test_proxy_trust.py established), so the REAL
    ``_remote_is_loopback`` runs against it. The tunnel-token header gives a
    remote peer a valid credential: open mode refuses its synthetic admin
    grant to non-loopback peers, so without one the request would die at the
    auth gate (401) before ever reaching the route under test.
    """
    import subprocess as _sp

    from routes.api_v1 import browser as browser_routes

    calls = []

    def _fake_popen(argv, **kwargs):
        calls.append({'argv': argv, 'kwargs': kwargs})
        return object()

    monkeypatch.setattr(_sp, 'Popen', _fake_popen)
    # The route now resolves the browser through a PROBE that also reports the
    # family-specific extensions URL (chrome:// vs edge://) — the Chrome-only
    # _find_chrome_binary this used to patch is gone. `chrome=None` still
    # means "no browser on this machine".
    monkeypatch.setattr(
        browser_routes, '_detect_local_browser',
        lambda: ({'binary': chrome, 'family': 'chrome', 'name': 'Chrome',
                  'extensionsUrl': 'chrome://extensions'} if chrome else None))
    monkeypatch.setenv('TUNNEL_TOKEN', _ROUTE_TOKEN)
    resp = flask_client.post(
        '/api/v1/browser/open-extensions',
        headers={'X-Tunnel-Token': _ROUTE_TOKEN},
        scope_base={'client': client_addr})
    return resp.status_code, (resp.get_json(silent=True) or {}), calls


def test_open_extensions_refuses_a_remote_peer(flask_client, monkeypatch):
    """The page opens on the SERVER — a remote peer gets 403 and NO spawn.

    An authenticated remote user is exactly who the gate exists for: valid
    credential, wrong machine. Spawning here would open a browser window on
    a headless server nobody is looking at.
    """
    status, body, calls = _post_open_extensions(
        flask_client, monkeypatch, ('203.0.113.7', 5555))
    assert status == 403, (
        f"a non-loopback peer must be refused — got {status} with {body}")
    assert calls == [], (
        f"the route spawned a browser for a REMOTE peer: {calls}")


def test_open_extensions_argv_is_shell_free(flask_client, monkeypatch):
    """The launch is a list-form argv headed by the browser binary."""
    status, body, calls = _post_open_extensions(
        flask_client, monkeypatch, ('127.0.0.1', 5555))
    assert status == 200, f"loopback launch failed: {status} {body}"
    assert body.get('ok') is True
    assert len(calls) == 1, f"expected exactly one spawn, got {calls}"
    argv, kwargs = calls[0]['argv'], calls[0]['kwargs']
    assert isinstance(argv, list) and argv[0] == '/fake/chrome', (
        f"argv must be list-form headed by the browser binary, got {argv!r}")
    assert 'chrome://extensions' in argv, (
        f"the extensions page URL is missing from {argv!r}")
    assert kwargs.get('shell') is not True, (
        "shell=True today is where the next interpolated argument becomes "
        "an injection vector tomorrow")


def test_open_extensions_reports_when_no_chrome_exists(flask_client,
                                                       monkeypatch):
    """No Chrome found → a clear 404, and NOTHING is spawned.

    The UI keeps the manual instruction for this case; the route must say
    'not found', not crash and not pretend success.
    """
    status, body, calls = _post_open_extensions(
        flask_client, monkeypatch, ('127.0.0.1', 5555), chrome=None)
    assert status == 404, (
        f"expected 404 when no browser exists, got {status} with {body}")
    assert calls == []
