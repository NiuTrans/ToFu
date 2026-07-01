"""lib.daily_report — Daily task-centric report engine.

Decomposed from the original 2405-LOC ``routes/daily_report.py``. The
Blueprint and route handlers live in ``routes/daily_report.py`` and stay
deliberately thin; everything else (storage, prompts, cost calculation,
TODO carryover/fuzzy-match, conversation extraction + LLM analysis,
async generator, scheduler) lives here as focused submodules.

Public surface — what existing callers import. New code should import
from this package facade rather than from submodules directly; the
submodule layout is implementation detail.
"""

# Storage (report JSON I/O + active-job tracking)
from .storage import (
    DEFAULT_USER_ID,
    _REPORTS_DIR,
    _active_jobs,
    _jobs_lock,
    _update_job,
    _get_job,
    _clear_job,
    _report_path,
    _save_report,
    _load_report,
)

# Prompts + persona constants
from .prompts import (
    _ANALYSIS_SYSTEM,
    _TODO_TOOL_DEFAULTS,
    _TODO_TOOL_MAP,
    _QUOTES,
)

# Cost calculation (per-message, per-day, per-month)
from .cost import (
    _calendar_cache,
    _CALENDAR_CACHE_TTL,
    _LEGACY_PRESET_TO_MODEL,
    _qwen_cny,
    _calc_msg_cost_cny,
    _scan_costs_in_range,
    _load_cached_day_costs,
    _persist_day_cost,
    invalidate_day_cost_cache,
    invalidate_cost_cache_for_messages,
    _get_monthly_costs,
)

# TODO carryover + fuzzy matching
from .todos import (
    _normalize_todo_text,
    _fuzzy_todo_match,
    _get_yesterday_carryover,
    _get_today_inherited_todos,
    _get_yesterday_todo_accountability,
    _mark_yesterday_todos_done,
    _close_yesterday_remaining_todos,
    _merge_manual_state,
)

# Conversation extraction + analysis
from .conversations import (
    _safe_int_ts,
    _build_transcript_from_messages,
    _extract_convs_for_date,
    _count_convs_for_date,
    _analyse_conversations,
)

# LLM analysis + persona pick
from .llm import (
    _extract_json_result,
    _run_llm_analysis,
    _pick_persona,
)

# Async generator + scheduler
from .generator import _generate_in_background
from .scheduler import (
    _backfill_yesterday_if_missing,
    _scheduler_loop,
    start_report_scheduler,
)

__all__ = [
    # storage
    'DEFAULT_USER_ID',
    '_REPORTS_DIR', '_active_jobs', '_jobs_lock',
    '_update_job', '_get_job', '_clear_job',
    '_report_path', '_save_report', '_load_report',
    # prompts
    '_ANALYSIS_SYSTEM', '_TODO_TOOL_DEFAULTS', '_TODO_TOOL_MAP', '_QUOTES',
    # cost
    '_calendar_cache', '_CALENDAR_CACHE_TTL', '_LEGACY_PRESET_TO_MODEL',
    '_qwen_cny', '_calc_msg_cost_cny', '_scan_costs_in_range',
    '_load_cached_day_costs', '_persist_day_cost',
    'invalidate_day_cost_cache', 'invalidate_cost_cache_for_messages',
    '_get_monthly_costs',
    # todos
    '_normalize_todo_text', '_fuzzy_todo_match',
    '_get_yesterday_carryover', '_get_today_inherited_todos',
    '_get_yesterday_todo_accountability',
    '_mark_yesterday_todos_done', '_close_yesterday_remaining_todos',
    '_merge_manual_state',
    # conversations
    '_safe_int_ts', '_build_transcript_from_messages',
    '_extract_convs_for_date', '_count_convs_for_date',
    '_analyse_conversations',
    # llm
    '_extract_json_result', '_run_llm_analysis', '_pick_persona',
    # async
    '_generate_in_background',
    '_backfill_yesterday_if_missing', '_scheduler_loop',
    'start_report_scheduler',
]
