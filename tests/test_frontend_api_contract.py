"""Frontend↔backend API-contract regression guard.

Two invariants that keep the unified API client (``static/js/api.js``) and the
backend route surface from silently drifting apart. Complements
``test_frontend_api_isolation.py`` (which only forbids RAW ``fetch('/api/...')``
outside api.js) — that guard proves nobody bypasses the client; THIS guard
proves the client's own contract is sound in both directions.

Direction 1 — every ``(/api/* path, HTTP method)`` pair used by api.js resolves
    to a REAL backend route that ACCEPTS that method.
    Authoritative source = the LIVE Quart ``url_map`` (built by the conftest
    ``flask_app`` fixture via ``server.register_all(app)``), NOT a regex scan of
    ``@route`` decorators. The url_map is the single source of truth: it already
    contains entry-point plugin blueprints (e.g. ``/api/v1/trading``),
    ``register_task_routes`` factory poll/abort routes, and ``@websocket``
    endpoints — so this guard is INHERENTLY immune to the false positives a
    source-only scan produces (a source scan flagged ``/api/v1/trading`` as a
    dead call because trading was extracted to a plugin). No hardcoded
    allowlist to drift.
    METHOD-AWARE: the check is on the ``(path, method)`` pair against each
    rule's ``rule.methods`` (auto HEAD/OPTIONS excluded), NOT a path-only set.
    A path that exists but is called with a verb the route does not allow is a
    runtime 405 — this guard catches that method-drift class, and the failure
    names the offending method plus the methods the route actually allows.
    The method for each call is resolved statically from the client's own verb
    wrappers (``get/post/put/patch/del/stream`` → GET/POST/PUT/PATCH/DELETE/GET)
    and from the literal ``method:`` in ``request(path, {method:'X'})`` calls —
    including the two-pass ``const url = <literal> … request(url, {method})``
    indirection. If a call site's method CANNOT be statically resolved (a truly
    dynamic ``method:`` expression), the test FAILS demanding the author make it
    explicit — it is never silently defaulted to GET, and there is no exemption
    allowlist (that would reintroduce the drift-prone whitelist this guard
    deliberately avoids).
    Fixture-build failure is a HARD FAIL, never a skip: a guard the environment
    can silently skip is no guard, and the whole suite already depends on this
    fixture.

Direction 2 — every ``Api.<domain>.<method>()`` call site (in any JS file other
    than api.js) names a method that is actually DEFINED under that exact domain
    in api.js.
    The check is PAIRED on ``(domain, method)`` — NOT a loose "some domain
    defines this bare method name" test. ``Api.project.redo()`` must fail if the
    ``project`` domain lacks ``redo``, even when another domain happens to
    define a ``redo``. This catches "added a call site but forgot the api.js
    definition" — a live ``TypeError: undefined is not a function`` dead button.
    Pure-static (no app build). Currently 0 violations; it locks that in.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
JS_DIR = os.path.normpath(os.path.join(HERE, '..', 'static', 'js'))
API_JS = os.path.join(JS_DIR, 'api.js')


# ── Shared helpers ────────────────────────────────────────────────────
def _strip_comments(src: str) -> str:
    """Remove /* */ block and // line comments (not a full JS parser — good
    enough to stop a path/call mentioned in a comment from being counted)."""
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.DOTALL)
    src = re.sub(r'//[^\n]*', '', src)
    return src


def _norm_path(p: str) -> str:
    """Normalize a URL path for set comparison: collapse every path parameter
    — Flask ``<int:x>`` / ``<conv_id>`` and JS ``${...}`` template exprs — to a
    single ``<>`` placeholder, drop the querystring, and strip a trailing slash.
    """
    p = p.split('?')[0].split('#')[0]
    p = re.sub(r'\$\{[^}]*\}', '<>', p)   # JS template expression
    p = re.sub(r'<[^>]+>', '<>', p)       # Flask/Quart converter
    p = re.sub(r'<>(?:<>)+', '<>', p)     # coalesce adjacent placeholders
    return p.rstrip('/') or '/'


def _read(path: str) -> str:
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


# ── api.js parsing ────────────────────────────────────────────────────
# Verb wrapper name → HTTP method. These wrappers encode the method in their
# NAME (see api.js ``function get/post/put/patch/del/stream``), so the method
# is 100% reliable from the call token.
_VERB_METHOD = {
    'get': 'GET', 'post': 'POST', 'put': 'PUT',
    'patch': 'PATCH', 'del': 'DELETE', 'stream': 'GET',
}
# Sentinel verb for a call whose method cannot be resolved statically.
_UNRESOLVED = '<dynamic>'


def _base_literals(s: str) -> set[str]:
    """Literals bound to a ``_*_BASE`` const → plugin-prefix constants."""
    return set(
        re.findall(r"""_[A-Z][A-Z0-9_]*BASE\s*=\s*[`'"](/api/[^`'"]*)[`'"]""", s)
    )


