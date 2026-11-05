"""Recognize config-proven registry installs for the existing network owner.

Entry point prepare_package_registry returns (handled, kind, index_url).
It performs only bounded metadata checks: no config contents, credentials,
installer subprocesses, network, environment mutation, or route selection.
Unknown sources/configuration retain the caller's original shell semantics.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path, PurePath
from urllib.parse import urlsplit
from urllib.request import proxy_bypass_environment

from .command_analysis import _mask_quoted_literals
from .command_setup import _scan

_DEFAULT_INDEX = 'https://pypi.org/simple'
_REQUIREMENT = re.compile(
    r'[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_.,-]+\])?'
    r'(?:(?:===|==|!=|~=|<=|>=|<|>)[A-Za-z0-9.*+!_-]+'
    r'(?:,(?:===|==|!=|~=|<=|>=|<|>)[A-Za-z0-9.*+!_-]+)*)?\Z')
_PYTHON = re.compile(r'python(?:\d+(?:\.\d+)*)?(?:\.exe)?\Z')
_PIP = re.compile(r'pip(?:\d+(?:\.\d+)*)?(?:\.exe)?\Z')
_FLAGS = frozenset({
    '-q', '-qq', '-qqq', '--quiet', '-v', '-vv', '-vvv', '--verbose',
    '-U', '--upgrade', '--reinstall', '--force', '--no-cache', '--no-cache-dir',
    '--no-progress', '--no-input', '--disable-pip-version-check',
    '--no-deps', '--no-build', '--no-python-downloads', '--no-managed-python',
})
_VALUES = frozenset({
    '--cache-dir', '--python', '-p', '--timeout', '--retries', '--color',
    '--link-mode', '--target', '-t', '--prefix',
})
_PROXY_NAMES = frozenset({'http_proxy', 'https_proxy', 'all_proxy', 'pip_proxy'})
_UV_OPAQUE_ENV = frozenset({
    'UV_INDEX', 'UV_EXTRA_INDEX_URL', 'UV_FIND_LINKS', 'UV_CONFIG_FILE',
    'UV_CONSTRAINT', 'UV_OVERRIDE', 'UV_EXCLUDE', 'UV_BUILD_CONSTRAINT',
    'UV_TORCH_BACKEND', 'UV_PROJECT', 'UV_WORKING_DIR', 'UV_WORKING_DIRECTORY',
    'UV_PYTHON_INSTALL_MIRROR', 'UV_PYTHON_PYPY_MIRROR', 'UV_PYTHON_GRAALPY_MIRROR',
    'UV_PYTHON_DOWNLOADS_JSON_URL', 'UV_INSECURE_HOST', 'UV_KEYRING_PROVIDER',
})
_PIP_OPAQUE_ENV = frozenset({
    'PIP_EXTRA_INDEX_URL', 'PIP_FIND_LINKS', 'PIP_REQUIREMENT', 'PIP_CONSTRAINT',
    'PIP_BUILD_CONSTRAINT', 'PIP_TRUSTED_HOST', 'PIP_CERT', 'PIP_CLIENT_CERT',
    'PIP_KEYRING_PROVIDER', 'PIP_ISOLATED',
})


def _install_arguments(words):
    if not words:
        return None
    executable = PurePath(words[0]).name.lower()
    if executable in {'uv', 'uv.exe'}:
        for position in range(1, len(words) - 1):
            if words[position:position + 2] in (['tool', 'install'], ['pip', 'install']):
                prefix = words[1:position]
                if any(word not in _FLAGS and word != '--no-config' for word in prefix):
                    return 'uv', words[position], None
                return 'uv', words[position], [*prefix, *words[position + 2:]]
    if _PYTHON.fullmatch(executable) and '-m' in words[1:]:
        position = words.index('-m', 1)
        if words[position + 1:position + 2] == ['pip'] and 'install' in words[position + 2:]:
            if position != 1:
                return 'pip', 'install', None
            words = ['pip', *words[position + 2:]]
            executable = 'pip'
    if _PIP.fullmatch(executable):
        # Recognize unsupported global options too, so a mixed command cannot
        # acquire a route from a later curl while this installer is opaque.
        if 'install' in words[1:]:
            position = words.index('install', 1)
            if all(word in _FLAGS for word in words[1:position]):
                return 'pip', 'install', words[position + 1:]
            return 'pip', 'install', None
    return None


def _truth(value):
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _absent(path):
    """A symlink, inaccessible path or non-file is not neutral configuration."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


