"""lib/pdf_parser/vlm/ — VLM-based PDF parsing (Gemini Flash Lite).

Façade package — import path unchanged (``from lib.pdf_parser.vlm import X``
and ``from lib.pdf_parser import vlm_parse_pdf`` both work byte-identically).

Renders each PDF page to a JPEG image, sends batches to a VLM via an
OpenAI-compatible API for transcription to high-quality Markdown, and
manages background parse jobs via an async task registry.

Layout:
    _config.py — _env_int, _get_vlm_models, _VLM_SYSTEM_PROMPT
    _parse.py  — _vlm_call_pages, vlm_parse_pdf (synchronous parse)
    _tasks.py  — ALL shared async-registry state (_vlm_tasks/_vlm_lock/
                 _TASK_TTL) + start_vlm_task/get_vlm_task/
                 find_vlm_tasks_by_filename/_cleanup_old_tasks

Speed knobs (env-tunable, all optional):
    PDF_VLM_BATCH_PAGES   — pages per VLM call (default 4).
    PDF_VLM_MAX_WORKERS   — concurrent VLM calls (default = number of batches).
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
    find_vlm_tasks_by_filename,
    get_vlm_task,
    start_vlm_task,
)

# Public API — preserved verbatim from the original module.
__all__ = ['vlm_parse_pdf', 'start_vlm_task', 'get_vlm_task',
           'find_vlm_tasks_by_filename']
