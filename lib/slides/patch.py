"""lib/slides/patch.py — deterministic, zero-LLM page edits.

A chat instruction like 「把第 3 页标题改成 X」 or 「这张图的来源换一下」
is a FIELD-LEVEL mutation, not a re-authoring job: spending an LLM round on
it is slower, costlier, and less predictable than patching the page YAML
directly. This module applies a bounded op set against ONE page's raw YAML
(elements addressed by ``elementId``, semantic components by
``componentId``), then re-runs the same zero-LLM validation the author loop
uses and re-measures the page's real layout; a patch that fails validation
or increases the page's measured findings is rolled back atomically — the
page file is never left half-edited.

Op vocabulary (closed — extend here, not per caller):

  ``set_text``    {target, text, field?} — a text element's content.text, or
                  a component scalar (metric: value/label/support/source;
                  quote: text/source; code: code).
  ``set_style``   {target, props{...}} — whitelisted text-content style
                  fields (fontSize, color, bold, italic, fontFamily,
                  lineHeight, letterSpacing, align, backgroundColor).
  ``set_bounds``  {target, bounds:[x,y,w,h]} — any element/component.
  ``swap_image``  {target, src} — an image element's src (media must exist).
  ``delete``      {target} — remove the element/component.
  ``duplicate``   {target, newId?, dx?, dy?} — copy with a fresh id.

Every op emits a structured ``changes[]`` row (op, target, kind,
before/after) — the chat card's 「改了什么」 summary and the model's own
narration both read it verbatim.
"""

from __future__ import annotations

import copy
import os

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['PATCH_OPS', 'apply_page_patch']

PATCH_OPS = frozenset((
    'set_text', 'set_style', 'set_bounds', 'swap_image', 'delete',
    'duplicate'))

#: componentType → scalar fields a set_text may write, with the default.
_COMPONENT_TEXT_FIELDS = {
    'metric': ('value', ('value', 'label', 'support', 'source')),
    'quote': ('text', ('text', 'source')),
    'code': ('code', ('code',)),
}

_STYLE_KEYS = frozenset((
    'fontSize', 'color', 'bold', 'italic', 'fontFamily', 'lineHeight',
    'letterSpacing', 'align', 'backgroundColor'))

_MAX_TEXT_CHARS = 4000
_MAX_OPS = 16


def _dump_yaml(data: dict) -> str:
    import yaml
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=120)


def _load_page_yaml(path: str) -> dict:
    import yaml
    with open(path, encoding='utf-8') as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError('page file is not a YAML mapping')
    return data


def _find_target(raw: dict, target: str) -> tuple:
    """Locate an element by elementId or a component by componentId.

    Returns ``(kind, collection, index, item)`` where kind ∈
    ``element|component``; ``(None, None, -1, None)`` when absent.
    """
    elements = raw.get('elements')
    if isinstance(elements, list):
        for i, el in enumerate(elements):
            if isinstance(el, dict) and str(el.get('elementId') or '') == target:
                return 'element', elements, i, el
    components = raw.get('components')
    if isinstance(components, list):
        for i, comp in enumerate(components):
            if isinstance(comp, dict) and str(comp.get('componentId') or '') == target:
                return 'component', components, i, comp
    return None, None, -1, None


