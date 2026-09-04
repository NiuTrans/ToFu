"""Generated conversation-sync schemas enforce bounded reference objects."""

from __future__ import annotations

import pytest

from lib.conversation_sync import validation as contract_validation
from lib.conversation_sync.compiled_validation import compile_success_predicates
from lib.conversation_sync.generated_contract import OPENAPI_SCHEMAS
from lib.conversation_sync.validation import ContractViolation, decode


pytestmark = pytest.mark.unit


def test_snapshot_document_reference_keys_and_fields_are_strict():
    key = "sha256:" + "a" * 64
    assert decode("SnapshotDocumentReferences", {"toolContent": key}) == {
        "toolContent": key,
    }

    for invalid in (
        {},
        {"toolContent": "sha256:short"},
        {"assistantContent": key},
        {"toolContent": key, "results": key, "extra": key},
    ):
        with pytest.raises(ContractViolation):
            decode("SnapshotDocumentReferences", invalid)


def test_shared_document_dictionary_validates_key_shape_and_capacity():
    valid = {"sha256:" + "a" * 64: {"payload": True}}
    assert decode("SnapshotSharedToolDocuments", valid) == valid

    with pytest.raises(ContractViolation):
        decode("SnapshotSharedToolDocuments", {"not-a-digest": "payload"})
    with pytest.raises(ContractViolation):
        decode("SnapshotSharedToolDocuments", {
            f"sha256:{index:064x}": index
            for index in range(257)
        })


def test_projection_reference_dictionary_is_strict_and_bounded():
    valid = {
        "turn-a": {
            "content": "text:terminal",
            "roundThinking": {"call-a": "thinking:round-a"},
        },
    }
    assert decode("SnapshotProjectionReferences", valid) == valid

    for invalid in (
        {},
        {"turn-a": {}},
        {"turn-a": {"content": ""}},
        {"turn-a": {"roundThinking": {}}},
        {"turn-a": {"roundThinking": {"call-a": ""}}},
        {"turn-a": {"undeclared": "text:terminal"}},
    ):
        with pytest.raises(ContractViolation):
            decode("SnapshotProjectionReferences", invalid)
    with pytest.raises(ContractViolation):
        decode("SnapshotProjectionReferences", {
            f"turn-{index}": {"content": "text:terminal"}
            for index in range(4_097)
        })
    with pytest.raises(ContractViolation):
        decode("SnapshotProjectionReferences", {
            "turn-a": {
                "roundThinking": {
                    f"call-{index}": "thinking:round"
                    for index in range(4_097)
                },
            },
        })


def test_json_constants_do_not_coerce_booleans_and_numbers():
    assert decode("AbortAttemptResponse", {"ok": True}) == {"ok": True}
    with pytest.raises(ContractViolation):
        decode("AbortAttemptResponse", {"ok": 1})

    proposed_plan = {
        "blockId": "proposed-plan",
        "planId": "plan_" + "a" * 24,
        "revision": 1,
        "format": "markdown",
        "text": "Execute the verified plan.",
    }
    assert decode("TurnProposedPlan", proposed_plan) == proposed_plan
    with pytest.raises(ContractViolation):
        decode("TurnProposedPlan", {**proposed_plan, "revision": True})


def test_fast_success_predicate_matches_diagnostic_validator():
    """The allocation-free path must never admit what diagnostics reject."""
    digest = "sha256:" + "a" * 64
    minimal_snapshot = {
        "ok": True,
        "contract": "tofu.conversation-sync.snapshot/v1",
        "conversationId": "conv-a",
        "conversationRevision": 0,
        "syncSeq": 0,
        "cursor": "cursor-a",
        "serverBootId": "boot-a",
        "heartbeatIntervalMs": 1_000,
        "settings": {},
        "turns": [],
        "attempts": [],
        "queueItems": [],
        "pushWithheld": False,
    }
    minimal_turn = {
        "turnId": "turn-a",
        "conversationId": "conv-a",
        "laneId": "main",
        "ordinal": 1,
        "actor": "assistant",
        "kind": "reply",
        "runId": "run-a",
        "status": "completed",
        "projection": {
            "content": "done",
            "segments": [
                {"type": "text", "blockId": "text:terminal", "text": "done"},
                {
                    "type": "tool_use",
                    "blockId": "tool:call-a",
                    "id": "call-a",
                    "name": "search",
                    "input": {"query": "evidence"},
                    "result": {"content": "found", "status": "done"},
                },
            ],
        },
        "projectionRevision": 1,
        "settlement": {},
        "createdAt": 1,
        "updatedAt": 1,
    }
    candidates = (
        None,
        False,
        True,
        -1,
        0,
        1,
        1.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        "",
        "value",
        digest,
        [],
        [None],
        [0, "value"],
        {},
        {"undeclared": None},
        {"toolContent": digest},
        {"toolContent": digest, "results": digest},
        {"toolContent": "invalid"},
        {"type": "text", "blockId": "text:1", "text": "hello"},
        {
            "type": "tool_use",
            "blockId": "tool:1",
            "id": "call-1",
            "name": "search",
            "input": {"query": "evidence"},
            "result": {"content": "done"},
        },
        minimal_snapshot,
        {**minimal_snapshot, "turns": [minimal_turn]},
        {
            **minimal_snapshot,
            "turns": [
                {**minimal_turn, "projection": {"undeclared": True}},
            ],
        },
        {**minimal_snapshot, "heartbeatIntervalMs": 999},
        {**minimal_snapshot, "unexpected": True},
    )

    for schema_name, schema in OPENAPI_SCHEMAS.items():
        for candidate in candidates:
            diagnostic_valid = not contract_validation._validate(
                schema, candidate, "$"
            )
            assert contract_validation._SUCCESS_PREDICATES[schema_name](
                candidate
            ) is diagnostic_valid, (schema_name, candidate)


def test_failed_fast_predicate_preserves_every_diagnostic_violation():
    invalid = {
        "toolContent": "invalid-content-key",
        "results": "invalid-results-key",
        "undeclared": "invalid-extra-key",
    }
    expected = contract_validation._validate(
        OPENAPI_SCHEMAS["SnapshotDocumentReferences"], invalid, "$"
    )

    with pytest.raises(ContractViolation) as raised:
        decode("SnapshotDocumentReferences", invalid)

    assert list(raised.value.violations) == expected
    assert len(expected) == 4


def test_compiler_resolves_recursive_named_refs_and_unknown_refs_fail_closed():
    predicates = compile_success_predicates({
        "Node": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "value": {"type": "string"},
                "next": {
                    "oneOf": [
                        {"$ref": "#/components/schemas/Node"},
                        {"type": "null"},
                    ],
                },
            },
            "required": ["value", "next"],
        },
        "Broken": {"$ref": "#/components/schemas/Missing"},
    })

    assert predicates["Node"]({
        "value": "first",
        "next": {"value": "second", "next": None},
    })
    assert not predicates["Node"]({"value": "first", "next": 7})
    assert not predicates["Broken"]({})
