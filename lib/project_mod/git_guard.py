"""Tofu Git workspace guard — presence-aware interception of tree-destroying Git.

Contract: docs/modules/project_integration.md "Git workspace guard".

In a shared canonical checkout, one conversation's tree-destroying Git command
(``checkout``, ``reset --hard``, ``stash``, …) rewrites the working tree under
every OTHER active conversation — the bytes may be recoverable, the victims'
in-context assumptions are not. The guard moves the decision from an
uninformed human approval to the executing agent: eligible commands with no
active peer run with an automatic receipt; with active peers they are refused with
a tool-result message that names the peers and asks the executor to wait or
use a separately authorized isolated checkout. A nonce never overrides peers.
Forced worktree cleanup retains explicit review: absent presence establishes
neither task ownership nor permission to discard dirty files or worktree locks.

This module owns ONLY:
  * the guard enable switch and the classification re-export (pure logic
    lives in ``command_analysis._git_guard_classify``);
  * the ack-nonce lifecycle — issue/consume, 90 s TTL, single-use, bounded;
  * the refusal text handed to the EXECUTING agent (never to peers);
  * the read-only presence probe (fail-open);
  * the read-only mergedness probe that downgrades ``git branch -D`` on
    provably merged branches (the precondition ``git branch -d`` would
    enforce) to the safe-write path, peers or not.

The guard never writes the filesystem, never announces into presence, never
notifies peers, and never consumes a model turn automatically. Presence is
conservative project activity evidence, not proof of a writer lease or file
conflict. Legacy nonce APIs grant no override; disguised command shapes that
evade the string classifier degrade to the existing attended gate.
"""

from __future__ import annotations

import os

import json
import re
import secrets
import subprocess
import threading
import time

from lib.log import get_logger
from lib.project_mod.command_analysis import (
    _ENV_ASSIGNMENT_GUARD_RE,
    _git_guard_classify,

    _classify_git_invocation,
    git_invocation_parts,

    git_command_words,
    git_integration_option_evidence,
    git_worktree_lifecycle,
    _shell_words,
    _split_pipeline,
    _unwrap_command_parts,
)

logger = get_logger(__name__)

# Kill switch: TOFU_GIT_GUARD=0 restores the pre-guard attended-gate behavior.
_ENABLED = os.environ.get('TOFU_GIT_GUARD', '1').strip().lower() not in (
    '0', 'false', 'no', 'off')

ACK_ENV_VAR = 'TOFU_GIT_ACK'
ACK_TTL_SEC = 90.0
_ACK_MAX_ENTRIES = 256
_ACK_TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{4,64}$')

_lock = threading.Lock()
# (root, nonce) -> (expiry_monotonic, conv_id). Process-local and ephemeral:
# retained only for legacy callers; tokens never authorize execution.
_acks: dict[tuple[str, str], tuple[float, str]] = {}


def enabled() -> bool:
    return _ENABLED


def classify(command: str) -> str | None:
    """Guard verdict for *command*; None when disabled or not classifiable."""
    if not _ENABLED:
        return None
    try:
        return _git_guard_classify(command)
    except Exception as exc:  # pure parser must never break dispatch
        logger.debug('[GitGuard] classify failed, falling back to gate: %s', exc)
        return None


def automatic_receipt_eligible(command: str) -> bool:
    """Presence absence proves neither opaque code nor forced cleanup safe."""
    if not _ENABLED:
        return False
    try:
        if _git_guard_classify(command, require_known_segments=True) is None:
            return False
        for segment in _split_pipeline(command):
            words = [word.text for word in _shell_words(segment)]
            while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
                words.pop(0)
            words = git_command_words(words)
            if not words or os.path.basename(words[0]) != 'git':
                continue
            parsed = git_invocation_parts(words[1:])
            if parsed and parsed[0] == 'worktree':
                lifecycle = git_worktree_lifecycle(parsed[1])
                if lifecycle is not None and lifecycle.forced:
                    # A force flag bypasses native dirty/locked-tree checks.
                    # No-presence is not task ownership or content review.
                    # Retain the existing explicit approval authority; do not
                    # add a path whitelist or a racy pre-approval status probe.
                    return False
        return True
    except Exception as exc:
        logger.debug('[GitGuard] receipt parse failed, falling back to gate: %s', exc)
        return False


