"""lib/slides/theme_ops.py — deck-level deterministic restyling.

「换个主题 / 换成深色 / 字体换成衬线」 is a TOKEN swap, not a re-authoring
job: the deck's elements reference ``$primary``/``$title`` tokens precisely
so the whole visual language can change without touching one page. This
module owns the three zero-LLM deck-level ops:

  * ``apply_deck_theme``   — bind a different curated theme (palette +
                             typeface pairing + table defaults), exactly the
                             tokens the design stage would have written.
  * ``apply_deck_palette`` — override individual palette slots.
  * ``apply_deck_fonts``   — swap typefaces per textStyle role (licensed
                             registry faces only, staged before render).

Every op rewrites the manifest atomically, re-renders ALL previews (one
browser boot) and re-exports the PPTX; the result is re-parsed and
re-validated, and any failure restores the pre-op manifest. The token
assembly (:func:`build_theme_tokens`) is the SAME function the recipe's
design stage consumes — one source of truth, so a restyled deck and a
freshly-designed one can never drift into two dialects of "theme".
"""

from __future__ import annotations

import os

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['build_theme_tokens', 'stage_theme_fonts', 'apply_deck_theme',
           'apply_deck_palette', 'apply_deck_fonts', 'apply_deck_restyle',
           'PALETTE_SLOTS', 'FONT_STYLE_ROLES']

#: Theme color slots the deck manifest carries (the design stage's set).
PALETTE_SLOTS = frozenset(
    ('bg', 'ink', 'primary', 'accent', 'muted', 'hairline'))

#: textStyle roles a font swap may retarget.
FONT_STYLE_ROLES = frozenset(('title', 'body', 'caption', 'bignum'))

_MAX_PALETTE_ENTRIES = 12
_HEX_RE_LEN = (7, 9)   # '#RRGGBB' / '#RRGGBBAA'


def stage_theme_fonts(theme) -> list:
    """Pre-warm every weight of a theme's display/body/latin faces."""
    import lib.design_sys.fonts as _fonts
    staged = []
    for role in ('display', 'body', 'latin'):
        face = _fonts.get_font(theme.fonts.get(role, ''))
        if face:
            for src in face.sources:
                if _fonts.ensure_font(face.id, src.weight):
                    staged.append(f'{face.id}-w{src.weight}')
    return staged


def build_theme_tokens(theme) -> dict:
    """The manifest ``theme`` block for a curated Theme — the design
    stage's exact token set (colors + textStyles + tableStyles)."""
    import lib.design_sys.fonts as _fonts
    c = theme.colors
    display_f = _fonts.get_font(theme.fonts['display'])
    body_f = _fonts.get_font(theme.fonts['body'])
    latin_f = _fonts.get_font(theme.fonts['latin'])
    return {
        'colors': {'bg': c['bg'], 'ink': c['ink'], 'primary': c['primary'],
                   'accent': c['accent'], 'muted': c['muted'],
                   'hairline': c['hairline']},
        'textStyles': {
            'title': {'fontSize': 40, 'color': '$primary', 'bold': True,
                      'fontFamily': display_f.family if display_f else 'MiSans',
                      'lineHeight': 1.2},
            'body': {'fontSize': 18, 'color': '$ink',
                     'fontFamily': body_f.family if body_f else 'MiSans',
                     'lineHeight': 1.5},
            'caption': {'fontSize': 12, 'color': '$muted', 'letterSpacing': 2,
                        'fontFamily': body_f.family if body_f else 'MiSans'},
            'bignum': {'fontSize': 88, 'color': '$accent', 'bold': True,
                       'fontFamily': latin_f.family if latin_f else 'MiSans'},
        },
        'tableStyles': {
            'default': {
                'firstRowStyle': {
                    'fill': {'type': 'solid', 'color': '$primary'},
                    'color': c['bg'], 'bold': True},
                'cellStyle': {'border': {'style': 'solid', 'width': 1,
                                         'color': '$hairline'},
                              'align': ['left', 'middle']},
                'bodyStyles': [],
            },
        },
    }


