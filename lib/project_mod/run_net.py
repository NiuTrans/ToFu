"""lib/project_mod/run_net.py — network layer for run_command subprocesses.

Python-side traffic already rides the adaptive network layer
(``lib.proxy`` + ``lib.netpath``); shell commands spawned by
``run_command`` (curl / pip / npm / conda / git …) did not: they
inherited whatever ``http_proxy`` the server booted with, their
successes never trained the scorer, and a failure came back to the
model as raw stderr with no hint about which path to try next.

This module closes the loop with two seams (mirroring the grep-redirect
pattern: plan before, teach after):

1. **Pre-exec route injection** — :func:`plan` detects network commands,
   asks netpath for each target host's measured best route and emits a
   child-env overlay (``http_proxy`` / ``no_proxy`` / package-mirror
   knobs) that :func:`env_overlay` applies.  The command string itself
   is NEVER rewritten; a route the scorer cannot vouch for changes
   nothing (deployment default).
2. **Post-exec outcome feed + diagnosis** — :func:`finalize` attributes
   the exit result to the routes/mirrors actually used (so subprocess
   traffic trains the same scorer as app traffic) and, on network-class
   failures, appends a short ``[network diagnosis]`` block telling the
   model what failed, which alternatives are healthy, and the exact
   next step — no more blind retry roulette.

Safety rules:

- Proxy credentials enter the CHILD ENV only (the pool's resolved URLs);
  diagnosis text carries route LABELS, never URLs with userinfo.
- Explicit bypass rules still win: a host ``lib.proxy`` reduces to
  direct-only is never pinned onto a proxy.
- Localhost / IP-literal targets are never touched.

Env knobs:
  ``TOFU_NETCMD``         master switch (default: on)
  ``TOFU_NETCMD_INJECT``  pre-exec env injection (default: on; off =
                          diagnosis + learning only)
"""

from __future__ import annotations

import ipaddress
import os
import re
import shlex
from urllib.parse import urlparse

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['plan', 'env_overlay', 'finalize', 'classify_failure',
           'NetPlan']

# ═════════════════════════════════════════════════════════════
#  Command-shape detection
# ═════════════════════════════════════════════════════════════

#: tool token → package ecosystem (mirror registry domain)
_TOOL_ECOSYSTEM = {
    'pip': 'pypi', 'pip3': 'pypi', 'pipx': 'pypi', 'uv': 'pypi',
    'npm': 'npm', 'npx': 'npm', 'yarn': 'npm', 'pnpm': 'npm',
    'conda': 'conda', 'mamba': 'conda', 'micromamba': 'conda',
}
#: tools that always mean network when present
_GENERIC_TOOLS = frozenset({'curl', 'wget'})
#: git only talks to the network for these subcommands
_GIT_NET_SUBCMDS = frozenset(
    {'clone', 'fetch', 'pull', 'push', 'ls-remote', 'submodule'})
#: ecosystem → subcommands that actually hit the network
_TOOL_NET_SUBCMDS = {
    'pypi': frozenset({'install', 'download', 'wheel', 'search', 'sync',
                       'add', 'remove', 'lock', 'publish', 'exec'}),
    'npm': frozenset({'install', 'i', 'ci', 'add', 'update', 'up',
                      'remove', 'rm', 'publish', 'view', 'info', 'search',
                      'create', 'init', 'exec', 'dlx'}),
    'conda': frozenset({'install', 'create', 'update', 'upgrade',
                        'search'}),
}
#: ecosystem → hosts its tooling contacts by default
_ECOSYSTEM_HOSTS = {
    'pypi': ('pypi.org', 'files.pythonhosted.org'),
    'npm': ('registry.npmjs.org',),
    'conda': ('conda.anaconda.org', 'repo.anaconda.com'),
}

_URL_RE = re.compile(r'https?://[^\s\'"<>|;&$`]+')
_SEGMENT_SPLIT_RE = re.compile(r'\|\||&&|[|;\n]')
_ENV_ASSIGN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
_EXIT_CODE_RE = re.compile(r'\[exit code: (-?\d+)\]\s*$')
_SHELL_WRAPPERS = frozenset({'sudo', 'env', 'command', 'builtin', 'time'})

_PROXY_VARS = ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY')


def _enabled() -> bool:
    return os.environ.get('TOFU_NETCMD', 'on').strip().lower() not in (
        '0', 'off', 'false', 'no')


def _inject_enabled() -> bool:
    return os.environ.get('TOFU_NETCMD_INJECT', 'on').strip().lower() not in (
        '0', 'off', 'false', 'no')


