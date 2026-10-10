"""Every shipped provider template brand resolves a real badge.

The Settings template list renders ``brandIconHtml(tpl.brand, …)``; a brand
absent from ``MODEL_BRAND_ICONS`` / ``MODEL_BRAND_COLORS`` silently degrades
to the generic grey smiley (2026-09-06: cerebras/deepinfra/fireworks/
huggingface/modelscope/nebius/novita/sambanova/siliconflow/together all did).
The catalog contract (docs/FRONTEND_ARCHITECTURE.md — every template/vendor
key must resolve) is enforced here against both file-backed template dirs.
The inline ``custom`` builtin is a form placeholder, not a brand, so it is
intentionally out of scope.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / 'frontend/src/core/model-brand-icons.ts'
TEMPLATE_DIRS = (
    ROOT / 'lib/model_info/data',
    ROOT / 'static/provider_templates',
)

_ICON_KEY_RE = re.compile(r"^  '?([A-Za-z0-9_]+)'?: '<svg", re.MULTILINE)
_COLOR_KEY_RE = re.compile(
    r"'?([A-Za-z0-9_]+)'?: '#(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})'")


def _template_brands() -> dict[str, str]:
    brands: dict[str, str] = {}
    for directory in TEMPLATE_DIRS:
        for path in sorted(directory.glob('*.json')):
            template = json.loads(path.read_text(encoding='utf-8'))
            brand = template.get('brand')
            assert brand, f'{path.name} has no brand key'
            brands[brand] = path.name
    return brands


def test_every_template_brand_has_icon_and_color():
    source = REGISTRY.read_text(encoding='utf-8')
    icon_keys = set(_ICON_KEY_RE.findall(source))
    color_keys = set(_COLOR_KEY_RE.findall(source))
    assert 'generic' in icon_keys and 'generic' in color_keys
    brands = _template_brands()
    missing_icons = sorted(set(brands) - icon_keys)
    missing_colors = sorted(set(brands) - color_keys)
    assert not missing_icons, (
        'template brands without MODEL_BRAND_ICONS entry: '
        + ', '.join(f'{b} ({brands[b]})' for b in missing_icons))
    assert not missing_colors, (
        'template brands without MODEL_BRAND_COLORS entry: '
        + ', '.join(f'{b} ({brands[b]})' for b in missing_colors))
