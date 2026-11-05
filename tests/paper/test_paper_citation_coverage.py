"""Completed citation tools must expose unverified bibliography coverage."""
import pytest

from lib.paper import citations
from lib.tasks_pkg.handlers.paper_citations import _audit_verdict
from lib.tools.paper_citations import PAPER_CITATION_AUDIT_TOOL

pytestmark = pytest.mark.unit


def test_bibliography_beyond_verification_batch_stays_explicitly_incomplete(tmp_path, monkeypatch):
    total = citations.VERIFY_MAX_ENTRIES + 1
    (tmp_path / 'main.tex').write_text('\\bibliography{refs}\n')
    (tmp_path / 'refs.bib').write_text('\n'.join(
        f'@article{{paper{index}, title={{Synthetic paper {index}}}, year={{2025}}}}'
        for index in range(total)))
    inspected = []

    def verify(entry):
        inspected.append(entry['key'])
        return {'key': entry['key'], 'status': 'verified', 'evidence': {}}

    monkeypatch.setattr(citations, '_verify_one', verify)
    monkeypatch.setattr(citations, '_VERIFY_POLITE_DELAY_SECONDS', 0)
    text, status = _audit_verdict(str(tmp_path), 'main.tex')
    assert len(inspected) == citations.VERIFY_MAX_ENTRIES
    assert f'paper{total-1}' not in inspected
    assert status == 'done'  # Execution completed; this is not scientific acceptance.
    assert '引用核验未完成' in text
    assert '全部已核验条目均为真实文献' not in text


def test_backend_unavailability_never_produces_an_all_clear_verdict(tmp_path, monkeypatch):
    (tmp_path / 'main.tex').write_text('\\bibliography{refs}\n')
    (tmp_path / 'refs.bib').write_text('@article{paper, title={Synthetic paper}, year={2025}}')

    def unavailable(_entry):
        raise ConnectionError('offline provider')

    monkeypatch.setattr(citations, '_verify_one', unavailable)
    text, status = _audit_verdict(str(tmp_path), 'main.tex')
    assert status == 'done'
    assert '引用核验未完成' in text
    assert '全部已核验条目均为真实文献' not in text


def test_model_facing_contract_exposes_bounded_unknown_coverage():
    description = PAPER_CITATION_AUDIT_TOOL['function']['description'].lower()
    assert 'bounded' in description
    assert 'unverified' in description