def _rewrite_manifest(deck_dir: str, mutate, change_desc: dict) -> dict:
    """Parse → mutate the manifest theme block → validate → render → export.

    The manifest write is atomic and the pre-op manifest text is restored
    on ANY downstream failure (a restyle must never strand a deck between
    two themes).
    """
    import yaml
    from lib.json_store import write_text_atomic
    from lib.slides.pptd import parse_deck, validate_deck

    manifest_path = os.path.join(deck_dir, 'deck.pptd')
    try:
        with open(manifest_path, encoding='utf-8') as f:
            original_text = f.read()
        data = yaml.safe_load(original_text)
    except Exception as e:
        logger.debug('rewrite manifest failed: %s', e)
        return {'ok': False, 'errors': [f'manifest unreadable: {e}']}
    if not isinstance(data, dict):
        return {'ok': False, 'errors': ['manifest is not a YAML mapping']}

    try:
        mutate(data)
    except Exception as e:
        logger.debug('rewrite manifest failed: %s', e)
        return {'ok': False, 'errors': [str(e)]}
    new_text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                              width=120)
    write_text_atomic(manifest_path, new_text)
    try:
        deck = parse_deck(manifest_path)
    except Exception as e:
        write_text_atomic(manifest_path, original_text)
        return {'ok': False, 'errors': [f're-parse failed (restored): {e}']}
    findings = validate_deck(deck)
    if findings:
        write_text_atomic(manifest_path, original_text)
        return {'ok': False,
                'errors': [f'validation: {f}' for f in findings[:8]]}

    try:
        from lib.slides.render_png import render_previews
        render = render_previews(deck, os.path.join(deck_dir, 'preview'),
                                 scale=2.0)
        if render.get('failed'):
            logger.warning('[Slides:restyle] %d preview(s) failed to '
                           're-render', len(render['failed']))
    except Exception as e:
        logger.warning('[Slides:restyle] preview re-render failed: %s', e)

    import glob as _glob
    from lib.slides.export_pptx import export_pptx
    existing = _glob.glob(os.path.join(deck_dir, '*.pptx'))
    out_path = existing[0] if existing else os.path.join(deck_dir,
                                                         'deck.pptx')
    try:
        export_pptx(deck, out_path)
    except Exception as e:
        write_text_atomic(manifest_path, original_text)
        return {'ok': False,
                'errors': [f'export failed (manifest restored): {e}']}
    logger.info('[Slides:restyle] %s → %s', change_desc, out_path)
    return {'ok': True, 'changes': [change_desc], 'errors': [],
            'pptx_path': out_path}


def apply_deck_theme(deck_dir: str, theme_id: str) -> dict:
    """Bind a different curated theme (palette + typefaces + table defaults)."""
    from lib.design_sys.themes import get_theme, list_themes
    theme = get_theme(str(theme_id or '').strip())
    if theme is None:
        known = [t.id for t in list_themes()]
        return {'ok': False,
                'errors': [f'unknown theme_id {theme_id!r}; available: '
                           f'{", ".join(known)}'],
                'available_themes': known}
    stage_theme_fonts(theme)
    tokens = build_theme_tokens(theme)

    def _mutate(data: dict) -> None:
        data['theme'] = tokens
    return _rewrite_manifest(
        deck_dir, _mutate,
        {'op': 'set_theme', 'kind': 'theme', 'before': '',
         'after': theme.id})


def _valid_palette_value(value) -> bool:
    return (isinstance(value, str)
            and value.startswith('#')
            and len(value) in _HEX_RE_LEN
            and all(ch in '0123456789abcdefABCDEF' for ch in value[1:]))


def apply_deck_palette(deck_dir: str, colors: dict) -> dict:
    """Override individual palette slots (bg/ink/primary/accent/muted/
    hairline) with hex colors."""
    if not isinstance(colors, dict) or not colors:
        return {'ok': False, 'errors': ['colors must be a non-empty object']}
    if len(colors) > _MAX_PALETTE_ENTRIES:
        return {'ok': False,
                'errors': [f'colors exceeds {_MAX_PALETTE_ENTRIES} entries']}
    unknown = sorted(set(colors) - PALETTE_SLOTS)
    if unknown:
        return {'ok': False,
                'errors': [f'unknown palette slots {unknown}; allowed: '
                           f'{sorted(PALETTE_SLOTS)}']}
    bad = [k for k, v in colors.items() if not _valid_palette_value(v)]
    if bad:
        return {'ok': False,
                'errors': [f'palette values must be #RRGGBB[AA] hex; '
                           f'invalid: {bad}']}

    def _mutate(data: dict) -> None:
        theme = data.setdefault('theme', {})
        palette = theme.setdefault('colors', {})
        palette.update({k: str(v).strip() for k, v in colors.items()})
    return _rewrite_manifest(
        deck_dir, _mutate,
        {'op': 'set_palette', 'kind': 'theme', 'before': '',
         'after': dict(colors)})


