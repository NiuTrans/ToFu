"""tests/test_frontend_local_control_floor.py — the install prompt must be on
screen in the FIRST paint, never behind a "checking…" state.

WHAT THIS GUARDS
----------------
Opening Local Control used to show, for every user:

    ● 正在检查…          ← status
    (empty box)          ← where the install instruction goes

``openLocalControlModal`` painted the modal, then fired two async status
calls; only when those resolved did any instruction appear. So the user who
needs this dialog most — the one who has installed nothing — paid a network
round-trip to be told the thing that is true for almost everyone opening it.

Three failure modes, not one:

  1. **The wait itself.** On a slow or remote backend the box sits empty.
  2. **A failed status call BLANKED it.** Both renderers ran
     ``setup.innerHTML = ''`` on error, so a backend hiccup replaced the
     guidance with nothing — worst exactly when things are already going
     wrong.
  3. **``Api`` not yet defined returned EARLY.** ``_lcRefresh`` opens with
     ``if (typeof Api === 'undefined' …) return;`` — no repaint, no error. The
     modal then showed "正在检查…" over an empty box *permanently*.

THE INVARIANT PINNED HERE
-------------------------
**Detection UPGRADES an instruction that is already on screen; it is never
what puts one there.** The floor (download the extension / install the desktop
app) needs no backend knowledge, so it ships in the static HTML and is
repainted by ``_lcPaintFloor`` on open. The worst case is guidance that is
merely generic — never guidance that is absent, and never a spinner.

WHY THE HTML IS ASSERTED, NOT JUST THE JS
-----------------------------------------
``index.html`` is served before any JS parses (the bundle is a concatenation
built at request time — see ``lib/js_bundler.py``). If the floor lived only in
JS, the first paint would still be an empty box on a cold load. Pinning the
static markup is what makes "first paint" literal rather than aspirational.
"""

from __future__ import annotations

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
LC_JS = JS_DIR / "local-control.js"


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not available")
    return exe


@functools.lru_cache(maxsize=1)
def _has_jsdom() -> bool:
    try:
        subprocess.run([_node(), "-e", "require('jsdom')"], cwd=ROOT,
                       capture_output=True, check=True)
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════
#  The static floor — what the browser paints before any JS runs
# ══════════════════════════════════════════════════════════════════

def _setup_div(html: str, div_id: str) -> str:
    """Return the inner HTML of one ``lc-cap-setup`` container."""
    m = re.search(
        r'<div class="lc-cap-setup" id="%s">(.*?)</div>' % re.escape(div_id),
        html, re.S)
    assert m, f"#{div_id} container not found in index.html"
    return m.group(1)


def test_the_shipped_html_already_carries_an_install_instruction():
    """THE REGRESSION. Both setup boxes shipped EMPTY (`<div …></div>`).

    Everything the user saw before the first status response came back was a
    spinner over a blank panel.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    for div_id in ("lcBrowserSetup", "lcDesktopSetup"):
        inner = _setup_div(html, div_id)
        assert inner.strip(), (
            f"#{div_id} ships empty, so the first paint of Local Control is a "
            f"blank box — the install instruction must be in the static HTML, "
            f"because index.html is served before any JS parses.")
        assert "lc-step" in inner, (
            f"#{div_id} has content but no .lc-step instruction — the floor "
            f"must be a followable step, not decoration.")


def test_the_first_paint_is_never_a_loading_state():
    """"Checking…" over an empty box tells the user to wait for nothing.

    Both status labels shipped ``data-i18n="local.checking"``. The honest
    default is the state that is true for anyone who needs this dialog.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "local.checking" not in html, (
        "the modal still ships a 'checking…' status. Detection resolves in "
        "one poll; until then the honest label is the not-installed state, "
        "which is also what the poll will almost always confirm.")
    for row_id, expect in (("lcBrowserStatus", "local.notInstalled"),
                           ("lcDesktopStatus", "local.notRunning")):
        m = re.search(
            r'id="%s">(.*?)</span>\s*</span>' % re.escape(row_id), html, re.S)
        assert m and expect in m.group(1), (
            f"#{row_id} must default to {expect}")


