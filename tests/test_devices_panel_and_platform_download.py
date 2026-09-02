"""tests/test_devices_panel_and_platform_download.py — the Devices settings
page must actually be STYLED, must paint its two lanes as one generation, and
the desktop download must name the file for the user's platform.

WHAT THIS GUARDS
----------------
Three defects, all measured on the shipped tree before this suite existed.

1. **The panel was never styled at all.** ``devices.html`` +
   ``settings/devices.js`` between them use nine classes — ``stg-desc``,
   ``stg-row``, ``stg-table``, ``stg-dim``, bare ``stg-btn``,
   ``devices-agent-row``, ``devices-offline``, ``devices-online-dot``,
   ``devices-token-row`` — and every one of them had **zero** rule in
   ``styles.css`` AND zero in ``settings.css``. So the browser fell back to
   its defaults: an unstyled ``<table>``, a system ``<button>``, ``<p>`` with
   nothing but default margin. This is not "ugly design", it is *the absence
   of design*, and no amount of tweaking existing rules could have fixed it
   because there were no existing rules.

   ``.stg-*`` is shared settings chrome and lives in ``styles.css``;
   ``.devices-*`` is page-specific and lives in ``settings.css`` (the split
   ``tests/test_settings_panels_parity.py`` enforces).

2. **Two lanes, two generations, one payload.** ``_populateDevicesTab`` reset
   ``devicesAgentsList`` to a loading line but never touched
   ``devicesTokensList``. One request fed both lanes, so the screen could show
   "loading…" above a settled "no tokens yet" — two different answers to the
   same question at the same instant.

   Worse, ``Api.desktop.devices()`` is declared ``{onError:'null'}``, which
   api.js documents as *"rejections become null"*. A dead backend therefore
   **resolves** with ``null`` and lands in ``.then``, so the ``.catch`` branch
   that painted the ⚠ line was structurally unreachable and "loading…" could
   stay on screen forever. A lane must always reach a terminal state.

3. **The download link made the user guess.** ``_desktop_download_url``
   returned only ``…/releases/latest`` — a page carrying five assets (two
   DMGs, an .exe, a .tar.gz, SHA256SUMS). The user had to know which one their
   machine takes.

   The asset names must be derived from the ONE list in
   ``scripts/release_assets.py``; a sixth hand-typed copy in a route would
   drift the moment a platform is added (and the release gates would keep
   passing, because they only know the platforms still on their own list).

THE macOS TRAP
--------------
Apple Silicon and Intel Macs are **indistinguishable from the UA string** —
Chrome and Safari both report ``Intel Mac OS X`` on an M-series machine.
Architecture is only knowable from ``Sec-CH-UA-Arch``, a high-entropy client
hint that Chromium sends only after an ``Accept-CH`` opt-in and that Safari
never sends. So when the arch is unknown the server MUST offer both DMGs
rather than pick one: guessing wrong hands the user a download that cannot
open, which is strictly worse than asking them to choose.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
STYLES_CSS = ROOT / "static" / "styles.css"
SETTINGS_CSS = ROOT / "static" / "settings.css"
DEVICES_JS = ROOT / "frontend" / "src" / "features" / "settings" / "devices.ts"
DEVICES_HTML = ROOT / "static" / "settings_panels" / "devices.html"
ASSET_SCRIPT = ROOT / "scripts" / "release_assets.py"

from tests._runtime_sections import runtime_section_path

PROJECT_JS = Path(runtime_section_path('project.js'))
LC_JS = Path(runtime_section_path('local-control.js'))


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


def _strip_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


@functools.lru_cache(maxsize=1)
def _styles() -> str:
    return _strip_css_comments(STYLES_CSS.read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=1)
def _settings() -> str:
    return _strip_css_comments(SETTINGS_CSS.read_text(encoding="utf-8"))


def _has_rule(css: str, cls: str) -> bool:
    """True when ``css`` declares at least one rule whose selector names ``cls``.

    Matches the class as a whole token followed by something that can legally
    continue a selector — so ``.stg-btn`` is NOT satisfied by ``.stg-btn-danger``
    (the defect: every variant existed, the base did not).
    """
    return re.search(r"\.%s(?![\w-])[^{}]*\{" % re.escape(cls), css) is not None


# ══════════════════════════════════════════════════════════════════
#  1. The panel must have styles at all
# ══════════════════════════════════════════════════════════════════

# Shared settings chrome — styles.css owns it (test_settings_panels_parity's
# split rule: only page-specific prefixes move to settings.css).
_SHARED_CHROME = ("stg-desc", "stg-row", "stg-table", "stg-dim", "stg-btn")

# Page-specific — settings.css owns it.
_PAGE_SPECIFIC = ("devices-agent-row", "devices-offline",
                  "devices-online-dot", "devices-token-row")


@pytest.mark.parametrize("cls", _SHARED_CHROME)
def test_shared_settings_chrome_is_defined(cls):
    """THE REGRESSION. Each of these had ZERO rules in either stylesheet.

    ``stg-btn`` is the sharpest case: ``.stg-btn-danger`` / ``-add`` / ``-icon``
    / ``-balance`` all existed, so the name *looked* defined — but the Devices
    page uses the BARE class for its two primary buttons, and no bare rule was
    ever written. Both buttons rendered as raw system chrome.
    """
    assert _has_rule(_styles(), cls), (
        f".{cls} has no rule in styles.css. It is used by the Devices settings "
        f"panel, so without one the element falls back to browser defaults — "
        f"which is exactly how this page shipped unstyled."
    )


@pytest.mark.parametrize("cls", _PAGE_SPECIFIC)
def test_page_specific_device_classes_are_defined(cls):
    """The agent table and token rows had no rules either."""
    assert _has_rule(_settings(), cls), (
        f".{cls} has no rule in settings.css. Page-specific Devices styles "
        f"belong there (styles.css keeps only shared .stg-* chrome)."
    )


def test_device_classes_are_not_split_across_both_stylesheets():
    """Half-migration guard, mirroring test_settings_panels_parity.

    A prefix living in BOTH files is the split-brain state where nobody can
    tell which file owns the page.
    """
    leaked = [c for c in _PAGE_SPECIFIC if _has_rule(_styles(), c)]
    assert not leaked, (
        f"page-specific Devices classes also defined in styles.css: {leaked}. "
        f"They belong ONLY in settings.css."
    )


def test_every_class_the_devices_panel_uses_has_a_rule():
    """Catch-all: scrape the markup + renderer, demand a rule for each class.

    The parametrised tests above pin the classes that were measured broken.
    This one keeps a NEW unstyled class from being added later — the same
    mistake, one commit further on.
    """
    html = DEVICES_HTML.read_text(encoding="utf-8")
    js = DEVICES_JS.read_text(encoding="utf-8")
    used: set[str] = set()
    for blob in (html, js):
        for group in re.findall(r'class="([^"]+)"', blob):
            for token in group.split():
                # The renderers build class lists by string concatenation, so a
                # scraped group can end mid-expression (`devices-agent-row'` +
                # …). Keep only whole, syntactically-valid class tokens — a
                # fragment would be an unfixable phantom, and demanding a rule
                # for one would make this guard impossible to satisfy.
                if not re.fullmatch(r"[A-Za-z_-][\w-]*", token):
                    continue
                if token.startswith(("stg-", "devices-")):
                    used.add(token)
    for group in re.findall(r"element\([^,]+,\s*'([^']*)'", js):
        used.update(token for token in group.split()
                    if token.startswith(("stg-", "devices-")))
    assert used, "scraper found no classes — the regex stopped matching"
    both = _styles() + "\n" + _settings()
    missing = sorted(c for c in used if not _has_rule(both, c))
    assert not missing, (
        f"Devices panel uses classes with NO rule anywhere: {missing}. "
        f"An unstyled class renders as a browser default, which is how this "
        f"panel shipped looking broken."
    )


def test_the_remote_devices_picker_is_styled_too():
    """The SAME feature paints a second surface — the project folder browser's
    "remote devices" group (``#remoteDevicesSection`` in index.html, rendered
    by ``project.js::_renderRemoteDevicesSection``). Its classes were unstyled
    for the same reason, so fixing only the settings tab would leave the other
    half of the feature raw.
    """
    js = PROJECT_JS.read_text(encoding="utf-8")
    used = {t for g in re.findall(r'class="([^"]+)"', js) for t in g.split()
            if t.startswith("remote-")
            and re.fullmatch(r"[A-Za-z_-][\w-]*", t)}
    assert used, "no remote-* classes found in project.js — regex stale"
    both = _styles() + "\n" + _settings()
    missing = sorted(c for c in used if not _has_rule(both, c))
    assert not missing, (
        f"remote-device picker classes with no rule: {missing}"
    )


# ══════════════════════════════════════════════════════════════════
#  2. Two lanes, ONE generation — and always a terminal state
# ══════════════════════════════════════════════════════════════════

DEVICES_DOM = """
<div id="settingsTab_devices">
  <div id="devicesAgentsList"></div>
  <div id="devicesTokensList"></div>
