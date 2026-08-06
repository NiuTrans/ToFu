"""lib/slides/render_html.py — PPTD → HTML renderer (the preview ground truth).

Every page of a deck becomes one self-contained HTML document at the deck's
native geometry (1px = 1pt, same numbers the PPTX exporter uses), rendered by
headless Chrome for previews, visual QA, and chart rasterisation. Because the
SAME mapping feeds the eye and (via the exporter's parallel mapping) the file,
"what you see is what you export" has a single reference implementation.

Mapping notes:

  * rich text is near-native HTML already (``<p>/<span>/<strong>``…); theme
    tokens inside inline ``style="color:$primary"`` are resolved here;
  * shapes are drawn as inline SVG (parametric paths for the built-in set,
    passthrough for ``custom``) — one code path covers fill/gradient/border;
  * image crop+fit maps to CSS ``object-view-box`` + ``object-fit``;
  * fonts are declared via ``@font-face`` pointing at the design_sys asset
    library (absolute file URLs — deterministic, offline, nothing copied).
"""

from __future__ import annotations

import html as _html
import re

from lib.log import get_logger
from lib.slides.pptd import (Deck, Page, resolve_color, resolve_media,
                             table_style, text_style)

logger = get_logger(__name__)

__all__ = ['render_page_html', 'render_deck_html', 'collect_families']

_SAFE_FONT_RE = re.compile(r'^[A-Za-z0-9 _-]+$')


# ── Color / fill helpers ──────────────────────────────────

def _css_color(value, theme: dict, default: str = 'transparent') -> str:
    v = resolve_color(value, theme, default)
    if re.match(r'^#[0-9a-fA-F]{8}$', v):
        r = int(v[1:3], 16)
        g = int(v[3:5], 16)
        b = int(v[5:7], 16)
        a = int(v[7:9], 16) / 255
        return f'rgba({r},{g},{b},{a:.3f})'
    return v


def _gradient_css(fill: dict, theme: dict) -> str:
    stops = fill.get('stops') or []
    parts = ', '.join(
        f'{_css_color(s.get("color"), theme)} '
        f'{max(0.0, min(1.0, float(s.get("position", 0)))) * 100:.1f}%'
        for s in stops if isinstance(s, dict))
    if fill.get('gradientType') == 'radial':
        return f'radial-gradient(circle, {parts})'
    # PPTD angle: 0 = left→right, clockwise. CSS: 0deg = bottom→top.
    angle = (float(fill.get('angle', 0)) + 90.0) % 360.0
    return f'linear-gradient({angle:.1f}deg, {parts})'


def _fill_css(fill: dict, theme: dict, deck: Deck) -> str:
    if not isinstance(fill, dict):
        return ''
    ftype = fill.get('type')
    if ftype == 'solid':
        return _css_color(fill.get('color'), theme)
    if ftype == 'gradient':
        return _gradient_css(fill, theme)
    if ftype == 'image':
        src = resolve_media(deck, str(fill.get('src') or ''))
        return f'url("file://{src}")' if not src.startswith(('http', 'data:')) \
            else f'url("{src}")'
    return ''


def _background_css(fill: dict, theme: dict, deck: Deck) -> str:
    if isinstance(fill, dict) and fill.get('type') == 'image':
        src = resolve_media(deck, str(fill.get('src') or ''))
        url = src if src.startswith(('http', 'data:')) else f'file://{src}'
        mode = (fill.get('fit') or {}).get('mode', 'cover')
        size = {'cover': 'cover', 'contain': 'contain',
                'fill': '100% 100%'}.get(mode, 'cover')
        op = fill.get('opacity', 1)
        extra = f';opacity:{float(op):.3f}' if float(op) < 1 else ''
        return f'background:url("{_html.escape(url, quote=True)}") ' \
               f'center/{size} no-repeat{extra}'
    css = _fill_css(fill, theme, deck)
    return f'background:{css}' if css else ''


def _shadow_css(shadow: dict, theme: dict, *, text: bool = False) -> str:
    if not isinstance(shadow, dict):
        return ''
    blur = float(shadow.get('blur', 0))
    ox, oy = (shadow.get('offset') or [0, 0])[:2]
    color = _css_color(shadow.get('color'), theme, 'rgba(0,0,0,.3)')
    if text:
        return f'text-shadow:{ox}px {oy}px {blur}px {color}'
    return (f'filter:drop-shadow({ox}px {oy}px {blur}px {color})')


# ── Rich text ─────────────────────────────────────────────

_TOKEN_IN_STYLE_RE = re.compile(r'\$([A-Za-z0-9_-]+)')


