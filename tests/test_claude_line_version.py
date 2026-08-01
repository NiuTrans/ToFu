"""tests/test_claude_line_version.py — P1 guards (pt_4b471ab54a5247f6).

Root-cause consolidation: every "Claude generation ≥ N" decision rides ONE
parser — ``lib.model_info._family.claude_line_version`` — shared by the
thinking-generation gate (``is_claude_opus_47``) and the compaction 1M-context
table (``tasks_pkg/compaction/_tokens``).

Why this suite exists (the bug class, twice burned):
  * ``claude-sonnet-5`` (official ID, 2026-06-30, native 1M) was invisible to
    the 1M table: its regex required TWO numeric groups (``sonnet-4.6``
    shape), so the bare single-digit alias fell through to ``'claude': 200K``
    — while the DATED alias (``claude-sonnet-5-20250630``) matched. Opus 5
    only survived because ``is_claude_opus_47`` caught it first — the same
    "two parsers, one lucky rescue" asymmetry this consolidation removes.
  * The old ``is_claude_opus_47`` regex read 8-digit date suffixes as MINOR
    versions: ``claude-opus-4-20250514`` (Opus 4.0!) parsed as
    (4, 20250514) ≥ (4,7) → True, and ``claude-3-opus-20240229`` (gen-3
    shape, version BEFORE the line) as (20240229, 0) → True — both would
    have received adaptive-thinking bodies and 1M windows. Latent, never
    fired in prod (those ids aren't routed), fixed here by construction.

Also in this batch (same epic):
  * P1-B — DeepSeek max-output entry: official page (verified 2026-07-31)
    says V4-Pro/V4-Flash = 384,000; legacy reasoner/R1 = 65,536;
    chat/v3.x = 8,192 (retired aliases still served as gateway mirrors).
    Before this, the whole family fell to the 16,384 unknown-family floor.
  * P2-A — ``claude-sonnet-5`` joins DEFAULT_SLOT_CONFIGS + MODEL_PRICING at
    the promo price $2/$10 (until 2026-08-31, then $3/$15 — the note must
    say so where the next editor reads it).

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
     tests/test_claude_line_version.py -p no:cacheprovider
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _static_limit(model):
    from lib.tasks_pkg.compaction._tokens import _get_static_context_limit
    return _get_static_context_limit({'config': {'model': model}})


# ═══════════════════════════════════════════════════════════════════
#  1. The shared parser — claude_line_version
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('model,line,expected', [
    # bare-major aliases (the shapes the old two-group regex missed)
    ('claude-sonnet-5', 'sonnet', (5, 0)),
    ('claude-opus-5', 'opus', (5, 0)),
    ('claude-fable-5', 'fable', (5, 0)),
    ('fable-5', 'fable', (5, 0)),
    # dated snapshots: the 8-digit suffix is a DATE, never a minor version
    ('claude-sonnet-5-20250630', 'sonnet', (5, 0)),
    ('claude-opus-4-5-20251001', 'opus', (4, 5)),
    ('claude-opus-4-20250514', 'opus', (4, 0)),
    ('claude-haiku-4-5-20251001', 'haiku', (4, 5)),
    # gateway prefixes / build tags
    ('aws.claude-sonnet-5', 'sonnet', (5, 0)),
    ('aws.claude-opus-4.7', 'opus', (4, 7)),
    ('aws.claude-opus-4.6-b', 'opus', (4, 6)),
    ('us.anthropic.claude-sonnet-5-v1:0', 'sonnet', (5, 0)),
    ('us.anthropic.claude-opus-4-7-v1:0', 'opus', (4, 7)),
    ('us.anthropic.fable-5-v1:0', 'fable', (5, 0)),
    ('yuju-claude-opus-5-evaDaily', 'opus', (5, 0)),
    # dotted minor
    ('claude-sonnet-4.6', 'sonnet', (4, 6)),
    # gen-3 shape (version BEFORE the line name) — not parseable, correctly
    ('claude-3-opus-20240229', 'opus', None),
    ('claude-3-5-sonnet-20241022', 'sonnet', None),
    # line not present / not Claude family
    ('claude-opus-5', 'sonnet', None),
    ('gpt-4o', 'opus', None),
    ('deepseek-v4-flash', 'sonnet', None),
])
def test_claude_line_version_shapes(model, line, expected):
    from lib.model_info import claude_line_version
    assert claude_line_version(model, line) == expected


# ═══════════════════════════════════════════════════════════════════
#  2. is_claude_opus_47 — rides the parser; date/gen-3 bugs fixed
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('model,expected', [
    # preserved positives
    ('claude-opus-5', True),
    ('aws.claude-opus-4.7', True),
    ('us.anthropic.claude-opus-4-7-v1:0', True),
    ('yuju-claude-opus-5-evaDaily', True),
    ('claude-opus-4-8', True),
    # preserved negatives
    ('claude-opus-4-6', False),
    ('claude-opus-4-5', False),
    ('claude-sonnet-5', False),
    # FIXED false-positives (were True under the old regex)
    ('claude-opus-4-20250514', False),   # Opus 4.0 snapshot, NOT ≥4.7
    ('claude-3-opus-20240229', False),   # gen-3 shape
])
def test_is_claude_opus_47_on_shared_parser(model, expected):
    from lib.model_info import is_claude_opus_47
    assert is_claude_opus_47(model) is expected


# ═══════════════════════════════════════════════════════════════════
#  3. Compaction 1M-context table — one parser for opus/sonnet/fable
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('model,expected', [
    # the 5-series bare aliases that fell through (RED on HEAD)
    ('claude-sonnet-5', 1_000_000),
    ('aws.claude-sonnet-5', 1_000_000),
    ('us.anthropic.claude-sonnet-5-v1:0', 1_000_000),
    ('claude-fable-5', 1_000_000),
    # anchors that already resolved 1M (must not regress)
    ('claude-opus-5', 1_000_000),
    ('claude-sonnet-5-20250630', 1_000_000),
    ('fable-5', 1_000_000),
    ('aws.fable-5', 1_000_000),
    ('claude-opus-4-6', 1_000_000),
    ('claude-sonnet-4-6', 1_000_000),
    ('aws.claude-opus-4.7', 1_000_000),
    # negative anchors — pre-4.6 Claude stays 200K
    ('claude-opus-4-20250514', 200_000),
    ('claude-3-opus-20240229', 200_000),
    ('claude-sonnet-4-5', 200_000),
    ('claude-haiku-4-5', 200_000),
])
def test_static_context_limit_claude_families(model, expected):
    assert _static_limit(model) == expected


# ═══════════════════════════════════════════════════════════════════
#  4. P1-B — DeepSeek max-output entry (official values, 2026-07-31)
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('model,expected_limit', [
    ('deepseek-v4-pro', 384000),
    ('deepseek-v4-flash', 384000),
    ('deepseek-v4-flash-huawei', 384000),   # gateway mirror of the same model
    ('deepseek-reasoner', 65536),
    ('deepseek-r1-0528', 65536),
    ('deepseek-v3.2', 8192),
    ('deepseek-v3.2-tencent', 8192),
    ('deepseek-chat', 8192),
])
def test_deepseek_max_output_clamped_to_official(model, expected_limit):
    from lib.model_info import _clamp_max_tokens
    assert _clamp_max_tokens(model, 999999) == expected_limit


def test_deepseek_below_limit_passthrough():
    from lib.model_info import _clamp_max_tokens
    assert _clamp_max_tokens('deepseek-v4-flash', 4096) == 4096


# ═══════════════════════════════════════════════════════════════════
#  5. P2-A — claude-sonnet-5 joins slots + pricing (promo price noted)
# ═══════════════════════════════════════════════════════════════════

def test_sonnet5_in_slot_configs():
    from lib.llm_dispatch.config._slots import DEFAULT_SLOT_CONFIGS
    row = DEFAULT_SLOT_CONFIGS['claude-sonnet-5']
    assert {'text', 'vision', 'thinking'} <= row['caps']


def test_sonnet5_in_pricing_at_promo_price():
    from lib.pricing import MODEL_PRICING
    row = MODEL_PRICING['claude-sonnet-5']
    assert row['input'] == 2.0 and row['output'] == 10.0
    assert row['cacheWriteMul'] == 1.25 and row['cacheReadMul'] == 0.10
    assert row['name'] == 'Claude Sonnet 5'


def test_sonnet5_promo_expiry_noted_where_editors_read():
    """The promo ends 2026-08-31 ($2/$10 → $3/$15). A price row without the
    expiry next to it rots into a silently-wrong table — the note must sit
    within a few lines of the row itself."""
    src = Path('lib/pricing/_tables.py').read_text(encoding='utf-8')
    anchor = src.index("'claude-sonnet-5':")
    window = src[max(0, anchor - 400):anchor + 200]
    assert '2026-08-31' in window and '3.0' in window or '$3' in window
