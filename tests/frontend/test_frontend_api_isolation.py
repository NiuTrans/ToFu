"""Frontend API-isolation guard.

Goal
----
The frontend has one HTTP transport owner:
``frontend/src/api/transport.ts``. The retained ``api.js`` runtime section is
an endpoint registry only and delegates every request to that required typed
owner. Every other source MUST call the owned client and MUST NOT issue a raw
``fetch('/api/...')`` or ``fetch(apiUrl('/api/...'))``.

End state reached 2026-05-28
----------------------------
``BASELINE = {}`` — every runtime section has been migrated. The hard rule is
now in steady state: ``test_no_new_files_call_api_directly`` fails CI the
moment any section adds a raw ``fetch('/api/...')``.

The ``BASELINE`` ratchet machinery is preserved in case a future
endpoint family lands in legacy form before the matching ``Api.*``
domain is added — populating ``BASELINE`` with the new file gives a
documented decrescendo path back to zero.

Variable-URL bypass guard (added 2026-07-14)
--------------------------------------------
The inline-string ratchet above only sees ``fetch('/api/...')`` literals.
A call whose URL is a VARIABLE or expression — ``fetch(startUrl)``,
``fetch(url)``, ``fetch(apiUrl(u))``, ``fetch(_logCleanApiUrl('/api/...'))``
— slips past it entirely. ``test_no_variable_url_api_fetches`` closes that
hole: it counts
every ``fetch(`` whose first argument is not a plain string literal, and
fails unless the file is a documented carve-out in
``_ALLOWED_VARIABLE_FETCHES`` (external OAuth token endpoint, image-blob
hydration). Comments are stripped before scanning so a ``fetch(...)`` shown
in a comment is not counted.

Adding a new endpoint
---------------------
1. Add a method to the owning ``frontend/src/api/endpoint-registry/`` module,
   or generate it from the endpoint contract for domains with a generated client.
2. Call that owner from the feature; never call ``fetch`` directly.
3. Run this test — it must stay green with ``BASELINE = {}``.
"""

from __future__ import annotations

import os
import json
import re
import subprocess

import pytest

from tests._jsdom import require_node_modules
from tests._runtime_sections import (
    runtime_section,
    runtime_section_names,
    shipped_source_text,
)

pytestmark = pytest.mark.unit


# ── Configuration ────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.normpath(os.path.join(HERE, '..', '..', 'frontend', 'src'))
NATIVE_TRANSPORT = os.path.join(FRONTEND_DIR, 'api', 'transport.ts')

# Matches:
#   fetch('/api/foo'      fetch("/api/foo"      fetch(`/api/foo`
#   fetch(apiUrl('/api/foo'))   fetch(apiUrl(`/api/${x}`))
_LEGACY_FETCH_RE = re.compile(
    r"""fetch\(\s*                       # fetch(
        (?:apiUrl\(\s*)?                 # optional apiUrl(
        ['"`]                            # opening quote
        /?api/                           # /api/ prefix (slash optional for tagged-template trims)
    """,
    re.VERBOSE,
)

# There is no runtime exception: even the retained endpoint registry delegates
# to the typed transport. Keep this explicit so a rollback transport cannot be
# reintroduced under the old logical ``api.js`` name.
ALLOWED_FILES: frozenset[str] = frozenset()