def _resolve_inline_tokens(text: str, theme: dict) -> str:
    """``$token`` inside inline style attributes → theme colors."""
    def _sub(m):
        return resolve_color(m.group(0), theme, m.group(0))
    return _TOKEN_IN_STYLE_RE.sub(_sub, text or '')


def _rich_html(raw: str, theme: dict) -> str:
    """PPTD rich text → inner HTML. Plain lines become <p>; markup passes."""
    raw = _resolve_inline_tokens(raw or '', theme)
    if re.search(r'</?(?:p|span|strong|em|u|s|sup|sub|a|ul|ol|li)\b', raw):
        return raw
    lines = [ln for ln in raw.split('\n')]
    return ''.join(f'<p>{_html.escape(ln)}</p>' for ln in lines if ln.strip()) \
        or f'<p>{_html.escape(raw.strip())}</p>'


def _text_div(el: dict, deck: Deck, theme: dict) -> str:
    x, y, w, h = [float(v) for v in el['bounds']]
    content = el.get('content') or {}
    st = text_style(content, theme)
    align = content.get('align') or ['left', 'top']
    halign, valign = (list(align) + ['left', 'top'])[:2]
    justify = {'left': 'flex-start', 'center': 'center', 'right': 'flex-end',
               'justify': 'flex-start', 'distributed': 'flex-start'}.get(
                   halign, 'flex-start')
    items = {'top': 'flex-start', 'middle': 'center',
             'bottom': 'flex-end'}.get(valign, 'flex-start')
    text_align = {'left': 'left', 'center': 'center', 'right': 'right',
                  'justify': 'justify',
                  'distributed': 'justify'}.get(halign, 'left')
    fam = st.get('fontFamily') or 'MiSans'
    if isinstance(fam, dict):
        fam = fam.get('ea') or fam.get('latin') or 'MiSans'
    if not _SAFE_FONT_RE.match(str(fam)):
        fam = 'MiSans'
    style = [
        f'font-size:{float(st.get("fontSize") or 18)}px',
        f'color:{_css_color(st.get("color"), theme, "#000")}',
        f"font-family:'{_html.escape(str(fam))}',sans-serif",
        f'font-weight:{700 if st.get("bold") else 400}',
        f'font-style:{"italic" if st.get("italic") else "normal"}',
        f'text-align:{text_align}',
        f'line-height:{st.get("lineHeight") or 1}',
        'width:100%',
    ]
    if st.get('lineHeightPx'):
        style.append(f'line-height:{float(st["lineHeightPx"])}px')
    if st.get('letterSpacing'):
        style.append(f'letter-spacing:{float(st["letterSpacing"])}px')
    if st.get('marginTop'):
        style.append(f'margin-top:{float(st["marginTop"])}px')
    if st.get('backgroundColor'):
        style.append(f'background-color:{_css_color(st["backgroundColor"], theme)}')
    if content.get('gradient'):
        g = _gradient_css(content['gradient'], theme)
        style += [f'background:{g}', '-webkit-background-clip:text',
                  'background-clip:text', 'color:transparent']
    if content.get('shadow'):
        style.append(_shadow_css(content['shadow'], theme, text=True))
    wrap = content.get('wrap', True)
    if not wrap:
        style.append('white-space:nowrap')
    if content.get('textDirection') == 'vertical':
        style.append('writing-mode:vertical-rl')
    outer = [
        f'left:{x}px', f'top:{y}px', f'width:{w}px', f'height:{h}px',
        'display:flex', f'justify-content:{justify}',
        f'align-items:{items}',
    ]
    if el.get('rotation'):
        outer.append(f'transform:rotate({float(el["rotation"])}deg)')
    if el.get('opacity') is not None and float(el['opacity']) < 1:
        outer.append(f'opacity:{float(el["opacity"]):.3f}')
    body = _rich_html(str(content.get('text') or ''), theme)
    return (f'<div class="el text" style="{";".join(outer)}">'
            f'<div style="{";".join(style)}">{body}</div></div>')


# ── Shapes (SVG) ──────────────────────────────────────────

