"""Fourth-generation structure specs: heredocs, substitutions, shell input.

Incident (logs 2026-10-26, root conv mvl63xs9yz2nyl): the third-generation
guard split command strings with an ad-hoc scanner that had no notion of
heredocs outside one admission shape, no quote tracking inside ``$(…)``, and
no nested ``$(`` inside ``$((…))`` — and turned every parse failure into a
refusal regardless of whether the fragment could run a delete:

  * 03:00:52 ``BLOCKED catastrophic delete of 'echo "$i rc=$? $(( $(date
    +%s)-s ))s $(t' (reason=unproven)`` for a scoped ``rm -rf /tmp/tl/…`` in
    a test-timing loop — the operand never contained a delete;
  * 06:27:46 ``BLOCKED catastrophic delete of "bundle's moduleImports, and
    prelude serv"`` for a ``git commit -F- <<'EOF'`` run in background: the
    subshell wrapper kept the heredoc body visible, its apostrophe desynced
    the splitter, and commit-message prose was judged as command words;
  * a swarm agent's read-only ``git diff -- . ':(exclude)frontend/src/i18n/
    generated'`` chain was refused for the same reason.

The fourth generation replaces the ad-hoc splitter with a bounded shell
structure parser (``command_structure.py``). Heredoc bodies are data; Git
pathspec magic inside a quoted word is data; command/process substitution
bodies, ( … ) groups, stdin heredocs of a shell (``bash <<'EOF'``,
``cat <<'EOF' | sh``) and ``sh -c`` strings are judged as commands. Unparseable
text refuses only when it could run rm/rmdir/unlink.

These cases fail on the third-generation guard: the allowed rows are refused
as "unbalanced quoting"/"unproven", and every blocked row is allowed (the
ad-hoc splitter judged each ( … ) segment with a fresh state fork, so
``( cd / && rm -rf mnt )`` — every background command's shape — escaped the
cd rule, and substitution bodies were never parsed at all).
"""

from __future__ import annotations

import subprocess

import pytest

from lib.project_mod.command_analysis import (
    _catastrophic_delete_detail,
    _is_catastrophic_delete,
)

pytestmark = pytest.mark.unit

_RM = chr(114) + chr(109)
_WS = '/tmp/tofu_delguard_ws/sub'


def _background_wrap(command):
    """The exact shape the background runner hands the executor-side guard."""
    from lib.tasks_pkg.background_commands import _wrap_for_record
    paths = {'stdout': '/tmp/tofu-dg-bg/stdout.log',
             'stderr': '/tmp/tofu-dg-bg/stderr.log',
             'exit': '/tmp/tofu-dg-bg/exit'}
    return _wrap_for_record(command, paths)


# ═══════════════════════════════════════════════════════════════════════════
#  Logged false positives (2026-10-26): the original commands, verbatim
# ═══════════════════════════════════════════════════════════════════════════

