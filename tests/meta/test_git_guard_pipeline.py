"""Decision tests for the Git workspace guard wiring in the tool pipeline.

Contract: docs/modules/project_integration.md "Git workspace guard".

Under test: ``lib.tasks_pkg.tool_dispatch._pipeline._approval_stops_tool_call``
with settlement / approval-prompt / receipt seams monkeypatched, so the tests
pin ONLY the guard decision:

  * safe_write            → allowed, receipt authority ``git_safe_write``
  * tree_destroying solo  → allowed, authority ``agent_git_guard_solo``
  * tree_destroying+peers → conservative presence refusal, not lock evidence
  * legacy ack retries (including expired/other conversation) → still refused
  * unknown git shapes and non-git destructive commands → existing gate
  * presence probe failure → fail-open to the existing gate
"""

from __future__ import annotations

import pytest

import lib.project_mod.git_guard as guard
import lib.tasks_pkg.tool_dispatch._approval as approval_mod
import lib.tasks_pkg.tool_dispatch._pipeline as pipeline

# Intentional references to paths that do not (and must not)
# exist in the real repo — negative-case fixture anchors.
_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/auth.py'}

pytestmark = pytest.mark.unit

ROOT = '/proj/guarded'
EXEC_CONV = 'conv-executor'


@pytest.fixture(autouse=True)
def _clean_guard_state():
    guard._reset_for_tests()
    yield
    guard._reset_for_tests()


@pytest.fixture
def harness(monkeypatch):
    """Patch settlement seams; return a recorder + invocation helper."""
    rec = {'receipts': [], 'rejections': [], 'settles': [], 'approvals': []}

    def fake_mint(task, fn_name, tc_id, fn_args, *, authority):
        rec['receipts'].append({'tc_id': tc_id, 'authority': authority,
                                'command': dict(fn_args)})
        return True

    def fake_stamp(round_entry, descriptor, *, reason='', retryable=False):
        rec['rejections'].append({'descriptor': descriptor, 'reason': reason,
                                  'retryable': retryable})
        if isinstance(round_entry, dict):
            round_entry['_rejection'] = descriptor

    def fake_settle(task, fn_name, tc_id, fn_args, rn, round_entry, content,
                    **kwargs):
        rec['settles'].append({'tc_id': tc_id, 'content': content,
                               'terminal_status': kwargs.get('terminal_status')})
        return content

    def fake_handle_approval(*args, **kwargs):
        rec['approvals'].append({'args': args, 'kwargs': kwargs})
        return False, 'denied-by-test-double'

    monkeypatch.setattr(approval_mod, 'mint_approval_receipt', fake_mint)
    monkeypatch.setattr(pipeline, 'stamp_tool_rejection', fake_stamp)
    monkeypatch.setattr(pipeline, '_settle_tool_result', fake_settle)
    monkeypatch.setattr(pipeline, '_handle_approval', fake_handle_approval)
    return rec


def _invoke(*, command, attended=True, auto_apply=True, project_path=ROOT,
            conv_id=EXEC_CONV, user_id=1):
    task = {'id': 'task-1', 'convId': conv_id, '_userId': user_id,
            'aborted': False}
    round_entry = {'toolName': 'run_command', 'toolCallId': 'call-1'}
    tool_results: dict = {}
    stopped = pipeline._approval_stops_tool_call(
        task=task, fn_name='run_command', fn_args={'command': command},
        tc_id='call-1', rn=1, round_entry=round_entry,
        confirmation_tools={'run_command'}, write_tools={'run_command'},
        attended=attended, auto_apply=auto_apply, cfg={},
        project_path=project_path, round_num=1, model='m',
        tool_results=tool_results, idempotent_tools=frozenset(), cache={},
        tid='task-1')
    return stopped, tool_results


def _peer(title='修复登录bug', current_file='lib/auth.py', conv='conv-victim'):
    return {'convId': conv, 'title': title, 'currentFile': current_file,
            'statusLabel': f'editing {current_file}' if current_file else ''}


# ── safe_write ──────────────────────────────────────────────────────

def test_safe_write_runs_with_receipt(harness):
    stopped, _ = _invoke(command='git add -A && git commit -m wip')
    assert stopped is False
    assert [r['authority'] for r in harness['receipts']] == ['git_safe_write']
    assert harness['rejections'] == [] and harness['approvals'] == []


