"""Frontend test — the input-toolbar declutter (2026-07-08).

Two low-risk simplifications of the "too complex" composer toolbar:

  #1  The thinking-depth bar was a second cluster in the toolbar row. It now
      lives folded INSIDE the model dropdown (#presetDropdown) as a titled
      footer (#thinkingDepthSection wrapping .thinking-depth-bar). Because the
      model LIST is rebuilt on every server-config refresh, the rebuild must
      write into the inner #presetDropdownList container — NOT #presetDropdown
      itself — or it would wipe the depth footer. This test drives the REAL
      shipped `_populateModelDropdown` under jsdom and asserts the footer
      survives a rebuild. A neuter (writing into #presetDropdown directly)
      must wipe it.

  #2  The two standalone launcher buttons (creative/image-gen mode + project)
      were folded into the existing Tools submenu (#submenuTools) below a
      divider — the confusing "···" overflow submenu (#submenuMore) was
      DELETED. The item IDs (imageGenModeBtn / projectToggle) are preserved so
      the existing .active-class handlers keep working; opening #submenuTools
      reveals them. This test drives the REAL shipped `toggleSubmenu`.

Extraction-and-eval under jsdom, matching the project's other frontend tests.
"""
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
TOOLBAR_JS = ROOT / "static" / "js" / "main" / "main_toolbar_ui.js"
INDEX_HTML = ROOT / "index.html"


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not available")
    return exe


def _has_jsdom() -> bool:
    try:
        subprocess.run([_node(), "-e", "require('jsdom')"], cwd=ROOT,
                       capture_output=True, check=True)
        return True
    except Exception:
        return False


def _slice_fn(src: str, signature: str) -> str:
    """Brace-match slice a named function body out of a source file."""
    start = src.index(signature)
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"could not slice {signature}")


def _populate_fn() -> str:
    return _slice_fn(TOOLBAR_JS.read_text(encoding="utf-8"),
                     "function _populateModelDropdown(models) {")


def _toggle_fn() -> str:
    return _slice_fn(TOOLBAR_JS.read_text(encoding="utf-8"),
                     "function toggleSubmenu(id) {")


# Minimal markup mirroring the shipped index.html dropdown + overflow structure.
DROPDOWN_HTML = """
  <div class="preset-toggle-wrapper" id="presetWrapper">
    <div class="preset-toggle" id="presetToggle" data-model=""><span class="ps-label">Model</span></div>
    <div class="preset-dropdown" id="presetDropdown">
      <div class="preset-dropdown-list" id="presetDropdownList">
        <div class="ps-dd-loading">loading</div>
      </div>
      <div class="ps-dd-depth-wrap" id="thinkingDepthSection" style="display:none">
        <div class="ps-dd-depth-title">DEPTH</div>
        <div class="thinking-depth-bar">
          <button class="depth-btn active" data-depth="medium">Med</button>
        </div>
      </div>
    </div>
  </div>
  <div class="toolbar-submenu" id="submenuTools">
    <button class="submenu-trigger" data-testid="toolstrigger">Tools</button>
    <div class="submenu-dropdown">
      <div class="submenu-item" id="browserToggle"><span>Browser</span></div>
      <div class="submenu-divider" role="separator"></div>
      <div class="submenu-item" id="imageGenModeBtn"><span>Creative</span></div>
      <div class="submenu-item" id="projectToggle"><span>Project</span></div>
    </div>
  </div>
"""

