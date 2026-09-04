#!/usr/bin/env python3
"""Brand-icon fill-integrity guard (bug class: stroke icon solid-filled blob).

The shared CSS rule (styles.css)::

    .stg-brand-icon svg { width: 100%; height: 100%; fill: currentColor; }
    .turn-ctx .tctx-logo .stg-brand-icon svg { ... fill: currentColor; }

is LOAD-BEARING for the ~20 mono FILL icons (claude / openai / gemini / …
carry no fill attribute on their paths, so they need the CSS to inherit the
brand color from the wrapper's inline ``color``). But a CSS declaration of
ANY specificity beats a presentation attribute, and ``fill`` inherits — so a
STROKE-based icon that declares ``fill="none"`` only on its root ``<svg>``
has that "none" overridden to ``currentColor``, and every rect / path that
relied on inheriting it paints SOLID: the icon becomes one dark blob.

Concrete case (2026-08-14): the preset's Codex-subscription row fell back to
the ``generic`` smiley (round-rect + eyes + smile) and rendered as a black
14×14 blob — the rect and the smile path were solid-filled by exactly this
cascade. ``local`` (server stack) had the same latent shape.

The fix follows the pattern ``shubiaobiao`` already established: every
stroke-based icon carries ``fill="none"`` (or its own fill) on EACH shape
child, because a child's own presentation attribute always beats the
inherited value — so the icon renders correctly both under the CSS rule and
without it.

Second bug class (2026-08-31): an icon painted via
``fill="url(#some-gradient)"`` resolves the paint server to the FIRST
matching id in the document — when that instance lives in a display:none
subtree (the closed preset dropdown also renders bedrock badges), browsers
drop the fill and EVERY bedrock badge paints nothing (a 20px blank box, as
seen in the provider-template picker). The registry therefore keeps every
icon free of url(#…)/gradient references; mono paths inherit currentColor.

Third bug class (2026-08-31): a presentation surface references a brand key
the registry never defined (provider-template ``brand: 'groq'`` had no
``groq`` icon — the card silently fell back to the generic smiley). Every
icon key referenced by the retained provider templates
must resolve in the registry.

This test:
  1. loads the REAL branding section from frontend/src/runtime/app-runtime.js
     and asserts every icon whose root svg declares fill="none" has an
     explicit fill on EVERY shape descendant (the blob guard, generic to any
     future stroke icon);
  2. pins the two concrete offenders: generic's round-rect AND smile path
     carry fill="none", its eye circles carry fill="currentColor"; local's
     rects/path too;
  3. pins the CSS side: ``.stg-brand-icon svg`` keeps fill:currentColor (so
     nobody "fixes" the blob by deleting the rule the FILL icons need);
  4. NEUTER — stripping fill="none" from generic's rect must make (1) fail,
     proving the guard has teeth.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

try:
    import tinycss2
except ModuleNotFoundError:
    tinycss2 = None

if tinycss2 is None:
    try:
        from tests._jsdom import frontend_required
    except ImportError:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from _jsdom import frontend_required  # type: ignore
    if frontend_required():
        pytest.fail(
            'TOFU_REQUIRE_FRONTEND=1 but tinycss2 is not installed '
            '(pip install -e ".[test]")', pytrace=False)
    pytest.skip(
        'tinycss2 not installed (pip install -e ".[test]")',
        allow_module_level=True)

from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..'))
CSS_PATH = os.path.join(ROOT, 'static', 'styles.css')
BRAND_ICONS_BUNDLE = native_module_path(
    '.native/model-brand-icons-fill-integrity.js',
    os.path.join(ROOT, 'frontend', 'src', 'core', 'model-brand-icons.ts'),
)

#: shape elements whose painted interior depends on the inherited `fill`
_SHAPES = ('path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon')


def _brand_icons() -> dict[str, str]:
    """Read the typed owner's public immutable registry through its bundle."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not installed')
    script = (
        "const fs=require('fs');"
        "(0,eval)(fs.readFileSync(process.argv[1],'utf8'));"
        "process.stdout.write(JSON.stringify(MODEL_BRAND_ICONS));"
    )
    proc = subprocess.run(
        [node, '-e', script, BRAND_ICONS_BUNDLE],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    icons = json.loads(proc.stdout)
    assert icons, 'typed MODEL_BRAND_ICONS registry is empty'
    return icons


def _root_fill_none(icon: str) -> bool:
    root = icon[:icon.index('>') + 1]
    return 'fill="none"' in root


def _shape_tags(icon: str):
    """Yield (tagname, attrs-string) for every shape element, skipping <defs>."""
    body = icon[icon.index('>') + 1:icon.rindex('</svg>')]
    # strip defs subtrees (gradients) — stops carry fills, not shapes
    body = re.sub(r'<defs\b.*?</defs>', '', body, flags=re.S)
    for shape in _SHAPES:
        for m in re.finditer(r'<%s\b([^>]*)/?>' % shape, body):
            yield shape, m.group(1)


def _blob_offenders(icons: dict) -> list:
    """Icons whose root says fill="none" but some shape has no own fill."""
    offenders = []
    for key, icon in icons.items():
        if not _root_fill_none(icon):
            continue
        for shape, attrs in _shape_tags(icon):
            if 'fill=' not in attrs:
                offenders.append(f'{key}<{shape}>')
    return offenders


def test_stroke_brand_icons_are_fill_self_contained():
    """Invariant: an icon whose root svg declares fill="none" must put an
    explicit fill on EVERY shape child — otherwise the load-bearing CSS
    `fill: currentColor` solid-fills the shapes into one dark blob."""
    icons = _brand_icons()
    stroke_icons = sorted(k for k, v in icons.items() if _root_fill_none(v))
    assert 'generic' in stroke_icons and 'local' in stroke_icons, (
        'expected the two stroke-based icons (generic/local) to be tracked; '
        f'root-fill-none icons found: {stroke_icons}')
    offenders = _blob_offenders(icons)
    assert not offenders, (
        f'stroke-based brand icons with inherited-fill shapes (render as a '
        f'solid blob under `.stg-brand-icon svg{{fill:currentColor}}`): '
        f'{offenders} — add per-child fill="none"/fill="currentColor" '
        f'(shubiaobiao pattern)')


def test_generic_smiley_pins():
    """Concrete offender pins: generic = hollow round-rect + filled eyes +
    stroked smile; local = hollow server rects + filled LEDs + stroked slots."""
    icons = _brand_icons()
    g = icons['generic']
    assert re.search(
        r'<rect\b[^>]*rx="4"[^>]*fill="none"', g), (
        'generic round-rect must carry fill="none" or it paints as a solid '
        'blob (the reported black 14×14 square)')
    assert re.search(
        r'<path d="M8\.5 15\.5c1 1\.5 6 1\.5 7 0"[^>]*fill="none"', g), (
        'generic smile path must carry fill="none" (a filled smile merges '
        'into the blob)')
    assert g.count('fill="currentColor"') >= 2, (
        'generic eye circles keep their own fill="currentColor"')
    l = icons['local']
    assert l.count('fill="none"') >= 3, (
        'local server-stack rects + slot path must each carry fill="none"')
    assert l.count('fill="currentColor"') >= 2, (
        'local LED circles keep their own fill="currentColor"')


def test_css_fill_currentcolor_stays():
    """The CSS rule is load-bearing for the mono FILL icons — it must stay so
    the fill icons keep taking the wrapper's brand color (deleting it is NOT
    the fix for the blob; per-child fills are)."""
    css = open(CSS_PATH, encoding='utf-8').read()
    rules = tinycss2.parse_stylesheet(
        css, skip_whitespace=True, skip_comments=True)

    def _fills(selector):
        want = ' '.join(selector.split())
        for rule in rules:
            if rule.type != 'qualified-rule':
                continue
            if ' '.join(tinycss2.serialize(rule.prelude).split()) != want:
                continue
            return {
                d.lower_name: tinycss2.serialize(d.value).strip()
                for d in tinycss2.parse_declaration_list(
                    rule.content, skip_whitespace=True, skip_comments=True)
                if d.type == 'declaration'}
        return None

    for sel in ('.stg-brand-icon svg',
                '.turn-ctx .tctx-logo .stg-brand-icon svg'):
        decls = _fills(sel)
        assert decls is not None, f'{sel} rule missing from styles.css'
        assert decls.get('fill') == 'currentColor', (
            f'{sel} must keep fill:currentColor — the ~20 mono FILL brand '
            f'icons (claude/openai/…) inherit their brand color from it; '
            f'fixing stroke-icon blobs by deleting this rule would turn '
            f'those icons black')


def test_blob_detector_rejects_missing_child_fill():
    """The invariant checker itself rejects the historical broken shape."""
    icons = _brand_icons()
    bad = icons['generic'].replace(
        '<rect x="3" y="3" width="18" height="18" rx="4" fill="none"/>',
        '<rect x="3" y="3" width="18" height="18" rx="4"/>', 1)
    assert bad != icons['generic'], 'historical broken-shape fixture drifted'
    offenders = _blob_offenders({**icons, 'generic': bad})
    assert any(o.startswith('generic<rect>') for o in offenders), (
        'the blob guard must flag generic<rect> when its fill="none" is '
        'stripped (the original blob shape)')


def test_no_url_paint_server_references():
    """Invariant: no registry icon paints via url(#…)/gradient references —
    a hidden first instance (display:none dropdown) invalidates the paint
    server document-wide and the badge renders as an empty box."""
    offenders = [
        key for key, icon in _brand_icons().items()
        if 'url(#' in icon or 'Gradient' in icon]
    assert not offenders, (
        f'brand icons using cross-SVG paint-server references (invisible '
        f'when the first badge is in a display:none subtree): {offenders} — '
        f'keep icons mono currentColor (see bedrock history 2026-08-31)')


def test_referenced_icon_keys_resolve():
    """Every retained template brand must exist in the icon registry."""
    icons = _brand_icons()
    referenced: dict[str, str] = {}
    templates_src = open(
        os.path.join(ROOT, 'frontend', 'src', 'runtime', 'sections',
                     'settings', 'provider_templates.js'),
        encoding='utf-8').read()
    for key in re.findall(r"brand: '([^']+)'", templates_src):
        referenced.setdefault(key, 'provider_templates.js')
    missing = sorted(k for k in referenced if k not in icons)
    assert not missing, (
        f'icon keys referenced but missing from MODEL_BRAND_ICONS: '
        f'{[(k, referenced[k]) for k in missing]}')


if __name__ == '__main__':
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    rc = 0
    for fn in fns:
        try:
            fn()
            print(f'PASS {fn.__name__}')
        except AssertionError as e:
            rc = 1
            print(f'FAIL {fn.__name__}: {e}')
    sys.exit(rc)
