#!/usr/bin/env python3
"""Convert PNG mascot icons to pixel-perfect SVGs using vtracer.

Traces PNG images into vector SVGs with automatic background removal.
The white/near-white background common in AI-generated images is detected
via flood-fill from image edges and made transparent before tracing.
PNGs are cropped to content, padded to a square, resized to 512×512,
then traced with parameters tuned for kawaii mascot art.

Usage:
    python3 scripts/png_to_svg.py                          # convert tofu-{planner,critic,worker}.png
    python3 scripts/png_to_svg.py tofu-planner.png         # convert specific file
    python3 scripts/png_to_svg.py --all                    # convert ALL PNGs in static/icons/
    python3 scripts/png_to_svg.py --size 1024              # trace at original resolution (larger SVG)
    python3 scripts/png_to_svg.py --tolerance 40           # more aggressive bg removal
    python3 scripts/png_to_svg.py --no-remove-bg           # skip background removal
"""

import argparse
import glob
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from lib.log import get_logger

logger = get_logger(__name__)

_ICONS_DIR = os.path.join(_PROJECT_ROOT, 'static', 'icons')

# ── vtracer parameters tuned for kawaii mascot icons ──
_VTRACER_PARAMS = {
    'colormode': 'color',        # Full color tracing
    'hierarchical': 'stacked',   # Stacked layers for correct overlap
    'mode': 'spline',            # Smooth curves
    'filter_speckle': 8,         # Remove noise artifacts (higher = cleaner)
    'color_precision': 5,        # Color quantization (lower = fewer layers)
    'layer_difference': 24,      # Merge similar colors (higher = simpler)
    'corner_threshold': 60,      # Corner detection angle
    'length_threshold': 4.0,     # Minimum path length
    'max_iterations': 10,        # Optimization iterations
    'splice_threshold': 45,      # Splice angle threshold
    'path_precision': 3,         # Decimal precision (lower = smaller file)
}

# Target size for consistent SVG output
_DEFAULT_TRACE_SIZE = 512

# Background removal tolerance (max RGB distance from corner-sampled bg color)
_DEFAULT_BG_TOLERANCE = 30


def _remove_background(img, tolerance: int = _DEFAULT_BG_TOLERANCE):
    """Remove white/near-white background from an RGBA image.

    Uses flood-fill from image edges to identify background pixels.
    Only pixels that are both (a) close to the corner-sampled background
    color AND (b) connected to the image border are made transparent.
    This preserves white highlights inside the character.

    Args:
        img: PIL Image in RGBA mode.
        tolerance: Max per-channel RGB distance from the detected bg color.
            Higher = more aggressive removal. Default 30.

    Returns:
        PIL Image with background made transparent, or the original if
        background removal is not applicable.
    """
    try:
        import numpy as np
        from PIL import Image
        from scipy import ndimage
    except ImportError as e:
        logger.warning('[PNG2SVG] numpy/scipy/Pillow not available, skipping bg removal: %s', e)
        return img

    arr = np.array(img)
    h, w = arr.shape[:2]

    # ── Sample corners (10×10 patches) to detect bg color ──
    patch = 10
    corners = np.concatenate([
        arr[:patch, :patch, :3].reshape(-1, 3),
        arr[:patch, -patch:, :3].reshape(-1, 3),
        arr[-patch:, :patch, :3].reshape(-1, 3),
        arr[-patch:, -patch:, :3].reshape(-1, 3),
    ])
    bg_color = corners.mean(axis=0)
    logger.info('[PNG2SVG] Detected bg color: RGB(%.0f, %.0f, %.0f)',
                bg_color[0], bg_color[1], bg_color[2])

    # ── Check if bg is actually near-white — skip if not ──
    if bg_color.min() < 200:
        logger.info('[PNG2SVG] Background is not near-white (min=%.0f), skipping removal',
                    bg_color.min())
        return img

    # ── Create mask of pixels close to bg color ──
    diff = np.abs(arr[:, :, :3].astype(float) - bg_color).max(axis=2)
    near_bg = diff < tolerance

    # ── Flood fill from edges — only remove connected bg ──
    labeled, n_labels = ndimage.label(near_bg)

    edge_labels = set()
    edge_labels.update(labeled[0, :].tolist())      # top row
    edge_labels.update(labeled[-1, :].tolist())     # bottom row
    edge_labels.update(labeled[:, 0].tolist())      # left column
    edge_labels.update(labeled[:, -1].tolist())     # right column
    edge_labels.discard(0)  # 0 = not near bg

    bg_mask = np.isin(labeled, list(edge_labels))
    removed_pct = bg_mask.sum() / (h * w) * 100

    logger.info('[PNG2SVG] Background removal: %d components, %d edge-connected, '
                '%.1f%% pixels removed',
                n_labels, len(edge_labels), removed_pct)

    # ── Apply transparency ──
    arr[bg_mask, 3] = 0
    return Image.fromarray(arr)


