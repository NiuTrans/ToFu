"""lib/optimizer/analyzer — Evidence gathering + post-apply metrics.

Builds a compact ``EvidenceBundle`` summarising what happened in the
last 24 h, plus a ``prior_actions`` section describing the effect of
previously-applied actions.  Everything here is read-only except for
writing computed ``outcome_metric`` values back to the action log.

FACADE PACKAGE
──────────────
This was a single ``analyzer.py`` module; it is now a package split by
evidence source (``_logs`` / ``_audit`` / ``_issues`` / ``_signals`` /
``_domains`` / ``_metrics`` / ``_model``).  This ``__init__`` re-exports
every public and private symbol the rest of the codebase and the test
suite rely on, so ``from lib.optimizer.analyzer import X``,
``from . import analyzer`` and ``from .analyzer import EvidenceBundle``
keep working byte-identically.

Note on mutable module state:  the log-path constants ``APP_LOG`` /
``ERROR_LOG`` / ``AUDIT_LOG_FILE`` and the ``storage`` module live HERE,
on the package object.  Every collector reaches them through this package
(``from lib.optimizer import analyzer as _facade``) at call-time, so
``monkeypatch.setattr(analyzer, "APP_LOG", ...)`` — and hot-reloads — are
observed by the collectors exactly as they were in the pre-split module.
"""

from __future__ import annotations

from lib.log import APP_LOG, AUDIT_LOG_FILE, ERROR_LOG, get_logger  # noqa: F401

from .. import storage  # noqa: F401  (re-exported; monkeypatched by tests)

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Public + private symbol re-exports
# ══════════════════════════════════════════════════════════

from ._logs import (  # noqa: E402,F401
    _APP_LOG_TS_RE,
    _collect_app_log_signals,
    _parse_app_log_ts,
    _safe_tail_lines,
)
from ._audit import (  # noqa: E402,F401
    _audit_ts_aware,
    _collect_audit_events,
    _collect_audit_secondary,
    _parse_audit_line,
)
from ._issues import (  # noqa: E402,F401
    _ERROR_SIGNATURES,
    _classify_error_signature,
    _collect_error_log_excerpts,
    _collect_recurring_issues,
)
from ._signals import (  # noqa: E402,F401
    _collect_conversation_tool_distribution,
    _collect_cost_outliers,
    _collect_scheduler_signals,
    _count_tool_errors,
)
from ._domains import (  # noqa: E402,F401
    _URL_DOMAIN_RE,
    _collect_daily_report_snippets,
    _count_irrelevant_dropped_for_domain,
    _domain_of,
)
from ._metrics import _compute_post_apply_metrics  # noqa: E402,F401
from ._model import EvidenceBundle, gather_evidence  # noqa: E402,F401


__all__ = [
    # public
    'EvidenceBundle',
    'gather_evidence',
    'storage',
    'APP_LOG',
    'ERROR_LOG',
    'AUDIT_LOG_FILE',
    # private (imported by the test suite / kept for parity)
    '_APP_LOG_TS_RE',
    '_URL_DOMAIN_RE',
    '_ERROR_SIGNATURES',
    '_safe_tail_lines',
    '_parse_app_log_ts',
    '_parse_audit_line',
    '_audit_ts_aware',
    '_collect_audit_events',
    '_collect_error_log_excerpts',
    '_collect_app_log_signals',
    '_collect_audit_secondary',
    '_classify_error_signature',
    '_collect_recurring_issues',
    '_collect_scheduler_signals',
    '_collect_cost_outliers',
    '_collect_conversation_tool_distribution',
    '_domain_of',
    '_collect_daily_report_snippets',
    '_count_irrelevant_dropped_for_domain',
    '_count_tool_errors',
    '_compute_post_apply_metrics',
]
