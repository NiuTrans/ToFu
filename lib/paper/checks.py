"""Submission-readiness quality gate over a LIVE paper source tree.

Track D of the paper toolchain: where ``lib.paper.builds`` answers "does it
compile", this module answers "is it ready to submit". Two families of checks
share one row shape (``severity``/``code``/``message``/``file``/``line``,
matching the build diagnostic contract):

* ``run_source_checks`` — TeXtidote/chktex-inspired academic QA rules over
  the live (pre-compile) source tree, comment-stripped and byte-bounded.
  Undefined citations/references are delegated to the existing
  ``citation_audit.audit_citations`` scan rather than re-implemented.
* ``run_pdf_checks`` — PDF-level checks on the latest retained build's
  artifacts (fonts embedded, raster image DPI, size/pages) via optional
  poppler tools; every subprocess is ``shutil.which``-guarded, timeout-bound
  and shell-free, and a missing tool degrades to one info row, never a
  failure.

``format_report`` renders both row sets as one grouped Chinese verdict for
the agent. Pure functions only: no model calls, no writes outside a temp
file for the PDF probe. See docs/PAPER_QUALITY.md for the authority record.
"""
from __future__ import annotations
from lib.paper.builds.contract import limit as limit

from lib.log import get_logger

logger = get_logger(__name__)
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lib.paper.builds.citation_audit import (_LABEL, _REF, _STRIP_COMMENTS,
                                             audit_citations)
from lib.paper.builds.errors import BuildError
from lib.paper.builds.runtime import get_scheduler
from lib.paper.builds.sources import read_source

# ── bounds ────────────────────────────────────────────────────────────────
_SOURCE_BUDGET_BYTES = 8 * 1024 * 1024      # total across one scan
_MAX_SOURCE_FILES = 256                     # .tex/.ltx members of one scan
_MAX_SCAN_ENTRIES = 4096                    # directory entries walked
_MAX_ROWS = 64                              # rows returned per run
_MAX_ROWS_PER_RULE = 16                     # one rule cannot drown the rest
_PDF_MAX_BYTES = 256 * 1024 * 1024          # temp-copy cap for PDF probes
_SUBPROCESS_TIMEOUT_SECONDS = 30
_SUBPROCESS_OUTPUT_BYTES = 1024 * 1024
_DPI_WARNING = 200                          # below: warning
_DPI_ERROR = 120                            # below: error

_SKIP_DIRS = frozenset({'.git', '.hg', '.svn', '.tofu', '.texlive',
                        'node_modules', '__pycache__', '.venv', 'venv',
                        'build', 'dist', 'out', 'output'})

_GRAPHIC_EXTENSIONS = ('.pdf', '.png', '.jpg', '.jpeg', '.eps', '.ps',
                       '.gif', '.bmp', '.tiff', '.tif', '.svg')

_SEVERITY_RANK = {'error': 0, 'warning': 1, 'info': 2}

# ── regexes (module-level so the rule set is greppable) ───────────────────
_FLOAT_BLOCK = re.compile(
    r'\\begin\{(figure\*?|table\*?)\}(.*?)\\end\{\1\}', re.S)
_CAPTION = re.compile(r'\\caption\s*(?:\[[^\]]{0,400}\])?\s*\{')
_HARDCODED_REF = re.compile(
    r'(?<![\\A-Za-z])(?:Figure|Table|Section|Chapter|Appendix)\s*~\s*\d+'
    r'|(?<![\\A-Za-z])Fig\.?\s*~?\s*\d+')
_WORD_ADJACENT_COMMAND = re.compile(
    r'(?<=[A-Za-z0-9])\\(?:cite|citep|citet|citealt|citealp|citeauthor'
    r'|citeyear|citeyearpar|parencite|textcite|autocite|footcite'
    r'|ref|eqref|autoref|cref|Cref|vref)\s*(?:\[[^\]]{0,200}\])?\s*\{')