# Matches a fetch( whose FIRST argument is NOT a plain string literal — i.e. a
# variable or expression URL (fetch(url) / fetch(startUrl) / fetch(apiUrl(u)) /
# fetch(_logCleanApiUrl('/api/...'))). These bypass the inline-string ratchet
# yet still hit the backend directly.
_VARIABLE_FETCH_RE = re.compile(r"\bfetch\s*\(\s*(?![)'\"`])")
_NATIVE_FETCH_SCANNER = r'''
const ts = require('typescript');
const sources = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const trees = new Map(Object.entries(sources).map(([name, source]) => [name,
  ts.createSourceFile(name, source, ts.ScriptTarget.Latest, true,
    name.endsWith('.ts') ? ts.ScriptKind.TS : ts.ScriptKind.JS)]));
const options = { allowJs: true, noLib: true, noResolve: true,
  target: ts.ScriptTarget.Latest };
const host = ts.createCompilerHost(options);
host.fileExists = file => trees.has(file);
host.readFile = file => sources[file];
host.getSourceFile = file => trees.get(file);
const checker = ts.createProgram([...trees.keys()], options, host).getTypeChecker();
const result = {};
for (const name of trees.keys()) {
  let count = 0;
  function visit(node) {
    if (!ts.isCallExpression(node)) {
      ts.forEachChild(node, visit);
      return;
    }
    const callee = node.expression;
    let nativeFetch = ts.isIdentifier(callee) && callee.text === 'fetch';
    if (ts.isPropertyAccessExpression(callee)
        && ts.isIdentifier(callee.name) && callee.name.text === 'fetch') {
      const symbol = checker.getSymbolAtLocation(callee.name);
      const declarations = symbol?.getDeclarations() || [];
      nativeFetch = declarations.length === 0;
    }
    if (nativeFetch) {
      const symbol = checker.getSymbolAtLocation(
        ts.isIdentifier(callee) ? callee : callee.name);
      const declarations = symbol?.getDeclarations() || [];
      const injected = declarations.some(declaration =>
        (ts.isParameter(declaration) && !!declaration.type)
        || (ts.isVariableDeclaration(declaration) && !!declaration.type)
        || ts.isImportSpecifier(declaration));
      const argument = node.arguments[0];
      if (!injected && argument && !ts.isStringLiteralLike(argument)) count++;
    }
    ts.forEachChild(node, visit);
  }
  visit(trees.get(name));
  result[name] = count;
}
process.stdout.write(JSON.stringify(result));
'''

# Documented, LEGITIMATE variable-URL fetches that are NOT Tofu /api/* business
# calls — the only permitted carve-outs. Keyed by posix relpath under static/js.
_ALLOWED_VARIABLE_FETCHES = {
    # Cross-origin OAuth provider token endpoint (Anthropic/OpenAI), not /api/*.
    'settings/oauth.js': 1,
    # Browser-assisted desktop transport talks to the TofuRemote's
    # loopback-only broker (127.0.0.1:15180..15189), not a Tofu /api route.
    # It intentionally needs cross-origin/PNA RequestInit fields that do not
    # belong in the authenticated backend API client. The actual same-origin
    # /api/desktop/poll hop is routed through Api.desktop.relayPoll().
    'local-control.js': 1,
    # (tofu-pet.js HAD a carve-out here for fetching its SVG frames; the
    # 2026-07-30 raster revamp loads frames with new Image() instead — zero
    # fetch() calls — so the entry was removed, tightening the ratchet.)
}

# Bundle output is generated; never count it. feature-*.js is the deferred
# bundle family (same generator, same rule).
def _is_generated(name: str) -> bool:
    return bool(re.fullmatch(
        r'(?:bundle|feature)-[0-9a-f]{8}\.js'
        r'|domain-[a-z0-9-]+-[0-9a-f]{8}\.js',
        name,
    ))


# ── Per-file ratchet baseline ───────────────────────────────────────
# Established 2026-05-28 right after creating api.js + migrating folders.
# Numbers MUST monotonically decrease toward {}. Do NOT raise a value here.
BASELINE: dict[str, int] = {}


# ── Helpers ─────────────────────────────────────────────────────────
def _count_legacy_calls(path: str) -> int:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        return 0
    return len(_LEGACY_FETCH_RE.findall(content))


def _strip_comments(src: str) -> str:
    """Remove /* */ block and // line comments so a fetch( mentioned inside a
    comment is not counted.

    Delegates to the SINGLE shared implementation (charter #24). This is an
    UPGRADE over the local ``re.sub(r'//[^\\n]*', '', s)`` it replaced, not mere
    parity: that regex treated the ``//`` inside a string literal as a comment,
    so ``if (path.startsWith('http://')) return path;`` in api.js lost
    everything from ``//`` onward — real code deleted before the scan saw it.
    Measured across all frontend JS: 27 files differ for that reason, and in
    every one the shared tokenizer preserves code the regex ate (zero cases of
    the reverse).

    Verified NOT to change THIS guard's verdict: the variable-URL fetch count is
    identical under both strippers for every scanned file (0 files differ), so
    the ratchet numbers below carry over unchanged.
    """
    from tests._source_scan import strip_comments
    return strip_comments(src, lang='js', inline=True)


