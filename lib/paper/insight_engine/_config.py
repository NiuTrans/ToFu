"""Insight-engine tunables + the flag/gate/lang-key helpers.

All module constants live here (temperatures, token
ceilings, reader-context caps, the headroom-gate threshold) alongside the three
pure predicates that read them: :func:`insight_gate_fires`,
:func:`insight_enabled`, :func:`insight_lang_key`.
"""

import os

from lib.log import get_logger
from lib.paper.report_artifact_keys import insight_lang_key as insight_lang_key

logger = get_logger(__name__)

# Above the report's temp=0 (insight is a divergent act) but NOT so high that
# strict-JSON extraction breaks: temp=0.7 failed to emit parseable JSON ~1/3 of
# real runs, which then silently returned nothing. 0.45 keeps some divergence
# while restoring reliability; the one-shot repair re-ask below (temp=0) is the
# safety net for the residual failures.
_INSIGHT_TEMPERATURE = 0.45

# Repair re-ask: cap tokens generously — this is pure JSON, no research.
_REPAIR_MAX_TOKENS = 4000

# Rubric scoring is a judgement call, not creative — keep it deterministic.
# max_tokens must comfortably clear the JSON + four passage-citing
# justifications; 1500 sat right at the ceiling and truncated → unparseable
# JSON → a spurious None. Give it ample headroom.
_RUBRIC_TEMPERATURE = 0.0
_RUBRIC_MAX_TOKENS = 3000

# Reader-context caps (kept small — this is a hint, not a corpus dump).
_CTX_LIBRARY_MAX = 8
_CTX_MEMORY_MAX = 6

# Headroom gate: the n=9 A/B showed the section-vs-report win is CONDITIONAL —
# it wins where the report's own insight rubric is LOW (all wins at overall
# baseline <= 3.9) and ties/loses where the report is already insight-saturated
# (all losses at baseline >= 4.5). The split is clean at ~4.0, fixed a-priori
# here so the gated eval isn't tuned post-hoc. The pass only fires when the
# report's OWN insight baseline is at or below this.
INSIGHT_GATE_THRESHOLD = 4.0


def insight_gate_fires(baseline_overall) -> bool:
    """Should the insight pass fire, given the report's own insight-rubric score?

    ``baseline_overall`` is the mean 4-axis rubric score of the plain report
    (arm A). Fires iff there is headroom (baseline <= INSIGHT_GATE_THRESHOLD).
    A ``None`` baseline (scoring failed) fails OPEN → fire (never silently
    withhold on an instrument error; the pass is non-destructive anyway).
    """
    if baseline_overall is None:
        return True
    return baseline_overall <= INSIGHT_GATE_THRESHOLD + 1e-9


def insight_enabled(cfg=None) -> bool:
    """Is the insight second-pass turned on for this report? Three-level chain:

      1. **explicit per-request cfg** ``paperInsightEnabled`` — a headless
         cfg-builder stamps it fail-closed via
         ``personal_scope.apply_headless_personal_defaults``; an opt-in caller
         sets it True. Always wins.
      2. **env** ``TOFU_PAPER_INSIGHT`` — the fleet kill switch
         (back-compat with the prototype flag).
      3. **default ON** — the interactive reader always gets the insight pass.
         The Settings toggle was retired 2026-08-06 (owner: "默认开启就行，
         不需要设计成开关") — there is no user-level off switch anymore;
         headless stays fail-closed via level 1.

    The rubric headroom gate (``insight_gate_fires``) is INDEPENDENT of this
    switch and always applies after it: this switch decides "do we want the
    feature", the gate decides "does THIS report need it".
    """
    if isinstance(cfg, dict) and 'paperInsightEnabled' in cfg:
        return bool(cfg['paperInsightEnabled'])
    env = (os.environ.get('TOFU_PAPER_INSIGHT', '') or '').strip().lower()
    if env:
        return env in ('1', 'true', 'yes', 'on')
    return True
