"""lib/tools/environment_capabilities.py — the environment-capability snapshot.

The one place that decides what THIS kernel can run. A Tofu server runs on a
desktop, on a minimal remote host, or in a phone's proot userland (the Android
local kernel, docs/ANDROID_KERNELS.md), and not every host has git, Node.js or
a browser. Tool families declare the capabilities their tools need
(``ToolSpec.environment_requirements_by_name``); tool assembly withholds a tool
whose requirement is unavailable from both the wire and the executable
catalog, and a call to it gets :func:`environment_unavailable_tool`'s
actionable refusal instead of failing inside the tool.

Entry points:

* :func:`environment_snapshot` — the current snapshot. The first read in a
  process probes: the server's boot reads it right after the readiness gate
  opens (a deferred boot step), unless a turn read it first.
* :func:`record_environment_launch_facts` — boot, before readiness: records
  the launch fact "local-kernel mode" without probing.
* :func:`mark_environment_stale` — the refresh event. Every finished model
  shell command marks the snapshot stale (a command is how a package gets
  installed through a tool); the next read re-probes.
* :func:`refresh_environment_snapshot` — an explicit re-scan that probes now.
* :func:`environment_unavailable_tool` — the refusal for a registered tool
  whose declared capability is missing.
* :func:`probe_environment` — the pure probe over an explicit environment
  mapping (tests pass a PATH without git or node).

A re-probe that observes the same result keeps the snapshot and its revision;
only a different result publishes a new revision. Tool exposure is recomputed
once per turn (tool assembly at turn start), so a refresh applies from the next
turn and never changes the schemas of a running one.

Probes are presence checks only and never start a process, open a socket or
import a capability runtime: PATH resolution against the mapping a tool's
child process receives (:mod:`lib.tools.process_environment`), a Playwright
Chromium build on disk (``chromium_env``, never launched), the LAN-discovery
policy (:mod:`lib.desktop.pairing`), and the local-kernel launch fact.

Dependencies: standard library at import time; the probe imports
``lib.tools.process_environment``, ``lib.compat``, ``chromium_env`` and
``lib.desktop.pairing`` lazily, and the refusal reads registry specs lazily.
It lives beside ``process_environment`` rather than under
``lib/tools/registry/`` because importing anything in that package registers
every built-in and discovers plugins, while route modules (which declare
:data:`ENVIRONMENT_SECTION_SCHEMA` at import) and the command runner (which
emits the refresh event) must stay import-safe.
"""

from __future__ import annotations

import importlib.util
import os
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


SHELL = 'shell'
GIT = 'git'
PYTHON = 'python'
UV = 'uv'
NODE = 'node'
BROWSER = 'browser'
DEVICES = 'devices'
LAN_DISCOVERY = 'lan_discovery'

#: The closed capability vocabulary, in projection order. A tool declares
#: requirements only from this set; adding a capability adds its probe here.
ENVIRONMENT_CAPABILITIES = (
    SHELL, GIT, PYTHON, UV, NODE, BROWSER, DEVICES, LAN_DISCOVERY,
)

#: Why a capability is unavailable (``reason`` in the public projection).
REASON_NOT_INSTALLED = 'not_installed'
REASON_LOCAL_KERNEL = 'local_kernel'
REASON_LOOPBACK_BIND = 'loopback_bind'
REASON_DISABLED = 'disabled'
ENVIRONMENT_REASONS = (
    REASON_NOT_INSTALLED, REASON_LOCAL_KERNEL, REASON_LOOPBACK_BIND,
    REASON_DISABLED,
)

#: Executables a capability needs on the child PATH (all must resolve).
_EXECUTABLES = {
    GIT: ('git',),
    PYTHON: ('python' if os.name == 'nt' else 'python3',),
    UV: ('uv', 'uvx'),
    NODE: ('node', 'npx'),
}

_LABELS = {
    SHELL: 'a POSIX shell (/bin/sh)',
    GIT: 'git',
    PYTHON: 'Python 3',
    UV: 'uv (uv and uvx)',
    NODE: 'Node.js (node and npx)',
    BROWSER: 'a headless Chromium for Playwright',
    DEVICES: 'a reachable device',
    LAN_DISCOVERY: 'LAN discovery',
}