</div>
"""

_HARNESS = textwrap.dedent("""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM(`<!DOCTYPE html><body>{html}</body>`);
    global.document = dom.window.document;
    global.window = dom.window;
    global.navigator = dom.window.navigator;
    global.HTMLElement = dom.window.HTMLElement;
    global.DocumentFragment = dom.window.DocumentFragment;
    global.AbortController = dom.window.AbortController;
    global.AbortSignal = dom.window.AbortSignal;
    global.t = (k) => k;
    global.escapeHtml = (s) => String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    global.showToast = () => {{}};
    global.Api = {{ desktop: {{
      devices: () => {payload},
      revokeToken: () => Promise.resolve(null),
    }} }};
    window.Api = global.Api;
    window.t = global.t;
    window.showToast = global.showToast;

    {shipped}

    const agents = () => document.getElementById('devicesAgentsList');
    const tokens = () => document.getElementById('devicesTokensList');
    const snap = () => ({{
      agentsHtml: agents().innerHTML,
      tokensHtml: tokens().innerHTML,
      agentsText: agents().textContent.trim(),
      tokensText: tokens().textContent.trim(),
    }});

    const out = {{}};
    DevicesFeature.populateDevicesTab();
    // The FIRST frame: synchronous, before any promise can settle. This is
    // literally what the user sees at t=0.
    out.firstPaint = snap();

    setTimeout(() => {{
      out.settled = snap();
      console.log(JSON.stringify(out));
    }}, 40);