def test_safe_write_unattended_no_longer_rejected(harness):
    # Previously: unattended + confirmation tool → hard rejection. The guard
    # now receipts safe git writes for unattended tasks too.
    stopped, _ = _invoke(command='git commit -m wip', attended=False)
    assert stopped is False
    assert [r['authority'] for r in harness['receipts']] == ['git_safe_write']


@pytest.mark.parametrize('command', [
    'git merge --no-ff topic -m "merge: reviewed work"',
    'git merge topic --message=--abort',
])
def test_merge_message_does_not_probe_presence(harness, monkeypatch, command):
    def unexpected_presence_probe(*args, **kwargs):
        pytest.fail('ordinary merge must not consult advisory presence')

    monkeypatch.setattr(guard, 'active_peers', unexpected_presence_probe)
    stopped, _ = _invoke(command=command)
    assert stopped is False
    assert [r['authority'] for r in harness['receipts']] == ['git_safe_write']
    assert harness['rejections'] == [] and harness['approvals'] == []


def test_merge_short_strategy_still_refuses_presence(harness, monkeypatch):
    monkeypatch.setattr(guard, 'active_peers',
                        lambda root, user_id, exclude_conv='': [_peer()])
    stopped, _ = _invoke(command='git merge -s ours topic -m message')
    assert stopped is True
    assert harness['rejections'][0]['descriptor']['kind'] == 'git_guard'
    assert harness['receipts'] == []


# ── tree_destroying, solo ───────────────────────────────────────────

def test_tree_destroying_solo_runs_with_solo_receipt(harness, monkeypatch):
    monkeypatch.setattr(guard, 'active_peers',
                        lambda root, user_id, exclude_conv='': [])
    stopped, _ = _invoke(command='git reset --hard HEAD~1')
    assert stopped is False
    assert [r['authority'] for r in harness['receipts']] == [
        'agent_git_guard_solo']
    assert harness['rejections'] == []


# ── tree_destroying with active presence: no ack override ──────────

def test_presence_refusal_names_peers_without_claiming_locks(harness, monkeypatch):
    monkeypatch.setattr(guard, 'active_peers',
                        lambda root, user_id, exclude_conv='': [_peer()])
    stopped, tool_results = _invoke(command='git reset --hard')
    assert stopped is True
    content, _ok = tool_results['call-1']
    assert '修复登录bug' in content
    assert 'lib/auth.py' in content
    assert 'git add -A && git commit' not in content
    assert f'{guard.ACK_ENV_VAR}=' not in content
    assert 'cannot override active project presence' in content
    assert 'Conservative project-presence policy' in content
    assert 'not confirmed writers' in content
    assert 'not evidence of an actual writer lock/lease or a file conflict' in content
    assert f'Checkout root: {ROOT}' in content
    assert 'convId=conv-victim agentId=(main)' in content
    descriptor = harness['rejections'][0]['descriptor']
    assert descriptor['kind'] == 'git_guard'
    assert descriptor['tool'] == 'run_command'
    assert descriptor['condition'] == 'active_project_presence_settled'
    assert descriptor['retryPolicy'] == {'mode': 'after_condition_change', 'automatic': False}
    assert descriptor['gitDiagnostics']['execution'] == 'not_started'
    assert descriptor['gitDiagnostics']['evidenceLevel'] == 'advisory_presence_only'
    assert descriptor['gitDiagnostics']['segments'][0]['verb'] == 'reset'
    assert harness['rejections'][0]['retryable'] is True
    assert harness['settles'][0]['terminal_status'] == 'rejected'
    # No human approval was involved anywhere.
    assert harness['approvals'] == [] and harness['receipts'] == []


@pytest.mark.parametrize('command', [
    'git push -vf origin feature',
    'git push -vd origin feature',
    'git fetch -pf origin',
    'git fetch -P origin',
    'git fetch -u origin',
])
def test_destructive_short_options_refuse_active_peers(harness, monkeypatch, command):
    monkeypatch.setattr(guard, 'active_peers',
                        lambda root, user_id, exclude_conv='': [_peer()])
    stopped, tool_results = _invoke(command=command)
    assert stopped is True
    assert tool_results['call-1'][1] is False

    reason = tool_results['call-1'][0]
    assert 'mutates Git state' in reason
    assert 'rewrites the working tree' not in reason
    assert 'would be destroyed' not in reason
    rejection = harness['rejections'][0]['descriptor']
    assert rejection['kind'] == 'git_guard'
    assert rejection['retryPolicy'] == {
        'mode': 'after_condition_change', 'automatic': False,
    }
    assert harness['receipts'] == []
    assert harness['approvals'] == []


