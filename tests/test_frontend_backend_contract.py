#!/usr/bin/env python3
"""Frontend ↔ backend CONTRACT guard.

Goal
----
``static/js/api.js`` is the single seam through which the frontend talks to
the backend (enforced by ``tests/test_frontend_api_isolation.py``). This
sibling test closes the *other half* of the guarantee: every ``/api/...`` URL
that ``api.js`` calls MUST resolve to a route that is actually registered on
``server.app``.

Why this exists (root cause)
----------------------------
A manual, grep-only contract audit is unreliable: it sees ``@bp.route('/…')``
decorators but is BLIND to routes registered by a factory. The real incident:
``Api.orchestrations.runPoll`` / ``runAbort`` hit
``/api/v1/orchestrations/run/poll/<id>`` + ``/run/abort/<id>``, which have NO
literal decorator — they are minted by ``register_task_routes(...)`` at the tail
of ``routes/api_v1/orchestrations.py``. A decorator grep flags them as
"404-in-waiting" (a false positive); the live ``url_map`` shows them present.

So this test asserts against the LIVE ``server.app.url_map`` — the same routing
table the running server uses — making the guarantee factory-aware and
un-rottable. Any genuinely dead client path (a typo, a removed route, a
never-registered endpoint) fails CI across the WHOLE client, not just the v1
slice.

Method
------
1. Boot ``server.app`` (Flask→Quart shim).
2. Extract every ``/api/...`` string / template literal referenced in
   ``static/js/api.js``.
3. Normalise both the client paths and the registered rule templates to a
   converter-agnostic shape: strip the query string, and collapse every
   dynamic segment — JS ``${...}`` OR Werkzeug ``<conv_id>`` / ``<int:x>`` —
   to a single ``<*>`` placeholder.
4. Assert each client path has a matching registered template.

Carve-outs
----------
``/api/v1/trading`` — the trading subsystem was EXTRACTED to an external
``tofu-trading`` package (see CLAUDE.md); it mounts via entry-point groups and
is NOT in-tree, so its routes never register in the vanilla test app. Excluded
exactly like the isolation ratchet excludes ``api.js`` itself.
"""

from __future__ import annotations

import importlib.util
import os
import re

import pytest

from tests._runtime_sections import runtime_section_path, runtime_section_names

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
API_JS = runtime_section_path('api.js')
ORCHESTRATION_API_JS = runtime_section_path('api/orchestrations.js')
API_SURFACE_FILES = (API_JS, ORCHESTRATION_API_JS)


def _api_surface_source() -> str:
    return '\n'.join(
        open(path, encoding='utf-8').read()  # noqa: SIM115
        for path in API_SURFACE_FILES
    )

# Install Flask→Quart shim before importing routes (mirrors the pattern used by
# tests/test_task_supersede_and_stuck.py).
import quart as _quart  # noqa: E402
import sys  # noqa: E402
sys.modules.setdefault('flask', _quart)


# ── Carve-outs: client paths NOT expected to register in the vanilla app ──
# Prefix-matched against the RAW (pre-normalisation) client path.
_EXTERNAL_PLUGIN_PREFIXES = (
    '/api/v1/trading',   # extracted to the external tofu-trading package
)


def _get_app():
    """Boot server.app once (module-level cache)."""
    if getattr(_get_app, '_app', None) is not None:
        return _get_app._app
    from lib import auth_mode as _auth_mode
    os.environ.pop('TOFU_AUTH_MODE', None)
    _auth_mode.reset_for_tests()
    _auth_mode.set_mode('open', set_by='fe-be-contract-test')
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(ROOT, 'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    _get_app._app = mod.app
    return mod.app


# ── Client-path extraction ───────────────────────────────────────────
# Any '/api/...' or `/api/...` string/template literal, single/double/backtick
# quoted. We deliberately scan ALL literals (not just verb-call first args) so
# ternary-built paths (…/messages/by-id/${id}) and _resolve()/rawUrl() builders
# are covered too. Stops at the closing quote OR a '?' (query) OR whitespace.
_API_LITERAL_RE = re.compile(r"""['"`](/api/[^'"`?\s]*)""")

# Comment strippers — api.js documents its own contract with illustrative
# ``fetch('/api/...')`` / ``/api/foo`` prose that is NOT a real call site. Scan
# only executable code. (api.js has no string literal containing '//' or '/*',
# so these simple strippers are safe here.)
_BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)
_LINE_COMMENT_RE = re.compile(r'(?m)//.*$')