class TestLoggedFalsePositives:
    def test_03_00_52_timing_loop_with_arithmetic_and_substitution(self):
        # Verbatim command of the 03:00:52 block (worker-c transcript).
        cmd = (
            'export TOFU_TEST_BUNDLE_CACHE=/tmp/tl/bcache2; '
            f'{_RM} -rf /tmp/tl/bcache2; '
            'F="tests/meta/test_hardware_never_limits_work.py '
            'tests/meta/test_nc_guard_belt.py"; '
            'for i in cold warm; do s=$(date +%s); timeout 1500 python '
            '-m pytest -q -p no:cacheprovider $F > /tmp/tl/v_$i.txt 2>&1; '
            'echo "$i rc=$? $(( $(date +%s)-s ))s $(tail -1 /tmp/tl/v_$i.txt)"; '
            'done; ls /tmp/tl/bcache2 | wc -l')
        assert _is_catastrophic_delete(cmd, cwd=_WS) is None
        # The exact shape the executor judged in the incident: background wrap.
        assert _is_catastrophic_delete(_background_wrap(cmd), cwd=_WS) is None

    def test_06_27_46_backgrounded_heredoc_commit(self):
        # Verbatim command of the 06:27:46 block (worker-f transcript), as the
        # background runner wraps it: message apostrophes are heredoc DATA.
        cmd = (
            "git add tests/_runtime_sections.py && git diff --cached --numstat "
            "&& git commit -q -F- <<'EOF'\n"
            "tests: compose the typed owners a raw retained section imports\n"
            "\n"
            "through the composed runtime's ESM header: the prelude's named "
            "imports, a lazy\n"
            "bundle's moduleImports, and prelude service constants such as\n"
            "EOF\n"
            "git log --oneline -1; comm -13 <(grep '^FAILED' /tmp/a.txt | "
            "awk '{print $2}' | sort) <(grep '^FAILED' /tmp/b.txt | "
            "awk '{print $2}' | sort)")
        assert _is_catastrophic_delete(cmd, cwd=_WS) is None
        assert _is_catastrophic_delete(_background_wrap(cmd), cwd=_WS) is None

    def test_07_52_16_heredoc_inside_substitution(self):
        # Verbatim command of the 07:52:16 pre-hook block (root transcript).
        cmd = (
            "sed -n '20,46p' tests/meta/test_style_composition.py; "
            "echo \"== sections over 100KiB\"; python3 - <<'PY'\n"
            "import json\nfrom pathlib import Path\nprint(1)\nPY\n"
            "echo \"== who grew it\"; git --no-pager log --oneline -5 "
            "--format='%h %ad %s' --date=short -- $(python3 - <<'PY'\n"
            "import json\nprint('x')\nPY\n)")
        assert _is_catastrophic_delete(cmd, cwd=_WS) is None
        assert _is_catastrophic_delete(cmd) is None

    @pytest.mark.parametrize('pathspec', [
        "':(exclude)frontend/src/i18n/generated'",
        "':(exclude,glob)frontend/**'",
        "':!frontend/src/i18n/generated'",
        "'::^static'",
        "':(top,literal)src'",
    ])
    def test_git_pathspec_magic_is_data(self, pathspec):
        # A swarm agent's read-only `git diff` with ':(exclude)…' pathspecs
        # was flagged (04:24:26). No quoting form of the magic is a delete.
        cmd = (f'git diff --stat HEAD -- . {pathspec}; '
               f'git diff 1e7b77ed5~1 1e7b77ed5 -- {pathspec}')
        assert _is_catastrophic_delete(cmd, cwd=_WS) is None

    @pytest.mark.parametrize('tail', [
        'echo "elapsed=$(( $(date +%s) - start ))s"',
        'printf "v=%s" "$(grep -c \'<\'line\' "$f")"',  # quoted << inside $()
        'n=$(git grep -l x | tr \'\\n\' \' \'); sed -n "${n},$((n+25))p" f',
        'echo "$(python3 -c "import json; print(json.load(open(\'/x\'))[\'k\'])")"',
    ])
    def test_substitution_shapes_are_parsed_not_refused(self, tail):
        # Arithmetic with a nested $(, a quoted `<<` pattern inside $(…),
        # arithmetic inside ${} expansion, and nested quotes in a substitution
        # all broke the third-generation scanner in logged commands.
        assert _is_catastrophic_delete(tail, cwd=_WS) is None


# ═══════════════════════════════════════════════════════════════════════════
#  Heredoc bodies are data; only shell-fed bodies are judged
# ═══════════════════════════════════════════════════════════════════════════

class TestHeredocBodies:
    def test_commit_message_with_apostrophes_and_parens_is_data(self):
        cmd = ("git commit -q -F- <<'EOF'\n"
               "fix: bundle's $(props) don't leak `state` (see #1)\n"
               "EOF\ngit log --oneline -1")
        assert _is_catastrophic_delete(cmd, cwd=_WS) is None

    def test_python_heredoc_body_with_rm_text_is_data(self):
        cmd = ("python3 - <<'EOF'\n"
               f"print('{_RM} -rf /')\n"
               "EOF\n")
        assert _is_catastrophic_delete(cmd, cwd=_WS) is None

    def test_heredoc_body_of_shell_stdin_is_judged(self):
        cmd = (f"bash <<'EOF'\n{_RM} -rf /\nEOF")
        assert _is_catastrophic_delete(cmd, cwd=_WS) is not None

    def test_heredoc_piped_through_cat_into_shell_is_judged(self):
        cmd = (f"cat <<'EOF' | sh\n{_RM} -rf /mnt\nEOF")
        assert _is_catastrophic_delete(cmd, cwd=_WS) is not None

    def test_scoped_delete_in_shell_heredoc_is_allowed(self):
        cmd = (f"bash <<'EOF'\n{_RM} -rf /tmp/tofu-delguard/scoped\nEOF")
        assert _is_catastrophic_delete(cmd, cwd=_WS) is None

    def test_expanding_heredoc_substitution_is_judged(self):
        # An unquoted delimiter expands $(…) in the outer shell even when the
        # consumer is cat.
        cmd = f"cat <<EOF\n$({_RM} -rf /)\nEOF"
        assert _is_catastrophic_delete(cmd, cwd=_WS) is not None

    def test_quoted_heredoc_child_shell_sees_no_unexported_binding(self):
        # `D` is not exported, so the child expands $D empty → /; refuse.
        cmd = (f"D=/tmp/tofu-delguard; sh <<'EOF'\n{_RM} -rf $D/\nEOF")
        assert _is_catastrophic_delete(cmd, cwd=_WS) is not None

    def test_shell_c_string_is_judged(self):
        assert _is_catastrophic_delete(
            f"sh -c '{_RM} -rf /mnt'", cwd=_WS) is not None
        assert _is_catastrophic_delete(
            f'bash -c "{_RM} -rf /"', cwd=_WS) is not None
        assert _is_catastrophic_delete(
            f"bash -c '{_RM} -rf /tmp/tofu-delguard/scoped'", cwd=_WS) is None