def _compact_change(value, limit: int = 120) -> str:
    text = str(value if value is not None else '')
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _op_set_text(kind, item, op) -> tuple:
    """Returns (change_row, error)."""
    text = op.get('text')
    if not isinstance(text, str) or not text.strip():
        return None, 'set_text needs a non-empty text string'
    if len(text) > _MAX_TEXT_CHARS:
        return None, f'set_text text exceeds {_MAX_TEXT_CHARS} chars'
    if kind == 'element':
        if str(item.get('elementType') or '') != 'text':
            return None, ('set_text only writes text elements; '
                          f"{item.get('elementType')!r} needs edit_slides")
        content = item.get('content')
        if not isinstance(content, dict):
            return None, 'text element has no content object'
        before = content.get('text')
        content['text'] = text
        return {'op': 'set_text', 'target': item.get('elementId'),
                'kind': 'text', 'before': _compact_change(before),
                'after': _compact_change(text)}, None
    # component
    ctype = str(item.get('componentType') or '')
    mapping = _COMPONENT_TEXT_FIELDS.get(ctype)
    if not mapping:
        return None, (f'component {ctype!r} has no patchable text field; '
                      'use edit_slides for structural changes')
    default_field, allowed = mapping
    field = str(op.get('field') or default_field)
    if field not in allowed:
        return None, (f'component {ctype!r} text field must be one of '
                      f'{allowed}, got {field!r}')
    before = item.get(field)
    item[field] = text
    return {'op': 'set_text', 'target': item.get('componentId'),
            'kind': f'{ctype}.{field}', 'before': _compact_change(before),
            'after': _compact_change(text)}, None


def _op_set_style(kind, item, op) -> tuple:
    if kind != 'element' or str(item.get('elementType') or '') != 'text':
        return None, 'set_style only styles text elements'
    props = op.get('props')
    if not isinstance(props, dict) or not props:
        return None, 'set_style needs a non-empty props object'
    unknown = sorted(set(props) - _STYLE_KEYS)
    if unknown:
        return None, (f'unknown style keys {unknown}; allowed: '
                      f'{sorted(_STYLE_KEYS)}')
    fs = props.get('fontSize')
    if fs is not None and (not isinstance(fs, (int, float))
                           or isinstance(fs, bool) or fs <= 0 or fs > 400):
        return None, f'fontSize {fs!r} out of range (0, 400]'
    for flag in ('bold', 'italic'):
        if flag in props and not isinstance(props[flag], bool):
            return None, f'{flag} must be boolean'
    align = props.get('align')
    if align is not None:
        if (not isinstance(align, list) or len(align) != 2
                or align[0] not in ('left', 'center', 'right')
                or align[1] not in ('top', 'middle', 'bottom')):
            return None, ('align must be [left|center|right, '
                          'top|middle|bottom]')
    content = item.get('content')
    if not isinstance(content, dict):
        return None, 'text element has no content object'
    before = {k: content.get(k) for k in props}
    content.update(props)
    return {'op': 'set_style', 'target': item.get('elementId'),
            'kind': 'style', 'before': _compact_change(before),
            'after': _compact_change(props)}, None


def _op_set_bounds(kind, item, op) -> tuple:
    bounds = op.get('bounds')
    if (not isinstance(bounds, list) or len(bounds) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       for v in bounds)):
        return None, 'set_bounds needs [x, y, w, h] numbers'
    if bounds[2] <= 0 or bounds[3] <= 0:
        return None, 'bounds w/h must be positive'
    before = item.get('bounds')
    item['bounds'] = [round(float(v), 2) for v in bounds]
    target = (item.get('elementId') if kind == 'element'
              else item.get('componentId'))
    return {'op': 'set_bounds', 'target': target, 'kind': 'geometry',
            'before': _compact_change(before),
            'after': _compact_change(item['bounds'])}, None


def _op_swap_image(deck, kind, item, op) -> tuple:
    if kind != 'element' or str(item.get('elementType') or '') != 'image':
        return None, 'swap_image only retargets image elements'
    src = str(op.get('src') or '').strip()
    if not src:
        return None, 'swap_image needs a non-empty src'
    import re as _re
    if not _re.match(r'^(?:https?://|data:)', src):
        from lib.slides.pptd import PPTDError, resolve_media
        try:
            local = resolve_media(deck, src)
        except PPTDError as e:
            return None, f'swap_image src rejected: {e}'
        if not os.path.isfile(local):
            return None, f'swap_image media file missing: {src}'
    before = item.get('src')
    item['src'] = src
    return {'op': 'swap_image', 'target': item.get('elementId'),
            'kind': 'media', 'before': _compact_change(before),
            'after': _compact_change(src)}, None


