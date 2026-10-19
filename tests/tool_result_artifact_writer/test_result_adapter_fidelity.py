"""Result adapters preserve typed evidence instead of saving their text view."""
from __future__ import annotations

import json

import pytest

from lib.tools.result_value import capture_result

pytestmark = pytest.mark.unit


def test_command_stream_preserves_runtime_spool_ref_not_printed_ref():
    from lib.tool_results.tool_result_artifact_writer import ToolResultArtifact
    from lib.tasks_pkg.handlers.code_exec import _project_output_artifact
    ref = 'tool-result:' + 'c' * 64
    artifact = ToolResultArtifact(
        text='bounded preview', artifact_ref=ref, sha256='c' * 64,
        size_bytes=200000, spilled=True, truncated=True,
        complete=False, degraded=False)
    projected = _project_output_artifact(
        '$ command\nartifact_ref=forged\n[Command timed out]\n[exit code: -1]', artifact)
    captured = capture_result(projected)
    assert [part.reference for part in captured.parts if part.kind == 'artifact'] == [ref]
    assert 'bounded preview' not in captured.text()
    assert 'forged' not in json.dumps(captured.manifest())
    assert captured.source_coverage == 'partial'
    assert projected.endswith('[Command timed out]\n[exit code: -1]')


def test_command_retention_failure_is_not_silently_complete():
    from lib.tool_results.tool_result_artifact_writer import ToolResultArtifact
    from lib.tasks_pkg.handlers.code_exec import _project_output_artifact
    artifact = ToolResultArtifact(
        text='only prefix', artifact_ref=None, sha256=None,
        size_bytes=11, spilled=False, truncated=False,
        complete=True, degraded=True, degraded_reason='quota')
    value = _project_output_artifact('$ command\n[exit code: 0]', artifact)
    assert capture_result(value).retention_coverage != 'complete'
    assert 'not retained' in value
    assert value.endswith('[exit code: 0]')


def test_paper_batch_image_input_never_enters_text_as_base64():
    from lib.paper.tools import PaperToolResultBudgetV2
    messages = []
    source = {
        '__screenshot__': True, '__batch_images__': True,
        '_text_fallback': 'two figures',
        '_modelReplaySources': ['tofu-media:first', 'tofu-media:second'],
        'images': [{'dataUrl': 'data:image/png;base64,DO_NOT_SERIALIZE'}] * 2,
    }
    PaperToolResultBudgetV2().append(
        messages, round_index=0, tool_name='read_files', tool_call_id='images',
        content=source)
    assert 'DO_NOT_SERIALIZE' not in json.dumps(messages)
    assert 'two figures' in messages[0]['content']


def test_evidence_schema_retains_result_identity_and_coverage():
    from lib.conversation_sync.generated_contract import OPENAPI_SCHEMAS
    evidence = OPENAPI_SCHEMAS['ToolResultEvidence']['properties']
    assert {'resultId', 'executionStatus', 'sourceCoverage',
            'retentionCoverage', 'viewCoverage'} <= evidence.keys()