def _is_exempt_host(host: str) -> bool:
    """localhost / IP literals: routing them is meaningless and dangerous."""
    h = (host or '').lower().strip('[]')
    if h in ('localhost', 'localhost.localdomain'):
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def _segment_commands(command: str) -> list:
    """First meaningful token of each pipeline/&& segment (cheap shlex)."""
    tokens = []
    for segment in _SEGMENT_SPLIT_RE.split(command or ''):
        try:
            parts = shlex.split(segment.strip())
        except ValueError:
            parts = segment.strip().split()
        idx = 0
        while idx < len(parts) and _ENV_ASSIGN_RE.match(parts[idx]):
            idx += 1
        while idx < len(parts) and parts[idx] in _SHELL_WRAPPERS:
            idx += 1
        if idx < len(parts):
            tokens.append((parts[idx].rsplit('/', 1)[-1],
                           parts[idx + 1:]))
    return tokens


def _detect_tools(command: str) -> 'tuple[set, set]':
    """(ecosystems, generic-tools) the command will exercise."""
    ecosystems = set()
    generic = set()
    for exe, args in _segment_commands(command):
        if exe in _GENERIC_TOOLS:
            generic.add(exe)
        elif exe == 'git':
            if any(a in _GIT_NET_SUBCMDS for a in args):
                generic.add('git')
        elif exe in _TOOL_ECOSYSTEM:
            eco = _TOOL_ECOSYSTEM[exe]
            net_subs = _TOOL_NET_SUBCMDS.get(eco, frozenset())
            if not args or any(a in net_subs for a in args):
                ecosystems.add(eco)
    return ecosystems, generic


def _extract_hosts(command: str) -> list:
    hosts = []
    for match in _URL_RE.finditer(command or ''):
        try:
            host = (urlparse(match.group(0)).hostname or '').lower()
        except ValueError:
            continue
        if host and not _is_exempt_host(host) and host not in hosts:
            hosts.append(host)
    return hosts


# ═════════════════════════════════════════════════════════════
#  The plan
# ═════════════════════════════════════════════════════════════

class NetPlan:
    """What run_command learned about one command's network needs."""

    __slots__ = ('ecosystems', 'tools', 'hosts', 'overlay',
                 'mirror_uses', 'notes')

    def __init__(self):
        self.ecosystems = set()          # {'pypi', ...}
        self.tools = set()               # {'curl', 'git', ...}
        self.hosts = {}                  # host → route id used (None = default)
        self.overlay = {}                # env var → value | None (None = unset)
        self.mirror_uses = {}            # ecosystem → mirror entry id
        self.notes = []                  # human-readable injection notes

    def __bool__(self):
        return bool(self.hosts or self.overlay or self.mirror_uses)


def _np():
    try:
        from lib import netpath
        return netpath
    except Exception as e:
        logger.debug('[RunNet] netpath unavailable: %s', e)
        return None


def _upstream_bad(netpath, ecosystem: str) -> bool:
    """True when EVERY available route to the ecosystem's upstream is bad."""
    try:
        from lib import netmirrors
        host = netmirrors.UPSTREAM_HOSTS.get(ecosystem)
    except Exception:
        host = None
    if not netpath or not host:
        return False
    st = netpath.host_status(host)
    if not st:
        return False
    avail = [r for r, v in st['routes'].items() if v['available']]
    return bool(avail) and all(st['routes'][r]['bad'] for r in avail)


def plan(command: str) -> 'NetPlan | None':
    """Analyse *command* and produce a NetPlan (None = not a network command).

    Pure string analysis + learned-state reads; never probes the network,
    never raises, and never rewrites the command.
    """
    if not _enabled() or not command:
        return None
    try:
        return _plan(command)
    except Exception as e:
        logger.debug('[RunNet] planning failed (command runs unmanaged): %s', e)
        return None


