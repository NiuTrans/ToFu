#!/usr/bin/env python3
"""Generate Rust constants from the canonical storage.v2 wire contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "contracts" / "storage_v2.json"
OUTPUT = ROOT / "packages" / "tofu-db" / "src" / "generated_storage_v2.rs"


def _constant_name(name: str) -> str:
    characters: list[str] = []
    for character in name:
        if character.isupper() and characters:
            characters.append("_")
        characters.append(character.upper())
    return "".join(characters)


def _load() -> dict:
    document = json.loads(SOURCE.read_text(encoding="utf-8"))
    if document.get("format") != "tofu.storage-protocol.v2":
        raise ValueError("storage_v2.json has an unexpected format")
    fields = document.get("fields")
    if not isinstance(fields, list) or [field.get("id") for field in fields] != list(range(22)):
        raise ValueError("storage_v2.json field IDs must be exactly 0..21")
    if document.get("frame", {}).get("activeFrameCountAdmission") != "none":
        raise ValueError("storage.v2 must not admit frames by count")
    if document["frame"].get("aggregateBufferPolicy") != "ram-threshold-then-private-anonymous-file":
        raise ValueError("native frame buffers must overflow without an aggregate byte quota")
    expected_worker_delivery = {
        "activeWorkerCountAdmission": "none",
        "pendingJobCountAdmission": "none",
        "workerCreation": "ready-handoff-or-first-owned-job",
        "idleReuse": "launch-derived-target-retire-surplus-after-completion",
        "creationFailure": "unpublished-job-drops-memory-lease-without-poisoning-peers",
        "close": "sender-drop-wakes-idle-workers-active-workers-retain-owned-state"
    }
    if document.get("embeddedWorkerDelivery") != expected_worker_delivery:
        raise ValueError("embedded worker delivery must use demand-owned jobs")
    expected_session = {
        "negotiation": "authenticate-token-then-exactly-one-hello-before-requests",
        "helloResponse": "zero-status-response-with-hello-correlation-and-negotiated-schema",
        "authority": "authenticated-owner-and-tenant-fixed-for-session",
        "responseCorrelation": "exact-request-correlation-id",
        "responseSchema": "negotiated-schema-id",
        "statusCodes": {
            "badRequest": 400,
            "forbidden": 403,
            "deadlineElapsed": 408,
            "conflict": 409,
            "resourceExhausted": 429,
            "notImplemented": 501,
            "unavailable": 503,
            "integrityFailure": 550,
        },
    }
    if document.get("serverSession") != expected_session:
        raise ValueError("storage_v2.json server session contract is incomplete")
    expected_listener = {
        "bindPolicy": "numeric-loopback-only",
        "maximumConnections": 512,
        "connectionCountAdmission": "none",
        "maximumConnectionsRole": "initial-reservation-and-reuse-target",
        "minimumConnectionStackBytes": 262_144,
        "maximumConnectionStackBytes": 2_097_152,
        "maximumIoTimeoutMilliseconds": 300_000,
        "maximumSessionIdleTimeoutMilliseconds": 3_600_000,
        "maximumAcceptPollMilliseconds": 1_000,
        "authorityLockScope": "one-semantic-request",
        "parentLease": "nonblocking-empty-pipe-eof-stops-admission-then-joins-connections",
    }
    if document.get("loopbackListener") != expected_listener:
        raise ValueError("storage_v2.json loopback listener contract is incomplete")
    expected_daemon = {
        "command": "serve",
        "authorityOpen": "existing-only",
        "endpoint": "numeric-ipv4-loopback-ephemeral-port",
        "authSecretSource": "TOFU_STORAGE_TOKEN-environment",
        "authSecretMinimumBytes": 32,
        "authSecretMaximumBytes": 256,
        "authTokenDerivation": "sha256-tofu.storage.v2.auth-token-nul-plus-ascii-secret",
        "ownerScope": "explicit-positive-owner-and-optional-positive-tenant",
        "ownershipChannel": "stdin-empty-pipe-eof",
        "readinessChannel": "stdout-single-json-line-maximum-4096-bytes-no-secrets",
        "readinessAuthorityUuid": "authorityUuid-from-open-CONTROL",
        "supervisorSelection": "selectable-certified-backend-not-default",
    }
    if document.get("daemon") != expected_daemon:
        raise ValueError("storage_v2.json daemon contract is incomplete")
    expected_probe = {
        "lifecycle": "one-shot-before-authority-open",
        "cpu": "minimum-of-process-affinity-and-cgroup-v1-or-v2-quota",
        "memory": "minimum-of-proc-memavailable-and-cgroup-v1-or-v2-limit-minus-current",
        "volume": "authority-volume-available-bytes",
        "failurePolicy": "fixed-lean-profile",
        "leanConnections": 4,
        "leanFrameBytes": 16_777_216,
        "connectionStackBytes": 1_048_576,
        "minimumWritableVolumeFreeBytes": 150_994_944,
        "maximumConnections": 512,
        "frameMemoryHeadroomDivisor": 32,
        "embeddedRequestMemory": {
            "headroomDivisor": 32,
            "fallback": "lean-frame-memory-hint",
            "reservationLifecycle": "owned-input-through-worker-release",
            "admission": "none",
            "memoryHintRole": "observation-only",
            "accounting": "owned-encoded-payload-capacity",
        },
    }
    if document.get("resourceProbe") != expected_probe:
        raise ValueError("storage_v2.json resource probe contract is incomplete")
    fastpath = document.get("nativeFastpath", {})
    interval = fastpath.get("intervalMilliseconds", {})
    minimum, maximum = interval.get("minimum"), interval.get("maximum")
    if (type(minimum) is not int or type(maximum) is not int
            or minimum <= 0 or maximum < minimum
            or type(fastpath.get("markerMaximumBytes")) is not int
            or fastpath["markerMaximumBytes"] <= 0
            or type(fastpath.get("minimumFsyncSpeedup")) is not int
            or fastpath["minimumFsyncSpeedup"] <= 0):
        raise ValueError("storage_v2.json native fastpath bounds are invalid")
    reason_diag = document.get("nativeResourceExhaustionDiagnostics", {})
    if (reason_diag.get("contract") != "tofudb.resource-exhaustion-reasons/v1"
        or reason_diag.get("reasonNames") != ["raw_archive_owner_capacity",
            "raw_archive_conversation_capacity", "raw_archive_restore_byte_budget",
            "domain_resource_bound", "request_accounting_overflow",
            "allocation_reservation_failure", "os_memory_failure", "other"]
        or reason_diag.get("fixedCounterBytes") != 64
        or reason_diag.get("formatScratchBytes") != 256):
        raise ValueError("storage.v2 resource-exhaustion diagnostics contract is incomplete")
    return document


def _render(document: dict | None = None) -> str:
    document = _load() if document is None else document
    frame = document["frame"]
    streamed_requests = document.get("streamedRequests")
    if streamed_requests != {
        "maximumPayloadBytes": 64 * 1024 * 1024,
        "maximumChunks": 64,
        "eligibility": "all-semantic-requests-larger-than-one-frame",
        "ordering": "one-incomplete-stream-per-connection-strict-zero-based-chunk-index",
        "identity": "every-chunk-exactly-matches-correlation-deadline-owner-tenant-command-schema-stream-and-declared-total; terminator-matches-request-identity",
        "terminator": "final-chunk-then-empty-payload-request-carries-operation",
        "memoryAdmission": "first-chunk-declared-total-is-validated-and-reserved-once-before-exact-capacity-allocation; one decoded chunk is the only additional transport buffer",
    }:
        raise ValueError("storage.v2 streamed request bounds are incomplete")
    streamed_responses = document.get("streamedResponses")
    if streamed_responses != {
        "maximumPayloadBytes": 64 * 1024 * 1024,
        "maximumChunks": 64,
        "chunkPayloadBytes": 1024 * 1024,
        "eligibility": "successful-response-payload-larger-than-one-chunk",
        "ordering": "strict-zero-based-chunk-index-with-final-on-last-chunk",
        "identity": "every-chunk-and-terminator-exactly-match-correlation-and-schema",
        "terminator": "empty-payload-success-response-after-final-chunk",
        "memoryAdmission": "each-encoded-chunk-holds-shared-frame-budget-through-write",
    }:
        raise ValueError("storage.v2 streamed response bounds are incomplete")
    lines = [
        "//! @generated by scripts/gen_storage_v2_contract.py; do not edit.",
        "",
        f"pub const PROTOCOL_VERSION: u16 = {document['protocolVersion']};",
        f"pub const MAX_FRAME_BODY_BYTES: usize = {frame['maximumBodyBytes']};",
        f"pub const MAX_PAYLOAD_BYTES: usize = {frame['maximumPayloadBytes']};",
        f"pub const MAX_BLOB_CHUNK_BYTES: usize = {frame['maximumBlobChunkBytes']};",
        "pub const DEFAULT_FRAME_MEMORY_BYTES: usize = "
        f"{frame['defaultFrameMemoryBytes']};",
        "pub const MAX_STREAMED_REQUEST_PAYLOAD_BYTES: usize = "
        f"{streamed_requests['maximumPayloadBytes']};",
        "pub const MAX_STREAMED_REQUEST_CHUNKS: u32 = "
        f"{streamed_requests['maximumChunks']};",
        "pub const MAX_STREAMED_RESPONSE_PAYLOAD_BYTES: usize = "
        f"{streamed_responses['maximumPayloadBytes']};",
        "pub const MAX_STREAMED_RESPONSE_CHUNKS: u32 = "
        f"{streamed_responses['maximumChunks']};",
        "pub const STREAMED_RESPONSE_CHUNK_BYTES: usize = "
        f"{streamed_responses['chunkPayloadBytes']};",
    ]
    notices = document["committedEvents"]
    if (notices["maximumKeysPerTransaction"] != 1024
            or notices["maximumKeyBytes"] != 2048):
        raise ValueError("invalid bounded commit notice contract")
    lines.extend([
        f'pub const NATIVE_EVENT_BATCH_COMPOSITION_CONTRACT: &str = "{document["nativeEventBatchComposition"]["contract"]}";',
        f'pub const COMMITTED_EVENTS_CONTRACT: &str = "{notices["envelopeContract"]}";',
        f'pub const COMMIT_NOTICE_CONTRACT: &str = "{notices["noticeContract"]}";',
        f"pub const MAX_COMMIT_NOTICE_KEYS: usize = {notices['maximumKeysPerTransaction']};",
        f"pub const MAX_COMMIT_NOTICE_KEY_BYTES: usize = {notices['maximumKeyBytes']};",
    ])
    field_limits = {
        "commandId": "MAX_COMMAND_ID_BYTES",
        "operation": "MAX_OPERATION_BYTES",
        "errorCode": "MAX_ERROR_CODE_BYTES",
        "errorMessage": "MAX_ERROR_MESSAGE_BYTES",
        "authToken": "AUTH_TOKEN_BYTES",
    }
    for field in document["fields"]:
        name = field["name"]
        lines.append(
            f"pub(crate) const FIELD_{_constant_name(name)}: u64 = {field['id']};"
        )
        if name in field_limits:
            bounds = [int(value) for value in re.findall(r"\d+", field["type"])]
            lines.append(f"pub const {field_limits[name]}: usize = {max(bounds)};")
    for name, value in document["messageKinds"].items():
        lines.append(f"pub(crate) const KIND_{_constant_name(name)}: u64 = {value};")
    maximum_schema_ids = max(
        int(value)
        for value in re.findall(
            r"\d+", next(field["type"] for field in document["fields"] if field["name"] == "schemaIds")
        )
    )
    lines.append(f"pub const MAX_SCHEMA_IDS: usize = {maximum_schema_ids};")
    for name, value in document["serverSession"]["statusCodes"].items():
        lines.append(f"pub const STATUS_{_constant_name(name)}: u16 = {value};")
    lines.append(f"pub const FRAME_MEMORY_HEADROOM_DIVISOR: usize = {document['resourceProbe']['frameMemoryHeadroomDivisor']};")
    listener = document["loopbackListener"]
    lines.append(
        f"pub const MAX_CONNECTION_REUSE_TARGET: usize = {listener['maximumConnections']};"
    )
    lines.append(
        "pub const MIN_CONNECTION_STACK_BYTES: usize = "
        f"{listener['minimumConnectionStackBytes']};"
    )
    lines.append(
        "pub const MAX_CONNECTION_STACK_BYTES: usize = "
        f"{listener['maximumConnectionStackBytes']};"
    )
    lines.append(
        "pub const MAX_IO_TIMEOUT_MILLISECONDS: u64 = "
        f"{listener['maximumIoTimeoutMilliseconds']};"
    )
    lines.append(
        "pub const MAX_SESSION_IDLE_TIMEOUT_MILLISECONDS: u64 = "
        f"{listener['maximumSessionIdleTimeoutMilliseconds']};"
    )
    lines.append(
        "pub const MAX_ACCEPT_POLL_MILLISECONDS: u64 = "
        f"{listener['maximumAcceptPollMilliseconds']};"
    )
    daemon = document["daemon"]
    lines.append(
        "pub const MIN_DAEMON_AUTH_SECRET_BYTES: usize = "
        f"{daemon['authSecretMinimumBytes']};"
    )
    lines.append(
        "pub const MAX_DAEMON_AUTH_SECRET_BYTES: usize = "
        f"{daemon['authSecretMaximumBytes']};"
    )
    fastpath = document["nativeFastpath"]
    for name, value, kind in (
        ("MIN_NATIVE_FASTPATH_SYNC_INTERVAL_MS", fastpath["intervalMilliseconds"]["minimum"], "u64"),
        ("MAX_NATIVE_FASTPATH_SYNC_INTERVAL_MS", fastpath["intervalMilliseconds"]["maximum"], "u64"),
        ("NATIVE_FASTPATH_MARKER_MAX_BYTES", fastpath["markerMaximumBytes"], "u64"),
        ("MIN_NATIVE_FASTPATH_FSYNC_SPEEDUP", fastpath["minimumFsyncSpeedup"], "u32"),
    ):
        lines.append(f"pub const {name}: {kind} = {value};")
    resource_probe = document["resourceProbe"]
    embedded = resource_probe["embeddedRequestMemory"]
    if embedded["admission"] != "none" or embedded["memoryHintRole"] != "observation-only":
        raise ValueError("embedded input bytes are observation, not aggregate admission")
    for name, key, kind in (
        ("EMBEDDED_REQUEST_MEMORY_HEADROOM_DIVISOR", "headroomDivisor", "u64"),
    ):
        lines.append(f"pub const {name}: {kind} = {embedded[key]};")
    lines.append(
        "pub const MIN_WRITABLE_VOLUME_FREE_BYTES: u64 = "
        f"{resource_probe['minimumWritableVolumeFreeBytes']};"
    )
    reasons = document["nativeResourceExhaustionDiagnostics"]
    lines.append(f'pub const NATIVE_RESOURCE_EXHAUSTION_CONTRACT: &str = "{reasons["contract"]}";')
    names = ", ".join(f'"{name}"' for name in reasons["reasonNames"])
    lines.append(f'pub const NATIVE_RESOURCE_EXHAUSTION_REASON_NAMES: [&str; {len(reasons["reasonNames"])}] = [{names}];')
    lines.append(f'pub const NATIVE_RESOURCE_EXHAUSTION_FORMAT_BYTES: usize = {reasons["formatScratchBytes"]};')
    for name, fields in document["requiredFields"].items():
        rendered = ", ".join(str(field) for field in fields)
        lines.append(
            f"pub(crate) const REQUIRED_{_constant_name(name)}_FIELDS: &[u8] = &[{rendered}];"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    document = _load()
    semantics = document["applicationSemantics"]
    python = ('"""@generated by scripts/gen_storage_v2_contract.py; do not edit."""\n'
        + f"OWNER_OMITTED_OPERATIONS = {tuple(semantics['ownerOmittedOperations'])!r}\n"
        + f"BOUNDED_PROGRESS_OPERATIONS = {semantics['boundedProgressOperations']!r}\n"
        + f"NOTIFICATION_TOPICS = {semantics['notificationTopics']!r}\n"
        + f"MIN_NATIVE_FASTPATH_SYNC_INTERVAL_MS = {document['nativeFastpath']['intervalMilliseconds']['minimum']}\n"
        + f"MAX_NATIVE_FASTPATH_SYNC_INTERVAL_MS = {document['nativeFastpath']['intervalMilliseconds']['maximum']}\n")
    notices = document['committedEvents']
    wire_python = ('"""@generated by scripts/gen_storage_v2_contract.py; do not edit."""\n'
        + f"NATIVE_EVENT_BATCH_COMPOSITION_CONTRACT = {document['nativeEventBatchComposition']['contract']!r}\n"
        + f"COMMITTED_EVENTS_CONTRACT = {notices['envelopeContract']!r}\n"
        + f"COMMIT_NOTICE_CONTRACT = {notices['noticeContract']!r}\n"
        + f"MAX_COMMIT_NOTICE_KEY_BYTES = {notices['maximumKeyBytes']!r}\n"
        + f"MAX_COMMIT_NOTICE_KEYS = {notices['maximumKeysPerTransaction']!r}\n")
    outputs = {OUTPUT: _render(document), ROOT / 'lib/storage/generated_v2_semantics.py': python,
               ROOT / 'tofu_db/generated_v2.py': wire_python,
               ROOT / 'scripts/generated_storage_v2.py': ('\"\"\"@generated compatibility import; owner is tofu_db.generated_v2.\"\"\"\n'
                   + 'from tofu_db.generated_v2 import *  # noqa: F401,F403\n')}
    if args.check:
        stale = [str(path.relative_to(ROOT)) for path, rendered in outputs.items()
                 if not path.exists() or path.read_text(encoding='utf-8') != rendered]
        if stale:
            print('stale storage.v2 generated outputs: ' + ', '.join(stale))
            return 1
        return 0
    for path, rendered in outputs.items():
        if not path.exists() or path.read_text(encoding='utf-8') != rendered:
            path.write_text(rendered, encoding='utf-8')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