def test_the_browser_floor_is_immediately_actionable():
    """The floor is only a floor if the user can act on it with no backend.

    ``downloadBrowserExtension()`` is a pure frontend call, which is exactly
    what makes this instruction usable before any status lands.
    """
    inner = _setup_div(INDEX_HTML.read_text(encoding="utf-8"), "lcBrowserSetup")
    assert 'id="lcExtDownloadBtn"' in inner, (
        "the browser floor must ship its download button, not just prose")


def test_the_desktop_floor_makes_no_claim_it_cannot_back():
    """No hardcoded download URL: it derives from the backend's UPDATE_REPO.

    Baking a URL into the static HTML would point a fork at the upstream
    releases page — a confidently wrong link, which is worse than a named
    step with no shortcut. ``_lcRenderDesktop`` adds the real link once the
    payload lands.
    """
    inner = _setup_div(INDEX_HTML.read_text(encoding="utf-8"), "lcDesktopSetup")
    assert "github.com" not in inner and "href=" not in inner, (
        "the desktop floor hardcodes a link. The download URL comes from the "
        "backend (UPDATE_REPO, env-overridable), so a baked-in one sends "
        "forks to the wrong releases page.")


def test_every_floor_string_is_translated():
    """A floor that renders a raw key is a floor that failed.

    The static markup is what a cold load paints, so an untranslated key here
    is visible before any JS could correct it.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    i18n = (JS_DIR / "i18n.js").read_text(encoding="utf-8")
    keys = set()
    for div_id in ("lcBrowserSetup", "lcDesktopSetup"):
        keys |= set(re.findall(r'data-i18n="([^"]+)"', _setup_div(html, div_id)))
    assert keys, "the floor renders no translated strings at all"
    for key in sorted(keys):
        assert re.search(r"^\s*'%s':" % re.escape(key), i18n, re.M), (
            f"floor string {key!r} is not defined in i18n.js — it would "
            f"render as the literal key on first paint")


# ══════════════════════════════════════════════════════════════════
#  The JS floor — behaviour on open, on error, and when Api is absent
# ══════════════════════════════════════════════════════════════════

# The modal markup with EMPTY setup boxes. Deliberately not the shipped
# static floor: this harness tests that the JS can put an instruction there
# on its own, which is what has to hold when the box was cleared by a prior
# render (a connected capability empties it) or on a re-open.
MODAL_HTML = """
<div id="localControlModal">
  <span class="lc-cap-status" id="lcBrowserStatus">
    <span class="browser-status-dot disconnected"></span>
    <span class="lc-status-text">STALE</span>
  </span>
  <button class="lc-switch" id="lcBrowserSwitch" aria-checked="false"></button>
  <p class="lc-cap-about" id="lcBrowserAbout"></p>
  <div class="lc-cap-setup" id="lcBrowserSetup"></div>
  <span class="lc-cap-status" id="lcDesktopStatus">
    <span class="browser-status-dot disconnected"></span>
    <span class="lc-status-text">STALE</span>
  </span>
  <button class="lc-switch" id="lcDesktopSwitch" aria-checked="false"></button>
  <p class="lc-cap-about" id="lcDesktopAbout"></p>
  <div class="lc-cap-setup" id="lcDesktopSetup"></div>
  <p class="lc-perm-note" id="lcPermNote" style="display:none"></p>
