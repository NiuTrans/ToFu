"""lib/env_health.py — Detect half-overwritten site-packages installs.

A *half-overwritten package* is one where a second version was installed on
top of an existing one without the old files being removed, so the wrong
version's files shadow the intended one and imports break in subtle ways.

This project's env has hit that class twice (see
``data/memories/global/half-overwritten_package_install_*``):

  * **scipy** — conda 1.13.0 + aborted pip 1.17.1 left a stray flat
    ``_propack.cpython-*.so`` shadowing the ``_propack/`` package →
    ``ImportError: cannot import name '_spropack'``.
  * **pydantic** — mypyc-compiled v1.10.26 ``.so`` files left in ``pydantic/``
    when pure-python v2.13.4 was installed on top. Python imports
    ``pydantic/__init__.cpython-*.so`` (v1) in preference to the v2
    ``__init__.py`` → ``TypeAdapter`` missing → the MCP SDK fails to import.

Both share two mechanical tells, computable purely from the filesystem
WITHOUT importing anything:

  1. **Duplicate dist-info** — two ``<name>-<ver>.dist-info`` dirs for the
     same canonical package name.
  2. **Shadow .so** — a compiled ``X.cpython-*.so`` sitting next to a sibling
     ``X.py`` that is NOT listed in any surviving dist-info ``RECORD`` (i.e.
     an orphaned compiled file that shadows the python source at import time).

The functions here are pure (they take a ``site_packages`` path) so they can
be unit-tested against synthetic trees. ``scan_current_env()`` is the
convenience wrapper that inspects the running interpreter's paths.
"""

from __future__ import annotations

import os
import re
import sysconfig
from dataclasses import dataclass, field

from lib.log import get_logger

logger = get_logger(__name__)

# A compiled extension for CPython, e.g. ``main.cpython-312-x86_64-linux-gnu.so``
# or the Windows/macOS variants. Group 1 is the module base name.
_EXT_SUFFIX_RE = re.compile(
    r'^(?P<base>.+?)\.(?:cpython-\d+[^.]*|abi\d+|cp\d+[^.]*)?\.?(?:so|pyd|dylib)$'
)

# ``<name>-<version>.dist-info`` — version starts at the last hyphen group.
_DIST_INFO_RE = re.compile(r'^(?P<name>.+)-(?P<version>[^-]+)\.dist-info$')


@dataclass
class EnvIssue:
    """One detected problem in a site-packages tree.

    ``severity`` distinguishes a decisive breakage from mere corroboration:

      * ``'error'`` — a shadow ``.so`` (imports actually resolve the stale
        compiled file), or a duplicate dist-info CORRELATED with one.
      * ``'warning'`` — a lone duplicate dist-info. Long-lived conda envs
        accumulate leftover ``.dist-info`` dirs from ``pip install -U`` that
        are harmless on their own, so this is informational, not blocking.
    """

    kind: str  # 'duplicate_dist_info' | 'shadow_so'
    package: str  # canonical package (or import) name the issue is about
    detail: str  # human-readable one-line description
    paths: list[str] = field(default_factory=list)  # offending paths (rel to site-packages)
    severity: str = 'error'  # 'error' | 'warning'

    def __str__(self) -> str:
        return f'[{self.severity}:{self.kind}] {self.package}: {self.detail}'


def canonical_name(name: str) -> str:
    """PEP 503 canonicalization: lowercase, collapse runs of ``-_.`` to ``-``."""
    return re.sub(r'[-_.]+', '-', name).lower()


def _strip_ext_suffix(filename: str) -> str | None:
    """Return the module base name of a compiled extension file, or None.

    ``main.cpython-312-x86_64-linux-gnu.so`` → ``main``;
    ``_spropack.cpython-312-x86_64-linux-gnu.so`` → ``_spropack``;
    ``foo.py`` → None (not a compiled extension).
    """
    m = _EXT_SUFFIX_RE.match(filename)
    if not m:
        return None
    base = m.group('base')
    # Guard against a plain ``x.so`` where base still carries a stray dot tag
    # that wasn't a real cpython tag; the regex already handles the common
    # cases, so just return the captured base.
    return base or None


def _iter_dist_info_dirs(site_packages: str):
    """Yield (dirname, canonical_name, version) for each ``*.dist-info`` dir."""
    try:
        entries = os.listdir(site_packages)
    except OSError as e:
        logger.debug('env_health: cannot list %s: %s', site_packages, e)
        return
    for entry in entries:
        if not entry.endswith('.dist-info'):
            continue
        m = _DIST_INFO_RE.match(entry)
        if not m:
            logger.debug('env_health: unparsable dist-info name: %s', entry)
            continue
        yield entry, canonical_name(m.group('name')), m.group('version')


def find_duplicate_dist_info(site_packages: str) -> list[EnvIssue]:
    """Find packages that have more than one ``*.dist-info`` dir.

    Two dist-info dirs for the same canonical name is the primary tell that a
    reinstall left the old metadata behind — the strongest early warning that
    a half-overwrite may be present.
    """
    by_name: dict[str, list[tuple[str, str]]] = {}
    for dirname, cname, version in _iter_dist_info_dirs(site_packages):
        by_name.setdefault(cname, []).append((version, dirname))

    issues: list[EnvIssue] = []
    for cname, versions in sorted(by_name.items()):
        if len(versions) > 1:
            vs = sorted(versions)
            issues.append(EnvIssue(
                kind='duplicate_dist_info',
                package=cname,
                detail='%d dist-info dirs present (%s) — a reinstall left stale '
                       'metadata; imported version may not match any dir'
                       % (len(vs), ', '.join(v for v, _ in vs)),
                paths=[d for _, d in vs],
                severity='warning',
            ))
    return issues