def _strip_comments(js_text: str) -> str:
    """Remove /* … */ and // comments.

    Delegates to the SINGLE shared implementation (charter #24).

    NOT a pure equivalence swap -- an UPGRADE, measured across all 171 frontend
    JS files: the local ``re.sub(r'//[^\n]*', '', s)`` treats the ``//`` inside
    a string literal such as ``path.startsWith('http://')`` as a comment and
    DELETES the rest of that line, i.e. it destroys real code. 27 of 171 files
    differ for exactly that reason, and in every one of them the shared
    tokenizer preserves code the local regex ate (checked: zero cases where the
    shared pass lost content the local one kept). So this strictly reduces false
    negatives -- a scan can no longer be blinded by a URL.
    """
    from tests._source_scan import strip_comments
    return strip_comments(js_text, lang='js', inline=True)


def _extract_client_paths(js_text: str) -> set[str]:
    js_text = _strip_comments(js_text)
    paths = set()
    for m in _API_LITERAL_RE.finditer(js_text):
        raw = m.group(1)
        # Trim any trailing concatenation artifact accidentally captured (none
        # expected given the char class, but be safe against a trailing '/').
        raw = raw.rstrip('/') or '/'
        paths.add(raw)
    return paths


# ── Coverage cross-check: does the extractor SEE every call site? ─────
# The verb wrappers whose first argument is the request path.
_VERB_OPEN_RE = re.compile(r'\b(?:get|post|put|patch|del|stream|request)\(')
# A first-arg the extractor CAN parse: an inline '/api/...' string/template
# literal (optionally wrapped in apiUrl(...)). This is exactly the shape
# _API_LITERAL_RE picks up as _extract_client_paths' first-arg case.
_INLINE_API_FIRST_ARG_RE = re.compile(r"""^\s*(?:apiUrl\(\s*)?['"`]/?api/""")


def _first_arg(src: str, start: int) -> str:
    """Return the first argument of a call, given the index just AFTER its '('.

    Walks to the matching depth-0 ',' or ')', respecting nested (){}[] — so a
    concatenation like ``_PREFIX + '/api/x'`` or ``BASE + path`` is returned in
    full, letting the caller tell an inline-literal first arg apart from a
    concatenation/variable one.
    """
    depth = 0
    n = len(src)
    j = start
    while j < n:
        c = src[j]
        if c in '([{':
            depth += 1
        elif c in ')]}':
            if depth == 0:
                return src[start:j]
            depth -= 1
        elif c == ',' and depth == 0:
            return src[start:j]
        j += 1
    return src[start:j]


def _scan_call_site_coverage(js_text: str) -> tuple[int, int]:
    """Return (refs, inline): verb-wrapper call sites whose FIRST ARG references
    '/api/' anywhere (``refs``), and the subset the extractor can parse as an
    inline literal (``inline``). ``refs > inline`` means a call site builds its
    path by concatenation/variable and would silently drop out of the contract
    check.
    """
    src = _strip_comments(js_text)
    refs = inline = 0
    for m in _VERB_OPEN_RE.finditer(src):
        arg = _first_arg(src, m.end())
        if '/api/' in arg:
            refs += 1
            if _INLINE_API_FIRST_ARG_RE.match(arg):
                inline += 1
    return refs, inline


