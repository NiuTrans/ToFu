"""Format sniffing and robust extraction for local knowledge documents."""

from __future__ import annotations

import io
import math
import os
import re
import zipfile

from lib.log import get_logger

from .assets import (
    IMAGE_EXTENSIONS, KnowledgeImageError, detect_image_mime, extract_package_assets,
    extract_pdf_assets, standalone_image,
)

logger = get_logger(__name__)

_OLE_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
_MAX_UNCOMPRESSED = 256 * 1024 * 1024
_MAX_ZIP_MEMBERS = 20_000

PLAIN_EXTENSIONS = (
    '.txt', '.md', '.markdown', '.csv', '.tsv', '.json', '.jsonl',
    '.xml', '.html', '.htm', '.yaml', '.yml', '.toml', '.ini', '.cfg',
    '.rst', '.log', '.tex', '.bib', '.srt', '.vtt', '.sql',
    '.py', '.js', '.ts', '.java', '.c', '.cpp', '.h', '.hpp', '.go',
    '.rs', '.rb', '.php', '.sh', '.bash', '.zsh', '.css', '.scss',
    '.less', '.r', '.m', '.swift',
)
OFFICE_EXTENSIONS = (
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
)
PORTABLE_EXTENSIONS = ('.rtf', '.eml', '.odt', '.ods', '.odp', '.epub')
SUPPORTED_EXTENSIONS = (
    OFFICE_EXTENSIONS + PLAIN_EXTENSIONS + PORTABLE_EXTENSIONS
    + IMAGE_EXTENSIONS
)

_HTML_EXTENSIONS = {'.html', '.htm'}
_OPEN_DOCUMENT_EXTENSIONS = {'.odt', '.ods', '.odp'}


class KnowledgeIngestError(ValueError):
    """A user-facing, per-document ingestion failure."""


def _limit_chars() -> int:
    raw = os.environ.get('TOFU_KNOWLEDGE_MAX_TEXT_CHARS', '12000000')
    try:
        return max(100_000, min(int(raw), 50_000_000))
    except (TypeError, ValueError) as exc:
        logger.debug('[Knowledge] invalid max-text limit %r: %s', raw, exc)
        return 12_000_000