# ═══════════════════════════════════════════════════════════════════════════
#  True refusals that survive every syntactic position
# ═══════════════════════════════════════════════════════════════════════════

class TestTrueRefusals:
    @pytest.mark.parametrize('shape', [
        f'{_RM} -rf /mnt',
        f'({ _RM} -rf /mnt )',
        f'echo "$({_RM} -rf /)"',
        f'x=$({_RM} -rf /mnt)',
        f'echo `{_RM} -rf /`',
        f'diff <(cat a) <(true; {_RM} -rf /)',
        f'case x in x) {_RM} -rf /;; esac',
        f'if [[ -n x ]]; then {_RM} -rf /; fi',
        f'for f in a; do {_RM} -rf /; done | cat',
        f'{_RM} -rf / & wait',
        f'g() {{ {_RM} -rf /; }}; g',
        f'nice -n 19 {_RM} -rf /',
        f'\\{_RM} -rf /',
        f'timeout 5 bash -c "{_RM} -rf /"',
    ])
    def test_root_delete_blocked_in_every_shape(self, shape):
        assert _is_catastrophic_delete(shape, cwd=_WS) is not None, shape
        assert _is_catastrophic_delete(shape) is not None, shape
        assert _is_catastrophic_delete(_background_wrap(shape), cwd=_WS) is not None

    def test_subshell_tracks_its_own_cd(self):
        # The third-generation guard forked per segment, so the `cd` never
        # reached the delete: every background command shaped like this ran.
        assert _is_catastrophic_delete(f'( cd / && {_RM} -rf mnt )', cwd=_WS) is not None
        assert _is_catastrophic_delete(f'( D=/mnt; {_RM} -rf $D )', cwd=_WS) is not None
        assert _is_catastrophic_delete(
            _background_wrap(f'cd / && {_RM} -rf mnt'), cwd=_WS) is not None

    def test_subshell_scoped_delete_still_allowed(self):
        assert _is_catastrophic_delete(
            f'( D=/tmp/tofu-delguard; {_RM} -rf $D/build )', cwd=_WS) is None
        assert _is_catastrophic_delete(
            f'( cd /tmp/tofu-delguard && {_RM} -rf build )', cwd=_WS) is None

    def test_pipeline_background_scoping_is_not_shared_state(self):
        # `D=/mnt | cat` binds D in a pipeline subshell only: under both bash
        # and a POSIX shell the outer $D expands EMPTY, so "$D/x" resolves to
        # /x (a top-level delete) while a bare "$D" deletes nothing.
        assert _is_catastrophic_delete(
            f'D=/mnt | cat; {_RM} -rf "$D/x"', cwd=_WS) is not None
        assert _is_catastrophic_delete(
            f'D=/mnt | cat; {_RM} -rf "$D/"', cwd=_WS) is not None
        assert _is_catastrophic_delete(
            f'D=/mnt | cat; {_RM} -rf "$D"', cwd=_WS) is None
        assert _is_catastrophic_delete(
            f'D=/mnt & wait; {_RM} -rf "$D/x"', cwd=_WS) is not None
        assert _is_catastrophic_delete(
            f'D=/mnt & wait; {_RM} -rf "$D/"', cwd=_WS) is not None

    def test_arithmetic_command_runs_under_posix_shell(self):
        # bash evaluates `(( … ))` as arithmetic; dash reads `( ( … ) )` as
        # nested subshells and RUNS the words. The guard judges both readings.
        assert _is_catastrophic_delete(f'(({ _RM} -rf / ))', cwd=_WS) is not None
        assert _is_catastrophic_delete(f'$(({ _RM} -rf / ))', cwd=_WS) is not None

    def test_bash_only_quoting_cannot_hide_an_operand(self):
        # bash decodes $'\x2f' to `/` and $"…" like "…"; both are opaque or
        # plain text to the guard, never a hidden deep path.
        assert _is_catastrophic_delete(f"{_RM} -rf $'\\x2f'", cwd=_WS) is not None
        assert _is_catastrophic_delete(f'{_RM} -rf $"/"', cwd=_WS) is not None

    def test_dbracket_operators_still_split_for_posix(self):
        # `[[` is a bash builtin; dash runs `[[` as a command name and the
        # || branch. The delete must be seen under the POSIX reading.
        assert _is_catastrophic_delete(
            f'[[ -n x ]] || {_RM} -rf /', cwd=_WS) is not None
        assert _is_catastrophic_delete(
            f'[[ -n x || {_RM} -rf / ]]', cwd=_WS) is not None


