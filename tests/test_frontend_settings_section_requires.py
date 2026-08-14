"""Frontend test — the degraded-section contract (settings/section_requires.js).

THE DEFECT (found by the owner in the live UI, 2026-07-29): the "Brand mascot"
settings block shipped its heading + description as static server-spliced HTML,
while its tiles were painted by JS from core/brand_logo.js. A running server
holds the bundle manifest it booted with, so a NEWLY added module is absent
from the served bundle until a restart — and the user was shown a title, a
description, and an EMPTY BOX. A control that looks available and does nothing.

That block has since been REMOVED (the owner settled the mascot question: one
mascot, no picker), so the contract currently has NO shipped consumer. The
mechanism is kept because it is generic and the defect is a property of how
JS-painted settings blocks are built, not of that one picker — see the header
of section_requires.js.

Consequently these tests drive SYNTHETIC markup built to the documented
contract. That is deliberate and stated plainly: a guard that pointed at the
deleted logo block would either fail or, worse, be quietly rewritten to assert
something that no longer ships. When a real block adopts `data-requires`, add a
wiring guard for THAT block — `test_a_shipped_block_that_declares_the_contract_is_wired`
below is the ready-made anchor for it.
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
MODULE = ROOT / "static" / "js" / "settings" / "section_requires.js"
MODULE_TS = ROOT / "frontend" / "src" / "features" / "settings" / "section-requires.ts"
ESBUILD = ROOT / "node_modules" / ".bin" / "esbuild"
PANEL = ROOT / "static" / "settings_panels" / "general.html"
STYLES = ROOT / "static" / "styles.css"


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not available")
    return exe


def _has_jsdom() -> bool:
    try:
        subprocess.run([_node(), "-e", "require('jsdom')"],
                       cwd=ROOT, capture_output=True, check=True)
        return True
    except Exception:
        return False


def _section_html(requires: str = "paintSomeTab", control_id: str = "someTabPicker") -> str:
    """A settings block built to the documented contract.

    Synthetic on purpose: the contract has no shipped consumer right now (the
    brand-mascot block that motivated it was removed). Mirrors the shape the
    module header documents — declaring wrapper, a `.theme-picker` control, and
    the notice element.
    """
    return ('<div class="settings-section-needs-js" data-requires="' + requires + '">'
            '<div class="settings-section-title">Some block</div>'
            '<div class="settings-toggle-desc">what it does</div>'
            '<div class="theme-picker" id="' + control_id + '"></div>'
            '<div class="settings-section-js-missing">\u6b64\u529f\u80fd\u9700\u91cd\u542f\u670d\u52a1\u540e\u751f\u6548\u3002</div>'
            '</div>')


HARNESS = textwrap.dedent("""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM(`<!DOCTYPE html><body>{body}</body>`,
                          {{ url: 'http://localhost:15000/' }});
    global.window = dom.window;
    global.document = dom.window.document;
    {defines}
    // ---- BEGIN real shipped module ----
    {source}
    // ---- END real shipped module ----
    const degraded = window.applySectionRequirements();
    const sec = document.querySelector('.settings-section-needs-js');
    const picker = document.querySelector('.settings-section-needs-js .theme-picker');
    const notice = document.querySelector('.settings-section-js-missing');
    console.log(JSON.stringify({{
      degradedCount: degraded,
      hasDegradedClass: sec ? sec.classList.contains('degraded') : null,
      pickerEmpty: picker ? picker.children.length === 0 : null,
      noticeText: notice ? notice.textContent.trim() : null,
    }}));