def _op_duplicate(raw, kind, collection, index, item, op) -> tuple:
    clone = copy.deepcopy(item)
    id_key = 'elementId' if kind == 'element' else 'componentId'
    base = str(item.get(id_key) or 'el')
    new_id = str(op.get('newId') or '').strip()
    if not new_id:
        suffix = 2
        while True:
            candidate = f'{base}-copy{suffix}'
            if _find_target(raw, candidate)[0] is None:
                new_id = candidate
                break
            suffix += 1
            if suffix > 99:
                return None, 'duplicate could not mint a unique id'
    elif _find_target(raw, new_id)[0] is not None:
        return None, f'duplicate id {new_id!r} already exists on this page'
    clone[id_key] = new_id
    bounds = clone.get('bounds')
    if isinstance(bounds, list) and len(bounds) == 4:
        try:
            dx = float(op.get('dx', 24))
            dy = float(op.get('dy', 24))
        except (TypeError, ValueError):
            return None, 'duplicate dx/dy must be numbers'
        clone['bounds'] = [round(bounds[0] + dx, 2),
                           round(bounds[1] + dy, 2), bounds[2], bounds[3]]
    collection.insert(index + 1, clone)
    return {'op': 'duplicate', 'target': item.get(id_key), 'kind': 'create',
            'before': '', 'after': new_id}, None


def _apply_ops(deck, raw: dict, ops: list) -> tuple:
    """Mutate ``raw`` in place. Returns (changes, errors)."""
    changes: list = []
    errors: list = []
    for position, op in enumerate(ops, 1):
        if not isinstance(op, dict):
            errors.append(f'op {position}: not an object')
            continue
        name = str(op.get('op') or '')
        if name not in PATCH_OPS:
            errors.append(f'op {position}: unknown op {name!r} '
                          f'(vocabulary: {sorted(PATCH_OPS)})')
            continue
        target = str(op.get('target') or '').strip()
        if not target:
            errors.append(f'op {position} ({name}): target is required')
            continue
        kind, collection, index, item = _find_target(raw, target)
        if kind is None:
            errors.append(f'op {position} ({name}): no element or component '
                          f'with id {target!r} on this page')
            continue
        if name == 'set_text':
            change, error = _op_set_text(kind, item, op)
        elif name == 'set_style':
            change, error = _op_set_style(kind, item, op)
        elif name == 'set_bounds':
            change, error = _op_set_bounds(kind, item, op)
        elif name == 'swap_image':
            change, error = _op_swap_image(deck, kind, item, op)
        elif name == 'delete':
            del collection[index]
            change, error = ({'op': 'delete', 'target': target,
                              'kind': kind, 'before': '', 'after': ''}, None)
        else:  # duplicate
            change, error = _op_duplicate(raw, kind, collection, index,
                                          item, op)
        if error:
            errors.append(f'op {position} ({name}): {error}')
        else:
            change['page_target'] = target
            changes.append(change)
    return changes, errors


def _page_layout_findings(deck, page_index: int):
    """Measured glyph-level finding count for ONE page (None = unmeasured).

    Best-effort: a host without a browser reports None and the patch gate
    falls back to schema validation only (reported honestly in the result).
    """
    try:
        from lib.slides.layout_qa import inspect_deck_layout
        result = inspect_deck_layout(deck)
        if not result.get('ran'):
            return None
        pages = result.get('pages') or []
        if 0 <= page_index < len(pages):
            return len(pages[page_index].get('findings') or [])
        return None
    except Exception as e:
        logger.info('[Slides:patch] layout measurement unavailable: %s', e)
        return None


