"""Delete-guard (``_is_catastrophic_delete``) behavioral specs, third generation.

Incident (conv mvl6adj1bcd1r1, 2026-10-24): the second-generation guard
expanded ``$VAR`` with ``os.path.expandvars`` against the SERVER process
environment. A variable assigned earlier in the SAME command
(``BAD=$R/frontend/...; rm -f $BAD/worktrees/scroll-flicker/node_modules``)
was invisible there, so the operand stayed a literal ``$BAD/...``, hit the
"unresolved -> treat as root-ish" branch, and a 13-deep scoped cleanup was
blocked as a "catastrophic delete" whose advice wrongly implied the target
was a filesystem root.

The same naive tokenization (``str.split``, no dequoting, no normpath, no
in-command ``cd`` tracking) left the rule bypassable in both directions:

  * quoting — ``rm -rf "/mnt"`` / ``rm -rf "$HOME"`` skipped the check
    (the operand started with a quote character, not ``/``);
  * dotdot — ``rm -rf /tmp/../mnt`` counted 3 textual components;
  * cwd — ``cd / && rm -rf mnt`` hid the absolute target behind a
    "relative stays inside cwd" assumption;
  * prefixes — ``then rm -rf /mnt``, ``env rm -rf /mnt``, ``{ rm -rf /; }``
    all hid the command word;
  * a separate pre-hook regex owned ``~``/``$HOME`` behind the parser's
    back, with its own quoting hole (``"$HOME"`` passed).

The third-generation guard tokenizes with the shared shell-word parser,
tracks literal in-command variable bindings and literal ``cd`` hops, and
judges every candidate expansion. A variable whose value is not statically
proven is evaluated as possibly-EMPTY (the Steam/``$STEAMROOT/`` bug class:
``rm -rf "$X/"`` with ``X`` empty deletes ``/``), never silently allowed.
"""

from __future__ import annotations

import pytest

from lib.project_mod.command_analysis import (
    _catastrophic_delete_detail,
    _is_catastrophic_delete,
)

pytestmark = pytest.mark.unit

_RM = chr(114) + chr(109)

# Project-shaped deep cwd: deleting it or its ancestors is never legitimate,
# deleting a deep child always is.
WS = '/tmp/tofu_delguard_ws/sub'


# ═══════════════════════════════════════════════════════════════════════════
#  The incident: shell-local variables assigned in the same command
# ═══════════════════════════════════════════════════════════════════════════