def _shape_path(name: str, w: float, h: float,
                adjustments: list | None = None) -> str:
    """Parametric path for a built-in shape at w×h (SVG user units)."""
    adj = (adjustments or [50000])[0] / 100000.0 if adjustments else 0.5
    if name == 'rect':
        return f'M0,0 H{w} V{h} H0 Z'
    if name == 'roundRect':
        r = min(w, h) * ((adjustments or [16667])[0] / 100000.0
                         if adjustments else 0.16667)
        return (f'M{r},0 H{w - r} Q{w},0 {w},{r} V{h - r} Q{w},{h} '
                f'{w - r},{h} H{r} Q0,{h} 0,{h - r} V{r} Q0,0 {r},0 Z')
    if name == 'ellipse':
        return (f'M{w / 2},0 A{w / 2},{h / 2} 0 1 1 {w / 2 - 0.01},0 Z')
    if name == 'triangle':
        return f'M{w * adj},0 L{w},{h} L0,{h} Z'
    if name == 'diamond':
        return f'M{w / 2},0 L{w},{h / 2} L{w / 2},{h} L0,{h / 2} Z'
    if name == 'homePlate':
        c = w * adj
        return f'M0,0 H{w - c} L{w},{h / 2} L{w - c},{h} H0 Z'
    if name == 'chevron':
        c = w * adj
        return f'M0,0 H{w - c} L{w},{h / 2} L{w - c},{h} H0 L{c},{h / 2} Z'
    if name == 'donut':
        r_out = min(w, h) / 2
        r_in = r_out * (1 - (adjustments or [25000])[0] / 100000.0) \
            if adjustments else r_out * 0.75
        cx, cy = w / 2, h / 2
        return (f'M{cx},{cy - r_out} A{r_out},{r_out} 0 1 1 {cx - 0.01},'
                f'{cy - r_out} Z M{cx},{cy - r_in} A{r_in},{r_in} 0 1 0 '
                f'{cx + 0.01},{cy - r_in} Z')
    if name == 'rightArrow':
        sw = (adjustments or [50000, 50000])[0] / 100000.0
        hl = (adjustments or [50000, 50000])[1] / 100000.0
        shaft = h * sw
        head = w * hl
        y0 = (h - shaft) / 2
        return (f'M0,{y0} H{w - head} V0 L{w},{h / 2} L{w - head},{h} '
                f'V{y0 + shaft} H0 Z')
    if name == 'star5':
        import math
        cx, cy, r = w / 2, h / 2, min(w, h) / 2
        pts = []
        for i in range(10):
            rr = r if i % 2 == 0 else r * 0.382
            a = -math.pi / 2 + i * math.pi / 5
            pts.append(f'{cx + rr * math.cos(a):.2f},{cy + rr * math.sin(a):.2f}')
        return 'M' + ' L'.join(pts) + ' Z'
    # Fallback for the long tail (pentagon/hexagon/heart/cloud/…): the
    # bounding rect — logged by the caller so the gap is visible.
    return f'M0,0 H{w} V{h} H0 Z'


def _svg_fill_defs(fill: dict, theme: dict, deck: Deck, gid: str) -> tuple:
    """(defs_markup, fill_attr) for a shape fill."""
    if not isinstance(fill, dict):
        return '', 'none'
    if fill.get('type') == 'solid':
        return '', _css_color(fill.get('color'), theme)
    if fill.get('type') == 'gradient':
        stops = ''.join(
            f'<stop offset="{max(0.0, min(1.0, float(s.get("position", 0)))) * 100:.1f}%" '
            f'stop-color="{_css_color(s.get("color"), theme)}"/>'
            for s in (fill.get('stops') or []) if isinstance(s, dict))
        import math
        ang = math.radians(float(fill.get('angle', 0)))
        x2, y2 = 0.5 + 0.5 * math.cos(ang), 0.5 + 0.5 * math.sin(ang)
        x1, y1 = 1 - x2, 1 - y2
        if fill.get('gradientType') == 'radial':
            grad = f'<radialGradient id="{gid}">{stops}</radialGradient>'
        else:
            grad = (f'<linearGradient id="{gid}" x1="{x1:.3f}" y1="{y1:.3f}" '
                    f'x2="{x2:.3f}" y2="{y2:.3f}">{stops}</linearGradient>')
        return grad, f'url(#{gid})'
    if fill.get('type') == 'image':
        src = resolve_media(deck, str(fill.get('src') or ''))
        href = src if src.startswith(('http', 'data:')) else f'file://{src}'
        return (f'<pattern id="{gid}" width="100%" height="100%" '
                f'patternContentUnits="objectBoundingBox">'
                f'<image href="{_html.escape(href, quote=True)}" width="1" '
                f'height="1" preserveAspectRatio="xMidYMid slice"/></pattern>',
                f'url(#{gid})')
    return '', 'none'