#: Install commands for the Ubuntu userland of the Android local kernel, which
#: are also the Debian-family commands for a server host.
_INSTALL_COMMANDS = {
    SHELL: 'apt install dash',
    GIT: 'apt install git',
    PYTHON: 'apt install python3',
    UV: 'curl -LsSf https://astral.sh/uv/install.sh | sh',
    NODE: 'apt install nodejs npm',
    BROWSER: 'playwright install --only-shell chromium',
}

#: Capabilities no install provides: (what is wrong, what to do).
_UNREACHABLE = {
    DEVICES: ('devices cannot reach this loopback-only phone kernel, which '
              'refuses device pairing',
              'Use a URL or SSH kernel that devices can reach.'),
    LAN_DISCOVERY: ('LAN discovery is off for this server',
                    'Pair devices with a pairing code instead.'),
}


@dataclass(frozen=True)
class EnvironmentSnapshot:
    """One observation of this kernel's capabilities.

    ``unavailable`` maps each missing capability to its reason; an absent key
    means available. ``revision`` increases whenever a re-probe observes a
    different result, so equal revisions mean equal tool exposure.
    """

    revision: int
    local_kernel: bool
    unavailable: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        # Shared across threads and turns: a read-only view of a private copy.
        object.__setattr__(
            self, 'unavailable', MappingProxyType(dict(self.unavailable)))

    def __reduce__(self):
        return (EnvironmentSnapshot,
                (self.revision, self.local_kernel, dict(self.unavailable)))

    def available(self, capability: str) -> bool:
        return capability not in self.unavailable

    def missing(self, requirements: Iterable[str]) -> tuple[str, ...]:
        """Unavailable members of ``requirements``, in vocabulary order."""
        required = set(requirements)
        return tuple(capability for capability in ENVIRONMENT_CAPABILITIES
                     if capability in required and capability in self.unavailable)

    def public_section(self) -> dict[str, Any]:
        """The ``environment`` section of the capabilities endpoint.

        Booleans and reason codes only: the endpoint is public, so resolved
        paths (home directories, install prefixes) are never projected.
        """
        return {
            'revision': self.revision,
            'local_kernel': self.local_kernel,
            'capabilities': {
                capability: {
                    'available': capability not in self.unavailable,
                    'reason': self.unavailable.get(capability),
                }
                for capability in ENVIRONMENT_CAPABILITIES
            },
        }


#: Every capability present. Inventory generation, parity checks and the test
#: suite gate against it so their output does not depend on the host.
EVERY_CAPABILITY_AVAILABLE = EnvironmentSnapshot(revision=1, local_kernel=False)

#: JSON schema of :meth:`EnvironmentSnapshot.public_section` — the contract of
#: the capabilities endpoint's ``environment`` section.
ENVIRONMENT_SECTION_SCHEMA: dict[str, Any] = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['revision', 'local_kernel', 'capabilities'],
    'description': (
        "This kernel's environment-capability snapshot: probed once at boot "
        'and again only after a shell command finishes or an explicit '
        're-scan. Equal revisions mean equal tool exposure.'),
    'properties': {
        'revision': {'type': 'integer', 'minimum': 1},
        'local_kernel': {
            'type': 'boolean',
            'description': 'Started as the Android local kernel (local_kernel.py).',
        },
        'capabilities': {
            'type': 'object',
            'additionalProperties': False,
            'required': list(ENVIRONMENT_CAPABILITIES),
            'properties': {
                capability: {
                    'type': 'object',
                    'additionalProperties': False,
                    'required': ['available', 'reason'],
                    'properties': {
                        'available': {'type': 'boolean'},
                        'reason': {
                            'type': ['string', 'null'],
                            'enum': [*ENVIRONMENT_REASONS, None],
                        },
                    },
                }
                for capability in ENVIRONMENT_CAPABILITIES
            },
        },
    },
}


