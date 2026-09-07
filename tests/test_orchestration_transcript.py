"""Execution transcript ledger and projection contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lib.orchestration_transcript import (
    OrchestrationTranscript,
    append_role_context,
    subflow_deliverable,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
VERIFIERS = frozenset({'critic', 'reviewer', 'virtual_user'})


def test_transcript_records_detached_rows_and_verifier_lookup():
    transcript = OrchestrationTranscript()
    transcript.record('w', 'worker', 'deliverable', 'completed', '', 1.234)
    transcript.record('c', 'critic', 'VERDICT: STOP', 'completed', '', 0.5)

    snapshot = transcript.snapshot()
    snapshot[0]['output'] = 'mutated'

    assert transcript.snapshot()[0]['output'] == 'deliverable'
    assert transcript.snapshot()[0]['elapsed'] == 1.23
    assert transcript.last_verifier_output(VERIFIERS) == 'VERDICT: STOP'
    assert transcript.last_verifier_role(VERIFIERS) == 'critic'


def test_verifier_output_falls_back_to_latest_turn_but_role_stays_empty():
    transcript = OrchestrationTranscript()
    transcript.record('w', 'worker', 'latest', 'completed', '', 0)
    assert transcript.last_verifier_output(VERIFIERS) == 'latest'
    assert transcript.last_verifier_role(VERIFIERS) == ''


def test_concurrent_records_are_not_lost():
    transcript = OrchestrationTranscript()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(
            lambda index: transcript.record(
                index, 'worker', str(index), 'completed', '', 0,
            ),
            range(100),
        ))
    assert len(transcript.snapshot()) == 100


def test_context_and_subflow_projection_share_transcript_semantics():
    assert append_role_context('', 'worker', 'done') == '[worker]\ndone'
    assert append_role_context('seed', 'worker', 'done') == (
        'seed\n\n[worker]\ndone'
    )
    result = {
        'final': 'scratchpad ending in verifier',
        'transcript': [
            {'role': 'worker', 'output': 'actual deliverable'},
            {'role': 'critic', 'output': 'VERDICT: STOP'},
        ],
    }
    assert subflow_deliverable(
        result, verifier_roles=VERIFIERS,
    ) == 'actual deliverable'


def test_engine_has_one_transcript_state_owner():
    engine = (ROOT / 'lib/orchestration_engine.py').read_text()
    assert 'OrchestrationTranscript(lock=self._lock)' in engine
    assert 'self._transcript: list[dict]' not in engine
    assert 'self._transcript.append(' not in engine
