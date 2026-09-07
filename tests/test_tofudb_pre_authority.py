"""Contract guards for the isolated Tofu-DB durability prototype."""

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


ROOT = Path(__file__).resolve().parents[1]
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
    assert len(names) == len(set(names)) == document["operationCount"] == 331
    assert document["sourceSchemaVersion"] == SCHEMA_VERSION == 58
    assert document["sourceRegistryVersion"] == REGISTRY_VERSION == 38
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


def test_tofudb_has_no_application_authority_selection_path():
    supervisor = (ROOT / "lib" / "storage" / "supervisor.py").read_text(
        encoding="utf-8"
    )
    runtime = (ROOT / "lib" / "storage" / "runtime.py").read_text(encoding="utf-8")
    assert "tofu-db" not in supervisor
    assert "tofu-db" not in runtime


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
    assert document["frame"] == {
        "lengthPrefix": "u32-big-endian-body-bytes",
        "checksumSuffix": "crc32c-u32-big-endian-over-body",
        "maximumBodyBytes": 8 * 1024 * 1024,
        "maximumPayloadBytes": 8 * 1024 * 1024 - 4096,
        "maximumBlobChunkBytes": 1024 * 1024,
        "maximumInFlightFrames": 64,
        "maximumInFlightFrameBytes": 128 * 1024 * 1024,
        "readAdmission": "reserve-declared-body-before-allocation",
        "writeAdmission": "reserve-maximum-body-before-encoding",
        "canonicalFieldOrder": "strictly-increasing-numeric-id",
        "unknownFieldPolicy": "reject",
    }
    assert document["streamedRequests"] == {
        "maximumPayloadBytes": 64 * 1024 * 1024,
        "maximumChunks": 64,
        "allowedOperations": [
            "artifact.create",
            "task_results.checkpoint",
            "tool_result_artifact.put",
        ],
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
        "maximumConnections": 64,
        "minimumConnectionStackBytes": 262_144,
        "maximumConnectionStackBytes": 2_097_152,
        "maximumIoTimeoutMilliseconds": 300_000,
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
        "supervisorSelection": "forbidden-while-pre-authority",
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
        "maximumConnections": 64,
        "maximumFrameBytes": 134_217_728,
    }


