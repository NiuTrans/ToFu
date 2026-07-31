#!/usr/bin/env python3
"""Generate ``static/js/globals.generated.d.ts`` from the real frontend source.

Why generated and not hand-written
----------------------------------
Tofu's frontend is a concatenated bundle with ONE shared global scope
(``lib/js_bundler.py`` joins files with no wrapper), so a module's public
surface is whatever it happens to leave on ``window`` — an implicit contract
that ``tsc --checkJs`` cannot see. The tempting fix is to hand-write
``declare var X: any;`` for each complaint, but that DOWNGRADES THE CONTRACT TO
A COMMENT: the declaration and the code drift the moment a symbol is renamed,
and nothing detects it. Worse, a hand-written ``declare`` silences a genuinely
MISSING symbol just as effectively as it declares a real one, so the tool stops
catching the exact bug class it was installed for.

This generator instead DERIVES the declarations from the source of truth:
  * ``window.X = ...`` / ``global.X = ...`` / ``globalThis.X = ...`` assignments
    (the deliberate export gesture).

Rename a symbol and the generated file changes with it; DELETE a symbol and its
declaration disappears, so every reader of it goes red. That is the property a
hand-written ambient file cannot have.

What is deliberately NOT declared (measured, not assumed)
--------------------------------------------------------
Top-level ``function``/``var``/``const`` declarations in BARE (non-IIFE) files
are already visible to ``tsc`` — a script-scope ``.js`` in the program
contributes its top-level names to the global scope on its own. Declaring them
AGAIN in an ambient ``.d.ts`` does not help; it CONFLICTS. Measured: declaring
all 2451 discovered symbols took the error count from 92 to 452, adding 174
TS2403 (subsequent declarations must have the same type), 162 TS2300 (duplicate
identifier) and 23 TS2451 (redeclare block-scoped variable).

So the rule is: declare a symbol ONLY when ``tsc`` cannot already resolve it —
i.e. it is exported via ``window.X =`` from inside an IIFE (private to that
function scope, public at runtime), or injected from outside ``static/js``
entirely. That keeps this file to the genuine interface surface instead of
restating what the compiler already knows.

Usage
-----
    python3 scripts/gen_frontend_globals.py            # write the file
    python3 scripts/gen_frontend_globals.py --check    # verify it is up to date

``--check`` is what CI runs (tests/test_frontend_globals_generated.py): it
regenerates in memory and fails if the committed file differs, so the
declarations can never silently drift from the code.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
OUT_REL = os.path.join('static', 'js', 'globals.generated.d.ts')
OUT_ABS = os.path.join(ROOT, OUT_REL)

# Content-hashed build outputs — never source. Mirrors the generated-file regex
# in lib/js_bundler.py and the tsconfig.json exclude list.
_GENERATED_RE = re.compile(
    r'^(?:bundle|feature)-[0-9a-f]{8}\.js$|^i18n-(?:zh|en)-[0-9a-f]{8}\.js$'
)

_EXPORT_RE = re.compile(
    r'\b(?:window|global|globalThis)\.([A-Za-z_$][\w$]*)\s*=(?!=)'
)
_TOPLEVEL_DECL_RE = re.compile(
    r'^(?:async\s+)?(?:function\*?|var|let|const|class)\s+([A-Za-z_$][\w$]*)',
    re.M,
)

# Symbols that exist at runtime but are injected from OUTSIDE static/js, so no
# scan of that tree can see them. Each needs a real, checkable justification —
# this is not a dumping ground for "tsc complained".
_EXTERNAL_GLOBALS = {
    '__I18N_PACK_URLS__':
        'injected by routes/common.py:559 as an inline <script> in the page shell',
    '_applyDebugModeVisibility':
        'defined inline in index.html:2068 (loadFeatureFlags block)',
    '_applyTradingVisibility':
        'defined inline in index.html next to _applyDebugModeVisibility; '
        'shows/hides the optional trading entry from the feature flag',
    'TOFU_CONV_WINDOW':
        'optional operator override read at runtime; never assigned in-tree',
    '_CONV_VERIFY_RETRY_DELAYS':
        'test seam: a jsdom harness may shorten the verify backoff via this override',
    '_waitForImageProcessing':
        'defined in the deferred upload bundle; every call site is typeof-guarded',
}


def _js_sources() -> list[str]:
    out = subprocess.check_output(['git', 'ls-files', 'static/js'], text=True, cwd=ROOT)
    files = []
    for rel in out.split():
        if not rel.endswith('.js'):
            continue
        base = os.path.basename(rel)
        if _GENERATED_RE.match(base) or rel.endswith('.nc_copy.js'):
            continue
        files.append(rel)
    return sorted(files)


def _strip_comments(src: str) -> str:
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    return re.sub(r'^\s*//.*$', '', src, flags=re.M)


def _is_iife(src: str) -> bool:
    """True when the file contributes NO names to the shared global scope.

    Detected by EVIDENCE, not by the file's opening bytes: a file is treated as
    fully wrapped only when it has no column-0 ``function``/``var``/``const``
    declaration at all.

    A head-only check ("does the body start with ``(function``?") is wrong here
    and was measured to be wrong: ``main/main_toolbar_ui.js`` OPENS with a small
    ``_installModelCapsFallback`` IIFE and then declares 48 symbols at column 0,
    and ``settings/oauth.js`` does the same with 15. Classifying those as
    wrapped made the generator re-declare names tsc already saw, producing a
    TS6200 "definitions conflict with those in another file" on the whole file.
    Counting the declarations cannot make that mistake.
    """
    return not _TOPLEVEL_DECL_RE.search(src)


_MODULE_MARKER_RE = re.compile(
    r'^\s*(?:module\.exports\b|export\s|import\s)', re.M
)


def _is_module(src: str) -> bool:
    """True when TypeScript treats this file as a MODULE rather than a script.

    A file containing ``module.exports`` / ``export`` / ``import`` has its own
    module scope in tsc's model, so its top-level names do NOT join the global
    scope -- even though ``lib/js_bundler.py`` concatenates it as a plain
    script at runtime, where they DO. That mismatch is invisible until a
    cross-file caller reports TS2304 on a symbol that plainly exists.

    Real case: ``ui/stream_reducer.js`` is the only such file in the tree (it
    carries a ``module.exports`` tail so the node test harnesses can require
    it). Its ``reduceStreamState`` / ``projectColdSnapshot`` are called from
    four other files, all of which reported "Cannot find name" until this
    branch put them back in the ambient surface.
    """
    return bool(_MODULE_MARKER_RE.search(src))


def collect() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Return (window.* exports, bare-script top-level, module-scoped top-level)."""
    exported: dict[str, str] = {}
    toplevel: dict[str, str] = {}
    module_scoped: dict[str, str] = {}
    for rel in _js_sources():
        with open(os.path.join(ROOT, rel), encoding='utf-8', errors='replace') as fh:
            raw = fh.read()
        src = _strip_comments(raw)
        for m in _EXPORT_RE.finditer(src):
            exported.setdefault(m.group(1), rel)
        if _is_iife(src):
            # No column-0 declarations: the file's names are all inside some
            # wrapper, so only its explicit window.* assignments are public.
            continue
        if _is_module(src):
            # Script at runtime, module to tsc -- must be declared explicitly.
            for m in _TOPLEVEL_DECL_RE.finditer(src):
                module_scoped.setdefault(m.group(1), rel)
        else:
            # Plain script: tsc already sees these top-level names.
            for m in _TOPLEVEL_DECL_RE.finditer(src):
                toplevel.setdefault(m.group(1), rel)
    return exported, toplevel, module_scoped