def test_legacy_ack_retry_cannot_override_active_presence(harness, monkeypatch):
    monkeypatch.setattr(guard, 'active_peers',
                        lambda root, user_id, exclude_conv='': [_peer()])
    stopped, tool_results = _invoke(command='git reset --hard')
    assert stopped is True
    nonce = guard.issue_ack(ROOT, EXEC_CONV)

    stopped, _ = _invoke(command=f'{guard.ACK_ENV_VAR}={nonce} git reset --hard')
    assert stopped is True
    assert harness['receipts'] == []

    # Reusing a legacy token cannot change the presence decision either.
    stopped, tool_results = _invoke(
        command=f'{guard.ACK_ENV_VAR}={nonce} git reset --hard')
    assert stopped is True
    assert 'cannot override active project presence' in tool_results['call-1'][0]


def test_ack_bound_to_another_conversation_is_refused(harness, monkeypatch):
    monkeypatch.setattr(guard, 'active_peers',
                        lambda root, user_id, exclude_conv='': [_peer()])
    nonce = guard.issue_ack(ROOT, 'conv-someone-else')
    stopped, tool_results = _invoke(
        command=f'{guard.ACK_ENV_VAR}={nonce} git reset --hard')
    assert stopped is True
    assert harness['receipts'] == []


def test_expired_ack_is_refused(harness, monkeypatch):
    monkeypatch.setattr(guard, 'active_peers',
                        lambda root, user_id, exclude_conv='': [_peer()])
    nonce = guard.issue_ack(ROOT, EXEC_CONV)
    past = guard.time.monotonic() - (guard.ACK_TTL_SEC + 1)
    with guard._lock:
        guard._acks[(guard.os.path.abspath(ROOT), nonce)] = (past, EXEC_CONV)
    stopped, _ = _invoke(command=f'{guard.ACK_ENV_VAR}={nonce} git reset --hard')
    assert stopped is True
    assert harness['receipts'] == []


def test_invalid_ack_without_peers_still_runs_solo(harness, monkeypatch):
    # A stale token with no active presence must not trap the agent.
    monkeypatch.setattr(guard, 'active_peers',
                        lambda root, user_id, exclude_conv='': [])
    stopped, _ = _invoke(
        command=f'{guard.ACK_ENV_VAR}=deadbeef git reset --hard')
    assert stopped is False
    assert [r['authority'] for r in harness['receipts']] == [
        'agent_git_guard_solo']


# ── Existing behavior preserved ─────────────────────────────────────

def test_readonly_git_keeps_existing_demotion(harness):
    stopped, _ = _invoke(command='git status')
    assert stopped is False
    # No guard authority and no human prompt for read-only git.
    assert all(not r['authority'].startswith(('agent_git', 'git_safe'))
               for r in harness['receipts'])
    assert harness['approvals'] == []


def test_unknown_git_shape_falls_back_to_human_gate(harness):
    stopped, _ = _invoke(command='git bisect start')
    assert stopped is True
    assert harness['approvals'], 'expected the attended approval prompt'
    assert harness['receipts'] == []


# ── Merged force-delete downgrade (git branch -D on merged branches) ──

def _merged_probe(merged_names):
    """Branch-probe double: names in merged_names report fully merged."""
    return lambda root, name: name in merged_names


def test_plain_branch_delete_needs_no_probe(harness, monkeypatch):
    # `branch -d` is safe_write at the classifier; active peers never enter.
    monkeypatch.setattr(guard, 'active_peers',
                        lambda root, user_id, exclude_conv='': [_peer()])
    monkeypatch.setattr(
        guard, 'branch_fully_merged',
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError('classifier-level safe_write must not probe')))
    stopped, _ = _invoke(command='git branch -d old-feature')
    assert stopped is False
    assert [r['authority'] for r in harness['receipts']] == ['git_safe_write']
    assert harness['rejections'] == []