def _resolve_request_method(s: str, lit_end: int) -> str | None:
    """For a ``request(<literal>, <opts>)`` call, read the literal ``method:``
    from the opts object that follows the path literal. Returns the METHOD
    string, ``'GET'`` when no ``method:`` key is present (request()'s default),
    or ``_UNRESOLVED`` when a ``method:`` key exists but its value is not a
    plain string literal (a dynamic expression → must be made explicit)."""
    after = s[lit_end:lit_end + 240]
    # Cut at the end of this call's opts object (first ');' or '})') to avoid
    # reading a sibling call's method.
    cut = re.search(r'\)\s*[;,]|\}\s*\)', after)
    window = after[:cut.end()] if cut else after
    km = re.search(r'method\s*:', window)
    if not km:
        return 'GET'  # request() defaults to GET
    lit = re.match(r"""\s*['"]([A-Za-z]+)['"]""", window[km.end():])
    if lit:
        return lit.group(1).upper()
    return _UNRESOLVED  # method: <expr> — dynamic, force explicit


def _api_js_path_verbs():
    """Extract every ``(normalized_path, METHOD)`` pair the client can hit,
    plus a list of unresolved-method call sites.

    Resolution strategy (see module docstring, Direction 1):
      1. Verb wrappers ``get/post/put/patch/del/stream('/api/…')`` → method
         from the wrapper NAME.
      2. ``request('/api/…', {method:'X'})`` → literal method (default GET).
      3. Two-pass indirection ``const url = <literal> … request(url,{method})``:
         a path literal NOT directly preceded by an opener is matched to the
         nearest following ``request(<var>, {method:'X'})`` in a bounded window.
      4. ``_resolve('/api/…')`` URL builders (href / asset / exportUrl) → GET.
      5. ``_*_BASE`` plugin-prefix constants → excluded (plugin owns routes).

    Returns ``(pairs, unresolved)`` where ``pairs`` is a set of
    ``(path, METHOD)`` and ``unresolved`` is a list of ``(path, reason)`` whose
    method could not be statically determined — the caller FAILS on any.
    """
    s = _strip_comments(_read(API_JS))
    bases = _base_literals(s)
    opener = re.compile(r'(get|post|put|patch|del|stream|request|_resolve)\(\s*$')

    pairs: set[tuple[str, str]] = set()
    unresolved: list[tuple[str, str]] = []

    for m in re.finditer(r"""[`'"](/api/[^`'"]*)[`'"]""", s):
        raw = m.group(1)
        if raw in bases:
            continue
        norm = _norm_path(raw)
        before = s[max(0, m.start() - 48):m.start()]
        om = opener.search(before)
        verb: str | None
        if om:
            fn = om.group(1)
            if fn in _VERB_METHOD:
                verb = _VERB_METHOD[fn]
            elif fn == '_resolve':
                verb = 'GET'          # URL builder → browser GET / asset href
            else:                     # request(<literal>, {...})
                verb = _resolve_request_method(s, m.end())
        else:
            # Indirection: literal assigned to a var (const url = <lit> / ? : ),
            # then request(url, {method:'X'}) later. Find the nearest following
            # request(<ident>, {...}) within a bounded window and read its
            # literal method.
            follow = s[m.end():m.end() + 400]
            rm = re.search(r'request\(\s*[A-Za-z_]\w*\s*,', follow)
            if rm:
                verb = _resolve_request_method(s, m.end() + rm.end())
            else:
                verb = _UNRESOLVED

        if verb == _UNRESOLVED:
            unresolved.append((norm, 'method not a string literal / no opener'))
        else:
            pairs.add((norm, verb))

    return pairs, unresolved