_SECTION = re.compile(r'\\(section|subsection|subsubsection)\s*\*?\s*\{')
_INCLUDEGRAPHICS = re.compile(
    r'\\includegraphics\s*(?:\[[^\]]{0,400}\])?\s*\{([^{}]{1,512})\}')
_GRAPHICSPATH = re.compile(r'\\graphicspath\s*\{((?:\s*\{[^{}]{0,256}\})+)\}')
_GRAPHICSPATH_DIR = re.compile(r'\{([^{}]{0,256})\}')
_INPUT = re.compile(r'\\(?:input|include|subfile)\s*\{')
_TODO = re.compile(r'(?<![A-Za-z])(?:TODO|FIXME|XXX)(?![A-Za-z])'
                   r'|\\todo\s*(?:\[[^\]]{0,200}\])?\s*\{')
_ABSTRACT = re.compile(
    r'\\begin\{abstract\}(.*?)\\end\{abstract\}', re.S)


@dataclass
class TexSource:
    """One bounded .tex member of a scan: raw text plus comment-stripped."""
    path: str
    raw: str
    text: str


def _line(text: str, pos: int) -> int:
    return text.count('\n', 0, pos) + 1


def _row(severity: str, code: str, message: str, file: str,
         line: int | None) -> dict:
    return {'severity': severity, 'code': code, 'message': message[:2000],
            'file': file[:512], 'line': line}


def read_tex_sources(root: Path, *, budget_bytes: int = _SOURCE_BUDGET_BYTES
                     ) -> tuple[list[TexSource], list[TexSource]]:
    """Read the bounded .tex/.bib set beneath root; returns (tex, bibs).

    Directory walk is incremental and refuses symlinks, mirroring paper
    discovery; every file is read through ``read_source`` with the shared
    per-file cap and a total byte budget. .bib members come back as raw
    ``TexSource`` rows too, so callers can feed ``audit_citations`` and
    bibliography listings without a second walk.
    """
    root = root.resolve(strict=True)
    tex: list[TexSource] = []
    bibs: list[TexSource] = []
    budget = {'bytes': 0}
    entries = 0

    def visit(path: Path, depth: int = 0) -> None:
        nonlocal entries
        if depth > 24 or budget['bytes'] > budget_bytes:
            return
        if len(tex) + len(bibs) >= _MAX_SOURCE_FILES:
            return
        if path.is_symlink():
            return
        if path.is_dir():
            try:
                with os.scandir(path) as children:
                    for child in children:
                        entries += 1
                        if entries > _MAX_SCAN_ENTRIES:
                            return
                        if child.name.startswith('.') or child.name in _SKIP_DIRS:
                            continue
                        visit(Path(child.path), depth + 1)
            except OSError:
                return
            return
        suffix = path.suffix.lower()
        if suffix not in ('.tex', '.ltx', '.bib'):
            return
        if not path.resolve().is_relative_to(root):
            return
        if budget['bytes'] > budget_bytes:
            return
        relative = path.relative_to(root).as_posix()
        try:
            data = read_source(root, path, limit=budget_bytes - budget['bytes'] + 1)
        except (BuildError, OSError):
            return
        budget['bytes'] += len(data)
        raw = data.decode('utf-8', errors='replace')
        if suffix == '.bib':
            bibs.append(TexSource(relative, raw, raw))
        else:
            tex.append(TexSource(relative, raw, _STRIP_COMMENTS.sub('', raw)))

    visit(root)
    tex.sort(key=lambda source: source.path)
    bibs.sort(key=lambda source: source.path)
    return tex, bibs


def _balanced_braces(text: str, open_pos: int) -> tuple[str, int]:
    """Extract brace-balanced content starting at the ``{`` at open_pos."""
    depth = 0
    for pos in range(open_pos, len(text)):
        char = text[pos]
        if char == '{' and (pos == 0 or text[pos - 1] != '\\'):
            depth += 1
        elif char == '}' and (pos == 0 or text[pos - 1] != '\\'):
            depth -= 1
            if depth == 0:
                return text[open_pos + 1:pos], pos + 1
    return text[open_pos + 1:], len(text)