</div>
<div id="localControlToggle"></div><span id="localControlBadge"></span>
"""

HARNESS = textwrap.dedent("""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM(`<!DOCTYPE html><body>{html}</body>`);
    global.document = dom.window.document;
    global.window = dom.window;
    global.t = (k) => k;   // real t() returns the KEY when undefined — match it
    global.escapeHtml = (s) => String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    global.browserEnabled = false;
    global.desktopEnabled = false;
    global._applyBrowserUI = (v) => {{ global.browserEnabled = !!v; }};
    global._applyDesktopUI = (v) => {{ global.desktopEnabled = !!v; }};
    global._saveConvToolState = () => {{}};
    global.updateSubmenuCounts = () => {{}};
    global.showToast = () => {{}};
    global._safeClipboardWrite = () => Promise.resolve();
    global._applyBrowserLnaWarning = () => {{}};
    global._rec = {{ downloads: 0 }};
    global.downloadBrowserExtension = () => {{ global._rec.downloads++; }};
    {api_stub}

    {shipped}

    const read = () => ({{
      browserSteps: document.querySelectorAll('#lcBrowserSetup .lc-step').length,
      browserText: document.getElementById('lcBrowserSetup').textContent.trim(),
      browserBtn: !!document.getElementById('lcExtDownloadBtn'),
      desktopSteps: document.querySelectorAll('#lcDesktopSetup .lc-step').length,
      desktopText: document.getElementById('lcDesktopSetup').textContent.trim(),
      browserStatus: document.querySelector(
        '#lcBrowserStatus .lc-status-text').textContent.trim(),
      desktopStatus: document.querySelector(
        '#lcDesktopStatus .lc-status-text').textContent.trim(),
    }});

    const out = {{}};

    // (a) SYNCHRONOUS open. Read IMMEDIATELY — before any promise can settle.
    //     This is literally the first frame the user sees.
    openLocalControlModal();
    out.firstPaint = read();

    // Did the floor's button actually work with no backend at all?
    const b = document.getElementById('lcExtDownloadBtn');
    if (b && b.onclick) b.onclick();
    out.floorBtnWorks = global._rec.downloads;
    closeLocalControlModal();

    // (b) A FAILED status call must not blank the guidance.
    _lcRenderBrowser(null, new Error('boom'));
    _lcRenderDesktop(null, new Error('boom'));
    out.afterError = read();

    // (c) A CONNECTED capability still clears its box — the floor must not
    //     have turned into an instruction that can never be dismissed.
    _lcRenderBrowser({{ connected: true, clients: [{{client_id:'x'}}] }});
    _lcRenderDesktop({{ connected: true, setup_state: 'connected' }});
    out.whenConnected = read();

    // (d) Detection UPGRADES the floor to the more specific instruction.
    _lcRenderBrowser({{ connected: false,
                       extensionPath: '/srv/tofu/browser_extension',
                       localBrowser: {{ family:'chrome', name:'Chrome' }} }});
    out.upgraded = {{
      hasPath: document.getElementById('lcBrowserSetup')
                 .textContent.includes('/srv/tofu/browser_extension'),
      hasOpenBtn: !!document.getElementById('lcExtOpenBtn'),
      hasDownloadBtn: !!document.getElementById('lcExtDownloadBtn'),
    }};

    console.log(JSON.stringify(out));
""")

# `Api` present but never resolving: proves the floor is painted by the
# SYNCHRONOUS path, not by a fast-resolving stub.
_API_PENDING = """
    global.Api = {
      browser: { status: () => new Promise(() => {}) },
      desktop: { status: () => new Promise(() => {}) },
    };
