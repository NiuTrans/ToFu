"""Generated conversation-sync schemas enforce bounded reference objects."""

from __future__ import annotations

from collections import UserDict
import json
import math
from pathlib import Path
import random
from types import MappingProxyType

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


_WIRE_FIXTURES = Path(__file__).resolve().parents[2] / "contracts/fixtures/sync_v3"
_REPLACEMENTS = (
    None, True, False, 0, 1, -1, 1.5, float("nan"), float("inf"), "", "x",
    "sha256:" + "a" * 64, [], [None], {}, {"undeclared": None},
)


def _document_paths(value, path=()):
    yield path
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _document_paths(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _document_paths(item, (*path, index))


def _value_at(value, path):
    for step in path:
        value = value[step]
    return value


def _with_replacement(value, path, replacement):
    if not path:
        return replacement
    copy = dict(value) if isinstance(value, dict) else list(value)
    copy[path[0]] = _with_replacement(value[path[0]], path[1:], replacement)
    return copy


def _mutations(original, rng):
    yield rng.choice(_REPLACEMENTS)
    if isinstance(original, dict):
        yield UserDict(original)
        yield MappingProxyType(original)
        yield {**original, "undeclaredMutationField": True}
        if original:
            dropped = rng.choice(list(original))
            yield {key: item for key, item in original.items() if key != dropped}
    elif isinstance(original, list):
        yield tuple(original)
        yield original[1:]
        yield original + original[:1]
    elif isinstance(original, bool):
        yield int(original)
    elif isinstance(original, int):
        yield float(original)
        yield bool(original % 2)
        yield -original - 1
        yield original + 2**63
    elif isinstance(original, float):
        yield int(original) if math.isfinite(original) else 0
    elif isinstance(original, str):
        yield original[:-1]
        yield original + "!"
        yield original + "x" * 4097


@pytest.mark.parametrize("schema_name", ["ConversationSyncSnapshot", "ConversationTurnPage"])
def test_specialized_predicates_match_diagnostics_on_mutated_wire_fixtures(schema_name):
    """Mutated nodes of a real-shaped document get the diagnostic verdict.

    Covers exact dict/int/float fast paths against bool, float-for-integer,
    out-of-range, missing-required, undeclared-field, pattern/length and
    non-dict Mapping values at every depth of the generated schema.
    """
    document = json.loads((_WIRE_FIXTURES / f"{schema_name}.json").read_text())
    schema = OPENAPI_SCHEMAS[schema_name]
    check = contract_validation._SUCCESS_PREDICATES[schema_name]
    assert check(document) and not contract_validation._validate(schema, document, "$")
    rng = random.Random(f"compiled-validation:{schema_name}")
    verdicts = set()
    # A fixed sample keeps the full-document diagnostic walk within unit time.
    for path in rng.sample(list(_document_paths(document)), 160):
        for mutated in _mutations(_value_at(document, path), rng):
            candidate = _with_replacement(document, path, mutated)
            diagnostic_valid = not contract_validation._validate(schema, candidate, "$")
            assert check(candidate) is diagnostic_valid, (path, mutated)
            verdicts.add(diagnostic_valid)
    assert verdicts == {True, False}


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


@pytest.mark.parametrize("version", ["tofu.tool-result/v2", "tofu.tool-result/l0"])
def test_tool_result_evidence_preserves_known_durable_versions(version):
    evidence = {
        "contractVersion": "tofu.tool-result-evidence/v1",
        "resultContractVersion": version,
        "status": "partial", "projectionKind": "partial", "truncated": True,
        "rawBytes": 100, "visibleBytes": 20, "envelopeBytes": 80,
        "evidenceId": "historical-result", "resultId": "result-1",
        "executionStatus": "ok", "sourceCoverage": "complete",
        "retentionCoverage": "complete", "viewCoverage": "partial",
    }
    assert decode("ToolResultEvidence", evidence) == evidence
    assert contract_validation._SUCCESS_PREDICATES["ToolResultEvidence"](evidence)
    assert not contract_validation._validate(OPENAPI_SCHEMAS["ToolResultEvidence"], evidence, "$<evidence>")
    for invalid in ("tofu.tool-result/unknown", "", "tofu.tool-result/v999"):
        with pytest.raises(ContractViolation):
            decode("ToolResultEvidence", {**evidence, "resultContractVersion": invalid})
    with pytest.raises(ContractViolation):
        decode("ToolResultEvidence", {**evidence, "unregisteredField": True})


def _tagged_branch(tag, *, required=True):
    return {
        "type": "object", "additionalProperties": False,
        "required": ["payload", *(["type"] if required else [])],
        "properties": {"type": {"const": tag}, "payload": {"type": "string"}},
    }


def test_tagged_union_checks_complete_selected_branch_and_mapping_wrappers():
    from collections import UserDict

    schemas = {
        "Text": _tagged_branch("text"),
        "Tool": _tagged_branch("tool"),
        "Union": {"oneOf": [
            {"$ref": "#/components/schemas/Text"},
            {"$ref": "#/components/schemas/Tool"},
        ]},
    }
    check = compile_success_predicates(schemas)["Union"]
    for tag in ("text", "tool"):
        assert check({"type": tag, "payload": "valid"})
        assert check(UserDict({"type": tag, "payload": "valid"}))
        assert not check({"type": tag, "payload": 42})
        assert not check({"type": tag, "payload": "valid", "extra": True})
    for invalid in ({}, {"payload": "valid"}, {"type": "unknown", "payload": "x"},
                    {"type": [], "payload": "x"}, None):
        assert not check(invalid)


@pytest.mark.parametrize("branches,value,expected", [
    ([_tagged_branch("same"), _tagged_branch("same")],
     {"type": "same", "payload": "x"}, False),
    ([_tagged_branch("a", required=False), _tagged_branch("b", required=False)],
     {"payload": "x"}, False),
    ([_tagged_branch("a", required=False), _tagged_branch("b")],
     {"payload": "x"}, True),
    ([_tagged_branch(True), _tagged_branch(1)],
     {"type": True, "payload": "x"}, True),
    ([_tagged_branch(True), _tagged_branch(1)],
     {"type": 1, "payload": "x"}, True),
    ([_tagged_branch("a"), {"type": "object"}],
     {"type": "a", "payload": "x"}, False),
])
def test_unproven_tags_preserve_exactly_one_semantics(branches, value, expected):
    check = compile_success_predicates({"Union": {"oneOf": branches}})["Union"]
    assert check(value) is expected


def test_tag_proof_does_not_override_reference_or_union_precedence():
    ignored_tag = _tagged_branch("ignored")
    ignored_tag["properties"]["type"] = {
        "const": "ignored", "oneOf": [{"type": "string"}],
    }
    check = compile_success_predicates({"Union": {
        "oneOf": [ignored_tag, _tagged_branch("text")],
    }})["Union"]
    assert not check({"type": "text", "payload": "overlaps"})
    assert check({"type": "other", "payload": "one match"})


def test_tagged_segment_failure_retains_diagnostic_details():
    invalid = {"type": "tool_use", "blockId": "tool:1", "id": "call-1",
               "name": "read", "input": {}, "result": {"status": 7},
               "extra": True}
    expected = contract_validation._validate(
        OPENAPI_SCHEMAS["TurnContentSegment"], invalid, "$"
    )
    with pytest.raises(ContractViolation) as raised:
        decode("TurnContentSegment", invalid)
    assert list(raised.value.violations) == expected