def test_tofudb_crate_declares_bounded_durability_primitives():
    library = (ROOT / "packages" / "tofu-db" / "src" / "lib.rs").read_text(
        encoding="utf-8"
    )
    wal = (ROOT / "packages" / "tofu-db" / "src" / "wal.rs").read_text(encoding="utf-8")
    blocks = (ROOT / "packages" / "tofu-db" / "src" / "block.rs").read_text(
        encoding="utf-8"
    )
    entities = (ROOT / "packages" / "tofu-db" / "src" / "entity.rs").read_text(
        encoding="utf-8"
    )
    vfs = (ROOT / "packages" / "tofu-db" / "src" / "vfs.rs").read_text(encoding="utf-8")
    assert "64 * 1024 * 1024" in library
    assert "MAX_TRANSACTION_BYTES" in wal
    assert "crc32c::crc32c" in wal
    assert "blake3::hash" in wal
    assert "MAX_BLOCK_BYTES" in blocks
    assert "content-addressed block" in blocks
    assert "EntityTransaction" in entities
    assert "MAX_RANGE_WITNESS_LEAVES" in entities
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
        "maximumExecutableOperations": 331,
    }
    assert document["physicalBounds"] == {
        "maximumEntityKeyBytes": 3072,
        "maximumEntityInlineValueBytes": 8 * 1024,
        "maximumEntityRangeRows": 1000,
        "maximumEntityTransactionWrites": 14336,
        "maximumEntityPointWitnesses": 6144,
        "maximumEntityRangeWitnesses": 1024,
        "maximumAggregateEntityTransactionBytes": 160 * 1024 * 1024,
        "maximumPinnedEntitySnapshots": 64,
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
        "toolResultPruneRowsPerRound": 128,
        "authorityAdmission": "nonblocking-try-lock-one-bounded-transaction-per-round",
        "searchProjectionAdmission": "at-most-16-dirty-conversations-with-8-row-2MiB-source-pages-releasing-authority-between-pages",
        "searchProjectionMaximumSourcePagesPerConversation": 1024,
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
        "payloadBlockBytes": 1024 * 1024,
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
    }
    assert document["authorityGarbageCollection"] == {
        "maximumVictimBytesPerRound": 256 * 1024 * 1024,
        "maximumTemporaryBytes": 1024 * 1024 * 1024,
        "temporaryFreeSpacePercent": 2,
        "leanTemporaryBytes": 64 * 1024 * 1024,
        "maximumBlocksPerRound": 65_536,
        "maximumPhysicalBlocks": 20_000_000,
        "rpcMaximumPhysicalBlocksPerRequestedMillisecond": 256,
        "maximumTemporaryBlockFilesRemovedPerRound": 1,
        "maximumPayloadSegmentFilesScannedPerRound": 4097,
        "maximumOrphanPayloadSegmentFilesRemovedPerRound": 1,
        "maximumPayloadSegmentsScannedPerRound": 16,
        "minimumPayloadCompactionBlocks": 128,
        "snapshotPolicy": "defer-before-io-while-any-in-memory-mvcc-handle-exists",
        "temporaryBlockFilePolicy": "stream-match-strict-new-uuid-name-before-marking-remove-one-and-sync-shard",
        "controlPolicy": "republish-unchanged-state-before-first-delete-so-both-slots-share-roots",
        "operatorPolicy": "existing-authority-plan-by-default-requires-execute-for-deletion",
        "payloadSegmentPolicy": "one-hash-shard-per-round-with-durable-control-cursor-and-at-most-one-segment-rewrite-or-retirement-after-loose-orphans",
        "orphanPayloadSegmentPolicy": "explicit-only-bounded-directory-window-stabilize-control-before-one-strictly-named-unreferenced-file-removal",
        "payloadCompactionPlanningPolicy": "one-generation-selected-loose-hash-shard-excluding-already-segmented-live-blocks-after-all-reclamation",
        "rpcOperation": "system.reclaim",
        "rpcBudgetPolicy": "legacy-request-bounds-may-narrow-never-widen-launch-derived-engine-bounds",
        "rpcResponsePolicy": "content-free-backend-physical-progress",
        "rpcTransactionPolicy": "explicit-physical-maintenance-without-business-transaction-or-receipt",
    }
    assert "raw key bytes" in document["physicalRules"][0]
    assert any(
        "Automatic history maintenance retains at most 64" in rule
        for rule in document["physicalRules"]
    )
    operations = document["executableOperations"]
    names = [operation["name"] for operation in operations]
    assert (
        names
        == sorted(names)
        == [
            "artifact.create",
            "artifact.delete",
            "artifact.get",
            "artifact.library",
            "artifact.list",
            "artifact.pin",
            "artifact.versions",
            "billing.ledger.append",
            "billing.ledger.find",
            "billing.ledger.list",
            "billing.ledger.recompute",
            "billing.payment.find",
            "billing.payment.list",
            "billing.payment.record",
            "billing.payment.settle",
            "billing.redeem_code.apply",
            "billing.redeem_codes.list",
            "billing.redeem_codes.mint",
            "billing.reserve.stale",
            "billing.wallet.apply",
            "billing.wallet.get",
            "billing.wallet.settle",
            "browser.site_observation.get",
            "browser.site_observation.record",
            "compaction_archive.create",
            "compaction_archive.delete_conversation",
            "compaction_archive.get",
            "compaction_archive.list",
            "compaction_archive.prune",
            "compaction_archive.update_summary",
            "conversation.activity_dates",
            "conversation.clone",
            "conversation.count",
            "conversation.create",
            "conversation.delete",
            "conversation.get",
            "conversation.list",
            "conversation.metadata.update",
            "conversation.purge",
            "conversation.restore",
            "conversation.search",
            "conversation.settings.update",
            "conversation.trash.prune",
            "credential.authenticate",
            "credential.create",
            "credential.create_if_owner_empty",
            "credential.exists",
            "credential.get",
            "credential.identify",
            "credential.list",
            "credential.revoke",
            "credential.touch",
            "credential.update",
            "credential.validate",
            "daily_cost.delete",
            "daily_cost.latest",
            "daily_cost.month",
            "daily_cost.persisted_dates",
            "daily_cost.upsert",
            "desktop.egress_agent.get",
            "desktop.egress_agent.initialize",
            "desktop.egress_agent.set",
            "event.append",
            "event.append_batch",
            "event.bounds",
            "event.inspector_summary",
            "event.latest",
            "event.list",
            "event.prune",
            "goal.run.get",
            "goal.run.latest",
            "goal.run.start",
            "goal.run.transition",
            "integration.event.record",
            "integration.status",
            "integration.workspace.claim_next",
            "integration.workspace.discard",
            "integration.workspace.get",
            "integration.workspace.get_integrating",
            "integration.workspace.mark_failed",
            "integration.workspace.mark_merged",
            "integration.workspace.peek_ready",
            "integration.workspace.quarantine",
            "integration.workspace.register",
            "integration.workspace.requeue",
            "integration.workspace.retry",
            "integration.workspace.save_checkpoint",
            "integration.workspace.set_meta",
            "integration.workspace.submit",
            "knowledge.asset.claim",
            "knowledge.asset.get",
            "knowledge.asset.update",
            "knowledge.assets.mark_no_vision",
            "knowledge.availability",
            "knowledge.catalog",
            "knowledge.document.assets",
            "knowledge.document.content",
            "knowledge.document.create",
            "knowledge.document.delete",
            "knowledge.document.find_digest",
            "knowledge.document.get",
            "knowledge.document.list",
            "knowledge.document.metadata",
            "knowledge.document.patch",
            "knowledge.document.replace",
            "knowledge.enrichment.activity",
            "knowledge.enrichment.owners",
            "knowledge.owner.clear",
            "knowledge.search.candidates",
            "knowledge.settings.get",
            "knowledge.settings.patch",
            "log_aggregate.flush",
            "log_aggregate.query",
            "model_routing.commit",
            "model_routing.get",
            "model_routing.migration_receipt",
            "model_routing.migration_receipt.put",
            "model_routing.secret.delete",
            "model_routing.secret.get",
            "model_routing.secret.list",
            "model_routing.secret.prune",
            "model_routing.secret.put",
            "optimizer.action.expired",
            "optimizer.action.for_proposal",
            "optimizer.action.list",
            "optimizer.action.outcome",
            "optimizer.action.record",
            "optimizer.action.revert",
            "optimizer.proposal.create",
            "optimizer.proposal.get",
            "optimizer.proposal.list",
            "optimizer.proposal.update",
            "orchestration.definition.create",
            "orchestration.definition.delete",
            "orchestration.definition.get",
            "orchestration.definition.list",
            "orchestration.definition.update",
            "orchestration.event.append",
            "orchestration.event.page",
            "orchestration.event.project",
            "orchestration.run.create",
            "orchestration.run.delete",
            "orchestration.run.get",
            "orchestration.run.list",
            "orchestration.run.retire_interrupted",
            "orchestration.run.retire_interrupted_all",
            "orchestration.run.update_status",
            "paper.library.delete",
            "paper.library.get",
            "paper.library.identity",
            "paper.library.inputs",
            "paper.library.list",
            "paper.library.put",
            "paper.library.reader",
            "paper.library.recent",
            "paper.library.summaries",
            "paper.library.title.backfill",
            "paper.note.create",
            "paper.note.delete",
            "paper.note.list",
            "paper.note.update",
            "paper.podcast.get",
            "paper.podcast.mark_interrupted",
            "paper.podcast.upsert",
            "paper.report.excerpts",
            "paper.report.get",
            "paper.report.latest",
            "paper.report.reopen",
            "paper.report.resolve",
            "paper.report.second_pass.accumulate",
            "paper.report.second_pass.merge",
            "paper.report.upsert",
            "paper.translation.get",
            "paper.translation.upsert",
            "plugin.manifest.get",
            "plugin.register",
            "project.recent.clear",
            "project.recent.list",
            "project.recent.touch",
            "project.recent.touch_many",
            "project.relink",
            "project_brain.active.list",
            "project_brain.checker.register",
            "project_brain.checker.result",
            "project_brain.cursor.confirm",
            "project_brain.cursor.prepare",
            "project_brain.cutover",
            "project_brain.cutover.status",
            "project_brain.decision.promote",
            "project_brain.get",
            "project_brain.narrative.add",
            "project_brain.rebuild",
            "project_brain.recovery.snapshot",
            "project_brain.watch.add",
            "project_brain.watch.delete",
            "project_brain.watch.update",
            "project_brain.work.change",
            "project_brain.work.finish",
            "project_brain.work.refine",
            "project_brain.work.start",
            "provider.create",
            "provider.delete",
            "provider.get",
            "provider.list",
            "provider.touch",
            "provider.update",
            "queue.autopilot.arm",
            "queue.autopilot.clear",
            "queue.autopilot.get",
            "queue.autopilot.list_all",
            "queue.clear",
            "queue.conversations.list_all",
            "queue.depth",
            "queue.dequeue",
            "queue.enqueue",
            "queue.finalize",
            "queue.kind.clear",
            "queue.lease.bind",
            "queue.lease.release",
            "queue.list",
            "queue.reap",
            "queue.remove",
            "rate_limit.record_and_check",
            "raw_archive.list",
            "raw_archive.put",
            "raw_archive.read",
            "record.delete",
            "record.get",
            "record.list",
            "record.put",
            "research.artifact.upsert",
            "research.artifacts.get",
            "research.directions.list",
            "research.workspace.get",
            "research.workspace.put",
            "scheduler.poll.append",
            "scheduler.poll.log",
            "scheduler.task.claim_due",
            "scheduler.task.create",
            "scheduler.task.delete",
            "scheduler.task.ensure",
            "scheduler.task.get",
            "scheduler.task.list",
            "scheduler.task.list_all",
            "scheduler.task.record_result",
            "scheduler.task.update",
            "swarm.agent.save",
            "swarm.agents.mark_delivered",
            "swarm.resumable.list",
            "swarm.session.delete",
            "swarm.session.get",
            "swarm.session.quarantine_ownerless",
            "swarm.session.save",
            "swarm.session.terminate",
            "system.reclaim",
            "system.schema_version",
            "task_results.abort",
            "task_results.abort_requested",
            "task_results.checkpoint",
            "task_results.cost_experiment_scan",
            "task_results.recover_running",
            "task_results.replay_get",
            "task_results.summary_list",
            "tenant.user.authentication",
            "tenant.user.create",
            "tenant.user.get",
            "tenant.user.list",
            "tenant.user.record_login",
            "tenant.user.set_role",
            "tenant.user.set_status",
            "timer.active.count",
            "timer.active.list_all",
            "timer.cancel",
            "timer.create",
            "timer.get",
            "timer.history",
            "timer.list",
            "timer.poll.append",
            "timer.poll.commit",
            "timer.poll.log",
            "timer.progress",
            "timer.update",
            "tool_result_artifact.prune",
            "tool_result_artifact.put",
            "tool_result_artifact.read",
            "tool_result_artifact.search",
            "turn.append_settled",
            "turn.attempt.bind",
            "turn.attempt.claim",
            "turn.attempt.create",
            "turn.attempt.dispatch_worker",
            "turn.attempt.dispatchable.list",
            "turn.attempt.get",
            "turn.attempt.start",
            "turn.branch.create",
            "turn.branch.delete",
            "turn.compact",
            "turn.create_pair",
            "turn.delete",
            "turn.event.record",
            "turn.events.list",
            "turn.events.prune",
            "turn.exists",
            "turn.get",
            "turn.image.get",
            "turn.list",
            "turn.list_delta",
            "turn.perception.record",
            "turn.projection.update",
            "turn.queue.activate",
            "turn.queue.cancel",
            "turn.recover",
            "turn.related.announce",
            "turn.revision",
            "turn.steer.commit",
            "turn.sync.changes",
            "turn.sync.page",
            "turn.sync.prune",
            "turn.sync.snapshot",
            "turn.timing_trace.get",
            "turn.timing_trace.list",
            "turn.visible.sync",
            "worker_job.claim_next",
            "worker_job.claim_state",
            "worker_job.complete",
            "worker_job.enqueue",
            "worker_job.get",
            "worker_job.heartbeat",
            "worker_job.request_cancel",
        ]
    )
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
        == "conversation_activity_candidates"
    )
    assert (
        tables["conversations"]["activityCandidateStateNamespace"]
        == "conversation_activity_candidate_state"
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
