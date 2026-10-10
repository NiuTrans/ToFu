"""lib/slides/templates.py — user-uploaded PPTX templates (A7).

「用我的模板生成 PPT」: the user uploads a brand deck; ``import_pptx``
already reverse-engineers its visual language (palette ramp + major/minor
fonts + page geometry) into PPTD theme tokens. This module owns the two
sides of that seam:

  * :func:`import_template` — upload → bounded storage → import → ONE
    ``template.json`` (theme tokens + size + font-substitution map).
    Fonts outside the license-audited registry are substituted to the
    nearest registered face and the mapping is REPORTED, never silent —
    the export gate embeds registered faces only.
  * :func:`load_template_theme` — the design stage's read side: tokens +
    geometry for one template id, owner-fenced.

The deck dir the importer writes is kept beside the manifest (a future
"clone this layout" path), but produce_slides consumes ONLY the theme.
"""

from __future__ import annotations

import os
import re
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['import_template', 'load_template_theme', 'MAX_TEMPLATE_BYTES',
           'templates_root']

MAX_TEMPLATE_BYTES = 32 * 1024 * 1024
_TEMPLATE_ID_RE = re.compile(r'^tpl_[A-Za-z0-9_-]{1,40}$')


def templates_root() -> str:
    from lib.slides.engine import slides_root
    root = os.path.join(slides_root(), 'templates')
    os.makedirs(root, exist_ok=True)
    return root


def _normalise_fonts(theme_tokens: dict) -> dict:
    """Map theme-token families onto the licensed registry.

    Returns ``{original_family: resolved_family}`` only for families that
    CHANGED. Registry resolution (id / canonical family / audited alias) is
    free; unresolvable sans-ish names fall to MiSans, serif-ish to Noto
    Serif SC — a substituted face embeds, an invented one fails the export
    gate.
    """
    import lib.design_sys.fonts as _fonts
    styles = (theme_tokens.get('textStyles') or {})
    substitutions: dict[str, str] = {}
    for role, style in styles.items():
        if not isinstance(style, dict):
            continue
        family = str(style.get('fontFamily') or '').strip()
        if not family:
            continue
        face = _fonts.resolve_font(family)
        if face is None:
            serif_hint = any(k in family.lower()
                             for k in ('serif', 'song', '宋', 'ming', '明'))
            face = _fonts.get_font(
                'noto-serif-sc' if serif_hint else 'misans')
        if face is not None and face.family != family:
            substitutions[family] = face.family
            style['fontFamily'] = face.family
    return substitutions


def import_template(pptx_path: str, *, title: str = '',
                    owner_user_id: int) -> dict:
    """Import one uploaded PPTX as a reusable visual template.

    Raises ValueError with a user-meaningful message on contract
    violations (size, unreadable pptx); infra errors propagate. Returns
    ``{'template_id', 'title', 'theme', 'size', 'source_pages',
    'font_substitutions'}``.
    """
    if int(owner_user_id or 0) < 1:
        raise ValueError('template import requires an explicit owner user_id')
    size_bytes = os.path.getsize(pptx_path)
    if not 0 < size_bytes <= MAX_TEMPLATE_BYTES:
        raise ValueError(
            f'template file is {size_bytes} bytes; limit is '
            f'{MAX_TEMPLATE_BYTES}')

    import hashlib
    digest = hashlib.sha256(
        f'{owner_user_id}:{time.time()}:{pptx_path}'.encode()).hexdigest()[:10]
    template_id = f'tpl_{digest}'
    dest_dir = os.path.join(templates_root(), template_id)
    os.makedirs(dest_dir, exist_ok=True)
    import shutil
    source_path = os.path.join(dest_dir, 'source.pptx')
    shutil.copy2(pptx_path, source_path)

    from lib.slides.import_pptx import import_pptx
    report = import_pptx(source_path, os.path.join(dest_dir, 'deck'),
                         title=title)
    theme_tokens = dict(report['theme'] or {})
    substitutions = _normalise_fonts(theme_tokens)
    import yaml
    with open(report['manifest'], encoding='utf-8') as f:
        manifest = yaml.safe_load(f)
    size = [int(v) for v in (manifest.get('size') or [1280, 720])]
    record = {
        'template_id': template_id,
        'owner_user_id': int(owner_user_id),
        'title': str(title or manifest.get('title') or '')[:160],
        'theme': theme_tokens,
        'size': size,
        'source_pages': int(report['pages']),
        'font_substitutions': substitutions,
        'created_at': round(time.time(), 3),
    }
    from lib.json_store import write_json_atomic
    write_json_atomic(os.path.join(dest_dir, 'template.json'), record,
                      sort_keys=True)
    logger.info('[Slides:template] %s imported (%d pages, %d font '
                'substitution(s))', template_id, report['pages'],
                len(substitutions))
    return {'template_id': template_id, 'title': record['title'],
            'theme': theme_tokens, 'size': size,
            'source_pages': record['source_pages'],
            'font_substitutions': substitutions}


def load_template_theme(template_id: str, *, owner_user_id: int) -> dict | None:
    """Read one template's theme tokens + geometry (owner-fenced).

    Returns ``{'theme', 'size', 'title'}`` or None (unknown/foreign/invalid
    id — the design stage then falls back to the curated theme path).
    """
    template_id = str(template_id or '').strip()
    if not _TEMPLATE_ID_RE.fullmatch(template_id):
        return None
    from lib.json_store import read_json
    record = read_json(
        os.path.join(templates_root(), template_id, 'template.json'),
        default=None)
    if not isinstance(record, dict):
        return None
    if int(record.get('owner_user_id') or 0) != int(owner_user_id):
        return None
    theme = record.get('theme')
    if not isinstance(theme, dict) or not theme.get('colors'):
        return None
    return {'theme': theme,
            'size': record.get('size') or [1280, 720],
            'title': str(record.get('title') or ''),
            'font_substitutions': dict(
                record.get('font_substitutions') or {})}
