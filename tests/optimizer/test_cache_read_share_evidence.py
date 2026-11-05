"""The nightly optimizer surfaces same-route prompt-cache splits.

The 2026-10-23 logs already showed the root lane reading back 3% of its prompt
while sub-agents on the same model read 94%, but nothing ran the report that
flags it. The optimizer now folds the last day of ``[CacheRoundRecord]`` lines
from the app log and its rotated siblings (not only its 4 MiB tail) through the
shared read-share tally, puts the split first in the proposer prompt, requires
a proposal for it, and keeps it in the run summary even if the LLM ignores it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from unittest import mock

import pytest

from lib.identity import PrincipalContext
from lib.optimizer import analyzer, orchestrator
from lib.optimizer.proposer import _build_user_prompt

pytestmark = pytest.mark.unit

MODEL = 'claude-opus-5.5'
_SPLIT = {'dimension': 'lane', 'model': MODEL,
          'cold_group': 'conversation', 'cold_share': 0.03, 'cold_rounds': 25,
          'warm_group': 'isolated', 'warm_share': 0.95, 'warm_rounds': 25}


def _principal():
    return PrincipalContext.system(
        subject_id='optimizer-cache-split-test', owner_user_id=23,
        scopes={'optimizer:maintain'})


def _rows(stamp, lane, read, write, count):
    rec = json.dumps({'call': 5, 'model': MODEL, 'lane': lane,
                      'cache_read': read, 'cache_write': write})
    return [f'{stamp} [INFO] lib.tasks_pkg.cache_tracking._detect [t]: '
            f'[CacheRoundRecord] {rec}\n'] * count


def _write_logs(tmp_path, monkeypatch):
    now = datetime.now()
    recent = (now - timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')
    stale = (now - timedelta(hours=30)).strftime('%Y-%m-%d %H:%M:%S')
    app_log = tmp_path / 'app.log'
    rotated = tmp_path / f'app.log.{(now - timedelta(days=1)):%Y-%m-%d}'
    # Cold root rounds were rotated out of the live file; warm child rounds
    # are in it. Rounds older than the window and lock files never count.
    rotated.write_text(''.join(
        _rows(recent, 'conversation', 3_000, 97_000, 25)
        + _rows(stale, 'isolated', 0, 100_000, 50)))
    app_log.write_text(''.join(_rows(recent, 'isolated', 95_000, 5_000, 25)))
    (tmp_path / 'app.log.lock').write_text(''.join(
        _rows(recent, 'conversation', 95_000, 5_000, 99)))
    monkeypatch.setattr(analyzer, 'APP_LOG', str(app_log))
    return now


def test_collector_folds_rotated_logs_inside_the_window(tmp_path, monkeypatch):
    now = _write_logs(tmp_path, monkeypatch)
    found = analyzer._collect_cache_read_share_divergence(now - timedelta(hours=24))
    assert [split for split in found if split['dimension'] == 'lane'] == [_SPLIT]


def test_gather_evidence_carries_the_split(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_DEPLOYMENT_MODE', 'personal')
    _write_logs(tmp_path, monkeypatch)
    for constant in ('ERROR_LOG', 'AUDIT_LOG_FILE'):
        path = tmp_path / f'{constant}.log'
        path.write_text('')
        monkeypatch.setattr(analyzer, constant, str(path))
    monkeypatch.setattr(analyzer.storage, 'list_applied_actions',
                        mock.Mock(side_effect=RuntimeError('no db')))

    bundle = analyzer.gather_evidence(principal=_principal(), window_hours=24)

    assert _SPLIT in bundle.cache_read_share_divergence
    assert 'cache_read_share_divergence' not in bundle.unavailable_signals


def test_unreadable_cache_evidence_is_reported_missing(tmp_path, monkeypatch):
    from lib.optimizer.analyzer import _model

    monkeypatch.setenv('TOFU_DEPLOYMENT_MODE', 'personal')
    for constant in ('APP_LOG', 'ERROR_LOG', 'AUDIT_LOG_FILE'):
        path = tmp_path / f'{constant}.log'
        path.write_text('')
        monkeypatch.setattr(analyzer, constant, str(path))
    monkeypatch.setattr(analyzer.storage, 'list_applied_actions',
                        mock.Mock(side_effect=RuntimeError('no db')))
    monkeypatch.setattr(_model, '_collect_cache_read_share_divergence',
                        mock.Mock(side_effect=OSError('rotated log vanished')))

    bundle = analyzer.gather_evidence(principal=_principal(), window_hours=24)

    assert bundle.cache_read_share_divergence == []
    assert 'cache_read_share_divergence' in bundle.unavailable_signals


def test_distributed_deployments_never_read_unowned_cache_records(
        tmp_path, monkeypatch):
    from lib.optimizer.analyzer import _model

    monkeypatch.setenv('TOFU_DEPLOYMENT_MODE', 'distributed')
    for constant in ('APP_LOG', 'ERROR_LOG', 'AUDIT_LOG_FILE'):
        path = tmp_path / f'{constant}.log'
        path.write_text('')
        monkeypatch.setattr(analyzer, constant, str(path))
    monkeypatch.setattr(analyzer.storage, 'list_applied_actions',
                        mock.Mock(side_effect=RuntimeError('no db')))
    collector = mock.Mock(return_value=[_SPLIT])
    monkeypatch.setattr(_model, '_collect_cache_read_share_divergence', collector)

    bundle = analyzer.gather_evidence(principal=_principal(), window_hours=24)

    # Process logs carry no owner, so another tenant's rounds stay unread.
    collector.assert_not_called()
    assert bundle.cache_read_share_divergence == []


def test_prompt_leads_with_the_split_and_requires_a_proposal():
    bundle = analyzer.EvidenceBundle(generated_at='2026-10-29T03:30:00')
    bundle.cache_read_share_divergence = [_SPLIT]
    prompt = _build_user_prompt(bundle)
    split_section = prompt.index('## Prompt-cache read-share splits')
    assert split_section < prompt.index('## Tool call counts')
    assert '"cold_group": "conversation"' in prompt
    assert 'Investigate prompt-cache split on <model>' in prompt
    assert 'Never answer a split with' in prompt


def test_run_summary_keeps_the_split_without_the_llm(monkeypatch):
    evidence = analyzer.EvidenceBundle(window_hours=24)
    evidence.cache_read_share_divergence = [_SPLIT]
    monkeypatch.setattr(orchestrator.applier, 'revert_expired_actions',
                        mock.Mock(return_value=[]))
    monkeypatch.setattr(orchestrator.analyzer, 'gather_evidence',
                        mock.Mock(return_value=evidence))
    monkeypatch.setattr(orchestrator.proposer, 'propose', mock.Mock(return_value=[]))

    summary = orchestrator.run_once(principal=_principal(), dry_run=True)

    assert summary['evidence_summary']['cache_read_share_divergence'] == [_SPLIT]