def _section_spans(text: str) -> list[dict]:
    """\\section spans: [{'line', 'body', 'body_start'}], until next \\section."""
    spans = []
    sections = [m for m in _SECTION.finditer(text) if m.group(1) == 'section']
    for index, match in enumerate(sections):
        end = (sections[index + 1].start() if index + 1 < len(sections)
               else len(text))
        spans.append({'line': _line(text, match.start()),
                      'body_start': match.end(),
                      'body': text[match.end():end]})
    return spans


# ── source rules: one small function each, table-driven below ─────────────

def _check_figure_without_label(sources: list[TexSource],
                                _aggregate: dict) -> list[dict]:
    rows = []
    for source in sources:
        for match in _FLOAT_BLOCK.finditer(source.text):
            if match.group(1).startswith('figure') and \
                    '\\label' not in match.group(2):
                rows.append(_row(
                    'warning', 'figure_without_label',
                    'figure environment has no \\label; it cannot be '
                    'referenced and is invisible to readers',
                    source.path, _line(source.text, match.start())))
    return rows


def _check_figure_not_referenced(sources: list[TexSource],
                                 aggregate: dict) -> list[dict]:
    referenced = aggregate['referenced_keys']
    rows = []
    for source in sources:
        for block in _FLOAT_BLOCK.finditer(source.text):
            for label in _LABEL.finditer(block.group(2)):
                if label.group(1) not in referenced:
                    rows.append(_row(
                        'warning', 'figure_not_referenced',
                        "\\label{%s} inside a %s is never referenced by any "
                        "\\ref/\\autoref/\\cref" % (label.group(1),
                                                    block.group(1)),
                        source.path,
                        _line(source.text, block.start(2) + label.start())))
    return rows


def _check_hardcoded_ref_number(sources: list[TexSource],
                                _aggregate: dict) -> list[dict]:
    rows = []
    for source in sources:
        for match in _HARDCODED_REF.finditer(source.text):
            rows.append(_row(
                'warning', 'hardcoded_ref_number',
                "Hard-coded cross-reference '%s'; use \\ref so numbering "
                "tracks the output" % match.group(0),
                source.path, _line(source.text, match.start())))
    return rows


def _check_caption_missing_period(sources: list[TexSource],
                                  _aggregate: dict) -> list[dict]:
    rows = []
    for source in sources:
        for match in _CAPTION.finditer(source.text):
            content, _end = _balanced_braces(source.text, match.end() - 1)
            tail = content.rstrip().rstrip('~').rstrip()
            while tail.endswith('\\relax'):
                tail = tail[:-len('\\relax')].rstrip()
            # An explicit closing group (math, \etal{...}) counts as ended.
            if tail and tail[-1] not in '.?!}':
                rows.append(_row(
                    'warning', 'caption_missing_period',
                    'Caption does not end with terminal punctuation: "%s"'
                    % tail[-60:],
                    source.path, _line(source.text, match.start())))
    return rows


def _check_missing_nbsp_before_cite(sources: list[TexSource],
                                    _aggregate: dict) -> list[dict]:
    rows = []
    for source in sources:
        for match in _WORD_ADJACENT_COMMAND.finditer(source.text):
            rows.append(_row(
                'info', 'missing_nbsp_before_cite',
                "Word directly abuts '%s'; insert a tie: 'word~\\cite{...}' "
                'so a line break cannot separate them'
                % match.group(0).split('{', 1)[0],
                source.path, _line(source.text, match.start())))
    return rows


def _check_single_subsection(sources: list[TexSource],
                             _aggregate: dict) -> list[dict]:
    rows = []
    for source in sources:
        for span in _section_spans(source.text):
            body = span['body']
            # Subsections may live in \\input children; never guess there.
            if _INPUT.search(body):
                continue
            if len(re.findall(r'\\subsection\s*\*?\s*\{', body)) == 1:
                rows.append(_row(
                    'info', 'single_subsection',
                    'Section contains exactly one \\subsection; either add a '
                    'sibling or promote the material into the section',
                    source.path, span['line']))
    return rows