def extract_ack(command: str) -> str | None:
    """Return the first leading ``TOFU_GIT_ACK=<token>`` env assignment.

    Only leading env assignments of a pipeline segment are honored — a token
    buried inside a quoted string is data, not an acknowledgement.
    """
    if not command or ACK_ENV_VAR not in command:
        return None
    prefix = ACK_ENV_VAR + '='
    for seg in _split_pipeline(command):
        for word in _shell_words(seg):
            text = word.text
            if '=' not in text:
                break  # first non-assignment word ends the env prefix
            if text.startswith(prefix):
                token = text[len(prefix):]
                return token if _ACK_TOKEN_RE.match(token) else None
            if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', text):
                break
    return None


def _sweep_expired_locked(now: float) -> None:
    expired = [k for k, (exp, _) in _acks.items() if exp < now]
    for k in expired:
        _acks.pop(k, None)


def issue_ack(root: str, conv_id: str) -> str:
    """Mint a one-time nonce bound to (root, conv_id); 90 s TTL."""
    root = os.path.abspath(root) if root else ''
    now = time.monotonic()
    with _lock:
        _sweep_expired_locked(now)
        while len(_acks) >= _ACK_MAX_ENTRIES:
            _acks.pop(next(iter(_acks)), None)
        nonce = secrets.token_hex(3)
        while (root, nonce) in _acks:
            nonce = secrets.token_hex(3)
        _acks[(root, nonce)] = (now + ACK_TTL_SEC, conv_id or '')
    logger.info('[GitGuard] ack issued root=%s conv=%s', os.path.basename(root),
                (conv_id or '-')[:8])
    return nonce


def consume_ack(root: str, conv_id: str, token: str) -> bool:
    """Retire legacy tokens without granting authority over active presence.

    Kept while older pipeline callers still invoke the nonce API. Presence
    must settle (and execution must acquire its lease); ack is not exclusion.
    """
    if not token:
        return False
    root = os.path.abspath(root) if root else ''
    with _lock:
        _acks.pop((root, token), None)
    return False


# Read-only subprocess probes get a hard wall-clock cap: a wedged transport or
# FUSE mount must degrade to the existing refusal path, not stall dispatch.
_PROBE_TIMEOUT_SEC = 10.0