class TestInCommandBindings:
    def test_incident_command_allowed(self):
        cmd = (f'set -e; R=/mnt/your-fs BAD=$R/frontend/src/styles/.tofu; '
               f'{_RM} -f $BAD/worktrees/scroll-flicker/node_modules')
        assert _is_catastrophic_delete(cmd, cwd='/mnt/your-fs') is None

    def test_relative_binding_scoped_delete_allowed(self):
        # The cwd-less pre-execution hook of a server predating the third-
        # generation parser blocked this exact command as a root delete.
        cmd = ('set -e; d=frontend/src/core; git show HEAD:$d/conversation-sync.ts '
               f'> $d/zz-baseline-sync.ts; {_RM} -f $d/zz-baseline-sync.ts '
               '$d/zz-baseline-sync.test.ts; git status --short')
        assert _is_catastrophic_delete(cmd) is None
        assert _is_catastrophic_delete(cmd, cwd=WS) is None
        assert _is_catastrophic_delete(f'{_RM} -rf $d/') is not None

    def test_literal_binding_chain_allowed(self):
        assert _is_catastrophic_delete(
            f'D=/tmp/scoped-x; E=$D/inner; {_RM} -rf "$E"', cwd=WS) is None

    def test_binding_to_shallow_dir_blocked(self):
        assert _is_catastrophic_delete(f'D=/tmp; {_RM} -rf $D', cwd=WS) is not None

    def test_binding_to_root_blocked(self):
        assert _is_catastrophic_delete(f'D=/; {_RM} -rf "$D"', cwd=WS) is not None

    def test_export_binding_blocked(self):
        assert _is_catastrophic_delete(
            f'export D=/mnt; {_RM} -rf $D', cwd=WS) is not None

    def test_readonly_binding_blocked(self):
        assert _is_catastrophic_delete(
            f'readonly D=/home; {_RM} -rf $D', cwd=WS) is not None

    def test_prefix_assignment_does_not_bind(self, monkeypatch):
        # `X=/mnt rm -rf $X` expands $X BEFORE the prefix applies, so with X
        # unset this is `rm -rf` with no operand — not a delete of /mnt.
        monkeypatch.delenv('X', raising=False)
        assert _is_catastrophic_delete(f'X=/mnt {_RM} -rf $X', cwd=WS) is None

    def test_conditional_binding_adds_empty_candidate(self):
        # `false || D=/mnt` may never run: both /mnt and the empty collapse
        # must be judged; /mnt refuses the command.
        assert _is_catastrophic_delete(
            f'false || D=/mnt; {_RM} -rf $D', cwd=WS) is not None

    def test_subshell_binding_does_not_escape(self):
        assert _is_catastrophic_delete(
            f'(D=/tmp/scoped-x); {_RM} -rf $D/', cwd=WS) is not None

    def test_pwd_substitution_uses_command_cwd(self):
        assert _is_catastrophic_delete(
            f'R=$(pwd); {_RM} -rf "$R/build/out"', cwd=WS) is None

    def test_pwd_variable_uses_command_cwd(self):
        assert _is_catastrophic_delete(
            f'{_RM} -rf "$PWD/build"', cwd=WS) is None

    def test_mktemp_binding_allowed(self):
        assert _is_catastrophic_delete(
            f'T=$(mktemp -d); {_RM} -rf "$T"', cwd=WS) is None

    def test_mktemp_binding_after_guard_allowed(self):
        assert _is_catastrophic_delete(
            f'T=$(mktemp -d) || exit 1; {_RM} -rf "$T/"', cwd=WS) is None

    def test_dynamic_binding_empty_collapse_root_blocked(self):
        # D=$(...) could come back empty: `rm -rf $D/` is then `rm -rf /`.
        assert _is_catastrophic_delete(
            f'D=$(some_cmd); {_RM} -rf $D/', cwd=WS) is not None

    def test_dynamic_binding_deep_suffix_allowed(self):
        # Even empty, the skeleton keeps two components.
        assert _is_catastrophic_delete(
            f'D=$(some_cmd); {_RM} -rf $D/build/out', cwd=WS) is None

    def test_dynamic_binding_bare_allowed(self):
        # An empty D makes this `rm -rf ""`/`rm -rf` — no operand, no delete.
        assert _is_catastrophic_delete(
            f'D=$(some_cmd); {_RM} -rf "$D"', cwd=WS) is None

    def test_unset_var_empty_collapse_home_suffix_blocked(self):
        assert _is_catastrophic_delete(
            f'{_RM} -rf "$TOFU_DELGUARD_UNSET/"', cwd=WS) is not None

    def test_unset_var_deep_suffix_allowed(self):
        assert _is_catastrophic_delete(
            f'{_RM} -rf "$TOFU_DELGUARD_UNSET/build/out"', cwd=WS) is None

    def test_env_var_resolves(self, monkeypatch):
        monkeypatch.setenv('TOFU_DELGUARD_DIR', '/tmp/tofu-delguard-env')
        assert _is_catastrophic_delete(
            f'{_RM} -rf "$TOFU_DELGUARD_DIR"', cwd=WS) is None

    def test_env_var_shallow_blocked(self, monkeypatch):
        monkeypatch.setenv('TOFU_DELGUARD_DIR', '/mnt')
        assert _is_catastrophic_delete(
            f'{_RM} -rf "$TOFU_DELGUARD_DIR"', cwd=WS) is not None


# ═══════════════════════════════════════════════════════════════════════════
#  Bypasses the naive tokenizer missed
# ═══════════════════════════════════════════════════════════════════════════