def test_merged_force_delete_downgrades_despite_active_peers(
        harness, monkeypatch):
    monkeypatch.setattr(guard, 'active_peers',
                        lambda root, user_id, exclude_conv='': [_peer()])
    monkeypatch.setattr(guard, 'branch_fully_merged',
                        _merged_probe({'old-feature'}))
    stopped, _ = _invoke(command='git branch -D old-feature')
    assert stopped is False
    assert [r['authority'] for r in harness['receipts']] == ['git_safe_write']
    assert harness['rejections'] == [] and harness['approvals'] == []


def test_multi_name_force_delete_requires_all_merged(harness, monkeypatch):
    monkeypatch.setattr(guard, 'active_peers',
                        lambda root, user_id, exclude_conv='': [_peer()])
    monkeypatch.setattr(guard, 'branch_fully_merged',
                        _merged_probe({'done-a'}))
    stopped, tool_results = _invoke(
        command='git branch -D done-a wip-b')
    assert stopped is True
    assert 'mutates Git state' in tool_results['call-1'][0]
    assert harness['receipts'] == []


def test_unmerged_force_delete_keeps_refusal_with_peers(harness, monkeypatch):
    monkeypatch.setattr(guard, 'active_peers',
                        lambda root, user_id, exclude_conv='': [_peer()])
    monkeypatch.setattr(guard, 'branch_fully_merged', _merged_probe(set()))
    stopped, tool_results = _invoke(command='git branch -D wip-feature')
    assert stopped is True
    assert 'cannot override active project presence' in tool_results['call-1'][0]
    assert harness['receipts'] == []


def test_force_delete_probe_failure_keeps_guard_verdict(harness, monkeypatch):
    def _boom(root, name):
        raise RuntimeError('git wedged')
    monkeypatch.setattr(guard, 'branch_fully_merged', _boom)
    monkeypatch.setattr(guard, 'active_peers',
                        lambda root, user_id, exclude_conv='': [_peer()])
    # The inner guard failure must fail open to the attended gate, never to
    # a silent allow.
    stopped, _ = _invoke(command='git branch -D whatever')
    assert stopped is True
    assert harness['approvals']
    assert harness['receipts'] == []


def test_merged_probe_with_no_peers_stays_solo(harness, monkeypatch):
    # Without the downgrade this command receipts as agent_git_guard_solo;
    # the merged probe must not mask the no-peer path's authority naming.
    monkeypatch.setattr(guard, 'active_peers',
                        lambda root, user_id, exclude_conv='': [])
    monkeypatch.setattr(guard, 'branch_fully_merged',
                        _merged_probe({'old-feature'}))
    stopped, _ = _invoke(command='git branch -D old-feature')
    assert stopped is False
    assert [r['authority'] for r in harness['receipts']] == ['git_safe_write']


def test_non_delete_tree_destroyer_never_probes(harness, monkeypatch):
    monkeypatch.setattr(
        guard, 'branch_fully_merged',
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError('non-delete segments must not probe branches')))
    monkeypatch.setattr(guard, 'active_peers',
                        lambda root, user_id, exclude_conv='': [])
    stopped, _ = _invoke(command='git reset --hard HEAD~1')
    assert stopped is False
    assert [r['authority'] for r in harness['receipts']] == [
        'agent_git_guard_solo']


# ── Mergedness probe unit behavior (private temp repository) ───────

@pytest.fixture
def merged_repo(tmp_path):
    """Private repo: main with feature-merged (fast-forwarded) and feature-wip."""
    import subprocess as sp
    repo = str(tmp_path / 'repo')

    def git(*args):
        sp.run(['git', *args], cwd=repo, check=True,
               stdout=sp.DEVNULL, stderr=sp.DEVNULL,
               env={'GIT_CONFIG_NOSYSTEM': '1', 'HOME': str(tmp_path),
                    'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
                    'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
                    'PATH': __import__('os').environ['PATH']})

    sp.run(['git', 'init', repo], check=True,
           stdout=sp.DEVNULL, stderr=sp.DEVNULL)
    git('symbolic-ref', 'HEAD', 'refs/heads/main')
    git('commit', '--allow-empty', '-m', 'base')
    git('checkout', '-b', 'feature-merged')
    git('commit', '--allow-empty', '-m', 'merged work')
    git('checkout', 'main')
    git('merge', '--ff-only', 'feature-merged')
    git('checkout', '-b', 'feature-wip', 'HEAD~1')
    git('commit', '--allow-empty', '-m', 'unmerged work')
    git('checkout', 'main')
    return repo


