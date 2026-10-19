"""Explicit SQLite → Tofu-DB domain import; never a startup or wire path.

The operator supplies a stopped SQLite authority, an exclusive ``tofu-db migration-session``
child, and the initialized target UUID. This module streams declared primary
keys, replays current semantic snapshots, and publishes progress only after
independent target enumeration matches the hashed source prefix. Conversation
headers reuse live create/get/list/count. Turns restore through the native
identity-preserving adapter: exact projection/settlement snapshots, lane
ordinals, counts, heads, and identity claims, with live attempts refused.
Document record and billing bodies are verified one row at a time. Domain
indexes are restored atomically by their native adapters, then independently
enumerated.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
import uuid

from lib.storage_authority_sql.migration.generated_tofudb_migration import (
    MAX_ENTITY_KEY_BYTES, MAX_MIGRATION_COMPACTION_DECODED_BYTES,
    MAX_MIGRATION_TASK_RESULT_DOCUMENT_BYTES,
    MAX_SOURCE_BATCH_ROWS, MAX_SOURCE_ROW_BYTES, MAX_SOURCE_CHUNK_BYTES,
    MAX_SOURCE_WIDE_ROW_BYTES,
    MAX_ATTEMPT_EVENT_DOCUMENT_BYTES, OPERATOR_SESSION,
)
from lib.storage.generated_worker_job_contract import WORKER_JOB_STATES
from lib.storage_authority_sql.schema import SCHEMA_VERSION
from lib.storage_authority_sql.migration.tofudb_migration_checkpoint import (
    MigrationBinding,
    MigrationCheckpoint,
    MigrationCheckpointError,
)
from lib.storage_authority_sql.migration.tofudb_migration_source import (
    MigrationSourceError,
    SQLiteMigrationSource,
)
from tofu_db.wire import StorageV2Error
from lib.storage_authority_sql.migration.tofudb_migration_target import OfflineMigrationSession

from lib.log import get_logger

from lib.storage_authority_sql.migration import tofudb_migration_turn_payload as turn_payload
from lib.storage_authority_sql.migration import tofudb_migration_retired as retired
from lib.storage_authority_sql.migration import tofudb_migration_retired_large as retired_large
from lib.storage_authority_sql.migration import tofudb_migration_project_brain as project_brain_fold
from lib.storage_authority_sql.migration.tofudb_migration_metadata import POLICY as SOURCE_METADATA_POLICY


logger = get_logger(__name__)


class MigrationImportError(ValueError):
    """Source, target, plan, or independent verification failed closed."""


_LIST_PAGE = 1000
# Absolute per-request deadline for both pipe directions. Large blob commits on
# slow volumes legitimately exceed 15 s; 120 s stays a bounded wedge detector
# (the session timeout admits at most 120 s by contract).
_REQUEST_DEADLINE_MS = 120_000

# Retired Project Brain authorities are folded into the projection the live
# ``project_brain.cutover`` would have produced, then dropped by that cutover.
# A native target has no legacy tables, so the importer reproduces the fold
# read-only and restores the synthesized projection through the same
# ``migration.project.brain.project.*`` operations. The coverage gate
# recognizes the retired tables only because this fold owns them.
LEGACY_PROJECT_BRAIN_CUTOVER_POLICY = {
    'consumedTables': list(project_brain_fold.CONSUMED_TABLES),
    'fold': 'watch-intent-into-projection;execution-history-dropped',
    'semantics': 'one-legacy-migrated-event-per-owner-project;exact-native-cutover-fold',
}

# Project keys the legacy fold restored for the current session owner. The
# projection importer excludes exactly these from its blanket catalog scan,
# because they are the fold's verified occupants rather than source-settled
# rows. Populated only when the fold completes in this process.
_FOLD_OCCUPANCY: set[str] = set()

PLAN: dict[str, Any] = {
    'id': 'tofu-db.offline-import.v27',
    'retiredSearchRebuildPolicy': 'fts5-internal-index-with-archived-content-and-config',
    'retiredTradingMirrorPolicy': 'exact-current-plugin-rows',
    'turnProjectionSourcePolicy': 'decode-private-storage-codec;require-materialized-inline-projection;reject-checkpoints-and-patch-heads',
    'domains': [
        {
            'name': 'tenant_users',
            'tables': [{
                'name': 'tenant_users',
                'keys': ['id'],
                'columns': [
                    'id', 'owner_user_id', 'email', 'password_hash',
                    'display_name', 'role', 'status', 'created_at',
                    'last_login_at', 'email_verified', 'metadata',
                ],
            }],
            'operations': OPERATOR_SESSION['tenantUserOperations'],
            'verification': OPERATOR_SESSION['tenantUserSnapshot'],
        },
        {
            'name': 'trash',
            'tables': [
                {'name': 'storage_conversation_trash', 'keys': ['conversation_id', 'user_id'],
                 'columns': ['conversation_id', 'user_id', 'title', 'messages_json', 'created_at_ms',
                             'updated_at_ms', 'settings_json', 'msg_count', 'rev', 'deleted_at_ms']},
                {'name': 'storage_conversation_trash_turns', 'keys': ['conversation_id', 'user_id', 'turn_id'],
                 'columns': ['conversation_id', 'user_id', 'turn_id', 'lane_id', 'parent_turn_id', 'ordinal',
                             'actor', 'kind', 'run_id', 'status', 'projection_json', 'projection_revision',
                             'settlement_json', 'created_at', 'updated_at']},
            ],
            'operations': OPERATOR_SESSION['trashOperations'],
            'verification': OPERATOR_SESSION['trashSnapshot'],
        },
        {
            'name': 'conversations',
            'tables': [{
                'name': 'storage_conversations',
                'keys': ['id', 'user_id'],
                'columns': [
                    'id', 'user_id', 'title', 'created_at_ms',
                    'updated_at_ms', 'settings_json', 'rev',
                ],
            }],
            'operations': [
                *OPERATOR_SESSION['conversationOperations'], 'conversation.get',
                'conversation.list', 'conversation.count',
            ],
            'verification': OPERATOR_SESSION['conversationSnapshot'],
        },
        {
            'name': 'turn_tombstones',
            'tables': [{
                'name': 'storage_turn_tombstones',
                'keys': ['conversation_id', 'turn_id'],
                'columns': ['conversation_id', 'turn_id', 'user_id', 'deleted_at'],
            }],
            'operations': [operation for operation in OPERATOR_SESSION['turnOperations']
                           if operation.startswith('migration.turn.tombstone.')],
            'verification': 'exact-owner-identity-deletion-clock;atomic-claim-delta-retention-indexes;independent-catalog',
        },
        {
            'name': 'attempts',
            'tables': [{
                'name': 'storage_generation_attempts', 'keys': ['attempt_id'],
                'columns': ['attempt_id', 'conversation_id', 'turn_id', 'command_id',
                            'task_id', 'operation', 'dispatch_mode', 'queue_id', 'queue_state',
                            'status', 'base_projection_revision', 'resume_anchor_json',
                            'config_json', 'error_json', 'timing_trace_json', 'created_at',
                            'started_at', 'settled_at', 'superseded_at'],
            }],
            'operations': OPERATOR_SESSION['attemptOperations'],
            'verification': OPERATOR_SESSION['attemptSnapshot'],
        },
        {
            'name': 'attempt_events',
            'tables': [{
                'name': 'storage_attempt_events', 'keys': ['attempt_id', 'sequence'],
                'columns': ['attempt_id', 'sequence', 'conversation_id', 'turn_id',
                            'projection_revision', 'type', 'payload_json', 'payload_bytes', 'created_at'],
            }],
            'operations': (OPERATOR_SESSION['attemptEventOperations']
                           + OPERATOR_SESSION['attemptEventUploadOperations']),
            'verification': OPERATOR_SESSION['attemptEventSnapshot'],
        },
        {
            # Live v2 idempotency receipts: 32-byte BLOB command key and request
            # digest, owner/tenant explicit, preserved through the native
            # atomic receipt/blob-family publisher.
            'name': 'command_receipts_v2',
            'tables': [{
                'name': 'storage_command_receipts_v2', 'keys': ['command_key'],
                'columns': ['command_key', 'operation', 'request_digest',
                            'response_json', 'committed_at_ms'],
            }],
            'operations': [operation for operation in OPERATOR_SESSION['commandReceiptOperations']
                           if operation.startswith('migration.command.receipt.')],
            'verification': OPERATOR_SESSION['commandReceiptSnapshot'],
        },
        {
            # Legacy TEXT-keyed receipts retained under the reserved global owner,
            # source-fenced, replayed only against the exact hashed request.
            'name': 'command_receipts',
            'tables': [{
                'name': 'storage_command_receipts', 'keys': ['command_id'],
                'columns': ['command_id', 'operation', 'request_digest',
                            'response_json', 'committed_at_ms'],
            }],
            'operations': [operation for operation in OPERATOR_SESSION['commandReceiptOperations']
                           if operation.startswith('migration.legacy.receipt.')],
            'verification': OPERATOR_SESSION['commandReceiptSnapshot'],
        },
        {
            'name': 'turns',
            'tables': [{
                'name': 'storage_conversation_turns',
                'keys': ['turn_id'],
                'columns': [
                    'turn_id', 'conversation_id', 'user_id',
                    'presentation_id', 'lane_id', 'parent_turn_id',
                    'ordinal', 'actor', 'kind', 'run_id', 'status',
                    'current_attempt_id', 'projection_json',
                    'projection_revision', 'settlement_json',
                    'created_at', 'updated_at',
                ],
            }],
            'operations': OPERATOR_SESSION['turnOperations'],
            'verification': OPERATOR_SESSION['turnSnapshot'],
        },
        {
            # Durable task spine: physical application event stream. The
            # native namespace is task-scoped and collision-free, so the
            # generic catalog scan verifies target==source exactly.
            'name': 'storage_events',
            'tables': [{
                'name': 'storage_events', 'keys': ['task_id', 'sequence'],
                'columns': ['task_id', 'sequence', 'event_json', 'created_at_ms',
                            'stream_kind', 'event_type', 'event_kind', 'owner_user_id',
                            'project_key', 'project_sequence'],
            }],
            'operations': OPERATOR_SESSION['storageEventOperations'],
            'verification': OPERATOR_SESSION['storageEventSnapshot'],
        },
        {
            # Conversation sync changes reference restored turns/attempts, so
            # they import after the turns domain; the native key is
            # conversation-scoped (session owner is fixed).
            'name': 'conversation_sync_events',
            'tables': [{
                'name': 'storage_conversation_changes',
                'keys': ['conversation_id', 'user_id', 'sync_sequence'],
                'columns': ['conversation_id', 'user_id', 'sync_sequence',
                            'change_type', 'turn_id', 'attempt_id', 'event_json',
                            'created_at', 'attempt_sequence'],
            }],
            'operations': OPERATOR_SESSION['conversationSyncEventOperations'],
            'verification': OPERATOR_SESSION['conversationSyncEventSnapshot'],
        },
        {
            # Heads are the max restored sync sequence per conversation and
            # must import only after every sync event has landed.
            'name': 'conversation_sync_heads',
            'tables': [{
                'name': 'storage_conversation_sync_heads',
                'keys': ['conversation_id', 'user_id'],
                'columns': ['conversation_id', 'user_id', 'sync_sequence', 'updated_at'],
            }],
            'operations': OPERATOR_SESSION['conversationSyncHeadOperations'],
            'verification': OPERATOR_SESSION['conversationSyncHeadSnapshot'],
        },
        {
            'name': 'frozen_transcripts',
            'tables': [{
                'name': 'storage_conversations', 'keys': ['id', 'user_id'],
                'columns': ['id', 'user_id', 'created_at_ms', 'messages_json'],
            }],
            'operations': (OPERATOR_SESSION['frozenTranscriptOperations']
                           + OPERATOR_SESSION['frozenTranscriptUploadOperations']),
            'verification': 'exact-public-message-pages-and-stored-source-sha256;complete-only-after-independent-page-catalog-and-content-verification',
        },
        {
            'name': 'raw_archives',
            'tables': [{
                'name': 'storage_raw_archives', 'keys': ['archive_id'],
                'columns': ['archive_id', 'user_id', 'conversation_id', 'turn_id', 'attempt_id',
                            'task_id', 'round_num', 'transport_attempt', 'request_blob', 'response_blob',
                            'request_bytes', 'response_bytes', 'stored_bytes', 'request_sha256',
                            'response_sha256', 'integrity', 'truncation_reason', 'summary_json', 'created_at_ms'],
            }],
            'operations': OPERATOR_SESSION['rawArchiveOperations'],
            'verification': OPERATOR_SESSION['rawArchiveSnapshot'],
        },
        {
            'name': 'compaction_archives',
            'tables': [{
                'name': 'storage_compaction_archives', 'keys': ['archive_id'],
                'columns': ['archive_id', 'archive_scope', 'conversation_id', 'user_id',
                            'messages_json', 'post_messages_json', 'summary', 'receipt_json',
                            'trigger', 'task_id', 'round_num', 'model', 'tokens_before',
                            'tokens_after', 'msgs_before', 'msgs_after', 'reason',
                            'payload_size', 'created_at_ms'],
            }],
            'operations': OPERATOR_SESSION['compactionArchiveOperations'],
            'verification': OPERATOR_SESSION['compactionArchiveSnapshot'],
        },
        {
            'name': 'credentials',
            'tables': [{
                'name': 'auth_credentials',
                'keys': ['id'],
                'columns': [
                    'id', 'owner_user_id', 'account_user_id', 'tenant_id',
                    'name', 'prefix', 'secret_hash', 'scopes',
                    'rate_limit_rpm', 'rate_limit_tpd', 'created_at',
                    'last_used_at', 'expires_at', 'disabled', 'revoked_at',
                    'metadata',
                ],
            }],
            'operations': [
                'credential.create', 'credential.update', 'credential.touch',
                'credential.revoke', 'credential.list', 'credential.identify',
            ],
        },
        {
            'name': 'providers',
            'tables': [{
                'name': 'byo_providers',
                'keys': ['id'],
                'columns': [
                    'id', 'owner_user_id', 'tenant_id', 'name', 'base_url',
                    'api_key_ciphertext', 'key_hint', 'models_json',
                    'extra_headers_json', 'thinking_format', 'disabled',
                    'created_at', 'updated_at', 'last_used_at',
                ],
            }],
            'operations': [
                'provider.create', 'provider.update', 'provider.touch',
                'provider.list', 'provider.get',
            ],
            'verification': 'independent-get-and-list;list-redacts-ciphertext',
        },
        {
            'name': 'billing',
            'tables': [
                {
                    'name': 'billing_wallets',
                    'keys': ['user_id'],
                    'columns': [
                        'user_id', 'balance_micro', 'currency',
                        'low_balance_alert_micro', 'updated_at',
                    ],
                },
                {
                    'name': 'billing_ledger',
                    'keys': ['id'],
                    'columns': [
                        'id', 'user_id', 'ts', 'amount_micro', 'kind',
                        'ref_type', 'ref_id', 'balance_after_micro', 'note',
                    ],
                },
            ],
            'operations': OPERATOR_SESSION['billingOperations'],
            'verification': OPERATOR_SESSION['billingSnapshot'],
        },
        {
            'name': 'task_results',
            'tables': [{
                'name': 'storage_records',
                'keys': ['namespace', 'record_key'],
                'columns': [
                    'namespace', 'record_key', 'value_json', 'version',
                    'updated_at_ms',
                ],
            }],
            'operations': OPERATOR_SESSION['taskResultOperations'],
            'verification': OPERATOR_SESSION['taskResultSnapshot'],
            'sourceNamespace': 'task_results',
        },
        {
            'name': 'continuation',
            'tables': [{
                'name': 'storage_records',
                'keys': ['namespace', 'record_key'],
                'columns': [
                    'namespace', 'record_key', 'value_json', 'version',
                    'updated_at_ms',
                ],
            }],
            'operations': OPERATOR_SESSION['continuationOperations'],
            'verification': OPERATOR_SESSION['continuationSnapshot'],
            'sourceNamespace': 'model.continuation.v1',
        },
        {
            'name': 'records',
            'tables': [{
                'name': 'storage_records',
                'keys': ['namespace', 'record_key'],
                'columns': [
                    'namespace', 'record_key', 'value_json', 'version',
                    'updated_at_ms',
                ],
            }],
            'operations': OPERATOR_SESSION['recordOperations'],
            'verification': 'sha256-chain-v1;exact-json-version-timestamp;independent-owner-catalog',
            'excludedNamespaces': OPERATOR_SESSION['excludedRecordNamespaces'],
            'excludedPrefixes': OPERATOR_SESSION['excludedRecordPrefixes'],
        },
        {
            'name': 'paper_build_budget',
            'tables': [{
                'name': 'storage_records',
                'keys': ['namespace', 'record_key'],
                'columns': [
                    'namespace', 'record_key', 'value_json', 'version',
                    'updated_at_ms',
                ],
            }],
            'operations': OPERATOR_SESSION['paperBuildBudgetOperations'],
            'verification': OPERATOR_SESSION['paperBuildBudgetSnapshot'],
            'sourceNamespace': 'paper.build.budget.v1',
        },
        {
            'name': 'paper_document_budget',
            'tables': [{
                'name': 'storage_records',
                'keys': ['namespace', 'record_key'],
                'columns': [
                    'namespace', 'record_key', 'value_json', 'version',
                    'updated_at_ms',
                ],
            }],
            'operations': OPERATOR_SESSION['paperDocumentBudgetOperations'],
            'verification': OPERATOR_SESSION['paperDocumentBudgetSnapshot'],
            'sourceNamespace': 'paper.document.budget.v1',
        },
        {
            'name': 'desktop_egress',
            'tables': [{
                'name': 'storage_desktop_egress_preferences',
                'keys': ['owner_user_id'],
                'columns': ['owner_user_id', 'agent_id', 'updated_at_ms'],
            }],
            'operations': OPERATOR_SESSION['desktopEgressOperations'],
            'verification': OPERATOR_SESSION['desktopEgressSnapshot'],
        },
        {
            'name': 'daily_cost',
            'tables': [{
                'name': 'daily_cost_cache',
                'keys': ['user_id', 'date'],
                'columns': ['user_id', 'date', 'cost', 'conversations_json',
                            'computed_at'],
            }],
            'operations': OPERATOR_SESSION['dailyCostOperations'],
            'verification': OPERATOR_SESSION['dailyCostSnapshot'],
        },
        {
            'name': 'rate_limit_events',
            'tables': [{
                'name': 'storage_rate_limit_events',
                'keys': ['event_id'],
                'columns': ['event_id', 'endpoint', 'client_key',
                            'occurred_at_ms', 'expires_at_ms'],
            }],
            'operations': OPERATOR_SESSION['rateLimitEventOperations'],
            'verification': OPERATOR_SESSION['rateLimitEventSnapshot'],
        },
        {
            'name': 'orchestration',
            'tables': [
                {
                    'name': 'orchestration_definitions',
                    'keys': ['id'],
                    'columns': ['id', 'user_id', 'tenant_id', 'name',
                                'definition_json', 'created_at_ms',
                                'updated_at_ms'],
                },
                {
                    'name': 'orchestration_runs',
                    'keys': ['id'],
                    'columns': ['id', 'user_id', 'tenant_id', 'orch_id', 'name',
                                'definition', 'input', 'status', 'final',
                                'error', 'created_by', 'created_at',
                                'updated_at', 'finished_at'],
                    'optionalColumns': ['continuity_pending'],
                },
                {
                    'name': 'orchestration_run_events',
                    'keys': ['run_id', 'user_id', 'tenant_id', 'seq'],
                    'columns': ['run_id', 'user_id', 'tenant_id', 'seq', 'type',
                                'node_id', 'payload', 'ts'],
                },
            ],
            'operations': (
                OPERATOR_SESSION['orchestrationDefinitionOperations']
                + OPERATOR_SESSION['orchestrationRunOperations']
                + OPERATOR_SESSION['orchestrationEventOperations']
            ),
            'verification': (
                OPERATOR_SESSION['orchestrationDefinitionSnapshot'] + ';'
                + OPERATOR_SESSION['orchestrationRunSnapshot'] + ';'
                + OPERATOR_SESSION['orchestrationEventSnapshot']
            ),
        },
        {
            'name': 'swarm',
            'tables': [
                {
                    'name': 'swarm_sessions',
                    'keys': ['swarm_key'],
                    'columns': ['swarm_key', 'conv_id', 'task_id', 'status',
                                'specs_json', 'config_json', 'created_at',
                                'updated_at'],
                },
                {
                    'name': 'swarm_agents',
                    'keys': ['swarm_key', 'agent_id'],
                    'columns': ['swarm_key', 'agent_id', 'role', 'objective',
                                'status', 'messages_json', 'result_json',
                                'rounds_used', 'delivered', 'updated_at'],
                },
            ],
            'operations': (
                OPERATOR_SESSION['swarmSessionOperations']
                + OPERATOR_SESSION['swarmAgentOperations']
            ),
            'verification': (
                OPERATOR_SESSION['swarmSessionSnapshot'] + ';'
                + OPERATOR_SESSION['swarmAgentSnapshot']
            ),
        },
        {
            'name': 'integration',
            'tables': [
                {
                    'name': 'integration_workspaces',
                    'keys': ['id'],
                    'columns': ['id', 'user_id', 'project_root', 'task_id',
                                'title', 'workspace_path', 'managed', 'state',
                                'base_sha', 'checkpoint_sha', 'candidate_sha',
                                'error', 'created_at', 'updated_at'],
                },
                {
                    'name': 'integration_events',
                    'keys': ['id'],
                    'columns': ['id', 'user_id', 'project_root', 'task_id',
                                'kind', 'message', 'detail', 'created_at'],
                },
                {
                    'name': 'integration_workspace_meta',
                    'keys': ['user_id', 'project_root', 'task_id'],
                    'columns': ['user_id', 'project_root', 'task_id',
                                'origin_json', 'updated_at'],
                },
            ],
            'operations': (
                OPERATOR_SESSION['integrationWorkspaceOperations']
                + OPERATOR_SESSION['integrationEventOperations']
                + OPERATOR_SESSION['integrationMetaOperations']
            ),
            'verification': (
                OPERATOR_SESSION['integrationWorkspaceSnapshot'] + ';'
                + OPERATOR_SESSION['integrationEventSnapshot'] + ';'
                + OPERATOR_SESSION['integrationMetaSnapshot']
            ),
        },
        {
            'name': 'paper',
            'tables': [
                {
                    'name': 'paper_library',
                    'keys': ['id', 'user_id'],
                    'columns': ['id', 'user_id', 'title', 'pdf_url',
                                'pdf_filename', 'arxiv_id', 'paper_hash',
                                'parsed_text', 'parser_version', 'qa_history',
                                'images', 'babel_cache', 'page_count',
                                'folder_id', 'created_at', 'updated_at'],
                },
                {
                    'name': 'paper_reports',
                    'keys': ['user_id', 'paper_hash', 'lang'],
                    'columns': ['user_id', 'paper_hash', 'lang', 'report',
                                'model', 'meta', 'created_at'],
                },
                {
                    'name': 'paper_translations',
                    'keys': ['user_id', 'paper_hash', 'lang'],
                    'columns': ['user_id', 'paper_hash', 'lang', 'text',
                                'model', 'created_at'],
                },
                {
                    'name': 'paper_notes',
                    'keys': ['user_id', 'id'],
                    'columns': ['user_id', 'id', 'paper_hash', 'lang',
                                'anchor', 'note', 'created_at', 'updated_at'],
                },
                {
                    'name': 'paper_podcasts',
                    'keys': ['user_id', 'paper_hash', 'mode', 'lang', 'voice'],
                    'columns': ['user_id', 'paper_hash', 'mode', 'lang',
                                'voice', 'status', 'script_json', 'file_path',
                                'duration_sec', 'model', 'tts_model', 'meta',
                                'created_at', 'updated_at'],
                },
            ],
            'operations': (
                OPERATOR_SESSION['paperLibraryOperations']
                + OPERATOR_SESSION['paperReportOperations']
                + OPERATOR_SESSION['paperTranslationOperations']
                + OPERATOR_SESSION['paperNoteOperations']
                + OPERATOR_SESSION['paperPodcastOperations']
            ),
            'verification': (
                OPERATOR_SESSION['paperLibrarySnapshot'] + ';'
                + OPERATOR_SESSION['paperReportSnapshot'] + ';'
                + OPERATOR_SESSION['paperTranslationSnapshot'] + ';'
                + OPERATOR_SESSION['paperNoteSnapshot'] + ';'
                + OPERATOR_SESSION['paperPodcastSnapshot']
            ),
        },
        {
            'name': 'knowledge',
            'tables': [
                {
                    'name': 'storage_knowledge_documents',
                    'keys': ['user_id', 'id'],
                    'columns': ['user_id', 'id', 'sha256', 'name',
                                'stored_name', 'kind', 'size_bytes', 'method',
                                'warnings_json', 'text_chars', 'chunk_count',
                                'pages', 'scope', 'media_metadata_json',
                                'created_at', 'updated_at'],
                },
                {
                    'name': 'storage_knowledge_chunks',
                    'keys': ['user_id', 'document_id', 'ordinal'],
                    'columns': ['user_id', 'document_id', 'ordinal',
                                'section', 'location', 'content',
                                'search_text'],
                },
                {
                    'name': 'storage_knowledge_assets',
                    'keys': ['user_id', 'id'],
                    'columns': ['user_id', 'id', 'document_id', 'ordinal',
                                'kind', 'stored_name', 'mime_type', 'sha256',
                                'size_bytes', 'width', 'height', 'page',
                                'pages_json', 'bbox_json', 'caption',
                                'ocr_text', 'description', 'enrichment_status',
                                'enrichment_model', 'enrichment_error',
                                'metadata_json', 'created_at', 'updated_at'],
                },
                {
                    'name': 'storage_knowledge_chunk_assets',
                    'keys': ['user_id', 'document_id', 'chunk_ordinal',
                             'asset_id', 'relation'],
                    'columns': ['user_id', 'document_id', 'chunk_ordinal',
                                'asset_id', 'relation', 'ordinal'],
                },
                {
                    'name': 'storage_knowledge_terms',
                    'keys': ['user_id', 'term', 'document_id',
                             'chunk_ordinal'],
                    'columns': ['user_id', 'term', 'document_id',
                                'chunk_ordinal'],
                },
                {
                    'name': 'storage_knowledge_settings',
                    'keys': ['user_id'],
                    'columns': ['user_id', 'enabled', 'visual_enrichment',
                                'updated_at'],
                },
            ],
            'operations': OPERATOR_SESSION['knowledgeOperations'],
            'verification': OPERATOR_SESSION['knowledgeSnapshot'],
        },
        {
            'name': 'chat_artifacts',
            'tables': [{
                'name': 'chat_artifacts',
                'keys': ['id'],
                'columns': [
                    'id', 'conv_id', 'task_id', 'msg_id', 'source',
                    'source_ref', 'format', 'title', 'content',
                    'content_sha256', 'size_bytes', 'version', 'parent_id',
                    'pinned', 'meta', 'created_at', 'deleted_at',
                ],
            }],
            'operations': OPERATOR_SESSION['chatArtifactOperations'],
            'verification': OPERATOR_SESSION['chatArtifactSnapshot'],
        },
        {
            'name': 'tool_result_artifacts',
            'tables': [{
                'name': 'tool_result_artifacts',
                'keys': ['user_id', 'content_sha256'],
                'columns': [
                    'user_id', 'content_sha256', 'content', 'media_type',
                    'size_bytes', 'created_at_ms', 'expires_at_ms',
                    'last_accessed_at_ms',
                ],
            }],
            'operations': OPERATOR_SESSION['toolResultOperations'],
            'verification': OPERATOR_SESSION['toolResultSnapshot'],
        },
        {
            'name': 'browser_observations',
            'tables': [
                {
                    'name': 'storage_browser_site_observations',
                    'keys': ['owner_user_id', 'origin', 'route_family', 'operation'],
                    'columns': [
                        'owner_user_id', 'origin', 'route_family', 'operation',
                        'schema_version', 'strategy', 'hints_json',
                        'anti_bot_vendor', 'auth_signal', 'status',
                        'confidence_milli', 'visit_count', 'successful_visits',
                        'hinted_visits', 'hint_match_visits',
                        'consecutive_failures', 'last_outcome',
                        'last_elapsed_ms', 'last_verified_at_ms',
                        'last_observed_at_ms', 'expires_at_ms', 'payload_bytes',
                    ],
                },
                {
                    'name': 'storage_browser_site_observation_owners',
                    'keys': ['owner_user_id'],
                    'columns': ['owner_user_id', 'touched_at_ms'],
                },
            ],
            'operations': OPERATOR_SESSION['browserObservationOperations'],
            'verification': OPERATOR_SESSION['browserObservationSnapshot'],
        },
        {
            'name': 'model_routing',
            'tables': [
                {
                    'name': 'storage_model_routing_authorities',
                    'keys': ['owner_user_id', 'tenant_id'],
                    'columns': [
                        'owner_user_id', 'tenant_id', 'revision',
                        'document_json', 'backup_json',
                        'migration_receipt_json', 'updated_at',
                    ],
                },
                {
                    'name': 'storage_model_routing_secrets',
                    'keys': ['owner_user_id', 'tenant_id', 'secret_reference'],
                    'columns': [
                        'owner_user_id', 'tenant_id', 'secret_reference',
                        'ciphertext', 'key_hint', 'created_at', 'updated_at',
                    ],
                },
            ],
            'operations': (OPERATOR_SESSION['modelRoutingAuthorityOperations']
                           + OPERATOR_SESSION['modelRoutingSecretOperations']),
            'verification': OPERATOR_SESSION['modelRoutingAuthoritySnapshot'],
        },
        {
            'name': 'plugin_store',
            'tables': [
                {
                    'name': 'storage_plugin_manifests',
                    'keys': ['namespace'],
                    'columns': ['namespace', 'manifest_version', 'manifest_json',
                                'updated_at_ms'],
                },
                {
                    'name': 'storage_plugin_rows',
                    'keys': ['namespace', 'table_name', 'row_key'],
                    'columns': ['namespace', 'table_name', 'row_key',
                                'document_json', 'version', 'updated_at_ms'],
                },
                {
                    'name': 'storage_plugin_unique_values',
                    'keys': ['namespace', 'table_name', 'index_name', 'index_value'],
                    'columns': ['namespace', 'table_name', 'index_name',
                                'index_value', 'row_key'],
                },
            ],
            'operations': (OPERATOR_SESSION['pluginManifestOperations']
                           + OPERATOR_SESSION['pluginRowsOperations']
                           + OPERATOR_SESSION['pluginUniqueOperations']),
            'verification': OPERATOR_SESSION['pluginManifestSnapshot'],
        },
        {
            # The fold owns no source table of its own; it consumes the retired
            # Project Brain authorities (see legacyProjectBrainCutoverPolicy)
            # and restores the synthesized projections through the same point
            # operations the settled-projection domain uses.
            'name': 'project_brain_legacy_cutover',
            'tables': [],
            'operations': OPERATOR_SESSION['projectBrainOperations'],
            'verification': ('exact-native-cutover-fold;owner-scoped-catalog;'
                             + OPERATOR_SESSION['projectBrainSnapshot']),
        },
        {
            'name': 'project_brain_projects',
            'tables': [{
                'name': 'storage_project_brain_projects',
                'keys': ['owner_user_id', 'project_key'],
                'columns': ['owner_user_id', 'project_key', 'head_sequence',
                            'checkpoint_sequence', 'projection_json',
                            'updated_at_ms'],
            }],
            'operations': OPERATOR_SESSION['projectBrainOperations'],
            'verification': OPERATOR_SESSION['projectBrainSnapshot'],
        },
        {
            'name': 'optimizer',
            'tables': [
                {
                    'name': 'optimizer_proposals',
                    'keys': ['user_id', 'id'],
                    'columns': [
                        'user_id', 'id', 'created_at', 'title', 'rationale',
                        'action_type', 'action_args', 'severity', 'confidence',
                        'evidence', 'status', 'status_reason',
                    ],
                },
                {
                    'name': 'optimizer_action_log',
                    'keys': ['user_id', 'id'],
                    'columns': [
                        'user_id', 'id', 'proposal_id', 'applied_at',
                        'expires_at', 'pre_metric', 'outcome_metric',
                        'outcome_recorded_at', 'reverted_at', 'revert_reason',
                    ],
                },
            ],
            'operations': OPERATOR_SESSION['optimizerOperations'],
            'verification': OPERATOR_SESSION['optimizerSnapshot'],
        },
        {
            'name': 'log_aggregates',
            'tables': [{
                'name': 'log_aggregates',
                'keys': ['fingerprint'],
                'columns': ['fingerprint', 'level', 'logger', 'template',
                            'sample', 'count', 'first_seen', 'last_seen'],
            }],
            'operations': OPERATOR_SESSION['logAggregateOperations'],
            'verification': OPERATOR_SESSION['logAggregateSnapshot'],
        },
        {
            'name': 'billing_payments',
            'tables': [{
                'name': 'billing_payments',
                'keys': ['id'],
                'columns': ['id', 'user_id', 'provider', 'provider_id',
                            'amount_minor', 'currency', 'credit_micro',
                            'status', 'created_at', 'settled_at', 'raw'],
            }],
            'operations': OPERATOR_SESSION['billingPaymentOperations'],
            'verification': OPERATOR_SESSION['billingPaymentSnapshot'],
        },
        {
            'name': 'billing_redeem_codes',
            'tables': [{
                'name': 'billing_redeem_codes',
                'keys': ['code'],
                'columns': ['code', 'amount_micro', 'batch', 'created_by',
                            'created_at', 'expires_at', 'redeemed_by',
                            'redeemed_at', 'note'],
            }],
            'operations': OPERATOR_SESSION['billingRedeemOperations'],
            'verification': OPERATOR_SESSION['billingRedeemSnapshot'],
        },
        {
            'name': 'queue_items',
            'tables': [{
                'name': 'storage_queue_items',
                'keys': ['id'],
                'columns': ['id', 'user_id', 'conv_id', 'payload_json',
                            'config_json', 'position', 'kind', 'priority',
                            'created_at_ms', 'leased_until_ms',
                            'lease_task_id', 'input_turn_id',
                            'output_turn_id', 'attempt_id'],
            }],
            'operations': OPERATOR_SESSION['queueItemOperations'],
            'verification': OPERATOR_SESSION['queueItemSnapshot'],
        },
        {
            'name': 'autopilot_markers',
            'tables': [{
                'name': 'storage_autopilot_markers',
                'keys': ['conv_id'],
                'columns': ['conv_id', 'user_id', 'queue_id', 'config_json',
                            'created_at_ms'],
            }],
            'operations': OPERATOR_SESSION['queueAutopilotMarkerOperations'],
            'verification': OPERATOR_SESSION['queueAutopilotMarkerSnapshot'],
        },
        {
            'name': 'timers',
            'tables': [
                {
                    'name': 'storage_timers',
                    'keys': ['id'],
                    'columns': ['id', 'user_id', 'conv_id', 'source_task_id',
                                'check_instruction', 'check_command',
                                'continuation_message', 'poll_interval',
                                'max_polls', 'poll_count', 'status',
                                'tools_config', 'created_at', 'updated_at',
                                'triggered_at', 'cancelled_at',
                                'execution_task_id', 'last_poll_at',
                                'last_poll_decision', 'last_poll_reason',
                                'condition_kind', 'condition_command',
                                'condition_regex', 'promotion_streak',
                                'fallback_streak', 'promoted_at', 'origin'],
                },
                {
                    'name': 'storage_timer_poll_log',
                    'keys': ['id'],
                    'columns': ['id', 'timer_id', 'poll_time', 'decision',
                                'reason', 'check_output', 'tokens_used',
                                'model', 'poll_id', 'raw_output', 'tier',
                                'predicate_matched', 'llm_agreed'],
                },
            ],
            'operations': (OPERATOR_SESSION['timerOperations']
                           + OPERATOR_SESSION['timerPollOperations']),
            'verification': (OPERATOR_SESSION['timerSnapshot'] + ';'
                             + OPERATOR_SESSION['timerPollSnapshot']),
        },
        {
            'name': 'scheduled_tasks',
            'tables': [
                {
                    'name': 'storage_scheduled_tasks',
                    'keys': ['id'],
                    'columns': ['id', 'user_id', 'system_key', 'name',
                                'schedule', 'task_type', 'command',
                                'description', 'enabled', 'notify_on_failure',
                                'notify_on_success', 'max_runtime', 'last_run',
                                'last_result', 'last_status', 'run_count',
                                'fail_count', 'created_at', 'updated_at',
                                'target_conv_id', 'source_conv_id',
                                'tools_config', 'poll_count', 'last_poll_at',
                                'last_poll_decision', 'last_poll_reason',
                                'last_execution_at', 'last_execution_task_id',
                                'last_execution_status', 'execution_count',
                                'max_executions', 'expires_at',
                                'condition_kind', 'condition_command',
                                'condition_regex', 'promotion_streak',
                                'fallback_streak', 'promoted_at'],
                },
                {
                    'name': 'storage_proactive_poll_log',
                    'keys': ['id'],
                    'columns': ['id', 'task_id', 'poll_time', 'decision',
                                'reason', 'status_snapshot', 'model',
                                'tokens_used', 'execution_task_id', 'tier',
                                'predicate_matched', 'llm_agreed'],
                },
            ],
            'operations': (OPERATOR_SESSION['schedulerTaskOperations']
                           + OPERATOR_SESSION['schedulerPollOperations']),
            'verification': (OPERATOR_SESSION['schedulerTaskSnapshot'] + ';'
                             + OPERATOR_SESSION['schedulerPollSnapshot']),
        },
        {
            'name': 'worker_jobs',
            'tables': [{
                'name': 'storage_worker_jobs',
                'optionalColumns': ['lease_authority_epoch', 'wake_sequence'],
                'keys': ['task_id'],
                'columns': ['task_id', 'user_id', 'tenant_id', 'task_kind',
                            'payload_json', 'idempotency_key',
                            'request_digest', 'status', 'priority',
                            'available_at_ms', 'claim_owner',
                            'lease_deadline_ms', 'fencing_token', 'attempt_no',
                            'heartbeat_at_ms', 'cancel_sequence',
                            'cancel_requested_at_ms', 'cancel_reason',
                            'replay_cursor', 'result_ref', 'error_json',
                            'created_at_ms', 'updated_at_ms',
                            'terminal_at_ms'],
            }],
            'operations': OPERATOR_SESSION['workerJobOperations'],
            'verification': OPERATOR_SESSION['workerJobSnapshot'],
        },
    ],
}

PLAN['sourceMetadataPolicy'] = SOURCE_METADATA_POLICY
PLAN['legacyProjectBrainCutoverPolicy'] = LEGACY_PROJECT_BRAIN_CUTOVER_POLICY
PLAN['retiredArchivePolicy'] = {
    'namespace': retired.NAMESPACE, 'maximumRows': retired.MAX_ARCHIVE_ROWS,
    'maximumBytes': retired.MAX_ARCHIVE_BYTES,
    'maximumEncodedDocumentBytes': 2 * 1024 * 1024,
    'semantics': 'inactive-typed-rows;explicit-operator-custody;no-live-state-replay',
}
PLAN['domains'].extend({
    'name': 'retired.' + table['name'], 'tables': [table],
    'operations': OPERATOR_SESSION['recordOperations'],
    'verification': 'typed-all-column-archive;exact-row-identity;independent-catalog',
} for table in retired.TABLES)
PLAN['retiredLargeArchivePolicy'] = {'namespace': retired_large.NAMESPACE, **retired_large.LIMITS}
PLAN['domains'].extend({
    'name': 'retired_large.' + table['name'], 'tables': [table],
    'operations': OPERATOR_SESSION['recordOperations'],
    'verification': 'typed-raw-cell-bytes;parts-before-row-manifest;independent-part-and-row-catalogs',
} for table in retired_large.TABLES)

PLAN_SHA256 = hashlib.sha256(
    json.dumps(PLAN, sort_keys=True, separators=(',', ':'),
               ensure_ascii=True).encode('ascii'),
).hexdigest()


# Tenant-global paper budget ledgers: domain name → (source namespace,
# restore operation, get operation). The native target stores them in a
# fixed ledger layout rather than as generic records, so they replay only
# through their dedicated migration operations.
_PAPER_BUDGET_DOMAINS: dict[str, tuple[str, str, str]] = {
    'paper_build_budget': (
        'paper.build.budget.v1',
        'migration.paper.build.budget.restore',
        'migration.paper.build.budget.get',
    ),
    'paper_document_budget': (
        'paper.document.budget.v1',
        'migration.paper.document.budget.restore',
        'migration.paper.document.budget.get',
    ),
}
_PAPER_BUDGET_NAMESPACES: tuple[str, ...] = tuple(
    mapping[0] for mapping in _PAPER_BUDGET_DOMAINS.values())


def _plan_domain(name: str) -> dict[str, Any]:
    for domain in PLAN['domains']:
        if domain['name'] == name:
            return domain
    raise MigrationImportError(f'migration plan is missing {name}')


def _plan_domain_index(name: str) -> int:
    for index, domain in enumerate(PLAN['domains']):
        if domain['name'] == name:
            return index
    raise MigrationImportError(f'migration plan is missing {name}')


def _plan_table(domain_name: str, table_name: str | None = None) -> dict[str, Any]:
    tables = _plan_domain(domain_name)['tables']
    if table_name is None:
        return tables[0]
    for table in tables:
        if table['name'] == table_name:
            return table
    raise MigrationImportError(f'migration plan is missing {table_name}')

AfterVerified = Callable[[str, dict[str, Any]], None]
__all__ = [
    'MigrationImportError', 'PLAN', 'PLAN_SHA256',
    'compute_binding', 'certify_rollback', 'import_sqlite_authority', 'main',
]


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(',', ':'),
            ensure_ascii=True, allow_nan=False,
        ).encode('ascii')
    except (TypeError, ValueError, RecursionError) as exc:
        raise MigrationImportError('migration document is not canonical JSON') from exc


def _json_load(value: Any) -> Any:
    if value is None or value == b'' or value == '':
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode('utf-8')
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise MigrationImportError('stored JSON is invalid') from exc
    raise MigrationImportError('stored JSON is not a document')


def _int(value: Any) -> int:
    if type(value) is int:
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise MigrationImportError('migration integer field is invalid')



def _nonneg_int(value: Any) -> int:
    parsed = _int(value)
    if parsed < 0:
        raise MigrationImportError('migration integer field is invalid')
    return parsed


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise MigrationImportError('migration numeric field is invalid')


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        # SQLite type affinity let legacy writers store UTF-8 text as BLOB;
        # admit it only when it decodes exactly (mirrors _json_load's rule).
        try:
            return bytes(value).decode('utf-8')
        except UnicodeDecodeError:
            pass
    raise MigrationImportError('migration text field is invalid')



def _tenant_user_document(row: dict[str, Any]) -> dict[str, Any]:
    metadata = _json_load(row['metadata'])
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise MigrationImportError('tenant user metadata is invalid')
    user_id = _text(row['id'])
    email = _text(row['email']).strip().lower()
    password_hash = _text(row['password_hash'] or '')
    display_name = _text(row['display_name'] or '')
    role = _text(row['role']).strip()
    status = _text(row['status']).strip()
    owner_user_id = _nonneg_int(row['owner_user_id'])
    verified = _nonneg_int(row['email_verified'] or 0)
    if (not user_id or len(user_id) > 256 or not email or len(email) > 320
            or len(password_hash) > 512 or len(display_name) > 256
            or owner_user_id < 2 or verified not in (0, 1)
            or role not in ('user', 'admin')
            or status not in ('active', 'suspended', 'deleted')):
        raise MigrationImportError('tenant user snapshot is invalid')
    return {
        'id': user_id,
        'owner_user_id': owner_user_id,
        'email': email,
        'password_hash': password_hash,
        'display_name': display_name,
        'role': role,
        'status': status,
        'created_at': _nonneg_int(row['created_at']),
        'last_login_at': _nonneg_int(row['last_login_at'] or 0),
        'email_verified': bool(verified),
        'metadata': metadata,
    }



def _conversation_document(row: dict[str, Any]) -> dict[str, Any]:
    identity = _text(row['id'])
    if not identity or len(identity) > 256:
        raise MigrationImportError('conversation identity is invalid')
    title = _text(row['title'] if row['title'] is not None else 'New Chat')
    if not title:
        title = 'New Chat'
    title = title[:500]
    settings = _json_load(row['settings_json'])
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise MigrationImportError('conversation settings are invalid')
    user_id = _int(row['user_id'])
    created_at_ms = _int(row['created_at_ms'])
    updated_at_ms = _int(row['updated_at_ms'])
    revision = _nonneg_int(row.get('rev', 0))
    if (revision >= 2**64 or user_id <= 0 or created_at_ms < 0 or updated_at_ms < 0
            or created_at_ms >= 2**64 or updated_at_ms >= 2**64
            or updated_at_ms < created_at_ms):
        raise MigrationImportError('conversation snapshot is invalid')
    return {
        'id': identity,
        'user_id': user_id,
        'title': title,
        'created_at_ms': created_at_ms,
        'updated_at_ms': updated_at_ms,
        'settings': settings,
        'rev': revision,
    }


def _conversation_from_target(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise MigrationImportError('conversation snapshot is invalid')
    metadata = row.get('metadata')
    if not isinstance(metadata, dict):
        raise MigrationImportError('conversation snapshot is invalid')
    identity = row.get('id') or metadata.get('id')
    title = metadata.get('title')
    if not title:
        title = 'New Chat'
    settings = metadata.get('settings')
    if settings is None:
        settings = {}
    return _conversation_document({
        'id': identity,
        'user_id': metadata.get('user_id'),
        'title': title,
        'created_at_ms': metadata.get('created_at'),
        'updated_at_ms': metadata.get('updated_at'),
        'settings_json': settings,
        'rev': metadata.get('rev'),
    })


def _session_owner(session: OfflineMigrationSession) -> int:
    owner = getattr(session, 'owner_id', None)
    if type(owner) is not int or owner <= 0:
        raise MigrationImportError('migration session owner is invalid')
    return owner

def _digest_pairs(pairs: Sequence[tuple[tuple[Any, ...], dict[str, Any]]]) -> str:
    parts = [
        (key, hashlib.sha256(_canonical(document)).hexdigest())
        for key, document in pairs
    ]
    parts.sort()
    digest = hashlib.sha256()
    digest.update(str(len(parts)).encode('ascii'))
    for key, item in parts:
        digest.update(_canonical({'d': item, 'k': list(key)}))
    return digest.hexdigest()


def _deadline() -> int:
    return int(time.time() * 1000) + _REQUEST_DEADLINE_MS


def _query(session: OfflineMigrationSession, operation: str, payload: dict[str, Any]) -> Any:
    # Read-only verification may bridge a transient database_unavailable (an
    # engine pack-publication race window observed on slow volumes): at most
    # two bounded retries, never for commands, and persistent failures still
    # abort the import fail-closed.
    for attempt in range(3):
        response = session.request(
            operation, payload, command_id=None, deadline_unix_ms=_deadline())
        try:
            response.raise_for_error()
        except StorageV2Error as exc:
            if exc.status == 9 and attempt < 2:
                time.sleep(1.0 + attempt)
                continue
            exc.add_note(f'migration op={operation}')
            raise
        return response.json()
    raise MigrationImportError(f'{operation} failed: retry budget exhausted')


def _command(session: OfflineMigrationSession, operation: str, payload: dict[str, Any]) -> Any:
    response = session.request(
        operation, payload, command_id=uuid.uuid4().hex,
        deadline_unix_ms=_deadline(),
    )
    try:
        response.raise_for_error()
    except StorageV2Error as exc:
        exc.add_note(f'migration op={operation}')
        raise
    return response.json()


def _schema_version(source: SQLiteMigrationSource) -> int:
    if 'storage_meta' not in source.tables:
        raise MigrationImportError('source is not a Tofu SQLite authority')
    for row in source.rows(
        'storage_meta', ('meta_key', 'meta_value'), ('meta_key',), batch_rows=64,
    ):
        if row['meta_key'] == 'schema_version':
            try:
                version = int(row['meta_value'])
            except (TypeError, ValueError) as exc:
                raise MigrationImportError('source schema version is invalid') from exc
            if version != SCHEMA_VERSION:
                raise MigrationImportError(
                    f'source schema version {version} is not {SCHEMA_VERSION}')
            return version
    raise MigrationImportError('source schema version is missing')


def _source_authority_uuid(source: SQLiteMigrationSource) -> str:
    for row in source.rows(
        'storage_meta', ('meta_key', 'meta_value'), ('meta_key',), batch_rows=64,
    ):
        if row['meta_key'] == 'authority_uuid':
            try:
                return str(uuid.UUID(str(row['meta_value'])))
            except (TypeError, ValueError) as exc:
                raise MigrationImportError('source authority identity is invalid') from exc
    raise MigrationImportError('source authority identity is missing')


def _row_digest(table: str, row: dict[str, Any]) -> bytes:
    encoded: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (bytes, bytearray, memoryview)):
            encoded[key] = hashlib.sha256(bytes(value)).hexdigest()
        else:
            encoded[key] = value
    return _canonical({'row': encoded, 'table': table})


def compute_binding(
    source: SQLiteMigrationSource, target_authority_uuid: str,
) -> MigrationBinding:
    schema_version = _schema_version(source)
    from lib.storage_authority_sql.migration.tofudb_migration_projection import require_materialized_turns
    require_materialized_turns(source)
    retired.preflight(source)
    retired_large.preflight(source)
    digest = hashlib.sha256()
    for domain in PLAN['domains']:
        for table in domain['tables']:
            if table['name'] not in source.tables:
                continue
            columns = _source_table_columns(source, table)
            if table.get('retiredLarge'):
                columns = table['keys']
            elif table['name'] in turn_payload.TABLES:
                columns = turn_payload.metadata_columns(table)
            elif table['name'] == 'storage_attempt_events':
                columns = [column for column in columns if column != 'payload_json']
            elif domain['name'] == 'frozen_transcripts' or table['name'] == 'storage_conversation_trash':
                columns = [column for column in columns if column != 'messages_json']
            if table['name'] in _DIGEST_KEY_TABLES:
                binding_rows = _digest_stream(
                    source, {**table, 'columns': columns},
                    batch_rows=MAX_SOURCE_BATCH_ROWS)
            else:
                binding_rows = source.rows(
                    table['name'], columns,
                    table['keys'], batch_rows=MAX_SOURCE_BATCH_ROWS,
                )
            for row in binding_rows:
                if table.get('retiredLarge'):
                    key = _pk(table, row)
                    cells, _, _ = retired_large._describe(source, table, key)
                    row = {**row, '_archive_snapshot': retired_large._root(table, key, cells)}
                elif table['name'] in turn_payload.TABLES:
                    row = {**row, **turn_payload.body_fingerprints(source, table, row)}
                elif domain['name'] == 'frozen_transcripts':
                    from lib.storage_authority_sql.migration.tofudb_migration_transcript import frozen_source_metadata
                    row = {**row, 'archive': frozen_source_metadata(source, row['id'], row['user_id'], row['created_at_ms'])}
                elif table['name'] == 'storage_conversation_trash':
                    from lib.storage_authority_sql.migration.tofudb_migration_transcript import frozen_source_metadata
                    row = {**row, 'archive': frozen_source_metadata(source, row['conversation_id'], row['user_id'], row['created_at_ms'], trashed=True)}
                elif table['name'] == 'storage_records' and domain['name'] == 'task_results':
                    if row['namespace'] != 'task_results':
                        continue
                    # Binding-time shape validation only; domain drivers adopt
                    # ownerless legacy rows into their explicit session scope.
                    _task_result_document(row, default_owner=1)
                elif table['name'] == 'storage_records' and domain['name'] == 'continuation':
                    if row['namespace'] != 'model.continuation.v1':
                        continue
                    _record_document(row)
                elif table['name'] == 'storage_records' and domain['name'] in _PAPER_BUDGET_DOMAINS:
                    if row['namespace'] != _PAPER_BUDGET_DOMAINS[domain['name']][0]:
                        continue
                    _record_document(row)
                elif table['name'] == 'storage_records':
                    # Dedicated-domain rows are hashed here too so the binding
                    # covers every row of the shared source table.
                    _record_document(row)
                elif table['name'] == 'byo_providers':
                    _provider_document(row)
                elif table['name'] == 'tenant_users':
                    _tenant_user_document(row)
                elif table['name'] == 'storage_conversations':
                    _conversation_document(row)
                elif table['name'] == 'storage_generation_attempts':
                    _attempt_document(row)
                elif table['name'] == 'storage_attempt_events':
                    row = {**row, 'event_manifest': _attempt_event_manifest(source, row)}
                elif table['name'] == 'storage_raw_archives':
                    _raw_archive_document(row)
                elif table['name'] == 'storage_conversation_turns':
                    _turn_document(row)
                elif domain['name'] == 'paper':
                    _paper_document(table['name'], row)
                elif table['name'] == 'storage_knowledge_settings':
                    _knowledge_settings_document(row)
                digest.update(_row_digest(table['name'], row))
    return MigrationBinding(
        source_sha256=digest.hexdigest(),
        source_schema_version=schema_version,
        target_authority_uuid=target_authority_uuid,
        plan_sha256=PLAN_SHA256,
    )


def _pk(table: dict[str, Any], row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row[column] for column in table['keys'])


def _key_le(left: Sequence[Any], right: Sequence[Any]) -> bool:
    return tuple(left) <= tuple(right)


def _credential_document(row: dict[str, Any]) -> dict[str, Any]:
    scopes = _json_load(row['scopes'])
    metadata = _json_load(row['metadata'])
    if not isinstance(scopes, list) or not all(isinstance(item, str) and item for item in scopes):
        raise MigrationImportError('credential scopes are invalid')
    if not isinstance(metadata, dict):
        raise MigrationImportError('credential metadata is invalid')
    created_at = _optional_float(row['created_at'])
    if created_at is None:
        raise MigrationImportError('credential created_at is required')
    return {
        'id': _text(row['id']),
        'owner_user_id': _int(row['owner_user_id']),
        'account_user_id': _text(row['account_user_id'] or '').strip(),
        'tenant_id': _text(row['tenant_id'] or '').strip(),
        'name': _text(row['name']).strip(),
        'prefix': _text(row['prefix']),
        'scopes': sorted(set(scopes)),
        'rate_limit_rpm': _int(row['rate_limit_rpm'] or 0),
        'rate_limit_tpd': _int(row['rate_limit_tpd'] or 0),
        'created_at': created_at,
        'last_used_at': _optional_float(row['last_used_at']),
        'expires_at': _optional_float(row['expires_at']),
        'disabled': bool(row['disabled']),
        'revoked_at': _optional_float(row['revoked_at']),
        'metadata': metadata,
    }


def _credential_from_target(row: dict[str, Any]) -> dict[str, Any]:
    return _credential_document({
        'id': row['id'],
        'owner_user_id': row['owner_user_id'],
        'account_user_id': row.get('account_user_id') or '',
        'tenant_id': row.get('tenant_id') or '',
        'name': row['name'],
        'prefix': row['prefix'],
        'scopes': row['scopes'],
        'rate_limit_rpm': row.get('rate_limit_rpm') or 0,
        'rate_limit_tpd': row.get('rate_limit_tpd') or 0,
        'created_at': row['created_at'],
        'last_used_at': row.get('last_used_at'),
        'expires_at': row.get('expires_at'),
        'disabled': row.get('disabled'),
        'revoked_at': row.get('revoked_at'),
        'metadata': row.get('metadata') or {},
    })


def _record_document(row: dict[str, Any]) -> dict[str, Any]:
    namespace = _text(row['namespace'])
    key = _text(row['record_key'])
    if not namespace or len(namespace) > 128:
        raise MigrationImportError('record namespace is invalid')
    if (not key or 2 + len(namespace.encode('utf-8')) + len(key.encode('utf-8'))
            > MAX_ENTITY_KEY_BYTES):
        raise MigrationImportError('record identity exceeds native key bound')
    raw = _text(row['value_json'])
    if not raw.strip():
        raise MigrationImportError('stored JSON is invalid')
    _canonical(_json_load(raw))  # Validate finite JSON, while retaining original bytes.
    version, updated = _int(row['version']), _int(row['updated_at_ms'])
    if not 0 < version < 2**64 or not 0 < updated < 2**64:
        raise MigrationImportError('record snapshot metadata must be positive u64')
    return {'key': key, 'namespace': namespace, 'value_json': raw,
            'version': version, 'updated_at_ms': updated}


def _wallet_document(row: dict[str, Any]) -> dict[str, Any]:
    return {
        'user_id': _text(row['user_id']),
        'balance_micro': _int(row['balance_micro']),
        'currency': _text(row['currency']),
        'low_balance_alert_micro': _int(row['low_balance_alert_micro']),
        'updated_at': _int(row['updated_at']),
    }


def _ledger_document(row: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': _text(row['id']),
        'user_id': _text(row['user_id']),
        'ts': _int(row['ts']),
        'amount_micro': _int(row['amount_micro']),
        'kind': _text(row['kind']),
        'ref_type': row['ref_type'] or '',
        'ref_id': row['ref_id'] or '',
        'balance_after_micro': _int(row['balance_after_micro']),
        'note': row['note'] or '',
    }


# Tables whose declared single-column primary key is a 32-byte BLOB (SHA-256
# identity). The generic keyset scanner admits only TEXT/INTEGER keys, so these
# stream through the certified digest reader: SQL orders raw bytes, the exposed
# cursor identity is lowercase hex. Keep in sync with receipt_source.TABLE_KEYS.
_DIGEST_KEY_TABLES = {'storage_command_receipts_v2': 'command_key'}


def _digest_stream(source, table, *, after=None, batch_rows):
    key_column = _DIGEST_KEY_TABLES[table['name']]
    if after is not None and (not isinstance(after, str)):
        raise MigrationImportError(f"invalid digest resume cursor for {table['name']}")
    return source.digest_key_rows(table['name'], table['columns'], key_column,
                                  after=after, batch_rows=batch_rows)


def _source_table_columns(source, table):
    """Include declared later-schema fields in snapshot bytes and source binding."""
    columns = list(table['columns'])
    optional = table.get('optionalColumns', ())
    if optional:
        available = source.table_columns(table['name'])
        columns.extend(column for column in optional if column in available and column not in columns)
    return columns


def _scan(
    source: SQLiteMigrationSource, table: dict[str, Any], *,
    until: Sequence[Any] | None = None, complete: bool = True,
) -> Iterator[dict[str, Any]]:
    if table['name'] not in source.tables:
        return
    if table['name'] in _DIGEST_KEY_TABLES:
        iterator = _digest_stream(source, table, batch_rows=MAX_SOURCE_BATCH_ROWS)
    elif table['name'] in turn_payload.TABLES:
        iterator = turn_payload.rows(source, table, batch_rows=MAX_SOURCE_BATCH_ROWS)
    else:
        iterator = source.rows(table['name'], _source_table_columns(source, table), table['keys'],
                               batch_rows=MAX_SOURCE_BATCH_ROWS)
    for row in iterator:
        key = _pk(table, row)
        if not complete and until is not None and not _key_le(key, until):
            return
        yield row
        if not complete and until is not None and tuple(key) == tuple(until):
            return


def _source_credential_pairs(
    source: SQLiteMigrationSource, *, until: Sequence[Any] | None, complete: bool,
) -> tuple[list[tuple[tuple[Any, ...], dict[str, Any]]], list[str]]:
    table = _plan_table('credentials')
    pairs = []
    revoked_hashes = []
    for row in _scan(source, table, until=until, complete=complete):
        document = _credential_document(row)
        pairs.append((_pk(table, row), document))
        if document['revoked_at'] is not None:
            revoked_hashes.append(_text(row['secret_hash']))
    return pairs, revoked_hashes


def _list_credentials(session: OfflineMigrationSession, boundaries: set[tuple[int, str]]) -> (
    list[tuple[tuple[Any, ...], dict[str, Any]]]
):
    pairs = []
    seen: set[str] = set()
    for owner_user_id, tenant_id in sorted(boundaries):
        rows = _query(session, 'credential.list', {
            'owner_user_id': owner_user_id, 'tenant_id': tenant_id,
        })
        if not isinstance(rows, list):
            raise MigrationImportError('credential.list did not return a catalog')
        for row in rows:
            if not isinstance(row, dict) or row.get('id') in seen:
                raise MigrationImportError('credential.list catalog is invalid')
            seen.add(row['id'])
            pairs.append(((row['id'],), _credential_from_target(row)))
    return pairs


def _identify_credentials(
    session: OfflineMigrationSession, hashes: Sequence[str],
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    pairs = []
    for secret_hash in hashes:
        row = _query(session, 'credential.identify', {'secret_hash': secret_hash})
        if not isinstance(row, dict) or 'id' not in row:
            raise MigrationImportError('revoked credential is missing on the target')
        pairs.append(((row['id'],), _credential_from_target(row)))
    return pairs


def _verify_credentials(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, *,
    until: Sequence[Any] | None, complete: bool,
) -> tuple[int, str]:
    source_pairs, revoked_hashes = _source_credential_pairs(
        source, until=until, complete=complete)
    boundaries = {
        (document['owner_user_id'], document['tenant_id'])
        for _key, document in source_pairs
    }
    live = [item for item in source_pairs if item[1]['revoked_at'] is None]
    revoked_rows = [item for item in source_pairs if item[1]['revoked_at'] is not None]
    target_live = _list_credentials(session, boundaries)
    target_revoked = _identify_credentials(session, revoked_hashes)
    if {item[0] for item in target_live} != {item[0] for item in live}:
        raise MigrationImportError('live credential catalogs differ')
    if {item[0] for item in target_revoked} != {item[0] for item in revoked_rows}:
        raise MigrationImportError('revoked credential catalogs differ')
    source_digest = _digest_pairs(source_pairs)
    target_digest = _digest_pairs(target_live + target_revoked)
    if source_digest != target_digest or len(source_pairs) != len(target_live) + len(target_revoked):
        raise MigrationImportError('credential source and target digests differ')
    return len(source_pairs), source_digest


def _provider_document(row: dict[str, Any]) -> dict[str, Any]:
    if 'models_json' in row:
        models = _json_load(row['models_json'])
        headers = _json_load(row['extra_headers_json'])
    else:
        models = row.get('models')
        headers = row.get('extra_headers')
    if not isinstance(models, list) or len(models) > 64:
        raise MigrationImportError('provider models are invalid')
    if not isinstance(headers, dict) or len(headers) > 16:
        raise MigrationImportError('provider extra headers are invalid')
    created_at = _optional_float(row['created_at'])
    updated_at = _optional_float(row['updated_at'])
    if created_at is None or updated_at is None:
        raise MigrationImportError('provider timestamps are required')
    ciphertext = row.get('api_key_ciphertext')
    return {
        'id': _text(row['id']),
        'owner_user_id': _int(row['owner_user_id']),
        'tenant_id': _text(row.get('tenant_id') or '').strip(),
        'name': _text(row['name']).strip(),
        'base_url': _text(row['base_url']).strip(),
        'api_key_ciphertext': _text(ciphertext or '').strip(),
        'key_hint': _text(row.get('key_hint') or '').strip(),
        'models': models,
        'extra_headers': headers,
        'thinking_format': _text(row.get('thinking_format') or '').strip(),
        'disabled': bool(row['disabled']),
        'created_at': created_at,
        'updated_at': updated_at,
        'last_used_at': _optional_float(row.get('last_used_at')),
    }


def _verify_provider(
    document: dict[str, Any], session: OfflineMigrationSession,
) -> None:
    observed = _query(session, 'provider.get', {
        'owner_user_id': document['owner_user_id'],
        'tenant_id': document['tenant_id'],
        'provider_id': document['id'],
    })
    if not isinstance(observed, dict) or 'api_key_ciphertext' not in observed:
        raise MigrationImportError('provider.get did not return a snapshot')
    if _provider_document(observed) != document:
        raise MigrationImportError('provider source and target catalogs differ')


def _source_provider_pairs(
    source: SQLiteMigrationSource, *,
    until: Sequence[Any] | None, complete: bool,
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    table = _plan_table('providers')
    return [
        (_pk(table, row), _provider_document(row))
        for row in _scan(source, table, until=until, complete=complete)
    ]


def _list_providers(
    session: OfflineMigrationSession, boundaries: set[tuple[int, str]],
) -> list[dict[str, Any]]:
    rows = []
    seen: set[str] = set()
    for owner_user_id, tenant_id in sorted(boundaries):
        page = _query(session, 'provider.list', {
            'owner_user_id': owner_user_id, 'tenant_id': tenant_id,
        })
        if not isinstance(page, list) or len(page) > 32:
            raise MigrationImportError('provider.list did not return a catalog')
        for row in page:
            if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                    or row['id'] in seen or 'api_key_ciphertext' in row):
                raise MigrationImportError('provider.list catalog is invalid')
            seen.add(row['id'])
            rows.append(row)
    return rows


def _verify_providers(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, *,
    until: Sequence[Any] | None, complete: bool,
) -> tuple[int, str]:
    source_pairs = _source_provider_pairs(
        source, until=until, complete=complete)
    for _key, document in source_pairs:
        _verify_provider(document, session)
    if complete:
        boundaries = {
            (document['owner_user_id'], document['tenant_id'])
            for _key, document in source_pairs
        }
        listed = _list_providers(session, boundaries)
        if {row['id'] for row in listed} != {document['id'] for _key, document in source_pairs}:
            raise MigrationImportError('provider source and target catalogs differ')
    return len(source_pairs), _digest_pairs(source_pairs)


def _replay_provider(session: OfflineMigrationSession, row: dict[str, Any]) -> None:
    document = _provider_document(row)
    payload = {
        'provider_id': document['id'],
        'owner_user_id': document['owner_user_id'],
        'tenant_id': document['tenant_id'],
        'name': document['name'],
        'base_url': document['base_url'],
        'api_key_ciphertext': document['api_key_ciphertext'],
        'key_hint': document['key_hint'],
        'models': document['models'],
        'extra_headers': document['extra_headers'],
        'thinking_format': document['thinking_format'],
        'created_at': document['created_at'],
    }
    try:
        _command(session, 'provider.create', payload)
    except StorageV2Error as exc:
        if exc.code != 'database_conflict':
            raise
        observed = _query(session, 'provider.get', {
            'owner_user_id': document['owner_user_id'],
            'tenant_id': document['tenant_id'],
            'provider_id': document['id'],
        })
        if not isinstance(observed, dict) or observed.get('id') != document['id']:
            raise MigrationImportError('provider identity already occupies the target') from exc
    _command(session, 'provider.update', {
        'owner_user_id': document['owner_user_id'],
        'tenant_id': document['tenant_id'],
        'provider_id': document['id'],
        'updated_at': document['updated_at'],
        'updates': {
            'name': document['name'],
            'base_url': document['base_url'],
            'api_key_ciphertext': document['api_key_ciphertext'],
            'key_hint': document['key_hint'],
            'models': document['models'],
            'extra_headers': document['extra_headers'],
            'thinking_format': document['thinking_format'],
            'disabled': document['disabled'],
        },
    })
    if document['last_used_at'] is not None:
        _command(session, 'provider.touch', {
            'owner_user_id': document['owner_user_id'],
            'tenant_id': document['tenant_id'],
            'provider_id': document['id'],
            'used_at': document['last_used_at'],
        })
    _verify_provider(document, session)


def _record_chain(previous: str, document: dict[str, Any]) -> str:
    return hashlib.sha256(bytes.fromhex(previous) + _canonical(document)).hexdigest()


def _reject_managed_record_namespace(namespace: str) -> None:
    if (namespace in (retired.NAMESPACE, retired_large.NAMESPACE)
            or namespace in OPERATOR_SESSION['excludedRecordNamespaces']
            or namespace.startswith(tuple(OPERATOR_SESSION['excludedRecordPrefixes']))):
        raise MigrationImportError('record namespace requires a dedicated domain importer')


def _preflight_managed_record_namespaces(source: SQLiteMigrationSource) -> None:
    # Namespaces owned by a still-missing dedicated adapter must fail before
    # any domain writes; task_results, model.continuation.v1 and the paper
    # budget ledger rows now belong to their own domains.
    table = _plan_table('records')
    for row in _scan(source, table):
        if row['namespace'] in ('task_results', 'model.continuation.v1') + _PAPER_BUDGET_NAMESPACES:
            continue
        _reject_managed_record_namespace(_text(row['namespace']))


def _verify_record(source_document: dict[str, Any], session: OfflineMigrationSession) -> None:
    observed = _query(session, 'migration.record.get', {
        'namespace': source_document['namespace'], 'key': source_document['key']})
    if observed != source_document:
        raise MigrationImportError('record source and target catalogs differ')


def _verify_record_catalog(source: SQLiteMigrationSource, session: OfflineMigrationSession) -> int:
    # Discover every target namespace independently of the source. Values are
    # fetched individually; only one bounded identity page remains resident.
    table = _plan_table('records')
    cursor = None
    previous = b''
    count = 0
    while True:
        page = _query(session, 'migration.record.scan', {'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('records'), list)
                or len(page['records']) > OPERATOR_SESSION['maximumCatalogPageRows']):
            raise MigrationImportError('invalid migration record catalog page')
        for identity in page['records']:
            if (not isinstance(identity, dict) or set(identity) != {'namespace', 'key'}
                    or any(not isinstance(value, str) for value in identity.values())):
                raise MigrationImportError('invalid migration record catalog identity')
            ns, key = identity['namespace'], identity['key']
            encoded_ns = ns.encode('utf-8')
            if len(encoded_ns) > 65535:
                raise MigrationImportError('invalid migration record namespace')
            encoded = len(encoded_ns).to_bytes(2, 'big') + encoded_ns + key.encode('utf-8')
            if encoded <= previous:
                raise MigrationImportError('migration catalog order did not advance')
            previous = encoded
            if ns in (retired.NAMESPACE, retired_large.NAMESPACE):
                # Dedicated archived-row catalogs verify these against their
                # exact historical source tables, including on resume.
                continue
            row = (source.point(table['name'], table['columns'], table['keys'], (ns, key))
                   if table['name'] in source.tables else None)
            if row is None:
                raise MigrationImportError('record source and target catalogs differ: extra target key')
            document = _record_document(row)
            if document['namespace'] == 'task_results':
                raise MigrationImportError(
                    'task result target key is outside the generic record catalog')
            if document['namespace'] == 'model.continuation.v1':
                raise MigrationImportError(
                    'continuation target key is outside the generic record catalog')
            _reject_managed_record_namespace(document['namespace'])
            _verify_record(document, session)
            count += 1
        following = page.get('cursor')
        if following is None:
            return count
        if (not page['records'] or not isinstance(following, str)
                or following != previous.hex() or following == cursor):
            raise MigrationImportError('migration record cursor did not advance')
        cursor = following


def _verify_records(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, *,
    until: Sequence[Any] | None, complete: bool,
) -> tuple[int, str]:
    rows = 0
    digest = hashlib.sha256(b'tofu-db.record-snapshot-chain.v1').hexdigest()
    table = _plan_table('records')
    for row in _scan(source, table, until=until, complete=complete):
        if row['namespace'] in ('task_results', 'model.continuation.v1') + _PAPER_BUDGET_NAMESPACES:
            continue
        document = _record_document(row)
        _reject_managed_record_namespace(document['namespace'])
        _verify_record(document, session)
        digest = _record_chain(digest, document)
        rows += 1
    if complete and _verify_record_catalog(source, session) != rows:
        raise MigrationImportError('record source and target catalogs differ')
    return rows, digest


def _replay_credential(session: OfflineMigrationSession, row: dict[str, Any]) -> None:
    document = _credential_document(row)
    payload = {
        'credential_id': document['id'],
        'owner_user_id': document['owner_user_id'],
        'account_user_id': document['account_user_id'],
        'tenant_id': document['tenant_id'],
        'name': document['name'],
        'prefix': document['prefix'],
        'secret_hash': _text(row['secret_hash']),
        'scopes': document['scopes'],
        'rate_limit_rpm': document['rate_limit_rpm'],
        'rate_limit_tpd': document['rate_limit_tpd'],
        'created_at': document['created_at'],
        'metadata': document['metadata'],
    }
    if document['expires_at'] is not None:
        payload['expires_at'] = document['expires_at']
    try:
        _command(session, 'credential.create', payload)
    except StorageV2Error as exc:
        if exc.code != 'database_conflict':
            raise
        identified = _query(session, 'credential.identify', {
            'secret_hash': payload['secret_hash'],
        })
        if not isinstance(identified, dict) or identified.get('id') != document['id']:
            raise MigrationImportError('credential identity already occupies the target') from exc
    if document['last_used_at'] is not None:
        _command(session, 'credential.touch', {
            'owner_user_id': document['owner_user_id'],
            'tenant_id': document['tenant_id'],
            'credential_id': document['id'],
            'used_at': document['last_used_at'],
            'touch_if_before': document['last_used_at'],
        })
    updates: dict[str, Any] = {}
    if document['disabled'] and document['revoked_at'] is None:
        updates['disabled'] = True
    if updates:
        _command(session, 'credential.update', {
            'owner_user_id': document['owner_user_id'],
            'tenant_id': document['tenant_id'],
            'credential_id': document['id'],
            'updates': updates,
        })
    if document['revoked_at'] is not None:
        _command(session, 'credential.revoke', {
            'owner_user_id': document['owner_user_id'],
            'tenant_id': document['tenant_id'],
            'credential_id': document['id'],
            'revoked_at': document['revoked_at'],
        })


def _replay_record(session: OfflineMigrationSession, row: dict[str, Any]) -> dict[str, Any]:
    document = _record_document(row)
    if document['namespace'] == 'task_results':
        raise MigrationImportError(
            'task result row is outside the generic record domain')
    if document['namespace'] == 'model.continuation.v1':
        raise MigrationImportError(
            'continuation row is outside the generic record domain')
    _reject_managed_record_namespace(document['namespace'])
    _command(session, 'migration.record.restore', document)
    _verify_record(document, session)
    return document


def _publish(
    checkpoint: MigrationCheckpoint, domain: str, *, cursor: list[Any],
    rows: int, digest: str, complete: bool, after_verified_prefix: AfterVerified | None,
) -> None:
    checkpoint.record_verified_prefix(
        domain, cursor=cursor, source_rows=rows, source_sha256=digest,
        target_rows=rows, target_sha256=digest, complete=complete,
    )
    if after_verified_prefix is not None:
        entry = checkpoint.domain(domain)
        if entry is None:
            raise MigrationImportError('verified prefix was not published')
        after_verified_prefix(domain, entry)


def _resume_or_empty(
    checkpoint: MigrationCheckpoint, domain: str,
) -> dict[str, Any] | None:
    try:
        return checkpoint.domain(domain)
    except MigrationCheckpointError as exc:
        raise MigrationImportError(str(exc)) from exc


def _source_tenant_user_pairs(
    source: SQLiteMigrationSource, *, until: Sequence[Any] | None, complete: bool,
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    table = _plan_table('tenant_users')
    return [
        (_pk(table, row), _tenant_user_document(row))
        for row in _scan(source, table, until=until, complete=complete)
    ]


def _tenant_user_catalog(session: OfflineMigrationSession) -> Iterator[dict[str, Any]]:
    cursor = None
    previous = b''
    while True:
        page = _query(session, 'migration.tenant.user.scan', {'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']
                or any(not isinstance(row, dict) for row in page['rows'])):
            raise MigrationImportError('invalid tenant user catalog page')
        yield from page['rows']
        following = page.get('cursor')
        if following is None:
            return
        if (not isinstance(following, str) or not following.startswith('users:')
                or not page['rows']):
            raise MigrationImportError('invalid tenant user catalog cursor')
        try:
            raw = bytes.fromhex(following[len('users:'):])
        except ValueError:
            raise MigrationImportError('invalid tenant user catalog cursor') from None
        if not raw or len(raw) > MAX_ENTITY_KEY_BYTES or raw <= previous:
            raise MigrationImportError('tenant user catalog did not advance')
        previous, cursor = raw, following


def _verify_tenant_user(
    document: dict[str, Any], session: OfflineMigrationSession,
) -> None:
    observed = _query(session, 'migration.tenant.user.get', {'id': document['id']})
    if observed != document:
        raise MigrationImportError('tenant user source and target snapshots differ')


def _verify_tenant_user_catalog(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
) -> int:
    table = _plan_table('tenant_users')
    rows = 0
    for identity in _tenant_user_catalog(session):
        if set(identity) != {'id'} or not isinstance(identity['id'], str):
            raise MigrationImportError('invalid tenant user catalog identity')
        row = (
            source.point(table['name'], table['columns'], table['keys'], (identity['id'],))
            if table['name'] in source.tables else None
        )
        if row is None:
            raise MigrationImportError('tenant user target contains an extra account')
        _verify_tenant_user(_tenant_user_document(row), session)
        rows += 1
    return rows


def _verify_tenant_users(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, *,
    until: Sequence[Any] | None, complete: bool,
) -> tuple[int, str]:
    pairs = _source_tenant_user_pairs(source, until=until, complete=complete)
    for _key, document in pairs:
        _verify_tenant_user(document, session)
    if complete and _verify_tenant_user_catalog(source, session) != len(pairs):
        raise MigrationImportError('tenant user source and target catalogs differ')
    return len(pairs), _digest_pairs(pairs)


def _replay_tenant_user(session: OfflineMigrationSession, row: dict[str, Any]) -> None:
    document = _tenant_user_document(row)
    _command(session, 'migration.tenant.user.restore', document)
    _verify_tenant_user(document, session)


def _import_tenant_users(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    table = _plan_table('tenant_users')
    progress = _resume_or_empty(checkpoint, 'tenant_users')
    if progress is not None:
        until = None if progress['complete'] else progress['cursor']
        rows, digest = _verify_tenant_users(
            source, session, until=until, complete=progress['complete'])
        if rows != progress['rows'] or digest != progress['sha256']:
            raise MigrationImportError(
                'resumed tenant user target no longer matches progress')
        if progress['complete']:
            return
        after: Sequence[Any] | None = progress['cursor']
    else:
        after = None
    if table['name'] not in source.tables:
        rows, digest = _verify_tenant_users(
            source, session, until=None, complete=True)
        _publish(
            checkpoint, 'tenant_users', cursor=[], rows=rows, digest=digest,
            complete=True, after_verified_prefix=after_verified_prefix,
        )
        return
    _verify_tenant_user_catalog(source, session)
    last: list[Any] | None = None if after is None else list(after)
    pending = 0
    for row in source.rows(
        table['name'], table['columns'], table['keys'], after=after,
        batch_rows=batch_rows,
    ):
        _replay_tenant_user(session, row)
        last = list(_pk(table, row))
        pending += 1
        if pending < batch_rows:
            continue
        rows, digest = _verify_tenant_users(
            source, session, until=last, complete=False)
        _publish(
            checkpoint, 'tenant_users', cursor=last, rows=rows, digest=digest,
            complete=False, after_verified_prefix=after_verified_prefix,
        )
        pending = 0
        after = last
    rows, digest = _verify_tenant_users(
        source, session, until=None, complete=True)
    _publish(
        checkpoint, 'tenant_users', cursor=last or [], rows=rows, digest=digest,
        complete=True, after_verified_prefix=after_verified_prefix,
    )


def _source_conversation_pairs(
    source: SQLiteMigrationSource, *, until: Sequence[Any] | None, complete: bool,
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    table = _plan_table('conversations')
    return [
        (_pk(table, row), _conversation_document(row))
        for row in _scan(source, table, until=until, complete=complete)
    ]


def _conversation_owner_count(
    session: OfflineMigrationSession, owner_user_id: int,
) -> int:
    observed = _query(session, 'conversation.count', {'user_id': owner_user_id})
    if (not isinstance(observed, dict) or type(observed.get('count')) is not int
            or observed['count'] < 0):
        raise MigrationImportError('conversation.count did not return a catalog')
    return observed['count']


def _conversation_catalog(
    session: OfflineMigrationSession, owner_user_id: int,
) -> list[dict[str, Any]]:
    page_rows = OPERATOR_SESSION['maximumCatalogPageRows']
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    before_updated_at: int | None = None
    before_id = ''
    while True:
        payload: dict[str, Any] = {
            'user_id': owner_user_id,
            'catalog_page': True,
            'include_messages': False,
            'order_by': 'updated_at_desc',
            'limit': page_rows,
        }
        if before_updated_at is not None:
            payload['before_updated_at'] = before_updated_at
            payload['before_id'] = before_id
        page = _query(session, 'conversation.list', payload)
        if (not isinstance(page, dict)
                or not isinstance(page.get('items'), list)
                or type(page.get('has_more')) is not bool
                or len(page['items']) > page_rows):
            raise MigrationImportError('conversation.list did not return a catalog page')
        if page['has_more'] and not page['items']:
            raise MigrationImportError('conversation catalog page did not advance')
        if 'total_count' in page and type(page['total_count']) is not int:
            raise MigrationImportError('conversation.list catalog is invalid')
        for item in page['items']:
            if not isinstance(item, dict) or item.get('messages') not in (None, []):
                raise MigrationImportError('conversation.list catalog is invalid')
            identity = item.get('id')
            if not isinstance(identity, str) or not identity:
                metadata = item.get('metadata')
                identity = metadata.get('id') if isinstance(metadata, dict) else None
            if not isinstance(identity, str) or not identity or identity in seen:
                raise MigrationImportError('conversation.list catalog is invalid')
            seen.add(identity)
            items.append(item)
        if not page['has_more']:
            counted = _conversation_owner_count(session, owner_user_id)
            if counted != len(items):
                raise MigrationImportError('conversation count and catalog disagree')
            if type(page.get('total_count')) is int and page['total_count'] != counted:
                raise MigrationImportError('conversation count and catalog disagree')
            return items
        last = page['items'][-1]
        metadata = last.get('metadata')
        if not isinstance(metadata, dict):
            raise MigrationImportError('conversation.list catalog is invalid')
        next_updated = metadata.get('updated_at')
        next_id = last.get('id') or metadata.get('id')
        if (type(next_updated) is not int or not isinstance(next_id, str)
                or not next_id or (before_updated_at == next_updated
                                   and before_id == next_id)):
            raise MigrationImportError('conversation catalog page did not advance')
        before_updated_at, before_id = next_updated, next_id


def _verify_conversation(
    document: dict[str, Any], session: OfflineMigrationSession,
) -> None:
    observed = _query(session, 'conversation.get', {
        'user_id': document['user_id'],
        'conv_id': document['id'],
        'derive_messages': False,
    })
    if not isinstance(observed, dict):
        raise MigrationImportError('conversation source and target catalogs differ')
    if _conversation_from_target(observed) != document:
        raise MigrationImportError('conversation source and target catalogs differ')


def _verify_conversation_catalog(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
) -> int:
    table = _plan_table('conversations')
    owner_user_id = _session_owner(session)
    rows = 0
    for item in _conversation_catalog(session, owner_user_id):
        metadata = item.get('metadata')
        if not isinstance(metadata, dict):
            raise MigrationImportError('invalid conversation catalog identity')
        listed = _conversation_from_target(item)
        row = (
            source.point(table['name'], table['columns'], table['keys'],
                         (listed['id'], owner_user_id))
            if table['name'] in source.tables else None
        )
        if row is None:
            raise MigrationImportError('conversation target contains an extra conversation')
        document = _conversation_document(row)
        if listed != document:
            raise MigrationImportError('conversation source and target catalogs differ')
        _verify_conversation(document, session)
        rows += 1
    return rows


def _verify_conversations(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, *,
    until: Sequence[Any] | None, complete: bool,
) -> tuple[int, str]:
    pairs = _source_conversation_pairs(source, until=until, complete=complete)
    owner_user_id = _session_owner(session)
    for _key, document in pairs:
        if document['user_id'] != owner_user_id:
            raise MigrationImportError(
                'conversation owner does not match the operator session')
        _verify_conversation(document, session)
    if complete and _verify_conversation_catalog(source, session) != len(pairs):
        raise MigrationImportError('conversation source and target catalogs differ')
    return len(pairs), _digest_pairs(pairs)


def _replay_conversation(session: OfflineMigrationSession, row: dict[str, Any]) -> None:
    document = _conversation_document(row)
    if document['user_id'] != _session_owner(session):
        raise MigrationImportError(
            'conversation owner does not match the operator session')
    _command(session, 'migration.conversation.restore', document)
    _verify_conversation(document, session)


def _import_conversations(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    table = _plan_table('conversations')
    progress = _resume_or_empty(checkpoint, 'conversations')
    if progress is not None:
        until = None if progress['complete'] else progress['cursor']
        rows, digest = _verify_conversations(
            source, session, until=until, complete=progress['complete'])
        if rows != progress['rows'] or digest != progress['sha256']:
            raise MigrationImportError(
                'resumed conversation target no longer matches progress')
        if progress['complete']:
            return
        after: Sequence[Any] | None = progress['cursor']
    else:
        after = None
    if table['name'] not in source.tables:
        rows, digest = _verify_conversations(
            source, session, until=None, complete=True)
        _publish(
            checkpoint, 'conversations', cursor=[], rows=rows, digest=digest,
            complete=True, after_verified_prefix=after_verified_prefix,
        )
        return
    _verify_conversation_catalog(source, session)
    last: list[Any] | None = None if after is None else list(after)
    pending = 0
    for row in source.rows(
        table['name'], table['columns'], table['keys'], after=after,
        batch_rows=batch_rows,
    ):
        _replay_conversation(session, row)
        last = list(_pk(table, row))
        pending += 1
        if pending < batch_rows:
            continue
        rows, digest = _verify_conversations(
            source, session, until=last, complete=False)
        _publish(
            checkpoint, 'conversations', cursor=last, rows=rows, digest=digest,
            complete=False, after_verified_prefix=after_verified_prefix,
        )
        pending = 0
        after = last
    rows, digest = _verify_conversations(
        source, session, until=None, complete=True)
    _publish(
        checkpoint, 'conversations', cursor=last or [], rows=rows, digest=digest,
        complete=True, after_verified_prefix=after_verified_prefix,
    )


def _turn_document(row: dict[str, Any]) -> dict[str, Any]:
    turn_id = _text(row['turn_id'])
    conversation_id = _text(row['conversation_id'])
    presentation_id = _text(row['presentation_id'] or '')
    lane_id = _text(row['lane_id'])
    parent = row['parent_turn_id']
    if parent is not None:
        parent = _text(parent)
        if not parent:
            raise MigrationImportError('turn parent identity is invalid')
    current_attempt = row['current_attempt_id']
    if current_attempt is not None:
        current_attempt = _text(current_attempt)
        if (not current_attempt or len(current_attempt) > 128
                or row['status'] not in {'completed', 'failed', 'interrupted',
                                         'truncated', 'aborted', 'superseded'}):
            raise MigrationImportError('turn still has a live attempt')
    actor = _text(row['actor'])
    kind = _text(row['kind'])
    run_id = _text(row['run_id'] or '')
    status = _text(row['status'])
    from lib.storage_authority_sql.projection_codec import (
        ProjectionCodecError, decode_projection_from_storage,
    )

    try:
        projection = decode_projection_from_storage(_json_load(row['projection_json']))
    except ProjectionCodecError as exc:
        raise MigrationImportError('turn projection codec is invalid') from exc
    settlement = _json_load(row['settlement_json'])
    if projection is None:
        projection = {}
    if settlement is None:
        settlement = {}
    if not isinstance(projection, dict) or not isinstance(settlement, dict):
        raise MigrationImportError('turn projection or settlement is invalid')
    ordinal = _nonneg_int(row['ordinal'])
    revision = _nonneg_int(row['projection_revision'])

    display_offset = _nonneg_int(row.get('model_context_display_offset', 0))
    if display_offset > revision:
        raise MigrationImportError('turn context display offset exceeds revision')
    created_at = _nonneg_int(row['created_at'])
    updated_at = _nonneg_int(row['updated_at'])
    user_id = _int(row['user_id'])
    if (not turn_id or len(turn_id) > 128
            or not conversation_id or len(conversation_id) > 256
            or len(presentation_id) > 128
            or not lane_id or len(lane_id) > 64
            or (parent is not None and len(parent) > 128)
            or not actor or len(actor) > 64
            or not kind or len(kind) > 64
            or len(run_id) > 256
            or not status or len(status) > 64
            or user_id <= 0
            or max(ordinal, revision, created_at, updated_at) >= 2**64):
        raise MigrationImportError('turn snapshot is invalid')
    return {
        'turn_id': turn_id,
        'conversation_id': conversation_id,
        'presentation_id': presentation_id,
        'lane_id': lane_id,
        'parent_turn_id': parent,
        'ordinal': ordinal,
        'actor': actor,
        'kind': kind,
        'run_id': run_id,
        'status': status,
        'current_attempt_id': current_attempt,
        'projection': projection,
        'projection_revision': revision,
        **({'model_context_display_offset': display_offset} if display_offset else {}),
        'settlement': settlement,
        'created_at': created_at,
        'updated_at': updated_at,
        'user_id': user_id,
    }


def _turn_restore_payload(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != 'user_id'}


def _verify_turn(session: OfflineMigrationSession, document: dict[str, Any]) -> None:
    identity = {'id': document['turn_id'], 'conversation_id': document['conversation_id']}
    raw = turn_payload.encoded_snapshot(_turn_restore_payload(document))
    large = turn_payload.is_large(raw)
    del raw
    observed = (turn_payload.read(session, identity) if large else
                _query(session, 'migration.turn.get', identity))
    if observed != _turn_restore_payload(document):
        raise MigrationImportError('turn source and target snapshots differ')


def _turn_catalog(session: OfflineMigrationSession) -> Iterator[str]:
    cursor = None
    previous = b''
    seen: set[str] = set()
    while True:
        page = _query(session, 'migration.turn.scan', {'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']
                or any(not isinstance(row, dict) or set(row) != {'id'}
                       or not isinstance(row['id'], str) for row in page['rows'])):
            raise MigrationImportError('invalid turn catalog page')
        for row in page['rows']:
            if row['id'] in seen:
                raise MigrationImportError('turn catalog repeated an identity')
            seen.add(row['id'])
            yield row['id']
        following = page.get('cursor')
        if following is None:
            return
        if (not page['rows'] or not isinstance(following, str)
                or not following.startswith('turns:')):
            raise MigrationImportError('invalid turn catalog cursor')
        try:
            raw = bytes.fromhex(following[len('turns:'):])
        except ValueError:
            raise MigrationImportError('invalid turn catalog cursor') from None
        if not raw or len(raw) > MAX_ENTITY_KEY_BYTES or raw <= previous:
            raise MigrationImportError('turn catalog did not advance')
        previous = raw
        cursor = following


def _verify_turn_catalog(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, *,
    complete: bool, expected_rows: int,
) -> None:
    table = _plan_table('turns')
    owner_user_id = _session_owner(session)
    rows = 0
    for turn_id in _turn_catalog(session):
        row = (
            turn_payload.point(source, table, (turn_id,))
            if table['name'] in source.tables else None
        )
        if row is None:
            raise MigrationImportError('turn target contains an extra turn')
        document = _turn_document(row)
        if document['user_id'] != owner_user_id:
            raise MigrationImportError('turn catalog owner differs')
        _verify_turn(session, document)
        rows += 1
    if complete and rows != expected_rows:
        raise MigrationImportError('turn source and target catalogs differ')


def _verify_turns(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, *,
    until: Sequence[Any] | None, complete: bool,
) -> tuple[int, str]:
    rows = 0
    digest = hashlib.sha256(b'tofu-db.turns.snapshot-chain.v1').hexdigest()
    table = _plan_table('turns')
    for row in _scan(source, table, until=until, complete=complete):
        document = _turn_document(row)
        if document['user_id'] != _session_owner(session):
            raise MigrationImportError(
                'turn snapshots belong to owners outside the operator scope')
        _verify_turn(session, document)
        digest = _record_chain(digest, _turn_restore_payload(document))
        rows += 1
    if complete:
        _verify_turn_catalog(
            source, session, complete=True, expected_rows=rows)
    return rows, digest


def _replay_turn(session: OfflineMigrationSession, document: dict[str, Any]) -> None:
    if document['user_id'] != _session_owner(session):
        raise MigrationImportError(
            'turn snapshots belong to owners outside the operator scope')
    raw = turn_payload.encoded_snapshot(_turn_restore_payload(document))
    if turn_payload.is_large(raw):
        turn_payload.upload(session, raw)
    else:
        _command(session, 'migration.turn.restore', _turn_restore_payload(document))
    del raw
    _verify_turn(session, document)


def _import_turns(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    table = _plan_table('turns')
    progress = _resume_or_empty(checkpoint, 'turns')
    if progress is None:
        # Fail a foreign-owner snapshot before this domain writes anything.
        for row in _scan(source, table):
            if _turn_document(row)['user_id'] != _session_owner(session):
                raise MigrationImportError(
                    'turn snapshots belong to owners outside the operator scope')
    after = None
    if progress is not None:
        rows, digest = _verify_turns(
            source, session, until=None if progress['complete'] else progress['cursor'],
            complete=progress['complete'])
        if rows != progress['rows'] or digest != progress['sha256']:
            raise MigrationImportError(
                'resumed turn target no longer matches progress')
        if progress['complete']:
            return
        after = progress['cursor']
    _verify_turn_catalog(source, session, complete=False, expected_rows=0)
    last: list[Any] | None = None if after is None else list(after)
    pending = 0
    iterator = (turn_payload.rows(source, table, after=after, batch_rows=batch_rows)
                if table['name'] in source.tables else ())
    for row in iterator:
        document = _turn_document(row)
        _replay_turn(session, document)
        last = list(_pk(table, row))
        pending += 1
        if pending < batch_rows:
            continue
        rows, digest = _verify_turns(source, session, until=last, complete=False)
        _publish(
            checkpoint, 'turns', cursor=last, rows=rows, digest=digest,
            complete=False, after_verified_prefix=after_verified_prefix,
        )
        pending = 0
        after = last
    rows, digest = _verify_turns(source, session, until=None, complete=True)
    _publish(
        checkpoint, 'turns', cursor=last or [], rows=rows, digest=digest,
        complete=True, after_verified_prefix=after_verified_prefix,
    )

def _import_credentials(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    table = _plan_table('credentials')
    progress = _resume_or_empty(checkpoint, 'credentials')
    if progress is not None:
        until = None if progress['complete'] else progress['cursor']
        rows, digest = _verify_credentials(
            source, session, until=until, complete=progress['complete'])
        if rows != progress['rows'] or digest != progress['sha256']:
            raise MigrationImportError('resumed credential target no longer matches progress')
        if progress['complete']:
            return
        after: Sequence[Any] | None = progress['cursor']
    else:
        after = None
    if table['name'] not in source.tables:
        rows, digest = _verify_credentials(source, session, until=None, complete=True)
        _publish(
            checkpoint, 'credentials', cursor=[], rows=rows, digest=digest,
            complete=True, after_verified_prefix=after_verified_prefix,
        )
        return
    batch: list[dict[str, Any]] = []
    last: list[Any] | None = None if after is None else list(after)
    for row in source.rows(
        table['name'], table['columns'], table['keys'], after=after,
        batch_rows=batch_rows,
    ):
        _replay_credential(session, row)
        last = list(_pk(table, row))
        batch.append(row)
        if len(batch) < batch_rows:
            continue
        rows, digest = _verify_credentials(
            source, session, until=last, complete=False)
        _publish(
            checkpoint, 'credentials', cursor=last, rows=rows, digest=digest,
            complete=False, after_verified_prefix=after_verified_prefix,
        )
        batch.clear()
        after = last
    rows, digest = _verify_credentials(source, session, until=None, complete=True)
    _publish(
        checkpoint, 'credentials', cursor=last or [], rows=rows, digest=digest,
        complete=True, after_verified_prefix=after_verified_prefix,
    )


def _import_providers(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    table = _plan_table('providers')
    progress = _resume_or_empty(checkpoint, 'providers')
    if progress is not None:
        until = None if progress['complete'] else progress['cursor']
        rows, digest = _verify_providers(
            source, session, until=until, complete=progress['complete'])
        if rows != progress['rows'] or digest != progress['sha256']:
            raise MigrationImportError('resumed provider target no longer matches progress')
        if progress['complete']:
            return
        after: Sequence[Any] | None = progress['cursor']
    else:
        after = None
    if table['name'] not in source.tables:
        rows, digest = _verify_providers(source, session, until=None, complete=True)
        _publish(
            checkpoint, 'providers', cursor=[], rows=rows, digest=digest,
            complete=True, after_verified_prefix=after_verified_prefix,
        )
        return
    last: list[Any] | None = None if after is None else list(after)
    pending = 0
    for row in source.rows(
        table['name'], table['columns'], table['keys'], after=after,
        batch_rows=batch_rows,
    ):
        _replay_provider(session, row)
        last = list(_pk(table, row))
        pending += 1
        if pending < batch_rows:
            continue
        rows, digest = _verify_providers(
            source, session, until=last, complete=False)
        _publish(
            checkpoint, 'providers', cursor=last, rows=rows, digest=digest,
            complete=False, after_verified_prefix=after_verified_prefix,
        )
        pending = 0
        after = last
    rows, digest = _verify_providers(source, session, until=None, complete=True)
    _publish(
        checkpoint, 'providers', cursor=last or [], rows=rows, digest=digest,
        complete=True, after_verified_prefix=after_verified_prefix,
    )


def _task_result_catalog(session: OfflineMigrationSession, name: str) -> Iterator[str]:
    cursor = None
    previous = b''
    while True:
        page = _query(session, 'migration.task.result.scan', {'table': name, 'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']
                or any(not isinstance(row, dict) or set(row) != {'task_id'}
                       or not isinstance(row['task_id'], str) for row in page['rows'])):
            raise MigrationImportError('invalid task result catalog page')
        for row in page['rows']:
            encoded = row['task_id'].encode('utf-8')
            if encoded <= previous:
                raise MigrationImportError('task result catalog order did not advance')
            previous = encoded
            yield row['task_id']
        following = page.get('cursor')
        if following is None:
            return
        if (not page['rows'] or not isinstance(following, str)
                or not following.startswith(name + ':')):
            raise MigrationImportError('invalid task result catalog cursor')
        try:
            raw = bytes.fromhex(following[len(name) + 1:])
        except ValueError:
            raise MigrationImportError('invalid task result catalog cursor') from None
        if not raw or len(raw) > MAX_ENTITY_KEY_BYTES or raw <= previous:
            raise MigrationImportError('task result catalog did not advance')
        cursor = following


def _task_result_row_skip(row: dict[str, Any]) -> bool:
    # Other namespaces are not hashed into this domain's binding; rows still
    # owned only by the generic records domain preflight fail there instead.
    return row['namespace'] != 'task_results'


def _task_result_document(row: dict[str, Any], *, default_owner: int | None = None) -> dict[str, Any]:
    if _text(row['namespace']) != 'task_results':
        raise MigrationImportError('task result domain source row escaped its namespace')
    task_id = _text(row['record_key'])
    if (not task_id or len(task_id) > 512
            or 2 + len('task_results') + len(task_id.encode('utf-8')) > MAX_ENTITY_KEY_BYTES):
        raise MigrationImportError('task result identity exceeds its bound')
    raw = _text(row['value_json'])
    if not raw.strip():
        raise MigrationImportError('stored JSON is invalid')
    if len(raw.encode('utf-8')) > MAX_MIGRATION_TASK_RESULT_DOCUMENT_BYTES:
        raise MigrationImportError('task result document exceeds the migration byte budget')
    body = _json_load(raw)
    if not isinstance(body, dict):
        raise MigrationImportError('task result snapshot is not an object')
    owner_user_id = body.get('user_id')
    if owner_user_id is None and type(default_owner) is int and default_owner > 0:
        # Legacy rows predate owner stamping. The single-operator migration
        # scope adopts them explicitly; drivers pass their session owner, so
        # the restored document's owner always matches the operator scope.
        owner_user_id = default_owner
    owner_user_id = _nonneg_int(owner_user_id)
    if owner_user_id == 0:
        raise MigrationImportError('task result snapshot owner is invalid')
    task_field = body.get('task_id')
    if task_field is not None and task_field != task_id:
        raise MigrationImportError('task result snapshot identity differs')
    version, updated = _int(row['version']), _int(row['updated_at_ms'])
    if not 0 < version < 2**64 or not 0 < updated < 2**64:
        raise MigrationImportError('task result snapshot metadata must be positive u64')
    return {
        'task_id': task_id,
        'owner_user_id': owner_user_id,
        'version': version,
        'updated_at_ms': updated,
        'value_json': raw,
    }


def _task_result_canonical(document: dict[str, Any]) -> dict[str, Any]:
    body = _json_load(document['value_json'])
    _canonical(body)  # Validate finite JSON before comparison.
    return {
        'task_id': document['task_id'],
        'owner_user_id': document['owner_user_id'],
        'version': document['version'],
        'updated_at_ms': document['updated_at_ms'],
        'value_json': body,
    }


def _task_result_status(document: dict[str, Any]) -> str:
    status = _json_load(document['value_json']).get('status')
    return status if isinstance(status, str) else ''


def _task_result_experiment(document: dict[str, Any]) -> str:
    metadata = _json_load(document['value_json']).get('metadata')
    if not isinstance(metadata, dict):
        return ''
    outcome = metadata.get('costExperiment')
    if not isinstance(outcome, dict) or not outcome:
        return ''
    experiment_id = outcome.get('experimentId') or outcome.get('experiment_id')
    return experiment_id if isinstance(experiment_id, str) and experiment_id else ''


def _verify_task_result(session: OfflineMigrationSession, document: dict[str, Any]) -> None:
    observed = _query(session, 'migration.task.result.get', {'task_id': document['task_id']})
    if observed != _task_result_canonical(document):
        raise MigrationImportError('task result source and target snapshots differ')


def _task_result_index_count(documents: list[dict[str, Any]], name: str) -> int:
    if name in ('headers', 'summaries'):
        return len(documents)
    if name == 'live':
        return sum(_task_result_status(document) in ('pending', 'running')
                   for document in documents)
    if name == 'cost_index':
        return sum(bool(_task_result_experiment(document)) for document in documents)
    raise MigrationImportError('unknown task result migration table')


def _verify_task_result_catalogs(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, *,
    owner_user_id: int, complete: bool,
) -> None:
    table = _plan_table('task_results')
    documents: dict[str, dict[str, Any]] = {}
    if complete:
        for row in _scan(source, table):
            if _task_result_row_skip(row):
                continue
            document = _task_result_document(row, default_owner=owner_user_id)
            documents[document['task_id']] = document
    owned = [document for document in documents.values()
             if document['owner_user_id'] == owner_user_id]
    foreign = [document for document in documents.values()
               if document['owner_user_id'] != owner_user_id]
    if complete and foreign:
        raise MigrationImportError(
            'task result snapshots belong to owners outside the operator scope')
    for name in ('headers', 'summaries', 'live', 'cost_index'):
        identities = list(_task_result_catalog(session, name))
        if len(identities) != len(set(identities)):
            raise MigrationImportError('task result catalog repeated an identity')
        if complete:
            expected = _task_result_index_count(owned, name)
            if len(identities) != expected:
                raise MigrationImportError('task result catalog count differs')
            continue
        # Incremental passes only prove each visible index row resolves to a
        # verified imported snapshot; missing rows surface through point reads.
        for task_id in identities:
            row = (source.point(table['name'], table['columns'], table['keys'],
                                ('task_results', task_id))
                   if table['name'] in source.tables else None)

            if row is None:
                raise MigrationImportError('task result catalog contains an extra task')
            document = _task_result_document(row, default_owner=owner_user_id)
            if document['owner_user_id'] != owner_user_id:
                raise MigrationImportError('task result catalog owner differs')
            if name == 'live' and _task_result_status(document) not in ('pending', 'running'):
                raise MigrationImportError('task result live catalog is stale')
            if name == 'cost_index' and not _task_result_experiment(document):
                raise MigrationImportError('task result cost catalog is stale')
            _verify_task_result(session, document)


def _verify_task_results(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, *,
    until: Sequence[Any] | None, complete: bool,
) -> tuple[int, str]:
    rows = 0
    digest = hashlib.sha256(b'tofu-db.task-results.snapshot-chain.v1').hexdigest()
    table = _plan_table('task_results')
    for row in _scan(source, table, until=until, complete=complete):
        if _task_result_row_skip(row):
            continue
        document = _task_result_document(row, default_owner=_session_owner(session))
        if document['owner_user_id'] != _session_owner(session):
            raise MigrationImportError(
                'task result snapshots belong to owners outside the operator scope')
        _verify_task_result(session, document)
        digest = _record_chain(digest, document)
        rows += 1
    if complete:
        _verify_task_result_catalogs(
            source, session, owner_user_id=_session_owner(session), complete=True)
    return rows, digest


def _import_task_results(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    table = _plan_table('task_results')
    progress = _resume_or_empty(checkpoint, 'task_results')
    if progress is None:
        # Fail a foreign-owner snapshot before this domain writes anything.
        for row in _scan(source, table):
            if _task_result_row_skip(row):
                continue
            if _task_result_document(row, default_owner=_session_owner(session))['owner_user_id'] != _session_owner(session):
                raise MigrationImportError(
                    'task result snapshots belong to owners outside the operator scope')
    after = None
    if progress is not None:
        rows, digest = _verify_task_results(
            source, session, until=None if progress['complete'] else progress['cursor'],
            complete=progress['complete'])
        if rows != progress['rows'] or digest != progress['sha256']:
            raise MigrationImportError(
                'resumed task result target no longer matches progress')
        if progress['complete']:
            return
        after = progress['cursor']
    _verify_task_result_catalogs(
        source, session, owner_user_id=_session_owner(session), complete=False)
    last: list[Any] | None = None if after is None else list(after)
    pending = 0
    iterator = (source.rows(table['name'], table['columns'], table['keys'],
                            after=after, batch_rows=batch_rows)
                if table['name'] in source.tables else ())
    for row in iterator:
        if _task_result_row_skip(row):
            continue
        document = _task_result_document(row, default_owner=_session_owner(session))
        if document['owner_user_id'] != _session_owner(session):
            raise MigrationImportError(
                'task result snapshots belong to owners outside the operator scope')
        _command(session, 'migration.task.result.restore', document)
        _verify_task_result(session, document)
        last = list(_pk(table, row))
        pending += 1
        if pending < batch_rows:
            continue
        rows, digest = _verify_task_results(
            source, session, until=last, complete=False)
        _publish(
            checkpoint, 'task_results', cursor=last, rows=rows, digest=digest,
            complete=False, after_verified_prefix=after_verified_prefix,
        )
        pending = 0
        after = last
    rows, digest = _verify_task_results(
        source, session, until=None, complete=True)
    _publish(
        checkpoint, 'task_results', cursor=last or [], rows=rows, digest=digest,
        complete=True, after_verified_prefix=after_verified_prefix,
    )


def _import_records(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    table = _plan_table('records')
    progress = _resume_or_empty(checkpoint, 'records')
    rows = 0
    digest = hashlib.sha256(b'tofu-db.record-snapshot-chain.v1').hexdigest()
    after = None
    if progress is not None:
        rows, digest = _verify_records(
            source, session, until=None if progress['complete'] else progress['cursor'],
            complete=progress['complete'])
        if rows != progress['rows'] or digest != progress['sha256']:
            raise MigrationImportError('resumed record target no longer matches progress')
        if progress['complete']:
            return
        after = progress['cursor']
    # Committed rows beyond the durable checkpoint are valid only when every
    # target identity and exact snapshot independently matches this source.
    _verify_record_catalog(source, session)
    last = [] if after is None else list(after)
    pending = 0
    iterator = (source.rows(table['name'], table['columns'], table['keys'],
                            after=after, batch_rows=batch_rows)
                if table['name'] in source.tables else ())
    for row in iterator:
        if row['namespace'] in ('task_results', 'model.continuation.v1') + _PAPER_BUDGET_NAMESPACES:
            continue
        document = _replay_record(session, row)
        rows += 1
        digest = _record_chain(digest, document)
        last = list(_pk(table, row))
        pending += 1
        if pending == batch_rows:
            _publish(checkpoint, 'records', cursor=last, rows=rows, digest=digest,
                     complete=False, after_verified_prefix=after_verified_prefix)
            pending = 0
    if _verify_record_catalog(source, session) != rows:
        raise MigrationImportError('record source and target catalogs differ')
    _publish(checkpoint, 'records', cursor=last, rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


def _continuation_row_skip(row: dict[str, Any]) -> bool:
    return row['namespace'] != 'model.continuation.v1'


def _continuation_restore_payload(document: dict[str, Any]) -> dict[str, Any]:
    return {
        'record_key': document['key'],
        'value_json': document['value_json'],
        'version': document['version'],
        'updated_at_ms': document['updated_at_ms'],
    }


def _verify_continuation(source_document: dict[str, Any], session: OfflineMigrationSession) -> None:
    observed = _query(session, 'migration.continuation.get', {
        'record_key': source_document['key']})
    expected = {
        'record_key': source_document['key'],
        'value_json': _json_load(source_document['value_json']),
        'version': source_document['version'],
        'updated_at_ms': source_document['updated_at_ms'],
    }
    if observed != expected:
        raise MigrationImportError('continuation source and target snapshots differ')


def _verify_continuation_catalog(source: SQLiteMigrationSource, session: OfflineMigrationSession) -> int:
    # Independently enumerate the target continuation catalog and confirm each
    # identity maps back to an exact source snapshot. No derived indexes exist.
    table = _plan_table('continuation')
    cursor = None
    count = 0
    while True:
        payload: dict[str, Any] = {} if cursor is None else {'cursor': cursor}
        page = _query(session, 'migration.continuation.scan', payload)
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']):
            raise MigrationImportError('invalid migration continuation catalog page')
        for identity in page['rows']:
            if not isinstance(identity, dict) or set(identity) != {'record_key'}:
                raise MigrationImportError('invalid migration continuation catalog identity')
            record_key = identity['record_key']
            if not isinstance(record_key, str):
                raise MigrationImportError('invalid migration continuation catalog identity')
            row = (source.point(table['name'], table['columns'], table['keys'],
                                ('model.continuation.v1', record_key))
                   if table['name'] in source.tables else None)
            if row is None:
                raise MigrationImportError('continuation source and target catalogs differ: extra target key')
            document = _record_document(row)
            _verify_continuation(document, session)
            count += 1
        following = page.get('cursor')
        if following is None:
            return count
        if not page['rows'] or not isinstance(following, str) or following == cursor:
            raise MigrationImportError('migration continuation cursor did not advance')
        cursor = following


def _verify_continuations(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, *,
    until: Sequence[Any] | None, complete: bool,
) -> tuple[int, str]:
    rows = 0
    digest = hashlib.sha256(b'tofu-db.continuation-snapshot-chain.v1').hexdigest()
    table = _plan_table('continuation')
    for row in _scan(source, table, until=until, complete=complete):
        if _continuation_row_skip(row):
            continue
        document = _record_document(row)
        _verify_continuation(document, session)
        digest = _record_chain(digest, document)
        rows += 1
    if complete and _verify_continuation_catalog(source, session) != rows:
        raise MigrationImportError('continuation source and target catalogs differ')
    return rows, digest


def _import_continuations(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    table = _plan_table('continuation')
    progress = _resume_or_empty(checkpoint, 'continuation')
    rows = 0
    digest = hashlib.sha256(b'tofu-db.continuation-snapshot-chain.v1').hexdigest()
    after = None
    if progress is not None:
        rows, digest = _verify_continuations(
            source, session, until=None if progress['complete'] else progress['cursor'],
            complete=progress['complete'])
        if rows != progress['rows'] or digest != progress['sha256']:
            raise MigrationImportError('resumed continuation target no longer matches progress')
        if progress['complete']:
            return
        after = progress['cursor']
    _verify_continuation_catalog(source, session)
    last = [] if after is None else list(after)
    pending = 0
    iterator = (source.rows(table['name'], table['columns'], table['keys'],
                            after=after, batch_rows=batch_rows)
                if table['name'] in source.tables else ())
    for row in iterator:
        if _continuation_row_skip(row):
            continue
        document = _record_document(row)
        _command(session, 'migration.continuation.restore',
                 _continuation_restore_payload(document))
        _verify_continuation(document, session)
        rows += 1
        digest = _record_chain(digest, document)
        last = list(_pk(table, row))
        pending += 1
        if pending == batch_rows:
            _publish(checkpoint, 'continuation', cursor=last, rows=rows, digest=digest,
                     complete=False, after_verified_prefix=after_verified_prefix)
            pending = 0
    if _verify_continuation_catalog(source, session) != rows:
        raise MigrationImportError('continuation source and target catalogs differ')
    _publish(checkpoint, 'continuation', cursor=last, rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


def _verify_paper_budget(
    session: OfflineMigrationSession, domain_name: str,
    document: dict[str, Any],
) -> None:
    _, _, get_operation = _PAPER_BUDGET_DOMAINS[domain_name]
    observed = _query(session, get_operation, {'record_key': document['key']})
    if not isinstance(observed, dict):
        raise MigrationImportError('paper budget source and target snapshots differ')
    expected_value = _json_load(document['value_json'])
    observed_value = _json_load(observed.get('value_json'))
    if domain_name == 'paper_document_budget' and isinstance(expected_value, dict):
        # The native ledger always reports both counters; the source row may
        # omit an associations count of zero.
        expected_value = {
            'count': expected_value.get('count', 0),
            'associations': expected_value.get('associations', 0),
        }
    if (observed.get('record_key') != document['key']
            or observed_value != expected_value
            or observed.get('version') != document['version']
            or observed.get('updated_at_ms') != document['updated_at_ms']):
        raise MigrationImportError('paper budget source and target snapshots differ')


def _verify_paper_budgets(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    domain_name: str, *, until: Sequence[Any] | None, complete: bool,
) -> tuple[int, str]:
    namespace, _, get_operation = _PAPER_BUDGET_DOMAINS[domain_name]
    rows = 0
    digest = hashlib.sha256(b'tofu-db.paper-budget-snapshot-chain.v1').hexdigest()
    table = _plan_table(domain_name)
    for row in _scan(source, table, until=until, complete=complete):
        if row['namespace'] != namespace:
            continue
        document = _record_document(row)
        _verify_paper_budget(session, domain_name, document)
        digest = _record_chain(digest, document)
        rows += 1
    if complete:
        if rows > 1:
            raise MigrationImportError('paper budget source holds divergent ledger rows')
        if rows == 0 and _query(session, get_operation, {'record_key': 'capacity'}) is not None:
            raise MigrationImportError('paper budget target holds a ledger the source does not')
    return rows, digest


def _import_paper_budget(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, domain_name: str, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    namespace, restore_operation, _ = _PAPER_BUDGET_DOMAINS[domain_name]
    table = _plan_table(domain_name)
    progress = _resume_or_empty(checkpoint, domain_name)
    rows = 0
    digest = hashlib.sha256(b'tofu-db.paper-budget-snapshot-chain.v1').hexdigest()
    after = None
    if progress is not None:
        rows, digest = _verify_paper_budgets(
            source, session, domain_name,
            until=None if progress['complete'] else progress['cursor'],
            complete=progress['complete'])
        if rows != progress['rows'] or digest != progress['sha256']:
            raise MigrationImportError(
                'resumed paper budget target no longer matches progress')
        if progress['complete']:
            return
        after = progress['cursor']
    last = [] if after is None else list(after)
    pending = 0
    iterator = (source.rows(table['name'], table['columns'], table['keys'],
                            after=after, batch_rows=batch_rows)
                if table['name'] in source.tables else ())
    for row in iterator:
        if row['namespace'] != namespace:
            continue
        document = _record_document(row)
        _command(session, restore_operation, {
            'record_key': document['key'],
            'value_json': document['value_json'],
            'version': document['version'],
            'updated_at_ms': document['updated_at_ms'],
        })
        _verify_paper_budget(session, domain_name, document)
        rows += 1
        digest = _record_chain(digest, document)
        last = list(_pk(table, row))
        pending += 1
        if pending == batch_rows:
            _publish(checkpoint, domain_name, cursor=last, rows=rows, digest=digest,
                     complete=False, after_verified_prefix=after_verified_prefix)
            pending = 0
    final_rows, final_digest = _verify_paper_budgets(
        source, session, domain_name, until=None, complete=True)
    if final_rows != rows or final_digest != digest:
        raise MigrationImportError('paper budget source and target snapshots differ')
    _publish(checkpoint, domain_name, cursor=last, rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


def _desktop_egress_document(row: dict[str, Any]) -> dict[str, Any]:
    agent_id = _text(row['agent_id'])
    updated_at_ms = _int(row['updated_at_ms'])
    if not 0 < updated_at_ms < 2**64:
        raise MigrationImportError('desktop egress timestamp must be a positive u64')
    return {'agent_id': agent_id, 'updated_at_ms': updated_at_ms}


def _verify_desktop_egress(
    session: OfflineMigrationSession, document: dict[str, Any],
) -> None:
    observed = _query(session, 'migration.desktop.egress.get', {})
    if observed != document:
        raise MigrationImportError('desktop egress source and target snapshots differ')


def _verify_desktop_egress_catalog(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, *,
    expected_rows: int,
) -> None:
    table = _plan_table('desktop_egress')
    owner_user_id = _session_owner(session)
    page = _query(session, 'migration.desktop.egress.scan', {})
    if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
            or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']
            or page.get('cursor') is not None):
        raise MigrationImportError('invalid desktop egress catalog page')
    if len(page['rows']) != expected_rows:
        raise MigrationImportError('desktop egress source and target catalogs differ')
    for identity in page['rows']:
        if set(identity) != {'owner_user_id'}:
            raise MigrationImportError('invalid desktop egress catalog identity')
        if identity['owner_user_id'] != owner_user_id:
            raise MigrationImportError('desktop egress catalog owner differs')
        row = (source.point(table['name'], table['columns'], table['keys'], (owner_user_id,))
               if table['name'] in source.tables else None)
        if row is None:
            raise MigrationImportError('desktop egress target holds a row the source does not')
        _verify_desktop_egress(session, _desktop_egress_document(row))


def _verify_desktop_egresses(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, *,
    until: Sequence[Any] | None, complete: bool,
) -> tuple[int, str]:
    rows = 0
    digest = hashlib.sha256(b'tofu-db.desktop-egress-snapshot-chain.v1').hexdigest()
    table = _plan_table('desktop_egress')
    owner_user_id = _session_owner(session)
    for row in _scan(source, table, until=until, complete=complete):
        if _int(row['owner_user_id']) != owner_user_id:
            raise MigrationImportError(
                'desktop egress snapshots belong to owners outside the operator scope')
        document = _desktop_egress_document(row)
        _verify_desktop_egress(session, document)
        digest = _record_chain(digest, document)
        rows += 1
    if complete:
        _verify_desktop_egress_catalog(source, session, expected_rows=rows)
    return rows, digest


def _import_desktop_egress(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    table = _plan_table('desktop_egress')
    owner_user_id = _session_owner(session)
    progress = _resume_or_empty(checkpoint, 'desktop_egress')
    after = None
    if progress is not None:
        rows, digest = _verify_desktop_egresses(
            source, session,
            until=None if progress['complete'] else progress['cursor'],
            complete=progress['complete'])
        if rows != progress['rows'] or digest != progress['sha256']:
            raise MigrationImportError(
                'resumed desktop egress target no longer matches progress')
        if progress['complete']:
            return
        after = progress['cursor']
    last: list[Any] | None = None if after is None else list(after)
    pending = 0
    rows = 0 if progress is None else progress['rows']
    digest = (hashlib.sha256(b'tofu-db.desktop-egress-snapshot-chain.v1').hexdigest()
              if progress is None else progress['sha256'])
    iterator = (source.rows(table['name'], table['columns'], table['keys'],
                            after=after, batch_rows=batch_rows)
                if table['name'] in source.tables else ())
    for row in iterator:
        if _int(row['owner_user_id']) != owner_user_id:
            raise MigrationImportError(
                'desktop egress snapshots belong to owners outside the operator scope')
        document = _desktop_egress_document(row)
        _command(session, 'migration.desktop.egress.restore', document)
        _verify_desktop_egress(session, document)
        digest = _record_chain(digest, document)
        rows += 1
        last = list(_pk(table, row))
        pending += 1
        if pending == batch_rows:
            _publish(checkpoint, 'desktop_egress', cursor=last, rows=rows, digest=digest,
                     complete=False, after_verified_prefix=after_verified_prefix)
            pending = 0
    final_rows, final_digest = _verify_desktop_egresses(
        source, session, until=None, complete=True)
    if final_rows != rows or final_digest != digest:
        raise MigrationImportError('desktop egress source and target snapshots differ')
    _publish(checkpoint, 'desktop_egress', cursor=last or [], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


def _daily_cost_document(row: dict[str, Any]) -> dict[str, Any]:
    date = _text(row['date'])
    conversations = _json_load(row['conversations_json'])
    if not isinstance(conversations, dict):
        raise MigrationImportError('daily cost conversations are invalid')
    cost = row['cost']
    if not isinstance(cost, (int, float)) or isinstance(cost, bool):
        raise MigrationImportError('daily cost amount is invalid')
    computed_at = _int(row['computed_at'])
    if not 0 < computed_at < 2**64:
        raise MigrationImportError('daily cost computed_at must be a positive u64')
    return {'date': date, 'cost': float(cost), 'conversations': conversations,
            'computed_at': computed_at}


def _verify_daily_cost(
    session: OfflineMigrationSession, document: dict[str, Any],
) -> None:
    observed = _query(session, 'migration.daily.cost.get', {'date': document['date']})
    if not isinstance(observed, dict):
        raise MigrationImportError('daily cost source and target snapshots differ')
    if (observed.get('date') != document['date']
            or float(observed.get('cost')) != document['cost']
            or observed.get('conversations') != document['conversations']
            or observed.get('computed_at') != document['computed_at']):
        raise MigrationImportError('daily cost source and target snapshots differ')


def _daily_cost_catalog(session: OfflineMigrationSession) -> Iterator[str]:
    cursor = None
    previous = ''
    while True:
        page = _query(session, 'migration.daily.cost.scan', {'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']
                or any(not isinstance(row, dict) or set(row) != {'date'}
                       or not isinstance(row['date'], str) for row in page['rows'])):
            raise MigrationImportError('invalid daily cost catalog page')
        for row in page['rows']:
            if row['date'] <= previous:
                raise MigrationImportError('daily cost catalog order did not advance')
            previous = row['date']
            yield row['date']
        following = page.get('cursor')
        if following is None:
            return
        if (not page['rows'] or not isinstance(following, str)
                or not following.startswith('daily_cost:')
                or following[len('daily_cost:'):] != previous):
            raise MigrationImportError('daily cost catalog cursor did not advance')
        cursor = following


def _verify_daily_cost_catalog(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, *,
    expected_rows: int,
) -> None:
    table = _plan_table('daily_cost')
    owner_user_id = _session_owner(session)
    rows = 0
    for date in _daily_cost_catalog(session):
        row = (source.point(table['name'], table['columns'], table['keys'],
                            (owner_user_id, date))
               if table['name'] in source.tables else None)
        if row is None:
            raise MigrationImportError('daily cost target holds a date the source does not')
        _verify_daily_cost(session, _daily_cost_document(row))
        rows += 1
    if rows != expected_rows:
        raise MigrationImportError('daily cost source and target catalogs differ')


def _verify_daily_costs(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, *,
    until: Sequence[Any] | None, complete: bool,
) -> tuple[int, str]:
    rows = 0
    digest = hashlib.sha256(b'tofu-db.daily-cost-snapshot-chain.v1').hexdigest()
    table = _plan_table('daily_cost')
    owner_user_id = _session_owner(session)
    for row in _scan(source, table, until=until, complete=complete):
        if _int(row['user_id']) != owner_user_id:
            raise MigrationImportError(
                'daily cost snapshots belong to owners outside the operator scope')
        document = _daily_cost_document(row)
        _verify_daily_cost(session, document)
        digest = _record_chain(digest, document)
        rows += 1
    if complete:
        _verify_daily_cost_catalog(source, session, expected_rows=rows)
    return rows, digest


def _import_daily_cost(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    table = _plan_table('daily_cost')
    owner_user_id = _session_owner(session)
    progress = _resume_or_empty(checkpoint, 'daily_cost')
    after = None
    if progress is not None:
        rows, digest = _verify_daily_costs(
            source, session,
            until=None if progress['complete'] else progress['cursor'],
            complete=progress['complete'])
        if rows != progress['rows'] or digest != progress['sha256']:
            raise MigrationImportError(
                'resumed daily cost target no longer matches progress')
        if progress['complete']:
            return
        after = progress['cursor']
    last: list[Any] | None = None if after is None else list(after)
    pending = 0
    # Running totals carry the already-verified prefix so incremental
    # publication never regresses a recorded checkpoint.
    rows = 0 if progress is None else progress['rows']
    digest = (hashlib.sha256(b'tofu-db.daily-cost-snapshot-chain.v1').hexdigest()
              if progress is None else progress['sha256'])
    iterator = (source.rows(table['name'], table['columns'], table['keys'],
                            after=after, batch_rows=batch_rows)
                if table['name'] in source.tables else ())
    for row in iterator:
        if _int(row['user_id']) != owner_user_id:
            raise MigrationImportError(
                'daily cost snapshots belong to owners outside the operator scope')
        document = _daily_cost_document(row)
        _command(session, 'migration.daily.cost.restore', document)
        _verify_daily_cost(session, document)
        digest = _record_chain(digest, document)
        rows += 1
        last = list(_pk(table, row))
        pending += 1
        if pending == batch_rows:
            _publish(checkpoint, 'daily_cost', cursor=last, rows=rows, digest=digest,
                     complete=False, after_verified_prefix=after_verified_prefix)
            pending = 0
    final_rows, final_digest = _verify_daily_costs(
        source, session, until=None, complete=True)
    if final_rows != rows or final_digest != digest:
        raise MigrationImportError('daily cost source and target snapshots differ')
    _publish(checkpoint, 'daily_cost', cursor=last or [], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


def _rate_limit_document(row: dict[str, Any]) -> dict[str, Any]:
    occurred_at_ms = _int(row['occurred_at_ms'])
    expires_at_ms = _int(row['expires_at_ms'])
    if not 0 < occurred_at_ms < 2**64 or not occurred_at_ms < expires_at_ms < 2**64:
        raise MigrationImportError('rate limit event timestamps are invalid')
    return {'event_id': _text(row['event_id']),
            'endpoint': _text(row['endpoint']),
            'client_key': _text(row['client_key']),
            'occurred_at_ms': occurred_at_ms,
            'expires_at_ms': expires_at_ms}


def _verify_rate_limit_event(
    session: OfflineMigrationSession, document: dict[str, Any],
) -> None:
    observed = _query(session, 'migration.rate.limit.event.get',
                      {'event_id': document['event_id']})
    if not isinstance(observed, dict):
        raise MigrationImportError('rate limit source and target snapshots differ')
    for field in ('event_id', 'endpoint', 'client_key',
                  'occurred_at_ms', 'expires_at_ms'):
        if observed.get(field) != document[field]:
            raise MigrationImportError('rate limit source and target snapshots differ')


def _rate_limit_catalog(session: OfflineMigrationSession) -> Iterator[str]:
    cursor = None
    previous = ''
    while True:
        page = _query(session, 'migration.rate.limit.event.scan', {'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']
                or any(not isinstance(row, dict) or set(row) != {'event_id'}
                       or not isinstance(row['event_id'], str) for row in page['rows'])):
            raise MigrationImportError('invalid rate limit catalog page')
        for row in page['rows']:
            if row['event_id'] <= previous:
                raise MigrationImportError('rate limit catalog order did not advance')
            previous = row['event_id']
            yield row['event_id']
        following = page.get('cursor')
        if following is None:
            return
        if (not page['rows'] or not isinstance(following, str)
                or not following.startswith('rate_limit:')
                or following[len('rate_limit:'):] != previous):
            raise MigrationImportError('rate limit catalog cursor did not advance')
        cursor = following


def _verify_rate_limit_catalog(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, *,
    expected_rows: int,
) -> None:
    table = _plan_table('rate_limit_events')
    rows = 0
    for event_id in _rate_limit_catalog(session):
        row = (source.point(table['name'], table['columns'], table['keys'], (event_id,))
               if table['name'] in source.tables else None)
        if row is None:
            raise MigrationImportError('rate limit target holds an event the source does not')
        _verify_rate_limit_event(session, _rate_limit_document(row))
        rows += 1
    if rows != expected_rows:
        raise MigrationImportError('rate limit source and target catalogs differ')


def _verify_rate_limit_events(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, *,
    until: Sequence[Any] | None, complete: bool,
) -> tuple[int, str]:
    rows = 0
    digest = hashlib.sha256(b'tofu-db.rate-limit-event-snapshot-chain.v1').hexdigest()
    table = _plan_table('rate_limit_events')
    for row in _scan(source, table, until=until, complete=complete):
        document = _rate_limit_document(row)
        _verify_rate_limit_event(session, document)
        digest = _record_chain(digest, document)
        rows += 1
    if complete:
        _verify_rate_limit_catalog(source, session, expected_rows=rows)
    return rows, digest


def _import_rate_limit_events(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    table = _plan_table('rate_limit_events')
    progress = _resume_or_empty(checkpoint, 'rate_limit_events')
    after = None
    if progress is not None:
        rows, digest = _verify_rate_limit_events(
            source, session,
            until=None if progress['complete'] else progress['cursor'],
            complete=progress['complete'])
        if rows != progress['rows'] or digest != progress['sha256']:
            raise MigrationImportError(
                'resumed rate limit target no longer matches progress')
        if progress['complete']:
            return
        after = progress['cursor']
    last: list[Any] | None = None if after is None else list(after)
    pending = 0
    rows = 0 if progress is None else progress['rows']
    digest = (hashlib.sha256(b'tofu-db.rate-limit-event-snapshot-chain.v1').hexdigest()
              if progress is None else progress['sha256'])
    iterator = (source.rows(table['name'], table['columns'], table['keys'],
                            after=after, batch_rows=batch_rows)
                if table['name'] in source.tables else ())
    for row in iterator:
        document = _rate_limit_document(row)
        _command(session, 'migration.rate.limit.event.restore', document)
        _verify_rate_limit_event(session, document)
        digest = _record_chain(digest, document)
        rows += 1
        last = list(_pk(table, row))
        pending += 1
        if pending == batch_rows:
            _publish(checkpoint, 'rate_limit_events', cursor=last, rows=rows, digest=digest,
                     complete=False, after_verified_prefix=after_verified_prefix)
            pending = 0
    final_rows, final_digest = _verify_rate_limit_events(
        source, session, until=None, complete=True)
    if final_rows != rows or final_digest != digest:
        raise MigrationImportError('rate limit source and target snapshots differ')
    _publish(checkpoint, 'rate_limit_events', cursor=last or [], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


def _json_object(value: Any, name: str) -> dict[str, Any]:
    loaded = _json_load(value)
    if not isinstance(loaded, dict):
        raise MigrationImportError(f'{name} is not a JSON object')
    return loaded


def _queue_item_document(row: dict[str, Any], owner: int) -> dict[str, Any]:
    if _int(row['user_id']) != owner:
        raise MigrationImportError(
            'queue snapshots belong to owners outside the operator scope')
    leased_until = row['leased_until_ms']
    document = {
        'id': _text(row['id']),
        'conv_id': _text(row['conv_id']),
        'payload': _json_object(row['payload_json'], 'queue payload'),
        'config': _json_object(row['config_json'], 'queue config'),
        'position': _int(row['position']),
        'kind': _text(row['kind']),
        'priority': _int(row['priority']),
        'created_at_ms': _int(row['created_at_ms']),
        'leased_until_ms': None if leased_until is None else _int(leased_until),
        'lease_task_id': _text(row['lease_task_id']),
        'input_turn_id': _text(row['input_turn_id']),
        'output_turn_id': _text(row['output_turn_id']),
        'attempt_id': _text(row['attempt_id']),
    }
    if (not document['id'] or not document['conv_id']
            or document['kind'] not in (
                'real', 'goal_continuation', 'peer_msg', 'workflow_step', 'autopilot')
            or not 0 < document['position'] < 2**64
            or not 0 <= document['priority'] < 2**64
            or not 0 < document['created_at_ms'] < 2**64
            or (document['leased_until_ms'] is not None
                and not 0 < document['leased_until_ms'] < 2**64)):
        raise MigrationImportError('queue item snapshot is invalid')
    return document


def _verify_queue_item(session: OfflineMigrationSession,
                       document: dict[str, Any]) -> None:
    observed = _query(session, 'migration.queue.item.get', {'id': document['id']})
    if observed != document:
        raise MigrationImportError('queue item source and target snapshots differ')


def _queue_item_catalog(session: OfflineMigrationSession) -> Iterator[str]:
    cursor = None
    previous = ''
    while True:
        page = _query(session, 'migration.queue.item.scan', {'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']
                or any(not isinstance(row, dict) or set(row) != {'id'}
                       or not isinstance(row['id'], str) for row in page['rows'])):
            raise MigrationImportError('invalid queue item catalog page')
        for row in page['rows']:
            if row['id'] <= previous:
                raise MigrationImportError('queue item catalog order did not advance')
            previous = row['id']
            yield row['id']
        following = page.get('cursor')
        if following is None:
            return
        if (not page['rows'] or not isinstance(following, str)
                or not following.startswith('queue_item:')
                or following[len('queue_item:'):] != previous):
            raise MigrationImportError('queue item catalog cursor did not advance')
        cursor = following


def _import_queue_items(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    del batch_rows  # insert-or-match replays are verified one row at a time.
    owner = _session_owner(session)
    table = _plan_table('queue_items')
    rows = 0
    digest = hashlib.sha256(b'tofu-db.queue-item-snapshot-chain.v1').hexdigest()
    for row in _scan(source, table):
        document = _queue_item_document(row, owner)
        _command(session, 'migration.queue.item.restore', document)
        _verify_queue_item(session, document)
        digest = _record_chain(digest, document)
        rows += 1
    catalog_rows = 0
    for queue_id in _queue_item_catalog(session):
        row = (source.point(table['name'], table['columns'], table['keys'], (queue_id,))
               if table['name'] in source.tables else None)
        if row is None:
            raise MigrationImportError('queue target holds an item the source does not')
        _verify_queue_item(session, _queue_item_document(row, owner))
        catalog_rows += 1
    if catalog_rows != rows:
        raise MigrationImportError('queue item source and target catalogs differ')
    progress = _resume_or_empty(checkpoint, 'queue_items')
    if progress and (not progress['complete'] or progress['rows'] != rows
                     or progress['sha256'] != digest):
        raise MigrationImportError('resumed queue item verification differs')
    _publish(checkpoint, 'queue_items', cursor=[], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


def _autopilot_marker_document(row: dict[str, Any], owner: int) -> dict[str, Any]:
    if _int(row['user_id']) != owner:
        raise MigrationImportError(
            'autopilot markers belong to owners outside the operator scope')
    document = {
        'conv_id': _text(row['conv_id']),
        'queue_id': _text(row['queue_id']),
        'config': _json_object(row['config_json'], 'autopilot marker config'),
        'created_at_ms': _int(row['created_at_ms']),
    }
    if (not document['conv_id'] or not document['queue_id']
            or not 0 < document['created_at_ms'] < 2**64):
        raise MigrationImportError('autopilot marker snapshot is invalid')
    return document


def _verify_autopilot_marker(session: OfflineMigrationSession,
                             document: dict[str, Any]) -> None:
    observed = _query(session, 'migration.queue.autopilot.marker.get',
                      {'conv_id': document['conv_id']})
    if observed != document:
        raise MigrationImportError('autopilot marker source and target differ')


def _autopilot_marker_catalog(session: OfflineMigrationSession) -> Iterator[str]:
    cursor = None
    previous = ''
    while True:
        page = _query(session, 'migration.queue.autopilot.marker.scan',
                      {'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']
                or any(not isinstance(row, dict) or set(row) != {'conv_id'}
                       or not isinstance(row['conv_id'], str) for row in page['rows'])):
            raise MigrationImportError('invalid autopilot marker catalog page')
        for row in page['rows']:
            if row['conv_id'] <= previous:
                raise MigrationImportError('autopilot marker catalog did not advance')
            previous = row['conv_id']
            yield row['conv_id']
        following = page.get('cursor')
        if following is None:
            return
        if (not page['rows'] or not isinstance(following, str)
                or not following.startswith('queue_autopilot:')
                or following[len('queue_autopilot:'):] != previous):
            raise MigrationImportError('autopilot marker catalog cursor did not advance')
        cursor = following


def _import_autopilot_markers(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    del batch_rows
    owner = _session_owner(session)
    table = _plan_table('autopilot_markers')
    rows = 0
    digest = hashlib.sha256(b'tofu-db.autopilot-marker-snapshot-chain.v1').hexdigest()
    for row in _scan(source, table):
        document = _autopilot_marker_document(row, owner)
        _command(session, 'migration.queue.autopilot.marker.restore', document)
        _verify_autopilot_marker(session, document)
        digest = _record_chain(digest, document)
        rows += 1
    catalog_rows = 0
    for conv_id in _autopilot_marker_catalog(session):
        row = (source.point(table['name'], table['columns'], table['keys'], (conv_id,))
               if table['name'] in source.tables else None)
        if row is None:
            raise MigrationImportError('autopilot target holds a marker the source does not')
        _verify_autopilot_marker(session, _autopilot_marker_document(row, owner))
        catalog_rows += 1
    if catalog_rows != rows:
        raise MigrationImportError('autopilot marker source and target catalogs differ')
    progress = _resume_or_empty(checkpoint, 'autopilot_markers')
    if progress and (not progress['complete'] or progress['rows'] != rows
                     or progress['sha256'] != digest):
        raise MigrationImportError('resumed autopilot marker verification differs')
    _publish(checkpoint, 'autopilot_markers', cursor=[], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


_TIMER_TEXT_FIELDS = (
    'id', 'conv_id', 'source_task_id', 'check_instruction', 'check_command',
    'continuation_message', 'status', 'created_at', 'updated_at',
    'triggered_at', 'cancelled_at', 'execution_task_id', 'last_poll_at',
    'last_poll_decision', 'last_poll_reason', 'condition_kind',
    'condition_command', 'condition_regex', 'promoted_at', 'origin',
)
_TIMER_INT_FIELDS = (
    'poll_interval', 'max_polls', 'poll_count', 'promotion_streak',
    'fallback_streak',
)


def _timer_document(row: dict[str, Any], owner: int) -> dict[str, Any]:
    if _int(row['user_id']) != owner:
        raise MigrationImportError(
            'timer snapshots belong to owners outside the operator scope')
    document: dict[str, Any] = {'user_id': owner}
    for field in _TIMER_TEXT_FIELDS:
        document[field] = _text(row[field])
    for field in _TIMER_INT_FIELDS:
        document[field] = _int(row[field])
    document['tools_config'] = _json_object(row['tools_config'], 'timer tools config')
    if not document['id'] or not document['conv_id'] or not document['status']:
        raise MigrationImportError('timer snapshot is invalid')
    return document


def _verify_timer(session: OfflineMigrationSession, timer_id: str,
                  document: dict[str, Any]) -> None:
    observed = _query(session, 'migration.timer.get', {'id': timer_id})
    if not isinstance(observed, dict) or observed.get('document') != document:
        raise MigrationImportError('timer source and target snapshots differ')


def _timer_catalog(session: OfflineMigrationSession) -> Iterator[str]:
    cursor = None
    previous = ''
    while True:
        page = _query(session, 'migration.timer.scan', {'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']
                or any(not isinstance(row, dict) or set(row) != {'id'}
                       or not isinstance(row['id'], str) for row in page['rows'])):
            raise MigrationImportError('invalid timer catalog page')
        for row in page['rows']:
            if row['id'] <= previous:
                raise MigrationImportError('timer catalog order did not advance')
            previous = row['id']
            yield row['id']
        following = page.get('cursor')
        if following is None:
            return
        if (not page['rows'] or not isinstance(following, str)
                or not following.startswith('timer:')
                or following[len('timer:'):] != previous):
            raise MigrationImportError('timer catalog cursor did not advance')
        cursor = following


_TIMER_POLL_TEXT_FIELDS = (
    'poll_time', 'decision', 'reason', 'check_output', 'model', 'poll_id',
    'raw_output', 'tier',
)
_TIMER_POLL_INT_FIELDS = ('tokens_used', 'predicate_matched', 'llm_agreed')


def _timer_poll_document(row: dict[str, Any]) -> tuple[int, str, dict[str, Any]]:
    poll_id = _int(row['id'])
    timer_id = _text(row['timer_id'])
    document: dict[str, Any] = {}
    for field in _TIMER_POLL_TEXT_FIELDS:
        document[field] = _text(row[field])
    for field in _TIMER_POLL_INT_FIELDS:
        document[field] = _int(row[field])
    if not 0 < poll_id < 2**64 or not timer_id or not document['poll_time']:
        raise MigrationImportError('timer poll snapshot is invalid')
    return poll_id, timer_id, document


def _verify_timer_poll(session: OfflineMigrationSession, timer_id: str,
                       poll_id: int, document: dict[str, Any]) -> None:
    observed = _query(session, 'migration.timer.poll.get',
                      {'timer_id': timer_id, 'id': poll_id})
    if not isinstance(observed, dict) or observed.get('poll') != document:
        raise MigrationImportError('timer poll source and target snapshots differ')


def _timer_poll_catalog(session: OfflineMigrationSession,
                        timer_id: str) -> Iterator[int]:
    cursor = None
    previous = -1
    while True:
        page = _query(session, 'migration.timer.poll.scan',
                      {'timer_id': timer_id, 'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']
                or any(not isinstance(row, dict) or set(row) != {'id'}
                       or type(row['id']) is not int or row['id'] <= 0
                       for row in page['rows'])):
            raise MigrationImportError('invalid timer poll catalog page')
        for row in page['rows']:
            if row['id'] <= previous:
                raise MigrationImportError('timer poll catalog order did not advance')
            previous = row['id']
            yield row['id']
        following = page.get('cursor')
        if following is None:
            return
        if not page['rows'] or type(following) is not int or following != previous:
            raise MigrationImportError('timer poll catalog cursor did not advance')
        cursor = following


def _import_timers(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    del batch_rows
    owner = _session_owner(session)
    timer_table = _plan_table('timers', 'storage_timers')
    poll_table = _plan_table('timers', 'storage_timer_poll_log')
    rows = 0
    digest = hashlib.sha256(b'tofu-db.timer-snapshot-chain.v1').hexdigest()
    for row in _scan(source, timer_table):
        document = _timer_document(row, owner)
        _command(session, 'migration.timer.restore',
                 {'document': document, 'updated_at_ms': 1})
        _verify_timer(session, document['id'], document)
        digest = _record_chain(digest, document)
        rows += 1
    poll_rows = 0
    restored_timers: set[str] = set()
    for row in _scan(source, timer_table):
        restored_timers.add(_text(row['id']))
    timer_polls: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for row in _scan(source, poll_table):
        poll_id, timer_id, document = _timer_poll_document(row)
        if timer_id not in restored_timers:
            raise MigrationImportError(
                'timer poll references a timer outside the operator scope')
        expected = dict(document)
        expected['id'] = poll_id
        expected['timer_id'] = timer_id
        _command(session, 'migration.timer.poll.restore',
                 {'timer_id': timer_id, 'id': poll_id, 'poll': document})
        _verify_timer_poll(session, timer_id, poll_id, expected)
        digest = _record_chain(digest, expected)
        rows += 1
        timer_polls.setdefault(timer_id, []).append((poll_id, expected))
        poll_rows += 1
    catalog_rows = 0
    for timer_id in _timer_catalog(session):
        row = (source.point(timer_table['name'], timer_table['columns'],
                            timer_table['keys'], (timer_id,))
               if timer_table['name'] in source.tables else None)
        if row is None:
            raise MigrationImportError('timer target holds a timer the source does not')
        _verify_timer(session, timer_id, _timer_document(row, owner))
        catalog_rows += 1
        observed_polls = list(_timer_poll_catalog(session, timer_id))
        expected_polls = sorted(poll_id for poll_id, _ in timer_polls.get(timer_id, []))
        if observed_polls != expected_polls:
            raise MigrationImportError('timer poll source and target catalogs differ')
    if catalog_rows != len(restored_timers):
        raise MigrationImportError('timer source and target catalogs differ')
    progress = _resume_or_empty(checkpoint, 'timers')
    if progress and (not progress['complete'] or progress['rows'] != rows
                     or progress['sha256'] != digest):
        raise MigrationImportError('resumed timer verification differs')
    _publish(checkpoint, 'timers', cursor=[], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


_SCHEDULER_TEXT_FIELDS = (
    'id', 'system_key', 'name', 'schedule', 'task_type', 'command',
    'description', 'last_run', 'last_result', 'last_status', 'created_at',
    'updated_at', 'target_conv_id', 'source_conv_id', 'last_poll_at',
    'last_poll_decision', 'last_poll_reason', 'last_execution_at',
    'last_execution_task_id', 'last_execution_status', 'expires_at',
    'condition_kind', 'condition_command', 'condition_regex', 'promoted_at',
)
_SCHEDULER_INT_FIELDS = (
    'enabled', 'notify_on_failure', 'notify_on_success', 'max_runtime',
    'run_count', 'fail_count', 'poll_count', 'execution_count',
    'max_executions', 'promotion_streak', 'fallback_streak',
)


def _scheduler_task_document(row: dict[str, Any], owner: int) -> dict[str, Any]:
    if _int(row['user_id']) != owner:
        raise MigrationImportError(
            'scheduler snapshots belong to owners outside the operator scope')
    document: dict[str, Any] = {'user_id': owner}
    for field in _SCHEDULER_TEXT_FIELDS:
        document[field] = _text(row[field])
    for field in _SCHEDULER_INT_FIELDS:
        document[field] = _int(row[field])
    document['tools_config'] = _json_object(
        row['tools_config'], 'scheduler tools config')
    if not document['id'] or not document['created_at']:
        raise MigrationImportError('scheduler task snapshot is invalid')
    return document


def _verify_scheduler_task(session: OfflineMigrationSession, task_id: str,
                           document: dict[str, Any]) -> None:
    observed = _query(session, 'migration.scheduler.task.get', {'id': task_id})
    if not isinstance(observed, dict) or observed.get('document') != document:
        raise MigrationImportError('scheduler task source and target differ')


def _scheduler_task_catalog(session: OfflineMigrationSession) -> Iterator[str]:
    cursor = None
    previous = ''
    while True:
        page = _query(session, 'migration.scheduler.task.scan', {'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']
                or any(not isinstance(row, dict) or set(row) != {'id'}
                       or not isinstance(row['id'], str) for row in page['rows'])):
            raise MigrationImportError('invalid scheduler task catalog page')
        for row in page['rows']:
            if row['id'] <= previous:
                raise MigrationImportError('scheduler task catalog did not advance')
            previous = row['id']
            yield row['id']
        following = page.get('cursor')
        if following is None:
            return
        if (not page['rows'] or not isinstance(following, str)
                or not following.startswith('scheduler_task:')
                or following[len('scheduler_task:'):] != previous):
            raise MigrationImportError('scheduler task catalog cursor did not advance')
        cursor = following


_SCHEDULER_POLL_TEXT_FIELDS = (
    'poll_time', 'decision', 'reason', 'status_snapshot', 'model',
    'execution_task_id', 'tier',
)
_SCHEDULER_POLL_INT_FIELDS = ('tokens_used', 'predicate_matched', 'llm_agreed')


def _scheduler_poll_document(row: dict[str, Any]) -> tuple[int, str, dict[str, Any]]:
    poll_id = _int(row['id'])
    task_id = _text(row['task_id'])
    document: dict[str, Any] = {}
    for field in _SCHEDULER_POLL_TEXT_FIELDS:
        document[field] = _text(row[field])
    for field in _SCHEDULER_POLL_INT_FIELDS:
        document[field] = _int(row[field])
    if not 0 < poll_id < 2**64 or not task_id or not document['poll_time']:
        raise MigrationImportError('scheduler poll snapshot is invalid')
    return poll_id, task_id, document


def _verify_scheduler_poll(session: OfflineMigrationSession, task_id: str,
                           poll_id: int, document: dict[str, Any]) -> None:
    observed = _query(session, 'migration.scheduler.poll.get',
                      {'task_id': task_id, 'id': poll_id})
    if not isinstance(observed, dict) or observed.get('poll') != document:
        raise MigrationImportError('scheduler poll source and target differ')


def _scheduler_poll_catalog(session: OfflineMigrationSession,
                            task_id: str) -> Iterator[int]:
    cursor = None
    previous = -1
    while True:
        page = _query(session, 'migration.scheduler.poll.scan',
                      {'task_id': task_id, 'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']
                or any(not isinstance(row, dict) or set(row) != {'id'}
                       or type(row['id']) is not int or row['id'] <= 0
                       for row in page['rows'])):
            raise MigrationImportError('invalid scheduler poll catalog page')
        for row in page['rows']:
            if row['id'] <= previous:
                raise MigrationImportError('scheduler poll catalog did not advance')
            previous = row['id']
            yield row['id']
        following = page.get('cursor')
        if following is None:
            return
        if not page['rows'] or type(following) is not int or following != previous:
            raise MigrationImportError('scheduler poll catalog cursor did not advance')
        cursor = following


def _import_scheduled_tasks(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    del batch_rows
    owner = _session_owner(session)
    task_table = _plan_table('scheduled_tasks', 'storage_scheduled_tasks')
    poll_table = _plan_table('scheduled_tasks', 'storage_proactive_poll_log')
    rows = 0
    digest = hashlib.sha256(b'tofu-db.scheduler-snapshot-chain.v1').hexdigest()
    restored_tasks: set[str] = set()
    for row in _scan(source, task_table):
        document = _scheduler_task_document(row, owner)
        _command(session, 'migration.scheduler.task.restore',
                 {'document': document, 'updated_at_ms': 1})
        _verify_scheduler_task(session, document['id'], document)
        restored_tasks.add(document['id'])
        digest = _record_chain(digest, document)
        rows += 1
    task_polls: dict[str, list[int]] = {}
    for row in _scan(source, poll_table):
        poll_id, task_id, document = _scheduler_poll_document(row)
        if task_id not in restored_tasks:
            raise MigrationImportError(
                'scheduler poll references a task outside the operator scope')
        expected = dict(document)
        expected['id'] = poll_id
        expected['task_id'] = task_id
        _command(session, 'migration.scheduler.poll.restore',
                 {'task_id': task_id, 'id': poll_id, 'poll': document})
        _verify_scheduler_poll(session, task_id, poll_id, expected)
        digest = _record_chain(digest, expected)
        rows += 1
        task_polls.setdefault(task_id, []).append(poll_id)
    catalog_rows = 0
    for task_id in _scheduler_task_catalog(session):
        row = (source.point(task_table['name'], task_table['columns'],
                            task_table['keys'], (task_id,))
               if task_table['name'] in source.tables else None)
        if row is None:
            raise MigrationImportError('scheduler target holds a task the source does not')
        _verify_scheduler_task(session, task_id, _scheduler_task_document(row, owner))
        catalog_rows += 1
        observed_polls = list(_scheduler_poll_catalog(session, task_id))
        if observed_polls != sorted(task_polls.get(task_id, [])):
            raise MigrationImportError('scheduler poll source and target catalogs differ')
    if catalog_rows != len(restored_tasks):
        raise MigrationImportError('scheduler task source and target catalogs differ')
    progress = _resume_or_empty(checkpoint, 'scheduled_tasks')
    if progress and (not progress['complete'] or progress['rows'] != rows
                     or progress['sha256'] != digest):
        raise MigrationImportError('resumed scheduler verification differs')
    _publish(checkpoint, 'scheduled_tasks', cursor=[], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


_WORKER_JOB_INT_FIELDS = (
    'priority', 'available_at_ms', 'lease_deadline_ms', 'fencing_token',
    'attempt_no', 'heartbeat_at_ms', 'cancel_sequence',
    'cancel_requested_at_ms', 'replay_cursor', 'created_at_ms',
    'updated_at_ms', 'terminal_at_ms',
)
_WORKER_JOB_TEXT_FIELDS = (
    'task_id', 'tenant_id', 'task_kind', 'idempotency_key', 'request_digest',
    'status', 'claim_owner', 'cancel_reason', 'result_ref',
)
_WORKER_JOB_STATUSES = WORKER_JOB_STATES


def _worker_job_document(row: dict[str, Any], owner: int) -> dict[str, Any]:
    if _int(row['user_id']) != owner:
        raise MigrationImportError(
            'worker job snapshots belong to owners outside the operator scope')
    document: dict[str, Any] = {}
    for field in _WORKER_JOB_TEXT_FIELDS:
        document[field] = _text(row[field])
    for field in _WORKER_JOB_INT_FIELDS:
        document[field] = _int(row[field])
    document['wake_sequence'] = _int(row.get('wake_sequence', 0))
    epoch = row.get('lease_authority_epoch', '')
    if (not isinstance(epoch, str) or (epoch and (len(epoch) != 32
            or any(byte not in '0123456789abcdef' for byte in epoch)))):
        raise MigrationImportError('worker job lease authority epoch is invalid')
    # Absence and the empty legacy default have the same canonical snapshot.
    # Nonempty source epochs are preserved exactly, never replaced by target life.
    if epoch:
        document['lease_authority_epoch'] = epoch
    document['payload'] = _json_object(row['payload_json'], 'worker job payload')
    document['error'] = _json_object(row['error_json'], 'worker job error')
    clock_ceiling = 9223372036854775000
    if (not document['task_id'] or not document['task_kind']
            or not document['idempotency_key']
            or len(document['request_digest']) != 64
            or any(byte not in '0123456789abcdef'
                   for byte in document['request_digest'])
            or document['status'] not in _WORKER_JOB_STATUSES
            or not 0 <= document['priority'] <= 1000
            or any(not 0 <= document[field] <= clock_ceiling
                   for field in _WORKER_JOB_INT_FIELDS if field != 'priority')):
        raise MigrationImportError('worker job snapshot is invalid')
    return document


def _verify_worker_job(session: OfflineMigrationSession,
                       document: dict[str, Any], owner: int) -> None:
    observed = _query(session, 'migration.worker.job.get',
                      {'task_id': document['task_id']})
    # The native document carries its explicit user owner; the restore
    # payload derives it from the session scope.
    expected = dict(document)
    expected['user_id'] = owner
    if observed != expected:
        raise MigrationImportError('worker job source and target snapshots differ')


def _worker_job_catalog(session: OfflineMigrationSession) -> Iterator[str]:
    cursor = None
    previous = ''
    while True:
        page = _query(session, 'migration.worker.job.scan', {'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']
                or any(not isinstance(row, dict)
                       or set(row) != {'task_id', 'idempotency_key'}
                       or not isinstance(row['task_id'], str)
                       or not isinstance(row['idempotency_key'], str)
                       for row in page['rows'])):
            raise MigrationImportError('invalid worker job catalog page')
        for row in page['rows']:
            if row['idempotency_key'] <= previous:
                raise MigrationImportError('worker job catalog order did not advance')
            previous = row['idempotency_key']
            yield row['task_id']
        following = page.get('cursor')
        if following is None:
            return
        if (not page['rows'] or not isinstance(following, str)
                or not following.startswith('worker_job:')
                or following[len('worker_job:'):] != previous):
            raise MigrationImportError('worker job catalog cursor did not advance')
        cursor = following


def _import_worker_jobs(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    del batch_rows
    owner = _session_owner(session)
    table = dict(_plan_table('worker_jobs'))
    columns = source.table_columns(table['name']) if table['name'] in source.tables else ()
    table['columns'] = [*table['columns'], *(field for field in table['optionalColumns'] if field in columns)]
    rows = 0
    digest = hashlib.sha256(b'tofu-db.worker-job-snapshot-chain.v1').hexdigest()
    for row in _scan(source, table):
        document = _worker_job_document(row, owner)
        _command(session, 'migration.worker.job.restore', document)
        _verify_worker_job(session, document, owner)
        digest = _record_chain(digest, document)
        rows += 1
    catalog_rows = 0
    for task_id in _worker_job_catalog(session):
        row = (source.point(table['name'], table['columns'], table['keys'], (task_id,))
               if table['name'] in source.tables else None)
        if row is None:
            raise MigrationImportError('worker job target holds a job the source does not')
        _verify_worker_job(session, _worker_job_document(row, owner), owner)
        catalog_rows += 1
    if catalog_rows != rows:
        raise MigrationImportError('worker job source and target catalogs differ')
    progress = _resume_or_empty(checkpoint, 'worker_jobs')
    if progress and (not progress['complete'] or progress['rows'] != rows
                     or progress['sha256'] != digest):
        raise MigrationImportError('resumed worker job verification differs')
    _publish(checkpoint, 'worker_jobs', cursor=[], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


def _billing_table(name: str) -> dict[str, Any]:
    return _plan_table('billing', 'billing_' + name)


def _billing_document(name: str, row: dict[str, Any]) -> dict[str, Any]:
    return _wallet_document(row) if name == 'wallets' else _ledger_document(row)


def _billing_catalog(session: OfflineMigrationSession, name: str) -> Iterator[dict[str, Any]]:
    cursor = None
    previous = b''
    while True:
        page = _query(session, 'migration.billing.scan', {'table': name, 'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']
                or any(not isinstance(row, (dict, list)) for row in page['rows'])):
            raise MigrationImportError('invalid billing catalog page')
        yield from page['rows']
        following = page.get('cursor')
        if following is None:
            return
        if (not isinstance(following, str) or not following.startswith(name + ':')
                or not page['rows']):
            raise MigrationImportError('invalid billing catalog cursor')
        try:
            raw = bytes.fromhex(following[len(name) + 1:])
        except ValueError:
            raise MigrationImportError('invalid billing catalog cursor') from None
        if not raw or len(raw) > MAX_ENTITY_KEY_BYTES or raw <= previous:
            raise MigrationImportError('billing catalog did not advance')
        previous, cursor = raw, following


def _verify_billing_row(session: OfflineMigrationSession, name: str,
                        document: dict[str, Any]) -> None:
    identity = document['user_id'] if name == 'wallets' else document['id']
    observed = _query(session, 'migration.billing.get', {'table': name, 'id': identity})
    if observed != document:
        raise MigrationImportError('billing source and target snapshots differ')


def _verify_billing_catalog(source: SQLiteMigrationSource, session: OfflineMigrationSession,
                            name: str) -> int:
    table = _billing_table(name)
    rows = 0
    for identity in _billing_catalog(session, name):
        if set(identity) != {'id'} or not isinstance(identity['id'], str):
            raise MigrationImportError('invalid billing catalog identity')
        row = (source.point(table['name'], table['columns'], table['keys'], (identity['id'],))
               if table['name'] in source.tables else None)
        if row is None:
            raise MigrationImportError('billing target contains an extra primary record')
        _verify_billing_row(session, name, _billing_document(name, row))
        rows += 1
    return rows


def _import_billing_table(source: SQLiteMigrationSource, session: OfflineMigrationSession,
                          checkpoint: MigrationCheckpoint, name: str, *, batch_rows: int,
                          after_verified_prefix: AfterVerified | None) -> tuple[int, str]:
    table = _billing_table(name)
    domain = 'billing.' + name
    progress = _resume_or_empty(checkpoint, domain)
    rows, digest = 0, hashlib.sha256(('tofu-db.' + domain + '.snapshot.v1').encode()).hexdigest()
    after = None
    if progress is not None:
        for row in _scan(source, table, until=None if progress['complete'] else progress['cursor'],
                         complete=progress['complete']):
            document = _billing_document(name, row)
            _verify_billing_row(session, name, document)
            rows += 1
            digest = _record_chain(digest, document)
        if rows != progress['rows'] or digest != progress['sha256']:
            raise MigrationImportError('resumed billing prefix differs')
        if progress['complete']:
            if _verify_billing_catalog(source, session, name) != rows:
                raise MigrationImportError('billing primary catalog count differs')
            return rows, digest
        after = progress['cursor']
    _verify_billing_catalog(source, session, name)
    iterator = (source.rows(table['name'], table['columns'], table['keys'],
                            after=after, batch_rows=batch_rows)
                if table['name'] in source.tables else ())
    pending = 0
    last = [] if after is None else list(after)
    for row in iterator:
        document = _billing_document(name, row)
        _command(session, 'migration.billing.restore', {'table': name, 'document': document})
        _verify_billing_row(session, name, document)
        rows += 1
        digest = _record_chain(digest, document)
        last = list(_pk(table, row))
        pending += 1
        if pending == batch_rows:
            _publish(checkpoint, domain, cursor=last, rows=rows, digest=digest,
                     complete=False, after_verified_prefix=after_verified_prefix)
            pending = 0
    if _verify_billing_catalog(source, session, name) != rows:
        raise MigrationImportError('billing primary catalog count differs')
    _publish(checkpoint, domain, cursor=last, rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)
    return rows, digest


def _verify_billing_indexes(source: SQLiteMigrationSource, session: OfflineMigrationSession,
                            ledger_rows: int) -> None:
    table = _billing_table('ledger')
    eligible = sum(bool(row['ref_type'] and row['ref_id']) for row in _scan(source, table))
    for name, count in (('id_claims', ledger_rows), ('time_index', ledger_rows),
                        ('idempotency', eligible)):
        found = 0
        for identity in _billing_catalog(session, name):
            if set(identity) != {'id'} or not isinstance(identity['id'], str):
                raise MigrationImportError('invalid billing index identity')
            row = source.point(table['name'], table['columns'], table['keys'], (identity['id'],))
            if row is None or (name == 'idempotency' and not (row['ref_type'] and row['ref_id'])):
                raise MigrationImportError('billing index has an extra target')
            found += 1
        if found != count:
            raise MigrationImportError('billing index count differs')
    for row in _billing_catalog(session, 'aggregates'):
        expected = source.billing_aggregate(_text(row.get('user_id')))
        if expected['count'] == 0 or row != {'user_id': row['user_id'], **expected}:
            raise MigrationImportError('billing aggregate differs from source')
    for name in ('reserves', 'reserve_age'):
        for row in _billing_catalog(session, name):
            user, ref = _text(row.get('user_id')), _text(row.get('ref_id'))
            expected = source.billing_aggregate(user, ref)
            if (expected.pop('count') == 0 or row != {'user_id': user, 'ref_id': ref, **expected}
                    or (name == 'reserve_age' and (expected['held_micro'] <= 0
                                                  or expected['last_reserve_ts'] <= 0))):
                raise MigrationImportError('billing reserve evidence differs from source')


def _import_billing(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    # Primary snapshots are independent: ledger-only sources must not acquire
    # synthesized wallets, and wallet values are never reconstructed by apply.
    summaries = {name: _import_billing_table(source, session, checkpoint, name,
                 batch_rows=batch_rows, after_verified_prefix=after_verified_prefix)
                 for name in ('wallets', 'ledger')}
    _verify_billing_indexes(source, session, summaries['ledger'][0])
    rows = sum(value[0] for value in summaries.values())
    digest = hashlib.sha256(_canonical(summaries)).hexdigest()
    progress = _resume_or_empty(checkpoint, 'billing')
    if progress and (not progress['complete'] or progress['rows'] != rows
                     or progress['sha256'] != digest):
        raise MigrationImportError('resumed billing verification differs')
    _publish(checkpoint, 'billing', cursor=[], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


_GOAL_CREATED_BY = 'chat_goal_mode'
_GOAL_ORCHESTRATION_PREFIX = 'chat-goal:'


def _goal_status_from_storage(status: str) -> str:
    return {
        'pending': 'active',
        'running': 'active',
        'paused': 'active',
        'done': 'completed',
        'error': 'failed',
        'aborted': 'cancelled',
    }.get(status, 'active')


def _source_event_rows(
    source: SQLiteMigrationSource, run_id: str, owner: int, tenant_label: str,
) -> list[dict[str, Any]]:
    table = _plan_table('orchestration', 'orchestration_run_events')
    if table['name'] not in source.tables:
        return []
    rows = []
    for row in _scan(source, table):
        if (row['run_id'] == run_id and _int(row['user_id']) == owner
                and _text(row['tenant_id']) == tenant_label):
            rows.append(row)
    rows.sort(key=lambda row: _int(row['seq']))
    return rows


def _orchestration_definition_document(row: dict[str, Any], owner: int) -> dict[str, Any]:
    if _int(row['user_id']) != owner:
        raise MigrationImportError(
            'orchestration snapshots belong to owners outside the operator scope')
    definition = _json_load(row['definition_json'])
    if not isinstance(definition, dict):
        raise MigrationImportError('orchestration definition is invalid')
    created_at_ms = _int(row['created_at_ms'])
    updated_at_ms = _int(row['updated_at_ms'])
    if not 0 < created_at_ms < 2**64 or not created_at_ms <= updated_at_ms < 2**64:
        raise MigrationImportError('orchestration definition timestamps are invalid')
    return {
        'id': _text(row['id']),
        'tenant_label': _text(row['tenant_id']).strip(),
        'name': _text(row['name']),
        'definition': definition,
        'created_at_ms': created_at_ms,
        'updated_at_ms': updated_at_ms,
    }


def _orchestration_run_document(
    source: SQLiteMigrationSource, row: dict[str, Any], owner: int,
) -> dict[str, Any]:
    if _int(row['user_id']) != owner:
        raise MigrationImportError(
            'orchestration snapshots belong to owners outside the operator scope')
    definition = _json_load(row['definition'])
    if not isinstance(definition, dict):
        raise MigrationImportError('orchestration run definition is invalid')
    error_raw = _text(row['error'])
    error = _json_load(error_raw) if error_raw.strip() else None
    status = _text(row['status'])
    if status not in ('pending', 'running', 'paused', 'done', 'error', 'aborted'):
        raise MigrationImportError('orchestration run status is invalid')
    created_at = _int(row['created_at'])
    updated_at = _int(row['updated_at'])
    finished_at = _int(row['finished_at'])
    if not 0 < created_at < 2**64 or not created_at <= updated_at < 2**64:
        raise MigrationImportError('orchestration run timestamps are invalid')
    created_by = _text(row['created_by'])
    tenant_label = _text(row['tenant_id']).strip()
    run_id = _text(row['id'])
    events = _source_event_rows(source, run_id, owner, tenant_label)
    next_event_sequence = len(events)
    goal_status = ''
    goal_reason = ''
    goal_policy: dict[str, Any] = {}
    goal_outcome: dict[str, Any] = {}
    goal_stop_report = ''
    if created_by == _GOAL_CREATED_BY:
        # Reconstruct the settled Goal state from the source event history
        # exactly as the live Goal projection derives it.
        last: dict[str, Any] = {}
        started: dict[str, Any] = {}
        for event in events:
            payload = _json_load(event['payload'])
            if not isinstance(payload, dict):
                continue
            if payload.get('type') == 'goal_run_started':
                if not started:
                    started = payload
                last = payload
            elif payload.get('type') == 'goal_run_transition':
                last = payload
        goal_status = str(last.get('status') or _goal_status_from_storage(status))
        goal_reason = str(last.get('reason') or '')
        policy = started.get('policy')
        goal_policy = dict(policy) if isinstance(policy, dict) else {}
        outcome = last.get('outcome')
        goal_outcome = dict(outcome) if isinstance(outcome, dict) else {}
        goal_stop_report = str(last.get('stopReport') or '')
    pending = _int(row.get('continuity_pending', 0))
    if pending not in (0, 1) or (pending and (
            created_by != _GOAL_CREATED_BY or status != 'error'
            or not isinstance(error, dict) or error.get('kind') != 'worker_lost'
            or error.get('source') != 'orchestration.startup_recovery')):
        raise MigrationImportError('orchestration continuity election is invalid')
    return {
        'id': run_id,
        'tenant_label': tenant_label,
        'orch_id': _text(row['orch_id']),
        'name': _text(row['name']),
        'definition': definition,
        'input': _text(row['input']),
        'created_by': created_by,
        'created_at': created_at,
        'status': status,
        'final_text': _text(row['final']),
        'error': error,
        'updated_at': updated_at,
        'finished_at': finished_at,
        'next_event_sequence': next_event_sequence,
        'continuity_pending': bool(pending),
        'goal_status': goal_status,
        'goal_reason': goal_reason,
        'goal_policy': goal_policy,
        'goal_outcome': goal_outcome,
        'goal_stop_report': goal_stop_report,
    }


def _orchestration_event_document(
    row: dict[str, Any], owner: int,
) -> dict[str, Any]:
    if _int(row['user_id']) != owner:
        raise MigrationImportError(
            'orchestration snapshots belong to owners outside the operator scope')
    payload = _json_load(row['payload'])
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise MigrationImportError('orchestration event payload is invalid')
    sequence = _int(row['seq'])
    if not 0 <= sequence < 2**63:
        raise MigrationImportError('orchestration event sequence is invalid')
    return {
        'run_id': _text(row['run_id']),
        'tenant_label': _text(row['tenant_id']).strip(),
        'sequence': sequence,
        'payload': payload,
    }


def _verify_orchestration_definition(
    session: OfflineMigrationSession, document: dict[str, Any],
) -> None:
    observed = _query(session, 'migration.orchestration.definition.get', {
        'id': document['id'], 'tenant_label': document['tenant_label']})
    if observed != document:
        raise MigrationImportError('orchestration definition source and target differ')


def _verify_orchestration_run(
    session: OfflineMigrationSession, document: dict[str, Any],
) -> None:
    observed = _query(session, 'migration.orchestration.run.get', {
        'id': document['id'], 'tenant_label': document['tenant_label']})
    if observed != document:
        raise MigrationImportError('orchestration run source and target differ')


def _verify_orchestration_event(
    session: OfflineMigrationSession, document: dict[str, Any],
) -> None:
    observed = _query(session, 'migration.orchestration.event.get', {
        'run_id': document['run_id'], 'tenant_label': document['tenant_label'],
        'sequence': document['sequence']})
    if observed != document:
        raise MigrationImportError('orchestration event source and target differ')


def _swarm_session_document(row: dict[str, Any], owner: int) -> dict[str, Any]:
    del owner  # swarm rows carry no owner column; the session is owner-scoped.
    specs = _json_load(row['specs_json'])
    config = _json_load(row['config_json'])
    if not isinstance(specs, list) or not isinstance(config, dict):
        raise MigrationImportError('swarm session document is invalid')
    status = _text(row['status'])
    if status not in ('running', 'terminated', 'quarantined:ownerless'):
        raise MigrationImportError('swarm session status is invalid')
    created_at = _int(row['created_at'])
    updated_at = _int(row['updated_at'])
    if not 0 < created_at < 2**64 or not created_at <= updated_at < 2**64:
        raise MigrationImportError('swarm session timestamps are invalid')
    return {
        'swarm_key': _text(row['swarm_key']),
        'conv_id': _text(row['conv_id']),
        'task_id': _text(row['task_id']),
        'status': status,
        'specs': specs,
        'config': config,
        'created_at': created_at,
        'updated_at': updated_at,
    }


def _swarm_agent_document(row: dict[str, Any]) -> dict[str, Any]:
    messages = _json_load(row['messages_json'])
    result = _json_load(row['result_json'])
    if not isinstance(messages, list) or not isinstance(result, dict):
        raise MigrationImportError('swarm agent document is invalid')
    rounds_used = _int(row['rounds_used'])
    updated_at = _int(row['updated_at'])
    if not 0 <= rounds_used < 2**64 or not 0 < updated_at < 2**64:
        raise MigrationImportError('swarm agent counters are invalid')
    return {
        'swarm_key': _text(row['swarm_key']),
        'agent_id': _text(row['agent_id']),
        'role': _text(row['role']),
        'objective': _text(row['objective']),
        'status': _text(row['status']),
        'messages': messages,
        'result': result,
        'rounds_used': rounds_used,
        'delivered': bool(_int(row['delivered'])),
        'updated_at': updated_at,
    }


def _verify_swarm_session(
    session: OfflineMigrationSession, document: dict[str, Any],
) -> None:
    observed = _query(session, 'migration.swarm.session.get', {
        'swarm_key': document['swarm_key']})
    if observed != document:
        raise MigrationImportError('swarm session source and target differ')


def _verify_swarm_agent(
    session: OfflineMigrationSession, document: dict[str, Any],
) -> None:
    observed = _query(session, 'migration.swarm.agent.get', {
        'swarm_key': document['swarm_key'], 'agent_id': document['agent_id']})
    if observed != document:
        raise MigrationImportError('swarm agent source and target differ')


def _integration_workspace_document(
    row: dict[str, Any], meta: dict[str, Any] | None, owner: int,
) -> dict[str, Any]:
    if _int(row['user_id']) != owner:
        raise MigrationImportError(
            'integration snapshots belong to owners outside the operator scope')
    origin: dict[str, Any] = {}
    if meta is not None:
        loaded = _json_load(meta['origin_json'])
        if isinstance(loaded, dict):
            origin = loaded
    created_at = _optional_float(row['created_at'])
    updated_at = _optional_float(row['updated_at'])
    if created_at is None or updated_at is None or created_at <= 0:
        raise MigrationImportError('integration workspace timestamps are invalid')
    return {
        'id': _int(row['id']),
        'project_root': _text(row['project_root']),
        'task_id': _text(row['task_id']),
        'title': _text(row['title']),
        'workspace_path': _text(row['workspace_path']),
        'managed': bool(_int(row['managed'])),
        'state': _text(row['state']),
        'base_sha': _text(row['base_sha']),
        'checkpoint_sha': _text(row['checkpoint_sha']),
        'candidate_sha': _text(row['candidate_sha']),
        'error': _text(row['error']),
        'origin': origin,
        'created_at': created_at,
        'updated_at': updated_at,
    }


def _integration_event_document(row: dict[str, Any], owner: int) -> dict[str, Any]:
    if _int(row['user_id']) != owner:
        raise MigrationImportError(
            'integration snapshots belong to owners outside the operator scope')
    created_at = _optional_float(row['created_at'])
    if created_at is None:
        raise MigrationImportError('integration event timestamp is invalid')
    return {
        'id': _int(row['id']),
        'project_root': _text(row['project_root']),
        'task_id': _text(row['task_id']),
        'kind': _text(row['kind']),
        'message': _text(row['message']),
        'detail': _text(row['detail']),
        'created_at': created_at,
    }


def _verify_integration_workspace(
    session: OfflineMigrationSession, document: dict[str, Any],
) -> None:
    observed = _query(session, 'migration.integration.workspace.get', {
        'project_root': document['project_root'], 'task_id': document['task_id']})
    if observed != document:
        raise MigrationImportError('integration workspace source and target differ')


def _verify_integration_event(
    session: OfflineMigrationSession, document: dict[str, Any],
) -> None:
    observed = _query(session, 'migration.integration.event.get', {
        'id': document['id'], 'project_root': document['project_root']})
    if observed is None or observed.get('id') != document['id']:
        raise MigrationImportError('integration event source and target differ')


def _import_orchestration(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    owner = _session_owner(session)
    definition_table = _plan_table('orchestration', 'orchestration_definitions')
    run_table = _plan_table('orchestration', 'orchestration_runs')
    event_table = _plan_table('orchestration', 'orchestration_run_events')
    rows = 0
    digest = hashlib.sha256(b'tofu-db.orchestration-snapshot-chain.v1').hexdigest()
    # Definitions and runs restore first; events restore against their run.
    run_ids: list[tuple[str, str]] = []
    for row in _scan(source, definition_table):
        document = _orchestration_definition_document(row, owner)
        _command(session, 'migration.orchestration.definition.restore', document)
        _verify_orchestration_definition(session, document)
        digest = _record_chain(digest, document)
        rows += 1
    for row in _scan(source, run_table):
        document = _orchestration_run_document(source, row, owner)
        _command(session, 'migration.orchestration.run.restore', document)
        _verify_orchestration_run(session, document)
        run_ids.append((document['id'], document['tenant_label']))
        digest = _record_chain(digest, document)
        rows += 1
    restored_runs = set(run_ids)
    for row in _scan(source, event_table):
        document = _orchestration_event_document(row, owner)
        if (document['run_id'], document['tenant_label']) not in restored_runs:
            raise MigrationImportError(
                'orchestration event references a run outside the operator scope')
        _command(session, 'migration.orchestration.event.restore', document)
        _verify_orchestration_event(session, document)
        digest = _record_chain(digest, document)
        rows += 1
    progress = _resume_or_empty(checkpoint, 'orchestration')
    if progress and (not progress['complete'] or progress['rows'] != rows
                     or progress['sha256'] != digest):
        raise MigrationImportError('resumed orchestration verification differs')
    _publish(checkpoint, 'orchestration', cursor=[], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


def _import_swarm(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    owner = _session_owner(session)
    session_table = _plan_table('swarm', 'swarm_sessions')
    agent_table = _plan_table('swarm', 'swarm_agents')
    rows = 0
    digest = hashlib.sha256(b'tofu-db.swarm-snapshot-chain.v1').hexdigest()
    restored_sessions: set[str] = set()
    for row in _scan(source, session_table):
        document = _swarm_session_document(row, owner)
        _command(session, 'migration.swarm.session.restore', document)
        _verify_swarm_session(session, document)
        restored_sessions.add(document['swarm_key'])
        digest = _record_chain(digest, document)
        rows += 1
    for row in _scan(source, agent_table):
        document = _swarm_agent_document(row)
        if document['swarm_key'] not in restored_sessions:
            raise MigrationImportError(
                'swarm agent references a session outside the operator scope')
        _command(session, 'migration.swarm.agent.restore', document)
        _verify_swarm_agent(session, document)
        digest = _record_chain(digest, document)
        rows += 1
    progress = _resume_or_empty(checkpoint, 'swarm')
    if progress and (not progress['complete'] or progress['rows'] != rows
                     or progress['sha256'] != digest):
        raise MigrationImportError('resumed swarm verification differs')
    _publish(checkpoint, 'swarm', cursor=[], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


def _import_integration(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    owner = _session_owner(session)
    workspace_table = _plan_table('integration', 'integration_workspaces')
    event_table = _plan_table('integration', 'integration_events')
    meta_table = _plan_table('integration', 'integration_workspace_meta')
    metas: dict[tuple[str, str], dict[str, Any]] = {}
    if meta_table['name'] in source.tables:
        for row in _scan(source, meta_table):
            if _int(row['user_id']) == owner:
                metas[(_text(row['project_root']), _text(row['task_id']))] = row
    rows = 0
    digest = hashlib.sha256(b'tofu-db.integration-snapshot-chain.v1').hexdigest()
    for row in _scan(source, workspace_table):
        meta = metas.get((_text(row['project_root']), _text(row['task_id'])))
        document = _integration_workspace_document(row, meta, owner)
        _command(session, 'migration.integration.workspace.restore', document)
        _verify_integration_workspace(session, document)
        digest = _record_chain(digest, document)
        rows += 1
    for row in _scan(source, event_table):
        document = _integration_event_document(row, owner)
        _command(session, 'migration.integration.event.restore', document)
        _verify_integration_event(session, document)
        digest = _record_chain(digest, document)
        rows += 1
    progress = _resume_or_empty(checkpoint, 'integration')
    if progress and (not progress['complete'] or progress['rows'] != rows
                     or progress['sha256'] != digest):
        raise MigrationImportError('resumed integration verification differs')
    _publish(checkpoint, 'integration', cursor=[], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


# ---------------------------------------------------------------------------
# Paper domain: library, reports, translations, notes, podcasts. Every row
# replays through its native restore op with exact source fields (no
# server-clock overwrite); get re-verifies the exact snapshot and scan
# enumerates the owner catalog independently.
# ---------------------------------------------------------------------------

_PAPER_TABLES: dict[str, tuple[str, str, str, str]] = {
    # plan table name -> (checkpoint sub-domain, restore, get, scan)
    'paper_library': (
        'paper.library',
        'migration.paper.library.restore',
        'migration.paper.library.get',
        'migration.paper.library.scan',
    ),
    'paper_reports': (
        'paper.report',
        'migration.paper.report.restore',
        'migration.paper.report.get',
        'migration.paper.report.scan',
    ),
    'paper_translations': (
        'paper.translation',
        'migration.paper.translation.restore',
        'migration.paper.translation.get',
        'migration.paper.translation.scan',
    ),
    'paper_notes': (
        'paper.note',
        'migration.paper.note.restore',
        'migration.paper.note.get',
        'migration.paper.note.scan',
    ),
    'paper_podcasts': (
        'paper.podcast',
        'migration.paper.podcast.restore',
        'migration.paper.podcast.get',
        'migration.paper.podcast.scan',
    ),
}

_PAPER_PODCAST_STATUSES = (
    'generating', 'interrupted', 'done', 'script_only', 'error', 'aborted')


def _paper_table(name: str) -> dict[str, Any]:
    return _plan_table('paper', name)


def _paper_library_document(row: dict[str, Any]) -> dict[str, Any]:
    created_at = _nonneg_int(row['created_at'])
    updated_at = _nonneg_int(row['updated_at'])
    if not 0 < created_at < 2**64 or not 0 < updated_at < 2**64 or updated_at < created_at:
        raise MigrationImportError('paper library snapshot metadata is invalid')
    return {
        'id': _text(row['id']),
        'title': _text(row['title']),
        'pdf_url': _text(row['pdf_url']),
        'pdf_filename': _text(row['pdf_filename']),
        'arxiv_id': _text(row['arxiv_id']),
        'paper_hash': _text(row['paper_hash']),
        'parsed_text': _text(row['parsed_text']),
        'parser_version': _text(row['parser_version']),
        'qa_history_json': _text(row['qa_history']),
        'images_json': _text(row['images']),
        'babel_cache_json': _text(row['babel_cache']),
        'page_count': _nonneg_int(row['page_count']),
        'folder_id': _text(row['folder_id']),
        'created_at': created_at,
        'updated_at': updated_at,
    }


def _paper_report_document(row: dict[str, Any]) -> dict[str, Any]:
    meta = _json_load(row['meta'])
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise MigrationImportError('paper report metadata is invalid')
    created_at = _nonneg_int(row['created_at'])
    if not 0 < created_at < 2**64:
        raise MigrationImportError('paper report snapshot metadata is invalid')
    return {
        'paper_hash': _text(row['paper_hash']),
        'lang': _text(row['lang']),
        'report': _text(row['report']),
        'model': _text(row['model']),
        'meta': meta,
        'created_at': created_at,
    }


def _paper_translation_document(row: dict[str, Any]) -> dict[str, Any]:
    created_at = _nonneg_int(row['created_at'])
    if not 0 < created_at < 2**64:
        raise MigrationImportError('paper translation snapshot metadata is invalid')
    return {
        'paper_hash': _text(row['paper_hash']),
        'lang': _text(row['lang']),
        'text': _text(row['text']),
        'model': _text(row['model']),
        'created_at': created_at,
    }


def _paper_note_document(row: dict[str, Any]) -> dict[str, Any]:
    anchor = _json_load(row['anchor'])
    if anchor is None:
        anchor = {}
    if not isinstance(anchor, dict):
        raise MigrationImportError('paper note anchor is invalid')
    created_at = _nonneg_int(row['created_at'])
    updated_at = _nonneg_int(row['updated_at'])
    if not 0 < created_at < 2**64 or not 0 < updated_at < 2**64 or updated_at < created_at:
        raise MigrationImportError('paper note snapshot metadata is invalid')
    return {
        'id': _text(row['id']),
        'paper_hash': _text(row['paper_hash']),
        'lang': _text(row['lang']),
        'anchor': anchor,
        'note': _text(row['note']),
        'created_at': created_at,
        'updated_at': updated_at,
    }


def _paper_podcast_document(row: dict[str, Any]) -> dict[str, Any]:
    script = _json_load(row['script_json'])
    if script is None:
        script = {}
    if not isinstance(script, dict):
        raise MigrationImportError('paper podcast script is invalid')
    meta = _json_load(row['meta'])
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise MigrationImportError('paper podcast metadata is invalid')
    status = _text(row['status'])
    if status not in _PAPER_PODCAST_STATUSES:
        raise MigrationImportError('paper podcast status is invalid')
    duration = row['duration_sec']
    if not isinstance(duration, (int, float)) or isinstance(duration, bool):
        raise MigrationImportError('paper podcast duration is invalid')
    created_at = _nonneg_int(row['created_at'])
    updated_at = _nonneg_int(row['updated_at'])
    if not 0 < created_at < 2**64 or not 0 < updated_at < 2**64 or updated_at < created_at:
        raise MigrationImportError('paper podcast snapshot metadata is invalid')
    return {
        'paper_hash': _text(row['paper_hash']),
        'mode': _text(row['mode']),
        'lang': _text(row['lang']),
        'voice': _text(row['voice']),
        'status': status,
        'script': script,
        'file_path': _text(row['file_path']),
        'duration_sec': float(duration),
        'model': _text(row['model']),
        'tts_model': _text(row['tts_model']),
        'meta': meta,
        'created_at': created_at,
        'updated_at': updated_at,
    }


def _paper_document(name: str, row: dict[str, Any]) -> dict[str, Any]:
    if name == 'paper_library':
        return _paper_library_document(row)
    if name == 'paper_reports':
        return _paper_report_document(row)
    if name == 'paper_translations':
        return _paper_translation_document(row)
    if name == 'paper_notes':
        return _paper_note_document(row)
    return _paper_podcast_document(row)


def _paper_get_identity(name: str, document: dict[str, Any]) -> dict[str, Any]:
    if name == 'paper_library' or name == 'paper_notes':
        return {'id': document['id']}
    if name == 'paper_reports' or name == 'paper_translations':
        return {'paper_hash': document['paper_hash'], 'lang': document['lang']}
    return {
        'paper_hash': document['paper_hash'],
        'mode': document['mode'],
        'lang': document['lang'],
        'voice': document['voice'],
    }


def _verify_paper_row(
    session: OfflineMigrationSession, name: str, document: dict[str, Any],
) -> None:
    _, _, get, _ = _PAPER_TABLES[name]
    observed = _query(session, get, _paper_get_identity(name, document))
    if not isinstance(observed, dict) or observed != document:
        raise MigrationImportError('paper source and target snapshots differ')


def _paper_catalog(session: OfflineMigrationSession, name: str) -> Iterator[dict[str, Any]]:
    _, _, _, scan = _PAPER_TABLES[name]
    cursor = None
    while True:
        page = _query(session, scan, {'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']
                or any(not isinstance(row, dict) for row in page['rows'])):
            raise MigrationImportError('invalid paper catalog page')
        yield from page['rows']
        following = page.get('cursor')
        if following is None:
            return
        if not page['rows'] or not isinstance(following, str):
            raise MigrationImportError('invalid paper catalog cursor')
        cursor = following


def _verify_paper_catalog(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, name: str,
    *, expected_rows: int,
) -> None:
    table = _paper_table(name)
    owner_user_id = _session_owner(session)
    rows = 0
    for identity in _paper_catalog(session, name):
        row = (source.point(table['name'], table['columns'], table['keys'],
                            _paper_source_pk(name, owner_user_id, identity))
               if table['name'] in source.tables else None)
        if row is None:
            raise MigrationImportError('paper target holds a row the source does not')
        _verify_paper_row(session, name, _paper_document(name, row))
        rows += 1
    if rows != expected_rows:
        raise MigrationImportError('paper source and target catalogs differ')


def _paper_source_pk(name: str, owner: int, identity: dict[str, Any]) -> tuple[Any, ...]:
    if name == 'paper_library':
        return (identity['id'], owner)
    if name == 'paper_reports' or name == 'paper_translations':
        return (owner, identity['paper_hash'], identity['lang'])
    if name == 'paper_notes':
        return (owner, identity['id'])
    return (owner, identity['paper_hash'], identity['mode'],
            identity['lang'], identity['voice'])


def _verify_paper_rows(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, name: str, *,
    until: Sequence[Any] | None, complete: bool,
) -> tuple[int, str]:
    rows = 0
    digest = hashlib.sha256(('tofu-db.' + name + '.snapshot-chain.v1').encode()).hexdigest()
    table = _paper_table(name)
    owner_user_id = _session_owner(session)
    for row in _scan(source, table, until=until, complete=complete):
        if _int(row['user_id']) != owner_user_id:
            raise MigrationImportError(
                'paper snapshots belong to owners outside the operator scope')
        document = _paper_document(name, row)
        _verify_paper_row(session, name, document)
        digest = _record_chain(digest, document)
        rows += 1
    if complete:
        _verify_paper_catalog(source, session, name, expected_rows=rows)
    return rows, digest


def _import_paper_table(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, name: str, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> tuple[int, str]:
    table = _paper_table(name)
    domain, restore, _, _ = _PAPER_TABLES[name]
    owner_user_id = _session_owner(session)
    progress = _resume_or_empty(checkpoint, domain)
    after = None
    if progress is not None:
        rows, digest = _verify_paper_rows(
            source, session, name,
            until=None if progress['complete'] else progress['cursor'],
            complete=progress['complete'])
        if rows != progress['rows'] or digest != progress['sha256']:
            raise MigrationImportError('resumed paper target no longer matches progress')
        if progress['complete']:
            return rows, digest
        after = progress['cursor']
    if progress is None:
        for row in _scan(source, table):
            if _int(row['user_id']) != owner_user_id:
                raise MigrationImportError(
                    'paper snapshots belong to owners outside the operator scope')
    last = None if after is None else list(after)
    pending = 0
    rows = 0 if progress is None else progress['rows']
    digest = (hashlib.sha256(('tofu-db.' + name + '.snapshot-chain.v1').encode()).hexdigest()
              if progress is None else progress['sha256'])
    iterator = (source.rows(table['name'], table['columns'], table['keys'],
                            after=after, batch_rows=batch_rows)
                if table['name'] in source.tables else ())
    for row in iterator:
        document = _paper_document(name, row)
        _command(session, restore, document)
        _verify_paper_row(session, name, document)
        digest = _record_chain(digest, document)
        rows += 1
        last = list(_pk(table, row))
        pending += 1
        if pending == batch_rows:
            _publish(checkpoint, domain, cursor=last, rows=rows, digest=digest,
                     complete=False, after_verified_prefix=after_verified_prefix)
            pending = 0
    final_rows, final_digest = _verify_paper_rows(
        source, session, name, until=None, complete=True)
    if final_rows != rows or final_digest != digest:
        raise MigrationImportError('paper source and target snapshots differ')
    _publish(checkpoint, domain, cursor=last or [], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)
    return rows, digest


def _import_paper(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    summaries = {
        name: _import_paper_table(source, session, checkpoint, name,
                                  batch_rows=batch_rows,
                                  after_verified_prefix=after_verified_prefix)
        for name in ('paper_library', 'paper_reports', 'paper_translations',
                     'paper_notes', 'paper_podcasts')
    }
    rows = sum(value[0] for value in summaries.values())
    digest = hashlib.sha256(_canonical(summaries)).hexdigest()
    progress = _resume_or_empty(checkpoint, 'paper')
    if progress and (not progress['complete'] or progress['rows'] != rows
                     or progress['sha256'] != digest):
        raise MigrationImportError('resumed paper verification differs')
    _publish(checkpoint, 'paper', cursor=[], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


# ---------------------------------------------------------------------------
# Knowledge domain: one assembled document snapshot per source document plus
# the owner settings row. Chunk content and asset text route through the
# blob-capable restore without truncation; terms rebuild from chunk search
# text exactly like the live paths; the catalog cross-checks counts.
# ---------------------------------------------------------------------------

_KNOWLEDGE_DOCUMENT_COLUMNS = (
    'id', 'sha256', 'name', 'stored_name', 'kind', 'size_bytes', 'method',
    'warnings_json', 'text_chars', 'chunk_count', 'pages', 'scope',
    'media_metadata_json', 'created_at', 'updated_at',
)


def _knowledge_table(name: str) -> dict[str, Any]:
    return _plan_table('knowledge', name)


def _knowledge_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MigrationImportError('knowledge number field is invalid')
    number = float(value)
    if not number == number or number in (float('inf'), float('-inf')) or number < 0.0:
        raise MigrationImportError('knowledge number field is invalid')
    return 0.0 if number == 0.0 else number


def _knowledge_settings_document(
    row: dict[str, Any],
) -> dict[str, Any]:
    return {
        'enabled': bool(_nonneg_int(row['enabled'])),
        'visual_enrichment': bool(_nonneg_int(row['visual_enrichment'])),
        'updated_at': _knowledge_number(row['updated_at']),
    }


def _knowledge_document_snapshot(
    source: SQLiteMigrationSource, owner: int, document_row: dict[str, Any],
) -> dict[str, Any]:
    document_id = _text(document_row['id'])
    document: dict[str, Any] = {
        'id': document_id,
        'sha256': _text(document_row['sha256']),
        'name': _text(document_row['name']),
        'stored_name': _text(document_row['stored_name']),
        'kind': _text(document_row['kind']),
        'size_bytes': _nonneg_int(document_row['size_bytes']),
        'method': _text(document_row['method']),
        'warnings_json': _text(document_row['warnings_json']),
        'text_chars': _nonneg_int(document_row['text_chars']),
        'chunk_count': _nonneg_int(document_row['chunk_count']),
        'pages': _nonneg_int(document_row['pages']),
        'scope': _text(document_row['scope']),
        'media_metadata_json': _text(document_row['media_metadata_json']),
        'created_at': _knowledge_number(document_row['created_at']),
        'updated_at': _knowledge_number(document_row['updated_at']),
    }
    chunk_table = _knowledge_table('storage_knowledge_chunks')
    chunks: list[dict[str, Any]] = []
    if chunk_table['name'] in source.tables:
        for row in source.rows(
            chunk_table['name'], chunk_table['columns'], chunk_table['keys'],
            batch_rows=MAX_SOURCE_BATCH_ROWS,
        ):
            if _int(row['user_id']) != owner or _text(row['document_id']) != document_id:
                continue
            chunks.append({
                'ordinal': _nonneg_int(row['ordinal']),
                'section': _text(row['section']),
                'location': _text(row['location']),
                'content': _text(row['content']),
                'search_text': _text(row['search_text']),
                'assets': [],
            })
    chunks.sort(key=lambda chunk: chunk['ordinal'])
    if [chunk['ordinal'] for chunk in chunks] != list(range(len(chunks))):
        raise MigrationImportError('knowledge chunk ordinals are not contiguous')
    if len(chunks) != document['chunk_count']:
        raise MigrationImportError('knowledge chunk_count does not match chunks')

    asset_table = _knowledge_table('storage_knowledge_assets')
    assets: list[dict[str, Any]] = []
    if asset_table['name'] in source.tables:
        for row in source.rows(
            asset_table['name'], asset_table['columns'], asset_table['keys'],
            batch_rows=MAX_SOURCE_BATCH_ROWS,
        ):
            if _int(row['user_id']) != owner or _text(row['document_id']) != document_id:
                continue
            assets.append({
                'id': _text(row['id']),
                'ordinal': _nonneg_int(row['ordinal']),
                'kind': _text(row['kind']),
                'stored_name': _text(row['stored_name']),
                'mime_type': _text(row['mime_type']),
                'sha256': _text(row['sha256']),
                'size_bytes': _nonneg_int(row['size_bytes']),
                'width': _nonneg_int(row['width']),
                'height': _nonneg_int(row['height']),
                'page': _nonneg_int(row['page']),
                'pages_json': _text(row['pages_json']),
                'bbox_json': _text(row['bbox_json']),
                'caption': _text(row['caption']),
                'ocr_text': _text(row['ocr_text']),
                'description': _text(row['description']),
                'enrichment_status': _text(row['enrichment_status']),
                'enrichment_model': _text(row['enrichment_model']),
                'enrichment_error': _text(row['enrichment_error']),
                'metadata_json': _text(row['metadata_json']),
                'created_at': _knowledge_number(row['created_at']),
                'updated_at': _knowledge_number(row['updated_at']),
            })
    assets.sort(key=lambda asset: asset['ordinal'])
    if [asset['ordinal'] for asset in assets] != list(range(len(assets))):
        raise MigrationImportError('knowledge asset ordinals are not contiguous')
    document['assets'] = assets

    link_table = _knowledge_table('storage_knowledge_chunk_assets')
    if link_table['name'] in source.tables:
        for row in source.rows(
            link_table['name'], link_table['columns'], link_table['keys'],
            batch_rows=MAX_SOURCE_BATCH_ROWS,
        ):
            if _int(row['user_id']) != owner or _text(row['document_id']) != document_id:
                continue
            chunk_ordinal = _nonneg_int(row['chunk_ordinal'])
            if chunk_ordinal >= len(chunks):
                raise MigrationImportError('knowledge link references a missing chunk')
            chunks[chunk_ordinal]['assets'].append({
                'id': _text(row['asset_id']),
                'relation': _text(row['relation']),
                '_link_ordinal': _nonneg_int(row['ordinal']),
            })
    for chunk in chunks:
        chunk['assets'].sort(key=lambda link: link['_link_ordinal'])
        for link in chunk['assets']:
            del link['_link_ordinal']
    document['chunks'] = chunks
    return document


def _verify_knowledge_settings(
    session: OfflineMigrationSession, document: dict[str, Any],
) -> None:
    observed = _query(session, 'migration.knowledge.settings.get', {})
    if not isinstance(observed, dict) or observed != document:
        raise MigrationImportError('knowledge settings source and target differ')


def _verify_knowledge_document(
    session: OfflineMigrationSession, document: dict[str, Any],
) -> None:
    observed = _query(session, 'migration.knowledge.document.get',
                      {'document_id': document['id']})
    if not isinstance(observed, dict) or observed.get('document') != document:
        raise MigrationImportError('knowledge document source and target differ')


def _verify_knowledge_catalog(
    source: SQLiteMigrationSource, session: OfflineMigrationSession, *,
    expected_documents: int,
) -> None:
    owner_user_id = _session_owner(session)
    document_table = _knowledge_table('storage_knowledge_documents')
    cursor = None
    rows = 0
    while True:
        page = _query(session, 'migration.knowledge.catalog.scan', {'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']
                or any(not isinstance(row, dict) for row in page['rows'])):
            raise MigrationImportError('invalid knowledge catalog page')
        for identity in page['rows']:
            document_id = identity.get('id')
            row = (source.point(document_table['name'], document_table['columns'],
                                document_table['keys'], (owner_user_id, document_id))
                   if document_table['name'] in source.tables else None)
            if row is None:
                raise MigrationImportError('knowledge target holds a document the source does not')
            if (identity.get('chunk_count') != _nonneg_int(row['chunk_count'])):
                raise MigrationImportError('knowledge catalog chunk count differs')
            rows += 1
        following = page.get('cursor')
        if following is None:
            break
        if not page['rows'] or not isinstance(following, str):
            raise MigrationImportError('invalid knowledge catalog cursor')
        cursor = following
    if rows != expected_documents:
        raise MigrationImportError('knowledge source and target catalogs differ')


def _import_knowledge(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    document_table = _knowledge_table('storage_knowledge_documents')
    settings_table = _knowledge_table('storage_knowledge_settings')
    owner_user_id = _session_owner(session)
    progress = _resume_or_empty(checkpoint, 'knowledge')
    settings_document: dict[str, Any] | None = None
    if settings_table['name'] in source.tables:
        for row in _scan(source, settings_table):
            if _int(row['user_id']) != owner_user_id:
                raise MigrationImportError(
                    'knowledge settings belong to owners outside the operator scope')
            settings_document = _knowledge_settings_document(row)
    documents: list[dict[str, Any]] = []
    if document_table['name'] in source.tables:
        for row in _scan(source, document_table):
            if _int(row['user_id']) != owner_user_id:
                raise MigrationImportError(
                    'knowledge documents belong to owners outside the operator scope')
            documents.append(
                _knowledge_document_snapshot(source, owner_user_id, row))
    digest = hashlib.sha256(b'tofu-db.knowledge.snapshot-chain.v1').hexdigest()
    if settings_document is not None:
        digest = _record_chain(digest, {'settings': settings_document})
    for document in documents:
        digest = _record_chain(digest, document)
    rows = len(documents) + (1 if settings_document is not None else 0)
    if progress is not None:
        if (not progress['complete'] or progress['rows'] != rows
                or progress['sha256'] != digest):
            raise MigrationImportError('resumed knowledge target no longer matches progress')
        _verify_knowledge_catalog(source, session, expected_documents=len(documents))
        if settings_document is not None:
            _verify_knowledge_settings(session, settings_document)
        for document in documents:
            _verify_knowledge_document(session, document)
        return
    if settings_document is not None:
        _command(session, 'migration.knowledge.settings.restore', settings_document)
        _verify_knowledge_settings(session, settings_document)
    for document in documents:
        _command(session, 'migration.knowledge.document.restore',
                 {'document': document, 'settings': settings_document})
        _verify_knowledge_document(session, document)
    _verify_knowledge_catalog(source, session, expected_documents=len(documents))
    _publish(checkpoint, 'knowledge', cursor=[], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)

# ---------------------------------------------------------------------------
# ARTIFACTS/MISC domain group: chat/tool-result blobs, browser observations,
# model routing, plugin store, project brain, optimizer, log aggregates,
# billing payments and redeem codes. Each importer replays exact source
# snapshots through its native migration adapter, re-verifies row-by-row,
# then publishes checkpoint progress only after the independent target
# catalog matches the hashed source prefix.
# ---------------------------------------------------------------------------


def _import_document_domain(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, domain: str, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
    table: dict[str, Any],
    build_document: Callable[[dict[str, Any]], dict[str, Any]],
    restore_operation: str,
    get_operation: str,
    get_identity: Callable[[dict[str, Any]], dict[str, Any]],
    scan_operation: str,
    cursor_label: str,
    identity_of_row: Callable[[dict[str, Any]], dict[str, Any]],
    point_key: Callable[[dict[str, Any]], tuple[Any, ...]],
    chain_seed: str,
    precheck: Callable[[dict[str, Any]], None] | None = None,
    restore_payload: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    scan_payload_extra: dict[str, Any] | None = None,
    restore_document: Callable[[dict[str, Any]], None] | None = None,
    verify_document: Callable[[dict[str, Any]], None] | None = None,
    verify_catalog: Callable[[], None] | None = None,
    skip_catalog_check: bool = False,
    catalog_extra_keys: Callable[[], frozenset[tuple[Any, ...]]] | None = None,
    restore_batch: Callable[[list[tuple[dict[str, Any], dict[str, Any]]]], None] | None = None,
    restore_batchable: Callable[[dict[str, Any]], bool] | None = None,
    restore_batch_max_rows: int = 128,
) -> None:
    """Shared restore/verify/publish loop for exact-snapshot domains.

    When ``restore_batch`` is supplied, batchable rows are committed through
    it in bounded runs (one transaction per run) and every row is still
    verified and accounted individually after each run commits, so resume,
    digest chains and checkpoint publication keep their per-row semantics.
    """
    # Operator cutover knob (default OFF): skip the write-time per-row verify
    # get. Every restored row is still byte-exact verified at domain
    # completion (independent catalog enumeration) and at every resume
    # (prefix re-verification), so the end-to-end certification guarantee is
    # unchanged; only the redundant write-time re-read is elided.
    skip_row_verify = os.environ.get('TOFU_MIGRATION_SKIP_ROW_VERIFY', '') == '1'
    progress = _resume_or_empty(checkpoint, domain)

    def verify(document: dict[str, Any]) -> None:
        if verify_document is not None:
            verify_document(document)
            return
        observed = _query(session, get_operation, get_identity(document))
        if observed != document:
            raise MigrationImportError(f'{domain} source and target snapshots differ')

    def scan_source(*, until, complete):
        digest = hashlib.sha256(chain_seed.encode()).hexdigest()
        rows = 0
        if table['name'] in source.tables:
            for row in _scan(source, table, until=until, complete=complete):
                if precheck is not None:
                    precheck(row)
                document = build_document(row)
                verify(document)
                digest = _record_chain(digest, document)
                rows += 1
        return rows, digest

    after = None
    if progress is not None:
        rows, digest = scan_source(
            until=None if progress['complete'] else progress['cursor'],
            complete=progress['complete'])
        if rows != progress['rows'] or digest != progress['sha256']:
            raise MigrationImportError(
                f'resumed {domain} target no longer matches progress')
        after = progress['cursor']
    completed = bool(progress and progress['complete'])
    last: list[Any] | None = None if after is None else list(after)
    pending = 0
    rows = 0 if progress is None else progress['rows']
    digest = (hashlib.sha256(chain_seed.encode()).hexdigest()
              if progress is None else progress['sha256'])
    if table['name'] in source.tables and not completed:
        if table['name'] in _DIGEST_KEY_TABLES:
            digest_after = None
            if after is not None:
                if len(after) != 1:
                    raise MigrationImportError(f'invalid {domain} digest resume cursor')
                digest_after = after[0]
            iterator = _digest_stream(source, table, after=digest_after, batch_rows=batch_rows)
        else:
            iterator = source.rows(table['name'], table['columns'], table['keys'],
                                   after=after, batch_rows=batch_rows)
    else:
        iterator = ()
    def account(row: dict[str, Any], document: dict[str, Any]) -> None:
        nonlocal digest, rows, last, pending
        digest = _record_chain(digest, document)
        rows += 1
        last = list(_pk(table, row))
        pending += 1
        if pending == batch_rows:
            _publish(checkpoint, domain, cursor=last, rows=rows, digest=digest,
                     complete=False, after_verified_prefix=after_verified_prefix)
            pending = 0

    batch: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def flush_batch() -> None:
        if not batch:
            return
        restore_batch(batch)
        for batch_row, batch_document in batch:
            if not skip_row_verify:
                verify(batch_document)
            account(batch_row, batch_document)
        batch.clear()

    for row in iterator:
        if precheck is not None:
            precheck(row)
        document = build_document(row)
        if restore_batch is not None and (
                restore_batchable is None or restore_batchable(document)):
            batch.append((row, document))
            if len(batch) >= restore_batch_max_rows:
                flush_batch()
            continue
        if batch:
            flush_batch()
        if restore_document is not None:
            restore_document(document)
        else:
            _command(session, restore_operation,
                     document if restore_payload is None else restore_payload(document))
        if not skip_row_verify:
            verify(document)
        account(row, document)
    flush_batch()
    # Independent owner-scoped catalog enumeration before completion. Receipt
    # domains share the live command_receipt namespace with the migration
    # session's own exactly-once operational receipts, so a blanket catalog
    # scan cannot prove target\u2286source; per-row point-get already proves
    # source\u2286target, which is the sound direction for idempotency state.
    seen = 0
    extras_seen: set[tuple[Any, ...]] = set()
    extra_keys = (frozenset() if catalog_extra_keys is None
                  else frozenset(catalog_extra_keys()))
    cursor = None
    while not skip_catalog_check:
        page = _query(session, scan_operation,
                      {'cursor': cursor, **(scan_payload_extra or {})})
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']):
            raise MigrationImportError(f'invalid {domain} catalog page')
        for identity in page['rows']:
            key = point_key(identity_of_row(identity))
            source_row = (source.point(table['name'], table['columns'],
                                       table['keys'], key)
                          if table['name'] in source.tables else None)
            if source_row is None:
                if key in extra_keys:
                    # A fold-owned occupant this domain declared ahead of time;
                    # the fold already verified it point-by-point.
                    extras_seen.add(key)
                    continue
                raise MigrationImportError(
                    f'{domain} target holds a row the source does not')
            verify(build_document(source_row))
            seen += 1
        following = page.get('cursor')
        if following is None:
            break
        if (not page['rows'] or not isinstance(following, str)
                or not following.startswith(cursor_label)
                or following == cursor):
            raise MigrationImportError(f'{domain} catalog cursor did not advance')
        cursor = following
    if not skip_catalog_check and (seen != rows or extras_seen != set(extra_keys)):
        raise MigrationImportError(f'{domain} source and target catalogs differ')
    final_rows, final_digest = scan_source(until=None, complete=True)
    if final_rows != rows or final_digest != digest:
        raise MigrationImportError(f'{domain} source and target snapshots differ')
    if verify_catalog is not None:
        verify_catalog()
    _publish(checkpoint, domain, cursor=last or [], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


def _attempt_event_document(row):
    encoded = row['payload_json']
    if isinstance(encoded, bytes):
        if len(encoded) > MAX_SOURCE_ROW_BYTES:
            raise MigrationImportError('attempt event exceeds native document bound')
        try:
            event_json = encoded.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise MigrationImportError('attempt event is not UTF-8') from exc
    else:
        event_json = _text(encoded)
    if len(event_json.encode('utf-8')) > MAX_SOURCE_ROW_BYTES:
        raise MigrationImportError('attempt event exceeds native document bound')
    event = _json_load(event_json)
    document = {key: _text(row[key]) for key in ('attempt_id', 'conversation_id', 'turn_id', 'type')}
    document.update({key: _nonneg_int(row[key]) for key in ('sequence', 'projection_revision', 'created_at')})
    if not document['sequence'] or not document['created_at']:
        raise MigrationImportError('attempt event sequence and timestamp must be positive')
    fields = {'attempt_id': 'attemptId', 'conversation_id': 'conversationId',
              'turn_id': 'turnId', 'type': 'type', 'sequence': 'seq',
              'projection_revision': 'projectionRevision'}
    if not isinstance(event, dict) or any(type(event.get(target)) is not type(document[source]) or event.get(target) != document[source]
                                          for source, target in fields.items()):
        raise MigrationImportError('attempt event envelope identity differs')
    document['event_json'] = event_json
    return document


def _attempt_event_manifest(source, row):
    metadata = {key: _text(row[key]) for key in ('attempt_id', 'conversation_id', 'turn_id', 'type')}
    metadata.update({key: _nonneg_int(row[key]) for key in ('sequence', 'projection_revision', 'created_at')})
    if not metadata['sequence'] or not metadata['created_at']:
        raise MigrationImportError('attempt event sequence and timestamp must be positive')
    metadata['event_json'] = ''
    digest = hashlib.sha256()
    size = 0
    for chunk in _attempt_event_source_chunks(source, metadata):
        digest.update(chunk)
        size += len(chunk)
    return {'snapshot': metadata, 'event_bytes': size, 'sha256': digest.hexdigest()}


def _attempt_event_source_chunks(source, metadata):
    return source.blob_chunks('storage_attempt_events', 'payload_json', ('attempt_id', 'sequence'),
                              (metadata['attempt_id'], metadata['sequence']),
                              maximum_bytes=MAX_ATTEMPT_EVENT_DOCUMENT_BYTES)


def _import_attempt_events(source, session, checkpoint, *, batch_rows, after_verified_prefix):
    def precheck(row):
        table = _plan_table('attempts')
        attempt = (source.point(table['name'], table['columns'], table['keys'], (row['attempt_id'],))
                   if table['name'] in source.tables else None)
        if attempt is None or any(attempt[key] != row[key] for key in ('conversation_id', 'turn_id')):
            raise MigrationImportError('attempt event parent is missing or differs')
        _require_attempt_source_owner(source, session, attempt)

    def _inline_event_json(manifest):
        metadata = manifest['snapshot']
        encoded = b''.join(_attempt_event_source_chunks(source, metadata))
        if len(encoded) != manifest['event_bytes'] or hashlib.sha256(encoded).hexdigest() != manifest['sha256']:
            raise MigrationImportError('event source bytes changed')
        try:
            return metadata, encoded.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise MigrationImportError('event source is not UTF-8') from exc

    def restore(manifest):
        metadata = manifest['snapshot']
        if manifest['event_bytes'] <= MAX_SOURCE_CHUNK_BYTES:
            metadata, event_json = _inline_event_json(manifest)
            _command(session, 'migration.attempt.event.restore', {**metadata, 'event_json': event_json})
            return
        _command(session, 'migration.attempt.event.upload.begin', manifest)
        try:
            offset = 0
            for chunk in _attempt_event_source_chunks(source, metadata):
                _command(session, 'migration.attempt.event.upload.append', {'offset': offset, 'hex': chunk.hex()})
                offset += len(chunk)
            _command(session, 'migration.attempt.event.upload.finish', {})
        except BaseException:
            try:
                _command(session, 'migration.attempt.event.upload.abort', {})
            except Exception as abort_exc:
                # Best-effort abort; the original failure re-raises below.
                logger.debug('attempt event upload abort failed: %s', abort_exc)
            raise

    def verify(manifest):
        metadata = manifest['snapshot']
        observed = _query(session, 'migration.attempt.event.chunk', {
            'attempt_id': metadata['attempt_id'], 'sequence': metadata['sequence'], 'offset': 0})
        if (not isinstance(observed, dict) or observed.get('snapshot') != metadata
                or observed.get('event_bytes') != manifest['event_bytes']
                or observed.get('sha256') != manifest['sha256']):
            raise MigrationImportError('attempt event bytes or metadata differ')

    def restore_batch(manifests):
        rows = []
        for manifest in manifests:
            metadata, event_json = _inline_event_json(manifest)
            rows.append({**metadata, 'event_json': event_json})
        _command(session, 'migration.attempt.event.restore_batch', {'rows': rows})

    table = _plan_table('attempt_events')
    metadata_table = {**table, 'columns': [name for name in table['columns'] if name != 'payload_json']}
    _import_document_domain(
        source, session, checkpoint, 'attempt_events', batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix, table=metadata_table,
        build_document=lambda row: _attempt_event_manifest(source, row),
        restore_operation='migration.attempt.event.restore', get_operation='migration.attempt.event.get',
        get_identity=lambda row: {}, scan_operation='migration.attempt.event.scan', cursor_label='attempt_events:',
        identity_of_row=lambda row: row, point_key=lambda row: (_text(row['attempt_id']), _nonneg_int(row['sequence'])),
        chain_seed='tofu-db.attempt-event-manifest-chain.v2', precheck=precheck,
        restore_document=restore, verify_document=verify,
        restore_batch=lambda docs: restore_batch([document for _row, document in docs]),
        restore_batchable=lambda manifest: manifest['event_bytes'] <= MAX_SOURCE_CHUNK_BYTES,
    )


_COMMAND_KEY_DOMAIN = b'tofu.command-receipt.command.v2\x00'
_LEGACY_RECEIPT_OWNER_ID = 2**64 - 2


def _receipt_response_hex(row: dict[str, Any]) -> str:
    encoded = row['response_json']
    if not isinstance(encoded, (bytes, bytearray, memoryview)):
        raise MigrationImportError('receipt response is not stored bytes')
    encoded = bytes(encoded)
    if len(encoded) > MAX_SOURCE_ROW_BYTES:
        raise MigrationImportError('receipt response exceeds the native document bound')
    return encoded.hex()


def _command_receipt_v2_document(session, row: dict[str, Any]) -> dict[str, Any]:
    command_key = row['command_key']
    if isinstance(command_key, str):
        # Digest-key stream rows already expose the canonical hex identity.
        if (len(command_key) != 64
                or any(character not in '0123456789abcdef' for character in command_key)):
            raise MigrationImportError('command receipt v2 identity is not a 32-byte digest')
        command_key_hex = command_key
    elif (isinstance(command_key, (bytes, bytearray, memoryview))
            and len(bytes(command_key)) == 32):
        command_key_hex = bytes(command_key).hex()
    else:
        raise MigrationImportError('command receipt v2 identity is not a 32-byte digest')
    request_digest = row['request_digest']
    if (not isinstance(request_digest, (bytes, bytearray, memoryview))
            or len(bytes(request_digest)) != 32):
        raise MigrationImportError('command receipt v2 identity is not a 32-byte digest')
    return {
        'tenant_id': session.tenant_id,
        'owner_user_id': session.owner_id,
        'command_key': command_key_hex,
        'operation': _text(row['operation']),
        'request_digest': bytes(request_digest).hex(),
        'response_hex': _receipt_response_hex(row),
        'committed_at_ms': _nonneg_int(row['committed_at_ms']),
    }


def _import_command_receipts_v2(source, session, checkpoint, *, batch_rows, after_verified_prefix):
    _import_document_domain(
        source, session, checkpoint, 'command_receipts_v2', batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix,
        table=_plan_table('command_receipts_v2'),
        build_document=lambda row: _command_receipt_v2_document(session, row),
        restore_operation='migration.command.receipt.restore',
        get_operation='migration.command.receipt.get',
        get_identity=lambda document: {key: document[key] for key in (
            'tenant_id', 'owner_user_id', 'command_key')},
        scan_operation='migration.command.receipt.scan',
        cursor_label='receipts:',
        identity_of_row=lambda row: row,
        point_key=lambda row: (bytes.fromhex(_text(row['command_key'])),),
        chain_seed='tofu-db.command-receipt-v2-chain.v1',
        scan_payload_extra={'tenant_id': session.tenant_id, 'owner_user_id': session.owner_id},
        skip_catalog_check=True,
    )



def _storage_event_document(session, row: dict[str, Any]) -> dict[str, Any]:
    payload = row['event_json']
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise MigrationImportError('storage event payload is not stored bytes')
    payload = bytes(payload)
    if len(payload) > MAX_SOURCE_ROW_BYTES:
        raise MigrationImportError('storage event exceeds the native document bound')
    return {
        'tenant_id': session.tenant_id,
        'owner_user_id': session.owner_id,
        'task_id': _text(row['task_id']),
        'sequence': _nonneg_int(row['sequence']),
        'event_type': _text(row['event_type']),
        'payload_json_hex': payload.hex(),
        'created_at_ms': _nonneg_int(row['created_at_ms']),
    }


def _import_storage_events(source, session, checkpoint, *, batch_rows, after_verified_prefix):
    def verify(document):
        observed = _query(session, 'migration.storage.event.get', {
            key: document[key] for key in ('tenant_id', 'owner_user_id', 'task_id', 'sequence')})
        expected = {key: document[key] for key in
                    ('tenant_id', 'owner_user_id', 'task_id', 'sequence', 'payload_json_hex')}
        if observed != expected:
            raise MigrationImportError('storage_events source and target snapshots differ')

    _import_document_domain(
        source, session, checkpoint, 'storage_events', batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix,
        table=_plan_table('storage_events'),
        build_document=lambda row: _storage_event_document(session, row),
        restore_operation='migration.storage.event.restore',
        get_operation='migration.storage.event.get',
        get_identity=lambda document: {key: document[key] for key in (
            'tenant_id', 'owner_user_id', 'task_id', 'sequence')},
        scan_operation='migration.storage.event.scan',
        cursor_label='storage_events:',
        identity_of_row=lambda row: row,
        point_key=lambda row: (_text(row['task_id']), _nonneg_int(row['sequence'])),
        chain_seed='tofu-db.storage-event-chain.v1',
        scan_payload_extra={'tenant_id': session.tenant_id, 'owner_user_id': session.owner_id},
        verify_document=verify,
        restore_batch=lambda docs: _command(
            session, 'migration.storage.event.restore_batch',
            {'rows': [document for _row, document in docs]}),
        restore_batchable=lambda document: len(document['payload_json_hex']) <= 131072,
    )


def _sync_change_owner(source, session, row) -> None:
    if _nonneg_int(row['user_id']) != session.owner_id:
        raise MigrationImportError('sync change owner is outside the operator scope')


def _conversation_sync_event_document(session, row: dict[str, Any]) -> dict[str, Any]:
    # event_json already stores the full sync envelope; the native restore
    # re-encodes the decoded payload into the same envelope, so verify against
    # the canonical text (same exact-equality pattern as attempt events).
    event = _json_load(row['event_json'])
    if not isinstance(event, dict):
        raise MigrationImportError('conversation sync event payload is not an object')
    occurred = _nonneg_int(row['created_at'])
    document = {
        'tenant_id': session.tenant_id,
        'owner_user_id': session.owner_id,
        'conversation_id': _text(row['conversation_id']),
        'sync_sequence': _nonneg_int(row['sync_sequence']),
        'change_type': _text(row['change_type']),
        'occurred_at_ms': occurred,
        'committed_at_ms': occurred,
        'payload_json': _canonical(event).decode('ascii'),
    }
    for key in ('turn_id', 'attempt_id'):
        value = _text(row[key]) if row[key] is not None else ''
        if value:
            document[key] = value
    return document


def _import_conversation_sync_events(source, session, checkpoint, *, batch_rows, after_verified_prefix):
    _import_document_domain(
        source, session, checkpoint, 'conversation_sync_events', batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix,
        table=_plan_table('conversation_sync_events'),
        build_document=lambda row: _conversation_sync_event_document(session, row),
        restore_operation='migration.conversation.sync.event.restore',
        get_operation='migration.conversation.sync.event.get',
        get_identity=lambda document: {key: document[key] for key in (
            'tenant_id', 'owner_user_id', 'conversation_id', 'sync_sequence')},
        scan_operation='migration.conversation.sync.event.scan',
        cursor_label='conversation_sync_events:',
        identity_of_row=lambda row: row,
        point_key=lambda row: (_text(row['conversation_id']), _nonneg_int(row['owner_user_id']), _nonneg_int(row['sync_sequence'])),
        chain_seed='tofu-db.conversation-sync-event-chain.v1',
        precheck=lambda row: _sync_change_owner(source, session, row),
        scan_payload_extra={'tenant_id': session.tenant_id, 'owner_user_id': session.owner_id},
    )


def _import_conversation_sync_heads(source, session, checkpoint, *, batch_rows, after_verified_prefix):
    def build_document(row):
        return {
            'tenant_id': session.tenant_id,
            'owner_user_id': session.owner_id,
            'conversation_id': _text(row['conversation_id']),
            'sync_sequence': _nonneg_int(row['sync_sequence']),
        }
    _import_document_domain(
        source, session, checkpoint, 'conversation_sync_heads', batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix,
        table=_plan_table('conversation_sync_heads'),
        build_document=build_document,
        restore_operation='migration.conversation.sync.head.restore',
        get_operation='migration.conversation.sync.head.get',
        get_identity=lambda document: {key: document[key] for key in (
            'tenant_id', 'owner_user_id', 'conversation_id')},
        scan_operation='migration.conversation.sync.head.scan',
        cursor_label='conversation_sync_heads:',
        identity_of_row=lambda row: row,
        point_key=lambda row: (_text(row['conversation_id']), _nonneg_int(row['owner_user_id'])),
        chain_seed='tofu-db.conversation-sync-head-chain.v1',
        precheck=lambda row: _sync_change_owner(source, session, row),
        scan_payload_extra={'tenant_id': session.tenant_id, 'owner_user_id': session.owner_id},
    )

def _legacy_command_receipt_document(session, source_uuid: str, row: dict[str, Any]) -> dict[str, Any]:
    command_id = _text(row['command_id'])
    if not command_id or len(command_id.encode('utf-8')) > 200 or '\x00' in command_id:
        raise MigrationImportError('legacy receipt command identity is invalid')
    digest_text = _text(row['request_digest'])
    if (len(digest_text) != 64
            or any(character not in '0123456789abcdef' for character in digest_text)):
        raise MigrationImportError('legacy receipt request digest is not a SHA-256 hex identity')
    command_key = hashlib.sha256(_COMMAND_KEY_DOMAIN + command_id.encode('utf-8')).hexdigest()
    return {
        'tenant_id': session.tenant_id,
        'source_authority_uuid': source_uuid,
        'command_key': command_key,
        'operation': _text(row['operation']),
        'request_digest': digest_text,
        'response_hex': _receipt_response_hex(row),
        'committed_at_ms': _nonneg_int(row['committed_at_ms']),
    }


def _import_command_receipts(source, session, checkpoint, *, batch_rows, after_verified_prefix):
    table = _plan_table('command_receipts')
    first_row = next(iter(source.rows(table['name'], table['columns'], table['keys'],
                                      batch_rows=1)), None) if table['name'] in source.tables else None
    has_rows = first_row is not None
    # The legacy source fence is meaningful only when a receipt actually crosses
    # the boundary; an empty table scans an empty target regardless of the UUID.
    source_uuid = (_source_authority_uuid(source)
                   if has_rows
                   else str(uuid.UUID('00000000-0000-0000-0000-000000000001')))
    _import_document_domain(
        source, session, checkpoint, 'command_receipts', batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix,
        table=_plan_table('command_receipts'),
        build_document=lambda row: _legacy_command_receipt_document(session, source_uuid, row),
        restore_operation='migration.legacy.receipt.restore',
        get_operation='migration.legacy.receipt.get',
        get_identity=lambda document: {key: document[key] for key in (
            'tenant_id', 'source_authority_uuid', 'command_key')},
        scan_operation='migration.legacy.receipt.scan',
        cursor_label=f'legacy-receipts:{source_uuid}:',
        identity_of_row=lambda row: row,
        point_key=lambda row: (_text(row['command_id']),),
        chain_seed='tofu-db.legacy-command-receipt-chain.v1',
        scan_payload_extra={'tenant_id': session.tenant_id,
                            'source_authority_uuid': source_uuid},
        skip_catalog_check=True,
    )


def _attempt_document(row: dict[str, Any]) -> dict[str, Any]:
    document = {name: _text(row[name]) for name in (
        'attempt_id', 'conversation_id', 'turn_id', 'command_id', 'task_id',
        'operation', 'dispatch_mode', 'queue_id', 'queue_state', 'status',
    )}
    if document['status'] in {'pending', 'running'}:
        raise MigrationImportError('live attempts must settle before migration')
    for name in ('base_projection_revision', 'created_at', 'settled_at'):
        document[name] = _nonneg_int(row[name])
    for name in ('started_at', 'superseded_at'):
        document[name] = None if row[name] is None else _nonneg_int(row[name])
    for name in ('resume_anchor', 'config', 'error', 'timing_trace'):
        value = _json_load(row[name + '_json'])
        if name != 'error' and not isinstance(value, dict):
            raise MigrationImportError('attempt snapshot JSON field is not an object')
        document[name] = value
    return document


def _require_attempt_source_owner(source, session, row):
    evidence = []
    conversation = row['conversation_id']
    if 'storage_conversations' in source.tables:
        found = source.point('storage_conversations', ['user_id'], ['id', 'user_id'],
                             (conversation, session.owner_id))
        if found is not None:
            evidence.append(found['user_id'])
    if 'storage_conversation_turns' in source.tables:
        found = source.point('storage_conversation_turns', ['user_id', 'conversation_id'],
                             ['turn_id'], (row['turn_id'],))
        if found is not None:
            if found['conversation_id'] != conversation:
                raise MigrationImportError('attempt source Turn conversation differs')
            evidence.append(found['user_id'])
    if 'storage_conversation_trash' in source.tables:
        found = source.point('storage_conversation_trash', ['user_id'],
                             ['conversation_id', 'user_id'], (conversation, session.owner_id))
        if found is not None:
            evidence.append(found['user_id'])
    if 'storage_turn_tombstones' in source.tables:
        found = source.point('storage_turn_tombstones', ['user_id'],
                             ['conversation_id', 'turn_id'], (conversation, row['turn_id']))
        if found is not None:
            evidence.append(found['user_id'])
    if not evidence or any(_int(owner) != session.owner_id for owner in evidence):
        raise MigrationImportError('attempt source owner is outside or missing from the operator scope')


def _import_attempts(source, session, checkpoint, *, batch_rows, after_verified_prefix):
    _import_document_domain(
        source, session, checkpoint, 'attempts', batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix,
        table=_plan_table('attempts'), build_document=_attempt_document,
        restore_operation='migration.attempt.restore', get_operation='migration.attempt.get',
        get_identity=lambda document: {key: document[key] for key in ('conversation_id', 'attempt_id')},
        scan_operation='migration.attempt.scan', cursor_label='attempts:',
        identity_of_row=lambda row: row, point_key=lambda row: (_text(row['attempt_id']),),
        chain_seed='tofu-db.attempt-chain.v1',
        precheck=lambda row: _require_attempt_source_owner(source, session, row),
    )


def _compaction_archive_document(row: dict[str, Any]) -> dict[str, Any]:
    from lib.storage_authority_sql.archived_message_codec import (
        decode_archived_message_sequence_from_storage,
    )
    from lib.storage_authority_sql.projection_codec import ProjectionCodecError

    if row['archive_scope']:
        raise MigrationImportError('child archive migration requires its invocation adapter')
    def decode_sequence(encoded):
        stored = _json_load(encoded)
        if not isinstance(stored, list):
            raise MigrationImportError('archive messages are not an array')
        result = []
        size = 2
        for message in stored:
            decoded = decode_archived_message_sequence_from_storage([message])[0]
            size += len(json.dumps(decoded, ensure_ascii=False, allow_nan=False,
                                   separators=(',', ':')).encode('utf-8')) + bool(result)
            if size > MAX_MIGRATION_COMPACTION_DECODED_BYTES:
                raise MigrationImportError('decoded archive transcript exceeds migration bound')
            result.append(decoded)
        return result

    try:
        messages = decode_sequence(row['messages_json'])
        post = (decode_sequence(row['post_messages_json'])
                if row['post_messages_json'] is not None else None)
    except ProjectionCodecError as exc:
        raise MigrationImportError('archive stored message codec is invalid') from exc
    receipt = _json_load(row['receipt_json'])
    if not isinstance(receipt, dict):
        raise MigrationImportError('archive receipt is not an object')
    document = {field: _text(row[field]) for field in (
        'archive_id', 'archive_scope', 'conversation_id', 'summary', 'trigger',
        'task_id', 'model', 'reason',
    )}
    document.update({field: _nonneg_int(row[field]) for field in (
        'round_num', 'tokens_before', 'tokens_after', 'msgs_before', 'msgs_after',
        'payload_size', 'created_at_ms',
    )})
    document.update(messages=messages, post_messages=post, receipt=receipt)
    return document


def _replay_frozen_transcript(source, session, document, *, trashed=False):
    from lib.storage_authority_sql.migration.tofudb_migration_transcript import frozen_pages
    identity = document['source']['conversation_id']
    _command(session, 'migration.frozen.transcript.begin', document['source'])
    for index, (raw, descriptor) in enumerate(frozen_pages(source, identity, session.owner_id, trashed=trashed)):
        if index >= len(document['pages']) or descriptor != document['pages'][index]:
            raise MigrationImportError('frozen transcript source page changed')
        if len(raw) <= MAX_SOURCE_CHUNK_BYTES:
            _command(session, 'migration.frozen.transcript.restore', {
                'conversation_id': identity, 'page_index': index,
                'start': descriptor['start'], 'page_json': raw.decode('utf-8')})
        else:
            _command(session, 'migration.frozen.transcript.upload.begin', {
                'conversation_id': identity, 'page_index': index, 'start': descriptor['start'],
                'bytes': len(raw), 'sha256': descriptor['sha256']})
            for offset in range(0, len(raw), MAX_SOURCE_CHUNK_BYTES):
                _command(session, 'migration.frozen.transcript.upload.append', {
                    'offset': offset, 'hex': raw[offset:offset+MAX_SOURCE_CHUNK_BYTES].hex()})
            _command(session, 'migration.frozen.transcript.upload.finish', {})
    _command(session, 'migration.frozen.transcript.finish', {
        'conversation_id': identity, 'page_count': len(document['pages']),
        'message_count': document['messages'], 'decoded_bytes': document['decoded_bytes']})


def _import_frozen_transcripts(source, session, checkpoint, *, batch_rows, after_verified_prefix):
    from lib.storage_authority_sql.migration.tofudb_migration_transcript import frozen_snapshot

    def owner(row):
        if _int(row['user_id']) != session.owner_id:
            raise MigrationImportError('frozen transcript owner is outside the operator scope')

    def restore(document):
        _replay_frozen_transcript(source, session, document)

    def finish(document):
        _command(session, 'migration.frozen.transcript.finish', {
            'conversation_id': document['source']['conversation_id'],
            'page_count': len(document['pages']), 'message_count': document['messages'],
            'decoded_bytes': document['decoded_bytes']})

    def verify(document):
        identity = document['source']['conversation_id']
        observed = _query(session, 'migration.frozen.transcript.get', {'conversation_id': identity})
        if observed != document:
            raise MigrationImportError('frozen transcript source and target manifests differ')
        # finish independently enumerates physical pages even for completed imports.
        finish(document)
        for index, descriptor in enumerate(document['pages']):
            chunk = _query(session, 'migration.frozen.transcript.chunk', {
                'conversation_id': identity, 'page_index': index, 'offset': 0})
            if not isinstance(chunk, dict) or chunk.get('page') != descriptor:
                raise MigrationImportError('frozen transcript native page bytes differ')

    table = _plan_table('frozen_transcripts')
    metadata_table = {**table, 'columns': [c for c in table['columns'] if c != 'messages_json']}
    _import_document_domain(
        source, session, checkpoint, 'frozen_transcripts', batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix, table=metadata_table,
        build_document=lambda row: frozen_snapshot(source, row),
        restore_operation='', get_operation='', get_identity=lambda row: {},
        scan_operation='migration.frozen.transcript.scan', cursor_label='frozen_transcripts:',
        identity_of_row=lambda row: row,
        point_key=lambda row: (_text(row['conversation_id']), session.owner_id),
        chain_seed='tofu-db.frozen-transcript-chain.v1', precheck=owner,
        restore_document=restore, verify_document=verify,
    )


def _raw_archive_document(row):
    document = {key: _text(row[key]) for key in (
        'archive_id', 'conversation_id', 'turn_id', 'attempt_id', 'task_id',
        'request_sha256', 'response_sha256', 'integrity', 'truncation_reason')}
    document.update({key: _nonneg_int(row[key]) for key in (
        'round_num', 'transport_attempt', 'request_bytes', 'response_bytes',
        'stored_bytes', 'created_at_ms')})
    size = 0
    for part in ('request', 'response'):
        raw = row[part + '_blob']
        if not isinstance(raw, bytes):
            raise MigrationImportError('raw archive compressed part is not bytes')
        size += len(raw)
        document[part + '_blob_b64'] = base64.b64encode(raw).decode('ascii')
    if document['stored_bytes'] != size:
        raise MigrationImportError('raw archive stored byte count differs')
    document['summary'] = _json_load(row['summary_json'])
    if not isinstance(document['summary'], dict):
        raise MigrationImportError('raw archive summary is not an object')
    return document


def _import_raw_archives(source, session, checkpoint, *, batch_rows, after_verified_prefix):
    def owner(row):
        if _int(row['user_id']) != session.owner_id:
            raise MigrationImportError('raw archive owner is outside the operator scope')

    def verify_usage():
        total, = source.raw_archive_usage(session.owner_id)
        for scope, expected in [
            ({'scope': 'owner'}, {'count': total['count']}),
            ({'scope': 'tenant'}, {'stored_bytes': total['stored_bytes']}),
        ]:
            if _query(session, 'migration.raw.archive.stats', scope) != expected:
                raise MigrationImportError('raw archive total counters differ')
        for group in ('conversation_id', 'attempt_id'):
            for row in source.raw_archive_usage(session.owner_id, group):
                scope = {'scope': 'conversation' if group == 'conversation_id' else 'attempt',
                         group: row[group]}
                expected = {'stored_bytes': row['stored_bytes']}
                if group == 'conversation_id':
                    expected['count'] = row['count']
                if _query(session, 'migration.raw.archive.stats', scope) != expected:
                    raise MigrationImportError('raw archive grouped counters differ')

    _import_document_domain(
        source, session, checkpoint, 'raw_archives', batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix, table=_plan_table('raw_archives'),
        build_document=_raw_archive_document, restore_operation='migration.raw.archive.restore',
        get_operation='migration.raw.archive.get',
        get_identity=lambda document: {'archive_id': document['archive_id']},
        scan_operation='migration.raw.archive.scan', cursor_label='raw_archives:',
        identity_of_row=lambda row: row, point_key=lambda row: (_text(row['archive_id']),),
        chain_seed='tofu-db.raw-archive-chain.v1', precheck=owner, verify_catalog=verify_usage,
    )


def _import_compaction_archives(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    def owner(row):
        if _int(row['user_id']) != session.owner_id:
            raise MigrationImportError('archive owner is outside the operator scope')

    _import_document_domain(
        source, session, checkpoint, 'compaction_archives', batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix,
        table=_plan_table('compaction_archives'), build_document=_compaction_archive_document,
        restore_operation='migration.compaction.archive.restore',
        get_operation='migration.compaction.archive.get',
        get_identity=lambda document: {'archive_id': document['archive_id']},
        scan_operation='migration.compaction.archive.scan', cursor_label='compaction_archives:',
        identity_of_row=lambda row: row, point_key=lambda row: (_text(row['archive_id']),),
        chain_seed='tofu-db.compaction-archive-chain.v1', precheck=owner,
    )


def _chat_artifact_document(row: dict[str, Any]) -> dict[str, Any]:
    source_ref = _json_load(row['source_ref'])
    meta = _json_load(row['meta'])
    if not isinstance(source_ref, dict) or not isinstance(meta, dict):
        raise MigrationImportError('chat artifact JSON fields are invalid')
    content = _text(row['content'])
    content_sha256 = _text(row['content_sha256'])
    if hashlib.sha256(content.encode('utf-8')).hexdigest() != content_sha256:
        raise MigrationImportError('chat artifact content digest differs')
    size_bytes = _nonneg_int(row['size_bytes'])
    if size_bytes != len(content.encode('utf-8')):
        raise MigrationImportError('chat artifact size differs from its content')
    version = _int(row['version'])
    if version <= 0:
        raise MigrationImportError('chat artifact version is invalid')
    parent_id = _text(row['parent_id'] or '')
    if (version == 1) != (not parent_id):
        raise MigrationImportError('chat artifact version chain is invalid')
    return {
        'id': _text(row['id']),
        'conv_id': _text(row['conv_id']),
        'task_id': _text(row['task_id'] or ''),
        'msg_id': _text(row['msg_id'] or ''),
        'source': _text(row['source']),
        'source_ref': source_ref,
        'format': _text(row['format']),
        'title': _text(row['title'] or ''),
        'content': content,
        'content_sha256': content_sha256,
        'size_bytes': size_bytes,
        'version': version,
        'parent_id': parent_id,
        'pinned': bool(row['pinned']),
        'meta': meta,
        'created_at': _nonneg_int(row['created_at']),
        'deleted_at': _nonneg_int(row['deleted_at'] or 0),
    }


def _import_chat_artifacts(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    table = _plan_table('chat_artifacts')
    _import_document_domain(
        source, session, checkpoint, 'chat_artifacts', batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix,
        table=table,
        build_document=_chat_artifact_document,
        restore_operation='migration.chat.artifact.restore',
        get_operation='migration.chat.artifact.get',
        get_identity=lambda document: {'id': document['id']},
        scan_operation='migration.chat.artifact.scan',
        cursor_label='chat_artifacts:',
        identity_of_row=lambda identity: identity,
        point_key=lambda identity: (_text(identity['id']),),
        chain_seed='tofu-db.chat-artifact-snapshot-chain.v1',
    )


def _tool_result_document(row: dict[str, Any]) -> dict[str, Any]:
    content = _text(row['content'])
    content_sha256 = _text(row['content_sha256'])
    if hashlib.sha256(content.encode('utf-8')).hexdigest() != content_sha256:
        raise MigrationImportError('tool result content digest differs')
    size_bytes = _nonneg_int(row['size_bytes'])
    if size_bytes != len(content.encode('utf-8')):
        raise MigrationImportError('tool result size differs from its content')
    return {
        'content_sha256': content_sha256,
        'content': content,
        'media_type': _text(row['media_type'] or 'text/plain'),
        'size_bytes': size_bytes,
        'created_at_ms': _nonneg_int(row['created_at_ms']),
        'expires_at_ms': _nonneg_int(row['expires_at_ms']),
        'last_accessed_at_ms': _nonneg_int(row['last_accessed_at_ms']),
    }


def _import_tool_result_artifacts(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    table = _plan_table('tool_result_artifacts')
    owner_user_id = _session_owner(session)

    def precheck(row: dict[str, Any]) -> None:
        if _int(row['user_id']) != owner_user_id:
            raise MigrationImportError(
                'tool result snapshots belong to owners outside the operator scope')

    _import_document_domain(
        source, session, checkpoint, 'tool_result_artifacts',
        batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix,
        table=table,
        build_document=_tool_result_document,
        restore_operation='migration.tool.result.restore',
        get_operation='migration.tool.result.get',
        get_identity=lambda document: {'content_sha256': document['content_sha256']},
        scan_operation='migration.tool.result.scan',
        cursor_label='tool_results:',
        identity_of_row=lambda identity: identity,
        point_key=lambda identity: (owner_user_id, _text(identity['content_sha256'])),
        chain_seed='tofu-db.tool-result-snapshot-chain.v1',
        precheck=precheck,
    )


def _browser_observation_document(row: dict[str, Any]) -> dict[str, Any]:
    hints = _json_load(row['hints_json'])
    if hints is None:
        hints = []
    if not isinstance(hints, list):
        raise MigrationImportError('browser observation hints are invalid')
    observation = {
        'schema_version': _int(row['schema_version']),
        'origin': _text(row['origin']),
        'route_family': _text(row['route_family']),
        'operation': _text(row['operation']),
        'strategy': _text(row['strategy']),
        'api_hints': hints,
        'anti_bot_vendor': _text(row['anti_bot_vendor'] or ''),
        'auth_signal': _text(row['auth_signal'] or 'none'),
        'status': _text(row['status']),
        'confidence_milli': _int(row['confidence_milli']),
        'visit_count': _nonneg_int(row['visit_count']),
        'successful_visits': _nonneg_int(row['successful_visits']),
        'hinted_visits': _nonneg_int(row['hinted_visits']),
        'hint_match_visits': _nonneg_int(row['hint_match_visits']),
        'consecutive_failures': _nonneg_int(row['consecutive_failures']),
        'last_outcome': _text(row['last_outcome']),
        'last_elapsed_ms': _nonneg_int(row['last_elapsed_ms']),
        'last_verified_at_ms': _nonneg_int(row['last_verified_at_ms']),
        'last_observed_at_ms': _nonneg_int(row['last_observed_at_ms']),
        'expires_at_ms': _nonneg_int(row['expires_at_ms']),
        'payload_bytes': _nonneg_int(row['payload_bytes']),
    }
    return observation


def _import_browser_observations(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    table = _plan_table('browser_observations', 'storage_browser_site_observations')
    owners_table = _plan_table(
        'browser_observations', 'storage_browser_site_observation_owners')
    owner_user_id = _session_owner(session)
    touched_at_ms = 0
    if owners_table['name'] in source.tables:
        owner_row = source.point(owners_table['name'], owners_table['columns'],
                                 owners_table['keys'], (owner_user_id,))
        if owner_row is not None:
            touched_at_ms = _nonneg_int(owner_row['touched_at_ms'])
    domain = 'browser_observations'
    progress = _resume_or_empty(checkpoint, domain)

    def build(row: dict[str, Any]) -> dict[str, Any]:
        return {
            'origin': _text(row['origin']),
            'route_family': _text(row['route_family']),
            'operation': _text(row['operation']),
            'observation': _browser_observation_document(row),
            'version': 1,
            'updated_at_ms': _nonneg_int(row['last_observed_at_ms']),
            'owner_touched_at_ms': touched_at_ms,
        }

    def verify(document: dict[str, Any]) -> None:
        observed = _query(session, 'migration.browser.observation.get', {
            'origin': document['origin'],
            'route_family': document['route_family'],
            'operation': document['operation'],
        })
        # The native get returns the stored snapshot only; the owner touch
        # mark lives in a separate owner-scoped record and is verified with
        # the terminal catalog page below.
        expected = {key: document[key] for key in (
            'origin', 'route_family', 'operation', 'observation', 'version',
            'updated_at_ms')}
        if observed != expected:
            raise MigrationImportError(
                'browser observation source and target snapshots differ')

    def scan_source(*, until, complete):
        digest = hashlib.sha256(b'tofu-db.browser-observation-chain.v1').hexdigest()
        rows = 0
        if table['name'] in source.tables:
            for row in _scan(source, table, until=until, complete=complete):
                if _int(row['owner_user_id']) != owner_user_id:
                    raise MigrationImportError(
                        'browser observations belong to owners outside the operator scope')
                document = build(row)
                verify(document)
                digest = _record_chain(digest, document)
                rows += 1
        return rows, digest

    after = None
    if progress is not None:
        rows, digest = scan_source(
            until=None if progress['complete'] else progress['cursor'],
            complete=progress['complete'])
        if rows != progress['rows'] or digest != progress['sha256']:
            raise MigrationImportError(
                'resumed browser observation target no longer matches progress')
        if progress['complete']:
            return
        after = progress['cursor']
    last: list[Any] | None = None if after is None else list(after)
    pending = 0
    rows = 0 if progress is None else progress['rows']
    digest = (hashlib.sha256(b'tofu-db.browser-observation-chain.v1').hexdigest()
              if progress is None else progress['sha256'])
    iterator = (source.rows(table['name'], table['columns'], table['keys'],
                            after=after, batch_rows=batch_rows)
                if table['name'] in source.tables else ())
    for row in iterator:
        if _int(row['owner_user_id']) != owner_user_id:
            raise MigrationImportError(
                'browser observations belong to owners outside the operator scope')
        document = build(row)
        _command(session, 'migration.browser.observation.restore', document)
        verify(document)
        digest = _record_chain(digest, document)
        rows += 1
        last = list(_pk(table, row))
        pending += 1
        if pending == batch_rows:
            _publish(checkpoint, domain, cursor=last, rows=rows, digest=digest,
                     complete=False, after_verified_prefix=after_verified_prefix)
            pending = 0
    seen = 0
    cursor = None
    observed_touch = 0
    while True:
        page = _query(session, 'migration.browser.observation.scan', {'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']):
            raise MigrationImportError('invalid browser observation catalog page')
        for identity in page['rows']:
            key = (owner_user_id, _text(identity['origin']),
                   _text(identity['route_family']), _text(identity['operation']))
            source_row = (source.point(table['name'], table['columns'],
                                       table['keys'], key)
                          if table['name'] in source.tables else None)
            if source_row is None:
                raise MigrationImportError(
                    'browser observation target holds a row the source does not')
            verify(build(source_row))
            seen += 1
        following = page.get('cursor')
        if following is None:
            observed_touch = _nonneg_int(page.get('owner_touched_at_ms') or 0)
            break
        if (not page['rows'] or not isinstance(following, str)
                or not following.startswith('browser_observations:')
                or following == cursor):
            raise MigrationImportError(
                'browser observation catalog cursor did not advance')
        cursor = following
    if seen != rows or observed_touch != touched_at_ms:
        raise MigrationImportError(
            'browser observation source and target catalogs differ')
    final_rows, final_digest = scan_source(until=None, complete=True)
    if final_rows != rows or final_digest != digest:
        raise MigrationImportError(
            'browser observation source and target snapshots differ')
    _publish(checkpoint, domain, cursor=last or [], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


def _model_routing_authority_document(row: dict[str, Any]) -> dict[str, Any]:
    document = _json_load(row['document_json'])
    backup = _json_load(row['backup_json'])
    receipt = _json_load(row['migration_receipt_json'])
    if not isinstance(document, dict):
        raise MigrationImportError('model routing authority document is invalid')
    if backup is not None and not isinstance(backup, dict):
        raise MigrationImportError('model routing backup document is invalid')
    if receipt is not None and not isinstance(receipt, dict):
        raise MigrationImportError('model routing migration receipt is invalid')
    updated_at = _optional_float(row['updated_at'])
    if updated_at is None or updated_at <= 0:
        raise MigrationImportError('model routing authority timestamp is invalid')
    return {
        'tenant_id': _text(row['tenant_id'] or ''),
        'revision': _nonneg_int(row['revision']),
        'document': document,
        'backup': backup,
        'migration_receipt': receipt,
        'updated_at': updated_at,
    }


def _model_routing_secret_document(row: dict[str, Any]) -> dict[str, Any]:
    # Ciphertext is opaque preserved bytes; it is never logged or transformed.
    created_at = _optional_float(row['created_at'])
    updated_at = _optional_float(row['updated_at'])
    if created_at is None or created_at <= 0 or updated_at is None or updated_at <= 0:
        raise MigrationImportError('model routing secret timestamps are invalid')
    return {
        'tenant_id': _text(row['tenant_id'] or ''),
        'secret_reference': _text(row['secret_reference']),
        'ciphertext': _text(row['ciphertext']),
        'key_hint': _text(row['key_hint'] or ''),
        'created_at': created_at,
        'updated_at': updated_at,
    }


def _import_model_routing(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    owner_user_id = _session_owner(session)

    def owner_check(row: dict[str, Any]) -> None:
        if _int(row['owner_user_id']) != owner_user_id:
            raise MigrationImportError(
                'model routing snapshots belong to owners outside the operator scope')

    authority_table = _plan_table('model_routing', 'storage_model_routing_authorities')
    _import_document_domain(
        source, session, checkpoint, 'model_routing.authorities',
        batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix,
        table=authority_table,
        build_document=_model_routing_authority_document,
        restore_operation='migration.model.routing.authority.restore',
        get_operation='migration.model.routing.authority.get',
        get_identity=lambda document: {'tenant_id': document['tenant_id']},
        scan_operation='migration.model.routing.authority.scan',
        cursor_label='model_routing:',
        identity_of_row=lambda identity: identity,
        point_key=lambda identity: (owner_user_id, _text(identity['tenant_id'])),
        chain_seed='tofu-db.model-routing-authority-chain.v1',
        precheck=owner_check,
    )
    secret_table = _plan_table('model_routing', 'storage_model_routing_secrets')
    _import_document_domain(
        source, session, checkpoint, 'model_routing.secrets',
        batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix,
        table=secret_table,
        build_document=_model_routing_secret_document,
        restore_operation='migration.model.routing.secret.restore',
        get_operation='migration.model.routing.secret.get',
        get_identity=lambda document: {
            'tenant_id': document['tenant_id'],
            'secret_reference': document['secret_reference'],
        },
        scan_operation='migration.model.routing.secret.scan',
        cursor_label='model_routing:',
        identity_of_row=lambda identity: identity,
        point_key=lambda identity: (
            owner_user_id, _text(identity['tenant_id']),
            _text(identity['secret_reference'])),
        chain_seed='tofu-db.model-routing-secret-chain.v1',
        precheck=owner_check,
    )
    rows, digest = 0, hashlib.sha256(b'tofu-db.model-routing.v1').hexdigest()
    for sub in ('authorities', 'secrets'):
        progress = _resume_or_empty(checkpoint, 'model_routing.' + sub)
        if progress is None or not progress['complete']:
            raise MigrationImportError('model routing sub-domain is not complete')
        rows += progress['rows']
        digest = _record_chain(digest, {'sub': sub, 'sha256': progress['sha256']})
    progress = _resume_or_empty(checkpoint, 'model_routing')
    if progress and (not progress['complete'] or progress['rows'] != rows
                     or progress['sha256'] != digest):
        raise MigrationImportError('resumed model routing verification differs')
    _publish(checkpoint, 'model_routing', cursor=[], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


def _plugin_manifest_document(row: dict[str, Any]) -> dict[str, Any]:
    manifest = row['manifest_json']
    if isinstance(manifest, (bytes, bytearray, memoryview)):
        manifest = bytes(manifest).decode('utf-8')
    manifest = _json_load(manifest)
    if not isinstance(manifest, dict):
        raise MigrationImportError('plugin manifest is invalid')
    version = _int(row['manifest_version'])
    if version <= 0:
        raise MigrationImportError('plugin manifest version is invalid')
    if manifest.get('namespace') != _text(row['namespace']):
        raise MigrationImportError('plugin manifest identity differs')
    return {
        'namespace': _text(row['namespace']),
        'manifest_version': version,
        'manifest_json': manifest,
        'updated_at_ms': _nonneg_int(row['updated_at_ms']),
    }


def _plugin_row_document(row: dict[str, Any]) -> dict[str, Any]:
    raw = row['document_json']
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw = bytes(raw).decode('utf-8')
    raw = _text(raw)
    parsed = _json_load(raw)
    if not isinstance(parsed, dict):
        raise MigrationImportError('plugin row document is not an object')
    version = _int(row['version'])
    if version <= 0:
        raise MigrationImportError('plugin row version is invalid')
    return {
        'namespace': _text(row['namespace']),
        'table_name': _text(row['table_name']),
        'row_key': _text(row['row_key']),
        'document_json': raw,
        'version': version,
        'updated_at_ms': _nonneg_int(row['updated_at_ms']),
    }


def _plugin_unique_document(row: dict[str, Any]) -> dict[str, Any]:
    return {
        'namespace': _text(row['namespace']),
        'table_name': _text(row['table_name']),
        'index_name': _text(row['index_name']),
        'index_value': _text(row['index_value']),
        'row_key': _text(row['row_key']),
    }


def _import_plugin_store(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    manifest_table = _plan_table('plugin_store', 'storage_plugin_manifests')
    _import_document_domain(
        source, session, checkpoint, 'plugin_store.manifests',
        batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix,
        table=manifest_table,
        build_document=_plugin_manifest_document,
        restore_operation='migration.plugin.manifest.restore',
        get_operation='migration.plugin.manifest.get',
        get_identity=lambda document: {'namespace': document['namespace']},
        scan_operation='migration.plugin.manifest.scan',
        cursor_label='plugin_manifests:',
        identity_of_row=lambda identity: identity,
        point_key=lambda identity: (_text(identity['namespace']),),
        chain_seed='tofu-db.plugin-manifest-chain.v1',
    )
    rows_table = _plan_table('plugin_store', 'storage_plugin_rows')
    _import_document_domain(
        source, session, checkpoint, 'plugin_store.rows',
        batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix,
        table=rows_table,
        build_document=_plugin_row_document,
        restore_operation='migration.plugin.rows.restore',
        get_operation='migration.plugin.rows.get',
        get_identity=lambda document: {
            'namespace': document['namespace'],
            'table_name': document['table_name'],
            'row_key': document['row_key'],
        },
        scan_operation='migration.plugin.rows.scan',
        cursor_label='plugin_rows:',
        identity_of_row=lambda identity: identity,
        point_key=lambda identity: (
            _text(identity['namespace']), _text(identity['table_name']),
            _text(identity['row_key'])),
        chain_seed='tofu-db.plugin-rows-chain.v1',
    )
    unique_table = _plan_table('plugin_store', 'storage_plugin_unique_values')
    _import_document_domain(
        source, session, checkpoint, 'plugin_store.unique_values',
        batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix,
        table=unique_table,
        build_document=_plugin_unique_document,
        restore_operation='migration.plugin.unique.restore',
        get_operation='migration.plugin.unique.get',
        get_identity=lambda document: {
            'namespace': document['namespace'],
            'table_name': document['table_name'],
            'index_name': document['index_name'],
            'index_value': document['index_value'],
        },
        scan_operation='migration.plugin.unique.scan',
        cursor_label='plugin_unique:',
        identity_of_row=lambda identity: identity,
        point_key=lambda identity: (
            _text(identity['namespace']), _text(identity['table_name']),
            _text(identity['index_name']), _text(identity['index_value'])),
        chain_seed='tofu-db.plugin-unique-chain.v1',
    )
    rows, digest = 0, hashlib.sha256(b'tofu-db.plugin-store.v1').hexdigest()
    for sub in ('manifests', 'rows', 'unique_values'):
        progress = _resume_or_empty(checkpoint, 'plugin_store.' + sub)
        if progress is None or not progress['complete']:
            raise MigrationImportError('plugin store sub-domain is not complete')
        rows += progress['rows']
        digest = _record_chain(digest, {'sub': sub, 'sha256': progress['sha256']})
    progress = _resume_or_empty(checkpoint, 'plugin_store')
    if progress and (not progress['complete'] or progress['rows'] != rows
                     or progress['sha256'] != digest):
        raise MigrationImportError('resumed plugin store verification differs')
    _publish(checkpoint, 'plugin_store', cursor=[], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


def _project_brain_document(row: dict[str, Any]) -> dict[str, Any]:
    projection_raw = _text(row['projection_json'])
    projection = _json_load(projection_raw)
    if not isinstance(projection, dict):
        raise MigrationImportError('project brain projection is invalid')
    head_sequence = _nonneg_int(row['head_sequence'])
    checkpoint_sequence = _nonneg_int(row['checkpoint_sequence'] or 0)
    if (projection.get('headSequence') != head_sequence
            or projection.get('checkpointSequence') != checkpoint_sequence
            or projection.get('projectKey') != _text(row['project_key'])):
        raise MigrationImportError(
            'project brain projection disagrees with its source row')
    canonical = json.dumps(projection, sort_keys=True, separators=(',', ':'))
    return {
        'project_key': _text(row['project_key']),
        'head_sequence': head_sequence,
        'checkpoint_sequence': checkpoint_sequence,
        'projection_json': canonical,
        'updated_at_ms': _nonneg_int(row['updated_at_ms']),
    }


def _import_project_brain_legacy_cutover(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    """Fold retired Project Brain tables into their settled projections.

    This reproduces the live ``project_brain.cutover`` read-only over the
    pinned source and restores each synthesized projection through the same
    point restore/get/scan operations as a settled projection. Per-owner
    folding keeps every restored row inside the operator scope, so the
    subsequent ``project_brain_projects`` domain sees only the rows it does
    not already own.
    """
    domain = 'project_brain_legacy_cutover'
    owner_user_id = _session_owner(session)
    documents = [
        document for document in project_brain_fold.fold_legacy_projections(source)
        if document['owner_user_id'] == owner_user_id
    ]
    progress = _resume_or_empty(checkpoint, domain)
    digest = hashlib.sha256(b'tofu-db.project-brain-legacy-fold.v1').hexdigest()
    for document in documents:
        digest = _record_chain(digest, _fold_restore_document(document))
    if progress is not None:
        if (not progress['complete'] or progress['rows'] != len(documents)
                or progress['sha256'] != digest):
            raise MigrationImportError(
                'resumed project brain legacy cutover verification differs')
        return

    def restore(document: dict[str, Any]) -> None:
        _command(session, 'migration.project.brain.project.restore',
                 _fold_restore_document(document))

    def verify(document: dict[str, Any]) -> None:
        observed = _query(session, 'migration.project.brain.project.get',
                          {'project_key': document['project_key']})
        if observed != _fold_restore_document(document):
            raise MigrationImportError(
                'project brain legacy cutover source and target differ')

    rows = 0
    pending = 0
    last: list[Any] = []
    for document in documents:
        restore(document)
        verify(document)
        rows += 1
        last = [document['owner_user_id'], document['project_key']]
        pending += 1
        if pending == batch_rows:
            _publish(checkpoint, domain, cursor=last, rows=rows, digest=digest,
                     complete=False, after_verified_prefix=after_verified_prefix)
            pending = 0
    # Owner-scoped catalog check: the fold proves target\u2286folded for exactly
    # the projections it created. ``project_brain_projects`` restores the
    # remaining source-settled rows and skips its own blanket scan, which would
    # otherwise see these folded rows as foreign occupants.
    expected = {document['project_key'] for document in documents}
    seen: set[str] = set()
    cursor = None
    while True:
        page = _query(session, 'migration.project.brain.project.scan',
                      {'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('rows'), list)
                or len(page['rows']) > OPERATOR_SESSION['maximumCatalogPageRows']):
            raise MigrationImportError('invalid project brain legacy fold catalog page')
        for identity in page['rows']:
            project_key = identity.get('project_key')
            if project_key in expected:
                if project_key in seen:
                    raise MigrationImportError(
                        'project brain legacy fold catalog repeats a projection')
                seen.add(project_key)
        following = page.get('cursor')
        if following is None:
            break
        if (not page['rows'] or not isinstance(following, str)
                or not following.startswith('project_brain:') or following == cursor):
            raise MigrationImportError(
                'project brain legacy fold catalog cursor did not advance')
        cursor = following
    if seen != expected:
        raise MigrationImportError(
            'project brain legacy fold source and target catalogs differ')
    # Record the folded keys so the projection importer can exclude exactly
    # these owner-scoped rows from its blanket catalog scan (they are the
    # fold's verified occupants, not source-settled rows).
    _FOLD_OCCUPANCY.clear()
    _FOLD_OCCUPANCY.update(expected)
    _publish(checkpoint, domain, cursor=last, rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


def _fold_restore_document(document: dict[str, Any]) -> dict[str, Any]:
    """The exact point-restore payload the native adapter persists."""
    return {
        'project_key': document['project_key'],
        'head_sequence': document['head_sequence'],
        'checkpoint_sequence': document['checkpoint_sequence'],
        'projection_json': document['projection_json'],
        'updated_at_ms': document['updated_at_ms'],
    }


def _import_project_brain_projects(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    table = _plan_table('project_brain_projects')
    owner_user_id = _session_owner(session)

    def precheck(row: dict[str, Any]) -> None:
        if _int(row['owner_user_id']) != owner_user_id:
            raise MigrationImportError(
                'project brain snapshots belong to owners outside the operator scope')

    _import_document_domain(
        source, session, checkpoint, 'project_brain_projects',
        batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix,
        table=table,
        build_document=_project_brain_document,
        restore_operation='migration.project.brain.project.restore',
        get_operation='migration.project.brain.project.get',
        get_identity=lambda document: {'project_key': document['project_key']},
        scan_operation='migration.project.brain.project.scan',
        cursor_label='project_brain:',
        identity_of_row=lambda identity: identity,
        point_key=lambda identity: (owner_user_id, _text(identity['project_key'])),
        chain_seed='tofu-db.project-brain-chain.v1',
        precheck=precheck,
        catalog_extra_keys=lambda: frozenset(
            (owner_user_id, key) for key in _FOLD_OCCUPANCY),
    )


def _optimizer_proposal_document(row: dict[str, Any]) -> dict[str, Any]:
    confidence = row['confidence']
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise MigrationImportError('optimizer proposal confidence is invalid')
    return {
        'id': _text(row['id']),
        'created_at': _text(row['created_at']),
        'title': _text(row['title']),
        'rationale': _text(row['rationale']),
        'action_type': _text(row['action_type']),
        'action_args': _text(row['action_args']),
        'severity': _text(row['severity']),
        'confidence': float(confidence),
        'evidence': _text(row['evidence']),
        'status': _text(row['status']),
        'status_reason': _text(row['status_reason']),
    }


def _optimizer_action_document(row: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': _text(row['id']),
        'proposal_id': _text(row['proposal_id']),
        'applied_at': _text(row['applied_at']),
        'expires_at': _text(row['expires_at'] or ''),
        'pre_metric': _text(row['pre_metric'] or ''),
        'outcome_metric': _text(row['outcome_metric'] or ''),
        'outcome_recorded_at': _text(row['outcome_recorded_at'] or ''),
        'reverted_at': _text(row['reverted_at'] or ''),
        'revert_reason': _text(row['revert_reason'] or ''),
    }


def _import_optimizer(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    owner_user_id = _session_owner(session)

    def owner_check(row: dict[str, Any]) -> None:
        if _int(row['user_id']) != owner_user_id:
            raise MigrationImportError(
                'optimizer snapshots belong to owners outside the operator scope')

    proposal_table = _plan_table('optimizer', 'optimizer_proposals')
    _import_document_domain(
        source, session, checkpoint, 'optimizer.proposals',
        batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix,
        table=proposal_table,
        build_document=_optimizer_proposal_document,
        restore_operation='migration.optimizer.restore',
        get_operation='migration.optimizer.get',
        get_identity=lambda document: {
            'table': 'proposals', 'id': document['id']},
        scan_operation='migration.optimizer.scan',
        cursor_label='optimizer:',
        identity_of_row=lambda identity: identity,
        point_key=lambda identity: (owner_user_id, _text(identity['id'])),
        chain_seed='tofu-db.optimizer-proposal-chain.v1',
        precheck=owner_check,
        restore_payload=lambda document: {
            'table': 'proposals', 'document': document, 'updated_at_ms': 1},
        scan_payload_extra={'table': 'proposals'},
    )
    action_table = _plan_table('optimizer', 'optimizer_action_log')
    # Actions restore only after every proposal landed; the native adapter
    # fails closed on an action whose proposal is absent.
    _import_document_domain(
        source, session, checkpoint, 'optimizer.actions',
        batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix,
        table=action_table,
        build_document=_optimizer_action_document,
        restore_operation='migration.optimizer.restore',
        get_operation='migration.optimizer.get',
        get_identity=lambda document: {
            'table': 'actions', 'id': document['id']},
        scan_operation='migration.optimizer.scan',
        cursor_label='optimizer:',
        identity_of_row=lambda identity: identity,
        point_key=lambda identity: (owner_user_id, _text(identity['id'])),
        chain_seed='tofu-db.optimizer-action-chain.v1',
        precheck=owner_check,
        restore_payload=lambda document: {
            'table': 'actions', 'document': document, 'updated_at_ms': 1},
        scan_payload_extra={'table': 'actions'},
    )
    rows, digest = 0, hashlib.sha256(b'tofu-db.optimizer.v1').hexdigest()
    for sub in ('proposals', 'actions'):
        progress = _resume_or_empty(checkpoint, 'optimizer.' + sub)
        if progress is None or not progress['complete']:
            raise MigrationImportError('optimizer sub-domain is not complete')
        rows += progress['rows']
        digest = _record_chain(digest, {'sub': sub, 'sha256': progress['sha256']})
    progress = _resume_or_empty(checkpoint, 'optimizer')
    if progress and (not progress['complete'] or progress['rows'] != rows
                     or progress['sha256'] != digest):
        raise MigrationImportError('resumed optimizer verification differs')
    _publish(checkpoint, 'optimizer', cursor=[], rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


def _log_aggregate_document(row: dict[str, Any]) -> dict[str, Any]:
    return {
        'fingerprint': _text(row['fingerprint']),
        'level': _text(row['level']),
        'logger': _text(row['logger'] or ''),
        'template': _text(row['template'] or ''),
        'sample': _text(row['sample'] or ''),
        'count': _int(row['count']),
        'first_seen': _nonneg_int(row['first_seen']),
        'last_seen': _nonneg_int(row['last_seen']),
        'updated_at_ms': max(_nonneg_int(row['last_seen']), 1),
    }


def _import_log_aggregates(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    table = _plan_table('log_aggregates')
    _import_document_domain(
        source, session, checkpoint, 'log_aggregates', batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix,
        table=table,
        build_document=_log_aggregate_document,
        restore_operation='migration.log.aggregate.restore',
        get_operation='migration.log.aggregate.get',
        get_identity=lambda document: {'fingerprint': document['fingerprint']},
        scan_operation='migration.log.aggregate.scan',
        cursor_label='log_aggregates:',
        identity_of_row=lambda identity: identity,
        point_key=lambda identity: (_text(identity['fingerprint']),),
        chain_seed='tofu-db.log-aggregate-chain.v1',
    )


def _billing_payment_document(row: dict[str, Any]) -> dict[str, Any]:
    raw = _json_load(row['raw'])
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise MigrationImportError('billing payment raw document is invalid')
    status = _text(row['status'])
    settled_at = _nonneg_int(row['settled_at'] or 0)
    if status not in ('pending', 'settled', 'failed'):
        raise MigrationImportError('billing payment status is invalid')
    if (status == 'settled') != (settled_at > 0):
        raise MigrationImportError('billing payment settlement is inconsistent')
    return {
        'id': _text(row['id']),
        'user_id': _text(row['user_id']),
        'provider': _text(row['provider']),
        'provider_id': _text(row['provider_id'] or ''),
        'amount_minor': _nonneg_int(row['amount_minor']),
        'currency': _text(row['currency'] or 'USD'),
        'credit_micro': _nonneg_int(row['credit_micro']),
        'status': status,
        'created_at': _nonneg_int(row['created_at']),
        'settled_at': settled_at,
        'raw': raw,
    }


def _import_billing_payments(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    table = _plan_table('billing_payments')
    domain = 'billing_payments'
    progress = _resume_or_empty(checkpoint, domain)
    rows = 0
    digest = hashlib.sha256(b'tofu-db.billing-payments.snapshot.v1').hexdigest()
    after = None
    if progress is not None:
        for row in _scan(source, table,
                         until=None if progress['complete'] else progress['cursor'],
                         complete=progress['complete']):
            document = _billing_payment_document(row)
            observed = _query(session, 'migration.billing.payment.get',
                              {'id': document['id']})
            if observed != document:
                raise MigrationImportError(
                    'billing payment source and target snapshots differ')
            rows += 1
            digest = _record_chain(digest, document)
        if rows != progress['rows'] or digest != progress['sha256']:
            raise MigrationImportError('resumed billing payment prefix differs')
        if progress['complete']:
            if _verify_payment_catalog(source, session) != rows:
                raise MigrationImportError('billing payment catalog count differs')
            return
        after = progress['cursor']
    pending = 0
    last = [] if after is None else list(after)
    iterator = (source.rows(table['name'], table['columns'], table['keys'],
                            after=after, batch_rows=batch_rows)
                if table['name'] in source.tables else ())
    for row in iterator:
        document = _billing_payment_document(row)
        _command(session, 'migration.billing.payment.restore',
                 {'document': document})
        observed = _query(session, 'migration.billing.payment.get',
                          {'id': document['id']})
        if observed != document:
            raise MigrationImportError(
                'billing payment source and target snapshots differ')
        rows += 1
        digest = _record_chain(digest, document)
        last = list(_pk(table, row))
        pending += 1
        if pending == batch_rows:
            _publish(checkpoint, domain, cursor=last, rows=rows, digest=digest,
                     complete=False, after_verified_prefix=after_verified_prefix)
            pending = 0
    if _verify_payment_catalog(source, session) != rows:
        raise MigrationImportError('billing payment catalog count differs')
    _publish(checkpoint, domain, cursor=last, rows=rows, digest=digest,
             complete=True, after_verified_prefix=after_verified_prefix)


def _verify_payment_catalog(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
) -> int:
    table = _plan_table('billing_payments')
    rows = 0
    for identity in _billing_catalog(session, 'payments'):
        if set(identity) != {'id'} or not isinstance(identity['id'], str):
            raise MigrationImportError('invalid billing payment catalog identity')
        row = (source.point(table['name'], table['columns'], table['keys'],
                            (identity['id'],))
               if table['name'] in source.tables else None)
        if row is None:
            raise MigrationImportError('billing payment target holds an extra row')
        observed = _query(session, 'migration.billing.payment.get',
                          {'id': identity['id']})
        if observed != _billing_payment_document(row):
            raise MigrationImportError(
                'billing payment source and target snapshots differ')
        rows += 1
    count_page = _billing_catalog(session, 'payment_count')
    counts = [row.get('count') for row in count_page]
    if counts != ([rows] if rows else []):
        raise MigrationImportError('billing payment count differs')
    for derived in ('payment_id_claims', 'payment_provider_claims',
                    'payment_created_index'):
        derived_rows = 0
        for identity in _billing_catalog(session, derived):
            if set(identity) != {'id'} or not isinstance(identity['id'], str):
                raise MigrationImportError('invalid billing payment index identity')
            row = (source.point(table['name'], table['columns'], table['keys'],
                                (identity['id'],))
                   if table['name'] in source.tables else None)
            if row is None:
                raise MigrationImportError('billing payment index has an extra target')
            if derived == 'payment_provider_claims' and not _text(row['provider_id'] or ''):
                raise MigrationImportError('billing payment provider index has an extra target')
            derived_rows += 1
        if derived == 'payment_provider_claims':
            expected = sum(bool(_text(r['provider_id'] or ''))
                           for r in _scan(source, table))
            if derived_rows != expected:
                raise MigrationImportError('billing payment provider index count differs')
        elif derived_rows != rows:
            raise MigrationImportError('billing payment index count differs')
    return rows


def _redeem_batches_from_source(
    source: SQLiteMigrationSource,
) -> list[dict[str, Any]]:
    table = _plan_table('billing_redeem_codes')
    groups: dict[str, dict[str, Any]] = {}
    for row in _scan(source, table):
        code = _text(row['code'])
        amount_micro = _nonneg_int(row['amount_micro'])
        batch_label = _text(row['batch'] or '')
        created_by = _text(row['created_by'] or '')
        created_at = _nonneg_int(row['created_at'])
        expires_at = _nonneg_int(row['expires_at'] or 0)
        note = _text(row['note'] or '')
        redeemed_by = _text(row['redeemed_by'] or '')
        redeemed_at = _nonneg_int(row['redeemed_at'] or 0)
        if bool(redeemed_by) != bool(redeemed_at):
            raise MigrationImportError('redeem code redemption state is inconsistent')
        key = (batch_label, amount_micro, created_by, created_at, expires_at, note)
        group = groups.get(repr(key))
        if group is None:
            group = {
                'codes': [], 'amount_micro': amount_micro, 'batch': batch_label,
                'created_by': created_by, 'created_at': created_at,
                'expires_at': expires_at, 'note': note, 'redeemed': [],
            }
            groups[repr(key)] = group
        group['codes'].append(code)
        if redeemed_by:
            group['redeemed'].append({
                'code': code, 'redeemed_by': redeemed_by,
                'redeemed_at': redeemed_at,
            })
    batches = []
    for group in groups.values():
        if not 1 <= len(group['codes']) <= 10_000:
            raise MigrationImportError('redeem batch exceeds the ten-thousand-code contract')
        batches.append(group)
    batches.sort(key=lambda group: group['codes'][0])
    return batches


def _import_billing_redeem_codes(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
    checkpoint: MigrationCheckpoint, *, batch_rows: int,
    after_verified_prefix: AfterVerified | None,
) -> None:
    del batch_rows  # batches restore whole; progress is per batch
    domain = 'billing_redeem_codes'
    batches = _redeem_batches_from_source(source)
    progress = _resume_or_empty(checkpoint, domain)

    def digest_batches(prefix: list[dict[str, Any]]) -> str:
        digest = hashlib.sha256(b'tofu-db.billing-redeem.snapshot.v1').hexdigest()
        for batch in prefix:
            digest = _record_chain(digest, batch)
        return digest

    def verify_batch(batch: dict[str, Any]) -> None:
        observed = _query(session, 'migration.billing.redeem.get',
                          {'code': batch['codes'][0]})
        expected = {
            'code': batch['codes'][0],
            'amount_micro': batch['amount_micro'],
            'batch': batch['batch'],
            'created_by': batch['created_by'],
            'created_at': batch['created_at'],
            'expires_at': batch['expires_at'],
            'note': batch['note'],
            'record_id': batch['codes'][0],
            'codes': batch['codes'],
        }
        for field, value in expected.items():
            if observed is None or observed.get(field) != value:
                raise MigrationImportError(
                    'billing redeem source and target snapshots differ')
        states = {state['code']: state for state in batch['redeemed']}
        for code in batch['codes']:
            found = _query(session, 'migration.billing.redeem.get', {'code': code})
            state = states.get(code)
            if (found is None
                    or found.get('redeemed_by') != (state['redeemed_by'] if state else '')
                    or found.get('redeemed_at') != (state['redeemed_at'] if state else 0)):
                raise MigrationImportError(
                    'billing redeem source and target snapshots differ')

    start = 0
    if progress is not None:
        completed = progress['rows']
        if progress['complete'] and completed != len(batches):
            raise MigrationImportError('resumed billing redeem verification differs')
        if completed > len(batches) or digest_batches(batches[:completed]) != progress['sha256']:
            raise MigrationImportError('resumed billing redeem prefix differs')
        for batch in batches[:completed]:
            verify_batch(batch)
        if progress['complete']:
            _verify_redeem_catalog(source, session)
            return
        start = completed
    for index in range(start, len(batches)):
        batch = batches[index]
        _command(session, 'migration.billing.redeem.restore', batch)
        verify_batch(batch)
        _publish(checkpoint, domain, cursor=[index], rows=index + 1,
                 digest=digest_batches(batches[:index + 1]),
                 complete=False, after_verified_prefix=after_verified_prefix)
    _verify_redeem_catalog(source, session)
    _publish(checkpoint, domain, cursor=[], rows=len(batches),
             digest=digest_batches(batches), complete=True,
             after_verified_prefix=after_verified_prefix)


def _verify_redeem_catalog(
    source: SQLiteMigrationSource, session: OfflineMigrationSession,
) -> None:
    table = _plan_table('billing_redeem_codes')
    source_codes = {_text(row['code']) for row in _scan(source, table)}
    source_redeemed = {
        _text(row['code'])
        for row in _scan(source, table) if _text(row['redeemed_by'] or '')
    }
    count_rows = _billing_catalog(session, 'redeem_count')
    counts = [row.get('count') for row in count_rows]
    if counts != ([len(source_codes)] if source_codes else []):
        raise MigrationImportError('billing redeem code count differs')
    locator_codes = set()
    for shard in _billing_catalog(session, 'redeem_locators'):
        if not isinstance(shard, list):
            raise MigrationImportError('invalid billing redeem locator catalog')
        for row in shard:
            code = row.get('code')
            if not isinstance(code, str):
                raise MigrationImportError('invalid billing redeem locator identity')
            locator_codes.add(code)
    if locator_codes != source_codes:
        raise MigrationImportError('billing redeem locator catalog differs')
    state_codes = set()
    for shard in _billing_catalog(session, 'redeem_states'):
        if not isinstance(shard, list):
            raise MigrationImportError('invalid billing redeem state catalog')
        for row in shard:
            code = row.get('code')
            if not isinstance(code, str) or code not in source_redeemed:
                raise MigrationImportError('billing redeem state catalog differs')
            state_codes.add(code)
    if state_codes != source_redeemed:
        raise MigrationImportError('billing redeem state catalog differs')
    batches = _redeem_batches_from_source(source)
    batch_rows = _billing_catalog(session, 'redeem_batches')
    found_batches = set()
    for shard in batch_rows:
        if not isinstance(shard, list):
            raise MigrationImportError('invalid billing redeem batch catalog')
        for row in shard:
            if not isinstance(row.get('record_id'), str):
                raise MigrationImportError('invalid billing redeem batch identity')
            found_batches.add(row['record_id'])
    if found_batches != {batch['codes'][0] for batch in batches}:
        raise MigrationImportError('billing redeem batch catalog differs')

def _require_source_table_coverage(source: SQLiteMigrationSource) -> None:
    from lib.storage_authority_sql.migration.tofudb_migration_coverage import source_table_coverage

    coverage = source_table_coverage(source, PLAN)
    if not coverage['sourceTableCoverageComplete']:
        raise MigrationImportError(
            'source contains unmapped populated tables: '
            + ', '.join(coverage['unmappedPopulatedTables']))


def import_sqlite_authority(
    source_path: Any,
    session: OfflineMigrationSession,
    checkpoint_path: Any,
    target_authority_uuid: str, *,
    batch_rows: int = 16,
    after_verified_prefix: AfterVerified | None = None,
    binding: MigrationBinding | None = None,
    require_complete_source_coverage: bool = False,
) -> MigrationBinding:
    """Import declared domains from one pinned SQLite snapshot into Tofu-DB."""
    if type(batch_rows) is not int or not 1 <= batch_rows <= MAX_SOURCE_BATCH_ROWS:
        raise MigrationImportError('invalid migration batch size')
    try:
        with SQLiteMigrationSource(
            source_path, maximum_row_bytes=MAX_SOURCE_WIDE_ROW_BYTES
        ) as source:
            if require_complete_source_coverage:
                _require_source_table_coverage(source)
            resolved = compute_binding(source, target_authority_uuid)
            if binding is not None and binding != resolved:
                raise MigrationImportError('supplied migration binding differs from current source or plan')
            if resolved.target_authority_uuid != target_authority_uuid:
                raise MigrationImportError('target authority identity differs')
            if resolved.plan_sha256 != PLAN_SHA256:
                raise MigrationImportError('mapping plan digest differs')
            _preflight_managed_record_namespaces(source)
            with MigrationCheckpoint(checkpoint_path, resolved) as checkpoint:
                _import_tenant_users(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                from lib.storage_authority_sql.migration.tofudb_migration_trash import import_trash
                import_trash(source, session, checkpoint, batch_rows=batch_rows,
                             after_verified_prefix=after_verified_prefix)
                _import_conversations(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_attempts(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_attempt_events(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_turns(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                # Durable-spine domains: sync changes reference restored
                # turns/attempts, and heads are the max restored sync
                # sequence, so heads import last of the three.
                _import_storage_events(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_conversation_sync_events(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_conversation_sync_heads(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                from lib.storage_authority_sql.migration.tofudb_migration_tombstones import import_tombstones
                import_tombstones(source, session, checkpoint, batch_rows=batch_rows,
                                  after_verified_prefix=after_verified_prefix)
                _import_frozen_transcripts(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_raw_archives(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_compaction_archives(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_credentials(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_providers(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_billing(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_task_results(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_continuations(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_records(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                # Idempotency receipts are owner/tenant explicit and must land
                # before any post-migration command could re-execute; the live
                # v2 family restores before the source-fenced legacy one.
                _import_command_receipts_v2(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_command_receipts(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                retired.import_retired(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                retired_large.import_archives(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                # The build budget restore cross-validates every reservation
                # against the artifact metadata replayed by the records
                # domain, so both budgets run only after records complete.
                _import_paper_budget(
                    source, session, checkpoint, 'paper_build_budget',
                    batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_paper_budget(
                    source, session, checkpoint, 'paper_document_budget',
                    batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_desktop_egress(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_daily_cost(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_rate_limit_events(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_orchestration(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_swarm(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_integration(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_paper(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_knowledge(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_chat_artifacts(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_tool_result_artifacts(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_browser_observations(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_model_routing(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_plugin_store(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                # Fold the retired Project Brain authorities into their settled
                # projections before the projection domain restores the
                # remaining source-settled rows around them.
                _import_project_brain_legacy_cutover(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_project_brain_projects(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_optimizer(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_log_aggregates(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_billing_payments(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_billing_redeem_codes(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                # Queue items restore before autopilot markers: a marker
                # references its armed queue identity, and the timer and
                # scheduler poll logs restore only against restored owners.
                _import_queue_items(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_autopilot_markers(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_timers(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_scheduled_tasks(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
                _import_worker_jobs(
                    source, session, checkpoint, batch_rows=batch_rows,
                    after_verified_prefix=after_verified_prefix,
                )
            return resolved
    except (MigrationSourceError, MigrationCheckpointError) as exc:
        raise MigrationImportError(str(exc)) from exc


def certify_rollback(
    source_path: Any, checkpoint_path: Any, *,
    target_authority_uuid: str | None = None,
) -> dict[str, Any]:
    """Certify that abandoning the partial Tofu-DB target is safe.

    Rollback falls back to the untouched SQLite authority; this certification
    tool only proves that fallback is sound and reports the verified journal.
    It never deletes or mutates source, target, or checkpoint data. It is
    fail-closed: any source divergence or missing, corrupt, or locked
    checkpoint/binding raises :class:`MigrationImportError` instead of
    returning a verdict.
    """
    verdict: dict[str, Any] = {
        'rollbackSafe': False,
        'sourceUntouched': False,
        'completeDomains': [],
        'incompleteDomains': [],
        'unchangedDomains': [domain['name'] for domain in PLAN['domains']],
    }
    try:
        if not Path(checkpoint_path).is_file():
            raise MigrationImportError(
                'checkpoint binding differs: no verified migration journal exists')
        with SQLiteMigrationSource(
            source_path, maximum_row_bytes=MAX_SOURCE_WIDE_ROW_BYTES
        ) as source:
            live = compute_binding(source, target_authority_uuid or str(uuid.uuid4()))
            with MigrationCheckpoint(checkpoint_path, live) as checkpoint:
                binding = checkpoint.binding
                if (target_authority_uuid is not None
                        and binding.target_authority_uuid != target_authority_uuid):
                    raise MigrationImportError(
                        'target authority identity differs from the migration binding')
                if binding.plan_sha256 != PLAN_SHA256:
                    raise MigrationImportError(
                        'checkpoint binding plan digest differs from this importer')
                for name in list(verdict['unchangedDomains']):
                    entry = checkpoint.domain(name)
                    if entry is None:
                        continue
                    verdict['unchangedDomains'].remove(name)
                    (verdict['completeDomains'] if entry['complete']
                     else verdict['incompleteDomains']).append(name)
                verdict['completeDomains'].sort(
                    key=lambda n: _plan_domain_index(n))
                verdict['incompleteDomains'].sort(
                    key=lambda n: _plan_domain_index(n))
    except (MigrationSourceError, MigrationCheckpointError) as exc:
        raise MigrationImportError(str(exc)) from exc
    # Reaching here proves the pinned source re-hash and schema version equal
    # the checkpoint binding exactly, so the SQLite authority is untouched.
    verdict['sourceUntouched'] = True
    verdict['rollbackSafe'] = True
    return verdict

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='python -m lib.storage_authority_sql.migration.tofudb_migration_import',
        description='Explicit SQLite to Tofu-DB domain import; never a startup path.',
    )
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--target', type=Path)
    parser.add_argument('--owner-id', type=int)
    parser.add_argument('--tenant-id', type=int)
    parser.add_argument('--authority-uuid')
    parser.add_argument('--batch-rows', type=int, default=16)
    parser.add_argument(
        '--rollback', action='store_true',
        help='certify abandoning the partial target is safe; deletes nothing.',
    )
    args = parser.parse_args(argv)
    source = args.source.expanduser()
    checkpoint = args.checkpoint.expanduser()
    if not source.is_absolute() or not checkpoint.is_absolute():
        print(json.dumps({
            'ok': False, 'error': 'MigrationImportError',
            'message': 'migration source and checkpoint require absolute paths',
        }, separators=(',', ':')), file=sys.stderr)
        return 2
    if args.rollback:
        try:
            verdict = certify_rollback(
                source, checkpoint, target_authority_uuid=args.authority_uuid,
            )
        except (MigrationImportError, OSError, ValueError) as exc:
            verdict = {
                'rollbackSafe': False,
                'sourceUntouched': False,
                'completeDomains': [],
                'incompleteDomains': [],
                'unchangedDomains': sorted(
                    domain['name'] for domain in PLAN['domains']),
                'reason': f'{type(exc).__name__}: {exc}',
            }
        print(json.dumps(verdict, separators=(',', ':'), sort_keys=True))
        return 0 if verdict['rollbackSafe'] else 2
    missing = [name for name in ('binary', 'target', 'owner_id', 'tenant_id',
                                 'authority_uuid')
               if getattr(args, name) is None]
    if missing:
        print(json.dumps({
            'ok': False, 'error': 'MigrationImportError',
            'message': 'import mode requires ' + ', '.join(
                '--' + name.replace('_', '-') for name in missing),
        }, separators=(',', ':')), file=sys.stderr)
        return 2
    session = None
    try:
        with SQLiteMigrationSource(source) as coverage_source:
            _require_source_table_coverage(coverage_source)
        session = OfflineMigrationSession(
            args.binary, args.target, args.authority_uuid,
            owner_id=args.owner_id, tenant_id=args.tenant_id,
            timeout=_REQUEST_DEADLINE_MS / 1000,
        )
        binding = import_sqlite_authority(
            source, session, checkpoint, args.authority_uuid,
            batch_rows=args.batch_rows,
            require_complete_source_coverage=True,
        )
        print(json.dumps({
            'ok': True,
            'planId': PLAN['id'],
            'sourceSha256': binding.source_sha256,
            'sourceSchemaVersion': binding.source_schema_version,
            'targetAuthorityUuid': binding.target_authority_uuid,
            'planSha256': binding.plan_sha256,
        }, separators=(',', ':'), sort_keys=True))
        return 0
    except (MigrationImportError, StorageV2Error, OSError, ValueError) as exc:
        print(json.dumps({
            'ok': False,
            'error': type(exc).__name__,
            'message': str(exc),
        }, separators=(',', ':')), file=sys.stderr)
        return 2
    finally:
        if session is not None:
            session.close()


if __name__ == '__main__':
    raise SystemExit(main())
