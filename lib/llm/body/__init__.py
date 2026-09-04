# HOT_PATH
"""LLM request body construction — model-aware payload builder.

Public API:
  - build_body(model, messages, ...) → dict
  - _validate_image_blocks(messages) → messages
  - _downscale_oversized_images(messages, model) → None
  - _project_images_for_text_model(messages, model, ...) → int
  - _strip_trailing_assistant_for_claude(messages, model) → None

This ``__init__`` is a pure re-export facade — every implementation lives in
the sub-modules (``_images`` / ``_model_tweaks`` / ``_clamp`` / ``_build``).
Every ``from lib.llm.body import X`` continues to resolve byte-identically.
"""

from lib.log import get_logger

logger = get_logger(__name__)


# ── Image handling: MIME sniff / validation / downscaling ─────────────────────
from lib.llm.body._images import (  # noqa: E402,F401
    _CLAUDE_IMAGE_MAX_PX,
    _IMAGE_MAGICS,
    _downscale_oversized_images,
    _project_images_for_text_model,
    _validate_image_blocks,
    sniff_image_mime,
)

# ── Model-specific message tweaks ─────────────────────────────────────────────
from lib.llm.body._model_tweaks import (  # noqa: E402,F401
    _GEMINI_SKIP_SIGNATURE,
    _inject_claude_reasoning_details,
    _inject_gemini_thought_signatures,
    _strip_trailing_assistant_for_claude,
)

# ── Context-window completion clamp ───────────────────────────────────────────
from lib.llm.body._clamp import (  # noqa: E402,F401
    _COMPLETION_INPUT_MARGIN,
    _COMPLETION_MIN_FLOOR,
    _clamp_completion_to_context_window,
)

# ── Main entrypoint ───────────────────────────────────────────────────────────
from lib.llm.body._build import build_body  # noqa: E402,F401


__all__ = [
    # main entrypoint
    'build_body',
    # images
    'sniff_image_mime',
    '_validate_image_blocks',
    '_downscale_oversized_images',
    '_project_images_for_text_model',
    '_CLAUDE_IMAGE_MAX_PX',
    '_IMAGE_MAGICS',
    # model tweaks
    '_strip_trailing_assistant_for_claude',
    '_inject_claude_reasoning_details',
    '_inject_gemini_thought_signatures',
    '_GEMINI_SKIP_SIGNATURE',
    # clamp
    '_clamp_completion_to_context_window',
    '_COMPLETION_INPUT_MARGIN',
    '_COMPLETION_MIN_FLOOR',
]
