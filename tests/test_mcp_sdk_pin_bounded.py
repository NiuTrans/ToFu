"""tests/test_mcp_sdk_pin_bounded.py — every MCP SDK dependency spec is bounded.

WHY THIS GUARD EXISTS (measured, 2026-07-29)
--------------------------------------------
The MCP Python SDK shipped **2.0.0** on 2026-07-28, the same day as the
``2026-07-28`` protocol revision. It is a breaking rework of the library, and
every declaration in this repo said ``mcp>=1.0`` with NO upper bound. Measured
consequences, verified against the real 2.0.0 wheel:

  * ``mcp.client.streamable_http.streamablehttp_client`` was RENAMED to
    ``streamable_http_client`` — ``lib/mcp/client/_bridge.py`` imports the old
    name, so a v2 resolve is an **ImportError** at first remote connect.
  * The transport yields ``(read_stream, write_stream)``; ``_bridge.py`` unpacks
    THREE values (``GetSessionIdCallback`` is gone) — **ValueError**.
  * Model fields moved to snake_case (``isError`` → ``is_error``,
    ``inputSchema`` → ``input_schema``, ``serverInfo`` → ``server_info``).
  * The low-level ``Server`` decorator API (``@server.list_tools()`` /
    ``@server.call_tool()``) was replaced by ``on_list_tools=`` /
    ``on_call_tool=`` constructor parameters, with NO ``__getattr__`` fallback
    — every vendored server under ``tools/`` registers via the decorators.

The pin sites are NOT one file. They span three install layers, and the most
dangerous one had no guard at all:

  1. ``requirements.txt``        — the normal install path.
  2. ``bootstrap.py``            — ``_CONDA_DEPS``, the PRE-BOOT installer. An
     unbounded spec here installs the breaking 2.x into Tofu's own interpreter
     *before the app starts*. ``test_bootstrap_conda_deps_coverage.py`` only
     asserted PRESENCE of ``_CRITICAL_BOOT_PACKAGES``, so this site was
     invisible to every existing guard.
  3. ``tools/*/pyproject.toml``  — vendored servers, pip-installed into TOFU'S
     OWN interpreter by ``lib/mcp/client/_install.py``. An unbounded spec here
     upgrades the SDK out from under the Tofu client.

  4. The SIBLING dev checkouts (``../hope-mcp``, ``../llm-mcp``, …) that
     ``tools/`` is vendored FROM. ``/tools/`` is gitignored, so the snapshots
     are not tracked here and re-running ``make vendor-mcp`` overwrites them
     from the sibling — fixing only the snapshot is therefore temporary. This
     guard can only see what is inside THIS repo, so the sibling pins are
     fixed at their source and the staleness detector in
     ``lib/mcp/client/_vendor.py`` reports snapshot drift.

WHY IT SCANS BY RESOURCE RATHER THAN A HARD-CODED FILE LIST
------------------------------------------------------------
``tools/*/pyproject.toml`` is GLOBBED, not enumerated. A hard-coded list is a
second copy of "which servers exist" that drifts the moment someone vendors a
new server — and the new server would inherit exactly the unbounded spec this
guard exists to forbid, while the guard stayed green. Discovery is the point.

Comments are stripped first via ``tests/_source_scan.strip_comments`` (charter
#24): this very file's prose contains the string ``mcp>=1.0`` as an example of
the FORBIDDEN shape, and a naive scanner would either flag the documentation or
be satisfied by it. Both directions are failures.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _source_scan import strip_comments  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.unit

#: A dependency spec naming the MCP SDK, in either quoting style:
#:   requirements.txt →  mcp>=1.0
#:   pyproject/py     →  "mcp>=1.0.0"  /  'mcp>=1.0'
#: The name must be EXACTLY ``mcp`` (not ``mcp-types``, not ``hope-mcp``), so
#: the boundary is anchored on both sides.
_MCP_SPEC_RE = re.compile(
    r'''(?<![\w.-])mcp\s*(?P<spec>(?:[<>=!~]=?\s*[0-9][^"'\s,\]]*\s*,?\s*)+)''')


def _pin_sites():
    """Discover every file that may declare an MCP SDK dependency.

    Returns a list of (label, abs_path, lang) — ``lang`` drives comment
    stripping. ``tools/*/pyproject.toml`` is globbed so a newly vendored
    server is covered automatically.
    """
    sites = [
        ('requirements.txt', os.path.join(REPO, 'requirements.txt'), 'shell'),
        ('bootstrap.py', os.path.join(REPO, 'bootstrap.py'), 'python'),
    ]
    tools_dir = os.path.join(REPO, 'tools')
    if os.path.isdir(tools_dir):
        for name in sorted(os.listdir(tools_dir)):
            pj = os.path.join(tools_dir, name, 'pyproject.toml')
            if os.path.isfile(pj):
                sites.append((f'tools/{name}/pyproject.toml', pj, 'shell'))
    return sites


def _specs_in(path, lang):
    """Every live (non-comment) MCP SDK spec string in ``path``."""
    with open(path, encoding='utf-8', errors='ignore') as f:
        live = strip_comments(f.read(), lang=lang)
    return [m.group('spec').strip().rstrip(',').strip()
            for m in _MCP_SPEC_RE.finditer(live)]


def _has_upper_bound(spec):
    """True when ``spec`` constrains the MAJOR version from above.

    ``<``/``<=`` are the explicit forms; ``==`` and ``~=`` are bounded by
    construction. A bare ``>=`` is exactly the unbounded shape that let 2.0.0
    in.
    """
    return bool(re.search(r'(?:<|==|~=)', spec))


# ── The guard ────────────────────────────────────────────────────────

#: Sites that exist in EVERY checkout, including a fresh clone. Everything
#: under ``tools/`` is a VENDORED SNAPSHOT and ``/tools/`` is gitignored
#: (.gitignore:38), so those directories are absent on a fresh clone and their
#: real upstream is the sibling dev checkout (see lib/mcp/vendored.py).
_ALWAYS_PRESENT = ('requirements.txt', 'bootstrap.py')


def test_every_mcp_spec_is_discovered():
    """The scanner must actually FIND specs at every always-present site.

    Without this, a regex that silently matches nothing turns every assertion
    below into a vacuous pass — the failure mode where a guard reports green
    because it looked at nothing at all.

    The threshold is derived from the sites actually DISCOVERED rather than
    hard-coded. An earlier version asserted ``total >= 5``, which was measured
    to FAIL ON A FRESH CLONE: ``/tools/`` is gitignored, so the three vendored
    snapshots contribute 0 specs there and only 2 remain. A guard that goes red
    on a clean checkout gets switched off, which costs more than it protects.
    """
    sites = _pin_sites()
    labels = [label for label, _, _ in sites]
    for required in _ALWAYS_PRESENT:
        assert required in labels, f'{required} missing from the scanned sites'

    per_site = {label: len(_specs_in(p, lang)) for label, p, lang in sites}
    for required in _ALWAYS_PRESENT:
        assert per_site[required] >= 1, (
            f'{required} yielded no MCP SDK spec. Either the declaration was '
            f'removed (decide that deliberately) or _MCP_SPEC_RE broke — a '
            f'zero count means the scanner looked at nothing, not that the '
            f'repo is clean.'
        )
    # Every discovered site must contribute at least one spec: a vendored
    # pyproject.toml that exists but declares no mcp dep would mean the
    # snapshot is broken, and silently scanning 0 specs from it is how this
    # guard would go quiet without failing.
    assert sum(per_site.values()) >= len(_ALWAYS_PRESENT)


def test_bootstrap_site_is_covered():
    """``bootstrap.py`` MUST be among the scanned sites, with a live spec.

    Called out separately because it is the site that had NO guard: it is the
    pre-boot installer, so an unbounded spec there breaks Tofu before the app
    starts, and the existing conda-coverage guard never looked at version
    bounds.
    """
    labels = [label for label, _, _ in _pin_sites()]
    assert 'bootstrap.py' in labels
    specs = _specs_in(os.path.join(REPO, 'bootstrap.py'), 'python')
    assert specs, (
        'bootstrap.py declares no MCP SDK spec. If _CONDA_DEPS legitimately '
        'dropped mcp, delete this assertion deliberately — do not let the '
        'scanner go quiet, because a silent miss here is a pre-boot break.'
    )


@pytest.mark.parametrize('label,path,lang', _pin_sites(),
                         ids=[s[0] for s in _pin_sites()])
def test_mcp_spec_has_upper_bound(label, path, lang):
    """Every live MCP SDK spec pins an upper bound.

    The bound is not cosmetic: without it `pip install` resolves to 2.x, which
    this codebase cannot speak (see module docstring for the measured import
    and unpack failures).
    """
    for spec in _specs_in(path, lang):
        assert _has_upper_bound(spec), (
            f'{label}: MCP SDK spec "mcp{spec}" has no upper bound. '
            f'`pip install mcp` resolves to 2.x, which renamed '
            f'streamablehttp_client and changed the transport tuple arity — '
            f'the Tofu bridge raises ImportError/ValueError on first connect. '
            f'Use "mcp>=1.0,<2" (keep the existing floor; do NOT raise it — '
            f'the v1 line does not speak the 2026-07-28 revision either way).'
        )


def test_upper_bound_predicate_rejects_bare_floor():
    """The predicate itself must be able to FAIL.

    A ``_has_upper_bound`` that returned True for everything would make the
    parametrized guard above pass on a fully unbounded repo. Pin the predicate
    to concrete inputs in both directions.
    """
    assert not _has_upper_bound('>=1.0')
    assert not _has_upper_bound('>=1.0.0')
    assert _has_upper_bound('>=1.0,<2')
    assert _has_upper_bound('==2.0.0')
    assert _has_upper_bound('~=1.27')


def test_scanner_ignores_commented_and_unrelated_names():
    """Comments must neither satisfy nor violate the guard, and sibling
    packages whose names merely CONTAIN 'mcp' must not be scanned.

    ``hope-mcp`` / ``mcp-types`` are different distributions; matching them
    would make this guard demand upper bounds on packages it knows nothing
    about (a false alarm that trains people to ignore it).
    """
    sample = (
        '# mcp>=1.0\n'          # commented-out unbounded spec — must be ignored
        'mcp>=1.0,<2\n'         # the real, bounded one
        'hope-mcp>=1.0.0\n'     # different distribution
        'mcp-types>=2.0.0\n'    # different distribution
    )
    live = strip_comments(sample, lang='shell')
    specs = [m.group('spec').strip().rstrip(',').strip()
             for m in _MCP_SPEC_RE.finditer(live)]
