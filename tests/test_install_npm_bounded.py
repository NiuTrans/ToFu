#!/usr/bin/env python3
"""Guard tests: install.sh's OPTIONAL npm step must FAIL-FAST, never hang.

Background — the "install stuck under `npm ci`" bug (2026-07-10):
  install.sh's Node.js/esbuild step is explicitly OPTIONAL and fail-open
  (the Python minifier is byte-identical without it). But unlike the conda
  (Step 1.5) and pip mirror redirects, the npm step shipped with NO
  registry mirror and NO timeout. On a network that blocks
  registry.npmjs.org, npm's defaults (fetch-timeout 300s x fetch-retries 2)
  make `npm ci`/`npm install` STALL for many minutes per package instead of
  erroring — so the `|| warn` fail-open fallback never fires and the whole
  install appears frozen (the observed MaxListenersExceededWarning is
  sockets piling up on the stalled requests).

  The fix bounds npm hard: npm_config_fetch_* caps + an outer `timeout`
  wrapper (absolute ceiling) + an optional TOFU_NPM_REGISTRY mirror baked
  by export for corp hosts. These tests pin every one of those so the
  hang can't silently return.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _install_sh() -> str:
    with open(os.path.join(ROOT, 'install.sh'), 'r', encoding='utf-8') as f:
        return f.read()


def _npm_block(text: str) -> str:
    """The install.sh region from the Node.js step to the next step marker."""
    start = text.index('Installing Node.js + esbuild')
    # Next `step "..."` after the node step ends the region.
    tail = text[start:]
    nxt = re.search(r'\nstep "', tail[10:])
    end = start + 10 + (nxt.start() if nxt else len(tail) - 10)
    return text[start:end]


def test_npm_invocations_are_timeout_wrapped():
    """Every `npm ci` / `npm install` must run under the $_NPM_TIMEOUT ceiling."""
    block = _npm_block(_install_sh())
    # A timeout wrapper var must be defined and applied to both invocations.
    assert '_NPM_TIMEOUT' in block, \
        'no $_NPM_TIMEOUT hard-timeout wrapper defined in the npm step'
    for cmd in ('npm ci', 'npm install'):
        m = re.search(r'\$_NPM_TIMEOUT\s+' + cmd.replace(' ', r'\s+'), block)
        assert m, f'`{cmd}` is not wrapped by $_NPM_TIMEOUT (could hang forever)'
    # The wrapper must actually resolve to a bounded `timeout`/`gtimeout`.
    assert re.search(r'_NPM_TIMEOUT="?timeout \d', block), \
        'timeout wrapper does not use GNU `timeout N`'
    assert 'gtimeout' in block, 'no macOS `gtimeout` fallback for the wrapper'
    _ok('both npm invocations run under a bounded timeout wrapper')


def test_npm_fetch_timeouts_are_capped():
    """npm's own fetch-timeout/retries must be capped so it errors in ~1 min."""
    block = _npm_block(_install_sh())
    assert 'npm_config_fetch_timeout' in block, \
        'npm_config_fetch_timeout not set — npm keeps its 300s default'
    assert 'npm_config_fetch_retries' in block, \
        'npm_config_fetch_retries not set — npm keeps its 2-retry default'
    # fetch_timeout should be well under the outer 5-min ceiling.
    m = re.search(r'npm_config_fetch_timeout=(\d+)', block)
    assert m and int(m.group(1)) <= 120000, \
        'npm_config_fetch_timeout must be <= 120000ms to fail fast'
    _ok('npm fetch-timeout and retries are capped for fail-fast behavior')


def test_npm_registry_override_is_honored():
    """install.sh must honor TOFU_NPM_REGISTRY when the export baked one in."""
    block = _npm_block(_install_sh())
    assert 'TOFU_NPM_REGISTRY' in block, \
        'install.sh does not read TOFU_NPM_REGISTRY (no corp-mirror redirect)'
    assert 'npm_config_registry' in block, \
        'install.sh does not export npm_config_registry from the override'
    _ok('install.sh honors the TOFU_NPM_REGISTRY mirror override')


def test_npm_step_stays_fail_open():
    """A failed/timed-out npm must WARN, never abort the install."""
    block = _npm_block(_install_sh())
    # Both invocations keep the `|| warn ...` fail-open tail.
    assert block.count('|| warn') >= 2, \
        'npm step lost its `|| warn` fail-open fallback'
    # No `exit`/`set -e` escape hatch inside the block that would abort.
    assert 'exit 1' not in block, 'npm step must never `exit 1` (it is optional)'
    _ok('npm step remains optional and fail-open on failure/timeout')