def _config_is_neutral(family, operation, env, *, cwd, no_config):
    if family == 'uv' and (no_config or _truth(env.get('UV_NO_CONFIG', ''))):
        return True
    if family == 'pip':
        # pip reads its actual interpreter's sys.prefix, not VIRTUAL_ENV.
        # Without a client-owned plan, only an explicit upstream disable
        # establishes neutral config; never inject this into the child.
        return env.get('PIP_CONFIG_FILE') == os.devnull
    # Config search layouts differ on macOS/Windows. Until their complete
    # metadata discovery is covered, require an explicit supported disable.
    if sys.platform != 'linux' or not env.get('HOME'):
        return False
    home = Path(env['HOME'])
    xdg_home = Path(env.get('XDG_CONFIG_HOME') or home / '.config')
    xdg_dirs = (env.get('XDG_CONFIG_DIRS') or '/etc/xdg').split(os.pathsep)
    if len(xdg_dirs) > 16 or any(not Path(p).is_absolute() for p in xdg_dirs):
        return False
    if not home.is_absolute() or not xdg_home.is_absolute():
        return False
    paths = [xdg_home / 'uv/uv.toml', Path('/etc/uv/uv.toml')]
    paths.extend(Path(p) / 'uv/uv.toml' for p in xdg_dirs)
    # Official uv contract: tool commands ignore local/project config.
    # uv pip does not, so any ancestor config keeps its inherited route.
    if operation != 'tool':
        if not cwd or not Path(cwd).is_absolute():
            return False
        try:
            physical_cwd = Path(cwd).resolve(strict=True)
            if not physical_cwd.is_dir():
                return False
        except (OSError, RuntimeError):
            return False
        parents = [physical_cwd, *physical_cwd.parents]
        if len(parents) > 64:
            return False
        paths.extend(p / name for p in parents for name in ('uv.toml', 'pyproject.toml'))
    return all(_absent(path) for path in paths)


def _parse_requirements(arguments, family, operation):
    if arguments is None:
        return None
    packages, indexes, no_config = [], [], False
    cursor = 0
    while cursor < len(arguments):
        argument = arguments[cursor]
        option, equal, value = argument.partition('=')
        if argument == '--no-config' and family == 'uv':
            no_config = True
        elif argument in _FLAGS:
            pass
        elif option in {'--index-url', '-i', '--default-index'}:
            if option == '--default-index' and family != 'uv':
                return None
            if not equal:
                cursor += 1
                if cursor == len(arguments):
                    return None
                value = arguments[cursor]
            indexes.append(value)
        elif option in {'--with', '-w'} and family == 'uv' and operation == 'tool':
            if not equal:
                cursor += 1
                if cursor == len(arguments):
                    return None
                value = arguments[cursor]
            if not _REQUIREMENT.fullmatch(value):
                return None
            packages.append(value)
        elif option in _VALUES:
            if not equal:
                cursor += 1
                if cursor == len(arguments):
                    return None
                value = arguments[cursor]
            if not value or value.startswith('-'):
                return None
        elif not _REQUIREMENT.fullmatch(argument):
            return None
        else:
            packages.append(argument)
        cursor += 1
    if not packages or len(indexes) > 1:
        return None
    return indexes[0] if indexes else None, no_config