# ── Normalisation: collapse dynamic segments on BOTH sides ────────────
_DYNAMIC_SEG_RE = re.compile(r'(\$\{[^}]*\}|<[^>]*>)')


def _normalise(path: str) -> str:
    """Collapse every dynamic segment to '<*>' and strip a trailing slash.

    A segment is dynamic if it contains a JS ``${...}`` interpolation OR a
    Werkzeug ``<...>`` converter. Mixed literal+dynamic segments (e.g.
    ``poll-${x}``) collapse the whole segment to ``<*>`` — acceptable because
    api.js never mixes a literal prefix with an interpolation inside one path
    segment for a route that also has a static sibling.

    Dangling ``${``: the extractor stops at ``?`` even when the ``?`` lives
    INSIDE a query-template interpolation — api.js:1251
    ``…/live-session${refresh ? '?refresh=1' : ''}`` extracts as
    ``…/live-session${refresh``. The interpolation only ever yields a query
    string (or nothing), so the ROUTE segment ends before it: truncate at the
    unclosed ``${`` (and drop the segment if that empties it). Without this,
    the half-interpolation fails _DYNAMIC_SEG_RE and the path reports a
    phantom MISS against a perfectly registered route.
    """
    segs = [s for s in path.split('/') if s != '']
    out = []
    for s in segs:
        if '${' in s and '}' not in s[s.index('${'):]:
            s = s[:s.index('${')]
            if not s:
                continue
        out.append('<*>' if _DYNAMIC_SEG_RE.search(s) else s)
    return '/' + '/'.join(out)


def _registered_templates(app) -> set[str]:
    tmpl = set()
    for rule in app.url_map.iter_rules():
        r = str(rule)
        if not r.startswith('/api/'):
            continue
        tmpl.add(_normalise(r))
    return tmpl


# ── Tests ─────────────────────────────────────────────────────────────
def test_api_js_exists():
    assert 'api.js' in runtime_section_names(), 'migrated api.js owner is missing'
    assert os.path.isfile(ORCHESTRATION_API_JS), (
        'focused orchestration API transport is missing')


def test_normalise_truncates_dangling_query_interpolation():
    """NEUTER-class pin for the api.js:1251 incident: a query-template
    interpolation truncated mid-``${`` must not become a phantom dynamic
    segment. Also pins the shapes that must NOT change."""
    # 事故原形:截断在第二个插值内 → 段尾截断,route identity 精确还原
    assert _normalise(
        '/api/v1/auth-sources/${encodeURIComponent(domain)}/live-session${refresh'
    ) == '/api/v1/auth-sources/<*>/live-session'
    # 悬空插值占满整段 → 段消失
    assert _normalise('/api/foo/${cond') == '/api/foo'
    # 完整插值语义不动
    assert _normalise('/api/x/${id}') == '/api/x/<*>'
    # 混合字面+插值语义不动
    assert _normalise('/api/poll-${id}') == '/api/<*>'
    # Werkzeug 转换器语义不动
    assert _normalise('/api/x/<int:y>') == '/api/x/<*>'


def test_every_api_js_path_resolves_to_a_live_route():
    """Every /api path api.js calls must map to a registered url_map rule.

    This is the automated, factory-aware replacement for the manual grep audit
    — it sees factory-registered routes (register_task_routes) that a decorator
    grep misses, and it covers the WHOLE client, not just /api/v1/*.
    """
    js = _api_surface_source()

    client_paths = _extract_client_paths(js)
    # Drop external-plugin carve-outs (raw-prefix match, pre-normalisation).
    client_paths = {
        p for p in client_paths
        if not any(p.startswith(pref) for pref in _EXTERNAL_PLUGIN_PREFIXES)
    }
    assert client_paths, 'extracted zero /api paths from api.js — regex broke'

    app = _get_app()
    registered = _registered_templates(app)

    normed = {p: _normalise(p) for p in client_paths}
    missing = sorted(p for p, n in normed.items() if n not in registered)

    checked = sorted(client_paths)
    print(f'\n[contract] checked {len(checked)} api.js paths against '
          f'{len(registered)} registered /api rule templates')
    for p in checked:
        print(f'  {"OK " if normed[p] in registered else "MISS"} {p}')

    assert not missing, (
        'api.js calls these /api paths that resolve to NO registered route '
        '(dead client endpoints / typos / removed routes):\n' +
        '\n'.join(f'  {p}  (normalised: {normed[p]})' for p in missing) +
        '\n\nFix the client path OR register/repoint the backend route. '
        '(register_task_routes-style factory routes ARE seen by this test.)'
    )