def probe_environment(environ: Mapping[str, str], *,
                      local_kernel: bool) -> dict[str, str]:
    """Return ``{capability: reason}`` for every unavailable capability.

    ``environ`` is the server's environment. Executables resolve against the
    PATH a tool's child process receives: ``environ['PATH']`` plus the
    persistent tool directories ``apply_tool_paths`` adds (bundled toolchain,
    uv tool bin, ``~/.local/bin``, cargo and npm prefixes).
    """
    from lib.compat import get_shell_args
    from lib.tools.process_environment import (
        apply_tool_paths, resolve_executable)

    child = {key: value for key, value in environ.items()
             if isinstance(key, str) and isinstance(value, str)}
    apply_tool_paths(child)
    unavailable: dict[str, str] = {}
    if resolve_executable(get_shell_args('')[0], child) is None:
        unavailable[SHELL] = REASON_NOT_INSTALLED
    for capability, executables in _EXECUTABLES.items():
        if any(resolve_executable(name, child) is None for name in executables):
            unavailable[capability] = REASON_NOT_INSTALLED
    if not _playwright_browser_present():
        unavailable[BROWSER] = REASON_NOT_INSTALLED
    if local_kernel:
        unavailable[DEVICES] = REASON_LOCAL_KERNEL
        unavailable[LAN_DISCOVERY] = REASON_LOCAL_KERNEL
    else:
        from lib.desktop.pairing import lan_discovery_refusal
        refusal = lan_discovery_refusal(
            environ, bind_host=environ.get('_TOFU_RUNTIME_HOST') or '')
        if refusal:
            unavailable[LAN_DISCOVERY] = refusal
    return unavailable


def _playwright_browser_present() -> bool:
    """The Playwright package and a Chromium build in its registry, on disk.

    The server-side pool launches Playwright's own registry build, so a system
    Chrome alone does not count. Presence only: nothing is launched.
    """
    if importlib.util.find_spec('playwright') is None:
        return False
    from chromium_env import chromium_binaries
    return bool(chromium_binaries(include_system=False))


# ── Process snapshot ─────────────────────────────────────────────────

_LOCK = threading.RLock()
_snapshot: EnvironmentSnapshot | None = None
_stale = False
_local_kernel = False
_stale_reason = ''


def _probe_locked(reason: str) -> EnvironmentSnapshot:
    global _snapshot, _stale, _stale_reason
    current = _snapshot
    _stale = False
    _stale_reason = ''
    try:
        unavailable = probe_environment(os.environ, local_kernel=_local_kernel)
    except Exception:
        # The snapshot only withholds what it observed missing; a failed
        # observation must not shrink the tool surface of every turn.
        logger.warning('[Environment] probe failed (%s); keeping %s', reason,
                       'the previous snapshot' if current is not None
                       else 'only the launch facts', exc_info=True)
        if current is not None and current.local_kernel == _local_kernel:
            return current
        unavailable = ({DEVICES: REASON_LOCAL_KERNEL,
                        LAN_DISCOVERY: REASON_LOCAL_KERNEL}
                       if _local_kernel else {})
    if (current is not None and current.local_kernel == _local_kernel
            and dict(current.unavailable) == unavailable):
        return current
    _snapshot = EnvironmentSnapshot(
        revision=(current.revision + 1) if current is not None else 1,
        local_kernel=_local_kernel,
        unavailable=unavailable,
    )
    logger.info(
        '[Environment] snapshot r%d (%s): local_kernel=%s unavailable=%s',
        _snapshot.revision, reason, _local_kernel,
        ', '.join(f'{name}:{why}' for name, why in unavailable.items()) or 'none')
    return _snapshot


def environment_snapshot() -> EnvironmentSnapshot:
    """The current snapshot, probing on the first read or after a refresh event."""
    with _LOCK:
        if _snapshot is None:
            return _probe_locked('first read')
        if _stale:
            return _probe_locked(_stale_reason or 'refresh')
        return _snapshot


def record_environment_launch_facts(app: Any) -> None:
    """Boot, before readiness: record local-kernel mode without probing.

    Local-kernel mode is the published ``app.extensions['tofu_local_kernel']``
    entry of ``local_kernel.py`` (docs/ANDROID_LOCAL_KERNEL.md); a server
    started any other way has none. A snapshot read before this call is
    re-probed on its next read.
    """
    global _local_kernel, _stale, _stale_reason
    extensions = getattr(app, 'extensions', None) or {}
    local_kernel = bool(extensions.get('tofu_local_kernel'))
    with _LOCK:
        if local_kernel != _local_kernel:
            _local_kernel = local_kernel
            if _snapshot is not None:
                _stale, _stale_reason = True, 'launch facts'


