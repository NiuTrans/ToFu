"""Standalone OCR kernel and process entry; no application imports.

Parent entry: ``recognize_image`` and ``recognize_pdf`` run against an injected
PyMuPDF module. Subprocess entry (``main``): argv names the job, stdin carries
the source bytes and stdout receives exactly one JSON completion record:
``{"text", "warnings", "failures", "error"}``. PyMuPDF's OCR call holds the
GIL for its whole Tesseract run, so the server only ever runs this kernel in
an owned child (``lib.knowledge.ocr``); the child owns a private MuPDF context
and runs one job on one thread, so it takes no MuPDF lock.
"""
from __future__ import annotations

import json
import sys

OCR_LANGUAGES = ('chi_sim+eng', 'eng')
OCR_DPI = 150
IMAGE_TEXT_MAX_CHARS = 20_000


def _note_failure(failures: dict[str, int], language: str, error: Exception) -> None:
    message = f'{language}: {type(error).__name__}: {str(error)[:4096]}'
    failures[message] = failures.get(message, 0) + 1


def recognize_page(page, failures: dict[str, int]) -> str:
    """OCR one page; ``eng`` runs only when the combined pass cannot run.

    Empty text is a result, not a failure: retrying it re-ran a full Tesseract
    pass for every blank page and recovered no text in production samples.
    """
    for language in OCR_LANGUAGES:
        try:
            text_page = page.get_textpage_ocr(language=language, dpi=OCR_DPI, full=True)
            return page.get_text('text', textpage=text_page) or ''
        except Exception as error:
            _note_failure(failures, language, error)
    return ''


def recognize_image(pdf_module, raw: bytes, filetype: str, failures: dict[str, int]) -> str:
    image_document = pdf_module.open(stream=raw, filetype=filetype)
    try:
        pdf_bytes = image_document.convert_to_pdf()
    finally:
        image_document.close()
    pdf = pdf_module.open(stream=pdf_bytes, filetype='pdf')
    try:
        return recognize_page(pdf[0], failures).strip()[:IMAGE_TEXT_MAX_CHARS]
    finally:
        pdf.close()


def recognize_pdf(
    pdf_module,
    raw: bytes,
    *,
    page_limit: int | None,
    max_chars: int,
    failures: dict[str, int],
) -> tuple[str, list[str]]:
    """OCR pages in order until the explicit page range or text budget is spent."""
    warnings: list[str] = []
    char_limit = max(1, int(max_chars))
    document = pdf_module.open(stream=raw, filetype='pdf')
    try:
        total = document.page_count
        requested_pages = total if page_limit is None else max(0, int(page_limit))
        pages_to_process = min(total, requested_pages)
        parts: list[str] = []
        used_chars = 0
        text_limit_reached = False
        for page_no in range(pages_to_process):
            if used_chars >= char_limit:
                break
            text = recognize_page(document[page_no], failures)
            if text.strip():
                rendered = f'## Page {page_no + 1}\n\n{text.strip()}'
                separator_chars = 2 if parts else 0
                remaining = max(0, char_limit - used_chars - separator_chars)
                if len(rendered) > remaining:
                    if remaining:
                        parts.append(rendered[:remaining])
                    used_chars = char_limit
                    text_limit_reached = True
                    warnings.append(
                        f'OCR stopped at the {char_limit:,}-character text budget')
                    break
                parts.append(rendered)
                used_chars += separator_chars + len(rendered)
                if used_chars >= char_limit and page_no + 1 < pages_to_process:
                    text_limit_reached = True
                    warnings.append(
                        f'OCR stopped at the {char_limit:,}-character text budget')
                    break
        if total > requested_pages and not text_limit_reached:
            warnings.append(
                f'OCR read {pages_to_process} of {total} pages; remaining pages were not OCRed')
        return '\n\n'.join(parts), warnings
    finally:
        document.close()


def _run(arguments: list[str], raw: bytes, failures: dict[str, int]) -> tuple[str, list[str]]:
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf
    if len(arguments) == 2 and arguments[0] == 'image':
        return recognize_image(pymupdf, raw, arguments[1], failures), []
    if len(arguments) == 3 and arguments[0] == 'pdf':
        page_limit = None if arguments[1] == '-' else int(arguments[1])
        return recognize_pdf(pymupdf, raw, page_limit=page_limit,
                             max_chars=int(arguments[2]), failures=failures)
    raise ValueError('OCR worker arguments are not understood; restart the Tofu server')


def main() -> int:
    completion = {'text': '', 'warnings': [], 'failures': {}, 'error': None}
    try:
        completion['text'], completion['warnings'] = _run(
            sys.argv[1:], sys.stdin.buffer.read(), completion['failures'])
    except Exception as error:
        # The completion record carries the failure to the parent, which raises it.
        print('main failed:', type(error).__name__, file=sys.stderr)
        completion['error'] = {'type': type(error).__name__, 'message': str(error)[:4096]}
    sys.stdout.buffer.write(json.dumps(completion).encode('utf-8'))
    sys.stdout.buffer.flush()
    return int(completion['error'] is not None)


if __name__ == '__main__':
    raise SystemExit(main())