def _api_js_paths() -> set[str]:
    """Path-only view (kept for the domain sanity checks / callers that only
    need the set of paths). Method-aware verification uses _api_js_path_verbs."""
    pairs, _ = _api_js_path_verbs()
    return {p for p, _ in pairs}


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
    s = _strip_comments(_read(API_JS))
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
    return domains


# ── call-site scan ────────────────────────────────────────────────────
def _api_call_sites() -> set[tuple[str, str, str]]:
    """All ``Api.<domain>.<method>(`` call sites across static/js EXCEPT api.js
    (and generated bundles). Returns {(domain, method, relpath)}."""
    call_re = re.compile(r'\bApi\.([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\s*\(')
    out: set[tuple[str, str, str]] = set()
    for root, dirs, files in os.walk(JS_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(('.', '__'))]
        for name in sorted(files):
            if not name.endswith('.js'):
                continue
            if name == 'api.js' or name.startswith('bundle-') or name.startswith('feature-'):
                continue
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            s = _strip_comments(_read(path))
            rel = os.path.relpath(path, JS_DIR).replace(os.sep, '/')
            for m in call_re.finditer(s):
                out.add((m.group(1), m.group(2), rel))
    return out


# ── Direction 1: FE (path, method) ⊆ live backend routes ──────────────
def _live_route_methods(flask_app) -> dict[str, set[str]]:
    """Map normalized route path → set of accepted HTTP methods from the LIVE
    url_map (auto-added HEAD/OPTIONS excluded)."""
    out: dict[str, set[str]] = {}
    for rule in flask_app.url_map.iter_rules():
        methods = {m for m in (rule.methods or set()) if m not in ('HEAD', 'OPTIONS')}
        out.setdefault(_norm_path(str(rule.rule)), set()).update(methods)
    return out


def test_api_js_paths_resolve_to_real_routes(flask_app):
    """Every (/api/* path, HTTP method) pair used by api.js must match a real
    registered route that ACCEPTS that method (checked against rule.methods —
    NOT a path-only set, so 405 method-drift is caught, not just 404).

    HARD requirement: this depends on the ``flask_app`` fixture (the live app).
    If the fixture cannot build, the test FAILS — it is never skipped. A guard
    the environment can silently disable is not a guard.
    """
    # Fixture-fail = hard fail: assert we actually got a usable app + url_map.
    assert flask_app is not None, 'flask_app fixture did not build the app'
    assert hasattr(flask_app, 'url_map'), 'flask_app has no url_map — cannot verify routes'

    live = _live_route_methods(flask_app)
    assert live, 'url_map yielded zero routes — app built empty, cannot verify'

    pairs, unresolved = _api_js_path_verbs()
    assert pairs, 'api.js yielded zero /api (path, method) pairs — parser broke'

    # (a) Any call whose method could not be statically resolved must be made
    #     explicit — never silently defaulted, never allowlisted.
    if unresolved:
        details = '\n'.join(f'  {p}  ({why})' for p, why in sorted(set(unresolved)))
        pytest.fail(
            'api.js has /api call sites whose HTTP method cannot be resolved '
            'statically (dynamic method: expression). Make the method an '
            'explicit string literal so the contract can be verified:\n' + details
        )

    # (b) path-missing (404 class) and method-mismatch (405 class), reported
    #     distinctly so a CI failure is diagnosable from the log alone.
    missing_path: list[str] = []
    method_mismatch: list[str] = []
    for path, verb in sorted(pairs):
        allowed = live.get(path)
        if allowed is None:
            missing_path.append(f'  {verb} {path}')
        elif verb not in allowed:
            method_mismatch.append(
                f'  {verb} {path}  — route allows {sorted(allowed)}'
            )

    if missing_path or method_mismatch:
        msg = ['api.js references /api routes that do not resolve as called:']
        if missing_path:
            msg.append('\n404 — path has NO matching backend route:')
            msg.extend(missing_path)
        if method_mismatch:
            msg.append('\n405 — path exists but does NOT accept that method:')
            msg.extend(method_mismatch)
        msg.append(
            '\nFix: correct the api.js path/verb, or add/adjust the backend '
            'route so the method is accepted.'
        )
        pytest.fail('\n'.join(msg))


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
        defined = domain_methods.get(domain)
        # Unknown domain (not a const block) → skip: it may be a low-level verb
        # namespace or a false match; the paired guard targets defined domains.
        if defined is None:
            continue
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
