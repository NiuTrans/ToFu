"""Route-preparation observations require complete work and joined disposal."""

import pytest

from benchmarks.model_routing_request import measure_case
from lib.model_routing import dispatch_adapter as adapter
from lib.llm_dispatch import ephemeral

pytestmark = pytest.mark.unit


def test_measurement_receipt_covers_candidates_secrets_and_disposal():
    observed = measure_case(2, 1, None, 3)
    assert observed['candidate_count'] == 4
    assert observed['counts'] == {'read': 1, 'secret': 2, 'minted': 4, 'disposed': 4}
    assert len(observed['receipt_sha256']) == 64
    assert len(observed['elapsed_ns']) == 3 and observed['median_ns'] > 0
    assert len(observed['peak_bytes']) == 3 and observed['median_peak_bytes'] > 0


def test_measurement_rejects_an_omitted_authorized_candidate(monkeypatch):
    original = adapter.mint_routed_slot_group
    def omit_one(*args, **kwargs):
        group = original(*args, **kwargs)
        ephemeral.dispose_ephemeral_slot(group.handles.pop())
        group.candidates.pop()
        return group
    monkeypatch.setattr(adapter, 'mint_routed_slot_group', omit_one)
    with pytest.raises(AssertionError):
        measure_case(2, 1, None, 3)
