"""
Desktop Agent — screenshot resolution scaling & coordinate remapping.

Grounding accuracy of a vision model degrades on high-resolution screenshots:
Anthropic's Computer Use guidance is explicit that you should NOT send images
above ~XGA/WXGA, and that you should scale the screenshot DOWN yourself, let the
model act on the scaled image, then map the returned coordinates back to real
screen pixels proportionally (relying on the API's own resize lowers accuracy).
See https://docs.claude.com/en/docs/build-with-claude/computer-use.

This module is pure arithmetic (no pyautogui / PIL / screen access) so the
coordinate contract can be unit-tested deterministically:

  * ``compute_scale(real_w, real_h)`` — the single source of truth for the
    downscale factor. Both the screenshot handler (which shrinks the image) and
    the GUI-action handler (which enlarges the model's click coordinate back to
    real pixels) call it with the real screen size, so no shared state is needed
    — the mapping is deterministic given the physical display size.
  * ``scaled_dimensions`` — the image size the model actually sees.
  * ``api_to_real`` / ``real_to_api`` — the bidirectional coordinate map.
"""

import os

from lib.log import get_logger

logger = get_logger(__name__)

# XGA — Anthropic's recommended target. Env-overridable for power users with a
# model tuned for a different resolution; both handlers read the same target.
_DEFAULT_TARGET_W = 1024
_DEFAULT_TARGET_H = 768


def target_size() -> tuple:
    """Return the (width, height) the screenshot is downscaled to fit within.

    Defaults to XGA 1024x768. Overridable via TOFU_DESKTOP_TARGET_W /
    TOFU_DESKTOP_TARGET_H (both must be positive ints or the default is kept).
    """
    w, h = _DEFAULT_TARGET_W, _DEFAULT_TARGET_H
    for env, default in (('TOFU_DESKTOP_TARGET_W', w), ('TOFU_DESKTOP_TARGET_H', h)):
        raw = os.environ.get(env)
        if raw is None:
            continue
        try:
            val = int(raw)
            if val > 0:
                if env.endswith('_W'):
                    w = val
                else:
                    h = val
        except (ValueError, TypeError) as e:
            logger.debug('[Desktop] bad %s=%r (%s) — keeping default %d', env, raw, e, default)
    return w, h


def compute_scale(real_w, real_h, target_w=None, target_h=None) -> float:
    """Downscale factor to fit a real screen within the target box.

    Never upscales: a display already at or below the target keeps scale 1.0
    (the model sees it at native size, no coordinate translation). The factor is
    the smaller of the width/height ratios so the whole screen fits and aspect
    ratio is preserved.

    Args:
        real_w, real_h: physical screen dimensions in pixels.
        target_w, target_h: fit box; defaults to :func:`target_size`.

    Returns:
        A float in (0, 1]. Returns 1.0 for degenerate (<=0) inputs.
    """
    if target_w is None or target_h is None:
        tw, th = target_size()
        target_w = target_w or tw
        target_h = target_h or th
    if real_w <= 0 or real_h <= 0:
        return 1.0
    scale = min(target_w / real_w, target_h / real_h)
    return scale if scale < 1.0 else 1.0


def scaled_dimensions(real_w, real_h, target_w=None, target_h=None) -> tuple:
    """Return (scaled_w, scaled_h, scale) — the image size the model sees."""
    scale = compute_scale(real_w, real_h, target_w, target_h)
    return max(1, round(real_w * scale)), max(1, round(real_h * scale)), scale


def api_to_real(x, y, scale) -> tuple:
    """Map a coordinate the MODEL produced (scaled/API space) to real pixels.

    The model clicks on the downscaled screenshot, so its coordinate must be
    divided by the scale factor to land on the real screen. Inverse of
    :func:`real_to_api`.
    """
    if not scale or scale <= 0:
        scale = 1.0
    return round(x / scale), round(y / scale)


def real_to_api(x, y, scale) -> tuple:
    """Map a real-screen coordinate into the scaled/API space the model sees."""
    if not scale or scale <= 0:
        scale = 1.0
    return round(x * scale), round(y * scale)