def test_branch_fully_merged_real_repo(merged_repo):
    assert guard.branch_fully_merged(merged_repo, 'feature-merged') is True
    assert guard.branch_fully_merged(merged_repo, 'feature-wip') is False
    assert guard.branch_fully_merged(merged_repo, 'no-such-branch') is False
    assert guard.branch_fully_merged(merged_repo, '') is False
    assert guard.branch_fully_merged('', 'feature-merged') is False
    assert guard.branch_fully_merged(
        '/nonexistent/repo/path', 'feature-merged') is False


def test_force_delete_targets_merged_real_repo(merged_repo):
    cmd = 'git branch -D feature-merged'
    assert guard.force_delete_targets_merged(cmd, [merged_repo]) is True
    assert guard.force_delete_targets_merged(
        'git branch -D feature-wip', [merged_repo]) is False
    assert guard.force_delete_targets_merged(
        'git branch -D feature-merged feature-wip', [merged_repo]) is False
    # Root and command must agree: merged elsewhere is not merged here.
    assert guard.force_delete_targets_merged(cmd, ['/nonexistent']) is False
    assert guard.force_delete_targets_merged(cmd, []) is False
    assert guard.force_delete_targets_merged(
        cmd, [merged_repo, merged_repo]) is False
    # Non-delete shapes never downgrade.
    assert guard.force_delete_targets_merged(
        'git branch -D feature-merged && git reset --hard',
        [merged_repo]) is False
    assert guard.force_delete_targets_merged(
        'git branch -D', [merged_repo]) is False
    assert guard.force_delete_targets_merged(
        'git branch -d feature-merged', [merged_repo]) is False


def test_non_git_destructive_unattended_still_rejected(harness):
    stopped, tool_results = _invoke(command='rm -rf build/', attended=False)
    assert stopped is True
    assert 'requires attended human confirmation' in tool_results['call-1'][0]
    assert harness['receipts'] == []


def test_mixed_opaque_command_keeps_human_gate(harness):
    stopped, _ = _invoke(command='git add x && rm -rf build/')
    assert stopped is True
    assert harness['approvals']


def test_guard_failure_fails_open_to_existing_gate(harness, monkeypatch):
    def _boom(root, user_id, exclude_conv=''):
        raise RuntimeError('registry down')
    monkeypatch.setattr(guard, 'active_peers', _boom)
    stopped, _ = _invoke(command='git reset --hard')
    # fail-open: the existing attended gate decides (approval prompt).
    assert stopped is True
    assert harness['approvals']
    assert harness['receipts'] == []


def test_active_peers_probe_failure_returns_empty(monkeypatch):
    # The presence read itself is fail-open at the module level.
    import lib.presence as presence
    monkeypatch.setattr(
        presence, 'snapshot',
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('down')))
    assert guard.active_peers(ROOT, 1, exclude_conv='x') == []
    assert guard.active_peers('', 1) == []          # no root → no peers
    assert guard.active_peers(ROOT, None) == []     # bad owner → no peers


def test_guard_disabled_env_restores_old_gate(harness, monkeypatch):
    monkeypatch.setattr(guard, '_ENABLED', False)
    stopped, _ = _invoke(command='git reset --hard')
    assert stopped is True
    assert harness['approvals']
    assert harness['receipts'] == []


def test_no_project_root_means_no_peers(harness, monkeypatch):
    seen = {}

    def _spy(root, user_id, exclude_conv=''):
        seen['root'] = root
        return []
    monkeypatch.setattr(guard, 'active_peers', _spy)
    stopped, _ = _invoke(command='git reset --hard', project_path=None)
    assert stopped is False
    assert seen['root'] == ''
    assert [r['authority'] for r in harness['receipts']] == [
        'agent_git_guard_solo']


# ── Nonce store unit behavior ───────────────────────────────────────

def test_legacy_nonce_store_is_bounded_and_never_grants_authority():
    nonce = guard.issue_ack(ROOT, 'c1')
    assert guard.consume_ack(ROOT, 'c2', nonce) is False  # no authority, regardless of conv
    nonce = guard.issue_ack(ROOT, 'c1')
    assert guard.consume_ack(ROOT, 'c1', nonce) is False
    assert guard.consume_ack(ROOT, 'c1', nonce) is False  # single-use
    for _ in range(guard._ACK_MAX_ENTRIES + 10):
        guard.issue_ack(ROOT, 'cx')
    assert len(guard._acks) <= guard._ACK_MAX_ENTRIES


