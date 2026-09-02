"""Frontend test — a project-less conversation is NEVER Studio.

Reported bug: click the Studio segment → the project panel pops up → close it
without picking a project → the dial still reads "Studio". The click path
itself is correct (``setChatMode('studio')`` with no project opens the panel
and deliberately does NOT flip the dial — pinned by
tests/test_frontend_studio_reopens_project.py). The dial was resurrected by
two interlocking holes:

1.  ``onProjectCleared()`` (and ``onProjectAttached()``) repainted the dial
    but never persisted it, so ``conv.chatMode`` kept the stale ``'studio'``
    with an empty ``projectPath`` after the project was cleared.
2.  ``restoreConversationSettingsToComposer()`` trusted that stored tier verbatim —
    ``conv.chatMode || _deriveChatModeFromFlags(conv)`` — so the poisoned
    ``'studio'`` + no-``projectPath`` combination restored as Studio on the
    next reload / conv switch. Clicking Studio then only opens the panel
    (no dial change), and closing it leaves the wrongly-restored Studio in
    place — "还是 studio 模式".

Fix: (a) persist the tier immediately on attach/clear so ``conv.chatMode``
never lags the truth; (b) clamp on restore — a stored ``'studio'`` with no
``projectPath`` falls back to ``'chat'``, healing already-poisoned convs.

Bidirectional extension (epic pt_19634a23c5744c18, owner-directed 2026-07-28):
the MIRROR poison — a stored non-studio tier WITH a ``projectPath`` (stamped
by the one-way projectState→conv sync while another conversation's project
was globally active; measured in the DB: ms3sl904z633by, ms43foj3og3qh7) —
restored with the dial painted Chat while the project bar and collab bar
both appeared. The dial can never produce this shape deliberately (picking
Chat detaches the project), so the restore clamp now heals BOTH ways:
``studio ⟺ projectPath`` — demote studio-without-project, PROMOTE
non-studio-with-project. Both heals stay paint-only.

These tests drive the retained source owners under node
(``restoreConversationSettingsToComposer`` from ``runtime/sections/main.js``;
``_deriveChatModeFromFlags`` / ``onProjectAttached`` / ``onProjectCleared``
from ``runtime/sections/main/main_toolbar_ui.js``). Two neuters prove the assertions are
load-bearing: without the restore clamp the poisoned conv restores as Studio;
without the persist hook the clear path reopens the poison.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from tests._runtime_sections import runtime_section

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = runtime_section("main.js", scope_prelude=False)
TOOLBAR_SOURCE = runtime_section(
    "main/main_toolbar_ui.js", scope_prelude=False,
)
PROJECT_SOURCE = runtime_section("project.js", scope_prelude=False)

RESTORE_CLAMP = ("(_storedMode === 'studio' && !conv.projectPath) ? 'chat'\n"
                 "      : (_storedMode !== 'studio' && conv.projectPath) ? 'studio'\n"
                 "      : _storedMode")
PERSIST_HOOK = "if (typeof captureActiveConversationSettings === 'function') captureActiveConversationSettings();"


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


def _restore_fn() -> str:
    return _slice_fn(
        MAIN_SOURCE, "function restoreConversationSettingsToComposer(conv) {",
    )


def _derive_fn() -> str:
    return _slice_fn(TOOLBAR_SOURCE, "function _deriveChatModeFromFlags(conv) {")


def _toolbar_fn(name: str) -> str:
    return _slice_fn(TOOLBAR_SOURCE, f"function {name}() {{")


def _run(script: str) -> dict:
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── Harness A: restore path ─────────────────────────────────────────────
# Stubs for every bare call inside the real restoreConversationSettingsToComposer. The
# typeof-guarded hooks (updateSubmenuCounts / updateContextBar /
# presenceRefresh / projectBrainRefresh / convInfluenceRefresh) are left
# undefined on purpose so the guard branches are exercised too.
RESTORE_HARNESS = """
const runtimeScope = globalThis;
const appliedLog = [];
const stub = () => {};
const config = {};
const serverModel = 'stub-model';
const _applyModelUI = stub, _applySearchModeUI = stub, _applyFetchEnabledUI = stub,
      _applyCodeExecUI = stub, _applyBrowserUI = stub, _applyDesktopUI = stub,
      _applyMemoryUI = stub, _applyAgentModeUI = stub, _applyFlowUI = stub,
      _applyImageGenToolUI = stub, _applyImageGenUI = stub,
      _applyHumanGuidanceUI = stub, _applyAutoTranslateUI = stub,
      _updateAutoApplyUI = stub, _scheduleReflow = stub,
      convAutoTranslate = () => false;
