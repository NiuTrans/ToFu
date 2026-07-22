#!/usr/bin/env python3
"""Guard: export.py force-pushes ONLY on a non-fast-forward REJECTION.

Background — the data-loss class:

  ``_git_push`` mirrors an export tree to a git remote. A freshly re-``git
  init``'d dest shares no ancestry with the mirror, so the remote legitimately
  REJECTS the push as non-fast-forward; a ``--force`` there is the intended,
  warned one-way-mirror behavior. The bug: the fallback forced on ANY
  ``RuntimeError`` — including auth failures, DNS/connection errors, permission
  denials and missing repos — where a force-push is useless at best and (if the
  transient error later clears) a remote-history clobber at worst.

  Fix: :func:`_is_nonff_push_rejection` classifies the git stderr; the fallback
  force-pushes only when it returns True and re-raises otherwise. This test
  pins that classifier — NOT the (owner-gated) question of whether divergence
  should force at all.

No git, no network, no DB — pure string classification.

Run:  pytest tests/test_export_push_force_guard.py -m unit
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# export.py is the maintainer's release tool; not shipped in opensource builds.
pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

pytestmark = pytest.mark.unit


# Real git stderr fragments for a non-fast-forward rejection.
_NONFF_CASES = [
    "! [rejected]        main -> main (non-fast-forward)\n"
    "error: failed to push some refs to 'github.com:o/r.git'\n"
    "hint: Updates were rejected because the tip of your current branch is behind",
    "! [rejected] main -> main (fetch first)\n"
    "error: failed to push some refs",
    "Updates were rejected because a pushed branch tip is behind its remote",
]

# Failures where forcing is useless-or-dangerous → must NOT force (re-raise).
_HARD_FAILURE_CASES = [
    "fatal: Authentication failed for 'https://github.com/o/r.git/'",
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    "fatal: could not resolve host: github.com",
    "fatal: unable to access 'https://...': Connection refused",
    "remote: Permission to o/r.git denied to user.\nfatal: unable to access",
    "remote: Repository not found.\nfatal: repository 'https://...' not found",
    "",           # empty stderr
    "some totally unrelated error text",
]


def test_nonff_rejections_are_forceable():
    from export import _is_nonff_push_rejection
    for txt in _NONFF_CASES:
        assert _is_nonff_push_rejection(txt) is True, txt


def test_hard_failures_are_not_forceable():
    from export import _is_nonff_push_rejection
    for txt in _HARD_FAILURE_CASES:
        assert _is_nonff_push_rejection(txt) is False, txt


def test_auth_failure_wins_even_if_it_mentions_rejected():
    """A combined message that carries BOTH an auth failure and a reject
    marker must be treated as a hard failure (do not force)."""
    from export import _is_nonff_push_rejection
    mixed = ("! [rejected] main -> main (non-fast-forward)\n"
             "fatal: Authentication failed for 'https://github.com/o/r.git/'")
    assert _is_nonff_push_rejection(mixed) is False


def test_case_insensitive():
    from export import _is_nonff_push_rejection
    assert _is_nonff_push_rejection("NON-FAST-FORWARD") is True
    assert _is_nonff_push_rejection("AUTHENTICATION FAILED") is False


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