# ═══════════════════════════════════════════════════════════════════════════
#  Repository metadata: the checkout holding the cwd
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def repo_tree(tmp_path):
    root = tmp_path / 'repo'
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'f.txt').write_text('x', encoding='utf-8')
    subprocess.run(['git', '-C', str(root), 'add', 'f.txt'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.email=a@b',
                    '-c', 'user.name=a', 'commit', '-qm', 'i'], check=True)
    worktree = tmp_path / 'worktree'
    subprocess.run(['git', '-C', str(root), 'worktree', 'add', '-q',
                    '--detach', str(worktree)], check=True)
    yield root, worktree
    subprocess.run(['git', '-C', str(root), 'worktree', 'remove', '--force',
                    str(worktree)], check=False)


class TestRepositoryMetadata:
    @pytest.mark.parametrize('command', [
        f'{_RM} -rf .git',
        f'{_RM} -rf .git/*',
        f'{_RM} -rf ../.git',
    ])
    def test_metadata_delete_blocked(self, repo_tree, command):
        root, _worktree = repo_tree
        cwd = str(root / 'sub') if '..' in command else str(root)
        (root / 'sub').mkdir(exist_ok=True)
        assert _is_catastrophic_delete(command, cwd=cwd) is not None

    def test_metadata_delete_blocked_from_linked_worktree(self, repo_tree):
        root, worktree = repo_tree
        # The worktree's .git file, its git dir, and the COMMON dir of the
        # main checkout are all metadata the delete must not touch.
        assert _is_catastrophic_delete(f'{_RM} -rf .git', cwd=str(worktree)) is not None
        assert _is_catastrophic_delete(
            f'{_RM} -rf {root}/.git', cwd=str(worktree)) is not None

    @pytest.mark.parametrize('command', [
        f'{_RM} -f .git/index.lock',
        f'{_RM} -rf .git/worktrees/stale',
        f'{_RM} -rf build dist',
        'find . -name "*.pyc" -delete',
    ])
    def test_scoped_deletes_still_allowed(self, repo_tree, command):
        root, _worktree = repo_tree
        assert _is_catastrophic_delete(command, cwd=str(root)) is None

    def test_unrelated_repository_metadata_is_allowed(self, repo_tree):
        root, _worktree = repo_tree
        # Outside the checkout that holds the cwd, a `repo/.git` delete keeps
        # the depth rules: refuse the shallow mount root, allow a deep one.
        assert _is_catastrophic_delete(
            f'{_RM} -rf {root}/.git', cwd='/tmp') is None


# ═══════════════════════════════════════════════════════════════════════════
#  One program view: assignments, wrappers, runners, eval and shell strings
# ═══════════════════════════════════════════════════════════════════════════

