"""Frontend test — composer width floored to the chat reading column (2026-07-08).

The input box width is driven by `.input-inner{max-width:var(--toolbar-w,820px)}`,
and `_reflowToolbar()` (static/js/main.js) sets `--toolbar-w` to the MEASURED
natural width of the toolbar content. After the toolbar declutter (buttons
folded into submenus + a "···" overflow), that content shrank to ~540px, while
the message column `.chat-inner` stays 820px — so on a wide landscape display
the composer read as "too narrow" under the 820px messages.

The fix floors `--toolbar-w` to the `.chat-inner` reading measure (single-source
in CSS: 820 desktop / 920 portrait-tablet), while still letting a genuinely
wider toolbar EXPAND beyond it, and still capping to the viewport on a narrow
window. This test drives the REAL shipped `_reflowToolbar` under jsdom.

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
MAIN_JS = ROOT / "static" / "js" / "main.js"


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


def _reflow_fn() -> str:
    return _slice_fn(MAIN_JS.read_text(encoding="utf-8"),
                     "function _reflowToolbar() {")


# A composer whose toolbar content is intentionally NARROW (~540px total) —
# mirroring the decluttered toolbar — under a wide viewport with an 820px
# reading column. The fix must floor the composer to 820, not leave it at 540.
DOM_HTML = """
  <div class="chat-inner" id="chatInner" style="max-width:820px"></div>
  <div class="input-area"><div class="input-inner" id="inputInner">
    <div class="input-box" id="inputBox">
      <div class="input-actions" id="bar">
        <div class="input-group" id="k1"></div>
        <div class="input-group" id="k2"></div>
      </div>
    </div>
  </div></div>
"""

HARNESS = textwrap.dedent("""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM(`<!DOCTYPE html><body>{html}</body>`);
    global.document = dom.window.document;
    global.window = dom.window;
    global.getComputedStyle = dom.window.getComputedStyle.bind(dom.window);
    global.requestAnimationFrame = (cb) => {{}};  // no-op; only re-enables transition

    // jsdom does no layout: force the two runtime reads the fn depends on.
    Object.defineProperty(dom.window.HTMLElement.prototype, 'offsetParent',
      {{ configurable: true, get() {{ return document.body; }} }});
    Object.defineProperty(document.documentElement, 'clientWidth',
      {{ configurable: true, value: 1600 }});

    // Toolbar children measure ~540px total (270 + 270) — a narrow decluttered bar.
    document.getElementById('k1').getBoundingClientRect = () => ({{ width: 270 }});
    document.getElementById('k2').getBoundingClientRect = () => ({{ width: 270 }});

    {reflow}

    _reflowToolbar();
    const tw = document.getElementById('inputInner').style.getPropertyValue('--toolbar-w');
    console.log(JSON.stringify({{ toolbarW: parseFloat(tw) }}));
""")


def _run(reflow: str) -> dict:
    script = HARNESS.format(reflow=reflow, html=DOM_HTML.replace("`", "\\`"))
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_composer_floored_to_chat_reading_column():
    out = _run(_reflow_fn())
    # Narrow toolbar (~540px) must be floored UP to the 820px reading column
    # (the fn may add a small border allowance, so assert >= 820, not ==).
    assert out["toolbarW"] >= 820, (
        f"composer width {out['toolbarW']} should be floored to the 820px chat "
        "reading column, not left at the ~540px decluttered-toolbar width"
    )


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NC_old_480_floor_leaves_composer_narrow():
    """Neuter: restore the old `Math.max(480, Math.min(w, maxW))` floor (no
    reading-column floor) → the narrow toolbar stays ~540px, proving the
    reading-column floor is load-bearing."""
    fn = _reflow_fn()
    # Replace the whole reading-floor block with the pre-fix single line.
    neutered = re.sub(
        r"let readingFloor = 820;.*?w = Math\.min\(Math\.max\(w, readingFloor\), maxW\);",
        "w = Math.max(480, Math.min(w, maxW));",
        fn,
        flags=re.DOTALL,
    )
    assert neutered != fn, "neuter substitution did not apply"
    out = _run(neutered)
    assert out["toolbarW"] < 820, (
        "NEUTER must leave the composer at the narrow measured toolbar width "
        f"(<820), got {out['toolbarW']}"
    )