HARNESS = textwrap.dedent("""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM(`<!DOCTYPE html><body>{html}</body>`);
    global.document = dom.window.document;
    global.window = dom.window;

    // Stubs the shipped fns reference (defensive; the paths we hit avoid most).
    global.config = {{ model: 'aws.claude-opus-4.8' }};
    global.serverModel = 'aws.claude-opus-4.8';
    global._registeredModels = [];
    global._hiddenModels = new Set();
    global._detectBrand = () => 'generic';
    global._brandSvg = () => '<svg></svg>';
    global._modelShortName = (m) => m;
    global.selectModel = () => {{}};
    global.t = (k) => k;

    {populate}
    {toggle}

    // ---- #1: rebuild the model list, assert the depth footer survives ----
    const models = [
      {{ model_id: 'aws.claude-opus-4.8', provider_id: 'p', provider_name: 'P', capabilities: ['text'] }},
      {{ model_id: 'gemini-3.1', provider_id: 'p', provider_name: 'P', capabilities: ['text'] }},
    ];
    _populateModelDropdown(models);
    const footerAfter = !!document.getElementById('thinkingDepthSection');
    const depthBtnAfter = !!document.querySelector('#thinkingDepthSection .depth-btn');
    const itemsRendered = document.querySelectorAll('#presetDropdownList .preset-dropdown-item').length;
    const loadingGone = !document.querySelector('#presetDropdownList .ps-dd-loading');

    // ---- #2: launchers folded into the Tools submenu; #submenuMore is gone --
    const igId = !!document.getElementById('imageGenModeBtn');
    const projId = !!document.getElementById('projectToggle');
    // The launchers must live INSIDE the Tools submenu now (not a separate one).
    const igInTools = !!document.querySelector('#submenuTools #imageGenModeBtn');
    const projInTools = !!document.querySelector('#submenuTools #projectToggle');
    const moreGone = !document.getElementById('submenuMore');
    const tools = document.getElementById('submenuTools');
    toggleSubmenu('submenuTools');
    const openedAfter = tools.classList.contains('open');
    toggleSubmenu('submenuTools');
    const closedAfter = !tools.classList.contains('open');

    console.log(JSON.stringify({{
      footerAfter, depthBtnAfter, itemsRendered, loadingGone,
      igId, projId, igInTools, projInTools, moreGone, openedAfter, closedAfter
    }}));
""")


def _run(populate: str, toggle: str, html: str = DROPDOWN_HTML) -> dict:
    script = HARNESS.format(populate=populate, toggle=toggle, html=html.replace("`", "\\`"))
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_depth_footer_survives_model_list_rebuild_and_overflow_toggles():
    out = _run(_populate_fn(), _toggle_fn())
    # #1 — model list rebuilt into the inner container, footer intact
    assert out["itemsRendered"] == 2, "both models should render as dropdown items"
    assert out["loadingGone"] is True, "loading placeholder should be replaced"
    assert out["footerAfter"] is True, "depth footer wrapper must survive the rebuild"
    assert out["depthBtnAfter"] is True, "depth buttons must survive the rebuild"
    # #2 — launchers folded into the Tools submenu; #submenuMore removed
    assert out["igId"] is True and out["projId"] is True, \
        "launcher element IDs must be preserved"
    assert out["igInTools"] is True and out["projInTools"] is True, \
        "launchers must live inside the Tools submenu now"
    assert out["moreGone"] is True, "the confusing '···' #submenuMore must be gone"
    assert out["openedAfter"] is True, "toggleSubmenu should open #submenuTools"
    assert out["closedAfter"] is True, "toggleSubmenu should close #submenuTools"


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NC_populate_into_dropdown_root_wipes_footer():
    """Neuter: point _populateModelDropdown at #presetDropdown (the OLD target)
    instead of the inner list → the innerHTML='' wipes the folded-in depth
    footer, proving the inner-container write is load-bearing."""
    fn = _populate_fn()
    neutered = fn.replace(
        'const dropdown = document.getElementById("presetDropdownList")\n'
        '    || document.getElementById("presetDropdown");',
        'const dropdown = document.getElementById("presetDropdown");',
    )
    assert neutered != fn, "neuter substitution did not apply"
    out = _run(neutered, _toggle_fn())
    assert out["footerAfter"] is False, (
        "NEUTER must wipe: writing the model list into #presetDropdown root "
        "destroys the depth footer"
    )