def _crop_and_square(img, padding_pct: float = 0.05):
    """Crop image to content bounding box, then pad to a square.

    Args:
        img: PIL RGBA Image.
        padding_pct: Padding as a fraction of the content size (default 5%).

    Returns:
        Square PIL Image with transparent padding.
    """
    from PIL import Image as PILImage

    bbox = img.getbbox()
    if not bbox:
        logger.warning('[PNG2SVG] Image is fully transparent after bg removal')
        return img

    w, h = img.size
    content_w = bbox[2] - bbox[0]
    content_h = bbox[3] - bbox[1]
    pad = max(10, int(padding_pct * max(content_w, content_h)))

    crop_box = (
        max(0, bbox[0] - pad),
        max(0, bbox[1] - pad),
        min(w, bbox[2] + pad),
        min(h, bbox[3] + pad),
    )
    cropped = img.crop(crop_box)

    # Make square by padding the shorter dimension
    cw, ch = cropped.size
    side = max(cw, ch)
    square = PILImage.new('RGBA', (side, side), (0, 0, 0, 0))
    square.paste(cropped, ((side - cw) // 2, (side - ch) // 2))

    logger.info('[PNG2SVG] Cropped from %d×%d → %d×%d, squared to %d×%d',
                w, h, cw, ch, side, side)
    return square


def convert_png_to_svg(png_path: str, svg_path: str | None = None,
                       trace_size: int = _DEFAULT_TRACE_SIZE,
                       remove_bg: bool = True,
                       bg_tolerance: int = _DEFAULT_BG_TOLERANCE) -> bool:
    """Convert a single PNG to SVG using vtracer.

    Pipeline:
        1. Load PNG as RGBA
        2. Remove white background (flood-fill from edges)
        3. Crop to content bounding box + pad to square
        4. Resize to trace_size×trace_size
        5. Trace with vtracer
        6. Validate output SVG

    Args:
        png_path: Path to the input PNG file.
        svg_path: Path for output SVG (default: same name with .svg extension).
        trace_size: Resize PNG to this dimension before tracing.
        remove_bg: Whether to auto-remove white background.
        bg_tolerance: RGB tolerance for background detection (0-255).

    Returns:
        True if conversion succeeded.
    """
    try:
        import vtracer
    except ImportError:
        logger.error('[PNG2SVG] vtracer not installed — run: pip install vtracer')
        return False

    try:
        from PIL import Image
    except ImportError:
        logger.error('[PNG2SVG] Pillow not installed — run: pip install Pillow')
        return False

    if not os.path.exists(png_path):
        logger.error('[PNG2SVG] Input file not found: %s', png_path)
        return False

    if svg_path is None:
        svg_path = os.path.splitext(png_path)[0] + '.svg'

    png_size = os.path.getsize(png_path)
    basename = os.path.basename(png_path)
    logger.info('[PNG2SVG] Converting %s (%d bytes) → %s',
                basename, png_size, os.path.basename(svg_path))

    tmp_path = None
    try:
        # ── Step 1: Load as RGBA ──
        img = Image.open(png_path).convert('RGBA')

        # ── Step 2: Remove background ──
        if remove_bg:
            img = _remove_background(img, tolerance=bg_tolerance)

        # ── Step 3: Crop to content + square ──
        if remove_bg:
            img = _crop_and_square(img)

        # ── Step 4: Resize to trace_size ──
        w, h = img.size
        if w != trace_size or h != trace_size:
            logger.info('[PNG2SVG] Resizing from %d×%d → %d×%d', w, h, trace_size, trace_size)
            img = img.resize((trace_size, trace_size), Image.LANCZOS)

        # Save preprocessed PNG to temp file (use project-local data/tmp/
        # instead of system /tmp which may not be accessible on all machines)
        _tmp_dir = os.path.join(_PROJECT_ROOT, 'data', 'tmp')
        os.makedirs(_tmp_dir, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(suffix='.png', dir=_tmp_dir)
        os.close(tmp_fd)
        img.save(tmp_path, 'PNG')
        img.close()

        # ── Step 5: Trace with vtracer ──
        vtracer.convert_image_to_svg_py(
            image_path=tmp_path,
            out_path=svg_path,
            **_VTRACER_PARAMS,
        )
    except Exception as e:
        logger.error('[PNG2SVG] Conversion failed for %s: %s', basename, e, exc_info=True)
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    if not os.path.exists(svg_path):
        logger.error('[PNG2SVG] Output SVG not created: %s', svg_path)
        return False

    svg_size = os.path.getsize(svg_path)
    logger.info('[PNG2SVG] ✅ Created %s (%d bytes, %.1f%% of PNG)',
                os.path.basename(svg_path), svg_size,
                (svg_size / png_size * 100) if png_size else 0)

    # ── Step 6: Validate SVG XML ──
    _validate_svg(svg_path)

    return True


def _validate_svg(svg_path: str) -> bool:
    """Validate that the SVG is well-formed XML.

    Args:
        svg_path: Path to the SVG file.

    Returns:
        True if the SVG is valid XML with an <svg> root.
    """
    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()
        tag = root.tag
        if not tag.endswith('svg'):
            logger.warning('[PNG2SVG] Root element is not <svg>: %s', tag)
            return False
        vb = root.get('viewBox') or root.get('viewbox', '')
        w = root.get('width', '?')
        h = root.get('height', '?')
        logger.info('[PNG2SVG] SVG validated: %s (viewBox=%s, width=%s, height=%s)',
                    os.path.basename(svg_path), vb, w, h)
        return True
    except ET.ParseError as e:
        logger.error('[PNG2SVG] Invalid SVG XML in %s: %s', svg_path, e)
        return False


def main():
    """Convert PNG files to SVG with automatic background removal."""
    parser = argparse.ArgumentParser(
        description='Convert PNGs to SVGs using vtracer (with auto bg removal)')
    parser.add_argument('files', nargs='*',
                        help='PNG filenames (relative to static/icons/)')
    parser.add_argument('--all', action='store_true',
                        help='Convert ALL PNGs in static/icons/')
    parser.add_argument('--size', type=int, default=_DEFAULT_TRACE_SIZE,
                        help=f'Trace at this resolution (default: {_DEFAULT_TRACE_SIZE})')
    parser.add_argument('--tolerance', type=int, default=_DEFAULT_BG_TOLERANCE,
                        help=f'Background color tolerance 0-255 (default: {_DEFAULT_BG_TOLERANCE})')
    parser.add_argument('--no-remove-bg', action='store_true',
                        help='Skip automatic background removal')
    args = parser.parse_args()

    if args.all:
        png_files = sorted(glob.glob(os.path.join(_ICONS_DIR, '*.png')))
    elif args.files:
        png_files = []
        for f in args.files:
            path = f if os.path.isabs(f) else os.path.join(_ICONS_DIR, f)
            if os.path.exists(path):
                png_files.append(path)
            else:
                logger.warning('[PNG2SVG] File not found: %s', path)
    else:
        # Default: convert tofu-planner, tofu-critic, tofu-worker
        png_files = [
            os.path.join(_ICONS_DIR, f'tofu-{role}.png')
            for role in ('planner', 'critic', 'worker')
        ]
        existing = [p for p in png_files if os.path.exists(p)]
        if not existing:
            logger.error('[PNG2SVG] No tofu-{planner,critic,worker}.png found in %s',
                         _ICONS_DIR)
            logger.info('[PNG2SVG] Run scripts/gen_tofu_icons.py first to generate them')
            sys.exit(1)
        png_files = existing

    if not png_files:
        logger.error('[PNG2SVG] No PNG files to convert')
        sys.exit(1)

    results = {}
    for png_path in png_files:
        name = os.path.basename(png_path)
        ok = convert_png_to_svg(
            png_path,
            trace_size=args.size,
            remove_bg=not args.no_remove_bg,
            bg_tolerance=args.tolerance,
        )
        results[name] = ok

    # ── Summary ──
    print('\n' + '=' * 60)
    print('Conversion Results:')
    for name, ok in results.items():
        status = '✅ SUCCESS' if ok else '❌ FAILED'
        svg_name = os.path.splitext(name)[0] + '.svg'
        svg_path = os.path.join(_ICONS_DIR, svg_name)
        size = ''
        if ok and os.path.exists(svg_path):
            size = f' ({os.path.getsize(svg_path):,} bytes)'
        print(f'  {name:30s} → {svg_name:30s} {status}{size}')
    print('=' * 60)

    if not all(results.values()):
        sys.exit(1)


if __name__ == '__main__':
    main()
