"""lib/mcp/updates.py — Upstream version checks + one-click updates for MCP servers.

Installed stdio servers are launched through a package runner (``npx`` for
npm packages, ``uvx`` for PyPI packages). The pinned (or cached) copy ages
silently: the catalog card keeps working while upstream ships fixes. This
module answers two questions for the settings UI:

  1. **check** — is there a newer version upstream than what this server's
     stored launch args would start? (``check_all_updates``)
  2. **apply** — rewrite the stored args to pin the latest upstream release
     and reconnect so the new code is what actually runs. (``apply_update``)

Only stdio servers launched via ``npx``/``uvx`` with a registry-resolvable
package are updatable. Remote-transport servers (URL-based), vendored
launchers, and local-path ``--from`` specs have no upstream registry to
compare against and report ``updatable: False``.

Latest-version lookups go through a short-TTL cache so the settings panel
can re-poll cheaply; ``apply_update`` always refreshes so it pins the true
latest rather than a cached sighting.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from lib.http_client import async_http_get
from lib.log import get_logger
from lib.ttl_cache import TTLCache

logger = get_logger(__name__)

# ── Upstream registries ─────────────────────────────────────────────

_PYPI_JSON_URL = 'https://pypi.org/pypi/{package}/json'
_NPM_LATEST_URL = 'https://registry.npmjs.org/{package}/latest'

# Latest-release sightings. Short TTL: a settings-panel revisit within the
# window is free, yet a freshly-published upstream surfaces within the hour.
_LATEST_CACHE = TTLCache(ttl=3600, max_size=256, name='mcp_upstream_latest')
_FETCH_TIMEOUT = 10.0


# ── Version comparison ──────────────────────────────────────────────
# Tolerant semver/PEP-440 subset: numeric release segments compared
# numerically; dev < alpha/a < beta/b < rc < (final) < post. Returns None
# when either side cannot be parsed — callers must treat "unknown" as
# "cannot decide", never as "up to date".

_PRE_RANK = {
    'dev': -4, 'a': -3, 'alpha': -3, 'b': -2, 'beta': -2,
    'rc': -1, '': 0, 'post': 1,
}
_VERSION_RE = re.compile(r'^(\d+(?:\.\d+)*)(.*)$')
_PRE_RE = re.compile(r'^[-._]?(dev|alpha|a|beta|b|rc|post)[-._]?(\d*)')


def _parse_version(value: str) -> tuple | None:
    text = (value or '').strip().lstrip('vV')
    if not text:
        return None
    match = _VERSION_RE.match(text)
    if not match:
        return None
    release = tuple(int(part) for part in match.group(1).split('.'))
    rest = match.group(2)
    pre = (0, 0)
    if rest:
        pre_match = _PRE_RE.match(rest)
        if not pre_match:
            return None
        pre = (_PRE_RANK[pre_match.group(1).lower()],
               int(pre_match.group(2) or 0))
    return (release, pre)


def compare_versions(a: str, b: str) -> int | None:
    """Compare two version strings: -1/0/1, or None when unparseable."""
    key_a = _parse_version(a)
    key_b = _parse_version(b)
    if key_a is None or key_b is None:
        return None
    rel_a, rel_b = list(key_a[0]), list(key_b[0])
    width = max(len(rel_a), len(rel_b))
    rel_a += [0] * (width - len(rel_a))
    rel_b += [0] * (width - len(rel_b))
    if rel_a != rel_b:
        return -1 if rel_a < rel_b else 1
    if key_a[1] != key_b[1]:
        return -1 if key_a[1] < key_b[1] else 1
    return 0


# ── Launch-arg parsing ──────────────────────────────────────────────

# npm dist-tags and PEP 440 range operators mark a FLOATING spec — the
# launched version is whatever the runner cached, so the stored args alone
# cannot name a current version (the live handshake version is used instead,
# when the server is connected).
_NPM_DIST_TAGS = {'latest', 'next', 'beta', 'alpha', 'canary', 'nightly'}

# Runner flags that consume the FOLLOWING token as their value — skipped
# when hunting for the package positional.
_NPX_VALUE_FLAGS = {'-p', '--package', '-c', '--call'}
_UVX_VALUE_FLAGS = {
    '--with', '--python', '--index-url', '--default-index',
    '--extra-index-url', '--find-links', '--index', '--exclude-newer',
}

# PEP 508-ish: name[extras]<op><version>
_FROM_SPEC_RE = re.compile(
    r'^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)'
    r'(\[[^\]]*\])?'
    r'\s*(==|>=|<=|~=|!=|>|<)?'
    r'\s*([^,;\s]+)?$')


class PackageRef(dict):
    """A parsed launch spec: where to check + where to rewrite.

    Keys: ``source`` ('npm'|'pypi'), ``package``, ``extras`` (PEP 508 extras
    bracket string, '' otherwise), ``current`` (pinned version or ''),
    ``pinned`` (bool), ``arg_index`` (position of the spec token in args).
    """


def _parse_npm_token(token: str) -> tuple[str, str, bool] | None:
    """Split an npm package token into (package, version, pinned)."""
    if not token:
        return None
    if token.startswith('@'):                       # scoped: @scope/name[@ver]
        slash = token.find('/')
        if slash < 0:
            return None
        at = token.find('@', slash)
    else:
        at = token.find('@')
    if at < 0:
        return token, '', False
    package, tag = token[:at], token[at + 1:]
    if not package or not tag:
        return None
    if tag in _NPM_DIST_TAGS or not tag[0].isdigit():
        return package, '', False                  # dist-tag → floating
    return package, tag, True


def _parse_pypi_spec(spec: str) -> tuple[str, str, str, bool] | None:
    """Split a PEP 508-ish spec into (package, extras, version, pinned)."""
    text = (spec or '').strip()
    if not text:
        return None
    # A local path / archive has no upstream registry to poll.
    if (text.startswith(('/', './', '../', '~'))
            or '/' in text or os.sep in text
            or text.endswith(('.whl', '.tar.gz', '.zip'))):
        return None
    match = _FROM_SPEC_RE.match(text)
    if not match:
        return None
    package = match.group(1)
    extras = match.group(2) or ''
    op = match.group(3) or ''
    version = match.group(4) or ''
    if op == '==' and version:
        return package, extras, version, True
    # Bare name or a range (>=, ~=, …): floating — resolved version unknown.
    return package, extras, '', False


def parse_package_ref(cfg: dict) -> tuple[PackageRef | None, str]:
    """Parse a stored server config into a :class:`PackageRef`.

    Returns ``(ref, '')`` on success, ``(None, reason)`` otherwise where
    reason is one of: ``remote-transport``, ``unsupported-launcher``,
    ``local-path``, ``no-package``.
    """
    from lib.mcp.transport import is_stdio

    if not is_stdio(cfg):
        return None, 'remote-transport'
    command = os.path.basename(str((cfg or {}).get('command') or ''))
    args = list((cfg or {}).get('args') or [])

    if command == 'npx':
        index = _first_positional(args, _NPX_VALUE_FLAGS)
        if index is None:
            return None, 'no-package'
        parsed = _parse_npm_token(args[index])
        if parsed is None:
            return None, 'no-package'
        package, version, pinned = parsed
        return PackageRef(source='npm', package=package, extras='',
                          current=version, pinned=pinned,
                          arg_index=index), ''

    if command == 'uvx':
        if '--from' in args:
            from_at = args.index('--from')
            if from_at + 1 >= len(args):
                return None, 'no-package'
            index = from_at + 1
            parsed = _parse_pypi_spec(args[index])
            if parsed is None:
                return None, 'local-path'
        else:
            index = _first_positional(args, _UVX_VALUE_FLAGS)
            if index is None:
                return None, 'no-package'
            token = args[index]
            if token.endswith('@latest'):
                token = token[:-len('@latest')]
            parsed = _parse_pypi_spec(token)
            if parsed is None:
                return None, 'local-path'
        package, extras, version, pinned = parsed
        return PackageRef(source='pypi', package=package, extras=extras,
                          current=version, pinned=pinned,
                          arg_index=index), ''

    return None, 'unsupported-launcher'


def _first_positional(args: list[str], value_flags: set[str]) -> int | None:
    """Index of the first non-flag token (the package spec), skipping
    flag/value pairs and the ``--`` separator."""
    skip_next = False
    for index, token in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if token == '--':
            continue
        if token in value_flags:
            skip_next = True
            continue
        if token.startswith('-'):
            continue
        return index
    return None


# ── Upstream latest-version lookup ──────────────────────────────────

async def fetch_latest_version(source: str, package: str,
                               *, refresh: bool = False) -> str:
    """Latest published version for an npm/PyPI package ('' on failure).

    Cached for an hour per (source, package); ``refresh=True`` bypasses and
    re-populates the cache (used by apply so the pin is the true latest).
    """
    key = (source, package)
    if refresh:
        _LATEST_CACHE.invalidate(key)
    cached = _LATEST_CACHE.get(key)
    if cached is not None:
        return cached

    if source == 'pypi':
        url = _PYPI_JSON_URL.format(package=package)
    elif source == 'npm':
        # Scoped packages need an encoded slash: @scope/name → @scope%2Fname.
        from urllib.parse import quote
        url = _NPM_LATEST_URL.format(package=quote(package, safe='@'))
    else:
        logger.warning('[MCP:Updates] unknown source %r for %s', source, package)
        return ''

    try:
        resp = await async_http_get(url, timeout=_FETCH_TIMEOUT)
        if resp.status_code != 200:
            logger.info('[MCP:Updates] %s lookup for %s → HTTP %s',
                        source, package, resp.status_code)
            return ''
        data = resp.json()
        if source == 'pypi':
            version = str((data.get('info') or {}).get('version') or '')
        else:
            version = str(data.get('version') or '')
    except Exception as e:
        logger.warning('[MCP:Updates] %s latest lookup failed for %s: %s',
                       source, package, e)
        return ''
    if not version or _parse_version(version) is None:
        logger.warning('[MCP:Updates] %s lookup for %s returned bad version %r',
                       source, package, version[:80])
        return ''
    _LATEST_CACHE.set(key, version)
    logger.debug('[MCP:Updates] %s latest for %s = %s', source, package, version)
    return version


# ── Check ───────────────────────────────────────────────────────────

async def check_server_update(name: str, cfg: dict,
                              live_version: str = '') -> dict[str, Any]:
    """Update status for one configured server.

    ``live_version`` is the version reported by the running server's MCP
    handshake — the only "current" signal available for floating (unpinned)
    specs, since the stored args name no version.
    """
    ref, reason = parse_package_ref(cfg)
    if ref is None:
        return {'updatable': False, 'reason': reason}

    latest = await fetch_latest_version(ref['source'], ref['package'])
    current = ref['current']
    if not current and live_version and _parse_version(live_version):
        current = live_version.strip().lstrip('vV')

    update_available: bool | None = None
    if latest and current:
        newer = compare_versions(latest, current)
        update_available = bool(newer and newer > 0)

    return {
        'updatable': True,
        'source': ref['source'],
        'package': ref['package'],
        'pinned': ref['pinned'],
        'current': current,
        'latest': latest,
        'update_available': update_available,
        'error': '' if latest else 'lookup-failed',
    }


async def check_all_updates() -> dict[str, dict[str, Any]]:
    """Check every configured server; failures are per-server, never fatal."""
    from lib.mcp import get_bridge
    from lib.mcp.config import load_mcp_config

    config = load_mcp_config()
    live_versions: dict[str, str] = {}
    try:
        for info in get_bridge().list_servers():
            version = info.get('server_version') or ''
            if version:
                live_versions[info['name']] = version
    except Exception as e:
        logger.warning('[MCP:Updates] live version harvest failed: %s', e)

    async def _one(name: str, cfg: dict) -> tuple[str, dict[str, Any]]:
        try:
            return name, await check_server_update(
                name, cfg, live_versions.get(name, ''))
        except Exception as e:
            logger.warning('[MCP:Updates] check failed for %s: %s', name, e)
            return name, {'updatable': False, 'reason': 'check-error'}

    pairs = await asyncio.gather(
        *[_one(name, cfg) for name, cfg in config.items()])
    return dict(pairs)


# ── Apply ───────────────────────────────────────────────────────────

def build_updated_args(cfg: dict, ref: PackageRef, latest: str) -> list[str]:
    """Rewrite the launch args so they pin ``latest``.

    The package token is swapped in place (position ``ref['arg_index']``),
    preserving executable args, extras and every unrelated flag. A
    ``--exclude-newer`` cutoff is dropped: it exists to cap floating
    resolution at a reviewed date, and would reject the deliberately-newer
    pin the user just asked for.
    """
    args = list(cfg.get('args') or [])
    if ref['source'] == 'npm':
        args[ref['arg_index']] = f"{ref['package']}@{latest}"
    else:
        args[ref['arg_index']] = f"{ref['package']}{ref['extras']}=={latest}"
        while '--exclude-newer' in args:
            at = args.index('--exclude-newer')
            del args[at:at + 2]
    return args


async def apply_update(name: str) -> dict[str, Any]:
    """Pin the stored config to the latest upstream release + reconnect.

    Returns a result dict; raises ``KeyError`` for an unknown server and
    ``ValueError`` for a non-updatable one. A reconnect failure propagates
    as ``MCPConnectError`` AFTER the config was already updated — the route
    surfaces that split state exactly like the install flow does.
    """
    from lib.mcp import get_bridge
    from lib.mcp.config import load_mcp_config, patch_server

    config = load_mcp_config()
    cfg = config.get(name)
    if not isinstance(cfg, dict):
        raise KeyError(name)
    ref, reason = parse_package_ref(cfg)
    if ref is None:
        raise ValueError(reason)

    latest = await fetch_latest_version(ref['source'], ref['package'],
                                        refresh=True)
    if not latest:
        return {'updated': False, 'error': 'lookup-failed',
                'package': ref['package'], 'source': ref['source']}

    current = ref['current']
    if current:
        newer = compare_versions(latest, current)
        if not newer or newer <= 0:
            return {'updated': False, 'already_latest': True,
                    'version': current, 'latest': latest,
                    'package': ref['package'], 'source': ref['source']}

    new_args = build_updated_args(cfg, ref, latest)
    if patch_server(name, {'args': new_args}) is None:
        raise KeyError(name)
    logger.info('[MCP:Updates] %s: %s %s → %s (args rewritten, env preserved)',
                name, ref['package'], current or '(floating)', latest)

    result: dict[str, Any] = {
        'updated': True, 'version': latest, 'previous': current,
        'package': ref['package'], 'source': ref['source'],
        'reconnected': False,
    }
    if not cfg.get('enabled', True):
        return result

    bridge = get_bridge()
    connected = {s['name'] for s in bridge.list_servers()}
    if name in connected:
        try:
            bridge._disconnect_one(name, forget=True)
        except Exception as e:
            logger.warning('[MCP:Updates] pre-update disconnect %s failed: %s',
                           name, e)
    refreshed = load_mcp_config().get(name, {})
    tools = bridge.connect_server(name, refreshed)   # may raise MCPConnectError
    result['reconnected'] = True
    result['tools_count'] = len(tools)
    result['tool_names'] = [t.name for t in tools]
    logger.info('[MCP:Updates] %s reconnected on v%s with %d tools',
                name, latest, len(tools))
    return result


__all__ = [
    'PackageRef', 'parse_package_ref', 'compare_versions',
    'fetch_latest_version', 'check_server_update', 'check_all_updates',
    'build_updated_args', 'apply_update',
]