def _shape_div(el: dict, deck: Deck, theme: dict, idx: int) -> str:
    x, y, w, h = [float(v) for v in el['bounds']]
    name = str(el.get('shapeName') or 'rect')
    if name == 'custom':
        vb = el.get('viewBox') or [w, h]
        path = str(el.get('path') or '')
        view_box = f'0 0 {vb[0]} {vb[1]}'
    else:
        if name not in ('rect', 'roundRect', 'ellipse', 'triangle', 'diamond',
                        'homePlate', 'chevron', 'donut', 'rightArrow', 'star5'):
            logger.info('[Slides] shape %s drawn as rect fallback', name)
        path = _shape_path(name, w, h, el.get('adjustments'))
        view_box = f'0 0 {w} {h}'
    gid = f'g{idx}'
    defs, fill_attr = _svg_fill_defs(el.get('fill'), theme, deck, gid)
    border = el.get('border') or {}
    stroke = ''
    if border:
        dash = {'dash': 'stroke-dasharray="8,5"',
                'dot': 'stroke-dasharray="2,4"'}.get(border.get('style'), '')
        stroke = (f'stroke="{_css_color(border.get("color"), theme, "#000")}" '
                  f'stroke-width="{float(border.get("width", 1))}" {dash}')
    style = [f'left:{x}px', f'top:{y}px', f'width:{w}px', f'height:{h}px']
    if el.get('rotation'):
        style.append(f'transform:rotate({float(el["rotation"])}deg)')
    if el.get('opacity') is not None and float(el['opacity']) < 1:
        style.append(f'opacity:{float(el["opacity"]):.3f}')
    if el.get('shadow'):
        style.append(_shadow_css(el['shadow'], theme))
    return (f'<div class="el shape" style="{";".join(style)}">'
            f'<svg width="{w}" height="{h}" viewBox="{view_box}" '
            f'preserveAspectRatio="none">'
            f'{f"<defs>{defs}</defs>" if defs else ""}'
            f'<path d="{_html.escape(path, quote=True)}" fill="{fill_attr}" '
            f'{stroke}/></svg></div>')


# ── Line ──────────────────────────────────────────────────

def _line_path(points: list, curve: str) -> str:
    if len(points) < 2:
        return ''
    if curve == 'smooth' and len(points) >= 3:
        # Catmull-Rom → cubic bezier through every point.
        d = f'M{points[0][0]},{points[0][1]}'
        for i in range(len(points) - 1):
            p0 = points[i - 1] if i > 0 else points[i]
            p1, p2 = points[i], points[i + 1]
            p3 = points[i + 2] if i + 2 < len(points) else p2
            c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
            c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
            d += (f' C{c1[0]:.2f},{c1[1]:.2f} {c2[0]:.2f},{c2[1]:.2f} '
                  f'{p2[0]:.2f},{p2[1]:.2f}')
        return d
    join = 'L'
    return f'M{points[0][0]},{points[0][1]} ' + ' '.join(
        f'{join}{px},{py}' for px, py in points[1:])


def _line_div(el: dict, deck: Deck, theme: dict, idx: int) -> str:
    x, y, w, h = [float(v) for v in el['bounds']]
    vb = el.get('viewBox') or [w, h]
    pts = []
    for tok in str(el.get('points') or '').split():
        try:
            px, py = tok.split(',')
            pts.append((float(px), float(py)))
        except ValueError:
            continue
    border = el.get('border') or {}
    color = _css_color(border.get('color'), theme, '#000')
    width = float(border.get('width', 1))
    dash = {'dash': 'stroke-dasharray="8,5"',
            'dot': 'stroke-dasharray="2,4"'}.get(border.get('style'), '')
    markers = ''
    attrs = ''
    arrow = el.get('arrow') or [None, None]
    if any(arrow):
        markers = (f'<defs><marker id="ah{idx}" markerWidth="10" '
                   f'markerHeight="10" refX="8" refY="3" orient="auto" '
                   f'markerUnits="strokeWidth">'
                   f'<path d="M0,0 L8,3 L0,6 Z" fill="{color}"/></marker>'
                   f'</defs>')
        if len(arrow) > 0 and arrow[0]:
            attrs += f' marker-start="url(#ah{idx})"'
        if len(arrow) > 1 and arrow[1]:
            attrs += f' marker-end="url(#ah{idx})"'
    d = _line_path(pts, str(el.get('curve') or 'round'))
    join = 'round' if el.get('curve') in (None, 'round', 'smooth') else 'miter'
    style = [f'left:{x}px', f'top:{y}px', f'width:{w}px', f'height:{h}px']
    if el.get('rotation'):
        style.append(f'transform:rotate({float(el["rotation"])}deg)')
    return (f'<div class="el line" style="{";".join(style)}">'
            f'<svg width="{w}" height="{h}" viewBox="0 0 {vb[0]} {vb[1]}" '
            f'preserveAspectRatio="none">{markers}'
            f'<path d="{_html.escape(d, quote=True)}" fill="none" '
            f'stroke="{color}" stroke-width="{width}" {dash} '
            f'stroke-linejoin="{join}" stroke-linecap="round"{attrs}/>'
            f'</svg></div>')


