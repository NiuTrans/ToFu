"""Standalone PDF render kernel and binary file writer; no application imports.

Parent entry: render_document into a page sink. Subprocess entry: stdin PDF,
stdout length-prefixed JPEGs followed by a zero-length JSON completion record.
Only the explicit caller page budget can exclude pages before allocation.
"""
from __future__ import annotations

import json
import sys

FRAME_HEADER_BYTES = 8
COMPLETION_MAX_BYTES = 65536


class PdfPageLimitExceeded(ValueError):
    """The document exceeds the caller's pre-render page budget."""


def render_document(pdf_module, pdf_bytes, *, dpi, max_pages, sink, abort_check=None):
    doc = pdf_module.open(stream=pdf_bytes, filetype='pdf')
    try:
        total = len(doc)
        if max_pages is not None and total > max(0, int(max_pages)):
            raise PdfPageLimitExceeded(
                f'PDF has {total} pages; VLM page limit is {int(max_pages)}')
        for index in range(total):
            if abort_check is not None:
                abort_check()
            image = doc[index].get_pixmap(dpi=dpi).tobytes('jpeg')
            sink.append(image)
        return total
    finally:
        doc.close()


class _FrameSink:
    def append(self, image):
        if not image:
            raise ValueError('PDF renderer returned an empty JPEG')
        sys.stdout.buffer.write(len(image).to_bytes(FRAME_HEADER_BYTES, 'big'))
        sys.stdout.buffer.write(image)


def main():
    completion = {'pages': 0, 'error': None}
    try:
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf
        completion['pages'] = render_document(
            pymupdf, sys.stdin.buffer.read(), dpi=int(sys.argv[1]),
            max_pages=None if sys.argv[2] == '-' else int(sys.argv[2]), sink=_FrameSink())
    except Exception as error:
        print('main failed:', type(error).__name__, file=sys.stderr)
        completion['error'] = {'type': type(error).__name__, 'message': str(error)[:4096]}
    sys.stdout.buffer.write(bytes(FRAME_HEADER_BYTES))
    sys.stdout.buffer.write(json.dumps(completion).encode('utf-8'))
    sys.stdout.buffer.flush()
    return int(completion['error'] is not None)


if __name__ == '__main__':
    raise SystemExit(main())
