"""Frontend boot-restore race — the "loading finished and suddenly switched my
conversation" bug.

On a slow/poor connection the sidebar is painted from the IndexedDB cache
INSTANTLY, so the user can click into a conversation (or start a new chat)
BEFORE the network-bound boot load (`initActiveTasks`) resolves. The boot
`.then()` used to UNCONDITIONALLY `loadConversation(_restoredConvId)` (the conv
active before the refresh) — yanking the user off whatever they had just opened.

`_bootRestoreActiveConv(restoredId)` (static/js/main.js) fixes this by acting
ONLY when `activeConvId` is still null (still on the welcome screen). This runs
the REAL shipped function body under node with a minimal harness; the biting
NEUTER strips the early `if (activeConvId) return;` guard and proves the user's
in-progress navigation is then clobbered.
"""
import json
import re
import subprocess
from pathlib import Path

from tests._runtime_sections import runtime_section_path

REPO = Path(__file__).resolve().parent.parent
MAIN_JS = Path(runtime_section_path('main.js'))


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"(async\s+)?function %s\s*\(" % re.escape(name), src)
    assert m, f"{name} not found"
    i = src.index("{", m.start())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


_HARNESS = r"""
'use strict';
let activeConvId = __ACTIVE_CONV__;
let conversations = __CONVS__;
const _loaded = [];
function loadConversation(id) { _loaded.push(id); activeConvId = id; }
const document = {
  getElementById(id) { return (id === 'messageInput') ? { value: __INPUT_VALUE__ } : null; },
};

__FN__

_bootRestoreActiveConv(__RESTORED_ID__);
console.log(JSON.stringify({ loaded: _loaded, activeConvId }));
"""


def _run(fn_src, active_conv, convs, restored_id, input_value=""):
    script = (_HARNESS
              .replace("__FN__", fn_src)
              .replace("__ACTIVE_CONV__", json.dumps(active_conv))
              .replace("__CONVS__", json.dumps(convs))
              .replace("__RESTORED_ID__", json.dumps(restored_id))
              .replace("__INPUT_VALUE__", json.dumps(input_value)))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    last = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(last)


_CONVS = [{"id": "conv-A"}, {"id": "conv-B"}]


def _fn():
    return _extract_fn(MAIN_JS.read_text(), "_bootRestoreActiveConv")


def test_restores_when_still_on_welcome():
    """activeConvId still null → restore the pre-refresh conv."""
    r = _run(_fn(), active_conv=None, convs=_CONVS, restored_id="conv-A")
    assert r["loaded"] == ["conv-A"], r
    assert r["activeConvId"] == "conv-A", r


def test_does_not_switch_when_user_already_navigated():
    """THE BUG: user clicked into conv-B during the slow load (activeConvId set).
    The late boot restore must NOT switch them back to the pre-refresh conv-A."""
    r = _run(_fn(), active_conv="conv-B", convs=_CONVS, restored_id="conv-A")
    assert r["loaded"] == [], f"boot restore yanked the user off their conv: {r}"
    assert r["activeConvId"] == "conv-B", r


def test_autoselect_newest_when_no_restore_target():
    """No valid restore id + still on welcome → auto-select conversations[0]."""
    r = _run(_fn(), active_conv=None, convs=_CONVS, restored_id="gone")
    assert r["loaded"] == ["conv-A"], r


def test_no_autoselect_when_user_typing():
    """Still on welcome but the composer has text → don't steal focus."""
    r = _run(_fn(), active_conv=None, convs=_CONVS, restored_id="gone", input_value="draft")
    assert r["loaded"] == [], r


def test_neuter_guard_clobbers_user_navigation():
    """NEUTER: strip the `if (activeConvId) return;` early-exit → the late boot
    restore switches the user off their in-progress conversation (the bug)."""
    src = _fn()
    neutered = re.sub(r"if \(activeConvId\) return;[^\n]*", "/* guard neutered */", src, count=1)
    assert neutered != src and "if (activeConvId) return;" not in neutered, "neuter did not strip the guard"
    r = _run(neutered, active_conv="conv-B", convs=_CONVS, restored_id="conv-A")
    assert r["loaded"] == ["conv-A"], f"neutered guard should clobber the user's nav: {r}"
    assert r["activeConvId"] == "conv-A", r


if __name__ == "__main__":
    test_restores_when_still_on_welcome()
    print("PASS test_restores_when_still_on_welcome")
    test_does_not_switch_when_user_already_navigated()
    print("PASS test_does_not_switch_when_user_already_navigated")
    test_autoselect_newest_when_no_restore_target()
    print("PASS test_autoselect_newest_when_no_restore_target")
    test_no_autoselect_when_user_typing()
    print("PASS test_no_autoselect_when_user_typing")
    test_neuter_guard_clobbers_user_navigation()
    print("PASS test_neuter_guard_clobbers_user_navigation")
    print("ALL GREEN")