def _plan(command: str) -> 'NetPlan | None':
    ecosystems, generic = _detect_tools(command)
    if not ecosystems and not generic:
        return None
    p = NetPlan()
    p.ecosystems = ecosystems
    p.tools = generic | {
        exe for exe, _ in _segment_commands(command)
        if exe in _TOOL_ECOSYSTEM}

    # ── target hosts: explicit URLs + ecosystem defaults ──
    hosts = _extract_hosts(command)
    for eco in ecosystems:
        for host in _ECOSYSTEM_HOSTS.get(eco, ()):
            if host not in hosts:
                hosts.append(host)
    netpath = _np()

    inject = _inject_enabled()
    proxy_overlay = {}       # route → proxy url (for _PROXY_VARS)
    no_proxy_add = []
    for host in hosts:
        decision = None
        if netpath is not None:
            netpath.note_url('https://%s/' % host)
            decision = netpath.decide(host)
        p.hosts[host] = decision
        if not inject or not decision or decision == 'env':
            continue
        if decision == 'direct':
            no_proxy_add.append(host)
            continue
        # A pinned pool route: resolve its proxy URL for the child env.
        try:
            from lib import proxy as lib_proxy
            resolved = lib_proxy.proxies_for_route(decision, host)
        except Exception as e:
            logger.debug('[RunNet] route resolve failed for %s: %s',
                         decision, e)
            resolved = None
        if resolved:
            proxy_overlay[decision] = resolved.get('https') or resolved.get(
                'http')
            p.notes.append('%s → pinned route %s' % (host, decision))

    if inject:
        if proxy_overlay:
            # One proxy URL for the child; multiple distinct pinned pool
            # routes collapse to the first (per-host proxy env is not a
            # thing curl/pip understand).
            url = next(iter(proxy_overlay.values()))
            for var in _PROXY_VARS:
                p.overlay[var] = url
        if no_proxy_add:
            base = os.environ.get('no_proxy') or os.environ.get(
                'NO_PROXY') or ''
            merged = ','.join(
                [x for x in (base.split(',') + no_proxy_add) if x.strip()])
            p.overlay['no_proxy'] = merged
            p.overlay['NO_PROXY'] = merged
            p.notes.append('direct: %s' % ', '.join(no_proxy_add))

    # ── package mirrors ──
    for eco in sorted(ecosystems):
        try:
            from lib import netmirrors
            entry = netmirrors.best(eco)
        except Exception as e:
            logger.debug('[RunNet] mirror lookup failed for %s: %s', eco, e)
            entry = None
        if not entry:
            continue
        use = bool(entry.get('preferred')) or _upstream_bad(netpath, eco)
        if not use:
            continue
        p.mirror_uses[eco] = entry['id']
        if inject:
            p.overlay.update(netmirrors.env_overlay(eco, entry))
            p.notes.append('%s → mirror %s (%s)'
                           % (eco, entry['id'], entry.get('label') or ''))

    if p.notes:
        logger.info('[RunNet] %s', '; '.join(p.notes))
    return p if bool(p) else None


def env_overlay(p: 'NetPlan | None') -> 'dict | None':
    """The child-env overlay for a plan (None when injection is off)."""
    if p is None or not _inject_enabled():
        return None
    return dict(p.overlay) if p.overlay else None


# ═════════════════════════════════════════════════════════════
#  Outcome feed + diagnosis
# ═════════════════════════════════════════════════════════════

#: (category, regex, human description) — first match wins.
_FAILURE_PATTERNS = (
    ('proxy_auth', re.compile(
        r'407|Proxy Authentication Required', re.I),
     'the proxy rejected authentication (HTTP 407)'),
    ('forbidden', re.compile(
        r'403\s+Forbidden|HTTP/[0-9.]+ 403|ERROR 403', re.I),
     'the proxy/gateway forbids this target (HTTP 403)'),
    ('dns', re.compile(
        r'Name or service not known|Temporary failure in name resolution|'
        r'Could not resolve (host|proxy)|nodename nor servname|getaddrinfo|'
        r'NameResolutionError|DNS', re.I),
     'DNS resolution failed'),
    ('timeout', re.compile(
        r'timed out|timeout expired|ReadTimeout|ConnectTimeout', re.I),
     'the connection timed out'),
    ('refused', re.compile(
        r'Connection refused|Failed to connect|No route to host', re.I),
     'the connection was refused'),
    ('reset', re.compile(
        r'Connection reset|Broken pipe|ECONNRESET', re.I),
     'the connection was reset'),
    ('tls', re.compile(
        r'SSL:|certificate verify failed|TLS handshake|CERTIFICATE', re.I),
     'the TLS handshake failed'),
    ('http5xx', re.compile(
        r'502 Bad Gateway|503 Service|504 Gateway', re.I),
     'the gateway returned 5xx'),
)
_NETWORK_CATEGORIES = frozenset(
    cat for cat, _, _ in _FAILURE_PATTERNS)


def classify_failure(text: str) -> 'str | None':
    """Network failure category for stderr/stdout text (None = not network)."""
    if not text:
        return None
    for cat, pattern, _desc in _FAILURE_PATTERNS:
        if pattern.search(text):
            return cat
    return None


def _route_label(route_id) -> str:
    if not route_id:
        return 'deployment default'
    if route_id == 'direct':
        return 'direct'
    if route_id == 'env':
        return 'environment proxy'
    if isinstance(route_id, str) and route_id.startswith('pool:'):
        try:
            from lib import proxy as lib_proxy
            for entry in lib_proxy.get_proxy_pool():
                if entry.get('id') == route_id[5:]:
                    return 'proxy ' + (entry.get('name')
                                       or entry.get('id') or '?')
        except Exception:
            pass
        return 'proxy ' + route_id[5:]
    return str(route_id)


