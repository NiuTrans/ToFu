"""Tofu Git workspace guard — presence-aware interception of tree-destroying Git.

Contract: docs/modules/project_integration.md "Git workspace guard".

In a shared canonical checkout, one conversation's tree-destroying Git command
(``checkout``, ``reset --hard``, ``stash``, …) rewrites the working tree under
every OTHER active conversation — the bytes may be recoverable, the victims'
in-context assumptions are not. The guard moves the decision from an
uninformed human approval to the executing agent: with no active peer the
command runs with an automatic receipt; with active peers it is refused with
a tool-result message that names the peers and asks the executor to wait or
use a separately authorized isolated checkout. A nonce never overrides peers.

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
import re
import secrets
import subprocess
import threading
import time

from lib.log import get_logger
from lib.project_mod.command_analysis import (
    _ENV_ASSIGNMENT_GUARD_RE,
    _git_guard_classify,
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
    """Risk presence checks do not authorize unknown or opaque shell code."""
    if not _ENABLED:
        return False
    try:
        return _git_guard_classify(command, require_known_segments=True) is not None
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
    for segment in _split_pipeline(command):
        words = [w.text for w in _shell_words(segment)]
        while words and _ENV_ASSIGNMENT_GUARD_RE.match(words[0]):
            words = words[1:]
        words = _unwrap_command_parts(words)
        if not words or os.path.basename(words[0]) != 'git':
            return False
        sub_words = words[1:]
        index = 0
        while index < len(sub_words):
            tok = sub_words[index]
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


def format_refusal(command: str, peers: list[dict], nonce: str,
                   roots: list[str] | None = None) -> str:
    """Build a conservative presence refusal for the EXECUTING agent.

    ``nonce`` is retained for older callers, not rendered or used as authority.
    Callers may supply resolved checkout roots; legacy callers omit them.
    """
    lines = [
        f'[git-guard] REFUSED: `{command}` mutates Git state. '
        'Conservative project-presence policy blocks this high-risk operation '
        'while other conversations/agents are active on this project.',
    ]
    for root in roots or []:
        lines.append(f'Checkout root: {root or "(unresolved)"}')
    if not roots:
        lines.append('Checkout root: (not supplied by caller)')
    lines.append('Active project presence (not confirmed writers):')
    for peer in peers[:5]:
        lines.append(f'  - {_peer_label(peer)}')
    if len(peers) > 5:
        lines.append(f'  - … and {len(peers) - 5} more')
    lines += [
        'Presence is not evidence of an actual writer lock/lease or a file '
        'conflict. Reported files are incomplete activity hints, not a safe '
        'write set; this operation may overwrite working files or change '
        'refs/history that peers rely on.',
        'Wait for active project presence to settle, then retry; or use a '
        'separately authorized isolated worktree.',
        'Acknowledgement tokens cannot override active project presence. '
        'Committing or stashing shared work does not clear this presence refusal.',
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
    'consume_ack',
    'enabled',
    'extract_ack',
    'force_delete_targets_merged',
    'format_refusal',
    'issue_ack',
]
