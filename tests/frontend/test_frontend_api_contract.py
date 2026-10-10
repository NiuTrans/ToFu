"""Browser/backend route, verb and retained domain-method contracts.

AST inventory covers retained and typed HTTP owners against the real production
route assembly without storage or worker startup. The independent paired-domain
check prevents retained Api.<domain>.<method> calls from naming missing methods.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
JS_DIR = os.path.normpath(os.path.join(HERE, '..', '..', 'frontend', 'src'))
API_JS = runtime_section_path('api.js')
ORCHESTRATION_API_TS = os.path.join(
    JS_DIR, 'features', 'orchestration', 'api-client.ts')
ORCHESTRATION_CONTRACT_TS = os.path.join(
    JS_DIR, 'features', 'orchestration', 'request-contracts.generated.ts')
API_SURFACE_FILES = (API_JS, *sorted(
    str(path) for path in Path(JS_DIR, 'api', 'endpoint-registry').glob('*.ts')
))
API_SURFACE_RELPATHS = {
    os.path.relpath(path, JS_DIR).replace(os.sep, '/')
    for path in (*API_SURFACE_FILES, ORCHESTRATION_API_TS,
                 ORCHESTRATION_CONTRACT_TS)
}


# ── Shared helpers ────────────────────────────────────────────────────
def _strip_comments(src: str) -> str:
    """Remove /* */ block and // line comments WITHOUT ever treating a comment
    marker that appears inside a string/template literal — or a ``/*`` that
    appears inside an already-open ``//`` line comment — as real syntax.

    WHY that rule matters (the incident this guard was born from): the naive
    ``re.sub(r'/\\*.*?\\*/', …, DOTALL)`` pass ran FIRST and matched the ``/*``
    inside the *line* comment ``// … /api/paper/* (multipart …)`` — that ``*``
    is a path wildcard, not a comment closer. It then ate forward to the next
    real ``*/``, deleting the live ``upload:`` / ``fetchArxivStream:`` entries
    of the ``paper`` domain in api.js, and Direction-2 reported those real call
    sites as "method NOT defined under that domain". The only correct rule is
    "whichever comment/string opener comes FIRST wins".

    Delegates to the SINGLE shared implementation (charter #24), which
    implements exactly that rule with the same single-pass, state-tracking
    scanner. MEASURED equivalent before the swap, both ways:
      * on the real api.js this guard scans — identical line count (1680) and
        an IDENTICAL set of surviving code identifiers;
      * on the incident itself, reproduced as a fixture: with
        ``// … /api/paper/* (multipart form-data)`` immediately above them,
        both ``upload:`` and ``fetchArxivStream:`` survive the shared pass.
    """
    from tests._source_scan import strip_comments
    return strip_comments(src, lang='js', inline=True)


def _read(path: str) -> str:
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def _api_surface_source() -> str:
    """Return the core client plus focused domain transports as one surface."""
    return '\n'.join(_read(path) for path in API_SURFACE_FILES)


def _match_first_brace_block(s: str, open_idx: int) -> str:
    """Return the substring of the { ... } block whose opening brace is at
    ``open_idx`` (balanced-brace scan, ignoring braces inside strings/templates
    is NOT attempted — api.js domain blocks don't embed literal unbalanced
    braces in strings, and the parser is only used to collect top-level keys)."""
    depth = 0
    for i in range(open_idx, len(s)):
        c = s[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return s[open_idx:i + 1]
    return s[open_idx:]


def _api_js_domain_methods() -> dict[str, set[str]]:
    """Parse api.js into {domain: {method, ...}}.

    Each domain is declared ``const <domain> = { ... }``. Within a domain block
    we collect top-level ``key:`` identifiers (method names). Nested object
    literals are skipped by only taking keys at brace-depth 1 of the block.
    """
    s = _strip_comments(_api_surface_source())
    domains: dict[str, set[str]] = {}
    for m in re.finditer(r'\bconst\s+([a-zA-Z_]\w*)\s*=\s*\{', s):
        name = m.group(1)
        block = _match_first_brace_block(s, m.end() - 1)
        methods: set[str] = set()
        depth = 0
        # Walk the block char by char; record ``ident:`` only at depth 1.
        i = 0
        while i < len(block):
            c = block[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
            elif depth == 1:
                km = re.match(r'\s*([a-zA-Z_]\w*)\s*:', block[i:])
                if km:
                    methods.add(km.group(1))
                    i += km.end() - 1
            i += 1
        if methods:
            domains[name] = methods
    # The Orchestration facade is generated from the canonical typed request
    # registry, so its dynamic method set cannot be recovered by parsing an
    # object literal. Seed the exact generated policy instead of restoring a
    # hand-maintained classic method table for this source-only guard.
    from lib.orchestration.browser_endpoint_contract import (
        orchestration_browser_request_contract_dicts,
    )
    orchestration_methods = {'listResult', 'list', 'save'}
    for contract in orchestration_browser_request_contract_dicts().values():
        orchestration_methods.add(str(contract['resultMethod']))
        orchestration_methods.add(str(contract['directMethod']))
    domains['orchestrations'] = orchestration_methods
    return domains


# ── call-site scan ────────────────────────────────────────────────────
def _api_call_sites() -> set[tuple[str, str, str]]:
    """All ``Api.<domain>.<method>(`` call sites across retained sources EXCEPT api.js
    (and generated bundles). Returns {(domain, method, relpath)}."""
    call_re = re.compile(r'\bApi\.([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\s*\(')
    out: set[tuple[str, str, str]] = set()
    for root, dirs, files in os.walk(JS_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(('.', '__'))]
        for name in sorted(files):
            if not name.endswith('.js'):
                continue
            rel = os.path.relpath(
                os.path.join(root, name), JS_DIR).replace(os.sep, '/')
            if rel in API_SURFACE_RELPATHS \
                    or name.startswith('bundle-') \
                    or name.startswith('feature-'):
                continue
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            s = _strip_comments(_read(path))
            for m in call_re.finditer(s):
                out.add((m.group(1), m.group(2), rel))
    return out


# ── Direction 1: FE (path, method) ⊆ live backend routes ──────────────
def test_api_js_paths_resolve_to_real_routes():
    """All browser HTTP owners resolve to production paths with valid verbs.

    Assemble the real route registry without starting storage or workers.
    AST extraction covers typed clients, generated catalogues, path helpers,
    concatenations and aliases; validation does not rewrite retired paths.
    """
    from tests.support.frontend_api_inventory import (
        browser_api_inventory, browser_route_app, route_failure,
    )

    app = browser_route_app()
    failures = []
    for endpoint in browser_api_inventory()['endpoints']:
        if endpoint['path'].startswith('/api/v4/trading'):
            continue  # External plugin owns this transport.
        failure = route_failure(app, endpoint['path'], endpoint['method'])
        if failure:
            failures.append(f"{endpoint['file']}:{endpoint['line']}: {failure}")
    assert not failures, '\n'.join(failures)


# ── Direction 2: (domain, method) call sites ⊆ api.js definitions ──────
def test_api_call_sites_are_defined_paired():
    """Every Api.<domain>.<method>() call names a method DEFINED under that
    exact domain in api.js. Paired check — a bare method name defined under a
    DIFFERENT domain does not satisfy it."""
    domain_methods = _api_js_domain_methods()
    assert domain_methods, 'failed to parse any domain from api.js'
    # Sanity: the known domains from the Api = {...} assembly must be present.
    for expected in ('conversations', 'project', 'folders', 'chat'):
        assert expected in domain_methods, (
            f'api.js parser missed the {expected!r} domain — parser is broken, '
            'the guard would be a false green'
        )

    violations: dict[tuple[str, str], set[str]] = {}
    for domain, method, rel in _api_call_sites():
        defined = domain_methods.get(domain, set())
        if method not in defined:
            violations.setdefault((domain, method), set()).add(rel)

    if violations:
        details = '\n'.join(
            f'  Api.{d}.{m}()  <- {sorted(files)}'
            for (d, m), files in sorted(violations.items())
        )
        pytest.fail(
            'Call sites invoke Api.<domain>.<method>() where the method is NOT '
            'defined under that domain in api.js (runtime TypeError — dead '
            'button). Add the method to the domain in api.js or fix the call:\n'
            + details
        )


@pytest.mark.parametrize('domain,method', [
    ('missing_domain', 'list'), ('folders', 'missing_method'),
])
def test_domain_method_guard_rejects_unknown_names(monkeypatch, domain, method):
    monkeypatch.setattr(
        __import__(__name__, fromlist=['_api_call_sites']),
        '_api_call_sites', lambda: {(domain, method, 'synthetic-client.js')},
    )
    with pytest.raises(pytest.fail.Exception, match='NOT defined'):
        test_api_call_sites_are_defined_paired()


def test_browser_contract_scanner_and_native_list_regressions():
    """The shipping guard also exercises adversarial inventory/list examples."""
    import subprocess

    root = os.path.dirname(JS_DIR)
    root = os.path.dirname(root)
    result = subprocess.run(
        ['node', '--test', 'tests/frontend_api_inventory.test.mjs',
         'tests/frontend_api_lists.test.mjs'], cwd=root,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