def render() -> str:
    exported, toplevel, module_scoped = collect()
    # A symbol already declared at the top level of a BARE SCRIPT is visible to
    # tsc on its own (script-scope files contribute their top-level names to
    # the global scope). Re-declaring it here does not help and actively
    # CONFLICTS -- measured: declaring all of them took 92 errors to 452, of
    # which 359 were duplicate-identifier/redeclaration noise.
    #
    # So keep ONLY the symbols tsc cannot otherwise see:
    #   * exported with `window.X =` from inside an IIFE, and
    #   * top-level names of a MODULE-scoped file (module.exports present),
    #     which the bundler nonetheless concatenates as a plain script.
    names = {n: rel for n, rel in exported.items() if n not in toplevel}
    for n, rel in module_scoped.items():
        if n not in toplevel:
            names.setdefault(n, rel)

    lines = [
        '/* AUTO-GENERATED by scripts/gen_frontend_globals.py — DO NOT EDIT.',
        ' *',
        ' * Ambient declarations for the frontend\'s shared global scope. The bundle',
        ' * (lib/js_bundler.py) concatenates every file into ONE scope with no module',
        ' * wrapper, so these symbols really are globals at runtime; without this file',
        ' * `tsc --checkJs` reports each cross-file reference as an undefined name and',
        ' * the real bugs drown in the noise.',
        ' *',
        ' * DERIVED FROM THE SOURCE, never hand-maintained: rename or delete a symbol',
        ' * and this file changes with it, so a stale declaration cannot outlive its',
        ' * definition. Regenerate with:',
        ' *',
        ' *     python3 scripts/gen_frontend_globals.py',
        ' *',
        ' * tests/test_frontend_globals_generated.py runs --check in CI.',
        ' *',
        ' * Types are intentionally `any`: the goal is SYMBOL RESOLUTION (does this',
        ' * name exist?), not describing the shapes. Precise types belong in JSDoc on',
        ' * the definitions themselves.',
        ' */',
        '',
        '// ── Symbols exported via `window.X =` from inside an IIFE ──',
        '// Private to their function scope (so tsc cannot see them) but public',
        '// on the shared global object at runtime. Top-level names in BARE files',
        '// are deliberately NOT listed: tsc already resolves those, and',
        '// re-declaring them produces duplicate-identifier errors.',
    ]
    for name in sorted(names):
        lines.append(f'declare var {name}: any;   // {names[name]}')

    lines += [
        '',
        '// ── Runtime globals injected from OUTSIDE static/js ──',
        '// Not discoverable by scanning that tree, so each is listed explicitly',
        '// WITH the reason it is legitimately absent.',
    ]
    for name in sorted(_EXTERNAL_GLOBALS):
        if name in names:
            continue  # now defined in-tree; the scan already covers it
        lines.append(f'declare var {name}: any;   // {_EXTERNAL_GLOBALS[name]}')

    lines.append('')
    # ── Window interface augmentation ──────────────────────────────────
    # `declare var X` fixes the BARE reference `X`, but not the PROPERTY
    # access `window.X` -- TS checks that against the Window interface, which
    # knows nothing about our expandos. Both spellings appear in the codebase
    # (often for the same symbol: a bare top-level `const X` re-exported as
    # `window.X = X` so late/deferred bundles can find it), so the ambient
    # surface has to cover both or half the references still error.
    #
    # This block is derived from the SAME scan, so it cannot drift from the
    # `declare var` list above.
    lines += [
        '// ── window.<name> property access ──',
        '// `declare var X` covers the bare reference; this covers `window.X`.',
        '// Both spellings are in use, frequently for the same symbol.',
        'interface Window {',
    ]
    for name in sorted(exported):
        lines.append(f'  {name}: any;')
    for name in sorted(_EXTERNAL_GLOBALS):
        if name not in exported:
            lines.append(f'  {name}: any;')
    lines.append('}')
    lines.append('')
    return '\n'.join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='exit non-zero if the committed file is out of date')
    args = ap.parse_args()

    content = render()
    if args.check:
        if not os.path.exists(OUT_ABS):
            print(f'MISSING: {OUT_REL} — run: python3 scripts/gen_frontend_globals.py')
            return 1
        with open(OUT_ABS, encoding='utf-8') as fh:
            current = fh.read()
        if current != content:
            print(f'STALE: {OUT_REL} does not match the source.\n'
                  f'Regenerate with: python3 scripts/gen_frontend_globals.py')
            return 1
        print(f'OK: {OUT_REL} is up to date '
              f'({content.count("declare var")} symbols)')
        return 0

    with open(OUT_ABS, 'w', encoding='utf-8') as fh:
        fh.write(content)
    print(f'Wrote {OUT_REL} ({content.count("declare var")} symbols)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