def _run_readonly_git(cwd, args):
    """Run a read-only git probe; return (exit_code, stdout) or (None, '').

    Never raises: timeout / spawn failure / missing git all yield None so the
    caller degrades to the conservative guard verdict.
    """
    try:
        proc = subprocess.run(
            ['git', *args], cwd=cwd or None,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=_PROBE_TIMEOUT_SEC,
            env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
    except Exception as exc:
        logger.debug('[GitGuard] probe spawn failed: %s', exc)
        return None, ''
    return proc.returncode, proc.stdout.decode('utf-8', 'replace')


def branch_fully_merged(root: str, branch: str) -> bool:
    """True when ``git branch -d branch`` would succeed on mergedness grounds.

    Mirrors Git's own -d precondition: the branch tip must be reachable from
    HEAD or from the branch's configured upstream. Unknown revs, a missing
    repo, and probe failures all return False (keep the guard verdict).
    """
    if not root or not branch:
        return False
    code, out = _run_readonly_git(root, ['rev-parse', '--verify', branch])
    if code != 0:
        return False
    tip = out.split('\n', 1)[0].strip()
    if not tip:
        return False
    code, _ = _run_readonly_git(
        root, ['merge-base', '--is-ancestor', tip, 'HEAD'])
    if code == 0:
        return True
    if code is None:
        return False
    code, out = _run_readonly_git(
        root, ['rev-parse', '--symbolic-full-name', '--abbrev-ref',
               f'{branch}@{{upstream}}'])
    if code != 0:
        return False
    upstream = out.split('\n', 1)[0].strip()
    if not upstream:
        return False
    code, _ = _run_readonly_git(
        root, ['merge-base', '--is-ancestor', tip, upstream])
    return code == 0


def _force_deleted_branch_names(segment_words):
    """Branch names of a ``git branch -D <name>...`` segment; None otherwise.

    Only the provably force-delete-only shape is extracted: -D/--delete+force
    flags plus plain names. Any other option or argument form returns None.
    """
    force_flags = {'-D', '-f', '--force'}
    names = []
    for word in segment_words:
        if word in force_flags or word == '--delete':
            continue
        if word.startswith('-'):
            return None
        names.append(word)
    return names or None


def force_delete_targets_merged(command: str, roots: list) -> bool:
    """True when every git segment is a force-delete of fully-merged branches.

    The caller already classified *command* tree_destroying; this downgrade
    requires that EVERY segment be a plain ``git branch -D/--force --delete``
    shape targeting the single resolved checkout root, with every named
    branch provably merged (the same precondition ``git branch -d`` would
    enforce). Anything else — mixed segments, multiple roots, opaque shapes,
    unmerged or unknown branches, probe failure — keeps tree_destroying.
    """
    if not roots or len(roots) != 1:
        return False
    root = roots[0]
    if not root:
        return False
    saw_force_delete = False
    # A read-only probe cannot borrow routing/config from another invocation.
    # Unsupported env/config overrides retain risk rather than prove another HEAD.
    if any(os.environ.get(key) for key in (
            'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE', 'GIT_NAMESPACE')):
        return False
    for segment in _split_pipeline(command):
        words = [w.text for w in _shell_words(segment)]
        while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
            if words[0].startswith('GIT_'):
                return False
            words = words[1:]
        words = _unwrap_command_parts(words)
        if not words or os.path.basename(words[0]) != 'git':
            return False
        sub_words = words[1:]
        index = 0
        while index < len(sub_words):
            tok = sub_words[index]
            if tok != '-C' and (tok.startswith('-c') or tok.startswith('--git-dir')
                               or tok.startswith('--work-tree') or tok.startswith('--namespace')
                               or tok.startswith('--exec-path') or tok.startswith('--config-env')):
                return False
            if tok in ('-C', '-c', '--git-dir', '--work-tree',
                       '--namespace', '--exec-path'):
                index += 2
                continue
            if tok.startswith('--') and '=' in tok:
                index += 1
                continue
            if tok.startswith('-'):
                return False
            break
        if index >= len(sub_words) or sub_words[index] != 'branch':
            return False
        names = _force_deleted_branch_names(sub_words[index + 1:])
        if not names:
            return False
        if not all(branch_fully_merged(root, name) for name in names):
            return False
        saw_force_delete = True
    return saw_force_delete


def active_peers(root: str, user_id, exclude_conv: str = '') -> list[dict]:
    """Active presence peers on *root* excluding *exclude_conv*; fail-open.

    A probe failure returns [] (solo path) per the presence layer's
    notify-only design — the guard never turns a registry hiccup into a
    tool-dispatch outage. No root means no attached project, hence no peers.
    """
    if not root:
        return []
    try:
        owner = int(user_id)
    except (TypeError, ValueError):
        return []
    if owner < 1:
        return []
    try:
        from lib import presence
        peers = presence.snapshot(root, user_id=owner).get('peers') or []
    except Exception as exc:
        logger.debug('[GitGuard] presence probe failed (fail-open): %s', exc)
        return []
    return [p for p in peers if (p.get('convId') or '') != exclude_conv]


def _peer_label(peer: dict) -> str:
    identity = (f"convId={peer.get('convId') or '?'} "
                f"agentId={peer.get('agentId') or '(main)'}")
    title = peer.get('title') or ''
    label = f'{title} [{identity}]' if title else identity
    detail = peer.get('statusLabel') or ''
    if peer.get('currentFile'):
        detail = f"reported currentFile={peer['currentFile']}"
    if detail:
        label += f' — reported presence: {detail}'
    if peer.get('checkoutRoot'):
        label += f" — checkout root: {peer['checkoutRoot']}"
    return label


# Pure evidence; no additional probes, model calls, cache or retry loop.
# Never echo arbitrary argv, config values, revisions or commit messages.
_DIAGNOSTIC_SEGMENTS = 16


def _operation_diagnostic(verb: str, args: list[str]) -> tuple[str, list[str], str]:
    """Annotation of a classified mutation, never execution authority."""
    from lib.project_mod.git_command_scope import git_metadata_mutation_mode
    metadata_mode = git_metadata_mutation_mode(verb, args)
    if verb == 'worktree':
        lifecycle = git_worktree_lifecycle(args)
        if lifecycle is not None:
            return ('forced_worktree_retirement' if lifecycle.forced else 'worktree_retirement',
                    ['target_worktree', 'worktree_metadata'],
                    'verify_target_ownership_and_discarded_content' if lifecycle.forced
                    else 'inspect_target_activity')
    if verb == 'reset':
        if metadata_mode == 'soft_reset':
            return 'head_rewrite', ['refs', 'operation_state'], 'inspect_head_and_task_ownership'
        return 'index_or_worktree_reset', ['refs', 'index', 'worktree'], 'preserve_uncommitted_work'
    if verb == 'restore':
        if metadata_mode == 'staged_restore':
            return 'staged_content_removal', ['index'], 'preserve_shared_index'
        return 'index_or_worktree_restore', ['index', 'worktree'], 'inspect_target_paths'
    if verb == 'clean':
        return 'untracked_deletion', ['untracked_worktree'], 'preview_deletion_first'
    if verb == 'stash':
        return 'stash_mutation', ['stash_refs', 'index', 'worktree'], 'do_not_stash_shared_work'
    if verb in {'merge', 'cherry-pick', 'revert', 'rebase', 'am'}:
        return 'integration_recovery_or_strategy', ['operation_state', 'refs', 'index', 'worktree'], 'inspect_operation_ownership'
    if verb in {'branch', 'tag', 'update-ref', 'symbolic-ref', 'replace', 'reflog'}:
        return 'reference_mutation', ['refs'], 'inspect_reference_reachability'
    if verb in {'push', 'fetch'}:
        return 'forced_or_pruning_reference_update', ['refs', 'remote_refs'], 'inspect_reference_and_remote_authority'
    if verb == 'commit':
        return 'history_rewrite', ['refs', 'index'], 'preserve_shared_index'
    if verb == 'apply':
        return 'partial_or_extended_patch_application', ['index', 'worktree'], 'preflight_reviewed_patch'
    return 'checkout_mutation', ['refs', 'index', 'worktree'], 'inspect_target_and_task_ownership'


def command_diagnostics(command: str) -> dict:
    """Reuse the risk classifier's argv parser for numbered shell segments.

    Unknown syntax is not evidence of destructive execution. Segment numbers
    include cd/filter segments. Evidence lists are bounded; none is authority.
    """
    segments = []
    omitted = 0
    for number, segment in enumerate(_split_pipeline(command), 1):
        words = [w.text for w in _shell_words(segment)]
        while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
            words = words[1:]
        words = git_command_words(words)
        if not words or os.path.basename(words[0]) != 'git':
            continue
        parsed = git_invocation_parts(words[1:])
        classification = _classify_git_invocation(words[1:])
        if classification in {'readonly', 'safe_write'}:
            continue
        verb, args = parsed if parsed else ('git', [])
        if classification == 'tree_destroying':
            reason, effects, action = _operation_diagnostic(verb, args)
        else:
            # Unknown subcommands may be shell aliases; never echo their text.
            verb = verb if verb in {'merge', 'cherry-pick', 'revert', 'apply', 'clean'} else 'git'
            reason, effects, action = 'unresolved_syntax_or_external_execution', ['unresolved'], 'inspect_syntax_and_request_approval'
        if len(segments) < _DIAGNOSTIC_SEGMENTS:
            entry = {'segment': number, 'verb': verb,
                     'classification': classification or 'unknown',
                     'reasonCode': reason, 'effects': effects,
                     'nextAction': action}
            if verb in {'merge', 'cherry-pick', 'revert'}:
                option_evidence = git_integration_option_evidence(verb, args)
                entry['riskyOptions'] = list(dict.fromkeys(option_evidence.risky_options))[:16]
                entry['hasUnknownOptions'] = bool(option_evidence.unknown_options)
                entry['hasExternalExecution'] = bool(option_evidence.external_options)
            segments.append(entry)
        else:
            omitted += 1
    return {'source': 'git_guard', 'classification': classify(command) or 'unknown',
            'segments': segments, 'omittedSegments': omitted,
            'automaticReceiptEligible': automatic_receipt_eligible(command)}


def refusal_diagnostics(command: str) -> dict:
    """Evidence extension of the existing public rejection descriptor."""
    evidence = command_diagnostics(command)
    return {**evidence, 'syntaxFullyKnown': evidence['automaticReceiptEligible'],
            'automaticReceiptEligible': False, 'execution': 'not_started',
            'evidenceLevel': 'advisory_presence_only',
            'nextActions': ['inspect_status_and_staged_diff',
                            'preserve_other_tasks_work',
                            'evaluate_reviewed_path_integration']}


def format_refusal(command: str, peers: list[dict], nonce: str,
                   roots: list[str] | None = None) -> str:
    """Build a conservative presence refusal for the EXECUTING agent.

    ``nonce`` is retained for older callers, not rendered or used as authority.
    Callers may supply resolved checkout roots; legacy callers omit them.
    """
    evidence = refusal_diagnostics(command)
    lines = [
        '[git-guard] REFUSED before execution: a flagged segment mutates Git state. '
        'Conservative project-presence policy applies to that operation, '
        'not ordinary integration elsewhere in the command.',
        'Source: git_guard (not Git stderr or a confirmed writer-lock conflict).',
    ]
    for segment in evidence['segments']:
        lines.append(f"Trigger segment {segment['segment']}: git {segment['verb']}; "
                     f"reason={segment['reasonCode']}; "
                     f"nativeEffects={','.join(segment['effects'])}; "
                     f"nextAction={segment['nextAction']}")

        if segment.get('riskyOptions'):
            lines.append('  Risk options: ' + ', '.join(segment['riskyOptions']))
    if evidence['omittedSegments']:
        lines.append(f"Additional diagnostic segments omitted: {evidence['omittedSegments']}")
    for root in (roots or [])[:8]:
        root_label = str(root or '(unresolved)')[:512].replace('\n', '\\n').replace('\r', '\\r')
        lines.append(f'Checkout root: {root_label}')
    if len(roots or []) > 8:
        lines.append('Additional checkout roots omitted from text; all targets still checked.')
    if not roots:
        lines.append('Checkout root: (not supplied by caller)')
    lines.append('Active project presence (not confirmed writers):')
    for peer in peers[:5]:
        lines.append('  - ' + json.dumps(_peer_label(peer)[:1024], ensure_ascii=False))
    if len(peers) > 5:
        lines.append(f'  - … and {len(peers) - 5} more')
    lines += [
        'Presence is not evidence of an actual writer lock/lease or a file '
        'conflict. Reported files are incomplete activity hints, not a safe '
        'write set; this operation may overwrite working files or change '
        'refs/history that peers rely on.',
        'Next: inspect git status --porcelain and git diff --cached; inspect '
        'current content of task target paths. Peer names or online status '
        'do not establish file ownership.',
        'For task integration, ordinary merge/cherry-pick do not require '
        'project-wide idle. Remove an unnecessary stash/abort step from the '
        'plan; this does not authorize the flagged operation or erase command '
        'dependencies. Submit intended ordinary integration for its own admission.',
        'A refusal applies only to the flagged segment. Read-only or non-Git '
        'work elsewhere in the command (e.g. running tests or inspections) is '
        'unaffected: re-issue that part on its own, without the refused '
        'wrapper, in a new call. Never treat this refusal as a reason to stop '
        'the task.',
        'If Git itself refuses because unrelated changes are staged, preserve '
        'them. Only after current-content checks and patch preflight, consider '
        'reviewed-path integration and git commit --only -- <reviewed-paths>; '
        'verify unrelated staged blob hashes are unchanged and rerun tests. '
        'Do not add -A to capture another task\'s work.',
        'For abort/quit, inspect operation state and task ownership first; '
        'never use an unrelated abort as cleanup. Preview deletion with '
        'dry-run rather than forcing deletion as a diagnostic.',
        'If the SAME risky operation is essential, wait for target '
        'preconditions or use a separately authorized isolated worktree. '
        'Do not repeatedly retry an unchanged command.',
        'Acknowledgement tokens cannot override active project presence. '
        'Committing or stashing shared work does not clear this presence refusal. '
        'Do not disable the guard, delete locks, clear the index or force overwrite.',
    ]
    return '\n'.join(lines)


def _reset_for_tests() -> None:
    with _lock:
        _acks.clear()


__all__ = [
    'ACK_ENV_VAR',
    'ACK_TTL_SEC',
    'active_peers',

    'automatic_receipt_eligible',
    'branch_fully_merged',
    'classify',

    'command_diagnostics',
    'refusal_diagnostics',
    'consume_ack',
    'enabled',
    'extract_ack',
    'force_delete_targets_merged',
    'format_refusal',
    'issue_ack',
]
