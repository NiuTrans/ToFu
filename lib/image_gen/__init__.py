"""lib/image_gen — Image generation via FRIDAY APIs.

Was a single 1125-line ``lib/image_gen.py``; decomposed into function-seam
submodules behind this facade so the public contract is byte-identical:

    from lib.image_gen import generate_image

is the ONE public entry point (every caller in routes/, scripts/, lib/tools/
uses exactly this). The internal generators are split by provider family:

  * ``_errors``   — _RateLimitError / _HttpError / _download_image + base default
  * ``_slots``    — dispatch-slot pick + FRIDAY-vs-OpenAI-compat routing + constants
  * ``_openai``   — FRIDAY OpenAI-native generate + edit
  * ``_chat``     — OpenAI-compatible chat-completions generate + multi-turn builder
  * ``_gemini``   — FRIDAY Gemini async submit+poll
  * ``_generate`` — the public generate_image retry orchestrator

Supports two families of image generation models:

 1. **Gemini** (async submit+poll):
    POST /v1/google/models/{model}:imageGenerate   → task ID
    GET  /v1/google/models/{taskId}:imageGenerateQuery → poll result

 2. **OpenAI** (sync one-shot):
    POST /v1/openai/native/images/generations → b64_json / url

Usage:
    from lib.image_gen import generate_image

    result = generate_image("A serene mountain landscape at sunset")
    if result['ok']:
        image_b64 = result['image_b64']
        mime_type = result['mime_type']
"""

from ._chat import _build_multiturn_contents, _generate_chat_completions
from ._errors import (
    _IMAGE_GEN_BASE_DEFAULT,
    _HttpError,
    _RateLimitError,
    _download_image,
)
from ._gemini import _generate_gemini
from ._generate import generate_image
from ._openai import _edit_openai, _generate_openai
from ._slots import (
    _api_base_from_slot,
    _friday_base_from_slot,
    _is_friday_provider,
    _is_openai_model,
    _pick_image_slot,
)

__all__ = [
    'generate_image',
]
