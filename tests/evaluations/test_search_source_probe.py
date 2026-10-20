"""External source probes freeze selection and never turn missing evidence into wins."""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluations.search_quality import source_corpus as corpus
from evaluations.search_quality import source_probe as probe
from evaluations.search_quality.contracts import DDGS_VERSION

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def csv_bytes(rows=None):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=corpus.FIELDS)
    writer.writeheader()
    writer.writerows(rows if rows is not None else [source_row()])
    return stream.getvalue().encode()


def source_row(identifier='7', topic='Art'):
    return {'original_index': identifier, 'problem': 'Which city hosted the exhibition?',
            'answer': 'private reference label', 'topic': topic, 'answer_type': 'Place',
            'multi_step': 'False', 'requires_reasoning': 'true',
            'urls': 'https://reference.example/a,b?x=1#section,https://archive.example/source)'}


def cases():
    return corpus.parse_rows(csv_bytes([source_row(), source_row('8', 'History')]))


def test_corpus_retains_exact_questions_and_reference_punctuation_but_discards_answers():
    rows = corpus.parse_rows(csv_bytes())
    assert rows[0]['query'] == source_row()['problem']
    assert rows[0]['reference_urls'] == ['https://reference.example/a,b?x=1#section',
                                       'https://archive.example/source)']
    assert rows[0]['multi_step'] is False and rows[0]['requires_reasoning'] is True
    assert 'private reference label' not in json.dumps(rows)


@pytest.mark.parametrize('mutation', ['duplicate', 'topic', 'flag', 'credential',
                                     'long_question', 'missing_column', 'extra_column'])
def test_invalid_corpus_rows_are_rejected_not_silently_excluded(mutation):
    row = source_row()
    if mutation == 'topic':
        row['topic'] = 'unregistered topic'
    elif mutation == 'flag':
        row['multi_step'] = 'unknown'
    elif mutation == 'credential':
        row['urls'] = 'https://name:password@reference.example/a,https://archive.example/b'
    elif mutation == 'long_question':
        row['problem'] = 'q' * 501
    raw = csv_bytes([row, row] if mutation == 'duplicate' else [row])
    if mutation == 'missing_column':
        raw = raw.replace(b'answer_type,', b'')
    elif mutation == 'extra_column':
        raw = raw.replace(b'urls\r\n', b'urls,extra\r\n')
    with pytest.raises(ValueError):
        corpus.parse_rows(raw)


def test_load_refuses_wrong_release_and_population(tmp_path, monkeypatch):
    path = tmp_path / 'corpus.csv'
    raw = csv_bytes()
    path.write_bytes(raw)
    with pytest.raises(ValueError, match='checksum'):
        corpus.load_corpus(path)
    monkeypatch.setattr(corpus, 'CSV_SHA256', hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match='population'):
        corpus.load_corpus(path)
    monkeypatch.setattr(corpus, 'DATASET_BYTES', len(raw) - 1)
    with pytest.raises(ValueError, match='byte budget'):
        corpus.read_bytes(path)


def test_sampling_balances_topics_is_order_independent_and_preserves_inputs():
    rows = corpus.parse_rows(csv_bytes([source_row(str(i), topic)
        for i, topic in enumerate(['Art'] * 8 + ['History'] * 8)]))
    before = copy.deepcopy(rows)
    selected = corpus.select_cases(rows, 2, 7)
    assert selected == corpus.select_cases(list(reversed(rows)), 2, 7)
    assert len(selected) == 4
    assert [r['topic'] for r in selected].count('Art') == 2
    assert selected != corpus.select_cases(rows, 2, 8)
    assert rows == before
    with pytest.raises(ValueError, match='fewer'):
        corpus.select_cases(rows[:1], 2)


def test_metrics_distinguish_reference_url_host_hint_and_unannotated_evidence():
    case = cases()[0]
    response = {'results': [{'url': 'https://reference.example/unannotated'},
                            {'url': 'https://REFERENCE.example/a,b?x=1#other'}]}
    result = probe.source_metrics(case, response)
    assert result['reference_hit_at_6'] and not result['reference_hit_at_1']
    assert result['reference_reciprocal_rank'] == 0.5
    assert result['exact_reference_host_seen']
    for other in ('http://reference.example/a,b?x=1', 'https://reference.example/a,b?x=2',
                  'https://reference.example.evil.test/a,b?x=1'):
        assert not probe.source_metrics(case, {'results': [{'url': other}]})['reference_hit_at_6']
    assert 'relevance' not in result and 'answer_correct' not in result


def test_error_results_cannot_earn_reference_credit():
    result = probe.source_metrics(cases()[0], {'error': 'worker_timeout',
        'results': [{'url': cases()[0]['reference_urls'][0]}]})
    assert not result['reference_hit_at_6']
    assert result['returned_count'] == 0
    with pytest.raises(ValueError):
        probe.source_metrics(cases()[0], {})


def test_raw_trace_never_counts_as_displayed_source_evidence():
    result = probe.source_metrics(cases()[0], {'results': [], 'source_trace': {
        'candidates': [{'engine': 'fixture', 'url': cases()[0]['reference_urls'][0]}],
        'truncated': True}})
    assert result['raw_reference_url_seen'] and result['source_trace_truncated']
    assert not result['reference_hit_at_6'] and result['returned_count'] == 0


