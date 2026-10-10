#!/usr/bin/env python3
"""Generate engine/migration bounds and worker lifecycle types from contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "contracts" / "tofudb_ir_v1.json"
OUTPUT = ROOT / "packages" / "tofu-db" / "src" / "generated_tofudb_ir.rs"
MIGRATION_OUTPUT = ROOT / "lib" / "storage_sidecar" / "generated_tofudb_migration.py"
SEARCH_SOURCE = ROOT / "contracts" / "conversation_search_v2.json"
SEARCH_OUTPUT = ROOT / "packages" / "tofu-db" / "src" / "generated_conversation_search.rs"


RECEIPT_NAMES = {
    'RECEIPT_COMPRESSION_THRESHOLD_BYTES': 'compressionThresholdBytes',
    'MAX_STORED_RECEIPT_BYTES': 'maximumStoredBytes',
    'MAX_DECODED_RECEIPT_BYTES': 'maximumDecodedBytes',
    'MAX_INLINE_RECEIPT_BYTES': 'maximumInlineBytes',
}


def _worker_lifecycle() -> dict:
    document = json.loads((ROOT / 'contracts/worker_job_lifecycle_v1.json').read_text())
    states = document.get('states')
    terminals = document.get('terminalStates')
    if (document.get('format') != 'tofu.worker-job-lifecycle/v1' or not isinstance(states, list)
            or len(states) != len(set(states)) or any(not isinstance(s,str) or not s.isidentifier() for s in states)
            or terminals != ['succeeded', 'failed', 'cancelled'] or not set(terminals) <= set(states)
            or document.get('claimableStates') != ['queued', 'suspended']):
        raise ValueError('invalid canonical worker lifecycle')
    lease = document.get('leaseAuthority', {})
    if (not isinstance(lease, dict) or lease.get('leaseIndexMarker') != 255
            or lease.get('epochCharacters') != 32 or lease.get('maximumLeaseProbesPerKind') != 4
            or any(not isinstance(lease.get(key), str) or not lease[key]
                   for key in ('clock', 'lifetime', 'callerTime', 'restart', 'legacy'))):
        raise ValueError('invalid worker lease authority contract')
    server = lease.get('serverClock')
    if (lease.get('maximumLeaseMilliseconds') != 300_000 or not isinstance(server, dict)
            or server.get('format') != 'tofu.worker-lease-clock/v1'
            or server.get('minimumPostgresMajor') != 17 or server.get('maximumStateBytes') != 128
            or server.get('fields') != ['format', 'epoch', 'nowMs', 'utcMs', 'stateBytes']
            or not isinstance(lease.get('eligibilityClock'), str) or not lease['eligibilityClock']):
        raise ValueError('invalid shared server worker clock contract')
    return document


def _automation_runtime() -> dict:
    document = json.loads((ROOT / 'contracts/automation_v1.schema.json').read_text())['x-runtime']
    for key in ('terminalRunStates', 'activeEffectStepStates', 'cancellablePendingStepStates'):
        values = document[key]
        if (not isinstance(values, list) or not values or len(set(values)) != len(values)
                or any(not isinstance(value, str) or not value.isidentifier() for value in values)):
            raise ValueError('Invalid automation lifecycle vocabulary')
    return document


def _automation_namespaces() -> dict:
    return json.loads((ROOT / 'contracts/automation_storage_v1.schema.json').read_text())['x-namespaces']


def _automation_catalog_json() -> str:
    return json.dumps(json.loads((ROOT / 'contracts/automation_actions_v1.json').read_text()),
                      sort_keys=True,ensure_ascii=False,separators=(',', ':'))


def _load() -> dict:
    document = json.loads(SOURCE.read_text(encoding="utf-8"))
    if document.get("format") != "tofu.database-ir.v1":
        raise ValueError("tofudb_ir_v1.json has an unexpected format")
    receipts = document['commandReceipts']
    if any(type(receipts[key]) is not int or receipts[key] <= 0 for key in RECEIPT_NAMES.values()):
        raise ValueError('invalid command receipt byte budget')
    if not (receipts['maximumInlineBytes'] < receipts['compressionThresholdBytes']
            < receipts['maximumStoredBytes'] == receipts['maximumDecodedBytes'] <= 4 * 1024 * 1024):
        raise ValueError('inconsistent command receipt budgets')
    expected_migration = {
        "checkpointFormat": "tofu-db.migration-checkpoint.v1",
        "maximumCheckpointBytes": 65536,
        "maximumCursorBytes": 8192,
        "maximumDomains": 128,
        "maximumKeyColumns": 8,
        "maximumSourceRowBytes": 8388608,
        "maximumSourceWideRowBytes": 16777216,
        "maximumSourceChunkBytes": 262144,
        "maximumFrozenTranscriptSourceBytes": 67108864,
        "maximumSourceBatchRows": 1000,
        "maximumSourceTables": 512,
        "maximumSourceColumns": 256,
        "identityFields": ["sourceSha256", "sourceSchemaVersion",
                           "targetAuthorityUuid", "planSha256"],
        "publication": "file-sync-atomic-replace-directory-sync",
        "completedDomainCursor": "released-after-full-verification",
        "resume": "reject-changed-source-target-or-plan;verify-before-progress",
        "documentImport": {
            "mode": "insert-or-match", "maximumJSONBytes": 8388608,
            "taskResultMaximumJSONBytes": 12582912,
            "compactionArchiveMaximumDecodedBytes": 12582912,
            "metadata": "preserve-positive-version-and-timestamp",
            "targetBinding": "expected-authority-uuid",
            "publication": "caller-owned-authority-transaction",
            "indexes": "domain-adapter-owned",
        },
    }
    expected_migration["legacyReceiptImport"] = {'maximumRows': 1000000, 'maximumStoredBytes': 1073741824, 'scope': 'one-source-authority-per-tenant;reserved-global-owner;exact-request-digest-and-explicit-authenticated-owner-before-replay', 'reservedOwnerId': 18446744073709551614}
    expected_migration["retiredLargeArchive"] = {'maximumSourceRows': 65536, 'maximumRowBytes': 67108864, 'maximumEncodedBytes': 68719476736, 'maximumTargetRecords': 1048576, 'inlineCellBytes': 1024, 'maximumRootBytes': 1048576, 'publication': 'parts-before-row-manifest;verified-prefix-before-checkpoint;inactive-user-history;no-automatic-expiry'}
    expected_migration["operatorSession"] = {'command': 'migration-session',
     'format': 'tofu-db.offline-migration.v1',
     'transport': 'exclusive-authority-lease;private-stdin-stdout;no-network',
     'maximumFrameBytes': 16785408,
     'maximumCatalogPageRows': 128,
     'maximumCatalogPageBytes': 8388608,
     'snapshot': 'exact-json-bytes-version-and-timestamp;insert-or-match',
     'scope': 'explicit-positive-tenant-and-owner;uuid-on-every-request',
     'semanticOperations': ['billing.ledger.list',
                            'billing.wallet.get',
                            'conversation.count',
                            'conversation.create',
                            'conversation.get',
                            'conversation.list',
                            'credential.create',
                            'credential.identify',
                            'credential.list',
                            'credential.revoke',
                            'credential.touch',
                            'credential.update',
                            'provider.create',
                            'provider.get',
                            'provider.list',
                            'provider.touch',
                            'provider.update'],
     'recordOperations': ['migration.record.restore',
                          'migration.record.get',
                          'migration.record.scan'],
     'commandReceiptOperations': ['migration.command.receipt.restore',
                                  'migration.command.receipt.get',
                                  'migration.command.receipt.scan',
                                  'migration.legacy.receipt.restore',
                                  'migration.legacy.receipt.get',
                                  'migration.legacy.receipt.scan',
                                  'migration.legacy.receipt.stats'],
     'commandReceiptSnapshot': 'explicit-tenant-owner;exact-command-key-request-digest-encoded-response-clock;insert-or-match;atomic-live-receipt-blob-family-publication;no-owner-inference-or-digest-rewriting',
     'excludedRecordNamespaces': ['task_results',
                                  'paper.build.budget.v1',
                                  'paper.document.budget.v1'],
     'excludedRecordPrefixes': [],
     'publication': 'semantic-domains-own-indexes;external-checkpoint-only-after-independent-verification',
     'billingOperations': ['migration.billing.restore',
                           'migration.billing.get',
                           'migration.billing.scan'],
     'billingSnapshot': 'exact-wallet-and-ledger-fields;insert-or-match;atomic-domain-indexes;independent-primary-and-derived-catalogs',
     'tenantUserOperations': ['migration.tenant.user.restore',
                              'migration.tenant.user.get',
                              'migration.tenant.user.scan'],
     'tenantUserSnapshot': 'exact-profile-state-owner-and-password;insert-or-match;claim-owner-and-advance-sequence;independent-catalog',
     'taskResultOperations': ['migration.task.result.restore',
                              'migration.task.result.get',
                              'migration.task.result.scan'],
     'taskResultSnapshot': 'exact-document-version-and-timestamp;insert-or-match;atomic-summary-live-and-cost-indexes;independent-catalogs',
     'conversationOperations': ['migration.conversation.restore'],
     'trashOperations': ['migration.trash.begin', 'migration.trash.finish', 'migration.trash.get', 'migration.trash.scan', 'migration.trash.turn.get', 'migration.trash.turn.scan'],
     'trashSnapshot': 'one-owner-scoped-staging-capsule;header-turns-frozen-pages-verified-before-seal;committed-seal-before-range-pin;resume-before-active-domain-verification;pending-stage-refuses-serving;catalog-exposes-pending-source;completion-requires-no-stage',
     'conversationSnapshot': 'header-identity-title-settings-timestamps-revision;insert-or-match;turn-import-preserves-header-clock;independent-catalog-page',
     'turnOperations': ['migration.turn.restore',
                        'migration.turn.get',
                        'migration.turn.scan', 'migration.turn.tombstone.restore',
                        'migration.turn.tombstone.get', 'migration.turn.tombstone.scan', 'migration.turn.transfer.begin', 'migration.turn.transfer.append', 'migration.turn.transfer.finish', 'migration.turn.transfer.abort', 'migration.turn.transfer.read.begin', 'migration.turn.transfer.read.chunk', 'migration.turn.transfer.read.abort'],
     'attemptEventUploadOperations': ['migration.attempt.event.upload.begin', 'migration.attempt.event.upload.append', 'migration.attempt.event.upload.finish', 'migration.attempt.event.upload.abort'],
     'attemptEventUpload': 'one-32MiB-volatile-buffer-per-exclusive-session;256KiB-hex-decoded-chunks;exact-offset-or-identical-replay;sha256-before-atomic-publication;abort-eof-or-failed-finish-releases-buffer',
     'attemptEventOperations': ['migration.attempt.event.restore', 'migration.attempt.event.restore_batch', 'migration.attempt.event.get', 'migration.attempt.event.scan', 'migration.attempt.event.chunk'],
     'attemptEventSnapshot': 'exact-event-json-bytes-and-created-timestamp;insert-or-match;terminal-attempt-owner-fence;atomic-event-and-maximum-sequence;retention-gaps-preserved;independent-owner-catalog;payload-byte-accounting-derived-from-native-bytes',
     'attemptOperations': ['migration.attempt.restore', 'migration.attempt.get', 'migration.attempt.scan'],
     'attemptSnapshot': 'exact-terminal-attempt-identity-command-config-error-timing-queue-and-timestamps;insert-or-match;atomic-locator-command-turn-directory-timing-retention-indexes;detached-history-retained;independent-owner-catalog',
     'storageEventOperations': ['migration.storage.event.restore',
                                'migration.storage.event.restore_batch',
                                'migration.storage.event.get',
                                'migration.storage.event.scan'],
     'storageEventSnapshot': 'caller-explicit-application-sequence-and-payload-bytes;insert-or-match;atomic-sparse-index-and-physical-stream;no-sequence-recomputation;independent-task-catalog',
     'conversationSyncEventOperations': ['migration.conversation.sync.event.restore',
                                         'migration.conversation.sync.event.get',
                                         'migration.conversation.sync.event.scan'],
     'conversationSyncEventSnapshot': 'caller-explicit-sync-sequence-and-envelope-payload;insert-or-match;exact-event-contract-and-timestamps;retention-indexes-preserved;head-restored-separately',
     'conversationSyncHeadOperations': ['migration.conversation.sync.head.restore',
                                        'migration.conversation.sync.head.get',
                                        'migration.conversation.sync.head.scan'],
     'conversationSyncHeadSnapshot': 'caller-explicit-sync-sequence;insert-or-match;restored-after-all-changes;mismatch-refused',
     'turnSnapshot': 'exact-identity-projection-settlement-ordinal-timestamps;insert-or-match;claim-turn-id;independent-catalog;terminal-current-attempt-pointer-verified-against-imported-attempt;live-attempts-refused',
     'continuationOperations': ['migration.continuation.restore',
                                'migration.continuation.get',
                                'migration.continuation.scan'],
     'continuationSnapshot': 'exact-projection-frontier-version-and-timestamp;insert-or-match;owner-bound-record-key;no-derived-indexes;independent-catalog',
     'paperBuildBudgetOperations': ['migration.paper.build.budget.restore',
                                    'migration.paper.build.budget.get'],
     'paperBuildBudgetSnapshot': 'exact-ledger-record-count-and-reserved-bytes;owner-scoped-reservation-claims;insert-or-match;artifact-metadata-cross-validation;tenant-global-atomic-rebuild',
     'paperDocumentBudgetOperations': ['migration.paper.document.budget.restore',
                                       'migration.paper.document.budget.get'],
     'paperDocumentBudgetSnapshot': 'exact-record-and-association-counts;insert-or-match;tenant-global-atomic-rebuild',
     'desktopEgressOperations': ['migration.desktop.egress.restore',
                                  'migration.desktop.egress.get',
                                  'migration.desktop.egress.scan'],
     'desktopEgressSnapshot': 'exact-agent-id-and-timestamp;insert-or-match;single-owner-scoped-document;independent-catalog',
     'dailyCostOperations': ['migration.daily.cost.restore',
                             'migration.daily.cost.get',
                             'migration.daily.cost.scan'],
     'dailyCostSnapshot': 'exact-date-cost-conversations-and-computed-at;insert-or-match;atomic-owner-count;independent-date-catalog',
     'paperLibraryOperations': ['migration.paper.library.restore',
                                'migration.paper.library.get',
                                'migration.paper.library.scan'],
     'paperLibrarySnapshot': 'exact-library-state-and-core-fields;insert-or-match;blob-capable-parsed-text-and-auxiliary-json;atomic-updated-hash-and-arxiv-indexes-and-owner-count;independent-id-catalog',
     'paperReportOperations': ['migration.paper.report.restore',
                               'migration.paper.report.get',
                               'migration.paper.report.scan'],
     'paperReportSnapshot': 'exact-report-core-and-metadata-fields;insert-or-match;blob-capable-report-body;atomic-latest-index-presence-and-owner-count;independent-identity-catalog',
     'paperTranslationOperations': ['migration.paper.translation.restore',
                                    'migration.paper.translation.get',
                                    'migration.paper.translation.scan'],
     'paperTranslationSnapshot': 'exact-translation-text-model-and-created-at;insert-or-match;blob-capable-translation-text;atomic-owner-count;independent-identity-catalog',
     'paperNoteOperations': ['migration.paper.note.restore',
                             'migration.paper.note.get',
                             'migration.paper.note.scan'],
     'paperNoteSnapshot': 'exact-note-anchor-body-and-timestamps;insert-or-match;atomic-paper-lang-created-index-and-owner-count;independent-id-catalog',
     'paperPodcastOperations': ['migration.paper.podcast.restore',
                                'migration.paper.podcast.get',
                                'migration.paper.podcast.scan'],
     'paperPodcastSnapshot': 'exact-podcast-core-and-state-fields;insert-or-match;blob-capable-script;atomic-tenant-global-active-locator-and-counts;independent-identity-catalog',
     'knowledgeOperations': ['migration.knowledge.settings.restore',
                             'migration.knowledge.settings.get',
                             'migration.knowledge.document.restore',
                             'migration.knowledge.document.get',
                             'migration.knowledge.catalog.scan'],
     'knowledgeSnapshot': 'exact-document-chunk-asset-link-and-settings-fields;insert-or-match;blob-capable-chunk-content-and-asset-text;atomic-digest-ordinal-term-link-enrichment-queue-and-tenant-global-owner-indexes;independent-document-catalog',
     'chatArtifactOperations': ['migration.chat.artifact.restore',
                                'migration.chat.artifact.get',
                                'migration.chat.artifact.scan'],
     'chatArtifactSnapshot': 'exact-metadata-version-parent-pinned-meta-timestamps-and-content-bytes;insert-or-match;blob-capable-content;atomic-dedupe-conversation-library-parent-and-path-indexes;independent-id-catalog',
     'toolResultOperations': ['migration.tool.result.restore',
                              'migration.tool.result.get',
                              'migration.tool.result.scan'],
     'toolResultSnapshot': 'exact-content-bytes-media-type-size-and-lifecycle-timestamps;insert-or-match;blob-capable-content;atomic-expiry-index;independent-digest-catalog',
     'browserObservationOperations': ['migration.browser.observation.restore',
                                      'migration.browser.observation.get',
                                      'migration.browser.observation.scan'],
     'browserObservationSnapshot': 'exact-observation-document-version-and-timestamp;insert-or-match;atomic-lru-index-and-owner-touch;independent-identity-catalog',
     'modelRoutingAuthorityOperations': ['migration.model.routing.authority.restore',
                                         'migration.model.routing.authority.get',
                                         'migration.model.routing.authority.scan'],
     'modelRoutingAuthoritySnapshot': 'exact-revision-document-backup-receipt-and-float-timestamp;insert-or-match;atomic-authority-metadata;independent-boundary-catalog',
     'modelRoutingSecretOperations': ['migration.model.routing.secret.restore',
                                      'migration.model.routing.secret.get',
                                      'migration.model.routing.secret.scan'],
     'modelRoutingSecretSnapshot': 'exact-ciphertext-bytes-key-hint-and-float-timestamps;no-secret-material-in-logs;insert-or-match;atomic-reference-updated-indexes-and-owner-count;independent-identity-catalog',
     'pluginManifestOperations': ['migration.plugin.manifest.restore',
                                  'migration.plugin.manifest.get',
                                  'migration.plugin.manifest.scan'],
     'pluginManifestSnapshot': 'exact-manifest-json-version-and-timestamp;insert-or-match;atomic-version-registration;independent-namespace-catalog',
     'pluginRowsOperations': ['migration.plugin.rows.restore',
                              'migration.plugin.rows.get',
                              'migration.plugin.rows.scan'],
     'pluginRowsSnapshot': 'exact-utf8-document-json-version-and-timestamp;insert-or-match;atomic-unique-value-rebuild;independent-composite-key-catalog',
     'pluginUniqueOperations': ['migration.plugin.unique.restore',
                                'migration.plugin.unique.get',
                                'migration.plugin.unique.scan'],
     'pluginUniqueSnapshot': 'exact-unique-value-claims-against-restored-plugin-rows;insert-or-match;row-presence-cross-validation;independent-claim-catalog',
     'projectBrainOperations': ['migration.project.brain.project.restore',
                                'migration.project.brain.project.get',
                                'migration.project.brain.project.scan'],
     'projectBrainSnapshot': 'exact-projection-json-head-checkpoint-sequences-and-timestamp;insert-or-match;atomic-updated-index;independent-key-catalog',
     'optimizerOperations': ['migration.optimizer.restore',
                             'migration.optimizer.get',
                             'migration.optimizer.scan'],
     'optimizerSnapshot': 'exact-proposal-and-action-fields;insert-or-match;atomic-created-status-proposal-applied-active-and-expiry-indexes-and-owner-counts;independent-id-catalogs',
     'logAggregateOperations': ['migration.log.aggregate.restore',
                                'migration.log.aggregate.get',
                                'migration.log.aggregate.scan'],
     'logAggregateSnapshot': 'exact-level-logger-template-sample-count-and-first-last-seen;insert-or-match;atomic-sweep-index-and-sequence-advance;independent-fingerprint-catalog',
     'billingPaymentOperations': ['migration.billing.payment.restore',
                                  'migration.billing.payment.get',
                                  'migration.billing.payment.scan'],
     'billingPaymentSnapshot': 'exact-payment-fields-status-timestamps-and-raw-document;insert-or-match;atomic-id-and-provider-claims-created-index-and-count;tenant-global;independent-id-catalog',
     'billingRedeemOperations': ['migration.billing.redeem.restore',
                                 'migration.billing.redeem.get',
                                 'migration.billing.redeem.scan'],
     'billingRedeemSnapshot': 'exact-batch-of-codes-amount-timestamps-and-redemption-states;ten-thousand-code-batches-in-one-transaction;insert-or-match;atomic-batch-locator-state-shards-created-indexes-and-code-count;tenant-global;independent-code-and-batch-catalogs',
     'rateLimitEventOperations': ['migration.rate.limit.event.restore',
                                  'migration.rate.limit.event.get',
                                  'migration.rate.limit.event.scan'],
     'rateLimitEventSnapshot': 'exact-endpoint-client-key-occurred-and-expires-timestamps;insert-or-match;atomic-radix-counter-path-expiry-index-and-monotonic-bucket-clock;independent-event-catalog-cross-checked-against-counter-total',
     'orchestrationDefinitionOperations': ['migration.orchestration.definition.restore',
                                           'migration.orchestration.definition.get',
                                           'migration.orchestration.definition.scan'],
     'orchestrationDefinitionSnapshot': 'exact-name-definition-and-created-updated-timestamps;insert-or-match;tenant-global-id-claim;atomic-owner-updated-index-and-count;independent-id-catalog',
     'orchestrationRunOperations': ['migration.orchestration.run.restore',
                                    'migration.orchestration.run.get',
                                    'migration.orchestration.run.scan'],
     'orchestrationRunSnapshot': 'exact-core-and-lifecycle-state-with-goal-fields-continuity-election-and-timestamps;insert-or-match;tenant-global-id-claim;atomic-created-status-orchestration-goal-and-active-indexes-and-count;independent-id-catalog',
     'orchestrationEventOperations': ['migration.orchestration.event.restore',
                                      'migration.orchestration.event.get',
                                      'migration.orchestration.event.scan'],
     'orchestrationEventSnapshot': 'exact-run-sequence-and-payload;insert-or-match;sequence-bound-by-restored-frontier;independent-run-scoped-catalog',
     'swarmSessionOperations': ['migration.swarm.session.restore',
                                'migration.swarm.session.get',
                                'migration.swarm.session.scan'],
     'swarmSessionSnapshot': 'exact-document-and-state-with-created-updated-timestamps;insert-or-match;tenant-global-key-claim;atomic-resumable-index-and-owner-count;independent-key-catalog',
     'swarmAgentOperations': ['migration.swarm.agent.restore',
                              'migration.swarm.agent.get',
                              'migration.swarm.agent.scan'],
     'swarmAgentSnapshot': 'exact-core-and-state-with-rounds-delivered-and-updated-timestamp;insert-or-match;atomic-agent-count-and-session-resumable-derivation;independent-identity-catalog',
     'integrationWorkspaceOperations': ['migration.integration.workspace.restore',
                                        'migration.integration.workspace.get',
                                        'migration.integration.workspace.scan'],
     'integrationWorkspaceSnapshot': 'exact-id-document-and-timestamps-with-origin;insert-or-match;natural-claim-and-row-locator;atomic-ready-integrating-project-indexes-active-and-owner-counts-and-row-sequence;independent-identity-catalog',
     'integrationEventOperations': ['migration.integration.event.restore',
                                    'migration.integration.event.get',
                                    'migration.integration.event.scan'],
     'integrationEventSnapshot': 'exact-id-kind-message-detail-and-created-timestamp;insert-or-match;atomic-event-count-and-sequence-advance;independent-project-catalog',
     'integrationMetaOperations': ['migration.integration.meta.restore'],
     'integrationMetaSnapshot': 'exact-origin-document-merge-into-workspace;insert-or-match;no-queue-clock-advance;workspace-scoped',
     'queueItemOperations': ['migration.queue.item.restore',
                             'migration.queue.item.get',
                             'migration.queue.item.scan'],
     'queueItemSnapshot': 'exact-core-payload-config-position-kind-priority-created-lease-and-turn-bindings;insert-or-match;atomic-conversation-order-index-summary-locators-claim-and-lease-index;independent-claim-catalog-cross-checked-against-order-index',
     'queueAutopilotMarkerOperations': ['migration.queue.autopilot.marker.restore',
                                        'migration.queue.autopilot.marker.get',
                                        'migration.queue.autopilot.marker.scan'],
     'queueAutopilotMarkerSnapshot': 'exact-queue-id-config-and-created-timestamp;insert-or-match;atomic-global-owner-index;independent-conversation-catalog',
     'timerOperations': ['migration.timer.restore',
                         'migration.timer.get',
                         'migration.timer.scan'],
     'timerSnapshot': 'exact-document-fields-and-counters;insert-or-match;atomic-status-conversation-and-global-active-indexes-id-claim-and-total-active-counts;independent-claim-catalog-cross-checked-against-count',
     'timerPollOperations': ['migration.timer.poll.restore',
                             'migration.timer.poll.get',
                             'migration.timer.poll.scan'],
     'timerPollSnapshot': 'exact-sequence-id-and-poll-fields;insert-or-match;atomic-time-index-poll-id-claim-and-global-sequence-advance;timer-scoped;independent-poll-catalog',
     'schedulerTaskOperations': ['migration.scheduler.task.restore',
                                 'migration.scheduler.task.get',
                                 'migration.scheduler.task.scan'],
     'schedulerTaskSnapshot': 'exact-document-fields-and-counters;insert-or-match;atomic-created-and-enabled-indexes-id-and-system-key-claims-and-owner-count;independent-claim-catalog-cross-checked-against-count',
     'schedulerPollOperations': ['migration.scheduler.poll.restore',
                                 'migration.scheduler.poll.get',
                                 'migration.scheduler.poll.scan'],
     'schedulerPollSnapshot': 'exact-sequence-id-and-poll-fields;insert-or-match;atomic-time-index-and-global-sequence-advance;task-scoped;independent-poll-catalog',
     'frozenTranscriptOperations': ['migration.frozen.transcript.begin', 'migration.frozen.transcript.restore', 'migration.frozen.transcript.finish', 'migration.frozen.transcript.get', 'migration.frozen.transcript.scan', 'migration.frozen.transcript.chunk'],
     'frozenTranscriptUploadOperations': ['migration.frozen.transcript.upload.begin', 'migration.frozen.transcript.upload.append', 'migration.frozen.transcript.upload.finish', 'migration.frozen.transcript.upload.abort'],
     'rawArchiveOperations': ['migration.raw.archive.restore', 'migration.raw.archive.get', 'migration.raw.archive.scan', 'migration.raw.archive.stats'],
     'rawArchiveSnapshot': 'exact-compressed-bytes-metadata-and-part-availability;insert-or-match;detached-owner-history-retained;atomic-identity-covering-indexes-and-byte-counters;hard-quota-refusal-without-scrubbing;independent-owner-catalog-and-scoped-statistics',
     'compactionArchiveOperations': ['migration.compaction.archive.restore', 'migration.compaction.archive.get', 'migration.compaction.archive.scan'],
     'compactionArchiveSnapshot': 'exact-root-archive-messages-post-messages-summary-receipt-metadata-and-source-byte-count;insert-or-match;atomic-identity-chronological-index-and-count;independent-owner-catalog;scoped-child-archives-refused;detached-history-retained-with-live-parent-read-fence',
     'workerJobOperations': ['migration.worker.job.restore',
                             'migration.worker.job.get',
                             'migration.worker.job.scan'],
     'workerJobSnapshot': 'exact-status-fencing-attempt-lease-result-references-and-timestamps;insert-or-match;atomic-idempotency-claim-queued-lease-indexes-and-priority-summary;tenant-global;independent-owner-claim-catalog'}
    if document.get("conversationTrashMigration") != {'stageNamespace': 'migration_conversation_trash_stage', 'maximumPendingPerOwner': 1, 'maximumTurnsPerCapsule': 65536, 'maximumStageBytes': 8388608}:
        raise ValueError("unexpected conversation trash migration contract")
    if document.get("offlineMigration") != expected_migration:
        raise ValueError("Tofu-DB offline migration contract differs")
    if document.get("turnSnapshots") != {'maximumSourceBytes': 67108864, 'maximumSnapshotBytes': 67043328, 'maximumResponseBytes': 67104768, 'lifecycle': 'one-exclusive-session-buffer;256KiB-chunks;sha256-before-atomic-restore;read-buffer-released-at-final-chunk;abort-and-eof-release;ordinary-writes-remain-8MiB'}:
        raise ValueError("turn snapshot contract differs")
    if document.get("frozenTranscripts") != {'manifestNamespace': 'frozen_transcript_manifests', 'pageNamespace': 'frozen_transcript_pages', 'maximumPages': 1024, 'maximumMessages': 65536, 'targetPageBytes': 4194304, 'maximumPageBytes': 67043328, 'maximumDecodedBytes': 1073741824, 'lifecycle': 'owner-scoped-immutable-pages;complete-manifest-before-read;turns-take-precedence;recoverable-range-pins;blob-sharing-clone', 'responseBudget': 'message-bearing-conversation-list-shares-page-byte-ceiling;metadata-only-lists-keep-ordinary-8MiB-budget'}:
        raise ValueError('frozen transcript contract drifted')
    if document.get("transcriptArchives") != {'manifestNamespace': 'transcript_archive_manifests', 'pageNamespace': 'transcript_archive_pages', 'maximumPages': 4096, 'maximumMessages': 262144, 'tailTargetBytes': 262144, 'maximumDecodedBytes': 1073741824, 'lifecycle': 'owner-scoped-live-tail;append-maintained;other-mutations-invalidate;lane-count-fence;exact-read-fallback', 'responseBudget': 'message-bearing-conversation-list-shares-page-byte-ceiling;metadata-only-lists-keep-ordinary-8MiB-budget'}:
        raise ValueError("frozen transcript contract differs")
    if document.get("migrationStores") != {
        "browserObservationOwnerTouchNamespace": "browser_site_observation_owner_touches",
        "pluginRowDocumentNamespace": "plugin_row_documents",
        "pluginRowUniqueNamespace": "plugin_row_unique_values",
        "maximumPluginRowDocumentBytes": 8 * 1024 * 1024,
        "maximumPluginRowFields": 64,
        "maximumPluginUniqueValuesPerRow": 16,
    }:
        raise ValueError("Tofu-DB migration store namespaces differ")
    catalog = json.loads((ROOT / "contracts" / "storage_operations_v1.json").read_text(encoding="utf-8"))
    catalog_names = sorted(operation["name"] for operation in catalog["operations"])
    bounds = document.get("bounds")
    if document.get("attemptExecutionConfig") != {
        "operation":"turn.attempt.get","flag":"include_execution_config","default":False,
        "authorization":"existing owner locator/live parent/turn proof applies before private config is returned",
        "delivery":"internal lifecycle port only; public endpoints do not forward the flag",
    }:
        raise ValueError("attempt execution config authority differs")
    expected_bounds = {
        "maximumSteps": 128,
        "maximumSlots": 64,
        "maximumLiteralBytes": 8 * 1024 * 1024,
        "maximumExecutableOperations": len(catalog_names),
    }
    if bounds != expected_bounds:
        raise ValueError("Tofu-DB IR bounds differ from the certified v1 bounds")
    expected_physical_bounds = {
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
    if document.get("physicalBounds") != expected_physical_bounds:
        raise ValueError("Tofu-DB physical bounds differ from the certified v1 bounds")
    if (
        expected_physical_bounds["maximumEntityMountConsolidationRows"] + 1
        > expected_physical_bounds["maximumEntityRangeRows"]
    ):
        raise ValueError("entity mount consolidation bounds are inconsistent")
    if document.get("physicalFamilies") != ["entity", "stream", "blob"]:
        raise ValueError("Tofu-DB IR physical families are incomplete")
    expected_persistent_snapshots = {
        "rootPinNamespace": "entity_root_pins",
        "catalogNamespace": "entity_root_pin_catalog",
        "maximumPinsPerAuthority": 1_000_000,
        "maximumLegacyPinsWithoutCatalog": 64,
        "maximumPinIdBytes": 128,
        "maximumRangesPerCapsule": 64,
    }
    if document.get("persistentSnapshots") != expected_persistent_snapshots:
        raise ValueError("Tofu-DB persistent snapshot bounds are incomplete")
    if (
        expected_persistent_snapshots["rootPinNamespace"]
        == expected_persistent_snapshots["catalogNamespace"]
        or len(expected_persistent_snapshots["catalogNamespace"].encode("utf-8")) > 63
        or expected_persistent_snapshots["maximumPinsPerAuthority"]
        > expected_physical_bounds["maximumEntityReachabilityPages"]
        or expected_persistent_snapshots["maximumLegacyPinsWithoutCatalog"]
        >= expected_persistent_snapshots["maximumPinsPerAuthority"]
    ):
        raise ValueError("persistent snapshot catalog bounds are inconsistent")
    if (
        expected_persistent_snapshots["maximumRangesPerCapsule"]
        != expected_physical_bounds["maximumEntityRetiredRanges"]
    ):
        raise ValueError("snapshot capsule and retirement range bounds differ")
    expected_wal_rotation = {
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
    if document.get("activeWalRotation") != expected_wal_rotation:
        raise ValueError("active WAL rotation contract mismatch")
    expected_page_packing = {
        "targetBytes": 4096,
        "maximumBytes": 16384,
        "minimumChildrenPerSplitPage": 2,
        "encodingAllocation": "exact-page-length;transfer-to-existing-pending-store;retain-no-spare-capacity",
        "modifiedLeafRuns": "repack-adjacent-written-leaves-with-one-page-buffer;retain-tombstones-and-untouched-references",
        "tailRebalance": "redistribute-or-merge-within-maximum-page-bytes",
    }
    if document.get("entityPagePacking") != expected_page_packing:
        raise ValueError("Tofu-DB entity page packing bounds differ")
    expected_maintenance_scheduler = {
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
    if document.get("maintenanceScheduler") != expected_maintenance_scheduler:
        raise ValueError("Tofu-DB maintenance scheduler contract is incomplete")
    expected_search_projection = {
        "minimumBytesPerOwner": 128 * 1024 * 1024,
        "maximumBytesPerOwner": 4 * 1024 * 1024 * 1024,
        "observedVolumeFreePercent": 2,
        "pathPolicy": "explicit-absolute-dedicated-persistent-directory-outside-authority",
        "queryAdmission": "independent-deadline-bounded-projection-mutex",
        "unavailablePolicy": "retryable-capability-error-without-authority-failure",
    }
    if document.get("searchProjection") != expected_search_projection:
        raise ValueError("Tofu-DB search projection contract is incomplete")
    expected_filesystem_certification = {
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
                "vfs-fault.short-write.at-rest", "vfs-fault.lost-sync.at-rest",
                "vfs-fault.lost-sync.correlated-all.at-rest",
            ],
            "summaryOutcomes": ["pass", "partial", "fail"],
        },
        "resourceGate": {
            "format": "tofu-db.resource-gate.v1",
            "operatorCommand": "certify-resource-gate",
            # The fixed gate workload: sequential inline commits plus 1 MiB
            # block-carried commits that cross the 60 MiB rotation window
            # with one midpoint checkpoint, one explicit commit+checkpoint
            # stage, and one reopen. Block bytes stay below the WAL carried
            # ceiling so the checkpoint stage exercises segment rotation
            # rather than loose-block spill.
            "inlineCommits": 512,
            "blockCommits": 24,
            "blockBytes": 1024 * 1024,
            "maximumInlineBytes": 64,
            # Observational reference budgets for the personal-computer
            # contract (8 GiB RAM / 500 GB disk) on one operator-recorded
            # volume class; the runner pins build and volume and compares
            # the emitted exceeded flags.
            "referenceVolumeBytes": 500_000_000_000,
            "referenceMemoryBytes": 8 * 1024**3,
            "referencePeakRssBytes": 512 * 1024**2,
            "referenceRetainedBytes": 1024**3,
            "referenceCommitMeanMicros": 5000,
            "referenceTotalMicros": 300_000_000,
            "budgetPolicy": "observational-reference-budgets-for-one-operator-recorded-volume-class;runner-pins-build-and-volume-and-compares-exceeded-flags",
        },
    }
    if document.get("filesystemCertification") != expected_filesystem_certification:
        raise ValueError("Tofu-DB filesystem certification contract is incomplete")
    expected_block_payload_cache = {
        "maximumResidentEntries": 65536,
        "maximumRecencyRecords": 131072,
        "residentBytesPolicy": "existing-launch-derived-byte-cap-charges-vector-capacity;entry-and-recency-caps-also-apply;zero-disables-all-population",
        "evictionPolicy": "bounded-access-history-with-exact-lru;disable-releases-payload-and-metadata-allocations"
    }
    if document.get("blockPayloadReadCache") != expected_block_payload_cache:
        raise ValueError("Tofu-DB block payload read cache policy is incomplete")
    expected_payload_read_cache = {
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
    if document.get("payloadSegmentReadCache") != expected_payload_read_cache:
        raise ValueError("Tofu-DB payload-segment read cache policy is incomplete")
    expected_authority_gc = {
        "maximumSessionCalls": 4096,
        "maximumOperatorMilliseconds": 600000,
        "defaultOperatorMilliseconds": 60000,
        "sessionPolicy": "exclusive-authority-borrow;stable-data-marks-plus-current-payload-manifest;no-cross-session-cache;poison-on-error;checked-finish-and-best-effort-drop-cleanup",
        "sessionTemporarySpacePolicy": "retained-marks-and-rewrite-share-existing-budget;release-marks-before-rewrite-when-combined-space-would-exceed-it",
        "sessionCompletionPolicy": "all-unchanged-catalog-windows-checked-before-idle;reset-on-manifest-change;plans-do-not-advance-coverage",
        "operatorBatchPolicy": "execute-only-max-rounds-and-budget-ms;soft-time-bound-between-rounds;stream-rounds-and-final-idle-or-budget-summary",
        "maximumVictimBytesPerRound": 256 * 1024 * 1024,
        "maximumTemporaryBytes": 1024 * 1024 * 1024,
        "temporaryFreeSpacePercent": 2,
        "leanTemporaryBytes": 64 * 1024 * 1024,
        "maximumBlocksPerRound": 65_536,
        "maximumPhysicalBlocks": 20_000_000,
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
        "operatorPolicy": "existing-authority-plan-by-default-requires-execute-for-deletion",
    }
    if document.get("authorityGarbageCollection") != expected_authority_gc:
        raise ValueError("Tofu-DB authority garbage-collection contract is incomplete")
    expected_physical_rules = [
        "Entity B+Tree order is tenant ID, owner ID, physical namespace, then raw key bytes; key length never precedes key content.",
        "Every authority commit carries a tri-state entity-root update in its v2 transaction envelope and publishes the resulting current root in checksummed CONTROL; normal entity open verifies that CONTROL root without reading history, while legacy slots use one exact bounded transaction lookup.",
        "Every live entity transaction pins its immutable MVCC root in a database-instance registry; handles have no count admission quota, release is tied to handle lifetime, and handles cannot cross reopen boundaries.",
        "Entity transactions retain all distinct point and range witnesses needed for OCC without a witness-count admission quota; repeated point reads reuse their existing witness.",
        "The per-open entity transaction registry accounts actual retained bytes across point witnesses, worst-case range witnesses, and staged writes without an aggregate admission quota; arithmetic overflow fails closed and reservations release with transaction lifetime.",
        "Normal entity open reads and verifies only the content-addressed current root node; fully materialized roots without restored ranges remain direct B+Tree pages; partial materialization uses a version-2 directory with exact WAL coverage, while a versioned directory may overlay at most 64 sorted non-overlapping capsule range mounts on one base root. Descendant pages are verified lazily, base records and tombstones override mounted values, writes COW only the base, and one transaction may retire at most 64 sorted non-overlapping ranges or change mounts under an exact root witness. Explicit lazy consolidation materializes at most 999 rows and 8388608 bytes per commit by atomically replacing a mounted prefix in the base; it never runs during open and eventually collapses an exhausted directory to its direct base root.",
        "Explicit entity reachability walks are streaming and reject level or key-range transplants without a database-sized visited set; they admit at most 20000000 pages and an 8192-node frontier, follow root-directory bases and mounts plus at most 1000000 authority-global monotonic persistent full roots or 64-range capsules declared by the current CONTROL root, and require the discovered pin count to equal its transactional catalog. Catalogued pin admission uses bounded point reads; an absent catalog may upgrade at most 64 legacy pins.",
        "Explicit backup and garbage-collection reachability follows persisted stream segment references plus blob-backed versioned-document, command-receipt, and logical-outbox leaf values as owner-bound semantic graph edges without consulting transaction history; ordinary open and foreground range retirement do not scan values.",
        "Stream cursors and immutable segment references live in the current entity root alongside retained-prefix watermarks; metadata v2 decodes legacy v1 with a retained base of one, normal authority open never rebuilds catalogs from historical commits, retired cursors fail explicitly, and reads resolve at most 1000 events and 8 MiB from the caller's MVCC snapshot.",
        "Automatic history maintenance retains at most 64 complete immutable 4 MiB segments through a suffix-only v2 manifest, never rewrites transaction payloads or checkpoints an active WAL, and publishes only after one launch-probed low-priority maintenance admission.",
        "Explicit authority garbage collection first stream-matches at most 20000000 loose-directory entries with constant retained memory and removes at most one strictly named bounded .new temporary block file before reachability marking. It then streams current Entity, persistent capsule, semantic external-block, retained-history, and active-WAL reachability into bounded on-volume mark runs; it refuses live MVCC handles, republishes unchanged CONTROL state before referenced-file deletion, removes at most 65536 loose blocks or 268435456 bytes, scans at most 4163 payload-segment directory entries (4096 catalog slots + 2 in-flight background publications + 64 deferred retirements + 1) and removes at most one strictly named unreferenced complete or temporary segment, then inspects one generation-selected catalog window of at most 16 manifest segments and rewrites or retires at most one through a double-stabilized catalog per repeatable round. When the selected round has no reclaimable victim, it may compact at least 128 and at most 65536 reachable, not-already-segmented loose blocks in one bounded cross-shard batch within both the 268435456-byte segment ceiling and the launch-derived temporary-space budget; an executed no-victim catalog window advances the durable CONTROL cursor.",
    ]
    if document.get("physicalRules") != expected_physical_rules:
        raise ValueError("Tofu-DB physical ordering rule is incomplete")
    operations = document.get("executableOperations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("Tofu-DB IR needs executable operations")
    if len(operations) != bounds["maximumExecutableOperations"]:
        raise ValueError("executable operation inventory exceeds or differs from its certified count")
    names = [operation.get("name") for operation in operations]
    if names != sorted(names) or len(names) != len(set(names)):
        raise ValueError("Tofu-DB executable operations must be sorted and unique")
    if names != catalog_names:
        raise ValueError("native executable operations differ from the application catalog")
    tables = document.get("logicalTables")
    if not isinstance(tables, list) or len(tables) != 39:
        raise ValueError("Tofu-DB IR v1 must declare its implemented logical tables")
    tables_by_name = {table.get("name"): table for table in tables}
    browser_observations = tables_by_name.get("browser_site_observations")
    desktop = tables_by_name.get("desktop_egress_preferences")
    records = tables_by_name.get("storage_records")
    rate_limits = tables_by_name.get("rate_limit_events")
    daily_costs = tables_by_name.get("daily_cost_cache")
    orchestration_definitions = tables_by_name.get("orchestration_definitions")
    orchestration_runs = tables_by_name.get("orchestration_runs")
    swarm = tables_by_name.get("swarm_sessions")
    integration = tables_by_name.get("integration_workspaces")
    project_brain = tables_by_name.get("project_brain_projects")
    scheduler = tables_by_name.get("scheduled_tasks")
    queue = tables_by_name.get("conversation_queue")
    timers = tables_by_name.get("timers")
    recent_projects = tables_by_name.get("recent_projects")
    providers = tables_by_name.get("byo_providers")
    artifacts = tables_by_name.get("chat_artifacts")
    tool_result_artifacts = tables_by_name.get("tool_result_artifacts")
    log_aggregates = tables_by_name.get("log_aggregates")
    optimizer_actions = tables_by_name.get("optimizer_actions")
    optimizer_proposals = tables_by_name.get("optimizer_proposals")
    paper_library = tables_by_name.get("paper_library")
    paper_notes = tables_by_name.get("paper_notes")
    plugin_manifests = tables_by_name.get("plugin_manifests")
    knowledge_documents = tables_by_name.get("knowledge_documents")
    paper_reports = tables_by_name.get("paper_reports")
    paper_translations = tables_by_name.get("paper_translations")
    paper_podcasts = tables_by_name.get("paper_podcasts")
    raw_archives = tables_by_name.get("raw_archives")
    research_artifacts = tables_by_name.get("research_artifacts")
    research_workspaces = tables_by_name.get("research_workspaces")
    model_routing = tables_by_name.get("model_routing")
    worker_jobs = tables_by_name.get("storage_worker_jobs")
    events = tables_by_name.get("task_events")
    conversations = tables_by_name.get("conversations")
    turns = tables_by_name.get("conversation_turns")
    archives = tables_by_name.get("compaction_archives")
    tenant_users = tables_by_name.get("tenant_users")
    credentials = tables_by_name.get("auth_credentials")
    billing = tables_by_name.get("billing_wallets_and_ledger")
    if (
        not isinstance(orchestration_definitions, dict)
        or orchestration_definitions.get("ownerKey") != "authenticated_tenant_and_owner"
        or orchestration_definitions.get("physicalFamily") != "entity+blob"
        or orchestration_definitions.get("documentNamespace")
        != "orchestration_definition_documents"
        or orchestration_definitions.get("tenantGlobalIdClaimNamespace")
        != "orchestration_definition_id_claims"
        or orchestration_definitions.get("ownerUpdatedIndexNamespace")
        != "orchestration_definitions_by_updated_desc"
        or orchestration_definitions.get("ownerCountNamespace")
        != "orchestration_definition_counts"
        or orchestration_definitions.get("maximumDefinitionsPerOwner") != 1000
        or orchestration_definitions.get("maximumDefinitionDocumentBytes")
        != 8 * 1024 * 1024
        or orchestration_definitions.get("maximumDefinitionIdCharacters") != 200
        or orchestration_definitions.get("maximumResponseBytes") != 8 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB orchestration definitions must be bounded and exact")
    if (
        not isinstance(orchestration_runs, dict)
        or orchestration_runs.get("ownerKey")
        != "authenticated_tenant_and_owner_or_internal_tenant_maintenance"
        or orchestration_runs.get("physicalFamily") != "entity+blob"
        or orchestration_runs.get("coreNamespace") != "orchestration_run_cores"
        or orchestration_runs.get("stateNamespace") != "orchestration_run_states"
        or orchestration_runs.get("tenantGlobalIdClaimNamespace")
        != "orchestration_run_id_claims"
        or orchestration_runs.get("ownerCreatedIndexNamespace")
        != "orchestration_runs_by_created_desc"
        or orchestration_runs.get("ownerStatusCreatedIndexNamespace")
        != "orchestration_runs_by_status_created_desc"
        or orchestration_runs.get("ownerOrchestrationCreatedIndexNamespace")
        != "orchestration_runs_by_orchestration_created_desc"
        or orchestration_runs.get("tenantGlobalActiveIndexNamespace")
        != "orchestration_runs_global_active"
        or orchestration_runs.get("tenantGlobalPendingContinuityIndexNamespace")
        != "orchestration_runs_pending_continuity"
        or orchestration_runs.get("goalActiveClaimNamespace")
        != "orchestration_goal_active_runs"
        or orchestration_runs.get("ownerGoalCreatedIndexNamespace")
        != "orchestration_goal_runs_by_created_desc"
        or orchestration_runs.get("eventDocumentNamespace")
        != "orchestration_run_event_documents"
        or orchestration_runs.get("ownerCountNamespace") != "orchestration_run_counts"
        or orchestration_runs.get("maximumRunsPerOwner") != 10_000
        or orchestration_runs.get("maximumRunCoreBytes") != 8 * 1024 * 1024
        or orchestration_runs.get("maximumRunStateBytes") != 8 * 1024 * 1024
        or orchestration_runs.get("maximumEventsPerRun") != 100_000
        or orchestration_runs.get("maximumEventDocumentBytes") != 1024 * 1024
        or orchestration_runs.get("maximumEventPageRows") != 2000
        or orchestration_runs.get("maximumListRows") != 200
        or orchestration_runs.get("maximumMaintenanceRows") != 1000
        or orchestration_runs.get("maximumRunIdCharacters") != 200
        or orchestration_runs.get("maximumTenantLabelCharacters") != 256
        or orchestration_runs.get("maximumResponseBytes") != 8 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB orchestration runs must be bounded and exact")
    if (
        not isinstance(swarm, dict)
        or swarm.get("ownerKey") != "authenticated_tenant_and_owner"
        or swarm.get("physicalFamily") != "entity+blob"
        or swarm.get("sessionDocumentNamespace") != "swarm_session_documents"
        or swarm.get("sessionStateNamespace") != "swarm_session_states"
        or swarm.get("tenantGlobalKeyClaimNamespace") != "swarm_session_key_claims"
        or swarm.get("ownerResumableIndexNamespace")
        != "swarm_sessions_resumable_by_updated_desc"
        or swarm.get("ownerSessionCountNamespace") != "swarm_session_counts"
        or swarm.get("agentCoreNamespace") != "swarm_agent_cores"
        or swarm.get("agentStateNamespace") != "swarm_agent_states"
        or swarm.get("agentCountNamespace") != "swarm_agent_counts"
        or swarm.get("maximumSessionsPerOwner") != 512
        or swarm.get("maximumAgentsPerSession") != 1000
        or swarm.get("maximumSessionDocumentBytes") != 8 * 1024 * 1024
        or swarm.get("maximumAgentCoreBytes") != 64 * 1024 * 1024 - 65536
        or swarm.get("maximumResponseBytes") != 64 * 1024 * 1024 - 4096
        or swarm.get("maximumSwarmKeyCharacters") != 512
        or swarm.get("maximumAgentIdCharacters") != 512
        or swarm.get("maximumObjectiveCharacters") != 100_000
        or swarm.get("maximumAgentIdsPerDelivery") != 1000
        or swarm.get("maximumRoundsUsed") != 1_000_000
    ):
        raise ValueError("Tofu-DB swarm persistence must be bounded and owner scoped")
    if (
        not isinstance(integration, dict)
        or integration.get("ownerKey")
        != "authenticated_owner_or_internal_tenant_worker"
        or integration.get("physicalFamily") != "entity"
        or integration.get("workspaceNamespace")
        != "integration_workspace_documents"
        or integration.get("naturalKeyClaimNamespace")
        != "integration_workspace_natural_claims"
        or integration.get("rowLocatorNamespace")
        != "integration_workspace_row_locators"
        or integration.get("rowSequenceNamespace")
        != "integration_workspace_row_sequence"
        or integration.get("globalReadyIndexNamespace")
        != "integration_workspaces_ready_by_updated_asc"
        or integration.get("globalIntegratingIndexNamespace")
        != "integration_workspaces_integrating_by_updated_asc"
        or integration.get("globalProjectActiveClaimNamespace")
        != "integration_project_active_claims"
        or integration.get("globalActiveCountNamespace") != "integration_active_count"
        or integration.get("ownerProjectUpdatedIndexNamespace")
        != "integration_workspaces_by_project_updated_desc"
        or integration.get("ownerWorkspaceCountNamespace")
        != "integration_workspace_counts"
        or integration.get("eventNamespace") != "integration_event_documents"
        or integration.get("eventSequenceNamespace") != "integration_event_sequence"
        or integration.get("eventCountNamespace") != "integration_event_counts"
        or integration.get("maximumWorkspacesPerOwner") != 10_000
        or integration.get("maximumActiveWorkspacesPerAuthority") != 1000
        or integration.get("maximumStatusRows") != 1000
        or integration.get("maximumEventsPerProject") != 300
        or integration.get("maximumStatusEvents") != 30
        or integration.get("maximumProjectRootCharacters") != 4096
        or integration.get("maximumTaskIdCharacters") != 512
        or integration.get("maximumWorkspaceDocumentBytes") != 16_384
        or integration.get("maximumResponseBytes") != 8 * 1024 * 1024
        or integration.get("staleIntegratingSeconds") != 660
    ):
        raise ValueError("Tofu-DB integration workspaces must be bounded and exact")
    if (
        not isinstance(queue, dict)
        or queue.get("ownerKey") != "authenticated_tenant_and_owner"
        or queue.get("physicalFamily") != "entity+blob"
        or queue.get("coreNamespace") != "queue_item_cores"
        or queue.get("stateNamespace") != "queue_item_states"
        or queue.get("tenantGlobalIdClaimNamespace") != "queue_item_id_claims"
        or queue.get("ownerConversationOrderIndexNamespace")
        != "queue_items_by_conversation_order"
        or queue.get("tenantGlobalConversationIndexNamespace")
        != "queue_conversations_by_kind_oldest"
        or queue.get("tenantGlobalLeaseIndexNamespace") != "queue_leases_by_expiry"
        or queue.get("autopilotMarkerNamespace") != "queue_autopilot_markers"
        or queue.get("tenantGlobalAutopilotIndexNamespace")
        != "queue_autopilot_by_owner_conversation"
        or queue.get("maximumItemsPerConversation") != 512
        or queue.get("maximumItemCoreBytes") != 1024 * 1024
        or queue.get("maximumGlobalConversationRows") != 1000
        or queue.get("maximumAutopilotMarkers") != 1000
        or queue.get("maximumReapItems") != 128
        or queue.get("maximumResponseBytes") != 8 * 1024 * 1024
        or queue.get("maximumIdCharacters") != 256
        or queue.get("maximumConversationIdCharacters") != 256
    ):
        raise ValueError("Tofu-DB conversation queue must be bounded and owner scoped")
    if (
        not isinstance(scheduler, dict)
        or scheduler.get("ownerKey") != "authenticated_tenant_and_owner"
        or scheduler.get("physicalFamily") != "entity+blob"
        or scheduler.get("physicalNamespace") != "scheduled_task_documents"
        or scheduler.get("tenantGlobalIdClaimNamespace") != "scheduled_task_id_claims"
        or scheduler.get("systemKeyClaimNamespace") != "scheduled_task_system_key_claims"
        or scheduler.get("createdIndexNamespace") != "scheduled_tasks_by_created_desc"
        or scheduler.get("enabledCreatedIndexNamespace")
        != "scheduled_tasks_enabled_by_created_desc"
        or scheduler.get("tenantGlobalCreatedIndexNamespace")
        != "scheduled_tasks_global_by_created_desc"
        or scheduler.get("tenantGlobalEnabledCreatedIndexNamespace")
        != "scheduled_tasks_global_enabled_by_created_desc"
        or scheduler.get("ownerCountNamespace") != "scheduled_task_counts"
        or scheduler.get("pollDocumentNamespace") != "scheduler_poll_documents"
        or scheduler.get("pollTimeIndexNamespace")
        != "scheduler_polls_by_task_time_desc"
        or scheduler.get("pollSequenceNamespace") != "scheduler_poll_global_sequence"
        or "maximumTasksPerOwner" in scheduler
        or scheduler.get("maximumTaskDocumentBytes") != 1_048_576
        or scheduler.get("maximumTaskIdCharacters") != 128
        or scheduler.get("maximumSystemKeyCharacters") != 128
        or scheduler.get("listLimitZeroReadsAll") is not True
        or "maximumTaskListRows" in scheduler
        or scheduler.get("maximumPollRows") != 200
        or scheduler.get("maximumPollDocumentBytes") != 16_384
    ):
        raise ValueError("Tofu-DB scheduled-task physical contract is incomplete")
    if (
        not isinstance(timers, dict)
        or timers.get("ownerKey") != "authenticated_tenant_and_owner"
        or timers.get("physicalFamily") != "entity+blob"
        or timers.get("physicalNamespace") != "timer_documents"
        or timers.get("tenantGlobalIdClaimNamespace") != "timer_id_claims"
        or timers.get("ownerStatusCreatedIndexNamespace") != "timers_by_status_created_desc"
        or timers.get("ownerConversationIndexNamespace") != "timers_by_conversation"
        or timers.get("tenantGlobalActiveCreatedIndexNamespace")
        != "timers_global_active_by_created_asc"
        or timers.get("ownerTotalCountNamespace") != "timer_total_counts"
        or timers.get("ownerActiveCountNamespace") != "timer_active_counts"
        or timers.get("pollDocumentNamespace") != "timer_poll_documents"
        or timers.get("pollTimeIndexNamespace") != "timer_polls_by_timer_time_desc"
        or timers.get("pollIdClaimNamespace") != "timer_poll_id_claims"
        or timers.get("pollSequenceNamespace") != "timer_poll_global_sequence"
        or timers.get("countPolicy") != "observational-counts-without-admission-quota"
        or timers.get("maximumTimerDocumentBytes") != 1_048_576
        or timers.get("maximumTimerIdCharacters") != 128
        or timers.get("maximumTimerListRows") != 200
        or timers.get("maximumGlobalActiveRows") != 2_000
        or timers.get("maximumPollRows") != 200
        or timers.get("maximumPollDocumentBytes") != 16_384
    ):
        raise ValueError("Tofu-DB timer physical contract is incomplete")
    if (
        not isinstance(billing, dict)
        or billing.get("ownerKey") != "billing_subject_user_id"
        or billing.get("physicalFamily") != "entity+blob"
        or billing.get("walletNamespace") != "billing_wallets"
        or billing.get("ledgerNamespace") != "billing_ledger_entries"
        or billing.get("ledgerIdClaimNamespace") != "billing_ledger_id_claims"
        or billing.get("idempotencyNamespace") != "billing_ledger_idempotency"
        or billing.get("userTimeIndexNamespace") != "billing_ledger_by_user_time_desc"
        or billing.get("userAggregateNamespace") != "billing_ledger_user_aggregates"
        or billing.get("reserveStateNamespace") != "billing_reserve_states"
        or billing.get("reserveAgeIndexNamespace") != "billing_reserves_by_age"
        or billing.get("paymentDocumentNamespace") != "billing_payment_documents"
        or billing.get("paymentIdClaimNamespace") != "billing_payment_id_claims"
        or billing.get("paymentProviderClaimNamespace") != "billing_payment_provider_claims"
        or billing.get("paymentCreatedIndexNamespace") != "billing_payments_by_created_desc"
        or billing.get("paymentCountNamespace") != "billing_payment_counts"
        or billing.get("redeemBatchDocumentNamespace") != "billing_redeem_batches"
        or billing.get("redeemCodeLocatorNamespace") != "billing_redeem_code_locators"
        or billing.get("redeemCodeStateNamespace") != "billing_redeem_code_states"
        or billing.get("redeemCreatedIndexNamespace") != "billing_redeem_batches_by_created_desc"
        or billing.get("redeemBatchCreatedIndexNamespace")
        != "billing_redeem_batches_by_label_created_desc"
        or billing.get("redeemCountNamespace") != "billing_redeem_code_counts"
        or billing.get("maximumLedgerEntriesPerUser") != 100_000
        or billing.get("maximumPayments") != 100_000
        or billing.get("maximumPaymentDocumentBytes") != 8 * 1024 * 1024
        or billing.get("maximumRedeemCodes") != 100_000
        or billing.get("maximumRedeemCodesPerMint") != 10_000
        or billing.get("maximumRedeemBatchDocumentBytes") != 8 * 1024 * 1024
        or billing.get("redeemBatchShards") != 4096
        or billing.get("maximumRedeemBatchShardBytes") != 64 * 1024 * 1024
        or billing.get("redeemLocatorShards") != 4096
        or billing.get("maximumRedeemLocatorShardBytes") != 64 * 1024 * 1024
        or billing.get("maximumRedeemStateShardBytes") != 64 * 1024 * 1024
        or billing.get("maximumListRows") != 1000
        or billing.get("maximumListScanRows") != 10_000
        or billing.get("maximumResponseBytes") != 8 * 1024 * 1024
        or billing.get("maximumNoteCharacters") != 4000
    ):
        raise ValueError("Tofu-DB billing ledger must be bounded and exact")
    if (
        not isinstance(tenant_users, dict)
        or tenant_users.get("ownerKey") != "tenant_admin_boundary"
        or tenant_users.get("physicalFamily") != "entity+blob"
        or tenant_users.get("profileNamespace") != "tenant_user_profiles"
        or tenant_users.get("stateNamespace") != "tenant_user_states"
        or tenant_users.get("emailIndexNamespace") != "tenant_users_by_email_digest"
        or tenant_users.get("createdIndexNamespace") != "tenant_users_by_created_desc"
        or tenant_users.get("statusIndexNamespace")
        != "tenant_users_by_status_created_desc"
        or tenant_users.get("ownerSequenceNamespace") != "tenant_user_owner_sequence"
        or tenant_users.get("maximumDocumentBytes") != 8 * 1024 * 1024
        or tenant_users.get("maximumListRows") != 1000
        or tenant_users.get("maximumListScanRows") != 10_000
        or tenant_users.get("maximumResponseBytes") != 8 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB tenant users must be bounded and tenant-global")
    if (
        not isinstance(credentials, dict)
        or credentials.get("ownerKey") != "owner_user_id+tenant_label"
        or credentials.get("physicalFamily") != "entity+blob"
        or credentials.get("coreNamespace") != "credential_cores"
        or credentials.get("settingsNamespace") != "credential_settings"
        or credentials.get("stateNamespace") != "credential_states"
        or credentials.get("secretIndexNamespace") != "credentials_by_secret_hash"
        or credentials.get("ownerIndexNamespace")
        != "credentials_by_owner_created_desc"
        or credentials.get("ownerCountNamespace") != "credential_owner_counts"
        or credentials.get("maximumDocumentBytes") != 8 * 1024 * 1024
        or "maximumCredentialsPerOwnerBoundary" in credentials
        or credentials.get("ownerCountIsIntegrityObservation") is not True
        or credentials.get("listHasNoAggregateResponseQuota") is not True
        or credentials.get("maximumScopes") != 128
        or credentials.get("maximumResponseBytes") != 8 * 1024 * 1024
        or credentials.get("maximumTimestampSeconds") != 32_503_680_000
    ):
        raise ValueError("Tofu-DB credentials must be bounded and owner scoped")
    if (
        not isinstance(browser_observations, dict)
        or browser_observations.get("ownerKey") != "owner_user_id"
        or browser_observations.get("physicalFamily") != "entity+blob"
        or browser_observations.get("physicalNamespace")
        != "browser_site_observation_documents"
        or browser_observations.get("lruNamespace")
        != "browser_site_observations_by_last_observed"
        or browser_observations.get("maximumDocumentsPerOwner") != 200
        or browser_observations.get("maximumObservationPayloadBytes") != 4096
        or browser_observations.get("maximumStoredDocumentBytes") != 8192
        or browser_observations.get("maximumExpiredPrunedPerCommand") != 64
    ):
        raise ValueError("Tofu-DB browser observations must be bounded and owner scoped")
    if not isinstance(desktop, dict) or desktop.get("ownerKey") != "owner_user_id":
        raise ValueError("Tofu-DB desktop table must be owner scoped")
    if (
        not isinstance(records, dict)
        or records.get("ownerKey") != "authenticated_owner_user_id"
        or records.get("taskResultPhysicalNamespace") != "task_result_documents"
        or records.get("taskResultHeaderNamespace") != "task_result_headers"
        or records.get("taskResultReplayNamespace") != "task_result_replay_projections"
        or records.get("taskResultSummaryNamespace") != "task_results_by_owner"
        or records.get("taskResultLiveNamespace") != "task_results_live_by_owner"
        or records.get("taskResultCostExperimentNamespace")
        != "task_results_by_cost_experiment"
        or records.get("taskResultMaximumBytes") != 64 * 1024 * 1024
        or records.get("taskResultMaximumSummaryRows") != 1000
        or records.get("taskResultMaximumSummaryScanRows") != 10_000
        or records.get("taskResultSummaryPageRows") != 1000
        or records.get("taskResultMaximumRecoveryRows") != 500
        or records.get("taskResultMaximumRecoveryScanRows") != 100_000
        or records.get("taskResultMaximumCostExperimentRows") != 10_000
        or records.get("taskResultMaximumCostExperimentScanRows") != 256
        or records.get("taskResultCheckpointGuardContract")
        != "tofu.task-results.checkpoint.guard/v1"
        or records.get("taskResultCacheSettingsContract")
        != "tofu.task-results.checkpoint.cache-settings/v1"
        or records.get("taskResultCacheFactMaximum") != 2_147_483_647
    ):
        raise ValueError("Tofu-DB records table must be owner scoped")
    if (
        not isinstance(project_brain, dict)
        or project_brain.get("ownerKey") != "authenticated_owner_user_id"
        or project_brain.get("physicalFamily") != "entity+stream+blob"
        or project_brain.get("physicalNamespace") != "project_brain_documents"
        or project_brain.get("checkpointNamespace") != "project_brain_checkpoints"
        or project_brain.get("eventIndexNamespace") != "project_brain_event_positions"
        or project_brain.get("updatedIndexNamespace") != "project_brain_by_updated_desc"
        or project_brain.get("streamDomain") != "project_brain"
        or project_brain.get("maximumProjectKeyCharacters") != 4096
        or project_brain.get("maximumDocumentBytes") != 8 * 1024 * 1024
        or project_brain.get("maximumActiveWorkItems") != 100
        or project_brain.get("maximumWorkHistoryItems") != 100
        or project_brain.get("maximumChangedPathsPerWork") != 200
        or project_brain.get("maximumArtifactsPerWork") != 100
        or project_brain.get("maximumNarratives") != 500
        or project_brain.get("maximumNarrativeTextBytes") != 720
        or project_brain.get("maximumWatchItems") != 100
        or project_brain.get("maximumCheckerVersions") != 128
        or project_brain.get("maximumCharterDecisions") != 256
        or project_brain.get("maximumCursors") != 1000
        or project_brain.get("maximumEventBytes") != 8 * 1024 * 1024 - 4096
        or project_brain.get("eventChunkBytes") != 256 * 1024 - 60
        or project_brain.get("eventCheckpointThreshold") != 1200
        or project_brain.get("eventCheckpointTail") != 600
    ):
        raise ValueError("Tofu-DB Project Brain must be bounded and owner scoped")
    if (
        not isinstance(daily_costs, dict)
        or daily_costs.get("ownerKey") != "authenticated_owner_user_id"
        or daily_costs.get("physicalFamily") != "entity+blob"
        or daily_costs.get("physicalNamespace") != "daily_cost_documents"
        or daily_costs.get("countNamespace") != "daily_cost_counts"
        or daily_costs.get("maximumDocumentBytes") != 8 * 1024 * 1024
        or daily_costs.get("maximumMonthRows") != 100
        or daily_costs.get("maximumPersistedDateProbes") != 366
        or daily_costs.get("maximumResponseBytes") != 8 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB daily costs must be bounded and owner scoped")
    if (
        not isinstance(recent_projects, dict)
        or recent_projects.get("ownerKey") != "authenticated_owner_user_id"
        or recent_projects.get("physicalFamily") != "entity+blob"
        or recent_projects.get("physicalNamespace") != "recent_project_documents"
        or recent_projects.get("countNamespace") != "recent_project_counts"
        or recent_projects.get("maximumProjectsPerOwner") != 1000
        or recent_projects.get("maximumTouchBatch") != 32
        or recent_projects.get("maximumPathCharacters") != 4096
    ):
        raise ValueError("Tofu-DB recent projects must be bounded and owner scoped")
    if (
        not isinstance(log_aggregates, dict)
        or log_aggregates.get("ownerKey") != "authenticated_tenant_and_owner"
        or log_aggregates.get("physicalFamily") != "entity+blob"
        or log_aggregates.get("physicalNamespace") != "log_aggregate_documents"
        or log_aggregates.get("sweepIndexNamespace") != "log_aggregates_by_last_seen"
        or log_aggregates.get("sequenceNamespace") != "log_aggregate_sequences"
        or log_aggregates.get("maximumFingerprintCharacters") != 64
        or log_aggregates.get("maximumLevelCharacters") != 32
        or log_aggregates.get("maximumLoggerCharacters") != 256
        or log_aggregates.get("maximumTemplateCharacters") != 200
        or log_aggregates.get("maximumSampleCharacters") != 2000
        or log_aggregates.get("maximumDocumentBytes") != 16384
        or log_aggregates.get("maximumFlushBatch") != 500
        or log_aggregates.get("maximumQueryRows") != 500
        or log_aggregates.get("maximumScanRows") != 4096
        or log_aggregates.get("maximumSweepBatch") != 500
        or log_aggregates.get("maximumRowCount") != 1_000_000_000
    ):
        raise ValueError("Tofu-DB log aggregates must be bounded tenant-global state")
    if (
        not isinstance(paper_library, dict)
        or paper_library.get("ownerKey") != "authenticated_tenant_and_owner"
        or paper_library.get("physicalFamily") != "entity+blob"
        or paper_library.get("coreNamespace") != "paper_library_core_documents"
        or paper_library.get("stateNamespace") != "paper_library_state_documents"
        or paper_library.get("updatedIndexNamespace") != "paper_library_by_updated_desc"
        or paper_library.get("hashIndexNamespace") != "paper_library_by_hash_updated_desc"
        or paper_library.get("arxivIndexNamespace") != "paper_library_by_arxiv_updated_desc"
        or paper_library.get("reportPresenceNamespace") != "paper_report_presence"
        or paper_library.get("countNamespace") != "paper_library_counts"
        or paper_library.get("maximumRowsPerOwner") != 1024
        or paper_library.get("maximumCoreDocumentBytes") != 64 * 1024 * 1024
        or paper_library.get("maximumStateDocumentBytes") != 64 * 1024
        or paper_library.get("maximumResponseBytes") != 64 * 1024 * 1024
        or paper_library.get("maximumFaninPapers") != 40
        or paper_library.get("maximumFaninTextCharacters") != 6000
        or paper_library.get("maximumQaTextCharacters") != 1_000_000
    ):
        raise ValueError("Tofu-DB paper library must be bounded and owner scoped")
    if (
        not isinstance(paper_notes, dict)
        or paper_notes.get("ownerKey") != "authenticated_tenant_and_owner"
        or paper_notes.get("physicalFamily") != "entity+blob"
        or paper_notes.get("physicalNamespace") != "paper_note_documents"
        or paper_notes.get("listIndexNamespace") != "paper_notes_by_paper_lang_created"
        or paper_notes.get("countNamespace") != "paper_note_counts"
        or paper_notes.get("maximumRowsPerOwner") != 4096
        or paper_notes.get("maximumDocumentBytes") != 512 * 1024
        or paper_notes.get("maximumResponseBytes") != 64 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB paper notes must be bounded and owner scoped")
    if (
        not isinstance(plugin_manifests, dict)
        or plugin_manifests.get("ownerKey") != "authenticated_tenant_and_owner"
        or plugin_manifests.get("physicalFamily") != "entity+blob"
        or plugin_manifests.get("physicalNamespace") != "plugin_manifest_documents"
        or plugin_manifests.get("maximumNamespaceCharacters") != 128
        or plugin_manifests.get("maximumManifestBytes") != 4 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB plugin manifests must be bounded and owner scoped")
    if (
        not isinstance(knowledge_documents, dict)
        or knowledge_documents.get("ownerKey") != "authenticated_tenant_and_owner"
        or knowledge_documents.get("physicalFamily") != "entity+blob"
        or knowledge_documents.get("documentNamespace") != "knowledge_document_documents"
        or knowledge_documents.get("chunkNamespace") != "knowledge_chunk_documents"
        or knowledge_documents.get("assetNamespace") != "knowledge_asset_documents"
        or knowledge_documents.get("assetOrdinalIndexNamespace") != "knowledge_asset_ordinals"
        or knowledge_documents.get("assetChunkReverseNamespace") != "knowledge_asset_chunk_reverse"
        or knowledge_documents.get("termNamespace") != "knowledge_terms"
        or knowledge_documents.get("digestIndexNamespace") != "knowledge_documents_by_sha256"
        or knowledge_documents.get("settingsNamespace") != "knowledge_settings"
        or knowledge_documents.get("chunkAssetLinkNamespace") != "knowledge_chunk_asset_links"
        or knowledge_documents.get("enrichmentQueueNamespace") != "knowledge_enrichment_queue"
        or knowledge_documents.get("enrichmentOwnerIndexNamespace") != "knowledge_enrichment_owners"
        or knowledge_documents.get("maximumDocumentIdCharacters") != 128
        or knowledge_documents.get("maximumAssetIdCharacters") != 128
        or knowledge_documents.get("maximumNameCharacters") != 240
        or knowledge_documents.get("maximumStoredNameCharacters") != 512
        or knowledge_documents.get("maximumDocumentKindCharacters") != 32
        or knowledge_documents.get("maximumAssetKindCharacters") != 64
        or knowledge_documents.get("maximumMimeTypeCharacters") != 255
        or knowledge_documents.get("maximumMethodCharacters") != 255
        or knowledge_documents.get("maximumRelationCharacters") != 64
        or knowledge_documents.get("maximumStatusCharacters") != 32
        or knowledge_documents.get("maximumScopeCharacters") != 32
        or knowledge_documents.get("maximumSectionCharacters") != 20_000
        or knowledge_documents.get("maximumLocationCharacters") != 20_000
        or knowledge_documents.get("maximumChunkContentCharacters") != 4_000_000
        or knowledge_documents.get("maximumSearchTextCharacters") != 4_000_000
        or knowledge_documents.get("maximumAssetTextCharacters") != 1_000_000
        or knowledge_documents.get("maximumCaptionCharacters") != 20_000
        or knowledge_documents.get("maximumEnrichmentModelCharacters") != 512
        or knowledge_documents.get("maximumEnrichmentErrorCharacters") != 2_000
        or knowledge_documents.get("maximumMediaMetadataCharacters") != 100_000
        or knowledge_documents.get("maximumAssetMetadataCharacters") != 20_000
        or knowledge_documents.get("maximumJsonArrayCharacters") != 200_000
        or knowledge_documents.get("maximumTermCharacters") != 128
        or knowledge_documents.get("maximumTermsPerChunk") != 4_096
        or knowledge_documents.get("maximumSearchTokens") != 32
        or knowledge_documents.get("maximumPageRows") != 200
        or knowledge_documents.get("maximumCatalogPageSize") != 100
        or knowledge_documents.get("maximumCatalogQueryCharacters") != 200
        or knowledge_documents.get("maximumEnrichmentOwnerRows") != 512
        or knowledge_documents.get("maximumDocumentBytes") != 1024 * 1024
        or knowledge_documents.get("maximumChunkDocumentBytes") != 32 * 1024 * 1024
        or knowledge_documents.get("maximumAssetDocumentBytes") != 16 * 1024 * 1024
        or knowledge_documents.get("maximumSearchPostingsScan") != 262_144
        or knowledge_documents.get("maximumCatalogScanDocuments") != 65_536
        or knowledge_documents.get("maximumClaimScanRows") != 4_096
        or knowledge_documents.get("maximumChunksPerDocument") != 65_536
        or knowledge_documents.get("maximumAssetsPerDocument") != 65_536
        or knowledge_documents.get("maximumOwnerAssetRows") != 262_144
    ):
        raise ValueError("Tofu-DB knowledge documents must be bounded and owner scoped")
    if (
        not isinstance(paper_reports, dict)
        or paper_reports.get("ownerKey") != "authenticated_tenant_and_owner"
        or paper_reports.get("coreNamespace") != "paper_report_core_documents"
        or paper_reports.get("metaNamespace") != "paper_report_meta_documents"
        or paper_reports.get("latestIndexNamespace") != "paper_reports_by_hash_created_desc"
        or paper_reports.get("presenceNamespace") != "paper_report_presence"
        or paper_reports.get("countNamespace") != "paper_report_counts"
        or paper_reports.get("maximumRowsPerOwner") != 4096
        or paper_reports.get("maximumDocumentBytes") != 64 * 1024 * 1024
        or paper_reports.get("maximumMetaBytes") != 8 * 1024 * 1024
        or paper_reports.get("maximumResponseBytes") != 64 * 1024 * 1024
        or paper_reports.get("maximumReopenSiblings") != 8
        or paper_reports.get("maximumExcerptHashes") != 40
        or paper_reports.get("maximumExcerptTextCharacters") != 6000
    ):
        raise ValueError("Tofu-DB paper reports must be bounded and owner scoped")
    if (
        not isinstance(paper_translations, dict)
        or paper_translations.get("ownerKey") != "authenticated_tenant_and_owner"
        or paper_translations.get("documentNamespace") != "paper_translation_documents"
        or paper_translations.get("countNamespace") != "paper_translation_counts"
        or paper_translations.get("maximumRowsPerOwner") != 4096
        or paper_translations.get("maximumDocumentBytes") != 16 * 1024 * 1024
        or paper_translations.get("maximumResponseBytes") != 16 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB paper translations must be bounded and owner scoped")
    if (
        not isinstance(paper_podcasts, dict)
        or paper_podcasts.get("ownerKey") != "authenticated_tenant_and_owner"
        or paper_podcasts.get("physicalFamily") != "entity+blob"
        or paper_podcasts.get("coreNamespace") != "paper_podcast_core_documents"
        or paper_podcasts.get("stateNamespace") != "paper_podcast_state_documents"
        or paper_podcasts.get("countNamespace") != "paper_podcast_counts"
        or paper_podcasts.get("activeIndexNamespace") != "paper_podcast_generating_by_tenant"
        or paper_podcasts.get("activeCountNamespace") != "paper_podcast_generating_counts"
        or paper_podcasts.get("maximumRowsPerOwner") != 4096
        or paper_podcasts.get("maximumActivePerTenant") != 256
        or paper_podcasts.get("maximumScriptBytes") != 8 * 1024 * 1024
        or paper_podcasts.get("maximumMetaBytes") != 4 * 1024 * 1024
        or paper_podcasts.get("maximumCoreDocumentBytes") != 16 * 1024 * 1024
        or paper_podcasts.get("maximumStateDocumentBytes") != 64 * 1024
        or paper_podcasts.get("maximumResponseBytes") != 16 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB paper podcasts must be bounded and owner scoped")
    if (
        not isinstance(raw_archives, dict)
        or raw_archives.get("ownerKey") != "authenticated_tenant_and_owner"
        or raw_archives.get("physicalFamily") != "entity+blob"
        or raw_archives.get("physicalNamespace") != "raw_archive_documents"
        or raw_archives.get("idClaimNamespace") != "raw_archive_id_claims"
        or raw_archives.get("tenantUsageNamespace") != "raw_archive_tenant_usage"
        or raw_archives.get("attemptUsageNamespace") != "raw_archive_attempt_usage"
        or raw_archives.get("ownerCountNamespace") != "raw_archive_owner_counts"
        or raw_archives.get("taskIndexNamespace") != "raw_archives_by_task_created"
        or raw_archives.get("taskRoundIndexNamespace")
        != "raw_archives_by_task_round_created"
        or raw_archives.get("attemptIndexNamespace") != "raw_archives_by_attempt_created"
        or raw_archives.get("conversationIndexNamespace")
        != "raw_archives_by_conversation_created"
        or raw_archives.get("conversationStatsNamespace")
        != "raw_archive_conversation_stats"
        or raw_archives.get("maximumStoredBytes") != 16 * 1024 * 1024
        or raw_archives.get("maximumDocumentBytes") != 24 * 1024 * 1024
        or raw_archives.get("maximumDecodedPartBytes") != 64 * 1024 * 1024
        or raw_archives.get("maximumTenantBudgetBytes") != 4 * 1024 * 1024 * 1024
        or raw_archives.get("maximumReadChunkBytes") != 1024 * 1024
        or raw_archives.get("maximumArchivesPerOwner") != 65_536
        or raw_archives.get("maximumArchivesPerConversation") != 1792
        or raw_archives.get("maximumSummaryBytes") != 64 * 1024
        or raw_archives.get("maximumSummaryKeys") != 32
        or raw_archives.get("maximumListRows") != 64
        or raw_archives.get("freeSpaceWireMaximumBytes") != 1_000_000_000_000_000
    ):
        raise ValueError("Tofu-DB raw archives must be bounded and owner scoped")
    if (
        not isinstance(research_artifacts, dict)
        or research_artifacts.get("ownerKey") != "authenticated_tenant_and_owner"
        or research_artifacts.get("physicalFamily") != "entity+blob"
        or research_artifacts.get("physicalNamespace") != "research_artifact_documents"
        or research_artifacts.get("stateNamespace") != "research_artifact_state"
        or research_artifacts.get("createdIndexNamespace") != "research_artifacts_by_created_desc"
        or research_artifacts.get("countNamespace") != "research_artifact_counts"
        or research_artifacts.get("maximumRowsPerOwner") != 4096
        or research_artifacts.get("maximumDirectionRows") != 1000
        or research_artifacts.get("maximumDirectionScanRows") != 2000
        or research_artifacts.get("maximumDocumentBytes") != 64 * 1024 * 1024
        or research_artifacts.get("maximumResponseBytes") != 64 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB research artifacts must be bounded and owner scoped")
    if (
        not isinstance(research_workspaces, dict)
        or research_workspaces.get("ownerKey") != "authenticated_tenant_and_owner"
        or research_workspaces.get("physicalFamily") != "entity+blob"
        or research_workspaces.get("physicalNamespace") != "research_workspace_documents"
        or research_workspaces.get("maximumDocumentBytes") != 8 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB research workspaces must be bounded and owner scoped")
    if (
        not isinstance(optimizer_proposals, dict)
        or optimizer_proposals.get("ownerKey") != "authenticated_tenant_and_owner"
        or optimizer_proposals.get("physicalFamily") != "entity+blob"
        or optimizer_proposals.get("physicalNamespace") != "optimizer_proposal_documents"
        or optimizer_proposals.get("createdIndexNamespace") != "optimizer_proposals_by_created_desc"
        or optimizer_proposals.get("statusCreatedIndexNamespace") != "optimizer_proposals_by_status_created_desc"
        or optimizer_proposals.get("statusNamespace") != "optimizer_proposal_status"
        or optimizer_proposals.get("countNamespace") != "optimizer_proposal_counts"
        or optimizer_proposals.get("maximumRowsPerOwner") != 4096
        or optimizer_proposals.get("maximumListRows") != 500
        or optimizer_proposals.get("maximumDocumentBytes") != 4 * 1024 * 1024
        or optimizer_proposals.get("maximumResponseBytes") != 8 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB optimizer proposals must be bounded and owner scoped")
    if (
        not isinstance(optimizer_actions, dict)
        or optimizer_actions.get("ownerKey") != "authenticated_tenant_and_owner"
        or optimizer_actions.get("physicalFamily") != "entity+blob"
        or optimizer_actions.get("physicalNamespace") != "optimizer_action_documents"
        or optimizer_actions.get("appliedIndexNamespace") != "optimizer_actions_by_applied_desc"
        or optimizer_actions.get("activeAppliedIndexNamespace") != "optimizer_active_actions_by_applied_desc"
        or optimizer_actions.get("proposalIndexNamespace") != "optimizer_actions_by_proposal_applied_desc"
        or optimizer_actions.get("expiryIndexNamespace") != "optimizer_actions_by_expiry"
        or optimizer_actions.get("countNamespace") != "optimizer_action_counts"
        or optimizer_actions.get("maximumRowsPerOwner") != 2048
        or optimizer_actions.get("maximumListRows") != 500
        or optimizer_actions.get("maximumDocumentBytes") != 4 * 1024 * 1024
        or optimizer_actions.get("maximumResponseBytes") != 8 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB optimizer actions must be bounded and owner scoped")
    if (
        not isinstance(rate_limits, dict)
        or rate_limits.get("ownerKey") != "authenticated_tenant_and_owner"
        or rate_limits.get("physicalFamily") != "entity"
        or rate_limits.get("bucketNamespace") != "rate_limit_buckets"
        or rate_limits.get("eventNamespace") != "rate_limit_events"
        or rate_limits.get("expiryIndexNamespace") != "rate_limit_events_by_expiry"
        or rate_limits.get("countNamespace") != "rate_limit_radix_counts"
        or rate_limits.get("maximumEndpointCharacters") != 256
        or rate_limits.get("maximumClientKeyCharacters") != 512
        or rate_limits.get("maximumEventIdCharacters") != 200
        or rate_limits.get("maximumEventsPerWindow") != 1_000_000
        or rate_limits.get("maximumWindowSeconds") != 7 * 24 * 60 * 60
        or rate_limits.get("maximumPruneRows") != 256
        or rate_limits.get("counterRadix") != 256
        or rate_limits.get("counterDepth") != 8
        or rate_limits.get("maximumWindowCounterReads") != 8 * 255
    ):
        raise ValueError("Tofu-DB rate limits must be bounded and owner scoped")
    if (
        not isinstance(providers, dict)
        or providers.get("ownerKey") != "owner_user_id"
        or providers.get("physicalFamily") != "entity+blob"
        or providers.get("physicalNamespace") != "byo_provider_documents"
        or providers.get("createdIndexNamespace") != "byo_providers_by_created"
        or providers.get("ownerCountNamespace") != "byo_provider_counts"
        or providers.get("tenantGlobalClaimNamespace") != "byo_provider_id_claims"
        or providers.get("countPolicy") != "exact-owner-label-count-without-admission-quota"
        or providers.get("maximumTenantLabelCharacters") != 256
    ):
        raise ValueError("Tofu-DB providers must preserve owner-scoped count and document validation")
    if (
        not isinstance(artifacts, dict)
        or artifacts.get("ownerKey") != "authenticated_owner_user_id"
        or artifacts.get("maximumContentBytes") != 8 * 1024 * 1024
        or artifacts.get("maximumRowsPerConversation") != 1000
        or not isinstance(tool_result_artifacts, dict)
        or tool_result_artifacts.get("ownerKey") != "user_id"
        or tool_result_artifacts.get("maximumContentBytes") != 16 * 1024 * 1024
        or tool_result_artifacts.get("maximumRangeBytes") != 64 * 1024
        or tool_result_artifacts.get("maximumTtlMilliseconds")
        != 7 * 24 * 60 * 60 * 1000
        or tool_result_artifacts.get("maximumPruneRows") != 5000
    ):
        raise ValueError("Tofu-DB artifacts must be bounded and owner scoped")
    if (
        not isinstance(model_routing, dict)
        or model_routing.get("ownerKey") != "owner_user_id"
        or model_routing.get("physicalFamily") != "entity+blob"
        or model_routing.get("authorityNamespace") != "model_routing_authorities"
        or model_routing.get("authorityMetadataNamespace")
        != "model_routing_authority_metadata"
        or model_routing.get("backupNamespace") != "model_routing_backups"
        or model_routing.get("migrationReceiptNamespace")
        != "model_routing_migration_receipts"
        or model_routing.get("secretNamespace") != "model_routing_secrets"
        or model_routing.get("secretCountNamespace") != "model_routing_secret_counts"
        or model_routing.get("secretReferenceIndexNamespace")
        != "model_routing_secrets_by_reference"
        or model_routing.get("secretUpdatedIndexNamespace")
        != "model_routing_secrets_by_updated"
        or model_routing.get("maximumDocumentBytes") != 8 * 1024 * 1024
        or model_routing.get("maximumSecretReferenceCharacters") != 256
        or model_routing.get("maximumCiphertextCharacters") != 32768
        or model_routing.get("maximumKeyHintCharacters") != 64
        or model_routing.get("maximumPrunedPerCommand") != 256
        or model_routing.get("maximumTimestampSeconds") != 32_503_680_000
    ):
        raise ValueError("Tofu-DB model routing must validate records and preserve owner scope")
    if (
        not isinstance(worker_jobs, dict)
        or worker_jobs.get("ownerKey") != "user_id"
        or worker_jobs.get("physicalFamily") != "entity+blob"
        or worker_jobs.get("physicalNamespace") != "worker_job_documents"
        or worker_jobs.get("idempotencyNamespace") != "worker_job_idempotency"
        or worker_jobs.get("queuedIndexNamespace") != "worker_jobs_queued"
        or worker_jobs.get("queuedSummaryNamespace") != "worker_jobs_queued_summary"
        or worker_jobs.get("leaseIndexNamespace") != "worker_jobs_by_lease"
        or worker_jobs.get("maximumPayloadBytes") != 1024 * 1024
        or worker_jobs.get("maximumErrorBytes") != 64 * 1024
        or worker_jobs.get("maximumClaimTaskKinds") != 32
        or worker_jobs.get("maximumPriority") != 1000
        or worker_jobs.get("maximumClockMilliseconds") != 9_223_372_036_854_775_000
        or worker_jobs.get("maximumFutureClockSkewMilliseconds") != 86_400_000
    ):
        raise ValueError("Tofu-DB worker jobs must be bounded, indexed, and fenced")
    if not isinstance(events, dict) or events.get("physicalFamily") != "entity+stream":
        raise ValueError("Tofu-DB events table must bind its index and stream")
    if (
        not isinstance(archives, dict)
        or archives.get("ownerKey") != "user_id"
        or archives.get("physicalFamily") != "entity+blob"
        or archives.get("physicalNamespace") != "compaction_archive_metadata"
        or archives.get("messagesNamespace") != "compaction_archive_messages"
        or archives.get("postMessagesNamespace")
        != "compaction_archive_post_messages"
        or archives.get("summaryNamespace") != "compaction_archive_summaries"
        or archives.get("receiptNamespace") != "compaction_archive_receipts"
        or archives.get("conversationIndexNamespace")
        != "compaction_archives_by_conversation"
        or archives.get("conversationCountNamespace") != "compaction_archive_counts"
        or archives.get("tenantGlobalClaimNamespace")
        != "compaction_archive_id_claims"
        or archives.get("maximumArchivesPerConversation") != 1000
        or archives.get("maximumDocumentBytes") != 67108864
    ):
        raise ValueError("Tofu-DB archives table must be bounded and owner scoped")
    if (
        not isinstance(conversations, dict)
        or conversations.get("ownerKey") != "user_id"
        or conversations.get("tenantGlobalClaimNamespace") != "conversation_id_claims"
        or conversations.get("updatedIndexNamespace") != "conversation_by_updated"
        or conversations.get("activityCandidateIndexNamespace")
        != "conversation_activity_candidates_v2"
        or conversations.get("activityCandidateStateNamespace")
        != "conversation_activity_candidate_state_v2"
        or conversations.get("maximumActivityCandidateBackfillRowsPerTransaction") != 256
        or conversations.get("maximumActivityCandidateBackfillSourceBytesPerTransaction")
        != 16 * 1024 * 1024
        or conversations.get("executionEpochNamespace") != "conversation_execution_epochs"
        or conversations.get("trashMetadataNamespace") != "conversation_trash_metadata"
        or conversations.get("trashAgeIndexNamespace") != "conversation_trash_by_age"
        or conversations.get("trashSettingsOverlayNamespace")
        != "conversation_trash_settings_overlays"
        or conversations.get("recencyDirectoryNamespace")
        != "conversation_recency_directory"
        or conversations.get("recencyDirectoryStateNamespace")
        != "conversation_recency_directory_state"
        or conversations.get("maximumRecencyDirectoryEntries") != 128
        or conversations.get("maximumRecencyDirectoryBackfillRowsPerTransaction") != 64
        or conversations.get("maximumProjectRelinkConversationCandidates") != 10_000
    ):
        raise ValueError("Tofu-DB conversations table must preserve scoped ownership and ID claims")
    if (
        not isinstance(turns, dict)
        or turns.get("ownerKey") != "user_id"
        or turns.get("physicalFamily") != "entity+blob"
        or turns.get("laneIndexNamespace") != "turns_by_lane_ordinal"
        or turns.get("activityIndexNamespace") != "turn_activity_timestamps"
        or turns.get("maximumActivityTurnRowsPerQuery") != 100_000
        or turns.get("laneCountNamespace") != "turn_lane_counts"
        or turns.get("turnIdClaimNamespace") != "turn_id_claims"
        or turns.get("attemptIdClaimNamespace") != "attempt_id_claims"
        or turns.get("attemptClaimLocatorVersion") != 1
        or turns.get("attemptCommandNamespace") != "generation_attempt_commands"
        or turns.get("attemptDispatchableIndexNamespace")
        != "generation_attempts_dispatchable"
        or turns.get("maximumDispatchableAttemptsPerQuery") != 32
        or turns.get("laneCompactionIndexNamespace")
        != "turns_by_lane_compaction"
        or turns.get("maximumCompactionMetadataRows") != 100_000
        or turns.get("maximumCompactionProjectionUpdates") != 512
        or turns.get("maximumCompactionReparentedTurns") != 512
        or turns.get("maximumCompactionProjectionBytes") != 8 * 1024 * 1024
        or turns.get("maximumCompactionMaterializedBytes") != 64 * 1024 * 1024
        or turns.get("attemptRecoveryIndexNamespace")
        != "generation_attempts_live_by_created"
        or turns.get("laneLiveAttemptNamespace") != "turn_lane_live_attempts"
        or turns.get("maximumRecoveryRowsPerTransaction") != 500
        or turns.get("maximumRecoveryIndexRowsPerOwner") != 10_000
        or turns.get("maximumRecoveryProjectionBytesPerTransaction") != 8 * 1024 * 1024
        or turns.get("attemptTimingTaskIndexNamespace")
        != "generation_attempts_by_timing_task"
        or turns.get("attemptTimingConversationIndexNamespace")
        != "generation_attempts_by_conversation_created_desc"
        or turns.get("maximumTimingTraceRowsPerQuery") != 100
        or turns.get("maximumTimingTraceTaskCandidates") != 100
        or turns.get("maximumTimingTraceClientObservations") != 64
        or turns.get("maximumTimingTracePersistedBytes") != 96 * 1024
        or turns.get("maximumTimingTraceCounter") != 2_147_483_647
        or turns.get("projectionHeadNamespace") != "turn_projection_heads"
        or turns.get("maximumTurnProjectionInlineLiveBytes") != 64 * 1024
        or turns.get("maximumTurnProjectionHeadPatches") != 64
        or turns.get("maximumTurnProjectionTextPageBytes") != 16 * 1024
        or turns.get("maximumTurnProjectionTextAppendBytes") != 64 * 1024
        or turns.get("maximumTurnProjectionPatchBytes") != 1024 * 1024
        or turns.get("maximumTurnProjectionWirePatchBytes") != 8 * 1024 * 1024
        or turns.get("maximumTurnProjectionPatchDepth") != 128
        or turns.get("maximumTurnProjectionPatchOperations") != 65_536
        or turns.get("maximumLegacyTurnImages") != 20
        or turns.get("maximumLegacyTurnImageBytes") != 8 * 1024 * 1024
        or turns.get("attemptTurnCountNamespace") != "generation_attempt_turn_counts"
        or turns.get("attemptTurnDirectoryNamespace") != "generation_attempts_by_turn"
        or turns.get("maximumAttemptsPerTurn") != 64
        or turns.get("maximumAttemptTurnDirectoryBytes") != 65_536
        or turns.get("maximumAttemptTombstoneBytes") != 16_384
        or turns.get("attemptEventHeadNamespace") != "attempt_event_heads"
        or turns.get("attemptEventNamespace") != "attempt_events"
        or turns.get("maximumAttemptEventDocumentBytes") != 32 * 1024 * 1024
        or turns.get("maximumAttemptEventPageBytes") != 64 * 1024 * 1024 - 4096
        or turns.get("attemptEventRetentionIndexNamespace")
        != "attempt_events_by_settled"
        or turns.get("syncAgeIndexNamespace")
        != "conversation_sync_events_by_created"
        or turns.get("attemptEventSyncReferenceNamespace")
        != "attempt_event_sync_references"
        or turns.get("maximumSyncPruneRowsPerTransaction") != 1_000
        or turns.get("maximumSyncPruneMaterializedBytes") != 64 * 1024 * 1024
        or turns.get("maximumAttemptEventPruneAttemptsPerTransaction") != 64
        or turns.get("maximumAttemptEventPruneRowsPerTransaction") != 200_000
        or turns.get("maximumAttemptEventPruneMaterializedBytes") != 64 * 1024 * 1024
        or turns.get("updatedIndexNamespace") != "turns_by_updated"
        or turns.get("tombstoneNamespace") != "turn_tombstones"
        or turns.get("tombstoneAgeIndexNamespace") != "turn_tombstones_by_age"
        or turns.get("supersededAttemptIndexNamespace")
        != "generation_attempts_by_superseded"
        or turns.get("maximumCleanupAttemptsPerTransaction") != 64
        or turns.get("maximumCleanupIndexScanRowsPerTransaction") != 256
    ):
        raise ValueError(
            "Tofu-DB turns table must preserve owner scope, global identity claims, "
            "and lane ordering"
        )
    return document


def _render() -> str:
    document = _load()
    bounds = document["bounds"]
    physical_bounds = document["physicalBounds"]
    persistent_snapshots = document["persistentSnapshots"]
    maintenance_scheduler = document["maintenanceScheduler"]
    page_packing = document["entityPagePacking"]
    wal_rotation = document["activeWalRotation"]
    search_projection = document["searchProjection"]
    filesystem_certification = document["filesystemCertification"]
    certification_matrix = filesystem_certification["matrix"]
    resource_gate = filesystem_certification["resourceGate"]
    block_payload_cache = document["blockPayloadReadCache"]
    payload_read_cache = document["payloadSegmentReadCache"]
    authority_gc = document["authorityGarbageCollection"]
    tables = {table["name"]: table for table in document["logicalTables"]}
    browser_observations = tables["browser_site_observations"]
    table = tables["desktop_egress_preferences"]
    records = tables["storage_records"]
    rate_limits = tables["rate_limit_events"]
    daily_costs = tables["daily_cost_cache"]
    orchestration_definitions = tables["orchestration_definitions"]
    orchestration_runs = tables["orchestration_runs"]
    swarm = tables["swarm_sessions"]
    integration = tables["integration_workspaces"]
    project_brain = tables["project_brain_projects"]
    scheduler = tables["scheduled_tasks"]
    queue = tables["conversation_queue"]
    timers = tables["timers"]
    recent_projects = tables["recent_projects"]
    providers = tables["byo_providers"]
    artifacts = tables["chat_artifacts"]
    tool_result_artifacts = tables["tool_result_artifacts"]
    log_aggregates = tables["log_aggregates"]
    optimizer_actions = tables["optimizer_actions"]
    optimizer_proposals = tables["optimizer_proposals"]
    paper_library = tables["paper_library"]
    paper_notes = tables["paper_notes"]
    plugin_manifests = tables["plugin_manifests"]
    knowledge_documents = tables["knowledge_documents"]
    paper_reports = tables["paper_reports"]
    paper_translations = tables["paper_translations"]
    paper_podcasts = tables["paper_podcasts"]
    raw_archives = tables["raw_archives"]
    research_artifacts = tables["research_artifacts"]
    research_workspaces = tables["research_workspaces"]
    model_routing = tables["model_routing"]
    worker_jobs = tables["storage_worker_jobs"]
    conversations = tables["conversations"]
    turns = tables["conversation_turns"]
    archives = tables["compaction_archives"]
    tenant_users = tables["tenant_users"]
    credentials = tables["auth_credentials"]
    billing = tables["billing_wallets_and_ledger"]
    agent_field = next(
        field for field in table["fields"] if field["name"] == "agent_id"
    )
    lines = [
        "//! @generated by scripts/gen_tofudb_ir_contract.py; do not edit.",
        "",
        *[f"pub const {name}: usize = {document['commandReceipts'][key]};"
          for name, key in RECEIPT_NAMES.items()],
        f"pub const SCHEMA_IR_VERSION: u32 = {document['schemaIrVersion']};",
        f"pub const TRANSACTION_IR_VERSION: u32 = {document['transactionIrVersion']};",
        "pub const EVENT_FORMAT_VERSION: u32 = "
        f"{document['eventFormat']['version']};",
        "pub const MIN_SEARCH_PROJECTION_BYTES: u64 = "
        f"{search_projection['minimumBytesPerOwner']};",
        "pub const MAX_SEARCH_PROJECTION_BYTES: u64 = "
        f"{search_projection['maximumBytesPerOwner']};",
        "pub const SEARCH_PROJECTION_VOLUME_FREE_PERCENT: u64 = "
        f"{search_projection['observedVolumeFreePercent']};",
        f"pub const MAX_TRANSACTION_IR_STEPS: usize = {bounds['maximumSteps']};",
        f"pub const MAX_TRANSACTION_IR_SLOTS: usize = {bounds['maximumSlots']};",
        f"pub const MAX_TRANSACTION_IR_LITERAL_BYTES: usize = {bounds['maximumLiteralBytes']};",
        "pub const MAX_TASK_RESULT_DOCUMENT_BYTES: usize = "
        f"{records['taskResultMaximumBytes']};",
        "pub const TASK_RESULT_DOCUMENT_NAMESPACE: &str = "
        f'"{records["taskResultPhysicalNamespace"]}";',
        "pub const TASK_RESULT_HEADER_NAMESPACE: &str = "
        f'"{records["taskResultHeaderNamespace"]}";',
        "pub const TASK_RESULT_REPLAY_NAMESPACE: &str = "
        f'"{records["taskResultReplayNamespace"]}";',
        "pub const TASK_RESULT_SUMMARY_NAMESPACE: &str = "
        f'"{records["taskResultSummaryNamespace"]}";',
        "pub const TASK_RESULT_LIVE_NAMESPACE: &str = "
        f'"{records["taskResultLiveNamespace"]}";',
        "pub const TASK_RESULT_COST_EXPERIMENT_NAMESPACE: &str = "
        f'"{records["taskResultCostExperimentNamespace"]}";',
        "pub const MAX_TASK_RESULT_SUMMARY_ROWS: usize = "
        f"{records['taskResultMaximumSummaryRows']};",
        "pub const MAX_TASK_RESULT_SUMMARY_SCAN_ROWS: usize = "
        f"{records['taskResultMaximumSummaryScanRows']};",
        "pub const TASK_RESULT_SUMMARY_PAGE_ROWS: usize = "
        f"{records['taskResultSummaryPageRows']};",
        "pub const MAX_TASK_RESULT_RECOVERY_ROWS: usize = "
        f"{records['taskResultMaximumRecoveryRows']};",
        "pub const MAX_TASK_RESULT_RECOVERY_SCAN_ROWS: usize = "
        f"{records['taskResultMaximumRecoveryScanRows']};",
        "pub const MAX_TASK_RESULT_COST_EXPERIMENT_ROWS: usize = "
        f"{records['taskResultMaximumCostExperimentRows']};",
        "pub const MAX_TASK_RESULT_COST_EXPERIMENT_SCAN_ROWS: usize = "
        f"{records['taskResultMaximumCostExperimentScanRows']};",
        "pub const TASK_RESULT_CHECKPOINT_GUARD_CONTRACT: &str = "
        f'"{records["taskResultCheckpointGuardContract"]}";',
        "pub const TASK_RESULT_CACHE_SETTINGS_CONTRACT: &str =",
        f'    "{records["taskResultCacheSettingsContract"]}";',
        "pub const MAX_TASK_RESULT_CACHE_FACT: u64 = "
        f"{records['taskResultCacheFactMaximum']};",
        "pub const ACTIVE_LOG_MAX_BYTES: u64 = "
        f"{wal_rotation['maximumBytes']};",
        "pub const ACTIVE_LOG_ROTATE_BYTES: u64 = "
        f"{wal_rotation['softThresholdBytes']};",
        "pub const MAX_CHECKPOINT_MATERIALIZE_BATCH_BYTES: u64 = "
        f"{wal_rotation['foregroundMaterializeTargetBytes']};",
        "pub const HISTORY_PREPARATION_TARGET_BYTES: usize = "
        f"{wal_rotation['historyPreparationTargetBytes']};",
        "pub const MAX_HISTORY_PREPARATION_BYTES: usize = "
        f"{wal_rotation['maximumHistoryPreparationBytes']};",
        "pub const BACKGROUND_PACK_WAIT_ROUND_MILLISECONDS: u64 = "
        f"{wal_rotation['backgroundWaitRoundMilliseconds']};",
        "pub const BACKGROUND_PACK_DEFAULT_WAIT_ROUNDS: u32 = "
        f"{wal_rotation['maximumBackgroundWaitRounds']};",
        "pub const ENTITY_PAGE_TARGET_BYTES: usize = "
        f"{page_packing['targetBytes']};",
        "pub const MAX_ENTITY_PAGE_BYTES: usize = "
        f"{page_packing['maximumBytes']};",
        "pub const MIN_ENTITY_INTERNAL_SPLIT_CHILDREN: usize = "
        f"{page_packing['minimumChildrenPerSplitPage']};",
        "pub const MAX_ENTITY_KEY_BYTES: usize = "
        f"{physical_bounds['maximumEntityKeyBytes']};",
        "pub const MAX_ENTITY_INLINE_VALUE_BYTES: usize = "
        f"{physical_bounds['maximumEntityInlineValueBytes']};",
        "pub const MAX_ENTITY_RANGE_ROWS: usize = "
        f"{physical_bounds['maximumEntityRangeRows']};",
        "pub const MAX_ENTITY_TRANSACTION_WRITES: usize = "
        f"{physical_bounds['maximumEntityTransactionWrites']};",
        "pub const MAX_ENTITY_REACHABILITY_PAGES: usize = "
        f"{physical_bounds['maximumEntityReachabilityPages']};",
        "pub const MAX_ENTITY_REACHABILITY_FRONTIER: usize = "
        f"{physical_bounds['maximumEntityReachabilityFrontier']};",
        "pub const MAX_ENTITY_RETIRED_RANGES: usize = "
        f"{physical_bounds['maximumEntityRetiredRanges']};",
        "pub const MAX_ENTITY_ROOT_RANGE_MOUNTS: usize = "
        f"{physical_bounds['maximumEntityRootRangeMounts']};",
        "pub const MAX_ENTITY_MOUNT_CONSOLIDATION_ROWS: usize = "
        f"{physical_bounds['maximumEntityMountConsolidationRows']};",
        "pub const MAX_ENTITY_MOUNT_CONSOLIDATION_BYTES: usize = "
        f"{physical_bounds['maximumEntityMountConsolidationBytes']};",
        "pub const MAX_STREAM_EVENT_BYTES: usize = "
        f"{physical_bounds['maximumStreamEventBytes']};",
        "pub const MAX_STREAM_APPEND_EVENTS: usize = "
        f"{physical_bounds['maximumStreamAppendEvents']};",
        "pub const MAX_STREAM_APPEND_BYTES: usize = "
        f"{physical_bounds['maximumStreamAppendBytes']};",
        "pub const MAX_STREAM_READ_EVENTS: usize = "
        f"{physical_bounds['maximumStreamReadEvents']};",
        "pub const MAX_STREAM_READ_BYTES: usize = "
        f"{physical_bounds['maximumStreamReadBytes']};",
        "pub const MAX_TRANSACTION_STREAM_APPENDS: usize = "
        f"{physical_bounds['maximumTransactionStreamAppends']};",
        "pub const PERSISTENT_ENTITY_ROOT_PIN_NAMESPACE: &str = "
        f'"{persistent_snapshots["rootPinNamespace"]}";',
        "pub const PERSISTENT_ENTITY_ROOT_PIN_CATALOG_NAMESPACE: &str = "
        f'"{persistent_snapshots["catalogNamespace"]}";',
        "pub const MAX_PERSISTENT_ENTITY_ROOT_PINS: usize = "
        f"{persistent_snapshots['maximumPinsPerAuthority']};",
        "pub const MAX_LEGACY_PERSISTENT_ENTITY_ROOT_PINS: usize = "
        f"{persistent_snapshots['maximumLegacyPinsWithoutCatalog']};",
        "pub const MAX_PERSISTENT_ENTITY_ROOT_PIN_ID_BYTES: usize = "
        f"{persistent_snapshots['maximumPinIdBytes']};",
        "pub const MAX_PERSISTENT_ENTITY_CAPSULE_RANGES: usize = "
        f"{persistent_snapshots['maximumRangesPerCapsule']};",
        "pub const MAX_MAINTENANCE_WORKERS: usize = "
        f"{maintenance_scheduler['maximumWorkers']};",
        "pub const MAX_MAINTENANCE_SCOPES: usize = "
        f"{maintenance_scheduler['maximumScopes']};",
        "pub const MAINTENANCE_WORKER_STACK_BYTES: usize = "
        f"{maintenance_scheduler['workerStackBytes']};",
        "pub const OBSERVED_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS: u64 = "
        f"{maintenance_scheduler['observedIdleIntervalMilliseconds']};",
        "pub const LEAN_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS: u64 = "
        f"{maintenance_scheduler['leanIdleIntervalMilliseconds']};",
        "pub const MAX_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS: u64 = "
        f"{maintenance_scheduler['maximumIdleIntervalMilliseconds']};",
        "pub const OBSERVED_HISTORY_RETAINED_SEGMENTS: usize = "
        f"{maintenance_scheduler['observedHistoryRetainedSegments']};",
        "pub const LEAN_HISTORY_RETAINED_SEGMENTS: usize = "
        f"{maintenance_scheduler['leanHistoryRetainedSegments']};",
        "pub const MAX_HISTORY_RETAINED_SEGMENTS: usize = "
        f"{maintenance_scheduler['maximumHistoryRetainedSegments']};",
        "pub const TOOL_RESULT_PRUNE_ROWS_PER_MAINTENANCE_ROUND: usize = "
        f"{maintenance_scheduler['toolResultPruneRowsPerRound']};",
        "pub const SEARCH_PROJECTION_BACKFILL_PAGE_ROWS: usize = "
        f"{maintenance_scheduler['searchProjectionBackfillPageRows']};",
        "pub const SEARCH_PROJECTION_MAXIMUM_DIRTY_CONVERSATIONS: usize = "
        f"{maintenance_scheduler['searchProjectionMaximumDirtyConversations']};",
        "pub const SEARCH_PROJECTION_MAXIMUM_SOURCE_PAGES: usize = "
        f"{maintenance_scheduler['searchProjectionMaximumSourcePagesPerConversation']};",
        "pub const FILESYSTEM_CERTIFICATION_PAYLOAD_BLOCK_BYTES: usize = "
        f"{filesystem_certification['payloadBlockBytes']};",
        "pub const FILESYSTEM_CERTIFICATION_MAXIMUM_RETAINED_ENTRIES: usize = "
        f"{filesystem_certification['maximumRetainedEntries']};",
        "pub const FILESYSTEM_CERTIFICATION_MATRIX_FORMAT: &str =\n    "
        f"{json.dumps(certification_matrix['format'])};",
        "pub const FILESYSTEM_CERTIFICATION_MATRIX_COMMAND: &str = "
        f"{json.dumps(certification_matrix['operatorCommand'])};",
        "pub const FILESYSTEM_CERTIFICATION_MATRIX_MOUNT_ENV: &str = "
        f"{json.dumps(certification_matrix['mountDirectoryEnvironment'])};",
        "pub const FILESYSTEM_CERTIFICATION_MATRIX_MAX_TRACE_OPERATIONS: usize = "
        f"{certification_matrix['maximumTraceOperations']};",
        "pub const FILESYSTEM_CERTIFICATION_MATRIX_MAX_DETAIL_CHARACTERS: usize = "
        f"{certification_matrix['maximumDetailCharacters']};",
        "pub const RESOURCE_GATE_FORMAT: &str = "
        f"{json.dumps(resource_gate['format'])};",
        "pub const RESOURCE_GATE_COMMAND: &str = "
        f"{json.dumps(resource_gate['operatorCommand'])};",
        "pub const RESOURCE_GATE_COMMIT_INLINE_OPS: usize = "
        f"{resource_gate['inlineCommits']};",
        "pub const RESOURCE_GATE_COMMIT_BLOCK_OPS: usize = "
        f"{resource_gate['blockCommits']};",
        "pub const RESOURCE_GATE_BLOCK_BYTES: usize = "
        f"{resource_gate['blockBytes']};",
        "pub const RESOURCE_GATE_MAX_INLINE_BYTES: usize = "
        f"{resource_gate['maximumInlineBytes']};",
        "pub const RESOURCE_GATE_REFERENCE_VOLUME_BYTES: u64 = "
        f"{resource_gate['referenceVolumeBytes']};",
        "pub const RESOURCE_GATE_REFERENCE_MEMORY_BYTES: u64 = "
        f"{resource_gate['referenceMemoryBytes']};",
        "pub const RESOURCE_GATE_REFERENCE_PEAK_RSS_BYTES: u64 = "
        f"{resource_gate['referencePeakRssBytes']};",
        "pub const RESOURCE_GATE_REFERENCE_RETAINED_BYTES: u64 = "
        f"{resource_gate['referenceRetainedBytes']};",
        "pub const RESOURCE_GATE_REFERENCE_COMMIT_MEAN_MICROS: u64 = "
        f"{resource_gate['referenceCommitMeanMicros']};",
        "pub const RESOURCE_GATE_REFERENCE_TOTAL_MICROS: u64 = "
        f"{resource_gate['referenceTotalMicros']};",
        "pub const MAX_BLOCK_PAYLOAD_CACHE_ENTRIES: usize = "
        f"{block_payload_cache['maximumResidentEntries']};",
        "pub const MAX_BLOCK_PAYLOAD_CACHE_RECENCY_RECORDS: usize = "
        f"{block_payload_cache['maximumRecencyRecords']};",
        "pub const MAX_RECOVERY_PACKED_LOCATIONS: usize = "
        f"{wal_rotation['maximumRecoveryPackedLocations']};",
        "pub const PAYLOAD_SEGMENT_CACHE_BYTES: usize = "
        f"{payload_read_cache['maximumResidentIndexAndFilterBytes']};",
        "pub const PAYLOAD_SEGMENT_MEMBERSHIP_BUDGET_DIVISOR: usize = "
        f"{payload_read_cache['independentMembershipBudgetDivisor']};",
        "pub const MAX_PAYLOAD_SEGMENT_CACHE_SEGMENTS: usize = "
        f"{payload_read_cache['maximumResidentSegments']};",
        "pub const MAX_PAYLOAD_SEGMENT_CACHE_RECENCY_RECORDS: usize = "
        f"{payload_read_cache['maximumRecencyRecords']};",
        "pub const PAYLOAD_SEGMENT_FILTER_BITS_PER_BLOCK: usize = "
        f"{payload_read_cache['membershipFilterBitsPerBlock']};",
        "pub const MIN_PAYLOAD_SEGMENT_FILTER_BITS: usize = "
        f"{payload_read_cache['membershipFilterMinimumBits']};",
        "pub const PAYLOAD_SEGMENT_FILTER_HASH_ROUNDS: usize = "
        f"{payload_read_cache['membershipFilterHashRounds']};",
        "pub const MAX_PAYLOAD_SEGMENT_FILTER_BYTES: usize = "
        f"{payload_read_cache['maximumFilterBytesPerSegment']};",
        "pub const MAX_AUTHORITY_GC_SESSION_CALLS: u32 = "
        f"{authority_gc['maximumSessionCalls']};",
        "pub const MAX_AUTHORITY_GC_OPERATOR_MILLISECONDS: u64 = "
        f"{authority_gc['maximumOperatorMilliseconds']};",
        "pub const DEFAULT_AUTHORITY_GC_OPERATOR_MILLISECONDS: u64 = "
        f"{authority_gc['defaultOperatorMilliseconds']};",
        "pub const MAX_AUTHORITY_GC_VICTIM_BYTES: u64 = "
        f"{authority_gc['maximumVictimBytesPerRound']};",
        "pub const MAX_AUTHORITY_GC_TEMPORARY_BYTES: u64 = "
        f"{authority_gc['maximumTemporaryBytes']};",
        "pub const AUTHORITY_GC_TEMPORARY_FREE_SPACE_PERCENT: u64 = "
        f"{authority_gc['temporaryFreeSpacePercent']};",
        "pub const LEAN_AUTHORITY_GC_TEMPORARY_BYTES: u64 = "
        f"{authority_gc['leanTemporaryBytes']};",
        "pub const MAX_AUTHORITY_GC_BLOCKS_PER_ROUND: usize = "
        f"{authority_gc['maximumBlocksPerRound']};",
        "pub const MAX_AUTHORITY_GC_PHYSICAL_BLOCKS: usize = "
        f"{authority_gc['maximumPhysicalBlocks']};",
        "pub const AUTHORITY_GC_RPC_PHYSICAL_BLOCKS_PER_REQUESTED_MILLISECOND: usize = "
        f"{authority_gc['rpcMaximumPhysicalBlocksPerRequestedMillisecond']};",
        "pub const MAX_AUTHORITY_GC_TEMPORARY_BLOCK_FILES_REMOVED: usize = "
        f"{authority_gc['maximumTemporaryBlockFilesRemovedPerRound']};",
        "pub const MAX_AUTHORITY_GC_PAYLOAD_SEGMENT_FILES_SCANNED: usize = "
        f"{authority_gc['maximumPayloadSegmentFilesScannedPerRound']};",
        "pub const MAX_AUTHORITY_GC_ORPHAN_PAYLOAD_SEGMENT_FILES_REMOVED: usize = "
        f"{authority_gc['maximumOrphanPayloadSegmentFilesRemovedPerRound']};",
        "pub const MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED: usize = "
        f"{authority_gc['maximumPayloadSegmentsScannedPerRound']};",
        "pub const MIN_AUTHORITY_GC_PAYLOAD_COMPACTION_BLOCKS: usize = "
        f"{authority_gc['minimumPayloadCompactionBlocks']};",
        f'pub const DESKTOP_PREFERENCE_NAMESPACE: &str = "{table["physicalNamespace"]}";',
        f'pub const BILLING_WALLET_NAMESPACE: &str = "{billing["walletNamespace"]}";',
        f'pub const BILLING_LEDGER_NAMESPACE: &str = "{billing["ledgerNamespace"]}";',
        f'pub const BILLING_LEDGER_ID_CLAIM_NAMESPACE: &str = "{billing["ledgerIdClaimNamespace"]}";',
        f'pub const BILLING_IDEMPOTENCY_NAMESPACE: &str = "{billing["idempotencyNamespace"]}";',
        f'pub const BILLING_USER_TIME_INDEX_NAMESPACE: &str = "{billing["userTimeIndexNamespace"]}";',
        f'pub const BILLING_USER_AGGREGATE_NAMESPACE: &str = "{billing["userAggregateNamespace"]}";',
        f'pub const BILLING_RESERVE_STATE_NAMESPACE: &str = "{billing["reserveStateNamespace"]}";',
        f'pub const BILLING_RESERVE_AGE_INDEX_NAMESPACE: &str = "{billing["reserveAgeIndexNamespace"]}";',
        f'pub const BILLING_PAYMENT_DOCUMENT_NAMESPACE: &str = "{billing["paymentDocumentNamespace"]}";',
        f'pub const BILLING_PAYMENT_ID_CLAIM_NAMESPACE: &str = "{billing["paymentIdClaimNamespace"]}";',
        f'pub const BILLING_PAYMENT_PROVIDER_CLAIM_NAMESPACE: &str = "{billing["paymentProviderClaimNamespace"]}";',
        f'pub const BILLING_PAYMENT_CREATED_INDEX_NAMESPACE: &str = "{billing["paymentCreatedIndexNamespace"]}";',
        f'pub const BILLING_PAYMENT_COUNT_NAMESPACE: &str = "{billing["paymentCountNamespace"]}";',
        f'pub const BILLING_REDEEM_BATCH_DOCUMENT_NAMESPACE: &str = "{billing["redeemBatchDocumentNamespace"]}";',
        f'pub const BILLING_REDEEM_CODE_LOCATOR_NAMESPACE: &str = "{billing["redeemCodeLocatorNamespace"]}";',
        f'pub const BILLING_REDEEM_CODE_STATE_NAMESPACE: &str = "{billing["redeemCodeStateNamespace"]}";',
        f'pub const BILLING_REDEEM_CREATED_INDEX_NAMESPACE: &str = "{billing["redeemCreatedIndexNamespace"]}";',
        "pub const BILLING_REDEEM_BATCH_CREATED_INDEX_NAMESPACE: &str =",
        f'    "{billing["redeemBatchCreatedIndexNamespace"]}";',
        f'pub const BILLING_REDEEM_COUNT_NAMESPACE: &str = "{billing["redeemCountNamespace"]}";',
        "pub const MAX_BILLING_LEDGER_ENTRIES_PER_USER: usize = "
        f"{billing['maximumLedgerEntriesPerUser']};",
        f"pub const MAX_BILLING_PAYMENTS: usize = {billing['maximumPayments']};",
        "pub const MAX_BILLING_PAYMENT_DOCUMENT_BYTES: usize = "
        f"{billing['maximumPaymentDocumentBytes']};",
        f"pub const MAX_BILLING_REDEEM_CODES: usize = {billing['maximumRedeemCodes']};",
        "pub const MAX_BILLING_REDEEM_CODES_PER_MINT: usize = "
        f"{billing['maximumRedeemCodesPerMint']};",
        "pub const MAX_BILLING_REDEEM_BATCH_DOCUMENT_BYTES: usize = "
        f"{billing['maximumRedeemBatchDocumentBytes']};",
        f"pub const BILLING_REDEEM_BATCH_SHARDS: usize = {billing['redeemBatchShards']};",
        "pub const MAX_BILLING_REDEEM_BATCH_SHARD_BYTES: usize = "
        f"{billing['maximumRedeemBatchShardBytes']};",
        f"pub const BILLING_REDEEM_LOCATOR_SHARDS: usize = {billing['redeemLocatorShards']};",
        "pub const MAX_BILLING_REDEEM_LOCATOR_SHARD_BYTES: usize = "
        f"{billing['maximumRedeemLocatorShardBytes']};",
        "pub const MAX_BILLING_REDEEM_STATE_SHARD_BYTES: usize = "
        f"{billing['maximumRedeemStateShardBytes']};",
        f"pub const MAX_BILLING_LIST_ROWS: usize = {billing['maximumListRows']};",
        "pub const MAX_BILLING_LIST_SCAN_ROWS: usize = "
        f"{billing['maximumListScanRows']};",
        "pub const MAX_BILLING_RESPONSE_BYTES: usize = "
        f"{billing['maximumResponseBytes']};",
        "pub const MAX_BILLING_NOTE_CHARACTERS: usize = "
        f"{billing['maximumNoteCharacters']};",
        f'pub const TENANT_USER_PROFILE_NAMESPACE: &str = "{tenant_users["profileNamespace"]}";',
        f'pub const TENANT_USER_STATE_NAMESPACE: &str = "{tenant_users["stateNamespace"]}";',
        f'pub const TENANT_USER_EMAIL_INDEX_NAMESPACE: &str = "{tenant_users["emailIndexNamespace"]}";',
        f'pub const TENANT_USER_CREATED_INDEX_NAMESPACE: &str = "{tenant_users["createdIndexNamespace"]}";',
        f'pub const TENANT_USER_STATUS_INDEX_NAMESPACE: &str = "{tenant_users["statusIndexNamespace"]}";',
        f'pub const TENANT_USER_OWNER_SEQUENCE_NAMESPACE: &str = "{tenant_users["ownerSequenceNamespace"]}";',
        "pub const MAX_TENANT_USER_DOCUMENT_BYTES: usize = "
        f"{tenant_users['maximumDocumentBytes']};",
        "pub const MAX_TENANT_USER_LIST_ROWS: usize = "
        f"{tenant_users['maximumListRows']};",
        "pub const MAX_TENANT_USER_LIST_SCAN_ROWS: usize = "
        f"{tenant_users['maximumListScanRows']};",
        "pub const MAX_TENANT_USER_RESPONSE_BYTES: usize = "
        f"{tenant_users['maximumResponseBytes']};",
        f'pub const CREDENTIAL_CORE_NAMESPACE: &str = "{credentials["coreNamespace"]}";',
        f'pub const CREDENTIAL_SETTINGS_NAMESPACE: &str = "{credentials["settingsNamespace"]}";',
        f'pub const CREDENTIAL_STATE_NAMESPACE: &str = "{credentials["stateNamespace"]}";',
        f'pub const CREDENTIAL_SECRET_INDEX_NAMESPACE: &str = "{credentials["secretIndexNamespace"]}";',
        f'pub const CREDENTIAL_OWNER_INDEX_NAMESPACE: &str = "{credentials["ownerIndexNamespace"]}";',
        f'pub const CREDENTIAL_OWNER_COUNT_NAMESPACE: &str = "{credentials["ownerCountNamespace"]}";',
        "pub const MAX_CREDENTIAL_DOCUMENT_BYTES: usize = "
        f"{credentials['maximumDocumentBytes']};",
        f"pub const MAX_CREDENTIAL_SCOPES: usize = {credentials['maximumScopes']};",
        "pub const MAX_CREDENTIAL_RESPONSE_BYTES: usize = "
        f"{credentials['maximumResponseBytes']};",
        "pub const MAX_CREDENTIAL_TIMESTAMP_SECONDS: f64 = "
        f"{credentials['maximumTimestampSeconds']}.0;",
        "pub const BROWSER_SITE_OBSERVATION_DOCUMENT_NAMESPACE: &str = "
        f'"{browser_observations["physicalNamespace"]}";',
        "pub const BROWSER_SITE_OBSERVATION_LRU_NAMESPACE: &str =\n"
        f'    "{browser_observations["lruNamespace"]}";',
        "pub const MAX_BROWSER_SITE_OBSERVATIONS_PER_OWNER: usize = "
        f"{browser_observations['maximumDocumentsPerOwner']};",
        "pub const MAX_BROWSER_SITE_OBSERVATION_BYTES: usize = "
        f"{browser_observations['maximumObservationPayloadBytes']};",
        "pub const MAX_BROWSER_SITE_OBSERVATION_STORED_BYTES: usize = "
        f"{browser_observations['maximumStoredDocumentBytes']};",
        f'pub const RECORD_DOCUMENT_NAMESPACE: &str = "{records["physicalNamespace"]}";',
        f'pub const RATE_LIMIT_BUCKET_NAMESPACE: &str = "{rate_limits["bucketNamespace"]}";',
        f'pub const RATE_LIMIT_EVENT_NAMESPACE: &str = "{rate_limits["eventNamespace"]}";',
        f'pub const RATE_LIMIT_EXPIRY_NAMESPACE: &str = "{rate_limits["expiryIndexNamespace"]}";',
        f'pub const RATE_LIMIT_COUNT_NAMESPACE: &str = "{rate_limits["countNamespace"]}";',
        f'pub const DAILY_COST_DOCUMENT_NAMESPACE: &str = "{daily_costs["physicalNamespace"]}";',
        f'pub const DAILY_COST_COUNT_NAMESPACE: &str = "{daily_costs["countNamespace"]}";',
        "pub const MAX_DAILY_COST_DOCUMENT_BYTES: usize = "
        f"{daily_costs['maximumDocumentBytes']};",
        f"pub const MAX_DAILY_COST_MONTH_ROWS: usize = {daily_costs['maximumMonthRows']};",
        "pub const MAX_DAILY_COST_PERSISTED_DATES: usize = "
        f"{daily_costs['maximumPersistedDateProbes']};",
        f'pub const ORCHESTRATION_DEFINITION_DOCUMENT_NAMESPACE: &str = "{orchestration_definitions["documentNamespace"]}";',
        f'pub const ORCHESTRATION_DEFINITION_ID_CLAIM_NAMESPACE: &str = "{orchestration_definitions["tenantGlobalIdClaimNamespace"]}";',
        "pub const ORCHESTRATION_DEFINITION_UPDATED_INDEX_NAMESPACE: &str =\n"
        f'    "{orchestration_definitions["ownerUpdatedIndexNamespace"]}";',
        f'pub const ORCHESTRATION_DEFINITION_COUNT_NAMESPACE: &str = "{orchestration_definitions["ownerCountNamespace"]}";',
        f"pub const MAX_ORCHESTRATION_DEFINITIONS_PER_OWNER: usize = {orchestration_definitions['maximumDefinitionsPerOwner']};",
        f"pub const MAX_ORCHESTRATION_DEFINITION_DOCUMENT_BYTES: usize = {orchestration_definitions['maximumDefinitionDocumentBytes']};",
        f"pub const MAX_ORCHESTRATION_DEFINITION_ID_CHARACTERS: usize = {orchestration_definitions['maximumDefinitionIdCharacters']};",
        f"pub const MAX_ORCHESTRATION_RESPONSE_BYTES: usize = {orchestration_definitions['maximumResponseBytes']};",
        f'pub const ORCHESTRATION_RUN_CORE_NAMESPACE: &str = "{orchestration_runs["coreNamespace"]}";',
        f'pub const ORCHESTRATION_RUN_STATE_NAMESPACE: &str = "{orchestration_runs["stateNamespace"]}";',
        f'pub const ORCHESTRATION_RUN_ID_CLAIM_NAMESPACE: &str = "{orchestration_runs["tenantGlobalIdClaimNamespace"]}";',
        f'pub const ORCHESTRATION_RUN_CREATED_INDEX_NAMESPACE: &str = "{orchestration_runs["ownerCreatedIndexNamespace"]}";',
        "pub const ORCHESTRATION_RUN_STATUS_CREATED_INDEX_NAMESPACE: &str =\n"
        f'    "{orchestration_runs["ownerStatusCreatedIndexNamespace"]}";',
        "pub const ORCHESTRATION_RUN_ORCHESTRATION_CREATED_INDEX_NAMESPACE: &str =\n"
        f'    "{orchestration_runs["ownerOrchestrationCreatedIndexNamespace"]}";',
        "pub const ORCHESTRATION_RUN_GLOBAL_ACTIVE_INDEX_NAMESPACE: &str =\n"
        f'    "{orchestration_runs["tenantGlobalActiveIndexNamespace"]}";',
        "pub const ORCHESTRATION_RUN_PENDING_CONTINUITY_INDEX_NAMESPACE: &str =\n"
        f'    "{orchestration_runs["tenantGlobalPendingContinuityIndexNamespace"]}";',
        f'pub const ORCHESTRATION_GOAL_ACTIVE_CLAIM_NAMESPACE: &str = "{orchestration_runs["goalActiveClaimNamespace"]}";',
        "pub const ORCHESTRATION_GOAL_CREATED_INDEX_NAMESPACE: &str =\n"
        f'    "{orchestration_runs["ownerGoalCreatedIndexNamespace"]}";',
        f'pub const ORCHESTRATION_RUN_EVENT_DOCUMENT_NAMESPACE: &str = "{orchestration_runs["eventDocumentNamespace"]}";',
        f'pub const ORCHESTRATION_RUN_COUNT_NAMESPACE: &str = "{orchestration_runs["ownerCountNamespace"]}";',
        f"pub const MAX_ORCHESTRATION_RUNS_PER_OWNER: usize = {orchestration_runs['maximumRunsPerOwner']};",
        f"pub const MAX_ORCHESTRATION_RUN_CORE_BYTES: usize = {orchestration_runs['maximumRunCoreBytes']};",
        f"pub const MAX_ORCHESTRATION_RUN_STATE_BYTES: usize = {orchestration_runs['maximumRunStateBytes']};",
        f"pub const MAX_ORCHESTRATION_EVENTS_PER_RUN: usize = {orchestration_runs['maximumEventsPerRun']};",
        f"pub const MAX_ORCHESTRATION_EVENT_DOCUMENT_BYTES: usize = {orchestration_runs['maximumEventDocumentBytes']};",
        f"pub const MAX_ORCHESTRATION_EVENT_PAGE_ROWS: usize = {orchestration_runs['maximumEventPageRows']};",
        f"pub const MAX_ORCHESTRATION_RUN_LIST_ROWS: usize = {orchestration_runs['maximumListRows']};",
        f"pub const MAX_ORCHESTRATION_MAINTENANCE_ROWS: usize = {orchestration_runs['maximumMaintenanceRows']};",
        f"pub const MAX_ORCHESTRATION_RUN_ID_CHARACTERS: usize = {orchestration_runs['maximumRunIdCharacters']};",
        f"pub const MAX_ORCHESTRATION_TENANT_LABEL_CHARACTERS: usize = {orchestration_runs['maximumTenantLabelCharacters']};",
        f'pub const SWARM_SESSION_DOCUMENT_NAMESPACE: &str = "{swarm["sessionDocumentNamespace"]}";',
        f'pub const SWARM_SESSION_STATE_NAMESPACE: &str = "{swarm["sessionStateNamespace"]}";',
        f'pub const SWARM_SESSION_KEY_CLAIM_NAMESPACE: &str = "{swarm["tenantGlobalKeyClaimNamespace"]}";',
        f'pub const SWARM_RESUMABLE_INDEX_NAMESPACE: &str = "{swarm["ownerResumableIndexNamespace"]}";',
        f'pub const SWARM_SESSION_COUNT_NAMESPACE: &str = "{swarm["ownerSessionCountNamespace"]}";',
        f'pub const SWARM_AGENT_CORE_NAMESPACE: &str = "{swarm["agentCoreNamespace"]}";',
        f'pub const SWARM_AGENT_STATE_NAMESPACE: &str = "{swarm["agentStateNamespace"]}";',
        f'pub const SWARM_AGENT_COUNT_NAMESPACE: &str = "{swarm["agentCountNamespace"]}";',
        f"pub const MAX_SWARM_SESSIONS_PER_OWNER: usize = {swarm['maximumSessionsPerOwner']};",
        f"pub const MAX_SWARM_AGENTS_PER_SESSION: usize = {swarm['maximumAgentsPerSession']};",
        f"pub const MAX_SWARM_SESSION_DOCUMENT_BYTES: usize = {swarm['maximumSessionDocumentBytes']};",
        f"pub const MAX_SWARM_AGENT_CORE_BYTES: usize = {swarm['maximumAgentCoreBytes']};",
        f"pub const MAX_SWARM_RESPONSE_BYTES: usize = {swarm['maximumResponseBytes']};",
        f"pub const MAX_SWARM_KEY_CHARACTERS: usize = {swarm['maximumSwarmKeyCharacters']};",
        f"pub const MAX_SWARM_AGENT_ID_CHARACTERS: usize = {swarm['maximumAgentIdCharacters']};",
        f"pub const MAX_SWARM_OBJECTIVE_CHARACTERS: usize = {swarm['maximumObjectiveCharacters']};",
        f"pub const MAX_SWARM_AGENT_IDS_PER_DELIVERY: usize = {swarm['maximumAgentIdsPerDelivery']};",
        f"pub const MAX_SWARM_ROUNDS_USED: u64 = {swarm['maximumRoundsUsed']};",
        f'pub const INTEGRATION_WORKSPACE_NAMESPACE: &str = "{integration["workspaceNamespace"]}";',
        f'pub const INTEGRATION_NATURAL_CLAIM_NAMESPACE: &str = "{integration["naturalKeyClaimNamespace"]}";',
        f'pub const INTEGRATION_ROW_LOCATOR_NAMESPACE: &str = "{integration["rowLocatorNamespace"]}";',
        f'pub const INTEGRATION_ROW_SEQUENCE_NAMESPACE: &str = "{integration["rowSequenceNamespace"]}";',
        f'pub const INTEGRATION_READY_INDEX_NAMESPACE: &str = "{integration["globalReadyIndexNamespace"]}";',
        "pub const INTEGRATION_INTEGRATING_INDEX_NAMESPACE: &str =",
        f'    "{integration["globalIntegratingIndexNamespace"]}";',
        f'pub const INTEGRATION_PROJECT_ACTIVE_CLAIM_NAMESPACE: &str = "{integration["globalProjectActiveClaimNamespace"]}";',
        f'pub const INTEGRATION_ACTIVE_COUNT_NAMESPACE: &str = "{integration["globalActiveCountNamespace"]}";',
        "pub const INTEGRATION_PROJECT_UPDATED_INDEX_NAMESPACE: &str =",
        f'    "{integration["ownerProjectUpdatedIndexNamespace"]}";',
        f'pub const INTEGRATION_WORKSPACE_COUNT_NAMESPACE: &str = "{integration["ownerWorkspaceCountNamespace"]}";',
        f'pub const INTEGRATION_EVENT_NAMESPACE: &str = "{integration["eventNamespace"]}";',
        f'pub const INTEGRATION_EVENT_SEQUENCE_NAMESPACE: &str = "{integration["eventSequenceNamespace"]}";',
        f'pub const INTEGRATION_EVENT_COUNT_NAMESPACE: &str = "{integration["eventCountNamespace"]}";',
        f"pub const MAX_INTEGRATION_WORKSPACES_PER_OWNER: usize = {integration['maximumWorkspacesPerOwner']};",
        f"pub const MAX_INTEGRATION_ACTIVE_WORKSPACES: usize = {integration['maximumActiveWorkspacesPerAuthority']};",
        f"pub const MAX_INTEGRATION_STATUS_ROWS: usize = {integration['maximumStatusRows']};",
        f"pub const MAX_INTEGRATION_EVENTS_PER_PROJECT: usize = {integration['maximumEventsPerProject']};",
        f"pub const MAX_INTEGRATION_STATUS_EVENTS: usize = {integration['maximumStatusEvents']};",
        f"pub const MAX_INTEGRATION_PROJECT_ROOT_CHARACTERS: usize = {integration['maximumProjectRootCharacters']};",
        f"pub const MAX_INTEGRATION_TASK_ID_CHARACTERS: usize = {integration['maximumTaskIdCharacters']};",
        f"pub const MAX_INTEGRATION_WORKSPACE_DOCUMENT_BYTES: usize = {integration['maximumWorkspaceDocumentBytes']};",
        f"pub const MAX_INTEGRATION_RESPONSE_BYTES: usize = {integration['maximumResponseBytes']};",
        f"pub const INTEGRATION_STALE_SECONDS: f64 = {integration['staleIntegratingSeconds']}.0;",
        f'pub const PROJECT_BRAIN_DOCUMENT_NAMESPACE: &str = "{project_brain["physicalNamespace"]}";',
        f'pub const PROJECT_BRAIN_CHECKPOINT_NAMESPACE: &str = "{project_brain["checkpointNamespace"]}";',
        f'pub const PROJECT_BRAIN_EVENT_INDEX_NAMESPACE: &str = "{project_brain["eventIndexNamespace"]}";',
        f'pub const PROJECT_BRAIN_UPDATED_INDEX_NAMESPACE: &str = "{project_brain["updatedIndexNamespace"]}";',
        "pub const MAX_PROJECT_BRAIN_PROJECT_KEY_CHARACTERS: usize = "
        f"{project_brain['maximumProjectKeyCharacters']};",
        "pub const MAX_PROJECT_BRAIN_DOCUMENT_BYTES: usize = "
        f"{project_brain['maximumDocumentBytes']};",
        "pub const MAX_PROJECT_BRAIN_ACTIVE_WORK: usize = "
        f"{project_brain['maximumActiveWorkItems']};",
        "pub const MAX_PROJECT_BRAIN_WORK_HISTORY: usize = "
        f"{project_brain['maximumWorkHistoryItems']};",
        "pub const MAX_PROJECT_BRAIN_CHANGED_PATHS: usize = "
        f"{project_brain['maximumChangedPathsPerWork']};",
        "pub const MAX_PROJECT_BRAIN_ARTIFACTS_PER_WORK: usize = "
        f"{project_brain['maximumArtifactsPerWork']};",
        "pub const MAX_PROJECT_BRAIN_NARRATIVES: usize = "
        f"{project_brain['maximumNarratives']};",
        "pub const MAX_PROJECT_BRAIN_NARRATIVE_TEXT_BYTES: usize = "
        f"{project_brain['maximumNarrativeTextBytes']};",
        "pub const MAX_PROJECT_BRAIN_WATCH_ITEMS: usize = "
        f"{project_brain['maximumWatchItems']};",
        "pub const MAX_PROJECT_BRAIN_CHECKER_VERSIONS: usize = "
        f"{project_brain['maximumCheckerVersions']};",
        "pub const MAX_PROJECT_BRAIN_CHARTER_DECISIONS: usize = "
        f"{project_brain['maximumCharterDecisions']};",
        "pub const MAX_PROJECT_BRAIN_CURSORS: usize = "
        f"{project_brain['maximumCursors']};",
        "pub const MAX_PROJECT_BRAIN_EVENT_BYTES: usize = "
        f"{project_brain['maximumEventBytes']};",
        "pub const PROJECT_BRAIN_EVENT_CHECKPOINT_THRESHOLD: u64 = "
        f"{project_brain['eventCheckpointThreshold']};",
        "pub const PROJECT_BRAIN_EVENT_CHECKPOINT_TAIL: u64 = "
        f"{project_brain['eventCheckpointTail']};",
        f'pub const SCHEDULER_TASK_DOCUMENT_NAMESPACE: &str = "{scheduler["physicalNamespace"]}";',
        f'pub const SCHEDULER_TASK_ID_CLAIM_NAMESPACE: &str = "{scheduler["tenantGlobalIdClaimNamespace"]}";',
        f'pub const SCHEDULER_TASK_SYSTEM_KEY_CLAIM_NAMESPACE: &str = "{scheduler["systemKeyClaimNamespace"]}";',
        f'pub const SCHEDULER_TASK_CREATED_INDEX_NAMESPACE: &str = "{scheduler["createdIndexNamespace"]}";',
        "pub const SCHEDULER_TASK_ENABLED_CREATED_INDEX_NAMESPACE: &str =\n"
        f'    "{scheduler["enabledCreatedIndexNamespace"]}";',
        "pub const SCHEDULER_TASK_GLOBAL_CREATED_INDEX_NAMESPACE: &str =\n"
        f'    "{scheduler["tenantGlobalCreatedIndexNamespace"]}";',
        "pub const SCHEDULER_TASK_GLOBAL_ENABLED_CREATED_INDEX_NAMESPACE: &str =\n"
        f'    "{scheduler["tenantGlobalEnabledCreatedIndexNamespace"]}";',
        f'pub const SCHEDULER_TASK_COUNT_NAMESPACE: &str = "{scheduler["ownerCountNamespace"]}";',
        f'pub const SCHEDULER_POLL_DOCUMENT_NAMESPACE: &str = "{scheduler["pollDocumentNamespace"]}";',
        f'pub const SCHEDULER_POLL_TIME_INDEX_NAMESPACE: &str = "{scheduler["pollTimeIndexNamespace"]}";',
        f'pub const SCHEDULER_POLL_SEQUENCE_NAMESPACE: &str = "{scheduler["pollSequenceNamespace"]}";',
        "pub const MAX_SCHEDULER_TASK_DOCUMENT_BYTES: usize = "
        f"{scheduler['maximumTaskDocumentBytes']};",
        "pub const MAX_SCHEDULER_TASK_ID_CHARACTERS: usize = "
        f"{scheduler['maximumTaskIdCharacters']};",
        "pub const MAX_SCHEDULER_SYSTEM_KEY_CHARACTERS: usize = "
        f"{scheduler['maximumSystemKeyCharacters']};",
        "pub const MAX_SCHEDULER_POLL_ROWS: usize = "
        f"{scheduler['maximumPollRows']};",
        "pub const MAX_SCHEDULER_POLL_DOCUMENT_BYTES: usize = "
        f"{scheduler['maximumPollDocumentBytes']};",
        f'pub const QUEUE_ITEM_CORE_NAMESPACE: &str = "{queue["coreNamespace"]}";',
        f'pub const QUEUE_ITEM_STATE_NAMESPACE: &str = "{queue["stateNamespace"]}";',
        f'pub const QUEUE_ITEM_ID_CLAIM_NAMESPACE: &str = "{queue["tenantGlobalIdClaimNamespace"]}";',
        f'pub const QUEUE_CONVERSATION_ORDER_INDEX_NAMESPACE: &str = "{queue["ownerConversationOrderIndexNamespace"]}";',
        f'pub const QUEUE_GLOBAL_CONVERSATION_INDEX_NAMESPACE: &str = "{queue["tenantGlobalConversationIndexNamespace"]}";',
        f'pub const QUEUE_GLOBAL_LEASE_INDEX_NAMESPACE: &str = "{queue["tenantGlobalLeaseIndexNamespace"]}";',
        f'pub const QUEUE_AUTOPILOT_MARKER_NAMESPACE: &str = "{queue["autopilotMarkerNamespace"]}";',
        f'pub const QUEUE_GLOBAL_AUTOPILOT_INDEX_NAMESPACE: &str = "{queue["tenantGlobalAutopilotIndexNamespace"]}";',
        f"pub const MAX_QUEUE_ITEMS_PER_CONVERSATION: usize = {queue['maximumItemsPerConversation']};",
        f"pub const MAX_QUEUE_ITEM_CORE_BYTES: usize = {queue['maximumItemCoreBytes']};",
        f"pub const MAX_QUEUE_GLOBAL_CONVERSATION_ROWS: usize = {queue['maximumGlobalConversationRows']};",
        f"pub const MAX_QUEUE_AUTOPILOT_MARKERS: usize = {queue['maximumAutopilotMarkers']};",
        f"pub const MAX_QUEUE_REAP_ITEMS: usize = {queue['maximumReapItems']};",
        f"pub const MAX_QUEUE_RESPONSE_BYTES: usize = {queue['maximumResponseBytes']};",
        f"pub const MAX_QUEUE_ID_CHARACTERS: usize = {queue['maximumIdCharacters']};",
        f"pub const MAX_QUEUE_CONVERSATION_ID_CHARACTERS: usize = {queue['maximumConversationIdCharacters']};",
        f'pub const TIMER_DOCUMENT_NAMESPACE: &str = "{timers["physicalNamespace"]}";',
        f'pub const TIMER_ID_CLAIM_NAMESPACE: &str = "{timers["tenantGlobalIdClaimNamespace"]}";',
        f'pub const TIMER_STATUS_CREATED_INDEX_NAMESPACE: &str = "{timers["ownerStatusCreatedIndexNamespace"]}";',
        f'pub const TIMER_CONVERSATION_INDEX_NAMESPACE: &str = "{timers["ownerConversationIndexNamespace"]}";',
        f'pub const TIMER_GLOBAL_ACTIVE_CREATED_INDEX_NAMESPACE: &str = "{timers["tenantGlobalActiveCreatedIndexNamespace"]}";',
        f'pub const TIMER_TOTAL_COUNT_NAMESPACE: &str = "{timers["ownerTotalCountNamespace"]}";',
        f'pub const TIMER_ACTIVE_COUNT_NAMESPACE: &str = "{timers["ownerActiveCountNamespace"]}";',
        f'pub const TIMER_POLL_DOCUMENT_NAMESPACE: &str = "{timers["pollDocumentNamespace"]}";',
        f'pub const TIMER_POLL_TIME_INDEX_NAMESPACE: &str = "{timers["pollTimeIndexNamespace"]}";',
        f'pub const TIMER_POLL_ID_CLAIM_NAMESPACE: &str = "{timers["pollIdClaimNamespace"]}";',
        f'pub const TIMER_POLL_SEQUENCE_NAMESPACE: &str = "{timers["pollSequenceNamespace"]}";',
        f"pub const MAX_TIMER_DOCUMENT_BYTES: usize = {timers['maximumTimerDocumentBytes']};",
        f"pub const MAX_TIMER_ID_CHARACTERS: usize = {timers['maximumTimerIdCharacters']};",
        f"pub const MAX_TIMER_LIST_ROWS: usize = {timers['maximumTimerListRows']};",
        f"pub const MAX_TIMER_GLOBAL_ACTIVE_ROWS: usize = {timers['maximumGlobalActiveRows']};",
        f"pub const MAX_TIMER_POLL_ROWS: usize = {timers['maximumPollRows']};",
        f"pub const MAX_TIMER_POLL_DOCUMENT_BYTES: usize = {timers['maximumPollDocumentBytes']};",
        "pub const MAX_RATE_LIMIT_ENDPOINT_CHARACTERS: usize = "
        f"{rate_limits['maximumEndpointCharacters']};",
        "pub const MAX_RATE_LIMIT_CLIENT_KEY_CHARACTERS: usize = "
        f"{rate_limits['maximumClientKeyCharacters']};",
        "pub const MAX_RATE_LIMIT_EVENT_ID_CHARACTERS: usize = "
        f"{rate_limits['maximumEventIdCharacters']};",
        "pub const MAX_RATE_LIMIT_EVENTS_PER_WINDOW: u64 = "
        f"{rate_limits['maximumEventsPerWindow']};",
        "pub const MAX_RATE_LIMIT_WINDOW_SECONDS: u64 = "
        f"{rate_limits['maximumWindowSeconds']};",
        f"pub const MAX_RATE_LIMIT_PRUNE_ROWS: usize = {rate_limits['maximumPruneRows']};",
        f"pub const RATE_LIMIT_COUNTER_RADIX: usize = {rate_limits['counterRadix']};",
        f"pub const RATE_LIMIT_COUNTER_DEPTH: usize = {rate_limits['counterDepth']};",
        "pub const MAX_RATE_LIMIT_WINDOW_COUNTER_READS: usize = "
        f"{rate_limits['maximumWindowCounterReads']};",
        f'pub const RECENT_PROJECT_DOCUMENT_NAMESPACE: &str = "{recent_projects["physicalNamespace"]}";',
        f'pub const RECENT_PROJECT_COUNT_NAMESPACE: &str = "{recent_projects["countNamespace"]}";',
        "pub const MAX_RECENT_PROJECTS_PER_OWNER: usize = "
        f"{recent_projects['maximumProjectsPerOwner']};",
        "pub const MAX_RECENT_PROJECT_TOUCH_BATCH: usize = "
        f"{recent_projects['maximumTouchBatch']};",
        "pub const MAX_RECENT_PROJECT_PATH_CHARACTERS: usize = "
        f"{recent_projects['maximumPathCharacters']};",
        f'pub const PROVIDER_DOCUMENT_NAMESPACE: &str = "{providers["physicalNamespace"]}";',
        f'pub const PROVIDER_CREATED_INDEX_NAMESPACE: &str = "{providers["createdIndexNamespace"]}";',
        f'pub const PROVIDER_COUNT_NAMESPACE: &str = "{providers["ownerCountNamespace"]}";',
        f'pub const PROVIDER_ID_CLAIM_NAMESPACE: &str = "{providers["tenantGlobalClaimNamespace"]}";',
        f'pub const ARTIFACT_DOCUMENT_NAMESPACE: &str = "{artifacts["physicalNamespace"]}";',
        f'pub const ARTIFACT_CONVERSATION_INDEX_NAMESPACE: &str = "{artifacts["conversationIndexNamespace"]}";',
        f'pub const ARTIFACT_LIBRARY_INDEX_NAMESPACE: &str = "{artifacts["libraryIndexNamespace"]}";',
        f'pub const ARTIFACT_DEDUPE_NAMESPACE: &str = "{artifacts["dedupeNamespace"]}";',
        f'pub const ARTIFACT_PATH_HEAD_NAMESPACE: &str = "{artifacts["pathHeadNamespace"]}";',
        f'pub const ARTIFACT_PARENT_INDEX_NAMESPACE: &str = "{artifacts["parentIndexNamespace"]}";',
        f'pub const MAX_ARTIFACT_BYTES: usize = {artifacts["maximumContentBytes"]};',
        f'pub const MAX_ARTIFACT_ROWS_PER_CONVERSATION: usize = {artifacts["maximumRowsPerConversation"]};',
        f'pub const TOOL_RESULT_ARTIFACT_NAMESPACE: &str = "{tool_result_artifacts["physicalNamespace"]}";',
        f'pub const TOOL_RESULT_EXPIRY_INDEX_NAMESPACE: &str = "{tool_result_artifacts["expiryIndexNamespace"]}";',
        f'pub const MAX_TOOL_RESULT_ARTIFACT_BYTES: usize = {tool_result_artifacts["maximumContentBytes"]};',
        f'pub const MAX_TOOL_RESULT_RANGE_BYTES: usize = {tool_result_artifacts["maximumRangeBytes"]};',
        f'pub const MAX_TOOL_RESULT_TTL_MILLISECONDS: u64 = {tool_result_artifacts["maximumTtlMilliseconds"]};',
        f'pub const MAX_TOOL_RESULT_PRUNE_ROWS: usize = {tool_result_artifacts["maximumPruneRows"]};',
        f'pub const LOG_AGGREGATE_DOCUMENT_NAMESPACE: &str = "{log_aggregates["physicalNamespace"]}";',
        f'pub const LOG_AGGREGATE_SWEEP_INDEX_NAMESPACE: &str = "{log_aggregates["sweepIndexNamespace"]}";',
        f'pub const LOG_AGGREGATE_SEQUENCE_NAMESPACE: &str = "{log_aggregates["sequenceNamespace"]}";',
        f'pub const MAX_LOG_AGGREGATE_DOCUMENT_BYTES: usize = {log_aggregates["maximumDocumentBytes"]};',
        f'pub const MAX_LOG_AGGREGATE_FINGERPRINT_CHARACTERS: usize = {log_aggregates["maximumFingerprintCharacters"]};',
        f'pub const MAX_LOG_AGGREGATE_LEVEL_CHARACTERS: usize = {log_aggregates["maximumLevelCharacters"]};',
        f'pub const MAX_LOG_AGGREGATE_LOGGER_CHARACTERS: usize = {log_aggregates["maximumLoggerCharacters"]};',
        f'pub const MAX_LOG_AGGREGATE_TEMPLATE_CHARACTERS: usize = {log_aggregates["maximumTemplateCharacters"]};',
        f'pub const MAX_LOG_AGGREGATE_SAMPLE_CHARACTERS: usize = {log_aggregates["maximumSampleCharacters"]};',
        f'pub const MAX_LOG_AGGREGATE_FLUSH_BATCH: usize = {log_aggregates["maximumFlushBatch"]};',
        f'pub const MAX_LOG_AGGREGATE_QUERY_ROWS: usize = {log_aggregates["maximumQueryRows"]};',
        f'pub const MAX_LOG_AGGREGATE_SCAN_ROWS: usize = {log_aggregates["maximumScanRows"]};',
        f'pub const MAX_LOG_AGGREGATE_SWEEP_BATCH: usize = {log_aggregates["maximumSweepBatch"]};',
        f'pub const MAX_LOG_AGGREGATE_ROW_COUNT: u64 = {log_aggregates["maximumRowCount"]};',
        f'pub const PAPER_LIBRARY_CORE_NAMESPACE: &str = "{paper_library["coreNamespace"]}";',
        f'pub const PAPER_LIBRARY_STATE_NAMESPACE: &str = "{paper_library["stateNamespace"]}";',
        f'pub const PAPER_LIBRARY_UPDATED_INDEX_NAMESPACE: &str = "{paper_library["updatedIndexNamespace"]}";',
        f'pub const PAPER_LIBRARY_HASH_INDEX_NAMESPACE: &str = "{paper_library["hashIndexNamespace"]}";',
        f'pub const PAPER_LIBRARY_ARXIV_INDEX_NAMESPACE: &str = "{paper_library["arxivIndexNamespace"]}";',
        f'pub const PAPER_LIBRARY_REPORT_PRESENCE_NAMESPACE: &str = "{paper_library["reportPresenceNamespace"]}";',
        f'pub const PAPER_LIBRARY_COUNT_NAMESPACE: &str = "{paper_library["countNamespace"]}";',
        f'pub const MAX_PAPER_LIBRARY_ROWS_PER_OWNER: usize = {paper_library["maximumRowsPerOwner"]};',
        f'pub const MAX_PAPER_LIBRARY_CORE_DOCUMENT_BYTES: usize = {paper_library["maximumCoreDocumentBytes"]};',
        f'pub const MAX_PAPER_LIBRARY_STATE_DOCUMENT_BYTES: usize = {paper_library["maximumStateDocumentBytes"]};',
        f'pub const MAX_PAPER_LIBRARY_RESPONSE_BYTES: usize = {paper_library["maximumResponseBytes"]};',
        f'pub const MAX_PAPER_LIBRARY_FANIN_PAPERS: usize = {paper_library["maximumFaninPapers"]};',
        f'pub const MAX_PAPER_LIBRARY_FANIN_TEXT_CHARACTERS: usize = {paper_library["maximumFaninTextCharacters"]};',
        f'pub const MAX_PAPER_LIBRARY_QA_TEXT_CHARACTERS: usize = {paper_library["maximumQaTextCharacters"]};',
        f'pub const PAPER_NOTE_DOCUMENT_NAMESPACE: &str = "{paper_notes["physicalNamespace"]}";',
        f'pub const PAPER_NOTE_LIST_INDEX_NAMESPACE: &str = "{paper_notes["listIndexNamespace"]}";',
        f'pub const PAPER_NOTE_COUNT_NAMESPACE: &str = "{paper_notes["countNamespace"]}";',
        f'pub const MAX_PAPER_NOTES_PER_OWNER: usize = {paper_notes["maximumRowsPerOwner"]};',
        f'pub const MAX_PAPER_NOTE_DOCUMENT_BYTES: usize = {paper_notes["maximumDocumentBytes"]};',
        f'pub const MAX_PAPER_NOTE_RESPONSE_BYTES: usize = {paper_notes["maximumResponseBytes"]};',
        f'pub const PLUGIN_MANIFEST_DOCUMENT_NAMESPACE: &str = "{plugin_manifests["physicalNamespace"]}";',
        "pub const MAX_PLUGIN_NAMESPACE_CHARACTERS: usize = "
        f"{plugin_manifests['maximumNamespaceCharacters']};",
        "pub const MAX_PLUGIN_MANIFEST_BYTES: usize = "
        f"{plugin_manifests['maximumManifestBytes']};",
        f'pub const KNOWLEDGE_DOCUMENT_NAMESPACE: &str = "{knowledge_documents["documentNamespace"]}";',
        f'pub const KNOWLEDGE_CHUNK_DOCUMENT_NAMESPACE: &str = "{knowledge_documents["chunkNamespace"]}";',
        f'pub const KNOWLEDGE_ASSET_DOCUMENT_NAMESPACE: &str = "{knowledge_documents["assetNamespace"]}";',
        f'pub const KNOWLEDGE_ASSET_ORDINAL_INDEX_NAMESPACE: &str = "{knowledge_documents["assetOrdinalIndexNamespace"]}";',
        f'pub const KNOWLEDGE_ASSET_CHUNK_REVERSE_NAMESPACE: &str = "{knowledge_documents["assetChunkReverseNamespace"]}";',
        f'pub const KNOWLEDGE_TERM_NAMESPACE: &str = "{knowledge_documents["termNamespace"]}";',
        f'pub const KNOWLEDGE_DIGEST_INDEX_NAMESPACE: &str = "{knowledge_documents["digestIndexNamespace"]}";',
        f'pub const KNOWLEDGE_SETTINGS_NAMESPACE: &str = "{knowledge_documents["settingsNamespace"]}";',
        f'pub const KNOWLEDGE_CHUNK_ASSET_LINK_NAMESPACE: &str = "{knowledge_documents["chunkAssetLinkNamespace"]}";',
        f'pub const KNOWLEDGE_ENRICHMENT_QUEUE_NAMESPACE: &str = "{knowledge_documents["enrichmentQueueNamespace"]}";',
        f'pub const KNOWLEDGE_ENRICHMENT_OWNER_INDEX_NAMESPACE: &str = "{knowledge_documents["enrichmentOwnerIndexNamespace"]}";',
        "pub const MAX_KNOWLEDGE_DOCUMENT_ID_CHARACTERS: usize = "
        f"{knowledge_documents['maximumDocumentIdCharacters']};",
        "pub const MAX_KNOWLEDGE_ASSET_ID_CHARACTERS: usize = "
        f"{knowledge_documents['maximumAssetIdCharacters']};",
        "pub const MAX_KNOWLEDGE_NAME_CHARACTERS: usize = "
        f"{knowledge_documents['maximumNameCharacters']};",
        "pub const MAX_KNOWLEDGE_STORED_NAME_CHARACTERS: usize = "
        f"{knowledge_documents['maximumStoredNameCharacters']};",
        "pub const MAX_KNOWLEDGE_DOCUMENT_KIND_CHARACTERS: usize = "
        f"{knowledge_documents['maximumDocumentKindCharacters']};",
        "pub const MAX_KNOWLEDGE_ASSET_KIND_CHARACTERS: usize = "
        f"{knowledge_documents['maximumAssetKindCharacters']};",
        "pub const MAX_KNOWLEDGE_MIME_TYPE_CHARACTERS: usize = "
        f"{knowledge_documents['maximumMimeTypeCharacters']};",
        "pub const MAX_KNOWLEDGE_METHOD_CHARACTERS: usize = "
        f"{knowledge_documents['maximumMethodCharacters']};",
        "pub const MAX_KNOWLEDGE_RELATION_CHARACTERS: usize = "
        f"{knowledge_documents['maximumRelationCharacters']};",
        "pub const MAX_KNOWLEDGE_STATUS_CHARACTERS: usize = "
        f"{knowledge_documents['maximumStatusCharacters']};",
        "pub const MAX_KNOWLEDGE_SCOPE_CHARACTERS: usize = "
        f"{knowledge_documents['maximumScopeCharacters']};",
        "pub const MAX_KNOWLEDGE_SECTION_CHARACTERS: usize = "
        f"{knowledge_documents['maximumSectionCharacters']};",
        "pub const MAX_KNOWLEDGE_LOCATION_CHARACTERS: usize = "
        f"{knowledge_documents['maximumLocationCharacters']};",
        "pub const MAX_KNOWLEDGE_CHUNK_CONTENT_CHARACTERS: usize = "
        f"{knowledge_documents['maximumChunkContentCharacters']};",
        "pub const MAX_KNOWLEDGE_SEARCH_TEXT_CHARACTERS: usize = "
        f"{knowledge_documents['maximumSearchTextCharacters']};",
        "pub const MAX_KNOWLEDGE_ASSET_TEXT_CHARACTERS: usize = "
        f"{knowledge_documents['maximumAssetTextCharacters']};",
        "pub const MAX_KNOWLEDGE_CAPTION_CHARACTERS: usize = "
        f"{knowledge_documents['maximumCaptionCharacters']};",
        "pub const MAX_KNOWLEDGE_ENRICHMENT_MODEL_CHARACTERS: usize = "
        f"{knowledge_documents['maximumEnrichmentModelCharacters']};",
        "pub const MAX_KNOWLEDGE_ENRICHMENT_ERROR_CHARACTERS: usize = "
        f"{knowledge_documents['maximumEnrichmentErrorCharacters']};",
        "pub const MAX_KNOWLEDGE_MEDIA_METADATA_CHARACTERS: usize = "
        f"{knowledge_documents['maximumMediaMetadataCharacters']};",
        "pub const MAX_KNOWLEDGE_ASSET_METADATA_CHARACTERS: usize = "
        f"{knowledge_documents['maximumAssetMetadataCharacters']};",
        "pub const MAX_KNOWLEDGE_JSON_ARRAY_CHARACTERS: usize = "
        f"{knowledge_documents['maximumJsonArrayCharacters']};",
        "pub const MAX_KNOWLEDGE_TERM_CHARACTERS: usize = "
        f"{knowledge_documents['maximumTermCharacters']};",
        "pub const MAX_KNOWLEDGE_TERMS_PER_CHUNK: usize = "
        f"{knowledge_documents['maximumTermsPerChunk']};",
        "pub const MAX_KNOWLEDGE_SEARCH_TOKENS: usize = "
        f"{knowledge_documents['maximumSearchTokens']};",
        "pub const MAX_KNOWLEDGE_PAGE_ROWS: usize = "
        f"{knowledge_documents['maximumPageRows']};",
        "pub const MAX_KNOWLEDGE_CATALOG_PAGE_SIZE: usize = "
        f"{knowledge_documents['maximumCatalogPageSize']};",
        "pub const MAX_KNOWLEDGE_CATALOG_QUERY_CHARACTERS: usize = "
        f"{knowledge_documents['maximumCatalogQueryCharacters']};",
        "pub const MAX_KNOWLEDGE_CHUNKS_PER_DOCUMENT: usize = "
        f"{knowledge_documents['maximumChunksPerDocument']};",
        "pub const MAX_KNOWLEDGE_ASSETS_PER_DOCUMENT: usize = "
        f"{knowledge_documents['maximumAssetsPerDocument']};",
        "pub const MAX_KNOWLEDGE_OWNER_ASSET_ROWS: usize = "
        f"{knowledge_documents['maximumOwnerAssetRows']};",
        "pub const MAX_KNOWLEDGE_ENRICHMENT_OWNER_ROWS: usize = "
        f"{knowledge_documents['maximumEnrichmentOwnerRows']};",
        "pub const MAX_KNOWLEDGE_DOCUMENT_BYTES: usize = "
        f"{knowledge_documents['maximumDocumentBytes']};",
        "pub const MAX_KNOWLEDGE_CHUNK_DOCUMENT_BYTES: usize = "
        f"{knowledge_documents['maximumChunkDocumentBytes']};",
        "pub const MAX_KNOWLEDGE_ASSET_DOCUMENT_BYTES: usize = "
        f"{knowledge_documents['maximumAssetDocumentBytes']};",
        "pub const MAX_KNOWLEDGE_SEARCH_POSTINGS_SCAN: usize = "
        f"{knowledge_documents['maximumSearchPostingsScan']};",
        "pub const MAX_KNOWLEDGE_CATALOG_SCAN_DOCUMENTS: usize = "
        f"{knowledge_documents['maximumCatalogScanDocuments']};",
        "pub const MAX_KNOWLEDGE_CLAIM_SCAN_ROWS: usize = "
        f"{knowledge_documents['maximumClaimScanRows']};",
        f'pub const PAPER_REPORT_CORE_NAMESPACE: &str = "{paper_reports["coreNamespace"]}";',
        f'pub const PAPER_REPORT_META_NAMESPACE: &str = "{paper_reports["metaNamespace"]}";',
        f'pub const PAPER_REPORT_LATEST_INDEX_NAMESPACE: &str = "{paper_reports["latestIndexNamespace"]}";',
        f'pub const PAPER_REPORT_PRESENCE_NAMESPACE: &str = "{paper_reports["presenceNamespace"]}";',
        f'pub const PAPER_REPORT_COUNT_NAMESPACE: &str = "{paper_reports["countNamespace"]}";',
        f'pub const MAX_PAPER_REPORT_ROWS_PER_OWNER: usize = {paper_reports["maximumRowsPerOwner"]};',
        f'pub const MAX_PAPER_REPORT_DOCUMENT_BYTES: usize = {paper_reports["maximumDocumentBytes"]};',
        f'pub const MAX_PAPER_REPORT_META_BYTES: usize = {paper_reports["maximumMetaBytes"]};',
        f'pub const MAX_PAPER_REPORT_RESPONSE_BYTES: usize = {paper_reports["maximumResponseBytes"]};',
        f'pub const MAX_PAPER_REPORT_REOPEN_SIBLINGS: usize = {paper_reports["maximumReopenSiblings"]};',
        f'pub const MAX_PAPER_REPORT_EXCERPT_HASHES: usize = {paper_reports["maximumExcerptHashes"]};',
        f'pub const MAX_PAPER_REPORT_EXCERPT_TEXT_CHARACTERS: usize = {paper_reports["maximumExcerptTextCharacters"]};',
        f'pub const PAPER_TRANSLATION_DOCUMENT_NAMESPACE: &str = "{paper_translations["documentNamespace"]}";',
        f'pub const PAPER_TRANSLATION_COUNT_NAMESPACE: &str = "{paper_translations["countNamespace"]}";',
        f'pub const MAX_PAPER_TRANSLATION_ROWS_PER_OWNER: usize = {paper_translations["maximumRowsPerOwner"]};',
        f'pub const MAX_PAPER_TRANSLATION_DOCUMENT_BYTES: usize = {paper_translations["maximumDocumentBytes"]};',
        f'pub const MAX_PAPER_TRANSLATION_RESPONSE_BYTES: usize = {paper_translations["maximumResponseBytes"]};',
        f'pub const PAPER_PODCAST_CORE_NAMESPACE: &str = "{paper_podcasts["coreNamespace"]}";',
        f'pub const PAPER_PODCAST_STATE_NAMESPACE: &str = "{paper_podcasts["stateNamespace"]}";',
        f'pub const PAPER_PODCAST_COUNT_NAMESPACE: &str = "{paper_podcasts["countNamespace"]}";',
        f'pub const PAPER_PODCAST_ACTIVE_INDEX_NAMESPACE: &str = "{paper_podcasts["activeIndexNamespace"]}";',
        f'pub const PAPER_PODCAST_ACTIVE_COUNT_NAMESPACE: &str = "{paper_podcasts["activeCountNamespace"]}";',
        f'pub const MAX_PAPER_PODCAST_ROWS_PER_OWNER: usize = {paper_podcasts["maximumRowsPerOwner"]};',
        f'pub const MAX_PAPER_PODCAST_ACTIVE_PER_TENANT: usize = {paper_podcasts["maximumActivePerTenant"]};',
        f'pub const MAX_PAPER_PODCAST_SCRIPT_BYTES: usize = {paper_podcasts["maximumScriptBytes"]};',
        f'pub const MAX_PAPER_PODCAST_META_BYTES: usize = {paper_podcasts["maximumMetaBytes"]};',
        f'pub const MAX_PAPER_PODCAST_CORE_DOCUMENT_BYTES: usize = {paper_podcasts["maximumCoreDocumentBytes"]};',
        f'pub const MAX_PAPER_PODCAST_STATE_DOCUMENT_BYTES: usize = {paper_podcasts["maximumStateDocumentBytes"]};',
        f'pub const MAX_PAPER_PODCAST_RESPONSE_BYTES: usize = {paper_podcasts["maximumResponseBytes"]};',
        f'pub const RAW_ARCHIVE_DOCUMENT_NAMESPACE: &str = "{raw_archives["physicalNamespace"]}";',
        f'pub const RAW_ARCHIVE_ID_CLAIM_NAMESPACE: &str = "{raw_archives["idClaimNamespace"]}";',
        f'pub const RAW_ARCHIVE_TENANT_USAGE_NAMESPACE: &str = "{raw_archives["tenantUsageNamespace"]}";',
        f'pub const RAW_ARCHIVE_ATTEMPT_USAGE_NAMESPACE: &str = "{raw_archives["attemptUsageNamespace"]}";',
        f'pub const RAW_ARCHIVE_OWNER_COUNT_NAMESPACE: &str = "{raw_archives["ownerCountNamespace"]}";',
        f'pub const RAW_ARCHIVE_TASK_INDEX_NAMESPACE: &str = "{raw_archives["taskIndexNamespace"]}";',
        f'pub const RAW_ARCHIVE_TASK_ROUND_INDEX_NAMESPACE: &str = "{raw_archives["taskRoundIndexNamespace"]}";',
        f'pub const RAW_ARCHIVE_ATTEMPT_INDEX_NAMESPACE: &str = "{raw_archives["attemptIndexNamespace"]}";',
        f'pub const RAW_ARCHIVE_CONVERSATION_INDEX_NAMESPACE: &str = "{raw_archives["conversationIndexNamespace"]}";',
        f'pub const RAW_ARCHIVE_CONVERSATION_STATS_NAMESPACE: &str = "{raw_archives["conversationStatsNamespace"]}";',
        "pub const MAX_RAW_ARCHIVE_STORED_BYTES: usize = "
        f"{raw_archives['maximumStoredBytes']};",
        "pub const MAX_RAW_ARCHIVE_DOCUMENT_BYTES: usize = "
        f"{raw_archives['maximumDocumentBytes']};",
        "pub const MAX_RAW_ARCHIVE_DECODED_PART_BYTES: usize = "
        f"{raw_archives['maximumDecodedPartBytes']};",
        "pub const MAX_RAW_ARCHIVE_TENANT_BUDGET_BYTES: u64 = "
        f"{raw_archives['maximumTenantBudgetBytes']};",
        "pub const MAX_RAW_ARCHIVE_READ_CHUNK_BYTES: usize = "
        f"{raw_archives['maximumReadChunkBytes']};",
        "pub const MAX_RAW_ARCHIVES_PER_OWNER: u64 = "
        f"{raw_archives['maximumArchivesPerOwner']};",
        "pub const MAX_RAW_ARCHIVES_PER_CONVERSATION: usize = "
        f"{raw_archives['maximumArchivesPerConversation']};",
        "pub const MAX_RAW_ARCHIVE_SUMMARY_BYTES: usize = "
        f"{raw_archives['maximumSummaryBytes']};",
        "pub const MAX_RAW_ARCHIVE_SUMMARY_KEYS: usize = "
        f"{raw_archives['maximumSummaryKeys']};",
        "pub const MAX_RAW_ARCHIVE_LIST_ROWS: usize = "
        f"{raw_archives['maximumListRows']};",
        "pub const RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES: u64 = "
        f"{raw_archives['freeSpaceWireMaximumBytes']};",
        f'pub const RESEARCH_ARTIFACT_DOCUMENT_NAMESPACE: &str = "{research_artifacts["physicalNamespace"]}";',
        f'pub const RESEARCH_ARTIFACT_STATE_NAMESPACE: &str = "{research_artifacts["stateNamespace"]}";',
        f'pub const RESEARCH_ARTIFACT_CREATED_INDEX_NAMESPACE: &str = "{research_artifacts["createdIndexNamespace"]}";',
        f'pub const RESEARCH_ARTIFACT_COUNT_NAMESPACE: &str = "{research_artifacts["countNamespace"]}";',
        f'pub const MAX_RESEARCH_ARTIFACTS_PER_OWNER: usize = {research_artifacts["maximumRowsPerOwner"]};',
        f'pub const MAX_RESEARCH_DIRECTION_ROWS: usize = {research_artifacts["maximumDirectionRows"]};',
        f'pub const MAX_RESEARCH_DIRECTION_SCAN_ROWS: usize = {research_artifacts["maximumDirectionScanRows"]};',
        f'pub const MAX_RESEARCH_ARTIFACT_DOCUMENT_BYTES: usize = {research_artifacts["maximumDocumentBytes"]};',
        f'pub const MAX_RESEARCH_RESPONSE_BYTES: usize = {research_artifacts["maximumResponseBytes"]};',
        f'pub const RESEARCH_WORKSPACE_DOCUMENT_NAMESPACE: &str = "{research_workspaces["physicalNamespace"]}";',
        f'pub const MAX_RESEARCH_WORKSPACE_BYTES: usize = {research_workspaces["maximumDocumentBytes"]};',
        f'pub const OPTIMIZER_PROPOSAL_DOCUMENT_NAMESPACE: &str = "{optimizer_proposals["physicalNamespace"]}";',
        f'pub const OPTIMIZER_PROPOSAL_CREATED_INDEX_NAMESPACE: &str = "{optimizer_proposals["createdIndexNamespace"]}";',
        "pub const OPTIMIZER_PROPOSAL_STATUS_INDEX_NAMESPACE: &str =\n"
        f'    "{optimizer_proposals["statusCreatedIndexNamespace"]}";',
        f'pub const OPTIMIZER_PROPOSAL_STATUS_NAMESPACE: &str = "{optimizer_proposals["statusNamespace"]}";',
        f'pub const OPTIMIZER_PROPOSAL_COUNT_NAMESPACE: &str = "{optimizer_proposals["countNamespace"]}";',
        f'pub const OPTIMIZER_ACTION_DOCUMENT_NAMESPACE: &str = "{optimizer_actions["physicalNamespace"]}";',
        f'pub const OPTIMIZER_ACTION_APPLIED_INDEX_NAMESPACE: &str = "{optimizer_actions["appliedIndexNamespace"]}";',
        "pub const OPTIMIZER_ACTION_ACTIVE_APPLIED_INDEX_NAMESPACE: &str =\n"
        f'    "{optimizer_actions["activeAppliedIndexNamespace"]}";',
        "pub const OPTIMIZER_ACTION_PROPOSAL_INDEX_NAMESPACE: &str =\n"
        f'    "{optimizer_actions["proposalIndexNamespace"]}";',
        f'pub const OPTIMIZER_ACTION_EXPIRY_INDEX_NAMESPACE: &str = "{optimizer_actions["expiryIndexNamespace"]}";',
        f'pub const OPTIMIZER_ACTION_COUNT_NAMESPACE: &str = "{optimizer_actions["countNamespace"]}";',
        f'pub const MAX_OPTIMIZER_PROPOSALS_PER_OWNER: usize = {optimizer_proposals["maximumRowsPerOwner"]};',
        f'pub const MAX_OPTIMIZER_ACTIONS_PER_OWNER: usize = {optimizer_actions["maximumRowsPerOwner"]};',
        f'pub const MAX_OPTIMIZER_LIST_ROWS: usize = {optimizer_proposals["maximumListRows"]};',
        f'pub const MAX_OPTIMIZER_DOCUMENT_BYTES: usize = {optimizer_proposals["maximumDocumentBytes"]};',
        f'pub const MAX_OPTIMIZER_RESPONSE_BYTES: usize = {optimizer_proposals["maximumResponseBytes"]};',
        "pub const MAX_PROVIDER_TENANT_LABEL_CHARACTERS: usize = "
        f"{providers['maximumTenantLabelCharacters']};",
        f'pub const MODEL_ROUTING_AUTHORITY_NAMESPACE: &str = "{model_routing["authorityNamespace"]}";',
        "pub const MODEL_ROUTING_AUTHORITY_METADATA_NAMESPACE: &str = "
        f'"{model_routing["authorityMetadataNamespace"]}";',
        f'pub const MODEL_ROUTING_BACKUP_NAMESPACE: &str = "{model_routing["backupNamespace"]}";',
        "pub const MODEL_ROUTING_MIGRATION_RECEIPT_NAMESPACE: &str = "
        f'"{model_routing["migrationReceiptNamespace"]}";',
        f'pub const MODEL_ROUTING_SECRET_NAMESPACE: &str = "{model_routing["secretNamespace"]}";',
        f'pub const MODEL_ROUTING_SECRET_COUNT_NAMESPACE: &str = "{model_routing["secretCountNamespace"]}";',
        "pub const MODEL_ROUTING_SECRET_REFERENCE_INDEX_NAMESPACE: &str =\n"
        f'    "{model_routing["secretReferenceIndexNamespace"]}";',
        "pub const MODEL_ROUTING_SECRET_UPDATED_INDEX_NAMESPACE: &str = "
        f'"{model_routing["secretUpdatedIndexNamespace"]}";',
        "pub const MAX_MODEL_ROUTING_DOCUMENT_BYTES: usize = "
        f"{model_routing['maximumDocumentBytes']};",
        "pub const MAX_MODEL_ROUTING_SECRET_REFERENCE_CHARACTERS: usize = "
        f"{model_routing['maximumSecretReferenceCharacters']};",
        "pub const MAX_MODEL_ROUTING_CIPHERTEXT_CHARACTERS: usize = "
        f"{model_routing['maximumCiphertextCharacters']};",
        "pub const MAX_MODEL_ROUTING_KEY_HINT_CHARACTERS: usize = "
        f"{model_routing['maximumKeyHintCharacters']};",
        "pub const MAX_MODEL_ROUTING_PRUNED_PER_COMMAND: usize = "
        f"{model_routing['maximumPrunedPerCommand']};",
        "pub const MAX_MODEL_ROUTING_TIMESTAMP_SECONDS: f64 = "
        f"{model_routing['maximumTimestampSeconds']}.0;",
        f'pub const WORKER_JOB_STATES: &[&str] = &{json.dumps(_worker_lifecycle()["states"])};',
        *(f'pub const AUTOMATION_{name.upper()}_NAMESPACE: &str = {json.dumps(value)};'
          for name,value in sorted(_automation_namespaces().items())),
        *(f'pub const MAX_AUTOMATION_{name}: {kind} = {json.loads((ROOT / "contracts/automation_v1.schema.json").read_text())["x-limits"][key]};'
          for name,kind,key in (("RECORD_DEPTH","usize","recordDepth"),("RECORD_NODES","usize","recordNodes"),
                                ("RECORD_BYTES","usize","recordBytes"),("SAFE_INTEGER","u64","safeIntegerMax"))),
        f'pub const MAX_AUTOMATION_STEPS: usize = {json.loads((ROOT / "contracts/automation_v1.schema.json").read_text())["properties"]["steps"]["maxItems"]};',
        f'pub const MAX_AUTOMATION_DEFINITION_BYTES: usize = {json.loads((ROOT / "contracts/automation_v1.schema.json").read_text())["x-limits"]["definitionBytes"]};',
        f'pub const MAX_AUTOMATION_PUBLICATION_REQUEST_BYTES: usize = {json.loads((ROOT / "contracts/automation_storage_v1.schema.json").read_text())["x-limits"]["publicationRequestBytes"]};',
        f'pub const AUTOMATION_PUBLICATION_METADATA_RESERVE_BYTES: usize = {json.loads((ROOT / "contracts/automation_storage_v1.schema.json").read_text())["x-limits"]["publicationMetadataReserveBytes"]};',
        f'pub const AUTOMATION_RESERVED_WORKER_TASK_KINDS: &[&str] = &{json.dumps(sorted(json.loads((ROOT / "contracts/automation_storage_v1.schema.json").read_text())["x-reservedTaskKinds"]))};',
        f'pub const MAX_AUTOMATION_CLOCK_FAILURE_KIND_CHARACTERS: usize = {json.loads((ROOT / "contracts/automation_storage_v1.schema.json").read_text())["x-clock"]["supersession"]["failureKindCharacters"]};',
        f'pub const AUTOMATION_CLOCK_JOB_CONTRACT: &str = {json.dumps(json.loads((ROOT / "contracts/automation_storage_v1.schema.json").read_text())["$defs"]["clockJob"]["properties"]["contract"]["const"])};',
        f'pub const AUTOMATION_EVENT_JOB_CONTRACT: &str = {json.dumps(json.loads((ROOT / "contracts/automation_storage_v1.schema.json").read_text())["$defs"]["taskStartedJob"]["properties"]["contract"]["const"])};',
        f'pub const MAX_WORKER_JOB_LEASE_AUTHORITY_EPOCH_CHARACTERS: usize = {_worker_lifecycle()["leaseAuthority"]["epochCharacters"]};',
        f'pub const WORKER_LEASE_INDEX_MARKER: u8 = {_worker_lifecycle()["leaseAuthority"]["leaseIndexMarker"]};',
        f'pub const AUTOMATION_EVENT_JOB_PRIORITY: u16 = {json.loads((ROOT / "contracts/automation_storage_v1.schema.json").read_text())["x-event"]["priority"]};',
        *(f'pub const MAX_AUTOMATION_EVENT_{name}: usize = {json.loads((ROOT / "contracts/automation_storage_v1.schema.json").read_text())["$defs"]["taskStartedContext"]["properties"][prop][field]};'
          for name,prop,field in (("TENANT_CHARACTERS","tenantId","maxLength"),("PROJECT_CHARACTERS","projectId","maxLength"),("ANCESTORS","automationChain","maxItems"))),
        f'pub const MAX_AUTOMATION_EVENT_ANCESTOR_CHARACTERS: usize = {json.loads((ROOT / "contracts/automation_storage_v1.schema.json").read_text())["$defs"]["taskStartedContext"]["properties"]["automationChain"]["items"]["maxLength"]};',
        *(f'pub const MAX_AUTOMATION_EVENT_{name}: usize = {json.loads((ROOT / "contracts/automation_storage_v1.schema.json").read_text())["x-event"][field]};'
          for name,field in (("BATCH_ROWS","batchRows"),("REQUEST_BYTES","settlementRequestBytes"))),
        f'pub const MAX_AUTOMATION_EVENT_ERROR_CHARACTERS: usize = {json.loads((ROOT / "contracts/automation_storage_v1.schema.json").read_text())["$defs"]["eventSettle"]["properties"]["error_kind"]["maxLength"]};',
        f'pub const AUTOMATION_PROJECT_CONTRACT: &str = {json.dumps(json.loads((ROOT / "contracts/automation_storage_v1.schema.json").read_text())["x-project"]["contract"])};',
        *(f'pub const AUTOMATION_SKILL_SOURCE_{name}: &str = {json.dumps(json.loads((ROOT / "contracts/automation_storage_v1.schema.json").read_text())["x-skillSource"][field])};'
          for name,field in (("CONTRACT","contract"),("ID","id"),("CREDENTIAL_SCOPE_DOMAIN","credentialScopeDomain"))),
        f'pub const AUTOMATION_SKILL_SOURCE_ENVIRONMENTS: &[&str] = &{json.dumps(list(json.loads((ROOT / "contracts/automation_skill_sources_v1.json").read_text())["environments"]))};',
        f'pub const MAX_AUTOMATION_PROJECT_ROOT_CHARACTERS: usize = {json.loads((ROOT / "contracts/automation_storage_v1.schema.json").read_text())["x-project"]["rootCharacters"]};',
        f'pub const MAX_AUTOMATION_PROJECT_LABEL_CHARACTERS: usize = {json.loads((ROOT / "contracts/automation_storage_v1.schema.json").read_text())["x-project"]["labelCharacters"]};',
        f'pub const MAX_AUTOMATION_EVENT_CLOCK_SKEW_MS: u64 = {json.loads((ROOT / "contracts/automation_storage_v1.schema.json").read_text())["x-limits"]["clockSkewMs"]};',
        f'pub const MAX_AUTOMATION_TRIGGER_CHAIN: usize = {json.loads((ROOT / "contracts/automation_v1.schema.json").read_text())["x-limits"]["triggerChain"]};',
        f'pub const AUTOMATION_ACTION_CATALOG_JSON: &str = {json.dumps(_automation_catalog_json(), ensure_ascii=False)};',
        f'pub const AUTOMATION_TERMINAL_RUN_STATES: &[&str] = &{json.dumps(_automation_runtime()["terminalRunStates"])};',
        f'pub const AUTOMATION_ACTIVE_EFFECT_STEP_STATES: &[&str] = &{json.dumps(_automation_runtime()["activeEffectStepStates"])};',
        f'pub const AUTOMATION_CANCELLABLE_PENDING_STEP_STATES: &[&str] = &{json.dumps(_automation_runtime()["cancellablePendingStepStates"])};',
        f'pub const WORKER_JOB_DOCUMENT_NAMESPACE: &str = "{worker_jobs["physicalNamespace"]}";',
        f'pub const WORKER_JOB_IDEMPOTENCY_NAMESPACE: &str = "{worker_jobs["idempotencyNamespace"]}";',
        f'pub const WORKER_JOB_QUEUED_INDEX_NAMESPACE: &str = "{worker_jobs["queuedIndexNamespace"]}";',
        f'pub const WORKER_JOB_QUEUED_SUMMARY_NAMESPACE: &str = "{worker_jobs["queuedSummaryNamespace"]}";',
        f'pub const WORKER_JOB_LEASE_INDEX_NAMESPACE: &str = "{worker_jobs["leaseIndexNamespace"]}";',
        "pub const MAX_WORKER_JOB_PAYLOAD_BYTES: usize = "
        f"{worker_jobs['maximumPayloadBytes']};",
        "pub const MAX_WORKER_JOB_ERROR_BYTES: usize = "
        f"{worker_jobs['maximumErrorBytes']};",
        "pub const MAX_WORKER_JOB_CLAIM_KINDS: usize = "
        f"{worker_jobs['maximumClaimTaskKinds']};",
        "pub const MAX_WORKER_JOB_PRIORITY: usize = "
        f"{worker_jobs['maximumPriority']};",
        "pub const MAX_WORKER_JOB_CLOCK_MILLISECONDS: u64 = "
        f"{worker_jobs['maximumClockMilliseconds']};",
        'pub const MAX_WORKER_JOB_LEASE_NOW_MILLISECONDS: u64 = '
        f'{worker_jobs["maximumClockMilliseconds"] - _worker_lifecycle()["leaseAuthority"]["maximumLeaseMilliseconds"]};',
        "pub const MAX_WORKER_JOB_FUTURE_CLOCK_SKEW_MILLISECONDS: u64 = "
        f"{worker_jobs['maximumFutureClockSkewMilliseconds']};",
        f'pub const COMPACTION_ARCHIVE_DOCUMENT_NAMESPACE: &str = "{archives["physicalNamespace"]}";',
        f'pub const COMPACTION_ARCHIVE_MESSAGES_NAMESPACE: &str = "{archives["messagesNamespace"]}";',
        f'pub const COMPACTION_ARCHIVE_POST_MESSAGES_NAMESPACE: &str = "{archives["postMessagesNamespace"]}";',
        f'pub const COMPACTION_ARCHIVE_SUMMARY_NAMESPACE: &str = "{archives["summaryNamespace"]}";',
        f'pub const COMPACTION_ARCHIVE_RECEIPT_NAMESPACE: &str = "{archives["receiptNamespace"]}";',
        "pub const COMPACTION_ARCHIVE_CONVERSATION_INDEX_NAMESPACE: &str =\n"
        f'    "{archives["conversationIndexNamespace"]}";',
        f'pub const COMPACTION_ARCHIVE_COUNT_NAMESPACE: &str = "{archives["conversationCountNamespace"]}";',
        f'pub const COMPACTION_ARCHIVE_ID_CLAIM_NAMESPACE: &str = "{archives["tenantGlobalClaimNamespace"]}";',
        f"pub const MAX_COMPACTION_ARCHIVE_DOCUMENT_BYTES: usize = {archives['maximumDocumentBytes']};",
        "pub const MAX_COMPACTION_ARCHIVES_PER_CONVERSATION: usize = "
        f"{archives['maximumArchivesPerConversation']};",
        f'pub const CONVERSATION_DOCUMENT_NAMESPACE: &str = "{conversations["physicalNamespace"]}";',
        f'pub const CONVERSATION_ID_CLAIM_NAMESPACE: &str = "{conversations["tenantGlobalClaimNamespace"]}";',
        f'pub const CONVERSATION_COUNT_NAMESPACE: &str = "{conversations["ownerCountNamespace"]}";',
        f'pub const CONVERSATION_SEARCH_DIRTY_NAMESPACE: &str = "{conversations["searchDirtyNamespace"]}";',
        f'pub const CONVERSATION_UPDATED_INDEX_NAMESPACE: &str = "{conversations["updatedIndexNamespace"]}";',
        "pub const CONVERSATION_ACTIVITY_CANDIDATE_INDEX_NAMESPACE: &str =\n"
        f'    "{conversations["activityCandidateIndexNamespace"]}";',
        "pub const CONVERSATION_ACTIVITY_CANDIDATE_STATE_NAMESPACE: &str =\n"
        f'    "{conversations["activityCandidateStateNamespace"]}";',
        "pub const MAX_ACTIVITY_CANDIDATE_BACKFILL_ROWS_PER_TRANSACTION: usize = "
        f'{conversations["maximumActivityCandidateBackfillRowsPerTransaction"]};',
        "pub const MAX_ACTIVITY_CANDIDATE_BACKFILL_SOURCE_BYTES_PER_TRANSACTION: usize = "
        f'{conversations["maximumActivityCandidateBackfillSourceBytesPerTransaction"]};',
        f'pub const CONVERSATION_EXECUTION_EPOCH_NAMESPACE: &str = "{conversations["executionEpochNamespace"]}";',
        f'pub const CONVERSATION_TRASH_METADATA_NAMESPACE: &str = "{conversations["trashMetadataNamespace"]}";',
        f'pub const CONVERSATION_TRASH_AGE_INDEX_NAMESPACE: &str = "{conversations["trashAgeIndexNamespace"]}";',
        "pub const CONVERSATION_TRASH_SETTINGS_OVERLAY_NAMESPACE: &str =\n"
        f'    "{conversations["trashSettingsOverlayNamespace"]}";',
        "pub const CONVERSATION_RECENCY_DIRECTORY_NAMESPACE: &str =\n"
        f'    "{conversations["recencyDirectoryNamespace"]}";',
        "pub const CONVERSATION_RECENCY_DIRECTORY_STATE_NAMESPACE: &str =\n"
        f'    "{conversations["recencyDirectoryStateNamespace"]}";',
        "pub const MAX_RECENCY_DIRECTORY_ENTRIES: usize = "
        f'{conversations["maximumRecencyDirectoryEntries"]};',
        "pub const MAX_RECENCY_DIRECTORY_BACKFILL_ROWS_PER_TRANSACTION: usize = "
        f'{conversations["maximumRecencyDirectoryBackfillRowsPerTransaction"]};',
        "pub const MAX_PROJECT_RELINK_CONVERSATION_CANDIDATES: usize = "
        f"{conversations['maximumProjectRelinkConversationCandidates']};",
        f'pub const TURN_DOCUMENT_NAMESPACE: &str = "{turns["physicalNamespace"]}";',
        f'pub const TURN_LANE_INDEX_NAMESPACE: &str = "{turns["laneIndexNamespace"]}";',
        f'pub const TURN_ACTIVITY_INDEX_NAMESPACE: &str = "{turns["activityIndexNamespace"]}";',
        "pub const MAX_ACTIVITY_TURN_ROWS_PER_QUERY: usize = "
        f"{turns['maximumActivityTurnRowsPerQuery']};",
        f'pub const TURN_LANE_HEAD_NAMESPACE: &str = "{turns["laneHeadNamespace"]}";',
        "pub const TURN_LANE_LIVE_ATTEMPT_NAMESPACE: &str = "
        f'"{turns["laneLiveAttemptNamespace"]}";',
        f'pub const TURN_LANE_COUNT_NAMESPACE: &str = "{turns["laneCountNamespace"]}";',
        f'pub const TURN_SEARCH_DIRTY_NAMESPACE: &str = "{turns["searchDirtyNamespace"]}";',
        f'pub const CONVERSATION_SYNC_HEAD_NAMESPACE: &str = "{turns["syncHeadNamespace"]}";',
        f'pub const CONVERSATION_SYNC_EVENT_NAMESPACE: &str = "{turns["syncEventNamespace"]}";',
        "pub const CONVERSATION_SYNC_AGE_INDEX_NAMESPACE: &str = "
        f'"{turns["syncAgeIndexNamespace"]}";',
        "pub const ATTEMPT_EVENT_SYNC_REFERENCE_NAMESPACE: &str = "
        f'"{turns["attemptEventSyncReferenceNamespace"]}";',
        "pub const MAX_SYNC_PRUNE_ROWS_PER_TRANSACTION: usize = "
        f'{turns["maximumSyncPruneRowsPerTransaction"]};',
        "pub const MAX_SYNC_PRUNE_MATERIALIZED_BYTES: usize = "
        f'{turns["maximumSyncPruneMaterializedBytes"]};',
        f'pub const GENERATION_ATTEMPT_NAMESPACE: &str = "{turns["attemptNamespace"]}";',
        "pub const ATTEMPT_DISPATCHABLE_INDEX_NAMESPACE: &str = "
        f'"{turns["attemptDispatchableIndexNamespace"]}";',
        "pub const MAX_DISPATCHABLE_ATTEMPTS_PER_QUERY: usize = "
        f'{turns["maximumDispatchableAttemptsPerQuery"]};',
        "pub const TURN_LANE_COMPACTION_INDEX_NAMESPACE: &str = "
        f'"{turns["laneCompactionIndexNamespace"]}";',
        "pub const MAX_TURN_COMPACTION_METADATA_ROWS: usize = "
        f'{turns["maximumCompactionMetadataRows"]};',
        "pub const MAX_TURN_COMPACTION_PROJECTION_UPDATES: usize = "
        f'{turns["maximumCompactionProjectionUpdates"]};',
        "pub const MAX_TURN_COMPACTION_REPARENTED_TURNS: usize = "
        f'{turns["maximumCompactionReparentedTurns"]};',
        "pub const MAX_TURN_COMPACTION_PROJECTION_BYTES: usize = "
        f'{turns["maximumCompactionProjectionBytes"]};',
        "pub const MAX_TURN_COMPACTION_MATERIALIZED_BYTES: usize = "
        f'{turns["maximumCompactionMaterializedBytes"]};',
        "pub const ATTEMPT_RECOVERY_INDEX_NAMESPACE: &str = "
        f'"{turns["attemptRecoveryIndexNamespace"]}";',
        "pub const MAX_RECOVERY_ROWS_PER_TRANSACTION: usize = "
        f'{turns["maximumRecoveryRowsPerTransaction"]};',
        "pub const MAX_RECOVERY_INDEX_ROWS_PER_OWNER: usize = "
        f'{turns["maximumRecoveryIndexRowsPerOwner"]};',
        "pub const MAX_RECOVERY_PROJECTION_BYTES_PER_TRANSACTION: usize = "
        f'{turns["maximumRecoveryProjectionBytesPerTransaction"]};',
        "pub const ATTEMPT_TIMING_TASK_INDEX_NAMESPACE: &str = "
        f'"{turns["attemptTimingTaskIndexNamespace"]}";',
        "pub const ATTEMPT_TIMING_CONVERSATION_INDEX_NAMESPACE: &str =\n    "
        f'"{turns["attemptTimingConversationIndexNamespace"]}";',
        "pub const MAX_TIMING_TRACE_ROWS_PER_QUERY: usize = "
        f'{turns["maximumTimingTraceRowsPerQuery"]};',
        "pub const MAX_TIMING_TRACE_TASK_CANDIDATES: usize = "
        f'{turns["maximumTimingTraceTaskCandidates"]};',
        "pub const MAX_TIMING_TRACE_CLIENT_OBSERVATIONS: usize = "
        f'{turns["maximumTimingTraceClientObservations"]};',
        "pub const MAX_TIMING_TRACE_PERSISTED_BYTES: usize = "
        f'{turns["maximumTimingTracePersistedBytes"]};',
        "pub const MAX_TIMING_TRACE_COUNTER: u64 = "
        f'{turns["maximumTimingTraceCounter"]};',
        "pub const TURN_PROJECTION_HEAD_NAMESPACE: &str = "
        f'"{turns["projectionHeadNamespace"]}";',
        "pub const MAX_TURN_PROJECTION_INLINE_LIVE_BYTES: usize = "
        f'{turns["maximumTurnProjectionInlineLiveBytes"]};',
        "pub const MAX_TURN_PROJECTION_HEAD_PATCHES: usize = "
        f'{turns["maximumTurnProjectionHeadPatches"]};',
        "pub const MAX_TURN_PROJECTION_TEXT_PAGE_BYTES: usize = "
        f'{turns["maximumTurnProjectionTextPageBytes"]};',
        "pub const MAX_TURN_PROJECTION_TEXT_APPEND_BYTES: usize = "
        f'{turns["maximumTurnProjectionTextAppendBytes"]};',
        "pub const MAX_TURN_PROJECTION_PATCH_BYTES: usize = "
        f'{turns["maximumTurnProjectionPatchBytes"]};',
        "pub const MAX_TURN_PROJECTION_WIRE_PATCH_BYTES: usize = "
        f'{turns["maximumTurnProjectionWirePatchBytes"]};',
        "pub const MAX_TURN_PROJECTION_PATCH_DEPTH: usize = "
        f'{turns["maximumTurnProjectionPatchDepth"]};',
        "pub const MAX_TURN_PROJECTION_PATCH_OPERATIONS: usize = "
        f'{turns["maximumTurnProjectionPatchOperations"]};',
        "pub const MAX_LEGACY_TURN_IMAGES: usize = "
        f'{turns["maximumLegacyTurnImages"]};',
        "pub const MAX_LEGACY_TURN_IMAGE_BYTES: usize = "
        f'{turns["maximumLegacyTurnImageBytes"]};',
        f'pub const ATTEMPT_COMMAND_NAMESPACE: &str = "{turns["attemptCommandNamespace"]}";',
        f'pub const ATTEMPT_TURN_COUNT_NAMESPACE: &str = "{turns["attemptTurnCountNamespace"]}";',
        f'pub const ATTEMPT_TURN_DIRECTORY_NAMESPACE: &str = "{turns["attemptTurnDirectoryNamespace"]}";',
        "pub const MAX_ATTEMPTS_PER_TURN: usize = "
        f'{turns["maximumAttemptsPerTurn"]};',
        "pub const MAX_ATTEMPT_TURN_DIRECTORY_BYTES: usize = "
        f'{turns["maximumAttemptTurnDirectoryBytes"]};',
        "pub const MAX_ATTEMPT_TOMBSTONE_BYTES: usize = "
        f'{turns["maximumAttemptTombstoneBytes"]};',
        f'pub const ATTEMPT_EVENT_HEAD_NAMESPACE: &str = "{turns["attemptEventHeadNamespace"]}";',
        f'pub const ATTEMPT_EVENT_NAMESPACE: &str = "{turns["attemptEventNamespace"]}";',
        f'pub const MAX_ATTEMPT_EVENT_DOCUMENT_BYTES: usize = {turns["maximumAttemptEventDocumentBytes"]};',
        f'pub const MAX_ATTEMPT_EVENT_PAGE_BYTES: usize = {turns["maximumAttemptEventPageBytes"]};',
        "pub const ATTEMPT_EVENT_RETENTION_INDEX_NAMESPACE: &str = "
        f'"{turns["attemptEventRetentionIndexNamespace"]}";',
        "pub const MAX_ATTEMPT_EVENT_PRUNE_ATTEMPTS_PER_TRANSACTION: usize = "
        f'{turns["maximumAttemptEventPruneAttemptsPerTransaction"]};',
        "pub const MAX_ATTEMPT_EVENT_PRUNE_ROWS_PER_TRANSACTION: usize = "
        f'{turns["maximumAttemptEventPruneRowsPerTransaction"]};',
        "pub const MAX_ATTEMPT_EVENT_PRUNE_MATERIALIZED_BYTES: usize = "
        f'{turns["maximumAttemptEventPruneMaterializedBytes"]};',
        f'pub const TURN_ID_CLAIM_NAMESPACE: &str = "{turns["turnIdClaimNamespace"]}";',
        f'pub const ATTEMPT_ID_CLAIM_NAMESPACE: &str = "{turns["attemptIdClaimNamespace"]}";',
        "pub const ATTEMPT_CLAIM_LOCATOR_VERSION: u32 = "
        f"{turns['attemptClaimLocatorVersion']};",
        f'pub const TURN_UPDATED_INDEX_NAMESPACE: &str = "{turns["updatedIndexNamespace"]}";',
        f'pub const TURN_TOMBSTONE_NAMESPACE: &str = "{turns["tombstoneNamespace"]}";',
        f'pub const TURN_TOMBSTONE_AGE_INDEX_NAMESPACE: &str = "{turns["tombstoneAgeIndexNamespace"]}";',
        "pub const ATTEMPT_SUPERSEDED_INDEX_NAMESPACE: &str = "
        f'"{turns["supersededAttemptIndexNamespace"]}";',
        "pub const MAX_CLEANUP_ATTEMPTS_PER_TRANSACTION: usize = "
        f'{turns["maximumCleanupAttemptsPerTransaction"]};',
        "pub const MAX_CLEANUP_INDEX_SCAN_ROWS_PER_TRANSACTION: usize = "
        f'{turns["maximumCleanupIndexScanRowsPerTransaction"]};',
        "pub const MAX_DESKTOP_AGENT_ID_CHARACTERS: usize = "
        f"{agent_field['maximumCharacters']};",
        "pub static EXECUTABLE_STORAGE_OPERATIONS: &[&str] = &[",
    ]
    lines.extend(
        f'    "{operation["name"]}",' for operation in document["executableOperations"]
    )
    lines.extend(["];"])
    trash = document["conversationTrashMigration"]
    lines.append(f'pub const MIGRATION_TRASH_STAGE_NAMESPACE: &str = "{trash["stageNamespace"]}";')
    lines.append(f'pub const MAX_MIGRATION_TRASH_TURNS: usize = {trash["maximumTurnsPerCapsule"]};')
    lines.append(f'pub const MAX_MIGRATION_TRASH_STAGE_BYTES: usize = {trash["maximumStageBytes"]};')
    stores = document["migrationStores"]
    lines.append("pub const BROWSER_OBSERVATION_OWNER_TOUCH_NAMESPACE: &str =\n"
                 f'    "{stores["browserObservationOwnerTouchNamespace"]}";')
    lines.append("pub const PLUGIN_ROW_DOCUMENT_NAMESPACE: &str = "
                 f'"{stores["pluginRowDocumentNamespace"]}";')
    lines.append("pub const PLUGIN_ROW_UNIQUE_NAMESPACE: &str = "
                 f'"{stores["pluginRowUniqueNamespace"]}";')
    lines.append("pub const MAX_PLUGIN_ROW_DOCUMENT_BYTES: usize = "
                 f'{stores["maximumPluginRowDocumentBytes"]};')
    lines.append("pub const MAX_PLUGIN_ROW_FIELDS: usize = "
                 f'{stores["maximumPluginRowFields"]};')
    lines.append("pub const MAX_PLUGIN_UNIQUE_VALUES_PER_ROW: usize = "
                 f'{stores["maximumPluginUniqueValuesPerRow"]};')
    lines.append("pub const MAX_MIGRATION_DOCUMENT_BYTES: usize = "
                 f'{document["offlineMigration"]["documentImport"]["maximumJSONBytes"]};')
    lines.append("pub const MAX_MIGRATION_TASK_RESULT_DOCUMENT_BYTES: usize = "
                 f'{document["offlineMigration"]["documentImport"]["taskResultMaximumJSONBytes"]};')
    operator = document["offlineMigration"]["operatorSession"]
    lines.append(f'pub const MIGRATION_SESSION_FORMAT: &str = "{operator["format"]}";')
    lines.append(f'pub const MIGRATION_SESSION_MAX_FRAME_BYTES: usize = {operator["maximumFrameBytes"]};')
    lines.append(f'pub const MIGRATION_SESSION_MAX_PAGE_ROWS: usize = {operator["maximumCatalogPageRows"]};')
    lines.append(f'pub const MAX_FROZEN_TRANSCRIPT_SOURCE_BYTES: usize = {document["offlineMigration"]["maximumFrozenTranscriptSourceBytes"]};')
    for name, field in [("MAX_TURN_SNAPSHOT_SOURCE_BYTES", "maximumSourceBytes"),
                        ("MAX_TURN_SNAPSHOT_BYTES", "maximumSnapshotBytes"),
                        ("MAX_TURN_RESPONSE_BYTES", "maximumResponseBytes")]:
        lines.append(f'pub const {name}: usize = {document["turnSnapshots"][field]};')
    frozen = document["frozenTranscripts"]
    for name, field in [("FROZEN_TRANSCRIPT_MANIFEST_NAMESPACE", "manifestNamespace"),
                        ("FROZEN_TRANSCRIPT_PAGE_NAMESPACE", "pageNamespace")]:
        lines.append(f'pub const {name}: &str = "{frozen[field]}";')
    archive = document["transcriptArchives"]
    for name, field in [("TRANSCRIPT_ARCHIVE_MANIFEST_NAMESPACE", "manifestNamespace"),
                        ("TRANSCRIPT_ARCHIVE_PAGE_NAMESPACE", "pageNamespace")]:
        lines.append(f'pub const {name}: &str = "{archive[field]}";')
    for name, field in [("MAX_TRANSCRIPT_ARCHIVE_PAGES", "maximumPages"),
                        ("MAX_TRANSCRIPT_ARCHIVE_MESSAGES", "maximumMessages"),
                        ("TRANSCRIPT_ARCHIVE_TAIL_TARGET_BYTES", "tailTargetBytes"),
                        ("MAX_TRANSCRIPT_ARCHIVE_DECODED_BYTES", "maximumDecodedBytes")]:
        lines.append(f'pub const {name}: usize = {archive[field]};')
    for name, field in [("MAX_FROZEN_TRANSCRIPT_PAGES", "maximumPages"),
                        ("MAX_FROZEN_TRANSCRIPT_MESSAGES", "maximumMessages"),
                        ("MAX_FROZEN_TRANSCRIPT_PAGE_BYTES", "maximumPageBytes"),
                        ("MAX_FROZEN_TRANSCRIPT_DECODED_BYTES", "maximumDecodedBytes")]:
        lines.append(f'pub const {name}: usize = {frozen[field]};')
    lines.append(f'pub const MIGRATION_SESSION_MAX_SOURCE_CHUNK_BYTES: usize = {document["offlineMigration"]["maximumSourceChunkBytes"]};')
    lines.append(f'pub const MIGRATION_SESSION_MAX_PAGE_BYTES: usize = {operator["maximumCatalogPageBytes"]};')
    legacy_receipts = document["offlineMigration"]["legacyReceiptImport"]
    lines.append(f'pub const LEGACY_RECEIPT_OWNER_ID: u64 = {legacy_receipts["reservedOwnerId"]};')
    lines.append(f'pub const MAX_LEGACY_RECEIPT_ROWS: u64 = {legacy_receipts["maximumRows"]};')
    lines.append(f'pub const MAX_LEGACY_RECEIPT_STORED_BYTES: u64 = {legacy_receipts["maximumStoredBytes"]};')
    for name, key in [("MIGRATION_SESSION_SEMANTIC_OPERATIONS", "semanticOperations"),
                      ("MIGRATION_SESSION_EXCLUDED_NAMESPACES", "excludedRecordNamespaces"),
                      ("MIGRATION_SESSION_BILLING_OPERATIONS", "billingOperations"),
                      ("MIGRATION_SESSION_COMMAND_RECEIPT_OPERATIONS", "commandReceiptOperations"),
                      ("MIGRATION_SESSION_TENANT_USER_OPERATIONS", "tenantUserOperations"),
                      ("MIGRATION_SESSION_TASK_RESULT_OPERATIONS", "taskResultOperations"),
                      ("MIGRATION_SESSION_TURN_OPERATIONS", "turnOperations"),
                      ("MIGRATION_SESSION_CONVERSATION_OPERATIONS", "conversationOperations"),
                      ("MIGRATION_SESSION_TRASH_OPERATIONS", "trashOperations"),
                      ("MIGRATION_SESSION_ATTEMPT_OPERATIONS", "attemptOperations"),
                      ("MIGRATION_SESSION_ATTEMPT_EVENT_OPERATIONS", "attemptEventOperations"),
                      ("MIGRATION_SESSION_ATTEMPT_EVENT_UPLOAD_OPERATIONS", "attemptEventUploadOperations"),
                      ("MIGRATION_SESSION_STORAGE_EVENT_OPERATIONS", "storageEventOperations"),
                      ("MIGRATION_SESSION_CONVERSATION_SYNC_EVENT_OPERATIONS", "conversationSyncEventOperations"),
                      ("MIGRATION_SESSION_CONVERSATION_SYNC_HEAD_OPERATIONS", "conversationSyncHeadOperations"),
                      ("MIGRATION_SESSION_CONTINUATION_OPERATIONS", "continuationOperations"),
                      ("MIGRATION_SESSION_PAPER_BUILD_BUDGET_OPERATIONS", "paperBuildBudgetOperations"),
                      ("MIGRATION_SESSION_PAPER_DOCUMENT_BUDGET_OPERATIONS", "paperDocumentBudgetOperations"),
                      ("MIGRATION_SESSION_DESKTOP_EGRESS_OPERATIONS", "desktopEgressOperations"),
                      ("MIGRATION_SESSION_DAILY_COST_OPERATIONS", "dailyCostOperations"),
                      ("MIGRATION_SESSION_PAPER_LIBRARY_OPERATIONS", "paperLibraryOperations"),
                      ("MIGRATION_SESSION_PAPER_REPORT_OPERATIONS", "paperReportOperations"),
                      ("MIGRATION_SESSION_PAPER_TRANSLATION_OPERATIONS", "paperTranslationOperations"),
                      ("MIGRATION_SESSION_PAPER_NOTE_OPERATIONS", "paperNoteOperations"),
                      ("MIGRATION_SESSION_PAPER_PODCAST_OPERATIONS", "paperPodcastOperations"),
                      ("MIGRATION_SESSION_KNOWLEDGE_OPERATIONS", "knowledgeOperations"),
                      ("MIGRATION_SESSION_CHAT_ARTIFACT_OPERATIONS", "chatArtifactOperations"),
                      ("MIGRATION_SESSION_TOOL_RESULT_OPERATIONS", "toolResultOperations"),
                      ("MIGRATION_SESSION_BROWSER_OBSERVATION_OPERATIONS", "browserObservationOperations"),
                      ("MIGRATION_SESSION_MODEL_ROUTING_AUTHORITY_OPERATIONS", "modelRoutingAuthorityOperations"),
                      ("MIGRATION_SESSION_MODEL_ROUTING_SECRET_OPERATIONS", "modelRoutingSecretOperations"),
                      ("MIGRATION_SESSION_PLUGIN_MANIFEST_OPERATIONS", "pluginManifestOperations"),
                      ("MIGRATION_SESSION_PLUGIN_ROWS_OPERATIONS", "pluginRowsOperations"),
                      ("MIGRATION_SESSION_PLUGIN_UNIQUE_OPERATIONS", "pluginUniqueOperations"),
                      ("MIGRATION_SESSION_PROJECT_BRAIN_OPERATIONS", "projectBrainOperations"),
                      ("MIGRATION_SESSION_OPTIMIZER_OPERATIONS", "optimizerOperations"),
                      ("MIGRATION_SESSION_LOG_AGGREGATE_OPERATIONS", "logAggregateOperations"),
                      ("MIGRATION_SESSION_BILLING_PAYMENT_OPERATIONS", "billingPaymentOperations"),
                      ("MIGRATION_SESSION_BILLING_REDEEM_OPERATIONS", "billingRedeemOperations"),
                      ("MIGRATION_SESSION_RATE_LIMIT_OPERATIONS", "rateLimitEventOperations"),
                      ("MIGRATION_SESSION_ORCHESTRATION_DEFINITION_OPERATIONS", "orchestrationDefinitionOperations"),
                      ("MIGRATION_SESSION_ORCHESTRATION_RUN_OPERATIONS", "orchestrationRunOperations"),
                      ("MIGRATION_SESSION_ORCHESTRATION_EVENT_OPERATIONS", "orchestrationEventOperations"),
                      ("MIGRATION_SESSION_SWARM_SESSION_OPERATIONS", "swarmSessionOperations"),
                      ("MIGRATION_SESSION_SWARM_AGENT_OPERATIONS", "swarmAgentOperations"),
                      ("MIGRATION_SESSION_INTEGRATION_WORKSPACE_OPERATIONS", "integrationWorkspaceOperations"),
                      ("MIGRATION_SESSION_INTEGRATION_EVENT_OPERATIONS", "integrationEventOperations"),
                      ("MIGRATION_SESSION_INTEGRATION_META_OPERATIONS", "integrationMetaOperations"),
                      ("MIGRATION_SESSION_QUEUE_ITEM_OPERATIONS", "queueItemOperations"),
                      ("MIGRATION_SESSION_QUEUE_AUTOPILOT_MARKER_OPERATIONS", "queueAutopilotMarkerOperations"),
                      ("MIGRATION_SESSION_TIMER_OPERATIONS", "timerOperations"),
                      ("MIGRATION_SESSION_TIMER_POLL_OPERATIONS", "timerPollOperations"),
                      ("MIGRATION_SESSION_SCHEDULER_TASK_OPERATIONS", "schedulerTaskOperations"),
                      ("MIGRATION_SESSION_SCHEDULER_POLL_OPERATIONS", "schedulerPollOperations"),
                      ("MIGRATION_SESSION_WORKER_JOB_OPERATIONS", "workerJobOperations"),
                      ("MIGRATION_SESSION_FROZEN_TRANSCRIPT_OPERATIONS", "frozenTranscriptOperations"),
                      ("MIGRATION_SESSION_FROZEN_TRANSCRIPT_UPLOAD_OPERATIONS", "frozenTranscriptUploadOperations"),
                      ("MIGRATION_SESSION_RAW_ARCHIVE_OPERATIONS", "rawArchiveOperations"),
                      ("MIGRATION_SESSION_COMPACTION_ARCHIVE_OPERATIONS", "compactionArchiveOperations"),
                      ("MIGRATION_SESSION_EXCLUDED_PREFIXES", "excludedRecordPrefixes")]:
        prefix = f'pub const {name}: &[&str] ='
        literal = f'&{json.dumps(operator[key])};'
        if len(prefix) + 1 + len(literal) <= 100:
            lines.append(f'{prefix} {literal}')
        elif len(operator[key]) == 1 and len(literal) <= 96:
            # rustfmt-canonical two-line layout for a short array literal:
            # `cargo fmt --check` and this generator's exact-text --check
            # must agree.
            lines.append(prefix)
            lines.append(f'    {literal}')
        else:
            # rustfmt-canonical multi-line array layout: `cargo fmt --check`
            # and this generator's exact-text --check must agree.
            lines.append(f'{prefix} &[')
            lines.extend(f'    "{operation}",' for operation in operator[key])
            lines.append('];')
    return "\n".join(lines) + "\n"



def _render_paper_build() -> str:
    """Generate the bounded receipt vocabulary from the public build contract."""
    document = json.loads((ROOT / "contracts/paper_build_v1.schema.json").read_text())
    names = ("PaperBuildReceiptV1", "PaperBuildArtifactV1", "PaperBuildDiagnosticV1",
             "PaperDocumentV1", "PaperLocalDocumentV1", "PaperOverleafDocumentV1",
             "PaperStoredSourceV1", "PaperResearchSourceV1",
             "PaperReadingStateV1", "PaperReadingStateRequestV1")
    definitions = {name: document["$defs"][name] for name in names}
    keywords = {"type", "required", "properties", "additionalProperties", "enum", "const",
                "minimum", "maximum", "maxLength", "minLength", "maxItems", "items", "$ref",
                "anyOf", "allOf", "if", "then", "pattern"}
    patterns = {"^[0-9a-f]{24}$", "^[0-9a-f]{32}$", "^[0-9a-f]{64}$", "^[a-zA-Z0-9_-]{1,128}$"}

    def check(schema, path=()):
        if not isinstance(schema, dict) or set(schema) - keywords or len(path) > 16:
            raise ValueError("native retained receipt schema vocabulary changed")
        if "pattern" in schema and schema["pattern"] not in patterns:
            raise ValueError("native retained receipt pattern changed")
        if "additionalProperties" in schema and schema["additionalProperties"] is not False:
            raise ValueError("native retained receipt must reject unknown properties")
        if "type" in schema:
            types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not types or set(types) - {"object", "string", "integer", "number", "boolean", "null", "array"}:
                raise ValueError("native retained receipt type changed")
        if "$ref" in schema:
            ref = schema["$ref"].removeprefix("#/$defs/")
            if ref not in definitions or ref in path:
                raise ValueError("native retained receipt reference changed")
            check(definitions[ref], (*path, ref))
        for value in schema.get("properties", {}).values():
            check(value, (*path, "property"))
        for key in ("items", "if", "then"):
            if key in schema:
                check(schema[key], (*path, key))
        for key in ("anyOf", "allOf"):
            for value in schema.get(key, []):
                check(value, (*path, key))
    for name, schema in definitions.items():
        check(schema, (name,))
    limits = document["x-resource-limits"]
    # Increases require re-certifying transaction, frame, receipt and memory bounds.
    certified = {"retained_chunk_bytes": 262144, "retained_receipt_bytes": 262144,
                 "retained_upload_seconds": 300, "retained_gc_chunks": 32,
                 "retained_gc_scan_rows": 128,
                 "build_output_bytes": 134217728, "artifact_bytes": 67108864,
                 "viewer_lease_seconds": 240,
                 "document_request_seconds": 3600, "discovery_candidates": 64,
                 "reading_state_bytes": 1024}
    if any(limits[key] != value for key, value in certified.items()):
        raise ValueError("native retained artifact resource bounds changed")
    policy = _load()["retainedPaperArtifacts"]
    if policy.get("chunkAdmission") != "strict-base64-once;immutable-canonical-data-and-digest;literal-budget-from-bounded-fields;no-validation-reserialization":
        raise ValueError("native retained artifact admission contract changed")
    if policy["budgetNamespace"] != "paper_build_budget":
        raise ValueError("native retained artifact budget namespace changed")
    lines = ['//! @generated by scripts/gen_tofudb_ir_contract.py; do not edit.',
             '//! Source: contracts/paper_build_v1.schema.json and tofudb_ir_v1.json.', '']
    for name, value in certified.items():
        lines.append(f'pub const {name.upper()}: usize = {value};')
    lines.append('pub const BUDGET_NAMESPACE: &str = "paper_build_budget";')
    if _load().get("savedPaperDocuments") != {'contract': 'paper_build_v1.schema.json', 'budgetNamespace': 'paper_document_budget', 'budgetScope': 'tenant-shared-usage-accounting;no-count-admission', 'physicalDocuments': 'storage_records;owner-bound-versioned-document-and-blob', 'identity': 'opaque-64-hex;python-canonical-source-binding-sha256', 'publication': 'requested-generation-first-terminal-receipt-and-retained-pin-in-one-authority-transaction', 'reading': 'revision-CAS;writer-base-revision-and-monotonic-sequence', 'reclamation': 'explicit-owner-lifecycle;never-delete-user-state', 'maximumDocumentJSONBytes': 65536, 'budgetAdmission': 'only-transactions-containing-saved-document-IR'}:
        raise ValueError("native saved paper document policy changed")
    lines.append('pub const DOCUMENT_BUDGET_NAMESPACE: &str = "paper_document_budget";')
    lines.append('pub const DOCUMENT_MAX_JSON_BYTES: usize = 65536;')
    encoded = json.dumps({"$defs": definitions}, separators=(",", ":"))
    lines.append('pub const RECEIPT_SCHEMA: &str = r####"' + encoded + '"####;')
    return "\n".join(lines) + "\n"

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    contract = _load()
    migration = contract["offlineMigration"]
    names = {
        "CHECKPOINT_FORMAT": "checkpointFormat",
        "MAX_CHECKPOINT_BYTES": "maximumCheckpointBytes",
        "MAX_CURSOR_BYTES": "maximumCursorBytes",
        "MAX_DOMAINS": "maximumDomains",
        "MAX_KEY_COLUMNS": "maximumKeyColumns",
        "MAX_SOURCE_ROW_BYTES": "maximumSourceRowBytes",
        "MAX_SOURCE_CHUNK_BYTES": "maximumSourceChunkBytes",
        "MAX_FROZEN_TRANSCRIPT_SOURCE_BYTES": "maximumFrozenTranscriptSourceBytes",
        "MAX_SOURCE_BATCH_ROWS": "maximumSourceBatchRows",
        "MAX_SOURCE_TABLES": "maximumSourceTables",
        "MAX_SOURCE_COLUMNS": "maximumSourceColumns",
    }
    frozen = contract["frozenTranscripts"]
    python_lines = [
        '# Generated by scripts/gen_tofudb_ir_contract.py; do not edit.',
        '# Source: contracts/tofudb_ir_v1.json offlineMigration.',
        '',
    ]
    python_lines.extend(f'{name} = {migration[key]!r}' for name, key in names.items())
    for name, field in [("MAX_TURN_SNAPSHOT_SOURCE_BYTES", "maximumSourceBytes"),
                        ("MAX_TURN_SNAPSHOT_BYTES", "maximumSnapshotBytes")]:
        python_lines.append(f'{name} = {contract["turnSnapshots"][field]}')

    python_lines.append(f'MAX_MIGRATION_TRASH_TURNS = {contract["conversationTrashMigration"]["maximumTurnsPerCapsule"]}')
    python_lines.append(f'MAX_SOURCE_WIDE_ROW_BYTES = {migration["maximumSourceWideRowBytes"]}')
    python_lines.append(f'MAX_MIGRATION_TASK_RESULT_DOCUMENT_BYTES = {migration["documentImport"]["taskResultMaximumJSONBytes"]}')
    python_lines.append(f'MAX_MIGRATION_COMPACTION_DECODED_BYTES = {migration["documentImport"]["compactionArchiveMaximumDecodedBytes"]}')
    event_bound = next(table for table in contract["logicalTables"] if table["name"] == "conversation_turns")["maximumAttemptEventDocumentBytes"]
    python_lines.append(f'MAX_ATTEMPT_EVENT_DOCUMENT_BYTES = {event_bound}')
    python_lines.append(f'FROZEN_TRANSCRIPTS = {frozen!r}')
    python_lines.append(f'LEGACY_RECEIPT_IMPORT = {migration["legacyReceiptImport"]!r}')
    python_lines.append(f'RETIRED_LARGE_ARCHIVE = {migration["retiredLargeArchive"]!r}')
    python_lines.append(f'OPERATOR_SESSION = {migration["operatorSession"]!r}')
    python_lines.append(f'MAX_ENTITY_KEY_BYTES = {contract["physicalBounds"]["maximumEntityKeyBytes"]}')
    search = json.loads(SEARCH_SOURCE.read_text(encoding="utf-8"))
    if search.get('format') != 'tofu.conversation-search.v2':
        raise ValueError('conversation search contract format differs')
    search_names = {
        'QUERY_CHARACTERS': 'queryCharacters', 'QUERY_TERMS': 'queryTerms',
        'CURSOR_BYTES': 'cursorBytes', 'MAX_PAGE_RESULTS': 'maximumPageResults',
        'CANDIDATE_SCAN_PAGE': 'candidateScanPage', 'CHUNK_SCAN_PAGE': 'chunkScanPage',
        'CHUNK_SCAN_BATCH': 'chunkScanBatch', 'CHUNK_CHARACTERS': 'chunkCharacters',
        'CHUNK_OVERLAP_CHARACTERS': 'chunkOverlapCharacters',
        'SOURCE_PAGE_ROWS': 'sourcePageRows', 'SOURCE_PAGE_BYTES': 'sourcePageBytes',
    }
    search_lines = ['//! @generated by scripts/gen_tofudb_ir_contract.py; do not edit.',
                    '//! Source: contracts/conversation_search_v2.json.', '']
    native = search['nativeProjection']
    if type(native['headerScanBatch']) is not int or not 1 <= native['headerScanBatch'] <= search['limits']['candidateScanPage']:
        raise ValueError('invalid native search header batch')
    if native['formatVersion'] != 2 or native['invalidationTokenBytes'] != 16 or native['legacyInvalidationTokenBytes'] != 8:
        raise ValueError('native search projection encoding differs')
    search_lines.extend([
        f"pub const PROJECTION_FORMAT_VERSION: u8 = {native['formatVersion']};",
        f"pub const HEADER_SCAN_BATCH: usize = {native['headerScanBatch']};",
        f"pub const DIRTY_TOKEN_BYTES: usize = {native['invalidationTokenBytes']};",
        f"pub const LEGACY_DIRTY_TOKEN_BYTES: usize = {native['legacyInvalidationTokenBytes']};",
    ])
    for name, key in search_names.items():
        value = search['limits'][key]
        if type(value) is not int or value <= 0:
            raise ValueError(f'invalid conversation search bound: {key}')
        search_lines.append(f'pub const {name}: usize = {value};')
    receipt_lines = ['# Generated by scripts/gen_tofudb_ir_contract.py; do not edit.',
                     '# Source: contracts/tofudb_ir_v1.json commandReceipts.', '']
    receipt_lines.extend(f'{name} = {contract["commandReceipts"][key]}'
                         for name, key in RECEIPT_NAMES.items())
    worker = _worker_lifecycle()
    lease = worker['leaseAuthority']
    server_clock = lease['serverClock']
    job_table = next(table for table in contract['logicalTables'] if table['name'] == 'storage_worker_jobs')
    maximum_clock = job_table['maximumClockMilliseconds']
    maximum_now = maximum_clock - lease['maximumLeaseMilliseconds']
    worker_lines = [
        '# Generated by scripts/gen_tofudb_ir_contract.py; do not edit.',
        '# Source: contracts/worker_job_lifecycle_v1.json.',
        'from typing import Literal, NamedTuple',
        f'WORKER_JOB_STATES = frozenset({worker["states"]!r})',
        f'WORKER_TERMINAL_STATES = frozenset({worker["terminalStates"]!r})',
        f'WORKER_JOB_MAX_CLOCK_MS = {maximum_clock}',
        f'WORKER_LEASE_MAX_NOW_MS = {maximum_now}',
        f'WORKER_LEASE_MAX_MS = {lease["maximumLeaseMilliseconds"]}',
        f'WORKER_LEASE_EPOCH_CHARACTERS = {lease["epochCharacters"]}',
        f'WORKER_SERVER_CLOCK_FORMAT = {server_clock["format"]!r}',
        f'WORKER_SERVER_CLOCK_FIELDS = frozenset({server_clock["fields"]!r})',
        f'WORKER_SERVER_CLOCK_MAX_STATE_BYTES = {server_clock["maximumStateBytes"]}',
        f'POSTGRES_WORKER_CLOCK_MIN_MAJOR = {server_clock["minimumPostgresMajor"]}',

        '',
        'class WorkerLeaseAuthoritySnapshot(NamedTuple):',
        '    epoch: str',
        '    lease_now_ms: int',
        '    utc_now_ms: int',
        '',
        'WorkerJobStatus = Literal[' + ', '.join(repr(s) for s in worker['states']) + ']',
        'WorkerTerminalStatus = Literal[' + ', '.join(repr(s) for s in worker['terminalStates']) + ']',
    ]
    outputs = {ROOT / 'lib/storage_sidecar/generated_storage_receipts.py': '\n'.join(receipt_lines) + '\n',
               ROOT / "packages/tofu-db/src/generated_paper_build.rs": _render_paper_build(),
               OUTPUT: _render(), MIGRATION_OUTPUT: "\n".join(python_lines) + "\n",
               SEARCH_OUTPUT: "\n".join(search_lines) + "\n"}
    outputs[ROOT / 'lib/storage/generated_worker_job_contract.py'] = '\n'.join(worker_lines) + '\n'
    outputs[ROOT / 'lib/storage_sidecar/native_worker_clock/generated_clock_contract.h'] = (
        '/* Generated by scripts/gen_tofudb_ir_contract.py; do not edit. */\n'
        '#ifndef TOFU_WORKER_CLOCK_CONTRACT_H\n#define TOFU_WORKER_CLOCK_CONTRACT_H\n'
        f'#define TOFU_WORKER_CLOCK_FORMAT {json.dumps(server_clock["format"])}\n'
        f'#define TOFU_WORKER_CLOCK_MIN_PG_VERSION {server_clock["minimumPostgresMajor"] * 10000}\n'
        f'#define TOFU_WORKER_CLOCK_MAX_STATE_BYTES {server_clock["maximumStateBytes"]}\n'
        f'#define TOFU_WORKER_CLOCK_MAX_MS UINT64CONST({maximum_now})\n'
        f'#define TOFU_WORKER_CLOCK_EPOCH_BYTES {lease["epochCharacters"] // 2}\n'
        '#endif\n')
    stale = False
    for path, rendered in outputs.items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                print(f'{path.relative_to(ROOT)} is stale; run '
                      'python3 scripts/gen_tofudb_ir_contract.py')
                stale = True
        else:
            path.write_text(rendered, encoding="utf-8")
    return int(stale)


if __name__ == "__main__":
    raise SystemExit(main())
