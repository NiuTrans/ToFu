"""Lazy compatibility facade for owner-scoped daily report services."""

from __future__ import annotations

from importlib import import_module

__all__ = [
    '_REPORTS_DIR', '_active_jobs', '_jobs_lock',
    '_update_job', '_get_job', '_clear_job',
    '_reports_dir_for_owner', '_report_path', '_is_report_date',
    '_save_report', '_save_generated_report', '_update_report', '_load_report',
    '_ANALYSIS_SYSTEM', '_TODO_TOOL_DEFAULTS', '_TODO_TOOL_MAP', '_QUOTES',
    '_calendar_cache', '_CALENDAR_CACHE_TTL', '_LEGACY_PRESET_TO_MODEL',
    '_qwen_cny', '_calc_msg_cost_cny', '_scan_costs_in_range',
    '_load_cached_day_costs', '_persist_day_cost', '_persisted_cost_dates',
    '_should_pin_day', 'invalidate_day_cost_cache',
    'invalidate_cost_cache_for_messages', '_get_monthly_costs',
    '_normalize_todo_text', '_fuzzy_todo_match', '_get_yesterday_carryover',
    '_get_today_inherited_todos', '_get_yesterday_todo_accountability',
    '_mark_yesterday_todos_done', '_close_yesterday_remaining_todos',
    '_merge_manual_state', '_safe_int_ts', '_build_transcript_from_messages',
    '_extract_convs_for_date', '_count_convs_for_date',
    '_activity_counts_for_range', '_analyse_conversations',
    '_extract_json_result', '_run_llm_analysis', '_pick_persona',
    '_generate_in_background', '_backfill_yesterday_if_missing',
    'start_report_scheduler', 'stop_report_scheduler',
]

_EXPORT_MODULES = {
    # Durable report and active-job state.
    '_REPORTS_DIR': 'lib.daily_report.storage',
    '_active_jobs': 'lib.daily_report.storage',
    '_jobs_lock': 'lib.daily_report.storage',
    '_update_job': 'lib.daily_report.storage',
    '_get_job': 'lib.daily_report.storage',
    '_clear_job': 'lib.daily_report.storage',
    '_reports_dir_for_owner': 'lib.daily_report.storage',
    '_report_path': 'lib.daily_report.storage',
    '_is_report_date': 'lib.daily_report.storage',
    '_save_report': 'lib.daily_report.storage',
    '_save_generated_report': 'lib.daily_report.storage',
    '_update_report': 'lib.daily_report.storage',
    '_load_report': 'lib.daily_report.storage',
    # Prompt constants.
    '_ANALYSIS_SYSTEM': 'lib.daily_report.prompts',
    '_TODO_TOOL_DEFAULTS': 'lib.daily_report.prompts',
    '_TODO_TOOL_MAP': 'lib.daily_report.prompts',
    '_QUOTES': 'lib.daily_report.prompts',
    # Cost projection and bounded caches.
    '_calendar_cache': 'lib.daily_report.cost',
    '_CALENDAR_CACHE_TTL': 'lib.daily_report.cost',
    '_LEGACY_PRESET_TO_MODEL': 'lib.daily_report.cost',
    '_qwen_cny': 'lib.daily_report.cost',
    '_calc_msg_cost_cny': 'lib.daily_report.cost',
    '_scan_costs_in_range': 'lib.daily_report.cost',
    '_load_cached_day_costs': 'lib.daily_report.cost',
    '_persist_day_cost': 'lib.daily_report.cost',
    '_persisted_cost_dates': 'lib.daily_report.cost',
    '_should_pin_day': 'lib.daily_report.cost',
    'invalidate_day_cost_cache': 'lib.daily_report.cost',
    'invalidate_cost_cache_for_messages': 'lib.daily_report.cost',
    '_get_monthly_costs': 'lib.daily_report.cost',
    # TODO carryover and manual-state merge.
    '_normalize_todo_text': 'lib.daily_report.todos',
    '_fuzzy_todo_match': 'lib.daily_report.todos',
    '_get_yesterday_carryover': 'lib.daily_report.todos',
    '_get_today_inherited_todos': 'lib.daily_report.todos',
    '_get_yesterday_todo_accountability': 'lib.daily_report.todos',
    '_mark_yesterday_todos_done': 'lib.daily_report.todos',
    '_close_yesterday_remaining_todos': 'lib.daily_report.todos',
    '_merge_manual_state': 'lib.daily_report.todos',
    # Conversation projection and report analysis.
    '_safe_int_ts': 'lib.daily_report.conversations',
    '_build_transcript_from_messages': 'lib.daily_report.conversations',
    '_extract_convs_for_date': 'lib.daily_report.conversations',
    '_count_convs_for_date': 'lib.daily_report.conversations',
    '_activity_counts_for_range': 'lib.daily_report.conversations',
    '_analyse_conversations': 'lib.daily_report.conversations',
    '_extract_json_result': 'lib.daily_report.llm',
    '_run_llm_analysis': 'lib.daily_report.llm',
    '_pick_persona': 'lib.daily_report.llm',
    # Explicit background execution.
    '_generate_in_background': 'lib.daily_report.generator',
    '_backfill_yesterday_if_missing': 'lib.daily_report.scheduler',
    'start_report_scheduler': 'lib.daily_report.scheduler',
    'stop_report_scheduler': 'lib.daily_report.scheduler',
}

_CHILD_MODULES = {
    'conversations', 'cost', 'generator', 'llm', 'prompts', 'scheduler',
    'storage', 'todos',
}


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None and name in _CHILD_MODULES:
        module_name = f'lib.daily_report.{name}'
    if module_name is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    module = import_module(module_name)
    value = module if name in _CHILD_MODULES else getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__) | _CHILD_MODULES)
