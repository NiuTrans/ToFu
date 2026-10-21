"""Child environment contract shared by command and MCP execution.

Entry points validate_environment, apply_tool_paths, resolve_executable and
environment_report operate on explicit mappings, never process-global state.
Package caches and installed uv tools are persistent; no package is installed
or deleted here. Credential selection stays with each execution authority.
"""
from __future__ import annotations

import os
import shutil
import sys


def validate_environment(environment: dict[str, str]) -> dict[str, str]:
    for key, value in environment.items():
        if not isinstance(key, str) or not key or '=' in key or '\0' in key:
            raise ValueError('Invalid subprocess environment variable name')
        if not isinstance(value, str) or '\0' in value:
            raise ValueError('Subprocess environment values must be strings without NUL')
    return environment


def tool_cache_defaults(cache_root: str) -> dict[str, str]:
    root = os.path.abspath(cache_root)
    share = os.path.join(root, 'share')
    return {
        'UV_CACHE_DIR': os.path.join(root, 'uv'),
        'XDG_CACHE_HOME': root,
        'npm_config_cache': os.path.join(root, 'npm'),
        'PIP_CACHE_DIR': os.path.join(root, 'pip'),
        'XDG_DATA_HOME': share,
        'UV_PYTHON_INSTALL_DIR': os.path.join(share, 'uv', 'python'),
        'UV_TOOL_DIR': os.path.join(share, 'uv', 'tools'),
        'UV_TOOL_BIN_DIR': os.path.join(root, 'bin'),
        'UV_LINK_MODE': 'copy',
    }


def bundled_toolchain_bins() -> list[str]:
    """Project-bundled toolchain bins that outrank the home directories.

    A launcher can exist at ``~/.local/bin/node`` and still be unrunnable:
    official Node builds need glibc >= 2.28, and a host on 2.17 answers every
    spawn with ``GLIBCXX_3.4.21 not found``. A toolchain the project itself
    ships under ``<repo>/.tofu/node-toolchain/bin`` is the one artifact whose
    ABI was verified against this host, so it must win the PATH race — placed
    after ``~/.local/bin`` it would never be reached, and ``npx``-launched MCP
    servers would die in the handshake on every boot. Absent on deployments
    that do not need it (the tree is gitignored, never exported).
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    bins = [os.path.join(repo_root, '.tofu', 'node-toolchain', 'bin')]
    return [path for path in bins if os.path.isdir(path)]


def node_executable() -> str | None:
    """Absolute path of a Node binary that can actually RUN on this host.

    ``shutil.which('node')`` only proves a file is on PATH. On a glibc 2.17
    host it happily returns an official Node build that dies with
    ``GLIBCXX_3.4.21 not found`` — so a caller that spawns Node by name gets a
    usable-looking failure deep inside its own pipeline (frontend test bundler,
    ``node --check``). The repo-bundled toolchain was verified against this
    host, so it wins; otherwise fall back to PATH for hosts that carry a
    working system Node.
    """
    for directory in bundled_toolchain_bins():
        candidate = os.path.join(directory, 'node')
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which('node')


def apply_tool_paths(environment: dict[str, str], *, interpreter_first=False,
                     cache_root: str | None = None) -> None:
    """Discover installed tools again on every spawn, including after installers.

    Explicit cache settings win. Keep nonexistent bin directories in PATH so
    an installer and later pipeline segments can use them during the same call.
    Shell startup files are never evaluated.
    """
    validate_environment(environment)
    if cache_root is not None:
        for key, value in tool_cache_defaults(cache_root).items():
            environment.setdefault(key, value)
    home = environment.get('HOME') or environment.get('USERPROFILE')
    bins = bundled_toolchain_bins()
    if interpreter_first and sys.executable:
        bins.append(os.path.dirname(os.path.abspath(sys.executable)))
        bins.append(os.path.join(sys.base_prefix, 'Scripts' if os.name == 'nt' else 'bin'))
    if environment.get('UV_TOOL_BIN_DIR'):
        bins.append(environment['UV_TOOL_BIN_DIR'])
    if home:
        bins.extend([os.path.join(home, '.local', 'bin'),
                     os.path.join(home, '.cargo', 'bin')])
    if environment.get('CARGO_HOME'):
        bins.append(os.path.join(environment['CARGO_HOME'], 'bin'))
    npm_prefix = environment.get('npm_config_prefix') or environment.get('NPM_CONFIG_PREFIX')
    if npm_prefix:
        bins.append(npm_prefix if os.name == 'nt' else os.path.join(npm_prefix, 'bin'))
    existing = environment.get('PATH', os.defpath).split(os.pathsep)
    environment['PATH'] = os.pathsep.join(dict.fromkeys([*bins, *existing]))
    validate_environment(environment)


def resolve_executable(command: str, environment: dict[str, str], *, cwd: str | None = None) -> str | None:
    """Resolve against the mapping the child will actually receive."""
    validate_environment(environment)
    path = environment.get('PATH', os.defpath)
    if cwd is not None:
        path = os.pathsep.join(entry if os.path.isabs(entry) else os.path.join(cwd, entry)
                               for entry in path.split(os.pathsep))
        if os.path.dirname(command) and not os.path.isabs(command):
            command = os.path.join(cwd, command)
    resolved = shutil.which(command, path=path)
    return os.path.abspath(resolved) if resolved else None


def environment_report(environment: dict[str, str], commands=(), *, cwd=None) -> dict:
    """Read-only, credential-free diagnostic of the final launch mapping."""
    validate_environment(environment)
    return {
        'path': environment.get('PATH', '').split(os.pathsep),
        'python_environment': environment.get('VIRTUAL_ENV'),
        'tool_bin': environment.get('UV_TOOL_BIN_DIR'),
        'executables': {command: resolve_executable(command, environment, cwd=cwd)
                        for command in commands},
    }