def _load_recorded_paths(site_packages: str) -> set[str]:
    """Union of every file path listed in every dist-info ``RECORD``.

    Paths are normalized to forward-slash, relative to site-packages (that is
    exactly how RECORD stores them). Used to decide whether an on-disk ``.so``
    is a legitimately-installed file or an orphan left by a prior version.
    """
    recorded: set[str] = set()
    for dirname, _cname, _version in _iter_dist_info_dirs(site_packages):
        record_path = os.path.join(site_packages, dirname, 'RECORD')
        try:
            with open(record_path, 'r', encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    # RECORD is CSV: <path>,<hash>,<size>. The path is field 0
                    # and never contains a comma for our packages.
                    rel = line.split(',', 1)[0].strip()
                    if rel:
                        recorded.add(rel.replace('\\', '/'))
        except FileNotFoundError:
            logger.debug('env_health: no RECORD in %s', dirname)
        except OSError as e:
            logger.debug('env_health: cannot read RECORD in %s: %s', dirname, e)
    return recorded


def find_shadow_so(site_packages: str) -> list[EnvIssue]:
    """Find compiled ``.so`` files that shadow a sibling ``.py`` yet are not
    listed in any dist-info RECORD.

    This is the decisive tell: at import time Python prefers the compiled
    extension over the ``.py``, so an *orphaned* ``X.cpython-*.so`` (left by a
    previous version, absent from every current RECORD) silently overrides the
    ``X.py`` that the currently-recorded version shipped — the exact pydantic
    v1-shadows-v2 failure.
    """
    recorded = _load_recorded_paths(site_packages)
    issues: list[EnvIssue] = []

    # Group offending .so files by their top-level package for a tidy report.
    by_pkg: dict[str, list[str]] = {}
    for root, dirs, files in os.walk(site_packages):
        # Only descend into importable package dirs: every real package /
        # subpackage dir name is a valid Python identifier. This prunes
        # metadata dirs (``*.dist-info`` / ``*.egg-info``) AND non-importable
        # junk such as move-aside backups (``pydantic.corrupt.<ts>``) whose
        # dotted names can never shadow anything at import time.
        dirs[:] = [d for d in dirs if d.isidentifier()]
        for fname in files:
            base = _strip_ext_suffix(fname)
            if base is None:
                continue
            sibling_py = os.path.join(root, base + '.py')
            if not os.path.isfile(sibling_py):
                continue  # a .so with no .py sibling is a normal C extension
            rel_so = os.path.relpath(os.path.join(root, fname), site_packages)
            rel_so_norm = rel_so.replace('\\', '/')
            if rel_so_norm in recorded:
                continue  # legitimately installed compiled module
            top_pkg = rel_so_norm.split('/', 1)[0] or rel_so_norm
            by_pkg.setdefault(top_pkg, []).append(rel_so_norm)

    for pkg, paths in sorted(by_pkg.items()):
        issues.append(EnvIssue(
            kind='shadow_so',
            package=pkg,
            detail='%d orphaned compiled file(s) shadow a sibling .py but are '
                   'absent from every dist-info RECORD — a prior version was '
                   'overwritten without cleanup; imports resolve the stale .so'
                   % len(paths),
            paths=sorted(paths),
        ))
    return issues


def scan_site_packages(site_packages: str) -> list[EnvIssue]:
    """Run all half-overwrite detectors against one site-packages dir.

    A lone duplicate dist-info stays a ``warning`` (benign leftover metadata is
    common). When the SAME package also has a shadow ``.so``, that duplicate is
    escalated to ``error`` — together they are the true half-overwrite
    signature (the pydantic v1-shadows-v2 repro).
    """
    if not os.path.isdir(site_packages):
        logger.debug('env_health: not a directory: %s', site_packages)
        return []
    dupes = find_duplicate_dist_info(site_packages)
    shadows = find_shadow_so(site_packages)
    shadow_names = {canonical_name(s.package) for s in shadows}
    for d in dupes:
        if canonical_name(d.package) in shadow_names:
            d.severity = 'error'
            d.detail += ' — AND an orphaned shadow .so is present (half-overwrite)'
    return dupes + shadows


def _candidate_site_dirs() -> list[str]:
    """De-duplicated purelib + platlib of the running interpreter."""
    dirs: list[str] = []
    for key in ('purelib', 'platlib'):
        try:
            p = sysconfig.get_paths().get(key)
        except Exception as e:  # sysconfig is defensive; never let it raise
            logger.debug('env_health: sysconfig.get_paths failed: %s', e)
            p = None
        if p and os.path.isdir(p) and p not in dirs:
            dirs.append(p)
    return dirs


def scan_current_env() -> list[EnvIssue]:
    """Scan the running interpreter's site-packages for half-overwrites."""
    issues: list[EnvIssue] = []
    for d in _candidate_site_dirs():
        issues.extend(scan_site_packages(d))
    return issues