class TestSharedProgramView:
    """The delete guard reads the program view the dangerous-command guard
    reads. Before 2026-11-02 it had its own view that missed an assignment
    prefix, xargs, eval and every runner, so `FOO=1 rm -rf /` ran."""

    @pytest.mark.parametrize('shape', [
        f'FOO=1 {_RM} -rf /',
        f'A=1 B=2 {_RM} -rf /mnt',
        f'xargs {_RM} -rf /',
        f'xargs -0 -n1 {_RM} -rf /mnt',
        f'eval "{_RM} -rf /"',
        f'eval {_RM} -rf /',
        f'sudo -u x {_RM} -rf /',
        f'command -p {_RM} -rf /',
        f'setsid {_RM} -rf /',
        f"su -c '{_RM} -rf /'",
        f'runuser -u x -- {_RM} -rf /',
        f"watch '{_RM} -rf /mnt'",
        f'find / -exec {_RM} -rf /mnt \\;',
        f'ssh host {_RM} -rf /',
        f"ssh host '{_RM} -rf /mnt'",
        f'docker exec c {_RM} -rf /',
        f'kubectl exec pod -- {_RM} -rf /',
        f'flock /tmp/l {_RM} -rf /',
        f"flock /tmp/l -c '{_RM} -rf /'",
        f'chroot / {_RM} -rf /mnt',
        f'unshare {_RM} -rf /',
        f'nsenter -t 1 -m {_RM} -rf /',
        f'ionice -c 3 {_RM} -rf /',
        f'chrt 10 {_RM} -rf /',
        f'taskset 0x1 {_RM} -rf /',
        f"script -c '{_RM} -rf /'",
        f"bash -c $'{_RM} -rf /'",
        f'timeout $T {_RM} -rf /',
        f'setsid --bogus {_RM} -rf /tmp/scoped/x',
    ])
    def test_root_delete_blocked_through_the_shared_view(self, shape):
        assert _is_catastrophic_delete(shape, cwd=_WS) is not None, shape
        assert _is_catastrophic_delete(shape) is not None, shape

    @pytest.mark.parametrize('shape', [
        f'FOO=1 {_RM} -rf /tmp/tofu-delguard/scoped',
        f'find . -name "*.pyc" | xargs {_RM} -f',
        f'find . -name "*.pyc" -exec {_RM} -f {{}} +',
        f'eval "{_RM} -rf /tmp/tofu-delguard/scoped"',
        f'setsid {_RM} -rf /tmp/tofu-delguard/scoped',
        f"ssh host '{_RM} -rf /tmp/tofu-delguard/scoped'",
        f'docker exec c {_RM} -rf /tmp/tofu-delguard/scoped',
        f"su -c 'echo {_RM} -rf /'",
        'setsid --bogus python3 x.py',
        'eval "echo hi"',
        f"bash -c $'echo {_RM}'",
    ])
    def test_scoped_and_data_forms_allowed(self, shape):
        assert _is_catastrophic_delete(shape, cwd=_WS) is None, shape

    def test_variable_wrapper_option_refuses_as_unproven(self):
        detail = _catastrophic_delete_detail(f'timeout $T {_RM} -rf /', cwd=_WS)
        assert detail is not None and detail[1] == 'unproven'

    def test_eval_shares_the_shell_state(self):
        # eval runs in this shell: its cd reaches the next command.
        assert _is_catastrophic_delete(
            f'eval "cd /"; {_RM} -rf mnt', cwd=_WS) is not None


# ═══════════════════════════════════════════════════════════════════════════
#  sudo/doas: an option's value is never the program
# ═══════════════════════════════════════════════════════════════════════════

# Every sudo(8) option that takes a value, in each spelling getopt_long
# accepts (separate, attached, `--long value`, `--long=value`), clusters,
# VAR=value words before or between options (any word with `=` that does not
# start with `/`), `--`, and options whose value is optional (`-E`,
# `--preserve-env` take none unless attached). -c/-a are BSD login-class and
# auth-type options; this host's sudo 1.9.9 parses their value and exits with
# a usage error. doas(1): `-u user`, `-C config`, BSD `-a style`.
_SUDO_OPTION_FORMS = [
    '-u x', '-ux', '--user x', '--user=x',
    '-g w', '-gw', '--group w', '--group=w',
    '-D /tmp', '-D/tmp', '--chdir /tmp', '--chdir=/tmp',
    '-R /', '-R/', '--chroot /', '--chroot=/',
    '-T 5', '-T5', '--command-timeout 5', '--command-timeout=5',
    '-C 3', '-C3', '--close-from 3', '--close-from=3',
    '-h host', '-hhost', '--host host', '--host=host',
    '-p pw', '-ppw', '--prompt pw', '--prompt=pw',
    '-r role', '-rrole', '--role role', '--role=role',
    '-t type', '-ttype', '--type type', '--type=type',
    '-U y', '-Uy', '--other-user y', '--other-user=y',
    '-c staff', '-cstaff', '--login-class staff', '--login-class=staff',
    '-a passwd', '-apasswd', '--auth-type passwd', '--auth-type=passwd',
    '-nu x', '-Eu x', '-HnD /tmp', '-iu x', '-E', '--preserve-env',
    '--preserve-env=PATH -u x', 'FOO=1 -u x', '-u x FOO=1',
    'FOO=1 -D /tmp BAR=2', './x=y -u x', '-u x --', '-p -u -D /tmp',
]
_DOAS_OPTION_FORMS = [
    '-u x', '-ux', '-C /etc/doas.conf', '-C/etc/doas.conf', '-a passwd',
    '-apasswd', '-nu x', '-n -u x', '-u x --',
]


