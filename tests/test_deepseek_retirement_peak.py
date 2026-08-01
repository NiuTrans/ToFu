"""tests/test_deepseek_retirement_peak.py — P2-B guards (pt_c1db412d445b44eb).

Two independent halves, one suite.

A. Retired-name cleanup
   ``deepseek-chat`` / ``deepseek-reasoner`` were retired by DeepSeek on
   2026-07-24 15:59 UTC (api-docs.deepseek.com/updates, 2026-04-24 entry:
   "will be discontinued in three months (2026-07-24)"). They must be gone
   from the three reference surfaces — ``DEFAULT_SLOT_CONFIGS``,
   ``MODEL_PRICING``, ``_THINKING_FORMAT_HINTS`` — with a dated NOTE left
   exactly where a future editor adding a deepseek row will read it, and no
   test premise may depend on their presence.

B. Peak-hour pricing mechanism
   DeepSeek announced (api-docs.deepseek.com/quick_start/pricing, verified
   2026-07-31): ALL billing items 2x during 09:00-12:00 + 14:00-18:00
   Beijing time (UTC+8, no DST), effective date "subject to official
   announcement" (TBA). The mechanism ships INERT (``effective_from=None``)
   and must:
     * scale ``input`` + ``output`` unit prices when active and inside a
       window — the cache multipliers are RELATIVE to input, so all four
       billing items (uncached in / cache write / cache read / out) scale
       together, matching "applicable to all billing items";
     * evaluate the timestamp PER LOOKUP (``at``) so historical cost
       recomputation (daily_report backfill) bills each message at ITS OWN
       time, not at the rescan's wall clock;
     * leave the shipped deepseek rows at base prices today.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
     tests/test_deepseek_retirement_peak.py -p no:cacheprovider
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_CST = timezone(timedelta(hours=8))


def _cst(y, mo, d, h, mi=0):
    """Epoch seconds for a Beijing-time wall clock reading."""
    return datetime(y, mo, d, h, mi, tzinfo=_CST).timestamp()


# A synthetic peak schedule already in force (effective 2026-07-01 CST).
_PEAK_BLOCK = {'mul': 2.0, 'windows': [(9, 12), (14, 18)], 'tz_offset': 8,
               'effective_from': _cst(2026, 7, 1, 0)}


# ═══════════════════════════════════════════════════════════════════
#  A. Retired-name cleanup
# ═══════════════════════════════════════════════════════════════════

def test_retired_names_absent_from_slots():
    from lib.llm_dispatch.config._slots import DEFAULT_SLOT_CONFIGS
    assert 'deepseek-chat' not in DEFAULT_SLOT_CONFIGS
    assert 'deepseek-reasoner' not in DEFAULT_SLOT_CONFIGS


def test_retired_names_absent_from_pricing():
    from lib.pricing import MODEL_PRICING
    assert 'deepseek-chat' not in MODEL_PRICING
    assert 'deepseek-reasoner' not in MODEL_PRICING


def test_retired_reasoner_matches_no_thinking_hint():
    """The 'none' hint for the dead name must go — a still-registered legacy
    alias now falls through to auto-detect like any unknown model."""
    from lib.llm_dispatch.discovery._thinking import _THINKING_FORMAT_HINTS
    hits = [fmt for pat, fmt in _THINKING_FORMAT_HINTS
            if pat.search('deepseek-reasoner')]
    assert hits == [], f'retired name still matched by hints: {hits}'


def test_slots_retirement_note_anchored_in_deepseek_section():
    """A row deletion without a dated reason rots into legend — the NOTE must
    sit INSIDE the DeepSeek section where the next row-editor reads it."""
    src = Path('lib/llm_dispatch/config/_slots.py').read_text(encoding='utf-8')
    start = src.index('# ── DeepSeek ──')
    end = src.index('# ──', start + 10)
    section = src[start:end]
    assert 'deepseek-chat' in section and 'deepseek-reasoner' in section, (
        'the DeepSeek section must name what was removed')
    assert '2026-07-24' in section, (
        'the removal note must carry the retirement date')


def test_pricing_retirement_note_anchored_above_v4_rows():
    src = Path('lib/pricing/_tables.py').read_text(encoding='utf-8')
    anchor = src.index("'deepseek-v4-pro':")
    window = src[max(0, anchor - 1200):anchor]
    assert 'deepseek-chat' in window and 'deepseek-reasoner' in window
    assert '2026-07-24' in window


def test_live_text_only_deepseek_entries_for_vision_probe():
    """Premise anchors for the screenshot no-vision lane: these LIVE entries
    are what tests may rely on as 'real text-only models' (measured, not
    assumed) now that the retired names are gone."""
    from lib.model_info import model_supports_vision
    assert model_supports_vision('deepseek-v3.2') is False
    assert model_supports_vision('deepseek-v4-flash') is False


def test_v4_thinking_hint_survives_cleanup():
    """Regression anchor: removing the dead-name hint must not disturb the
    live deepseek-v4 → thinking_type mapping."""
    from lib.llm_dispatch.discovery._thinking import _detect_thinking_format
    assert _detect_thinking_format(
        [{'model_id': 'deepseek-v4-flash'}], brand='') == 'thinking_type'


# ═══════════════════════════════════════════════════════════════════
#  B. Peak-hour pricing mechanism
# ═══════════════════════════════════════════════════════════════════

def test_peak_multiplier_inert_cases():
    from lib.pricing._peak import peak_multiplier
    assert peak_multiplier({}) == 1.0
    assert peak_multiplier({'input': 1}) == 1.0
    # Announced but NOT yet in force (the shipped deepseek rows' state).
    assert peak_multiplier(
        {'peak': dict(_PEAK_BLOCK, effective_from=None)},
        at=_cst(2026, 8, 3, 10)) == 1.0
    # In force only in the future.
    assert peak_multiplier(
        {'peak': dict(_PEAK_BLOCK, effective_from=_cst(2027, 1, 1, 0))},
        at=_cst(2026, 8, 3, 10)) == 1.0
    # Malformed block → 1.0 and NEVER raises (cost paths must not crash).
    assert peak_multiplier(
        {'peak': {'mul': 2.0, 'windows': [('a', 'b')], 'tz_offset': 8,
                  'effective_from': 1}}, at=100.0) == 1.0


@pytest.mark.parametrize('hour,minute,expected', [
    (8, 59, 1.0), (9, 0, 2.0), (11, 59, 2.0), (12, 0, 1.0),
    (13, 30, 1.0), (14, 0, 2.0), (17, 59, 2.0), (18, 0, 1.0),
])
def test_peak_window_boundaries(hour, minute, expected):
    """Windows are [start, end) in local (Beijing) hour-of-day."""
    from lib.pricing._peak import peak_multiplier
    assert peak_multiplier({'peak': dict(_PEAK_BLOCK)},
                           at=_cst(2026, 8, 3, hour, minute)) == expected


def test_peak_tz_offset_shifts_window():
    """The SAME UTC instant is peak under +8 and off-peak under +0 — proves
    the offset is applied, not ignored."""
    from lib.pricing._peak import peak_multiplier
    at = datetime(2026, 8, 3, 2, 0, tzinfo=timezone.utc).timestamp()  # 10:00 CST
    assert peak_multiplier({'peak': dict(_PEAK_BLOCK)}, at=at) == 2.0
    assert peak_multiplier({'peak': dict(_PEAK_BLOCK, tz_offset=0)},
                           at=at) == 1.0


@pytest.fixture
def peak_override():
    """A live peak-active pricing row registered as a provider override."""
    from lib.pricing import clear_provider_pricing, set_provider_pricing
    set_provider_pricing('t-peak', 'fake-ds-v9', {
        'input': 1.0, 'output': 2.0, 'cacheWriteMul': 1.0,
        'cacheReadMul': 0.1, 'name': 'Fake DS', 'peak': dict(_PEAK_BLOCK)})
    yield
    clear_provider_pricing('t-peak')


def test_lookup_pricing_scales_at_peak(peak_override):
    """At peak the unit prices double; cache multipliers stay RELATIVE
    (0.1 of the now-doubled input price); the lookup stamps peakMul."""
    from lib.pricing import lookup_pricing
    hit = lookup_pricing('fake-ds-v9', 't-peak', at=_cst(2026, 8, 3, 10, 0))
    assert hit['input'] == 2.0 and hit['output'] == 4.0
    assert hit['cacheReadMul'] == 0.1
    assert hit['peakMul'] == 2.0
    off = lookup_pricing('fake-ds-v9', 't-peak', at=_cst(2026, 8, 3, 13, 0))
    assert off['input'] == 1.0 and off['output'] == 2.0
    assert 'peakMul' not in off


def test_shipped_deepseek_rows_carry_inert_peak_block():
    """The two official-API rows carry the announced schedule INERT
    (effective_from=None — one-line flip when DeepSeek names the date);
    today's lookups return base prices. The Meituan-gateway mirror
    (-huawei) must NOT carry it — the 2x policy is DeepSeek-direct only."""
    from lib.pricing import MODEL_PRICING, lookup_pricing
    for mid, base_in in (('deepseek-v4-flash', 0.14), ('deepseek-v4-pro', 0.435)):
        peak = MODEL_PRICING[mid].get('peak')
        assert peak, f'{mid}: peak block missing'
        assert peak['effective_from'] is None
        assert peak['mul'] == 2.0 and peak['windows'] == [(9, 12), (14, 18)]
        resolved = lookup_pricing(mid)
        assert resolved['input'] == base_in
        assert 'peakMul' not in resolved
    assert 'peak' not in MODEL_PRICING['deepseek-v4-flash-huawei']


def test_compute_cost_peak_end_to_end(peak_override, monkeypatch):
    """The single cost engine (display + billing share it) charges 2x for
    EVERY component at peak and stamps the multiplier for transparency."""
    import lib.pricing._peak as peak_mod
    from lib.cost import compute_cost
    usage = {'prompt_tokens': 1_000_000, 'completion_tokens': 1_000_000}
    monkeypatch.setattr(peak_mod, '_utc_now',
                        lambda: _cst(2026, 8, 3, 10, 0))
    r = compute_cost(usage, 'fake-ds-v9', 't-peak')
    assert r['costUsd'] == pytest.approx(6.0)  # 1M×$2 + 1M×$4
    assert r.get('peakMultiplier') == 2.0
    monkeypatch.setattr(peak_mod, '_utc_now',
                        lambda: _cst(2026, 8, 3, 13, 0))
    r2 = compute_cost(usage, 'fake-ds-v9', 't-peak')
    assert r2['costUsd'] == pytest.approx(3.0)
    assert 'peakMultiplier' not in r2


def test_daily_report_bills_message_at_its_own_time(peak_override):
    """Historical recomputation must evaluate the schedule at the MESSAGE's
    timestamp: the same usage billed at 10:00 CST costs 2x its 13:00 self."""
    from lib.daily_report.cost import _calc_msg_cost_cny
    usage = {'prompt_tokens': 1_000_000, 'completion_tokens': 0}
    peak_cost = _calc_msg_cost_cny(usage, 'fake-ds-v9', 't-peak',
                                   at=_cst(2026, 8, 3, 10, 0))
    off_cost = _calc_msg_cost_cny(usage, 'fake-ds-v9', 't-peak',
                                  at=_cst(2026, 8, 3, 13, 0))
    assert off_cost > 0
    assert peak_cost == pytest.approx(off_cost * 2, rel=1e-6)