def _check_heading_level_skip(sources: list[TexSource],
                              _aggregate: dict) -> list[dict]:
    rows = []
    for source in sources:
        for span in _section_spans(source.text):
            first = _SECTION.search(span['body'])
            if first and first.group(1) == 'subsubsection':
                rows.append(_row(
                    'warning', 'heading_level_skip',
                    '\\subsubsection directly under \\section with no '
                    '\\subsection between; heading levels must not skip',
                    source.path,
                    _line(source.text, span['body_start'] + first.start())))
    return rows


def _check_duplicate_label(sources: list[TexSource],
                           _aggregate: dict) -> list[dict]:
    seen: dict[str, tuple[str, int]] = {}
    rows = []
    for source in sources:
        for match in _LABEL.finditer(source.text):
            key = match.group(1)
            line = _line(source.text, match.start())
            if key in seen:
                first_file, first_line = seen[key]
                rows.append(_row(
                    'error', 'duplicate_label',
                    "\\label{%s} is defined more than once (first at %s:%d); "
                    'cross-references become ambiguous'
                    % (key, first_file, first_line),
                    source.path, line))
            else:
                seen[key] = (source.path, line)
    return rows


def _check_undefined_crossrefs(_sources: list[TexSource],
                               aggregate: dict) -> list[dict]:
    # Delegated to the shared audit so .bib parsing and log-key dedupe stay
    # in one place; only the two load-bearing codes join this gate.
    rows = []
    try:
        audited = aggregate['citation_audit']()
    except BuildError:
        return rows
    for row in audited:
        if row['code'] in ('undefined_citation', 'undefined_reference'):
            rows.append(dict(row))
    return rows


def _graphics_dirs(root: Path, main_file: str,
                   sources: list[TexSource]) -> list[Path]:
    dirs = [root, (root / main_file).parent]
    for source in sources:
        for match in _GRAPHICSPATH.finditer(source.text):
            for directory in _GRAPHICSPATH_DIR.findall(match.group(1)):
                candidate = (root / directory.strip()).resolve()
                if candidate.is_dir() and candidate.is_relative_to(root):
                    dirs.append(candidate)
    return list(dict.fromkeys(dirs))


def _graphics_resolvable(directories: list[Path], target: str) -> bool:
    target_path = Path(target)
    names = [target] if target_path.suffix else [
        target + extension for extension in _GRAPHIC_EXTENSIONS]
    for directory in directories:
        for name in names:
            try:
                candidate = (directory / name).resolve()
                if candidate.is_file():
                    return True
            except OSError:
                continue
    return False


def _check_includegraphics_missing_file(sources: list[TexSource],
                                        aggregate: dict) -> list[dict]:
    directories = aggregate['graphics_dirs']
    rows = []
    for source in sources:
        for match in _INCLUDEGRAPHICS.finditer(source.text):
            target = match.group(1).strip()
            # Macro-built paths and URLs are not resolvable statically.
            if not target or '\\' in target or '://' in target:
                continue
            if not _graphics_resolvable(directories, target):
                rows.append(_row(
                    'error', 'includegraphics_missing_file',
                    "\\includegraphics target '%s' does not resolve under the "
                    'project (checked .%s + \\graphicspath)'
                    % (target, '/.'.join(e[1:] for e in _GRAPHIC_EXTENSIONS)),
                    source.path, _line(source.text, match.start())))
    return rows


def _check_todo_markers(sources: list[TexSource],
                        _aggregate: dict) -> list[dict]:
    rows = []
    for source in sources:
        # Raw text on purpose: TODOs conventionally live in comments, and
        # comment-stripping would hide exactly what this rule hunts.
        for match in _TODO.finditer(source.raw):
            rows.append(_row(
                'info', 'todo_markers',
                "Leftover marker '%s'; resolve or remove before submission"
                % match.group(0)[:40],
                source.path, _line(source.raw, match.start())))
    return rows