def _scan_native_fetches(sources: dict[str, str]) -> dict[str, int]:
    """Use the TypeScript AST to exclude typed ports and receiver methods."""
    require_node_modules('typescript')
    completed = subprocess.run(
        ['node', '-e', _NATIVE_FETCH_SCANNER],
        input=json.dumps(sources), text=True, capture_output=True, check=True,
    )
    return json.loads(completed.stdout)


def _scan_variable_fetches() -> dict[str, int]:
    """Scan authored sections, without the harness's injected owner graphs."""
    sources = {
        rel: _strip_comments(
            shipped_source_text(f'frontend/src/runtime/sections/{rel}'))
        for rel in runtime_section_names() if rel not in ALLOWED_FILES
    }
    return {
        rel: count for rel, count in _scan_native_fetches(sources).items()
        if count > 0
    }


def _scan_all() -> dict[str, int]:
    """Scan every logical section of the retained runtime for API fetches."""
    out: dict[str, int] = {}
    for rel in runtime_section_names():
        if rel in ALLOWED_FILES:
            continue
        c = len(_LEGACY_FETCH_RE.findall(_strip_comments(
            shipped_source_text(f'frontend/src/runtime/sections/{rel}'))))
        if c > 0:
            out[rel] = c
    return out


# ── Tests ───────────────────────────────────────────────────────────
def test_api_js_exists():
    """The migrated endpoint registry and typed transport must both exist."""
    assert 'api.js' in runtime_section_names()
    assert os.path.isfile(NATIVE_TRANSPORT)
    source = runtime_section('api.js')
    assert 'runtimeScope.Api = createEndpointRegistry(requiredApiTransport' in source
    assert 'const Api = runtimeScope.Api;' in source


def test_vite_transport_is_the_only_native_fetch_owner():
    """Backend HTTP has one owner; hashed locale data has one asset loader."""
    sources = {}
    for root, _dirs, files in os.walk(FRONTEND_DIR):
        for name in files:
            if name.endswith('.ts'):
                path = os.path.join(root, name)
                with open(path, encoding='utf-8') as fh:
                    sources[os.path.relpath(path, FRONTEND_DIR).replace(os.sep, '/')] = fh.read()
    actual = {path: count for path, count in _scan_native_fetches(sources).items() if count}
    assert actual == {'api/transport.ts': 1, 'i18n/index.ts': 1}, (
        'native backend calls must use api/transport.ts and static locale data '
        f'must use only i18n/index.ts; fetch owners were {actual}'
    )
    transport = _strip_comments(sources['api/transport.ts'])
    assert "headers['X-Request-ID']" in transport
    assert "headers['X-Tofu-Affinity-Key']" in transport
    locale_loader = _strip_comments(sources['i18n/index.ts'])
    assert 'fetch(descriptors.get(language)!.catalogUrl' in locale_loader
    assert '/api/' not in locale_loader


def test_no_new_files_call_api_directly():
    """A file that currently has zero legacy calls must STAY at zero."""
    actual = _scan_all()
    new_violators = sorted(set(actual) - set(BASELINE))
    if new_violators:
        details = '\n'.join(f'  {n}: {actual[n]} call(s)' for n in new_violators)
        pytest.fail(
            'New files are calling /api/* directly instead of going through '
            'the owned frontend transport:\n' + details +
            '\n\nFix: route those fetches through the endpoint registry or a '
            'generated typed client.'
        )