def _known_index(family, env, explicit):
    opaque = _UV_OPAQUE_ENV if family == 'uv' else _PIP_OPAQUE_ENV
    if any(name in env for name in opaque):
        return None
    if family == 'uv':
        if _truth(env.get('UV_OFFLINE', '')) or _truth(env.get('UV_NO_INDEX', '')):
            return None
        candidates = [env[name] for name in ('UV_DEFAULT_INDEX', 'UV_INDEX_URL') if name in env]
        # Conflicting deprecated/current declarations are not normalized here.
        if len(set(candidates)) > 1:
            return None
    else:
        if 'PIP_NO_INDEX' in env and str(env['PIP_NO_INDEX']).lower() not in {'0', 'false', 'no', 'off'}:
            return None
        candidates = [env['PIP_INDEX_URL']] if 'PIP_INDEX_URL' in env else []
    index = explicit if explicit is not None else (candidates[0] if candidates else _DEFAULT_INDEX)
    try:
        parsed = urlsplit(index)
        port = parsed.port
        if parsed.scheme not in {'https', 'http'} or not parsed.hostname:
            return None
        if any(char.isspace() for char in index) or port == 0:
            return None
    except (ValueError, TypeError):
        return None
    # URL userinfo/path/query never leave this local selection; the shared
    # network owner derives an anonymous origin-only reachability probe.
    return index


def prepare_package_registry(command, groups, child_env, *, cwd=None):
    """Return one known index or a handled/opaque installer with zero effects."""
    installs = [(i, parsed) for i, (words, _) in enumerate(groups)
                if (parsed := _install_arguments(words)) is not None]
    if not installs:
        return False, '', ''
    if len(command) > 16_384 or len(installs) != 1 or installs[0][0] != 0:
        return True, '', ''
    try:
        raw_groups = _scan(command)
    except ValueError:
        return True, '', ''
    if (len(raw_groups) != len(groups) or any(separator not in {'', '|'} for _, separator in raw_groups)
            or any(char in command for char in '$`\\')
            or any(char in _mask_quoted_literals(command) for char in '<>*?[]~{}')):
        return True, '', ''
    import shlex
    raw_words = shlex.split(raw_groups[0][0])
    if raw_words and PurePath(raw_words[0]).name == 'env' and any(word.startswith('-') for word in raw_words[1:2]):
        return True, '', ''
    if sum(len(words) for words, _ in groups) > 256:
        return True, '', ''
    for words, assignments in groups[1:]:
        # Only inert stream filters may share the package child's route.
        if (assignments or not words or words[0] not in {'head', 'tail', 'wc'}
                or any(not re.fullmatch(r'-[A-Za-z]+|-?\d+', word) for word in words[1:])):
            return True, '', ''
    words, assignments = groups[0]
    if PurePath(words[0]).name == 'env' or any(name in assignments for name in ('PATH', 'PWD', 'VIRTUAL_ENV')):
        return True, '', ''
    env = {**child_env, **assignments}
    if any(name.lower() in _PROXY_NAMES for name in env):
        return True, '', ''
    if any(name in env for name in ('SSL_CLIENT_CERT', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'SSL_CERT_FILE', 'SSL_CERT_DIR')):
        return True, '', ''
    family, operation, arguments = installs[0][1]
    parsed = _parse_requirements(arguments, family, operation)
    if parsed is None:
        return True, '', ''
    explicit_index, no_config = parsed
    index = _known_index(family, env, explicit_index)
    if index is None or not _config_is_neutral(family, operation, env, cwd=cwd, no_config=no_config):
        return True, '', ''
    # Inline bypasses cannot be changed by a child environment overlay.
    bypasses = [env[name] for name in ('no_proxy', 'NO_PROXY') if name in env]
    if len(set(bypasses)) > 1 or any('/' in value for value in bypasses):
        # Package clients disagree on CIDR bypasses; urllib's matcher cannot
        # prove those semantics. Leave the existing client configuration alone.
        return True, '', ''
    endpoint = urlsplit(index)
    host = endpoint.hostname or ''
    if bypasses and proxy_bypass_environment(
            host + (':' + str(endpoint.port) if endpoint.port else ''), {'no': bypasses[0]}):
        return True, '', ''
    return True, f'package:{family}:{operation}', index