def registration(tmp_path, fetch_top=1):
    path = tmp_path / 'corpus.csv'
    path.write_bytes(csv_bytes())
    value = probe.plan(ROOT, cases(), 7, 1, fetch_top)
    value['dataset']['csv_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    return value, SimpleNamespace(output=tmp_path / 'results', corpus=path, fetch_top=fetch_top)


class FakeWorkers:
    proxy = 'private-route-value'

    def __init__(self, registration, fault=None):
        self.registration, self.fault, self.calls = registration, fault, []

    def call(self, arm, operation, arguments, **kwargs):
        self.calls.append((arm, operation, arguments))
        common = {'worker_settled': True, 'worker_pid': 123, 'worker_seconds': 0.1}
        if operation == 'identity':
            identity = ({'tofu_source_sha256': self.registration['tofu_source_sha256']} if arm == 'tofu'
                        else {'ddgs': DDGS_VERSION, 'ddgsSourceSha256': 'd' * 64})
            if self.fault == 'identity':
                identity = {}
            if self.fault == 'ddgs_version' and arm == 'ddgs':
                identity['ddgs'] = 'unexpected'
            return {**common, 'identity': identity}
        if operation == 'search':
            if self.fault == 'unsettled':
                return {**common, 'worker_settled': False, 'results': []}
            if self.fault == 'worker_exit':
                return {**common, 'error': 'worker_exit'}
            rows = [{'url': 'https://reference.example/a,b?x=1#section'}]
            return {**common, 'results': rows,
                    **({'error': 'worker_timeout'} if self.fault == 'timeout' else {})}
        assert operation == 'fetch'
        if self.fault == 'fetch_exit':
            return {**common, 'error': 'worker_exit'}
        return {**common, 'url': arguments['url'], 'text': 'Source text.'}


def test_live_uses_same_question_and_only_observed_fetch_targets(tmp_path, capsys):
    plan, args = registration(tmp_path)
    args.fetch_top = 0  # The frozen plan, not a second mutable argument, owns dispatch.
    workers = FakeWorkers(plan)
    probe.live(ROOT, args, plan, workers)
    summary = json.loads((args.output / 'summary.json').read_text())
    assert summary['complete'] and summary['comparable']
    assert all(arm['attempts'] == 2 and arm['reference_hits_at_6'] == 2
               and arm['nonempty_fetches'] == 2 for arm in summary['arms'].values())
    searches = [args for _, op, args in workers.calls if op == 'search']
    assert searches == [{'query': row['query'], 'include_source_trace': True}
                        for row in plan['cases'] for _ in range(2)]
    assert [arm for arm, op, _ in workers.calls if op == 'search'] == ['tofu', 'ddgs', 'ddgs', 'tofu']
    text = ''.join(p.read_text() for p in args.output.rglob('*.json')) + capsys.readouterr().out
    assert workers.proxy not in text
    assert 'private reference label' not in text
    assert not summary['leadership_claim']


def test_tool_timeout_stays_in_denominator_and_does_not_authorize_fetch(tmp_path):
    plan, args = registration(tmp_path)
    workers = FakeWorkers(plan, 'timeout')
    probe.live(ROOT, args, plan, workers)
    summary = json.loads((args.output / 'summary.json').read_text())
    assert summary['complete'] and summary['comparable']
    assert all(arm['reference_hits_at_6'] == 0 and arm['attempts'] == 2
               and arm['search_errors'] == {'worker_timeout': 2} for arm in summary['arms'].values())
    assert not any(op == 'fetch' for _, op, _ in workers.calls)


@pytest.mark.parametrize('fault', ['unsettled', 'worker_exit', 'identity', 'ddgs_version', 'fetch_exit'])
def test_unproven_ownership_or_identity_stops_later_dispatch(tmp_path, fault):
    plan, args = registration(tmp_path)
    workers = FakeWorkers(plan, fault)
    with pytest.raises((RuntimeError, ValueError)):
        probe.live(ROOT, args, plan, workers)
    summary = json.loads((args.output / 'summary.json').read_text())
    assert not summary['complete'] and not summary['comparable']
    assert sum(op == 'search' for _, op, _ in workers.calls) <= 1
    assert sum(op == 'fetch' for _, op, _ in workers.calls) == (1 if fault == 'fetch_exit' else 0)
    if fault in {'worker_exit', 'fetch_exit'}:
        assert sum(arm['attempts'] for arm in summary['arms'].values()) == 1


def test_changed_source_or_corpus_refuses_comparability_and_missing_rows_are_not_complete(tmp_path):
    plan, args = registration(tmp_path)
    probe.live(ROOT, args, plan, FakeWorkers(plan))
    records = [json.loads(p.read_text()) for p in args.output.glob('*/record.json')]
    assert not probe.summarize(plan, records[:-1], source_unchanged=True,
                               corpus_unchanged=True)['complete']
    for source, data in ((False, True), (True, False)):
        result = probe.summarize(plan, records, source_unchanged=source, corpus_unchanged=data)
        assert result['complete'] and not result['comparable']
    with pytest.raises(ValueError, match='Duplicate'):
        probe.summarize(plan, records + records[:1], source_unchanged=True, corpus_unchanged=True)


def test_default_cli_only_writes_an_offline_plan(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(probe, 'load_corpus', lambda path: cases())
    monkeypatch.setattr(probe, 'Workers', lambda *a, **k: pytest.fail('Offline plan created workers'))
    assert probe.main(['--corpus', str(tmp_path / 'local.csv'), '--per-topic', '1']) == 0
    value = json.loads(capsys.readouterr().out)
    assert len(value['operations']) == 4 and value['model_calls'] == 0
    assert value['dataset']['answers_used'] is False