""")


def _run_devices(payload_js: str) -> dict:
    if not _has_jsdom():
        pytest.skip("jsdom not available")
    src = _HARNESS.format(
        html=DEVICES_DOM.replace("\n", ""),
        payload=payload_js,
        shipped=_devices_bundle(),
    )
    proc = subprocess.run([_node(), "-e", src], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        f"harness exited {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_both_lanes_leave_the_previous_generation_together():
    """THE REGRESSION (the first screenshot).

    Only the agents lane was reset, so a repaint showed "loading…" over a
    tokens lane still displaying the PREVIOUS answer. One payload must never
    produce two visible generations.
    """
    out = _run_devices("Promise.resolve({agents: [], tokens: []})")
    first = out["firstPaint"]
    # Neither lane may still be showing stale/settled content while the other
    # announces it is loading.
    assert first["tokensHtml"].strip(), (
        "the tokens lane is EMPTY on first paint while the agents lane shows a "
        "loading line — the two lanes are painting different generations of "
        "the same single request."
    )
    assert ("stg-loading" in first["agentsHtml"]) == \
           ("stg-loading" in first["tokensHtml"]), (
        f"only one lane entered the loading state: agents="
        f"{first['agentsHtml']!r} tokens={first['tokensHtml']!r}. Both lanes "
        f"are fed by ONE request, so they must change generation together."
    )


def test_a_null_payload_reaches_a_visible_terminal_state():
    """THE UNREACHABLE CATCH.

    ``Api.desktop.devices()`` is declared ``{onError:'null'}``, so a dead
    backend RESOLVES with ``null`` — it never rejects. The ``.catch`` branch
    that was supposed to paint the ⚠ line therefore could not run, and both
    lanes were left showing "loading…" forever.
    """
    out = _run_devices("Promise.resolve(null)")
    settled = out["settled"]
    for lane in ("agentsHtml", "tokensHtml"):
        assert "stg-loading" not in settled[lane], (
            f"{lane} is STILL showing the loading state after a null payload. "
            f"onError:'null' resolves rather than rejects, so a failure must "
            f"be handled on the success path — otherwise 'loading…' is "
            f"permanent."
        )
    # …and it must not LIE. `null` means "we could not ask", which is a
    # different statement from "you own no devices" — and the second one is
    # actively misleading: a user whose agent IS running would conclude it had
    # died. The old code passed `null && null.agents || []` straight into the
    # empty-state renderer, so an unreachable backend rendered as a confident
    # "No devices yet."
    for lane in ("agentsHtml", "tokensHtml"):
        assert "devices-load-failed" in settled[lane], (
            f"{lane} renders the EMPTY state after a failed load: "
            f"{settled[lane]!r}. 'We could not reach the server' and 'you have "
            f"no devices' are different facts; showing the second one when the "
            f"first is true tells the user their agent died."
        )


def test_a_rejected_promise_also_terminates_both_lanes():
    """Complement: the throwing path must clear BOTH lanes, not just agents.

    The old ``.catch`` set ``tokensEl.innerHTML = ''`` — blanking the lane
    rather than reporting the failure, so the user was told "no tokens" by an
    empty box when the truth was "we could not ask".
    """
    out = _run_devices("Promise.reject(new Error('boom'))")
    settled = out["settled"]
    for lane in ("agentsHtml", "tokensHtml"):
        assert "stg-loading" not in settled[lane], (
            f"{lane} still shows loading after a rejection")
    assert settled["tokensText"].strip(), (
        "the tokens lane was blanked on error instead of reporting it")


def test_a_successful_payload_still_renders_both_lanes():
    """THE COMPLEMENT that stops 'always show an error' from passing."""
    out = _run_devices(
        "Promise.resolve({agents: [{agent_id:'abcdef1234', name:'my-mac',"
        " platform:'darwin', online:true, share_roots:[{name:'work'}]}],"
        " tokens: [{id:'k1', name:'laptop', created_at:1700000000}]})")
    settled = out["settled"]
    assert "my-mac" in settled["agentsText"], settled
    assert "laptop" in settled["tokensText"], settled
    for lane in ("agentsHtml", "tokensHtml"):
        assert "stg-loading" not in settled[lane]


# ══════════════════════════════════════════════════════════════════
#  3. The download must name the file for THIS machine
# ══════════════════════════════════════════════════════════════════

def _asset_mod():
    spec = importlib.util.spec_from_file_location(
        "_tofu_release_assets_dl", ASSET_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _platform_mod():
    from routes.api_v1 import desktop as mod
    return mod


def test_the_route_derives_asset_names_from_the_shared_list():
    """No sixth copy of the filenames.

    ``scripts/release_assets.py`` is the single source of truth the two release
    gates already share. A route that hand-typed ``Tofu-Setup-*-win64.exe``
    would keep working today and silently miss the next platform added to that
    list — and, unlike the gates, nothing would ever go red.
    """
    src = (ROOT / "routes" / "api_v1" / "desktop.py").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    mod = _asset_mod()
    leaked = [pat for *_rest, pat in mod.REQUIRED_PLATFORM_ASSETS if pat in code]
    assert not leaked, (
        f"asset globs hardcoded in the route: {leaked}. Import them from "
        f"scripts/release_assets.py instead — that list is what the release "
        f"gates already agree on."
    )
    assert "release_assets" in code, (
        "the route must consult scripts/release_assets.py for the asset names")


def test_the_shared_list_carries_a_machine_readable_platform_key():
    """A human label like 'macOS arm64 DMG' cannot be matched against a UA.

    Parsing the label (or the glob) with a regex would put a second, implicit
    copy of the platform mapping in the route. The list itself must say which
    OS and architecture each asset is for.
    """
    mod = _asset_mod()
    seen = set()
    for entry in mod.PLATFORM_ASSETS:
        # The arity is pinned because consumers unpack these rows POSITIONALLY
        # (routes/api_v1/desktop.py, scripts/release_assets.py and the helpers
        # in this file), so widening a row silently breaks them — the route's
        # table load is wrapped in `except Exception` and degrades to the
        # releases page, i.e. the symptom is "direct download links quietly
        # disappeared", not a traceback. min_bytes joined the row when the
        # release gate learned to reject a correctly-named but hollow
        # installer; see tests/test_release_asset_size_floor.py.
        assert len(entry) == 5, (
            f"expected (os, arch, label, glob, min_bytes) entries, got {entry!r}")
        os_key, arch, _label, _glob, _min_bytes = entry
        assert os_key in {"windows", "macos", "linux"}, os_key
        seen.add((os_key, arch))
    assert ("macos", "arm64") in seen and ("macos", "x86_64") in seen, seen
    assert ("windows", "x86_64") in seen and ("linux", "x86_64") in seen, seen
    # The release gates consume the 2-tuple shape. It must be DERIVED from the
    # list above, never maintained beside it — two hand-kept lists is the exact
    # drift scripts/release_assets.py exists to prevent. Since A2b
    # (2026-08-02) the derivation spans BOTH component tables: a release
    # missing an agent asset is exactly as incomplete as one missing a
    # platform (build-desktop.yml's agent legs landed atomically with the
    # join — legs without the join would publish hollow, the join without
    # the legs would fail every publish).
    assert mod.REQUIRED_PLATFORM_ASSETS == tuple(
        (label, glob) for _o, _a, label, glob, _min in (
            mod.PLATFORM_ASSETS + mod.AGENT_PLATFORM_ASSETS)) + (
                ('prebuilt frontend graph', 'frontend-dist-*.tar.gz'),
                ('prebuilt frontend SHA-256', 'frontend-dist-*.tar.gz.sha256'),
            ), (
        "REQUIRED_PLATFORM_ASSETS is not derived from "
        "PLATFORM_ASSETS + AGENT_PLATFORM_ASSETS")


# A realistic published-asset list, DERIVED from the shared globs rather than
# hand-typed — a hand-typed copy here would be the very drift the single-source
# rule exists to prevent, and would keep passing after a platform was added.
def _published():
    """The release's assets in the shape the API returns them.

    Each record carries its OWN pinned-tag URL next to the name. Deriving both
    from one version string is deliberate: that IS the one-snapshot property
    the production parser has to preserve, so a fixture allowing them to differ
    would not exercise the real contract.
    """
    mod = _asset_mod()
    out = []
    for _o, _a, _l, pat, _min in mod.PLATFORM_ASSETS:
        name = pat.replace('*', _FIXTURE_VER)
        out.append({
            'name': name,
            'url': ('https://github.com/rangehow/ToFu/releases/download/'
                    f'v{_FIXTURE_VER}/{name}'),
        })
    return out


_FIXTURE_VER = "0.15.2"


@pytest.mark.parametrize("ua,expect_os", [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120", "windows"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605", "macos"),
    ("Mozilla/5.0 (X11; Linux x86_64) Chrome/120", "linux"),
])
def test_the_users_platform_is_detected_from_the_request(ua, expect_os):
    """The user must not be handed a five-asset page to choose from."""
    mod = _platform_mod()
    picks = mod._match_platform_assets(ua, arch_hint="", published=_published())
    assert picks, f"no asset matched for {ua!r}"
    assert all(p["os"] == expect_os for p in picks), picks


def test_an_unknown_arch_on_macos_offers_BOTH_dmgs():
    """THE TRAP. Apple Silicon reports ``Intel Mac OS X`` in its UA.

    Both Chrome and Safari do this on an M-series machine, so the UA can never
    tell the two DMGs apart. Picking one would hand roughly half of Mac users
    a download that will not open — strictly worse than showing two clearly
    labelled choices.
    """
    mod = _platform_mod()
    picks = mod._match_platform_assets(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605",
        arch_hint="", published=_published())
    arches = sorted(p["arch"] for p in picks)
    assert arches == ["arm64", "x86_64"], (
        f"macOS with an unknown architecture must offer BOTH DMGs, got "
        f"{arches}. The UA string says 'Intel' on Apple Silicon too, so a "
        f"single pick is a coin flip that breaks the download."
    )


@pytest.mark.parametrize("hint,expect", [
    ('"arm"', "arm64"),
    ('"x86"', "x86_64"),
])
def test_a_client_hint_narrows_macos_to_one_dmg(hint, expect):
    """``Sec-CH-UA-Arch`` is the ONLY honest source of the architecture.

    When Chromium does supply it, the ambiguity is gone and the user should
    get exactly one file — otherwise the hint buys nothing.
    """
    mod = _platform_mod()
    picks = mod._match_platform_assets(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120",
        arch_hint=hint, published=_published())
    assert [p["arch"] for p in picks] == [expect], picks


def test_an_unrecognised_platform_falls_back_to_the_releases_page():
    """A BSD / mobile / bot UA must not be guessed at.

    Returning no direct link is honest; the releases page still works. Guessing
    would send a phone user a Windows installer.
    """
    mod = _platform_mod()
    assert mod._match_platform_assets("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)",
                                      arch_hint="", published=_published()) == []
    assert mod._match_platform_assets("", arch_hint="", published=_published()) == []


def test_direct_urls_point_at_a_real_asset_not_the_listing_page():
    """The whole point: a URL that downloads a file, not one that shows five.

    ``filename`` must be the RESOLVED asset name, never the glob it was matched
    with. This assertion exists because a NEUTER that fed the glob straight
    into the URL slipped past an earlier version of this test: the glob also
    ends in ``.exe``, so an extension check alone cannot tell
    ``Tofu-Setup-0.15.2-win64.exe`` from ``Tofu-Setup-<star>-win64.exe`` — and
    the second one is a guaranteed 404, because GitHub's
    ``/releases/latest/download/<name>`` resolves the RELEASE but not the
    FILENAME.
    """
    mod = _platform_mod()
    picks = mod._match_platform_assets(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120", arch_hint="", published=_published())
    assert picks, (
        'no asset matched a Windows visitor against an injected, COMPLETE '
        'release asset list — that is a broken matcher, not an unreachable '
        'network. (This was a pytest.skip while the test still probed GitHub; '
        'leaving it would let the whole assertion vacuum out on any host '
        'without egress.)')
    assert len(picks) == 1, picks
    url = picks[0]["url"]
    name = picks[0]["filename"]
    assert "/releases/" in url and url.endswith(".exe"), url
    assert not url.endswith("/releases/latest"), (
        "this is the listing page again — the user still has to choose")
    for meta in "*?[":
        assert meta not in name, (
            f"filename {name!r} still carries the glob metacharacter {meta!r} — "
            f"it is the PATTERN, not a resolved asset name, so the download URL "
            f"is a guaranteed 404.")
        assert meta not in url, f"url {url!r} carries glob metacharacter {meta!r}"


# ══════════════════════════════════════════════════════════════════
#  4. The frontend must actually CONSUME the platform links
# ══════════════════════════════════════════════════════════════════
#
# A backend that resolves the right installer is worth nothing if no rendered
# element links to it. This is the "my Python guards were all green while the
# fix was unreachable in the product" failure mode: the only way to rule it out
# is a guard that drives the SHIPPED renderer end-to-end.

@functools.lru_cache(maxsize=1)
def _devices_bundle() -> str:
    esbuild = ROOT / 'node_modules' / '.bin' / 'esbuild'
    if not esbuild.is_file():
        pytest.skip('esbuild not installed')
    proc = subprocess.run(
        [str(esbuild), str(DEVICES_JS), '--bundle', '--format=iife',
         '--global-name=DevicesFeature', '--platform=browser'],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout

_LC_HARNESS = textwrap.dedent("""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM('<!DOCTYPE html><body>' +
      '<div id="localControlModal">' +
      '<span class="lc-cap-status" id="lcDesktopStatus">' +
      '<span class="browser-status-dot"></span>' +
      '<span class="lc-status-text"></span></span>' +
      '<button class="lc-switch" id="lcDesktopSwitch"></button>' +
      '<p class="lc-cap-about" id="lcDesktopAbout"></p>' +
      '<div class="lc-cap-setup" id="lcDesktopSetup"></div>' +
      '<p class="lc-perm-note" id="lcPermNote"></p>' +
      '</div><div id="localControlToggle"></div>' +
      '<span id="localControlBadge"></span></body>');
    global.document = dom.window.document;
    global.window = dom.window;
    global.navigator = dom.window.navigator;
    global.t = (k) => k;
    global.escapeHtml = (s) => String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;');
    global.desktopEnabled = false;
    global.browserEnabled = false;
    global.showToast = () => {{}};
    window.t = global.t;
    window.escapeHtml = global.escapeHtml;
    window.showToast = global.showToast;

    {shipped}

    _lcRenderDesktop({payload});
    const box = document.getElementById('lcDesktopSetup');
    const links = Array.from(box.querySelectorAll('a')).map(a => ({{
      href: a.getAttribute('href'),
      text: a.textContent,
      cls: a.className,
    }}));
    console.log(JSON.stringify({{ html: box.innerHTML, links: links }}));
