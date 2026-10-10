"""Generated contract and package-boundary guards for Tofu-DB."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from lib.storage_sidecar.operation_domains import REGISTRY_VERSION
from lib.storage_sidecar.operation_registry import build_registry
from lib.storage_sidecar.schema import SCHEMA_VERSION
from tofu_db.wire import STORAGE_SCHEMA_VERSION as CLIENT_SCHEMA_VERSION


ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "contracts" / "storage_operations_v1.json"
STORAGE_V2 = ROOT / "contracts" / "storage_v2.json"
TOFUDB_IR = ROOT / "contracts" / "tofudb_ir_v1.json"
pytestmark = pytest.mark.unit


def test_storage_operation_catalog_matches_executable_registry():
    completed = subprocess.run(
        [sys.executable, "scripts/gen_storage_operation_catalog.py", "--check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    casefold = subprocess.run(
        [sys.executable, "scripts/gen_tofudb_unicode_casefold.py", "--check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert casefold.returncode == 0, casefold.stdout + casefold.stderr
    simple_fold = subprocess.run(
        [sys.executable, "scripts/gen_tofudb_unicode_simple_fold.py", "--check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert simple_fold.returncode == 0, simple_fold.stdout + simple_fold.stderr

    document = json.loads(CATALOG.read_text(encoding="utf-8"))
    names = [operation["name"] for operation in document["operations"]]
    assert names == sorted(build_registry())
    assert len(names) == len(set(names)) == document["operationCount"]
    assert document["sourceSchemaVersion"] == SCHEMA_VERSION
    assert CLIENT_SCHEMA_VERSION == SCHEMA_VERSION
    assert document["sourceRegistryVersion"] == REGISTRY_VERSION
    canonical = json.dumps(
        document["operations"],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert document["operationsSha256"] == hashlib.sha256(canonical).hexdigest()
    assert all(
        operation["ownerScope"] == "handler-enforced"
        for operation in document["operations"]
    )


def test_tofudb_public_client_does_not_import_application():
    completed = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys; sys.path.insert(0, " + repr(str(ROOT)) + "); "
         "from tofu_db import StorageV2Session; "
         "assert not any(name in sys.modules for name in ('lib', 'scripts', 'tofu_agent'))"],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_storage_v2_machine_contract_has_fixed_complete_bounded_fields():
    completed = subprocess.run(
        [sys.executable, "scripts/gen_storage_v2_contract.py", "--check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    document = json.loads(STORAGE_V2.read_text(encoding="utf-8"))
    assert document["format"] == "tofu.storage-protocol.v2"
    assert document["protocolVersion"] == 2
    assert document["encoding"] == "canonical-flat-messagepack"
    assert document["messageKinds"] == {
        "hello": 1,
        "request": 2,
        "response": 3,
        "blobChunk": 4,
        "responseChunk": 5,
    }
    assert [field["id"] for field in document["fields"]] == list(range(22))
    assert len({field["name"] for field in document["fields"]}) == 22
    assert document["embeddedWorkerDelivery"] == {
        "activeWorkerCountAdmission": "none",
        "pendingJobCountAdmission": "none",
        "workerCreation": "ready-handoff-or-first-owned-job",
        "idleReuse": "launch-derived-target-retire-surplus-after-completion",
        "creationFailure": "unpublished-job-drops-memory-lease-without-poisoning-peers",
        "close": "sender-drop-wakes-idle-workers-active-workers-retain-owned-state"
    }
    assert document["frame"] == {
        "lengthPrefix": "u32-big-endian-body-bytes",
        "checksumSuffix": "crc32c-u32-big-endian-over-body",
        "maximumBodyBytes": 8 * 1024 * 1024,
        "maximumPayloadBytes": 8 * 1024 * 1024 - 4096,
        "maximumBlobChunkBytes": 1024 * 1024,
        "activeFrameCountAdmission": "none",
        "defaultFrameMemoryBytes": 128 * 1024 * 1024,
        "aggregateBufferPolicy": "ram-threshold-then-private-anonymous-file",
        "readAdmission": "reserve-declared-body-before-allocation",
        "writeAdmission": "reserve-exact-encoded-body-before-allocation",
        "canonicalFieldOrder": "strictly-increasing-numeric-id",
        "unknownFieldPolicy": "reject",
    }
    assert document["streamedRequests"] == {
        "maximumPayloadBytes": 64 * 1024 * 1024,
        "maximumChunks": 64,
        "eligibility": "all-semantic-requests-larger-than-one-frame",
        "ordering": "one-incomplete-stream-per-connection-strict-zero-based-chunk-index",
        "identity": "every-chunk-exactly-matches-correlation-deadline-owner-tenant-command-schema-stream-and-declared-total; terminator-matches-request-identity",
        "terminator": "final-chunk-then-empty-payload-request-carries-operation",
        "memoryAdmission": "first-chunk-declared-total-is-validated-and-reserved-once-before-exact-capacity-allocation; one decoded chunk is the only additional transport buffer",
    }
    assert document["streamedResponses"] == {
        "maximumPayloadBytes": 64 * 1024 * 1024,
        "maximumChunks": 64,
        "chunkPayloadBytes": 1024 * 1024,
        "eligibility": "successful-response-payload-larger-than-one-chunk",
        "ordering": "strict-zero-based-chunk-index-with-final-on-last-chunk",
        "identity": "every-chunk-and-terminator-exactly-match-correlation-and-schema",
        "terminator": "empty-payload-success-response-after-final-chunk",
        "memoryAdmission": "each-encoded-chunk-holds-shared-frame-budget-through-write",
    }
    required = document["requiredFields"]
    assert required["hello"] == [0, 2, 17, 18, 19, 20]
    assert required["request"] == list(range(10))
    assert required["response"] == [0, 1, 2, 7, 9, 10, 11, 12, 13]
    assert required["blobChunk"] == [0, 1, 2, 3, 4, 5, 6, 7, 9, 14, 15, 16, 21]
    assert required["responseChunk"] == [0, 1, 2, 7, 9, 14, 15, 16]
    assert document["serverSession"] == {
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
    assert document["loopbackListener"] == {
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
    assert document["daemon"] == {
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
    assert document["resourceProbe"] == {
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


def test_tofudb_crate_declares_bounded_durability_primitives():
    wal = (ROOT / "packages" / "tofu-db" / "src" / "wal.rs").read_text(encoding="utf-8")
    blocks = (ROOT / "packages" / "tofu-db" / "src" / "block.rs").read_text(
        encoding="utf-8"
    )
    entities = (ROOT / "packages" / "tofu-db" / "src" / "entity.rs").read_text(
        encoding="utf-8"
    )
    vfs = (ROOT / "packages" / "tofu-db" / "src" / "vfs.rs").read_text(encoding="utf-8")
    assert "MAX_TRANSACTION_BYTES" in wal
    assert "crc32c::crc32c" in wal
    assert "blake3::hash" in wal
    assert "MAX_BLOCK_BYTES" in blocks
    assert "content-addressed block" in blocks
    assert "EntityTransaction" in entities
    assert "DeterministicVfs" in vfs
    assert "FaultAction" in vfs


def test_tofudb_ir_contract_is_generated_bounded_and_default_deny():
    completed = subprocess.run(
        [sys.executable, "scripts/gen_tofudb_ir_contract.py", "--check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    document = json.loads(TOFUDB_IR.read_text(encoding="utf-8"))
    assert document["format"] == "tofu.database-ir.v1"
    assert document["schemaIrVersion"] == document["transactionIrVersion"] == 1
    assert document["bounds"] == {
        "maximumSteps": 128,
        "maximumSlots": 64,
        "maximumLiteralBytes": 8 * 1024 * 1024,
        "maximumExecutableOperations": len(build_registry()),
    }
    assert document["physicalBounds"] == {
        "maximumEntityKeyBytes": 3072,
        "maximumEntityInlineValueBytes": 8 * 1024,
        "maximumEntityRangeRows": 1000,
        "maximumEntityTransactionWrites": 14336,
        "maximumEntityReachabilityPages": 20_000_000,
        "maximumEntityReachabilityFrontier": 8192,
        "maximumEntityRetiredRanges": 64,
        "maximumEntityRootRangeMounts": 64,
        "maximumEntityMountConsolidationRows": 999,
        "maximumEntityMountConsolidationBytes": 8 * 1024 * 1024,
        "maximumStreamEventBytes": 256 * 1024,
        "maximumStreamAppendEvents": 4096,
        "maximumStreamAppendBytes": 8 * 1024 * 1024,
        "maximumStreamReadEvents": 1000,
        "maximumStreamReadBytes": 8 * 1024 * 1024,
        "maximumTransactionStreamAppends": 500,
    }
    assert document["physicalFamilies"] == ["entity", "stream", "blob"]
    assert document["persistentSnapshots"] == {
        "rootPinNamespace": "entity_root_pins",
        "catalogNamespace": "entity_root_pin_catalog",
        "maximumPinsPerAuthority": 1_000_000,
        "maximumLegacyPinsWithoutCatalog": 64,
        "maximumPinIdBytes": 128,
        "maximumRangesPerCapsule": 64,
    }
    assert document["activeWalRotation"] == {
        "maximumBytes": 67108864,
        "softThresholdBytes": 62914560,
        "maximumRecoveryPackedLocations": 65536,
        "foregroundMaterializeTargetBytes": 8388608,
        "historyPreparationTargetBytes": 262144,
        "maximumHistoryPreparationBytes": 4194304,
        "historyPreparationPolicy": "one-partial-or-pending-segment;whole-record-steps-may-exceed-target;finish-before-checkpoint;worker-absence-is-synchronous",
        "backgroundWaitRoundMilliseconds": 250,
        "maximumBackgroundWaitRounds": 240,
        "softPolicy": "incremental-materialize;let-partial-history-encoding-and-its-live-packer-finish;defer-checkpoint-while-any-staged-payload-including-background-reservations-remains",
        "hardPolicy": "complete-materialization-and-checkpoint-before-single-or-group-append-would-exceed-maximum",
        "waitPolicy": "recheck-background-creating-under-condition-mutex-before-wait;bounded-synchronous-fallback",
        "maximumPendingRetiredWals": 1,
        "maximumPendingRetiredWalBytes": 67108864,
        "retirementPolicy": "publish-checkpoint-before-queue;existing-background-packer-removes-and-syncs-parent-outside-authority;worker-absence-is-synchronous",
        "retirementBackpressure": "drain-previous-retirement-before-next-checkpoint;retain-slot-on-delete-or-sync-error",
        "retirementRecovery": "reconstruct-only-immediate-predecessor-from-current-control-generation;no-startup-directory-scan",
        "retirementShutdown": "finish-pending-retirement-before-worker-registration-release;surface-cleanup-error"
    }
    assert document["entityPagePacking"] == {
        "targetBytes": 4096,
        "maximumBytes": 16384,
        "minimumChildrenPerSplitPage": 2,
        "encodingAllocation": "exact-page-length;transfer-to-existing-pending-store;retain-no-spare-capacity",
        "modifiedLeafRuns": "repack-adjacent-written-leaves-with-one-page-buffer;retain-tombstones-and-untouched-references",
        "tailRebalance": "redistribute-or-merge-within-maximum-page-bytes",
    }
    assert document["maintenanceScheduler"] == {
        "maximumWorkers": 1,
        "maximumScopes": 64,
        "workerStackBytes": 512 * 1024,
        "observedIdleIntervalMilliseconds": 250,
        "leanIdleIntervalMilliseconds": 1000,
        "maximumIdleIntervalMilliseconds": 60_000,
        "observedHistoryRetainedSegments": 64,
        "leanHistoryRetainedSegments": 16,
        "maximumHistoryRetainedSegments": 64,
        "historyRetirementPolicy": "trim-checkpointed-history-over-budget-with-active-wal-and-staged-prefix-preserved;never-force-foreground-checkpoint",
        "payloadPublicationPolicy": "idle-round-adopts-one-completed-background-pack-only-with-catalog-capacity;no-merge-or-wal-checkpoint",
        "backgroundPackShutdownPolicy": "reclaim-completed-unpublished-segment-before-worker-registration-release;preserve-staged-payloads-and-in-progress-publication;surface-cleanup-errors",
        "toolResultPruneRowsPerRound": 128,
        "authorityAdmission": "nonblocking-try-lock-one-bounded-transaction-per-round",
        "searchProjectionAdmission": "at-most-16-dirty-conversations-with-8-row-2MiB-source-pages-releasing-authority-between-pages",
        "searchProjectionMaximumSourcePagesPerConversation": 1024,
        "searchProjectionPageBudgetPolicy": "publish-explicit-partial-generation-and-ack-observed-tokens;source-mutation-rebuilds",
        "searchProjectionBackfillPageRows": 16,
        "searchProjectionMaximumDirtyConversations": 16,
        "searchProjectionBackfillPolicy": "enqueue-before-rebuild;defer-dirty-keys-beyond-pending-backfill-cursor",
        "searchProjectionFailurePolicy": "nonterminal-rebuildable-capability-degradation",
        "startupPolicy": "first-round-after-readiness-plus-one-idle-interval",
        "failurePolicy": "terminal-maintenance-error-stops-daemon-admission",
    }
    assert document["searchProjection"] == {
        "minimumBytesPerOwner": 128 * 1024 * 1024,
        "maximumBytesPerOwner": 4 * 1024 * 1024 * 1024,
        "observedVolumeFreePercent": 2,
        "pathPolicy": "explicit-absolute-dedicated-persistent-directory-outside-authority",
        "queryAdmission": "independent-deadline-bounded-projection-mutex",
        "unavailablePolicy": "retryable-capability-error-without-authority-failure",
    }
    assert document["filesystemCertification"] == {
        "maximumRetainedEntries": 4096,
        # Must exceed the WAL carried ceiling (2 MiB) so the certification
        # commit spills its block to a loose file for the reclaim stage.
        "payloadBlockBytes": 2 * 1024 * 1024 + 1,
        "requiredMeasurements": [
            "write-process-micros",
            "first-reopen-micros",
            "post-reopen-commit-micros",
            "second-reopen-process-micros",
            "total-micros",
            "retained-file-count",
            "retained-file-bytes",
        ],
        "targetPolicy": "explicit-empty-persistent-path-retained-as-auditable-store",
        "requiredStages": [
            "exclusive-lock",
            "immutable-block",
            "group-commit",
            "checkpoint-rotation",
            "payload-segment-publication",
            "payload-loose-reclaim",
            "destructor-free-reopen",
            "cross-process-segment-random-read",
        ],
        "matrix": {
            "format": "tofu-db.filesystem-certification-matrix.v1",
            "operatorCommand": "certify-filesystem-matrix",
            "mountDirectoryEnvironment": "TOFU_DB_CERT_MATRIX_MOUNT_DIR",
            "maximumTraceOperations": 4096,
            "maximumDetailCharacters": 1024,
            "requiredCells": [
                "primary.path", "primary.path.at-rest",
                "mount.opt-in", "mount.opt-in.at-rest",
                "vfs-fault.syscall-error.interrupted",
                "vfs-fault.syscall-error.storage-full",
                "vfs-fault.short-write", "vfs-fault.lost-sync",
                "vfs-fault.lost-sync.correlated-all",
                "vfs-fault.syscall-error.interrupted.at-rest",
                "vfs-fault.syscall-error.storage-full.at-rest",
                "vfs-fault.short-write.at-rest",
                "vfs-fault.lost-sync.at-rest",
                "vfs-fault.lost-sync.correlated-all.at-rest",
            ],
            "summaryOutcomes": ["pass", "partial", "fail"],
        },
        "resourceGate": {
            "format": "tofu-db.resource-gate.v1",
            "operatorCommand": "certify-resource-gate",
            "inlineCommits": 512,
            "blockCommits": 24,
            "blockBytes": 1048576,
            "maximumInlineBytes": 64,
            "referenceVolumeBytes": 500000000000,
            "referenceMemoryBytes": 8589934592,
            "referencePeakRssBytes": 536870912,
            "referenceRetainedBytes": 1073741824,
            "referenceCommitMeanMicros": 5000,
            "referenceTotalMicros": 300000000,
            "budgetPolicy": "observational-reference-budgets-for-one-operator-recorded-volume-class;runner-pins-build-and-volume-and-compares-exceeded-flags",
        },
    }
    assert document["blockPayloadReadCache"] == {
        "maximumResidentEntries": 65536,
        "maximumRecencyRecords": 131072,
        "residentBytesPolicy": "existing-launch-derived-byte-cap-charges-vector-capacity;entry-and-recency-caps-also-apply;zero-disables-all-population",
        "evictionPolicy": "bounded-access-history-with-exact-lru;disable-releases-payload-and-metadata-allocations"
    }
    assert document["payloadSegmentReadCache"] == {
        "maximumResidentIndexAndFilterBytes": 67108864,
        "independentMembershipBudgetDivisor": 4,
        "maximumResidentSegments": 4096,
        "maximumRecencyRecords": 8192,
        "membershipFilterBitsPerBlock": 16,
        "membershipFilterMinimumBits": 64,
        "membershipFilterHashRounds": 2,
        "maximumFilterBytesPerSegment": 131072,
        "membershipPolicy": "verified-index-derived-negative-filter;positive-requires-exact-id;payload-crc-and-hash-on-every-read",
        "evictionPolicy": "bounded-access-history-with-exact-lru;forget-on-segment-removal"
    }
    assert document["authorityGarbageCollection"] == {
        "maximumSessionCalls": 4096,
        "maximumOperatorMilliseconds": 600000,
        "defaultOperatorMilliseconds": 60000,
        "sessionPolicy": "exclusive-authority-borrow;stable-data-marks-plus-current-payload-manifest;no-cross-session-cache;poison-on-error;checked-finish-and-best-effort-drop-cleanup",
        "sessionTemporarySpacePolicy": "retained-marks-and-rewrite-share-existing-budget;release-marks-before-rewrite-when-combined-space-would-exceed-it",
        "sessionCompletionPolicy": "all-unchanged-catalog-windows-checked-before-idle;reset-on-manifest-change;plans-do-not-advance-coverage",
        "operatorBatchPolicy": "execute-only-max-rounds-and-budget-ms;soft-time-bound-between-rounds;stream-rounds-and-final-idle-or-budget-summary",
        "maximumVictimBytesPerRound": 268435456,
        "maximumTemporaryBytes": 1073741824,
        "temporaryFreeSpacePercent": 2,
        "leanTemporaryBytes": 67108864,
        "maximumBlocksPerRound": 65536,
        "maximumPhysicalBlocks": 20000000,
        "rpcMaximumPhysicalBlocksPerRequestedMillisecond": 256,
        "maximumTemporaryBlockFilesRemovedPerRound": 1,
        "maximumPayloadSegmentFilesScannedPerRound": 4163,
        "maximumOrphanPayloadSegmentFilesRemovedPerRound": 1,
        "maximumPayloadSegmentsScannedPerRound": 16,
        "minimumPayloadCompactionBlocks": 128,
        "snapshotPolicy": "defer-before-io-while-any-in-memory-mvcc-handle-exists",
        "temporaryBlockFilePolicy": "stream-match-strict-new-uuid-name-before-marking-remove-one-and-sync-shard",
        "controlPolicy": "republish-unchanged-state-before-first-delete-so-both-slots-share-roots",
        "payloadSegmentPolicy": "one-bounded-catalog-window-per-round-with-durable-control-cursor-and-at-most-one-segment-rewrite-or-retirement-after-loose-orphans",
        "payloadSegmentLivenessPolicy": "exact-block-id-intersection-across-all-hash-partitions;one-mark-run-and-one-segment-index-at-a-time",
        "orphanPayloadSegmentPolicy": "explicit-only-bounded-directory-window-stabilize-control-before-one-strictly-named-unreferenced-file-removal",
        "payloadCompactionPlanningPolicy": "one-bounded-loose-block-set-excluding-all-already-segmented-live-blocks-after-all-reclamation",
        "payloadCompactionMembershipPolicy": "bounded-filter-negative-or-exact-catalogued-index-membership;never-retain-catalog-wide-block-id-set",
        "looseMarkLookupPolicy": "skip-empty-shards;read-marks-only-for-populated-shards",
        "rpcOperation": "system.reclaim",
        "rpcBudgetPolicy": "validate-legacy-request-bounds;online-rpc-performs-no-physical-io",
        "rpcResponsePolicy": "online-collector-cumulative-progress;offline-not-required;statistics-after-first-epoch",
        "rpcTransactionPolicy": "explicit-physical-maintenance-without-business-transaction-or-receipt",
        "rpcPhysicalWalkPolicy": "online-two-mark-epoch-collector-reclaims;offline-exclusive-cli-remains-certified-heavy-hammer",
        "operatorPolicy": "existing-authority-plan-by-default-requires-execute-for-deletion"
    }
    assert "raw key bytes" in document["physicalRules"][0]
    assert any(
        "Automatic history maintenance retains at most 64" in rule
        for rule in document["physicalRules"]
    )
    operations = document["executableOperations"]
    names = [operation["name"] for operation in operations]
    # Native coverage is the complete semantic catalog, not a copied name list.
    assert names == sorted(build_registry())
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    catalog_by_name = {
        operation["name"]: operation for operation in catalog["operations"]
    }
    for operation in operations:
        frozen = catalog_by_name[operation["name"]]
        assert operation["kind"] == frozen["kind"]
        assert operation["receiptRequired"] == frozen["receiptRequired"]
    tables = {table["name"]: table for table in document["logicalTables"]}
    assert tables["tenant_users"] == {
        "name": "tenant_users",
        "ownerKey": "tenant_admin_boundary",
        "physicalFamily": "entity+blob",
        "profileNamespace": "tenant_user_profiles",
        "stateNamespace": "tenant_user_states",
        "emailIndexNamespace": "tenant_users_by_email_digest",
        "createdIndexNamespace": "tenant_users_by_created_desc",
        "statusIndexNamespace": "tenant_users_by_status_created_desc",
        "ownerSequenceNamespace": "tenant_user_owner_sequence",
        "maximumDocumentBytes": 8 * 1024 * 1024,
        "maximumListRows": 1000,
        "maximumListScanRows": 10_000,
        "maximumResponseBytes": 8 * 1024 * 1024,
        "constraints": [
            "account IDs and normalized emails are tenant-global unique while repository owner IDs are allocated monotonically from two",
            "offline restore claims an explicit owner identity and advances the sequence high-water without reallocating a live account",
            "profile blobs exclude mutable role, status, and login state so account control-plane writes never rewrite metadata blobs",
            "email digest indexes retain and verify the exact normalized email before lookup results are returned",
            "created and status indexes provide bounded descending list order without scanning profile payloads",
        ],
    }
    assert tables["chat_artifacts"]["maximumContentBytes"] == 8 * 1024 * 1024
    assert tables["chat_artifacts"]["maximumRowsPerConversation"] == 1000
    assert tables["tool_result_artifacts"]["maximumContentBytes"] == 16 * 1024 * 1024
    assert tables["tool_result_artifacts"]["maximumRangeBytes"] == 64 * 1024
    assert tables["storage_records"]["taskResultMaximumBytes"] == 64 * 1024 * 1024
    assert (
        tables["storage_records"]["taskResultPhysicalNamespace"]
        == "task_result_documents"
    )
    assert tables["storage_records"]["taskResultHeaderNamespace"] == "task_result_headers"
    assert (
        tables["storage_records"]["taskResultReplayNamespace"]
        == "task_result_replay_projections"
    )
    assert tables["storage_records"]["taskResultSummaryNamespace"] == "task_results_by_owner"
    assert (
        tables["storage_records"]["taskResultLiveNamespace"]
        == "task_results_live_by_owner"
    )
    assert (
        tables["storage_records"]["taskResultCostExperimentNamespace"]
        == "task_results_by_cost_experiment"
    )
    assert tables["storage_records"]["taskResultMaximumSummaryRows"] == 1000
    assert tables["storage_records"]["taskResultMaximumSummaryScanRows"] == 10_000
    assert tables["storage_records"]["taskResultSummaryPageRows"] == 1000
    assert tables["storage_records"]["taskResultMaximumRecoveryRows"] == 500
    assert tables["storage_records"]["taskResultMaximumRecoveryScanRows"] == 100_000
    assert tables["storage_records"]["taskResultMaximumCostExperimentRows"] == 10_000
    assert tables["storage_records"]["taskResultMaximumCostExperimentScanRows"] == 256
    assert tables["storage_records"]["taskResultCheckpointGuardContract"] == (
        "tofu.task-results.checkpoint.guard/v1"
    )
    assert tables["storage_records"]["taskResultCacheSettingsContract"] == (
        "tofu.task-results.checkpoint.cache-settings/v1"
    )
    assert tables["storage_records"]["taskResultCacheFactMaximum"] == 2_147_483_647
    assert tables["rate_limit_events"]["ownerKey"] == "authenticated_tenant_and_owner"
    assert tables["rate_limit_events"]["maximumPruneRows"] == 256
    assert tables["rate_limit_events"]["counterRadix"] == 256
    assert tables["rate_limit_events"]["counterDepth"] == 8
    assert tables["rate_limit_events"]["maximumWindowCounterReads"] == 2_040
    assert tables["daily_cost_cache"]["ownerKey"] == "authenticated_owner_user_id"
    assert tables["daily_cost_cache"]["physicalFamily"] == "entity+blob"
    assert tables["daily_cost_cache"]["maximumDocumentBytes"] == 8 * 1024 * 1024
    assert tables["daily_cost_cache"]["maximumMonthRows"] == 100
    assert tables["daily_cost_cache"]["maximumPersistedDateProbes"] == 366
    assert tables["daily_cost_cache"]["maximumResponseBytes"] == 8 * 1024 * 1024
    assert tables["log_aggregates"]["ownerKey"] == "authenticated_tenant_and_owner"
    assert tables["log_aggregates"]["physicalFamily"] == "entity+blob"
    assert tables["log_aggregates"]["maximumFlushBatch"] == 500
    assert tables["log_aggregates"]["maximumScanRows"] == 4096
    assert tables["log_aggregates"]["maximumSweepBatch"] == 500
    assert tables["project_brain_projects"]["ownerKey"] == "authenticated_owner_user_id"
    assert tables["project_brain_projects"]["physicalFamily"] == "entity+stream+blob"
    assert tables["project_brain_projects"]["maximumDocumentBytes"] == 8 * 1024 * 1024
    assert tables["project_brain_projects"]["maximumActiveWorkItems"] == 100
    assert tables["project_brain_projects"]["maximumWorkHistoryItems"] == 100
    assert tables["project_brain_projects"]["maximumNarratives"] == 500
    assert tables["project_brain_projects"]["maximumNarrativeTextBytes"] == 720
    assert tables["project_brain_projects"]["maximumWatchItems"] == 100
    assert tables["project_brain_projects"]["maximumCheckerVersions"] == 128
    assert tables["project_brain_projects"]["maximumCharterDecisions"] == 256
    assert tables["project_brain_projects"]["maximumCursors"] == 1000
    assert tables["model_routing"]["physicalFamily"] == "entity+blob"
    assert (
        tables["model_routing"]["authorityMetadataNamespace"]
        == "model_routing_authority_metadata"
    )
    assert tables["model_routing"]["maximumDocumentBytes"] == 8 * 1024 * 1024
    assert tables["model_routing"]["maximumSecretsPerOwnerBoundary"] == 1024
    assert tables["model_routing"]["maximumPrunedPerCommand"] == 256
    assert tables["storage_worker_jobs"]["maximumClaimTaskKinds"] == 32
    assert tables["storage_worker_jobs"]["maximumPriority"] == 1000
    assert tables["storage_worker_jobs"]["physicalFamily"] == "entity+blob"
    assert tables["browser_site_observations"]["ownerKey"] == "owner_user_id"
    assert tables["browser_site_observations"]["physicalFamily"] == "entity+blob"
    assert tables["browser_site_observations"]["maximumDocumentsPerOwner"] == 200
    assert (
        tables["browser_site_observations"]["maximumObservationPayloadBytes"]
        == 4096
    )
    assert tables["browser_site_observations"]["maximumStoredDocumentBytes"] == 8192
    assert tables["recent_projects"] == {
        "name": "recent_projects",
        "ownerKey": "authenticated_owner_user_id",
        "physicalFamily": "entity+blob",
        "physicalNamespace": "recent_project_documents",
        "countNamespace": "recent_project_counts",
        "maximumProjectsPerOwner": 1000,
        "maximumTouchBatch": 32,
        "maximumPathCharacters": 4096,
        "lifecycle": "durable-user-state",
        "constraints": [
            "physical keys are domain-separated SHA-256 path digests while every document retains and verifies the exact path",
            "an OCC-protected exact count bounds list materialization and rejects creation beyond 1000 owner-scoped projects",
            "touch_many deduplicates at most 32 paths in request order and updates the complete batch plus receipt and outbox atomically",
            "clear retires the complete owner-scoped document range and resets its exact count without walking project rows",
            "project relink folds the old digest document into the destination, summing use counts and taking the latest timestamp, then retires the old document while the exact owner count tracks the net document delta",
            "list verifies count-to-document agreement then preserves storage.v1 last_used descending order with path order as the stable tie break",
        ],
        "fields": [
            {"name": "path", "type": "utf8", "maximumCharacters": 4096, "nullable": False},
            {"name": "count", "type": "u64-positive", "nullable": False},
            {"name": "last_used", "type": "u64", "nullable": False},
        ],
    }
    assert tables["compaction_archives"]["ownerKey"] == "user_id"
    assert tables["compaction_archives"]["physicalFamily"] == "entity+blob"
    assert (
        tables["compaction_archives"]["tenantGlobalClaimNamespace"]
        == "compaction_archive_id_claims"
    )
    assert tables["compaction_archives"]["maximumArchivesPerConversation"] == 1000
    assert tables["conversations"]["ownerKey"] == "user_id"
    assert tables["conversations"]["physicalFamily"] == "entity+blob"
    assert (
        tables["conversations"]["tenantGlobalClaimNamespace"]
        == "conversation_id_claims"
    )
    assert (
        tables["conversations"]["updatedIndexNamespace"]
        == "conversation_by_updated"
    )
    assert (
        tables["conversations"]["executionEpochNamespace"]
        == "conversation_execution_epochs"
    )
    assert (
        tables["conversations"]["trashMetadataNamespace"]
        == "conversation_trash_metadata"
    )
    assert (
        tables["conversations"]["trashAgeIndexNamespace"]
        == "conversation_trash_by_age"
    )
    assert (
        tables["conversations"]["activityCandidateIndexNamespace"]
        == "conversation_activity_candidates_v2"
    )
    assert (
        tables["conversations"]["activityCandidateStateNamespace"]
        == "conversation_activity_candidate_state_v2"
    )
    assert (
        tables["conversations"]["maximumActivityCandidateBackfillRowsPerTransaction"]
        == 256
    )
    assert (
        tables["conversations"][
            "maximumActivityCandidateBackfillSourceBytesPerTransaction"
        ]
        == 16 * 1024 * 1024
    )
    assert tables["desktop_egress_preferences"]["ownerKey"] == "owner_user_id"
    assert tables["desktop_egress_preferences"]["physicalFamily"] == "entity"
    assert tables["storage_records"]["ownerKey"] == "authenticated_owner_user_id"
    assert tables["storage_records"]["physicalFamily"] == "entity+blob"
    assert tables["task_events"]["physicalFamily"] == "entity+stream"
    assert tables["conversation_turns"]["ownerKey"] == "user_id"
    assert tables["conversation_turns"]["physicalFamily"] == "entity+blob"
    assert (
        tables["conversation_turns"]["laneIndexNamespace"]
        == "turns_by_lane_ordinal"
    )
    assert (
        tables["conversation_turns"]["activityIndexNamespace"]
        == "turn_activity_timestamps"
    )
    assert (
        tables["conversation_turns"]["maximumActivityTurnRowsPerQuery"]
        == 100_000
    )
    assert (
        tables["conversation_turns"]["turnIdClaimNamespace"]
        == "turn_id_claims"
    )
    assert (
        tables["conversation_turns"]["attemptIdClaimNamespace"]
        == "attempt_id_claims"
    )
    assert tables["conversation_turns"]["attemptClaimLocatorVersion"] == 1
    assert (
        tables["conversation_turns"]["maximumTimingTraceClientObservations"]
        == 64
    )
    assert (
        tables["conversation_turns"]["maximumTimingTracePersistedBytes"]
        == 96 * 1024
    )
    assert (
        tables["conversation_turns"]["maximumTimingTraceCounter"]
        == 2_147_483_647
    )
    assert tables["conversation_turns"]["projectionHeadNamespace"] == "turn_projection_heads"
    assert (
        tables["conversation_turns"]["maximumTurnProjectionInlineLiveBytes"]
        == 64 * 1024
    )
    assert tables["conversation_turns"]["maximumTurnProjectionHeadPatches"] == 64
    assert (
        tables["conversation_turns"]["maximumTurnProjectionPatchBytes"]
        == 1024 * 1024
    )
    assert tables["conversation_turns"]["maximumTurnProjectionWirePatchBytes"] == 8 * 1024 * 1024
    assert tables["conversation_turns"]["maximumTurnProjectionPatchDepth"] == 128
    assert (
        tables["conversation_turns"]["maximumTurnProjectionPatchOperations"]
        == 65_536
    )
    assert (
        tables["conversation_turns"]["updatedIndexNamespace"]
        == "turns_by_updated"
    )
    assert (
        tables["conversation_turns"]["laneCountNamespace"]
        == "turn_lane_counts"
    )
    assert (
        tables["conversation_turns"]["tombstoneNamespace"]
        == "turn_tombstones"
    )
    assert (
        tables["conversation_turns"]["tombstoneAgeIndexNamespace"]
        == "turn_tombstones_by_age"
    )
    assert (
        tables["conversation_turns"]["supersededAttemptIndexNamespace"]
        == "generation_attempts_by_superseded"
    )
    assert tables["conversation_turns"]["maximumCleanupAttemptsPerTransaction"] == 64
    assert (
        tables["conversation_turns"]["maximumCleanupIndexScanRowsPerTransaction"]
        == 256
    )