class TestTokenizationBypasses:
    @pytest.mark.parametrize('command', [
        f'{_RM} -rf "/mnt"',
        f"{_RM} -rf '/mnt'",
        f'{_RM} -rf /"mnt"',
        f'{_RM} -rf /tmp/../mnt',
        f'{_RM} -rf /./mnt',
        f'{_RM} -rf /mnt/.',
        f'if true; then {_RM} -rf /mnt; fi',
        f'for d in a b; do {_RM} -rf /mnt; done',
        f'env {_RM} -rf /mnt',
        f'command {_RM} -rf /mnt',
        f'{{ {_RM} -rf /mnt; }}',
        f'! {_RM} -rf /mnt',
        f'nohup {_RM} -rf /mnt',
        f'timeout 5 {_RM} -rf /mnt',
        f'{_RM} -rf /*',
        f'{_RM} -rf /mnt/*',
        f'echo ok & {_RM} -rf /mnt',
    ])
    def test_blocked(self, command):
        assert _is_catastrophic_delete(command, cwd=WS) is not None, command

    @pytest.mark.parametrize('command', [
        f'{_RM} -rf /tmp/build-*/node_modules',   # anchored glob, not catch-all
        f'{_RM} -rf "/tmp/scoped dir/x"',         # quoted deep path
        f'echo "{{ {_RM} -rf /mnt; }}"',          # quoted mention stays data
        f'printf "%s" "if x; then {_RM} -rf /mnt; fi"',
    ])
    def test_allowed(self, command):
        assert _is_catastrophic_delete(command, cwd=WS) is None, command

    def test_redirect_target_is_not_an_operand(self):
        assert _is_catastrophic_delete(
            f'{_RM} -rf build 2>/dev/null', cwd=WS) is None

    def test_end_of_options_marker(self):
        assert _is_catastrophic_delete(f'{_RM} -rf -- /mnt', cwd=WS) is not None
        assert _is_catastrophic_delete(
            f'{_RM} -rf -- /tmp/scoped-x', cwd=WS) is None

    def test_command_substitution_operand_blocked_only_when_shallow(self):
        # $(mktemp -d) directly as the operand: dynamic but never shallow.
        assert _is_catastrophic_delete(f'{_RM} -rf $(mktemp -d)', cwd=WS) is None
        # An arbitrary substitution could print anything — the empty collapse
        # of `$(x)/` is `/`.
        assert _is_catastrophic_delete(f'{_RM} -rf $(some_cmd)/', cwd=WS) is not None

    def test_heredoc_data_body_not_a_command(self):
        cmd = (f"cat > /tmp/tofu_delguard_hint.sh <<'EOF'\n"
               f"{_RM} -rf /\n"
               f"EOF\n"
               f"true")
        assert _is_catastrophic_delete(cmd, cwd=WS) is None


# ═══════════════════════════════════════════════════════════════════════════
#  In-command cd tracking
# ═══════════════════════════════════════════════════════════════════════════

class TestCdTracking:
    def test_cd_root_then_relative_delete_blocked(self):
        assert _is_catastrophic_delete(f'cd / && {_RM} -rf mnt', cwd=WS) is not None

    def test_cd_tmp_then_deep_relative_allowed(self):
        assert _is_catastrophic_delete(
            f'cd /tmp && {_RM} -rf scoped-x', cwd=WS) is None

    def test_cd_tmp_then_dotdot_blocked(self):
        assert _is_catastrophic_delete(
            f'cd /tmp && {_RM} -rf ../mnt', cwd=WS) is not None

    def test_cd_failure_fallback_still_judged(self):
        # `cd / ; ...` separated by ';' runs the delete from BOTH cwds.
        assert _is_catastrophic_delete(
            f'cd /; {_RM} -rf mnt', cwd=WS) is not None

    def test_cd_subshell_does_not_leak(self):
        assert _is_catastrophic_delete(
            f'(cd /); {_RM} -rf build/out', cwd=WS) is None

    def test_dotdot_climb_from_workspace_blocked(self):
        assert _is_catastrophic_delete(
            f'{_RM} -rf ../../../../../../../../..', cwd=WS) is not None