""")


def _run_lc_desktop(payload_js: str) -> dict:
    if not _has_jsdom():
        pytest.skip("jsdom not available")
    src = _LC_HARNESS.format(
        shipped=LC_JS.read_text(encoding="utf-8"), payload=payload_js)
    proc = subprocess.run([_node(), "-e", src], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        f"harness exited {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_panel_renders_a_direct_link_per_matched_installer():
    """THE REACHABILITY GUARD.

    Without this, every backend test above can pass while the settings panel
    still shows only the five-asset releases page — the fix would be correct
    and invisible.
    """
    out = _run_lc_desktop(
        "{setup_state:'remote', download_url:'https://example.invalid/releases/latest',"
        " server_url:'https://tofu.invalid',"
        " downloads:[{os:'windows',arch:'x86_64',label:'Windows installer',"
        " filename:'Tofu-Setup-0.15.2-win64.exe',"
        " url:'https://example.invalid/releases/latest/download/Tofu-Setup-0.15.2-win64.exe'}]}")
    direct = [l for l in out["links"] if "lc-dl-direct" in (l["cls"] or "")]
    assert direct, (
        f"no direct-download link rendered; the panel still only offers the "
        f"releases page. links={out['links']!r}")
    assert direct[0]["href"].endswith("Tofu-Setup-0.15.2-win64.exe"), direct


def test_both_macos_dmgs_are_rendered_with_a_reason():
    """The ambiguous case must be USABLE, not just correct.

    Two unexplained links look like a bug. The panel has to say why there are
    two and how to tell which is yours, or the user picks at random — which is
    the same coin flip the backend deliberately refused to make for them.
    """
    out = _run_lc_desktop(
        "{setup_state:'remote', download_url:'https://example.invalid/releases/latest',"
        " server_url:'https://tofu.invalid',"
        " downloads:[{os:'macos',arch:'arm64',label:'macOS arm64 DMG',"
        " filename:'Tofu-0.15.2-macos-arm64.dmg',"
        " url:'https://example.invalid/d/Tofu-0.15.2-macos-arm64.dmg'},"
        " {os:'macos',arch:'x86_64',label:'macOS x86_64 DMG',"
        " filename:'Tofu-0.15.2-macos-x86_64.dmg',"
        " url:'https://example.invalid/d/Tofu-0.15.2-macos-x86_64.dmg'}]}")
    direct = [l for l in out["links"] if "lc-dl-direct" in (l["cls"] or "")]
    assert len(direct) == 2, f"expected both DMGs, got {direct!r}"
    assert any("arm64" in (l["text"] or "") for l in direct), direct
    assert any("x86_64" in (l["text"] or "") for l in direct), direct
    # Assert on the TEXT THE USER READS, not the i18n key. `_lcT` falls back to
    # the literal string when `t()` cannot resolve the key, so keying on
    # 'local.desktopArchAmbiguous' would pass ONLY in the untranslated case and
    # go red the moment the string was wired up properly — a guard that fails
    # when the product gets better is worse than no guard.
    assert "About This Mac" in out["html"] or "关于本机" in out["html"], (
        f"two links rendered with no explanation of which one to take — the "
        f"user is left guessing, which is what offering both was meant to "
        f"avoid. html={out['html']!r}")


def test_a_mac_visitor_is_never_offered_the_windows_agent_exe():
    """THE REGRESSION GUARD for the Mac-gets-an-.exe defect.

    The personalized one-file installer is a Windows NSIS .exe by
    construction, so `agent_installer_ready` means "the WINDOWS installer
    is ready" — and the renderer used to take that as licence to offer the
    .exe to EVERY visitor. A Mac client's primary button therefore
    downloaded TofuAgent-Setup-*-win64.exe, a package it cannot run. A
    macOS visitor must get the mirrored agent DMGs from `agent_downloads`
    instead, with the .exe button nowhere on the surface.
    """
    out = _run_lc_desktop(
        "{setup_state:'remote', visitor_os:'macos', agent_installer_ready:true,"
        " download_url:'https://example.invalid/releases/latest',"
        " server_url:'https://tofu.invalid',"
        " agent_downloads:[{os:'macos',arch:'arm64',"
        " label:'macOS agent arm64 DMG',"
        " filename:'TofuAgent-0.16.0-macos-arm64.dmg',"
        " url:'https://example.invalid/d/TofuAgent-0.16.0-macos-arm64.dmg'},"
        " {os:'macos',arch:'x86_64',label:'macOS agent x86_64 DMG',"
        " filename:'TofuAgent-0.16.0-macos-x86_64.dmg',"
        " url:'https://example.invalid/d/TofuAgent-0.16.0-macos-x86_64.dmg'}],"
        " downloads:[]}")
    hrefs = [l["href"] for l in out["links"]]
    assert not any('agent-installer' in (h or '') for h in hrefs), (
        f"a macOS visitor was still offered the Windows-only personalized "
        f".exe: {hrefs!r}")
    assert "lcAgentInstallerBtn" not in out["html"], (
        "the .exe button must not even render for a macOS visitor")
    direct = [l for l in out["links"]
              if "lc-dl-direct" in (l["cls"] or "")
              and "TofuAgent" in (l["href"] or "")]
    assert len(direct) == 2, (
        f"expected BOTH mirrored agent DMGs as the primary offer (arch "
        f"unknowable ⇒ offer both, never guess): {out['links']!r}")
    assert any("arm64" in (l["text"] or "") for l in direct), direct
    assert any("x86_64" in (l["text"] or "") for l in direct), direct


def test_a_mac_visitor_with_no_agent_asset_gets_an_honest_wait_not_an_exe():
    """Mirror still fetching (or no GitHub route): the macOS visitor waits
    for the DMG with the releases page as escape hatch — the .exe is never
    the stand-in."""
    out = _run_lc_desktop(
        "{setup_state:'remote', visitor_os:'macos', agent_installer_ready:true,"
        " download_url:'https://example.invalid/releases/latest',"
        " server_url:'https://tofu.invalid', agent_downloads:[], downloads:[]}")
    hrefs = [l["href"] for l in out["links"]]
    assert not any('agent-installer' in (h or '') for h in hrefs), hrefs
    assert "lcAgentInstallerBtn" not in out["html"]
    assert 'https://example.invalid/releases/latest' in hrefs, (
        f"the releases-page escape hatch vanished: {hrefs!r}")


def test_a_windows_visitor_keeps_the_personalized_agent_installer():
    """The platform gate must not hide the zero-config flow from its one
    supported platform: Windows + ready ⇒ the personalized .exe button."""
    out = _run_lc_desktop(
        "{setup_state:'remote', visitor_os:'windows', agent_installer_ready:true,"
        " download_url:'https://example.invalid/releases/latest',"
        " server_url:'https://tofu.invalid',"
        " agent_downloads:[{os:'windows',arch:'x86_64',"
        " label:'Windows agent installer',"
        " filename:'TofuAgent-Setup-0.16.0-win64.exe',"
        " url:'https://example.invalid/d/TofuAgent-Setup-0.16.0-win64.exe'}],"
        " downloads:[]}")
    assert "lcAgentInstallerBtn" in out["html"], (
        f"the personalized installer button vanished for the one platform "
        f"it exists for: {out['html']!r}")
    assert any('/api/v1/desktop/agent-installer' in (l["href"] or "")
               for l in out["links"]), out["links"]


# ══════════════════════════════════════════════════════════════════
#  The browser-pushed zero-config attach (macOS/Linux parity)
# ══════════════════════════════════════════════════════════════════
#
# The mirrored DMG/tarball carries no credential — the signed-in page
# pushes it to the freshly installed agent's loopback broker instead.
# These drive the SHIPPED watch/push flow end to end against a fake
# broker: discovery → push → the broker's `transport` answer picks the
# continuation (direct ⇒ the watch's job is done; browser ⇒ keep
# relaying for the SSO case).

_LC_PUSH_PRELUDE = textwrap.dedent("""
    const { JSDOM } = require('jsdom');
    const dom = new JSDOM('<!DOCTYPE html><body></body>');
    global.document = dom.window.document;
    global.window = dom.window;
    global.navigator = dom.window.navigator;
    global.t = (k) => k;
    global.escapeHtml = (s) => String(s == null ? '' : s);
    global.showToast = () => {};
    window.t = global.t;
    window.escapeHtml = global.escapeHtml;
    window.showToast = global.showToast;

    const posted = [];
    const calls = { status: 0, take: 0 };
    global.fetch = function (url, opts) {
      if (url.endsWith('/v1/status')) {
        calls.status++;
        return Promise.resolve({ ok: true, status: 200,
          json: () => Promise.resolve(__STATUS__) });
      }
      if (url.endsWith('/v1/attach')) {
        posted.push(JSON.parse(opts.body));
        return Promise.resolve({ ok: __OK__, status: __CODE__,
          json: () => Promise.resolve(__BODY__) });
      }
      if (url.endsWith('/v1/take')) {
        calls.take++;
        return new Promise(() => {});   // a live long-poll, never answered
      }
      return Promise.reject(new Error('unexpected fetch ' + url));
    };
    global.Api = { desktop: {
      mintAttachBundle: (base) => Promise.resolve({ v: 1,
        token: 'tofu_live_T', candidates: ['http://10.9.8.7:15000'],
        fallback_candidates: ['https://tofu.example'] }),
    } };

