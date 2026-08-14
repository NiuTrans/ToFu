#!/usr/bin/env python3
"""Generate tofu-series mascot icon variants via text-to-image.

Produces role-specific PNGs in static/icons/:
  - tofu-planner.png  (beret + clipboard, thoughtful)
  - tofu-critic.png   (monocle + magnifying glass, discerning)
  - tofu-worker.png   (hard hat + wrench, determined)

Uses lib.image_gen.generate_image() with logo.png as style reference.

Usage:
    python3 scripts/gen_tofu_icons.py
    python3 scripts/gen_tofu_icons.py --role planner
    python3 scripts/gen_tofu_icons.py --no-reference
"""

import argparse
import base64
import os
import sys
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from lib.log import get_logger
from lib.image_gen import generate_image

logger = get_logger(__name__)

# ── Icon definitions ──
_ICONS = {
    'planner': {
        'filename': 'tofu-planner.png',
        'prompt': (
            'A kawaii isometric 3D tofu cube character on a clean white background. '
            'The tofu block is cream/beige colored with soft dark outlines, '
            'cute rectangular pixel-art eyes with white highlights, rosy blush marks on cheeks, '
            'and a gentle smile. '
            'The tofu wears a small dark blue beret on its top-left corner and holds '
            'a tiny clipboard with a checklist in one hand. '
            'It has a thoughtful, planning expression. '
            'The style matches the reference image exactly — same isometric perspective, '
            'same cream color palette, same kawaii face proportions, same outline weight. '
            'Simple, clean composition with no background clutter. '
            'High quality digital illustration, flat shading, mascot icon style.'
        ),
    },
    'critic': {
        'filename': 'tofu-critic.png',
        'prompt': (
            'A kawaii isometric 3D tofu cube character on a clean white background. '
            'The tofu block is cream/beige colored with soft dark outlines, '
            'cute rectangular pixel-art eyes with white highlights, rosy blush marks on cheeks. '
            'The tofu wears a small round monocle on one eye and holds '
            'a tiny magnifying glass. '
            'It has a discerning, analytical expression with one eyebrow slightly raised. '
            'The style matches the reference image exactly — same isometric perspective, '
            'same cream color palette, same kawaii face proportions, same outline weight. '
            'Simple, clean composition with no background clutter. '
            'High quality digital illustration, flat shading, mascot icon style.'
        ),
    },
    'worker': {
        'filename': 'tofu-worker.png',
        'prompt': (
            'A kawaii isometric 3D tofu cube character on a clean white background. '
            'The tofu block is cream/beige colored with soft dark outlines, '
            'cute rectangular pixel-art eyes with white highlights, rosy blush marks on cheeks, '
            'and a determined smile. '
            'The tofu wears a small yellow hard hat on top and holds '
            'a tiny wrench tool in one hand. '
            'It has a busy, industrious expression showing determination. '
            'The style matches the reference image exactly — same isometric perspective, '
            'same cream color palette, same kawaii face proportions, same outline weight. '
            'Simple, clean composition with no background clutter. '
            'High quality digital illustration, flat shading, mascot icon style.'
        ),
    },
}

_ICONS_DIR = os.path.join(_PROJECT_ROOT, 'static', 'icons')
_LOGO_PATH = os.path.join(_ICONS_DIR, 'logo.png')


def _load_source_image() -> list:
    """Load logo.png as base64 source_image for style reference.

    Returns:
        List of source_image dicts for generate_image().
    """
    if not os.path.exists(_LOGO_PATH):
        logger.error('[GenTofu] logo.png not found at %s', _LOGO_PATH)
        return []

    with open(_LOGO_PATH, 'rb') as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode('ascii')
    logger.info('[GenTofu] Loaded logo.png as style reference: %d bytes → %d chars b64',
                len(raw), len(b64))
    return [{'image_b64': b64, 'mime_type': 'image/png'}]


def generate_icon(role: str, source_images: list, max_attempts: int = 5) -> bool:
    """Generate a single tofu icon for the given role.

    Args:
        role: One of 'planner', 'critic', 'worker'.
        source_images: Base64 source images for style reference.
        max_attempts: Max generation attempts.

    Returns:
        True if icon was generated successfully.
    """
    icon_def = _ICONS.get(role)
    if not icon_def:
        logger.error('[GenTofu] Unknown role: %s', role)
        return False

    out_path = os.path.join(_ICONS_DIR, icon_def['filename'])
    prompt = icon_def['prompt']

    for attempt in range(1, max_attempts + 1):
        logger.info('[GenTofu] Generating %s icon (attempt %d/%d)…',
                     role, attempt, max_attempts)

        try:
            result = generate_image(
                prompt=prompt,
                aspect_ratio='1:1',
                resolution='1K',
                source_images=source_images if source_images else None,
                timeout=180,
                max_retries=5,
            )
        except Exception as e:
            logger.error('[GenTofu] generate_image() raised for %s: %s',
                         role, e, exc_info=True)
            if attempt < max_attempts:
                time.sleep(5)
                continue
            return False

        if not result.get('ok'):
            logger.warning('[GenTofu] Generation failed for %s: %s',
                           role, result.get('error', 'unknown'))
            if attempt < max_attempts:
                time.sleep(3)
                continue
            return False

        image_b64 = result.get('image_b64', '')
        if not image_b64:
            logger.warning('[GenTofu] No image data for %s', role)
            continue

        try:
            img_bytes = base64.b64decode(image_b64)
            with open(out_path, 'wb') as f:
                f.write(img_bytes)
            logger.info('[GenTofu] ✅ Saved %s → %s (%d bytes)',
                        role, out_path, len(img_bytes))
            return True
        except Exception as e:
            logger.error('[GenTofu] Failed to save %s: %s', role, e, exc_info=True)
            return False

    return False


def main():
    """Generate all (or selected) tofu icons."""
    parser = argparse.ArgumentParser(description='Generate tofu mascot icons')
    parser.add_argument('--role', choices=list(_ICONS.keys()),
                        help='Generate only this role (default: all)')
    parser.add_argument('--no-reference', action='store_true',
                        help='Skip loading logo.png as style reference')
    args = parser.parse_args()

    roles = [args.role] if args.role else list(_ICONS.keys())
    source_images = [] if args.no_reference else _load_source_image()

    if not source_images and not args.no_reference:
        logger.warning('[GenTofu] No source image loaded — generating without style reference')

    results = {}
    for role in roles:
        ok = generate_icon(role, source_images)
        results[role] = ok
        if ok and len(roles) > 1:
            time.sleep(2)

    # Summary
    print('\n' + '=' * 50)
    print('Generation Results:')
    for role, ok in results.items():
        status = '✅ SUCCESS' if ok else '❌ FAILED'
        path = os.path.join(_ICONS_DIR, _ICONS[role]['filename'])
        size = ''
        if ok and os.path.exists(path):
            size = f' ({os.path.getsize(path):,} bytes)'
        print(f'  {role:10s}: {status}{size}')
    print('=' * 50)

    if not all(results.values()):
        sys.exit(1)


if __name__ == '__main__':
    main()
