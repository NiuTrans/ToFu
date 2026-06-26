"""lib.paper — Paper Reading Mode engine + runtimes.

Decomposed from the original 3089-LOC ``routes/paper.py``. The Blueprint
and route handlers live in ``routes/paper.py`` and stay deliberately
thin; everything else (prompts, hashing, figure extraction + injection,
report TaskRuntime + tool-calling engine, translate TaskRuntime + chunk
worker, library schema, arxiv ID parser) lives here.

Public surface — what existing callers in ``routes/api_v1/agents`` and
``tests/test_paper_migration`` expect. New code should import from this
package facade rather than from submodules directly; the submodule
layout is implementation detail.
"""

# Hashing / paths
from .hashing import BASE_DIR, PAPER_DIR, PAPER_IMG_DIR, _paper_hash, _safe_hash_dir

# Prompts + tool list
from .prompts import (
    _MAX_REPORT_TOOL_ROUNDS,
    _REPORT_PROMPT_EN,
    _REPORT_PROMPT_ZH,
    _REPORT_TOOLS,
)

# LLM streaming + tool execution
from .llm_stream import _stream_llm_sse
from .tools import _execute_report_tool

# Images
from .images import (
    _FIG_EXTRACT_VERSION,
    _backfill_library_title,
    _build_image_manifest,
    _ensure_paper_images,
    _ensure_title_heading,
    _extract_paper_figures,
    _extract_title_from_report,
    _inject_images_into_report,
    _is_placeholder_title,
    _load_image_manifest,
    _lookup_paper_title,
)

# arXiv + library
from .arxiv import _extract_arxiv_id, fetch_arxiv_title, search_arxiv
from .library import (
    _LIB_IMAGES_CAP,
    _LIB_PARSED_TEXT_CAP,
    _LIB_QA_HISTORY_CAP,
    _LIB_TITLE_CAP,
    _PAPER_LIB_COLUMNS,
    _lib_row_to_dict,
)

# Report runtime + engine
from .report_runtime import (
    _REPORT_TASK_TTL,
    _append_report_event,
    _cleanup_stale_report_tasks,
    _new_report_task,
    _report_dedup_index,
    _report_dedup_lock,
    _report_index_get,
    _report_index_register,
    _report_runtime,
    _report_tasks,
    _report_tasks_lock,
)
from .report_engine import _run_report_task

# Q&A runtime + engine + context builder (agentic Q&A)
from .qa_context import build_qa_messages, select_relevant_sections, split_into_sections
from .qa_runtime import (
    _QA_TASK_TTL,
    _append_qa_event,
    _cleanup_stale_qa_tasks,
    _new_qa_task,
    _qa_latest_for,
    _qa_latest_index,
    _qa_register_latest,
    _qa_runtime,
    _qa_tasks,
    _qa_tasks_lock,
)
from .qa_engine import _MAX_QA_TOOL_ROUNDS, _run_qa_task

# Translate runtime + engine
from .translate_runtime import (
    _LANG_NAMES,
    _TRANSLATE_CHUNK_SIZE,
    _TRANSLATE_TASK_TTL,
    _append_translate_event,
    _cleanup_stale_translate_tasks,
    _new_translate_task,
    _translate_dedup_index,
    _translate_dedup_lock,
    _translate_index_get,
    _translate_index_register,
    _translate_runtime,
    _translate_tasks,
    _translate_tasks_lock,
)
from .translate_engine import _run_translate_task

__all__ = [
    # hashing
    'BASE_DIR', 'PAPER_DIR', 'PAPER_IMG_DIR', '_paper_hash', '_safe_hash_dir',
    # prompts
    '_REPORT_PROMPT_EN', '_REPORT_PROMPT_ZH', '_REPORT_TOOLS', '_MAX_REPORT_TOOL_ROUNDS',
    # llm + tools
    '_stream_llm_sse', '_execute_report_tool',
    # images
    '_FIG_EXTRACT_VERSION', '_load_image_manifest', '_extract_paper_figures',
    '_ensure_paper_images', '_inject_images_into_report',
    '_lookup_paper_title', '_ensure_title_heading', '_build_image_manifest',
    '_extract_title_from_report', '_backfill_library_title',
    '_is_placeholder_title',
    # arxiv + library
    '_extract_arxiv_id', 'fetch_arxiv_title', 'search_arxiv',
    '_PAPER_LIB_COLUMNS', '_LIB_PARSED_TEXT_CAP', '_LIB_QA_HISTORY_CAP',
    '_LIB_IMAGES_CAP', '_LIB_TITLE_CAP', '_lib_row_to_dict',
    # report
    '_report_runtime', '_report_dedup_index', '_report_dedup_lock',
    '_report_tasks', '_report_tasks_lock', '_REPORT_TASK_TTL',
    '_report_index_get', '_report_index_register',
    '_new_report_task', '_append_report_event',
    '_cleanup_stale_report_tasks', '_run_report_task',
    # translate
    '_translate_runtime', '_translate_dedup_index', '_translate_dedup_lock',
    '_translate_tasks', '_translate_tasks_lock',
    '_TRANSLATE_TASK_TTL', '_TRANSLATE_CHUNK_SIZE', '_LANG_NAMES',
    '_translate_index_get', '_translate_index_register',
    '_new_translate_task', '_append_translate_event',
    '_cleanup_stale_translate_tasks', '_run_translate_task',
    # qa
    'build_qa_messages', 'select_relevant_sections', 'split_into_sections',
    '_qa_runtime', '_qa_tasks', '_qa_tasks_lock', '_QA_TASK_TTL',
    '_qa_latest_index', '_qa_latest_for', '_qa_register_latest',
    '_new_qa_task', '_append_qa_event', '_cleanup_stale_qa_tasks',
    '_run_qa_task', '_MAX_QA_TOOL_ROUNDS',
]
