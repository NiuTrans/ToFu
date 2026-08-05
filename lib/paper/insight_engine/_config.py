"""Insight-engine tunables + the flag/gate/lang-key helpers.

All module constants live here (the tool-round cap, temperatures, token
ceilings, reader-context caps, the headroom-gate threshold) alongside the three
pure predicates that read them: :func:`insight_gate_fires`,
:func:`insight_enabled`, :func:`insight_lang_key`.
"""

import os

from lib.log import get_logger

logger = get_logger(__name__)

# How many tool-eligible research rounds the insight agent gets before it MUST
# produce its final JSON. Enough to scan the current frontier + open a couple of
# hits, but bounded so the pass stays a cheap add-on to the report.
_MAX_INSIGHT_TOOL_ROUNDS = 6

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
_INSIGHT_LANG_PREFIX = 'insight'

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


def _reading_experience_cfg() -> dict:
    """The ``paper.reading_experience`` section of server_config (hot-reload safe).

    Reads ``lib._SAVED_CONFIG`` at CALL time — ``lib.reload_config()`` rebinds
    that name when the Settings panel saves, so this never serves a stale
    value. Returns {} on any anomaly (missing section, malformed file) — the
    caller's chain then falls through to the env / default levels.
    """
    try:
        import lib as _lib
        saved = getattr(_lib, '_SAVED_CONFIG', None) or {}
        paper = saved.get('paper') if isinstance(saved, dict) else None
        if isinstance(paper, dict):
            rx = paper.get('reading_experience')
            if isinstance(rx, dict):
                return rx
    except Exception as e:
        logger.debug('[Paper:Insight] reading_experience cfg read failed: %s', e)
    return {}


def insight_enabled(cfg=None) -> bool:
    """Is the insight second-pass turned on for this report? Four-level chain:

      1. **explicit per-request cfg** ``paperInsightEnabled`` — a headless
         cfg-builder stamps it fail-closed via
         ``personal_scope.apply_headless_personal_defaults``; an opt-in caller
         sets it True. Always wins.
      2. **server_config** ``paper.reading_experience.insight`` — the user-level
         Settings toggle (Settings → General → 功能模块). The owner's explicit
         choice beats the fleet seed.
      3. **env** ``TOFU_PAPER_INSIGHT`` — the fleet default seed / kill switch
         (back-compat with the prototype flag).
      4. **default ON** — the interactive reader gets the insight pass unless
         someone above said no (owner decision 2026-08-02, design
         docs/PAPER_READING_EXPERIENCE_DESIGN.md §3.4). The headless surface
         never reaches this level — level 1 is always stamped there.

    The rubric headroom gate (``insight_gate_fires``) is INDEPENDENT of this
    switch and always applies after it: this switch decides "do we want the
    feature", the gate decides "does THIS report need it".
    """
    if isinstance(cfg, dict) and 'paperInsightEnabled' in cfg:
        return bool(cfg['paperInsightEnabled'])
    rx = _reading_experience_cfg()
    if 'insight' in rx:
        return bool(rx['insight'])
    env = (os.environ.get('TOFU_PAPER_INSIGHT', '') or '').strip().lower()
    if env:
        return env in ('1', 'true', 'yes', 'on')
    return True


def insight_lang_key(ui_lang: str) -> str:
    """Composite ``paper_reports.lang`` key for a persisted insight pass.

    ``insight:<ui_lang>`` — a separate row from the plain ``'en'`` / ``'zh'``
    report, so persisting an insight NEVER overwrites the fidelity report and
    the two can be diffed. Mirrors Review Mode's ``review:<venue>:<uilang>``.
    """
    return f'{_INSIGHT_LANG_PREFIX}:{ui_lang or "en"}'
