"""Unicode code-point bounds agree across generated browser and Python decoders."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess

import pytest

from lib.conversation_sync import validation
from lib.conversation_sync.compiled_validation import compile_success_predicates
from lib.conversation_sync.generated_contract import OPENAPI_SCHEMAS
from lib.conversation_sync.validation import ContractViolation, decode


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_unicode_length_parity(tmp_path):
    cases = []
    for text, valid in (
        ("x" * 1999 + "😀", True),
        ("😀" * 2000, True),
        ("漢" * 2000, True),
        ("e\u0301" * 1000, True),
        ("x" * 2000 + "😀", False),
        ("😀" * 2001, False),
        ("e\u0301" * 1000 + "x", False),
    ):
        completion = {
            "commandId": "command-a", "status": "completed",
            "command": text, "resultPreview": text, "resultTruncated": True,
        }
        cases.append(["BackgroundCommandCompletion", completion, valid])
        # Force diagnostics independently of the successful fast path. A valid
        # supplementary preview must not produce a spurious length violation.
        cases.append([
            "BackgroundCommandCompletion", {**completion, "status": "invalid"}, False,
        ])
        for name in ("ConversationSyncSnapshot", "ConversationTurnPage"):
            document = json.loads((ROOT / f"contracts/fixtures/sync_v3/{name}.json").read_text())
            document["turns"][0]["projection"]["backgroundCommand"] = copy.deepcopy(completion)
            document["turns"][0]["projection"]["_bgCommandInjects"] = [{
                "blockId": "background-command:1", "round": 1, "count": 1, "previews": [{
                    "commandId": "command-a", "text": "completion",
                    "backgroundCommand": copy.deepcopy(completion),
                }],
            }]
            if name == "ConversationSyncSnapshot":
                document["queueItems"][0]["backgroundCommand"] = copy.deepcopy(completion)
            cases.append([name, document, valid])

    # Durable tool evidence must also agree across browser/fast/diagnostic
    # readers; unknown versions remain rejected rather than normalized away.
    for version, valid in (("tofu.tool-result/v2", True),
                           ("tofu.tool-result/l0", True),
                           ("tofu.tool-result/unknown", False)):
        cases.append(["ToolResultEvidence", {
            "contractVersion": "tofu.tool-result-evidence/v1",
            "resultContractVersion": version, "status": "partial",
            "projectionKind": "partial", "truncated": True,
            "rawBytes": 100, "visibleBytes": 20, "envelopeBytes": 80,
            "evidenceId": "durable-tool-evidence",
        }, valid])

    # Current contract minima are 1; an isolated schema exercises a meaningful
    # supplementary-character minLength boundary without changing wire policy.
    bounds = {"type": "string", "minLength": 2, "maxLength": 2}
    for text, valid in (("", False), ("😀", False), ("😀x", True),
                        ("e\u0301", True), ("😀xx", False)):
        cases.append(["UnicodeBounds", text, valid])
    schemas = {**OPENAPI_SCHEMAS, "UnicodeBounds": bounds}
    predicates = compile_success_predicates(schemas)
    expected_errors = []
    for name, value, valid in cases:
        errors = validation._validate(schemas[name], value, "$")
        assert (not errors) is valid, (name, errors)
        assert predicates[name](value) is valid
        if name != "UnicodeBounds":
            if valid:
                assert decode(name, value) == value
            else:
                with pytest.raises(ContractViolation):
                    decode(name, value)
        expected_errors.append(errors)

    entry = tmp_path / "unicode.ts"
    entry.write_text(
        "import { readFileSync } from 'node:fs';\n"
        f"import {{ assertConversationSyncSchema, CONVERSATION_SYNC_SCHEMAS }} from {json.dumps(str(ROOT / 'frontend/src/api/conversation-sync.generated.ts'))};\n"
        f"(CONVERSATION_SYNC_SCHEMAS as any).UnicodeBounds = {json.dumps(bounds)};\n"
        "const cases = JSON.parse(readFileSync(0, 'utf8'));\n"
        "console.log(JSON.stringify(cases.map(([name, value]) => {\n"
        "  try { assertConversationSyncSchema(name, value); return []; }\n"
        "  catch (error) { return error.violations; }\n"
        "})));\n"
    )
    built = tmp_path / "unicode.cjs"
    subprocess.run(
        ["node", str(ROOT / "scripts/vite_test_bundle.mjs"), str(entry),
         "--bundle", "--platform=node", "--format=cjs", f"--outfile={built}"],
        cwd=ROOT, capture_output=True, text=True, check=True, timeout=60,
    )
    result = subprocess.run(
        ["node", "-e", "globalThis.window = {}; require(process.argv[1]);", str(built)],
        input=json.dumps(cases), cwd=ROOT,
        capture_output=True, text=True, check=True, timeout=30,
    )
    actual_errors = json.loads(result.stdout)
    for (name, value, valid), actual, expected in zip(cases, actual_errors, expected_errors, strict=True):
        assert (not actual) is valid, (name, actual)
        # Error wording differs by language; compare the fields that fail.
        assert sorted(error.split(": ", 1)[0] for error in actual) == sorted(
            error.split(": ", 1)[0] for error in expected
        ), (name, actual, expected)
