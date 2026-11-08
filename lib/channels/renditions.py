"""Preview copies of attachments for the phone (docs/CHANNELS.md C10).

Responsibility: for an attachment whose suffix is in ``x-dxAttachments``
``renditionSources`` (pptx, docx, xlsx), make a PDF copy, and for
``slideImageSources`` (pptx) also one PNG per slide, so a phone can glance at
it without opening Office.

The only converter in this repository and environment is Tofu's slide
renderer (``lib.slides``), so the copies exist for a pptx exported from a Tofu
deck: one that sits beside its ``deck.pptd`` source and is at least as new as
that source and its page files (every Tofu edit re-exports the pptx). The
deck is rendered fresh to one PNG per page, and the PDF is made of those
pages (PyMuPDF). Anything else gets no copy, with the reason for the message.
A pptx is never re-imported into a deck to render it: that import drops
placeholders, groups and SmartArt, and a preview that misrepresents the file
is worse than none. Copies are never uploaded anywhere but the chat.

Entry points: ``converter_for`` (what would run, without running it),
``preview_plan`` (the same for a sender), ``render``. Dependencies: PyMuPDF
and ``lib.slides`` (both loaded lazily).
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lib.channels.model import DX_ATTACHMENTS
from lib.log import get_logger

logger = get_logger(__name__)

RENDITION_SOURCES = frozenset(DX_ATTACHMENTS['renditionSources'])
SLIDE_IMAGE_SOURCES = frozenset(DX_ATTACHMENTS['slideImageSources'])
DECK_MANIFEST = 'deck.pptd'
DECK_PAGES = 'pages'
NO_CONVERTER = 'Tofu 服务器上没有可用的文档转换器'
DECK_CHANGED = '它的 Tofu 幻灯片源文件在导出后改过，预览会与文件不符'


@dataclass
class Previews:
    """Preview copies of one original, in send order, or why there are none."""

    files: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ''


def _suffix(name: str) -> str:
    return Path(name).suffix.lower().lstrip('.')


def _deck(record: dict[str, Any]) -> tuple[str, bool]:
    """The ``deck.pptd`` beside a pptx (a real file), and whether the pptx is as new as its sources."""
    path = str((record.get('source') or {}).get('path') or '')
    if not path or _suffix(record['name']) != 'pptx':
        return '', False
    deck_dir = os.path.dirname(path)
    manifest = os.path.join(deck_dir, DECK_MANIFEST)
    try:
        source = os.lstat(manifest)
        exported = os.lstat(path).st_mtime_ns
    except OSError:
        return '', False
    if not stat.S_ISREG(source.st_mode):
        return '', False
    changed = [source.st_mtime_ns]
    try:
        with os.scandir(os.path.join(deck_dir, DECK_PAGES)) as pages:
            changed += [page.stat(follow_symlinks=False).st_mtime_ns for page in pages
                        if page.is_file(follow_symlinks=False)]
    except OSError:
        pass
    return manifest, exported >= max(changed)


def converter_for(record: dict[str, Any]) -> tuple[str, str]:
    """``('slides', '')`` when a preview copy can be made for one original, else ``('', reason)``."""
    if _suffix(record['name']) not in RENDITION_SOURCES:
        return '', ''
    manifest, current = _deck(record)
    if not manifest:
        return '', NO_CONVERTER
    return ('slides', '') if current else ('', DECK_CHANGED)


def preview_plan(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    """What each Office original will get when sent, for the sender (no conversion runs here)."""
    plan = []
    for record in records:
        suffix = _suffix(record['name'])
        if record.get('renditionOf') is not None or suffix not in RENDITION_SOURCES:
            continue
        converter, reason = converter_for(record)
        if not converter:
            why = ('its Tofu deck source changed after this pptx was exported' if reason == DECK_CHANGED
                   else 'the only converter here renders pptx files exported from a Tofu deck')
            plan.append({'name': record['name'],
                         'preview': f'none ({why}); the message will say that no preview copy was made'})
            continue
        copies = 'a PDF copy and one image per slide' if suffix in SLIDE_IMAGE_SOURCES else 'a PDF copy'
        plan.append({'name': record['name'],
                     'preview': f"{copies}, rendered from its Tofu deck (deck.pptd) when it is sent"})
    return plan


def _pdf_from_images(images: list[str], pdf: str) -> str:
    import pymupdf
    with pymupdf.open() as document:
        for image in images:
            with pymupdf.open(image) as picture:
                with pymupdf.open('pdf', picture.convert_to_pdf()) as page:
                    document.insert_pdf(page)
        document.save(pdf)
    return pdf


def _deck_previews(manifest: str, stem: str, out_dir: str) -> tuple[str, list[str]]:
    from lib.slides.pptd import parse_deck
    from lib.slides.render_png import render_previews
    rendered = render_previews(parse_deck(manifest), os.path.abspath(os.path.join(out_dir, 'deck')))
    if not rendered['ok'] or not rendered['pages']:
        raise RuntimeError(f'the slide renderer failed {len(rendered["failed"])} page(s)')
    pages = [entry['png'] for entry in sorted(rendered['pages'], key=lambda entry: entry['index'])]
    width = len(str(len(pages)))
    images = []
    for number, page in enumerate(pages, 1):
        target = os.path.join(out_dir, f'{stem}-{number:0{width}d}.png')
        os.replace(page, target)
        images.append(target)
    return _pdf_from_images(images, os.path.join(out_dir, stem + '.pdf')), images


def render(record: dict[str, Any], original: str, out_dir: str) -> Previews:
    """Preview copies of the original (verified bytes at ``original``) in ``out_dir``."""
    converter, reason = converter_for(record)
    if not converter:
        return Previews(reason=reason or NO_CONVERTER)
    stem = Path(record['name']).stem
    try:
        os.makedirs(out_dir, exist_ok=True)
        pdf, images = _deck_previews(_deck(record)[0], stem, out_dir)
    except Exception as error:
        logger.warning('[Channels] preview copy of %s failed: %s', record['name'], type(error).__name__)
        return Previews(reason='幻灯片预览渲染失败')
    files = [{'name': stem + '.pdf', 'path': pdf, 'kind': 'file', 'type': 'application/pdf'}]
    if _suffix(record['name']) in SLIDE_IMAGE_SOURCES:
        files += [{'name': os.path.basename(path), 'path': path, 'kind': 'image', 'type': 'image/png'}
                  for path in images]
    return Previews(files=files)


__all__ = ['DECK_CHANGED', 'NO_CONVERTER', 'Previews', 'RENDITION_SOURCES', 'SLIDE_IMAGE_SOURCES',
           'converter_for', 'preview_plan', 'render']