# ── Image ─────────────────────────────────────────────────

def _image_div(el: dict, deck: Deck, theme: dict) -> str:
    x, y, w, h = [float(v) for v in el['bounds']]
    src = resolve_media(deck, str(el.get('src') or ''))
    url = src if src.startswith(('http', 'data:')) else f'file://{src}'
    fit = (el.get('fit') or {}).get('mode', 'cover')
    crop = el.get('crop') or {}
    vb = ''
    if crop:
        t = float(crop.get('top', 0)) * 100
        r = float(crop.get('right', 0)) * 100
        b = float(crop.get('bottom', 0)) * 100
        lf = float(crop.get('left', 0)) * 100
        vb = (f'object-view-box:inset({t:.2f}% {r:.2f}% {b:.2f}% {lf:.2f}%);')
    style = [f'left:{x}px', f'top:{y}px', f'width:{w}px', f'height:{h}px',
             'overflow:hidden']
    if el.get('rotation'):
        style.append(f'transform:rotate({float(el["rotation"])}deg)')
    if el.get('opacity') is not None and float(el['opacity']) < 1:
        style.append(f'opacity:{float(el["opacity"]):.3f}')
    if el.get('shadow'):
        style.append(_shadow_css(el['shadow'], theme))
    border = el.get('border') or {}
    if border:
        style.append(f'border:{float(border.get("width", 1))}px '
                     f'{border.get("style", "solid")} '
                     f'{_css_color(border.get("color"), theme, "#000")}')
    radius = ''
    cs = el.get('cropShape') or {}
    if cs.get('shapeName') == 'roundRect':
        adj = (cs.get('adjustments') or [16667])[0] / 100000.0
        radius = f'border-radius:{min(w, h) * adj:.1f}px;'
    elif cs.get('shapeName') == 'ellipse':
        radius = 'border-radius:50%;'
    img = (f'<img src="{_html.escape(url, quote=True)}" style="width:100%;'
           f'height:100%;object-fit:{fit};{vb}display:block"/>')
    return f'<div class="el image" style="{";".join(style)};{radius}">{img}</div>'


# ── Icon (built-in mini set, v1) ──────────────────────────