const normalizeConversationInteractionModes = () => ({
  agentMode: 'standard', activeFlow: '',
});
const _applyChatModeUI = (m) => { appliedLog.push(m); };
__DERIVE_FN__
__RESTORE_FN__
restoreConversationSettingsToComposer(__CONV__);
console.log(JSON.stringify({
  applied: appliedLog.length ? appliedLog[appliedLog.length - 1] : null,
  calls: appliedLog.length,
}));
"""


def _run_restore(conv: dict, restore_src: str | None = None) -> dict:
    script = (RESTORE_HARNESS
              .replace("__DERIVE_FN__", _derive_fn())
              .replace("__RESTORE_FN__", restore_src or _restore_fn())
              .replace("__CONV__", json.dumps(conv)))
    return _run(script)


# ── Harness B: attach/clear persistence ─────────────────────────────────
TIER_HOOK_HARNESS = """
let applied = null;
let saved = 0;
let chatMode = __CHAT_MODE__;
const _applyChatModeUI = (m) => { applied = m; };
const captureActiveConversationSettings = () => { saved++; };
__FN__
__CALL__();
console.log(JSON.stringify({ applied, saved }));
"""


def _run_tier_hook(fn_name: str, chat_mode: str, fn_src: str | None = None) -> dict:
    script = (TIER_HOOK_HARNESS
              .replace("__CHAT_MODE__", json.dumps(chat_mode))
              .replace("__FN__", fn_src or _toolbar_fn(fn_name))
              .replace("__CALL__", fn_name))
    return _run(script)


# ═══════════════════════════════════════════════════════════════════════
# Restore clamp — a stored 'studio' with no projectPath must not survive
# ═══════════════════════════════════════════════════════════════════════

def test_restore_poisoned_studio_without_project_falls_back_to_chat():
    """The exact poisoned state from the reported bug: chatMode='studio' but
    no projectPath (left behind when the project was cleared before the
    fallback was persisted). Restore must heal it to chat."""
    out = _run_restore({"chatMode": "studio", "projectPath": ""})
    assert out["calls"] == 1
    assert out["applied"] == "chat", (
        "a project-less conversation must NEVER restore as Studio — the "
        "stored tier is poisoned and must be clamped"
    )


def test_restore_legit_studio_with_project_survives():
    """Studio + an attached project is the ONE legitimate studio state."""
    out = _run_restore({"chatMode": "studio", "projectPath": "/mnt/work/repo"})
    assert out["applied"] == "studio", (
        "clamping must not over-fire: a stored studio WITH a projectPath is valid"
    )


def test_restore_legacy_conv_without_chatMode_derives_from_project():
    """Pre-feature convs have no chatMode — derivation (project ⟺ studio)
    must still work."""
    out = _run_restore({"projectPath": "/mnt/work/repo"})
    assert out["applied"] == "studio"
    out = _run_restore({})
    assert out["applied"] == "chat"


def test_restore_clamp_leaves_legacy_non_studio_tiers_untouched():
    """The DEMOTE clamp must ONLY touch 'studio' — a legacy air tier with no
    project flows through to _applyChatModeUI (which normalises it) unchanged."""
    out = _run_restore({"chatMode": "air"})
    assert out["applied"] == "air", (
        "the clamp fired on a non-studio tier with no project — the demote "
        "branch must be studio-specific"
    )


# ═══════════════════════════════════════════════════════════════════════
# Promote clamp — a stored non-studio tier WITH a projectPath is the mirror
# poison: the project IS attached, so the conversation IS Studio
# ═══════════════════════════════════════════════════════════════════════

def test_restore_poisoned_chat_with_project_promotes_to_studio():
    """The exact mirror poison measured in the DB (chatMode='chat' +
    projectPath=chatui on ms3sl904z633by / ms43foj3og3qh7): restore painted
    the dial Chat while the project bar and the collab bar both showed — the
    'Studio not selected but Project Brain exists' complaint. Restore must
    heal it UP to Studio: a project attached IS Studio."""
    out = _run_restore({"chatMode": "chat", "projectPath": "/mnt/work/repo"})
    assert out["calls"] == 1
    assert out["applied"] == "studio", (
        "a conversation WITH an attached project must restore as Studio — the "
        "stored 'chat' tier is the mirror poison and must be clamped upward"
    )


def test_promote_clamp_does_not_fire_without_project():
    """The promote branch must key on projectPath — a plain 'chat' conv with
    no project stays chat (no phantom Studio)."""
    out = _run_restore({"chatMode": "chat", "projectPath": ""})
    assert out["applied"] == "chat"


def test_NC_without_promote_clamp_poisoned_conv_restores_as_chat():
    """Neuter: revert the clamp to the PRE-FIX one-way shape (demote only)
    → the poisoned conv restores as Chat with a project attached (the
    reported mismatch) — proving the promote branch specifically, not just
    'a clamp exists', is what heals the mirror poison."""
    fn = _restore_fn()
    assert RESTORE_CLAMP in fn, "harness stale: clamp expression not found"
    one_way = ("(_storedMode === 'studio' && !conv.projectPath) ? 'chat'"
               " : _storedMode")
    neutered = fn.replace(RESTORE_CLAMP, one_way)
    assert neutered != fn
    out = _run_restore({"chatMode": "chat", "projectPath": "/mnt/work/repo"},
                       restore_src=neutered)
    assert out["applied"] == "chat", (
        "NEUTER must reproduce the bug: with the pre-fix one-way clamp, "
        "poisoned chatMode='chat' + projectPath restores as Chat with the "
        "project still attached"
    )
    # discriminating: the demote direction still works on the one-way shape,
    # so the red is attributable to the MISSING promote branch alone.
    out2 = _run_restore({"chatMode": "studio", "projectPath": ""},
                        restore_src=neutered)
    assert out2["applied"] == "chat"


def test_NC_without_clamp_poisoned_conv_restores_as_studio():
    """Neuter: strip the restore clamp and the poisoned conv restores as
    Studio — proving the clamp is the only thing standing between a stale
    conv.chatMode and the reported bug."""
    fn = _restore_fn()
    assert RESTORE_CLAMP in fn, "harness stale: clamp expression not found"
    neutered = fn.replace(RESTORE_CLAMP, "_storedMode")
    assert neutered != fn
    out = _run_restore({"chatMode": "studio", "projectPath": ""},
                       restore_src=neutered)
    assert out["applied"] == "studio", (
        "NEUTER must reproduce the bug: without the clamp, poisoned "
        "chatMode='studio' + empty projectPath restores as Studio"
    )


# ═══════════════════════════════════════════════════════════════════════
# Attach / clear persistence — conv.chatMode must never lag the truth
# ═══════════════════════════════════════════════════════════════════════

def test_on_project_cleared_persists_chat_fallback():
    """Source of the poison: clearing a project while in studio repainted the
    dial but left conv.chatMode='studio' on disk. The fallback must be saved."""
    out = _run_tier_hook("onProjectCleared", "studio")
    assert out["applied"] == "chat"
    assert out["saved"] == 1, (
        "onProjectCleared must persist the chat fallback immediately — "
        "otherwise the stale 'studio' tier is resurrected on the next restore"
    )


def test_on_project_cleared_noop_when_already_chat():
    """No tier change → no needless persist."""
    out = _run_tier_hook("onProjectCleared", "chat")
    assert out["applied"] is None
    assert out["saved"] == 0


def test_on_project_attached_persists_studio_promotion():
    """Symmetric: the promotion to studio must also be durable right away,
    not whenever the next unrelated toggle happens to save."""
    out = _run_tier_hook("onProjectAttached", "chat")
    assert out["applied"] == "studio"
    assert out["saved"] == 1


def test_on_project_attached_noop_when_already_studio():
    out = _run_tier_hook("onProjectAttached", "studio")
    assert out["applied"] is None
    assert out["saved"] == 0


def test_NC_on_project_cleared_without_persist_reopens_poison():
    """Neuter: drop the persist hook from onProjectCleared — the save count
    falls to 0, which is exactly the old shape that poisoned conv.chatMode."""
    fn = _toolbar_fn("onProjectCleared")
    assert PERSIST_HOOK in fn, "harness stale: persist hook not found"
    neutered = fn.replace(PERSIST_HOOK, "")
    assert neutered != fn
    out = _run_tier_hook("onProjectCleared", "studio", fn_src=neutered)
    assert out["applied"] == "chat", "the repaint itself must still happen"
    assert out["saved"] == 0, (
        "NEUTER must reproduce the poison: repaint without persist leaves "
        "conv.chatMode stale"
    )


# ═══════════════════════════════════════════════════════════════════════
# Failure-rollback demotion — mpApplyFolders' optimistic promotion must be
# rolled back TOGETHER with the project state when setPaths fails
# ═══════════════════════════════════════════════════════════════════════
#
# Owner-acceptance catch on the first batch: mpApplyFolders promotes the dial
# to Studio optimistically (onProjectAttached — which now persists
# immediately), then reconciles Api.project.setPaths in the background. The
# catch branch reverted projectState + conv paths but NEVER demoted the dial,
# so a first-attach failure left the conv in the same poisoned
# "Studio + no project" shape — and now durably persisted. The fix: when the
# rolled-back previous state had no project, call onProjectCleared() (which
# repaints AND persists); when it DID have a project, Studio stays.

DEMOTION_LINE = ("if (!_prevProjectState.path && "
                 "typeof onProjectCleared === 'function') onProjectCleared();")

# Stubs for every bare call inside the real mpApplyFolders. The tier hooks
# (onProjectAttached / onProjectCleared) are the REAL shipped functions so
# the assertions observe the true end-state (dial + persist), and
# _applyChatModeUI is a recorder that also tracks the global chatMode like
# the real one does.
MP_APPLY_HARNESS = """
const appliedLog = [];
const savePathCalls = [];
let saved = 0;
let chatMode = __CHAT_MODE__;
const stub = () => {};
const _applyChatModeUI = (m) => { appliedLog.push(m); chatMode = m; };
const captureActiveConversationSettings = () => { saved++; };
const _saveConvProjectPath = (p, extras) => { savePathCalls.push({ p: p || '' }); };
const _applyProjectData = stub, _updateProjectUI = stub, closeProjectModal = stub,
      saveRecentProject = stub, debugLog = stub, renderConversationList = stub,
      saveConversations = stub, escapeHtml = (s) => String(s);