def apply_page_patch(deck_dir: str, page_index: int, ops: list) -> dict:
    """Apply deterministic ops to one page; returns a structured result.

    NEVER raises: a failed patch leaves the on-disk page untouched (the
    pre-patch bytes are restored whenever a write already happened). Result
    shape: ``{'ok', 'changes', 'errors', 'preview', 'pptx_path',
    'layout_checked'}``.
    """
    from lib.slides.pptd import parse_deck

    if not isinstance(ops, list) or not ops:
        return {'ok': False, 'errors': ['ops must be a non-empty array']}
    if len(ops) > _MAX_OPS:
        return {'ok': False,
                'errors': [f'ops exceeds the {_MAX_OPS}-op patch bound']}
    try:
        deck = parse_deck(os.path.join(deck_dir, 'deck.pptd'))
    except Exception as e:
        logger.debug('apply page patch failed: %s', e)
        return {'ok': False, 'errors': [f'deck unreadable: {e}']}
    if page_index < 0 or page_index >= len(deck.pages):
        return {'ok': False,
                'errors': [f'page {page_index + 1} out of range '
                           f'(deck has {len(deck.pages)} pages)']}
    page = deck.pages[page_index]
    page_abs = os.path.join(deck_dir, page.path)
    try:
        with open(page_abs, encoding='utf-8') as f:
            original_text = f.read()
    except OSError as e:
        return {'ok': False, 'errors': [f'cannot read page file: {e}']}
    try:
        raw = _load_page_yaml(page_abs)
    except Exception as e:
        logger.debug('apply page patch failed: %s', e)
        return {'ok': False, 'errors': [f'page YAML unparseable: {e}']}

    changes, errors = _apply_ops(deck, raw, ops)
    if errors:
        return {'ok': False, 'errors': errors, 'changes': changes}

    # Validate the patched page BEFORE it touches disk (same zero-LLM gate
    # the author's own inner loop repairs against).
    from lib.slides.author import _validate_page_text
    patched_text = _dump_yaml(raw)
    findings = _validate_page_text(deck, page.path, patched_text)
    if findings:
        return {'ok': False,
                'errors': [f'validation: {f}' for f in findings[:8]],
                'changes': changes}

    baseline = _page_layout_findings(deck, page_index)

    from lib.json_store import write_text_atomic
    write_text_atomic(page_abs, patched_text)
    try:
        deck = parse_deck(os.path.join(deck_dir, 'deck.pptd'))
    except Exception as e:
        write_text_atomic(page_abs, original_text)
        return {'ok': False, 'errors': [f're-parse failed (restored): {e}'],
                'changes': changes}

    measured = _page_layout_findings(deck, page_index)
    layout_checked = measured is not None and baseline is not None
    if layout_checked and measured > baseline:
        write_text_atomic(page_abs, original_text)
        return {'ok': False,
                'errors': [f'layout regression: measured findings rose '
                           f'{baseline} → {measured}; page restored'],
                'changes': changes, 'layout_checked': True}

    preview = os.path.join(deck_dir, 'preview', 'pages',
                           f'{page_index + 1:02d}.png')
    try:
        from lib.slides.render_png import render_page_png
        render_page_png(deck, page_index, preview, scale=2.0)
    except Exception as e:
        logger.warning('[Slides:patch] page %d preview re-render failed: %s',
                       page_index + 1, e)

    import glob as _glob
    from lib.slides.export_pptx import export_pptx
    existing = _glob.glob(os.path.join(deck_dir, '*.pptx'))
    out_path = existing[0] if existing else os.path.join(deck_dir,
                                                         'deck.pptx')
    try:
        export_pptx(deck, out_path)
    except Exception as e:
        write_text_atomic(page_abs, original_text)
        return {'ok': False,
                'errors': [f'export failed (page restored): {e}'],
                'changes': changes}
    logger.info('[Slides:patch] page %d: %d op(s) applied → %s',
                page_index + 1, len(changes), out_path)
    return {'ok': True, 'changes': changes, 'errors': [],
            'preview': preview, 'pptx_path': out_path,
            'layout_checked': layout_checked}