def test_legacy_fetch_count_only_decreases():
    """Each known-legacy file must decrease its raw fetch count, never grow.

    When you migrate calls, run this test and paste the new actual
    counts into BASELINE above (lower numbers only).
    """
    actual = _scan_all()
    regressions = []
    for name, baseline in BASELINE.items():
        cur = actual.get(name, 0)
        if cur > baseline:
            regressions.append((name, baseline, cur))
    if regressions:
        msg = '\n'.join(
            f'  {n}: baseline={b}, now={c} (+{c - b})'
            for n, b, c in regressions
        )
        pytest.fail(
            'Frontend legacy fetch count increased — new direct calls to /api/* '
            'must instead go through the owned frontend transport:\n' + msg
        )


def test_fetch_scanner_distinguishes_native_calls_from_injected_ports():
    """The guard must keep detecting global fetches without flagging ports."""
    actual = _scan_native_fetches({
        'native.ts': 'fetch(startUrl); window.fetch(url);',
        'receivers.ts': 'declare const ports: { fetch(id: string): Promise<void> }; '
            + 'ports.fetch(id); await fetch(url);',
        'no-argument.ts': (
            'declare const ports: { fetch(id: string): Promise<void> }; '
            + 'ports.fetch(id); fetch();'),
        'callback.ts': (
            'async function loadConfirmed(fetch: () => Promise<Row[]>) { await fetch(); }'),
        'interface.ts': (
            'interface Row { id: string }; '
            'function resolveMissing(id: string, ports: { fetch(id: string): Promise<Row> }) '
            '{ return ports.fetch(id); }'),
        'typed-receiver.ts': (
            'declare const ports: { fetch(id: string): Promise<void> }; ports.fetch(url);'),
        'untyped.js': 'function read(fetch) { return fetch(url); }',
        'scoped.ts': 'function port(fetch: () => Promise<void>) { fetch(); } '
          + 'function native() { fetch(url); }',
    })
    assert actual == {
        'native.ts': 2, 'receivers.ts': 1, 'no-argument.ts': 0,
        'callback.ts': 0, 'interface.ts': 0, 'typed-receiver.ts': 0,
        'untyped.js': 1, 'scoped.ts': 1,
    }


def test_no_variable_url_api_fetches():
    """A fetch() with a VARIABLE/expression URL bypasses the inline-string
    ratchet but still calls the backend directly. Only the documented non-/api
    carve-outs in _ALLOWED_VARIABLE_FETCHES are permitted."""
    suspects = _scan_variable_fetches()
    violations = {}
    for f, cnt in suspects.items():
        allowed = _ALLOWED_VARIABLE_FETCHES.get(f, 0)
        if cnt > allowed:
            violations[f] = (cnt, allowed)
    if violations:
        details = '\n'.join(
            f'  {f}: {c} variable-URL fetch(es), only {a} allowed'
            for f, (c, a) in sorted(violations.items())
        )
        pytest.fail(
            'Variable-URL fetch() calls bypass the owned frontend transport '
            '(the inline-string ratchet cannot see them):\n' + details +
            '\n\nFix: route those fetches through the endpoint registry or a '
            'generated typed client, or — '
            'if it is a genuine non-/api call — add it to '
            '_ALLOWED_VARIABLE_FETCHES with a reason.'
        )


def test_baseline_reflects_real_counts():
    """A stale BASELINE entry (file dropped below its budget, or vanished) must
    FAIL so the ratchet is tightened.

    This previously ``pytest.skip()``d, i.e. it could never report a problem —
    the one outcome it was written to detect produced a 'skipped' verdict. With
    ``BASELINE = {}`` it is currently a no-op; it earns its keep the moment a
    future endpoint family is granted a temporary budget here, by forcing that
    budget back down as the migration progresses instead of letting it linger.
    """
    actual = _scan_all()
    stale = [(n, b, actual.get(n, 0)) for n, b in BASELINE.items() if actual.get(n, 0) < b]
    assert not stale, (
        'BASELINE in tests/frontend/test_frontend_api_isolation.py is too generous — '
        'tighten it to the actual counts so the migrated files stay migrated:\n'
        + '\n'.join(f'  {n}: BASELINE={b}, actual={c}' for n, b, c in stale))
