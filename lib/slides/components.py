"""Semantic PPTD components expanded into editable native primitives.

Responsibility: let page authors express meaning (metric, quote, comparison,
timeline, process, code) without manually rebuilding fragile shape/text groups.
Expansion happens before PPTD validation, HTML rendering, and PPTX export, so
all downstream owners continue to consume the single primitive element model.
"""

from __future__ import annotations

import html
import re

__all__ = ['COMPONENT_TYPES', 'expand_page_components']

COMPONENT_TYPES = (
    'metric', 'quote', 'comparison', 'timeline', 'process', 'code',
)


def _compact(value, limit: int = 1200) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:limit]


def _bounds(component: dict) -> tuple[float, float, float, float]:
    values = component.get('bounds')
    if (not isinstance(values, list) or len(values) != 4
            or not all(isinstance(value, (int, float)) for value in values)
            or values[2] <= 0 or values[3] <= 0):
        raise ValueError('component bounds must be [x,y,w,h] positive numbers')
    return tuple(float(value) for value in values)


def _text(element_id: str, bounds, value: str, *, style: str = '$body',
          align=('left', 'middle'), **overrides) -> dict:
    content = {
        'style': style,
        'align': list(align),
        'wrap': True,
        'fit': 'shrink',
        'text': value,
    }
    content.update(overrides)
    return {'elementId': element_id, 'elementType': 'text',
            'bounds': [round(value, 2) for value in bounds],
            'content': content}


def _shape(element_id: str, bounds, *, fill='$bg', border='$hairline',
           shape='roundRect', adjustments=None) -> dict:
    out = {'elementId': element_id, 'elementType': 'shape',
           'bounds': [round(value, 2) for value in bounds],
           'shapeName': shape,
           'fill': {'type': 'solid', 'color': fill}}
    if border:
        out['border'] = {'style': 'solid', 'width': 1, 'color': border}
    if adjustments is not None:
        out['adjustments'] = list(adjustments)
    return out


def _metric(component: dict, component_id: str, bounds) -> list[dict]:
    x, y, width, height = bounds
    value = _compact(component.get('value'), 120)
    label = _compact(component.get('label'), 240)
    if not value or not label:
        raise ValueError('metric component needs value and label')
    support = _compact(component.get('support'), 500)
    source = _compact(component.get('source'), 240)
    value_height = height * 0.42
    value_font_size = max(32.0, min(80.0, height * 0.24))
    out = [
        _text(f'{component_id}--value', (x, y, width, value_height), value,
              style='$bignum', align=('left', 'middle'),
              fontSize=round(value_font_size, 1)),
        _text(f'{component_id}--label',
              (x, y + height * 0.45, width, height * 0.16), label,
              style='$body', align=('left', 'top'), fontSize=22, bold=True),
    ]
    if support:
        out.append(_text(
            f'{component_id}--support',
            (x, y + height * 0.64, width, height * 0.23), support,
            style='$body', align=('left', 'top'), fontSize=15))
    if source:
        out.append(_text(
            f'{component_id}--source',
            (x, y + height * 0.90, width, height * 0.10), source,
            style='$caption', align=('left', 'bottom'), fontSize=10))
    return out


def _quote(component: dict, component_id: str, bounds) -> list[dict]:
    x, y, width, height = bounds
    quote = _compact(component.get('quote') or component.get('text'), 1200)
    if not quote:
        raise ValueError('quote component needs quote')
    attribution = _compact(component.get('attribution'), 300)
    return [
        _shape(f'{component_id}--rule', (x, y, max(5, width * 0.012), height),
               fill='$accent', border=''),
        _text(f'{component_id}--quote',
              (x + width * 0.05, y, width * 0.95, height * 0.76),
              f'<p><strong>“{html.escape(quote)}”</strong></p>',
              style='$title', align=('left', 'middle')),
        _text(f'{component_id}--attribution',
              (x + width * 0.05, y + height * 0.78, width * 0.95,
               height * 0.22), attribution or '—', style='$caption',
              align=('left', 'top')),
    ]


def _comparison(component: dict, component_id: str, bounds) -> list[dict]:
    x, y, width, height = bounds
    gap = max(12.0, width * 0.025)
    panel_width = (width - gap) / 2
    out: list[dict] = []
    for side_index, side_name in enumerate(('left', 'right')):
        side = component.get(side_name)
        if not isinstance(side, dict):
            raise ValueError('comparison component needs left and right objects')
        heading = _compact(side.get('heading'), 180)
        raw_points = side.get('points')
        if not isinstance(raw_points, list):
            raw_points = []
        points = [_compact(point, 300) for point in raw_points]
        points = [point for point in points if point][:6]
        if not heading or not points:
            raise ValueError('comparison sides need heading and points')
        px = x + side_index * (panel_width + gap)
        prefix = f'{component_id}--{side_name}'
        out.append(_shape(f'{prefix}-panel', (px, y, panel_width, height)))
        out.append(_text(
            f'{prefix}-heading',
            (px + panel_width * 0.08, y + height * 0.07,
             panel_width * 0.84, height * 0.18), heading, style='$body',
            align=('left', 'middle'), fontSize=24, bold=True))
        body = ''.join(f'<p>• {html.escape(point)}</p>' for point in points)
        out.append(_text(
            f'{prefix}-points',
            (px + panel_width * 0.08, y + height * 0.29,
             panel_width * 0.84, height * 0.64), body, style='$body',
            align=('left', 'top')))
    return out