""")


def _run(body: str, defines: str = "", source: str | None = None) -> dict:
    script = HARNESS.format(body=body, defines=defines,
                            source=(source if source is not None else
                                    MODULE.read_text(encoding="utf-8")))
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_missing_module_degrades_the_section():
    """The exact live failure: the painting module is absent → the block must
    NOT sit there as an empty box; it must be marked degraded so CSS hides the
    control and reveals the notice."""
    out = _run(_section_html())               # no paintSomeTab defined
    assert out["degradedCount"] == 1, "the block must be counted as degraded"
    assert out["hasDegradedClass"] is True, (
        "with its module missing the section MUST carry .degraded — otherwise "
        "the user sees a title + description + empty box (a dead control)"
    )
    assert out["pickerEmpty"] is True, "precondition: no JS means no tiles"
    assert out["noticeText"], "a human-readable notice must be present to show"


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_present_module_leaves_the_section_fully_usable():
    out = _run(_section_html(),
               defines="global.window.paintSomeTab = function () { return []; };")
    assert out["degradedCount"] == 0
    assert out["hasDegradedClass"] is False, (
        "with the module present the section must render normally"
    )


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_contract_is_generic_not_a_logo_special_case():
    """Any block can declare a dependency — adding a JS-gated section is one
    attribute, not new code. Also covers the multi-symbol form."""
    body = ('<div class="settings-section-needs-js" data-requires="someFn otherFn">'
            '<div class="theme-picker" id="someTabPicker"></div>'
            '<div class="settings-section-js-missing">needs restart</div></div>')
    both_missing = _run(body)
    assert both_missing["hasDegradedClass"] is True
    one_missing = _run(body, defines="global.window.someFn = function () {};")
    assert one_missing["hasDegradedClass"] is True, (
        "a block must degrade unless EVERY declared symbol is present"
    )
    all_present = _run(body, defines=("global.window.someFn = function () {};"
                                      "global.window.otherFn = function () {};"))
    assert all_present["hasDegradedClass"] is False


@pytest.mark.skipif(not _has_jsdom() or not ESBUILD.is_file(),
                    reason="jsdom + esbuild not installed")
def test_vite_section_requirements_match_classic_contract(tmp_path):
    built = tmp_path / "section-requires.js"
    compiled = subprocess.run(
        [str(ESBUILD), str(MODULE_TS), "--bundle", "--format=iife",
         "--platform=browser", f"--outfile={built}"],
        cwd=ROOT, capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr
    source = built.read_text(encoding="utf-8")

    missing = _run(_section_html(), source=source)
    present = _run(
        _section_html(),
        defines="global.window.paintSomeTab = function () {};",
        source=source,
    )
    multi = _run(
        _section_html("paintSomeTab otherTab"),
        defines=("global.window.paintSomeTab = function () {};"
                 "global.window.otherTab = true;"),
        source=source,
    )
    assert missing["degradedCount"] == 1
    assert missing["hasDegradedClass"] is True
    assert present["degradedCount"] == 0
    assert present["hasDegradedClass"] is False
    assert multi["hasDegradedClass"] is False


def test_css_actually_hides_the_control_and_shows_the_notice():
    """The class flip is only meaningful if the stylesheet acts on it —
    otherwise the guard would pass while the user still sees the empty box."""
    css = STYLES.read_text(encoding="utf-8")
    assert re.search(r"\.settings-section-js-missing\{[^}]*display:none", css), (
        "the notice must be hidden by default"
    )
    assert re.search(r"\.settings-section-needs-js\.degraded>\.theme-picker[^{]*\{[^}]*display:none", css), (
        "a degraded section MUST hide its control (the empty box must go away)"
    )
    assert re.search(r"\.settings-section-needs-js\.degraded>\.settings-section-js-missing\{[^}]*display:block", css), (
        "a degraded section MUST reveal its notice"
    )


def test_a_shipped_block_that_declares_the_contract_is_wired():
    """Wiring guard for FUTURE adopters (no consumer today — by design).

    The brand-mascot block was the only `data-requires` consumer and it was
    removed with the mascot picker, so this asserts a conditional: any shipped
    block that declares the contract must also ship the notice element the
    contract reveals — without it, degrading hides the control and leaves
    NOTHING, which is worse than the empty box it replaced.

    It also pins the call site: `applySectionRequirements()` must stay wired
    into settings-open. Dropping the call would make `data-requires` silently
    inert — the next adopter would get no protection and no error either.
    """
    html = PANEL.read_text(encoding="utf-8")
    if 'data-requires=' in html:
        assert "settings-section-js-missing" in html, (
            "a block declaring data-requires must also ship a notice element, "
            "or degrading it hides the control and shows nothing at all"
        )
    core_panel = (ROOT / "static" / "js" / "settings" / "core_panel.js").read_text(encoding="utf-8")
    assert "applySectionRequirements()" in core_panel, (
        "settings-open must still invoke the contract — otherwise data-requires "
        "becomes a no-op attribute that looks like protection"
    )


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NC_requirement_check_removed_leaves_the_dead_control():
    """Neuter: make the check always pass → the live defect returns (a block
    whose module is missing still presents itself as usable)."""
    src = MODULE.read_text(encoding="utf-8")
    poisoned = src.replace("if (typeof window[names[i]] === 'undefined') return false;",
                           "/* neutered */")
    script = HARNESS.format(body=_section_html(), defines="", source=poisoned)
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["hasDegradedClass"] is False, (
        "NEUTER must leak: without the symbol check a section with a missing "
        "module is left looking usable — the exact dead control the owner hit"
    )