def _safe_package_kind(raw: bytes) -> str:
    """Identify safe OOXML/ODF/EPUB packages from their members."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_ZIP_MEMBERS:
                raise KnowledgeIngestError('Office package contains too many entries')
            total = sum(max(0, i.file_size) for i in infos)
            if total > _MAX_UNCOMPRESSED:
                raise KnowledgeIngestError(
                    f'Office package expands beyond {_MAX_UNCOMPRESSED // 1048576} MB')
            compressed = max(1, sum(max(0, i.compress_size) for i in infos))
            if total > 32 * 1024 * 1024 and total / compressed > 200:
                raise KnowledgeIngestError('Office package has an unsafe compression ratio')
            names = {i.filename.lower() for i in infos}
            try:
                with archive.open('mimetype') as mimetype_file:
                    mimetype = mimetype_file.read(256).decode(
                        'ascii', errors='ignore').strip().lower()
            except (KeyError, RuntimeError, TypeError) as exc:
                logger.debug('[Knowledge] package mimetype entry unavailable: %s',
                             exc)
                mimetype = ''
    except KnowledgeIngestError:
        raise
    except (zipfile.BadZipFile, OSError) as exc:
        raise KnowledgeIngestError(f'Invalid ZIP/Office container: {exc}') from exc
    if 'word/document.xml' in names:
        return '.docx'
    if 'xl/workbook.xml' in names:
        return '.xlsx'
    if 'ppt/presentation.xml' in names:
        return '.pptx'
    if mimetype == 'application/vnd.oasis.opendocument.text':
        return '.odt'
    if mimetype == 'application/vnd.oasis.opendocument.spreadsheet':
        return '.ods'
    if mimetype == 'application/vnd.oasis.opendocument.presentation':
        return '.odp'
    if mimetype == 'application/epub+zip' or 'meta-inf/container.xml' in names:
        return '.epub'
    return ''


def detect_kind(raw: bytes, filename: str) -> str:
    """Return a canonical extension, preferring bytes over a misleading name."""
    ext = os.path.splitext(filename or '')[1].lower()
    image_mime = detect_image_mime(raw)
    if image_mime:
        return {
            'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif',
            'image/webp': '.webp', 'image/bmp': '.bmp',
        }[image_mime]
    if raw.startswith(b'%PDF-'):
        return '.pdf'
    if raw.startswith(b'PK\x03\x04'):
        kind = _safe_package_kind(raw)
        if kind:
            return kind
        raise KnowledgeIngestError('ZIP archives are not knowledge documents')
    if raw.startswith(_OLE_MAGIC):
        if ext in ('.doc', '.xls', '.ppt'):
            return ext
        # Inspect OLE stream names when the extension is absent or wrong.
        try:
            import olefile
            ole = olefile.OleFileIO(io.BytesIO(raw))
            paths = {'/'.join(p).lower() for p in ole.listdir()}
            ole.close()
            if 'worddocument' in paths:
                return '.doc'
            if 'workbook' in paths or 'book' in paths:
                return '.xls'
            if 'powerpoint document' in paths:
                return '.ppt'
        except Exception as exc:
            logger.debug('[Knowledge] OLE type inspection failed: %s', exc)
        raise KnowledgeIngestError('Unrecognized legacy Office document')
    if raw.startswith((b'\xef\xbb\xbf', b'\xff\xfe', b'\xfe\xff',
                       b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
        return ext if ext in PLAIN_EXTENSIONS else '.txt'
    if raw.lstrip()[:5].lower() == b'{\\rtf':
        return '.rtf'
    if ext in PLAIN_EXTENSIONS or ext in ('.rtf', '.eml'):
        return ext
    # Unknown/extensionless files are accepted only when they look textual.
    probe = raw[:8192]
    if not probe:
        raise KnowledgeIngestError('Empty file')
    nul_ratio = probe.count(b'\x00') / len(probe)
    controls = sum(1 for b in probe if b < 8 or 13 < b < 32)
    if nul_ratio < 0.08 and controls / len(probe) < 0.20:
        return '.txt'
    raise KnowledgeIngestError(f'Unsupported or binary file type: {ext or "unknown"}')


def _decode_text(raw: bytes, filename: str, limit: int) -> tuple[str, list[str]]:
    from lib.doc_parser._plain import _extract_plaintext
    result = _extract_plaintext(raw, filename, limit)
    return str(result.get('text') or ''), [
        str(item) for item in (result.get('warnings') or [])]


def _html_to_text(value: str) -> str:
    """Turn HTML into readable, structure-preserving text without scripts."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(value or '', 'html.parser')
        for node in soup(['script', 'style', 'template', 'noscript', 'svg']):
            node.decompose()
        for heading in soup.find_all(re.compile(r'^h[1-6]$')):
            level = int(heading.name[1])
            heading.string = f"{'#' * level} {heading.get_text(' ', strip=True)}"
        for row in soup.find_all('tr'):
            cells = [cell.get_text(' ', strip=True).replace('|', '\\|')
                     for cell in row.find_all(['th', 'td'])]
            if cells:
                row.string = '| ' + ' | '.join(cells) + ' |'
        text = soup.get_text('\n')
    except Exception as exc:
        logger.debug('[Knowledge] BeautifulSoup HTML cleanup failed: %s', exc)
        from html.parser import HTMLParser

        class _TextParser(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.parts: list[str] = []
                self.hidden = 0

            def handle_starttag(self, tag, attrs):
                if tag in ('script', 'style', 'template', 'noscript'):
                    self.hidden += 1
                elif tag in ('p', 'br', 'div', 'li', 'tr', 'h1', 'h2', 'h3',
                             'h4', 'h5', 'h6'):
                    self.parts.append('\n')

            def handle_endtag(self, tag):
                if tag in ('script', 'style', 'template', 'noscript'):
                    self.hidden = max(0, self.hidden - 1)
                elif tag in ('p', 'div', 'li', 'tr'):
                    self.parts.append('\n')

            def handle_data(self, data):
                if not self.hidden:
                    self.parts.append(data)

        parser = _TextParser()
        parser.feed(value or '')
        text = ''.join(parser.parts)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n[ \t]+', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _extract_rtf(raw: bytes, limit: int) -> dict:
    """Decode common RTF control words, unicode escapes and paragraphs."""
    value = raw.decode('latin-1', errors='replace')
    destinations = {
        'fonttbl', 'colortbl', 'datastore', 'themedata', 'stylesheet',
        'info', 'pict', 'object', 'header', 'footer', 'fldinst',
    }
    token_re = re.compile(
        r"\\([a-zA-Z]{1,32})(-?\d{1,10})?[ ]?|\\'([0-9a-fA-F]{2})|"
        r"\\([^a-zA-Z])|([{}])|[\r\n]+|(.)",
        re.S,
    )
    stack: list[tuple[int, bool]] = []
    out: list[str] = []
    ucskip = 1
    curskip = 0
    ignorable = False
    for match in token_re.finditer(value):
        word, arg, hex_value, symbol, brace, char = match.groups()
        if brace:
            if brace == '{':
                stack.append((ucskip, ignorable))
            elif stack:
                ucskip, ignorable = stack.pop()
            continue
        if symbol:
            if symbol == '*':
                ignorable = True
            elif not ignorable and symbol in '{}\\':
                out.append(symbol)
            elif not ignorable and symbol == '~':
                out.append(' ')
            continue
        if word:
            lowered = word.lower()
            if lowered in destinations:
                ignorable = True
            elif lowered == 'uc' and arg is not None:
                ucskip = max(0, int(arg))
            elif lowered == 'u' and arg is not None and not ignorable:
                codepoint = int(arg)
                if codepoint < 0:
                    codepoint += 65536
                out.append(chr(codepoint))
                curskip = ucskip
            elif lowered in ('par', 'line') and not ignorable:
                out.append('\n')
            elif lowered == 'tab' and not ignorable:
                out.append('\t')
            continue
        if ignorable:
            continue
        if hex_value:
            if curskip:
                curskip -= 1
            else:
                out.append(bytes.fromhex(hex_value).decode(
                    'cp1252', errors='replace'))
            continue
        if char:
            if curskip:
                curskip -= 1
            else:
                out.append(char)
    text = re.sub(r'\n{3,}', '\n\n', ''.join(out)).strip()
    warnings = []
    if len(text) > limit:
        warnings.append(f'Text was truncated to {limit:,} characters')
        text = text[:limit]
    return {'text': text, 'kind': '.rtf', 'method': 'rtf-local',
            'warnings': warnings, 'pages': 0}


def _open_document_text(raw: bytes, kind: str, limit: int) -> dict:
    """Extract paragraphs, headings and table rows from an ODF package."""
    import xml.etree.ElementTree as ET
    from lib.doc_parser._tables import render_markdown_table
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            root = ET.fromstring(archive.read('content.xml'))
    except (KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise KnowledgeIngestError(f'Invalid OpenDocument content: {exc}') from exc
    parts: list[str] = []
    total_chars = 0
    warnings: list[str] = []

    def attr(elem, local_name: str) -> str:
        for name, value in elem.attrib.items():
            if str(name).rsplit('}', 1)[-1] == local_name:
                return str(value)
        return ''

    def repeated(elem, local_name: str, cap: int) -> int:
        try:
            value = max(1, int(attr(elem, local_name) or '1'))
        except ValueError as exc:
            logger.debug('[Knowledge] invalid ODF repeat count %r: %s',
                         attr(elem, local_name), exc)
            return 1
        if value > cap:
            warnings.append(
                f'OpenDocument {local_name} was capped at {cap:,}')
        return min(value, cap)

    def odf_cell_text(cell) -> str:
        value = ' '.join(''.join(cell.itertext()).split())
        if value:
            return value
        for fallback in (
                'string-value', 'date-value', 'time-value',
                'boolean-value', 'value'):
            value = attr(cell, fallback).strip()
            if value:
                return value
        return ''

    def append(value: str) -> None:
        nonlocal total_chars
        value = (value or '').strip()
        if value and (not parts or parts[-1] != value):
            parts.append(value)
            total_chars += len(value) + 1

    def walk(elem) -> None:
        local = str(elem.tag).rsplit('}', 1)[-1]
        if total_chars >= limit:
            return
        if local == 'table':
            rows: list[list[str]] = []
            for row in elem.iter():
                if str(row.tag).rsplit('}', 1)[-1] != 'table-row':
                    continue
                cells: list[str] = []
                for child in row:
                    child_local = str(child.tag).rsplit('}', 1)[-1]
                    if child_local not in ('table-cell', 'covered-table-cell'):
                        continue
                    cell = (odf_cell_text(child)
                            if child_local == 'table-cell' else '')
                    cell_count = repeated(
                        child, 'number-columns-repeated', 256 - len(cells))
                    cells.extend([cell] * cell_count)
                    if len(cells) >= 256:
                        break
                if any(cells):
                    row_count = repeated(row, 'number-rows-repeated', 1000)
                    rows.extend([cells] * min(row_count, 20_000 - len(rows)))
                if len(rows) >= 20_000:
                    warnings.append(
                        'OpenDocument table was capped at 20,000 populated rows')
                    break
            table_name = attr(elem, 'name').strip()
            if table_name:
                append(f'## Table: {table_name}')
            append(render_markdown_table(rows))
            return
        if local in ('h', 'p', 'list-item'):
            text = ' '.join(''.join(elem.itertext()).split())
            if local == 'h' and text:
                text = '## ' + text
            elif local == 'list-item' and text:
                text = '- ' + text
            append(text)
            return
        for child in elem:
            walk(child)

    walk(root)
    text = '\n'.join(parts)[:limit].strip()
    if len('\n'.join(parts)) > limit:
        warnings.append(f'Text was truncated to {limit:,} characters')
    return {'text': text, 'kind': kind, 'method': 'opendocument-local',
            'warnings': warnings, 'pages': 0}


def _delimited_text(raw: bytes, filename: str, kind: str, limit: int) -> dict:
    """Parse CSV/TSV dialects and normalize ragged, preambled tables."""
    import csv
    from lib.doc_parser._tables import render_markdown_table

    decoded, warnings = _decode_text(raw, filename, limit)
    sample = decoded[:65_536]
    delimiter = '\t' if kind == '.tsv' else ','
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter_label = 'tab' if delimiter == '\t' else repr(delimiter)
        warnings.append(
            f'Could not infer delimiter; parsed as {delimiter_label}')
    rows = list(csv.reader(io.StringIO(decoded), delimiter=delimiter))
    blocks: list[list[list[str]]] = []
    current: list[list[str]] = []
    for row in rows:
        if any(str(value).strip() for value in row):
            current.append(row)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    rendered = []
    for index, block in enumerate(blocks, 1):
        if len(blocks) > 1:
            rendered.append(f'## Table block {index}')
        table = render_markdown_table(block)
        if table:
            rendered.append(table)
    text = '\n\n'.join(rendered)[:limit].strip()
    return {'text': text, 'kind': kind, 'method': 'delimited-table-local',
            'warnings': list(dict.fromkeys(warnings)), 'pages': 1}


def _strip_repeated_pdf_margins(text: str) -> str:
    """Remove only high-confidence headers/footers repeated across PDF pages.

    Per-page Markdown extraction preserves ``---`` page boundaries, which lets
    us distinguish a real repeated margin from a legitimate repeated table
    value. A line must occur near an edge on at least half of three or more
    pages before it is removed.
    """
    pages = re.split(r'\n\s*---\s*\n', text or '')
    if len(pages) < 3:
        return text

    def canonical(line: str) -> str:
        value = re.sub(r'^[#>*\s-]+|[#>*\s-]+$', '', line or '')
        value = re.sub(r'\*{1,2}|_{1,2}|`', '', value)
        return re.sub(r'\s+', '', value).casefold()

    counts: dict[str, int] = {}
    edge_keys: list[set[str]] = []
    for page in pages:
        lines = [line for line in page.splitlines() if line.strip()]
        keys = set()
        for line in lines[:3] + lines[-3:]:
            key = canonical(line)
            if (4 <= len(key) <= 120 and not line.lstrip().startswith('|')
                    and not re.fullmatch(r'\d+', key)):
                keys.add(key)
        edge_keys.append(keys)
        for key in keys:
            counts[key] = counts.get(key, 0) + 1
    threshold = max(3, math.ceil(len(pages) / 2))
    repeated = {key for key, count in counts.items() if count >= threshold}
    if not repeated:
        return text
    cleaned_pages: list[str] = []
    for page, keys in zip(pages, edge_keys):
        lines = page.splitlines()
        nonblank = [index for index, line in enumerate(lines) if line.strip()]
        edge_indexes = set(nonblank[:3] + nonblank[-3:])
        kept = [
            line for index, line in enumerate(lines)
            if not (index in edge_indexes and canonical(line) in repeated
                    and canonical(line) in keys)
        ]
        cleaned_pages.append('\n'.join(kept).strip())
    return '\n\n---\n\n'.join(page for page in cleaned_pages if page)


def _epub_text(raw: bytes, limit: int) -> dict:
    """Read EPUB chapters in spine order, falling back to archive order."""
    import posixpath
    import xml.etree.ElementTree as ET
    warnings: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = {name.lower(): name for name in archive.namelist()}
            chapters: list[str] = []
            try:
                container = ET.fromstring(archive.read(
                    names['meta-inf/container.xml']))
                rootfile = next(
                    elem.attrib.get('full-path', '')
                    for elem in container.iter()
                    if str(elem.tag).rsplit('}', 1)[-1] == 'rootfile')
                package = ET.fromstring(archive.read(rootfile))
                manifest = {
                    elem.attrib.get('id', ''): elem.attrib.get('href', '')
                    for elem in package.iter()
                    if str(elem.tag).rsplit('}', 1)[-1] == 'item'
                }
                base = posixpath.dirname(rootfile)
                for elem in package.iter():
                    if str(elem.tag).rsplit('}', 1)[-1] != 'itemref':
                        continue
                    href = manifest.get(elem.attrib.get('idref', ''), '')
                    if href:
                        chapters.append(posixpath.normpath(
                            posixpath.join(base, href)))
            except (KeyError, StopIteration, ET.ParseError):
                warnings.append('EPUB spine metadata was incomplete; used archive order')
            if not chapters:
                chapters = [name for name in archive.namelist()
                            if name.lower().endswith(('.xhtml', '.html', '.htm'))]
            parts: list[str] = []
            used_chars = 0
            for index, name in enumerate(chapters, 1):
                if name not in archive.namelist():
                    continue
                chapter_raw = archive.read(name)
                chapter, chapter_warnings = _decode_text(
                    chapter_raw, name, max(1, limit - used_chars))
                warnings.extend(chapter_warnings)
                cleaned = _html_to_text(chapter)
                if cleaned:
                    rendered = f'## Chapter {index}\n\n{cleaned}'
                    parts.append(rendered)
                    used_chars += len(rendered) + 2
                if used_chars >= limit:
                    warnings.append(f'EPUB text was truncated to {limit:,} characters')
                    break
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise KnowledgeIngestError(f'Invalid EPUB package: {exc}') from exc
    return {'text': '\n\n'.join(parts)[:limit], 'kind': '.epub',
            'method': 'epub-local', 'warnings': list(dict.fromkeys(warnings)),
            'pages': len(parts)}


def _email_text(raw: bytes, limit: int, *, depth: int) -> dict:
    """Extract an RFC email body and searchable text attachments locally."""
    from email import policy
    from email.parser import BytesParser
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:
        raise KnowledgeIngestError(f'Invalid email message: {exc}') from exc
    parts = [
        f'Subject: {message.get("subject", "")}',
        f'From: {message.get("from", "")}',
        f'To: {message.get("to", "")}',
        f'Date: {message.get("date", "")}',
    ]
    warnings: list[str] = []
    assets: list[dict] = []
    body = message.get_body(preferencelist=('plain', 'html'))
    if body is not None:
        try:
            content = body.get_content()
        except (LookupError, UnicodeDecodeError):
            payload = body.get_payload(decode=True) or b''
            content, decode_warnings = _decode_text(payload, 'message.txt', limit)
            warnings.extend(decode_warnings)
        if body.get_content_type() == 'text/html':
            content = _html_to_text(str(content))
        parts.extend(('', str(content).strip()))
    used_chars = sum(len(item) + 1 for item in parts)
    for attachment_index, attachment in enumerate(message.iter_attachments(), 1):
        name = attachment.get_filename() or ''
        payload = attachment.get_payload(decode=True) or b''
        if not name or not payload:
            continue
        if attachment_index > 50:
            warnings.append('Email attachment safety limit reached (50 files)')
            break
        if depth >= 2:
            warnings.append(
                f'Attachment {name} was skipped at the nested-email safety limit')
            continue
        if used_chars >= limit:
            warnings.append('Some email attachments were skipped after the text limit')
            break
        try:
            parsed = extract(payload, name, _depth=depth + 1)
            heading = f'## Attachment: {name}'
            remaining = max(0, limit - used_chars - len(heading) - 2)
            parsed_text = str(parsed['text'])
            attachment_text = parsed_text[:remaining]
            parts.extend(('', heading, attachment_text))
            used_chars += len(heading) + len(attachment_text) + 2
            if len(parsed_text) > remaining:
                warnings.append(
                    f'Attachment {name} was truncated at the email text limit')
            warnings.extend(parsed.get('warnings') or [])
            for asset in parsed.get('assets') or []:
                contextual = dict(asset)
                caption = str(contextual.get('caption') or '').strip()
                contextual['caption'] = (
                    f'Attachment {name}: {caption}' if caption
                    else f'Attachment: {name}')
                assets.append(contextual)
        except KnowledgeIngestError as exc:
            warnings.append(f'Attachment {name} was skipped: {exc}')
    return {'text': '\n'.join(parts)[:limit].strip(), 'kind': '.eml',
            'method': 'email-local', 'warnings': warnings, 'pages': 0,
            'assets': assets}


def _ocr_scanned_pdf(raw: bytes, page_limit: int) -> tuple[str, list[str]]:
    """Best-effort local OCR through PyMuPDF/Tesseract; never required."""
    warnings: list[str] = []
    try:
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf
        from lib.pdf_parser._common import PYMUPDF_LOCK
        with PYMUPDF_LOCK:
            doc = pymupdf.open(stream=raw, filetype='pdf')
            try:
                total = doc.page_count
                parts: list[str] = []
                for page_no in range(min(total, page_limit)):
                    page = doc[page_no]
                    text = ''
                    for language in ('chi_sim+eng', 'eng'):
                        try:
                            tp = page.get_textpage_ocr(
                                language=language, dpi=150, full=True)
                            text = page.get_text('text', textpage=tp) or ''
                            if text.strip():
                                break
                        except Exception as exc:
                            logger.debug('[Knowledge] OCR page %d (%s) failed: %s',
                                         page_no + 1, language, exc)
                    if text.strip():
                        parts.append(f'## Page {page_no + 1}\n\n{text.strip()}')
                if total > page_limit:
                    warnings.append(
                        f'OCR read {page_limit} of {total} pages; remaining pages were not OCRed')
                return '\n\n'.join(parts), warnings
            finally:
                doc.close()
    except Exception as exc:
        logger.info('[Knowledge] local PDF OCR unavailable: %s', exc)
        warnings.append('Scanned PDF OCR was unavailable; image-only pages may be missing')
        return '', warnings


def extract(raw: bytes, filename: str, *, _depth: int = 0) -> dict:
    """Extract a supported file to normalized text plus provenance metadata."""
    kind = detect_kind(raw, filename)
    limit = _limit_chars()
    canonical_name = (os.path.splitext(filename or 'document')[0] or 'document') + kind

    if kind in IMAGE_EXTENSIONS:
        try:
            asset = standalone_image(raw, filename)
        except KnowledgeImageError as exc:
            raise KnowledgeIngestError(str(exc)) from exc
        return {
            # OCR belongs to the visual proxy chunk below. Keeping a second
            # ordinary text chunk would duplicate ranking and could detach the
            # top hit from its original image.
            'text': '',
            'kind': kind,
            'method': 'image-local-ocr' if asset.get('ocr_text') else 'image-local',
            'warnings': [],
            'pages': 0,
            'assets': [asset],
        }

    if kind == '.pdf':
        from lib.pdf_parser.text import extract_pdf_text_with_meta, validate_pdf_bytes
        ok, pages, error = validate_pdf_bytes(raw)
        if not ok:
            raise KnowledgeIngestError(f'Invalid PDF: {error}')
        text, method = extract_pdf_text_with_meta(raw, max_chars=limit, mode='rich')
        text = text or ''
        warnings: list[str] = []
        visible = re.sub(r'\[[^\]]*error[^\]]*\]', '', text, flags=re.I).strip()
        is_scanned = len(visible) < max(80, pages * 40)
        if is_scanned:
            raw_limit = os.environ.get('TOFU_KNOWLEDGE_OCR_MAX_PAGES', '80')
            try:
                ocr_limit = max(1, min(int(raw_limit), 500))
            except (TypeError, ValueError) as exc:
                logger.debug('[Knowledge] invalid OCR page limit %r: %s',
                             raw_limit, exc)
                ocr_limit = 80
            ocr, ocr_warnings = _ocr_scanned_pdf(raw, ocr_limit)
            warnings.extend(ocr_warnings)
            if len(ocr.strip()) > len(visible):
                text, method = ocr, 'pymupdf-ocr'
        if text and method != 'pymupdf-ocr':
            text = _strip_repeated_pdf_margins(text)
        assets, visual_warnings = extract_pdf_assets(raw)
        warnings.extend(visual_warnings)
        if method == 'pymupdf-ocr' and text:
            page_sections = {
                int(match.group(1)): match.group(2).strip()[:20_000]
                for match in re.finditer(
                    r'^## Page (\d+)\s*\n(.*?)(?=^## Page \d+\s*\n|\Z)',
                    text, flags=re.M | re.S)
            }
            for asset in assets:
                if asset.get('kind') == 'page':
                    asset['ocr_text'] = page_sections.get(
                        int(asset.get('page') or 0), asset.get('ocr_text') or '')
        if (not text.strip() or method == 'error') and not assets:
            raise KnowledgeIngestError('No searchable text could be extracted from this PDF')
        if not text.strip() or method == 'error':
            text, method = '', 'pdf-visual-local'
        return {'text': text[:limit], 'kind': kind, 'method': method,
                'warnings': warnings, 'pages': pages, 'assets': assets}

    if kind == '.rtf':
        result = _extract_rtf(raw, limit)
        if not result['text'].strip():
            raise KnowledgeIngestError('No searchable text could be extracted from this RTF')
        return result

    if kind == '.eml':
        result = _email_text(raw, limit, depth=_depth)
        if not result['text'].strip():
            raise KnowledgeIngestError('No searchable text could be extracted from this email')
        return result

    if kind in _OPEN_DOCUMENT_EXTENSIONS:
        result = _open_document_text(raw, kind, limit)
        if not result['text'].strip():
            raise KnowledgeIngestError(
                'No searchable text could be extracted from this OpenDocument file')
        assets, visual_warnings = extract_package_assets(raw)
        result['assets'] = assets
        result['warnings'].extend(visual_warnings)
        return result

    if kind == '.epub':
        result = _epub_text(raw, limit)
        if not result['text'].strip():
            raise KnowledgeIngestError('No searchable text could be extracted from this EPUB')
        assets, visual_warnings = extract_package_assets(raw)
        result['assets'] = assets
        result['warnings'].extend(visual_warnings)
        return result

    if kind in _HTML_EXTENSIONS:
        text, warnings = _decode_text(raw, canonical_name, limit)
        text = _html_to_text(text)[:limit]
        if not text.strip():
            raise KnowledgeIngestError('No searchable text could be extracted from this HTML')
        return {'text': text, 'kind': kind, 'method': 'html-local',
                'warnings': warnings, 'pages': 1}

    if kind in ('.csv', '.tsv'):
        result = _delimited_text(raw, canonical_name, kind, limit)
        if not result['text'].strip():
            raise KnowledgeIngestError(
                'No searchable table content could be extracted')
        return result

    from lib.doc_parser import extract_document_text
    result = extract_document_text(
        raw, canonical_name, max_chars=limit, robust=True)
    text = str(result.get('text') or '')
    if not text.strip() or result.get('method') in ('error', 'unavailable', 'unsupported'):
        detail = '; '.join(str(w) for w in (result.get('warnings') or []))
        raise KnowledgeIngestError(detail or 'No searchable text could be extracted')
    assets: list[dict] = []
    visual_warnings: list[str] = []
    if kind in ('.docx', '.xlsx', '.pptx'):
        assets, visual_warnings = extract_package_assets(raw)
    return {
        'text': text,
        'kind': kind,
        'method': str(result.get('method') or 'text'),
        'warnings': [str(w) for w in (result.get('warnings') or [])]
                    + visual_warnings,
        'pages': int(result.get('totalPages') or 0),
        'assets': assets,
    }


__all__ = [
    'IMAGE_EXTENSIONS', 'KnowledgeIngestError', 'OFFICE_EXTENSIONS', 'PLAIN_EXTENSIONS',
    'PORTABLE_EXTENSIONS', 'SUPPORTED_EXTENSIONS', 'detect_kind', 'extract',
]
