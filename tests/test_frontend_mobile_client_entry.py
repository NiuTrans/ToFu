"""Frontend test — the config-gated mobile-client download entry.

The Settings footer shows a discreet "Mobile client" link ONLY when
``GET /api/health`` returns a ``mobile_client_url`` (set via the
``TOFU_MOBILE_CLIENT_URL`` env var). Absent → the anchor stays hidden, so no
dead button ever ships before a release APK exists.

This drives the REAL shipped render logic from ``settings/core_panel.js`` under
jsdom (extraction-and-eval, matching the project's other frontend tests), with
a fake ``Api.health.info()`` and ``t()``. A neuter (removing the ``if (url)``
gate) must make the absent-URL case wrongly render — proving the gate is
load-bearing.
"""
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
CORE_PANEL = ROOT / "static" / "js" / "settings" / "core_panel.js"


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not available")
    return exe


def _has_jsdom() -> bool:
    try:
        subprocess.run(
            [_node(), "-e", "require('jsdom')"],
            cwd=ROOT, capture_output=True, check=True,
        )
        return True
    except Exception:
        return False


def _extract_render_snippet() -> str:
    """Pull the mobile-client render block out of openSettings().

    We isolate just the health-callback body so the harness can run it without
    booting the whole settings panel. Kept as a literal-anchored slice so it
    tracks the real file (a drift in the source breaks the anchors → test fails
    loudly rather than silently testing stale code).
    """
    src = CORE_PANEL.read_text(encoding="utf-8")
    start = src.index("var mcEl = document.getElementById('settingsMobileClient');")
    end = src.index("mcEl.style.display = 'none';", start)
    end = src.index("}", end)  # close the else
    end = src.index("}", end + 1)  # close the if (mcEl)
    return src[start:end + 1]


HARNESS = textwrap.dedent("""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM(`<!DOCTYPE html><body>
      <a id="settingsMobileClient" style="display:none;"></a>
    </body>`);
    global.document = dom.window.document;
    // Minimal t() stub — returns the key's tail so we can assert the label wired.
    function t(k) {{ return k === 'settings.mobileClient' ? 'Mobile client' : k; }}
    // The mascot <img> comes from core/brand_logo.js — the single source of the
    // cache-busted URL. Stub it the way the real module renders (no onerror:
    // the skin fallback went away with the try-on mechanism, 2026-07-29).
    function brandLogoImgAttrs(size) {{
      return 'src="/static/icons/tofu-welcome.svg?v=TEST" data-brand-logo="1" alt="Tofu" '
        + 'width="' + size + '" height="' + size + '"';
    }}

    function runWith(d) {{
      // Reset the anchor to its default hidden state before each run. The
      // extracted snippet declares its OWN `var mcEl`, so we don't pre-declare.
      const _reset = document.getElementById('settingsMobileClient');
      _reset.style.display = 'none';
      _reset.setAttribute('href', '#');
      _reset.innerHTML = '';
      // ---- BEGIN extracted shipped snippet ----
      {snippet}
      // ---- END extracted shipped snippet ----
      return document.getElementById('settingsMobileClient');
    }}

    // Case 1: URL present → link renders, visible, correct href + label.
    let el = runWith({{ version: '1.0', mobile_client_url: 'https://github.com/x/y/releases' }});
    const shown = el.style.display !== 'none';
    const hasHref = el.getAttribute('href') === 'https://github.com/x/y/releases';
    const hasLabel = el.textContent.includes('Mobile client');
    const hasSvg = el.innerHTML.includes('tofu-welcome.svg');
    // The entry must route through the shared brand-logo helper so it picks up
    // the cache-bust token (a hand-written path here would silently keep showing
    // the old logo for up to 24h).
    const usesBrandHelper = el.innerHTML.includes('data-brand-logo');

    // Case 2: URL absent → stays hidden.
    let el2 = runWith({{ version: '1.0' }});
    const hidden = el2.style.display === 'none';

    console.log(JSON.stringify({{ shown, hasHref, hasLabel, hasSvg, usesBrandHelper, hidden }}));
""")


def _run(snippet: str) -> dict:
    import json
    script = HARNESS.format(snippet=snippet)
    proc = subprocess.run(
        [_node(), "-e", script], cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_mobile_client_entry_renders_only_when_url_present():
    out = _run(_extract_render_snippet())
    assert out["shown"] is True, "link must be visible when URL present"
    assert out["hasHref"] is True, "href must be the release URL"
    assert out["hasLabel"] is True, "label must use the i18n key"
    assert out["hasSvg"] is True, "must use the tofu-welcome.svg mascot icon (no emoji, §3.4)"
    assert out["usesBrandHelper"] is True, (
        "must render the mascot via brandLogoImgAttrs() — the single source of the "
        "cache-busted URL (core/brand_logo.js)"
    )
    assert out["hidden"] is True, "must stay hidden when URL absent"


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NC_gate_removed_leaks_dead_button():
    """Neuter: drop the `if (url)` gate → absent-URL case wrongly renders."""
    snippet = _extract_render_snippet()
    # Poison: force the render branch regardless of url (the exact defect the
    # gate prevents — a dead button when no APK URL is configured).
    neutered = snippet.replace("var url = d && d.mobile_client_url;",
                               "var url = 'https://DEAD-BUTTON';")
    out = _run(neutered)
    # With the gate neutered, the absent-URL case is no longer hidden.
    assert out["hidden"] is False, (
        "NEUTER must leak: without the url gate the entry renders even when "
        "the server exposes no mobile_client_url"
    )