class TestPrivilegeWrapperGrammar:
    """Before 2026-11-02 the sudo/doas unwrap knew eight value letters and no
    long option, cluster or VAR=value word: `sudo -D /tmp rm -rf /` was
    judged as the program `/tmp` and ran."""

    @pytest.mark.parametrize('options', _SUDO_OPTION_FORMS)
    def test_sudo_option_values_never_hide_the_delete(self, options):
        assert _is_catastrophic_delete(f'sudo {options} {_RM} -rf /', cwd=_WS) is not None

    @pytest.mark.parametrize('options', _DOAS_OPTION_FORMS)
    def test_doas_option_values_never_hide_the_delete(self, options):
        assert _is_catastrophic_delete(f'doas {options} {_RM} -rf /', cwd=_WS) is not None

    @pytest.mark.parametrize('shape', [
        'sudo -u x ls /',
        'sudo --user x ls /',
        'sudo -D /tmp ls',
        'sudo --chdir=/tmp ls /',
        'sudo -nu x -- ls /',
        'sudo FOO=1 -u x ls /',
        f'sudo -T 5 {_RM} -rf /tmp/tofu-delguard/scoped',
        f'sudo --user x {_RM} -rf /tmp/tofu-delguard/scoped',
        f'sudo -D /tmp {_RM} -f /tmp/tofu-delguard/scoped/file',
        'doas -u x ls /',
        'doas -C /etc/doas.conf ls /',
    ])
    def test_benign_privileged_commands_are_admitted(self, shape):
        assert _is_catastrophic_delete(shape, cwd=_WS) is None, shape

    @pytest.mark.parametrize('shape', [
        f'sudo --bogus {_RM} -rf /tmp/tofu-delguard/scoped',
        f'sudo -Z x {_RM} -rf /tmp/tofu-delguard/scoped',
        f'sudo --user=x --bogus=1 {_RM} -rf /tmp/tofu-delguard/scoped',
        f'doas -Z {_RM} -rf /tmp/tofu-delguard/scoped',
    ])
    def test_unknown_options_refuse_as_unproven_when_they_could_delete(self, shape):
        detail = _catastrophic_delete_detail(shape, cwd=_WS)
        assert detail is not None and detail[1] == 'unproven', shape

    @pytest.mark.parametrize('shape', [
        'sudo --bogus python3 x.py',
        'doas -Z ls /',
    ])
    def test_unknown_options_without_a_delete_are_admitted(self, shape):
        assert _is_catastrophic_delete(shape, cwd=_WS) is None

    @pytest.mark.parametrize('shape', [
        f"sudo -s <<'EOF'\n{_RM} -rf /\nEOF",
        f"sudo -i <<'EOF'\n{_RM} -rf /mnt\nEOF",
        f"sudo -u x -s <<< '{_RM} -rf /'",
        f"cat <<'EOF' | sudo -s\n{_RM} -rf /\nEOF",
        f"doas -s <<'EOF'\n{_RM} -rf /\nEOF",
    ])
    def test_a_wrapper_without_a_command_runs_a_shell_on_stdin(self, shape):
        # sudo -s/-i and doas -s with no command run a shell reading stdin.
        assert _is_catastrophic_delete(shape, cwd=_WS) is not None, shape

    def test_stdin_data_of_that_shell_is_admitted(self):
        assert _is_catastrophic_delete(
            f"sudo -s <<'EOF'\necho {_RM} -rf /\nEOF", cwd=_WS) is None
