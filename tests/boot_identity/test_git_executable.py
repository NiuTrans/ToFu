"""Executable admission for updater Git, before any repository operation."""
import os
import subprocess

import pytest

from lib.self_update import _git

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('failure', ['abi', 'timeout', 'missing'])
def test_skip_unusable_path_git(monkeypatch, failure):
    monkeypatch.setattr(_git, '_git_exe_cache', None)
    monkeypatch.setattr(os, 'get_exec_path', lambda: ['/broken', '/working'])
    monkeypatch.setattr('shutil.which', lambda name, path=None:
                        '/working/git' if path == '/working' else '/broken/git')
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == '/broken/git':
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
            if failure == 'missing':
                raise FileNotFoundError(argv[0])
            return subprocess.CompletedProcess(argv, 1, '', 'GLIBC unavailable')
        return subprocess.CompletedProcess(argv, 0, 'git version 2.11.0\n', '')

    monkeypatch.setattr(subprocess, 'run', run)
    assert _git._git_exe() == '/working/git'
    assert _git._git_exe() == '/working/git'
    assert calls == [['/broken/git', '--version'], ['/working/git', '--version']]


def test_unusable_candidates_do_not_claim_a_valid_binary(monkeypatch):
    monkeypatch.setattr(_git, '_git_exe_cache', None)
    monkeypatch.setattr(os, 'get_exec_path', lambda: [])
    monkeypatch.setattr('shutil.which', lambda *args, **kwargs: None)
    assert _git._git_exe() == 'git'
    assert _git._git_exe_cache == 'git'
