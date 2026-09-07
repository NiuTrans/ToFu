"""Frontend test — the config-gated mobile-client download card.

The Settings › General › About & Update card renders ONLY when
``GET /api/health`` returns a ``mobile_client_url`` (set via the
``TOFU_MOBILE_CLIENT_URL`` env var). Absent → the card stays hidden, so no
dead button ever ships before a release APK exists.

The Android row carries a version badge fed by ``mobile_client_version``
(backend ``MOBILE_CLIENT_VERSION``, pinned to the Gradle ``versionName`` by
tests/test_mobile_client_apk_url.py). The iOS row is an inert "coming soon"
until ``ios_client_url`` (TOFU_IOS_CLIENT_URL) ships a real link — then it
flips into an active download and the badge hides.

This drives the REAL shipped render logic from ``settings/core_panel.js`` under
jsdom (extraction-and-eval, matching the project's other frontend tests), with
a fake health payload. A neuter (removing the ``if (url)`` gate) must make the
absent-URL case wrongly render — proving the gate is load-bearing.
"""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
CORE_PANEL = Path(runtime_section_path("settings/core_panel.js"))


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not available")
    return exe


def _has_jsdom() -> bool:
    try:
        subprocess.run(
            [_node(), "-e", "require('jsdom')"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        )
        return True
    except Exception:
        return False


def _extract_render_snippet() -> str:
    """Pull the mobile-card render block out of openSettings().

    We isolate just the health-callback body so the harness can run it without
    booting the whole settings panel. Kept as a literal-anchored slice so it
    tracks the real file (a drift in the source breaks the anchors → test fails
    loudly rather than silently testing stale code).
    """
    src = CORE_PANEL.read_text(encoding="utf-8")
    start = src.index("var mcCard = document.getElementById('settingsMobileCard');")
    end = src.index("mcCard.style.display = 'none';", start)
    end = src.index("}", end)  # close the else
    end = src.index("}", end + 1)  # close the if (mcCard)
    return src[start : end + 1]


HARNESS = textwrap.dedent("""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM(`<!DOCTYPE html><body>
      <div id="settingsMobileCard" style="display:none">
        <a id="settingsMobileAndroid" href="#"></a>
        <span id="settingsMobileAndroidVersion"></span>
        <a id="settingsMobileIos" class="stg-mobile-row-soon"></a>
        <span id="settingsMobileIosBadge"></span>
      </div>
    </body>`);
    global.document = dom.window.document;

    function runWith(d) {{
      // Reset the card to its default hidden state before each run. The
      // extracted snippet declares its OWN `var mcCard`, so we don't pre-declare.
      const _card = document.getElementById('settingsMobileCard');
      _card.style.display = 'none';
      const _a = document.getElementById('settingsMobileAndroid');
      _a.setAttribute('href', '#');
      const _v = document.getElementById('settingsMobileAndroidVersion');
      _v.textContent = ''; _v.style.display = '';
      const _ios = document.getElementById('settingsMobileIos');
      _ios.removeAttribute('href'); _ios.removeAttribute('target');
      _ios.classList.add('stg-mobile-row-soon');
      document.getElementById('settingsMobileIosBadge').style.display = '';
      // ---- BEGIN extracted shipped snippet ----
      {snippet}
      // ---- END extracted shipped snippet ----
      return {{
        card: document.getElementById('settingsMobileCard'),
        android: document.getElementById('settingsMobileAndroid'),
        version: document.getElementById('settingsMobileAndroidVersion'),
        ios: document.getElementById('settingsMobileIos'),
        iosBadge: document.getElementById('settingsMobileIosBadge'),
      }};
    }}

    // Case 1: URL + version present → card visible, Android href + badge set,
    // iOS stays inert without an ios_client_url.
    let r1 = runWith({{
      version: '1.0',
      mobile_client_url: 'https://github.com/x/y/releases/latest/download/tofu-android.apk',
      mobile_client_version: '0.1.16',
    }});
    const shown = r1.card.style.display !== 'none';
    const hasHref = r1.android.getAttribute('href')
      === 'https://github.com/x/y/releases/latest/download/tofu-android.apk';
    const badgeShown = r1.version.textContent === 'v0.1.16'
      && r1.version.style.display !== 'none';
    const iosInert = r1.ios.classList.contains('stg-mobile-row-soon')
      && !r1.ios.getAttribute('href')
      && r1.iosBadge.style.display !== 'none';

    // Case 2: version absent → badge hides rather than rendering a bare "v".
    let r2 = runWith({{ version: '1.0', mobile_client_url: 'https://x/y.apk' }});
    const badgeHidden = r2.version.textContent === ''
      && r2.version.style.display === 'none';

    // Case 3: ios_client_url present → iOS row activates, badge hides.
    let r3 = runWith({{
      version: '1.0',
      mobile_client_url: 'https://x/y.apk',
      ios_client_url: 'https://testflight.apple.com/join/abc',
    }});
    const iosActive = r3.ios.getAttribute('href') === 'https://testflight.apple.com/join/abc'
      && r3.ios.getAttribute('target') === '_blank'
      && !r3.ios.classList.contains('stg-mobile-row-soon')
      && r3.iosBadge.style.display === 'none';

    // Case 4: URL absent → whole card stays hidden.
    let r4 = runWith({{ version: '1.0' }});
    const hidden = r4.card.style.display === 'none';

    console.log(JSON.stringify({{ shown, hasHref, badgeShown, iosInert, badgeHidden, iosActive, hidden }}));
""")


def _run(snippet: str) -> dict:
    import json

    script = HARNESS.format(snippet=snippet)
    proc = subprocess.run(
        [_node(), "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_mobile_client_card_renders_only_when_url_present():
    out = _run(_extract_render_snippet())
    assert out["shown"] is True, "card must be visible when URL present"
    assert out["hasHref"] is True, "Android row href must be the release URL"
    assert out["badgeShown"] is True, (
        "Android version badge must render v<mobile_client_version>"
    )
    assert out["badgeHidden"] is True, (
        "no mobile_client_version → badge hides (no bare 'v' left behind)"
    )
    assert out["iosInert"] is True, (
        "iOS row must stay an inert coming-soon without ios_client_url"
    )
    assert out["iosActive"] is True, (
        "ios_client_url set → iOS row flips into an active download, badge hides"
    )
    assert out["hidden"] is True, "card must stay hidden when URL absent"


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NC_gate_removed_leaks_dead_card():
    """Neuter: drop the `if (url)` gate → absent-URL case wrongly renders."""
    snippet = _extract_render_snippet()
    # Poison: force the render branch regardless of url (the exact defect the
    # gate prevents — a dead card when no APK URL is configured).
    neutered = snippet.replace(
        "var url = d && d.mobile_client_url;", "var url = 'https://DEAD-CARD';"
    )
    out = _run(neutered)
    # With the gate neutered, the absent-URL case is no longer hidden.
    assert out["hidden"] is False, (
        "NEUTER must leak: without the url gate the card renders even when "
        "the server exposes no mobile_client_url"
    )
