"""Frontend test — the Studio dial must ALWAYS open the project panel.

Regression: when the standalone project launcher button was folded into the
Air/Pro/Studio capability dial, the Studio segment became the ONLY affordance
for attaching / managing a project. But `setChatMode('studio')` only called
`openProjectModal()` when NO project was attached — once a project was already
attached, clicking Studio again just re-selected the mode and returned. That
left a conv already in Studio with no way to CHANGE its project path (clicking
Studio repeatedly was a silent no-op).

The fix: the Studio segment always opens the project panel — waiting for a real
attach when none exists, and reopening the panel (path management) when one is
already attached. This test drives the REAL shipped `setChatMode` under node
and asserts the panel opens in BOTH states. A neuter that early-returns without
opening in the has-project branch must fail the has-project case.
"""
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from tests._runtime_sections import runtime_section

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
TOOLBAR_SOURCE = runtime_section(
    "main/main_toolbar_ui.js", scope_prelude=False,
)


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not available")
    return exe


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


def _set_chat_mode_fn() -> str:
    return _slice_fn(TOOLBAR_SOURCE, "function setChatMode(mode) {")


HARNESS = textwrap.dedent("""
    // Minimal stubs the shipped setChatMode references. `order` records the
    // sequence of side effects so we can assert the panel opens FIRST.
    let openCount = 0;
    let applied = null;
    let saved = 0;
    const order = [];
    const throwInApply = {throw_in_apply};
    global.projectState = {projectState_js};
    global.openProjectModal = () => {{ openCount++; order.push('open'); }};
    global._applyChatModeUI = (m) => {{
      order.push('apply');
      if (throwInApply) throw new Error('simulated dial bookkeeping failure');
      applied = m;
    }};
    global.captureActiveConversationSettings = () => {{ saved++; order.push('save'); }};
    global.clearProject = () => {{}};
    global.debugLog = () => {{}};
    global.chatMode = 'chat';

    {set_chat_mode}

    // Tolerate a synchronous throw (the pre-fix code had no try/catch; the
    // neuter reproduces that) so we can still observe whether the panel opened.
    try {{ setChatMode('studio'); }} catch (e) {{ order.push('threw'); }}

    console.log(JSON.stringify({{ openCount, applied, saved, order }}));
""")


def _run(set_chat_mode: str, project_state: dict,
         throw_in_apply: bool = False) -> dict:
    script = HARNESS.format(set_chat_mode=set_chat_mode,
                            projectState_js=json.dumps(project_state),
                            throw_in_apply="true" if throw_in_apply else "false")
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


NO_PROJECT = {"active": False, "path": ""}
WITH_PROJECT = {"active": True, "path": "/mnt/work/repo"}


def test_studio_opens_panel_when_no_project():
    out = _run(_set_chat_mode_fn(), NO_PROJECT)
    assert out["openCount"] == 1, "Studio must open the project panel to attach"
    assert out["applied"] is None, "dial must NOT flip to studio before a real attach"


def test_studio_reopens_panel_when_project_already_attached():
    """The reported bug: an already-attached project could not change its path
    because clicking Studio again was a no-op."""
    out = _run(_set_chat_mode_fn(), WITH_PROJECT)
    assert out["openCount"] == 1, (
        "Studio must reopen the project panel even when a project is already "
        "attached, so the path can be changed"
    )
    assert out["applied"] == "studio", "dial stays in studio when a project is attached"
    assert out["saved"] == 1, "tool state should persist when re-selecting studio"
    # The panel must open FIRST — before the dial/state bookkeeping — so that
    # bookkeeping can never block the affordance.
    assert out["order"][0] == "open", (
        "openProjectModal must run BEFORE _applyChatModeUI/captureActiveConversationSettings"
    )


def test_studio_opens_panel_even_when_dial_bookkeeping_throws():
    """The real second bug: when a project is already attached, the pre-fix
    code ran _applyChatModeUI + captureActiveConversationSettings BEFORE opening the panel. If
    either threw synchronously the panel never opened — so an already-attached
    conv could never change its path, while attaching a fresh one (which skips
    that bookkeeping) worked. The panel must open regardless."""
    out = _run(_set_chat_mode_fn(), WITH_PROJECT, throw_in_apply=True)
    assert out["openCount"] == 1, (
        "panel must open even if the dial bookkeeping throws"
    )
    assert out["order"][0] == "open", "panel opens first, before the throwing bookkeeping"


def test_NC_open_after_bookkeeping_breaks_when_bookkeeping_throws():
    """Neuter: move the openProjectModal call to AFTER the bookkeeping block
    (the pre-fix ordering). With a throwing _applyChatModeUI the panel never
    opens — proving the open-first ordering is load-bearing."""
    fn = _set_chat_mode_fn()
    neutered = fn.replace(
        "    if (typeof openProjectModal === 'function') openProjectModal();\n"
        "    if (hasProject) {\n"
        "      try {\n"
        "        _applyChatModeUI('studio');\n"
        "        captureActiveConversationSettings();\n"
        "        debugLog('Mode: Studio (project attached)', 'success');\n"
        "      } catch (err) {\n"
        "        console.warn('[setChatMode] studio dial bookkeeping failed:', err);\n"
        "      }\n"
        "    }\n"
        "    return;",
        "    if (hasProject) {\n"
        "      _applyChatModeUI('studio');\n"
        "      captureActiveConversationSettings();\n"
        "      debugLog('Mode: Studio (project attached)', 'success');\n"
        "    }\n"
        "    if (typeof openProjectModal === 'function') openProjectModal();\n"
        "    return;",
    )
    assert neutered != fn, "neuter substitution did not apply"
    out = _run(neutered, WITH_PROJECT, throw_in_apply=True)
    assert out["openCount"] == 0, (
        "NEUTER must reproduce the bug: bookkeeping-before-open + a throw means "
        "the panel never opens"
    )
