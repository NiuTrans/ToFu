"""Offline SQLite → Tofu-DB importer: resume, identity, independent verify."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import tracemalloc
from uuid import uuid4

import pytest

import lib.storage_sidecar.tofudb_migration_import as importer
from lib.storage_sidecar.schema import SCHEMA_VERSION
from lib.storage_sidecar.tofudb_migration_checkpoint import MigrationCheckpoint
from lib.storage_sidecar.tofudb_migration_import import (
    MigrationImportError,
    certify_rollback,
    import_sqlite_authority,
    main,
)
from tofu_db.wire import StorageV2Error
from lib.storage_sidecar.tofudb_migration_target import OfflineMigrationSession
from lib.storage_sidecar.tofudb_migration_target import MigrationTargetError


pytestmark = pytest.mark.unit
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_TEST_SECRET = b'tofudb-differential-test-secret-0001'
_OWNER_ID = 11
_TENANT_ID = 7
_CREATED = 1_700_000_000.0
_SCHEMA_ARCHIVE_RECORD_KEY = (
    'migration.retired_sqlite.v1',
    'storage_meta:' + hashlib.sha256(b'[["text","schema_version"]]').hexdigest(),
)


class _Crash(RuntimeError):
    """Injected after a verified prefix so resume can be tested."""


class _Response:
    def __init__(
        self, payload, *, status: int = 0, code: str = '', message: str = '',
    ):
        self.payload = payload
        self.status = status
        self.error_code = code
        self.error_message = message
        self.retryable = False

    def json(self):
        return self.payload

    def raise_for_error(self) -> None:
        if self.status != 0:
            raise StorageV2Error(self.status, self.error_code, self.error_message, False)


class FakeSession:
    """In-memory storage.v2 stand-in covering the importer's declared domains."""

    def __init__(self):
        self.owner_id = _OWNER_ID
        self.tenant_id = _TENANT_ID
        self.credentials: dict[str, dict] = {}
        self.secrets: dict[str, str] = {}
        self.providers: dict[str, dict] = {}
        self.records: dict[tuple[str, str], dict] = {}
        self.continuations: dict[str, dict] = {}
        self.wallets: dict[str, dict] = {}
        self.ledger: dict[str, dict] = {}
        self.users: dict[str, dict] = {}
        self.conversations: dict[str, dict] = {}
        self.frozen_transcripts: dict[str, dict] = {}
        self.task_results: dict[str, dict] = {}
        self.turns: dict[str, dict] = {}
        self.paper_budgets: dict[str, dict] = {}
        self.desktop_egress: dict | None = None
        self.daily_costs: dict[str, dict] = {}
        self.chat_artifacts: dict[str, dict] = {}
        self.tool_results: dict[str, dict] = {}
        self.browser_observations: dict[tuple[str, str, str], dict] = {}
        self.browser_owner_touched_at_ms: int = 0
        self.routing_authorities: dict[str, dict] = {}
        self.routing_secrets: dict[tuple[str, str], dict] = {}
        self.plugin_manifests: dict[str, dict] = {}
        self.plugin_rows: dict[tuple[str, str, str], dict] = {}
        self.plugin_unique: dict[tuple[str, str, str, str], dict] = {}
        self.project_brain_projects: dict[str, dict] = {}
        self.optimizer_proposals: dict[str, dict] = {}
        self.optimizer_actions: dict[str, dict] = {}
        self.log_aggregates: dict[str, dict] = {}
        self.billing_payments: dict[str, dict] = {}
        self.redeem_batches: dict[str, dict] = {}
        self.redeem_states: dict[str, dict] = {}
        self.paper_library: dict[str, dict] = {}
        self.paper_reports: dict[tuple[str, str], dict] = {}
        self.paper_translations: dict[tuple[str, str], dict] = {}
        self.paper_notes: dict[str, dict] = {}
        self.paper_podcasts: dict[tuple[str, str, str, str], dict] = {}
        self.knowledge_documents: dict[str, dict] = {}
        self.knowledge_settings: dict | None = None
        self.orch_definitions: dict[tuple[str, str], dict] = {}
        self.orch_runs: dict[tuple[str, str], dict] = {}
        self.orch_events: dict[tuple[str, str, int], dict] = {}
        self.swarm_sessions: dict[str, dict] = {}
        self.swarm_agents: dict[tuple[str, str], dict] = {}
        self.integration_workspaces: dict[tuple[str, str], dict] = {}
        self.integration_events: dict[tuple[str, int], dict] = {}
        self.rate_limit_events: dict[str, dict] = {}
        self.queue_items: dict[str, dict] = {}
        self.autopilot_markers: dict[str, dict] = {}
        self.timers: dict[str, dict] = {}
        self.timer_polls: dict[tuple[str, int], dict] = {}
        self.scheduler_tasks: dict[str, dict] = {}
        self.scheduler_polls: dict[tuple[str, int], dict] = {}
        self.worker_jobs: dict[str, dict] = {}
        self.command_receipts_v2: dict[tuple[int, int, str], dict] = {}
        self.command_receipts: dict[tuple[int, str, str], dict] = {}

        self.command_receipts_v2: dict[tuple[int, int, str], dict] = {}
        self.command_receipts: dict[tuple[int, str, str], dict] = {}
        self.storage_events: dict[tuple[str, int], dict] = {}
        self.sync_events: dict[tuple[str, int], dict] = {}
        self.sync_heads: dict[str, dict] = {}
    def request(self, operation, payload, *, command_id, deadline_unix_ms):
        del command_id, deadline_unix_ms
        handlers = {
            'credential.create': self._create_credential,
            'credential.list': self._list_credentials,
            'credential.identify': self._identify,
            'credential.touch': self._touch,
            'credential.update': self._update,
            'credential.revoke': self._revoke,
            'provider.create': self._create_provider,
            'provider.get': self._get_provider,
            'provider.list': self._list_providers,
            'provider.update': self._update_provider,
            'provider.touch': self._touch_provider,
            'migration.record.get': self._record_get,
            'migration.record.restore': self._record_put,
            'migration.record.scan': self._record_list,
            'migration.continuation.get': self._continuation_get,
            'migration.continuation.restore': self._continuation_put,
            'migration.continuation.scan': self._continuation_scan,
            'migration.paper.build.budget.restore': self._paper_budget_restore(
                'paper.build.budget.v1'),
            'migration.paper.build.budget.get': self._paper_budget_get(
                'paper.build.budget.v1'),
            'migration.paper.document.budget.restore': self._paper_budget_restore(
                'paper.document.budget.v1'),
            'migration.paper.document.budget.get': self._paper_budget_get(
                'paper.document.budget.v1'),
            'migration.billing.restore': self._billing_restore,
            'migration.billing.get': self._billing_get,
            'migration.billing.scan': self._billing_scan,
            'migration.task.result.restore': self._task_result_restore,
            'migration.task.result.get': self._task_result_get,
            'migration.task.result.scan': self._task_result_scan,
            'migration.tenant.user.restore': self._tenant_restore,
            'migration.tenant.user.get': self._tenant_get,
            'migration.tenant.user.scan': self._tenant_scan,
            'migration.turn.restore': self._turn_restore,
            'migration.turn.get': self._turn_get,
            'migration.turn.scan': self._turn_scan,
            'conversation.create': self._create_conversation,
            'migration.conversation.restore': self._restore_conversation,
            'migration.turn.tombstone.scan': lambda payload: _Response({'rows': [], 'cursor': None}),
            'migration.trash.scan': lambda payload: _Response({'rows': [], 'cursor': None, 'pending_conversation_id': getattr(self, 'pending_trash', None)}),
            'conversation.get': self._get_conversation,
            'conversation.list': self._list_conversations,
            'conversation.count': self._count_conversations,
            'billing.wallet.apply': self._wallet_apply,
            'billing.wallet.get': self._wallet_get,
            'billing.ledger.list': self._ledger_list,
            'migration.desktop.egress.restore': self._desktop_egress_restore,
            'migration.desktop.egress.get': self._desktop_egress_get,
            'migration.desktop.egress.scan': self._desktop_egress_scan,
            'migration.daily.cost.restore': self._daily_cost_restore,
            'migration.daily.cost.get': self._daily_cost_get,
            'migration.daily.cost.scan': self._daily_cost_scan,
            'migration.chat.artifact.restore': self._chat_artifact_restore,
            'migration.chat.artifact.get': self._chat_artifact_get,
            'migration.chat.artifact.scan': self._chat_artifact_scan,
            'migration.tool.result.restore': self._tool_result_restore,
            'migration.tool.result.get': self._tool_result_get,
            'migration.tool.result.scan': self._tool_result_scan,
            'migration.browser.observation.restore': self._browser_observation_restore,
            'migration.browser.observation.get': self._browser_observation_get,
            'migration.browser.observation.scan': self._browser_observation_scan,
            'migration.model.routing.authority.restore': self._routing_authority_restore,
            'migration.model.routing.authority.get': self._routing_authority_get,
            'migration.model.routing.authority.scan': self._routing_authority_scan,
            'migration.model.routing.secret.restore': self._routing_secret_restore,
            'migration.model.routing.secret.get': self._routing_secret_get,
            'migration.model.routing.secret.scan': self._routing_secret_scan,
            'migration.plugin.manifest.restore': self._plugin_manifest_restore,
            'migration.plugin.manifest.get': self._plugin_manifest_get,
            'migration.plugin.manifest.scan': self._plugin_manifest_scan,
            'migration.plugin.rows.restore': self._plugin_rows_restore,
            'migration.plugin.rows.get': self._plugin_rows_get,
            'migration.plugin.rows.scan': self._plugin_rows_scan,
            'migration.plugin.unique.restore': self._plugin_unique_restore,
            'migration.plugin.unique.get': self._plugin_unique_get,
            'migration.plugin.unique.scan': self._plugin_unique_scan,
            'migration.project.brain.project.restore': self._project_brain_restore,
            'migration.project.brain.project.get': self._project_brain_get,
            'migration.project.brain.project.scan': self._project_brain_scan,
            'migration.optimizer.restore': self._optimizer_restore,
            'migration.optimizer.get': self._optimizer_get,
            'migration.optimizer.scan': self._optimizer_scan,
            'migration.log.aggregate.restore': self._log_aggregate_restore,
            'migration.log.aggregate.get': self._log_aggregate_get,
            'migration.log.aggregate.scan': self._log_aggregate_scan,
            'migration.billing.payment.restore': self._payment_restore,
            'migration.billing.payment.get': self._payment_get,
            'migration.billing.payment.scan': self._payment_scan,
            'migration.billing.redeem.restore': self._redeem_restore,
            'migration.billing.redeem.get': self._redeem_get,
            'migration.billing.redeem.scan': self._redeem_scan,
            'migration.paper.library.restore': self._paper_library_restore,
            'migration.paper.library.get': self._paper_library_get,
            'migration.paper.library.scan': self._paper_library_scan,
            'migration.paper.report.restore': self._paper_report_restore,
            'migration.paper.report.get': self._paper_report_get,
            'migration.paper.report.scan': self._paper_report_scan,
            'migration.paper.translation.restore': self._paper_translation_restore,
            'migration.paper.translation.get': self._paper_translation_get,
            'migration.paper.translation.scan': self._paper_translation_scan,
            'migration.paper.note.restore': self._paper_note_restore,
            'migration.paper.note.get': self._paper_note_get,
            'migration.paper.note.scan': self._paper_note_scan,
            'migration.paper.podcast.restore': self._paper_podcast_restore,
            'migration.paper.podcast.get': self._paper_podcast_get,
            'migration.paper.podcast.scan': self._paper_podcast_scan,
            'migration.knowledge.settings.restore': self._knowledge_settings_restore,
            'migration.knowledge.settings.get': self._knowledge_settings_get,
            'migration.knowledge.document.restore': self._knowledge_document_restore,
            'migration.knowledge.document.get': self._knowledge_document_get,
            'migration.knowledge.catalog.scan': self._knowledge_catalog_scan,
            'migration.rate.limit.event.restore': self._rate_limit_restore,
            'migration.rate.limit.event.get': self._rate_limit_get,
            'migration.rate.limit.event.scan': self._rate_limit_scan,
            'migration.orchestration.definition.restore': self._orch_definition_restore,
            'migration.orchestration.definition.get': self._orch_definition_get,
            'migration.orchestration.definition.scan': self._orch_definition_scan,
            'migration.orchestration.run.restore': self._orch_run_restore,
            'migration.orchestration.run.get': self._orch_run_get,
            'migration.orchestration.run.scan': self._orch_run_scan,
            'migration.orchestration.event.restore': self._orch_event_restore,
            'migration.orchestration.event.get': self._orch_event_get,
            'migration.orchestration.event.scan': self._orch_event_scan,
            'migration.swarm.session.restore': self._swarm_session_restore,
            'migration.swarm.session.get': self._swarm_session_get,
            'migration.swarm.session.scan': self._swarm_session_scan,
            'migration.swarm.agent.restore': self._swarm_agent_restore,
            'migration.swarm.agent.get': self._swarm_agent_get,
            'migration.swarm.agent.scan': self._swarm_agent_scan,
            'migration.integration.workspace.restore': self._integration_workspace_restore,
            'migration.integration.workspace.get': self._integration_workspace_get,
            'migration.integration.workspace.scan': self._integration_workspace_scan,
            'migration.integration.event.restore': self._integration_event_restore,
            'migration.integration.event.get': self._integration_event_get,
            'migration.integration.event.scan': self._integration_event_scan,
            'migration.integration.meta.restore': self._integration_meta_restore,
            'migration.queue.item.restore': self._queue_item_restore,
            'migration.queue.item.get': self._queue_item_get,
            'migration.queue.item.scan': self._queue_item_scan,
            'migration.queue.autopilot.marker.restore': self._autopilot_restore,
            'migration.queue.autopilot.marker.get': self._autopilot_get,
            'migration.queue.autopilot.marker.scan': self._autopilot_scan,
            'migration.timer.restore': self._timer_restore,
            'migration.timer.get': self._timer_get,
            'migration.timer.scan': self._timer_scan,
            'migration.timer.poll.restore': self._timer_poll_restore,
            'migration.timer.poll.get': self._timer_poll_get,
            'migration.timer.poll.scan': self._timer_poll_scan,
            'migration.scheduler.task.restore': self._scheduler_task_restore,
            'migration.scheduler.task.get': self._scheduler_task_get,
            'migration.scheduler.task.scan': self._scheduler_task_scan,
            'migration.scheduler.poll.restore': self._scheduler_poll_restore,
            'migration.scheduler.poll.get': self._scheduler_poll_get,
            'migration.scheduler.poll.scan': self._scheduler_poll_scan,
            'migration.worker.job.restore': self._worker_job_restore,
            'migration.worker.job.get': self._worker_job_get,
            'migration.worker.job.scan': self._worker_job_scan,
            'migration.frozen.transcript.begin': self._frozen_begin,
            'migration.frozen.transcript.finish': self._frozen_finish,
            'migration.frozen.transcript.get': lambda p: _Response(self.frozen_transcripts.get(p['conversation_id'])),
            'migration.frozen.transcript.scan': self._frozen_scan,
            'migration.raw.archive.scan': lambda _: _Response({'rows': [], 'cursor': None}),
            'migration.raw.archive.stats': lambda p: _Response({'count': 0} if p['scope'] == 'owner' else {'stored_bytes': 0}),
            'migration.compaction.archive.scan': lambda _: _Response({'rows': [], 'cursor': None}),
            'migration.attempt.event.scan': lambda _: _Response({'rows': [], 'cursor': None}),
            'migration.attempt.scan': lambda _: _Response({'rows': [], 'cursor': None}),
            'migration.command.receipt.restore': self._command_receipt_v2_restore,
            'migration.command.receipt.get': self._command_receipt_v2_get,
            'migration.command.receipt.scan': self._command_receipt_v2_scan,
            'migration.legacy.receipt.restore': self._legacy_receipt_restore,
            'migration.legacy.receipt.get': self._legacy_receipt_get,
            'migration.legacy.receipt.scan': self._legacy_receipt_scan,
            'migration.storage.event.restore': self._storage_event_restore,
            'migration.storage.event.restore_batch': self._storage_event_restore_batch,
            'migration.storage.event.get': self._storage_event_get,
            'migration.storage.event.scan': self._storage_event_scan,
            'migration.conversation.sync.event.restore': self._sync_event_restore,
            'migration.conversation.sync.event.get': self._sync_event_get,
            'migration.conversation.sync.event.scan': self._sync_event_scan,
            'migration.conversation.sync.head.restore': self._sync_head_restore,
            'migration.conversation.sync.head.get': self._sync_head_get,
            'migration.conversation.sync.head.scan': self._sync_head_scan,
        }
        try:
            return handlers[operation](payload)
        except StorageV2Error as exc:
            return _Response(None, status=exc.status, code=exc.code, message=exc.message)

    def _frozen_begin(self, payload):
        identity = payload['conversation_id']
        inserted = identity not in self.frozen_transcripts
        self.frozen_transcripts.setdefault(identity, {'source': payload, 'complete': False,
                                                     'messages': 0, 'decoded_bytes': 0, 'pages': []})
        return _Response({'inserted': inserted})

    def _frozen_scan(self, payload):
        after = (payload.get('cursor') or 'frozen_transcripts:').removeprefix('frozen_transcripts:')
        keys = [key for key in sorted(self.frozen_transcripts) if key > after][:128]
        return _Response({'rows': [{'conversation_id': key} for key in keys],
                          'cursor': 'frozen_transcripts:' + keys[-1] if len(keys) == 128 else None})

    def _frozen_finish(self, payload):
        document = self.frozen_transcripts[payload['conversation_id']]
        document['complete'] = True
        return _Response({'complete': True, 'message_count': document['messages'],
                          'page_count': len(document['pages']), 'decoded_bytes': document['decoded_bytes']})

    def _command_receipt_v2_restore(self, payload):
        key = (payload['tenant_id'], payload['owner_user_id'], payload['command_key'])
        inserted = key not in self.command_receipts_v2
        self.command_receipts_v2.setdefault(key, dict(payload))
        return _Response({'inserted': inserted})

    def _command_receipt_v2_get(self, payload):
        key = (payload['tenant_id'], payload['owner_user_id'], payload['command_key'])
        return _Response(self.command_receipts_v2.get(key))

    def _command_receipt_v2_scan(self, payload):
        after = (payload.get('cursor') or 'receipts:').removeprefix('receipts:')
        rows = [value for value in self.command_receipts_v2.values()
                if value['tenant_id'] == payload['tenant_id']
                and value['owner_user_id'] == payload['owner_user_id']
                and value['command_key'] > after]
        rows.sort(key=lambda value: value['command_key'])
        rows = rows[:128]
        cursor = 'receipts:' + rows[-1]['command_key'] if len(rows) == 128 else None
        return _Response({'rows': [{'tenant_id': v['tenant_id'], 'owner_user_id': v['owner_user_id'],
                                    'command_key': v['command_key']} for v in rows],
                          'cursor': cursor})

    def _legacy_receipt_restore(self, payload):
        key = (payload['tenant_id'], payload['source_authority_uuid'], payload['command_key'])
        inserted = key not in self.command_receipts
        self.command_receipts.setdefault(key, dict(payload))
        return _Response({'inserted': inserted})

    def _legacy_receipt_get(self, payload):
        key = (payload['tenant_id'], payload['source_authority_uuid'], payload['command_key'])
        return _Response(self.command_receipts.get(key))

    def _legacy_receipt_scan(self, payload):
        prefix = 'legacy-receipts:' + payload['source_authority_uuid'] + ':'
        after = (payload.get('cursor') or prefix).removeprefix(prefix)
        rows = [value for value in self.command_receipts.values()
                if value['tenant_id'] == payload['tenant_id']
                and value['source_authority_uuid'] == payload['source_authority_uuid']
                and value['command_key'] > after]
        rows.sort(key=lambda value: value['command_key'])
        rows = rows[:128]
        cursor = prefix + rows[-1]['command_key'] if len(rows) == 128 else None
        return _Response({'rows': [{'tenant_id': v['tenant_id'],
                                    'source_authority_uuid': v['source_authority_uuid'],
                                    'command_key': v['command_key']} for v in rows],
                          'cursor': cursor})

    def _storage_event_restore(self, payload):
        key = (payload['task_id'], payload['sequence'])
        inserted = key not in self.storage_events
        self.storage_events.setdefault(key, dict(payload))
        return _Response({'inserted': inserted})

    def _storage_event_restore_batch(self, payload):
        rows = payload['rows']
        results = []
        for row in rows:
            results.append(self._storage_event_restore(row).payload)
        return _Response({'results': results})

    def _storage_event_get(self, payload):
        stored = self.storage_events.get((payload['task_id'], payload['sequence']))
        if stored is None:
            return _Response(None)
        # Mirror the native get contract: identity plus payload hex only.
        return _Response({key: stored[key] for key in
                          ('tenant_id', 'owner_user_id', 'task_id', 'sequence', 'payload_json_hex')})

    def _storage_event_scan(self, payload):
        prefix = 'storage_events:'
        after = (payload.get('cursor') or prefix).removeprefix(prefix)
        rows = sorted(self.storage_events)
        rows = [key for key in rows
                if (not payload.get('task_id') or key[0] == payload['task_id'])
                and (key[0] + ':' + str(key[1])) > after][:128]
        cursor = prefix + rows[-1][0] + ':' + str(rows[-1][1]) if len(rows) == 128 else None
        return _Response({'rows': [{'task_id': k[0], 'sequence': k[1]} for k in rows],
                          'cursor': cursor})

    def _sync_event_restore(self, payload):
        key = (payload['conversation_id'], payload['sync_sequence'])
        inserted = key not in self.sync_events
        self.sync_events.setdefault(key, dict(payload))
        return _Response({'inserted': inserted})

    def _sync_event_get(self, payload):
        return _Response(self.sync_events.get(
            (payload['conversation_id'], payload['sync_sequence'])))

    def _sync_event_scan(self, payload):
        prefix = 'conversation_sync_events:'
        after = (payload.get('cursor') or prefix).removeprefix(prefix)
        rows = sorted(self.sync_events)
        rows = [key for key in rows if (key[0] + ':' + str(key[1])) > after][:128]
        cursor = prefix + rows[-1][0] + ':' + str(rows[-1][1]) if len(rows) == 128 else None
        return _Response({'rows': [{'conversation_id': k[0], 'owner_user_id': self.owner_id,
                                    'sync_sequence': k[1]} for k in rows],
                          'cursor': cursor})

    def _sync_head_restore(self, payload):
        identity = payload['conversation_id']
        inserted = identity not in self.sync_heads
        self.sync_heads.setdefault(identity, dict(payload))
        return _Response({'inserted': inserted})

    def _sync_head_get(self, payload):
        return _Response(self.sync_heads.get(payload['conversation_id']))

    def _sync_head_scan(self, payload):
        prefix = 'conversation_sync_heads:'
        after = (payload.get('cursor') or prefix).removeprefix(prefix)
        rows = [key for key in sorted(self.sync_heads) if key > after][:128]
        cursor = prefix + rows[-1] if len(rows) == 128 else None
        return _Response({'rows': [{'conversation_id': k, 'owner_user_id': self.owner_id}
                                   for k in rows], 'cursor': cursor})

    def _create_credential(self, payload):
        secret = payload['secret_hash']
        credential_id = payload['credential_id']
        if credential_id in self.credentials or secret in self.secrets:
            raise StorageV2Error(9, 'database_conflict', 'exists', False)
        document = {
            'id': credential_id,
            'owner_user_id': payload['owner_user_id'],
            'account_user_id': payload.get('account_user_id') or '',
            'tenant_id': payload.get('tenant_id') or '',
            'name': payload['name'],
            'prefix': payload['prefix'],
            'scopes': sorted(set(payload['scopes'])),
            'rate_limit_rpm': payload['rate_limit_rpm'],
            'rate_limit_tpd': payload['rate_limit_tpd'],
            'created_at': float(payload['created_at']),
            'last_used_at': None,
            'expires_at': payload.get('expires_at'),
            'disabled': False,
            'revoked_at': None,
            'metadata': payload.get('metadata') or {},
        }
        self.credentials[credential_id] = document
        self.secrets[secret] = credential_id
        return _Response(document)

    def _list_credentials(self, payload):
        rows = [
            dict(item) for item in self.credentials.values()
            if item['owner_user_id'] == payload['owner_user_id']
            and item['tenant_id'] == (payload.get('tenant_id') or '')
            and item['revoked_at'] is None
        ]
        return _Response(rows)

    def _identify(self, payload):
        credential_id = self.secrets.get(payload['secret_hash'])
        if credential_id is None:
            return _Response(None)
        return _Response(dict(self.credentials[credential_id]))

    def _touch(self, payload):
        document = self.credentials[payload['credential_id']]
        used_at = float(payload['used_at'])
        if document['last_used_at'] is None or document['last_used_at'] < used_at:
            document['last_used_at'] = used_at
        return _Response(dict(document))

    def _update(self, payload):
        document = self.credentials[payload['credential_id']]
        document.update(payload['updates'])
        return _Response(dict(document))

    def _revoke(self, payload):
        document = self.credentials[payload['credential_id']]
        document['disabled'] = True
        document['revoked_at'] = float(payload['revoked_at'])
        return _Response(dict(document))

    def _create_provider(self, payload):
        provider_id = payload['provider_id']
        if provider_id in self.providers:
            raise StorageV2Error(9, 'database_conflict', 'exists', False)
        document = {
            'id': provider_id,
            'owner_user_id': payload['owner_user_id'],
            'tenant_id': payload.get('tenant_id') or '',
            'name': payload['name'],
            'base_url': payload['base_url'],
            'api_key_ciphertext': payload.get('api_key_ciphertext') or '',
            'key_hint': payload.get('key_hint') or '',
            'models': list(payload['models']),
            'extra_headers': dict(payload['extra_headers']),
            'thinking_format': payload.get('thinking_format') or '',
            'disabled': False,
            'created_at': float(payload['created_at']),
            'updated_at': float(payload['created_at']),
            'last_used_at': None,
        }
        self.providers[provider_id] = document
        return _Response(dict(document))

    def _get_provider(self, payload):
        document = self.providers.get(payload['provider_id'])
        if (document is None
                or document['owner_user_id'] != payload['owner_user_id']
                or (document.get('tenant_id') or '') != (payload.get('tenant_id') or '')):
            return _Response(None)
        return _Response(dict(document))

    def _list_providers(self, payload):
        rows = []
        for item in self.providers.values():
            if (item['owner_user_id'] != payload['owner_user_id']
                    or (item.get('tenant_id') or '') != (payload.get('tenant_id') or '')):
                continue
            public = dict(item)
            public.pop('api_key_ciphertext', None)
            rows.append(public)
        return _Response(rows)

    def _update_provider(self, payload):
        document = self.providers.get(payload['provider_id'])
        if document is None:
            return _Response(None)
        document.update(payload['updates'])
        document['updated_at'] = float(payload['updated_at'])
        return _Response(dict(document))

    def _touch_provider(self, payload):
        document = self.providers.get(payload['provider_id'])
        if document is None:
            return _Response({'touched': False})
        document['last_used_at'] = float(payload['used_at'])
        return _Response({'touched': True})

    def _record_get(self, payload):
        item = self.records.get((payload['namespace'], payload['key']))
        return _Response(None if item is None else dict(item))

    def _record_put(self, payload):
        key = (payload['namespace'], payload['key'])
        existing = self.records.get(key)
        if existing is not None and existing != payload:
            raise StorageV2Error(9, 'database_conflict', 'divergent snapshot', False)
        self.records[key] = dict(payload)
        return _Response({'inserted': existing is None})

    def _record_list(self, payload):
        def encoded(key):
            ns, key = (part.encode('utf-8') for part in key)
            return len(ns).to_bytes(2, 'big') + ns + key
        after = bytes.fromhex(payload['cursor']) if payload.get('cursor') else b''
        keys = sorted(self.records, key=encoded)
        selected = [key for key in keys if encoded(key) > after][:128]
        return _Response({'records': [{'namespace': ns, 'key': key} for ns, key in selected],
                          'cursor': encoded(selected[-1]).hex() if len(selected) == 128 else None})

    def _continuation_restore_document(self, payload):
        return {
            'record_key': payload['record_key'],
            'value_json': payload['value_json'],
            'version': payload['version'],
            'updated_at_ms': payload['updated_at_ms'],
        }

    def _continuation_get(self, payload):
        item = self.continuations.get(payload['record_key'])
        if item is None:
            return _Response(None)
        return _Response({
            'record_key': item['record_key'],
            'value_json': json.loads(item['value_json']),
            'version': item['version'],
            'updated_at_ms': item['updated_at_ms'],
        })

    def _continuation_put(self, payload):
        key = payload['record_key']
        existing = self.continuations.get(key)
        document = self._continuation_restore_document(payload)
        if existing is not None and existing != document:
            raise StorageV2Error(9, 'database_conflict', 'continuation snapshot differs', False)
        self.continuations[key] = document
        return _Response({'inserted': existing is None})

    def _continuation_scan(self, payload):
        keys = sorted(self.continuations)
        cursor = payload.get('cursor')
        if cursor:
            after = bytes.fromhex(cursor.split(':', 1)[1])
            keys = [key for key in keys if key.encode('utf-8') > after]
        page = keys[:128]
        following = ('continuations:' + page[-1].encode('utf-8').hex()
                     if len(page) == 128 else None)
        return _Response({'rows': [{'record_key': key} for key in page],
                          'cursor': following})

    def _paper_budget_restore(self, namespace):
        def handler(payload):
            if payload['record_key'] != 'capacity':
                raise StorageV2Error(3, 'invalid_input',
                                     'invalid paper budget record key', False)
            value = json.loads(payload['value_json'])
            if namespace == 'paper.document.budget.v1':
                # The native ledger always reports both counters.
                value = {'count': value.get('count', 0),
                         'associations': value.get('associations', 0)}
            document = {
                'value_json': json.dumps(value, sort_keys=True, separators=(',', ':')),
                'version': payload['version'],
                'updated_at_ms': payload['updated_at_ms'],
            }
            existing = self.paper_budgets.get(namespace)
            if existing is not None and existing != document:
                raise StorageV2Error(9, 'database_conflict',
                                     'paper budget snapshot differs', False)
            self.paper_budgets[namespace] = document
            return _Response({'inserted': existing is None})
        return handler

    def _paper_budget_get(self, namespace):
        def handler(payload):
            if payload['record_key'] != 'capacity':
                raise StorageV2Error(3, 'invalid_input',
                                     'invalid paper budget record key', False)
            item = self.paper_budgets.get(namespace)
            if item is None:
                return _Response(None)
            return _Response({
                'record_key': 'capacity',
                'value_json': item['value_json'],
                'version': item['version'],
                'updated_at_ms': item['updated_at_ms'],
            })
        return handler

    def _billing_restore(self, payload):
        document = dict(payload['document'])
        target = self.wallets if payload['table'] == 'wallets' else self.ledger
        identity = document['user_id'] if payload['table'] == 'wallets' else document['id']
        old = target.get(identity)
        if old is not None and old != document:
            raise StorageV2Error(9, 'database_conflict', 'billing snapshot differs', False)
        if payload['table'] == 'ledger' and old is None and document['ref_type'] and document['ref_id']:
            if any(all(row[k] == document[k] for k in ('user_id','kind','ref_type','ref_id'))
                   for row in target.values()):
                raise StorageV2Error(9, 'database_conflict', 'billing idempotency collision', False)
        target[identity] = document
        return _Response({'inserted': old is None})

    def _billing_get(self, payload):
        target = self.wallets if payload['table'] == 'wallets' else self.ledger
        found = target.get(payload['id'])
        return _Response(None if found is None else dict(found))

    def _billing_scan(self, payload):
        table = payload['table']
        if table in ('payments', 'payment_id_claims', 'payment_provider_claims',
                     'payment_created_index', 'payment_count'):
            return self._payment_scan(payload)
        if table in ('redeem_batches', 'redeem_locators', 'redeem_states',
                     'redeem_created_index', 'redeem_batch_created_index',
                     'redeem_count'):
            return self._redeem_scan(payload)
        if table in ('wallets', 'ledger', 'id_claims', 'time_index', 'idempotency'):
            target = self.wallets if table == 'wallets' else self.ledger
            rows = [{'id': identity} for identity, value in target.items()
                    if table != 'idempotency' or value['ref_type'] and value['ref_id']]
        elif table == 'aggregates':
            groups = {}
            for value in self.ledger.values():
                row = groups.setdefault(value['user_id'], {'user_id': value['user_id'],
                                        'count': 0, 'total_micro': 0})
                row['count'] += 1
                row['total_micro'] += value['amount_micro']
            rows = list(groups.values())
        else:
            groups = {}
            for value in self.ledger.values():
                if (value['ref_type'] != 'reserve' or not value['ref_id']
                        or value['kind'] not in ('reserve', 'reserve_release')):
                    continue
                row = groups.setdefault((value['user_id'], value['ref_id']), {
                    'user_id': value['user_id'], 'ref_id': value['ref_id'],
                    'held_micro': 0, 'last_reserve_ts': 0})
                row['held_micro'] -= value['amount_micro']
                if value['kind'] == 'reserve':
                    row['last_reserve_ts'] = max(row['last_reserve_ts'], value['ts'])
            rows = [row for row in groups.values() if table != 'reserve_age'
                    or row['held_micro'] > 0 and row['last_reserve_ts'] > 0]
        def key(row):
            return json.dumps(row.get('id') or [row['user_id'], row.get('ref_id', '')]).encode()
        rows.sort(key=key)
        cursor = payload.get('cursor')
        after = bytes.fromhex(cursor.split(':', 1)[1]) if cursor else b''
        page = [row for row in rows if key(row) > after][:128]
        following = table + ':' + key(page[-1]).hex() if len(page) == 128 else None
        return _Response({'rows': page, 'cursor': following})

    def _task_result_restore(self, payload):
        document = dict(payload)
        identity = document['task_id']
        old = self.task_results.get(identity)
        if old is not None and old != document:
            raise StorageV2Error(9, 'database_conflict', 'task result snapshot differs', False)
        self.task_results[identity] = document
        return _Response({'inserted': old is None})

    def _task_result_get(self, payload):
        found = self.task_results.get(payload['task_id'])
        if found is None:
            return _Response(None)
        return _Response({
            'task_id': found['task_id'],
            'owner_user_id': found['owner_user_id'],
            'version': found['version'],
            'updated_at_ms': found['updated_at_ms'],
            'value_json': json.loads(found['value_json']),
        })

    def _task_result_scan(self, payload):
        table = payload['table']
        if table in ('headers', 'summaries'):
            identities = sorted(self.task_results)
        elif table == 'live':
            identities = sorted(
                identity for identity, document in self.task_results.items()
                if json.loads(document['value_json']).get('status') in ('pending', 'running'))
        elif table == 'cost_index':
            def tagged(identity):
                metadata = json.loads(self.task_results[identity]['value_json']).get('metadata')
                return (isinstance(metadata, dict)
                        and isinstance(metadata.get('costExperiment'), dict)
                        and bool(metadata['costExperiment']))
            identities = sorted(identity for identity in self.task_results if tagged(identity))
        else:
            raise StorageV2Error(9, 'database_protocol_error', 'unknown table', False)
        cursor = payload.get('cursor')
        after = bytes.fromhex(cursor.split(':', 1)[1]).decode() if cursor else ''
        page = [identity for identity in identities if identity > after][:128]
        following = (
            table + ':' + page[-1].encode().hex() if len(page) == 128 else None)
        return _Response({'rows': [{'task_id': identity} for identity in page],
                          'cursor': following})

    def _tenant_restore(self, payload):
        document = dict(payload)
        identity = document['id']
        old = self.users.get(identity)
        if old is not None and old != document:
            raise StorageV2Error(9, 'database_conflict', 'tenant user snapshot differs', False)
        for other in self.users.values():
            if other['id'] == identity:
                continue
            if (other['owner_user_id'] == document['owner_user_id']
                    or other['email'] == document['email']):
                raise StorageV2Error(
                    9, 'database_conflict', 'tenant user identity differs', False)
        self.users[identity] = document
        return _Response({'inserted': old is None})

    def _tenant_get(self, payload):
        found = self.users.get(payload['id'])
        return _Response(None if found is None else dict(found))

    def _tenant_scan(self, payload):
        rows = [{'id': identity} for identity in sorted(self.users)]

        def key(row):
            return row['id'].encode()

        cursor = payload.get('cursor')
        after = bytes.fromhex(cursor.split(':', 1)[1]) if cursor else b''
        page = [row for row in rows if key(row) > after][:128]
        following = (
            'users:' + key(page[-1]).hex() if len(page) == 128 else None)
        return _Response({'rows': page, 'cursor': following})

    def _wallet_apply(self, payload):
        user_id = payload['user_id']
        ledger_id = payload['ledger_id']
        wallet = self.wallets.get(user_id) or {
            'user_id': user_id, 'balance_micro': 0, 'currency': 'CREDIT',
            'low_balance_alert_micro': 0, 'updated_at': 0,
        }
        if ledger_id in self.ledger:
            return _Response({
                'applied': False, 'duplicate': True, 'insufficient': False,
                'wallet': wallet, 'entry': self.ledger[ledger_id],
            })
        amount = int(payload['amount_micro'])
        if payload['kind'] in {'debit', 'adjust_debit', 'reserve'} and amount > 0:
            amount = -amount
        next_balance = wallet['balance_micro'] + amount
        if next_balance < 0 and not payload.get('allow_negative'):
            return _Response({
                'applied': False, 'duplicate': False, 'insufficient': True,
                'wallet': wallet,
            })
        wallet = dict(wallet)
        wallet['balance_micro'] = next_balance
        wallet['updated_at'] = int(payload['occurred_at'])
        entry = {
            'id': ledger_id, 'user_id': user_id, 'ts': int(payload['occurred_at']),
            'amount_micro': amount, 'kind': payload['kind'],
            'ref_type': payload.get('ref_type') or '',
            'ref_id': payload.get('ref_id') or '',
            'balance_after_micro': next_balance,
            'note': payload.get('note') or '',
        }
        self.wallets[user_id] = wallet
        self.ledger[ledger_id] = entry
        return _Response({
            'applied': True, 'duplicate': False, 'insufficient': False,
            'wallet': wallet, 'entry': entry,
        })

    def _wallet_get(self, payload):
        wallet = self.wallets.get(payload['user_id']) or {
            'user_id': payload['user_id'], 'balance_micro': 0, 'currency': 'CREDIT',
            'low_balance_alert_micro': 0, 'updated_at': 0,
        }
        return _Response(dict(wallet))

    def _ledger_list(self, payload):
        rows = [item for item in self.ledger.values() if item['user_id'] == payload['user_id']]
        rows.sort(key=lambda item: (-item['ts'], item['id']))
        offset = int(payload.get('offset') or 0)
        limit = int(payload.get('limit') or 1000)
        return _Response(rows[offset:offset + limit])


    def _turn_restore(self, payload):
        if payload['conversation_id'] not in self.conversations:
            raise StorageV2Error(9, 'database_protocol_error', 'conversation not found', False)
        if payload.get('current_attempt_id') is not None:
            raise StorageV2Error(9, 'database_protocol_error', 'live attempt', False)
        identity = payload['turn_id']
        old = self.turns.get(identity)
        if old is not None and old != payload:
            raise StorageV2Error(9, 'database_conflict', 'turn snapshot differs', False)
        self.turns[identity] = dict(payload)
        return _Response({'inserted': old is None})

    def _turn_get(self, payload):
        found = self.turns.get(payload['id'])
        if found is None or found['conversation_id'] != payload['conversation_id']:
            return _Response(None)
        return _Response(dict(found))

    def _turn_scan(self, payload):
        cursor = payload.get('cursor')
        after = bytes.fromhex(cursor.split(':', 1)[1]).decode() if cursor else ''
        page = [identity for identity in sorted(self.turns) if identity > after][:128]
        following = (
            'turns:' + page[-1].encode().hex() if len(page) == 128 else None)
        return _Response({'rows': [{'id': identity} for identity in page],
                          'cursor': following})

    def _conversation_view(self, document):
        metadata = {
            'id': document['id'],
            'user_id': document['user_id'],
            'title': document['title'],
            'created_at': document['created_at'],
            'updated_at': document['updated_at'],
            'settings': dict(document['settings']),
            'msg_count': document.get('msg_count', 0),
            'search_text': '',
            'rev': document.get('rev', 0),
        }
        return {
            'id': document['id'],
            'metadata': metadata,
            'messages': [],
            'source': 'sidecar',
        }

    def _restore_conversation(self, payload):
        existing = self.conversations.get(payload['id'])
        expected = {'id': payload['id'], 'user_id': payload['user_id'],
                    'title': payload['title'], 'settings': payload['settings'],
                    'created_at': payload['created_at_ms'],
                    'updated_at': payload['updated_at_ms'], 'rev': payload['rev']}
        if existing is not None:
            if any(existing.get(key) != value for key, value in expected.items()):
                raise StorageV2Error(9, 'database_conflict', 'snapshot differs', False)
        else:
            self.conversations[payload['id']] = {**expected, 'msg_count': 0}
        return _Response({'inserted': existing is None})

    def _create_conversation(self, payload):
        if payload['user_id'] != self.owner_id:
            raise StorageV2Error(9, 'database_protocol_error', 'scope', False)
        conv_id = payload['conv_id']
        if conv_id in self.conversations:
            raise StorageV2Error(9, 'database_conflict', 'exists', False)
        title = payload.get('title') or 'New Chat'
        if not isinstance(title, str):
            raise StorageV2Error(9, 'database_protocol_error', 'title', False)
        settings = payload.get('settings') or {}
        created_at = int(payload['created_at'])
        updated_at = int(payload.get('updated_at', created_at))
        self.conversations[conv_id] = {
            'id': conv_id,
            'user_id': payload['user_id'],
            'title': title[:500],
            'created_at': created_at,
            'updated_at': updated_at,
            'settings': dict(settings),
            'msg_count': 0,
            'rev': 0,
        }
        return _Response({'applied': True, 'rev': 0})

    def _get_conversation(self, payload):
        document = self.conversations.get(payload['conv_id'])
        if document is None or document['user_id'] != payload['user_id']:
            return _Response(None)
        return _Response(self._conversation_view(document))

    def _list_conversations(self, payload):
        if payload.get('catalog_page') is not True:
            raise StorageV2Error(9, 'database_protocol_error', 'catalog_page required', False)
        rows = [
            item for item in self.conversations.values()
            if item['user_id'] == payload['user_id']
        ]
        rows.sort(key=lambda item: (item['updated_at'], item['id']), reverse=True)
        before_updated = payload.get('before_updated_at')
        before_id = payload.get('before_id') or ''
        if before_updated is not None:
            rows = [
                item for item in rows
                if (item['updated_at'], item['id']) < (before_updated, before_id)
            ]
        limit = int(payload.get('limit') or 128)
        has_more = len(rows) > limit
        page = rows[:limit]
        return _Response({
            'items': [self._conversation_view(item) for item in page],
            'total_count': sum(
                1 for item in self.conversations.values()
                if item['user_id'] == payload['user_id']
            ),
            'has_more': has_more,
        })

    def _count_conversations(self, payload):
        count = sum(
            1 for item in self.conversations.values()
            if item['user_id'] == payload['user_id']
        )
        return _Response({'count': count})

    def _desktop_egress_restore(self, payload):
        document = {'agent_id': payload['agent_id'],
                    'updated_at_ms': int(payload['updated_at_ms'])}
        if self.desktop_egress is not None and self.desktop_egress != document:
            raise StorageV2Error(9, 'database_conflict', 'desktop egress snapshot differs', False)
        inserted = self.desktop_egress is None
        self.desktop_egress = document
        return _Response({'inserted': inserted})

    def _desktop_egress_get(self, payload):
        return _Response(None if self.desktop_egress is None else dict(self.desktop_egress))

    def _desktop_egress_scan(self, payload):
        rows = ([] if self.desktop_egress is None
                else [{'owner_user_id': self.owner_id}])
        return _Response({'rows': rows, 'cursor': None})

    def _daily_cost_restore(self, payload):
        document = {'date': payload['date'], 'cost': float(payload['cost']),
                    'conversations': dict(payload.get('conversations') or {}),
                    'computed_at': int(payload['computed_at'])}
        existing = self.daily_costs.get(document['date'])
        if existing is not None and existing != document:
            raise StorageV2Error(9, 'database_conflict', 'daily cost snapshot differs', False)
        self.daily_costs[document['date']] = document
        return _Response({'inserted': existing is None})

    def _daily_cost_get(self, payload):
        found = self.daily_costs.get(payload['date'])
        return _Response(None if found is None else dict(found))

    def _daily_cost_scan(self, payload):
        keys = sorted(self.daily_costs)
        cursor = payload.get('cursor')
        if cursor:
            after = cursor.split(':', 1)[1]
            keys = [key for key in keys if key > after]
        page = keys[:128]
        following = ('daily_cost:' + page[-1] if len(page) == 128 else None)
        return _Response({'rows': [{'date': key} for key in page],
                          'cursor': following})

    @staticmethod
    def _paged(keys, cursor, label, identity):
        ordered = sorted(keys)
        if cursor:
            after = cursor.split(':', 1)[1]
            ordered = [key for key in ordered if str(key) > after]
        page = ordered[:128]
        following = (label + str(page[-1]) if len(page) == 128 else None)
        return _Response({'rows': [identity(key) for key in page],
                          'cursor': following})

    def _chat_artifact_restore(self, payload):
        document = dict(payload)
        inserted = self._insert_or_match(
            self.chat_artifacts, document['id'], document,
            'chat artifact snapshot differs')
        return _Response({'inserted': inserted})

    def _chat_artifact_get(self, payload):
        found = self.chat_artifacts.get(payload['id'])
        return _Response(None if found is None else dict(found))

    def _chat_artifact_scan(self, payload):
        return self._paged(self.chat_artifacts, payload.get('cursor'),
                           'chat_artifacts:', lambda key: {'id': key})

    def _tool_result_restore(self, payload):
        document = dict(payload)
        inserted = self._insert_or_match(
            self.tool_results, document['content_sha256'], document,
            'tool result snapshot differs')
        return _Response({'inserted': inserted})

    def _tool_result_get(self, payload):
        found = self.tool_results.get(payload['content_sha256'])
        return _Response(None if found is None else dict(found))

    def _tool_result_scan(self, payload):
        return self._paged(self.tool_results, payload.get('cursor'),
                           'tool_results:',
                           lambda key: {'content_sha256': key})

    def _browser_observation_restore(self, payload):
        document = {key: payload[key] for key in (
            'origin', 'route_family', 'operation', 'observation', 'version',
            'updated_at_ms')}
        key = (document['origin'], document['route_family'], document['operation'])
        inserted = self._insert_or_match(
            self.browser_observations, key, document,
            'browser observation snapshot differs')
        touched = int(payload.get('owner_touched_at_ms') or 0)
        if touched > self.browser_owner_touched_at_ms:
            self.browser_owner_touched_at_ms = touched
            inserted = True
        return _Response({'inserted': inserted})

    def _browser_observation_get(self, payload):
        key = (payload['origin'], payload['route_family'], payload['operation'])
        found = self.browser_observations.get(key)
        if found is None:
            return _Response(None)
        # Mirror the native get: the stored snapshot only; the owner touch
        # mark is exposed at the catalog page level below.
        return _Response({key: found[key] for key in (
            'origin', 'route_family', 'operation', 'observation', 'version',
            'updated_at_ms')})

    def _browser_observation_scan(self, payload):
        keys = sorted(self.browser_observations)
        cursor = payload.get('cursor')
        if cursor:
            after = cursor.split(':', 1)[1]
            keys = [key for key in keys if repr(key) > after]
        page = keys[:128]
        following = ('browser_observations:' + repr(page[-1])
                     if len(page) == 128 else None)
        return _Response({
            'rows': [{'origin': key[0], 'route_family': key[1], 'operation': key[2]}
                     for key in page],
            'cursor': following,
            'owner_touched_at_ms': self.browser_owner_touched_at_ms,
        })

    def _routing_authority_restore(self, payload):
        document = dict(payload)
        inserted = self._insert_or_match(
            self.routing_authorities, document['tenant_id'], document,
            'model routing authority snapshot differs')
        return _Response({'inserted': inserted})

    def _routing_authority_get(self, payload):
        found = self.routing_authorities.get(payload['tenant_id'])
        return _Response(None if found is None else dict(found))

    def _routing_authority_scan(self, payload):
        return self._paged(self.routing_authorities, payload.get('cursor'),
                           'model_routing:', lambda key: {'tenant_id': key})

    def _routing_secret_restore(self, payload):
        document = dict(payload)
        key = (document['tenant_id'], document['secret_reference'])
        inserted = self._insert_or_match(
            self.routing_secrets, key, document,
            'model routing secret snapshot differs')
        return _Response({'inserted': inserted})

    def _routing_secret_get(self, payload):
        found = self.routing_secrets.get(
            (payload['tenant_id'], payload['secret_reference']))
        return _Response(None if found is None else dict(found))

    def _routing_secret_scan(self, payload):
        keys = sorted(self.routing_secrets)
        cursor = payload.get('cursor')
        if cursor:
            after = cursor.split(':', 1)[1]
            keys = [key for key in keys if repr(key) > after]
        page = keys[:128]
        following = ('model_routing:' + repr(page[-1]) if len(page) == 128 else None)
        return _Response({'rows': [{'tenant_id': key[0], 'secret_reference': key[1]}
                                   for key in page],
                          'cursor': following})

    def _plugin_manifest_restore(self, payload):
        document = dict(payload)
        inserted = self._insert_or_match(
            self.plugin_manifests, document['namespace'], document,
            'plugin manifest snapshot differs')
        return _Response({'inserted': inserted})

    def _plugin_manifest_get(self, payload):
        found = self.plugin_manifests.get(payload['namespace'])
        return _Response(None if found is None else dict(found))

    def _plugin_manifest_scan(self, payload):
        return self._paged(self.plugin_manifests, payload.get('cursor'),
                           'plugin_manifests:', lambda key: {'namespace': key})

    def _plugin_rows_restore(self, payload):
        document = dict(payload)
        key = (document['namespace'], document['table_name'], document['row_key'])
        inserted = self._insert_or_match(
            self.plugin_rows, key, document, 'plugin row snapshot differs')
        return _Response({'inserted': inserted})

    def _plugin_rows_get(self, payload):
        key = (payload['namespace'], payload['table_name'], payload['row_key'])
        found = self.plugin_rows.get(key)
        return _Response(None if found is None else dict(found))

    def _plugin_rows_scan(self, payload):
        keys = sorted(self.plugin_rows)
        cursor = payload.get('cursor')
        if cursor:
            after = cursor.split(':', 1)[1]
            keys = [key for key in keys if repr(key) > after]
        page = keys[:128]
        following = ('plugin_rows:' + repr(page[-1]) if len(page) == 128 else None)
        return _Response({'rows': [{'namespace': key[0], 'table_name': key[1],
                                    'row_key': key[2]} for key in page],
                          'cursor': following})

    def _plugin_unique_restore(self, payload):
        document = dict(payload)
        row_key = (document['namespace'], document['table_name'], document['row_key'])
        if row_key not in self.plugin_rows:
            raise StorageV2Error(3, 'database_protocol_error',
                                 'plugin unique claim references a missing row', False)
        key = (document['namespace'], document['table_name'],
               document['index_name'], document['index_value'])
        inserted = self._insert_or_match(
            self.plugin_unique, key, document, 'plugin unique claim differs')
        return _Response({'inserted': inserted})

    def _plugin_unique_get(self, payload):
        key = (payload['namespace'], payload['table_name'],
               payload['index_name'], payload['index_value'])
        found = self.plugin_unique.get(key)
        return _Response(None if found is None else dict(found))

    def _plugin_unique_scan(self, payload):
        keys = sorted(self.plugin_unique)
        cursor = payload.get('cursor')
        if cursor:
            after = cursor.split(':', 1)[1]
            keys = [key for key in keys if repr(key) > after]
        page = keys[:128]
        following = ('plugin_unique:' + repr(page[-1]) if len(page) == 128 else None)
        return _Response({'rows': [{'namespace': key[0], 'table_name': key[1],
                                    'index_name': key[2], 'index_value': key[3],
                                    'row_key': self.plugin_unique[key]['row_key']}
                                   for key in page],
                          'cursor': following})

    def _project_brain_restore(self, payload):
        document = dict(payload)
        inserted = self._insert_or_match(
            self.project_brain_projects, document['project_key'], document,
            'project brain snapshot differs')
        return _Response({'inserted': inserted})

    def _project_brain_get(self, payload):
        found = self.project_brain_projects.get(payload['project_key'])
        return _Response(None if found is None else dict(found))

    def _project_brain_scan(self, payload):
        return self._paged(self.project_brain_projects, payload.get('cursor'),
                           'project_brain:', lambda key: {'project_key': key})

    def _optimizer_restore(self, payload):
        document = dict(payload['document'])
        if payload['table'] == 'proposals':
            store = self.optimizer_proposals
        else:
            if document['proposal_id'] not in self.optimizer_proposals:
                raise StorageV2Error(4, 'database_integrity_error',
                                     'Optimizer proposal does not exist', False)
            store = self.optimizer_actions
        inserted = self._insert_or_match(
            store, document['id'], document, 'optimizer snapshot differs')
        return _Response({'inserted': inserted})

    def _optimizer_get(self, payload):
        store = (self.optimizer_proposals if payload['table'] == 'proposals'
                 else self.optimizer_actions)
        found = store.get(payload['id'])
        return _Response(None if found is None else dict(found))

    def _optimizer_scan(self, payload):
        store = (self.optimizer_proposals if payload['table'] == 'proposals'
                 else self.optimizer_actions)
        return self._paged(store, payload.get('cursor'),
                           'optimizer:', lambda key: {'id': key})

    def _log_aggregate_restore(self, payload):
        document = dict(payload)
        inserted = self._insert_or_match(
            self.log_aggregates, document['fingerprint'], document,
            'log aggregate snapshot differs')
        return _Response({'inserted': inserted})

    def _log_aggregate_get(self, payload):
        found = self.log_aggregates.get(payload['fingerprint'])
        return _Response(None if found is None else dict(found))

    def _log_aggregate_scan(self, payload):
        return self._paged(self.log_aggregates, payload.get('cursor'),
                           'log_aggregates:', lambda key: {'fingerprint': key})

    def _payment_restore(self, payload):
        document = dict(payload['document'])
        inserted = self._insert_or_match(
            self.billing_payments, document['id'], document,
            'billing payment snapshot differs')
        return _Response({'inserted': inserted})

    def _payment_get(self, payload):
        found = self.billing_payments.get(payload['id'])
        return _Response(None if found is None else dict(found))

    def _payment_scan(self, payload):
        table = payload['table']
        if table == 'payments':
            rows = [{'id': key} for key in sorted(self.billing_payments)]
        elif table == 'payment_id_claims':
            rows = [{'id': key} for key in sorted(self.billing_payments)]
        elif table == 'payment_provider_claims':
            rows = [{'id': key} for key, value in sorted(self.billing_payments.items())
                    if value['provider_id']]
        elif table == 'payment_created_index':
            rows = [{'id': key} for key, value in sorted(
                self.billing_payments.items(),
                key=lambda item: (-item[1]['created_at'], item[0]))]
        elif table == 'payment_count':
            rows = ([{'count': len(self.billing_payments)}]
                    if self.billing_payments else [])
        else:
            raise StorageV2Error(3, 'database_protocol_error', 'unknown table', False)
        return _Response({'rows': rows, 'cursor': None})

    def _redeem_restore(self, payload):
        codes = list(payload['codes'])
        record_id = codes[0]
        batch = {key: payload[key] for key in (
            'amount_micro', 'batch', 'created_by', 'created_at', 'expires_at',
            'note')}
        batch['codes'] = codes
        batch['record_id'] = record_id
        existing = self.redeem_batches.get(record_id)
        if existing is not None and existing != batch:
            raise StorageV2Error(9, 'database_conflict',
                                 'billing redeem snapshot differs', False)
        states = {state['code']: dict(state) for state in payload.get('redeemed') or []}
        if existing is None:
            for code in codes:
                if any(code in other['codes'] for other in self.redeem_batches.values()):
                    raise StorageV2Error(9, 'database_conflict',
                                         'billing redemption code exists', False)
            self.redeem_batches[record_id] = batch
            for code, state in states.items():
                if code not in codes:
                    raise StorageV2Error(3, 'database_protocol_error',
                                         'billing redeem state targets a foreign code',
                                         False)
                self.redeem_states[code] = state
        else:
            for code, state in states.items():
                found = self.redeem_states.get(code)
                if found is not None and found != state:
                    raise StorageV2Error(9, 'database_conflict',
                                         'billing redeem state differs', False)
                self.redeem_states[code] = state
        return _Response({'inserted': existing is None})

    def _redeem_get(self, payload):
        code = payload['code']
        for batch in self.redeem_batches.values():
            if code in batch['codes']:
                state = self.redeem_states.get(code)
                return _Response({
                    'code': code,
                    'amount_micro': batch['amount_micro'],
                    'batch': batch['batch'],
                    'created_by': batch['created_by'],
                    'created_at': batch['created_at'],
                    'expires_at': batch['expires_at'],
                    'redeemed_by': state['redeemed_by'] if state else '',
                    'redeemed_at': state['redeemed_at'] if state else 0,
                    'note': batch['note'],
                    'record_id': batch['record_id'],
                    'codes': list(batch['codes']),
                })
        return _Response(None)

    def _redeem_scan(self, payload):
        table = payload['table']
        if table == 'redeem_count':
            total = sum(len(batch['codes']) for batch in self.redeem_batches.values())
            return _Response({'rows': [{'count': total}] if total else [],
                              'cursor': None})
        if table == 'redeem_batches':
            rows = [[{'record_id': batch['record_id'], 'batch': batch['batch'],
                      'created_at': batch['created_at'],
                      'codes': len(batch['codes'])}]
                for batch in self.redeem_batches.values()]
            return _Response({'rows': rows, 'cursor': None})
        if table == 'redeem_locators':
            rows = [[{'code': code, 'record_id': batch['record_id']}
                     for code in batch['codes']]
                for batch in self.redeem_batches.values()]
            return _Response({'rows': rows, 'cursor': None})
        if table == 'redeem_states':
            rows = [[{'code': code, 'redeemed_by': state['redeemed_by'],
                      'redeemed_at': state['redeemed_at']}
                     for code, state in sorted(self.redeem_states.items())]]
            rows = [row for row in rows if row]
            return _Response({'rows': rows, 'cursor': None})
        raise StorageV2Error(3, 'database_protocol_error', 'unknown table', False)

    @staticmethod
    def _insert_or_match(store, key, document, conflict):
        existing = store.get(key)
        if existing is not None and existing != document:
            raise StorageV2Error(9, 'database_conflict', conflict, False)
        store[key] = document
        return existing is None

    def _paper_library_restore(self, payload):
        document = dict(payload)
        inserted = self._insert_or_match(
            self.paper_library, document['id'], document,
            'paper library snapshot differs')
        return _Response({'inserted': inserted})

    def _paper_library_get(self, payload):
        found = self.paper_library.get(payload['id'])
        return _Response(None if found is None else dict(found))

    def _paper_library_scan(self, payload):
        keys = sorted(self.paper_library)
        cursor = payload.get('cursor')
        if cursor:
            keys = [key for key in keys if key > cursor.split(':', 1)[1]]
        page = keys[:128]
        following = ('paper_library:' + page[-1] if len(page) == 128 else None)
        return _Response({'rows': [{'id': key} for key in page],
                          'cursor': following})

    def _paper_report_restore(self, payload):
        document = dict(payload)
        key = (document['paper_hash'], document['lang'])
        inserted = self._insert_or_match(
            self.paper_reports, key, document, 'paper report snapshot differs')
        return _Response({'inserted': inserted})

    def _paper_report_get(self, payload):
        found = self.paper_reports.get((payload['paper_hash'], payload['lang']))
        return _Response(None if found is None else dict(found))

    def _paper_report_scan(self, payload):
        keys = sorted(self.paper_reports)
        cursor = payload.get('cursor')
        if cursor:
            after = bytes.fromhex(cursor.split(':', 1)[1])
            keys = [key for key in keys
                    if _paper_identity_raw(key[0], key[1]) > after]
        page = keys[:128]
        following = ('paper_report:'
                     + _paper_identity_raw(*page[-1]).hex() if len(page) == 128 else None)
        return _Response({'rows': [{'paper_hash': key[0], 'lang': key[1]}
                                   for key in page],
                          'cursor': following})

    def _paper_translation_restore(self, payload):
        document = dict(payload)
        key = (document['paper_hash'], document['lang'])
        inserted = self._insert_or_match(
            self.paper_translations, key, document,
            'paper translation snapshot differs')
        return _Response({'inserted': inserted})

    def _paper_translation_get(self, payload):
        found = self.paper_translations.get((payload['paper_hash'], payload['lang']))
        return _Response(None if found is None else dict(found))

    def _paper_translation_scan(self, payload):
        keys = sorted(self.paper_translations)
        cursor = payload.get('cursor')
        if cursor:
            after = bytes.fromhex(cursor.split(':', 1)[1])
            keys = [key for key in keys
                    if _paper_identity_raw(key[0], key[1]) > after]
        page = keys[:128]
        following = ('paper_translation:'
                     + _paper_identity_raw(*page[-1]).hex() if len(page) == 128 else None)
        return _Response({'rows': [{'paper_hash': key[0], 'lang': key[1]}
                                   for key in page],
                          'cursor': following})

    def _paper_note_restore(self, payload):
        document = dict(payload)
        inserted = self._insert_or_match(
            self.paper_notes, document['id'], document,
            'paper note snapshot differs')
        return _Response({'inserted': inserted})

    def _paper_note_get(self, payload):
        found = self.paper_notes.get(payload['id'])
        return _Response(None if found is None else dict(found))

    def _paper_note_scan(self, payload):
        keys = sorted(self.paper_notes)
        cursor = payload.get('cursor')
        if cursor:
            keys = [key for key in keys if key > cursor.split(':', 1)[1]]
        page = keys[:128]
        following = ('paper_note:' + page[-1] if len(page) == 128 else None)
        return _Response({'rows': [{'id': key} for key in page],
                          'cursor': following})

    def _paper_podcast_restore(self, payload):
        document = dict(payload)
        key = (document['paper_hash'], document['mode'],
               document['lang'], document['voice'])
        inserted = self._insert_or_match(
            self.paper_podcasts, key, document, 'paper podcast snapshot differs')
        return _Response({'inserted': inserted})

    def _paper_podcast_get(self, payload):
        key = (payload['paper_hash'], payload['mode'],
               payload['lang'], payload['voice'])
        found = self.paper_podcasts.get(key)
        return _Response(None if found is None else dict(found))

    def _paper_podcast_scan(self, payload):
        keys = sorted(self.paper_podcasts)
        cursor = payload.get('cursor')
        if cursor:
            after = bytes.fromhex(cursor.split(':', 1)[1])
            keys = [key for key in keys if _podcast_identity_raw(*key) > after]
        page = keys[:128]
        following = ('paper_podcast:'
                     + _podcast_identity_raw(*page[-1]).hex() if len(page) == 128 else None)
        return _Response({'rows': [{'paper_hash': key[0], 'mode': key[1],
                                    'lang': key[2], 'voice': key[3]}
                                   for key in page],
                          'cursor': following})

    def _knowledge_settings_restore(self, payload):
        document = {'enabled': bool(payload['enabled']),
                    'visual_enrichment': bool(payload['visual_enrichment']),
                    'updated_at': float(payload['updated_at'])}
        if (self.knowledge_settings is not None
                and self.knowledge_settings != document):
            raise StorageV2Error(
                9, 'database_conflict', 'knowledge settings snapshot differs', False)
        inserted = self.knowledge_settings is None
        self.knowledge_settings = document
        return _Response({'inserted': inserted})

    def _knowledge_settings_get(self, payload):
        return _Response(None if self.knowledge_settings is None
                         else dict(self.knowledge_settings))

    def _knowledge_document_restore(self, payload):
        document = payload['document']
        document_id = document['id']
        existing = self.knowledge_documents.get(document_id)
        if existing is not None and existing != document:
            raise StorageV2Error(
                9, 'database_conflict', 'knowledge document snapshot differs', False)
        self.knowledge_documents[document_id] = document
        return _Response({'inserted': existing is None})

    def _knowledge_document_get(self, payload):
        found = self.knowledge_documents.get(payload['document_id'])
        if found is None:
            return _Response(None)
        return _Response({'document': dict(found),
                          'settings': (None if self.knowledge_settings is None
                                       else dict(self.knowledge_settings))})

    def _knowledge_catalog_scan(self, payload):
        keys = sorted(self.knowledge_documents)
        cursor = payload.get('cursor')
        if cursor:
            after = bytes.fromhex(cursor.split(':', 1)[1])
            keys = [key for key in keys
                    if _knowledge_document_raw(key) > after]
        page = keys[:128]
        following = ('knowledge:'
                     + _knowledge_document_raw(page[-1]).hex() if len(page) == 128 else None)
        rows = [{'id': key,
                 'chunk_count': self.knowledge_documents[key]['chunk_count'],
                 'asset_count': len(self.knowledge_documents[key].get('assets') or [])}
                for key in page]
        return _Response({'rows': rows, 'cursor': following})

    def _rate_limit_restore(self, payload):
        document = {'event_id': payload['event_id'],
                    'endpoint': payload['endpoint'],
                    'client_key': payload['client_key'],
                    'occurred_at_ms': int(payload['occurred_at_ms']),
                    'expires_at_ms': int(payload['expires_at_ms'])}
        existing = self.rate_limit_events.get(document['event_id'])
        if existing is not None and existing != document:
            raise StorageV2Error(9, 'database_conflict', 'rate limit snapshot differs', False)
        self.rate_limit_events[document['event_id']] = document
        return _Response({'inserted': existing is None})

    def _rate_limit_get(self, payload):
        found = self.rate_limit_events.get(payload['event_id'])
        return _Response(None if found is None else dict(found))

    def _rate_limit_scan(self, payload):
        keys = sorted(self.rate_limit_events)
        cursor = payload.get('cursor')
        if cursor:
            after = cursor.split(':', 1)[1]
            keys = [key for key in keys if key > after]
        page = keys[:128]
        following = ('rate_limit:' + page[-1] if len(page) == 128 else None)
        return _Response({'rows': [{'event_id': key} for key in page],
                          'cursor': following})

    def _queue_item_restore(self, payload):
        document = dict(payload)
        existing = self.queue_items.get(document['id'])
        if existing is not None and existing != document:
            raise StorageV2Error(9, 'database_conflict', 'queue snapshot differs', False)
        self.queue_items[document['id']] = document
        return _Response({'inserted': existing is None})

    def _queue_item_get(self, payload):
        found = self.queue_items.get(payload['id'])
        return _Response(None if found is None else dict(found))

    def _queue_item_scan(self, payload):
        keys = sorted(self.queue_items)
        cursor = payload.get('cursor')
        if cursor:
            after = cursor.split(':', 1)[1]
            keys = [key for key in keys if key > after]
        page = keys[:128]
        following = ('queue_item:' + page[-1] if len(page) == 128 else None)
        return _Response({'rows': [{'id': key} for key in page],
                          'cursor': following})

    def _autopilot_restore(self, payload):
        document = dict(payload)
        existing = self.autopilot_markers.get(document['conv_id'])
        if existing is not None and existing != document:
            raise StorageV2Error(9, 'database_conflict',
                                 'autopilot marker snapshot differs', False)
        self.autopilot_markers[document['conv_id']] = document
        return _Response({'inserted': existing is None})

    def _autopilot_get(self, payload):
        found = self.autopilot_markers.get(payload['conv_id'])
        return _Response(None if found is None else dict(found))

    def _autopilot_scan(self, payload):
        keys = sorted(self.autopilot_markers)
        cursor = payload.get('cursor')
        if cursor:
            after = cursor.split(':', 1)[1]
            keys = [key for key in keys if key > after]
        page = keys[:128]
        following = ('queue_autopilot:' + page[-1] if len(page) == 128 else None)
        return _Response({'rows': [{'conv_id': key} for key in page],
                          'cursor': following})

    def _timer_restore(self, payload):
        document = dict(payload['document'])
        timer_id = document['id']
        existing = self.timers.get(timer_id)
        if existing is not None and existing['document'] != document:
            raise StorageV2Error(9, 'database_conflict', 'timer snapshot differs', False)
        self.timers[timer_id] = {'document': document,
                                 'updated_at_ms': int(payload['updated_at_ms'])}
        return _Response({'inserted': existing is None})

    def _timer_get(self, payload):
        found = self.timers.get(payload['id'])
        if found is None:
            return _Response(None)
        return _Response({'document': dict(found['document']),
                          'updated_at_ms': found['updated_at_ms']})

    def _timer_scan(self, payload):
        keys = sorted(self.timers)
        cursor = payload.get('cursor')
        if cursor:
            after = cursor.split(':', 1)[1]
            keys = [key for key in keys if key > after]
        page = keys[:128]
        following = ('timer:' + page[-1] if len(page) == 128 else None)
        return _Response({'rows': [{'id': key} for key in page],
                          'cursor': following})

    def _timer_poll_restore(self, payload):
        timer_id = payload['timer_id']
        poll_id = int(payload['id'])
        document = dict(payload['poll'])
        document['id'] = poll_id
        document['timer_id'] = timer_id
        key = (timer_id, poll_id)
        existing = self.timer_polls.get(key)
        if existing is not None and existing != document:
            raise StorageV2Error(9, 'database_conflict',
                                 'timer poll snapshot differs', False)
        self.timer_polls[key] = document
        return _Response({'inserted': existing is None})

    def _timer_poll_get(self, payload):
        found = self.timer_polls.get((payload['timer_id'], int(payload['id'])))
        if found is None:
            return _Response(None)
        return _Response({'timer_id': payload['timer_id'],
                          'id': int(payload['id']), 'poll': dict(found)})

    def _timer_poll_scan(self, payload):
        keys = sorted(poll_id for (timer_id, poll_id) in self.timer_polls
                      if timer_id == payload['timer_id'])
        cursor = payload.get('cursor')
        if cursor is not None:
            keys = [key for key in keys if key > int(cursor)]
        page = keys[:128]
        following = (page[-1] if len(page) == 128 else None)
        return _Response({'rows': [{'id': key} for key in page],
                          'cursor': following})

    def _scheduler_task_restore(self, payload):
        document = dict(payload['document'])
        task_id = document['id']
        existing = self.scheduler_tasks.get(task_id)
        if existing is not None and existing['document'] != document:
            raise StorageV2Error(9, 'database_conflict',
                                 'scheduler snapshot differs', False)
        self.scheduler_tasks[task_id] = {
            'document': document, 'updated_at_ms': int(payload['updated_at_ms'])}
        return _Response({'inserted': existing is None})

    def _scheduler_task_get(self, payload):
        found = self.scheduler_tasks.get(payload['id'])
        if found is None:
            return _Response(None)
        return _Response({'document': dict(found['document']),
                          'updated_at_ms': found['updated_at_ms']})

    def _scheduler_task_scan(self, payload):
        keys = sorted(self.scheduler_tasks)
        cursor = payload.get('cursor')
        if cursor:
            after = cursor.split(':', 1)[1]
            keys = [key for key in keys if key > after]
        page = keys[:128]
        following = ('scheduler_task:' + page[-1] if len(page) == 128 else None)
        return _Response({'rows': [{'id': key} for key in page],
                          'cursor': following})

    def _scheduler_poll_restore(self, payload):
        task_id = payload['task_id']
        poll_id = int(payload['id'])
        document = dict(payload['poll'])
        document['id'] = poll_id
        document['task_id'] = task_id
        key = (task_id, poll_id)
        existing = self.scheduler_polls.get(key)
        if existing is not None and existing != document:
            raise StorageV2Error(9, 'database_conflict',
                                 'scheduler poll snapshot differs', False)
        self.scheduler_polls[key] = document
        return _Response({'inserted': existing is None})

    def _scheduler_poll_get(self, payload):
        found = self.scheduler_polls.get((payload['task_id'], int(payload['id'])))
        if found is None:
            return _Response(None)
        return _Response({'task_id': payload['task_id'],
                          'id': int(payload['id']), 'poll': dict(found)})

    def _scheduler_poll_scan(self, payload):
        keys = sorted(poll_id for (task_id, poll_id) in self.scheduler_polls
                      if task_id == payload['task_id'])
        cursor = payload.get('cursor')
        if cursor is not None:
            keys = [key for key in keys if key > int(cursor)]
        page = keys[:128]
        following = (page[-1] if len(page) == 128 else None)
        return _Response({'rows': [{'id': key} for key in page],
                          'cursor': following})

    def _worker_job_restore(self, payload):
        document = dict(payload)
        document['user_id'] = self.owner_id
        existing = self.worker_jobs.get(document['task_id'])
        if existing is not None and existing != document:
            raise StorageV2Error(9, 'database_conflict',
                                 'worker job snapshot differs', False)
        self.worker_jobs[document['task_id']] = document
        return _Response({'inserted': existing is None})

    def _worker_job_get(self, payload):
        found = self.worker_jobs.get(payload['task_id'])
        return _Response(None if found is None else dict(found))

    def _worker_job_scan(self, payload):
        keys = sorted(self.worker_jobs,
                      key=lambda task_id: self.worker_jobs[task_id]['idempotency_key'])
        cursor = payload.get('cursor')
        if cursor:
            after = cursor.split(':', 1)[1]
            keys = [key for key in keys
                    if self.worker_jobs[key]['idempotency_key'] > after]
        page = keys[:128]
        following = ('worker_job:' + self.worker_jobs[page[-1]]['idempotency_key']
                     if len(page) == 128 else None)
        return _Response({'rows': [{'task_id': key,
                                    'idempotency_key': self.worker_jobs[key]['idempotency_key']}
                                   for key in page],
                          'cursor': following})

    def _orch_definition_restore(self, payload):
        key = (payload['id'], payload['tenant_label'])
        document = dict(payload)
        existing = self.orch_definitions.get(key)
        if existing is not None and existing != document:
            raise StorageV2Error(9, 'database_conflict',
                                 'orchestration snapshot already differs', False)
        self.orch_definitions[key] = document
        return _Response({'inserted': existing is None})

    def _orch_definition_get(self, payload):
        found = self.orch_definitions.get((payload['id'], payload['tenant_label']))
        return _Response(None if found is None else dict(found))

    def _orch_definition_scan(self, payload):
        keys = sorted(key for key in self.orch_definitions
                      if key[1] == payload['tenant_label'])
        cursor = payload.get('cursor')
        if cursor:
            after = cursor.split(':', 1)[1]
            keys = [key for key in keys if key[0] > after]
        page = keys[:128]
        following = ('orchestration_definition:' + page[-1][0]
                     if len(page) == 128 else None)
        return _Response({'rows': [{'id': key[0]} for key in page],
                          'cursor': following})

    def _orch_run_restore(self, payload):
        key = (payload['id'], payload['tenant_label'])
        document = dict(payload)
        existing = self.orch_runs.get(key)
        if existing is not None and existing != document:
            raise StorageV2Error(9, 'database_conflict',
                                 'orchestration snapshot already differs', False)
        self.orch_runs[key] = document
        return _Response({'inserted': existing is None})

    def _orch_run_get(self, payload):
        found = self.orch_runs.get((payload['id'], payload['tenant_label']))
        return _Response(None if found is None else dict(found))

    def _orch_run_scan(self, payload):
        keys = sorted(key for key in self.orch_runs
                      if key[1] == payload['tenant_label'])
        cursor = payload.get('cursor')
        if cursor:
            after = cursor.split(':', 1)[1]
            keys = [key for key in keys if key[0] > after]
        page = keys[:128]
        following = ('orchestration_run:' + page[-1][0] if len(page) == 128 else None)
        return _Response({'rows': [{'id': key[0]} for key in page],
                          'cursor': following})

    def _orch_event_restore(self, payload):
        key = (payload['run_id'], payload['tenant_label'], int(payload['sequence']))
        document = dict(payload)
        run = self.orch_runs.get((payload['run_id'], payload['tenant_label']))
        if run is None:
            raise StorageV2Error(3, 'invalid_input',
                                 'orchestration event run is not restored', False)
        if int(payload['sequence']) >= int(run['next_event_sequence']):
            raise StorageV2Error(4, 'database_integrity_error',
                                 'orchestration event sequence exceeds the restored frontier',
                                 False)
        existing = self.orch_events.get(key)
        if existing is not None and existing != document:
            raise StorageV2Error(9, 'database_conflict',
                                 'orchestration snapshot already differs', False)
        self.orch_events[key] = document
        return _Response({'inserted': existing is None})

    def _orch_event_get(self, payload):
        key = (payload['run_id'], payload['tenant_label'], int(payload['sequence']))
        found = self.orch_events.get(key)
        return _Response(None if found is None else dict(found))

    def _orch_event_scan(self, payload):
        keys = sorted(key for key in self.orch_events
                      if key[0] == payload['run_id']
                      and key[1] == payload['tenant_label'])
        rows = [{'run_id': key[0], 'sequence': key[2]} for key in keys]
        return _Response({'rows': rows, 'cursor': None})

    def _swarm_session_restore(self, payload):
        key = payload['swarm_key']
        document = dict(payload)
        existing = self.swarm_sessions.get(key)
        if existing is not None and existing != document:
            raise StorageV2Error(9, 'database_conflict',
                                 'swarm snapshot already differs', False)
        self.swarm_sessions[key] = document
        return _Response({'inserted': existing is None})

    def _swarm_session_get(self, payload):
        found = self.swarm_sessions.get(payload['swarm_key'])
        return _Response(None if found is None else dict(found))

    def _swarm_session_scan(self, payload):
        keys = sorted(self.swarm_sessions)
        cursor = payload.get('cursor')
        if cursor:
            after = cursor.split(':', 1)[1]
            keys = [key for key in keys if key > after]
        page = keys[:128]
        following = ('swarm_session:' + page[-1] if len(page) == 128 else None)
        return _Response({'rows': [{'swarm_key': key} for key in page],
                          'cursor': following})

    def _swarm_agent_restore(self, payload):
        key = (payload['swarm_key'], payload['agent_id'])
        document = dict(payload)
        if payload['swarm_key'] not in self.swarm_sessions:
            raise StorageV2Error(3, 'invalid_input',
                                 'swarm agent session is not restored', False)
        existing = self.swarm_agents.get(key)
        if existing is not None and existing != document:
            raise StorageV2Error(9, 'database_conflict',
                                 'swarm snapshot already differs', False)
        self.swarm_agents[key] = document
        return _Response({'inserted': existing is None})

    def _swarm_agent_get(self, payload):
        found = self.swarm_agents.get((payload['swarm_key'], payload['agent_id']))
        return _Response(None if found is None else dict(found))

    def _swarm_agent_scan(self, payload):
        keys = sorted(self.swarm_agents)
        cursor = payload.get('cursor')
        if cursor:
            after = cursor.split(':', 1)[1]
            keys = [key for key in keys if key[0] + '\x00' + key[1] > after]
        page = keys[:128]
        following = ('swarm_agent:' + page[-1][0] + '\x00' + page[-1][1]
                     if len(page) == 128 else None)
        return _Response({'rows': [{'swarm_key': key[0], 'agent_id': key[1]}
                                   for key in page],
                          'cursor': following})

    def _integration_workspace_restore(self, payload):
        key = (payload['project_root'], payload['task_id'])
        document = dict(payload)
        existing = self.integration_workspaces.get(key)
        if existing is not None and existing != document:
            raise StorageV2Error(9, 'database_conflict',
                                 'integration snapshot already differs', False)
        self.integration_workspaces[key] = document
        return _Response({'inserted': existing is None})

    def _integration_workspace_get(self, payload):
        found = self.integration_workspaces.get(
            (payload['project_root'], payload['task_id']))
        return _Response(None if found is None else dict(found))

    def _integration_workspace_scan(self, payload):
        keys = sorted(self.integration_workspaces)
        cursor = payload.get('cursor')
        if cursor:
            after = cursor.split(':', 1)[1]
            keys = [key for key in keys if key[0] + '\x00' + key[1] > after]
        page = keys[:128]
        following = ('integration_workspace:' + page[-1][0] + '\x00' + page[-1][1]
                     if len(page) == 128 else None)
        return _Response({'rows': [{'project_root': key[0], 'task_id': key[1]}
                                   for key in page],
                          'cursor': following})

    def _integration_event_restore(self, payload):
        key = (payload['project_root'], int(payload['id']))
        document = dict(payload)
        existing = self.integration_events.get(key)
        if existing is not None and existing != document:
            raise StorageV2Error(9, 'database_conflict',
                                 'integration snapshot already differs', False)
        self.integration_events[key] = document
        return _Response({'inserted': existing is None})

    def _integration_event_get(self, payload):
        found = self.integration_events.get(
            (payload['project_root'], int(payload['id'])))
        if found is None:
            return _Response(None)
        return _Response({
            'id': found['id'], 'user_id': self.owner_id,
            'project_root': found['project_root'], 'task_id': found['task_id'],
            'kind': found['kind'], 'message': found['message'],
            'detail': found['detail'], 'created_at': found['created_at'],
        })

    def _integration_event_scan(self, payload):
        keys = sorted(key for key in self.integration_events
                      if key[0] == payload['project_root'])
        rows = [{
            'id': self.integration_events[key]['id'], 'user_id': self.owner_id,
            'project_root': key[0],
            'task_id': self.integration_events[key]['task_id'],
            'kind': self.integration_events[key]['kind'],
            'message': self.integration_events[key]['message'],
            'detail': self.integration_events[key]['detail'],
            'created_at': self.integration_events[key]['created_at'],
        } for key in keys]
        return _Response({'rows': rows, 'cursor': None})

    def _integration_meta_restore(self, payload):
        key = (payload['project_root'], payload['task_id'])
        workspace = self.integration_workspaces.get(key)
        if workspace is None:
            raise StorageV2Error(3, 'invalid_input',
                                 'integration meta workspace is not restored', False)
        origin = dict(workspace.get('origin') or {})
        origin.update(payload.get('origin') or {})
        if origin == (workspace.get('origin') or {}):
            return _Response({'inserted': False})
        workspace['origin'] = origin
        return _Response({'inserted': True})

def _push_text(value: str) -> bytes:
    raw = value.encode('utf-8')
    return len(raw).to_bytes(2, 'big') + raw


def _paper_identity_raw(paper_hash: str, lang: str) -> bytes:
    return _push_text(paper_hash) + _push_text(lang)


def _podcast_identity_raw(paper_hash: str, mode: str, lang: str, voice: str) -> bytes:
    return (_push_text(paper_hash) + _push_text(mode)
            + _push_text(lang) + _push_text(voice))


def _knowledge_document_raw(document_id: str) -> bytes:
    return _push_text(document_id)


def _secret(label: str) -> str:
    return hashlib.sha256(label.encode('ascii')).hexdigest()


def _seed_source(
    path: Path, *,
    credentials: list[dict] | None = None,
    providers: list[dict] | None = None,
    records: list[dict] | None = None,
    ledger: list[dict] | None = None,
    wallets: list[dict] | None = None,
    users: list[dict] | None = None,
    conversations: list[dict] | None = None,
    turns: list[dict] | None = None,
    desktop_egress: list[dict] | None = None,
    daily_costs: list[dict] | None = None,
    rate_limit_events: list[dict] | None = None,
    orchestration_definitions: list[dict] | None = None,
    orchestration_runs: list[dict] | None = None,
    orchestration_events: list[dict] | None = None,
    swarm_sessions: list[dict] | None = None,
    swarm_agents: list[dict] | None = None,
    integration_workspaces: list[dict] | None = None,
    integration_events: list[dict] | None = None,
    integration_meta: list[dict] | None = None,
    paper_library: list[dict] | None = None,
    paper_reports: list[dict] | None = None,
    paper_translations: list[dict] | None = None,
    paper_notes: list[dict] | None = None,
    paper_podcasts: list[dict] | None = None,
    knowledge_documents: list[dict] | None = None,
    knowledge_chunks: list[dict] | None = None,
    knowledge_assets: list[dict] | None = None,
    knowledge_chunk_assets: list[dict] | None = None,
    knowledge_terms: list[dict] | None = None,
    knowledge_settings: list[dict] | None = None,
    chat_artifacts: list[dict] | None = None,
    tool_result_artifacts: list[dict] | None = None,
    browser_observations: list[dict] | None = None,
    browser_observation_owners: list[dict] | None = None,
    routing_authorities: list[dict] | None = None,
    routing_secrets: list[dict] | None = None,
    plugin_manifests: list[dict] | None = None,
    plugin_rows: list[dict] | None = None,
    plugin_unique_values: list[dict] | None = None,
    project_brain_projects: list[dict] | None = None,
    optimizer_proposals: list[dict] | None = None,
    optimizer_actions: list[dict] | None = None,
    log_aggregates: list[dict] | None = None,
    billing_payments: list[dict] | None = None,
    billing_redeem_codes: list[dict] | None = None,
    queue_items: list[dict] | None = None,
    autopilot_markers: list[dict] | None = None,
    timers: list[dict] | None = None,
    timer_poll_log: list[dict] | None = None,
    scheduled_tasks: list[dict] | None = None,
    proactive_poll_log: list[dict] | None = None,
    worker_jobs: list[dict] | None = None,
    sync_events: list[dict] | None = None,
    sync_heads: list[dict] | None = None,
    legacy_project_brain: dict | None = None,
) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        'CREATE TABLE storage_meta (meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL)')
    connection.execute(
        'INSERT INTO storage_meta VALUES (?, ?)', ('schema_version', str(SCHEMA_VERSION)))
    connection.execute(
        'CREATE TABLE auth_credentials ('
        'id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL, account_user_id TEXT NOT NULL,'
        'tenant_id TEXT NOT NULL, name TEXT NOT NULL, prefix TEXT NOT NULL,'
        'secret_hash TEXT NOT NULL UNIQUE, scopes TEXT NOT NULL,'
        'rate_limit_rpm INTEGER NOT NULL, rate_limit_tpd INTEGER NOT NULL,'
        'created_at REAL NOT NULL, last_used_at REAL, expires_at REAL,'
        'disabled INTEGER NOT NULL, revoked_at REAL, metadata TEXT NOT NULL)')
    connection.execute(
        'CREATE TABLE storage_records ('
        'namespace TEXT NOT NULL, record_key TEXT NOT NULL, value_json TEXT NOT NULL,'
        'version INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL,'
        'PRIMARY KEY (namespace, record_key))')
    connection.execute(
        'CREATE TABLE billing_wallets ('
        'user_id TEXT PRIMARY KEY, balance_micro INTEGER NOT NULL, currency TEXT NOT NULL,'
        'low_balance_alert_micro INTEGER NOT NULL, updated_at INTEGER NOT NULL)')
    connection.execute(
        'CREATE TABLE billing_ledger ('
        'id TEXT PRIMARY KEY, user_id TEXT NOT NULL, ts INTEGER NOT NULL,'
        'amount_micro INTEGER NOT NULL, kind TEXT NOT NULL, ref_type TEXT NOT NULL,'
        'ref_id TEXT NOT NULL, balance_after_micro INTEGER NOT NULL, note TEXT NOT NULL)')
    connection.execute('CREATE INDEX idx_ledger_user_ts ON billing_ledger(user_id, ts DESC)')
    connection.execute("CREATE INDEX idx_ledger_reserve_sweep ON billing_ledger(user_id, ref_id) WHERE ref_type='reserve'")
    if conversations is not None:
        connection.execute(
            'CREATE TABLE storage_conversations ('
            'id TEXT NOT NULL, user_id INTEGER NOT NULL, title TEXT NOT NULL,'
            'created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL,'
            "settings_json TEXT NOT NULL, messages_json TEXT NOT NULL DEFAULT '[]', rev INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (id, user_id))")
        for row in conversations:
            connection.execute(
                'INSERT INTO storage_conversations (id,user_id,title,created_at_ms,updated_at_ms,settings_json,rev) VALUES (?,?,?,?,?,?,?)',
                (
                    row['id'], row['user_id'], row['title'],
                    row['created_at_ms'], row['updated_at_ms'],
                    json.dumps(row.get('settings') or {}), row.get('rev', 0),
                ),
            )
    if turns is not None:
        connection.execute(
            'CREATE TABLE storage_conversation_turns ('
            'turn_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,'
            'user_id INTEGER NOT NULL, presentation_id TEXT NOT NULL,'
            'lane_id TEXT NOT NULL, parent_turn_id TEXT, ordinal INTEGER NOT NULL,'
            'actor TEXT NOT NULL, kind TEXT NOT NULL, run_id TEXT NOT NULL,'
            'status TEXT NOT NULL, current_attempt_id TEXT,'
            'projection_json TEXT NOT NULL, projection_revision INTEGER NOT NULL,'
            'settlement_json TEXT NOT NULL, created_at INTEGER NOT NULL,'
            'updated_at INTEGER NOT NULL)')
        for row in turns:
            connection.execute(
                'INSERT INTO storage_conversation_turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (
                    row['turn_id'], row['conversation_id'], row['user_id'],
                    row.get('presentation_id', ''), row.get('lane_id', 'main'),
                    row.get('parent_turn_id'), row['ordinal'], row['actor'],
                    row.get('kind', 'reply'), row.get('run_id', ''),
                    row.get('status', 'settled'), row.get('current_attempt_id'),
                    json.dumps(row.get('projection') or {}),
                    row.get('projection_revision', 1),
                    json.dumps(row.get('settlement') or {}),
                    row['created_at'], row['updated_at'],
                ),
            )
    if users is not None:
        connection.execute(
            'CREATE TABLE tenant_users ('
            'id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL UNIQUE,'
            'email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,'
            'display_name TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL,'
            'created_at INTEGER NOT NULL, last_login_at INTEGER NOT NULL,'
            'email_verified INTEGER NOT NULL, metadata TEXT NOT NULL)')
        for row in users:
            connection.execute(
                'INSERT INTO tenant_users VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (
                    row['id'], row['owner_user_id'], row['email'],
                    row.get('password_hash', ''), row.get('display_name', ''),
                    row['role'], row['status'], row['created_at'],
                    row.get('last_login_at', 0),
                    int(bool(row.get('email_verified'))),
                    json.dumps(row.get('metadata') or {}),
                ),
            )
    for row in credentials or []:
        connection.execute(
            'INSERT INTO auth_credentials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                row['id'], row['owner_user_id'], row.get('account_user_id', ''),
                row.get('tenant_id', ''), row['name'], row['prefix'], row['secret_hash'],
                json.dumps(row['scopes']), row.get('rate_limit_rpm', 60),
                row.get('rate_limit_tpd', 1000), row['created_at'],
                row.get('last_used_at'), row.get('expires_at'),
                int(bool(row.get('disabled'))), row.get('revoked_at'),
                json.dumps(row.get('metadata') or {}),
            ),
        )
    if providers is not None:
        connection.execute(
            'CREATE TABLE byo_providers ('
            'id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL, tenant_id TEXT NOT NULL,'
            'name TEXT NOT NULL, base_url TEXT NOT NULL, api_key_ciphertext TEXT NOT NULL,'
            'key_hint TEXT NOT NULL, models_json TEXT NOT NULL, extra_headers_json TEXT NOT NULL,'
            'thinking_format TEXT NOT NULL, disabled INTEGER NOT NULL,'
            'created_at REAL NOT NULL, updated_at REAL NOT NULL, last_used_at REAL)')
        for row in providers:
            connection.execute(
                'INSERT INTO byo_providers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (
                    row['id'], row['owner_user_id'], row.get('tenant_id', ''),
                    row['name'], row['base_url'], row['api_key_ciphertext'],
                    row.get('key_hint', ''), json.dumps(row.get('models') or []),
                    json.dumps(row.get('extra_headers') or {}),
                    row.get('thinking_format', ''), int(bool(row.get('disabled'))),
                    row['created_at'], row['updated_at'], row.get('last_used_at'),
                ),
            )
    for row in records or []:
        connection.execute(
            'INSERT INTO storage_records VALUES (?,?,?,?,?)',
            (
                row['namespace'], row['record_key'], row.get('value_json', json.dumps(row.get('value'))),
                row.get('version', 1), row.get('updated_at_ms', 1_700_000_000_000),
            ),
        )
    for row in wallets or []:
        connection.execute(
            'INSERT INTO billing_wallets VALUES (?,?,?,?,?)',
            (
                row['user_id'], row['balance_micro'], row.get('currency', 'CREDIT'),
                row.get('low_balance_alert_micro', 0), row.get('updated_at', 0),
            ),
        )
    for row in ledger or []:
        connection.execute(
            'INSERT INTO billing_ledger VALUES (?,?,?,?,?,?,?,?,?)',
            (
                row['id'], row['user_id'], row['ts'], row['amount_micro'], row['kind'],
                row.get('ref_type', ''), row.get('ref_id', ''),
                row['balance_after_micro'], row.get('note', ''),
            ),
        )
    if desktop_egress is not None:
        connection.execute(
            'CREATE TABLE storage_desktop_egress_preferences ('
            'owner_user_id INTEGER PRIMARY KEY, agent_id TEXT NOT NULL,'
            'updated_at_ms INTEGER NOT NULL)')
        for row in desktop_egress:
            connection.execute(
                'INSERT INTO storage_desktop_egress_preferences VALUES (?,?,?)',
                (row['owner_user_id'], row['agent_id'], row['updated_at_ms']),
            )
    if daily_costs is not None:
        connection.execute(
            'CREATE TABLE daily_cost_cache ('
            'user_id INTEGER NOT NULL, date TEXT NOT NULL, cost REAL NOT NULL,'
            'conversations_json TEXT NOT NULL, computed_at INTEGER NOT NULL,'
            'PRIMARY KEY (user_id, date))')
        for row in daily_costs:
            connection.execute(
                'INSERT INTO daily_cost_cache VALUES (?,?,?,?,?)',
                (row['user_id'], row['date'], row['cost'],
                 json.dumps(row.get('conversations') or {}), row['computed_at']),
            )
    if rate_limit_events is not None:
        connection.execute(
            'CREATE TABLE storage_rate_limit_events ('
            'event_id TEXT PRIMARY KEY, endpoint TEXT NOT NULL,'
            'client_key TEXT NOT NULL, occurred_at_ms INTEGER NOT NULL,'
            'expires_at_ms INTEGER NOT NULL)')
        for row in rate_limit_events:
            connection.execute(
                'INSERT INTO storage_rate_limit_events VALUES (?,?,?,?,?)',
                (row['event_id'], row['endpoint'], row['client_key'],
                 row['occurred_at_ms'], row['expires_at_ms']),
            )
    if orchestration_definitions is not None:
        connection.execute(
            'CREATE TABLE orchestration_definitions ('
            'id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,'
            'tenant_id TEXT NOT NULL DEFAULT \'\', name TEXT NOT NULL DEFAULT \'\','
            'definition_json TEXT NOT NULL, created_at_ms INTEGER NOT NULL,'
            'updated_at_ms INTEGER NOT NULL)')
        for row in orchestration_definitions:
            connection.execute(
                'INSERT INTO orchestration_definitions VALUES (?,?,?,?,?,?,?)',
                (row['id'], row['user_id'], row.get('tenant_id', ''),
                 row.get('name', ''), json.dumps(row.get('definition') or {}),
                 row['created_at_ms'], row['updated_at_ms']),
            )
    if orchestration_runs is not None:
        connection.execute(
            'CREATE TABLE orchestration_runs ('
            'id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,'
            'tenant_id TEXT NOT NULL DEFAULT \'\', orch_id TEXT NOT NULL DEFAULT \'\','
            'name TEXT NOT NULL DEFAULT \'\', definition TEXT NOT NULL DEFAULT \'{}\','
            'input TEXT NOT NULL DEFAULT \'\', status TEXT NOT NULL DEFAULT \'pending\','
            'final TEXT NOT NULL DEFAULT \'\', error TEXT NOT NULL DEFAULT \'\','
            'created_by TEXT NOT NULL DEFAULT \'\', created_at INTEGER NOT NULL DEFAULT 0,'
            'updated_at INTEGER NOT NULL DEFAULT 0, finished_at INTEGER NOT NULL DEFAULT 0)')
        for row in orchestration_runs:
            connection.execute(
                'INSERT INTO orchestration_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['id'], row['user_id'], row.get('tenant_id', ''),
                 row.get('orch_id', ''), row.get('name', ''),
                 json.dumps(row.get('definition') or {}), row.get('input', ''),
                 row.get('status', 'pending'), row.get('final', ''),
                 row.get('error', ''), row.get('created_by', ''),
                 row['created_at'], row['updated_at'], row.get('finished_at', 0)),
            )
    if orchestration_events is not None:
        connection.execute(
            'CREATE TABLE orchestration_run_events ('
            'run_id TEXT NOT NULL, user_id INTEGER NOT NULL,'
            'tenant_id TEXT NOT NULL DEFAULT \'\', seq INTEGER NOT NULL,'
            'type TEXT NOT NULL DEFAULT \'\', node_id TEXT NOT NULL DEFAULT \'\','
            'payload TEXT NOT NULL DEFAULT \'{}\', ts INTEGER NOT NULL DEFAULT 0,'
            'PRIMARY KEY (run_id, user_id, tenant_id, seq))')
        for row in orchestration_events:
            connection.execute(
                'INSERT INTO orchestration_run_events VALUES (?,?,?,?,?,?,?,?)',
                (row['run_id'], row['user_id'], row.get('tenant_id', ''),
                 row['seq'], row.get('type', ''), row.get('node_id', ''),
                 json.dumps(row.get('payload') or {}), row.get('ts', 0)),
            )
    if swarm_sessions is not None:
        connection.execute(
            'CREATE TABLE swarm_sessions ('
            'swarm_key TEXT PRIMARY KEY, conv_id TEXT NOT NULL DEFAULT \'\','
            'task_id TEXT NOT NULL DEFAULT \'\', status TEXT NOT NULL DEFAULT \'running\','
            'specs_json TEXT NOT NULL DEFAULT \'[]\', config_json TEXT NOT NULL DEFAULT \'{}\','
            'created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0)')
        for row in swarm_sessions:
            connection.execute(
                'INSERT INTO swarm_sessions VALUES (?,?,?,?,?,?,?,?)',
                (row['swarm_key'], row.get('conv_id', ''), row.get('task_id', ''),
                 row.get('status', 'running'), json.dumps(row.get('specs') or []),
                 json.dumps(row.get('config') or {}),
                 row['created_at'], row['updated_at']),
            )
    if swarm_agents is not None:
        connection.execute(
            'CREATE TABLE swarm_agents ('
            'swarm_key TEXT NOT NULL, agent_id TEXT NOT NULL,'
            'role TEXT NOT NULL DEFAULT \'\', objective TEXT NOT NULL DEFAULT \'\','
            'status TEXT NOT NULL DEFAULT \'pending\','
            'messages_json TEXT NOT NULL DEFAULT \'[]\','
            'result_json TEXT NOT NULL DEFAULT \'{}\','
            'rounds_used INTEGER NOT NULL DEFAULT 0,'
            'delivered INTEGER NOT NULL DEFAULT 0,'
            'updated_at INTEGER NOT NULL DEFAULT 0,'
            'PRIMARY KEY (swarm_key, agent_id))')
        for row in swarm_agents:
            connection.execute(
                'INSERT INTO swarm_agents VALUES (?,?,?,?,?,?,?,?,?,?)',
                (row['swarm_key'], row['agent_id'], row.get('role', ''),
                 row.get('objective', ''), row.get('status', 'pending'),
                 json.dumps(row.get('messages') or []),
                 json.dumps(row.get('result') or {}),
                 row.get('rounds_used', 0), int(bool(row.get('delivered'))),
                 row['updated_at']),
            )
    if integration_workspaces is not None:
        connection.execute(
            'CREATE TABLE integration_workspaces ('
            'id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,'
            'project_root TEXT NOT NULL, task_id TEXT NOT NULL,'
            'title TEXT NOT NULL DEFAULT \'\', workspace_path TEXT NOT NULL,'
            'managed INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT \'running\','
            'base_sha TEXT NOT NULL DEFAULT \'\', checkpoint_sha TEXT NOT NULL DEFAULT \'\','
            'candidate_sha TEXT NOT NULL DEFAULT \'\', error TEXT NOT NULL DEFAULT \'\','
            'created_at REAL NOT NULL, updated_at REAL NOT NULL)')
        for row in integration_workspaces:
            connection.execute(
                'INSERT INTO integration_workspaces VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['id'], row['user_id'], row['project_root'], row['task_id'],
                 row.get('title', ''), row.get('workspace_path', '/w'),
                 int(bool(row.get('managed'))), row.get('state', 'running'),
                 row.get('base_sha', ''), row.get('checkpoint_sha', ''),
                 row.get('candidate_sha', ''), row.get('error', ''),
                 row['created_at'], row['updated_at']),
            )
    if integration_events is not None:
        connection.execute(
            'CREATE TABLE integration_events ('
            'id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,'
            'project_root TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT \'\','
            'kind TEXT NOT NULL, message TEXT NOT NULL DEFAULT \'\','
            'detail TEXT NOT NULL DEFAULT \'\', created_at REAL NOT NULL)')
        for row in integration_events:
            connection.execute(
                'INSERT INTO integration_events VALUES (?,?,?,?,?,?,?,?)',
                (row['id'], row['user_id'], row['project_root'],
                 row.get('task_id', ''), row['kind'], row.get('message', ''),
                 row.get('detail', ''), row['created_at']),
            )
    if integration_meta is not None:
        connection.execute(
            'CREATE TABLE integration_workspace_meta ('
            'user_id INTEGER NOT NULL, project_root TEXT NOT NULL,'
            'task_id TEXT NOT NULL, origin_json TEXT NOT NULL DEFAULT \'{}\','
            'updated_at REAL NOT NULL,'
            'PRIMARY KEY (user_id, project_root, task_id))')
        for row in integration_meta:
            connection.execute(
                'INSERT INTO integration_workspace_meta VALUES (?,?,?,?,?)',
                (row['user_id'], row['project_root'], row['task_id'],
                 json.dumps(row.get('origin') or {}), row['updated_at']),
            )
    if paper_library is not None:
        connection.execute(
            'CREATE TABLE paper_library ('
            'id TEXT NOT NULL, user_id INTEGER NOT NULL, title TEXT NOT NULL,'
            'pdf_url TEXT NOT NULL, pdf_filename TEXT NOT NULL,'
            'arxiv_id TEXT NOT NULL, paper_hash TEXT NOT NULL,'
            'parsed_text TEXT NOT NULL, parser_version TEXT NOT NULL,'
            'qa_history TEXT NOT NULL, images TEXT NOT NULL,'
            'babel_cache TEXT NOT NULL, page_count INTEGER NOT NULL,'
            'folder_id TEXT NOT NULL, created_at INTEGER NOT NULL,'
            'updated_at INTEGER NOT NULL, PRIMARY KEY (id, user_id))')
        for row in paper_library:
            connection.execute(
                'INSERT INTO paper_library VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['id'], row['user_id'], row['title'], row['pdf_url'],
                 row['pdf_filename'], row['arxiv_id'], row['paper_hash'],
                 row['parsed_text'], row['parser_version'], row['qa_history'],
                 row['images'], row['babel_cache'], row['page_count'],
                 row['folder_id'], row['created_at'], row['updated_at']),
            )
    if paper_reports is not None:
        connection.execute(
            'CREATE TABLE paper_reports ('
            'user_id INTEGER NOT NULL, paper_hash TEXT NOT NULL,'
            'lang TEXT NOT NULL, report TEXT NOT NULL, model TEXT NOT NULL,'
            'meta TEXT NOT NULL, created_at INTEGER NOT NULL,'
            'PRIMARY KEY (user_id, paper_hash, lang))')
        for row in paper_reports:
            connection.execute(
                'INSERT INTO paper_reports VALUES (?,?,?,?,?,?,?)',
                (row['user_id'], row['paper_hash'], row['lang'], row['report'],
                 row['model'], row['meta'], row['created_at']),
            )
    if paper_translations is not None:
        connection.execute(
            'CREATE TABLE paper_translations ('
            'user_id INTEGER NOT NULL, paper_hash TEXT NOT NULL,'
            'lang TEXT NOT NULL, text TEXT NOT NULL, model TEXT NOT NULL,'
            'created_at INTEGER NOT NULL,'
            'PRIMARY KEY (user_id, paper_hash, lang))')
        for row in paper_translations:
            connection.execute(
                'INSERT INTO paper_translations VALUES (?,?,?,?,?,?)',
                (row['user_id'], row['paper_hash'], row['lang'], row['text'],
                 row['model'], row['created_at']),
            )
    if paper_notes is not None:
        connection.execute(
            'CREATE TABLE paper_notes ('
            'user_id INTEGER NOT NULL, id TEXT NOT NULL,'
            'paper_hash TEXT NOT NULL, lang TEXT NOT NULL,'
            'anchor TEXT NOT NULL, note TEXT NOT NULL,'
            'created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,'
            'PRIMARY KEY (user_id, id))')
        for row in paper_notes:
            connection.execute(
                'INSERT INTO paper_notes VALUES (?,?,?,?,?,?,?,?)',
                (row['user_id'], row['id'], row['paper_hash'], row['lang'],
                 row['anchor'], row['note'], row['created_at'], row['updated_at']),
            )
    if paper_podcasts is not None:
        connection.execute(
            'CREATE TABLE paper_podcasts ('
            'user_id INTEGER NOT NULL, paper_hash TEXT NOT NULL,'
            'mode TEXT NOT NULL, lang TEXT NOT NULL, voice TEXT NOT NULL,'
            'status TEXT NOT NULL, script_json TEXT NOT NULL,'
            'file_path TEXT NOT NULL, duration_sec REAL NOT NULL,'
            'model TEXT NOT NULL, tts_model TEXT NOT NULL, meta TEXT NOT NULL,'
            'created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,'
            'PRIMARY KEY (user_id, paper_hash, mode, lang, voice))')
        for row in paper_podcasts:
            connection.execute(
                'INSERT INTO paper_podcasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['user_id'], row['paper_hash'], row['mode'], row['lang'],
                 row['voice'], row['status'], row['script_json'], row['file_path'],
                 row['duration_sec'], row['model'], row['tts_model'], row['meta'],
                 row['created_at'], row['updated_at']),
            )
    if knowledge_documents is not None:
        connection.execute(
            'CREATE TABLE storage_knowledge_documents ('
            'user_id INTEGER NOT NULL, id TEXT NOT NULL, sha256 TEXT NOT NULL,'
            'name TEXT NOT NULL, stored_name TEXT NOT NULL, kind TEXT NOT NULL,'
            'size_bytes INTEGER NOT NULL, method TEXT NOT NULL,'
            'warnings_json TEXT NOT NULL, text_chars INTEGER NOT NULL,'
            'chunk_count INTEGER NOT NULL, pages INTEGER NOT NULL,'
            'scope TEXT NOT NULL, media_metadata_json TEXT NOT NULL,'
            'created_at REAL NOT NULL, updated_at REAL NOT NULL,'
            'PRIMARY KEY (user_id, id))')
        for row in knowledge_documents:
            connection.execute(
                'INSERT INTO storage_knowledge_documents '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['user_id'], row['id'], row['sha256'], row['name'],
                 row['stored_name'], row['kind'], row['size_bytes'], row['method'],
                 row['warnings_json'], row['text_chars'], row['chunk_count'],
                 row['pages'], row['scope'], row['media_metadata_json'],
                 row['created_at'], row['updated_at']),
            )
    if knowledge_chunks is not None:
        connection.execute(
            'CREATE TABLE storage_knowledge_chunks ('
            'user_id INTEGER NOT NULL, document_id TEXT NOT NULL,'
            'ordinal INTEGER NOT NULL, section TEXT NOT NULL,'
            'location TEXT NOT NULL, content TEXT NOT NULL,'
            'search_text TEXT NOT NULL,'
            'PRIMARY KEY (user_id, document_id, ordinal))')
        for row in knowledge_chunks:
            connection.execute(
                'INSERT INTO storage_knowledge_chunks VALUES (?,?,?,?,?,?,?)',
                (row['user_id'], row['document_id'], row['ordinal'],
                 row['section'], row['location'], row['content'],
                 row['search_text']),
            )
    if knowledge_assets is not None:
        connection.execute(
            'CREATE TABLE storage_knowledge_assets ('
            'user_id INTEGER NOT NULL, id TEXT NOT NULL,'
            'document_id TEXT NOT NULL, ordinal INTEGER NOT NULL,'
            'kind TEXT NOT NULL, stored_name TEXT NOT NULL,'
            'mime_type TEXT NOT NULL, sha256 TEXT NOT NULL,'
            'size_bytes INTEGER NOT NULL, width INTEGER NOT NULL,'
            'height INTEGER NOT NULL, page INTEGER NOT NULL,'
            'pages_json TEXT NOT NULL, bbox_json TEXT NOT NULL,'
            'caption TEXT NOT NULL, ocr_text TEXT NOT NULL,'
            'description TEXT NOT NULL, enrichment_status TEXT NOT NULL,'
            'enrichment_model TEXT NOT NULL, enrichment_error TEXT NOT NULL,'
            'metadata_json TEXT NOT NULL, created_at REAL NOT NULL,'
            'updated_at REAL NOT NULL, PRIMARY KEY (user_id, id))')
        for row in knowledge_assets:
            connection.execute(
                'INSERT INTO storage_knowledge_assets '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['user_id'], row['id'], row['document_id'], row['ordinal'],
                 row['kind'], row['stored_name'], row['mime_type'], row['sha256'],
                 row['size_bytes'], row['width'], row['height'], row['page'],
                 row['pages_json'], row['bbox_json'], row['caption'],
                 row['ocr_text'], row['description'], row['enrichment_status'],
                 row['enrichment_model'], row['enrichment_error'],
                 row['metadata_json'], row['created_at'], row['updated_at']),
            )
    if knowledge_chunk_assets is not None:
        connection.execute(
            'CREATE TABLE storage_knowledge_chunk_assets ('
            'user_id INTEGER NOT NULL, document_id TEXT NOT NULL,'
            'chunk_ordinal INTEGER NOT NULL, asset_id TEXT NOT NULL,'
            'relation TEXT NOT NULL, ordinal INTEGER NOT NULL,'
            'PRIMARY KEY (user_id, document_id, chunk_ordinal, asset_id, relation))')
        for row in knowledge_chunk_assets:
            connection.execute(
                'INSERT INTO storage_knowledge_chunk_assets VALUES (?,?,?,?,?,?)',
                (row['user_id'], row['document_id'], row['chunk_ordinal'],
                 row['asset_id'], row['relation'], row['ordinal']),
            )
    if knowledge_terms is not None:
        connection.execute(
            'CREATE TABLE storage_knowledge_terms ('
            'user_id INTEGER NOT NULL, term TEXT NOT NULL,'
            'document_id TEXT NOT NULL, chunk_ordinal INTEGER NOT NULL,'
            'PRIMARY KEY (user_id, term, document_id, chunk_ordinal))')
        for row in knowledge_terms:
            connection.execute(
                'INSERT INTO storage_knowledge_terms VALUES (?,?,?,?)',
                (row['user_id'], row['term'], row['document_id'],
                 row['chunk_ordinal']),
            )
    if knowledge_settings is not None:
        connection.execute(
            'CREATE TABLE storage_knowledge_settings ('
            'user_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL,'
            'visual_enrichment INTEGER NOT NULL, updated_at REAL NOT NULL)')
        for row in knowledge_settings:
            connection.execute(
                'INSERT INTO storage_knowledge_settings VALUES (?,?,?,?)',
                (row['user_id'], row['enabled'], row['visual_enrichment'],
                 row['updated_at']),
            )
    if chat_artifacts is not None:
        connection.execute(
            'CREATE TABLE chat_artifacts ('
            'id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, task_id TEXT NOT NULL,'
            'msg_id TEXT NOT NULL, source TEXT NOT NULL, source_ref TEXT NOT NULL,'
            'format TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,'
            'content_sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,'
            'version INTEGER NOT NULL, parent_id TEXT NOT NULL,'
            'pinned INTEGER NOT NULL, meta TEXT NOT NULL, created_at INTEGER NOT NULL,'
            'deleted_at INTEGER NOT NULL)')
        for row in chat_artifacts:
            connection.execute(
                'INSERT INTO chat_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['id'], row['conv_id'], row.get('task_id', ''),
                 row.get('msg_id', ''), row['source'],
                 json.dumps(row.get('source_ref') or {}), row['format'],
                 row.get('title', ''), row['content'], row['content_sha256'],
                 row['size_bytes'], row['version'], row.get('parent_id', ''),
                 int(bool(row.get('pinned'))), json.dumps(row.get('meta') or {}),
                 row['created_at'], row.get('deleted_at', 0)),
            )
    if tool_result_artifacts is not None:
        connection.execute(
            'CREATE TABLE tool_result_artifacts ('
            'user_id INTEGER NOT NULL, content_sha256 TEXT NOT NULL,'
            'content TEXT NOT NULL, media_type TEXT NOT NULL,'
            'size_bytes INTEGER NOT NULL, created_at_ms INTEGER NOT NULL,'
            'expires_at_ms INTEGER NOT NULL, last_accessed_at_ms INTEGER NOT NULL,'
            'PRIMARY KEY (user_id, content_sha256))')
        for row in tool_result_artifacts:
            connection.execute(
                'INSERT INTO tool_result_artifacts VALUES (?,?,?,?,?,?,?,?)',
                (row['user_id'], row['content_sha256'], row['content'],
                 row['media_type'], row['size_bytes'], row['created_at_ms'],
                 row['expires_at_ms'], row['last_accessed_at_ms']),
            )
    if browser_observations is not None:
        connection.execute(
            'CREATE TABLE storage_browser_site_observations ('
            'owner_user_id INTEGER NOT NULL, origin TEXT NOT NULL,'
            'route_family TEXT NOT NULL, operation TEXT NOT NULL,'
            'schema_version INTEGER NOT NULL, strategy TEXT NOT NULL,'
            'hints_json TEXT NOT NULL, anti_bot_vendor TEXT NOT NULL,'
            'auth_signal TEXT NOT NULL, status TEXT NOT NULL,'
            'confidence_milli INTEGER NOT NULL, visit_count INTEGER NOT NULL,'
            'successful_visits INTEGER NOT NULL, hinted_visits INTEGER NOT NULL,'
            'hint_match_visits INTEGER NOT NULL,'
            'consecutive_failures INTEGER NOT NULL, last_outcome TEXT NOT NULL,'
            'last_elapsed_ms INTEGER NOT NULL,'
            'last_verified_at_ms INTEGER NOT NULL,'
            'last_observed_at_ms INTEGER NOT NULL,'
            'expires_at_ms INTEGER NOT NULL, payload_bytes INTEGER NOT NULL,'
            'PRIMARY KEY (owner_user_id, origin, route_family, operation))')
        for row in browser_observations:
            connection.execute(
                'INSERT INTO storage_browser_site_observations VALUES ('
                '?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['owner_user_id'], row['origin'], row['route_family'],
                 row['operation'], row['schema_version'], row['strategy'],
                 json.dumps(row.get('hints') or []), row.get('anti_bot_vendor', ''),
                 row.get('auth_signal', 'none'), row['status'],
                 row['confidence_milli'], row['visit_count'],
                 row['successful_visits'], row['hinted_visits'],
                 row['hint_match_visits'], row['consecutive_failures'],
                 row['last_outcome'], row['last_elapsed_ms'],
                 row['last_verified_at_ms'], row['last_observed_at_ms'],
                 row['expires_at_ms'], row['payload_bytes']),
            )
    if browser_observation_owners is not None:
        connection.execute(
            'CREATE TABLE storage_browser_site_observation_owners ('
            'owner_user_id INTEGER PRIMARY KEY, touched_at_ms INTEGER NOT NULL)')
        for row in browser_observation_owners:
            connection.execute(
                'INSERT INTO storage_browser_site_observation_owners VALUES (?,?)',
                (row['owner_user_id'], row['touched_at_ms']),
            )
    if routing_authorities is not None:
        connection.execute(
            'CREATE TABLE storage_model_routing_authorities ('
            'owner_user_id INTEGER NOT NULL, tenant_id TEXT NOT NULL,'
            'revision INTEGER NOT NULL, document_json TEXT NOT NULL,'
            'backup_json TEXT, migration_receipt_json TEXT,'
            'updated_at REAL NOT NULL,'
            'PRIMARY KEY (owner_user_id, tenant_id))')
        for row in routing_authorities:
            connection.execute(
                'INSERT INTO storage_model_routing_authorities VALUES (?,?,?,?,?,?,?)',
                (row['owner_user_id'], row['tenant_id'], row['revision'],
                 json.dumps(row['document']),
                 None if row.get('backup') is None else json.dumps(row['backup']),
                 None if row.get('migration_receipt') is None
                 else json.dumps(row['migration_receipt']),
                 row['updated_at']),
            )
    if routing_secrets is not None:
        connection.execute(
            'CREATE TABLE storage_model_routing_secrets ('
            'owner_user_id INTEGER NOT NULL, tenant_id TEXT NOT NULL,'
            'secret_reference TEXT NOT NULL, ciphertext TEXT NOT NULL,'
            'key_hint TEXT NOT NULL, created_at REAL NOT NULL,'
            'updated_at REAL NOT NULL,'
            'PRIMARY KEY (owner_user_id, tenant_id, secret_reference))')
        for row in routing_secrets:
            connection.execute(
                'INSERT INTO storage_model_routing_secrets VALUES (?,?,?,?,?,?,?)',
                (row['owner_user_id'], row['tenant_id'], row['secret_reference'],
                 row['ciphertext'], row.get('key_hint', ''),
                 row['created_at'], row['updated_at']),
            )
    if plugin_manifests is not None:
        connection.execute(
            'CREATE TABLE storage_plugin_manifests ('
            'namespace TEXT PRIMARY KEY, manifest_version INTEGER NOT NULL,'
            'manifest_json BLOB NOT NULL, updated_at_ms INTEGER NOT NULL)')
        for row in plugin_manifests:
            connection.execute(
                'INSERT INTO storage_plugin_manifests VALUES (?,?,?,?)',
                (row['namespace'], row['manifest_version'],
                 json.dumps(row['manifest']).encode('utf-8'),
                 row['updated_at_ms']),
            )
    if plugin_rows is not None:
        connection.execute(
            'CREATE TABLE storage_plugin_rows ('
            'namespace TEXT NOT NULL, table_name TEXT NOT NULL,'
            'row_key TEXT NOT NULL, document_json BLOB NOT NULL,'
            'version INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL,'
            'PRIMARY KEY (namespace, table_name, row_key))')
        for row in plugin_rows:
            document = row['document_json']
            connection.execute(
                'INSERT INTO storage_plugin_rows VALUES (?,?,?,?,?,?)',
                (row['namespace'], row['table_name'], row['row_key'],
                 (document if isinstance(document, bytes)
                  else str(document).encode('utf-8')),
                 row['version'], row['updated_at_ms']),
            )
    if plugin_unique_values is not None:
        connection.execute(
            'CREATE TABLE storage_plugin_unique_values ('
            'namespace TEXT NOT NULL, table_name TEXT NOT NULL,'
            'index_name TEXT NOT NULL, index_value TEXT NOT NULL,'
            'row_key TEXT NOT NULL,'
            'PRIMARY KEY (namespace, table_name, index_name, index_value))')
        for row in plugin_unique_values:
            connection.execute(
                'INSERT INTO storage_plugin_unique_values VALUES (?,?,?,?,?)',
                (row['namespace'], row['table_name'], row['index_name'],
                 row['index_value'], row['row_key']),
            )
    if project_brain_projects is not None:
        connection.execute(
            'CREATE TABLE storage_project_brain_projects ('
            'owner_user_id INTEGER NOT NULL, project_key TEXT NOT NULL,'
            'head_sequence INTEGER NOT NULL, checkpoint_sequence INTEGER NOT NULL,'
            'projection_json TEXT NOT NULL, updated_at_ms INTEGER NOT NULL,'
            'PRIMARY KEY (owner_user_id, project_key))')
        for row in project_brain_projects:
            projection = dict(row['projection'])
            projection.setdefault('ownerUserId', row['owner_user_id'])
            projection.setdefault('projectKey', row['project_key'])
            projection.setdefault('headSequence', row['head_sequence'])
            projection.setdefault('checkpointSequence',
                                  row.get('checkpoint_sequence', 0))
            connection.execute(
                'INSERT INTO storage_project_brain_projects VALUES (?,?,?,?,?,?)',
                (row['owner_user_id'], row['project_key'], row['head_sequence'],
                 row.get('checkpoint_sequence', 0), json.dumps(projection),
                 row['updated_at_ms']),
            )
    if optimizer_proposals is not None:
        connection.execute(
            'CREATE TABLE optimizer_proposals ('
            'user_id INTEGER NOT NULL, id TEXT NOT NULL,'
            'created_at TEXT NOT NULL, title TEXT NOT NULL,'
            'rationale TEXT NOT NULL, action_type TEXT NOT NULL,'
            'action_args TEXT NOT NULL, severity TEXT NOT NULL,'
            'confidence REAL NOT NULL, evidence TEXT NOT NULL,'
            'status TEXT NOT NULL, status_reason TEXT NOT NULL,'
            'PRIMARY KEY (user_id, id))')
        for row in optimizer_proposals:
            connection.execute(
                'INSERT INTO optimizer_proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['user_id'], row['id'], row['created_at'], row['title'],
                 row['rationale'], row['action_type'], row['action_args'],
                 row.get('severity', 'low'), row.get('confidence', 0.5),
                 row.get('evidence', ''), row.get('status', 'pending_review'),
                 row.get('status_reason', '')),
            )
    if optimizer_actions is not None:
        connection.execute(
            'CREATE TABLE optimizer_action_log ('
            'user_id INTEGER NOT NULL, id TEXT NOT NULL,'
            'proposal_id TEXT NOT NULL, applied_at TEXT NOT NULL,'
            'expires_at TEXT NOT NULL, pre_metric TEXT NOT NULL,'
            'outcome_metric TEXT NOT NULL, outcome_recorded_at TEXT NOT NULL,'
            'reverted_at TEXT NOT NULL, revert_reason TEXT NOT NULL,'
            'PRIMARY KEY (user_id, id))')
        for row in optimizer_actions:
            connection.execute(
                'INSERT INTO optimizer_action_log VALUES (?,?,?,?,?,?,?,?,?,?)',
                (row['user_id'], row['id'], row['proposal_id'], row['applied_at'],
                 row.get('expires_at', ''), row.get('pre_metric', ''),
                 row.get('outcome_metric', ''), row.get('outcome_recorded_at', ''),
                 row.get('reverted_at', ''), row.get('revert_reason', '')),
            )
    if log_aggregates is not None:
        connection.execute(
            'CREATE TABLE log_aggregates ('
            'fingerprint TEXT PRIMARY KEY, level TEXT NOT NULL,'
            'logger TEXT NOT NULL, template TEXT NOT NULL, sample TEXT NOT NULL,'
            'count INTEGER NOT NULL, first_seen INTEGER NOT NULL,'
            'last_seen INTEGER NOT NULL)')
        for row in log_aggregates:
            connection.execute(
                'INSERT INTO log_aggregates VALUES (?,?,?,?,?,?,?,?)',
                (row['fingerprint'], row['level'], row.get('logger', ''),
                 row.get('template', ''), row.get('sample', ''), row['count'],
                 row['first_seen'], row['last_seen']),
            )
    if billing_payments is not None:
        connection.execute(
            'CREATE TABLE billing_payments ('
            'id TEXT PRIMARY KEY, user_id TEXT NOT NULL, provider TEXT NOT NULL,'
            'provider_id TEXT NOT NULL, amount_minor INTEGER NOT NULL,'
            'currency TEXT NOT NULL, credit_micro INTEGER NOT NULL,'
            'status TEXT NOT NULL, created_at INTEGER NOT NULL,'
            'settled_at INTEGER NOT NULL, raw TEXT NOT NULL)')
        for row in billing_payments:
            connection.execute(
                'INSERT INTO billing_payments VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (row['id'], row['user_id'], row['provider'],
                 row.get('provider_id', ''), row['amount_minor'],
                 row.get('currency', 'USD'), row['credit_micro'], row['status'],
                 row['created_at'], row.get('settled_at', 0),
                 json.dumps(row.get('raw') or {})),
            )
    if billing_redeem_codes is not None:
        connection.execute(
            'CREATE TABLE billing_redeem_codes ('
            'code TEXT PRIMARY KEY, amount_micro INTEGER NOT NULL,'
            'batch TEXT NOT NULL, created_by TEXT NOT NULL,'
            'created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,'
            'redeemed_by TEXT NOT NULL, redeemed_at INTEGER NOT NULL,'
            'note TEXT NOT NULL)')
        for row in billing_redeem_codes:
            connection.execute(
                'INSERT INTO billing_redeem_codes VALUES (?,?,?,?,?,?,?,?,?)',
                (row['code'], row['amount_micro'], row.get('batch', ''),
                 row.get('created_by', ''), row['created_at'],
                 row.get('expires_at', 0), row.get('redeemed_by', ''),
                 row.get('redeemed_at', 0), row.get('note', '')),
            )
    if queue_items is not None:
        connection.execute(
            'CREATE TABLE storage_queue_items ('
            'id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,'
            'conv_id TEXT NOT NULL, payload_json TEXT NOT NULL,'
            'config_json TEXT NOT NULL, position INTEGER NOT NULL,'
            'kind TEXT NOT NULL, priority INTEGER NOT NULL,'
            'created_at_ms INTEGER NOT NULL, leased_until_ms INTEGER,'
            'lease_task_id TEXT NOT NULL, input_turn_id TEXT NOT NULL,'
            'output_turn_id TEXT NOT NULL, attempt_id TEXT NOT NULL)')
        for row in queue_items:
            connection.execute(
                'INSERT INTO storage_queue_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['id'], row['user_id'], row['conv_id'],
                 json.dumps(row.get('payload') or {}),
                 json.dumps(row.get('config') or {}),
                 row['position'], row.get('kind', 'real'),
                 row.get('priority', 100), row['created_at_ms'],
                 row.get('leased_until_ms'), row.get('lease_task_id', ''),
                 row.get('input_turn_id', ''), row.get('output_turn_id', ''),
                 row.get('attempt_id', '')),
            )
    if autopilot_markers is not None:
        connection.execute(
            'CREATE TABLE storage_autopilot_markers ('
            'conv_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,'
            'queue_id TEXT NOT NULL, config_json TEXT NOT NULL,'
            'created_at_ms INTEGER NOT NULL)')
        for row in autopilot_markers:
            connection.execute(
                'INSERT INTO storage_autopilot_markers VALUES (?,?,?,?,?)',
                (row['conv_id'], row['user_id'], row['queue_id'],
                 json.dumps(row.get('config') or {}), row['created_at_ms']),
            )
    if timers is not None:
        connection.execute(
            'CREATE TABLE storage_timers ('
            'id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,'
            'conv_id TEXT NOT NULL, source_task_id TEXT NOT NULL,'
            'check_instruction TEXT NOT NULL, check_command TEXT NOT NULL,'
            'continuation_message TEXT NOT NULL, poll_interval INTEGER NOT NULL,'
            'max_polls INTEGER NOT NULL, poll_count INTEGER NOT NULL,'
            'status TEXT NOT NULL, tools_config TEXT NOT NULL,'
            'created_at TEXT NOT NULL, updated_at TEXT NOT NULL,'
            'triggered_at TEXT NOT NULL, cancelled_at TEXT NOT NULL,'
            'execution_task_id TEXT NOT NULL, last_poll_at TEXT NOT NULL,'
            'last_poll_decision TEXT NOT NULL, last_poll_reason TEXT NOT NULL,'
            'condition_kind TEXT NOT NULL, condition_command TEXT NOT NULL,'
            'condition_regex TEXT NOT NULL, promotion_streak INTEGER NOT NULL,'
            'fallback_streak INTEGER NOT NULL, promoted_at TEXT NOT NULL,'
            'origin TEXT NOT NULL)')
        for row in timers:
            connection.execute(
                'INSERT INTO storage_timers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'
                '?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['id'], row['user_id'], row['conv_id'],
                 row.get('source_task_id', ''), row.get('check_instruction', ''),
                 row.get('check_command', ''), row.get('continuation_message', ''),
                 row.get('poll_interval', 60), row.get('max_polls', 120),
                 row.get('poll_count', 0), row['status'],
                 json.dumps(row.get('tools_config') or {}),
                 row.get('created_at', ''), row.get('updated_at', ''),
                 row.get('triggered_at', ''), row.get('cancelled_at', ''),
                 row.get('execution_task_id', ''), row.get('last_poll_at', ''),
                 row.get('last_poll_decision', ''), row.get('last_poll_reason', ''),
                 row.get('condition_kind', 'llm'), row.get('condition_command', ''),
                 row.get('condition_regex', ''), row.get('promotion_streak', 0),
                 row.get('fallback_streak', 0), row.get('promoted_at', ''),
                 row.get('origin', 'background')),
            )
    if timer_poll_log is not None:
        connection.execute(
            'CREATE TABLE storage_timer_poll_log ('
            'id INTEGER PRIMARY KEY, timer_id TEXT NOT NULL,'
            'poll_time TEXT NOT NULL, decision TEXT NOT NULL,'
            'reason TEXT NOT NULL, check_output TEXT NOT NULL,'
            'tokens_used INTEGER NOT NULL, model TEXT NOT NULL,'
            'poll_id TEXT NOT NULL, raw_output TEXT NOT NULL,'
            'tier TEXT NOT NULL, predicate_matched INTEGER NOT NULL,'
            'llm_agreed INTEGER NOT NULL)')
        for row in timer_poll_log:
            connection.execute(
                'INSERT INTO storage_timer_poll_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['id'], row['timer_id'], row['poll_time'],
                 row.get('decision', 'wait'), row.get('reason', ''),
                 row.get('check_output', ''), row.get('tokens_used', 0),
                 row.get('model', ''), row.get('poll_id', ''),
                 row.get('raw_output', ''), row.get('tier', 'llm'),
                 row.get('predicate_matched', -1), row.get('llm_agreed', -1)),
            )
    if scheduled_tasks is not None:
        connection.execute(
            'CREATE TABLE storage_scheduled_tasks ('
            'id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,'
            'system_key TEXT NOT NULL, name TEXT NOT NULL,'
            'schedule TEXT NOT NULL, task_type TEXT NOT NULL,'
            'command TEXT NOT NULL, description TEXT NOT NULL,'
            'enabled INTEGER NOT NULL, notify_on_failure INTEGER NOT NULL,'
            'notify_on_success INTEGER NOT NULL, max_runtime INTEGER NOT NULL,'
            'last_run TEXT NOT NULL, last_result TEXT NOT NULL,'
            'last_status TEXT NOT NULL, run_count INTEGER NOT NULL,'
            'fail_count INTEGER NOT NULL, created_at TEXT NOT NULL,'
            'updated_at TEXT NOT NULL, target_conv_id TEXT NOT NULL,'
            'source_conv_id TEXT NOT NULL, tools_config TEXT NOT NULL,'
            'poll_count INTEGER NOT NULL, last_poll_at TEXT NOT NULL,'
            'last_poll_decision TEXT NOT NULL, last_poll_reason TEXT NOT NULL,'
            'last_execution_at TEXT NOT NULL, last_execution_task_id TEXT NOT NULL,'
            'last_execution_status TEXT NOT NULL, execution_count INTEGER NOT NULL,'
            'max_executions INTEGER NOT NULL, expires_at TEXT NOT NULL,'
            'condition_kind TEXT NOT NULL, condition_command TEXT NOT NULL,'
            'condition_regex TEXT NOT NULL, promotion_streak INTEGER NOT NULL,'
            'fallback_streak INTEGER NOT NULL, promoted_at TEXT NOT NULL)')
        for row in scheduled_tasks:
            connection.execute(
                'INSERT INTO storage_scheduled_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'
                '?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['id'], row['user_id'], row.get('system_key', ''),
                 row.get('name', ''), row.get('schedule', ''),
                 row.get('task_type', 'command'), row.get('command', ''),
                 row.get('description', ''), row.get('enabled', 1),
                 row.get('notify_on_failure', 1), row.get('notify_on_success', 0),
                 row.get('max_runtime', 300), row.get('last_run', ''),
                 row.get('last_result', ''), row.get('last_status', 'never'),
                 row.get('run_count', 0), row.get('fail_count', 0),
                 row['created_at'], row.get('updated_at', ''),
                 row.get('target_conv_id', ''), row.get('source_conv_id', ''),
                 json.dumps(row.get('tools_config') or {}),
                 row.get('poll_count', 0), row.get('last_poll_at', ''),
                 row.get('last_poll_decision', ''), row.get('last_poll_reason', ''),
                 row.get('last_execution_at', ''),
                 row.get('last_execution_task_id', ''),
                 row.get('last_execution_status', ''),
                 row.get('execution_count', 0), row.get('max_executions', 0),
                 row.get('expires_at', ''), row.get('condition_kind', 'llm'),
                 row.get('condition_command', ''), row.get('condition_regex', ''),
                 row.get('promotion_streak', 0), row.get('fallback_streak', 0),
                 row.get('promoted_at', '')),
            )
    if proactive_poll_log is not None:
        connection.execute(
            'CREATE TABLE storage_proactive_poll_log ('
            'id INTEGER PRIMARY KEY, task_id TEXT NOT NULL,'
            'poll_time TEXT NOT NULL, decision TEXT NOT NULL,'
            'reason TEXT NOT NULL, status_snapshot TEXT NOT NULL,'
            'model TEXT NOT NULL, tokens_used INTEGER NOT NULL,'
            'execution_task_id TEXT NOT NULL, tier TEXT NOT NULL,'
            'predicate_matched INTEGER NOT NULL, llm_agreed INTEGER NOT NULL)')
        for row in proactive_poll_log:
            connection.execute(
                'INSERT INTO storage_proactive_poll_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['id'], row['task_id'], row['poll_time'],
                 row.get('decision', 'skip'), row.get('reason', ''),
                 row.get('status_snapshot', ''), row.get('model', ''),
                 row.get('tokens_used', 0), row.get('execution_task_id', ''),
                 row.get('tier', 'llm'), row.get('predicate_matched', -1),
                 row.get('llm_agreed', -1)),
            )
    if sync_events is not None:
        connection.execute(
            'CREATE TABLE storage_conversation_changes ('
            'conversation_id TEXT NOT NULL, user_id INTEGER NOT NULL,'
            'sync_sequence INTEGER NOT NULL, change_type TEXT NOT NULL,'
            'turn_id TEXT, attempt_id TEXT, event_json TEXT NOT NULL,'
            'created_at INTEGER NOT NULL, attempt_sequence INTEGER NOT NULL,'
            'PRIMARY KEY (conversation_id, user_id, sync_sequence))')
        for row in sync_events:
            connection.execute(
                'INSERT INTO storage_conversation_changes VALUES (?,?,?,?,?,?,?,?,?)',
                (
                    row['conversation_id'], row['user_id'], row['sync_sequence'],
                    row['change_type'], row.get('turn_id'), row.get('attempt_id'),
                    json.dumps(row.get('event') or {}), row['created_at'],
                    row.get('attempt_sequence', 0),
                ),
            )
    if sync_heads is not None:
        connection.execute(
            'CREATE TABLE storage_conversation_sync_heads ('
            'conversation_id TEXT NOT NULL, user_id INTEGER NOT NULL,'
            'sync_sequence INTEGER NOT NULL, updated_at INTEGER NOT NULL,'
            'PRIMARY KEY (conversation_id, user_id))')
        for row in sync_heads:
            connection.execute(
                'INSERT INTO storage_conversation_sync_heads VALUES (?,?,?,?)',
                (row['conversation_id'], row['user_id'], row['sync_sequence'], row['updated_at']),
            )
    if worker_jobs is not None:
        connection.execute(
            'CREATE TABLE storage_worker_jobs ('
            'task_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,'
            'tenant_id TEXT NOT NULL, task_kind TEXT NOT NULL,'
            'payload_json TEXT NOT NULL, idempotency_key TEXT NOT NULL,'
            'request_digest TEXT NOT NULL, status TEXT NOT NULL,'
            'priority INTEGER NOT NULL, available_at_ms INTEGER NOT NULL,'
            'claim_owner TEXT NOT NULL, lease_deadline_ms INTEGER NOT NULL,'
            'fencing_token INTEGER NOT NULL, attempt_no INTEGER NOT NULL,'
            'heartbeat_at_ms INTEGER NOT NULL, cancel_sequence INTEGER NOT NULL,'
            'cancel_requested_at_ms INTEGER NOT NULL, cancel_reason TEXT NOT NULL,'
            'replay_cursor INTEGER NOT NULL, result_ref TEXT NOT NULL,'
            'error_json TEXT NOT NULL, created_at_ms INTEGER NOT NULL,'
            'updated_at_ms INTEGER NOT NULL, terminal_at_ms INTEGER NOT NULL)')
        for row in worker_jobs:
            connection.execute(
                'INSERT INTO storage_worker_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'
                '?,?,?,?,?,?,?,?,?,?)',
                (row['task_id'], row['user_id'], row.get('tenant_id', ''),
                 row['task_kind'], json.dumps(row.get('payload') or {}),
                 row['idempotency_key'], row['request_digest'], row['status'],
                 row.get('priority', 100), row.get('available_at_ms', 0),
                 row.get('claim_owner', ''), row.get('lease_deadline_ms', 0),
                 row.get('fencing_token', 0), row.get('attempt_no', 0),
                 row.get('heartbeat_at_ms', 0), row.get('cancel_sequence', 0),
                 row.get('cancel_requested_at_ms', 0),
                 row.get('cancel_reason', ''), row.get('replay_cursor', 0),
                 row.get('result_ref', ''), json.dumps(row.get('error') or {}),
                 row['created_at_ms'], row['updated_at_ms'],
                 row.get('terminal_at_ms', 0)),
            )
    if legacy_project_brain is not None:
        connection.execute(
            'CREATE TABLE storage_watch_items ('
            'item_id TEXT PRIMARY KEY, project_path TEXT, kind TEXT, text TEXT,'
            'status TEXT, promoted INTEGER, response_fingerprint TEXT,'
            'created_by_conv TEXT, created_at INTEGER, updated_at INTEGER,'
            'user_id INTEGER)')
        for row in legacy_project_brain.get('watch_items', []):
            connection.execute(
                'INSERT INTO storage_watch_items VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (row['item_id'], row['project_path'], row.get('kind', 'concern'),
                 row['text'], row.get('status', 'open'), row.get('promoted', 0),
                 row.get('response_fingerprint', ''), row.get('created_by_conv', ''),
                 row.get('created_at', 0), row.get('updated_at', 0),
                 row['user_id']))
        connection.execute(
            'CREATE TABLE storage_watch_responses ('
            'item_id TEXT, sequence INTEGER, project_path TEXT, response TEXT,'
            'pillar_state_json TEXT, trigger TEXT, ts INTEGER,'
            'PRIMARY KEY (item_id, sequence))')
        for row in legacy_project_brain.get('watch_responses', []):
            connection.execute(
                'INSERT INTO storage_watch_responses VALUES (?,?,?,?,?,?,?)',
                (row['item_id'], row['sequence'], row.get('project_path', ''),
                 row.get('response', ''), row.get('pillar_state_json', ''),
                 row.get('trigger', ''), row.get('ts', 0)))
        for table in legacy_project_brain.get('history_tables', []):
            connection.execute(
                f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
            connection.execute(f'INSERT INTO {table} VALUES (1)')
    connection.commit()
    connection.close()


def _sample_credentials():
    return [
        {
            'id': 'live-one', 'owner_user_id': _OWNER_ID, 'name': 'live',
            'prefix': 'tf_live', 'secret_hash': _secret('live'),
            'scopes': ['read', 'write'], 'created_at': _CREATED,
            'last_used_at': _CREATED + 10, 'disabled': True,
        },
        {
            'id': 'revoked-two', 'owner_user_id': _OWNER_ID, 'name': 'gone',
            'prefix': 'tf_gone', 'secret_hash': _secret('gone'),
            'scopes': ['read'], 'created_at': _CREATED + 1,
            'disabled': True,
            'revoked_at': _CREATED + 20,
        },
    ]


def _sample_providers():
    return [
        {
            'id': 'prov-alpha', 'owner_user_id': _OWNER_ID, 'tenant_id': '',
            'name': 'alpha', 'base_url': 'https://alpha.example/v1',
            'api_key_ciphertext': 'envelope:alpha', 'key_hint': 'pha',
            'models': [{'model_id': 'alpha-1'}], 'extra_headers': {'X-Test': 'a'},
            'thinking_format': '', 'disabled': False,
            'created_at': _CREATED, 'updated_at': _CREATED + 5,
            'last_used_at': _CREATED + 10,
        },
        {
            'id': 'prov-beta', 'owner_user_id': _OWNER_ID, 'tenant_id': '',
            'name': 'beta', 'base_url': 'https://beta.example/v1',
            'api_key_ciphertext': 'envelope:beta', 'key_hint': 'eta',
            'models': [], 'extra_headers': {}, 'thinking_format': 'none',
            'disabled': True, 'created_at': _CREATED + 1,
            'updated_at': _CREATED + 6, 'last_used_at': None,
        },
    ]


def _sample_users():
    return [
        {
            'id': 'acct-primary', 'owner_user_id': _OWNER_ID,
            'email': '  Owner@Example.COM ', 'password_hash': 'hash-primary',
            'display_name': 'Owner', 'role': 'admin', 'status': 'active',
            'created_at': 100, 'last_login_at': 250, 'email_verified': 1,
            'metadata': {'plan': 'pro'},
        },
        {
            'id': 'acct-gap', 'owner_user_id': 2,
            'email': 'gap@example.com', 'password_hash': '',
            'display_name': '', 'role': 'user', 'status': 'suspended',
            'created_at': 50, 'last_login_at': 0, 'email_verified': 0,
            'metadata': {},
        },
    ]



def _sample_conversations():
    return [
        {
            'id': 'conv-alpha', 'user_id': _OWNER_ID, 'title': 'Alpha',
            'created_at_ms': 1000, 'updated_at_ms': 3000,
            'settings': {'model': 'alpha'},
        },
        {
            'id': 'conv-empty-title', 'user_id': _OWNER_ID, 'title': '',
            'created_at_ms': 2000, 'updated_at_ms': 2000, 'settings': {},
        },
        {
            'id': 'conv-beta', 'user_id': _OWNER_ID, 'title': 'Beta',
            'created_at_ms': 1500, 'updated_at_ms': 4000,
            'settings': {'folderId': 'inbox'},
        },
    ]

def _sample_records():
    return [
        {'namespace': 'prefs', 'record_key': 'alpha', 'value': {'n': 1}},
        {'namespace': 'prefs', 'record_key': 'beta', 'value': {'n': 2}},
    ]


def _sample_billing():
    wallets = [{'user_id': '11', 'balance_micro': 700, 'updated_at': 20}]
    ledger = [
        {
            'id': 'led-1', 'user_id': '11', 'ts': 10, 'amount_micro': 1000,
            'kind': 'topup', 'balance_after_micro': 1000,
        },
        {
            'id': 'led-2', 'user_id': '11', 'ts': 20, 'amount_micro': -300,
            'kind': 'debit', 'balance_after_micro': 700,
        },
    ]
    return wallets, ledger


def test_import_replays_credentials_billing_and_records(tmp_path):
    source = tmp_path / 'source.db'
    wallets, ledger = _sample_billing()
    _seed_source(
        source, users=_sample_users(), conversations=_sample_conversations(),
        credentials=_sample_credentials(), providers=_sample_providers(),
        records=_sample_records(), wallets=wallets, ledger=ledger,
    )
    session = FakeSession()
    authority = str(uuid4())
    binding = import_sqlite_authority(
        source, session, tmp_path / 'progress.json', authority, batch_rows=16,
    )
    assert binding.target_authority_uuid == authority
    assert binding.plan_sha256
    assert set(session.users) == {'acct-primary', 'acct-gap'}
    assert session.users['acct-primary']['owner_user_id'] == _OWNER_ID
    assert session.users['acct-primary']['email'] == 'owner@example.com'
    assert session.users['acct-primary']['password_hash'] == 'hash-primary'
    assert session.users['acct-primary']['email_verified'] is True
    assert session.users['acct-gap']['owner_user_id'] == 2
    assert session.users['acct-gap']['status'] == 'suspended'
    assert set(session.conversations) == {'conv-alpha', 'conv-beta', 'conv-empty-title'}
    assert session.conversations['conv-alpha']['title'] == 'Alpha'
    assert session.conversations['conv-alpha']['settings'] == {'model': 'alpha'}
    assert session.conversations['conv-alpha']['created_at'] == 1000
    assert session.conversations['conv-alpha']['updated_at'] == 3000
    assert session.conversations['conv-empty-title']['title'] == 'New Chat'
    assert session.conversations['conv-beta']['settings'] == {'folderId': 'inbox'}
    assert set(session.credentials) == {'live-one', 'revoked-two'}
    assert session.credentials['live-one']['disabled'] is True
    assert session.credentials['live-one']['last_used_at'] == _CREATED + 10
    assert session.credentials['revoked-two']['revoked_at'] == _CREATED + 20
    assert set(session.providers) == {'prov-alpha', 'prov-beta'}
    assert session.providers['prov-alpha']['api_key_ciphertext'] == 'envelope:alpha'
    assert session.providers['prov-alpha']['last_used_at'] == _CREATED + 10
    assert session.providers['prov-beta']['disabled'] is True
    assert session.providers['prov-beta']['updated_at'] == _CREATED + 6
    listed = session._list_providers({'owner_user_id': _OWNER_ID, 'tenant_id': ''}).json()
    assert {row['id'] for row in listed} == {'prov-alpha', 'prov-beta'}
    assert all('api_key_ciphertext' not in row for row in listed)
    assert session.wallets['11']['balance_micro'] == 700
    assert set(session.ledger) == {'led-1', 'led-2'}
    assert json.loads(session.records[('prefs', 'alpha')]['value_json']) == {'n': 1}
    binding_again = import_sqlite_authority(
        source, session, tmp_path / 'progress.json', authority,
    )
    assert binding_again.source_sha256 == binding.source_sha256


def test_import_resumes_after_verified_prefix_crash(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, credentials=_sample_credentials(), records=_sample_records())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'

    def crash(domain, entry):
        if domain == 'credentials' and entry['rows'] == 1 and not entry['complete']:
            raise _Crash(domain)

    with pytest.raises(_Crash):
        import_sqlite_authority(
            source, session, checkpoint, authority, batch_rows=1,
            after_verified_prefix=crash,
        )
    assert len(session.credentials) == 1
    binding = import_sqlite_authority(
        source, session, checkpoint, authority, batch_rows=1,
    )
    assert set(session.credentials) == {'live-one', 'revoked-two'}
    assert json.loads(session.records[('prefs', 'beta')]['value_json']) == {'n': 2}
    assert binding.source_schema_version == SCHEMA_VERSION


def _rollback_arguments(source, checkpoint, authority):
    return ['--source', str(source), '--checkpoint', str(checkpoint),
            '--authority-uuid', authority, '--rollback']


def test_rollback_certifies_untouched_source_and_partitions_journal(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, credentials=_sample_credentials(), records=_sample_records())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'

    def crash(domain, entry):
        if domain == 'credentials' and entry['rows'] == 1 and not entry['complete']:
            raise _Crash(domain)

    with pytest.raises(_Crash):
        import_sqlite_authority(
            source, session, checkpoint, authority, batch_rows=1,
            after_verified_prefix=crash,
        )
    checkpoint_bytes = checkpoint.read_bytes()
    verdict = certify_rollback(source, checkpoint, target_authority_uuid=authority)
    assert verdict == {
        'rollbackSafe': True,
        'sourceUntouched': True,
        'completeDomains': ['tenant_users', 'trash', 'conversations', 'turn_tombstones', 'attempts', 'attempt_events', 'turns', 'storage_events', 'conversation_sync_events', 'conversation_sync_heads', 'frozen_transcripts', 'raw_archives', 'compaction_archives'],
        'incompleteDomains': ['credentials'],
        'unchangedDomains': ['command_receipts_v2', 'command_receipts',
                             'providers', 'billing', 'task_results', 'continuation',
                             'records', 'paper_build_budget', 'paper_document_budget',
                             'desktop_egress', 'daily_cost', 'rate_limit_events',
                             'orchestration', 'swarm', 'integration', 'paper',
                             'knowledge', 'chat_artifacts', 'tool_result_artifacts',
                             'browser_observations', 'model_routing', 'plugin_store',
                             'project_brain_legacy_cutover', 'project_brain_projects',
                             'optimizer', 'log_aggregates',
                             'billing_payments', 'billing_redeem_codes',
                             'queue_items', 'autopilot_markers', 'timers',
                             'scheduled_tasks', 'worker_jobs'] + [
                                 'retired.' + table['name'] for table in importer.retired.TABLES] + [
                                 'retired_large.' + table['name'] for table in importer.retired_large.TABLES],
    }
    # Certification is read-only: the journal and the source survive untouched.
    assert checkpoint.read_bytes() == checkpoint_bytes
    resumed = import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)
    assert resumed.source_sha256
    final = certify_rollback(source, checkpoint, target_authority_uuid=authority)
    assert final['rollbackSafe'] is True
    assert final['completeDomains'] == [
        domain['name'] for domain in importer.PLAN['domains']]
    assert final['incompleteDomains'] == []
    assert final['unchangedDomains'] == []


def test_rollback_cli_emits_machine_readable_verdict(tmp_path, capsys):
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_records())
    checkpoint = tmp_path / 'progress.json'
    authority = str(uuid4())
    import_sqlite_authority(source, FakeSession(), checkpoint, authority)
    assert main(_rollback_arguments(source, checkpoint, authority)) == 0
    verdict = json.loads(capsys.readouterr().out)
    assert verdict['rollbackSafe'] is True
    assert verdict['sourceUntouched'] is True
    assert len(verdict['completeDomains']) == len(importer.PLAN['domains'])
    assert verdict['incompleteDomains'] == []
    # A wrong target identity is a binding mismatch, so the run fails closed.
    assert main(_rollback_arguments(source, checkpoint, str(uuid4()))) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused['rollbackSafe'] is False
    assert refused['sourceUntouched'] is False
    assert 'differs' in refused['reason']


def test_rollback_fails_closed_when_source_diverges_after_binding(tmp_path, capsys):
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_records())
    checkpoint = tmp_path / 'progress.json'
    authority = str(uuid4())
    import_sqlite_authority(source, FakeSession(), checkpoint, authority)
    with sqlite3.connect(source) as connection:
        connection.execute(
            'UPDATE storage_records SET value_json = ? WHERE record_key = ?',
            ('{"n": 99}', 'alpha'))
    with pytest.raises(MigrationImportError, match='binding differs'):
        certify_rollback(source, checkpoint, target_authority_uuid=authority)
    assert main(_rollback_arguments(source, checkpoint, authority)) == 2
    verdict = json.loads(capsys.readouterr().out)
    assert verdict['rollbackSafe'] is False
    assert verdict['sourceUntouched'] is False
    assert 'binding differs' in verdict['reason']
    assert verdict['completeDomains'] == []


def test_rollback_fails_closed_on_missing_or_corrupt_checkpoint(tmp_path, capsys):
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_records())
    checkpoint = tmp_path / 'progress.json'
    authority = str(uuid4())
    import_sqlite_authority(source, FakeSession(), checkpoint, authority)

    missing = tmp_path / 'missing.json'
    with pytest.raises(MigrationImportError, match='binding differs'):
        certify_rollback(source, missing, target_authority_uuid=authority)
    assert main(_rollback_arguments(source, missing, authority)) == 2
    assert not missing.exists()
    missing_verdict = json.loads(capsys.readouterr().out)
    assert missing_verdict['rollbackSafe'] is False
    assert missing_verdict['sourceUntouched'] is False
    assert 'binding differs' in missing_verdict['reason']

    corrupt = tmp_path / 'corrupt.json'
    corrupt.write_bytes(b'{"body":')
    with pytest.raises(MigrationImportError, match='unreadable'):
        certify_rollback(source, corrupt, target_authority_uuid=authority)
    assert main(_rollback_arguments(source, corrupt, authority)) == 2
    verdict = json.loads(capsys.readouterr().out)
    assert verdict['rollbackSafe'] is False
    assert verdict['sourceUntouched'] is False
    assert 'unreadable' in verdict['reason']

    # A syntactically valid journal with a tampered binding also fails closed.
    envelope = json.loads(checkpoint.read_text())
    envelope['body']['binding']['sourceSha256'] = '0' * 64
    tampered = tmp_path / 'tampered.json'
    tampered.write_text(json.dumps(envelope))
    with pytest.raises(MigrationImportError, match='checksum|binding'):
        certify_rollback(source, tampered, target_authority_uuid=authority)
    assert main(_rollback_arguments(source, tampered, authority)) == 2
    tampered_verdict = json.loads(capsys.readouterr().out)
    assert tampered_verdict['rollbackSafe'] is False


def test_rollback_fails_closed_on_locked_checkpoint(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_records())
    checkpoint = tmp_path / 'progress.json'
    authority = str(uuid4())
    binding = import_sqlite_authority(source, FakeSession(), checkpoint, authority)
    with MigrationCheckpoint(checkpoint, binding):
        with pytest.raises(BlockingIOError):
            certify_rollback(source, checkpoint, target_authority_uuid=authority)


def test_import_rejects_replaced_target_identity(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_records()[:1])
    session = FakeSession()
    first = str(uuid4())
    import_sqlite_authority(source, session, tmp_path / 'progress.json', first)
    with pytest.raises(MigrationImportError, match='binding'):
        import_sqlite_authority(source, session, tmp_path / 'progress.json', str(uuid4()))


def test_independent_target_enumeration_rejects_divergence(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_records())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'

    def crash(domain, entry):
        if domain == 'records' and entry['rows'] == 1 and not entry['complete']:
            raise _Crash(domain)

    with pytest.raises(_Crash):
        import_sqlite_authority(
            source, session, checkpoint, authority, batch_rows=1,
            after_verified_prefix=crash,
        )
    session.records[('prefs', 'alpha')]['value_json'] = '{"n":99}'
    with pytest.raises(MigrationImportError, match='no longer matches|catalogs differ'):
        import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)


def test_import_resumes_providers_and_rejects_divergent_ciphertext(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, providers=_sample_providers())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'

    def crash(domain, entry):
        if domain == 'providers' and entry['rows'] == 1 and not entry['complete']:
            raise _Crash(domain)

    with pytest.raises(_Crash):
        import_sqlite_authority(
            source, session, checkpoint, authority, batch_rows=1,
            after_verified_prefix=crash,
        )
    assert set(session.providers) == {'prov-alpha'}
    session.providers['prov-alpha']['api_key_ciphertext'] = 'envelope:mutated'
    with pytest.raises(MigrationImportError, match='no longer matches|catalogs differ'):
        import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)
    session.providers['prov-alpha']['api_key_ciphertext'] = 'envelope:alpha'
    binding = import_sqlite_authority(
        source, session, checkpoint, authority, batch_rows=1,
    )
    assert set(session.providers) == {'prov-alpha', 'prov-beta'}
    assert session.providers['prov-beta']['api_key_ciphertext'] == 'envelope:beta'
    assert binding.source_schema_version == SCHEMA_VERSION


def test_provider_list_ciphertext_or_extra_identity_fails_closed(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, providers=_sample_providers()[:1])
    session = FakeSession()
    leaking = FakeSession()
    original_list = leaking._list_providers

    def leak(payload):
        rows = original_list(payload).json()
        for row in rows:
            row['api_key_ciphertext'] = 'leaked'
        return _Response(rows)

    leaking._list_providers = leak
    leaking.request = FakeSession.request.__get__(leaking, FakeSession)
    with pytest.raises(MigrationImportError, match='catalog is invalid'):
        import_sqlite_authority(source, leaking, tmp_path / 'leaked.json', str(uuid4()))
    extra = FakeSession()
    extra.providers['prov-extra'] = {
        'id': 'prov-extra', 'owner_user_id': _OWNER_ID, 'tenant_id': '',
        'name': 'extra', 'base_url': 'https://extra.example/v1',
        'api_key_ciphertext': 'envelope:extra', 'key_hint': '',
        'models': [], 'extra_headers': {}, 'thinking_format': '',
        'disabled': False, 'created_at': _CREATED, 'updated_at': _CREATED,
        'last_used_at': None,
    }
    with pytest.raises(MigrationImportError, match='catalogs differ'):
        import_sqlite_authority(source, extra, tmp_path / 'extra.json', str(uuid4()))


def test_cli_refuses_relative_paths_and_missing_binary(tmp_path, capsys):
    source = tmp_path / 'source.db'
    # Keep this input free of coverage failures so missing-binary admission
    # is the boundary under test; unknown source data has its own regression.
    sqlite3.connect(source).close()
    arguments = ['--checkpoint', str(tmp_path / 'p.json'),
                 '--binary', str(tmp_path / 'missing-binary'), '--target', str(tmp_path / 'target'),
                 '--owner-id', '11', '--tenant-id', '7', '--authority-uuid', str(uuid4())]
    assert main(['--source', 'source.db', *arguments]) == 2
    assert main(['--source', str(source), *arguments]) == 2
    captured = capsys.readouterr()
    assert 'absolute paths' in captured.err
    assert 'FileNotFoundError' in captured.err


def _resolve_tofudb_binary() -> Path:
    override = os.environ.get('TOFUDB_DIFFERENTIAL_BIN', '').strip()
    if override:
        binary = Path(override)
        if binary.is_file() and os.access(binary, os.X_OK):
            return binary
        pytest.skip(f'TOFUDB_DIFFERENTIAL_BIN is not executable: {override}')
    from tests.support.tofudb_binary import ensure_engine_binary
    binary = ensure_engine_binary(_PROJECT_ROOT)
    if binary is None:
        pytest.skip('no tofu-db binary available')
    return binary


def _initialize(binary, data_dir):
    result = subprocess.run([str(binary), 'init', '--data-dir', str(data_dir)],
                            check=True, capture_output=True, text=True, timeout=120)
    return next(part.split('=', 1)[1] for part in result.stdout.split()
                if part.startswith('authority_uuid='))


def test_offline_child_import_preserves_identity_and_resumes(tmp_path):
    binary = _resolve_tofudb_binary()
    source = tmp_path / 'source.db'
    wallets, ledger = _sample_billing()
    _seed_source(source, users=_sample_users(), conversations=_sample_conversations(),
                 turns=_sample_turns(),
                 credentials=_sample_credentials(), providers=_sample_providers(),
                 records=_sample_records(), wallets=wallets, ledger=ledger)
    data_dir = tmp_path / 'target'
    authority = _initialize(binary, data_dir)
    checkpoint = tmp_path / 'progress.json'

    def crash(domain, entry):
        if domain == 'records' and entry['rows'] == 1 and not entry['complete']:
            raise _Crash(domain)

    with OfflineMigrationSession(binary, data_dir, authority, owner_id=11, tenant_id=7) as session:
        with pytest.raises(_Crash):
            import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1,
                                    after_verified_prefix=crash)
    with OfflineMigrationSession(binary, data_dir, authority, owner_id=11, tenant_id=7) as session:
        binding = import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)
        deadline = int(time.time() * 1000) + 15_000
        listed = session.request('credential.list', {'owner_user_id': 11, 'tenant_id': ''},
                                 command_id=None, deadline_unix_ms=deadline)
        listed.raise_for_error()
        assert [item['id'] for item in listed.json()] == ['live-one']
        restored = session.request(
            'migration.tenant.user.get', {'id': 'acct-primary'},
            command_id=None, deadline_unix_ms=deadline)
        restored.raise_for_error()
        body = restored.json()
        assert body['owner_user_id'] == 11
        assert body['email'] == 'owner@example.com'
        assert body['password_hash'] == 'hash-primary'
        assert body['role'] == 'admin'
        gap = session.request(
            'migration.tenant.user.get', {'id': 'acct-gap'},
            command_id=None, deadline_unix_ms=deadline)
        gap.raise_for_error()
        assert gap.json()['owner_user_id'] == 2
        assert gap.json()['status'] == 'suspended'
        catalog = session.request(
            'migration.tenant.user.scan', {'cursor': None},
            command_id=None, deadline_unix_ms=deadline)
        catalog.raise_for_error()
        assert {row['id'] for row in catalog.json()['rows']} == {
            'acct-primary', 'acct-gap',
        }
        counted = session.request(
            'conversation.count', {'user_id': 11},
            command_id=None, deadline_unix_ms=deadline)
        counted.raise_for_error()
        assert counted.json()['count'] == 3
        listed_conversations = session.request(
            'conversation.list',
            {
                'user_id': 11, 'catalog_page': True,
                'include_messages': False, 'limit': 128,
            },
            command_id=None, deadline_unix_ms=deadline)
        listed_conversations.raise_for_error()
        page = listed_conversations.json()
        assert page['has_more'] is False
        assert {
            item['metadata']['id'] for item in page['items']
        } == {'conv-alpha', 'conv-beta', 'conv-empty-title'}
        header = session.request(
            'conversation.get',
            {'user_id': 11, 'conv_id': 'conv-alpha', 'derive_messages': False},
            command_id=None, deadline_unix_ms=deadline)
        header.raise_for_error()
        body = header.json()
        assert body['metadata']['title'] == 'Alpha'
        assert body['metadata']['created_at'] == 1000
        assert body['metadata']['updated_at'] == 3000
        assert body['metadata']['settings'] == {'model': 'alpha'}
        assert body['messages'] == []
        empty_title = session.request(
            'conversation.get',
            {
                'user_id': 11, 'conv_id': 'conv-empty-title',
                'derive_messages': False,
            },
            command_id=None, deadline_unix_ms=deadline)
        empty_title.raise_for_error()
        assert empty_title.json()['metadata']['title'] == 'New Chat'
        providers = session.request(
            'provider.list', {'owner_user_id': 11, 'tenant_id': ''},
            command_id=None, deadline_unix_ms=deadline)
        providers.raise_for_error()
        listed_providers = providers.json()
        assert {item['id'] for item in listed_providers} == {'prov-alpha', 'prov-beta'}
        assert all('api_key_ciphertext' not in item for item in listed_providers)
        snapshot = session.request(
            'provider.get',
            {'owner_user_id': 11, 'tenant_id': '', 'provider_id': 'prov-alpha'},
            command_id=None, deadline_unix_ms=deadline)
        snapshot.raise_for_error()
        body = snapshot.json()
        assert body['api_key_ciphertext'] == 'envelope:alpha'
        assert body['last_used_at'] == _CREATED + 10
        assert body['disabled'] is False
        for expected in _sample_records():
            record = session.request('migration.record.get', {'namespace': 'prefs',
                                     'key': expected['record_key']},
                                     command_id=None, deadline_unix_ms=deadline)
            record.raise_for_error()
            assert json.loads(record.json()['value_json']) == expected['value']
            assert record.json()['updated_at_ms'] == 1_700_000_000_000
        wallet = session.request('billing.wallet.get', {'user_id': '11'},
                                 command_id=None, deadline_unix_ms=deadline)
        wallet.raise_for_error()
        assert wallet.json()['balance_micro'] == 700
        turn = session.request(
            'migration.turn.get',
            {'id': 'turn-alpha-2', 'conversation_id': 'conv-alpha'},
            command_id=None, deadline_unix_ms=deadline)
        turn.raise_for_error()
        body = turn.json()
        assert body['parent_turn_id'] == 'turn-alpha-1'
        assert body['lane_id'] == 'main'
        assert body['projection'] == {'timestamp': 2200}
        assert body['settlement'] == {'usage': {'output': 5}}
        assert body['current_attempt_id'] is None
        branch = session.request(
            'migration.turn.get',
            {'id': 'turn-alpha-branch', 'conversation_id': 'conv-alpha'},
            command_id=None, deadline_unix_ms=deadline)
        branch.raise_for_error()
        assert branch.json()['lane_id'] == 'branch-1'
        turn_catalog = session.request(
            'migration.turn.scan', {'cursor': None},
            command_id=None, deadline_unix_ms=deadline)
        turn_catalog.raise_for_error()
        assert {row['id'] for row in turn_catalog.json()['rows']} == {
            'turn-alpha-1', 'turn-alpha-2', 'turn-alpha-branch', 'turn-beta-1',
        }
        transcript = session.request(
            'conversation.get',
            {'user_id': 11, 'conv_id': 'conv-alpha', 'derive_messages': True},
            command_id=None, deadline_unix_ms=deadline)
        transcript.raise_for_error()
        assert binding.target_authority_uuid == authority


def test_native_paged_import_recovers_commit_before_checkpoint_with_bounded_memory(tmp_path, record_property):
    binary = _resolve_tofudb_binary()
    source = tmp_path / 'source.db'
    raw = '{ "number": 1.00, "text": "' + 'x' * 16384 + '" }\n'
    _seed_source(source, records=[{'namespace': 'prefs', 'record_key': f'{i:05}',
                 'value_json': raw, 'version': 1042, 'updated_at_ms': 123456789}
                 for i in range(2005)])
    target = tmp_path / 'target'
    authority = _initialize(binary, target)
    checkpoint = tmp_path / 'progress.json'
    with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7) as session:
        original = session.request
        restored = 0

        def interrupted(operation, payload, **kwargs):
            nonlocal restored
            response = original(operation, payload, **kwargs)
            if operation == 'migration.record.restore':
                response.raise_for_error()
                restored += 1
                if restored == 257:
                    session._process.kill()
                    session._process.wait(timeout=5)
                    raise _Crash('committed before independent readback/checkpoint')
            return response

        session.request = interrupted
        with pytest.raises(_Crash):
            import_sqlite_authority(source, session, checkpoint, authority, batch_rows=128)
    with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7) as session:
        tracemalloc.start()
        try:
            import_sqlite_authority(source, session, checkpoint, authority, batch_rows=128)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 24 * 1024 * 1024, peak
        record_property('python_peak_bytes', peak)
        status = Path(f'/proc/{session._process.pid}/status')
        if status.exists():
            high_water_kib = next(int(line.split()[1]) for line in status.read_text().splitlines()
                                  if line.startswith('VmHWM:'))
            record_property('native_high_water_kib', high_water_kib)
            assert high_water_kib < 512 * 1024, high_water_kib
        response = session.request('migration.record.get', {'namespace': 'prefs', 'key': '02004'},
                                   command_id=None, deadline_unix_ms=int(time.time()*1000)+15000)
        response.raise_for_error()
        assert response.json() == {'namespace': 'prefs', 'key': '02004', 'value_json': raw,
                                   'version': 1042, 'updated_at_ms': 123456789}


@pytest.mark.parametrize('namespace', ['prefs', 'unseen-namespace'])
def test_uncheckpointed_extra_target_keys_are_rejected(tmp_path, namespace):
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_records())
    session = FakeSession()
    session.records[(namespace, 'extra')] = {'namespace': namespace, 'key': 'extra',
        'value_json': '{}', 'version': 1, 'updated_at_ms': 1}
    with pytest.raises(MigrationImportError, match='extra target key'):
        import_sqlite_authority(source, session, tmp_path / 'progress.json', str(uuid4()))
    assert len(session.records) == 1



def test_uncheckpointed_extra_tenant_user_is_rejected(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, users=_sample_users()[:1])
    session = FakeSession()
    session.users['ghost'] = {
        'id': 'ghost', 'owner_user_id': 99, 'email': 'ghost@example.com',
        'password_hash': '', 'display_name': '', 'role': 'user',
        'status': 'active', 'created_at': 1, 'last_login_at': 0,
        'email_verified': False, 'metadata': {},
    }
    with pytest.raises(MigrationImportError, match='extra account'):
        import_sqlite_authority(
            source, session, tmp_path / 'progress.json', str(uuid4()))
    assert set(session.users) == {'ghost'}


def test_import_resumes_conversations_and_rejects_divergent_headers(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, conversations=_sample_conversations())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'

    def crash(domain, entry):
        if domain == 'conversations' and entry['rows'] == 1 and not entry['complete']:
            raise _Crash(domain)

    with pytest.raises(_Crash):
        import_sqlite_authority(
            source, session, checkpoint, authority, batch_rows=1,
            after_verified_prefix=crash,
        )
    assert set(session.conversations) == {'conv-alpha'}
    session.conversations['conv-alpha']['title'] = 'mutated'
    with pytest.raises(MigrationImportError, match='no longer matches|catalogs differ'):
        import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)
    session.conversations['conv-alpha']['title'] = 'Alpha'
    binding = import_sqlite_authority(
        source, session, checkpoint, authority, batch_rows=1,
    )
    assert set(session.conversations) == {
        'conv-alpha', 'conv-beta', 'conv-empty-title',
    }
    assert session.conversations['conv-empty-title']['title'] == 'New Chat'
    assert binding.source_schema_version == SCHEMA_VERSION


def test_conversation_catalog_pages_and_count_match_source(tmp_path):
    source = tmp_path / 'source.db'
    rows = [
        {
            'id': f'conv-{index:03d}', 'user_id': _OWNER_ID,
            'title': f'T{index}', 'created_at_ms': 1000 + index,
            'updated_at_ms': 2000 + index, 'settings': {},
        }
        for index in range(130)
    ]
    _seed_source(source, conversations=rows)
    session = FakeSession()
    import_sqlite_authority(
        source, session, tmp_path / 'progress.json', str(uuid4()), batch_rows=16,
    )
    assert len(session.conversations) == 130
    page = session._list_conversations({
        'user_id': _OWNER_ID, 'catalog_page': True,
        'include_messages': False, 'limit': 128,
    }).json()
    assert page['has_more'] is True
    assert page['total_count'] == 130
    assert len(page['items']) == 128
    assert session._count_conversations({'user_id': _OWNER_ID}).json() == {'count': 130}


def test_conversation_owner_mismatch_fails_closed(tmp_path):
    source = tmp_path / 'source.db'
    foreign = dict(_sample_conversations()[0])
    foreign['id'] = 'foreign'
    foreign['user_id'] = 99
    _seed_source(source, conversations=[foreign])
    session = FakeSession()
    with pytest.raises(MigrationImportError, match='operator session'):
        import_sqlite_authority(
            source, session, tmp_path / 'progress.json', str(uuid4()))
    assert session.conversations == {}


def test_uncheckpointed_extra_conversation_is_rejected(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, conversations=_sample_conversations()[:1])
    session = FakeSession()
    session.conversations['ghost'] = {
        'id': 'ghost', 'user_id': _OWNER_ID, 'title': 'Ghost',
        'created_at': 1, 'updated_at': 1, 'settings': {},
        'msg_count': 0, 'rev': 0,
    }
    with pytest.raises(MigrationImportError, match='extra conversation'):
        import_sqlite_authority(
            source, session, tmp_path / 'progress.json', str(uuid4()))
    assert set(session.conversations) == {'ghost'}


def test_supplied_binding_cannot_bypass_changed_source(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_records())
    session = FakeSession()
    authority = str(uuid4())
    binding = import_sqlite_authority(source, session, tmp_path / 'progress.json', authority)
    with sqlite3.connect(source) as connection:
        connection.execute('UPDATE storage_records SET version=version+1')
    with pytest.raises(MigrationImportError, match='supplied migration binding'):
        import_sqlite_authority(source, session, tmp_path / 'another.json', authority,
                                binding=binding)
    assert not (tmp_path / 'another.json').exists()


@pytest.mark.parametrize('field,value', [('low_balance_alert_micro', 99), ('updated_at', 30)])
def test_wallet_snapshot_preserves_metadata_beyond_last_ledger_entry(tmp_path, field, value):
    from lib.storage_sidecar.tofudb_migration_checkpoint import MigrationCheckpoint
    source = tmp_path / 'source.db'
    wallets, ledger = _sample_billing()
    wallets[0][field] = value
    _seed_source(source, wallets=wallets, ledger=ledger)
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    session = FakeSession()
    binding = import_sqlite_authority(source, session, checkpoint, authority)
    assert session.wallets['11'][field] == value
    with MigrationCheckpoint(checkpoint, binding) as progress:
        assert progress.domain('billing')['complete'] is True


def _sample_task_results():
    return [
        {
            'namespace': 'task_results', 'record_key': 'task-alpha',
            'value': {
                'task_id': 'task-alpha', 'user_id': _OWNER_ID,
                'conv_id': 'conv-alpha', 'status': 'completed',
                'created_at': 1000, 'completed_at': 2000,
                'content': 'alpha-result',
                'metadata': {'model': 'alpha'}, 'error': None,
            },
            'version': 7, 'updated_at_ms': 123_456,
        },
        {
            'namespace': 'task_results', 'record_key': 'task-running',
            'value': {
                'task_id': 'task-running', 'user_id': _OWNER_ID,
                'conv_id': 'conv-alpha', 'status': 'running',
                'abort_requested_at': 41, 'abort_source': 'user',
            },
            'version': 3, 'updated_at_ms': 40,
        },
        {
            'namespace': 'task_results', 'record_key': 'task-cost',
            'value': {
                'user_id': _OWNER_ID, 'conv_id': 'conv-beta',
                'status': 'completed', 'completed_at': 9,
                'metadata': {'costExperiment': {
                    'experimentId': 'exp-1', 'totalCostMicro': 5}},
            },
            'version': 1, 'updated_at_ms': 10,
        },
    ]


def test_import_replays_task_results_with_derived_indexes(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_task_results())
    session = FakeSession()
    binding = import_sqlite_authority(
        source, session, tmp_path / 'progress.json', str(uuid4()), batch_rows=1,
    )
    assert binding.plan_sha256
    assert set(session.task_results) == {'task-alpha', 'task-cost', 'task-running'}
    alpha = session.task_results['task-alpha']
    assert alpha['version'] == 7
    assert alpha['updated_at_ms'] == 123_456
    assert json.loads(alpha['value_json'])['content'] == 'alpha-result'
    listed = session._task_result_scan({'table': 'headers', 'cursor': None}).json()
    assert [row['task_id'] for row in listed['rows']] == [
        'task-alpha', 'task-cost', 'task-running',
    ]
    live = session._task_result_scan({'table': 'live', 'cursor': None}).json()
    assert [row['task_id'] for row in live['rows']] == ['task-running']
    cost = session._task_result_scan({'table': 'cost_index', 'cursor': None}).json()
    assert [row['task_id'] for row in cost['rows']] == ['task-cost']
    # A resumed import revalidates the complete prefix without rewriting it.
    before = {key: dict(value) for key, value in session.task_results.items()}
    binding_again = import_sqlite_authority(
        source, session, tmp_path / 'progress.json', binding.target_authority_uuid,
    )
    assert binding_again.source_sha256 == binding.source_sha256
    assert session.task_results == before


def test_import_resumes_task_results_after_verified_prefix_crash(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_task_results())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'

    def crash(domain, entry):
        if domain == 'task_results' and entry['rows'] == 1 and not entry['complete']:
            raise _Crash(domain)

    with pytest.raises(_Crash):
        import_sqlite_authority(
            source, session, checkpoint, authority, batch_rows=1,
            after_verified_prefix=crash,
        )
    assert set(session.task_results) == {'task-alpha'}
    binding = import_sqlite_authority(
        source, session, checkpoint, authority, batch_rows=1,
    )
    assert set(session.task_results) == {'task-alpha', 'task-cost', 'task-running'}
    assert binding.source_schema_version == SCHEMA_VERSION


def test_task_result_divergent_or_extra_target_state_fails_closed(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_task_results())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'

    def crash(domain, entry):
        if domain == 'task_results' and entry['rows'] == 1 and not entry['complete']:
            raise _Crash(domain)

    with pytest.raises(_Crash):
        import_sqlite_authority(
            source, session, checkpoint, authority, batch_rows=1,
            after_verified_prefix=crash,
        )
    session.task_results['task-alpha']['version'] = 99
    with pytest.raises(MigrationImportError, match='no longer matches|differ'):
        import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)
    session.task_results['task-alpha']['version'] = 7
    ghost = dict(session.task_results['task-alpha'])
    ghost['task_id'] = 'task-ghost'
    session.task_results['task-ghost'] = ghost
    with pytest.raises(MigrationImportError, match='extra task'):
        import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)
    del session.task_results['task-ghost']
    binding = import_sqlite_authority(
        source, session, checkpoint, authority, batch_rows=1,
    )
    assert set(session.task_results) == {'task-alpha', 'task-cost', 'task-running'}
    assert binding.source_schema_version == SCHEMA_VERSION


def test_task_result_foreign_owner_rows_fail_before_any_write(tmp_path):
    source = tmp_path / 'source.db'
    foreign = {
        'namespace': 'task_results', 'record_key': 'task-foreign',
        'value': {'user_id': 99, 'status': 'completed'},
        'version': 1, 'updated_at_ms': 1,
    }
    _seed_source(source, records=_sample_task_results()[:1] + [foreign])
    session = FakeSession()
    with pytest.raises(MigrationImportError, match='outside the operator scope'):
        import_sqlite_authority(
            source, session, tmp_path / 'progress.json', str(uuid4()))
    assert session.task_results == {}


def test_generic_record_import_coexists_with_task_results_domain(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_task_results() + _sample_records())
    session = FakeSession()
    binding = import_sqlite_authority(
        source, session, tmp_path / 'progress.json', str(uuid4()))
    assert set(session.task_results) == {'task-alpha', 'task-cost', 'task-running'}
    assert set(session.records) == {('prefs', 'alpha'), ('prefs', 'beta'), _SCHEMA_ARCHIVE_RECORD_KEY}
    assert binding.source_schema_version == SCHEMA_VERSION


def _sample_continuation_records():
    conv = hashlib.sha256(b'conversation-alpha').hexdigest()
    lane = hashlib.sha256(b'lane-main').hexdigest()
    return [
        {'namespace': 'model.continuation.v1',
         'record_key': f'{_OWNER_ID}:{conv}:{lane}',
         'value_json': json.dumps({'turns': [['turn-1', 1]], 'summary': 's'}),
         'version': 4, 'updated_at_ms': 1_700_000_000_200},
    ]


def test_continuation_records_import_through_dedicated_domain(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, credentials=_sample_credentials(),
                 records=_sample_continuation_records() + _sample_records())
    session = FakeSession()
    binding = import_sqlite_authority(
        source, session, tmp_path / 'progress.json', str(uuid4()))
    assert binding.source_schema_version == SCHEMA_VERSION
    # Continuation rows land in the dedicated domain, not the generic records store.
    assert set(session.continuations) == {
        row['record_key'] for row in _sample_continuation_records()}
    assert not any(namespace == 'model.continuation.v1'
                   for namespace, _ in session.records)
    assert set(session.records) == {(row['namespace'], row['record_key'])
                                    for row in _sample_records()} | {_SCHEMA_ARCHIVE_RECORD_KEY}


def test_continuation_records_resume_after_verified_prefix_crash(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_continuation_records() + _sample_records())
    session = FakeSession()
    checkpoint = tmp_path / 'progress.json'
    authority = str(uuid4())

    def crash(domain, entry):
        if domain == 'records' and not entry['complete']:
            raise _Crash(domain)

    with pytest.raises(_Crash):
        import_sqlite_authority(
            source, session, checkpoint, authority, batch_rows=1,
            after_verified_prefix=crash)
    binding = import_sqlite_authority(
        source, session, checkpoint, authority, batch_rows=1)
    assert binding.source_schema_version == SCHEMA_VERSION
    assert set(session.continuations) == {
        row['record_key'] for row in _sample_continuation_records()}


def test_continuation_rejects_divergent_target_on_resume(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_continuation_records())
    session = FakeSession()
    checkpoint = tmp_path / 'progress.json'
    authority = str(uuid4())
    import_sqlite_authority(source, session, checkpoint, authority)
    # Tamper with the restored snapshot; resuming must fail closed.
    key = _sample_continuation_records()[0]['record_key']
    session.continuations[key]['value_json'] = json.dumps({'turns': []})
    with pytest.raises(MigrationImportError, match='differ|no longer matches'):
        import_sqlite_authority(source, session, checkpoint, authority)


def _sample_paper_records():
    return [
        {'namespace': 'paper.document.v1', 'record_key': '11:' + 'd' * 64,
         'value': {'document_id': 'd' * 64, 'status': 'ready', 'generation': 2},
         'version': 3, 'updated_at_ms': 1_700_000_000_100},
        {'namespace': 'paper.document.source.v1', 'record_key': '11:binding',
         'value': {'document_id': 'd' * 64}},
        {'namespace': 'paper.document.current.v1', 'record_key': '11:conv',
         'value': {'document_id': 'd' * 64}},
        {'namespace': 'paper.document.budget.v1', 'record_key': 'capacity',
         'value': {'count': 1}},
        {'namespace': 'paper.build.artifact.v1', 'record_key': '11:' + 'd' * 64 + ':b' * 32,
         'value': {'state': 'pinned', 'next_sequence': 1}},
        {'namespace': 'paper.build.chunk.v1',
         'record_key': '11:' + 'd' * 64 + ':b' * 32 + ':0',
         'value': {'data': 'QUJD', 'sha256': hashlib.sha256(b'ABC').hexdigest()}},
        {'namespace': 'paper.build.document.v1', 'record_key': '11:' + 'd' * 64,
         'value': {'build_id': 'b' * 32}},
        {'namespace': 'paper.build.budget.v1', 'record_key': 'capacity',
         'value': {'records': {}}},
    ]


_BUDGET_NAMESPACES = {'paper.build.budget.v1', 'paper.document.budget.v1'}


def test_paper_records_import_through_generic_and_budget_domains(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_paper_records() + _sample_records())
    session = FakeSession()
    binding = import_sqlite_authority(
        source, session, tmp_path / 'progress.json', str(uuid4()))
    expected = {(row['namespace'], row['record_key'])
                for row in _sample_paper_records() + _sample_records()
                if row['namespace'] not in _BUDGET_NAMESPACES}
    assert set(session.records) == expected | {_SCHEMA_ARCHIVE_RECORD_KEY}
    for row in _sample_paper_records():
        if row['namespace'] in _BUDGET_NAMESPACES:
            continue
        stored = session.records[(row['namespace'], row['record_key'])]
        assert json.loads(stored['value_json']) == row['value']
        assert stored['version'] == row.get('version', 1)
        assert stored['updated_at_ms'] == row.get('updated_at_ms', 1_700_000_000_000)
    document_budget = session.paper_budgets['paper.document.budget.v1']
    assert json.loads(document_budget['value_json']) == {'count': 1, 'associations': 0}
    build_budget = session.paper_budgets['paper.build.budget.v1']
    assert json.loads(build_budget['value_json']) == {'records': {}}
    assert binding.source_schema_version == SCHEMA_VERSION


def _crash_record_commands(monkey, *, after: int):
    """Crash the importer after `after` verified record restores, pre-checkpoint."""
    original = importer._command
    count = {'restores': 0}

    def flaky(session, operation, payload):
        result = original(session, operation, payload)
        if operation == 'migration.record.restore' and payload['namespace'].startswith('paper.'):
            count['restores'] += 1
            if count['restores'] == after:
                raise _Crash('after verified paper restore')
        return result

    monkey.setattr(importer, '_command', flaky)


def test_paper_records_resume_after_verified_prefix_crash(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_paper_records())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    with pytest.MonkeyPatch.context() as monkey:
        _crash_record_commands(monkey, after=2)
        with pytest.raises(_Crash):
            importer.import_sqlite_authority(source, session, checkpoint, authority,
                                             batch_rows=2)
    binding = importer.import_sqlite_authority(source, session, checkpoint, authority,
                                               batch_rows=2)
    expected = {(row['namespace'], row['record_key']) for row in _sample_paper_records()
                if row['namespace'] not in _BUDGET_NAMESPACES}
    assert set(session.records) == expected | {_SCHEMA_ARCHIVE_RECORD_KEY}
    assert set(session.paper_budgets) == _BUDGET_NAMESPACES
    assert binding.source_schema_version == SCHEMA_VERSION


def test_paper_budgets_resume_after_verified_prefix_crash(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_paper_records())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    original = importer._command

    def flaky(session_, operation, payload):
        result = original(session_, operation, payload)
        if operation == 'migration.paper.document.budget.restore':
            raise _Crash('after verified build budget restore')
        return result

    with pytest.MonkeyPatch.context() as monkey:
        monkey.setattr(importer, '_command', flaky)
        with pytest.raises(_Crash):
            importer.import_sqlite_authority(source, session, checkpoint, authority)
    binding = importer.import_sqlite_authority(source, session, checkpoint, authority)
    assert set(session.paper_budgets) == _BUDGET_NAMESPACES
    document_budget = session.paper_budgets['paper.document.budget.v1']
    assert json.loads(document_budget['value_json']) == {'count': 1, 'associations': 0}
    assert binding.source_schema_version == SCHEMA_VERSION


def test_paper_budget_rejects_divergent_target_on_resume(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_paper_records())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    importer.import_sqlite_authority(source, session, checkpoint, authority)
    # Tamper with the restored ledger; resuming must fail closed.
    session.paper_budgets['paper.build.budget.v1']['value_json'] = (
        '{"records":{"11:foreign:build":1}}')
    with pytest.raises(MigrationImportError, match='differ|no longer matches'):
        importer.import_sqlite_authority(source, session, checkpoint, authority)


def test_paper_records_reject_divergent_target_on_resume(tmp_path):
    source = tmp_path / 'source.db'
    rows = _sample_paper_records()
    _seed_source(source, records=rows)
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    with pytest.MonkeyPatch.context() as monkey:
        _crash_record_commands(monkey, after=2)
        with pytest.raises(_Crash):
            importer.import_sqlite_authority(source, session, checkpoint, authority,
                                             batch_rows=2)
    # Diverge a row already restored but not yet checkpointed; the catalog
    # revalidation on resume must refuse it before any further write.
    assert session.records
    key = next(iter(session.records))
    session.records[key] = {**session.records[key], 'value_json': '{"tampered":true}'}
    with pytest.raises(MigrationImportError):
        importer.import_sqlite_authority(source, session, checkpoint, authority,
                                         batch_rows=2)

def test_offline_child_rejects_wrong_identity_and_live_lease_and_releases_on_close(tmp_path):
    binary = _resolve_tofudb_binary()
    target = tmp_path / 'target'
    authority = _initialize(binary, target)
    with pytest.raises(MigrationTargetError, match='identity'):
        OfflineMigrationSession(binary, target, str(uuid4()), owner_id=11, tenant_id=7)
    with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7):
        with pytest.raises(MigrationTargetError, match='exited'):
            OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7)
    with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7) as session:
        with pytest.raises(TimeoutError):
            session.request('migration.record.scan', {'cursor': None}, command_id=None,
                            deadline_unix_ms=1)
        assert session._process.poll() is not None


@pytest.mark.parametrize('failure', ['truncated', 'oversized', 'wrong-correlation', 'stall'])
def test_private_transport_rejects_broken_children_and_reaps_them(tmp_path, failure):
    authority = str(uuid4())
    child = tmp_path / 'child'
    ready = json.dumps({'format': 'tofu-db.offline-migration.v1', 'authority_uuid': authority,
                        'owner_id': 11, 'tenant_id': 7}) + '\n'
    prefix = f'#!{sys.executable}\nimport sys, time\n'
    if failure == 'truncated':
        body = 'sys.stdout.write("{"); sys.stdout.flush()\n'
    elif failure == 'oversized':
        body = 'sys.stdout.write("x" * 4097); sys.stdout.flush()\n'
    elif failure == 'stall':
        body = 'time.sleep(30)\n'
    else:
        body = (f'sys.stdout.write({ready!r}); sys.stdout.flush()\n'
                'sys.stdin.readline()\n'
                'print(\'{"request_id":999,"ok":true,"value":null}\', flush=True)\n')
    child.write_text(prefix + body)
    child.chmod(0o700)
    if failure == 'wrong-correlation':
        with OfflineMigrationSession(child, tmp_path, authority, owner_id=11, tenant_id=7) as session:
            with pytest.raises(MigrationTargetError, match='correlation'):
                session.request('migration.record.scan', {'cursor': None}, command_id=None,
                                deadline_unix_ms=int(time.time()*1000)+15000)
            assert session._process.poll() is not None
    else:
        with pytest.raises((MigrationTargetError, TimeoutError)):
            OfflineMigrationSession(child, tmp_path, authority, owner_id=11, tenant_id=7,
                                    timeout=0.1)


def _sample_turns():
    return [
        {
            'turn_id': 'turn-alpha-1', 'conversation_id': 'conv-alpha',
            'user_id': _OWNER_ID, 'ordinal': 1, 'actor': 'human',
            'kind': 'prompt', 'status': 'settled',
            'projection': {'timestamp': 2000, 'segments': [{'text': 'hello'}]},
            'projection_revision': 2,
            'settlement': {'usage': {'input': 3}},
            'created_at': 2000, 'updated_at': 2100,
        },
        {
            'turn_id': 'turn-alpha-2', 'conversation_id': 'conv-alpha',
            'user_id': _OWNER_ID, 'ordinal': 2, 'actor': 'assistant',
            'parent_turn_id': 'turn-alpha-1', 'run_id': 'run-1',
            'projection': {'timestamp': 2200},
            'settlement': {'usage': {'output': 5}},
            'created_at': 2200, 'updated_at': 2300,
        },
        {
            'turn_id': 'turn-alpha-branch', 'conversation_id': 'conv-alpha',
            'user_id': _OWNER_ID, 'lane_id': 'branch-1',
            'parent_turn_id': 'turn-alpha-1', 'ordinal': 1, 'actor': 'assistant',
            'projection': {}, 'settlement': {},
            'created_at': 2400, 'updated_at': 2400,
        },
        {
            'turn_id': 'turn-beta-1', 'conversation_id': 'conv-beta',
            'user_id': _OWNER_ID, 'ordinal': 1, 'actor': 'human',
            'kind': 'prompt', 'projection': {'timestamp': 1500},
            'settlement': {}, 'created_at': 1500, 'updated_at': 1600,
        },
    ]


def test_import_replays_turns_with_exact_snapshots(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(
        source, conversations=_sample_conversations(), turns=_sample_turns(),
    )
    session = FakeSession()
    authority = str(uuid4())
    binding = import_sqlite_authority(
        source, session, tmp_path / 'progress.json', authority, batch_rows=2,
    )
    assert binding.target_authority_uuid == authority
    assert set(session.turns) == {
        'turn-alpha-1', 'turn-alpha-2', 'turn-alpha-branch', 'turn-beta-1',
    }
    restored = session.turns['turn-alpha-2']
    assert restored['parent_turn_id'] == 'turn-alpha-1'
    assert restored['lane_id'] == 'main'
    assert restored['projection'] == {'timestamp': 2200}
    assert restored['settlement'] == {'usage': {'output': 5}}
    assert restored['projection_revision'] == 1
    assert restored['current_attempt_id'] is None
    branch = session.turns['turn-alpha-branch']
    assert branch['lane_id'] == 'branch-1'
    assert branch['ordinal'] == 1
    binding_again = import_sqlite_authority(
        source, session, tmp_path / 'progress.json', authority,
    )
    assert binding_again.source_sha256 == binding.source_sha256


def test_import_turns_resumes_after_verified_prefix_crash(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(
        source, conversations=_sample_conversations(), turns=_sample_turns(),
    )
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'

    def crash(domain, entry):
        if domain == 'turns' and entry['rows'] == 1 and not entry['complete']:
            raise _Crash(domain)

    with pytest.raises(_Crash):
        import_sqlite_authority(
            source, session, checkpoint, authority, batch_rows=1,
            after_verified_prefix=crash,
        )
    assert len(session.turns) == 1
    import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)
    assert len(session.turns) == 4


def test_import_turns_rejects_divergent_target_on_resume(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(
        source, conversations=_sample_conversations(), turns=_sample_turns(),
    )
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'

    def crash(domain, entry):
        if domain == 'turns' and entry['rows'] == 1 and not entry['complete']:
            raise _Crash(domain)

    with pytest.raises(_Crash):
        import_sqlite_authority(
            source, session, checkpoint, authority, batch_rows=1,
            after_verified_prefix=crash,
        )
    session.turns['turn-alpha-1']['settlement'] = {'usage': {'input': 99}}
    with pytest.raises(MigrationImportError, match='no longer matches|snapshots differ'):
        import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)


def test_import_turns_rejects_live_attempt_and_missing_conversation(tmp_path):
    source = tmp_path / 'source.db'
    live = dict(_sample_turns()[0])
    live['current_attempt_id'] = 'attempt-1'
    _seed_source(source, conversations=_sample_conversations(), turns=[live])
    session = FakeSession()
    with pytest.raises(MigrationImportError, match='live attempt'):
        import_sqlite_authority(source, session, tmp_path / 'progress.json', str(uuid4()))

    orphan = tmp_path / 'orphan.db'
    _seed_source(
        orphan, conversations=_sample_conversations(),
        turns=[{
            'turn_id': 'turn-orphan', 'conversation_id': 'conv-missing',
            'user_id': _OWNER_ID, 'ordinal': 1, 'actor': 'human',
            'projection': {}, 'settlement': {},
            'created_at': 100, 'updated_at': 100,
        }],
    )
    with pytest.raises(StorageV2Error, match='conversation not found'):
        import_sqlite_authority(
            orphan, FakeSession(), tmp_path / 'orphan-progress.json', str(uuid4()),
        )


def test_import_turns_rejects_foreign_owner_before_writing(tmp_path):
    source = tmp_path / 'source.db'
    foreign = dict(_sample_turns()[0])
    foreign['user_id'] = _OWNER_ID + 100
    _seed_source(source, conversations=_sample_conversations(), turns=[foreign])
    session = FakeSession()
    with pytest.raises(MigrationImportError, match='outside the operator scope'):
        import_sqlite_authority(source, session, tmp_path / 'progress.json', str(uuid4()))
    assert not session.turns


def _sample_desktop_egress():
    return [{'owner_user_id': _OWNER_ID, 'agent_id': 'egress-agent-1',
             'updated_at_ms': 1_700_000_111_000}]


def test_import_replays_desktop_egress_preserving_exact_state(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, desktop_egress=_sample_desktop_egress())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    binding = import_sqlite_authority(
        source, session, checkpoint, authority, batch_rows=16,
    )
    assert binding.plan_sha256
    assert session.desktop_egress == {'agent_id': 'egress-agent-1',
                                      'updated_at_ms': 1_700_000_111_000}
    # Replaying the identical snapshot is a read-only no-op.
    binding_again = import_sqlite_authority(
        source, session, checkpoint, authority,
    )
    assert binding_again == binding
    assert session.desktop_egress == {'agent_id': 'egress-agent-1',
                                      'updated_at_ms': 1_700_000_111_000}


def test_import_desktop_egress_rejects_divergent_target_and_foreign_owner(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, desktop_egress=_sample_desktop_egress())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    import_sqlite_authority(source, session, checkpoint, authority)
    # A divergent occupant on the target fails closed instead of overwriting.
    session.desktop_egress = {'agent_id': 'other-agent', 'updated_at_ms': 1}
    with pytest.raises(StorageV2Error, match='desktop egress snapshot differs'):
        import_sqlite_authority(
            source, session, tmp_path / 'fresh.json', str(uuid4()),
        )

    foreign = tmp_path / 'foreign.db'
    _seed_source(
        foreign,
        desktop_egress=[{'owner_user_id': _OWNER_ID + 100, 'agent_id': 'x',
                         'updated_at_ms': 5}],
    )
    clean = FakeSession()
    with pytest.raises(MigrationImportError, match='outside the operator scope'):
        import_sqlite_authority(
            foreign, clean, tmp_path / 'foreign-progress.json', str(uuid4()),
        )
    assert clean.desktop_egress is None


def test_import_desktop_egress_resumes_after_verified_prefix_crash(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, desktop_egress=_sample_desktop_egress())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'

    def crash(domain, entry):
        if domain == 'desktop_egress' and entry['complete']:
            raise _Crash(domain)

    with pytest.raises(_Crash):
        import_sqlite_authority(
            source, session, checkpoint, authority,
            after_verified_prefix=crash,
        )
    # The row is already restored; a resume re-verifies the complete prefix
    # and finishes without rewriting.
    import_sqlite_authority(source, session, checkpoint, authority)
    assert session.desktop_egress == {'agent_id': 'egress-agent-1',
                                      'updated_at_ms': 1_700_000_111_000}


def _sample_daily_costs():
    return [
        {'user_id': _OWNER_ID, 'date': '2026-08-10', 'cost': 1.5,
         'conversations': {'conv-alpha': 1.0}, 'computed_at': 1_700_100_000_000},
        {'user_id': _OWNER_ID, 'date': '2026-08-11', 'cost': 2.25,
         'conversations': {'conv-beta': 2.25}, 'computed_at': 1_700_200_000_000},
    ]


def test_import_replays_daily_cost_preserving_exact_state(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, daily_costs=_sample_daily_costs())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    binding = import_sqlite_authority(
        source, session, checkpoint, authority, batch_rows=1,
    )
    assert binding.plan_sha256
    assert set(session.daily_costs) == {'2026-08-10', '2026-08-11'}
    assert session.daily_costs['2026-08-10'] == {
        'date': '2026-08-10', 'cost': 1.5,
        'conversations': {'conv-alpha': 1.0}, 'computed_at': 1_700_100_000_000,
    }
    # Identical replay is read-only and binds identically.
    binding_again = import_sqlite_authority(
        source, session, checkpoint, authority,
    )
    assert binding_again == binding
    assert session.daily_costs['2026-08-11']['cost'] == 2.25


def test_import_daily_cost_rejects_divergent_target_and_foreign_owner(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, daily_costs=_sample_daily_costs())
    session = FakeSession()
    authority = str(uuid4())
    import_sqlite_authority(source, session, tmp_path / 'progress.json', authority)
    session.daily_costs['2026-08-10'] = {
        'date': '2026-08-10', 'cost': 9.9, 'conversations': {}, 'computed_at': 1,
    }
    with pytest.raises(StorageV2Error, match='daily cost snapshot differs'):
        import_sqlite_authority(
            source, session, tmp_path / 'fresh.json', str(uuid4()),
        )

    foreign = tmp_path / 'foreign.db'
    _seed_source(
        foreign,
        daily_costs=[{'user_id': _OWNER_ID + 100, 'date': '2026-08-10',
                      'cost': 1.0, 'computed_at': 1_700_100_000_000}],
    )
    clean = FakeSession()
    with pytest.raises(MigrationImportError, match='outside the operator scope'):
        import_sqlite_authority(
            foreign, clean, tmp_path / 'foreign-progress.json', str(uuid4()),
        )
    assert not clean.daily_costs


def test_import_daily_cost_resumes_after_verified_prefix_crash(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, daily_costs=_sample_daily_costs())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'

    def crash(domain, entry):
        if domain == 'daily_cost' and entry['rows'] == 1 and not entry['complete']:
            raise _Crash(domain)

    with pytest.raises(_Crash):
        import_sqlite_authority(
            source, session, checkpoint, authority, batch_rows=1,
            after_verified_prefix=crash,
        )
    import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)
    assert set(session.daily_costs) == {'2026-08-10', '2026-08-11'}


def _sample_rate_limit_events():
    return [
        {'event_id': 'event-1', 'endpoint': '/v1/chat',
         'client_key': '198.51.100.8', 'occurred_at_ms': 1_700_100_000_000,
         'expires_at_ms': 1_700_100_060_000},
        {'event_id': 'event-2', 'endpoint': '/v1/chat',
         'client_key': '198.51.100.8', 'occurred_at_ms': 1_700_100_001_000,
         'expires_at_ms': 1_700_100_061_000},
    ]


def test_import_replays_rate_limit_events_preserving_exact_state(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, rate_limit_events=_sample_rate_limit_events())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    binding = import_sqlite_authority(
        source, session, checkpoint, authority, batch_rows=1,
    )
    assert binding.plan_sha256
    assert set(session.rate_limit_events) == {'event-1', 'event-2'}
    assert session.rate_limit_events['event-1'] == {
        'event_id': 'event-1', 'endpoint': '/v1/chat',
        'client_key': '198.51.100.8', 'occurred_at_ms': 1_700_100_000_000,
        'expires_at_ms': 1_700_100_060_000,
    }
    # Identical replay is read-only and binds identically.
    binding_again = import_sqlite_authority(
        source, session, checkpoint, authority,
    )
    assert binding_again == binding
    assert session.rate_limit_events['event-2']['occurred_at_ms'] == 1_700_100_001_000


def test_import_rate_limit_events_rejects_divergent_target(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, rate_limit_events=_sample_rate_limit_events())
    session = FakeSession()
    authority = str(uuid4())
    import_sqlite_authority(source, session, tmp_path / 'progress.json', authority)
    session.rate_limit_events['event-1'] = {
        'event_id': 'event-1', 'endpoint': '/v1/other',
        'client_key': '198.51.100.8', 'occurred_at_ms': 1, 'expires_at_ms': 2,
    }
    with pytest.raises(StorageV2Error, match='rate limit snapshot differs'):
        import_sqlite_authority(
            source, session, tmp_path / 'fresh.json', str(uuid4()),
        )


def test_import_rate_limit_events_resumes_after_verified_prefix_crash(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, rate_limit_events=_sample_rate_limit_events())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'

    def crash(domain, entry):
        if domain == 'rate_limit_events' and entry['rows'] == 1 and not entry['complete']:
            raise _Crash(domain)

    with pytest.raises(_Crash):
        import_sqlite_authority(
            source, session, checkpoint, authority, batch_rows=1,
            after_verified_prefix=crash,
        )
    import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)
    assert set(session.rate_limit_events) == {'event-1', 'event-2'}



def _sample_orchestration():
    definitions = [
        {'id': 'def-alpha', 'user_id': _OWNER_ID, 'tenant_id': '',
         'name': 'alpha', 'definition': {'name': 'alpha', 'steps': []},
         'created_at_ms': 1_700_000_100_000, 'updated_at_ms': 1_700_000_200_000},
    ]
    runs = [
        {'id': 'run-alpha', 'user_id': _OWNER_ID, 'tenant_id': '',
         'orch_id': 'def-alpha', 'name': 'alpha run',
         'definition': {'name': 'alpha'}, 'input': 'objective',
         'status': 'running', 'final': '', 'error': '', 'created_by': '',
         'created_at': 1_700_000_300_000, 'updated_at': 1_700_000_400_000,
         'finished_at': 0},
        {'id': 'run-beta', 'user_id': _OWNER_ID, 'tenant_id': '',
         'orch_id': 'def-alpha', 'name': 'beta run',
         'definition': {'name': 'beta'}, 'input': 'other',
         'status': 'done', 'final': 'finished', 'error': '', 'created_by': '',
         'created_at': 1_700_000_500_000, 'updated_at': 1_700_000_600_000,
         'finished_at': 1_700_000_600_000},
    ]
    events = [
        {'run_id': 'run-alpha', 'user_id': _OWNER_ID, 'tenant_id': '', 'seq': 0,
         'type': 'node_started', 'node_id': 'n1',
         'payload': {'type': 'node_started'}, 'ts': 1_700_000_350_000},
    ]
    return definitions, runs, events


def test_import_replays_orchestration_preserving_exact_state(tmp_path):
    definitions, runs, events = _sample_orchestration()
    source = tmp_path / 'source.db'
    _seed_source(
        source,
        orchestration_definitions=definitions,
        orchestration_runs=runs,
        orchestration_events=events,
    )
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    binding = import_sqlite_authority(
        source, session, checkpoint, authority, batch_rows=16,
    )
    assert binding.plan_sha256
    assert set(session.orch_definitions) == {('def-alpha', '')}
    assert set(session.orch_runs) == {('run-alpha', ''), ('run-beta', '')}
    alpha = session.orch_runs[('run-alpha', '')]
    assert alpha['status'] == 'running'
    assert alpha['created_at'] == 1_700_000_300_000
    assert alpha['updated_at'] == 1_700_000_400_000
    assert alpha['next_event_sequence'] == 1
    # The single source event restored against its run frontier.
    assert set(session.orch_events) == {('run-alpha', '', 0)}
    # Identical replay is read-only and binds identically.
    binding_again = import_sqlite_authority(
        source, session, checkpoint, authority,
    )
    assert binding_again == binding


def test_import_orchestration_rejects_divergent_target_and_foreign_owner(tmp_path):
    definitions, runs, events = _sample_orchestration()
    source = tmp_path / 'source.db'
    _seed_source(
        source,
        orchestration_definitions=definitions,
        orchestration_runs=runs,
        orchestration_events=events,
    )
    session = FakeSession()
    authority = str(uuid4())
    import_sqlite_authority(source, session, tmp_path / 'progress.json', authority)
    # A divergent run occupant on the target fails closed.
    divergent = dict(session.orch_runs[('run-alpha', '')])
    divergent['status'] = 'aborted'
    session.orch_runs[('run-alpha', '')] = divergent
    with pytest.raises(StorageV2Error, match='orchestration snapshot already differs'):
        import_sqlite_authority(
            source, session, tmp_path / 'fresh.json', str(uuid4()),
        )

    foreign = tmp_path / 'foreign.db'
    _seed_source(
        foreign,
        orchestration_runs=[
            {'id': 'run-x', 'user_id': _OWNER_ID + 100, 'tenant_id': '',
             'created_at': 1, 'updated_at': 1},
        ],
    )
    clean = FakeSession()
    with pytest.raises(MigrationImportError, match='outside the operator scope'):
        import_sqlite_authority(
            foreign, clean, tmp_path / 'foreign-progress.json', str(uuid4()),
        )
    assert not clean.orch_runs


def _sample_swarm():
    sessions = [
        {'swarm_key': 'swarm-1', 'conv_id': 'conv-1', 'task_id': 'task-1',
         'status': 'running', 'specs': [{'role': 'researcher'}],
         'config': {'user_id': _OWNER_ID},
         'created_at': 1_700_000_100_000, 'updated_at': 1_700_000_200_000},
    ]
    agents = [
        {'swarm_key': 'swarm-1', 'agent_id': 'agent-1', 'role': 'researcher',
         'objective': 'find evidence', 'status': 'completed',
         'messages': [{'role': 'user', 'content': 'go'}],
         'result': {'summary': 'done'}, 'rounds_used': 3, 'delivered': False,
         'updated_at': 1_700_000_210_000},
        {'swarm_key': 'swarm-1', 'agent_id': 'agent-2', 'role': 'critic',
         'objective': 'review', 'status': 'completed',
         'messages': [], 'result': {}, 'rounds_used': 1, 'delivered': True,
         'updated_at': 1_700_000_220_000},
    ]
    return sessions, agents


def test_import_replays_swarm_preserving_exact_state(tmp_path):
    sessions, agents = _sample_swarm()
    source = tmp_path / 'source.db'
    _seed_source(source, swarm_sessions=sessions, swarm_agents=agents)
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    binding = import_sqlite_authority(
        source, session, checkpoint, authority, batch_rows=16,
    )
    assert binding.plan_sha256
    assert set(session.swarm_sessions) == {'swarm-1'}
    assert set(session.swarm_agents) == {('swarm-1', 'agent-1'), ('swarm-1', 'agent-2')}
    restored = session.swarm_sessions['swarm-1']
    assert restored['status'] == 'running'
    assert restored['created_at'] == 1_700_000_100_000
    assert restored['updated_at'] == 1_700_000_200_000
    agent = session.swarm_agents[('swarm-1', 'agent-1')]
    assert agent['status'] == 'completed'
    assert agent['delivered'] is False
    assert agent['rounds_used'] == 3
    binding_again = import_sqlite_authority(
        source, session, checkpoint, authority,
    )
    assert binding_again == binding


def test_import_swarm_rejects_divergent_target(tmp_path):
    sessions, agents = _sample_swarm()
    source = tmp_path / 'source.db'
    _seed_source(source, swarm_sessions=sessions, swarm_agents=agents)
    session = FakeSession()
    authority = str(uuid4())
    import_sqlite_authority(source, session, tmp_path / 'progress.json', authority)
    divergent = dict(session.swarm_sessions['swarm-1'])
    divergent['status'] = 'terminated'
    session.swarm_sessions['swarm-1'] = divergent
    with pytest.raises(StorageV2Error, match='swarm snapshot already differs'):
        import_sqlite_authority(
            source, session, tmp_path / 'fresh.json', str(uuid4()),
        )


def _sample_integration():
    workspaces = [
        {'id': 1, 'user_id': _OWNER_ID, 'project_root': '/repo',
         'task_id': 'task-a', 'title': 'alpha', 'workspace_path': '/repo/.wt/a',
         'managed': True, 'state': 'ready', 'base_sha': 'base',
         'created_at': 1_700_000_100.0, 'updated_at': 1_700_000_200.0},
        {'id': 2, 'user_id': _OWNER_ID, 'project_root': '/repo',
         'task_id': 'task-b', 'title': 'beta', 'workspace_path': '/repo/.wt/b',
         'managed': False, 'state': 'running', 'base_sha': 'base',
         'created_at': 1_700_000_300.0, 'updated_at': 1_700_000_400.0},
    ]
    events = [
        {'id': 1, 'user_id': _OWNER_ID, 'project_root': '/repo',
         'task_id': 'task-a', 'kind': 'registered', 'message': 'registered',
         'detail': '/repo/.wt/a', 'created_at': 1_700_000_150.0},
    ]
    meta = [
        {'user_id': _OWNER_ID, 'project_root': '/repo', 'task_id': 'task-a',
         'origin': {'source': 'writer', 'kind': 'autopilot'},
         'updated_at': 1_700_000_160.0},
    ]
    return workspaces, events, meta


def test_import_replays_integration_preserving_exact_state(tmp_path):
    workspaces, events, meta = _sample_integration()
    source = tmp_path / 'source.db'
    _seed_source(
        source,
        integration_workspaces=workspaces,
        integration_events=events,
        integration_meta=meta,
    )
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    binding = import_sqlite_authority(
        source, session, checkpoint, authority, batch_rows=16,
    )
    assert binding.plan_sha256
    assert set(session.integration_workspaces) == {('/repo', 'task-a'), ('/repo', 'task-b')}
    restored = session.integration_workspaces[('/repo', 'task-a')]
    assert restored['id'] == 1
    assert restored['state'] == 'ready'
    assert restored['updated_at'] == 1_700_000_200.0
    # The meta origin folded into the workspace document.
    assert restored['origin'] == {'source': 'writer', 'kind': 'autopilot'}
    assert set(session.integration_events) == {('/repo', 1)}
    binding_again = import_sqlite_authority(
        source, session, checkpoint, authority,
    )
    assert binding_again == binding


def test_import_integration_rejects_divergent_target_and_foreign_owner(tmp_path):
    workspaces, events, meta = _sample_integration()
    source = tmp_path / 'source.db'
    _seed_source(
        source,
        integration_workspaces=workspaces,
        integration_events=events,
        integration_meta=meta,
    )
    session = FakeSession()
    authority = str(uuid4())
    import_sqlite_authority(source, session, tmp_path / 'progress.json', authority)
    divergent = dict(session.integration_workspaces[('/repo', 'task-a')])
    divergent['state'] = 'failed'
    session.integration_workspaces[('/repo', 'task-a')] = divergent
    with pytest.raises(StorageV2Error, match='integration snapshot already differs'):
        import_sqlite_authority(
            source, session, tmp_path / 'fresh.json', str(uuid4()),
        )

    foreign = tmp_path / 'foreign.db'
    _seed_source(
        foreign,
        integration_workspaces=[
            {'id': 9, 'user_id': _OWNER_ID + 100, 'project_root': '/r',
             'task_id': 't', 'created_at': 1.0, 'updated_at': 1.0},
        ],
    )
    clean = FakeSession()
    with pytest.raises(MigrationImportError, match='outside the operator scope'):
        import_sqlite_authority(
            foreign, clean, tmp_path / 'foreign-progress.json', str(uuid4()),
        )
    assert not clean.integration_workspaces


def _sample_paper():
    library = [
        {'id': 'paper-1', 'user_id': _OWNER_ID, 'title': 'First',
         'pdf_url': '', 'pdf_filename': '', 'arxiv_id': '2401.00001',
         'paper_hash': 'hash-1', 'parsed_text': 'parsed body',
         'parser_version': 'v1', 'qa_history': '[]', 'images': '[]',
         'babel_cache': '{}', 'page_count': 3, 'folder_id': '',
         'created_at': 100, 'updated_at': 200},
    ]
    reports = [
        {'user_id': _OWNER_ID, 'paper_hash': 'hash-1', 'lang': 'en',
         'report': 'report body', 'model': 'model', 'meta': json.dumps({'a': 1}),
         'created_at': 150},
    ]
    translations = [
        {'user_id': _OWNER_ID, 'paper_hash': 'hash-1', 'lang': 'zh',
         'text': 'translated', 'model': 'translator', 'created_at': 160},
    ]
    notes = [
        {'user_id': _OWNER_ID, 'id': 'note-1', 'paper_hash': 'hash-1',
         'lang': 'en', 'anchor': json.dumps({'page': 1}), 'note': 'a note',
         'created_at': 170, 'updated_at': 180},
    ]
    podcasts = [
        {'user_id': _OWNER_ID, 'paper_hash': 'hash-1', 'mode': 'dialogue',
         'lang': 'en', 'voice': 'narrator', 'status': 'done',
         'script_json': json.dumps({'turns': []}), 'file_path': '',
         'duration_sec': 12.5, 'model': 'model', 'tts_model': 'tts',
         'meta': '{}', 'created_at': 190, 'updated_at': 200},
    ]
    return library, reports, translations, notes, podcasts


def _seed_paper(path: Path) -> None:
    library, reports, translations, notes, podcasts = _sample_paper()
    _seed_source(
        path, paper_library=library, paper_reports=reports,
        paper_translations=translations, paper_notes=notes,
        paper_podcasts=podcasts,
    )


def test_import_replays_paper_preserving_exact_state(tmp_path):
    source = tmp_path / 'source.db'
    _seed_paper(source)
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    binding = import_sqlite_authority(
        source, session, checkpoint, authority, batch_rows=1,
    )
    assert binding.plan_sha256
    assert set(session.paper_library) == {'paper-1'}
    assert session.paper_library['paper-1']['parsed_text'] == 'parsed body'
    assert session.paper_reports[('hash-1', 'en')]['report'] == 'report body'
    assert session.paper_translations[('hash-1', 'zh')]['text'] == 'translated'
    assert session.paper_notes['note-1']['note'] == 'a note'
    assert session.paper_podcasts[('hash-1', 'dialogue', 'en', 'narrator')]['status'] == 'done'
    # Identical replay is read-only and binds identically.
    binding_again = import_sqlite_authority(
        source, session, checkpoint, authority,
    )
    assert binding_again == binding


def test_import_paper_rejects_divergent_target_and_foreign_owner(tmp_path):
    source = tmp_path / 'source.db'
    _seed_paper(source)
    session = FakeSession()
    import_sqlite_authority(source, session, tmp_path / 'progress.json', str(uuid4()))
    session.paper_notes['note-1'] = dict(
        session.paper_notes['note-1'], note='changed')
    with pytest.raises(StorageV2Error, match='paper note snapshot differs'):
        import_sqlite_authority(
            source, session, tmp_path / 'fresh.json', str(uuid4()),
        )

    foreign = tmp_path / 'foreign.db'
    _seed_source(
        foreign,
        paper_notes=[{'user_id': _OWNER_ID + 100, 'id': 'n', 'paper_hash': 'h',
                      'lang': 'en', 'anchor': '{}', 'note': 'x',
                      'created_at': 1, 'updated_at': 1}],
    )
    clean = FakeSession()
    with pytest.raises(MigrationImportError, match='outside the operator scope'):
        import_sqlite_authority(
            foreign, clean, tmp_path / 'foreign-progress.json', str(uuid4()),
        )
    assert not clean.paper_notes


def test_import_paper_resumes_after_verified_prefix_crash(tmp_path):
    source = tmp_path / 'source.db'
    _seed_paper(source)
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'

    def crash(domain, entry):
        if domain == 'paper.note' and entry['complete']:
            raise _Crash(domain)

    with pytest.raises(_Crash):
        import_sqlite_authority(
            source, session, checkpoint, authority, batch_rows=1,
            after_verified_prefix=crash,
        )
    import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)
    assert set(session.paper_library) == {'paper-1'}
    assert set(session.paper_notes) == {'note-1'}


def _sample_knowledge():
    documents = [
        {'user_id': _OWNER_ID, 'id': 'doc-1', 'sha256': 'a' * 64,
         'name': 'Spec.PDF', 'stored_name': 'spec.pdf', 'kind': '.PDF',
         'size_bytes': 12, 'method': 'text', 'warnings_json': '[]',
         'text_chars': 11, 'chunk_count': 2, 'pages': 1, 'scope': 'library',
         'media_metadata_json': '{}', 'created_at': 10.0, 'updated_at': 11.0},
    ]
    chunks = [
        {'user_id': _OWNER_ID, 'document_id': 'doc-1', 'ordinal': 0,
         'section': 'Intro', 'location': 'page 1', 'content': 'Hello World',
         'search_text': 'Hello WORLD hello'},
        {'user_id': _OWNER_ID, 'document_id': 'doc-1', 'ordinal': 1,
         'section': 'Body', 'location': 'page 1', 'content': 'Second',
         'search_text': 'second'},
    ]
    assets = [
        {'user_id': _OWNER_ID, 'id': 'asset-1', 'document_id': 'doc-1',
         'ordinal': 0, 'kind': 'image', 'stored_name': 'a.png',
         'mime_type': 'image/png', 'sha256': 'b' * 64, 'size_bytes': 5,
         'width': 1, 'height': 1, 'page': 0, 'pages_json': '[]',
         'bbox_json': '[]', 'caption': '', 'ocr_text': '', 'description': '',
         'enrichment_status': 'pending', 'enrichment_model': '',
         'enrichment_error': '', 'metadata_json': '{}',
         'created_at': 9.0, 'updated_at': 9.0},
    ]
    chunk_assets = [
        {'user_id': _OWNER_ID, 'document_id': 'doc-1', 'chunk_ordinal': 0,
         'asset_id': 'asset-1', 'relation': 'evidence', 'ordinal': 0},
    ]
    terms = [
        {'user_id': _OWNER_ID, 'term': 'hello', 'document_id': 'doc-1',
         'chunk_ordinal': 0},
    ]
    settings = [
        {'user_id': _OWNER_ID, 'enabled': 1, 'visual_enrichment': 0,
         'updated_at': 12.5},
    ]
    return documents, chunks, assets, chunk_assets, terms, settings


def _seed_knowledge(path: Path) -> None:
    documents, chunks, assets, chunk_assets, terms, settings = _sample_knowledge()
    _seed_source(
        path, knowledge_documents=documents, knowledge_chunks=chunks,
        knowledge_assets=assets, knowledge_chunk_assets=chunk_assets,
        knowledge_terms=terms, knowledge_settings=settings,
    )


def test_import_replays_knowledge_preserving_exact_state(tmp_path):
    source = tmp_path / 'source.db'
    _seed_knowledge(source)
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    binding = import_sqlite_authority(
        source, session, checkpoint, authority, batch_rows=1,
    )
    assert binding.plan_sha256
    assert set(session.knowledge_documents) == {'doc-1'}
    document = session.knowledge_documents['doc-1']
    assert document['chunk_count'] == 2
    assert len(document['chunks']) == 2
    assert document['chunks'][0]['assets'] == [{'id': 'asset-1', 'relation': 'evidence'}]
    assert session.knowledge_settings == {
        'enabled': True, 'visual_enrichment': False, 'updated_at': 12.5,
    }
    binding_again = import_sqlite_authority(
        source, session, checkpoint, authority,
    )
    assert binding_again == binding


def test_import_knowledge_rejects_divergent_target_and_foreign_owner(tmp_path):
    source = tmp_path / 'source.db'
    _seed_knowledge(source)
    session = FakeSession()
    import_sqlite_authority(source, session, tmp_path / 'progress.json', str(uuid4()))
    session.knowledge_documents['doc-1'] = dict(
        session.knowledge_documents['doc-1'], name='changed.pdf')
    with pytest.raises(StorageV2Error, match='knowledge document snapshot differs'):
        import_sqlite_authority(
            source, session, tmp_path / 'fresh.json', str(uuid4()),
        )

    foreign = tmp_path / 'foreign.db'
    _seed_source(
        foreign,
        knowledge_documents=[{'user_id': _OWNER_ID + 100, 'id': 'd',
                              'sha256': 'a' * 64, 'name': 'n', 'stored_name': 's',
                              'kind': '.PDF', 'size_bytes': 1, 'method': 'text',
                              'warnings_json': '[]', 'text_chars': 0,
                              'chunk_count': 0, 'pages': 0, 'scope': 'library',
                              'media_metadata_json': '{}',
                              'created_at': 1.0, 'updated_at': 1.0}],
    )
    clean = FakeSession()
    with pytest.raises(MigrationImportError, match='outside the operator scope'):
        import_sqlite_authority(
            foreign, clean, tmp_path / 'foreign-progress.json', str(uuid4()),
        )
    assert not clean.knowledge_documents


# ---------------------------------------------------------------------------
# ARTIFACTS/MISC domain group tests.
# ---------------------------------------------------------------------------


def _sample_misc() -> dict:
    content = '# notes\nbody'
    artifact_sha = hashlib.sha256(content.encode()).hexdigest()
    tool_content = 'x' * 4096
    tool_sha = hashlib.sha256(tool_content.encode()).hexdigest()
    projection = {
        'version': 1,
        'ownerUserId': _OWNER_ID,
        'projectKey': 'proj-1',
        'headSequence': 42,
        'checkpointSequence': 7,
        'narratives': [],
    }
    manifest = {
        'namespace': 'alpha.beta',
        'version': 1,
        'tables': [{
            'name': 'items',
            'columns': [{'name': 'id', 'type': 'string', 'required': True}],
            'primary_key': ['id'],
            'indexes': [{'name': 'by_id', 'columns': ['id'], 'unique': True}],
        }],
        'operations': [{
            'name': 'fetch', 'action': 'get', 'table': 'items', 'limit_max': 100,
        }],
    }
    return {
        'chat_artifacts': [{
            'id': 'art-1', 'conv_id': 'conv-1', 'source': 'chat',
            'format': 'markdown', 'title': 'A', 'content': content,
            'content_sha256': artifact_sha, 'size_bytes': len(content.encode()),
            'version': 1, 'created_at': 424242,
            'source_ref': {'path': 'notes/a.md'},
        }],
        'tool_result_artifacts': [{
            'user_id': _OWNER_ID, 'content_sha256': tool_sha,
            'content': tool_content, 'media_type': 'text/plain',
            'size_bytes': len(tool_content.encode()), 'created_at_ms': 1000,
            'expires_at_ms': 2000, 'last_accessed_at_ms': 1500,
        }],
        'browser_observations': [{
            'owner_user_id': _OWNER_ID, 'origin': 'https://example.com',
            'route_family': 'api', 'operation': 'list', 'schema_version': 1,
            'strategy': 'captured_api', 'hints': [], 'status': 'active',
            'confidence_milli': 500, 'visit_count': 3, 'successful_visits': 3,
            'hinted_visits': 0, 'hint_match_visits': 0,
            'consecutive_failures': 0, 'last_outcome': 'success',
            'last_elapsed_ms': 40, 'last_verified_at_ms': 1000,
            'last_observed_at_ms': 1000,
            'expires_at_ms': 1000 + 30 * 24 * 60 * 60 * 1000,
            'payload_bytes': 128,
        }],
        'browser_observation_owners': [
            {'owner_user_id': _OWNER_ID, 'touched_at_ms': 424242},
        ],
        'routing_authorities': [{
            'owner_user_id': _OWNER_ID, 'tenant_id': 'team-a', 'revision': 5,
            'document': {'contract_version': 'tofu.model-routing/v2', 'revision': 5},
            'backup': {'contract_version': 'tofu.model-routing/v2', 'revision': 4},
            'migration_receipt': {'status': 'complete'},
            'updated_at': 1_700_000_000.5,
        }],
        'routing_secrets': [{
            'owner_user_id': _OWNER_ID, 'tenant_id': 'team-a',
            'secret_reference': 'ref-1',
            'ciphertext': 'dGVzdC1jaXBoZXJ0ZXh0LXBhZGRpbmc',
            'key_hint': 'hint-1',
            'created_at': 1_700_000_000.25, 'updated_at': 1_700_000_000.5,
        }],
        'plugin_manifests': [{
            'namespace': 'alpha.beta', 'manifest_version': 1,
            'manifest': manifest, 'updated_at_ms': 424242,
        }],
        'plugin_rows': [{
            'namespace': 'alpha.beta', 'table_name': 'items',
            'row_key': 'item-1',
            'document_json': '{"id":"item-1","note":"n"}',
            'version': 3, 'updated_at_ms': 999,
        }],
        'plugin_unique_values': [{
            'namespace': 'alpha.beta', 'table_name': 'items',
            'index_name': 'by_id', 'index_value': 'item-1', 'row_key': 'item-1',
        }],
        'project_brain_projects': [{
            'owner_user_id': _OWNER_ID, 'project_key': 'proj-1',
            'head_sequence': 42, 'checkpoint_sequence': 7,
            'projection': projection, 'updated_at_ms': 424242,
        }],
        'optimizer_proposals': [{
            'user_id': _OWNER_ID, 'id': 'p-1',
            'created_at': '2026-08-01T00:00:00Z', 'title': 't',
            'rationale': 'r', 'action_type': 'cleanup', 'action_args': '{}',
            'severity': 'low', 'confidence': 0.5, 'evidence': 'e',
            'status': 'pending_review', 'status_reason': '',
        }],
        'optimizer_actions': [{
            'user_id': _OWNER_ID, 'id': 'a-1', 'proposal_id': 'p-1',
            'applied_at': '2026-08-02T00:00:00Z',
            'expires_at': '2026-09-02T00:00:00Z', 'pre_metric': '{}',
        }],
        'log_aggregates': [{
            'fingerprint': 'fp-1', 'level': 'ERROR', 'logger': 'app',
            'template': 'disk full: {}', 'sample': 'disk full: /dev/sda',
            'count': 5, 'first_seen': 1000, 'last_seen': 2000,
        }],
        'billing_payments': [{
            'id': 'pay-1', 'user_id': 'alice', 'provider': 'stripe',
            'provider_id': 'pi_1', 'amount_minor': 1200, 'currency': 'USD',
            'credit_micro': 10_000_000, 'status': 'settled',
            'created_at': 100, 'settled_at': 200, 'raw': {'k': 'v'},
        }],
        'billing_redeem_codes': [
            {'code': 'code-a', 'amount_micro': 75, 'batch': 'batch-a',
             'created_by': 'test', 'created_at': 100, 'note': 'n'},
            {'code': 'code-b', 'amount_micro': 75, 'batch': 'batch-a',
             'created_by': 'test', 'created_at': 100, 'note': 'n',
             'redeemed_by': 'alice', 'redeemed_at': 150},
            {'code': 'code-c', 'amount_micro': 75, 'batch': 'batch-a',
             'created_by': 'test', 'created_at': 100, 'note': 'n'},
        ],
    }


def test_import_misc_domains_preserve_exact_state(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, **_sample_misc())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    binding = import_sqlite_authority(
        source, session, checkpoint, authority, batch_rows=8,
    )
    assert binding.target_authority_uuid == authority
    # Chat artifact: exact metadata and content bytes survived.
    artifact = session.chat_artifacts['art-1']
    assert artifact['content_sha256'] == hashlib.sha256(
        artifact['content'].encode()).hexdigest()
    assert artifact['created_at'] == 424242
    assert artifact['version'] == 1
    # Tool result blob: exact content and lifecycle timestamps.
    tool = next(iter(session.tool_results.values()))
    assert tool['created_at_ms'] == 1000
    assert tool['expires_at_ms'] == 2000
    assert tool['last_accessed_at_ms'] == 1500
    assert len(tool['content']) == 4096
    # Browser observation: exact counters plus the owner touch row.
    observation = next(iter(session.browser_observations.values()))
    assert observation['observation']['visit_count'] == 3
    assert observation['version'] == 1
    assert session.browser_owner_touched_at_ms == 424242
    # Model routing: exact revision/backup/receipt and secret ciphertext.
    routing = session.routing_authorities['team-a']
    assert routing['revision'] == 5
    assert routing['backup']['revision'] == 4
    assert routing['migration_receipt'] == {'status': 'complete'}
    assert routing['updated_at'] == 1_700_000_000.5
    secret = session.routing_secrets[('team-a', 'ref-1')]
    assert secret['ciphertext'] == 'dGVzdC1jaXBoZXJ0ZXh0LXBhZGRpbmc'
    assert secret['created_at'] == 1_700_000_000.25
    # Plugin store: manifest, row and unique claim all landed.
    assert session.plugin_manifests['alpha.beta']['manifest_version'] == 1
    assert session.plugin_rows[('alpha.beta', 'items', 'item-1')]['version'] == 3
    assert session.plugin_unique[('alpha.beta', 'items', 'by_id', 'item-1')]
    # Project brain: exact projection and sequences.
    brain = session.project_brain_projects['proj-1']
    assert brain['head_sequence'] == 42
    assert brain['checkpoint_sequence'] == 7
    assert json.loads(brain['projection_json'])['headSequence'] == 42
    # Optimizer: proposal + action with referential order intact.
    assert session.optimizer_proposals['p-1']['status'] == 'pending_review'
    assert session.optimizer_actions['a-1']['proposal_id'] == 'p-1'
    # Log aggregates: exact counters.
    assert session.log_aggregates['fp-1']['count'] == 5
    assert session.log_aggregates['fp-1']['last_seen'] == 2000
    # Billing payments: exact settlement fields.
    payment = session.billing_payments['pay-1']
    assert payment['status'] == 'settled'
    assert payment['settled_at'] == 200
    assert payment['raw'] == {'k': 'v'}
    # Redeem codes: one batch of three with one redemption preserved.
    assert set(session.redeem_batches) == {'code-a'}
    batch = session.redeem_batches['code-a']
    assert batch['codes'] == ['code-a', 'code-b', 'code-c']
    assert batch['amount_micro'] == 75
    assert session.redeem_states['code-b'] == {
        'code': 'code-b', 'redeemed_by': 'alice', 'redeemed_at': 150,
    }
    # Replay against the same session is fully insert-or-match idempotent.
    binding_again = import_sqlite_authority(
        source, session, checkpoint, authority,
    )
    assert binding_again.source_sha256 == binding.source_sha256


def test_import_legacy_project_brain_cutover_coexists_with_settled(tmp_path):
    # A pre-cutover authority holds retired watch/board tables plus an already
    # settled projection. The fold must restore the watch-derived projection
    # and the projection domain must restore the settled one around it, with
    # the coverage gate green and rollback certifying both domains complete.
    source = tmp_path / 'source.db'
    _seed_source(
        source,
        project_brain_projects=[{
            'owner_user_id': _OWNER_ID, 'project_key': 'settled-proj',
            'head_sequence': 42, 'checkpoint_sequence': 7,
            'projection': {'version': 1, 'projectKey': 'settled-proj',
                           'headSequence': 42, 'checkpointSequence': 7,
                           'narratives': []},
            'updated_at_ms': 424242,
        }],
        legacy_project_brain={
            'watch_items': [{
                'item_id': 'w1', 'project_path': '/legacy-proj/',
                'kind': 'concern', 'text': 'watch me', 'status': 'open',
                'created_by_conv': 'conv-1', 'created_at': 10,
                'updated_at': 20, 'user_id': _OWNER_ID,
            }],
            'watch_responses': [{
                'item_id': 'w1', 'sequence': 3, 'project_path': '/legacy-proj',
                'response': 'latest answer', 'trigger': 'auto', 'ts': 99,
            }],
            'history_tables': ['project_events', 'project_tasks',
                               'storage_board_tasks'],
        },
    )
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    binding = import_sqlite_authority(
        source, session, checkpoint, authority, batch_rows=4)
    assert binding.plan_sha256
    # The fold restored the watch-derived projection under the session owner.
    folded = session.project_brain_projects['/legacy-proj']
    assert folded['head_sequence'] == 1
    assert folded['checkpoint_sequence'] == 0
    folded_projection = json.loads(folded['projection_json'])
    assert folded_projection['projectKey'] == '/legacy-proj'
    assert [item['id'] for item in folded_projection['watch']] == ['w1']
    assert folded_projection['watch'][0]['latestResult'] == {
        'text': 'latest answer', 'trigger': 'auto', 'timestamp': 99}
    # The settled projection restored independently and exactly.
    settled = session.project_brain_projects['settled-proj']
    assert settled['head_sequence'] == 42
    assert settled['checkpoint_sequence'] == 7
    assert json.loads(settled['projection_json'])['headSequence'] == 42
    # Both domains certify complete and rollback stays safe.
    verdict = certify_rollback(
        source, checkpoint, target_authority_uuid=authority)
    assert verdict['rollbackSafe'] is True
    assert 'project_brain_legacy_cutover' in verdict['completeDomains']
    assert 'project_brain_projects' in verdict['completeDomains']
    assert verdict['incompleteDomains'] == []


def test_import_misc_resume_after_crash(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, **_sample_misc())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    import_sqlite_authority(source, session, checkpoint, authority, batch_rows=4)
    # A second session against the same target re-verifies every prefix.
    binding = import_sqlite_authority(source, session, checkpoint, authority)
    assert binding.plan_sha256
    verdict = certify_rollback(
        source, checkpoint, target_authority_uuid=authority,
    )
    assert verdict['rollbackSafe'] is True
    for domain in (
        'chat_artifacts', 'tool_result_artifacts', 'browser_observations',
        'model_routing', 'plugin_store', 'project_brain_projects',
        'optimizer', 'log_aggregates', 'billing_payments',
        'billing_redeem_codes',
    ):
        assert domain in verdict['completeDomains'], domain


def test_import_misc_rejects_divergent_target(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, **_sample_misc())
    session = FakeSession()
    import_sqlite_authority(
        source, session, tmp_path / 'progress.json', str(uuid4()),
    )
    diverged = dict(session.chat_artifacts['art-1'], title='changed')
    session.chat_artifacts['art-1'] = diverged
    with pytest.raises(StorageV2Error, match='chat artifact snapshot differs'):
        import_sqlite_authority(
            source, session, tmp_path / 'fresh.json', str(uuid4()),
        )


def test_import_misc_rejects_foreign_owner_rows(tmp_path):
    source = tmp_path / 'source.db'
    misc = _sample_misc()
    misc['tool_result_artifacts'][0]['user_id'] = _OWNER_ID + 100
    _seed_source(source, **misc)
    session = FakeSession()
    with pytest.raises(MigrationImportError, match='outside the operator scope'):
        import_sqlite_authority(
            source, session, tmp_path / 'progress.json', str(uuid4()),
        )
    assert not session.tool_results


def test_import_misc_unique_claim_requires_its_row(tmp_path):
    source = tmp_path / 'source.db'
    misc = _sample_misc()
    misc['plugin_rows'] = []
    misc['plugin_unique_values'] = [{
        'namespace': 'alpha.beta', 'table_name': 'items',
        'index_name': 'by_id', 'index_value': 'missing', 'row_key': 'missing',
    }]
    _seed_source(source, **misc)
    session = FakeSession()
    with pytest.raises(StorageV2Error, match='missing row'):
        import_sqlite_authority(
            source, session, tmp_path / 'progress.json', str(uuid4()),
        )
    assert not session.plugin_unique


def test_import_misc_redeem_ten_thousand_code_batch(tmp_path):
    source = tmp_path / 'source.db'
    codes = [
        {'code': f'c{index:05}', 'amount_micro': 75, 'batch': 'maximum',
         'created_by': 'test', 'created_at': 100}
        for index in range(10_000)
    ]
    _seed_source(source, billing_redeem_codes=codes)
    session = FakeSession()
    import_sqlite_authority(
        source, session, tmp_path / 'progress.json', str(uuid4()),
        batch_rows=1000,
    )
    assert len(session.redeem_batches) == 1
    batch = session.redeem_batches['c00000']
    assert len(batch['codes']) == 10_000
    count = session._redeem_scan({'table': 'redeem_count', 'cursor': None}).json()
    assert count['rows'] == [{'count': 10_000}]


def test_import_misc_optimizer_action_requires_proposal(tmp_path):
    source = tmp_path / 'source.db'
    misc = _sample_misc()
    misc['optimizer_proposals'] = []
    _seed_source(source, **misc)
    session = FakeSession()
    with pytest.raises(StorageV2Error, match='proposal does not exist'):
        import_sqlite_authority(
            source, session, tmp_path / 'progress.json', str(uuid4()),
        )
    assert not session.optimizer_actions


# ---------------------------------------------------------------------------
# Final state-machine batch: queue items, autopilot markers, timers,
# scheduled tasks, and worker jobs.


def _sample_queue_items():
    return [
        {'id': 'queue-1', 'user_id': _OWNER_ID, 'conv_id': 'conv-1',
         'payload': {'text': 'first'}, 'config': {'model': 'm'},
         'position': 1, 'kind': 'real', 'priority': 100,
         'created_at_ms': 1_700_100_000_000},
        {'id': 'queue-2', 'user_id': _OWNER_ID, 'conv_id': 'conv-1',
         'payload': {'text': 'second'}, 'config': {},
         'position': 2, 'kind': 'real', 'priority': 50,
         'created_at_ms': 1_700_100_001_000,
         'leased_until_ms': 1_700_100_061_000, 'lease_task_id': 'task-9'},
    ]


def _sample_autopilot_markers():
    return [
        {'conv_id': 'conv-9', 'user_id': _OWNER_ID, 'queue_id': 'queue-9',
         'config': {'interval': 60}, 'created_at_ms': 1_700_100_000_000},
    ]


def _sample_timers():
    return [
        {'id': 'timer-1', 'user_id': _OWNER_ID, 'conv_id': 'conv-1',
         'check_instruction': 'check the build',
         'continuation_message': 'continue', 'poll_interval': 60,
         'max_polls': 120, 'poll_count': 2, 'status': 'active',
         'created_at': '2026-09-01T00:00:00',
         'updated_at': '2026-09-02T00:00:00',
         'last_poll_at': '2026-09-02T00:00:00',
         'last_poll_decision': 'wait'},
    ]


def _sample_timer_poll_log():
    return [
        {'id': 41, 'timer_id': 'timer-1', 'poll_time': '2026-09-02T00:00:00',
         'decision': 'wait', 'tokens_used': 12, 'poll_id': 'logical-1'},
        {'id': 42, 'timer_id': 'timer-1', 'poll_time': '2026-09-02T01:00:00',
         'decision': 'wait'},
    ]


def _sample_scheduled_tasks():
    return [
        {'id': 'sched-1', 'user_id': _OWNER_ID, 'system_key': 'sys.key',
         'name': 'nightly', 'schedule': '0 0 * * *', 'command': 'echo ok',
         'enabled': 1, 'run_count': 3, 'fail_count': 1,
         'created_at': '2026-09-01T00:00:00',
         'updated_at': '2026-09-02T00:00:00'},
    ]


def _sample_proactive_poll_log():
    return [
        {'id': 17, 'task_id': 'sched-1', 'poll_time': '2026-09-02T00:00:00',
         'decision': 'skip', 'reason': 'not due'},
    ]


def _sample_worker_jobs():
    return [
        {'task_id': 'job-1', 'user_id': _OWNER_ID, 'tenant_id': 'tenant-a',
         'task_kind': 'background-command', 'payload': {'command': 'echo ok'},
         'idempotency_key': 'idem-job-1', 'request_digest': 'a' * 64,
         'status': 'queued', 'priority': 100, 'available_at_ms': 1_000,
         'created_at_ms': 900, 'updated_at_ms': 1_000},
        {'task_id': 'job-2', 'user_id': _OWNER_ID, 'tenant_id': 'tenant-a',
         'task_kind': 'background-command', 'payload': {'command': 'echo run'},
         'idempotency_key': 'idem-job-2', 'request_digest': 'b' * 64,
         'status': 'running', 'priority': 50, 'available_at_ms': 1_000,
         'claim_owner': 'worker-1', 'lease_deadline_ms': 61_000,
         'fencing_token': 3, 'attempt_no': 2,
         'created_at_ms': 900, 'updated_at_ms': 1_000},
        {'task_id': 'job-3', 'user_id': _OWNER_ID, 'tenant_id': 'tenant-a',
         'task_kind': 'background-command', 'payload': {'command': 'echo done'},
         'idempotency_key': 'idem-job-3', 'request_digest': 'c' * 64,
         'status': 'succeeded', 'priority': 100, 'available_at_ms': 1_000,
         'result_ref': 'artifact://result',
         'created_at_ms': 900, 'updated_at_ms': 2_000, 'terminal_at_ms': 2_000},
    ]


def test_import_replays_queue_items_and_markers_preserving_exact_state(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, queue_items=_sample_queue_items(),
                 autopilot_markers=_sample_autopilot_markers())
    session = FakeSession()
    authority = str(uuid4())
    binding = import_sqlite_authority(source, session, tmp_path / 'progress.json', authority)
    assert binding.plan_sha256
    assert set(session.queue_items) == {'queue-1', 'queue-2'}
    leased = session.queue_items['queue-2']
    assert leased['leased_until_ms'] == 1_700_100_061_000
    assert leased['lease_task_id'] == 'task-9'
    assert leased['position'] == 2
    assert leased['priority'] == 50
    assert session.queue_items['queue-1']['leased_until_ms'] is None
    assert session.autopilot_markers['conv-9'] == {
        'conv_id': 'conv-9', 'queue_id': 'queue-9',
        'config': {'interval': 60}, 'created_at_ms': 1_700_100_000_000,
    }
    # A second run is fully insert-or-match idempotent.
    binding_again = import_sqlite_authority(
        source, session, tmp_path / 'progress.json', authority)
    assert binding_again == binding
    assert session.queue_items['queue-2']['created_at_ms'] == 1_700_100_001_000


def test_import_queue_items_rejects_divergent_target(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, queue_items=_sample_queue_items())
    session = FakeSession()
    authority = str(uuid4())
    import_sqlite_authority(source, session, tmp_path / 'progress.json', authority)
    divergent = dict(session.queue_items['queue-1'])
    divergent['position'] = 9
    session.queue_items['queue-1'] = divergent
    with pytest.raises(StorageV2Error, match='queue snapshot differs'):
        import_sqlite_authority(
            source, session, tmp_path / 'again.json', str(uuid4()))


def test_import_queue_item_rejects_foreign_owner(tmp_path):
    source = tmp_path / 'source.db'
    rows = _sample_queue_items()
    rows[0]['user_id'] = 12
    _seed_source(source, queue_items=rows)
    session = FakeSession()
    with pytest.raises(MigrationImportError, match='outside the operator scope'):
        import_sqlite_authority(
            source, session, tmp_path / 'progress.json', str(uuid4()))
    assert not session.queue_items


def test_import_replays_timers_and_poll_log_preserving_exact_state(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, timers=_sample_timers(),
                 timer_poll_log=_sample_timer_poll_log())
    session = FakeSession()
    authority = str(uuid4())
    import_sqlite_authority(source, session, tmp_path / 'progress.json', authority)
    timer = session.timers['timer-1']['document']
    assert timer['poll_count'] == 2
    assert timer['status'] == 'active'
    assert timer['created_at'] == '2026-09-01T00:00:00'
    assert timer['last_poll_decision'] == 'wait'
    first = session.timer_polls[('timer-1', 41)]
    assert first['id'] == 41
    assert first['timer_id'] == 'timer-1'
    assert first['decision'] == 'wait'
    assert first['tokens_used'] == 12
    assert first['poll_id'] == 'logical-1'
    assert set(session.timer_polls) == {('timer-1', 41), ('timer-1', 42)}
    # A second run is fully insert-or-match idempotent.
    import_sqlite_authority(source, session, tmp_path / 'progress.json', authority)
    assert session.timer_polls[('timer-1', 42)]['poll_time'] == '2026-09-02T01:00:00'


def test_import_timer_poll_requires_restored_timer(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, timers=_sample_timers(), timer_poll_log=[
        {'id': 1, 'timer_id': 'missing', 'poll_time': '2026-09-02T00:00:00'},
    ])
    session = FakeSession()
    with pytest.raises(MigrationImportError, match='outside the operator scope'):
        import_sqlite_authority(
            source, session, tmp_path / 'progress.json', str(uuid4()))
    assert not session.timer_polls


def test_import_timers_rejects_divergent_target(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, timers=_sample_timers())
    session = FakeSession()
    import_sqlite_authority(source, session, tmp_path / 'progress.json', str(uuid4()))
    session.timers['timer-1']['document']['poll_count'] = 99
    with pytest.raises(StorageV2Error, match='timer snapshot differs'):
        import_sqlite_authority(
            source, session, tmp_path / 'again.json', str(uuid4()))


def test_import_replays_scheduled_tasks_and_poll_log_preserving_exact_state(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, scheduled_tasks=_sample_scheduled_tasks(),
                 proactive_poll_log=_sample_proactive_poll_log())
    session = FakeSession()
    authority = str(uuid4())
    import_sqlite_authority(source, session, tmp_path / 'progress.json', authority)
    task = session.scheduler_tasks['sched-1']['document']
    assert task['system_key'] == 'sys.key'
    assert task['enabled'] == 1
    assert task['run_count'] == 3
    assert task['fail_count'] == 1
    poll = session.scheduler_polls[('sched-1', 17)]
    assert poll['id'] == 17
    assert poll['task_id'] == 'sched-1'
    assert poll['decision'] == 'skip'
    assert poll['reason'] == 'not due'
    # A second run is fully insert-or-match idempotent.
    import_sqlite_authority(source, session, tmp_path / 'progress.json', authority)
    assert session.scheduler_tasks['sched-1']['document']['created_at'] == '2026-09-01T00:00:00'


def test_import_scheduler_poll_requires_restored_task(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, scheduled_tasks=_sample_scheduled_tasks(),
                 proactive_poll_log=[
                     {'id': 1, 'task_id': 'missing',
                      'poll_time': '2026-09-02T00:00:00'},
                 ])
    session = FakeSession()
    with pytest.raises(MigrationImportError, match='outside the operator scope'):
        import_sqlite_authority(
            source, session, tmp_path / 'progress.json', str(uuid4()))
    assert not session.scheduler_polls


def test_import_replays_worker_jobs_preserving_exact_state(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, worker_jobs=_sample_worker_jobs())
    session = FakeSession()
    authority = str(uuid4())
    import_sqlite_authority(source, session, tmp_path / 'progress.json', authority)
    assert set(session.worker_jobs) == {'job-1', 'job-2', 'job-3'}
    running = session.worker_jobs['job-2']
    assert running['status'] == 'running'
    assert running['lease_deadline_ms'] == 61_000
    assert running['fencing_token'] == 3
    assert running['attempt_no'] == 2
    assert running['claim_owner'] == 'worker-1'
    assert running['user_id'] == _OWNER_ID
    succeeded = session.worker_jobs['job-3']
    assert succeeded['result_ref'] == 'artifact://result'
    assert succeeded['terminal_at_ms'] == 2_000
    # A second run is fully insert-or-match idempotent.
    import_sqlite_authority(source, session, tmp_path / 'progress.json', authority)
    assert session.worker_jobs['job-1']['created_at_ms'] == 900


@pytest.mark.parametrize('epoch', ['', 'f' * 32])
def test_import_worker_jobs_preserves_source_epoch_and_suspended_state(tmp_path, epoch):
    source = tmp_path / 'source.db'
    rows = _sample_worker_jobs()
    rows[0]['status'] = 'suspended'
    _seed_source(source, worker_jobs=rows)
    with sqlite3.connect(source) as connection:
        connection.execute("ALTER TABLE storage_worker_jobs ADD COLUMN lease_authority_epoch TEXT NOT NULL DEFAULT ''")
        connection.execute("UPDATE storage_worker_jobs SET lease_authority_epoch=? WHERE task_id='job-2'", (epoch,))
    session = FakeSession()
    authority = str(uuid4())
    import_sqlite_authority(source, session, tmp_path / 'progress.json', authority)
    assert session.worker_jobs['job-1']['status'] == 'suspended'
    restored = session.worker_jobs['job-2']
    if epoch:
        assert restored['lease_authority_epoch'] == epoch
    else:
        assert 'lease_authority_epoch' not in restored
    import_sqlite_authority(source, session, tmp_path / 'progress.json', authority)


def test_import_worker_jobs_rejects_divergent_target(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, worker_jobs=_sample_worker_jobs())
    session = FakeSession()
    import_sqlite_authority(source, session, tmp_path / 'progress.json', str(uuid4()))
    session.worker_jobs['job-1']['attempt_no'] = 7
    with pytest.raises(StorageV2Error, match='worker job snapshot differs'):
        import_sqlite_authority(
            source, session, tmp_path / 'again.json', str(uuid4()))


def test_import_worker_job_rejects_foreign_owner(tmp_path):
    source = tmp_path / 'source.db'
    rows = _sample_worker_jobs()
    rows[0]['user_id'] = 12
    _seed_source(source, worker_jobs=rows)
    session = FakeSession()
    with pytest.raises(MigrationImportError, match='outside the operator scope'):
        import_sqlite_authority(
            source, session, tmp_path / 'progress.json', str(uuid4()))
    assert not session.worker_jobs


def test_import_final_batch_resume_rejects_changed_progress(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, queue_items=_sample_queue_items(),
                 autopilot_markers=_sample_autopilot_markers(),
                 timers=_sample_timers(), timer_poll_log=_sample_timer_poll_log(),
                 scheduled_tasks=_sample_scheduled_tasks(),
                 proactive_poll_log=_sample_proactive_poll_log(),
                 worker_jobs=_sample_worker_jobs())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    import_sqlite_authority(source, session, checkpoint, authority)
    # A checkpoint claiming divergent verified progress fails closed.
    for domain in ('queue_items', 'autopilot_markers', 'timers',
                   'scheduled_tasks', 'worker_jobs'):
        envelope = json.loads(checkpoint.read_text())
        envelope['body']['domains'][domain]['sha256'] = '0' * 64
        tampered = tmp_path / f'tampered-{domain}.json'
        tampered.write_text(json.dumps(envelope))
        with pytest.raises(MigrationImportError):
            import_sqlite_authority(source, session, tampered, authority)


def _seed_compaction_archive(source, *, scope='', owner=_OWNER_ID):
    from lib.storage_sidecar.archived_message_codec import encode_archived_message_sequence_with_metrics
    messages = [{'role': 'user', 'content': '保留历史正文' * 8192}]
    post = [{'role': 'assistant', 'content': '恢复摘要'}]
    encoded = encode_archived_message_sequence_with_metrics(messages)
    row = {
        'archive_id': 'archive-1', 'archive_scope': scope,
        'conversation_id': 'conv-alpha', 'user_id': owner,
        'messages_json': encoded.stored_document.decode(),
        'post_messages_json': json.dumps(post, ensure_ascii=False),
        'summary': 'historical summary', 'receipt_json': '{"status":"complete","extra":17}',
        'trigger': 'force', 'task_id': 'old-task', 'round_num': 4, 'model': 'historical-model',
        'tokens_before': 12000, 'tokens_after': 50, 'msgs_before': 8, 'msgs_after': 2,
        'reason': 'context budget', 'payload_size': len(encoded.stored_document),
        'created_at_ms': 1700000000123,
    }
    assert len(encoded.stored_document) < len(messages[0]['content'].encode())
    columns = list(row)
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE storage_compaction_archives (' + ','.join(
            name + (' INTEGER' if isinstance(row[name], int) else ' TEXT')
            + (' PRIMARY KEY' if name == 'archive_id' else '') for name in columns) + ')')
        db.execute('INSERT INTO storage_compaction_archives VALUES ('
                   + ','.join('?' for _ in columns) + ')', list(row.values()))
    return row, messages, post


def test_native_compaction_archive_import_preserves_compressed_history_and_resume(tmp_path):
    binary = _resolve_tofudb_binary()
    source = tmp_path / 'source.db'
    _seed_source(source, conversations=_sample_conversations())
    row, messages, post = _seed_compaction_archive(source)
    with sqlite3.connect(source) as db:
        db.execute('DELETE FROM storage_conversations WHERE id=?', ('conv-alpha',))
    expected = importer._compaction_archive_document(row)
    target = tmp_path / 'target'
    authority = _initialize(binary, target)
    checkpoint = tmp_path / 'progress.json'

    def crash(domain, state):
        if domain == 'compaction_archives' and not state['complete']:
            raise _Crash('after archive commit and verified prefix')

    with OfflineMigrationSession(binary, target, authority, owner_id=_OWNER_ID,
                                 tenant_id=_TENANT_ID) as session:
        with pytest.raises(_Crash):
            import_sqlite_authority(source, session, checkpoint, authority,
                                    batch_rows=1, after_verified_prefix=crash)
    with OfflineMigrationSession(binary, target, authority, owner_id=_OWNER_ID,
                                 tenant_id=_TENANT_ID) as session:
        import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)
        observed = importer._query(session, 'migration.compaction.archive.get',
                                   {'archive_id': 'archive-1'})
        assert observed == expected
        assert observed['messages'] == messages
        assert observed['post_messages'] == post
        assert observed['payload_size'] == len(row['messages_json'].encode())
        changed = {**expected, 'summary': 'divergent'}
        with pytest.raises(StorageV2Error):
            importer._command(session, 'migration.compaction.archive.restore', changed)
        assert importer._query(session, 'migration.compaction.archive.get',
                               {'archive_id': 'archive-1'}) == expected
        extra = {**expected, 'archive_id': 'target-only'}
        importer._command(session, 'migration.compaction.archive.restore', extra)
        with pytest.raises(MigrationImportError, match='source does not'):
            import_sqlite_authority(source, session, checkpoint, authority)
        # A fresh checkpoint must enumerate the target independently, rather
        # than blessing the source count after only successful point reads.
        with pytest.raises(MigrationImportError, match='source does not'):
            import_sqlite_authority(source, session, tmp_path / 'extra-progress.json', authority)


def test_compaction_archive_converter_refuses_unknown_scope_and_decode_growth(tmp_path):
    from lib.storage_sidecar.archived_message_codec import encode_archived_message_sequence_with_metrics
    source = tmp_path / 'source.db'
    _seed_source(source)
    row, _, _ = _seed_compaction_archive(source, scope='child-invocation')
    with pytest.raises(MigrationImportError, match='invocation adapter'):
        importer._compaction_archive_document(row)
    row['archive_scope'] = ''
    # Decoded growth beyond the frame-coherent 12 MiB migration budget is
    # refused (the bound moved from the generic 8 MiB row cap with the
    # dedicated compaction-archive contract budget).
    row['messages_json'] = encode_archived_message_sequence_with_metrics(
        [{'role': 'user', 'content': 'x' * (5 * 1024 * 1024)}] * 3).stored_document.decode()
    with pytest.raises(MigrationImportError, match='decoded archive transcript'):
        importer._compaction_archive_document(row)


def _seed_attempt_snapshots(source):
    rows = []
    for identity in ('attempt-a', 'attempt-b'):
        rows.append({
            'attempt_id': identity, 'conversation_id': 'conv-alpha', 'turn_id': 'turn-alpha-2',
            'command_id': 'command-' + identity, 'task_id': 'task-' + identity,
            'operation': 'generate', 'dispatch_mode': 'conversation_executor',
            'queue_id': 'old-queue', 'queue_state': 'dispatched', 'status': 'completed',
            'base_projection_revision': 2, 'resume_anchor_json': '{"frontier":1}',
            'config_json': '{"model":"historical"}',
            'error_json': ('"historical error string"' if identity == 'attempt-b' else '{"past":"kept"}'),
            'timing_trace_json': '{"duration":1.25}', 'created_at': 2100,
            'started_at': 2101, 'settled_at': 2300, 'superseded_at': None,
        })
    table = importer._plan_table('attempts')
    columns = table['columns']
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE storage_generation_attempts (' + ','.join(
            name + (' INTEGER' if name in {'base_projection_revision', 'created_at',
                                          'started_at', 'settled_at', 'superseded_at'} else ' TEXT')
            + (' PRIMARY KEY' if name == 'attempt_id' else '') for name in columns) + ')')
        db.executemany('INSERT INTO storage_generation_attempts VALUES ('
                       + ','.join('?' for _ in columns) + ')',
                       [[row[name] for name in columns] for row in rows])
    return rows


@pytest.mark.parametrize('detached', [False, True])
def test_native_attempt_import_preserves_history_and_terminal_turn_pointer(tmp_path, detached):
    binary = _resolve_tofudb_binary()
    source = tmp_path / 'source.db'
    turns = _sample_turns()
    turns[1].update(status='completed', current_attempt_id='attempt-b')
    _seed_source(source, conversations=_sample_conversations(), turns=turns)
    attempts = _seed_attempt_snapshots(source)
    if detached:
        with sqlite3.connect(source) as db:
            db.execute('DELETE FROM storage_conversations WHERE id=?', ('conv-alpha',))
            db.execute('DELETE FROM storage_conversation_turns WHERE conversation_id=?', ('conv-alpha',))
        _seed_trash(source, conversation_id='conv-alpha')
    target = tmp_path / 'target'
    authority = _initialize(binary, target)
    checkpoint = tmp_path / 'progress.json'

    def crash(domain, state):
        if domain == 'attempts' and state['rows'] == 1:
            raise _Crash('after first terminal attempt commit')

    with OfflineMigrationSession(binary, target, authority, owner_id=_OWNER_ID,
                                 tenant_id=_TENANT_ID) as session:
        with pytest.raises(_Crash):
            import_sqlite_authority(source, session, checkpoint, authority,
                                    batch_rows=1, after_verified_prefix=crash)
    with OfflineMigrationSession(binary, target, authority, owner_id=_OWNER_ID,
                                 tenant_id=_TENANT_ID) as session:
        import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)
        for row in attempts:
            observed = importer._query(session, 'migration.attempt.get', {
                'conversation_id': row['conversation_id'], 'attempt_id': row['attempt_id'],
            })
            assert observed == importer._attempt_document(row)
        if not detached:
            turn = importer._query(session, 'migration.turn.get', {
                'conversation_id': 'conv-alpha', 'id': 'turn-alpha-2',
            })
            assert turn['current_attempt_id'] == 'attempt-b'
            assert turn['status'] == 'completed'
        extra = importer._attempt_document(attempts[0])
        extra.update(attempt_id='target-only', command_id='target-only-command')
        importer._command(session, 'migration.attempt.restore', extra)
        with pytest.raises(MigrationImportError, match='source does not'):
            import_sqlite_authority(source, session, checkpoint, authority)


def test_attempt_import_requires_proven_source_owner_and_terminal_state(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, conversations=_sample_conversations())
    row = _seed_attempt_snapshots(source)[0]
    with importer.SQLiteMigrationSource(source) as snapshot:
        importer._require_attempt_source_owner(snapshot, FakeSession(), row)
        with pytest.raises(MigrationImportError, match='operator scope'):
            importer._require_attempt_source_owner(snapshot, FakeSession(), {**row, 'conversation_id': 'unknown'})
    with pytest.raises(MigrationImportError, match='live attempts'):
        importer._attempt_document({**row, 'status': 'running'})


def _seed_attempt_events(source, count=129):
    rows = []
    for index in range(count):
        sequence = 5 + index * 3
        event = {'attemptId': 'attempt-b', 'conversationId': 'conv-alpha',
                 'turnId': 'turn-alpha-2', 'seq': sequence, 'projectionRevision': 2,
                 'type': 'projection_updated', 'payload': {'historical': '值', 'index': index}}
        rows.append({'attempt_id': 'attempt-b', 'sequence': sequence,
                     'conversation_id': 'conv-alpha', 'turn_id': 'turn-alpha-2',
                     'projection_revision': 2, 'type': 'projection_updated',
                     'payload_json': (json.dumps(event, ensure_ascii=False, indent=2).encode('utf-8')
                                      if index % 2 else json.dumps(event, ensure_ascii=False, indent=2)),
                     'payload_bytes': 0, 'created_at': 2200 + index})
    table = importer._plan_table('attempt_events')
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE storage_attempt_events (' + ','.join(
            name + (' INTEGER' if name in {'sequence', 'projection_revision', 'payload_bytes', 'created_at'} else ' TEXT')
            for name in table['columns']) + ', PRIMARY KEY(attempt_id,sequence))')
        db.executemany('INSERT INTO storage_attempt_events VALUES ('
                       + ','.join('?' for _ in table['columns']) + ')',
                       [[row[key] for key in table['columns']] for row in rows])
    return rows


@pytest.mark.parametrize('detached', [False, True])
def test_native_attempt_event_import_preserves_exact_bytes_gaps_and_resume(tmp_path, detached):
    binary = _resolve_tofudb_binary()
    source = tmp_path / 'source.db'
    turns = _sample_turns()
    turns[1].update(status='completed', current_attempt_id='attempt-b')
    _seed_source(source, conversations=_sample_conversations(), turns=turns)
    _seed_attempt_snapshots(source)
    events = _seed_attempt_events(source)
    if detached:
        with sqlite3.connect(source) as db:
            db.execute('DELETE FROM storage_conversations WHERE id=?', ('conv-alpha',))
            db.execute('DELETE FROM storage_conversation_turns WHERE conversation_id=?', ('conv-alpha',))
        _seed_trash(source, conversation_id='conv-alpha')
    target = tmp_path / 'target'
    authority = _initialize(binary, target)
    checkpoint = tmp_path / 'progress.json'
    def crash(domain, state):
        if domain == 'attempt_events' and state['rows'] == 32:
            raise _Crash('after durable event prefix')
    with OfflineMigrationSession(binary, target, authority, owner_id=_OWNER_ID, tenant_id=_TENANT_ID) as session:
        with pytest.raises(_Crash):
            import_sqlite_authority(source, session, checkpoint, authority, batch_rows=32, after_verified_prefix=crash)
    with OfflineMigrationSession(binary, target, authority, owner_id=_OWNER_ID, tenant_id=_TENANT_ID) as session:
        import_sqlite_authority(source, session, checkpoint, authority, batch_rows=32)
        for row in events:
            snapshot = importer._attempt_event_document(row)
            identity = {key: snapshot[key] for key in ('attempt_id', 'sequence')}
            assert importer._query(session, 'migration.attempt.event.get', identity) == snapshot
        forged = importer._attempt_event_document(events[0])
        forged['turn_id'] = 'different-turn'
        envelope = json.loads(forged['event_json']); envelope['turnId'] = 'different-turn'
        forged['event_json'] = json.dumps(envelope)
        with pytest.raises(StorageV2Error):
            importer._command(session, 'migration.attempt.event.restore', forged)
        first = importer._query(session, 'migration.attempt.event.scan', {'cursor': None})
        assert len(first['rows']) == 128
        last = importer._query(session, 'migration.attempt.event.scan', {'cursor': first['cursor']})
        assert len(last['rows']) == 1
        conflict = importer._attempt_event_document(events[0])
        conflict['created_at'] += 1
        with pytest.raises(StorageV2Error):
            importer._command(session, 'migration.attempt.event.restore', conflict)
        extra = dict(events[-1], sequence=1000)
        event = json.loads(extra['payload_json']); event['seq'] = 1000
        extra['payload_json'] = json.dumps(event)
        importer._command(session, 'migration.attempt.event.restore', importer._attempt_event_document(extra))
        with pytest.raises(MigrationImportError, match='source does not'):
            import_sqlite_authority(source, session, checkpoint, authority)


def test_attempt_event_snapshot_rejects_mismatched_envelope(tmp_path):
    source = tmp_path / 'source.db'
    row = _seed_attempt_events(source, count=1)[0]
    with pytest.raises(MigrationImportError, match='identity differs'):
        importer._attempt_event_document({**row, 'sequence': 10})


def test_native_oversized_event_streams_without_partial_publication(tmp_path):
    import hashlib
    import tracemalloc
    binary = _resolve_tofudb_binary()
    source = tmp_path / 'source.db'
    turns = _sample_turns()
    turns[1].update(status='completed', current_attempt_id='attempt-b')
    _seed_source(source, conversations=_sample_conversations(), turns=turns)
    attempts = _seed_attempt_snapshots(source)
    row = _seed_attempt_events(source, count=1)[0]
    event = json.loads(row['payload_json'])
    event['payload']['historical'] = 'x' * (25 * 1024 * 1024)
    encoded = json.dumps(event, ensure_ascii=False).encode('utf-8')
    expected_size = len(encoded)
    expected_hash = hashlib.sha256(encoded).hexdigest()
    with sqlite3.connect(source) as db:
        db.execute('UPDATE storage_attempt_events SET payload_json=?,payload_bytes=?', (encoded, len(encoded)))
    del encoded, event
    table = importer._plan_table('attempt_events')
    with importer.SQLiteMigrationSource(source) as snapshot:
        metadata = snapshot.point(table['name'], [name for name in table['columns'] if name != 'payload_json'],
                                  table['keys'], ('attempt-b', 5))
        manifest = importer._attempt_event_manifest(snapshot, metadata)
    target = tmp_path / 'target'
    authority = _initialize(binary, target)
    with OfflineMigrationSession(binary, target, authority, owner_id=_OWNER_ID, tenant_id=_TENANT_ID) as session:
        importer._command(session, 'migration.attempt.restore', importer._attempt_document(attempts[1]))
        importer._command(session, 'migration.attempt.event.upload.begin', manifest)
        with importer.SQLiteMigrationSource(source) as snapshot:
            chunks = importer._attempt_event_source_chunks(snapshot, manifest['snapshot'])
            first = next(chunks)
            chunks.close()
        importer._command(session, 'migration.attempt.event.upload.append', {'offset': 0, 'hex': first.hex()})
        assert importer._query(session, 'migration.attempt.event.get', {'attempt_id': 'attempt-b', 'sequence': 5}) is None
        # EOF discards the incomplete volatile upload; a new child replays it.
    with OfflineMigrationSession(binary, target, authority, owner_id=_OWNER_ID, tenant_id=_TENANT_ID) as session:
        tracemalloc.start()
        try:
            import_sqlite_authority(source, session, tmp_path / 'progress.json', authority, batch_rows=32)
            assert tracemalloc.get_traced_memory()[1] < 8 * 1024 * 1024
        finally:
            tracemalloc.stop()
        status_path = Path(f'/proc/{session._process.pid}/status')
        if status_path.exists():
            native_peak = int(next(line.split()[1] for line in status_path.read_text().splitlines()
                                   if line.startswith('VmHWM:'))) * 1024
            assert native_peak < 512 * 1024 * 1024
        observed = importer._query(session, 'migration.attempt.event.chunk', {
            'attempt_id': 'attempt-b', 'sequence': 5, 'offset': expected_size - 7})
        assert observed['sha256'] == expected_hash
        assert observed['event_bytes'] == expected_size
        assert len(bytes.fromhex(observed['hex'])) == 7
        assert observed['final'] is True
        with pytest.raises(StorageV2Error, match='chunk reads'):
            importer._query(session, 'migration.attempt.event.get', {'attempt_id': 'attempt-b', 'sequence': 5})
        # An oversized ordinary get is a bounded error, not a dead child.
        assert importer._query(session, 'migration.attempt.event.scan', {'cursor': None})['rows'] == [
            {'attempt_id': 'attempt-b', 'sequence': 5}]
    # The application protocol must read the imported value through response chunks.
    from tofu_db.wire import StorageV2Session
    import select
    secret = ('test-large-event-' + uuid4().hex).encode('ascii')
    env = {**os.environ, 'TOFU_STORAGE_TOKEN': secret.decode('ascii')}
    process = subprocess.Popen([
        str(binary), 'serve', '--data-dir', str(target), '--owner-id', str(_OWNER_ID),
        '--tenant-id', str(_TENANT_ID), '--search-projection-dir', str(tmp_path / 'search-projection'),
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True)
    try:
        assert select.select([process.stdout], [], [], 30)[0], 'native serve readiness timeout'
        ready = json.loads(process.stdout.readline())
        session = StorageV2Session('127.0.0.1', ready['port'], secret,
                                   owner_id=_OWNER_ID, tenant_id=_TENANT_ID, timeout=30)
        try:
            response = session.request('turn.events.list', {
                'attempt_id': 'attempt-b', 'user_id': _OWNER_ID, 'projection_mode': 'patch',
            }, command_id=None, deadline_unix_ms=int(time.time() * 1000) + 30000)
            response.raise_for_error()
            events = response.json()
            assert len(events) == 1
            assert len(events[0]['payload']['historical']) == 25 * 1024 * 1024
            assert events[0]['seq'] == 5
        finally:
            session.close()
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def _seed_raw_archive(source, *, owner=_OWNER_ID):
    import zlib
    request = b'provider request body\n' * 160_000
    response = b''
    row = {
        'archive_id': 'raw-1', 'user_id': owner, 'conversation_id': 'detached-conversation',
        'turn_id': 'historical-turn', 'attempt_id': 'historical-attempt', 'task_id': 'historical-task',
        'round_num': 2, 'transport_attempt': 3, 'request_blob': zlib.compress(request),
        'response_blob': response, 'request_bytes': len(request), 'response_bytes': 91,
        'stored_bytes': len(zlib.compress(request)), 'request_sha256': hashlib.sha256(request).hexdigest(),
        'response_sha256': 'a' * 64, 'integrity': 'partial',
        'truncation_reason': 'transport_interrupted', 'summary_json': '{"text":"preserved","nested":{"n":3}}',
        'created_at_ms': 1700000000123,
    }
    columns = list(row)
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE storage_raw_archives (' + ','.join(
            name + (' INTEGER' if isinstance(row[name], int) else ' BLOB' if isinstance(row[name], bytes) else ' TEXT')
            + (' PRIMARY KEY' if name == 'archive_id' else '') for name in columns) + ')')
        db.execute('INSERT INTO storage_raw_archives VALUES (' + ','.join('?' for _ in columns) + ')', list(row.values()))
    return row


def test_native_raw_archive_import_preserves_detached_bytes_resume_and_counts(tmp_path, monkeypatch):
    binary = _resolve_tofudb_binary()
    source = tmp_path / 'source.db'
    _seed_source(source, conversations=[])
    row = _seed_raw_archive(source)
    expected = importer._raw_archive_document(row)
    target = tmp_path / 'target'
    authority = _initialize(binary, target)
    checkpoint = tmp_path / 'progress.json'

    def crash(domain, state):
        if domain == 'raw_archives' and not state['complete']:
            raise _Crash('after raw archive verified prefix')

    with OfflineMigrationSession(binary, target, authority, owner_id=_OWNER_ID, tenant_id=_TENANT_ID) as session:
        with pytest.raises(_Crash):
            import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1, after_verified_prefix=crash)
    with OfflineMigrationSession(binary, target, authority, owner_id=_OWNER_ID, tenant_id=_TENANT_ID) as session:
        import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)
        assert importer._query(session, 'migration.raw.archive.get', {'archive_id': 'raw-1'}) == expected
        assert importer._query(session, 'migration.raw.archive.stats', {'scope': 'tenant'}) == {'stored_bytes': row['stored_bytes']}
        query = importer._query

        def wrong_counter(session, operation, payload):
            observed = query(session, operation, payload)
            if operation == 'migration.raw.archive.stats' and payload['scope'] == 'attempt':
                return {'stored_bytes': observed['stored_bytes'] + 1}
            return observed

        with monkeypatch.context() as patch:
            patch.setattr(importer, '_query', wrong_counter)
            with pytest.raises(MigrationImportError, match='grouped counters differ'):
                import_sqlite_authority(source, session, checkpoint, authority)
        extra = {**expected, 'archive_id': 'target-only'}
        importer._command(session, 'migration.raw.archive.restore', extra)
        with pytest.raises(MigrationImportError, match='source does not'):
            import_sqlite_authority(source, session, checkpoint, authority)


def test_raw_archive_source_conversion_and_aggregates_fail_closed(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, conversations=[])
    row = _seed_raw_archive(source)
    with importer.SQLiteMigrationSource(source) as snapshot:
        assert list(snapshot.raw_archive_usage(_OWNER_ID)) == [{'count': 1, 'stored_bytes': row['stored_bytes']}]
        assert list(snapshot.raw_archive_usage(_OWNER_ID, 'attempt_id')) == [
            {'attempt_id': row['attempt_id'], 'count': 1, 'stored_bytes': row['stored_bytes']}]
        assert list(snapshot.raw_archive_usage(_OWNER_ID + 1)) == [{'count': 0, 'stored_bytes': 0}]
        with pytest.raises(ValueError, match='grouping'):
            list(snapshot.raw_archive_usage(_OWNER_ID, 'unsafe'))
    for field, value in [('stored_bytes', 0), ('request_blob', 'text'), ('summary_json', '[]')]:
        with pytest.raises(MigrationImportError):
            importer._raw_archive_document({**row, field: value})


def test_native_frozen_transcript_import_preserves_pages_and_resume(tmp_path):
    from lib.storage_sidecar.tofudb_migration_transcript import frozen_snapshot
    binary = _resolve_tofudb_binary()
    source = tmp_path / 'source.db'
    conversations = _sample_conversations()
    conversations[0]['rev'] = 9183
    _seed_source(source, conversations=conversations)
    messages = [{'role': 'user', 'content': '历史问题'},
                {'role': 'assistant', 'content': 'x' * (9 * 1024 * 1024)},
                {'role': 'user', 'content': '继续', 'timestamp': 1786362450651.2947}]
    encoded = json.dumps(messages, ensure_ascii=False).encode()
    with sqlite3.connect(source) as db:
        db.execute('UPDATE storage_conversations SET messages_json=? WHERE id=?', (encoded, 'conv-alpha'))
    with importer.SQLiteMigrationSource(source) as snapshot:
        row = snapshot.point('storage_conversations', ('id', 'user_id', 'created_at_ms'),
                             ('id', 'user_id'), ('conv-alpha', _OWNER_ID))
        expected = frozen_snapshot(snapshot, row)
    assert len(expected['pages']) == 3
    target = tmp_path / 'target'
    authority = _initialize(binary, target)
    checkpoint = tmp_path / 'progress.json'

    def crash(domain, state):
        if domain == 'frozen_transcripts' and not state['complete']:
            raise _Crash('after frozen transcript verified prefix')

    with OfflineMigrationSession(binary, target, authority, owner_id=_OWNER_ID, tenant_id=_TENANT_ID) as session:
        with pytest.raises(_Crash):
            import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1, after_verified_prefix=crash)
    with OfflineMigrationSession(binary, target, authority, owner_id=_OWNER_ID, tenant_id=_TENANT_ID) as session:
        import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)
        assert importer._query(session, 'migration.frozen.transcript.get', {'conversation_id': 'conv-alpha'}) == expected
        history = importer._query(session, 'conversation.get', {'conv_id': 'conv-alpha', 'user_id': _OWNER_ID, 'derive_messages': True, 'message_window': 1})
        assert history['messages'] == messages[-1:]
        assert history['metadata']['msg_count'] == 3
        assert history['metadata']['rev'] == 9183
        altered = {**expected['source'], 'source_sha256': 'f' * 64}
        with pytest.raises(StorageV2Error):
            importer._command(session, 'migration.frozen.transcript.begin', altered)


def test_conversation_revision_is_preserved_and_bound_to_resume(tmp_path):
    source = tmp_path / 'source.db'
    rows = _sample_conversations()
    rows[0]['rev'] = 9183
    _seed_source(source, conversations=rows, turns=_sample_turns())
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'progress.json'
    import_sqlite_authority(source, session, checkpoint, authority)
    assert session.conversations[rows[0]['id']]['rev'] == 9183
    import_sqlite_authority(source, session, checkpoint, authority)
    with sqlite3.connect(source) as db:
        db.execute('UPDATE storage_conversations SET rev=rev+1 WHERE id=?', (rows[0]['id'],))
    with pytest.raises(MigrationImportError):
        import_sqlite_authority(source, session, checkpoint, authority)
    assert session.conversations[rows[0]['id']]['rev'] == 9183


def _seed_trash(source, *, conversation_id='retired', frozen=None, turns=()):
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE storage_conversation_trash ('
                   'conversation_id TEXT NOT NULL,user_id INTEGER NOT NULL,title TEXT NOT NULL,'
                   'messages_json TEXT NOT NULL,created_at_ms INTEGER NOT NULL,updated_at_ms INTEGER NOT NULL,'
                   'settings_json TEXT NOT NULL,msg_count INTEGER NOT NULL,rev INTEGER NOT NULL,'
                   'deleted_at_ms INTEGER NOT NULL,PRIMARY KEY(conversation_id,user_id))')
        db.execute('INSERT INTO storage_conversation_trash VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (conversation_id, _OWNER_ID, 'Retained history', json.dumps(frozen or []),
                    1000, 2000, '{}', len(turns) or len(frozen or []), 77, 9000))
        db.execute('CREATE TABLE storage_conversation_trash_turns ('
                   'conversation_id TEXT NOT NULL,user_id INTEGER NOT NULL,turn_id TEXT NOT NULL,'
                   'lane_id TEXT NOT NULL,parent_turn_id TEXT,ordinal INTEGER NOT NULL,actor TEXT NOT NULL,'
                   'kind TEXT NOT NULL,run_id TEXT NOT NULL,status TEXT NOT NULL,projection_json TEXT NOT NULL,'
                   'projection_revision INTEGER NOT NULL,settlement_json TEXT NOT NULL,'
                   'created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,'
                   'PRIMARY KEY(conversation_id,user_id,turn_id),UNIQUE(conversation_id,user_id,lane_id,ordinal))')
        for index, content in enumerate(turns):
            db.execute('INSERT INTO storage_conversation_trash_turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (conversation_id, _OWNER_ID, f'trash-turn-{index}', 'main', None, index+1,
                        'assistant', 'reply', '', 'completed', json.dumps({'content': content}), 7, '{}', 1500, 3000))


@pytest.mark.parametrize('frozen_only', [False, True])
def test_native_trash_import_resumes_pending_capsule_before_active_domains(tmp_path, frozen_only):
    from lib.storage_sidecar import tofudb_migration_trash as trash
    binary = _resolve_tofudb_binary()
    source = tmp_path / 'source.db'
    _seed_source(source, conversations=_sample_conversations())
    frozen = [{'role': 'assistant', 'content': '旧记录' * (1024 * 1024)}] if frozen_only else []
    _seed_trash(source, frozen=frozen, turns=[] if frozen_only else ['first retained reply', 'second retained reply'])
    with importer.SQLiteMigrationSource(source) as snapshot:
        row = snapshot.point('storage_conversation_trash', trash._header_table()['columns'],
                             ('conversation_id', 'user_id'), ('retired', _OWNER_ID))
        document, _ = trash._snapshot(snapshot, row)
    target = tmp_path / 'target'
    authority = _initialize(binary, target)
    checkpoint = tmp_path / 'progress.json'
    with OfflineMigrationSession(binary, target, authority, owner_id=_OWNER_ID, tenant_id=_TENANT_ID) as session:
        importer._command(session, 'migration.trash.begin', document)
        if not frozen_only:
            with importer.SQLiteMigrationSource(source) as snapshot:
                importer._replay_turn(session, next(trash._turns(snapshot, 'retired', _OWNER_ID)))
    # The executable must refuse application readiness until the stage is gone.
    result = subprocess.run([str(binary), 'serve', '--data-dir', str(target), '--owner-id', str(_OWNER_ID), '--tenant-id', str(_TENANT_ID)],
                            input='', capture_output=True, text=True, timeout=30,
                            env={**os.environ, 'TOFU_STORAGE_TOKEN': 'trash-readiness-test-private-token'})
    assert result.returncode != 0 and 'offline trash migration is incomplete' in result.stderr

    def crash(domain, state):
        if domain == 'trash' and not state['complete']:
            raise _Crash('after pinned trash verification')
    with OfflineMigrationSession(binary, target, authority, owner_id=_OWNER_ID, tenant_id=_TENANT_ID) as session:
        with pytest.raises(_Crash):
            import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1, after_verified_prefix=crash)
    with OfflineMigrationSession(binary, target, authority, owner_id=_OWNER_ID, tenant_id=_TENANT_ID) as session:
        import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)
        view = importer._query(session, 'migration.trash.get', {'conversation_id': 'retired'})
        assert view['complete'] is True and view['header']['rev'] == 77
        assert view['header']['updated_at'] == 2000 and view['deleted_at_ms'] == 9000
        assert view['frozen'] == document['frozen']
        status_path = Path(f'/proc/{session._process.pid}/status')
        if status_path.exists():
            peak_kib = int(next(line.split()[1] for line in status_path.read_text().splitlines() if line.startswith('VmHWM:')))
            assert peak_kib < 256 * 1024, 'bounded trash fixture exceeded the native process memory budget'
        assert importer._query(session, 'conversation.get', {'user_id': _OWNER_ID, 'conv_id': 'retired', 'derive_messages': False}) is None
    if not frozen_only:
        # A fresh checkpoint cannot hide divergent pinned content behind an old receipt.
        with sqlite3.connect(source) as db:
            db.execute('UPDATE storage_conversation_trash_turns SET projection_json=? WHERE turn_id=?',
                       (json.dumps({'content': 'changed source'}), 'trash-turn-0'))
        with OfflineMigrationSession(binary, target, authority, owner_id=_OWNER_ID, tenant_id=_TENANT_ID) as session:
            with pytest.raises(MigrationImportError, match='pinned target Turns'):
                import_sqlite_authority(source, session, tmp_path / 'fresh-progress.json', authority)


def test_trash_completion_rejects_a_stage_absent_from_source(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source, conversations=[])
    session = FakeSession()
    session.pending_trash = 'unmapped-stage'
    with pytest.raises(MigrationImportError, match='pending trash'):
        import_sqlite_authority(source, session, tmp_path / 'progress.json', str(uuid4()))


def test_trash_projection_codec_failure_is_a_migration_error(tmp_path):
    from lib.storage_sidecar import tofudb_migration_trash as trash
    source = tmp_path / 'source.db'
    _seed_source(source, conversations=[])
    _seed_trash(source, turns=['retained'])
    with sqlite3.connect(source) as db:
        db.execute('UPDATE storage_conversation_trash_turns SET projection_json=?',
                   (json.dumps({'_tofuStorageProjectionCodec': {'version': 999}}),))
    with importer.SQLiteMigrationSource(source) as snapshot:
        with pytest.raises(MigrationImportError, match='projection codec'):
            list(trash._turns(snapshot, 'retired', _OWNER_ID))


def test_native_tombstones_preserve_deletion_times_and_resume_catalog_pages(tmp_path):
    binary = _resolve_tofudb_binary()
    source = tmp_path / 'source.db'
    _seed_source(source)
    with sqlite3.connect(source) as connection:
        connection.execute('CREATE TABLE storage_turn_tombstones ('
                           'conversation_id TEXT, turn_id TEXT, user_id INTEGER, deleted_at INTEGER, '
                           'PRIMARY KEY(conversation_id,turn_id))')
        connection.executemany('INSERT INTO storage_turn_tombstones VALUES (?,?,?,?)',
                               [('retired-parent', f'gone-{i:03d}', 11, 123+i) for i in range(130)])
    target = tmp_path / 'target'
    authority = _initialize(binary, target)
    checkpoint = tmp_path / 'checkpoint'

    def crash(domain, entry):
        if domain == 'turn_tombstones' and not entry['complete']:
            raise _Crash(domain)

    with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7) as session:
        with pytest.raises(_Crash):
            import_sqlite_authority(source, session, checkpoint, authority,
                                    batch_rows=1, after_verified_prefix=crash)
    expected = {'conversation_id': 'retired-parent', 'turn_id': 'gone-129',
                'user_id': 11, 'deleted_at': 252}
    with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7) as session:
        import_sqlite_authority(source, session, checkpoint, authority)
        assert importer._query(session, 'migration.turn.tombstone.get', expected) == expected
        page = importer._query(session, 'migration.turn.tombstone.scan', {'cursor': None})
        assert len(page['rows']) == 128
        tail = importer._query(session, 'migration.turn.tombstone.scan', {'cursor': page['cursor']})
        assert len(tail['rows']) == 2
        assert tail['cursor'] is None
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE storage_turn_tombstones SET deleted_at=999 WHERE turn_id='gone-129'")
    with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7) as session:
        with pytest.raises((MigrationImportError, StorageV2Error)):
            import_sqlite_authority(source, session, tmp_path / 'new-checkpoint', authority)


@pytest.mark.parametrize('native', [False, True])
def test_turn_import_hydrates_private_storage_references(tmp_path, native):
    from lib.storage_sidecar.projection_codec import encode_projection_for_storage, STORAGE_PROJECTION_CODEC_KEY

    public = {'content': 'done', 'segments': [{
        'type': 'tool_use', 'id': 'call', 'input': '{"path":"a.py"}',
        'result': {'content': '工具结果' * 1000, 'isError': False},
    }], 'toolRounds': [{
        'toolCallId': 'call', 'toolArgs': '{"path":"a.py"}', 'toolContent': '工具结果' * 1000,
    }]}
    encoded = encode_projection_for_storage(public)
    assert STORAGE_PROJECTION_CODEC_KEY in encoded
    source = tmp_path / 'source.db'
    turns = _sample_turns()
    turns[1]['projection'] = encoded
    _seed_source(source, conversations=_sample_conversations(), turns=turns)
    checkpoint = tmp_path / 'checkpoint'
    if native:
        binary = _resolve_tofudb_binary()
        target = tmp_path / 'target'
        authority = _initialize(binary, target)
        with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7) as session:
            import_sqlite_authority(source, session, checkpoint, authority)
        with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7) as session:
            import_sqlite_authority(source, session, checkpoint, authority)
            observed = importer._query(session, 'migration.turn.get', {
                'id': 'turn-alpha-2', 'conversation_id': 'conv-alpha'})
    else:
        session = FakeSession()
        import_sqlite_authority(source, session, checkpoint, str(uuid4()))
        observed = session.turns['turn-alpha-2']
    assert observed['projection'] == public
    assert STORAGE_PROJECTION_CODEC_KEY not in observed['projection']


@pytest.mark.parametrize('state', ['checkpoint', 'head', 'orphan_checkpoint', 'invalid_head', 'materialized'])
def test_import_checks_projection_materialization_before_target_writes(tmp_path, state):
    source = tmp_path / 'source.db'
    _seed_source(source, conversations=_sample_conversations(), turns=_sample_turns())
    with sqlite3.connect(source) as connection:
        connection.executescript('''
            ALTER TABLE storage_conversation_turns ADD COLUMN projection_checkpoint_revision INTEGER;
            ALTER TABLE storage_conversation_turns ADD COLUMN projection_materialized_revision INTEGER;
            ALTER TABLE storage_conversation_turns ADD COLUMN projection_patch_count INTEGER DEFAULT 0;
            ALTER TABLE storage_conversation_turns ADD COLUMN projection_patch_bytes INTEGER DEFAULT 0;
        ''')
        if state == 'checkpoint':
            connection.execute('UPDATE storage_conversation_turns SET projection_checkpoint_revision=projection_revision')
        elif state == 'head':
            connection.execute('UPDATE storage_conversation_turns SET projection_materialized_revision=0, '
                               'projection_patch_count=1, projection_patch_bytes=10')
        elif state == 'invalid_head':
            connection.execute('UPDATE storage_conversation_turns SET projection_patch_count=1')
        elif state == 'orphan_checkpoint':
            connection.executescript('CREATE TABLE storage_turn_projection_checkpoints (turn_id TEXT PRIMARY KEY); '
                                     "INSERT INTO storage_turn_projection_checkpoints VALUES ('orphan')")
    session = FakeSession()
    checkpoint = tmp_path / 'checkpoint'
    if state == 'materialized':
        import_sqlite_authority(source, session, checkpoint, str(uuid4()))
        assert len(session.turns) == 4
        return
    with pytest.raises(MigrationImportError, match='projection'):
        import_sqlite_authority(source, session, checkpoint, str(uuid4()))
    assert not checkpoint.exists()
    assert not session.turns
    assert not session.conversations


def test_native_large_turn_transfers_reopens_and_preserves_public_content(tmp_path):
    binary = _resolve_tofudb_binary()
    source = tmp_path / 'source.db'
    turns = _sample_turns()
    turns[1]['projection'] = {'content': '文' * (3 * 1024 * 1024), 'timestamp': 2200}
    expected = turns[1]['projection']
    _seed_source(source, conversations=_sample_conversations(), turns=turns)
    target = tmp_path / 'target'
    authority = _initialize(binary, target)
    checkpoint = tmp_path / 'checkpoint'

    def crash(domain, entry):
        if domain == 'turns' and not entry['complete'] and entry['rows'] >= 2:
            raise _Crash(domain)

    with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7, timeout=120) as session:
        with pytest.raises(_Crash):
            import_sqlite_authority(source, session, checkpoint, authority,
                                    batch_rows=1, after_verified_prefix=crash)
    with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7, timeout=120) as session:
        import_sqlite_authority(source, session, checkpoint, authority)
        observed = importer.turn_payload.read(session, {'id': 'turn-alpha-2', 'conversation_id': 'conv-alpha'})
        assert observed['projection'] == expected
        status = Path(f'/proc/{session._process.pid}/status').read_text()
        peak = next(int(line.split()[1])*1024 for line in status.splitlines() if line.startswith('VmHWM:'))
        assert peak < 512 * 1024 * 1024

    # Serve the same authority and exercise ordinary streamed read operations.
    import select
    from tofu_db.wire import StorageV2Session
    secret = ('large-turn-' + uuid4().hex).encode()
    process = subprocess.Popen([
        str(binary), 'serve', '--data-dir', str(target), '--owner-id', '11',
        '--tenant-id', '7', '--search-projection-dir', str(tmp_path / 'search'),
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
       text=True, env={**os.environ, 'TOFU_STORAGE_TOKEN': secret.decode()})
    try:
        assert select.select([process.stdout], [], [], 30)[0]
        ready = json.loads(process.stdout.readline())
        client = StorageV2Session('127.0.0.1', ready['port'], secret, owner_id=11, tenant_id=7, timeout=120)
        try:
            response = client.request('turn.get', {'conversation_id': 'conv-alpha', 'turn_id': 'turn-alpha-2', 'user_id': 11},
                                      command_id=None, deadline_unix_ms=int(time.time()*1000)+120000)
            response.raise_for_error()
            assert response.json()['projection'] == expected
            response = client.request('turn.list', {'conversation_id': 'conv-alpha', 'user_id': 11},
                                      command_id=None, deadline_unix_ms=int(time.time()*1000)+120000)
            response.raise_for_error()
            assert next(turn for turn in response.json() if turn['turnId'] == 'turn-alpha-2')['projection'] == expected
        finally:
            client.close()
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def test_native_large_trash_turn_uses_pinned_snapshot_transfer(tmp_path):
    binary = _resolve_tofudb_binary()
    source = tmp_path / 'source.db'
    _seed_source(source)
    content = '旧' * (3 * 1024 * 1024)
    _seed_trash(source, turns=[content])
    target = tmp_path / 'target'
    authority = _initialize(binary, target)
    checkpoint = tmp_path / 'checkpoint'
    with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7, timeout=120) as session:
        import_sqlite_authority(source, session, checkpoint, authority)
    with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7, timeout=120) as session:
        import_sqlite_authority(source, session, checkpoint, authority)
        observed = importer.turn_payload.read(session, {
            'id': 'trash-turn-0', 'conversation_id': 'retired', 'trash': True})
        assert observed['projection']['content'] == content
        assert importer._query(session, 'conversation.get', {'conv_id': 'retired', 'user_id': 11}) is None


def _seed_retired_error_rows(path):
    _seed_source(path)
    with sqlite3.connect(path) as connection:
        connection.executescript('''CREATE TABLE error_resolutions (
            fingerprint TEXT PRIMARY KEY, logger_name TEXT, sample_message TEXT,
            resolved_by TEXT, ticket TEXT, notes BLOB, resolved_at REAL, updated_at REAL);
        ''')
        connection.executemany('INSERT INTO error_resolutions VALUES (?,?,?,?,?,?,?,?)', [
            ('old-a', 'legacy', '历史记录', '', None, b'\x00\xff', 1.25, -0.125),
            ('old-b', 'legacy', 'retained', '', 'T-1', 'original text', 2.5, 3.125),
        ])


def test_retired_rows_resume_preserve_types_and_refuse_missing_verified_prefix(tmp_path):
    from lib.storage_sidecar import tofudb_migration_retired as retired
    source = tmp_path / 'source.db'
    _seed_retired_error_rows(source)
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'checkpoint'
    def crash(domain, entry):
        if domain == 'retired.error_resolutions' and entry['rows'] == 1:
            raise _Crash(domain)
    with pytest.raises(_Crash):
        import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1,
                               after_verified_prefix=crash)
    import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)
    archives = [value for (namespace, _), value in session.records.items()
                if namespace == retired.NAMESPACE]
    assert len(archives) == 3
    assert _SCHEMA_ARCHIVE_RECORD_KEY in session.records
    payloads = [json.loads(value['value_json']) for value in archives]
    assert any(['blob', '00ff'] in value['cells'] for value in payloads)
    assert any(['real', '-0x1.0000000000000p-3'] in value['cells'] for value in payloads)
    key = next(key for key in session.records if key[0] == retired.NAMESPACE)
    del session.records[key]
    with pytest.raises(MigrationImportError, match='source and target'):
        import_sqlite_authority(source, session, checkpoint, authority)
    assert key not in session.records


def test_retired_rows_extra_columns_fail_before_target_mutation(tmp_path):
    source = tmp_path / 'source.db'
    _seed_retired_error_rows(source)
    with sqlite3.connect(source) as connection:
        connection.execute('ALTER TABLE error_resolutions ADD COLUMN unknown_history TEXT')
    session = FakeSession()
    with pytest.raises(MigrationImportError, match='unarchived columns'):
        import_sqlite_authority(source, session, tmp_path / 'checkpoint', str(uuid4()))
    assert session.records == {}


def test_native_retired_rows_reopen_without_reactivating_tasks(tmp_path):
    from lib.storage_sidecar import tofudb_migration_retired as retired
    binary = _resolve_tofudb_binary()
    source = tmp_path / 'source.db'
    _seed_retired_error_rows(source)
    target = tmp_path / 'target'
    authority = _initialize(binary, target)
    checkpoint = tmp_path / 'checkpoint'
    for _ in range(2):
        with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7) as session:
            import_sqlite_authority(source, session, checkpoint, authority)
            page = importer._query(session, 'migration.record.scan', {'cursor': None})
            assert len([row for row in page['records'] if row['namespace'] == retired.NAMESPACE
                        and row['key'].startswith('error_resolutions:')]) == 2
            assert importer._query(session, 'migration.timer.scan', {'cursor': None})['rows'] == []


@pytest.mark.parametrize('bound', ['MAX_ARCHIVE_ROWS', 'MAX_ARCHIVE_BYTES'])
def test_retired_archive_budget_refuses_before_target_mutation(tmp_path, monkeypatch, bound):
    source = tmp_path / 'source.db'
    _seed_retired_error_rows(source)
    monkeypatch.setattr(importer.retired, bound, 1)
    session = FakeSession()
    with pytest.raises(MigrationImportError, match='budget exceeded'):
        import_sqlite_authority(source, session, tmp_path / 'checkpoint', str(uuid4()))
    assert session.records == {}


def test_retired_archive_malformed_target_is_not_repaired_on_resume(tmp_path):
    source = tmp_path / 'source.db'
    _seed_retired_error_rows(source)
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'checkpoint'
    import_sqlite_authority(source, session, checkpoint, authority)
    key = next(key for key in session.records if key[0] == importer.retired.NAMESPACE)
    session.records[key]['value_json'] = '{malformed'
    with pytest.raises(MigrationImportError, match='malformed'):
        import_sqlite_authority(source, session, checkpoint, authority)
    assert session.records[key]['value_json'] == '{malformed'


def test_retired_archive_rejects_corrupt_empty_checkpoint(tmp_path):
    source = tmp_path / 'source.db'
    _seed_retired_error_rows(source)
    authority = str(uuid4())
    path = tmp_path / 'checkpoint'
    with importer.SQLiteMigrationSource(source) as source_reader:
        binding = importer.compute_binding(source_reader, authority)
    with MigrationCheckpoint(path, binding) as checkpoint:
        checkpoint.record_verified_prefix('retired.error_resolutions', cursor=[],
                                          source_rows=0, source_sha256='0' * 64,
                                          target_rows=0, target_sha256='0' * 64,
                                          complete=False)
    session = FakeSession()
    with pytest.raises(MigrationImportError, match='retired archive checkpoint differs'):
        import_sqlite_authority(source, session, path, authority)
    assert session.records == {}


def test_native_retired_accounts_and_counters_reopen_without_changing_login_accounts(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source)
    with sqlite3.connect(source) as connection:
        connection.executescript('''
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT,
                display_name TEXT, password_hash TEXT, created_at REAL);
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE scoped_sequences (namespace TEXT, scope_key TEXT, value INTEGER,
                PRIMARY KEY(namespace, scope_key));
            INSERT INTO users VALUES (1, 'retired-login', '历史账户',
                'archived-noncredential-test-hash', 1234.5);
            INSERT INTO schema_meta VALUES ('version', 'legacy-17');
            INSERT INTO scoped_sequences VALUES ('old-timer', 'shared', 9223372036854775807);
        ''')
    binary = _resolve_tofudb_binary()
    target = tmp_path / 'target'
    authority = _initialize(binary, target)
    checkpoint = tmp_path / 'checkpoint'
    with importer.SQLiteMigrationSource(source) as reader:
        expected = []
        for table in importer.retired.TABLES:
            if table['name'] in ('users', 'schema_meta', 'scoped_sequences'):
                expected.extend(importer.retired._document(table, row) for row in
                                reader.rows(table['name'], table['columns'], table['keys']))
    for _ in range(2):
        with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7) as session:
            import_sqlite_authority(source, session, checkpoint, authority)
            for document in expected:
                assert importer._query(session, 'migration.record.get', {
                    'namespace': document['namespace'], 'key': document['key']}) == document
            assert importer._query(session, 'migration.tenant.user.scan', {'cursor': None})['rows'] == []
    assert any('9223372036854775807' in document['value_json'] for document in expected)


def _seed_legacy_large_result(path, repeats=20000):
    _seed_source(path)
    table = next(table for table in importer.retired_large.TABLES if table['name'] == 'task_results')
    content = '汉字\x00' * repeats
    row = {column: None for column in table['columns']}
    row.update(task_id='legacy-large', conv_id='retired-parent', content=content,
               thinking='original reasoning', status='completed', tool_rounds=b'\x00\xff' * 1000,
               created_at=1.25, completed_at=2.5)
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE task_results (' + ','.join(
            '"' + column + '"' + (' TEXT PRIMARY KEY' if column == 'task_id' else '')
            for column in table['columns']) + ')')
        connection.execute('INSERT INTO task_results VALUES (' + ','.join('?' for _ in row) + ')',
                           tuple(row[column] for column in table['columns']))
    return content.encode('utf-8')


def test_retired_large_archive_recovers_parts_before_manifest_and_rejects_missing_part(tmp_path, monkeypatch):
    large = importer.retired_large
    source = tmp_path / 'source.db'
    _seed_legacy_large_result(source)
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'checkpoint'
    original = session._record_put
    def lost_ack(payload):
        result = original(payload)
        if payload['namespace'] == large.NAMESPACE and ':part:' in payload['key']:
            raise _Crash('part committed before manifest')
        return result
    monkeypatch.setattr(session, '_record_put', lost_ack)
    with pytest.raises(_Crash):
        import_sqlite_authority(source, session, checkpoint, authority)
    partial = [key for key in session.records if key[0] == large.NAMESPACE]
    assert partial and all(':part:' in key[1] for key in partial)
    monkeypatch.setattr(session, '_record_put', original)
    import_sqlite_authority(source, session, checkpoint, authority)
    assert any(key[0] == large.NAMESPACE and ':part:' not in key[1] for key in session.records)
    missing = next(key for key in session.records if key[0] == large.NAMESPACE and ':part:' in key[1])
    del session.records[missing]
    with pytest.raises(MigrationImportError, match='source and target'):
        import_sqlite_authority(source, session, checkpoint, authority)
    assert missing not in session.records


def test_retired_large_archive_rejects_extra_part_and_resource_overflow(tmp_path, monkeypatch):
    large = importer.retired_large
    source = tmp_path / 'source.db'
    _seed_legacy_large_result(source)
    session = FakeSession()
    authority = str(uuid4())
    checkpoint = tmp_path / 'checkpoint'
    monkeypatch.setitem(large.LIMITS, 'maximumRowBytes', 1024)
    with pytest.raises(MigrationImportError, match='row budget'):
        import_sqlite_authority(source, session, checkpoint, authority)
    assert not session.records
    monkeypatch.undo()
    import_sqlite_authority(source, session, checkpoint, authority)
    key = next(key for key in session.records if key[0] == large.NAMESPACE and ':part:' in key[1])
    bad = dict(session.records[key])
    bad['key'] = bad['key'].rsplit(':', 1)[0] + ':999999'
    value = json.loads(bad['value_json']); value['part'] = 999999
    bad['value_json'] = json.dumps(value)
    session.records[(large.NAMESPACE, bad['key'])] = bad
    with pytest.raises(MigrationImportError, match='no source bytes'):
        import_sqlite_authority(source, session, checkpoint, authority)


def test_native_retired_large_archive_reopens_with_exact_raw_bytes_and_bounded_memory(tmp_path):
    large = importer.retired_large
    source = tmp_path / 'source.db'
    raw = _seed_legacy_large_result(source, 1600000)
    binary = _resolve_tofudb_binary()
    target = tmp_path / 'target'
    authority = _initialize(binary, target)
    checkpoint = tmp_path / 'checkpoint'
    def crash(domain, entry):
        if domain == 'retired_large.task_results' and entry['rows'] == 1 and not entry['complete']:
            raise _Crash(domain)
    with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7) as session:
        tracemalloc.start()
        try:
            with pytest.raises(_Crash):
                import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1,
                                       after_verified_prefix=crash)
            assert tracemalloc.get_traced_memory()[1] < 32 * 1024 * 1024
        finally:
            tracemalloc.stop()
    with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7) as session:
        import_sqlite_authority(source, session, checkpoint, authority)
        table = next(table for table in large.TABLES if table['name'] == 'task_results')
        key = large._key(table, ('legacy-large',))
        root = importer._query(session, 'migration.record.get', {'namespace': large.NAMESPACE, 'key': key})
        cells = json.loads(root['value_json'])['cells']
        column = table['columns'].index('content')
        assert cells[column][1] == len(raw)
        assert cells[column][2] == hashlib.sha256(raw).hexdigest()
        digest = hashlib.sha256()
        for index in range(cells[column][3]):
            part = importer._query(session, 'migration.record.get', {
                'namespace': large.NAMESPACE, 'key': f'{key}:part:{column}:{index}'})
            digest.update(bytes.fromhex(json.loads(part['value_json'])['data_hex']))
        assert digest.digest() == hashlib.sha256(raw).digest()
        for catalog in ('headers', 'summaries', 'live', 'cost_index'):
            assert importer._query(session, 'migration.task.result.scan', {
                'table': catalog, 'cursor': None})['rows'] == []


def test_native_retired_fts_keeps_unique_text_rowids_and_configuration(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source)
    text = 'search-only history 独有正文\0' * 300
    with sqlite3.connect(source) as db:
        db.execute("CREATE VIRTUAL TABLE conversations_fts USING fts5(search_text, tokenize='unicode61')")
        db.execute('INSERT INTO conversations_fts(rowid,search_text) VALUES(91,?)', (text,))
    binary = _resolve_tofudb_binary()
    target = tmp_path / 'target'
    authority = _initialize(binary, target)
    for _ in range(2):
        with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7) as session:
            import_sqlite_authority(source, session, tmp_path / 'checkpoint', authority)
            table = next(t for t in importer.retired_large.TABLES if t['name'] == 'conversations_fts_content')
            key = importer.retired_large._key(table, (91,))
            root = importer._query(session, 'migration.record.get', {
                'namespace': importer.retired_large.NAMESPACE, 'key': key})
            cells = json.loads(root['value_json'])['cells']
            assert cells[0] == ['integer', '91']
            assert cells[1][2] == hashlib.sha256(text.encode()).hexdigest()
            config_table = next(t for t in importer.retired.TABLES if t['name'] == 'conversations_fts_config')
            with importer.SQLiteMigrationSource(source) as reader:
                for row in reader.rows(config_table['name'], config_table['columns'], config_table['keys']):
                    importer._verify_record(importer.retired._document(config_table, row), session)


def test_native_retired_pricing_and_limits_do_not_override_current_authority(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source)
    with sqlite3.connect(source) as connection:
        connection.executescript('''
            CREATE TABLE pricing_cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE rate_limit_events (id INTEGER PRIMARY KEY, endpoint TEXT NOT NULL, ip TEXT NOT NULL, ts_ms INTEGER NOT NULL);
            INSERT INTO pricing_cache VALUES ('pricing', '{"inputPrice":999}', 1234567);
            INSERT INTO rate_limit_events VALUES (9223372036854775807, '/retired', '127.0.0.9', 1000);
        ''')
    expected = []
    with importer.SQLiteMigrationSource(source) as reader:
        for table in importer.retired.TABLES:
            if table['name'] in ('pricing_cache', 'rate_limit_events'):
                expected.extend(importer.retired._document(table, row) for row in
                                reader.rows(table['name'], table['columns'], table['keys']))
    binary = _resolve_tofudb_binary()
    target = tmp_path / 'target'
    authority = _initialize(binary, target)
    for _ in range(2):
        with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7) as session:
            import_sqlite_authority(source, session, tmp_path / 'checkpoint', authority)
            for document in expected:
                assert importer._query(session, 'migration.record.get', {
                    'namespace': document['namespace'], 'key': document['key']}) == document
            assert importer._query(session, 'migration.record.get', {
                'namespace': 'pricing_cache', 'key': 'pricing'}) is None
            assert importer._query(session, 'migration.rate.limit.event.scan', {'cursor': None})['rows'] == []


@pytest.mark.parametrize('key,value', [
    ('unknown_replay_checkpoint', '123'),
    ('authority_uuid', 'invalid-uuid'),
    ('project_brain_cutover_v1', 'pending'),
    ('raw_archive_stored_bytes', '-1'),
    ('raw_archive_stored_bytes', '18446744073709551616'),
])
def test_source_metadata_requires_explicit_semantics_before_target_write(tmp_path, key, value):
    source = tmp_path / 'source.db'
    _seed_source(source)
    with sqlite3.connect(source) as db:
        db.execute('INSERT INTO storage_meta VALUES (?,?)', (key, value))
    session = FakeSession()
    with pytest.raises(MigrationImportError):
        import_sqlite_authority(source, session, tmp_path / 'checkpoint', str(uuid4()))
    assert session.records == {}
    assert not (tmp_path / 'checkpoint').exists()


def test_native_source_metadata_preserves_provenance_without_overwriting_native_identity(tmp_path):
    source = tmp_path / 'source.db'
    _seed_source(source)
    source_uuid = str(uuid4())
    with sqlite3.connect(source) as db:
        db.executemany('INSERT INTO storage_meta VALUES (?,?)', [
            ('authority_uuid', source_uuid), ('project_brain_cutover_v1', 'complete'),
            ('raw_archive_stored_bytes', '123456789'),
        ])
    table = next(t for t in importer.retired.TABLES if t['name'] == 'storage_meta')
    with importer.SQLiteMigrationSource(source) as reader:
        expected = [importer.retired._document(table, row) for row in
                    reader.rows(table['name'], table['columns'], table['keys'])]
    binary = _resolve_tofudb_binary()
    target = tmp_path / 'target'
    authority = _initialize(binary, target)
    assert authority != source_uuid
    for _ in range(2):
        with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7) as session:
            binding = import_sqlite_authority(source, session, tmp_path / 'checkpoint', authority)
            assert binding.target_authority_uuid == authority
            for document in expected:
                assert importer._query(session, 'migration.record.get', {
                    'namespace': document['namespace'], 'key': document['key']}) == document
            assert importer._query(session, 'migration.raw.archive.stats', {'scope': 'tenant'}) == {'stored_bytes': 0}


def test_conversation_sync_state_imports_events_and_heads_with_owner_scoping(tmp_path):
    source = tmp_path / 'source.db'
    conversation = {
        'id': 'conv-sync', 'user_id': _OWNER_ID, 'title': 'sync',
        'created_at_ms': 1_700_000_000_000, 'updated_at_ms': 1_700_000_000_100,
    }
    sync_events = [
        {'conversation_id': 'conv-sync', 'user_id': _OWNER_ID, 'sync_sequence': 1,
         'change_type': 'message.append', 'event': {'role': 'user', 'text': 'hello'},
         'created_at': 1_700_000_000_001},
        {'conversation_id': 'conv-sync', 'user_id': _OWNER_ID, 'sync_sequence': 2,
         'change_type': 'message.append', 'event': {'role': 'assistant', 'text': 'world'},
         'created_at': 1_700_000_000_002},
    ]
    sync_heads = [
        {'conversation_id': 'conv-sync', 'user_id': _OWNER_ID, 'sync_sequence': 2,
         'updated_at': 1_700_000_000_002},
    ]
    _seed_source(source, conversations=[conversation],
                 sync_events=sync_events, sync_heads=sync_heads)
    session = FakeSession()
    binding = import_sqlite_authority(source, session, tmp_path / 'checkpoint', str(uuid4()))

    # Heads import only after every event has landed; verify both snapshots.
    assert session.sync_events == {
        ('conv-sync', 1): {
            'tenant_id': _TENANT_ID, 'owner_user_id': _OWNER_ID,
            'conversation_id': 'conv-sync', 'sync_sequence': 1,
            'change_type': 'message.append',
            'occurred_at_ms': 1_700_000_000_001, 'committed_at_ms': 1_700_000_000_001,
            'payload_json': json.dumps({'role': 'user', 'text': 'hello'},
                                       separators=(',', ':'), sort_keys=True),
        },
        ('conv-sync', 2): {
            'tenant_id': _TENANT_ID, 'owner_user_id': _OWNER_ID,
            'conversation_id': 'conv-sync', 'sync_sequence': 2,
            'change_type': 'message.append',
            'occurred_at_ms': 1_700_000_000_002, 'committed_at_ms': 1_700_000_000_002,
            'payload_json': json.dumps({'role': 'assistant', 'text': 'world'},
                                       separators=(',', ':'), sort_keys=True),
        },
    }
    assert session.sync_heads == {
        'conv-sync': {
            'tenant_id': _TENANT_ID, 'owner_user_id': _OWNER_ID,
            'conversation_id': 'conv-sync', 'sync_sequence': 2,
        },
    }
    assert binding.target_authority_uuid is not None


def test_conversation_sync_event_owner_outside_operator_scope_fails_closed(tmp_path):
    source = tmp_path / 'source.db'
    conversation = {
        'id': 'conv-sync', 'user_id': _OWNER_ID, 'title': 'sync',
        'created_at_ms': 1_700_000_000_000, 'updated_at_ms': 1_700_000_000_100,
    }
    sync_events = [
        {'conversation_id': 'conv-sync', 'user_id': _OWNER_ID + 99, 'sync_sequence': 1,
         'change_type': 'message.append', 'event': {'role': 'user', 'text': 'x'},
         'created_at': 1_700_000_000_001},
    ]
    _seed_source(source, conversations=[conversation], sync_events=sync_events)
    session = FakeSession()
    with pytest.raises(MigrationImportError, match='sync change owner'):
        import_sqlite_authority(source, session, tmp_path / 'checkpoint', str(uuid4()))
    # Fail-closed: the out-of-scope event must not reach the target.
    assert session.sync_events == {}


def test_import_command_receipts_v2_blob_keys_stream_through_digest_path(tmp_path):
    """32-byte BLOB primary keys must stream through the digest reader.

    The generic keyset scanner admits only TEXT/INTEGER keys; production
    WITHOUT ROWID receipt tables hold raw SHA-256 BLOB keys. The domain driver
    routes them through digest_key_rows and exposes lowercase-hex identities.
    """
    from lib.storage_sidecar.receipt_codec import (
        command_receipt_identity_v2, encode_receipt_response)

    source = tmp_path / 'source.db'
    _seed_source(source)
    connection = sqlite3.connect(source)
    connection.execute(
        'CREATE TABLE storage_command_receipts_v2('
        'command_key BLOB PRIMARY KEY, operation TEXT NOT NULL,'
        'request_digest BLOB NOT NULL, response_json BLOB NOT NULL,'
        'committed_at_ms INTEGER NOT NULL) WITHOUT ROWID')
    keys = []
    for index in range(3):
        command_key, digest_bytes = command_receipt_identity_v2(
            f'cmd-{index}', 'record.put', f'{index}' * 64)
        keys.append(command_key.hex())
        connection.execute(
            'INSERT INTO storage_command_receipts_v2 VALUES (?,?,?,?,?)',
            (command_key, 'record.put', digest_bytes,
             encode_receipt_response({'ok': True, 'version': index}), index + 1))
    connection.commit()
    connection.close()

    session = FakeSession()
    checkpoint = tmp_path / 'progress.json'
    authority = str(uuid4())
    import_sqlite_authority(source, session, checkpoint, authority, batch_rows=2)
    restored = {key[2]: value for key, value in session.command_receipts_v2.items()}
    assert set(restored) == set(keys)
    assert restored[keys[1]]['operation'] == 'record.put'
    assert restored[keys[1]]['committed_at_ms'] == 2

    # Idempotent resume: a second full run restores nothing new and passes.
    import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)
    assert len(session.command_receipts_v2) == 3


def test_import_task_results_tolerates_blob_typed_json_cells(tmp_path):
    """Legacy SQLite affinity stored UTF-8 JSON as BLOB; text admission decodes it."""
    source = tmp_path / 'source.db'
    _seed_source(source, records=_sample_task_results())
    connection = sqlite3.connect(source)
    connection.execute(
        "UPDATE storage_records SET value_json = CAST(value_json AS BLOB) "
        "WHERE namespace = 'task_results'")
    connection.commit()
    connection.close()
    session = FakeSession()
    import_sqlite_authority(source, session, tmp_path / 'progress.json', str(uuid4()))
    assert set(session.task_results) == {'task-alpha', 'task-running', 'task-cost'}


def test_import_task_results_adopts_ownerless_legacy_rows(tmp_path):
    """Legacy task results without user_id join the single operator scope."""
    source = tmp_path / 'source.db'
    rows = _sample_task_results() + [{
        'namespace': 'task_results', 'record_key': 'task-legacy',
        'value': {'task_id': 'task-legacy', 'conv_id': 'conv-alpha',
                  'status': 'completed', 'created_at': 500, 'completed_at': 900,
                  'content': 'legacy-result'},
        'version': 2, 'updated_at_ms': 99,
    }]
    _seed_source(source, records=rows)
    session = FakeSession()
    import_sqlite_authority(source, session, tmp_path / 'progress.json', str(uuid4()))
    assert 'task-legacy' in session.task_results
    assert session.task_results['task-legacy']['owner_user_id'] == _OWNER_ID


def test_import_storage_events_round_trip_and_resume(tmp_path):
    """Storage event spine: exact payload bytes through the batch lane."""
    source = tmp_path / 'source.db'
    _seed_source(source)
    connection = sqlite3.connect(source)
    connection.execute(
        'CREATE TABLE storage_events ('
        'task_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_json BLOB NOT NULL,'
        'created_at_ms INTEGER NOT NULL, stream_kind TEXT, event_type TEXT, event_kind TEXT,'
        'owner_user_id INTEGER, project_key TEXT, project_sequence INTEGER,'
        'PRIMARY KEY (task_id, sequence))')
    for index in range(5):
        connection.execute(
            'INSERT INTO storage_events VALUES (?,?,?,?,?,?,?,?,?,?)',
            (f'task-{index % 2}', index, sqlite3.Binary(json.dumps({'seq': index}).encode()),
             1000 + index, 'task', 'log', '', 1, '', 0))
    connection.commit()
    connection.close()
    session = FakeSession()
    checkpoint = tmp_path / 'progress.json'
    authority = str(uuid4())
    import_sqlite_authority(source, session, checkpoint, authority, batch_rows=2)
    assert len(session.storage_events) == 5
    stored = session.storage_events[('task-1', 1)]
    assert stored['payload_json_hex'] == json.dumps({'seq': 1}).encode().hex()
    assert stored['event_type'] == 'log'
    # Idempotent resume: nothing new, nothing changes.
    import_sqlite_authority(source, session, checkpoint, authority, batch_rows=1)
    assert len(session.storage_events) == 5


def test_import_worker_wake_observation_preserves_nonzero_and_legacy_default(tmp_path):
    source = tmp_path/'wake-source.db'
    _seed_source(source,worker_jobs=_sample_worker_jobs())
    with sqlite3.connect(source) as connection:
        connection.execute('ALTER TABLE storage_worker_jobs ADD COLUMN wake_sequence BIGINT NOT NULL DEFAULT 0')
        connection.execute("UPDATE storage_worker_jobs SET wake_sequence=7 WHERE task_id='job-2'")
    session = FakeSession()
    import_sqlite_authority(source,session,tmp_path/'progress.json',str(uuid4()))
    assert session.worker_jobs['job-2']['wake_sequence'] == 7
    assert session.worker_jobs['job-1']['wake_sequence'] == 0


@pytest.mark.parametrize('pending', [False, True])
def test_orchestration_migration_preserves_election_instead_of_inferring_from_error(pending):
    from lib.storage_sidecar.tofudb_migration_import import _orchestration_run_document
    from lib.orchestration.events import CONTINUITY_RESTART_ERROR
    import json
    _, runs, _ = _sample_orchestration()
    row = dict(runs[0], status='error', created_by='chat_goal_mode',
               error=json.dumps(CONTINUITY_RESTART_ERROR), continuity_pending=int(pending))
    class Source:
        tables = ()
    document = _orchestration_run_document(Source(), row, _OWNER_ID)
    assert document['continuity_pending'] is pending


def test_orchestration_import_binds_and_restores_a_later_schema_election_column(tmp_path):
    definitions, runs, events = _sample_orchestration()
    source = tmp_path / 'pending-source.db'
    _seed_source(source, orchestration_definitions=definitions,
                 orchestration_runs=runs, orchestration_events=events)
    with sqlite3.connect(source) as connection:
        connection.execute('ALTER TABLE orchestration_runs ADD COLUMN continuity_pending INTEGER NOT NULL DEFAULT 0')
        connection.execute('UPDATE orchestration_runs SET continuity_pending=1,status=?,created_by=?,error=? WHERE id=?',
                           ('error', 'chat_goal_mode', json.dumps({'kind':'worker_lost','source':'orchestration.startup_recovery'}), 'run-alpha'))
    session = FakeSession()
    checkpoint = tmp_path / 'pending-progress.json'
    authority = str(uuid4())
    import_sqlite_authority(source, session, checkpoint, authority)
    assert session.orch_runs[('run-alpha', '')]['continuity_pending'] is True
    # The optional physical column participates in binding; it cannot change silently on resume.
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE orchestration_runs SET continuity_pending=0 WHERE id='run-alpha'")
    with pytest.raises((MigrationImportError, ValueError)):
        import_sqlite_authority(source, session, checkpoint, authority)


def test_native_orchestration_import_rebuilds_and_closes_pending_election(tmp_path):
    configured = os.environ.get('TOFUDB_BIN')
    if not configured:
        pytest.skip('Set TOFUDB_BIN to a source-matched engine')
    binary = Path(configured).resolve()
    target = tmp_path / 'pending-native-target'
    authority = _initialize(binary, target)
    snapshot = {
        'id': 'pending-native', 'tenant_label': '', 'orch_id': 'chat-goal:pending-native',
        'name': '', 'definition': {'nodes': [], 'edges': []}, 'input': 'objective',
        'created_by': 'chat_goal_mode', 'created_at': 100, 'status': 'error',
        'final_text': '', 'error': {'kind': 'worker_lost', 'source': 'orchestration.startup_recovery'},
        'updated_at': 200, 'finished_at': 200, 'next_event_sequence': 1,
        'continuity_pending': True, 'goal_status': 'failed', 'goal_reason': 'worker_lost',
        'goal_policy': {}, 'goal_outcome': {}, 'goal_stop_report': '',
    }
    def request(session, operation, payload):
        response = session.request(operation, payload,
                                   command_id=uuid4().hex if operation.endswith('.restore') else None,
                                   deadline_unix_ms=int(time.time() * 1000) + 15000)
        response.raise_for_error()
        return response.json()
    with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7) as session:
        request(session, 'migration.orchestration.run.restore', snapshot)
        # Idempotent readback verifies the pending index together with exact run state.
        request(session, 'migration.orchestration.run.restore', snapshot)
        value = request(session, 'migration.orchestration.run.get', {'id': snapshot['id'], 'tenant_label': ''})
        assert value['continuity_pending'] is True
        request(session, 'migration.orchestration.event.restore', {
            'run_id': snapshot['id'], 'tenant_label': '', 'sequence': 0,
            'payload': {'type': 'execution_recovery_claim', 'successor_run_id': 'bound-successor'}})
        value = request(session, 'migration.orchestration.run.get', {'id': snapshot['id'], 'tenant_label': ''})
        assert value['continuity_pending'] is False
        assert value['status'] == 'error' and value['finished_at'] == 200
        with pytest.raises(StorageV2Error):
            request(session, 'migration.orchestration.run.restore', snapshot)