def apply_deck_fonts(deck_dir: str, fonts: dict) -> dict:
    """Swap typefaces per textStyle role (title/body/caption/bignum).

    Faces must come from the license-audited registry (``get_font`` accepts
    id or family name); every weight is staged before the re-render so the
    preview and the embedded PPTX fonts stay identical.
    """
    import lib.design_sys.fonts as _fonts
    if not isinstance(fonts, dict) or not fonts:
        return {'ok': False, 'errors': ['fonts must be a non-empty object']}
    unknown = sorted(set(fonts) - FONT_STYLE_ROLES)
    if unknown:
        return {'ok': False,
                'errors': [f'unknown font roles {unknown}; allowed: '
                           f'{sorted(FONT_STYLE_ROLES)}']}
    resolved: dict[str, str] = {}
    for role, ref in fonts.items():
        face = _fonts.resolve_font(str(ref or '').strip())
        if face is None:
            return {'ok': False,
                    'errors': [f'font role {role}: unknown face {ref!r} '
                               '(registry id or family name)']}
        for src in face.sources:
            _fonts.ensure_font(face.id, src.weight)
        resolved[role] = face.family

    def _mutate(data: dict) -> None:
        theme = data.setdefault('theme', {})
        styles = theme.setdefault('textStyles', {})
        for role, family in resolved.items():
            style = styles.setdefault(role, {})
            style['fontFamily'] = family
    return _rewrite_manifest(
        deck_dir, _mutate,
        {'op': 'set_fonts', 'kind': 'theme', 'before': '',
         'after': dict(resolved)})


def apply_deck_restyle(deck_dir: str, *, theme_id: str = '',
                       colors: dict | None = None,
                       fonts: dict | None = None) -> dict:
    """One restyle call: theme first, then palette overrides, then fonts.

    Ordering matters (palette/fonts deliberately override the new theme's
    defaults); each stage validates and renders only after the LAST
    mutation, so a composite restyle costs ONE re-render + ONE export.
    """
    from lib.design_sys.themes import get_theme, list_themes
    import lib.design_sys.fonts as _fonts

    theme = None
    if theme_id:
        theme = get_theme(str(theme_id).strip())
        if theme is None:
            known = [t.id for t in list_themes()]
            return {'ok': False,
                    'errors': [f'unknown theme_id {theme_id!r}; available: '
                               f'{", ".join(known)}'],
                    'available_themes': known}
        stage_theme_fonts(theme)
    if colors:
        if not isinstance(colors, dict):
            return {'ok': False, 'errors': ['colors must be an object']}
        unknown = sorted(set(colors) - PALETTE_SLOTS)
        if unknown:
            return {'ok': False,
                    'errors': [f'unknown palette slots {unknown}; allowed: '
                               f'{sorted(PALETTE_SLOTS)}']}
        bad = [k for k, v in colors.items()
               if not _valid_palette_value(v)]
        if bad:
            return {'ok': False,
                    'errors': [f'palette values must be #RRGGBB[AA] hex; '
                               f'invalid: {bad}']}
    resolved_fonts: dict[str, str] = {}
    if fonts:
        if not isinstance(fonts, dict):
            return {'ok': False, 'errors': ['fonts must be an object']}
        unknown = sorted(set(fonts) - FONT_STYLE_ROLES)
        if unknown:
            return {'ok': False,
                    'errors': [f'unknown font roles {unknown}; allowed: '
                               f'{sorted(FONT_STYLE_ROLES)}']}
        for role, ref in fonts.items():
            face = _fonts.resolve_font(str(ref or '').strip())
            if face is None:
                return {'ok': False,
                        'errors': [f'font role {role}: unknown face {ref!r} '
                                   '(registry id or family name)']}
            for src in face.sources:
                _fonts.ensure_font(face.id, src.weight)
            resolved_fonts[role] = face.family
    if theme is None and not colors and not resolved_fonts:
        return {'ok': False,
                'errors': ['restyle needs theme_id, colors, or fonts']}

    changes: list = []
    if theme is not None:
        changes.append({'op': 'set_theme', 'kind': 'theme',
                        'before': '', 'after': theme.id})
    if colors:
        changes.append({'op': 'set_palette', 'kind': 'theme',
                        'before': '', 'after': dict(colors)})
    if resolved_fonts:
        changes.append({'op': 'set_fonts', 'kind': 'theme',
                        'before': '', 'after': dict(resolved_fonts)})

    def _mutate(data: dict) -> None:
        if theme is not None:
            data['theme'] = build_theme_tokens(theme)
        if colors:
            palette = data.setdefault('theme', {}).setdefault('colors', {})
            palette.update({k: str(v).strip() for k, v in colors.items()})
        if resolved_fonts:
            styles = data.setdefault('theme', {}).setdefault('textStyles', {})
            for role, family in resolved_fonts.items():
                styles.setdefault(role, {})['fontFamily'] = family
    result = _rewrite_manifest(deck_dir, _mutate,
                               {'op': 'restyle', 'kind': 'theme',
                                'before': '', 'after': ''})
    if result.get('ok'):
        result['changes'] = changes
    return result
