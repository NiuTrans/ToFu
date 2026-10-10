"""Real-sidecar integration for durable artifact-origin read-back.

``test_artifact_origin_persistence.py`` pins the logic against an in-memory
fake; this file exercises the SAME persist → cross-turn load path against a
real SQL sidecar so the generic ``record.put``/``record.get`` contract
(namespace, key, value shape, command id) is validated end to end, not
assumed. A cross-turn ``read_tool_artifact`` label depends on this surviving
the turn boundary, which the in-task map alone cannot do.
"""

from __future__ import annotations

import pytest

import lib.tool_result_artifacts as tra


pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open'),
              pytest.mark.usefixtures('chat_sidecar')]


REF = 'tool-result:feedface9876'


def test_origin_survives_turn_boundary_via_record_store():
    # Source turn spills an oversized result: origin registered in-task AND
    # mirrored to the durable record store.
    tra.persist_artifact_origin(
        1, REF, tool_name='web_search', display='web_search: quantum annealer',
        llm_round=41, tool_call_id='tc_src')

    # Later turn: the in-task map is gone, only the durable store remains.
    origin = tra.load_artifact_origin(1, REF)
    assert origin is not None
    assert origin['llmRound'] == 41
    assert origin['toolName'] == 'web_search'
    assert origin['display'] == 'web_search: quantum annealer'

    # … and the display-layer lookup resolves it for the new turn's row.
    via_task = tra.artifact_provenance({}, REF, user_id=1)
    assert via_task is not None and via_task['llmRound'] == 41

    # A foreign owner never sees another user's origin.
    assert tra.load_artifact_origin(2, REF) is None


def test_record_store_round_trip_value_shape():
    tra.persist_artifact_origin(
        1, 'tool-result:0badc0de', tool_name='read_files',
        display='Read 1 file: panel.ts', llm_round=3)
    origin = tra.load_artifact_origin(1, 'tool-result:0badc0de')
    assert origin == {
        'toolName': 'read_files',
        'display': 'Read 1 file: panel.ts',
        'llmRound': 3,
    }