#: Minimal FontAwesome-style solid glyph paths (viewBox 448/512 normalised to
#: 512). v1 covers the common set; unknown names render a labelled dot so a
#: bad icon name is VISIBLE rather than blank.
_ICON_PATHS = {
    'fas:lightbulb': 'M272 96c-78.6 0-144 64.5-144 144 0 45.3 21.8 86.7 56 112l0 32c0 8.8 7.2 16 16 16l96 0c8.8 0 16-7.2 16-16l0-32c34.2-25.3 56-66.7 56-112 0-79.5-65.4-144-144-144zM208 416l0 16c0 26.5 21.5 48 48 48s48-21.5 48-48l0-16-96 0z',
    'fas:check': 'M441 103c9.4 9.4 9.4 24.6 0 33.9L209 369c-9.4 9.4-24.6 9.4-33.9 0L7 201c-9.4-9.4-9.4-24.6 0-33.9s24.6-9.4 33.9 0l151 151L407 103c9.4-9.4 24.6-9.4 33.9 0z',
    'fas:star': 'M256 23l70 162 178 14-136 114 40 174-152-100-152 100 40-174L-12 199l178-14z',
    'fas:rocket': 'M224 32c-89 26-152 118-152 224l48 32 24 88 48 32c40 8 80 8 120 0l48-32 24-88 48-32c0-106-63-198-152-224zm32 192a40 40 0 1 0 0-80 40 40 0 0 0 0 80zM96 384l-64 96 96-64z',
    'fas:gear': 'M256 168a88 88 0 1 0 0 176 88 88 0 0 0 0-176zm0-40a128 128 0 1 1 0 256 128 128 0 0 1 0-256zm-24-96h48l8 56 40 16 48-24 24 40-32 40 8 40 56 16v48l-56 16-8 40 32 40-24 40-48-24-40 16-8 56h-48l-8-56-40-16-48 24-24-40 32-40-8-40-56-16v-48l56-16 8-40-32-40 24-40 48 24 40-16z',
    'fas:shield': 'M256 0l192 80v160c0 120-80 224-192 272C144 464 64 360 64 240V80z',
    'fas:users': 'M96 128a64 64 0 1 1 128 0 64 64 0 1 1 -128 0zm-96 256c0-53 43-96 96-96h128c53 0 96 43 96 96v32c0 35-29 64-64 64H96c-35 0-64-29-64-64zm288-256a48 48 0 1 1 96 0 48 48 0 1 1 -96 0z',
    'fas:book': 'M96 32h288c35 0 64 29 64 64v320c0 35-29 64-64 64H96c-35 0-64-29-64-64V96c0-35 29-64 64-64zm32 96v32h192v-32z',
    'fas:arrow-right': 'M438 239c9-9 9-25 0-34L310 77c-9-9-25-9-34 0s-9 25 0 34l71 71H48c-13 0-24 11-24 24s11 24 24 24h299l-71 71c-9 9-9 25 0 34s25 9 34 0l128-128z',
    'fas:chart-line': 'M64 64c0-18-14-32-32-32S0 46 0 64v320c0 53 43 96 96 96h352c18 0 32-14 32-32s-14-32-32-32H96c-18 0-32-14-32-32zm384 54c13-13 13-33 0-46s-33-13-46 0L288 186l-62-62c-13-13-33-13-46 0L76 228c-13 13-13 33 0 46s33 13 46 0l81-81 62 62c13 13 33 13 46 0z',
    'fas:globe': 'M256 0a256 256 0 1 0 0 512 256 256 0 0 0 0-512zm97 384c-29 8-61 12-97 12s-68-4-97-12c-9-32-15-76-15-128s6-96 15-128c29-8 61-12 97-12s68 4 97 12c9 32 15 76 15 128s-6 96-15 128z',
    'fas:heart': 'M256 480S32 360 32 192c0-71 55-128 124-128 44 0 83 23 100 58 17-35 56-58 100-58 69 0 124 57 124 128 0 168-224 288-224 288z',
    'fas:flag': 'M64 32c18 0 32 14 32 32v320c0 18-14 32-32 32S32 402 32 384V64c0-18 14-32 32-32zm64 32h256l-48 96 48 96H128z',
    'fas:lock': 'M144 128v64h160v-64c0-44-36-80-80-80s-80 36-80 80zm-64 64v64c-26 0-48 22-48 48v128c0 26 22 48 48 48h288c26 0 48-22 48-48V304c0-26-22-48-48-48v-64c0-71-57-128-128-128S80 121 80 192z',
    'fas:search': 'M368 208a160 160 0 1 0 -320 0 160 160 0 1 0 320 0zm-29 173l-83 83c-13 13-33 13-46 0s-13-33 0-46l83-83c13-13 33-13 46 0s13 33 0 46z',
}


def _icon_div(el: dict, deck: Deck, theme: dict) -> str:
    x, y, w, h = [float(v) for v in el['bounds']]
    name = str(el.get('iconName') or '')
    path = _ICON_PATHS.get(name)
    if path is None:
        logger.info('[Slides] icon %s not in the built-in set — placeholder',
                    name)
    fill = el.get('fill')
    color = (_css_color(fill.get('color'), theme)
             if isinstance(fill, dict) and fill.get('type') == 'solid'
             else _css_color((fill or {}).get('color'), theme, '#111'))
    style = [f'left:{x}px', f'top:{y}px', f'width:{w}px', f'height:{h}px']
    if el.get('rotation'):
        style.append(f'transform:rotate({float(el["rotation"])}deg)')
    if path is None:
        inner = (f'<div style="width:100%;height:100%;border:2px dashed {color};'
                 f'border-radius:50%;display:flex;align-items:center;'
                 f'justify-content:center;font-size:{min(w, h) * 0.35:.0f}px;'
                 f'color:{color}">?</div>')
        return f'<div class="el icon" style="{";".join(style)}">{inner}</div>'
    return (f'<div class="el icon" style="{";".join(style)}">'
            f'<svg width="{w}" height="{h}" viewBox="0 0 512 512">'
            f'<path d="{path}" fill="{color}"/></svg></div>')


# ── Table ─────────────────────────────────────────────────

