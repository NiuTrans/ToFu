"""lib/doc_parser/_legacy.py — Legacy binary Office (97-2003) text extractors.

Provides:
  - _extract_doc_legacy  (.doc, via olefile → binary-scan fallback)
  - _extract_xls_legacy  (.xls, via xlrd)
  - _extract_ppt_legacy  (.ppt, via olefile → binary-scan fallback)

All optional dependencies are imported lazily inside each extractor so that
importing this module never hard-fails when a backend package is missing.
"""

from lib.log import get_logger

from lib.doc_parser._plain import _binary_text_extract

logger = get_logger(__name__)


def _extract_doc_legacy(file_bytes: bytes, limit: int) -> dict:
    """Extract text from legacy .doc (Word 97-2003) files.

    Uses olefile to read the raw OLE2 stream and decode Word document text.
    Falls back to basic binary text extraction if olefile is unavailable.
    """
    warnings = []

    # Strategy 1: olefile — read the WordDocument stream
    try:
        import io

        import olefile

        ole = olefile.OleFileIO(io.BytesIO(file_bytes))
        # Word stores text in the 'WordDocument' stream; but the actual plaintext
        # is easier to extract from the '1Table' / '0Table' streams.
        # A simpler approach: read all streams and extract printable text.
        text_parts = []
        for stream_name in ['WordDocument', '1Table', '0Table']:
            if ole.exists(stream_name):
                try:
                    raw = ole.openstream(stream_name).read()
                    # Try UTF-16LE decode (Word's native encoding for text runs)
                    try:
                        decoded = raw.decode('utf-16-le', errors='ignore')
                        # Filter to printable chars
                        cleaned = ''.join(c if c.isprintable() or c in '\n\r\t' else ' ' for c in decoded)
                        if len(cleaned.strip()) > 50:
                            text_parts.append(cleaned)
                    except Exception as e:
                        logger.debug('[DocParser] OLE stream decode failed: %s', e)
                except Exception as e:
                    logger.debug('[DocParser] OLE stream read failed: %s', e)
        ole.close()

        if text_parts:
            # Prefer the longest extracted text
            text = max(text_parts, key=len)
            # Clean up: collapse whitespace runs, normalize line endings
            import re
            text = re.sub(r'[^\S\n]+', ' ', text)
            text = re.sub(r'\n{3,}', '\n\n', text)
            text = text.strip()
            if len(text) > limit:
                text = text[:limit]
                warnings.append(f'Text truncated at {limit:,} chars')
            logger.info('[DocParser] Extracted .doc via olefile: %s chars', f'{len(text):,}')
            return {
                'text': text,
                'textLength': len(text),
                'totalPages': max(1, len(text) // 3000),
                'isScanned': False,
                'method': 'olefile (.doc)',
                'warnings': warnings,
            }
    except ImportError:
        logger.debug('[DocParser] olefile not installed, trying binary fallback for .doc')
    except Exception as e:
        logger.warning('[DocParser] olefile extraction failed for .doc: %s', e)

    # Strategy 2: Binary grep — extract UTF-16LE / ASCII strings from raw bytes
    text = _binary_text_extract(file_bytes, limit)
    if text:
        warnings.append('Extracted via binary text scan (quality may vary)')
        logger.info('[DocParser] Extracted .doc via binary scan: %s chars', f'{len(text):,}')
        return {
            'text': text,
            'textLength': len(text),
            'totalPages': max(1, len(text) // 3000),
            'isScanned': False,
            'method': 'binary-scan (.doc)',
            'warnings': warnings,
        }

    return {
        'text': '[Could not extract text from .doc file — try converting to .docx]',
        'textLength': 0,
        'totalPages': 1,
        'isScanned': False,
        'method': 'unsupported',
        'warnings': ['Legacy .doc text extraction failed'],
    }


def _extract_xls_legacy(file_bytes: bytes, limit: int) -> dict:
    """Extract text from legacy .xls (Excel 97-2003) files using xlrd."""
    try:
        import xlrd
    except ImportError:
        logger.warning('[DocParser] xlrd not installed, cannot parse .xls')
        return {
            'text': '[xlrd not installed — run: pip install xlrd]',
            'textLength': 0,
            'totalPages': 0,
            'isScanned': False,
            'method': 'unavailable',
            'warnings': ['xlrd not installed'],
        }

    warnings = []
    try:
        wb = xlrd.open_workbook(file_contents=file_bytes)
    except Exception as e:
        logger.error('[DocParser] Failed to open .xls: %s', e, exc_info=True)
        return {
            'text': f'[Failed to parse .xls: {e}]',
            'textLength': 0,
            'totalPages': 0,
            'isScanned': False,
            'method': 'error',
            'warnings': [str(e)],
        }

    parts = []
    total_chars = 0

    for si in range(wb.nsheets):
        ws = wb.sheet_by_index(si)
        sheet_parts = [f'## Sheet: {ws.name}']
        rows_data = []
        for ri in range(min(ws.nrows, 1001)):
            cells = []
            for ci in range(ws.ncols):
                cell = ws.cell(ri, ci)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        dt = xlrd.xldate_as_datetime(cell.value, wb.datemode)
                        cells.append(dt.strftime('%Y-%m-%d %H:%M:%S').rstrip(' 00:00:00'))
                    except (ValueError, TypeError, OverflowError) as e:
                        # xlrd raises XLDateError (a ValueError subclass) for
                        # malformed dates; ValueError covers it portably.
                        logger.debug('[DocParser] xldate convert failed for %r: %s',
                                     cell.value, e)
                        cells.append(str(cell.value))
                elif cell.ctype == xlrd.XL_CELL_NUMBER:
                    # Show integers without .0
                    v = cell.value
                    cells.append(str(int(v)) if v == int(v) else str(v))
                elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                    cells.append('TRUE' if cell.value else 'FALSE')
                else:
                    cells.append(str(cell.value) if cell.value else '')
            rows_data.append('| ' + ' | '.join(c.replace('|', '\\|') for c in cells) + ' |')

        if ws.nrows > 1001:
            warnings.append(f'Sheet "{ws.name}" truncated at 1000 rows')

        if rows_data:
            header = rows_data[0]
            ncols = ws.ncols
            separator = '| ' + ' | '.join(['---'] * ncols) + ' |'
            table_md = header + '\n' + separator
            if len(rows_data) > 1:
                table_md += '\n' + '\n'.join(rows_data[1:])
            sheet_parts.append(table_md)

        sheet_text = '\n'.join(sheet_parts)
        total_chars += len(sheet_text)
        if total_chars > limit:
            parts.append('\n[…truncated]')
            break
        parts.append(sheet_text)

    text = '\n\n---\n\n'.join(parts)
    logger.info('[DocParser] Extracted .xls: %d sheets, %s chars',
                wb.nsheets, f'{len(text):,}')

    return {
        'text': text,
        'textLength': len(text),
        'totalPages': wb.nsheets,
        'isScanned': False,
        'method': 'xlrd (.xls)',
        'warnings': warnings,
    }


def _extract_ppt_legacy(file_bytes: bytes, limit: int) -> dict:
    """Extract text from legacy .ppt (PowerPoint 97-2003) files.

    Uses olefile to read the PowerPoint Document stream and extract text records.
    """
    warnings = []

    try:
        import io
        import struct

        import olefile
    except ImportError:
        logger.warning('[DocParser] olefile not installed, cannot parse .ppt')
        return {
            'text': '[olefile not installed — run: pip install olefile]',
            'textLength': 0,
            'totalPages': 0,
            'isScanned': False,
            'method': 'unavailable',
            'warnings': ['olefile not installed'],
        }

    try:
        ole = olefile.OleFileIO(io.BytesIO(file_bytes))
        # PPT stores content in 'PowerPoint Document' stream
        if not ole.exists('PowerPoint Document'):
            ole.close()
            # Fallback to binary extraction
            text = _binary_text_extract(file_bytes, limit)
            if text:
                warnings.append('Extracted via binary text scan')
                return {
                    'text': text,
                    'textLength': len(text),
                    'totalPages': max(1, len(text) // 1500),
                    'isScanned': False,
                    'method': 'binary-scan (.ppt)',
                    'warnings': warnings,
                }
            return {
                'text': '[Could not find PowerPoint content in .ppt file]',
                'textLength': 0,
                'totalPages': 0,
                'isScanned': False,
                'method': 'error',
                'warnings': ['PowerPoint Document stream not found'],
            }

        raw = ole.openstream('PowerPoint Document').read()
        ole.close()

        # Parse PPT binary records — TextBytesAtom (0x0FA8) and TextCharsAtom (0x0FA0)
        # contain the actual slide text.
        text_parts = []
        offset = 0
        while offset < len(raw) - 8:
            struct.unpack_from('<H', raw, offset)[0]
            rec_type = struct.unpack_from('<H', raw, offset + 2)[0]
            rec_len = struct.unpack_from('<I', raw, offset + 4)[0]
            offset += 8
            if offset + rec_len > len(raw):
                break
            if rec_type == 0x0FA0:  # TextCharsAtom — UTF-16LE text
                try:
                    text_parts.append(raw[offset:offset + rec_len].decode('utf-16-le', errors='ignore'))
                except Exception as e:
                    logger.debug('[DocParser] PPT TextCharsAtom decode failed: %s', e)
            elif rec_type == 0x0FA8:  # TextBytesAtom — ASCII/Latin-1 text
                try:
                    text_parts.append(raw[offset:offset + rec_len].decode('latin-1', errors='ignore'))
                except Exception as e:
                    logger.debug('[DocParser] PPT TextBytesAtom decode failed: %s', e)
            offset += rec_len

        if text_parts:
            # Join with newlines, clean up
            import re
            text = '\n'.join(t.strip() for t in text_parts if t.strip())
            text = re.sub(r'\n{3,}', '\n\n', text).strip()
            if len(text) > limit:
                text = text[:limit]
                warnings.append(f'Text truncated at {limit:,} chars')
            n_slides = max(1, text.count('\n\n') + 1)  # rough estimate
            logger.info('[DocParser] Extracted .ppt: ~%d text blocks, %s chars',
                        len(text_parts), f'{len(text):,}')
            return {
                'text': text,
                'textLength': len(text),
                'totalPages': n_slides,
                'isScanned': False,
                'method': 'olefile (.ppt)',
                'warnings': warnings,
            }

    except Exception as e:
        logger.warning('[DocParser] olefile extraction failed for .ppt: %s', e)

    # Fallback to binary extraction
    text = _binary_text_extract(file_bytes, limit)
    if text:
        warnings.append('Extracted via binary text scan (quality may vary)')
        return {
            'text': text,
            'textLength': len(text),
            'totalPages': max(1, len(text) // 1500),
            'isScanned': False,
            'method': 'binary-scan (.ppt)',
            'warnings': warnings,
        }

    return {
        'text': '[Could not extract text from .ppt file — try converting to .pptx]',
        'textLength': 0,
        'totalPages': 0,
        'isScanned': False,
        'method': 'unsupported',
        'warnings': ['Legacy .ppt text extraction failed'],
    }