""")

_LC_PUSH_DRIVE = textwrap.dedent("""

    _lcAgentRelay.watchUntil = Date.now() + 60000;
    _lcRelayWatchBeat();
    setTimeout(function () {
      console.log(JSON.stringify({
        posted: posted, state: _lcAgentRelay.state,
        running: _lcAgentRelay.running, takeCalls: calls.take,
        watchUntil: _lcAgentRelay.watchUntil,
        attachDone: _lcAgentRelay.attachDone }));
      process.exit(0);
    }, 200);
""")


def _run_lc_push(status_json: str, ok: str, code: str, body_json: str) -> dict:
    if not _has_jsdom():
        pytest.skip("jsdom not available")
    src = (_LC_PUSH_PRELUDE
           .replace('__STATUS__', status_json)
           .replace('__OK__', ok)
           .replace('__CODE__', code)
           .replace('__BODY__', body_json)
           + LC_JS.read_text(encoding="utf-8")
           + _LC_PUSH_DRIVE)
    proc = subprocess.run([_node(), "-e", src], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        f"harness exited {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_an_unattached_agent_gets_its_bundle_pushed_then_watch_stops():
    """THE PARITY FLOW: discovery finds a bootstrap broker, the page pushes
    the minted bundle, and a 'direct' answer ends the watch — the agent
    polls its own LAN route, and a lingering page must not make it prefer
    the browser transport."""
    out = _run_lc_push(
        "{ kind: 'tofu-agent-browser-relay', attached: false }",
        "true", "200",
        "{ accepted: true, reason: 'attached', transport: 'direct',"
        "  url: 'http://10.9.8.7:15000' }")
    assert len(out["posted"]) == 1, f"no bundle was pushed: {out!r}"
    assert out["posted"][0]["token"] == "tofu_live_T"
    assert out["state"] == "attached"
    assert out["attachDone"] is True
    assert out["watchUntil"] == 0, 'a direct attach ends the broker watch'
    assert out["running"] is False and out["takeCalls"] == 0, (
        'a direct attach must NOT enter the relay loop')


def test_an_sso_attach_keeps_the_page_relaying():
    """transport 'browser' (nothing answered a cookieless probe) ⇒ the
    page enters the relay loop — the agent's only route to Tofu rides
    this tab."""
    out = _run_lc_push(
        "{ kind: 'tofu-agent-browser-relay', attached: false }",
        "true", "200",
        "{ accepted: true, reason: 'attached_optimistic',"
        "  transport: 'browser', url: 'https://tofu.example' }")
    assert len(out["posted"]) == 1
    assert out["attachDone"] is True
    assert out["running"] is True and out["takeCalls"] >= 1, (
        f"the relay loop did not start after an SSO attach: {out!r}")


def test_an_already_attached_agent_enters_the_relay_without_pushing():
    """A 409 means another page (or a previous install) got there first —
    the relay flow takes over and the push is never retried."""
    out = _run_lc_push(
        "{ kind: 'tofu-agent-browser-relay', attached: false }",
        "false", "409", "{ accepted: false, reason: 'already_attached' }")
    assert len(out["posted"]) == 1
    assert out["attachDone"] is True
    assert out["running"] is True and out["takeCalls"] >= 1


def test_a_legacy_agent_without_the_attach_field_goes_straight_to_relay():
    """Agents built before the field existed report NO `attached` flag —
    they must read as attached (relay-only), never pushed a bundle they
    cannot understand."""
    out = _run_lc_push(
        "{ kind: 'tofu-agent-browser-relay' }",
        "true", "200", "{ accepted: true, transport: 'direct' }")
    assert out["posted"] == [], 'a legacy broker was pushed an attach'
    assert out["running"] is True and out["takeCalls"] >= 1


def test_the_releases_page_link_survives_as_a_fallback():
    """An unrecognised platform (empty `downloads`) must still get somewhere.

    This is the path for BSD, mobile, a release missing an asset, or an
    unreachable GitHub API — the direct links are an upgrade, never a
    replacement for the page that always works.
    """
    out = _run_lc_desktop(
        "{setup_state:'remote', download_url:'https://example.invalid/releases/latest',"
        " server_url:'https://tofu.invalid', downloads:[]}")
    hrefs = [l["href"] for l in out["links"]]
    assert "https://example.invalid/releases/latest" in hrefs, (
        f"the releases-page fallback disappeared; a visitor whose platform we "
        f"cannot identify now has NO way to download at all. links={hrefs!r}")


def test_the_local_source_branch_gets_the_links_too():
    """Both install branches share one authoring — a link the remote case has
    and the local-source case lacks is a drift that only one user population
    ever sees."""
    out = _run_lc_desktop(
        "{setup_state:'local_source', download_url:'https://example.invalid/releases/latest',"
        " downloads:[{os:'linux',arch:'x86_64',label:'Linux archive',"
        " filename:'Tofu-0.15.2-linux-x86_64.tar.gz',"
        " url:'https://example.invalid/d/Tofu-0.15.2-linux-x86_64.tar.gz'}]}")
    direct = [l for l in out["links"] if "lc-dl-direct" in (l["cls"] or "")]
    assert direct, f"local_source branch renders no direct link: {out['links']!r}"


def test_the_client_resolves_its_own_architecture():
    """macOS cannot be narrowed any other way.

    `Sec-CH-UA-Arch` only arrives AFTER a server has answered once with an
    `Accept-CH` opt-in, so the first paint — the one carrying the download
    button — would be arch-blind. The client must ask
    `navigator.userAgentData.getHighEntropyValues` itself and pass the answer.
    """
    src = LC_JS.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith(("*", "/*", "//")))
    assert "getHighEntropyValues" in code, (
        "local-control never asks the browser for its architecture, so the "
        "macOS two-DMG ambiguity can never be resolved for anyone")
    assert "Api.desktop.status(" in code, (
        "the resolved architecture is never threaded into the status probe")
    api = Path(runtime_section_path('api.js')).read_text(encoding='utf-8')
    assert re.search(r"status:\s*\(arch", api), (
        "Api.desktop.status does not accept the client-resolved architecture, "
        "so the value local-control computes cannot reach the server")


def test_every_new_download_string_is_translated():
    """A raw i18n key in the download row is a visible defect."""
    src = LC_JS.read_text(encoding="utf-8")
    keys = set(re.findall(r"_lcT\('(local\.desktopDownload[A-Za-z]*|"
                          r"local\.desktopArchAmbiguous)'", src))
    assert keys, "the download row uses no translated strings at all"
    catalogs = [json.loads((ROOT / 'frontend' / 'src' / 'i18n' / 'locales'
                            / f'{language}.json').read_text(encoding='utf-8'))
                for language in ('zh', 'en')]
    for key in sorted(keys):
        assert all(key in catalog for catalog in catalogs), (
            f"{key!r} is not defined in every locale chunk")


# ══════════════════════════════════════════════════════════════════
#  5. The URL and the FILENAME must come from ONE snapshot
# ══════════════════════════════════════════════════════════════════
#
# THE DEFECT. The URL was assembled as
# ``/releases/latest/download/<cached filename>`` — two halves with DIFFERENT
# lifetimes glued together:
#
#   * the filename is cached here for 900 s;
#   * ``latest`` is resolved by GitHub at click time.
#
# They agree at the instant the cache is filled and diverge the moment a new
# release publishes: ``latest`` moves to the new tag, whose asset set does not
# contain the old filename. Measured against the real repo:
#
#   latest/download/Tofu-Setup-0.14.2-win64.exe   -> 200  (in current latest)
#   latest/download/Tofu-Setup-0.15.2-win64.exe   -> 404  (not in latest)
#   latest/download/Tofu-Setup-0.13.0-win64.exe   -> 404  (older name)
#   releases/download/v0.14.2/Tofu-Setup-...exe   -> 200  (tag pinned)
#
# The window is open RIGHT NOW: VERSION says 0.15.2 while the newest release is
# v0.14.2, so the next publish must pass through it — handing 404s to everyone
# who clicks in the following 15 minutes, i.e. exactly when a fresh release
# draws the most downloads.
#
# Shortening the TTL cannot fix this (any window > 0 has the same failure), so
# the invariant is structural: THE URL MUST BE PINNED TO THE SAME RELEASE THE
# FILENAME CAME FROM. GitHub already hands us that URL — every asset in the
# payload carries ``browser_download_url`` of the form
# ``/releases/download/<tag>/<name>`` — so the fix is to stop discarding it.

_VER_RE = re.compile(r"\d+\.\d+\.\d+")


def _versions_in(text: str) -> list[str]:
    return _VER_RE.findall(text or "")


@pytest.mark.parametrize("ua", [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605",
    "Mozilla/5.0 (X11; Linux x86_64) Chrome/120",
])
def test_the_download_url_is_pinned_to_the_filenames_own_release(ua):
    """THE REGRESSION. A ``/releases/latest/download/`` URL is a time bomb.

    ``latest`` is resolved when the user CLICKS, but the filename was resolved
    when the server last probed. Pinning the URL to the release the filename
    actually belongs to is the only construction where the two cannot drift.
    """
    mod = _platform_mod()
    picks = mod._match_platform_assets(ua, arch_hint="",
                                      published=_published())
    assert picks, f"no asset matched for {ua!r}"
    for p in picks:
        url = p["url"]
        assert "/releases/latest/download/" not in url, (
            f"url {url!r} resolves 'latest' at CLICK time while its filename "
            f"was resolved at PROBE time — the moment a release publishes "
            f"inside the cache window, this 404s. Pin the URL to the release "
            f"the filename came from (the API's browser_download_url)."
        )
        assert "/releases/download/" in url, (
            f"url {url!r} is not a pinned-tag asset URL")


@pytest.mark.parametrize("ua", [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605",
    "Mozilla/5.0 (X11; Linux x86_64) Chrome/120",
])
def test_the_url_version_equals_the_filename_version(ua):
    """The sharper form of the same invariant, and the one that survives.

    Asserting merely "not /latest/" would be satisfied by any pinned URL, even
    one pinned to the WRONG tag. What actually has to hold is that the release
    the URL points at is the release the file belongs to — so the version in
    the tag segment must equal the version in the filename.
    """
    mod = _platform_mod()
    picks = mod._match_platform_assets(ua, arch_hint="",
                                       published=_published())
    assert picks, f"no asset matched for {ua!r}"
    for p in picks:
        fname, url = p["filename"], p["url"]
        fv = _versions_in(fname)
        assert fv, f"filename {fname!r} carries no version to compare"
        # The tag segment is what sits between /releases/download/ and the
        # trailing filename.
        m = re.search(r"/releases/download/([^/]+)/", url)
        assert m, f"url {url!r} has no pinned tag segment"
        tag_v = _versions_in(m.group(1))
        assert tag_v, f"tag {m.group(1)!r} in {url!r} carries no version"
        assert tag_v[0] == fv[0], (
            f"the URL points at release {tag_v[0]} while the file it names is "
            f"from {fv[0]} ({fname!r} vs {url!r}). A file is only downloadable "
            f"from the release it was uploaded to, so these must be equal."
        )
        assert url.endswith(fname), (
            f"url {url!r} does not end with its own filename {fname!r}")


def test_the_asset_url_comes_from_the_api_not_reassembled():
    """Structural: the URL must be CARRIED with the name, not rebuilt from it.

    Rebuilding is what allowed a stale name and a live ``latest`` to be paired
    in the first place. When the URL travels in the same record as the name,
    mismatching them stops being expressible — which is a stronger guarantee
    than any assertion about the string.

    Docstrings AND comments are stripped before scanning. The module's own
    prose NAMES the rejected construction (``/releases/latest/download/``) in
    order to explain why it is wrong, so a raw substring scan would flag the
    very documentation that prevents the regression — and could be silenced by
    deleting that explanation, which is exactly backwards.
    """
    import ast

    # The URL assembly moved with the 2026-07 extraction
    # (pt_a859c11e75d142d1) from routes/api_v1/desktop.py to
    # lib/desktop_dist/platforms.py — scan its real home, not the re-export.
    src = (ROOT / "lib" / "desktop_dist" / "platforms.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    # Collect every docstring's exact text so it can be excised.
    docs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                docs.append(d)
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    for d in docs:
        code = code.replace(d, "")
    assert "browser_download_url" in code, (
        "the route ignores the API's browser_download_url and builds its own "
        "URL — that is the reassembly this invariant forbids")
    assert "releases/latest/download" not in code, (
        "the 'latest/download' construction is still present in EXECUTABLE "
        "code; it pairs a probe-time filename with a click-time release")


def test_an_asset_without_a_browser_url_still_pins_the_tag():
    """The fallback must NOT reintroduce the defect.

    If the API ever omits ``browser_download_url``, the replacement must be
    built from the ``tag_name`` in that SAME payload — never from ``latest``,
    which is precisely the drift being fixed. Verified by driving the real
    payload parser with the field removed.
    """
    mod = _platform_mod()
    payload = {
        "tag_name": "v9.9.9",
        "assets": [{"name": "Tofu-Setup-9.9.9-win64.exe"}],   # no url field
    }
    rows = mod._assets_from_release_payload(payload, "owner/repo")
    assert rows, "the parser dropped an asset that has a name but no url"
    url = rows[0]["url"]
    assert "/releases/download/v9.9.9/" in url, (
        f"fallback url {url!r} must pin the payload's own tag")
    assert "latest" not in url, (
        f"fallback url {url!r} fell back to 'latest' — the same drift again")


def test_a_payload_with_no_tag_and_no_url_yields_nothing():
    """Refuse to invent a URL. An asset we cannot address is omitted, and the
    caller falls back to the releases page — a guessed URL would 404 silently
    while looking authoritative."""
    mod = _platform_mod()
    rows = mod._assets_from_release_payload(
        {"assets": [{"name": "Tofu-Setup-9.9.9-win64.exe"}]}, "owner/repo")
    assert rows == [], (
        f"with neither browser_download_url nor tag_name there is no honest "
        f"URL to build, so the asset must be dropped; got {rows!r}")