def test_skip_node_flag_exists_and_short_circuits():
    """--skip-node must parse and bypass the whole conda-nodejs + npm step."""
    text = _install_sh()
    assert '--skip-node)' in text, 'no --skip-node argument parser'
    assert 'SKIP_NODE=1' in text, '--skip-node does not set SKIP_NODE=1'
    assert 'SKIP_NODE=0' in text, 'SKIP_NODE has no default init'
    # The node step must gate on SKIP_NODE BEFORE the conda-nodejs solve.
    assert re.search(r'if \[\[ "\$SKIP_NODE" -eq 1 \]\]', text), \
        'node step does not branch on $SKIP_NODE'
    # When skipping, no conda-nodejs solve and no npm run happen in that branch.
    m = re.search(r'if \[\[ "\$SKIP_NODE" -eq 1 \]\]; then(.*?)elif conda install', text, re.S)
    assert m, 'SKIP_NODE branch is not followed by the elif conda-install path'
    skip_body = m.group(1)
    assert 'npm ' not in skip_body and 'conda install' not in skip_body, \
        '--skip-node branch still runs npm/conda — it must fully bypass them'
    _ok('--skip-node parses and fully bypasses the conda-nodejs + npm step')


def test_preflight_probe_is_bounded_and_skips_on_unreachable():
    """A ~5s reachability probe must gate npm and skip it when unreachable."""
    block = _npm_block(_install_sh())
    # The probe uses a bounded curl/wget (max 5s) — never an unbounded call.
    assert '--max-time 5' in block, \
        'preflight curl is not bounded to 5s (--max-time 5 missing)'
    assert '_NPM_REACHABLE' in block, 'no reachability flag drives the skip'
    # On unreachable, npm must be SKIPPED (warn) — the elif/else chain guards it.
    assert re.search(r'if \[\[ "\$_NPM_REACHABLE" -eq 0 \]\]; then', block), \
        'npm run is not gated on the reachability result'
    # The probe itself has a belt-and-braces timeout backstop.
    assert 'timeout 6' in block, \
        'preflight probe lacks a hard timeout backstop (could become the new hang)'
    # The npm invocations sit on the elif branches AFTER the unreachable check,
    # so an unreachable registry never reaches them.
    assert re.search(r'_NPM_REACHABLE" -eq 0 \]\]; then.*?elif \[\[ -f.*?npm ci', block, re.S), \
        'npm ci is not downstream of the unreachable-skip guard'
    _ok('preflight probe is 5s-bounded and skips npm on an unreachable registry')


def test_preflight_probes_a_package_endpoint_not_the_root():
    """The probe must hit a real package endpoint, not the registry ROOT.

    A transparent corp proxy can 200/redirect the registry root while still
    403-ing actual package traffic; a HEAD to the root would then
    false-positive 'reachable' and npm would stall on the first package
    fetch. So the probe GETs a package's metadata (esbuild) with curl -f so a
    403/404 on package traffic is correctly classified UNREACHABLE.
    """
    block = _npm_block(_install_sh())
    # A dedicated probe URL derived from the registry, targeting a package.
    assert '_NPM_PROBE_URL' in block, \
        'no dedicated package probe URL — probe likely hits the bare root'
    assert re.search(r'_NPM_PROBE_URL="\$\{_NPM_REGISTRY_URL%/\}/esbuild"', block), \
        'probe URL does not target the /esbuild package endpoint'
    # curl must FAIL on HTTP >= 400 so a proxy 403 on package traffic => skip.
    assert re.search(r'curl -fsS.*"\$_NPM_PROBE_URL"', block), \
        'curl probe is not `-f` (fails on 4xx/5xx) against the package URL'
    # It must NOT be a HEAD (-I) — a root HEAD is exactly the fooled case.
    m = re.search(r'curl -f\S* -I ', block)
    assert not m, 'probe still uses a HEAD (-I) request — can be fooled by a proxy'
    # The reachability decision must be driven by THIS package probe, and the
    # npm run must sit downstream of the unreachable guard.
    assert re.search(r'"\$_NPM_PROBE_URL".*?_NPM_REACHABLE=0', block, re.S), \
        'package probe result does not drive _NPM_REACHABLE'
    _ok('preflight probes the /esbuild package endpoint with curl -f, not the root')


def test_export_bakes_npm_registry_when_configured():
    """export._patch_install_sh_proxy emits TOFU_NPM_REGISTRY from npm_registry."""
    pytest.importorskip('export', reason='export.py not shipped in opensource builds')
    import inspect

    import export
    src = inspect.getsource(export._patch_install_sh_proxy)
    assert 'npm_registry' in src, \
        'export does not read the npm_registry config key'
    assert 'TOFU_NPM_REGISTRY' in src, \
        'export does not bake TOFU_NPM_REGISTRY into install.sh'
    _ok('export bakes TOFU_NPM_REGISTRY when npm_registry is configured')


def main():
    print()
    print(_color('═══ install.sh npm fail-fast Guard Tests ═══', '36'))
    print()
    tests = [
        test_npm_invocations_are_timeout_wrapped,
        test_npm_fetch_timeouts_are_capped,
        test_npm_registry_override_is_honored,
        test_npm_step_stays_fail_open,
        test_skip_node_flag_exists_and_short_circuits,
        test_preflight_probe_is_bounded_and_skips_on_unreachable,
        test_preflight_probes_a_package_endpoint_not_the_root,
        test_export_bakes_npm_registry_when_configured,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
