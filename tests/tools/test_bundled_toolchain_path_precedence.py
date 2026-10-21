#!/usr/bin/env python3
"""Project-bundled toolchain wins the child PATH.

Root cause this guards (2026-10-21 boot): ``npx``-launched MCP servers exited
during the handshake with ``GLIBCXX_3.4.21 not found``. The host runs glibc
2.17, official Node builds need >= 2.28, and ``~/.local/bin/node`` is an
official build — so "on PATH" and "runnable" disagreed. The project ships a
musl Node 20 under ``.tofu/node-toolchain/bin`` whose ABI was verified against
this host; it must outrank the home directories or it never gets reached.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
        tests/tools/test_bundled_toolchain_path_precedence.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

from lib.tools.process_environment import (
    apply_tool_paths,
    bundled_toolchain_bins,
    resolve_executable,
)

pytestmark = pytest.mark.unit


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_bundled_bin_is_discovered_only_when_the_tree_exists():
    expected = os.path.join(_repo_root(), '.tofu', 'node-toolchain', 'bin')
    bins = bundled_toolchain_bins()
    if os.path.isdir(expected):
        assert bins == [expected]
    else:
        # A deployment that does not ship the tree must not gain a phantom
        # PATH entry (importers and exports carry no .tofu directory).
        assert bins == []


@pytest.mark.skipif(not os.path.isdir(os.path.join(
    _repo_root(), '.tofu', 'node-toolchain', 'bin')), reason='no bundled toolchain here')
def test_bundled_toolchain_outranks_home_local_bin(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home_local = home / '.local' / 'bin'
    home_local.mkdir(parents=True)
    # The unrunnable home launcher this host actually has.
    home_node = home_local / 'node'
    home_node.write_text('#!/bin/sh\nexit 1\n')
    home_node.chmod(0o755)

    monkeypatch.setenv('HOME', str(home))
    for key in ('UV_TOOL_BIN_DIR', 'CARGO_HOME', 'npm_config_prefix',
                'NPM_CONFIG_PREFIX'):
        monkeypatch.delenv(key, raising=False)

    environment = {'HOME': str(home), 'PATH': '/usr/bin:/bin'}
    apply_tool_paths(environment, cache_root=str(tmp_path / 'cache'))

    bundled = os.path.join(_repo_root(), '.tofu', 'node-toolchain', 'bin')
    entries = environment['PATH'].split(os.pathsep)
    assert bundled in entries
    assert entries.index(bundled) < entries.index(str(home_local)), (
        'home launcher outranked the verified toolchain: %r' % (entries,))
    assert resolve_executable('node', environment).startswith(bundled)


def test_profile_stays_idempotent_with_the_bundle(tmp_path):
    environment = {'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}
    apply_tool_paths(environment, cache_root=str(tmp_path / 'cache'))
    first = environment['PATH']
    apply_tool_paths(environment, cache_root=str(tmp_path / 'cache'))
    assert environment['PATH'] == first
    entries = first.split(os.pathsep)
    assert len(entries) == len(set(entries)), 'duplicate PATH entry: %r' % (entries,)