# (code-owner function, severity-independent; order = report order)
_SOURCE_RULES = (
    _check_duplicate_label,
    _check_includegraphics_missing_file,
    _check_undefined_crossrefs,
    _check_figure_without_label,
    _check_figure_not_referenced,
    _check_hardcoded_ref_number,
    _check_caption_missing_period,
    _check_heading_level_skip,
    _check_missing_nbsp_before_cite,
    _check_single_subsection,
    _check_todo_markers,
)


def run_source_checks(root: Path, main_file: str, *,
                      budget_bytes: int = _SOURCE_BUDGET_BYTES
                      ) -> list[dict]:
    """Run the source-level gate on a live tree; ≤64 rows, never raises on
    malformed TeX. ``root`` is the already-authorized paper tree (typically
    the main file's directory); ``main_file`` is relative to it."""
    root = Path(root).resolve()
    sources, bibs = read_tex_sources(root, budget_bytes=budget_bytes)

    def citation_audit() -> list[dict]:
        files = tuple({'path': source.path}
                      for source in (*sources, *bibs))
        return audit_citations(root, files)

    aggregate = {
        'referenced_keys': {
            key for source in sources for match in _REF.finditer(source.text)
            for key in (k.strip() for k in match.group(1).split(',')) if key},
        'citation_audit': citation_audit,
        'graphics_dirs': _graphics_dirs(root, main_file, sources),
    }
    rows: list[dict] = []
    for rule in _SOURCE_RULES:
        try:
            rows.extend(rule(sources, aggregate)[:_MAX_ROWS_PER_RULE])
        except Exception as _e:
            logger.debug('run one source check failed: %s', _e)
            # One advisory rule must never take the gate down with it.
            continue
    rows.sort(key=lambda row: (_SEVERITY_RANK.get(row['severity'], 3),
                               row['file'], row['line'] or 0))
    return rows[:_MAX_ROWS]


# ── PDF-level checks on the latest retained build ─────────────────────────

def _parse_pdffonts(output: str) -> list[dict]:
    """Non-embedded fonts from ``pdffonts`` output (emb column == 'no')."""
    rows = []
    # Header/separator lines fail the yes-no anchor below and are skipped.
    for line in output.splitlines():
        tokens = line.split()
        # Layout: name type encoding emb sub uni object-ID. Both the name
        # and the type ("CID Type 0C") may contain spaces, so anchor from
        # the tail: the emb/sub/uni yes-no triple plus the two numeric
        # object-ID fields (older poppler omits the object ID).
        if len(tokens) >= 5 and all(t in ('yes', 'no') for t in tokens[-5:-2]) \
                and tokens[-2].isdigit() and tokens[-1].isdigit():
            embedded, name_end = tokens[-5], -5
        elif len(tokens) >= 3 and all(t in ('yes', 'no')
                                      for t in tokens[-3:]):
            embedded, name_end = tokens[-3], -3
        else:
            continue
        if embedded == 'no':
            head = tokens[:name_end] or ['?']
            rows.append({'name': head[0][:120],
                         'type': ' '.join(head[1:])[:80] or '?'})
    return rows


def _parse_pdfimages(output: str) -> list[dict]:
    """Raster images with effective DPI from ``pdfimages -list`` output."""
    rows = []
    # Header lines fail the int() anchors below and are skipped.
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) < 14:
            continue
        try:
            page = int(tokens[0])
            # Anchor from the tail (robust to poppler versions with/without
            # the object-ID columns): ratio ('42%'), size, y-ppi, x-ppi.
            x_ppi, y_ppi = int(tokens[-4]), int(tokens[-3])
        except (ValueError, IndexError):
            continue
        if tokens[2] != 'image':
            continue  # smask/stencil are masks, not placed raster art
        rows.append({'page': page, 'x_ppi': x_ppi, 'y_ppi': y_ppi,
                     'width': tokens[3], 'height': tokens[4]})
    return rows


