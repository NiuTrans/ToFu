"""lib/optimizer/analyzer/_model.py — EvidenceBundle + gather_evidence.

The ``EvidenceBundle`` dataclass (imported by proposer.py) plus the
public ``gather_evidence`` orchestrator that fans out to every ``_collect_*``
source and assembles the compact 24 h bundle.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from lib.log import get_logger

from ._logs import _collect_app_log_signals, _collect_error_log_excerpts
from ._audit import _collect_audit_events, _collect_audit_secondary
from ._issues import _collect_recurring_issues
from ._signals import (
    _collect_conversation_tool_distribution,
    _collect_cost_outliers,
    _collect_scheduler_signals,
)
from ._domains import _collect_daily_report_snippets
from ._metrics import _compute_post_apply_metrics

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Evidence model
# ══════════════════════════════════════════════════════════

@dataclass
class EvidenceBundle:
    window_hours: int = 24
    generated_at: str = ''
    # Aggregated counters (small, LLM-friendly)
    tool_call_counts: dict[str, int] = field(default_factory=dict)
    tool_error_counts: dict[str, int] = field(default_factory=dict)
    top_search_domains: list[dict] = field(default_factory=list)
    irrelevant_dropped_domains: list[dict] = field(default_factory=list)
    audit_event_counts: dict[str, int] = field(default_factory=dict)
    error_log_excerpts: list[str] = field(default_factory=list)
    warn_log_excerpts: list[str] = field(default_factory=list)
    daily_report_snippets: list[dict] = field(default_factory=list)
    prior_actions: list[dict] = field(default_factory=list)
    # ── Expanded signals for non-search action types ──
    fetch_timeout_count: int = 0
    fetch_failure_count: int = 0
    rate_limit_429_count: int = 0
    prompt_too_long_count: int = 0
    context_near_full_count: int = 0
    compaction_trigger_count: int = 0
    model_switch_events: list[dict] = field(default_factory=list)
    top_cost_conversations: list[dict] = field(default_factory=list)
    failing_scheduled_tasks: list[dict] = field(default_factory=list)
    idle_proactive_tasks: list[dict] = field(default_factory=list)
    # Fingerprint-clustered recurring failures (the recurring/unresolved
    # issue surface the removed project_error_tracker.py once provided).
    recurring_issues: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


# ══════════════════════════════════════════════════════════
#  Public entry point
# ══════════════════════════════════════════════════════════

def gather_evidence(window_hours: int = 24) -> EvidenceBundle:
    """Build an EvidenceBundle covering the past ``window_hours``."""
    now_local = datetime.now()
    cutoff_local = now_local - timedelta(hours=window_hours)
    cutoff_utc = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    bundle = EvidenceBundle(
        window_hours=window_hours,
        generated_at=now_local.isoformat(),
    )

    app_signals = _collect_app_log_signals(cutoff_local)
    bundle.tool_call_counts = app_signals['tool_call_counts']
    bundle.tool_error_counts = app_signals['tool_error_counts']
    bundle.irrelevant_dropped_domains = app_signals['irrelevant_dropped_domains']
    bundle.warn_log_excerpts = app_signals['warn_excerpts']
    bundle.fetch_timeout_count = app_signals['fetch_timeout_count']
    bundle.fetch_failure_count = app_signals['fetch_failure_count']
    bundle.rate_limit_429_count = app_signals['rate_limit_429_count']
    bundle.prompt_too_long_count = app_signals['prompt_too_long_count']
    bundle.context_near_full_count = app_signals['context_near_full_count']
    bundle.compaction_trigger_count = app_signals['compaction_trigger_count']

    audit2 = _collect_audit_secondary(cutoff_utc)
    bundle.model_switch_events = audit2['model_switch_events']

    sched = _collect_scheduler_signals()
    bundle.failing_scheduled_tasks = sched['failing_scheduled_tasks']
    bundle.idle_proactive_tasks = sched['idle_proactive_tasks']

    cost = _collect_cost_outliers()
    bundle.top_cost_conversations = cost['top_cost_conversations']

    conv_signals = _collect_conversation_tool_distribution(cutoff_local)
    # Merge conv-side tool counts with log-side counts (log wins for per-tool
    # invocation count, conv-side fills any gaps)
    merged = dict(bundle.tool_call_counts)
    for k, v in conv_signals['tool_counts'].items():
        merged[k] = max(merged.get(k, 0), v)
    bundle.tool_call_counts = merged
    bundle.top_search_domains = conv_signals['search_urls']

    bundle.audit_event_counts, optimizer_audit = _collect_audit_events(cutoff_utc)
    bundle.error_log_excerpts = _collect_error_log_excerpts(cutoff_local)
    bundle.recurring_issues = _collect_recurring_issues(cutoff_local, cutoff_utc)
    bundle.daily_report_snippets = _collect_daily_report_snippets(days=7)
    bundle.prior_actions = _compute_post_apply_metrics(cutoff_local)

    # optimizer_audit is kept as debug-only detail — expose via warn_log_excerpts
    # so it shows up in the prompt without a dedicated field
    for row in optimizer_audit[:10]:
        bundle.warn_log_excerpts.append('[optimizer_audit] ' + row['details_preview'][:240])

    logger.info('[Optimizer.analyzer] evidence: tools=%d errors=%d top_domains=%d '
                'prior_actions=%d audit_events=%d recurring_issues=%d',
                len(bundle.tool_call_counts), len(bundle.tool_error_counts),
                len(bundle.top_search_domains), len(bundle.prior_actions),
                len(bundle.audit_event_counts), len(bundle.recurring_issues))
    return bundle
