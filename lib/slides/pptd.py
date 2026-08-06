"""lib/slides/pptd.py — the PPTD subset: model, parser, theme resolution, validator.

PPTD is the YAML slide DSL this capability authors in
(docs/SLIDES_CAPABILITY_DESIGN.md §4.2): a simplified abstraction over OOXML
where every page is self-contained. The format is Moonshot's (spec: the
open-kimi-ppt-skill reference, MIT); this module implements the v1 SUBSET —
text / shape / line / image / icon / table elements, theme tokens
(``$primary`` colors, ``$title`` text styles, ``$default`` table styles),
solid/gradient/image fills, borders, shadows. Charts are deliberately NOT
parsed here (v1 rasterises them upstream).

Design rules:

  * **Parse is total**: a malformed deck raises ``PPTDError`` with a path —
    never a half-model.
  * **Validate is zero-LLM** and returns FINDINGS (strings), mirroring the
    motion gate philosophy: the author's inner loop repairs against them, and
    schema defects never reach the renderer.
  * **Theme resolution is centralised** here (``resolve_color`` /
    ``text_style`` / ``table_style``), so renderer and exporter can never
    disagree about what ``$primary`` meant.
  * Paths are contained: pages/media may not escape the deck directory
    (absolute paths and ``..`` rejected).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['PPTDError', 'Deck', 'Page', 'parse_deck', 'validate_deck',
           'resolve_color', 'text_style', 'table_style', 'resolve_media',
           'DEFAULT_TEXT_STYLE', 'ELEMENT_TYPES', 'SHAPES_KNOWN']

DEFAULT_SIZE = (1280, 720)                    # 16:9, 1px = 1pt
ELEMENT_TYPES = ('text', 'shape', 'line', 'image', 'icon', 'table',
                 'chart')

#: v1 chart element subset (bar/column/line/pie, category data).
CHART_TYPES = ('bar', 'column', 'line', 'pie')

#: Built-in shapes the renderer/exporter know (OOXML preset names; the full
#: 177-name list is the upstream spec's — these are the ones v1 ships).
SHAPES_KNOWN = frozenset({
    'rect', 'roundRect', 'ellipse', 'triangle', 'diamond', 'homePlate',
    'chevron', 'donut', 'star5', 'rightArrow', 'leftArrow', 'upArrow',
    'downArrow', 'leftRightArrow', 'pentagon', 'hexagon', 'parallelogram',
    'trapezoid', 'cross', 'ring', 'heart', 'lightningBolt', 'cloud',
    'bracePair', 'bracketPair', 'wedgeRectCallout', 'wedgeRoundRectCallout',
    'round1Rect', 'round2SameRect', 'custom',
})

DEFAULT_TEXT_STYLE = {
    'color': '#000000', 'fontSize': 18, 'fontFamily': 'MiSans',
    'bold': False, 'italic': False, 'lineHeight': 1.0, 'letterSpacing': 0,
    'marginTop': 0,
}

_HEX_RE = re.compile(r'^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$')
_TOKEN_RE = re.compile(r'^\$([A-Za-z0-9_-]+)$')


class PPTDError(Exception):
    """A malformed PPTD project (parse-time, total failure)."""


@dataclass
class Page:
    path: str                                   # deck-relative, e.g. pages/1.page
    page_type: str = 'content'
    background: dict = field(default_factory=lambda: {
        'type': 'solid', 'color': '#FFFFFF'})
    elements: list = field(default_factory=list)
    notes: str = ''
    raw: dict = field(default_factory=dict)


@dataclass
class Deck:
    title: str
    size: tuple
    theme: dict
    pages: list
    root: str                                   # absolute deck directory
    manifest_path: str = ''

    @property
    def width(self) -> int:
        return int(self.size[0])

    @property
    def height(self) -> int:
        return int(self.size[1])


# ── Parsing ───────────────────────────────────────────────

def _load_yaml(path: str) -> dict:
    try:
        import yaml
    except ImportError as e:                    # pragma: no cover
        raise PPTDError('PyYAML is required for PPTD parsing') from e
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError as e:
        raise PPTDError(f'cannot read {path}: {e}') from e
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PPTDError(f'invalid YAML in {path}: {e}') from e
    if not isinstance(value, dict):
        raise PPTDError(f'expected a YAML mapping in {path}')
    return value


def _safe_rel(root: str, rel: str) -> str:
    """Deck-contained relative path → absolute. Rejects escapes."""
    if not isinstance(rel, str) or not rel.strip():
        raise PPTDError('page/media path must be a non-empty string')
    rel = rel.strip().replace('\\', '/')
    if rel.startswith('/') or re.match(r'^[A-Za-z]:/', rel):
        raise PPTDError(f'absolute paths are not allowed in a deck: {rel}')
    candidate = os.path.realpath(os.path.join(root, rel))
    root_real = os.path.realpath(root)
    if not (candidate == root_real
            or candidate.startswith(root_real + os.sep)):
        raise PPTDError(f'path escapes the deck directory: {rel}')
    return candidate


def parse_deck(manifest_path: str) -> Deck:
    """Parse a ``.pptd`` manifest + all referenced page files into a Deck."""
    manifest_path = os.path.abspath(os.path.expanduser(manifest_path))
    if os.path.isdir(manifest_path):
        found = sorted(
            os.path.join(dp, f)
            for dp, _dn, fn in os.walk(manifest_path) for f in fn
            if f.endswith('.pptd'))
        if len(found) != 1:
            raise PPTDError(
                f'expected exactly one .pptd under {manifest_path}, '
                f'found {len(found)}')
        manifest_path = found[0]
    data = _load_yaml(manifest_path)
    if str(data.get('version')) != 'v2':
        raise PPTDError('deck requires version: v2')
    size = data.get('size') or list(DEFAULT_SIZE)
    if (not isinstance(size, list) or len(size) != 2
            or not all(isinstance(v, (int, float)) and v > 0 for v in size)):
        raise PPTDError(f'size must be [width, height] positive: {size!r}')
    pages_list = data.get('pages')
    if not isinstance(pages_list, list) or not pages_list:
        raise PPTDError('deck must contain a non-empty pages list')

    root = os.path.dirname(manifest_path)
    pages: list = []
    for entry in pages_list:
        page_path = _safe_rel(root, str(entry))
        if not os.path.isfile(page_path):
            raise PPTDError(f'missing page file: {entry}')
        pdata = _load_yaml(page_path)
        elements = pdata.get('elements')
        if not isinstance(elements, list):
            raise PPTDError(f'page elements must be an array: {entry}')
        pages.append(Page(
            path=str(entry),
            page_type=str(pdata.get('pageType') or 'content'),
            background=pdata.get('background')
                       or {'type': 'solid', 'color': '#FFFFFF'},
            elements=elements,
            notes=str(pdata.get('notes') or ''),
            raw=pdata,
        ))
    return Deck(
        title=str(data.get('title')
                  or os.path.splitext(os.path.basename(manifest_path))[0]),
        size=(int(size[0]), int(size[1])),
        theme=data.get('theme') or {},
        pages=pages,
        root=root,
        manifest_path=manifest_path,
    )


def resolve_media(deck: Deck, src: str) -> str:
    """A media reference → an absolute local path or an http(s) URL."""
    if re.match(r'^(?:https?://|data:)', src or ''):
        return src
    return _safe_rel(deck.root, src)


# ── Theme resolution ──────────────────────────────────────

def resolve_color(value, theme: dict, default: str = '') -> str:
    """Resolve a Color field: ``$token`` → theme color; HEX6/HEX8 passthrough."""
    if not isinstance(value, str) or not value:
        return default
    m = _TOKEN_RE.match(value.strip())
    if m:
        colors = (theme or {}).get('colors') or {}
        hit = colors.get(m.group(1))
        if isinstance(hit, str) and hit:
            return hit
        logger.debug('[PPTD] unknown theme color token: %s', value)
        return default
    return value.strip()


def text_style(content: dict, theme: dict) -> dict:
    """Merge a TextContent's effective style (theme style → inline fields)."""
    out = dict(DEFAULT_TEXT_STYLE)
    ref = (content or {}).get('style')
    if isinstance(ref, str):
        m = _TOKEN_RE.match(ref.strip())
        named = ((theme or {}).get('textStyles') or {}).get(
            m.group(1) if m else ref, {})
        if isinstance(named, dict):
            out.update({k: v for k, v in named.items() if v is not None})
    for key in ('color', 'fontSize', 'fontFamily', 'bold', 'italic',
                'backgroundColor', 'lineHeight', 'lineHeightPx',
                'letterSpacing', 'marginTop'):
        if (content or {}).get(key) is not None:
            out[key] = content[key]
    out['color'] = resolve_color(out.get('color'), theme,
                                 DEFAULT_TEXT_STYLE['color'])
    if out.get('backgroundColor'):
        out['backgroundColor'] = resolve_color(out['backgroundColor'], theme)
    return out


