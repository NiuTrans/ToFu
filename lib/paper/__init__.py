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
from .hashing import BASE_DIR, PAPER_DIR, PAPER_IMG_DIR, _paper_hash, _safe_hash_dir, resolve_paper_hash

# Prompts + tool list
from .prompts import (
    _MAX_REPORT_TOOL_ROUNDS,
    _REPORT_PROMPT_EN,
    _REPORT_PROMPT_ZH,
    _REPORT_TOOLS,
    date_anchor_clause,
)

# OpenReview single-page auto-fill (killer feature) — pure classifier +
# submit-free fill-plan + orchestration entry.
from .openreview_autofill import (
    autofill_openreview_review,
    build_fill_plan,
    classify_review_form,
    extract_forum_id,
    extract_pdf_url,
    extract_review_values,
    is_openreview_url,
    is_submit_control,
    plan_has_submit_action,
)

# Review Mode — venue registry + compound-key parser + review prompts
from .review import (
    DEFAULT_VENUE,
    REBUTTAL_DECISION_MARKER,
    REVIEW_VENUES,
    build_rebuttal_prompt,
    build_rebuttal_tool_instruction,
    build_review_prompt,
    build_review_tool_instruction,
    is_rebuttal_lang,
    is_review_family,
    is_review_lang,
    list_venues,
    make_rebuttal_lang,
    make_review_lang,
    parse_report_lang,
    parse_rebuttal_decision,
    rebuttal_decision_marker,
    finalize_rebuttal_body,
    finalize_review_body,
    scorecard_separator,
    smarten_quotes,
    strip_slop_dashes,
)

# Prompt-injection hardening for untrusted paper text
from .injection_guard import injection_notice, sanitize_paper_text, wrap_untrusted

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
from .arxiv import (
    ArxivQuerySyntaxError,
    _extract_arxiv_id,
    fetch_arxiv_title,
    search_arxiv,
    search_arxiv_explained,
)
from .harvest import HarvestResult, harvest_arxiv_batch, harvest_arxiv_id
from .survey import OPEN_GAPS_SCHEMA_VERSION, build_survey, survey_lang_key
from .ideate import IDEATE_GATE_THRESHOLD, generate_ideas, ideate_lang_key
from .recommend_engine import iter_recommend_events, recommend_papers
from .recommend_runtime import (
    _RECOMMEND_TASK_TTL,
    _append_recommend_event,
    _cleanup_stale_recommend_tasks,
    _new_recommend_task,
    _recommend_index_lock,
    _recommend_key,
    _recommend_latest_for,
    _recommend_latest_index,
    _recommend_register_latest,
    _recommend_runtime,
    _recommend_tasks,
    _recommend_tasks_lock,
)
from .recommend_task import _run_recommend_task
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
    'resolve_paper_hash',
    # prompts
    '_REPORT_PROMPT_EN', '_REPORT_PROMPT_ZH', '_REPORT_TOOLS', '_MAX_REPORT_TOOL_ROUNDS',
    'date_anchor_clause',
    # review mode
    'REVIEW_VENUES', 'DEFAULT_VENUE', 'parse_report_lang', 'is_review_lang',
    'make_review_lang', 'list_venues', 'build_review_prompt',
    'build_review_tool_instruction', 'smarten_quotes', 'strip_slop_dashes',
    'finalize_review_body', 'scorecard_separator',
    # rebuttal (author-response follow-up)
    'is_rebuttal_lang', 'is_review_family', 'make_rebuttal_lang',
    'build_rebuttal_prompt', 'build_rebuttal_tool_instruction',
    'parse_rebuttal_decision', 'rebuttal_decision_marker',
    'finalize_rebuttal_body', 'REBUTTAL_DECISION_MARKER',
    # openreview auto-fill (killer feature)
    'autofill_openreview_review', 'build_fill_plan', 'classify_review_form',
    'extract_forum_id', 'extract_pdf_url', 'extract_review_values',
    'is_openreview_url', 'is_submit_control', 'plan_has_submit_action',
    # injection guard
    'sanitize_paper_text', 'wrap_untrusted', 'injection_notice',
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
    'search_arxiv_explained', 'ArxivQuerySyntaxError', 'recommend_papers',
    'iter_recommend_events',
    # harvest (auto-research R1 — batch crawl + parse-once ingest)
    'harvest_arxiv_id', 'harvest_arxiv_batch', 'HarvestResult',
    # survey (auto-research R2 — fan-in survey + library-verified open-gap map)
    'build_survey', 'survey_lang_key', 'OPEN_GAPS_SCHEMA_VERSION',
    # ideate (auto-research R3 — anti-A+B idea gate)
    'generate_ideas', 'ideate_lang_key', 'IDEATE_GATE_THRESHOLD',
    '_recommend_runtime', '_recommend_tasks', '_recommend_tasks_lock',
    '_RECOMMEND_TASK_TTL', '_recommend_key', '_recommend_index_lock',
    '_recommend_latest_index', '_recommend_latest_for', '_recommend_register_latest',
    '_new_recommend_task', '_append_recommend_event',
    '_cleanup_stale_recommend_tasks', '_run_recommend_task',
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
