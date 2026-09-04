"""lib/optimizer/analyzer/_model.py — EvidenceBundle + gather_evidence.

The ``EvidenceBundle`` dataclass (imported by proposer.py) plus the
public ``gather_evidence`` orchestrator that fans out to every ``_collect_*``
source and assembles the compact 24 h bundle.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from lib.identity import PrincipalContext
from lib.log import get_logger

from lib.optimizer import analyzer as _facade
from ._logs import _collect_app_log_signals, _safe_tail_lines
from ._audit import _collect_audit_log_evidence
from ._issues import _collect_error_log_evidence, _collect_recurring_issues
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

def gather_evidence(
    *,
    principal: PrincipalContext,
    window_hours: int = 24,
) -> EvidenceBundle:
    """Build an EvidenceBundle covering the past ``window_hours``."""
    if not isinstance(principal, PrincipalContext):
        raise TypeError('optimizer evidence requires PrincipalContext')
    principal.require_scope('optimizer:maintain')
    owner_user_id = principal.require_owner(context='optimizer evidence')
    from runtime_guards import resolve_deployment_mode

    # Personal mode has one composition-owned user, so its process logs are an
    # owner-local evidence source. Distributed logs lack a trustworthy owner
    # on every line and must never be fed to an owner's optimizer prompt.
    allow_unowned_observability = resolve_deployment_mode() == 'personal'
    now_local = datetime.now()
    cutoff_local = now_local - timedelta(hours=window_hours)
    cutoff_utc = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    bundle = EvidenceBundle(
        window_hours=window_hours,
        generated_at=now_local.isoformat(),
    )

    if allow_unowned_observability:
        # Reuse one immutable tail across both app-log consumers.  Compute the
        # post-apply metrics while it is live, then release it before loading
        # another bounded log tail.
        app_log_lines = tuple(_safe_tail_lines(_facade.APP_LOG))
        app_signals = _collect_app_log_signals(
            cutoff_local, log_lines=app_log_lines)
        prior_actions = _compute_post_apply_metrics(
            cutoff_local,
            owner_user_id=owner_user_id,
            allow_unowned_observability=True,
            app_log_lines=app_log_lines,
        )
        del app_log_lines
    else:
        app_signals = {
            'tool_call_counts': {},
            'tool_error_counts': {},
            'irrelevant_dropped_domains': [],
            'warn_excerpts': [],
            'fetch_timeout_count': 0,
            'fetch_failure_count': 0,
            'rate_limit_429_count': 0,
            'prompt_too_long_count': 0,
            'context_near_full_count': 0,
            'compaction_trigger_count': 0,
        }
        prior_actions = _compute_post_apply_metrics(
            cutoff_local,
            owner_user_id=owner_user_id,
            allow_unowned_observability=False,
            app_log_lines=(),
        )
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

    # Audit evidence is owner-filtered in every deployment mode.  Error-log
    # evidence is personal-only because raw process lines carry no owner.
    audit_log_lines = tuple(_safe_tail_lines(_facade.AUDIT_LOG_FILE))
    audit_evidence = _collect_audit_log_evidence(
        cutoff_utc,
        owner_user_id=owner_user_id,
        allow_unowned=allow_unowned_observability,
        log_lines=audit_log_lines,
    )
    del audit_log_lines
    bundle.model_switch_events = audit_evidence['model_switch_events']
    bundle.audit_event_counts = audit_evidence['audit_event_counts']
    optimizer_audit = audit_evidence['optimizer_events']
    audit_issue_clusters = audit_evidence['tool_error_clusters']
    error_issue_clusters: dict[str, dict] = {}
    if allow_unowned_observability:
        error_log_lines = tuple(_safe_tail_lines(
            _facade.ERROR_LOG, max_bytes=2 * 1024 * 1024))
        bundle.error_log_excerpts, error_issue_clusters = (
            _collect_error_log_evidence(
                cutoff_local, log_lines=error_log_lines)
        )
        del error_log_lines
    bundle.recurring_issues = _collect_recurring_issues(
        cutoff_local,
        cutoff_utc,
        owner_user_id=owner_user_id,
        allow_unowned=allow_unowned_observability,
        audit_issue_clusters=audit_issue_clusters,
        error_issue_clusters=error_issue_clusters,
    )

    sched = _collect_scheduler_signals(owner_user_id=owner_user_id)
    bundle.failing_scheduled_tasks = sched['failing_scheduled_tasks']
    bundle.idle_proactive_tasks = sched['idle_proactive_tasks']

    cost = _collect_cost_outliers(owner_user_id=owner_user_id)
    bundle.top_cost_conversations = cost['top_cost_conversations']

    conv_signals = _collect_conversation_tool_distribution(
        cutoff_local, owner_user_id=owner_user_id)
    # Merge conv-side tool counts with log-side counts (log wins for per-tool
    # invocation count, conv-side fills any gaps)
    merged = dict(bundle.tool_call_counts)
    for k, v in conv_signals['tool_counts'].items():
        merged[k] = max(merged.get(k, 0), v)
    bundle.tool_call_counts = merged
    bundle.top_search_domains = conv_signals['search_urls']

    bundle.daily_report_snippets = _collect_daily_report_snippets(
        days=7, owner_user_id=owner_user_id)
    bundle.prior_actions = prior_actions

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