def test_extract_ack_honors_only_leading_env():
    assert guard.extract_ack('TOFU_GIT_ACK=abc123 git reset --hard') == 'abc123'
    assert guard.extract_ack('FOO=1 TOFU_GIT_ACK=zz99 git checkout x') == 'zz99'
    assert guard.extract_ack('echo "TOFU_GIT_ACK=fake"') is None
    assert guard.extract_ack('git reset --hard') is None
    assert guard.extract_ack('TOFU_GIT_ACK=!! git reset --hard') is None


@pytest.mark.parametrize('file_hint', ['', 'unrelated/file.py'])
def test_reset_refuses_presence_without_complete_file_evidence(
        harness, monkeypatch, file_hint):
    peer = _peer(title='Build task', current_file=file_hint,
                 conv='conversation-full-identity-123456789')
    peer.update(agentId='nested-agent-full-identity-987654321',
                statusLabel='working (testing)', files=[])
    monkeypatch.setattr(guard, 'active_peers',
                        lambda root, user_id, exclude_conv='': [peer])
    stopped, results = _invoke(command='git reset --hard')
    assert stopped is True
    content = results['call-1'][0]
    assert 'Build task' in content
    assert 'convId=conversation-full-identity-123456789' in content
    assert 'agentId=nested-agent-full-identity-987654321' in content
    assert 'not confirmed writers' in content
    assert 'incomplete activity hints' in content
    assert harness['receipts'] == [] and harness['approvals'] == []
    assert 'checkoutRoot' not in peer  # dispatch must not mutate registry payloads


def test_refusal_legacy_three_argument_call_keeps_identity_and_no_nonce():
    content = guard.format_refusal('git reset --hard', [_peer()], 'legacy-token')
    assert 'convId=conv-victim agentId=(main)' in content
    assert 'Checkout root: (not supplied by caller)' in content
    assert 'legacy-token' not in content
    assert 'not evidence of an actual writer lock/lease or a file conflict' in content


@pytest.mark.parametrize('command', [
    'git apply patch.diff', 'git apply --cached patch.diff',
    'git apply --index --exclude=generated.json patch.diff',
    'git apply --stat --apply patch.diff', 'git commit -m append',
    'git apply patch.diff && git apply --cached patch.diff && git commit -m append',
])
def test_ordinary_apply_and_commit_do_not_veto_on_active_presence(harness, monkeypatch, command):
    def unexpected_presence_probe(*args, **kwargs):
        raise AssertionError('ordinary writes must not depend on project activity')
    monkeypatch.setattr(guard, 'active_peers', unexpected_presence_probe)
    stopped, _ = _invoke(command=command)
    assert stopped is False
    assert [r['authority'] for r in harness['receipts']] == ['git_safe_write']
    assert harness['rejections'] == [] and harness['approvals'] == []


@pytest.mark.parametrize('command', [
    'python deploy.py && git reset --hard',
    'git reset --hard && python deploy.py',
    'git bisect start && git reset --hard',
    'git reset --hard "$(touch marker)"',
])
@pytest.mark.parametrize('with_ack', [False, True])
def test_solo_presence_never_authorizes_opaque_chain(harness, monkeypatch, command, with_ack):
    monkeypatch.setattr(guard, 'active_peers', lambda *args, **kwargs: [])
    if with_ack:
        nonce = guard.issue_ack(ROOT, EXEC_CONV)
        command = f'{guard.ACK_ENV_VAR}={nonce} {command}'
    stopped, _ = _invoke(command=command)
    assert stopped is True
    assert harness['receipts'] == [] and harness['approvals']


@pytest.mark.parametrize('command', [
    'python deploy.py && git reset --hard',
    'git reset --hard && python deploy.py',
])
def test_opaque_chain_still_refuses_high_risk_presence(harness, monkeypatch, command):
    monkeypatch.setattr(guard, 'active_peers', lambda *args, **kwargs: [_peer()])
    stopped, results = _invoke(command=command)
    assert stopped is True
    assert 'Conservative project-presence policy' in results['call-1'][0]
    assert harness['receipts'] == [] and harness['approvals'] == []