def mark_environment_stale(reason: str) -> None:
    """Refresh event: the next snapshot read re-probes."""
    global _stale, _stale_reason
    with _LOCK:
        _stale = True
        _stale_reason = reason


def refresh_environment_snapshot(reason: str) -> EnvironmentSnapshot:
    """Explicit re-scan: probe now and return the (possibly unchanged) snapshot."""
    with _LOCK:
        return _probe_locked(reason)


# ── Actionable refusal ───────────────────────────────────────────────

def environment_unavailable_tool(
        name: str,
        snapshot: EnvironmentSnapshot | None = None) -> dict[str, Any] | None:
    """Explain why registered tool ``name`` is withheld here, or ``None``.

    Returns ``{'tool_name', 'missing_capabilities', 'reason_code', 'message',
    'recovery_hint'}`` when a spec declares requirements for ``name`` that the
    snapshot lacks. Unknown names and available tools return ``None``.
    """
    from lib.tools.registry._spec import all_specs

    requirements: set[str] = set()
    for spec in all_specs():
        requirements.update(spec.environment_requirements_by_name.get(name, ()))
    if not requirements:
        return None
    current = snapshot if snapshot is not None else environment_snapshot()
    missing = current.missing(requirements)
    if not missing:
        return None
    message, hint = _refusal_text(name, missing, current)
    return {
        'tool_name': name,
        'missing_capabilities': list(missing),
        'reason_code': 'environment_capability_missing',
        'message': message,
        'recovery_hint': hint,
    }


def _refusal_text(name: str, missing: tuple[str, ...],
                  snapshot: EnvironmentSnapshot) -> tuple[str, str]:
    installable = [capability for capability in missing
                   if capability in _INSTALL_COMMANDS]
    unreachable = [capability for capability in missing
                   if capability in _UNREACHABLE]
    where = 'this kernel' if snapshot.local_kernel else 'this server'
    parts = []
    if installable:
        labels = ' and '.join(_LABELS[capability] for capability in installable)
        verb = 'is' if len(installable) == 1 else 'are'
        parts.append(f'{labels} {verb} not installed in {where}')
    parts.extend(_UNREACHABLE[capability][0] for capability in unreachable)
    message = (f'`{name}` is unavailable: ' + '; '.join(parts) + '.')
    hints = []
    if installable:
        commands = ' && '.join(_INSTALL_COMMANDS[capability]
                               for capability in installable)
        if snapshot.local_kernel:
            hints.append(f"In the phone's userland run `{commands}` (run_command "
                         'can do it), or switch to an SSH or URL kernel.')
        else:
            hints.append(f'Install it on the server host, for example `{commands}`.')
        hints.append('The tool becomes available from the next turn after the '
                     'install command finishes.')
    hints.extend(_UNREACHABLE[capability][1] for capability in unreachable)
    return message, ' '.join(hints)


def environment_search_availability(query: Any, namespace: Any,
                                    usable_tool_names: Iterable[str]) -> list[dict]:
    """Tool Search evidence for withheld tools a query names.

    A row explains one capability-gated tool this kernel withholds when the
    query or namespace names the tool or its missing capability.
    """
    from lib.tools.registry._spec import all_specs

    snapshot = environment_snapshot()
    if not snapshot.unavailable:
        return []
    usable = {str(value).casefold() for value in usable_tool_names or ()}
    text = f'{query or ""} {namespace or ""}'.casefold()
    evidence = []
    for spec in all_specs():
        for name in sorted(spec.environment_requirements_by_name):
            if name.casefold() in usable:
                continue
            missing = snapshot.missing(spec.environment_requirements_by_name[name])
            if not missing:
                continue
            if name.casefold() not in text and not any(
                    capability in text for capability in missing):
                continue
            detail = environment_unavailable_tool(name, snapshot)
            if detail is None:
                continue
            evidence.append({
                'capability': 'environment', 'namespace': spec.category or spec.key,
                'available': False, **detail,
                'matched_tools': [name],
            })
    return evidence