"""

# `Api` genuinely absent — the early-return path that used to leave the modal
# stuck on "checking…" over an empty box forever.
_API_ABSENT = ""


def _run(api_stub: str, neuter=None) -> dict:
    body = LC_JS.read_text(encoding="utf-8")
    if neuter:
        rewritten = neuter(body)
        assert rewritten != body, (
            "NEUTER substitution did not apply — the anchor text is gone, so "
            "this run proves nothing about whether the guard bites")
        body = rewritten
    script = HARNESS.format(shipped=body, api_stub=api_stub,
                            html=MODAL_HTML.replace("`", "\\`"))
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_opening_paints_an_instruction_before_any_response():
    """THE REGRESSION. Read synchronously, before a promise can settle."""
    out = _run(_API_PENDING)["firstPaint"]
    assert out["browserSteps"] >= 1, (
        "opening the modal left the browser box empty until the status call "
        "returned — the user waits to be told what they already need to do")
    assert out["desktopSteps"] >= 1, (
        "opening the modal left the computer box empty until the status call "
        "returned")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_the_first_paint_status_is_a_state_not_a_spinner():
    """The label must name a state the user can act on, not "checking…"."""
    out = _run(_API_PENDING)["firstPaint"]
    for k in ("browserStatus", "desktopStatus"):
        assert out[k] and "checking" not in out[k].lower(), (
            f"{k} reads {out[k]!r} — a spinner tells the user to wait for a "
            f"verdict that is almost always 'not installed'")
        assert out[k] != "STALE", (
            f"{k} still shows the pre-open markup — the open path did not "
            f"repaint it")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_the_floor_works_with_no_backend_at_all():
    """A painted instruction whose button is dead is a worse lie than a wait."""
    assert _run(_API_PENDING)["floorBtnWorks"] == 1, (
        "the floor's download button did not fire — it is rendered before any "
        "backend response, so it must be wired by the same paint")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_the_modal_is_usable_even_when_the_api_client_is_missing():
    """``_lcRefresh`` returns EARLY when ``Api`` is undefined — no repaint.

    Before the floor, that path left "正在检查…" over an empty box with no
    error and no recovery: a permanently useless dialog.
    """
    out = _run(_API_ABSENT)["firstPaint"]
    assert out["browserSteps"] >= 1 and out["desktopSteps"] >= 1, (
        "with Api absent the modal renders no guidance at all — this is the "
        "path that used to hang on 'checking…' forever")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_a_failed_status_call_does_not_blank_the_guidance():
    """Both renderers used to run ``setup.innerHTML = ''`` on error."""
    out = _run(_API_PENDING)["afterError"]
    assert out["browserSteps"] >= 1, (
        "a failed browser status call wiped the install instruction. Losing "
        "the status says nothing about whether the user needs the extension.")
    assert out["desktopSteps"] >= 1, (
        "a failed desktop status call wiped the install instruction")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_a_connected_capability_still_clears_its_box():
    """Complement. Without this, "always render something" would pass by
    showing install steps to a user who already installed it — the
    menu-of-everything the merged surface exists to remove."""
    out = _run(_API_PENDING)["whenConnected"]
    assert out["browserSteps"] == 0 and out["browserText"] == "", (
        "a connected extension is still being told how to install itself")
    assert out["desktopSteps"] == 0 and out["desktopText"] == "", (
        "a connected agent is still being told how to install itself")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_detection_upgrades_the_floor_to_the_specific_instruction():
    """Complement: the floor must not PREVENT the better instruction.

    If it did, every user would be stuck on download-the-ZIP even when the
    folder is already on their machine.
    """
    out = _run(_API_PENDING)["upgraded"]
    assert out["hasPath"] is True and out["hasOpenBtn"] is True, (
        "detection did not replace the floor with the on-disk instruction")
    assert out["hasDownloadBtn"] is False, (
        "the floor's download button survived alongside the upgrade — two "
        "competing paths is exactly the menu this surface forbids")


# ══════════════════════════════════════════════════════════════════
#  NEUTER — prove each guard bites
# ══════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_removing_the_floor_paint_restores_the_empty_box():
    """Drop the synchronous paint → back to waiting on the network."""
    out = _run(_API_PENDING,
               lambda s: s.replace("  _lcPaintFloor();\n", ""))["firstPaint"]
    assert out["browserSteps"] == 0 and out["desktopSteps"] == 0, (
        "NEUTER did not remove the floor")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_blanking_on_error_is_caught():
    """Restore ``innerHTML = ''`` on the browser error path."""
    out = _run(_API_PENDING, lambda s: s.replace(
        "    _lcBrowserDownload();\n    return;\n  }",
        "    if (setup) setup.innerHTML = '';\n    return;\n  }", 1,
    ))["afterError"]
    assert out["browserSteps"] == 0, (
        "NEUTER did not restore the blank-on-error behaviour")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_a_dead_floor_button_is_caught():
    """Render the button but never wire it — the shape of a dead control."""
    out = _run(_API_PENDING, lambda s: s.replace(
        "      if (typeof downloadBrowserExtension === 'function') "
        "downloadBrowserExtension();",
        "      /* neutered */",
    ))
    assert out["floorBtnWorks"] == 0, "NEUTER did not disconnect the button"
