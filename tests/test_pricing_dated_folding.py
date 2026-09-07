"""tests/test_pricing_dated_folding.py — dated-checkpoint pricing pins.

Bug class (reported 2026-09-04): a vendor GA's dated pin —
``deepseek-v4-pro-0813``, the exact checkpoint served behind the rolling
``deepseek-v4-pro`` alias — had NO price anywhere. ``MODEL_PRICING`` only
knew the rolling alias, so dispatch cost, pricing-tier tagging, and
discovery enrichment all booked zero for the exact-checkpoint id.
Hand-adding one row per pin does not scale (the next ``-0915`` pin would
miss again), so the lookup itself folds ONE trailing ``-MMDD`` /
``-YYYYMMDD`` stamp and retries the rolling alias's row —
``lib.pricing._tables.pricing_for_model``. Every former direct
``MODEL_PRICING.get(model_id)`` consumer now goes through that seam.

Also pinned here: the Fable 5.1 rows (GA 2026-09-01, $10/$50 per 1M,
cache-read 0.025x) across pricing / slots / aliases / release dates — the
"new flagship is invisible until someone hand-edits five files" half of
the same complaint.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
     tests/test_pricing_dated_folding.py -p no:cacheprovider
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
#  1. The folding seam — pricing_for_model
# ═══════════════════════════════════════════════════════════════════

def test_dated_pin_folds_to_rolling_alias():
    """deepseek-v4-pro-0813 inherits the rolling alias's row, peak schedule
    included — it is served behind the same official endpoint."""
    from lib.pricing import pricing_for_model
    row = pricing_for_model('deepseek-v4-pro-0813')
    assert row is not None
    assert row['input'] == 0.66 and row['output'] == 1.98
    assert row['cacheReadMul'] == 0.0333
    assert 'peak' in row


def test_flash_dated_pin_folds():
    from lib.pricing import pricing_for_model
    row = pricing_for_model('deepseek-v4-flash-0731')
    assert row is not None and row['input'] == 0.22 and row['output'] == 0.66


def test_eight_digit_claude_snapshot_folds():
    """The Claude -YYYYMMDD snapshot shape folds the same way."""
    from lib.pricing import pricing_for_model
    row = pricing_for_model('claude-haiku-4-5-20251001')
    assert row is not None and row['name'] == 'Claude Haiku 4.5'


def test_exact_id_always_wins():
    from lib.pricing import MODEL_PRICING, pricing_for_model
    assert pricing_for_model('deepseek-v4-pro') is MODEL_PRICING['deepseek-v4-pro']


def test_non_date_suffix_never_folds():
    """'-tencent' is a cloud mirror, not a date — the exact row must be the
    one returned, and the folding path must not strip it."""
    from lib.pricing import pricing_for_model
    row = pricing_for_model('deepseek-v4-pro-tencent')
    assert row is not None and row['name'] == 'DeepSeek V4 Pro (Tencent)'


def test_iteration_stamp_folds_to_no_row_and_misses():
    """LongCat -2601-style stamps fold to an id with no row: the outcome is
    a miss — same as before the mechanism existed, never a wrong row."""
    from lib.pricing import pricing_for_model
    assert pricing_for_model('longcat-flash-thinking-2601') is None


def test_unknown_model_stays_none():
    from lib.pricing import pricing_for_model
    assert pricing_for_model('deepseek-v5-ultra-9999') is None
    assert pricing_for_model('') is None
    assert pricing_for_model(None) is None


def test_lookup_pricing_integrates_folding_and_returns_a_copy():
    from lib.pricing import MODEL_PRICING, lookup_pricing
    # Fixed off-peak instant (2026-09-04 13:00 CST): the deepseek peak
    # schedule is in force since 2026-08-16, so wall-clock lookups double
    # inside peak windows.
    at = datetime(2026, 9, 4, 13, 0, tzinfo=timezone(timedelta(hours=8))).timestamp()
    resolved = lookup_pricing('deepseek-v4-pro-0813', at=at)
    assert resolved is not None
    assert resolved['_pricingSource'] == 'model_table'
    assert resolved['input'] == 0.66
    resolved['input'] = 999.0
    assert MODEL_PRICING['deepseek-v4-pro']['input'] == 0.66


def test_no_consumer_bypasses_the_folding_seam():
    """The former direct ``MODEL_PRICING.get(model_id)`` call sites must stay
    converted — a re-introduced bypass silently un-prices dated pins again.
    ``build_rate_card`` is the one exempt consumer: it exports the table's
    own keys, so folding is meaningless there."""
    converted = (
        'lib/llm_dispatch/config/_pricing.py',
        'lib/llm_dispatch/discovery/_discover.py',
        'lib/llm_dispatch/discovery/_capabilities.py',
    )
    for rel in converted:
        src = Path(rel).read_text(encoding='utf-8')
        assert 'pricing_for_model' in src, f'{rel} lost the folding seam'
        assert 'MODEL_PRICING.get(' not in src, f'{rel} re-bypassed the seam'


# ═══════════════════════════════════════════════════════════════════
#  2. Fable 5.1 (GA 2026-09-01) — pricing / slots / aliases / release
# ═══════════════════════════════════════════════════════════════════

FABLE_51_IDS = (
    'fable-5.1',
    'aws.fable-5.1',
    'us.anthropic.fable-5-1-v1:0',
    'claude-fable-5-1',
)


def test_fable_51_pricing_rows():
    """$10/$50 per 1M (Fable 5 list carried over); cache reads cut 75% to
    $0.25/1M → cacheReadMul 0.025."""
    from lib.pricing import MODEL_PRICING
    for model_id in FABLE_51_IDS:
        row = MODEL_PRICING[model_id]
        assert row['input'] == 10.0 and row['output'] == 50.0, model_id
        assert row['cacheWriteMul'] == 1.25 and row['cacheReadMul'] == 0.025, model_id


def test_fable_51_slot_configs():
    from lib.llm_dispatch.config._slots import DEFAULT_SLOT_CONFIGS
    for model_id in FABLE_51_IDS:
        row = DEFAULT_SLOT_CONFIGS[model_id]
        assert {'text', 'vision', 'thinking'} <= row['caps'], model_id


def test_fable_51_alias_group_interchangeable():
    from lib.llm_dispatch.config._aliases import MODEL_ALIASES
    group = MODEL_ALIASES['fable-5.1']
    for model_id in FABLE_51_IDS:
        assert model_id in group


@pytest.mark.parametrize('model_id', FABLE_51_IDS)
def test_fable_51_release_date_all_spellings(model_id):
    from lib.model_info._release import release_date
    assert release_date(model_id) == '2026-09-01'


def test_fable_5_release_date_unchanged():
    from lib.model_info._release import release_date
    assert release_date('claude-fable-5') == '2026-08'


# ═══════════════════════════════════════════════════════════════════
#  3. DeepSeek V4 Pro 0813 — routing metadata (price comes from folding)
# ═══════════════════════════════════════════════════════════════════

def test_0813_in_v4_pro_alias_group():
    """The dated pin is the same checkpoint the rolling alias serves."""
    from lib.llm_dispatch.config._aliases import MODEL_ALIASES
    group = MODEL_ALIASES['deepseek-v4-pro-0813']
    assert 'deepseek-v4-pro' in group
    assert 'deepseek-v4-pro-tencent' in group


def test_0813_slot_config():
    from lib.llm_dispatch.config._slots import DEFAULT_SLOT_CONFIGS
    row = DEFAULT_SLOT_CONFIGS['deepseek-v4-pro-0813']
    assert {'text', 'thinking', 'cheap'} <= row['caps']


def test_0813_release_date_precedes_rolling_alias():
    from lib.model_info._release import release_date
    assert release_date('deepseek-v4-pro-0813') == '2026-08-12'
    assert release_date('deepseek-v4-pro') == '2026-05'