def test_extractor_sees_every_api_call_site():
    """Coverage cross-check: the extractor must SEE every /api call site.

    ``test_every_api_js_path_resolves_to_a_live_route`` only contract-checks the
    paths ``_extract_client_paths`` recognises — a verb-wrapper call whose FIRST
    ARG is an inline ``/api/...`` string/template literal. A call site that
    builds its path by CONCATENATION or a VARIABLE (e.g.
    ``post(_PREFIX + '/api/v1/ghost', …)``) is invisible to the extractor, so
    its path would silently drop out of the checked set and never be verified —
    turning "184/184 green" into "184 of however-many".

    This makes completeness PROVABLE, not incidental. We scan every verb-wrapper
    call site, extract its FIRST ARGUMENT (depth-aware, so a concatenation is
    seen whole), and split into:
      • ``refs``   — first arg references ``/api/`` ANYWHERE (literal OR concat)
      • ``inline`` — first arg is a parseable inline ``/api`` literal (the ONLY
                     shape the extractor captures)
    Invariant: ``refs == inline``. A new concatenation/variable call site that
    embeds a ``/api`` literal (``BASE + '/api/x'``) bumps ``refs`` but not
    ``inline`` → this test FAILS loudly, instead of the path vanishing from the
    184 unnoticed. (Proven: adding ``post(_PREFIX + '/api/v1/ghost')`` →
    refs=211, inline=210 → fails; removed → 210==210.)

    Honest boundary: a fully-dynamic call site with NO ``/api`` substring at all
    (``get(SOME_BASE + p)``, path assembled entirely from variables) is
    undetectable by ANY static scan and is NOT counted — but that is exactly the
    trading ``request(_TRADING_BASE + path, …)`` carve-out shape, and no in-tree
    domain uses it. If a future domain does, it needs its own contract coverage
    (e.g. enumerate the suffixes), which this test can't infer statically.
    """
    js = _api_surface_source()

    refs, inline = _scan_call_site_coverage(js)

    print(f'\n[coverage] verb-wrapper call sites referencing /api = {refs}; '
          f'of which extractor-parseable inline literals = {inline}')

    assert refs > 0, 'found zero verb-wrapper /api call sites — regex broke'
    assert refs == inline, (
        f'extractor coverage GAP: {refs} verb-wrapper call sites reference /api '
        f'in their first arg but only {inline} are parseable inline literals — '
        f'{refs - inline} call site(s) build their path by concatenation/'
        'variable and are INVISIBLE to the contract check, so their path is '
        'never verified against the url_map. Rewrite those to pass an inline '
        '/api literal, OR extend _extract_client_paths + this scanner to '
        'recognise the new shape. Do NOT let a path silently drop out of the '
        'checked set.'
    )


if __name__ == '__main__':
    _js = _api_surface_source()
    _paths = sorted(_extract_client_paths(_js))
    print(f'Extracted {len(_paths)} /api paths from api.js:')
    for _p in _paths:
        print(' ', _p)
    _refs, _inline = _scan_call_site_coverage(_js)
    print(f'\ncall sites referencing /api = {_refs}; '
          f'extractor-parseable inline = {_inline}; complete = {_refs == _inline}')