def cell_content(cell) -> dict:
    """A table cell's effective content, unifying the two real-world forms.

    The spec form carries ``text``/``align``/style fields FLAT on the cell;
    decks produced by the reference implementation nest them under
    ``content: {text, align, ...}``. Flat fields win on conflict (they are
    the more specific override by convention).
    """
    if not isinstance(cell, dict):
        return {}
    content = cell.get('content')
    if isinstance(content, dict):
        merged = dict(content)
        merged.update({k: v for k, v in cell.items() if k != 'content'})
        return merged
    return dict(cell)


def table_style(style_ref, theme: dict) -> dict:
    """Resolve a Table.style (``$key`` or inline TableStyleConfig), then
    NORMALISE the ad-hoc flat form real decks carry (fontSize/bodyColor/
    headerBold/headerColor/headerFill/firstColumnColor/border) into the
    TableStyleConfig shape the style chain understands."""
    raw = {}
    if isinstance(style_ref, dict):
        raw = style_ref
    elif isinstance(style_ref, str):
        m = _TOKEN_RE.match(style_ref.strip())
        named = ((theme or {}).get('tableStyles') or {}).get(
            m.group(1) if m else style_ref, {})
        raw = named if isinstance(named, dict) else {}
    if not raw:
        return {}
    flat_keys = ('fontSize', 'bodyColor', 'headerBold', 'headerColor',
                 'headerFill', 'firstColumnColor', 'border')
    if not any(k in raw for k in flat_keys):
        return raw
    out: dict = {k: v for k, v in raw.items() if k not in flat_keys}
    cell_style = dict(out.get('cellStyle') or {})
    if raw.get('fontSize') is not None:
        cell_style.setdefault('fontSize', raw['fontSize'])
    if raw.get('bodyColor') is not None:
        cell_style.setdefault('color', raw['bodyColor'])
    if raw.get('border') is not None:
        cell_style.setdefault('border', raw['border'])
    if cell_style:
        out['cellStyle'] = cell_style
    first_row = dict(out.get('firstRowStyle') or {})
    if raw.get('headerBold') is not None:
        first_row.setdefault('bold', bool(raw['headerBold']))
    if raw.get('headerColor') is not None:
        first_row.setdefault('color', raw['headerColor'])
    if raw.get('headerFill') is not None:
        first_row.setdefault('fill', raw['headerFill'])
    if first_row:
        out['firstRowStyle'] = first_row
    if raw.get('firstColumnColor') is not None:
        fc = dict(out.get('firstColumnStyle') or {})
        fc.setdefault('color', raw['firstColumnColor'])
        out['firstColumnStyle'] = fc
    return out


