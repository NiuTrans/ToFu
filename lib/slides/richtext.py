"""lib/slides/richtext.py — PPTD rich text → paragraph/run model.

The SINGLE parser both the PPTX exporter and anything else that needs
structured text uses (the HTML renderer passes markup through natively and
does not need it). Parses the PPTD rich-text subset — ``<p>/<span>/<strong>/
<em>/<u>/<s>/<sup>/<sub>/<ul>/<ol>/<li>/<br>`` plus inline ``style`` props
(color/font-size/font-family/background-color, with ``$token`` resolution) —
into ``[Paragraph]`` of ``[Run]``.

Not an HTML parser by trade: unknown tags are unwrapped (their text kept),
never fatal — a model's malformed tag degrades to plain text, not an error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from lib.log import get_logger
from lib.slides.pptd import resolve_color

logger = get_logger(__name__)

__all__ = ['Run', 'Paragraph', 'parse_rich_text']


@dataclass
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    sup: bool = False
    sub: bool = False
    color: str = ''                 # resolved hex or ''
    font_size: float = 0.0          # pt override or 0
    font_family: str = ''
    background: str = ''
    link: str = ''


@dataclass
class Paragraph:
    runs: list = field(default_factory=list)
    align: str = ''                 # left/center/right/justify (or '')
    line_height: float = 0.0        # multiple (0 = inherit)
    line_height_px: float = 0.0
    margin_top: float = 0.0
    margin_left: float = 0.0
    list_kind: str = ''             # 'ul' / 'ol' / ''
    list_index: int = 0             # 1-based within its list


_INLINE_STYLE_RE = re.compile(r'([\w-]+)\s*:\s*([^;]+)')


class _Parser(HTMLParser):
    def __init__(self, theme: dict):
        super().__init__(convert_charrefs=True)
        self.theme = theme
        self.paragraphs: list = []
        self._cur: Paragraph | None = None
        self._stack: list = []      # inherited run flags
        self._list_stack: list = []
        self._list_counter: list = []

    # ── paragraph management ──
    def _para(self) -> Paragraph:
        if self._cur is None:
            self._cur = Paragraph()
        return self._cur

    def _close_para(self):
        if self._cur is not None and self._cur.runs:
            self.paragraphs.append(self._cur)
        self._cur = None

    def _flags(self) -> dict:
        out = {'bold': False, 'italic': False, 'underline': False,
               'strike': False, 'sup': False, 'sub': False, 'color': '',
               'font_size': 0.0, 'font_family': '', 'background': '',
               'link': ''}
        for flags in self._stack:
            for k, v in flags.items():
                if v not in (False, '', 0.0, None):
                    out[k] = v
        return out

    def _style_flags(self, style: str) -> dict:
        out: dict = {}
        for name, value in _INLINE_STYLE_RE.findall(style or ''):
            name, value = name.strip().lower(), value.strip()
            if name == 'color':
                out['color'] = resolve_color(value, self.theme, value)
            elif name == 'background-color':
                out['background'] = resolve_color(value, self.theme, value)
            elif name == 'font-size':
                m = re.match(r'([\d.]+)px', value)
                if m:
                    out['font_size'] = float(m.group(1))
            elif name == 'font-family':
                out['font_family'] = value.strip('\'"')
        return out

    # ── tag handlers ──
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'p':
            self._close_para()
            p = self._para()
            for name, value in _INLINE_STYLE_RE.findall(attrs.get('style', '')):
                name, value = name.strip().lower(), value.strip()
                if name == 'text-align':
                    p.align = value
                elif name == 'line-height':
                    if value.endswith('px'):
                        p.line_height_px = float(value[:-2] or 0)
                    else:
                        try:
                            p.line_height = float(value)
                        except ValueError as e:
                            logger.debug('[Slides] bad line-height %r: %s',
                                         value, e)
                            pass
                elif name == 'margin-top' and value.endswith('px'):
                    p.margin_top = float(value[:-2] or 0)
                elif name == 'margin-left' and value.endswith('px'):
                    p.margin_left = float(value[:-2] or 0)
            return
        if tag == 'br':
            self.handle_data('\n')
            return
        if tag in ('ul', 'ol'):
            self._close_para()
            self._list_stack.append(tag)
            self._list_counter.append(0)
            return
        if tag == 'li':
            self._close_para()
            p = self._para()
            kind = self._list_stack[-1] if self._list_stack else 'ul'
            if kind == 'ol':
                self._list_counter[-1] += 1
                p.list_index = self._list_counter[-1]
            p.list_kind = kind
            for name, value in _INLINE_STYLE_RE.findall(attrs.get('style', '')):
                if name.strip().lower() == 'text-align':
                    p.align = value.strip()
            return
        flags = {}
        if tag in ('strong', 'b'):
            flags['bold'] = True
        elif tag in ('em', 'i'):
            flags['italic'] = True
        elif tag == 'u':
            flags['underline'] = True
        elif tag == 's':
            flags['strike'] = True
        elif tag == 'sup':
            flags['sup'] = True
        elif tag == 'sub':
            flags['sub'] = True
        elif tag == 'a':
            href = attrs.get('href', '')
            if href.startswith(('http://', 'https://', 'mailto:')):
                flags['link'] = href
        elif tag == 'span':
            flags = self._style_flags(attrs.get('style', ''))
        else:
            return                            # unknown tag: unwrap
        self._stack.append(flags)

    def handle_endtag(self, tag):
        if tag == 'p':
            self._close_para()
        elif tag in ('ul', 'ol'):
            self._close_para()
            if self._list_stack:
                self._list_stack.pop()
                self._list_counter.pop()
        elif tag == 'li':
            self._close_para()
        elif tag in ('strong', 'b', 'em', 'i', 'u', 's', 'sup', 'sub',
                     'a', 'span'):
            if self._stack:
                self._stack.pop()

    def handle_data(self, data):
        if not data:
            return
        # Pretty-printed PPTD commonly separates sibling block tags with a
        # newline and indentation, e.g. ``</p>\n    <p>``.  Browsers discard
        # that inter-tag whitespace, but the OOXML path used to open a fresh
        # paragraph for it.  PowerPoint therefore received five paragraphs
        # for a visibly three-line title and expanded the shape past its
        # authored bounds.  Whitespace outside an active paragraph is source
        # formatting, never slide content.  Newlines inside a paragraph stay
        # significant (``<br>`` deliberately arrives here as ``"\n"``).
        if not data.strip() and self._cur is None:
            return
        if not data.strip() and '\n' not in data:
            return
        p = self._para()
        flags = self._flags()
        p.runs.append(Run(text=data, **flags))

    def close(self):
        super().close()
        self._close_para()


def parse_rich_text(text: str, theme: dict) -> list:
    """Rich text string → [Paragraph]. Plain text (no tags) → one <p> per
    line; an empty input yields []."""
    raw = (text or '').strip()
    if not raw:
        return []
    if not re.search(r'</?(?:p|span|strong|em|u|s|sup|sub|a|ul|ol|li|br|b|i)\b',
                     raw):
        return [Paragraph(runs=[Run(text=ln.strip())])
                for ln in raw.split('\n') if ln.strip()]
    parser = _Parser(theme)
    try:
        parser.feed(raw)
        parser.close()
    except Exception as e:
        logger.warning('[Slides] rich text parse degraded (%s) — plain text',
                       e)
        return [Paragraph(runs=[Run(text=re.sub(r'<[^>]+>', '', raw))])]
    return parser.paragraphs
