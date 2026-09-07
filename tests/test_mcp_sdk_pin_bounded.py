"""tests/test_mcp_sdk_pin_bounded.py — every MCP SDK dependency spec is bounded.

WHY THIS GUARD EXISTS (measured, 2026-07-29; updated 2026-07-31)
----------------------------------------------------------------
The MCP Python SDK shipped **2.0.0** on 2026-07-28, a breaking rework of the
library (transport renamed, tuple arity changed, model fields moved to
snake_case, the low-level ``Server`` decorator API replaced by constructor
kwargs). At the time every declaration in this repo said ``mcp>=1.0`` with NO
upper bound, so this guard was born to force a bound everywhere.

The doctrine CHANGED on 2026-07-31. A 2x2 wire-interop matrix (v1/v2 client
x v1/v2 server, real stdio handshakes) negotiated the protocol in ALL four
quadrants — SDK major version is NOT a wire boundary. What ever coupled a
server's SDK to Tofu's was the SHARED INTERPRETER (vendored servers were
pip-installed into it); that coupling is gone (they launch isolated via
``uv run --with-editable``). And the Tofu client itself was migrated to the
v2 API, because v1 structurally cannot speak the 2026-07-28 protocol
revision while a v2 client negotiates down correctly.

So the guard now enforces a SPLIT, not a single range:

  * Tofu client sites (``requirements.txt`` / ``bootstrap_pkg/install.py`` /
    ``install.sh``) must declare ``mcp>=2,<3`` — the client speaks the v2
    API, and stays major-bounded because the NEXT rework is unknowable.
  * Vendored server sites (``tools/*/pyproject.toml``) must be BOUNDED, but
    the range is each server's own business (they pin ``<2`` today for the
    v1 decorator API they register with — valid forever inside their
    isolated envs).

Comments are stripped first via ``tests/_source_scan.strip_comments`` (charter
#24): this very file's prose contains spec-like strings as examples, and a
naive scanner would either flag the documentation or be satisfied by it.
Both directions are failures.
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
#:   requirements.txt →  mcp>=2,<3
#:   pyproject/py     →  "mcp>=1.0.0,<2"  /  'mcp>=1.0'
#: The name must be EXACTLY ``mcp`` (not ``mcp-types``, not ``hope-mcp``), so
#: the boundary is anchored on both sides.
_MCP_SPEC_RE = re.compile(
    r'''(?<![\w.-])mcp\s*(?P<spec>(?:[<>=!~]=?\s*[0-9][^"'\s,\]]*\s*,?\s*)+)''')

#: Sites that declare the TOFU CLIENT's own SDK — these must be on the v2
#: line (the bridge was migrated to the v2 API). Vendored servers pin their
#: own mcp independently inside their isolated envs.
# ``bootstrap.py`` is a STDLIB-ONLY facade since the 2026-08-21 split
# (c88a63fe) — the pre-boot install declaration it used to carry moved to
# ``bootstrap_pkg/install.py`` (``_CONDA_PYTHON_DEPS``). The guard reads the
# REAL site; the facade itself is covered by a REVERSE pin below (it must
# never regain a spec).
_TOFU_CLIENT_SITES = ('requirements.txt', 'bootstrap_pkg/install.py',
                      'install.sh')


def _pin_sites():
    """Discover every file that may declare an MCP SDK dependency.

    Returns a list of (label, abs_path, lang) — ``lang`` drives comment
    stripping. ``tools/*/pyproject.toml`` is globbed so a newly vendored
    server is covered automatically.
    """
    sites = [
        ('requirements.txt', os.path.join(REPO, 'requirements.txt'), 'shell'),
        ('bootstrap_pkg/install.py',
         os.path.join(REPO, 'bootstrap_pkg', 'install.py'), 'python'),
        # install.sh's CONDA_PKGS array is a REAL install spec: it is fed to
        # `conda install -c conda-forge` for a fresh public deploy. Measured
        # 2026-07-31: conda-forge already carries mcp 2.0.0, and this site
        # carried a bare `mcp>=1.0` that no guard scanned.
        ('install.sh', os.path.join(REPO, 'install.sh'), 'shell'),
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
_ALWAYS_PRESENT = ('requirements.txt', 'bootstrap_pkg/install.py')


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
    """``bootstrap_pkg/install.py`` MUST be among the scanned sites, live spec.

    Called out separately because it is the site that had NO guard: it feeds
    the pre-boot installer, so an unbounded spec there breaks Tofu before the
    app starts, and the existing conda-coverage guard never looked at version
    bounds. The site moved in the 2026-08-21 facade-retained split
    (``bootstrap.py`` → ``bootstrap_pkg/install.py``); asserting on the
    facade instead goes red on every clean checkout even though the
    declaration is healthy.
    """
    labels = [label for label, _, _ in _pin_sites()]
    assert 'bootstrap_pkg/install.py' in labels
    specs = _specs_in(os.path.join(REPO, 'bootstrap_pkg', 'install.py'),
                      'python')
    assert specs, (
        'bootstrap_pkg/install.py declares no MCP SDK spec. If '
        '_CONDA_PYTHON_DEPS legitimately dropped mcp, delete this assertion '
        'deliberately — do not let the scanner go quiet, because a silent '
        'miss here is a pre-boot break.'
    )


def test_bootstrap_facade_stays_stdlib_only():
    """The ``bootstrap.py`` facade must never REGAIN an MCP SDK spec.

    Reverse pin for the 2026-08-21 facade-retained split (c88a63fe): the
    facade and the whole ``bootstrap_pkg`` package are stdlib-only by
    contract — they must run when EVERY pip package is missing (that is the
    launcher's whole point). The real client pin lives in
    ``bootstrap_pkg/install.py`` (covered above); a spec re-appearing in the
    facade means someone re-coupled the pre-boot launcher to a third-party
    dependency, exactly what the split abolished.
    """
    specs = _specs_in(os.path.join(REPO, 'bootstrap.py'), 'python')
    assert not specs, (
        'bootstrap.py gained an MCP SDK spec — the facade is stdlib-only by '
        'contract (it must run when every pip package is missing). The '
        'declaration belongs in bootstrap_pkg/install.py '
        '(_CONDA_PYTHON_DEPS).'
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
            f'An unbounded resolve pulls the NEXT breaking major the day it '
            f'lands. Tofu client sites use "mcp>=2,<3" (the bridge speaks the '
            f'v2 API); a vendored server picks its own bound for the API IT '
            f'registers with — it lives in an isolated env.'
        )


def test_bridge_source_keeps_v2_primary_with_v1_rolling_fallback():
    """``_bridge.py`` must retain v2 support plus the v1 deployment bridge.

    The pin alone does not migrate the client: ``requirements.txt`` saying
    ``mcp>=2,<3`` while the bridge still imports ``streamablehttp_client``
    (removed in v2) is an ImportError on the first remote connect. This is the
    code-side ratchet for the migration — cheap, and red the moment anyone
    reintroduces a v1 spelling:

      * ``streamablehttp_client``  (renamed ``streamable_http_client``)
      * ``.inputSchema``           (renamed ``.input_schema``)
      * ``.isError``               (renamed ``.is_error``)
      * ``'serverInfo'`` getattr   (renamed ``server_info``)

    Anchored to the bridge's OWN source (not a repo-wide grep) so prose,
    comments, and other modules' legitimate uses cannot satisfy or trip it.
    """
    import inspect

    from lib.mcp.client import _bridge as bridge_mod

    src = inspect.getsource(bridge_mod)
    for dead in ('streamablehttp_client', '.inputSchema', '.isError'):
        assert dead not in src, (
            f'_bridge.py references v1-only SDK name {dead!r} — removed in '
            f'mcp 2.x, this is an ImportError/AttributeError at runtime'
        )
    # And the live v2 names must be present, so this test cannot go green on
    # an emptied/rewritten module.
    for live in ('streamable_http_client', '.input_schema', '.is_error',
                 "'server_info'"):
        assert live in src, (
            f'_bridge.py no longer uses {live!r} — the v2 migration regressed'
        )
    assert "getattr(_mcp_sdk, 'Client', None)" in src
    assert 'ClientSession(read, write' in src


def test_tofu_client_sites_are_on_the_v2_line():
    """The Tofu client's own pin must be ``mcp>=2,<3``.

    Both halves are load-bearing:

      * floor >= 2: the bridge uses the v2 API (``streamable_http_client``,
        snake_case fields). A floor of 1.x lets a resolver hand us a v1 SDK
        the code cannot speak — and the v1 line structurally cannot speak
        the 2026-07-28 protocol revision.
      * ceiling < 3: the NEXT breaking rework is unknowable. This guard was
        born the day an unbounded spec pulled 2.0.0 into production.

    A regression here fights the migration in the worst way: some future
    agent "fixes" the red by re-pinning ``<2`` and silently reverts the
    client to a line that cannot reach 2026-07-28-only servers.
    """
    for label, path, lang in _pin_sites():
        if label not in _TOFU_CLIENT_SITES:
            continue
        specs = _specs_in(path, lang)
        assert specs, f'{label}: no mcp spec found at a Tofu client site'
        for spec in specs:
            assert re.search(r'>=\s*2', spec), (
                f'{label}: "mcp{spec}" floors below 2 — the bridge uses the '
                f'v2 API and would ImportError on a v1 resolve'
            )
            assert re.search(r'<\s*3', spec), (
                f'{label}: "mcp{spec}" has no major ceiling — the exact '
                f'shape that pulled a breaking 2.0.0 into production before'
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


def test_launcher_resolution_is_reproducible():
    """Every MCP launcher subprocess must inherit a supply-chain cutoff.

    WHY THIS IS A DIFFERENT DEFECT CLASS FROM THE UPPER BOUND ABOVE
    ---------------------------------------------------------------
    Bounding ``mcp`` protects ONE package that we happen to know broke. It does
    nothing for the other ~30 packages in a server's tree. Measured 2026-07-31
    against the real ``data/mcp-cache`` on this host: the FROZEN spec
    ``uvx --from 'overleaf-mcp-plus[compile]>=0.1.3'`` had materialised FIVE
    different ``mcp`` versions across 30 environments —

        1.28.1 ×16   1.28.0 ×5   1.27.2 ×5   2.0.0 ×3   1.29.0 ×1

    — while the server's own version was 0.2.1 in every single one. So 100% of
    the drift was TRANSITIVE, and neither pinning the launcher spec nor
    isolating the environment can fix it: each cold resolve is an independent
    draw against whatever the index holds at that instant. That is what makes
    the failure random and unreproducible, which is strictly worse than a
    failure that is merely frequent.

    A date cutoff fixes the entire tree at once without editing the 50 floating
    specs in the catalog. Measured: two cold resolves under the same cutoff
    produced byte-identical dependency trees, and the reported crash
    (``mcp 2.0.0`` → ``AttributeError: 'Server' object has no attribute
    'list_tools'``) becomes ``mcp 1.28.1`` + a clean import.

    WHY IT ASSERTS ON THE INJECTION SEAM, NOT ON A FILE
    ----------------------------------------------------
    The obvious guard — "every spec in mcp_servers.json names an exact
    version" — was rejected on evidence. That file is GITIGNORED user data
    (.gitignore:31, not tracked by git), so on a fresh clone the guard would
    scan nothing and pass vacuously; and the catalog it is populated from has
    50 floating entries and 0 pinned ones, so the assertion would demand 50
    hand-maintained pins that drift the moment upstream publishes. Both shapes
    fail for the same reason: they police the DECLARATIONS instead of the one
    place resolution actually happens.

    So this asserts the invariant where it is load-bearing: the env dict handed
    to every launcher subprocess. ``_ensure_writable_caches`` is on the stdio
    path (``_bridge.py`` calls it on the env passed to
    ``StdioServerParameters``), so covering it covers uv/uvx and npm/npx at
    once, including servers that do not exist yet.
    """
    from lib.mcp.client._vendor import _ensure_writable_caches

    env = {}
    _ensure_writable_caches(env)

    assert env.get('UV_EXCLUDE_NEWER'), (
        'MCP launcher env carries no UV_EXCLUDE_NEWER. Without it every cold '
        'uvx resolve re-draws the whole transitive tree from the live index — '
        'measured: one frozen spec produced 5 different mcp versions, one of '
        'them the 2.0.0 that crashes the server at import.'
    )
    assert env.get('npm_config_before'), (
        'MCP launcher env carries no npm_config_before. npx servers resolve '
        '`-y <pkg>` against the live registry on every spawn; measured, '
        '--before pins the transitive tree (zod 3.24.1 vs an unconstrained '
        '3.25.76 + 4.4.3).'
    )
    # npm's --before takes a plain date; uv wants an RFC3339 instant. A cutoff
    # that npm cannot parse would be silently ignored, re-opening the drift on
    # the npx half while this guard stayed green.
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}', env['npm_config_before']), (
        f'npm_config_before={env["npm_config_before"]!r} is not a plain '
        f'YYYY-MM-DD date — npm would ignore it and npx resolution would '
        f'float again.'
    )
    assert env['UV_EXCLUDE_NEWER'].startswith(env['npm_config_before']), (
        'The uv and npm cutoffs disagree. Two ecosystems pinned to different '
        'instants means "reproducible" is only half true, and which half '
        'depends on which launcher a server happens to use.'
    )


def test_default_cutoff_includes_current_adopted_mcp_releases():
    """The reproducibility ceiling must not exclude an adopted dependency."""
    from lib.mcp.client._vendor import _SUPPLY_CUTOFF_DEFAULT

    assert _SUPPLY_CUTOFF_DEFAULT >= '2026-08-14T00:00:00Z', (
        'overleaf-mcp-plus==0.3.1 is the adopted exact launcher pin; an '
        'earlier cutoff makes that release unsatisfiable'
    )


def test_supply_cutoff_is_operator_overridable_and_optional():
    """The cutoff must be a FLOOR, not a cage.

    Two directions, both load-bearing:

      * An operator who exports their own ``UV_EXCLUDE_NEWER`` keeps it. This
        is how a deployment adopts a newer server without waiting for us —
        without it, the floor would be a hard ceiling on everyone's ability to
        upgrade, which is the failure mode that gets guards deleted.
      * ``TOFU_MCP_SUPPLY_CUTOFF=''`` opts out entirely, restoring floating
        resolution for someone who genuinely wants the newest tree.

    Pinned in both directions because a cutoff that cannot be overridden and a
    cutoff that cannot be disabled are each worse than no cutoff at all.
    """
    import os as _os

    from lib.mcp.client._vendor import _ensure_writable_caches

    env = {'UV_EXCLUDE_NEWER': '2030-01-01T00:00:00Z'}
    _ensure_writable_caches(env)
    assert env['UV_EXCLUDE_NEWER'] == '2030-01-01T00:00:00Z', (
        'an operator-supplied cutoff was overwritten — the floor became a cage'
    )

    prev = _os.environ.get('TOFU_MCP_SUPPLY_CUTOFF')
    _os.environ['TOFU_MCP_SUPPLY_CUTOFF'] = ''
    try:
        opted_out = {}
        _ensure_writable_caches(opted_out)
        assert 'UV_EXCLUDE_NEWER' not in opted_out, (
            'TOFU_MCP_SUPPLY_CUTOFF="" must disable the cutoff entirely'
        )
        assert 'npm_config_before' not in opted_out
    finally:
        if prev is None:
            _os.environ.pop('TOFU_MCP_SUPPLY_CUTOFF', None)
        else:
            _os.environ['TOFU_MCP_SUPPLY_CUTOFF'] = prev


def test_stale_npx_slot_is_reconciled_against_the_cutoff(tmp_path):
    """A cache resolved under the OLD rules must be migrated, not left to break.

    THE REGRESSION THIS PINS (measured 2026-07-31, introduced by the cutoff)
    ------------------------------------------------------------------------
    npm caches each ``npx -y <pkg>`` run as a slot under
    ``$npm_config_cache/_npx/<hash>/`` containing a ``package-lock.json``. Once
    the supply cutoff shipped, npm began reconciling new requests against those
    PRE-EXISTING locks; a lock resolved with no cutoff can name versions
    published after it, so npm declares it untrustworthy and aborts:

        npm error code ECOMPROMISED
        npm error Lock compromised

    Same cache dir, measured: cutoff ON -> ECOMPROMISED on 3/3 runs; cutoff OFF
    -> the server starts normally. ``npm cache verify`` does NOT repair it (it
    collected 86 corrupt entries and the failure persisted) because the slot is
    a materialised install tree, not content-addressed cache.

    The blast radius made this worse than the bug it was fixing: a machine that
    had never run Tofu was fine (no slots), while every EXISTING deployment
    broke on every npx-launched server the instant the cutoff landed. A change
    that alters resolution therefore OWNS the migration of trees resolved under
    the previous rules -- otherwise "reproducible" is only true of empty disks.

    WHY A MARKER FILE RATHER THAN INSPECTING THE LOCK
    --------------------------------------------------
    npm does not record ``before`` anywhere in ``package-lock.json`` (verified
    by grep), so staleness cannot be read back out of npm's own metadata. The
    reconciler stamps its own marker beside the lock, which makes the check
    exact instead of heuristic -- no version parsing, no comparing publish
    dates -- and lets already-reconciled slots be skipped, so the cost is
    one-time per slot rather than a wipe on every connect.
    """
    from lib.mcp.client._vendor import _NPX_CUTOFF_MARKER, _reconcile_npx_cache

    cutoff = '2026-07-27T00:00:00Z'
    npx = tmp_path / '_npx'

    # (a) the broken shape: a lock with NO marker == resolved before the cutoff
    stale = npx / 'deadbeefcafe0001'
    stale.mkdir(parents=True)
    (stale / 'package-lock.json').write_text('{"lockfileVersion": 3}')
    (stale / 'package.json').write_text('{"dependencies":{"12306-mcp":"^0.3.10"}}')
    (stale / 'node_modules').mkdir()

    # (b) a slot ALREADY reconciled under the active cutoff -- must survive, or
    #     every connect would re-pay a full npx cold install (measured ~55s).
    fresh = npx / 'deadbeefcafe0002'
    fresh.mkdir(parents=True)
    (fresh / 'package-lock.json').write_text('{"lockfileVersion": 3}')
    (fresh / _NPX_CUTOFF_MARKER).write_text(cutoff)
    (fresh / 'node_modules').mkdir()

    # (c) a slot with no lock at all -- nothing for npm to reconcile against, so
    #     it cannot raise ECOMPROMISED and must not be touched.
    lockless = npx / 'deadbeefcafe0003'
    lockless.mkdir(parents=True)
    (lockless / 'package.json').write_text('{}')

    evicted = _reconcile_npx_cache(str(tmp_path), cutoff)

    assert evicted == 1, (
        f'expected exactly the one stale slot to be evicted, got {evicted}. '
        f'Evicting 0 leaves npm to abort with ECOMPROMISED on every existing '
        f'deployment; evicting more re-pays a cold npx install needlessly.'
    )
    assert not (stale / 'node_modules').exists(), (
        'the stale tree survived -- npm will still reconcile against its '
        'pre-cutoff lock and abort with ECOMPROMISED'
    )
    assert not (stale / 'package-lock.json').exists()
    assert (stale / _NPX_CUTOFF_MARKER).read_text().strip() == cutoff, (
        'an evicted slot must be stamped with the cutoff it was reconciled '
        'under, or it is re-evicted on every single connect'
    )
    assert (fresh / 'node_modules').exists(), (
        'an already-reconciled slot was wiped -- reconcile must be idempotent, '
        'not a cache purge on every connect'
    )
    assert (lockless / 'package.json').exists(), (
        'a slot with no lock cannot trigger ECOMPROMISED and must be left alone'
    )

    # Idempotence, stated as a property: re-running changes nothing.
    assert _reconcile_npx_cache(str(tmp_path), cutoff) == 0
    assert (stale / _NPX_CUTOFF_MARKER).exists()


def test_reconcile_runs_before_the_readiness_timer_not_inside_it(monkeypatch, tmp_path):
    """Eviction must happen in connect PRE-FLIGHT, never while building the env.

    THE SECOND REGRESSION THIS PINS (measured 2026-07-31)
    -----------------------------------------------------
    The first fix called the reconciler from ``_ensure_writable_caches``. That
    function runs inside the owner task, i.e. INSIDE the readiness timer, so the
    cold rebuild it forces competed with the handshake for one 65s budget
    (``MCP_CONNECT_TIMEOUT * 2 + 5``). Measured across three trials, the FIRST
    connect after an eviction took **58.6s / 65.0s / 63.8s** — one of them over
    the ceiling. That turned a DETERMINISTIC failure (ECOMPROMISED every time)
    into a NONDETERMINISTIC one surfacing as ``BrokenResourceError``, which is
    indistinguishable from a server that genuinely crashed. For diagnosis that
    is strictly worse, even though the average got better.

    Eviction is a cache-MIGRATION concern that happens once per cutoff change,
    not a per-spawn concern. Hence two separate assertions here:

      * building the launcher env must NOT evict (or the download races the
        handshake again, and the race is invisible to a fake-slot unit test);
      * the pre-flight entry point must evict, and must REPORT it, so the
        caller can widen the budget for the one identified state.
    """
    from lib.mcp.client._vendor import (
        _NPX_CUTOFF_MARKER,
        _ensure_writable_caches,
        reconcile_for_connect,
    )

    cache_root = tmp_path / 'mcp-cache'
    slot = cache_root / 'npm' / '_npx' / 'deadbeefcafe0004'
    slot.mkdir(parents=True)
    (slot / 'package-lock.json').write_text('{"lockfileVersion": 3}')
    (slot / 'node_modules').mkdir()

    monkeypatch.setenv('TOFU_MCP_CACHE_DIR', str(cache_root))
    monkeypatch.delenv('TOFU_MCP_SUPPLY_CUTOFF', raising=False)

    # (1) Building the env must leave the tree ALONE — it runs inside the timer.
    env = {}
    _ensure_writable_caches(env)
    assert (slot / 'node_modules').exists(), (
        'building the launcher env evicted a slot. That call happens inside the '
        'readiness timer, so the forced rebuild races the handshake — measured '
        '58.6/65.0/63.8s against a 65s ceiling, i.e. a coin flip that surfaces '
        'as BrokenResourceError.'
    )

    # (2) The pre-flight entry point must evict AND report the pending rebuild.
    #     It reports OUTSTANDING rebuilds rather than "what I just evicted" —
    #     eviction is global, the budget is per-connect. The lifecycle of that
    #     counter (clears once npm restores the lock) is pinned separately by
    #     test_pending_rebuild_is_visible_to_every_connect_not_just_the_evictor.
    pending = reconcile_for_connect()
    assert pending == 1, (
        f'pre-flight reconcile reported {pending}, expected 1. Returning 0 '
        f'means the caller cannot know a cold download is now unavoidable, so '
        f'it will apply the ordinary budget and lose the race.'
    )
    assert not (slot / 'node_modules').exists()
    assert (slot / _NPX_CUTOFF_MARKER).read_text().strip() == env['UV_EXCLUDE_NEWER']


def test_cold_install_gets_its_own_budget_without_relaxing_the_crash_ceiling():
    """A known-pending download may wait; an unidentified stall may not.

    ``lib/mcp/types.py`` draws a deliberate line: a handshake that never
    completes means the server never came up, which is a CRASH and must fail
    fast — that is why ``MCP_CALL_TIMEOUT`` is None while
    ``MCP_CONNECT_TIMEOUT`` deliberately stays. Simply raising the global
    ceiling to cover a 65s npx rebuild would erase that line and make every
    genuinely-dead server take minutes to report.

    So the wide budget is granted ONLY for the state we can positively identify
    (we just deleted this launcher's dependency tree). Both halves are pinned:
    the cold budget must actually exceed the measured rebuild range, and the
    ordinary ceiling must NOT have been widened.
    """
    import inspect

    from lib.mcp.client._bridge import MCPBridge
    from lib.mcp.types import MCP_COLD_INSTALL_TIMEOUT, MCP_CONNECT_TIMEOUT

    ordinary = MCP_CONNECT_TIMEOUT * 2 + 5
    assert ordinary == 65, (
        f'the ordinary readiness ceiling moved to {ordinary}s. If that was '
        f'deliberate, re-derive the measurements in this test; if it was a '
        f'workaround for cold installs, use the cold budget instead.'
    )
    # 65.0s was an observed FAILURE, and a clean-cache cold start measured
    # 55.4s, so the cold budget has to clear those with real headroom.
    assert MCP_COLD_INSTALL_TIMEOUT > 65, (
        'the cold-install budget does not exceed the ordinary ceiling, so the '
        'measured 58.6-65.0s rebuild is still a coin flip'
    )

    # The seam must actually accept the flag — a budget nothing can select is
    # dead weight, and the production race would be untouched.
    sig = inspect.signature(MCPBridge._async_start_owner)
    assert 'cold_install' in sig.parameters, (
        '_async_start_owner takes no cold_install flag, so the wide budget is '
        'unreachable from connect_server'
    )
    assert sig.parameters['cold_install'].default is False, (
        'cold_install must default to False — a routine connect must keep the '
        'fast crash ceiling'
    )

    # And connect_server must be the thing that sets it, from the reconcile
    # result. Asserting on the source keeps this honest without spawning a
    # real server (which is what made the original 58-65s window invisible).
    src = inspect.getsource(MCPBridge.connect_server)
    assert 'reconcile_for_connect' in src, (
        'connect_server does not run the pre-flight reconcile'
    )
    assert 'cold_install=' in src, (
        'connect_server never passes cold_install, so an eviction it just '
        'performed does not widen the budget for the rebuild it caused'
    )

    # ── The INNER timer is the one that actually binds ──────────────────
    # Measured: granting 300s on the OUTER readiness wait was NOT enough — a
    # trial still failed at 67.5s, because ``session.initialize()`` carries its
    # own MCP_CONNECT_TIMEOUT. The npx download finishes only AFTER the process
    # is spawned, so the wait lands on initialize(), not on spawn. Both timers
    # must honour the cold budget or the outer one is decorative.
    owner_src = inspect.getsource(MCPBridge._server_owner)
    assert 'MCP_COLD_INSTALL_TIMEOUT' in owner_src, (
        'the owner task still hard-codes MCP_CONNECT_TIMEOUT for the handshake. '
        'Measured: a 300s outer budget with a 30s initialize() timer still '
        'failed at 67.5s — the inner timer fires first.'
    )
    assert 'timeout=handshake_budget' in owner_src, (
        'initialize()/list_tools() do not use the selected handshake budget'
    )

    # The flag must survive onto the handle, or the owner cannot see it.
    # __slots__ is declared on _MCPServerHandle, so a missing slot would raise
    # AttributeError at runtime rather than silently defaulting.
    from lib.mcp.client._bridge import _MCPServerHandle
    handle = _MCPServerHandle('probe', {})
    assert handle._cold_install is False, (
        '_cold_install must default False so a routine connect keeps the fast '
        'crash ceiling'
    )
    handle._cold_install = True  # must not raise despite __slots__
    assert handle._cold_install is True


def test_pending_rebuild_is_visible_to_every_connect_not_just_the_evictor(tmp_path, monkeypatch):
    """Eviction is GLOBAL, so the wide budget cannot be per-evictor.

    THE THIRD REGRESSION THIS PINS (measured 2026-07-31)
    ----------------------------------------------------
    ``_reconcile_npx_cache`` clears EVERY stale slot in one pass, but the first
    version of the budget fix handed the wide budget only to the connect that
    happened to trigger that pass. With two stale slots in a fleet sweep:

        github      evicted BOTH, took the 300s budget, finished in 43.1s
        12306-train connected next with the ORDINARY budget against a tree that
                    was still empty -> FAIL at 31.4s (the narrow 30s handshake)

    i.e. 6 OK / 1 FAIL, and the surviving failure was *caused* by another
    server's migration. So "did I just evict something" is the wrong question.
    The right one is "is a rebuild still outstanding for anybody", which is what
    ``reconcile_for_connect`` must report.

    The signal is deliberately on-disk rather than in memory: an evicted slot is
    a directory holding ONLY the marker, and npm restores the lock when it
    rebuilds. "marker present, lock absent" is therefore exact, survives a
    process restart, and self-clears — all of which matter because the fleet
    sweep and a later manual reconnect are different call stacks.
    """
    from lib.mcp.client._vendor import _NPX_CUTOFF_MARKER, _npx_rebuild_pending

    cutoff = '2026-07-27T00:00:00Z'
    npx = tmp_path / '_npx'

    # Evicted-and-not-yet-rebuilt: marker, no lock. Counts as pending.
    emptied = npx / 'aaaa0001'
    emptied.mkdir(parents=True)
    (emptied / _NPX_CUTOFF_MARKER).write_text(cutoff)

    # Rebuilt: npm restored the lock. Must stop counting, or every connect
    # forever gets the wide budget and a genuinely dead server stops failing
    # fast.
    rebuilt = npx / 'aaaa0002'
    rebuilt.mkdir(parents=True)
    (rebuilt / _NPX_CUTOFF_MARKER).write_text(cutoff)
    (rebuilt / 'package-lock.json').write_text('{"lockfileVersion": 3}')
    (rebuilt / 'node_modules').mkdir()

    # Never touched by us: no marker. Two shapes, and the SECOND is the one
    # that actually discriminates — a foreign slot that still HAS a lock is
    # skipped by the lock check regardless, so it cannot tell whether the
    # marker check exists. A foreign slot with NO lock (npm mid-install, or a
    # dir some other tool made) is only excluded BY the marker check, so it is
    # what proves the two conditions are independent rather than redundant.
    foreign = npx / 'aaaa0003'
    foreign.mkdir(parents=True)
    (foreign / 'package-lock.json').write_text('{"lockfileVersion": 3}')

    foreign_nolock = npx / 'aaaa0004'
    foreign_nolock.mkdir(parents=True)
    (foreign_nolock / 'package.json').write_text('{}')

    assert _npx_rebuild_pending(str(tmp_path), cutoff) == 1, (
        'pending-rebuild detection is wrong. Counting 0 means a server that '
        'did not perform the eviction connects with the narrow budget against '
        'an empty tree — measured as a 31.4s failure; counting the rebuilt or '
        'foreign slots means the wide budget never expires.'
    )

    # Simulate npm finishing the rebuild -> nothing pending -> narrow budget
    # returns, which is what keeps a dead server failing fast.
    (emptied / 'package-lock.json').write_text('{"lockfileVersion": 3}')
    assert _npx_rebuild_pending(str(tmp_path), cutoff) == 0

    # And the pre-flight entry point must surface OUTSTANDING work, not merely
    # what it evicted on this call: a second pre-flight (which evicts nothing)
    # still has to report the rebuild left over from the first.
    from lib.mcp.client._vendor import reconcile_for_connect

    cache_root = tmp_path / 'mcp-cache'
    stale = cache_root / 'npm' / '_npx' / 'bbbb0001'
    stale.mkdir(parents=True)
    (stale / 'package-lock.json').write_text('{"lockfileVersion": 3}')
    (stale / 'node_modules').mkdir()
    monkeypatch.setenv('TOFU_MCP_CACHE_DIR', str(cache_root))
    monkeypatch.delenv('TOFU_MCP_SUPPLY_CUTOFF', raising=False)

    assert reconcile_for_connect() == 1, 'first pre-flight must report the rebuild'
    assert reconcile_for_connect() == 1, (
        'the SECOND pre-flight evicted nothing but the rebuild is still '
        'outstanding — it must still report 1, or the next server in a fleet '
        'sweep gets the narrow budget against an empty tree (the measured '
        '31.4s failure)'
    )


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
    live = strip_comments(sample, lang='shell')
    specs = [m.group('spec').strip().rstrip(',').strip()
             for m in _MCP_SPEC_RE.finditer(live)]


def test_tool_schema_accessor_accepts_both_sdk_spellings():
    """2026-08-06 outage pin: a boot-time auto-install resolved **mcp 1.29.0**
    into the shared env (below the ``>=2,<3`` client floor this guard
    enforces), whose ``Tool`` carries the SPEC spelling ``inputSchema``
    while the bridge only read the v2 snake_case ``input_schema`` — all 8
    MCP servers died on connect with ``AttributeError: 'Tool' object has no
    attribute 'input_schema'``. ``_tool_input_schema`` (and the OpenAI
    translation riding it) must accept BOTH spellings, so a wrong-line
    resolve degrades to working tools instead of a dead fleet.

    Load-bearing by construction: reverting the accessor to a direct
    ``tool.input_schema`` read makes the 1.29-shaped fake raise
    AttributeError here — the pin goes red with no separate neuter test.
    """
    from lib.mcp.client._bridge import MCPBridge, _tool_input_schema

    payload = {'type': 'object', 'properties': {'q': {'type': 'string'}}}

    class _V2Tool:   # mcp 2.x snake_case — the line the bridge targets
        name = 'search'
        description = 'd'
        input_schema = payload

    class _V129Tool:  # mcp 1.29 spec spelling — the 2026-08-06 incident shape
        name = 'search'
        description = 'd'
        inputSchema = payload

    class _BareTool:  # no schema attribute at all → open-object fallback
        name = 'search'
        description = 'd'

    assert _tool_input_schema(_V2Tool()) == payload
    assert _tool_input_schema(_V129Tool()) == payload
    assert _tool_input_schema(_BareTool()) == {'type': 'object', 'properties': {}}

    # The production call path: the OpenAI translation must carry the schema
    # for the 1.29 shape too (it rides the same accessor).
    spec = MCPBridge._tool_to_openai('github', _V129Tool())
    assert spec['function']['parameters'] == payload