const document = { getElementById: (id) => ({
  classList: { add() {}, remove() {} },
  set innerHTML(v) {}, get innerHTML() { return ''; },
}) };
const Api = { project: { setPaths: __SET_PATHS__ } };
const conversations = [];
let activeConvId = 'c1';
let _mpFolders = ['/__new__'];
let _mpReadOnly = new Set();
let projectState = __PREV_STATE__;
__ON_ATTACHED__
__ON_CLEARED__
let opaCalls = 0, opcCalls = 0;
const _origOPA = onProjectAttached, _origOPC = onProjectCleared;
onProjectAttached = function () { opaCalls++; return _origOPA(); };
onProjectCleared = function () { opcCalls++; return _origOPC(); };
__MP_APPLY__
(async () => {
  await mpApplyFolders();
  console.log(JSON.stringify({
    appliedLog, saved, savePathCalls, opaCalls, opcCalls,
    chatMode, statePath: projectState.path || '',
  }));
})();
"""

FAIL_SET_PATHS = ("async () => ({ ok: false, json: async () => "
                  "({ error: 'boom' }) })")
OK_SET_PATHS = ("async () => ({ ok: true, json: async () => "
                "({ path: '/__new__', active: true }) })")
NO_PREV_PROJECT = {"active": False, "path": "", "extraRoots": []}
WITH_PREV_PROJECT = {"active": True, "path": "/old", "extraRoots": []}


def _run_mp_apply(prev_state: dict, chat_mode: str,
                  set_paths: str = FAIL_SET_PATHS,
                  mp_src: str | None = None) -> dict:
    src = mp_src or _slice_fn(PROJECT_SOURCE, "async function mpApplyFolders() {")
    script = (MP_APPLY_HARNESS
              .replace("__CHAT_MODE__", json.dumps(chat_mode))
              .replace("__SET_PATHS__", set_paths)
              .replace("__PREV_STATE__", json.dumps(prev_state))
              .replace("__ON_ATTACHED__", _toolbar_fn("onProjectAttached"))
              .replace("__ON_CLEARED__", _toolbar_fn("onProjectCleared"))
              .replace("__MP_APPLY__", src))
    return _run(script)


def test_rollback_without_prior_project_demotes_and_persists():
    """First-attach failure: optimistic promotion (studio, persisted) →
    setPaths fails → rollback to NO project must demote to chat AND persist
    (saved a second time), so the conv never rests in 'studio + no project'."""
    out = _run_mp_apply(NO_PREV_PROJECT, "chat")
    assert out["opaCalls"] == 1 and out["opcCalls"] == 1
    assert out["appliedLog"] == ["studio", "chat"], (
        "the optimistic studio promotion must be repainted back to chat on "
        "rollback when there is no prior project"
    )
    assert out["chatMode"] == "chat"
    assert out["saved"] == 2, (
        "both the promotion AND the rollback demotion must be persisted — "
        "a repaint without save is exactly the poison this suite guards"
    )
    assert out["savePathCalls"][-1]["p"] == "", "project path must roll back"


def test_rollback_with_prior_project_keeps_studio():
    """Changing paths on an already-attached project: failure rolls back to
    the OLD project — Studio stays truthful, no demotion, no extra save."""
    out = _run_mp_apply(WITH_PREV_PROJECT, "studio")
    assert out["opcCalls"] == 0, (
        "demotion must NOT fire when the rolled-back state still has a project"
    )
    assert out["chatMode"] == "studio"
    assert out["appliedLog"] == [], "dial was already studio — no repaint"
    assert out["saved"] == 0
    assert out["savePathCalls"][-1]["p"] == "/old", (
        "the prior project path must be restored"
    )
    assert out["statePath"] == "/old"


def test_success_path_still_promotes_without_demotion():
    """Happy path unchanged: setPaths succeeds → studio promotion persists,
    demotion never fires."""
    out = _run_mp_apply(NO_PREV_PROJECT, "chat", set_paths=OK_SET_PATHS)
    assert out["chatMode"] == "studio"
    assert out["appliedLog"] == ["studio"]
    assert out["saved"] == 1
    assert out["opcCalls"] == 0


def test_NC_rollback_without_demotion_strands_studio():
    """Neuter: strip the rollback demotion from the REAL mpApplyFolders and
    the first-attach failure strands the dial at Studio with no project —
    the exact poisoned shape the owner caught."""
    src = _slice_fn(PROJECT_SOURCE, "async function mpApplyFolders() {")
    assert DEMOTION_LINE in src, "harness stale: demotion line not found"
    neutered = src.replace(DEMOTION_LINE, "")
    assert neutered != src
    out = _run_mp_apply(NO_PREV_PROJECT, "chat", mp_src=neutered)
    assert out["opcCalls"] == 0
    assert out["chatMode"] == "studio", (
        "NEUTER must reproduce the bug: without the demotion, a failed "
        "first-attach strands the dial at Studio with no project attached"
    )
    assert out["savePathCalls"][-1]["p"] == ""


def test_rollback_demotion_present_in_project_js():
    src = _slice_fn(PROJECT_SOURCE, "async function mpApplyFolders() {")
    assert DEMOTION_LINE in src, (
        "runtime/sections/project.js mpApplyFolders catch branch lost the rollback "
        "demotion — a failed first-attach would strand Studio with no project"
    )


# ═══════════════════════════════════════════════════════════════════════
# Static pin — the clamp must live in the shipped restore path
# ═══════════════════════════════════════════════════════════════════════

def test_restore_clamp_present_in_main_js():
    assert RESTORE_CLAMP in _restore_fn(), (
        "runtime/sections/main.js restoreConversationSettingsToComposer lost the studio⟺project "
        "clamp — a stored 'studio' with no projectPath would restore as Studio"
    )
