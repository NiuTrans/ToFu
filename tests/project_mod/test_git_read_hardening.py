"""Hardened Git reads and literal write targets, without workspace admission.

``_lease_free_git_segment`` is the hardened inspection grammar the runner uses
to execute literal Git reads without refreshing the shared index; the literal
target parsers feed file history (undo) and isolated-workspace refusals. Tool
calls take no workspace lease (docs/runbooks/GIT_WORKSPACE_GUARD.md), so none
of these decides admission any more.
"""
import shlex
import subprocess

import pytest

from lib.project_mod.command_analysis import (
    _extract_write_targets, _git_guard_classify, _lease_free_git_segment,
    command_segment_literal_targets, command_segment_targets,
)
from lib.project_mod.run_command import tool_run_command

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


def render(template, checkouts):
    current, peer, source, target = checkouts
    return template.format(current=shlex.quote(str(current)), peer=shlex.quote(str(peer)),
                           source=shlex.quote(str(source)), target=shlex.quote(str(target)))


def test_housekeeping_overrides_are_dropped_from_hardened_reads():
    hardened = _lease_free_git_segment('git -c gc.auto=0 -c maintenance.auto=false log --oneline -1')
    # No inspection runs auto-maintenance, so the canonical form needs no override.
    assert hardened is not None and 'gc.auto' not in hardened
    assert _lease_free_git_segment(hardened) == hardened


@pytest.mark.parametrize('command', [
    'git symbolic-ref --short HEAD', 'git symbolic-ref -q HEAD',
    'git cherry -v HEAD', 'git cherry --abbrev=7 HEAD HEAD',
    'git hash-object --no-filters input.txt', 'git hash-object --stdin',
    'git merge-base --is-ancestor HEAD HEAD',
    "git for-each-ref --format='%(refname)' refs/heads",
    'git check-ignore input.txt', 'git check-ignore -v --no-index input.txt',
    'git --version', 'git version', 'git version --build-options',
    'git -c maintenance.auto=false -c gc.auto=0 status --short',
    'git -c gc.auto=0 -c maintenance.auto=false grep -n needle -- input.txt',
])
def test_reading_plumbing_is_hardened_idempotently(command):
    hardened = _lease_free_git_segment(command)
    assert hardened is not None and _lease_free_git_segment(hardened) == hardened


@pytest.mark.parametrize('command', [
    'git symbolic-ref HEAD refs/heads/other', 'git symbolic-ref -d HEAD',
    'git symbolic-ref -m reason HEAD refs/heads/other',
    'git hash-object -w input.txt', 'git hash-object --stdin -w',
    # A path input runs configured clean/process filter drivers, which no
    # hardening flag disables (git-lfs clean stores the object it hashes).
    'git hash-object input.txt', 'git hash-object --stdin --path=input.txt',
    'git hash-object --stdin-paths',
    'git cherry --future-option HEAD',
    'git check-ignore --future-option input.txt',
    'git -c core.fsmonitor=evil check-ignore input.txt',
    'git -c alias.check-ignore=evil check-ignore input.txt',
    'git check-ignore "$DYNAMIC"',
    'git log --show-signature', 'git diff --ext-diff',
    'git version --future-option', 'git --version extra',
    # Only the exact disabling values are harmless.
    'git -c gc.auto=1 log --oneline -1', 'git -c maintenance.auto=true log --oneline -1',
])
def test_writing_filtering_or_helper_forms_are_never_hardened(command):
    assert _lease_free_git_segment(command) is None


def test_check_ignore_executes_hardened(checkouts):
    current, peer, _, target = checkouts
    (peer / '.gitignore').write_text('*.txt\n')
    command = 'git -C ' + shlex.quote(str(peer)) + ' check-ignore -- ' + shlex.quote(target.name)
    output = tool_run_command(str(current), command)
    assert '[exit code: 0]' in output and target.name in output
    assert '--no-optional-locks' in output


@pytest.mark.parametrize('verb', ['cherry-pick', 'revert'])
@pytest.mark.parametrize('mainline', ['-m 1', '-m2', '--mainline 1', '--mainline=3'])
def test_mainline_is_ordinary_write_never_readonly(verb, mainline):
    command = f'git {verb} {mainline} HEAD'
    assert _git_guard_classify(command, require_known_segments=True) == 'safe_write'
    assert _lease_free_git_segment(command) is None


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
def test_history_and_isolation_parsers_share_literal_targets(checkouts, template, expected):
    _current, peer, _source, target = checkouts
    command = render(template, checkouts)
    path = {'peer': str(peer), 'target': str(target)}[expected]
    parsed = command_segment_targets(command)
    assert parsed.complete and path in parsed.targets
    assert path in _extract_write_targets(command)
    assert path in command_segment_literal_targets(command)


@pytest.mark.parametrize('template', [
    'cargo test > {target}',
    'cargo test 2>>{target}',
    'cargo test "$FILTER" > {target} 2>&1',
    'env MODE=test cargo test > {target}',
])
def test_opaque_command_keeps_its_literal_redirect_target(checkouts, template):
    _, _, _, target = checkouts
    command = render(template, checkouts)
    assert _extract_write_targets(command) is None
    assert not command_segment_targets(command).complete
    assert command_segment_literal_targets(command) == (str(target),)