def _cell_style(cell: dict, row: int, col: int, rows: int, cols: int,
                tstyle: dict, theme: dict) -> dict:
    """The table style priority chain (spec §1.2)."""
    out = {'color': '#000000', 'fontSize': 14, 'fontFamily': 'MiSans',
           'bold': False, 'align': ['center', 'middle']}
    base = tstyle.get('cellStyle') or {}
    out.update({k: v for k, v in base.items() if v is not None})
    body = tstyle.get('bodyStyles') or []
    is_first_row, is_last_row = row == 0, row == rows - 1
    is_first_col, is_last_col = col == 0, col == cols - 1
    if body and not is_first_row and not is_last_row:
        cyc = body[(row - 1) % len(body)]
        out.update({k: v for k, v in cyc.items() if v is not None})
    row_over = tstyle.get('rowOverColumn', True)
    cat = []
    if is_first_col and tstyle.get('firstColumnStyle'):
        cat.append(tstyle['firstColumnStyle'])
    if is_last_col and tstyle.get('lastColumnStyle'):
        cat.append(tstyle['lastColumnStyle'])
    rows_cat = []
    if is_first_row and tstyle.get('firstRowStyle'):
        rows_cat.append(tstyle['firstRowStyle'])
    if is_last_row and tstyle.get('lastRowStyle'):
        rows_cat.append(tstyle['lastRowStyle'])
    for c in (rows_cat + cat if row_over else cat + rows_cat):
        out.update({k: v for k, v in c.items() if v is not None})
    ref = cell.get('textStyle')
    if isinstance(ref, str) and ref.startswith('$'):
        named = ((theme or {}).get('textStyles') or {}).get(ref[1:], {})
        out.update({k: v for k, v in named.items() if v is not None})
    out.update({k: v for k, v in cell.items()
                if k in ('color', 'fontSize', 'fontFamily', 'bold', 'italic',
                         'lineHeight', 'lineHeightPx', 'letterSpacing',
                         'marginTop', 'fill', 'border', 'align')
                and v is not None})
    return out


def _table_div(el: dict, deck: Deck, theme: dict) -> str:
    x, y, w, h = [float(v) for v in el['bounds']]
    rows = el.get('rows') or []
    n_rows = len(rows)
    n_cols = max((len(r) for r in rows), default=0)
    tstyle = table_style(el.get('style'), theme)
    cw = el.get('columnWidths') or [1.0 / max(1, n_cols)] * max(1, n_cols)
    rh = el.get('rowHeights') or [1.0 / max(1, n_rows)] * max(1, n_rows)

    # Expand merged cells into a grid of (cell, is_placeholder).
    grid: list = [[None] * n_cols for _ in range(n_rows)]
    skip: set = set()
    for ri, row in enumerate(rows):
        ci = 0
        for cell in row:
            while (ri, ci) in skip:
                ci += 1
            if ci >= n_cols:
                break
            grid[ri][ci] = cell
            rs = int(cell.get('rowSpan') or 1)
            cs = int(cell.get('colSpan') or 1)
            for dr in range(rs):
                for dc in range(cs):
                    if dr or dc:
                        skip.add((ri + dr, ci + dc))
            ci += cs

    html_rows = []
    for ri in range(n_rows):
        tds = []
        for ci in range(n_cols):
            if (ri, ci) in skip:
                continue
            cell = grid[ri][ci] or {}
            st = _cell_style(cell, ri, ci, n_rows, n_cols, tstyle, theme)
            css = [
                f'font-size:{float(st.get("fontSize") or 14)}px',
                f'color:{_css_color(st.get("color"), theme, "#000")}',
                f'font-weight:{700 if st.get("bold") else 400}',
                f'text-align:{(st.get("align") or ["center"])[0]}',
                'vertical-align:middle',
                f'padding:{max(2.0, h * float(rh[ri]) * 0.06):.1f}px 8px',
                f'line-height:{st.get("lineHeight") or 1.2}',
            ]
            fill = st.get('fill')
            if isinstance(fill, dict):
                fc = _fill_css(fill, theme, deck)
                if fc:
                    css.append(f'background:{fc}')
            border = st.get('border')
            if border is None:
                css.append('border:1px solid ' +
                           _css_color('$hairline', theme, '#D8D5CE'))
            elif border is not None and border != 'null':
                if isinstance(border, dict):
                    css.append(f'border:{float(border.get("width", 1))}px '
                               f'{border.get("style", "solid")} '
                               f'{_css_color(border.get("color"), theme, "#000")}')
            rs = int(cell.get('rowSpan') or 1)
            cs = int(cell.get('colSpan') or 1)
            span = (f' rowspan="{rs}"' if rs > 1 else '') + \
                   (f' colspan="{cs}"' if cs > 1 else '')
            text = _rich_html(str(cell.get('text') or ''), theme)
            tds.append(f'<td{span} style="{";".join(css)}">{text}</td>')
        html_rows.append(f'<tr>{"".join(tds)}</tr>')
    colgroup = ''.join(f'<col style="width:{float(c) * 100:.2f}%"/>'
                       for c in cw)
    table_bg = _fill_css(el.get('fill'), theme, deck) \
        if isinstance(el.get('fill'), dict) else ''
    style = (f'left:{x}px;top:{y}px;width:{w}px;height:{h}px;')
    return (f'<div class="el table" style="{style}">'
            f'<table style="width:100%;height:100%;border-collapse:collapse;'
            f'table-layout:fixed;{f"background:{table_bg}" if table_bg else ""}">'
            f'<colgroup>{colgroup}</colgroup>{"".join(html_rows)}</table></div>')


