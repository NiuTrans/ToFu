"""Local extraction and normalization of visual knowledge evidence.

The public knowledge pipeline stores original image bytes.  Text extracted
from an image (caption/OCR/model description) is a *search proxy*, never a
replacement for the original evidence.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import zipfile

from lib.log import get_logger

logger = get_logger(__name__)

IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')

_MIME_SUFFIX = {
    'image/png': '.png',
    'image/jpeg': '.jpg',
    'image/gif': '.gif',
    'image/webp': '.webp',
    'image/bmp': '.bmp',
}
_OOXML_MEDIA_RE = re.compile(
    r'^(?:word/media|ppt/media|xl/media|pictures)/', re.IGNORECASE)


class KnowledgeImageError(ValueError):
    """A safe, user-facing image validation failure."""


def _bounded_env(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        return max(low, min(int(raw), high))
    except (TypeError, ValueError):
        logger.debug('[Knowledge] invalid %s=%r; using %d', name, raw, default)
        return default


def detect_image_mime(raw: bytes) -> str:
    """Return a supported raster MIME from magic bytes, or ``''``."""
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if raw.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if raw.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if raw.startswith(b'RIFF') and raw[8:12] == b'WEBP':
        return 'image/webp'
    if raw.startswith(b'BM'):
        return 'image/bmp'
    return ''


def inspect_image(raw: bytes, *, expected_mime: str = '') -> dict:
    """Validate image structure and return canonical immutable metadata."""
    mime = detect_image_mime(raw)
    if not mime:
        raise KnowledgeImageError('Unsupported or corrupt image')
    if expected_mime and mime != expected_mime:
        raise KnowledgeImageError('Image contents do not match their declared type')
    max_bytes = _bounded_env(
        'TOFU_KNOWLEDGE_MAX_ASSET_BYTES', 25 * 1024 * 1024,
        64 * 1024, 100 * 1024 * 1024)
    if len(raw) > max_bytes:
        raise KnowledgeImageError(
            f'Image exceeds the {max_bytes // 1048576} MB visual-asset limit')
    try:
        from PIL import Image
    except ImportError as exc:
        raise KnowledgeImageError('Local image support is unavailable') from exc
    try:
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            image.verify()
    except (OSError, ValueError) as exc:
        raise KnowledgeImageError(f'Invalid image data: {exc}') from exc
    max_pixels = _bounded_env(
        'TOFU_KNOWLEDGE_MAX_IMAGE_PIXELS', 40_000_000,
        1_000_000, 100_000_000)
    if width <= 0 or height <= 0 or width * height > max_pixels:
        raise KnowledgeImageError(
            f'Image dimensions exceed the {max_pixels:,}-pixel safety limit')
    return {
        'mime_type': mime,
        'suffix': _MIME_SUFFIX[mime],
        'width': int(width),
        'height': int(height),
        'size_bytes': len(raw),
        'sha256': hashlib.sha256(raw).hexdigest(),
    }


def _ocr_image(raw: bytes, mime_type: str) -> str:
    """Best-effort local OCR. Missing Tesseract is a normal empty result."""
    try:
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf
        from lib.pdf_parser._common import PYMUPDF_LOCK
        filetype = _MIME_SUFFIX[mime_type].lstrip('.').replace('jpg', 'jpeg')
        with PYMUPDF_LOCK:
            image_doc = pymupdf.open(stream=raw, filetype=filetype)
            try:
                pdf_bytes = image_doc.convert_to_pdf()
            finally:
                image_doc.close()
            pdf = pymupdf.open(stream=pdf_bytes, filetype='pdf')
            try:
                page = pdf[0]
                for language in ('chi_sim+eng', 'eng'):
                    try:
                        text_page = page.get_textpage_ocr(
                            language=language, dpi=150, full=True)
                        text = page.get_text('text', textpage=text_page).strip()
                        if text:
                            return text[:20_000]
                    except Exception as exc:
                        logger.debug(
                            '[Knowledge] image OCR (%s) unavailable: %s',
                            language, exc)
            finally:
                pdf.close()
    except Exception as exc:
        logger.debug('[Knowledge] image OCR setup unavailable: %s', exc)
    return ''


def standalone_image(raw: bytes, filename: str) -> dict:
    metadata = inspect_image(raw)
    return {
        **metadata,
        'raw': raw,
        'kind': 'image',
        'page': 0,
        'pages': [],
        'bbox': [],
        'caption': os.path.basename(filename or 'image'),
        'ocr_text': _ocr_image(raw, metadata['mime_type']),
        'description': '',
        'source': 'standalone',
    }


def _asset_from_raw(raw: bytes, **values) -> dict:
    metadata = inspect_image(raw)
    return {
        **metadata,
        'raw': raw,
        'kind': str(values.get('kind') or 'image'),
        'page': int(values.get('page') or 0),
        'pages': list(values.get('pages') or []),
        'bbox': list(values.get('bbox') or []),
        'caption': str(values.get('caption') or ''),
        'ocr_text': str(values.get('ocr_text') or ''),
        'description': '',
        'source': str(values.get('source') or 'embedded'),
    }


def extract_pdf_assets(raw: bytes) -> tuple[list[dict], list[str]]:
    """Extract captioned figures/tables and render image-only PDF pages."""
    warnings: list[str] = []
    assets: list[dict] = []
    seen_embedded_xrefs: set[int] = set()
    max_pages = _bounded_env('TOFU_KNOWLEDGE_VISUAL_MAX_PAGES', 80, 1, 500)
    max_assets = _bounded_env('TOFU_KNOWLEDGE_MAX_VISUAL_ASSETS', 160, 1, 1000)
    max_total_bytes = _bounded_env(
        'TOFU_KNOWLEDGE_MAX_VISUAL_BYTES', 160 * 1024 * 1024,
        1024 * 1024, 1024 * 1024 * 1024)
    total_bytes = 0
    byte_limit_reached = False

    def keep(candidate: dict) -> bool:
        nonlocal total_bytes, byte_limit_reached
        candidate_bytes = int(candidate.get('size_bytes') or 0)
        if total_bytes + candidate_bytes > max_total_bytes:
            byte_limit_reached = True
            return False
        assets.append(candidate)
        total_bytes += candidate_bytes
        return True

    try:
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf
        from lib.pdf_parser._common import PYMUPDF_LOCK
        from lib.pdf_parser.images import detect_and_clip_figures
        with PYMUPDF_LOCK:
            doc = pymupdf.open(stream=raw, filetype='pdf')
            try:
                total_pages = doc.page_count
                for page_index in range(min(total_pages, max_pages)):
                    if len(assets) >= max_assets or byte_limit_reached:
                        break
                    page = doc[page_index]
                    page_text = (page.get_text('text') or '').strip()
                    try:
                        figures = detect_and_clip_figures(
                            page, page_index, total_pages, doc=doc)
                    except Exception as exc:
                        logger.warning(
                            '[Knowledge] visual detection failed on page %d: %s',
                            page_index + 1, exc)
                        figures = []
                    for figure in figures:
                        if len(assets) >= max_assets or byte_limit_reached:
                            break
                        try:
                            image_raw = base64.b64decode(
                                str(figure.get('base64') or ''), validate=True)
                            source = str(figure.get('source') or 'figure_clip')
                            keep(_asset_from_raw(
                                image_raw,
                                kind='table' if source.startswith('table') else 'figure',
                                page=figure.get('page') or page_index + 1,
                                pages=figure.get('pages') or [page_index + 1],
                                bbox=figure.get('bbox') or [],
                                caption=figure.get('caption') or '',
                                ocr_text=page_text[:20_000],
                                source=source))
                        except (KnowledgeImageError, ValueError) as exc:
                            logger.debug(
                                '[Knowledge] skipped invalid PDF visual: %s', exc)
                    # Caption heuristics intentionally stay precise, but many
                    # reports contain meaningful uncaptioned diagrams. Preserve
                    # sufficiently large embedded raster objects as independent
                    # evidence too; repeated logos share an xref and are kept
                    # only once per document.
                    try:
                        embedded = page.get_images(full=True)
                    except Exception as exc:
                        logger.debug(
                            '[Knowledge] embedded PDF image listing failed: %s', exc)
                        embedded = []
                    captioned_boxes = [
                        pymupdf.Rect(figure.get('bbox'))
                        for figure in figures
                        if isinstance(figure.get('bbox'), list)
                        and len(figure['bbox']) == 4
                    ]
                    for image_info in embedded:
                        if len(assets) >= max_assets or byte_limit_reached:
                            break
                        try:
                            xref = int(image_info[0])
                            if xref <= 0 or xref in seen_embedded_xrefs:
                                continue
                            seen_embedded_xrefs.add(xref)
                            try:
                                image_rects = page.get_image_rects(xref)
                            except Exception as exc:
                                logger.debug(
                                    '[Knowledge] PDF image bounds unavailable '
                                    'for xref %s: %s', xref, exc)
                                image_rects = []
                            if any(
                                rect.get_area() > 0
                                and any((rect & box).get_area()
                                        >= rect.get_area() * 0.8
                                        for box in captioned_boxes)
                                for rect in image_rects
                            ):
                                # The captioned crop is the stronger evidence:
                                # it preserves the figure and its local label.
                                continue
                            extracted = doc.extract_image(xref) or {}
                            image_raw = extracted.get('image') or b''
                            try:
                                metadata = inspect_image(image_raw)
                            except KnowledgeImageError:
                                pix = pymupdf.Pixmap(doc, xref)
                                try:
                                    if pix.alpha or pix.colorspace is None:
                                        converted = pymupdf.Pixmap(
                                            pymupdf.csRGB, pix)
                                        try:
                                            image_raw = converted.tobytes('png')
                                        finally:
                                            converted = None
                                    else:
                                        image_raw = pix.tobytes('png')
                                finally:
                                    pix = None
                                metadata = inspect_image(image_raw)
                            if (metadata['width'] < 100
                                    or metadata['height'] < 60
                                    or metadata['size_bytes'] < 2_000):
                                continue
                            keep(_asset_from_raw(
                                image_raw, kind='image', page=page_index + 1,
                                pages=[page_index + 1],
                                caption=f'Embedded image on page {page_index + 1}',
                                ocr_text=page_text[:20_000],
                                source='embedded_pdf'))
                        except Exception as exc:
                            logger.debug(
                                '[Knowledge] skipped embedded PDF image: %s', exc)
                    # Preserve the original visual evidence for scanned or
                    # image-dominant pages even when no caption heuristic fires.
                    if (len(page_text) < 80 and len(assets) < max_assets
                            and not byte_limit_reached):
                        try:
                            pix = page.get_pixmap(dpi=130, alpha=False)
                            page_raw = pix.tobytes('jpeg')
                            keep(_asset_from_raw(
                                page_raw, kind='page', page=page_index + 1,
                                pages=[page_index + 1],
                                caption=f'Page {page_index + 1}',
                                ocr_text=page_text, source='scanned_page'))
                        except Exception as exc:
                            logger.debug(
                                '[Knowledge] scanned page render failed: %s', exc)
                if total_pages > max_pages:
                    warnings.append(
                        f'Visual extraction read {max_pages} of {total_pages} pages')
                if len(assets) >= max_assets:
                    warnings.append(
                        f'Visual extraction stopped at {max_assets} image assets')
                if byte_limit_reached:
                    warnings.append(
                        'Visual extraction stopped at the '
                        f'{max_total_bytes // 1048576} MB asset budget')
            finally:
                doc.close()
    except Exception as exc:
        logger.warning('[Knowledge] PDF visual extraction unavailable: %s', exc)
        warnings.append('PDF images could not be extracted; text indexing still succeeded')
    return assets, warnings


def extract_package_assets(raw: bytes) -> tuple[list[dict], list[str]]:
    """Extract raster media embedded in OOXML/OpenDocument packages."""
    assets: list[dict] = []
    warnings: list[str] = []
    max_assets = _bounded_env('TOFU_KNOWLEDGE_MAX_VISUAL_ASSETS', 160, 1, 1000)
    max_total_bytes = _bounded_env(
        'TOFU_KNOWLEDGE_MAX_VISUAL_BYTES', 160 * 1024 * 1024,
        1024 * 1024, 1024 * 1024 * 1024)
    total_bytes = 0
    byte_limit_reached = False
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for info in archive.infolist():
                name = info.filename.replace('\\', '/').lstrip('/')
                if (len(assets) >= max_assets or byte_limit_reached
                        or not _OOXML_MEDIA_RE.match(name)
                        or os.path.splitext(name)[1].lower() not in IMAGE_EXTENSIONS):
                    continue
                try:
                    image_raw = archive.read(info)
                    digest = hashlib.sha256(image_raw).hexdigest()
                    if digest in seen:
                        continue
                    asset = _asset_from_raw(
                        image_raw, kind='image', caption=os.path.basename(name),
                        source=name)
                    if total_bytes + len(image_raw) > max_total_bytes:
                        byte_limit_reached = True
                        break
                    assets.append(asset)
                    total_bytes += len(image_raw)
                    seen.add(digest)
                except (KnowledgeImageError, OSError, RuntimeError) as exc:
                    logger.debug(
                        '[Knowledge] skipped embedded image %s: %s', name, exc)
        if len(assets) >= max_assets:
            warnings.append(
                f'Embedded-image extraction stopped at {max_assets} assets')
        if byte_limit_reached:
            warnings.append(
                'Embedded-image extraction stopped at the '
                f'{max_total_bytes // 1048576} MB asset budget')
    except (zipfile.BadZipFile, OSError) as exc:
        logger.debug('[Knowledge] embedded-image extraction failed: %s', exc)
    return assets, warnings


def proxy_text(document_name: str, asset: dict) -> str:
    """Build the canonical searchable textual surrogate for one image."""
    page = int(asset.get('page') or 0)
    kind = str(asset.get('kind') or 'image')
    heading = f'Visual evidence: {kind}' + (f' on page {page}' if page else '')
    fields = [
        heading,
        f'Document: {document_name}',
        f'Caption: {str(asset.get("caption") or "").strip()}',
        f'OCR text: {str(asset.get("ocr_text") or "").strip()}',
        f'Visual description: {str(asset.get("description") or "").strip()}',
    ]
    return '\n'.join(field for field in fields if not field.endswith(': ')).strip()


def model_ready_image(raw: bytes, mime_type: str) -> tuple[bytes, str]:
    """Normalize an asset to provider-safe JPEG while bounding prompt size."""
    if mime_type in ('image/jpeg', 'image/png', 'image/gif', 'image/webp') \
            and len(raw) <= 1024 * 1024:
        return raw, mime_type
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as source:
            source.seek(0)
            image = source.copy()
            image.thumbnail((1800, 1800))
            if image.mode in ('RGBA', 'LA'):
                background = Image.new('RGB', image.size, 'white')
                alpha = image.getchannel('A')
                background.paste(image.convert('RGB'), mask=alpha)
                image = background
            elif image.mode != 'RGB':
                image = image.convert('RGB')
            limit = 1536 * 1024
            quality = 88
            encoded = b''
            for _attempt in range(5):
                output = io.BytesIO()
                image.save(output, format='JPEG', quality=quality, optimize=True)
                encoded = output.getvalue()
                if len(encoded) <= limit or max(image.size) <= 640:
                    break
                ratio = max(0.58, min(0.88, (limit / len(encoded)) ** 0.5))
                image = image.resize(
                    (max(1, int(image.width * ratio)),
                     max(1, int(image.height * ratio))), Image.LANCZOS)
                quality = max(68, quality - 5)
            return encoded, 'image/jpeg'
    except Exception as exc:
        raise KnowledgeImageError(f'Image could not be prepared for the model: {exc}') from exc


__all__ = [
    'IMAGE_EXTENSIONS', 'KnowledgeImageError', 'detect_image_mime',
    'extract_package_assets', 'extract_pdf_assets', 'inspect_image',
    'model_ready_image', 'proxy_text', 'standalone_image',
]
