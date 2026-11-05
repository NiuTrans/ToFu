"""The coreutils ``timeout`` probe is resolved once per server PATH value.

Every run_command asks for it twice (grep-splice planning and the scanner
segment guard). ``shutil.which`` stats every PATH entry; on this host several
entries live on a network filesystem, so the probe is a fixed per-command cost.
"""
from __future__ import annotations

import os
import sys

import pytest

import lib.project_mod.grep_redirect as grep_redirect

pytestmark = pytest.mark.unit


@pytest.fixture
def which_calls(monkeypatch, tmp_path):
    binary = tmp_path / 'timeout'
    binary.write_text('#!/bin/sh\n', encoding='utf-8')
    binary.chmod(0o755)
    calls = []

    def fake_which(name, *args, **kwargs):
        calls.append((name, os.environ.get('PATH')))
        return str(binary) if binary.exists() else None

    monkeypatch.setattr(grep_redirect, '_TIMEOUT_BINARY_CACHE', {})
    monkeypatch.setattr(grep_redirect.shutil, 'which', fake_which)
    monkeypatch.setenv('PATH', '/first:/second')
    return calls, binary


def test_unchanged_path_reuses_the_resolved_binary(which_calls):
    calls, binary = which_calls
    assert grep_redirect.resolve_timeout_binary() == str(binary)
    assert grep_redirect.resolve_timeout_binary() == str(binary)
    assert calls == [('timeout', '/first:/second')]


def test_path_change_or_vanished_binary_resolves_again(which_calls, monkeypatch):
    calls, binary = which_calls
    grep_redirect.resolve_timeout_binary()
    monkeypatch.setenv('PATH', '/other')
    assert grep_redirect.resolve_timeout_binary() == str(binary)
    binary.unlink()
    assert grep_redirect.resolve_timeout_binary() is None
    assert grep_redirect.resolve_timeout_binary() is None
    assert [path for _name, path in calls] == ['/first:/second', '/other', '/other', '/other']


def test_both_command_paths_share_the_resolution(which_calls):
    calls, binary = which_calls
    wrap = grep_redirect._default_timeout_wrap()
    assert wrap is not None and wrap[0] == str(binary)
    assert grep_redirect.resolve_timeout_binary() == str(binary)
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform != 'linux', reason='coreutils timeout probe')
def test_real_probe_matches_shutil_which(monkeypatch):
    import shutil
    monkeypatch.setattr(grep_redirect, '_TIMEOUT_BINARY_CACHE', {})
    assert grep_redirect.resolve_timeout_binary() == shutil.which('timeout')