# ── Validation (zero-LLM) ─────────────────────────────────

def _valid_color(v) -> bool:
    return isinstance(v, str) and bool(
        _HEX_RE.match(v.strip()) or _TOKEN_RE.match(v.strip()))


def _check_fill(fill: dict, where: str, out: list) -> None:
    if not isinstance(fill, dict):
        out.append(f'{where}: fill must be an object')
        return
    ftype = fill.get('type')
    if ftype == 'solid':
        if not _valid_color(fill.get('color')):
            out.append(f'{where}: solid fill needs a valid color')
    elif ftype == 'gradient':
        stops = fill.get('stops')
        if not isinstance(stops, list) or len(stops) < 2:
            out.append(f'{where}: gradient needs ≥2 stops')
        else:
            for i, s in enumerate(stops):
                if not isinstance(s, dict) or not _valid_color(s.get('color')):
                    out.append(f'{where}: gradient stop {i} invalid')
                elif not isinstance(s.get('position'), (int, float)):
                    out.append(f'{where}: gradient stop {i} needs position')
    elif ftype == 'image':
        if not fill.get('src'):
            out.append(f'{where}: image fill needs src')
    else:
        out.append(f'{where}: unknown fill type {ftype!r}')


def validate_deck(deck: Deck) -> list:
    """Zero-LLM findings for a parsed deck. Empty = clean.

    These are the errors the author's inner loop repairs against; the renderer
    may assume everything here holds.
    """
    out: list = []
    colors = (deck.theme or {}).get('colors') or {}
    for name, value in colors.items():
        if not _valid_color(value):
            out.append(f'theme.colors.{name}: invalid color {value!r}')

    for pi, page in enumerate(deck.pages, 1):
        where = f'page {pi} ({page.path})'
        _check_fill(page.background, f'{where} background', out)
        seen_ids: set = set()
        for ei, el in enumerate(page.elements):
            ewhere = f'{where} element {ei}'
            if not isinstance(el, dict):
                out.append(f'{ewhere}: not an object')
                continue
            eid = str(el.get('elementId') or '')
            etype = str(el.get('elementType') or '')
            if not eid:
                out.append(f'{ewhere}: missing elementId')
            elif eid in seen_ids:
                out.append(f'{ewhere}: duplicate elementId {eid!r}')
            seen_ids.add(eid)
            if etype not in ELEMENT_TYPES:
                out.append(f'{ewhere} ({eid}): unknown elementType {etype!r}')
                continue
            b = el.get('bounds')
            if (not isinstance(b, list) or len(b) != 4
                    or not all(isinstance(v, (int, float)) for v in b)):
                out.append(f'{ewhere} ({eid}): bounds must be [x,y,w,h] numbers')
                continue
            if b[2] <= 0 or b[3] <= 0:
                out.append(f'{ewhere} ({eid}): bounds w/h must be positive')
            if (b[0] + b[2] < 0 or b[1] + b[3] < 0
                    or b[0] > deck.width or b[1] > deck.height):
                out.append(f'{ewhere} ({eid}): element fully outside the page')

            if etype == 'text':
                content = el.get('content')
                if not isinstance(content, dict):
                    out.append(f'{ewhere} ({eid}): text needs a content object')
                else:
                    if not str(content.get('text') or '').strip():
                        out.append(f'{ewhere} ({eid}): empty text')
                    fs = content.get('fontSize')
                    if fs is not None and (not isinstance(fs, (int, float))
                                           or fs <= 0 or fs > 400):
                        out.append(f'{ewhere} ({eid}): fontSize {fs!r} out of range')
                    ref = content.get('style')
                    if isinstance(ref, str) and ref.startswith('$'):
                        styles = (deck.theme or {}).get('textStyles') or {}
                        if ref[1:] not in styles:
                            out.append(f'{ewhere} ({eid}): unknown textStyle token {ref}')
            elif etype == 'shape':
                name = str(el.get('shapeName') or '')
                if not name:
                    out.append(f'{ewhere} ({eid}): shape needs shapeName')
                elif name not in SHAPES_KNOWN:
                    out.append(f'{ewhere} ({eid}): unsupported shape {name!r} '
                               f'(v1 set: see SHAPES_KNOWN)')
                if name == 'custom' and not (el.get('viewBox') and el.get('path')):
                    out.append(f'{ewhere} ({eid}): custom shape needs viewBox + path')
                if el.get('fill') is not None:
                    _check_fill(el['fill'], f'{ewhere} ({eid}) fill', out)
            elif etype == 'line':
                pts = str(el.get('points') or '').split()
                if len(pts) < 2:
                    out.append(f'{ewhere} ({eid}): line needs ≥2 points')
                if not el.get('viewBox'):
                    out.append(f'{ewhere} ({eid}): line needs viewBox')
            elif etype == 'image':
                src = str(el.get('src') or '')
                if not src:
                    out.append(f'{ewhere} ({eid}): image needs src')
                elif not re.match(r'^(?:https?://|data:)', src):
                    try:
                        local = resolve_media(deck, src)
                        if not os.path.isfile(local):
                            out.append(f'{ewhere} ({eid}): media file missing: {src}')
                    except PPTDError as e:
                        out.append(f'{ewhere} ({eid}): {e}')
            elif etype == 'icon':
                name = str(el.get('iconName') or '')
                if not re.match(r'^(fas|far|fab):[a-z0-9-]+$', name):
                    out.append(f'{ewhere} ({eid}): iconName must be "style:name" '
                               f'(fas:/far:/fab:), got {name!r}')
            elif etype == 'chart':
                ctype = str(el.get('chartType') or '')
                if ctype not in CHART_TYPES:
                    out.append(f'{ewhere} ({eid}): chartType must be one of '
                               f'{CHART_TYPES}, got {ctype!r}')
                data = el.get('data') or {}
                cats = data.get('categories')
                series = data.get('series')
                if not isinstance(cats, list) or not cats:
                    out.append(f'{ewhere} ({eid}): chart needs categories')
                if not isinstance(series, list) or not series:
                    out.append(f'{ewhere} ({eid}): chart needs series')
                else:
                    for si, s in enumerate(series):
                        vals = (s or {}).get('values')
                        if not isinstance(vals, list) or not vals:
                            out.append(f'{ewhere} ({eid}): series {si} needs '
                                       f'values')
                        elif isinstance(cats, list) and len(vals) != len(cats):
                            out.append(f'{ewhere} ({eid}): series {si} has '
                                       f'{len(vals)} values for '
                                       f'{len(cats)} categories')
            elif etype == 'table':
                cw = el.get('columnWidths')
                rh = el.get('rowHeights')
                rows = el.get('rows')
                if not isinstance(rows, list) or not rows:
                    out.append(f'{ewhere} ({eid}): table needs rows')
                    continue
                if (not isinstance(cw, list) or not cw
                        or abs(sum(cw) - 1.0) > 0.02):
                    out.append(f'{ewhere} ({eid}): columnWidths must sum to 1')
                if (not isinstance(rh, list) or len(rh) != len(rows)
                        or abs(sum(rh) - 1.0) > 0.02):
                    out.append(f'{ewhere} ({eid}): rowHeights must match rows '
                               f'and sum to 1')
                sref = el.get('style')
                if isinstance(sref, str) and sref.startswith('$'):
                    tstyles = (deck.theme or {}).get('tableStyles') or {}
                    if sref[1:] not in tstyles:
                        out.append(f'{ewhere} ({eid}): unknown tableStyle token {sref}')
    return out