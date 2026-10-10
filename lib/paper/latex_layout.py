"""lib/paper/latex_layout.py — Local LaTeX compile + page-layout awareness.

Responsibility: compile a local LaTeX working directory with the repo-local
TinyTeX toolchain (``.texlive/`` at the repository root) and report *where*
content lands in the rendered PDF — total page count, per-section page map,
physical page height (MediaBox), text-body bottom (geometry dump), and a
last-page fullness estimate. This is the local replacement for overleaf-mcp's
``compile_project`` / ``get_page_count`` / ``section_page_map`` tools, which
require an Overleaf session (and, for edits, a paid plan). The offline
SyncTeX decoding and geometry/MediaBox parsing are ported from
``overleaf-mcp`` (MIT) ``src/overleaf_mcp/layout.py`` — only the offline
subset; all Overleaf HTTP plumbing is dropped.

Entry points:
  * ``compile_and_report(workdir, ...)`` — one-shot: compile + full report dict
  * ``compile_project(workdir, ...)``      — latexmk wrapper only
  * ``page_count(workdir)``                — log → PDF-regex count
  * ``section_page_map(workdir, ...)``     — sections → PDF pages + fullness
  * CLI: ``python3 -m lib.paper.latex_layout <workdir> [--engine xelatex]
    [--no-compile]`` — prints the human-readable map

Dependencies: the TinyTeX tree at ``<repo>/.texlive`` (binaries under
``bin/x86_64-linux``, optional ``syslib/`` for libfontconfig on hosts that
lack it); falls back to whatever ``latexmk`` is on PATH when the repo tree is
absent. No required Python package dependencies; ``pymupdf`` is used when
importable to measure the last page's *visible* content bottom from the PDF
itself (SyncTeX streams carry phantom boxes — output-routine containers and
empty kern-only hboxes, notably under xelatex — that no parse-level filter
fully separates from real content). Without it the SyncTeX leaf-box estimate
is used and ``end.bottom_source`` says so.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import json
import os
import re
import subprocess
from pathlib import Path

from lib.log import get_logger

logger = get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEXROOT = _REPO_ROOT / '.texlive'

_COMPILE_TIMEOUT_S = 600


# Windows/macOS font names that CJK/Latin academic templates request by
# name (ctex fontset=windows, \setmainfont{Times New Roman}), mapped to the
# metric-compatible clones bundled in TeX Live. Append mode: the requested
# family stays primary, the clone is only a fallback when the original is
# absent — output is unchanged on hosts that have the real fonts.
_FONT_FALLBACKS = {
    'Times New Roman': 'TeX Gyre Termes',
    'Arial': 'TeX Gyre Heros',
    'Helvetica': 'TeX Gyre Heros',
    'Courier New': 'TeX Gyre Cursor',
    'SimSun': 'FandolSong',
    'NSimSun': 'FandolSong',
    'SimHei': 'FandolHei',
    'SimKai': 'FandolKai',
    'KaiTi': 'FandolKai',
    'SimFang': 'FandolFang',
    'FangSong': 'FandolFang',
    'Microsoft YaHei': 'FandolHei',
}


def _fonts_conf_text(workdir: Path, cachedir: Path,
                     base_conf: str | None = None, *, texroot: Path | None = None) -> str:
    from xml.sax.saxutils import escape
    base = base_conf or '/etc/fonts/fonts.conf'
    parts = [
        '<?xml version="1.0"?>',
        '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">',
        '<fontconfig>',
        f'  <include ignore_missing="yes">{escape(base)}</include>',
        f'  <dir>{escape(str(workdir))}</dir>',
    ]
    # TeX Live's bundled opentype fonts (TeX Gyre clones, Fandol CJK, Latin
    # Modern) — invisible to fontconfig unless this dir is registered.
    selected_root = texroot if texroot is not None else _TEXROOT
    tl_otf = selected_root / 'texmf-dist' / 'fonts' / 'opentype'
    if tl_otf.is_dir():
        parts.append(f'  <dir>{escape(str(tl_otf))}</dir>')
    # Renamed TeX Gyre clones whose name tables carry the Windows family
    # names verbatim — XeTeX rejects fontconfig family substitutions, so
    # aliases alone can never satisfy \setmainfont{Times New Roman}.
    renamed = selected_root / 'fonts'
    if renamed.is_dir():
        parts.append(f'  <dir>{escape(str(renamed))}</dir>')
    for want, fallback in _FONT_FALLBACKS.items():
        parts.append(
            '  <match target="pattern">'
            f'<test qual="any" name="family"><string>{escape(want)}</string></test>'
            f'<edit name="family" mode="append">'
            f'<string>{escape(fallback)}</string></edit></match>')
    parts.append(f'  <cachedir>{escape(str(cachedir))}</cachedir>')
    parts.append('</fontconfig>')
    return '\n'.join(parts) + '\n'


def _tex_env(workdir: Path | None = None) -> dict:
    """Environment with the repo-local TinyTeX toolchain on PATH.

    ``$HOME`` is read-only in this deployment, so fontconfig has no writable
    cachedir and cannot see fonts bundled inside a project (e.g. ctex
    ``fontset=windows`` looking up SimSun by name). Generate a repo-local
    ``fonts.conf`` that *augments* the inherited configuration — it
    includes the caller's ``FONTCONFIG_FILE`` when set (the conda
    activation exports one here), else the system ``/etc/fonts/fonts.conf`` —
    adds the project directory, and redirects the cache under
    ``.texlive/fontconfig/``.
    """
    env = dict(os.environ)
    texbin = _TEXROOT / 'bin' / 'x86_64-linux'
    if texbin.is_dir():
        env['PATH'] = f"{texbin}{os.pathsep}{env.get('PATH', '')}"
        syslib = _TEXROOT / 'syslib'
        if syslib.is_dir():
            prev = env.get('LD_LIBRARY_PATH')
            env['LD_LIBRARY_PATH'] = str(syslib) + (os.pathsep + prev if prev else '')
    if workdir is not None:
        conf_dir = _TEXROOT / 'fontconfig'
        try:
            (conf_dir / 'cache').mkdir(parents=True, exist_ok=True)
            fonts_conf = conf_dir / 'fonts.conf'
            # absolute: the compile subprocess runs with cwd=workdir, so a
            # relative <dir> would resolve against the wrong base.
            fonts_conf.write_text(
                _fonts_conf_text(Path(workdir).resolve(), conf_dir / 'cache',
                                 base_conf=env.get('FONTCONFIG_FILE')),
                encoding='utf-8')
        except OSError:
            logger.warning('could not write repo-local fonts.conf under %s', conf_dir)
        else:
            env['FONTCONFIG_FILE'] = str(fonts_conf)
    return env


def find_compile_root(workdir: str | os.PathLike) -> Path:
    """Locate the root ``.tex`` (the one holding ``\\documentclass``).

    Prefers ``main.tex``-style names when several candidates exist.
    Raises FileNotFoundError when no candidate qualifies.
    """
    wd = Path(workdir)
    candidates = []
    for tex in sorted(wd.glob('*.tex')):
        try:
            if '\\documentclass' in tex.read_text(encoding='utf-8', errors='replace'):
                candidates.append(tex)
        except OSError:
            continue
    if not candidates:
        raise FileNotFoundError(f'no .tex with \\documentclass under {wd}')
    for preferred in ('main.tex', 'Main.tex', 'paper.tex', 'Thesis.tex'):
        for c in candidates:
            if c.name == preferred:
                return c
    return candidates[0]


# ── Compile ───────────────────────────────────────────────────────────

_ERROR_LINE_RE = re.compile(r'^! ', re.MULTILINE)


def _extract_errors(log_text: str, limit: int = 10) -> list[str]:
    """First ``! `` error blocks (line + 2 context lines) from a LaTeX log."""
    lines = log_text.split('\n')
    out = []
    for i, line in enumerate(lines):
        if line.startswith('! '):
            out.append('\n'.join(lines[i:i + 3]))
            if len(out) >= limit:
                break
    return out


def compile_project(workdir: str | os.PathLike, engine: str = 'pdflatex',
                    timeout_s: int = _COMPILE_TIMEOUT_S) -> dict:
    """Run latexmk on the compile root. Returns a status dict.

    ``engine`` is ``'pdflatex'`` or ``'xelatex'``. Synctex is always on —
    the layout map depends on it.
    """
    wd = Path(workdir)
    root = find_compile_root(wd)
    flag = '-xelatex' if engine == 'xelatex' else '-pdf'
    # -g: force at least one full run. latexmk's fdb records a prior failure
    # and otherwise refuses to retry — which also leaves the .log stale,
    # hiding newly-installed packages from error diagnosis.
    cmd = ['latexmk', '-g', flag, '-interaction=nonstopmode', '-synctex=1', root.name]
    try:
        proc = subprocess.run(
            cmd, cwd=wd, env=_tex_env(wd), timeout=timeout_s,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    except subprocess.TimeoutExpired:
        return {'status': 'timeout', 'root': root.name, 'errors': [],
                'timeout_s': timeout_s}
    except FileNotFoundError:
        return {'status': 'no-toolchain', 'root': root.name,
                'errors': ['latexmk not found (repo .texlive missing and not on PATH)']}
    stem = root.stem
    log_text = ''
    log_path = wd / f'{stem}.log'
    if log_path.is_file():
        log_text = log_path.read_text(encoding='utf-8', errors='replace')
    errors = _extract_errors(log_text)
    pdf = wd / f'{stem}.pdf'
    if proc.returncode == 0 and pdf.is_file():
        status = 'success'
    elif pdf.is_file() and errors:
        status = 'errors'
    else:
        status = 'failure'
    return {
        'status': status,
        'root': root.name,
        'engine': engine,
        'errors': errors,
        'pdf': str(pdf) if pdf.is_file() else None,
        'log': str(log_path) if log_path.is_file() else None,
        'synctex': str(wd / f'{stem}.synctex.gz')
        if (wd / f'{stem}.synctex.gz').is_file() else None,
    }


# ── Page count / geometry / MediaBox (ported from overleaf-mcp, offline) ──

_PAGES_RE = re.compile(r"Output written on\s+\S+\s+\((\d+)\s+pages?", re.IGNORECASE)

_TEX_PT_TO_BP = 72.0 / 72.27
_ONE_INCH_PT = 72.27
_ASSUMED_LETTER_HEIGHT_PT = 792.0

_GEOMETRY_FIELD_RE = re.compile(r"^\*\s*\\([A-Za-z]+)\s*=\s*(-?[\d.]+)pt", re.MULTILINE)


def _pages_from_log_text(log_text: str | None) -> int | None:
    if not log_text:
        return None
    m = _PAGES_RE.search(log_text)
    return int(m.group(1)) if m else None


def _pages_from_pdf_bytes(data: bytes) -> int | None:
    count = len(re.findall(rb"/Type\s*/Page(?![s])", data))
    return count or None


def _parse_geometry_fields(log_text: str | None) -> dict[str, float]:
    """``* \\field=NNNpt`` layout block the geometry package writes to the log."""
    if not log_text:
        return {}
    out: dict[str, float] = {}
    for name, val in _GEOMETRY_FIELD_RE.findall(log_text):
        try:
            out[name] = float(val)
        except (ValueError, TypeError):
            continue
    return out


def _text_block_bottom_bp(fields: dict[str, float]) -> float | None:
    """Text-body bottom edge (from page top, in bp) from geometry fields.

    text_top = 1in + voffset + topmargin + headheight + headsep; the body then
    spans textheight. Converted pt→bp to match SyncTeX/MediaBox coordinates.
    """
    required = ('textheight', 'topmargin', 'headheight', 'headsep')
    if not all(k in fields for k in required):
        return None
    top_pt = (
        _ONE_INCH_PT
        + fields.get('voffset', 0.0)
        + fields['topmargin']
        + fields['headheight']
        + fields['headsep']
    )
    return (top_pt + fields['textheight']) * _TEX_PT_TO_BP


_MEDIABOX_RE = re.compile(
    rb"/MediaBox\s*\[\s*([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\s*\]")

_FLATE_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\n?endstream", re.DOTALL)


def _mediabox_height_in(chunk: bytes) -> float | None:
    m = _MEDIABOX_RE.search(chunk)
    if not m:
        return None
    try:
        height = float(m.group(4)) - float(m.group(2))
    except (ValueError, TypeError):
        return None
    return height if height > 0 else None


def _parse_mediabox_height(pdf_bytes: bytes) -> float | None:
    """Page height (PostScript points) from the first ``/MediaBox``.

    Modern pdflatex defaults ``\\pdfobjcompresslevel > 0``, so page objects
    (and their MediaBox) sit inside FlateDecode object streams — raw-byte
    search alone misses them. Raw first, then each zlib-decompressible
    stream; fail-closed to None.
    """
    height = _mediabox_height_in(pdf_bytes)
    if height is not None:
        return height
    import zlib
    for m in _FLATE_STREAM_RE.finditer(pdf_bytes):
        try:
            chunk = zlib.decompress(m.group(1))
        except (zlib.error, ValueError):
            continue
        height = _mediabox_height_in(chunk)
        if height is not None:
            return height
    return None


def _read(path: Path | None, binary: bool = False):
    if path is None or not path.is_file():
        return None
    data = path.read_bytes()
    return data if binary else data.decode('utf-8', errors='replace')


def _text_block_top_bp(fields: dict[str, float]) -> float | None:
    """Text-body TOP edge (from page top, in bp) from geometry fields."""
    required = ('topmargin', 'headheight', 'headsep')
    if not all(k in fields for k in required):
        return None
    top_pt = (
        _ONE_INCH_PT
        + fields.get('voffset', 0.0)
        + fields['topmargin']
        + fields['headheight']
        + fields['headsep']
    )
    return top_pt * _TEX_PT_TO_BP


_MAX_DRAWINGS_SCANNED = 2000


def _content_bottom_from_pdf(pdf_path: Path | None, page_no: int | None,
                             text_bottom: float | None,
                             text_top: float | None) -> float | None:
    """Deepest VISIBLE content bottom (bp from top) on ``page_no``.

    pymupdf ground truth: text/image blocks plus vector drawings, footer
    excluded via the geometry text-bottom when known. Returns ``text_top``
    for a page whose body is empty (0% full), None when undeterminable
    (pymupdf absent, page out of range, unreadable) so callers fail back
    to the SyncTeX estimate.
    """
    if pdf_path is None or not pdf_path.is_file() or not page_no:
        return None
    try:
        import pymupdf
    except ImportError:
        return None
    try:
        with pymupdf.open(pdf_path) as doc:
            if page_no < 1 or page_no > len(doc):
                return None
            page = doc[page_no - 1]
            ys = [b[3] for b in page.get_text('blocks')]
            for d in page.get_drawings()[:_MAX_DRAWINGS_SCANNED]:
                ys.append(d['rect'].y1)
    except Exception as exc:
        logger.debug('pdf block scan failed for %s page %s: %s',
                     pdf_path, page_no, type(exc).__name__)
        return None
    if text_bottom is not None:
        body = [y for y in ys if y <= text_bottom + _BODY_BOTTOM_EPS_BP]
        if body:
            return max(body)
        return text_top  # body provably empty (footer-only page)
    return max(ys) if ys else None


def page_count(workdir: str | os.PathLike, root: str | None = None) -> dict:
    """Total page count from the build log (PDF-regex fallback)."""
    wd = Path(workdir)
    stem = Path(root).stem if root else find_compile_root(wd).stem
    pages = _pages_from_log_text(_read(wd / f'{stem}.log'))
    source = 'log'
    if pages is None:
        data = _read(wd / f'{stem}.pdf', binary=True)
        pages = _pages_from_pdf_bytes(data) if data else None
        source = 'pdf'
    return {'pages': pages, 'source': source if pages is not None else None}


# ── SyncTeX offline engine (ported from overleaf-mcp layout.py) ────────

# sp → bp: (72/72.27)/65536. Reproduces the live endpoint to < 0.001 bp.
_SP_TO_BP = (72.0 / 72.27) / 65536.0

# TYPE TAG,LINE:H,V:WIDTH,HEIGHT,DEPTH  with TYPE ∈ ``( [ h v``.
_SYNCTEX_BOX_RE = re.compile(
    r"^[\(\[hv](\d+),(\d+):(-?\d+),(-?\d+):(-?\d+),(-?\d+),(-?\d+)"
)

# Slack (bp) for a box to still count as "within the text body" vs the footer.
_BODY_BOTTOM_EPS_BP = 3.0


def parse_synctex(raw: str) -> dict:
    """Decode a raw (decompressed) ``.synctex`` body into a queryable map.

    Returns ``{inputs, by_line, hbox_last_by_line, lines_by_tag,
    page_bottoms, page_box_vs, max_page}`` — ``by_line`` holds the FIRST
    box per source line (what forward-sync returns); ``hbox_last_by_line``
    holds the LAST hbox record per source line. TeX tags every hbox of a
    paragraph with the line current at paragraph *end*, so when a heading
    directly follows a paragraph with no blank line, the previous
    paragraph's boxes and the heading's boxes share one source line — the
    heading is the LAST hbox, not the first. ``lines_by_tag`` enables
    nearest-line fallback; ``page_bottoms``/``page_box_vs`` feed last-page
    fullness and exclude ``[``-opened container vboxes: the output
    routine's body container always reports V+depth at the text-block
    bottom (and the footer container below it) regardless of how much
    content the page holds, which would pin fullness at ~100% on every
    page.
    """
    inputs: dict[int, str] = {}
    by_line: dict[tuple[int, int], dict] = {}
    hbox_last_by_line: dict[tuple[int, int], dict] = {}
    lines_by_tag: dict[int, list[int]] = {}
    page_bottoms: dict[int, float] = {}
    page_box_vs: dict[int, list[float]] = {}
    page = None
    max_page = 0

    for line in raw.split('\n'):
        if not line:
            continue
        c = line[0]
        if c == 'I':
            mm = re.match(r"Input:(\d+):(.+)$", line)
            if mm:
                inputs[int(mm.group(1))] = mm.group(2).strip()
            continue
        if c == '{':
            num = line[1:].strip()
            if num.isdigit():
                page = int(num)
                max_page = max(max_page, page)
            continue
        if c == '}':
            page = None
            continue
        if page is None:
            continue
        m = _SYNCTEX_BOX_RE.match(line)
        if not m:
            continue
        tag, ln, h_sp, v_sp, w_sp, ht_sp, d_sp = (int(x) for x in m.groups())
        v_bp = (v_sp + d_sp) * _SP_TO_BP
        key = (tag, ln)
        rec = {
            'page': page,
            'v': v_bp,
            'h': h_sp * _SP_TO_BP,
            'width': w_sp * _SP_TO_BP,
            'height': (ht_sp + d_sp) * _SP_TO_BP,
        }
        if key not in by_line:
            by_line[key] = rec
            lines_by_tag.setdefault(tag, []).append(ln)
        if c in ('(', 'h'):
            hbox_last_by_line[key] = rec
        if c != '[':
            if v_bp > page_bottoms.get(page, float('-inf')):
                page_bottoms[page] = v_bp
            page_box_vs.setdefault(page, []).append(v_bp)

    for tag in lines_by_tag:
        lines_by_tag[tag].sort()

    return {
        'inputs': inputs,
        'by_line': by_line,
        'hbox_last_by_line': hbox_last_by_line,
        'lines_by_tag': lines_by_tag,
        'page_bottoms': page_bottoms,
        'page_box_vs': page_box_vs,
        'max_page': max_page,
    }


def _body_bottom_on_page(parsed: dict, page: int, text_bottom: float | None) -> float | None:
    """Deepest content baseline on ``page`` within the text body (footer excluded)."""
    vs = parsed.get('page_box_vs', {}).get(page)
    if not vs:
        return parsed.get('page_bottoms', {}).get(page)
    if text_bottom is None:
        return max(vs)
    body = [v for v in vs if v <= text_bottom + _BODY_BOTTOM_EPS_BP]
    return max(body) if body else None


def _synctex_tags_for_label(parsed: dict, label: str) -> list[int]:
    """Input tag(s) whose recorded path matches ``label`` (``/./``-normalised)."""
    def norm(s: str) -> str:
        return s.replace('/./', '/').lstrip('./')

    target = norm(label)
    tags = [t for t, p in parsed['inputs'].items() if norm(p) == target]
    if not tags:
        base = target.rsplit('/', 1)[-1]
        tags = [t for t, p in parsed['inputs'].items()
                if norm(p).rsplit('/', 1)[-1] == base]
    return tags


def _box_for_line(parsed: dict, tag: int, line: int) -> dict | None:
    """Box representing a source line for heading/forward-sync purposes.

    Prefers the LAST hbox record: a section heading directly after a
    paragraph (no blank line) shares the paragraph's SyncTeX line tag, and
    the heading's boxes come after the preceding paragraph's boxes.
    """
    return (parsed.get('hbox_last_by_line', {}).get((tag, line))
            or parsed['by_line'].get((tag, line)))


def _page_for_line(parsed: dict, tags: list[int], line: int) -> int | None:
    """Page for a source line: exact box first, then nearest boxed line.

    Ties prefer the LATER line (content following a heading) — mirrors
    SyncTeX forward-search. None only when the tag has no boxes at all.
    """
    for t in tags:
        rec = _box_for_line(parsed, t, line)
        if rec:
            return rec['page']
    for t in tags:
        boxed = parsed.get('lines_by_tag', {}).get(t)
        if not boxed:
            continue
        i = bisect.bisect_left(boxed, line)
        cands = []
        if i < len(boxed):
            cands.append(boxed[i])
        if i > 0:
            cands.append(boxed[i - 1])
        if cands:
            best = min(cands, key=lambda ln_: (abs(ln_ - line), -ln_))
            return _box_for_line(parsed, t, best)['page']
    return None


# ── Section scanning (ported from overleaf-mcp latex.py) ───────────────

SECTION_LEVELS = [
    'part', 'chapter', 'section', 'subsection',
    'subsubsection', 'paragraph', 'subparagraph',
]

SECTION_PATTERN = re.compile(
    r"\\(" + "|".join(SECTION_LEVELS) + r")(\*?)\s*\{",
    re.MULTILINE,
)

_SECTION_DEPTH = {name: max(0, i - 2) for i, name in enumerate(SECTION_LEVELS)}


def _extract_braced(content: str, open_idx: int) -> tuple[str, int] | None:
    """Balanced-brace walk from ``content[open_idx] == '{'`` → (body, after)."""
    if open_idx >= len(content) or content[open_idx] != '{':
        return None
    depth = 1
    i = open_idx + 1
    n = len(content)
    while i < n:
        ch = content[i]
        if ch == '\\' and i + 1 < n:
            i += 2
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return content[open_idx + 1:i], i + 1
        i += 1
    return None


def _iter_section_lines(text: str):
    """Yield ``(line_no, type, title)`` per sectioning command (1-based lines)."""
    for m in SECTION_PATTERN.finditer(text):
        extracted = _extract_braced(text, m.end() - 1)
        title = extracted[0] if extracted else ''
        yield text.count('\n', 0, m.start()) + 1, m.group(1), title


# ── Section page map ────────────────────────────────────────────────────

def section_page_map(workdir: str | os.PathLike, file: str | None = None,
                     root: str | None = None) -> dict:
    """Map every sectioning command to its PDF page, from LOCAL build artifacts.

    Expects an existing build (run :func:`compile_project` first, or use
    :func:`compile_and_report`). Returns sections, total pages, physical page
    height (MediaBox), text-body bottom (geometry), and last-page fullness —
    both against the physical page and against the printable text area.
    """
    wd = Path(workdir)
    root_path = wd / root if root else find_compile_root(wd)
    stem = root_path.stem

    synctex_path = wd / f'{stem}.synctex.gz'
    if not synctex_path.is_file():
        return {'error': f'no {stem}.synctex.gz — compile with -synctex=1 first',
                'sections': [], 'end': None}
    parsed = parse_synctex(gzip.decompress(synctex_path.read_bytes()).decode('utf-8', 'replace'))

    source_rel = file or root_path.name
    source_path = wd / source_rel
    text = _read(source_path)
    if text is None:
        return {'error': f'cannot read source {source_rel}', 'sections': [], 'end': None}

    tags = _synctex_tags_for_label(parsed, source_rel)
    if not tags:  # root in a subdir / absolute-recorded path: basename fallback
        tags = _synctex_tags_for_label(parsed, root_path.name)

    log_text = _read(wd / f'{stem}.log')
    pdf_bytes = _read(wd / f'{stem}.pdf', binary=True)
    pages = _pages_from_log_text(log_text)
    if pages is None and pdf_bytes:
        pages = _pages_from_pdf_bytes(pdf_bytes)

    page_height, height_source = None, 'unknown'
    if pdf_bytes:
        page_height = _parse_mediabox_height(pdf_bytes)
        if page_height is not None:
            height_source = 'mediabox'
    if page_height is None:
        page_height, height_source = _ASSUMED_LETTER_HEIGHT_PT, 'assumed_letter'

    geometry_fields = _parse_geometry_fields(log_text)
    text_bottom = _text_block_bottom_bp(geometry_fields)
    text_top = _text_block_top_bp(geometry_fields)

    sections = []
    n_failed = 0
    for line_no, sec_type, title in _iter_section_lines(text):
        page = _page_for_line(parsed, tags, line_no) if tags else None
        n_failed += page is None
        sections.append({'type': sec_type, 'title': title,
                         'line': line_no, 'page': page})

    end = None
    m = re.search(r"\\end\{document\}", text)
    if m:
        end_line = text.count('\n', 0, m.start()) + 1
        end_page = parsed['max_page'] or pages
        v = _body_bottom_on_page(parsed, end_page, text_bottom) if end_page else None
        bottom_source = 'synctex'
        pdf_v = _content_bottom_from_pdf(
            wd / f'{stem}.pdf' if pdf_bytes else None, end_page,
            text_bottom, text_top)
        if pdf_v is not None:
            v, bottom_source = pdf_v, 'pdf'
        if v is not None:
            end = {'line': end_line, 'page': end_page, 'v': round(v, 3),
                   'bottom_source': bottom_source}
            if page_height:
                end['fill_pct'] = round(100.0 * v / page_height, 1)
                end['remaining_pt'] = round(page_height - v, 1)
            if text_bottom:
                end['text_area_remaining_pt'] = round(text_bottom - v, 1)
                end['text_area_fill_pct'] = round(100.0 * v / text_bottom, 1)

    return {
        'pages': pages,
        'page_height_pt': round(page_height, 2) if page_height else None,
        'page_height_source': height_source,
        'text_area_bottom_pt': round(text_bottom, 2) if text_bottom else None,
        'root': source_rel,
        'engine': 'synctex-offline-local',
        'sections': sections,
        'end': end,
        'unresolved': n_failed,
    }


def compile_and_report(workdir: str | os.PathLike, engine: str = 'pdflatex',
                       file: str | None = None,
                       timeout_s: int = _COMPILE_TIMEOUT_S) -> dict:
    """Compile, then return the section page map plus compile status."""
    build = compile_project(workdir, engine=engine, timeout_s=timeout_s)
    if build['status'] in ('no-toolchain', 'timeout'):
        return {'status': build['status'], 'errors': build.get('errors', []),
                'sections': [], 'end': None}
    report = section_page_map(workdir, file=file, root=build.get('root'))
    report['status'] = build['status']
    if build.get('errors'):
        report['errors'] = build['errors']
    return report


# ── Human-readable rendering ───────────────────────────────────────────

def format_section_page_map(result: dict) -> str:
    """Render a :func:`section_page_map` / :func:`compile_and_report` result."""
    if result.get('error'):
        return f"Error: {result['error']}"
    lines = [
        f"Layout map — root {result.get('root')!r}, "
        f"total {result.get('pages', '?')} page(s), status={result.get('status', '?')}",
        '',
    ]
    if not result.get('sections'):
        lines.append('(no sectioning commands found in the root file)')
    for s in result.get('sections', []):
        pg = s['page'] if s['page'] is not None else '?'
        indent = '  ' * _SECTION_DEPTH.get(s['type'], 0)
        lines.append(f"  p.{pg:<3} {indent}\\{s['type']}{{{s['title']}}}  (line {s['line']})")
    if result.get('unresolved'):
        lines.append('')
        lines.append(f"  Note: {result['unresolved']} line(s) unresolved (p.?) — "
                     'typically commented-out or box-less headings.')
    ph = result.get('page_height_pt')
    if ph:
        measured = ('measured from PDF MediaBox'
                    if result.get('page_height_source') == 'mediabox'
                    else f"ASSUMED ({result.get('page_height_source')})")
        lines += ['', f'  Page height: {ph} pt ({measured})']
    end = result.get('end')
    if end:
        ta_rem = end.get('text_area_remaining_pt')
        ta_fill = end.get('text_area_fill_pct')
        fill = end.get('fill_pct')
        rem = end.get('remaining_pt')
        if ta_rem is not None:
            lines.append(
                f"  Last page (p.{end['page']}) text area is ~{ta_fill}% full — "
                f"≈{ta_rem} pt of body space remain (bottom margin excluded; "
                f"roughly {max(0, int(ta_rem // 11))} more line(s) at ≈11-12 pt/line)")
            if fill is not None:
                lines.append(
                    f"    (physical page incl. margin: ~{fill}% full, ≈{rem} pt; "
                    f"last baseline v={end.get('v')} pt from top, "
                    f"source={end.get('bottom_source', 'synctex')})")
        elif fill is not None:
            lines.append(
                f"  Last page (p.{end['page']}) is ~{fill}% full — ≈{rem} pt remain "
                f"(includes bottom margin; geometry not reported; "
                f"last baseline v={end.get('v')} pt from top)")
    if result.get('errors'):
        lines += ['', '  Compile errors (first few):']
        lines += [f'    {e}' for e in result['errors'][:5]]
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog='python3 -m lib.paper.latex_layout',
        description='Compile a local LaTeX dir and report its page layout map.')
    ap.add_argument('workdir')
    ap.add_argument('--engine', choices=['pdflatex', 'xelatex'], default='pdflatex')
    ap.add_argument('--no-compile', action='store_true',
                    help='report from existing build artifacts only')
    ap.add_argument('--file', default=None,
                    help='map sections of this file instead of the compile root')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    args = ap.parse_args(argv)

    if args.no_compile:
        result = section_page_map(args.workdir, file=args.file)
    else:
        result = compile_and_report(args.workdir, engine=args.engine, file=args.file)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_section_page_map(result))
    return 1 if result.get('error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