# ── Fonts & page assembly ─────────────────────────────────

def collect_families(deck: Deck) -> set:
    """Every fontFamily the deck references (for @font-face staging)."""
    fams: set = set()
    pat = re.compile(r"font-family:\s*'?([A-Za-z0-9 _-]+?)'?\s*(?:;|$)")

    def _add(v):
        if isinstance(v, dict):
            v = v.get('ea') or v.get('latin')
        if isinstance(v, str) and v and _SAFE_FONT_RE.match(v):
            fams.add(v)
    for cfg in ((deck.theme or {}).get('textStyles') or {}).values():
        if isinstance(cfg, dict):
            _add(cfg.get('fontFamily'))
    for page in deck.pages:
        for el in page.elements:
            if not isinstance(el, dict):
                continue
            content = el.get('content') or {}
            _add(content.get('fontFamily'))
            for m in pat.finditer(str(content.get('text') or '')):
                _add(m.group(1))
            if el.get('elementType') == 'table':
                for row in el.get('rows') or []:
                    for cell in row:
                        if isinstance(cell, dict):
                            _add(cell.get('fontFamily'))
    return fams


def _font_face_css(families: set) -> str:
    """@font-face rules for every registry-known family (absolute file URLs).

    Families not in the design_sys registry are left to the host — the QA
    pass sees the result either way, and the renderer never blocks on a font.
    """
    from lib.design_sys import fonts as _fonts
    by_family = {}
    for f in _fonts.FONT_REGISTRY:
        by_family[f.family] = f
        by_family[f.id] = f
    rules = []
    for fam in sorted(families):
        face = by_family.get(fam)
        if face is None:
            continue
        for src in face.sources:
            path = _fonts.ensure_font(face.id, src.weight)
            if not path:
                continue
            rules.append(
                f"@font-face {{ font-family: '{face.family}'; "
                f"src: url('file://{path}') format('{src.fmt}'); "
                f"font-weight: {src.weight}; font-style: normal; }}")
    return '\n'.join(rules)


def render_page_html(deck: Deck, page: Page, *, page_index: int = 0) -> str:
    """One page → a self-contained HTML document at deck geometry."""
    theme = deck.theme or {}
    parts = []
    for i, el in enumerate(page.elements):
        if not isinstance(el, dict):
            continue
        etype = el.get('elementType')
        try:
            if etype == 'text':
                parts.append(_text_div(el, deck, theme))
            elif etype == 'shape':
                parts.append(_shape_div(el, deck, theme, i))
            elif etype == 'line':
                parts.append(_line_div(el, deck, theme, i))
            elif etype == 'image':
                parts.append(_image_div(el, deck, theme))
            elif etype == 'icon':
                parts.append(_icon_div(el, deck, theme))
            elif etype == 'table':
                parts.append(_table_div(el, deck, theme))
        except Exception as e:
            logger.warning('[Slides] page %d element %s render failed: %s',
                           page_index + 1, el.get('elementId'), e)
    bg = _background_css(page.background, theme, deck)
    fonts_css = _font_face_css(collect_families(deck))
    return f'''<!doctype html>
<html><head><meta charset="utf-8"/>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
{fonts_css}
html, body {{ width:{deck.width}px; height:{deck.height}px; overflow:hidden; }}
.page {{ position:relative; width:{deck.width}px; height:{deck.height}px;
  overflow:hidden; {bg} }}
.el {{ position:absolute; }}
p {{ margin:0; }}
ul, ol {{ margin:0; padding-left:1.2em; }}
a {{ color:inherit; }}
</style></head>
<body><div class="page">{''.join(parts)}</div></body></html>'''


def render_deck_html(deck: Deck) -> list:
    """Every page → [(page_index, html)]."""
    return [(i, render_page_html(deck, page, page_index=i))
            for i, page in enumerate(deck.pages)]
