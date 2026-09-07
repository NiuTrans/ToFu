"""lib/pdf_parser/vlm/ — VLM-based PDF parsing (Gemini Flash Lite).

Public VLM parsing and task operations live at this module boundary.

Renders each PDF page to a JPEG image, sends batches to a VLM via an
OpenAI-compatible API for transcription to high-quality Markdown, and
manages background parse jobs via an async task registry.

Layout:
    _config.py — legacy request-lowering knobs + model discovery
    _policy.py — launch-probed task/page/call/deadline budgets
    _parse.py  — _vlm_call_pages, vlm_parse_pdf (synchronous parse)
    _tasks.py  — bounded owner-fair task/registry/cancellation lifecycle

Speed knobs (env-tunable, all optional):
    PDF_VLM_BATCH_PAGES   — pages per VLM call (default 4).
    PDF_VLM_MAX_WORKERS   — optional lower call-concurrency cap.
    PDF_VLM_MAX_TOKENS    — output-token cap per call (default 16384).
"""

from lib.log import get_logger

logger = get_logger(__name__)

from lib.pdf_parser.vlm._config import (  # noqa: E402
    _VLM_SYSTEM_PROMPT,
    _env_int,
    _get_vlm_models,
)
from lib.pdf_parser.vlm._parse import (  # noqa: E402
    _vlm_call_pages,
    vlm_parse_pdf,
)
from lib.pdf_parser.vlm._tasks import (  # noqa: E402
    _TASK_TTL,
    _cleanup_old_tasks,
    _vlm_lock,
    _vlm_tasks,
    VlmTaskQueueFull,
    cancel_vlm_task,
    find_vlm_tasks_by_filename,
    get_vlm_task,
    start_vlm_task,
    vlm_task_snapshot,
)

__all__ = [
    'VlmTaskQueueFull',
    'cancel_vlm_task',
    'find_vlm_tasks_by_filename',
    'get_vlm_task',
    'start_vlm_task',
    'vlm_parse_pdf',
    'vlm_task_snapshot',
]