def _sequence(component: dict, component_id: str, bounds, *, process: bool) \
        -> list[dict]:
    x, y, width, height = bounds
    items = [item for item in (component.get('items') or [])
             if isinstance(item, dict)][:6]
    if len(items) < 2:
        raise ValueError('timeline/process component needs at least two items')
    count = len(items)
    slot = width / count
    center_y = y + height * (0.32 if process else 0.42)
    out: list[dict] = []
    if not process:
        out.append({
            'elementId': f'{component_id}--rail', 'elementType': 'line',
            'bounds': [round(x + slot / 2, 2), round(center_y - 1, 2),
                       round(width - slot, 2), 2],
            'viewBox': [round(width - slot, 2), 2],
            'points': f'0,1 {round(width - slot, 2)},1',
            'border': {'style': 'solid', 'width': 2, 'color': '$hairline'},
        })
    for index, item in enumerate(items):
        item_x = x + index * slot
        label = _compact(item.get('label'), 120)
        detail = _compact(item.get('detail'), 280)
        if not label:
            raise ValueError('timeline/process item needs label')
        prefix = f'{component_id}--item-{index + 1}'
        if process:
            out.append(_shape(
                f'{prefix}-shape',
                (item_x + slot * 0.04, y + height * 0.10,
                 slot * 0.88, height * 0.48),
                fill='$primary', border='', shape='chevron',
                adjustments=[25000]))
            text_color = '$bg'
        else:
            diameter = min(24.0, height * 0.12)
            out.append(_shape(
                f'{prefix}-dot',
                (item_x + slot / 2 - diameter / 2,
                 center_y - diameter / 2, diameter, diameter),
                fill='$accent', border='', shape='ellipse'))
            text_color = '$ink'
        out.append(_text(
            f'{prefix}-label',
            (item_x + slot * 0.07,
             y + height * (0.18 if process else 0.02),
             slot * 0.78, height * 0.20), label, style='$body',
            align=('center', 'middle'), bold=True, color=text_color))
        if detail:
            out.append(_text(
                f'{prefix}-detail',
                (item_x + slot * 0.07, y + height * 0.61,
                 slot * 0.82, height * 0.34), detail, style='$caption',
                align=('center', 'middle')))
    return out


def _code(component: dict, component_id: str, bounds) -> list[dict]:
    x, y, width, height = bounds
    code = str(component.get('code') or '').strip()[:4000]
    if not code:
        raise ValueError('code component needs code')
    title = _compact(component.get('title') or component.get('language'), 120)
    return [
        _shape(f'{component_id}--panel', bounds, fill='$ink', border=''),
        _text(f'{component_id}--title',
              (x + width * 0.05, y + height * 0.04,
               width * 0.90, height * 0.10), title or 'CODE',
              style='$caption', align=('left', 'middle'), color='$accent'),
        _text(f'{component_id}--code',
              (x + width * 0.05, y + height * 0.16,
               width * 0.90, height * 0.78), html.escape(code),
              style='$body', align=('left', 'top'), color='$bg',
              fontFamily='Aptos Mono', lineHeight=1.15),
    ]


def expand_page_components(page: dict) -> list[dict]:
    """Return primitive elements plus expanded semantic page components."""
    elements = list(page.get('elements') or [])
    components = page.get('components') or []
    if not isinstance(components, list):
        raise ValueError('page components must be an array')
    seen_component_ids: set[str] = set()
    expanders = {
        'metric': _metric,
        'quote': _quote,
        'comparison': _comparison,
        'timeline': lambda item, cid, bounds: _sequence(
            item, cid, bounds, process=False),
        'process': lambda item, cid, bounds: _sequence(
            item, cid, bounds, process=True),
        'code': _code,
    }
    for index, component in enumerate(components, 1):
        if not isinstance(component, dict):
            raise ValueError(f'component {index} must be an object')
        component_type = str(component.get('componentType') or '').strip()
        if component_type not in COMPONENT_TYPES:
            raise ValueError(
                f'component {index} has unsupported componentType '
                f'{component_type!r}')
        component_id = re.sub(
            r'[^A-Za-z0-9_-]+', '-',
            str(component.get('componentId') or f'component-{index}')).strip('-')
        if not component_id or component_id in seen_component_ids:
            raise ValueError(f'component {index} needs a unique componentId')
        seen_component_ids.add(component_id)
        elements.extend(expanders[component_type](
            component, component_id, _bounds(component)))
    return elements