@pytest.mark.parametrize('command', [
    'git apply --3way --build-fake-ancestor=outside.index --stat patch.diff',
    'git apply --reject --future-option patch.diff',
    'git apply --future-option --reject patch.diff',
])
@pytest.mark.parametrize('peers', [False, True])
@pytest.mark.parametrize('attended', [False, True])
def test_unknown_risky_apply_never_mints_automatic_receipt(
        harness, monkeypatch, command, peers, attended):
    monkeypatch.setattr(guard, 'active_peers', lambda *args, **kwargs: [_peer()] if peers else [])
    stopped, results = _invoke(command=command, attended=attended)
    assert stopped is True
    assert harness['receipts'] == []
    if peers:
        assert 'Conservative project-presence policy' in results['call-1'][0]
        assert harness['approvals'] == []
    elif attended:
        assert harness['approvals']
    else:
        assert 'requires attended human confirmation' in results['call-1'][0]


@pytest.mark.parametrize('command', ['git merge feature', 'git cherry-pick abc123',
                                     'git revert HEAD', 'git cherry-pick --continue'])
def test_ordinary_integration_does_not_treat_presence_as_writer(harness, monkeypatch, command):
    def forbidden(*args, **kwargs):
        pytest.fail('ordinary integration must not probe project presence')
    monkeypatch.setattr(guard, 'active_peers', forbidden)
    stopped, _ = _invoke(command=command)
    assert stopped is False
    assert [receipt['authority'] for receipt in harness['receipts']] == ['git_safe_write']
    assert harness['approvals'] == []


@pytest.mark.parametrize('attended', [True, False])
def test_empty_target_resolution_never_mints_solo_or_consumes_ack(harness, monkeypatch, attended):
    monkeypatch.setattr(pipeline, '_git_guard_presence_roots', lambda *args: [])
    def forbidden(*args, **kwargs):
        pytest.fail('unresolved roots must not run presence, ACK or mergedness probes')
    monkeypatch.setattr(guard, 'consume_ack', forbidden)
    monkeypatch.setattr(guard, 'active_peers', forbidden)
    monkeypatch.setattr(guard, 'force_delete_targets_merged', forbidden)
    stopped, _ = _invoke(command='TOFU_GIT_ACK=abc123 git reset --hard', attended=attended)
    assert stopped is True
    assert harness['receipts'] == []
    assert bool(harness['approvals']) is attended


@pytest.mark.parametrize('command, trigger', [
    ('git stash push -m backup && git merge --no-ff topic -m merge', 'stash'),
    ('git merge --no-ff topic -m "--abort"; git cherry-pick --abort', 'cherry-pick'),
])
def test_refusal_diagnostics_pin_actual_risky_segment(harness, monkeypatch, command, trigger):
    monkeypatch.setattr(guard, 'active_peers', lambda *args, **kwargs: [_peer()])
    stopped, results = _invoke(command=command)
    assert stopped is True
    evidence = harness['rejections'][0]['descriptor']['gitDiagnostics']
    assert [segment['verb'] for segment in evidence['segments']] == [trigger]
    assert evidence['execution'] == 'not_started'
    assert 'git commit --only' in results['call-1'][0]
    assert harness['receipts'] == []


@pytest.mark.parametrize('command', [
    'git merge topic --cleanup=strip',
    'git cherry-pick --ff --empty=drop topic',
    'git -C/proj/guarded merge topic --cleanup=verbatim',
    'git clean -ndx',
])
def test_maintained_options_do_not_probe_presence(harness, monkeypatch, command):
    monkeypatch.setattr(guard, 'active_peers', lambda *args, **kwargs: pytest.fail('no presence for ordinary/preview'))
    stopped, _ = _invoke(command=command)
    assert stopped is False
    assert harness['approvals'] == []


@pytest.mark.parametrize('command', [
    'git merge topic --future-option',
    'git merge topic --gpg-sign',
    'git cherry-pick --empty=invalid topic',
])
@pytest.mark.parametrize('attended', [True, False])
def test_unknown_or_external_options_retain_approval_not_presence(harness, monkeypatch, command, attended):
    monkeypatch.setattr(guard, 'active_peers', lambda *args, **kwargs: pytest.fail('unknown is not proven destruction'))
    stopped, _ = _invoke(command=command, attended=attended)
    assert stopped is True
    assert harness['receipts'] == []
    assert bool(harness['approvals']) is attended