def _failure_desc(category: str) -> str:
    for cat, _pattern, desc in _FAILURE_PATTERNS:
        if cat == category:
            return desc
    return category


def _diagnosis(p: NetPlan, category: str, timed_out: bool) -> str:
    """Compact model-facing block: what failed, what's healthy, what next."""
    netpath = _np()
    lines = ['[network diagnosis]']
    if timed_out:
        lines.append('- failure class: the command timed out (network '
                     'stall suspected)')
    else:
        lines.append('- failure class: %s' % _failure_desc(category))

    for host, route in sorted(p.hosts.items()):
        lines.append('- %s was contacted via "%s"'
                     % (host, _route_label(route)))
        if netpath is None:
            continue
        st = netpath.host_status(host)
        if not st:
            continue
        bits = []
        for rid, info in sorted(st['routes'].items()):
            if not info['available']:
                continue
            state = ('%dms' % info['ms']) if info['ms'] is not None \
                else 'unmeasured'
            if info['bad']:
                state += ' (marked bad)'
            bits.append('%s: %s' % (_route_label(rid), state))
        if bits:
            lines.append('  route health: ' + '; '.join(bits))
        new = st.get('decision')
        if new and new != route:
            lines.append('  → netpath has repinned %s to "%s" — retrying '
                         'the same command now uses the better route'
                         % (host, _route_label(new)))

    # Mirror guidance per ecosystem.
    for eco in sorted(p.ecosystems):
        try:
            from lib import netmirrors
            entry = netmirrors.best(eco)
        except Exception:
            entry = None
        if not entry:
            continue
        used = p.mirror_uses.get(eco) == entry['id']
        if used:
            lines.append('- mirror %s (%s) was used and also failed; it is '
                         'now cooling down and will be avoided'
                         % (entry['id'], entry.get('label') or ''))
        else:
            overlay = netmirrors.env_overlay(eco, entry)
            hint = ' '.join('%s=%s' % kv for kv in overlay.items())
            lines.append('- mirror option for %s: prepend `%s` '
                         '(%s)' % (eco, hint, entry.get('label') or ''))

    if category == 'proxy_auth':
        lines.append('- a 407 means the proxy credential is wrong/expired — '
                     'check Settings → Network proxy pool credentials')
    lines.append('(network layer: TOFU_NETCMD=0 disables; '
                 'TOFU_NETCMD_INJECT=0 disables route injection only)')
    return '\n' + '\n'.join(lines) + '\n'


def _feed(p: NetPlan, ok: bool, elapsed_ms: 'float | None') -> None:
    """Attribute the outcome to every route/mirror the command used."""
    netpath = _np()
    if netpath is not None:
        for host, route in p.hosts.items():
            try:
                netpath.report_outcome(
                    'https://%s/' % host, ok,
                    elapsed_ms if ok else None,
                    path=route if route else None)
            except Exception as e:
                logger.debug('[RunNet] netpath feed failed for %s: %s',
                             host, e)
    for eco, entry_id in p.mirror_uses.items():
        try:
            from lib import netmirrors
            netmirrors.report_outcome(
                entry_id, ok, elapsed_ms if ok else None)
        except Exception as e:
            logger.debug('[RunNet] mirror feed failed for %s: %s',
                         entry_id, e)


def finalize(p: 'NetPlan | None', command: str, result_text: str,
             elapsed_ms: 'float | None' = None) -> str:
    """Feed the outcome back and append a diagnosis on network failures.

    Never raises and never alters a successful result.  ``result_text``
    must be the standard ``_format_run_output`` product (exit-code
    marker at the tail).
    """
    if p is None or not _enabled():
        return result_text
    try:
        return _finalize(p, command, result_text, elapsed_ms)
    except Exception as e:
        logger.debug('[RunNet] finalize skipped: %s', e)
        return result_text


def _finalize(p: NetPlan, command: str, result_text: str,
              elapsed_ms: 'float | None') -> str:
    if ('[Command aborted by user]' in result_text
            or '[Command interrupted by' in result_text):
        return result_text   # user action — not a route signal
    timed_out = '[Command timed out]' in result_text
    match = _EXIT_CODE_RE.search(result_text or '')
    if match:
        exit_code = int(match.group(1))
    else:
        exit_code = -1 if timed_out else None
    if exit_code is None:
        return result_text

    if exit_code == 0:
        _feed(p, True, elapsed_ms)
        return result_text

    category = 'timeout' if timed_out else classify_failure(result_text)
    if category not in _NETWORK_CATEGORIES:
        return result_text   # a real build/test/app failure, not the network
    _feed(p, False, None)
    return result_text + _diagnosis(p, category, timed_out)
