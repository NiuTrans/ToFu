"""Request-shape contract for slide production jobs.

Responsibility: define the finite page-count and supported page-geometry
surface shared by task admission and the checkpointed recipe. Rendering and
PPTD parsing retain their own bounded support for imported decks.
"""

from __future__ import annotations

from itertools import islice

DEFAULT_SLIDE_PAGES = 12
MIN_SLIDE_PAGES = 3
MAX_SLIDE_PAGES = 20
DEFAULT_SLIDE_SIZE = (1280, 720)
ALLOWED_SLIDE_SIZES = (
    DEFAULT_SLIDE_SIZE,
    (960, 540),
    (720, 540),
)
MAX_SLIDE_TOPIC_CHARS = 4000
MAX_SLIDE_STYLE_CHARS = 2000
MAX_SLIDE_MODEL_CHARS = 256
MAX_SLIDE_IMAGE_REFERENCES = 64
MAX_SLIDE_IMAGE_REFERENCE_CHARS = 4096

_PAGE_TYPES = frozenset(
    ('cover', 'table_of_contents', 'chapter', 'content', 'final'))
_BRIEF_TEXT_LIMITS = {
    'purpose': 600,
    'key_message': 600,
    'layout_hint': 600,
    'content_notes': 4000,
    'asset_prompt': 1200,
    'asset_semantic_target': 280,
    'layout_archetype': 64,
    'visual_modality': 64,
    'visual_anchor': 280,
    'handoff': 280,
}

__all__ = [
    'ALLOWED_SLIDE_SIZES',
    'DEFAULT_SLIDE_PAGES',
    'DEFAULT_SLIDE_SIZE',
    'MAX_SLIDE_PAGES',
    'MAX_SLIDE_MODEL_CHARS',
    'MAX_SLIDE_IMAGE_REFERENCES',
    'MAX_SLIDE_STYLE_CHARS',
    'MAX_SLIDE_TOPIC_CHARS',
    'MIN_SLIDE_PAGES',
    'normalise_slide_page_count',
    'normalise_slide_briefs',
    'normalise_slide_image_references',
    'normalise_slide_model',
    'normalise_slide_size',
    'normalise_slide_style',
    'normalise_slide_topic',
]


def normalise_slide_page_count(value) -> int:
    """Clamp caller page count to the product's finite 3..20 contract."""
    try:
        pages = int(value or DEFAULT_SLIDE_PAGES)
    except (TypeError, ValueError, OverflowError):
        pages = DEFAULT_SLIDE_PAGES
    return max(MIN_SLIDE_PAGES, min(MAX_SLIDE_PAGES, pages))


def normalise_slide_size(value) -> tuple[int, int]:
    """Return one supported geometry or reject before allocating a browser."""
    if value is None:
        return DEFAULT_SLIDE_SIZE
    if isinstance(value, str):
        candidates = {
            f'{width}x{height}': (width, height)
            for width, height in ALLOWED_SLIDE_SIZES
        }
        size = candidates.get(value.strip().lower())
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            size = (int(value[0]), int(value[1]))
        except (TypeError, ValueError, OverflowError):
            size = None
    else:
        size = None
    if size not in ALLOWED_SLIDE_SIZES:
        allowed = ', '.join(f'{width}x{height}'
                            for width, height in ALLOWED_SLIDE_SIZES)
        raise ValueError(f'unsupported slide size; expected one of {allowed}')
    return size


def _bounded_request_text(value, *, name: str, maximum: int,
                          required: bool = False) -> str:
    text = str(value or '').strip()
    if required and not text:
        raise ValueError(f'slide {name} is required')
    if len(text) > maximum:
        raise ValueError(
            f'slide {name} exceeds the {maximum}-character limit')
    return text


def normalise_slide_topic(value) -> str:
    return _bounded_request_text(
        value, name='topic', maximum=MAX_SLIDE_TOPIC_CHARS, required=True)


def normalise_slide_style(value) -> str:
    return _bounded_request_text(
        value, name='style', maximum=MAX_SLIDE_STYLE_CHARS)


def normalise_slide_model(value) -> str:
    return _bounded_request_text(
        value, name='model', maximum=MAX_SLIDE_MODEL_CHARS)


def normalise_slide_briefs(values, *, maximum: int) -> list[dict]:
    """Bound model-authored page fields before per-page prompt fan-out."""
    out = []
    for raw in (values or [])[:maximum]:
        if not isinstance(raw, dict):
            continue
        page = dict(raw)
        page_type = str(page.get('pageType') or 'content').strip().lower()
        page['pageType'] = page_type if page_type in _PAGE_TYPES else 'content'
        for field, limit in _BRIEF_TEXT_LIMITS.items():
            if field in page:
                page[field] = str(page.get(field) or '').strip()[:limit]
        out.append(page)
    return out


def normalise_slide_image_references(values) -> tuple[list[str], list[str]]:
    """Copy only a finite caller-reference prefix into durable task context."""
    if values is None:
        return [], []
    if isinstance(values, (str, bytes)):
        values = [values]
    try:
        sampled = list(islice(iter(values), MAX_SLIDE_IMAGE_REFERENCES + 1))
    except TypeError as exc:
        raise ValueError('slide image references must be iterable') from exc
    findings = []
    if len(sampled) > MAX_SLIDE_IMAGE_REFERENCES:
        findings.append(
            f'caller image reference limit {MAX_SLIDE_IMAGE_REFERENCES} '
            'reached; remaining references were omitted')
        sampled = sampled[:MAX_SLIDE_IMAGE_REFERENCES]
    out = []
    for raw in sampled:
        reference = str(raw or '').strip()
        if not reference:
            continue
        if len(reference) > MAX_SLIDE_IMAGE_REFERENCE_CHARS:
            findings.append('caller image reference exceeded the '
                            '4096-character limit and was omitted')
            continue
        out.append(reference)
    return out, findings