# ═══════════════════════════════════════════════════════════════════════════
#  Protected anchors: home root and the workspace itself
# ═══════════════════════════════════════════════════════════════════════════

class TestProtectedAnchors:
    def test_home_root_forms_blocked(self):
        assert _is_catastrophic_delete(f'{_RM} -rf ~', cwd=WS) is not None
        assert _is_catastrophic_delete(f'{_RM} -rf ~/', cwd=WS) is not None
        assert _is_catastrophic_delete(f'{_RM} -rf $HOME', cwd=WS) is not None
        assert _is_catastrophic_delete(f'{_RM} -rf "$HOME"', cwd=WS) is not None
        assert _is_catastrophic_delete(f'{_RM} -rf ${{HOME}}', cwd=WS) is not None

    def test_home_subpath_allowed(self):
        assert _is_catastrophic_delete(f'{_RM} -rf ~/old_build', cwd=WS) is None
        assert _is_catastrophic_delete(f'{_RM} -rf "$HOME/old_build"', cwd=WS) is None

    def test_workspace_root_delete_blocked(self):
        assert _is_catastrophic_delete(f'{_RM} -rf /tmp/tofu_delguard_ws', cwd=WS) is not None

    def test_workspace_cwd_delete_blocked(self):
        assert _is_catastrophic_delete(f'{_RM} -rf "$PWD"', cwd=WS) is not None
        assert _is_catastrophic_delete(f'{_RM} -rf .', cwd=WS) is not None

    def test_workspace_deep_child_allowed(self):
        assert _is_catastrophic_delete(
            f'{_RM} -rf /tmp/tofu_delguard_ws/sub/build', cwd=WS) is None

    def test_cwd_less_caller_keeps_absolute_rules(self):
        # The pre-hook runs without a cwd: absolute shapes still judge,
        # relative ones defer to the executor's cwd-aware pass.
        assert _is_catastrophic_delete(f'{_RM} -rf /mnt') is not None
        assert _is_catastrophic_delete(f'{_RM} -rf ../../../../../../..') is None


# ═══════════════════════════════════════════════════════════════════════════
#  Loop bindings
# ═══════════════════════════════════════════════════════════════════════════

class TestLoopBindings:
    def test_literal_loop_values_allowed(self):
        assert _is_catastrophic_delete(
            f'for d in /tmp/scoped-a /tmp/scoped-b; do {_RM} -rf $d; done',
            cwd=WS) is None

    def test_literal_loop_value_blocked(self):
        assert _is_catastrophic_delete(
            f'for d in /mnt /tmp/scoped-b; do {_RM} -rf $d; done',
            cwd=WS) is not None


# ═══════════════════════════════════════════════════════════════════════════
#  Reasons drive honest messages
# ═══════════════════════════════════════════════════════════════════════════

class TestRefusalDetail:
    def test_shallow_reason(self):
        detail = _catastrophic_delete_detail(f'{_RM} -rf /mnt', cwd=WS)
        assert detail is not None
        assert detail[1] == 'shallow'

    def test_unproven_reason_names_variable(self):
        detail = _catastrophic_delete_detail(
            f'D=$(some_cmd); {_RM} -rf $D/', cwd=WS)
        assert detail is not None
        assert detail[1] == 'unproven'
        assert 'D' in detail[2]

    def test_home_reason(self):
        detail = _catastrophic_delete_detail(f'{_RM} -rf "$HOME"', cwd=WS)
        assert detail is not None
        assert detail[1] == 'home'

    def test_workspace_reason(self):
        detail = _catastrophic_delete_detail(
            f'{_RM} -rf /tmp/tofu_delguard_ws', cwd=WS)
        assert detail is not None
        assert detail[1] == 'workspace'