def _run_probe(argv: list[str]) -> str | None:
    """One bounded, shell-free probe; None on timeout/failure (info, not error)."""
    try:
        proc = subprocess.run(argv, capture_output=True, shell=False,
                              timeout=_SUBPROCESS_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout[:_SUBPROCESS_OUTPUT_BYTES].decode('utf-8',
                                                         errors='replace')


def _receipt_pdf_size(receipt: dict) -> int:
    for artifact in receipt.get('artifacts') or []:
        if artifact.get('name') == 'document.pdf':
            return int(artifact.get('size_bytes') or 0)
    return 0


def _synctex_page_count(owner_user_id: int, document_id: str,
                        build_id: str) -> int:
    """Page count from the build's own SyncTeX (same trick as the handler)."""
    try:
        scheduler = get_scheduler()
        with scheduler.cache.open_artifact(owner_user_id, document_id,
                                           build_id,
                                           'document.synctex.gz') as artifact:
            stream, _size = artifact
            from lib.paper.latex_layout import parse_synctex
            from lib.paper.builds.synctex import _decode
            return int(parse_synctex(_decode(stream)).get('max_page') or 0)
    except Exception as _e:
        logger.debug('count synctex pages failed: %s', _e)
        # Advisory garnish; a cache/parse quirk never fails the gate.
        return 0


def run_pdf_checks(document: dict, receipt: dict, *,
                   owner_user_id: int) -> list[dict]:
    """PDF-level gate rows for the latest retained successful build.

    Missing poppler tools, a busy cache or an oversized PDF each degrade to
    exactly one info row; this function never raises for probe failures.
    """
    document_id = receipt.get('document_id') or document.get('document_id')
    build_id = receipt.get('build_id') or ''
    rows: list[dict] = []
    try:
        scheduler = get_scheduler()
        with scheduler.cache.open_artifact(owner_user_id, document_id,
                                           build_id, 'document.pdf') as artifact:
            stream, size = artifact
            data = stream.read(min(size, _PDF_MAX_BYTES) + 1)
    except (BuildError, OSError, ValueError, KeyError):
        return [_row('info', 'pdf_unavailable',
                     'Retained PDF artifact is unavailable; re-run '
                     'paper_build, then repeat the check', '', None)]
    if len(data) > _PDF_MAX_BYTES:
        return [_row('info', 'pdf_unavailable',
                     'PDF exceeds the %d MiB probe budget; PDF-level checks '
                     'skipped' % (_PDF_MAX_BYTES // (1024 * 1024)), '', None)]

    rows.append(_pdf_size_page_row(receipt, data, owner_user_id,
                                   document_id, build_id))

    pdffonts = shutil.which('pdffonts')
    pdfimages = shutil.which('pdfimages')
    if not pdffonts and not pdfimages:
        rows.append(_row('info', 'poppler_unavailable',
                         'poppler-utils not installed; font-embedding and '
                         'image-DPI checks unavailable (check skipped, not '
                         'failed)', '', None))
        return rows

    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=True) as handle:
        handle.write(data)
        handle.flush()
        if pdffonts:
            rows.extend(_fonts_rows(_run_probe([pdffonts, handle.name])))
        else:
            rows.append(_row('info', 'poppler_unavailable',
                             'pdffonts not installed; font-embedding check '
                             'unavailable (a hard IEEE/ACM requirement — '
                             'verify manually)', '', None))
        if pdfimages:
            rows.extend(_image_dpi_rows(
                _run_probe([pdfimages, '-list', handle.name])))
        else:
            rows.append(_row('info', 'poppler_unavailable',
                             'pdfimages not installed; image-DPI check '
                             'unavailable', '', None))
    return rows[:_MAX_ROWS]


def _pdf_size_page_row(receipt: dict, data: bytes, owner_user_id: int,
                       document_id: str, build_id: str) -> dict:
    size = _receipt_pdf_size(receipt) or len(data)
    pages = _synctex_page_count(owner_user_id, document_id, build_id)
    mib = size / (1024.0 * 1024.0)
    return _row('info', 'pdf_size_pages',
                'PDF %.2f MiB%s (build %s)'
                % (mib, ', %d pages' % pages if pages else ', page count '
                   'unknown (no SyncTeX)', build_id[:12]), '', None)


def _fonts_rows(output: str | None) -> list[dict]:
    if output is None:
        return [_row('info', 'fonts_check_failed',
                     'pdffonts probe timed out or failed; font embedding '
                     'not verified', '', None)]
    missing = _parse_pdffonts(output)
    if not missing:
        return [_row('info', 'fonts_embedded',
                     'All fonts are embedded (pdffonts emb=yes)', '', None)]
    rows = [_row('error', 'fonts_embedded',
                 "Font '%s' (%s) is NOT embedded — a hard IEEE/ACM "
                 'submission requirement' % (font['name'], font['type']),
                 '', None)
            for font in missing[:8]]
    if len(missing) > 8:
        rows.append(_row('error', 'fonts_embedded',
                         '…and %d more non-embedded fonts'
                         % (len(missing) - 8), '', None))
    return rows


def _image_dpi_rows(output: str | None) -> list[dict]:
    if output is None:
        return [_row('info', 'image_dpi_check_failed',
                     'pdfimages probe timed out or failed; image DPI not '
                     'verified', '', None)]
    rows = []
    for image in _parse_pdfimages(output):
        dpi = min(image['x_ppi'], image['y_ppi'])
        if dpi >= _DPI_WARNING:
            continue
        severity = 'error' if dpi < _DPI_ERROR else 'warning'
        rows.append(_row(
            severity, 'image_dpi',
            'Raster image on page %d (%sx%s px) has effective %d DPI '
            '(<%d); regenerate at higher resolution'
            % (image['page'], image['width'], image['height'], dpi,
               _DPI_ERROR if severity == 'error' else _DPI_WARNING),
            '', None))
    return rows[:16]


# ── report ────────────────────────────────────────────────────────────────

def format_report(source_rows: list[dict], pdf_rows: list[dict]) -> str:
    """Grouped Chinese verdict: ✓/✗ header counts, per-group rows, footer."""
    def counts(rows):
        return {severity: sum(1 for row in rows if row['severity'] == severity)
                for severity in ('error', 'warning', 'info')}

    all_rows = list(source_rows) + list(pdf_rows)
    total = counts(all_rows)
    if total['error']:
        headline = '✗ 投稿就绪检查未通过'
    elif total['warning']:
        headline = '△ 投稿就绪检查有警告'
    else:
        headline = '✓ 投稿就绪检查通过'
    lines = ['%s：错误 %d · 警告 %d · 提示 %d'
             % (headline, total['error'], total['warning'], total['info'])]

    def emit(title: str, rows: list[dict]) -> None:
        if not rows:
            lines.append('%s：无问题。' % title)
            return
        lines.append('%s：' % title)
        primary = [row for row in rows if row['severity'] != 'info']
        shown = primary + [row for row in rows
                           if row['severity'] == 'info'][:24]
        for row in shown:
            where = row['file'] or '-'
            if row.get('line'):
                where += ':%d' % row['line']
            lines.append('  [%s] %s %s（%s）'
                         % (row['severity'], where, row['message'][:300],
                            row['code']))
        if len(rows) > len(shown):
            lines.append('  …另有 %d 条提示未列出' % (len(rows) - len(shown)))

    emit('源码检查', source_rows)
    emit('PDF 检查（最近一次已保留构建）', pdf_rows)
    lines.append('先修 error，再处理 warning；改完用 paper_build 复验。')
    return '\n'.join(lines)
