"""Owner identity must cross every screening orchestration boundary."""

from __future__ import annotations

import pytest

from tofu_trading.trading import screening


@pytest.mark.unit
def test_smart_selection_forwards_owner_to_fund_and_stock_screeners(monkeypatch):
    observed: list[tuple[str, int]] = []

    def fake_screen_assets(*args, uid, **kwargs):
        observed.append(('fund', uid))
        return {'candidates': []}

    def fake_screen_stocks(*args, uid, **kwargs):
        observed.append(('stock', uid))
        return {'candidates': []}

    monkeypatch.setattr(screening, 'screen_assets', fake_screen_assets)
    monkeypatch.setattr(screening, 'screen_and_score_stocks', fake_screen_stocks)

    screening.smart_select_assets(uid=41, risk_level='medium')

    assert observed
    assert {kind for kind, _uid in observed} == {'fund', 'stock'}
    assert {_uid for _kind, _uid in observed} == {41}


@pytest.mark.unit
def test_full_pipeline_forwards_owner_to_screening(monkeypatch):
    observed: list[int] = []

    def fake_screen_assets(*args, uid, **kwargs):
        observed.append(uid)
        return {'candidates': []}

    monkeypatch.setattr(screening, 'screen_assets', fake_screen_assets)

    result = screening.run_screening_pipeline(uid=73)

    assert observed == [73]
    assert result['error'] == 'No candidates found matching criteria'


@pytest.mark.unit
def test_brain_candidate_scan_forwards_owner(monkeypatch):
    from tofu_trading.trading.brain import pipeline
    from tofu_trading.trading_autopilot import cycle

    observed: list[tuple[str, int]] = []

    monkeypatch.setattr(
        cycle,
        '_gather_context',
        lambda db, news_items, *, uid, client=None: {
            'cash': 2_000,
            'held_codes': [],
            'holdings': [],
        },
    )

    def fake_screen_assets(*args, uid, **kwargs):
        observed.append(('fund', uid))
        return {'candidates': []}

    def fake_screen_stocks(*args, uid, **kwargs):
        observed.append(('stock', uid))
        return {'candidates': []}

    monkeypatch.setattr(screening, 'screen_assets', fake_screen_assets)
    monkeypatch.setattr(screening, 'screen_and_score_stocks', fake_screen_stocks)

    pipeline._gather_full_context(object(), uid=97)

    assert observed == [('fund', 97), ('stock', 97)]
