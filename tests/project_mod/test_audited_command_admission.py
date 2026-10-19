"""Real utility writes must contend with a writer in their target checkout.

Authority: command_analysis's source-token/target parser, consumed by both
command admission wrappers. No command interception or mock mutation is used.
"""
import shlex
import subprocess
import threading

import pytest

from lib.project_mod.command_analysis import (
    _extract_write_targets, _git_guard_classify, _lease_free_git_segment,
    command_segment_literal_targets, command_segment_targets,
)
from lib.project_mod.run_command import (
    _with_command_writer, command_write_lease_claims, tool_run_command,
)
from lib.project_mod.workspace_lease import workspace_file_write_lease

pytestmark = pytest.mark.unit


@pytest.fixture
def checkouts(tmp_path):
    current = tmp_path / 'current checkout'
    peer = tmp_path / 'peer $literal checkout'
    for root in (current, peer):
        root.mkdir()
        subprocess.run(['git', 'init', '-q', str(root)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    name = 'victim $literal data.txt'
    source, target = current / name, peer / name
    source.write_text('z\na\n')
    target.write_text('b\na\n')
    return current, peer, source, target


@_with_command_writer
def actual_shell(base, command):
    return subprocess.run(command, shell=True, cwd=base, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


WRITES = [
    'sort -ro {target} {source}',
    'sort -rno{target} {source}',
    'sort --output {target} {source}',
    'sort --output={target} {source}',
    'cp -t {peer} {source}',
    'cp --target-directory {peer} {source}',
    'cp --target-directory={peer} {source}',
    'cp -vt{peer} {source}',
    'mv -t {peer} {source}',
    'mv --target-directory={peer} {source}',
    "sed -i 's/b/replaced/' {target}",
    "sed -ri 's/b/replaced/' {target}",
    "sed --in-place -e 's/b/replaced/' {target}",
    "gawk -i inplace '{{print toupper($0)}}' {target}",
    "gawk --include=inplace '{{print toupper($0)}}' {target}",
    'find {peer} -name *.txt -delete',

    'find -O3 {peer} -type f -delete',
    'find -- {peer} -type f -delete',
    'find {peer} -type f -exec rm {{}} \\;',
    "find . -name '*.txt' -exec cp {{}} {target} \\;",
    "find {peer} -type f -exec sed -i 's/b/replaced/' {{}} \\;",
    'find {peer} -type f -execdir rm {{}} +',
]


def render(template, checkouts):
    current, peer, source, target = checkouts
    return template.format(current=shlex.quote(str(current)), peer=shlex.quote(str(peer)),
                           source=shlex.quote(str(source)), target=shlex.quote(str(target)))


@pytest.mark.parametrize('template', WRITES)
@pytest.mark.parametrize('runner', [actual_shell, tool_run_command], ids=['outer-wrapper', 'runner'])
def test_actual_peer_writes_wait_for_concurrent_writer(checkouts, monkeypatch, template, runner):
    current, peer, source, target = checkouts
    command = render(template, checkouts)
    source_before, target_before = source.read_bytes(), target.read_bytes()
    ready, release = threading.Event(), threading.Event()
    errors = []

    def writer():
        try:
            with workspace_file_write_lease(target, timeout=1):
                ready.set()
                if not release.wait(10):
                    raise AssertionError('Writer release was not signalled')
        except BaseException as error:
            errors.append(error)
            ready.set()

    holder = threading.Thread(target=writer)
    holder.start()
    try:
        assert ready.wait(2) and not errors
        monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0.03')
        refused = runner(str(current), command)
        assert isinstance(refused, str) and 'workspace-busy' in refused
        assert 'Command was not started' in refused
        assert source.read_bytes() == source_before
        assert target.read_bytes() == target_before
    finally:
        release.set()
        holder.join(2)
    assert not holder.is_alive() and not errors


def test_execdir_relative_parent_target_refuses_before_any_mutation(tmp_path):
    base, search, destination = tmp_path / 'current', tmp_path / 'sources/search', tmp_path / 'sources/destination'
    for root in (base, search, destination):
        (root / '.git').mkdir(parents=True)
    (search / 'source.txt').write_text('new')
    target = destination / 'target.txt'
    target.write_text('old')
    command = shlex.join(['find', str(search), '-maxdepth', '1', '-name', 'source.txt',
                          '-execdir', 'cp', '{}', '../destination/target.txt', ';'])
    for runner in (actual_shell, tool_run_command):
        with pytest.raises(ValueError, match='workspace-write-scope-unsupported'):
            runner(str(base), command)
        assert target.read_text() == 'old'


@pytest.mark.parametrize('action', ['-delete', '-exec cp {} target ;'])
def test_following_symlink_find_mutation_is_not_a_proven_write_scope(checkouts, action):
    current, peer, _source, target = checkouts
    command = 'find -L ' + shlex.quote(str(peer)) + ' ' + action
    with pytest.raises(ValueError, match='workspace-write-scope-unsupported'):
        command_write_lease_claims(str(current), command)
    assert target.read_text() == 'b\na\n'


@pytest.mark.parametrize('pattern', ['-delete', '-execdir', '-fprint'])
def test_find_predicate_values_are_not_mutating_actions(checkouts, pattern):
    current, peer, _source, _target = checkouts
    command = shlex.join(['find', '-L', str(peer), '-name', pattern, '-print'])
    assert command_write_lease_claims(str(current), command) == ()
    assert command_segment_targets(command).targets == ()


def test_expression_follow_mutation_refuses_symlink_escape(tmp_path):
    current, search, peer = tmp_path / 'current', tmp_path / 'search', tmp_path / 'peer'
    for root in (current, search, peer):
        (root / '.git').mkdir(parents=True)
    target = peer / 'victim'
    target.write_text('old')
    (search / 'jump').symlink_to(peer, target_is_directory=True)
    command = shlex.join(['find', str(search), '-follow', '-type', 'f', '-exec', 'touch', '{}', ';'])
    before = target.stat().st_mtime_ns
    with pytest.raises(ValueError, match='workspace-write-scope-unsupported'):
        actual_shell(str(current), command)
    assert target.stat().st_mtime_ns == before


def test_follow_word_inside_predicate_data_is_not_a_scope_refusal(checkouts):
    current, peer, _source, _target = checkouts
    command = shlex.join(['find', str(peer), '-name', '-follow', '-print'])
    assert command_write_lease_claims(str(current), command) == ()


@pytest.fixture(scope='session')
def gawk_inplace_supported():
    probe = subprocess.run(['gawk', '-i', 'inplace', 'BEGIN { exit }'], text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return probe.returncode == 0


@pytest.mark.parametrize('template', WRITES)
def test_audited_commands_really_mutate_when_admitted(checkouts, template, gawk_inplace_supported):
    if template.startswith('gawk ') and not gawk_inplace_supported:
        pytest.skip('Host gawk lacks inplace extension; real-file admission tests still run')
    current, _, _, target = checkouts
    target_before = target.read_bytes()
    executed = actual_shell(str(current), render(template, checkouts))
    assert executed.returncode == 0, executed.stderr
    assert not target.exists() or target.read_bytes() != target_before


@pytest.mark.parametrize('template, expected', [
    ('sort -ro {target} {source}', 'target'),
    ('sort -rno{target} {source}', 'target'),
    ('sort --output={target} {source}', 'target'),
    ('cp -t {peer} {source}', 'peer'),
    ('cp --target-directory={peer} {source}', 'peer'),
    ("sed -i 's/b/a/' {target}", 'target'),
    ("gawk -i inplace '{{print $0}}' {target}", 'target'),
    ('find {peer} -delete', 'peer'),
])
def test_snapshot_and_admission_share_literal_target_authority(checkouts, template, expected):
    current, peer, source, target = checkouts
    command = render(template, checkouts)
    path = {'peer': str(peer), 'target': str(target)}[expected]
    parsed = command_segment_targets(command)
    assert parsed.complete and path in parsed.targets
    assert path in _extract_write_targets(command)
    assert path in command_segment_literal_targets(command)
    assert any(claim_path == path for _, claim_path in command_write_lease_claims(str(current), command))
    assert not any(claim_path == str(source) for _, claim_path in
                   command_write_lease_claims(str(current), command))


@pytest.mark.parametrize('template', [
    'sort --future-output={target} {source}',
    'cp --future-option {source} {target}',
    'cp --future-option={peer} {source}',
    'mv --future-option {source} {peer}',
    "sed --future-option -i 's/b/a/' {target}",
    "gawk --future-option -i inplace '{{print}}' {target}",
])
def test_unknown_mutating_options_retain_peer_root(checkouts, template):
    current, peer, _, target = checkouts
    command = render(template, checkouts)
    claims = command_write_lease_claims(str(current), command)
    assert ('tree', str(current)) in claims
    assert any(scope == 'tree' and path in {str(peer), str(target)} for scope, path in claims)


@pytest.mark.parametrize('template', [
    'cp -t {peer} "$SOURCE"',
    'sort -ro {target} "$SOURCE"',
    "sed -i \"s/$PATTERN/a/\" {target}",
    'cp source {peer}/"$OUTPUT"',
    'find {peer} -name "$PATTERN" -delete',
])
def test_dynamic_other_operands_cannot_erase_known_peer_target(checkouts, template):
    current, peer, _, target = checkouts
    claims = command_write_lease_claims(str(current), render(template, checkouts))
    assert ('tree', str(current)) in claims
    assert any(scope == 'tree' and path in {str(peer), str(target)} for scope, path in claims)


@pytest.mark.parametrize('command', [
    'npm run build', 'pytest -q', 'python3 -c "print(1)"', 'make all',
    'sort -rn input.txt', 'sort --reverse input.txt',
    "sed 's/b/a/' input.txt", "find . -name '*.txt' -print",
    'git check-ignore input.txt', 'git check-ignore -v --no-index input.txt',
    "git check-ignore -- 'literal$dollar.txt'",
])
def test_plain_builds_and_bounded_reads_remain_lease_free(checkouts, command):
    current, _, _, _ = checkouts
    assert command_write_lease_claims(str(current), command) == ()


@pytest.mark.parametrize('command', [
    'git check-ignore --future-option input.txt',
    'git -c core.fsmonitor=evil check-ignore input.txt',
    'git -c alias.check-ignore=evil check-ignore input.txt',
    'git check-ignore "$DYNAMIC"',
    'git log --show-signature', 'git diff --ext-diff',
])
def test_git_helpers_and_unknown_options_cannot_borrow_read_exemption(checkouts, command):
    current, _, _, _ = checkouts
    assert _lease_free_git_segment(command) is None
    assert command_write_lease_claims(str(current), command)


def test_check_ignore_executes_hardened_during_target_writer(checkouts):
    current, peer, _, target = checkouts
    (peer / '.gitignore').write_text('*.txt\n')
    command = 'git -C ' + shlex.quote(str(peer)) + ' check-ignore -- ' + shlex.quote(target.name)
    with workspace_file_write_lease(target, timeout=1):
        output = tool_run_command(str(current), command)
    assert '[exit code: 0]' in output and target.name in output
    assert 'workspace-busy' not in output
    assert '--no-optional-locks' in output


@pytest.mark.parametrize('verb', ['cherry-pick', 'revert'])
@pytest.mark.parametrize('mainline', ['-m 1', '-m2', '--mainline 1', '--mainline=3'])
def test_mainline_is_ordinary_write_never_readonly(checkouts, verb, mainline):
    current, _, _, _ = checkouts
    command = f'git {verb} {mainline} HEAD'
    assert _git_guard_classify(command, require_known_segments=True) == 'safe_write'
    assert _lease_free_git_segment(command) is None
    assert command_write_lease_claims(str(current), command) == (('root', str(current)),)


@pytest.mark.parametrize('option', ['-T', '--temporary-directory'])
def test_sort_explicit_temporary_directory_cannot_borrow_leaf_claim(checkouts, option):
    current, peer, source, _ = checkouts
    command = f'sort {option} {shlex.quote(str(peer))} -o local-output {shlex.quote(str(source))}'
    claims = command_write_lease_claims(str(current), command)
    assert ('tree', str(peer)) in claims
    assert ('tree', str(current)) in claims
    assert not any(scope == 'file' for scope, _ in claims)


def test_static_git_scope_does_not_reinterpret_literal_dollar_path(checkouts):
    from lib.project_mod.git_command_scope import git_command_scopes
    current, peer, _, _ = checkouts
    command = 'git -C ' + shlex.quote(str(peer)) + " merge -m 'literal $message' topic"
    scope, = git_command_scopes(command, str(current), environment={})
    assert scope.writer_paths() == (str(peer),)
    assert command_write_lease_claims(str(current), command) == (('root', str(peer)),)


@pytest.mark.parametrize('template', [
    'sudo cp {source} {target}', 'doas cp {source} {target}',
    'sudo mkdir -p {peer}/created',
])
def test_privilege_wrappers_cannot_borrow_file_narrowing(checkouts, template):
    current, peer, _, target = checkouts
    claims = command_write_lease_claims(str(current), render(template, checkouts))
    assert ('tree', str(current)) in claims
    assert any(scope == 'tree' and path in {str(target), str(peer / 'created')} for scope, path in claims)
    assert not any(scope == 'file' for scope, _ in claims)


@pytest.mark.parametrize('template', [
    'cargo test > {target}',
    'cargo test 2>>{target}',
    'cargo test "$FILTER" > {target} 2>&1',
    'env MODE=test cargo test > {target}',
])
def test_opaque_snapshot_keeps_literal_admission_targets(checkouts, template):
    _, _, _, target = checkouts
    command = render(template, checkouts)
    assert _extract_write_targets(command) is None
    assert not command_segment_targets(command).complete
    assert command_segment_literal_targets(command) == (str(target),)
