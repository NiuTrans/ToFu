"""Read and validate the database IR in declared guard order.
Entry: load_ir. Domain modules preserve exact compatibility checks; receipt field names are shared with projections."""

from .inputs import ContractInputs
from .validate_migration import validate_migration
from .validate_engine import validate_engine
from .validate_coordination import (
    validate_orchestration_definitions,
    validate_orchestration_runs,
    validate_swarm,
    validate_integration,
    validate_queue,
    validate_scheduler,
    validate_timers,
    validate_project_brain,
    validate_recent_projects,
)
from .validate_identity import (
    validate_billing,
    validate_tenant_users,
    validate_credentials,
    validate_providers,
    validate_model_routing,
)
from .validate_content import (
    validate_paper_library,
    validate_paper_notes,
    validate_plugin_manifests,
    validate_knowledge_documents,
    validate_paper_reports,
    validate_paper_translations,
    validate_paper_podcasts,
    validate_raw_archives,
    validate_research_artifacts,
    validate_research_workspaces,
)
from .validate_records import (
    validate_browser_observations,
    validate_desktop,
    validate_records,
    validate_daily_costs,
    validate_log_aggregates,
    validate_optimizer_proposals,
    validate_optimizer_actions,
    validate_rate_limits,
    validate_artifacts,
)
from .validate_conversations import (
    validate_worker_jobs,
    validate_events,
    validate_archives,
    validate_conversations,
    validate_turns,
)


RECEIPT_NAMES = {
    'RECEIPT_COMPRESSION_THRESHOLD_BYTES': 'compressionThresholdBytes',
    'MAX_INLINE_RECEIPT_BYTES': 'maximumInlineBytes',
}


def load_ir(inputs: ContractInputs) -> dict:
    document = inputs.read_json(inputs.ir_source)
    if document.get("format") != "tofu.database-ir.v1":
        raise ValueError("tofudb_ir_v1.json has an unexpected format")
    receipts = document['commandReceipts']
    if any(type(receipts[key]) is not int or receipts[key] <= 0 for key in RECEIPT_NAMES.values()):
        raise ValueError('invalid command receipt byte budget')
    if not (receipts['maximumInlineBytes'] < receipts['compressionThresholdBytes']
            and receipts.get('payloadAdmission') == 'complete-receipt-without-private-stored-or-decoded-byte-quota'):
        raise ValueError('inconsistent command receipt admission')
    validate_migration(document)
    catalog = inputs.contract("storage_operations_v1.json")
    catalog_names = sorted(operation["name"] for operation in catalog["operations"])
    validate_engine(document, catalog_names)
    bounds = document.get("bounds")
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
    validate_orchestration_definitions(tables_by_name.get('orchestration_definitions'))
    validate_orchestration_runs(tables_by_name.get('orchestration_runs'))
    validate_swarm(tables_by_name.get('swarm_sessions'))
    validate_integration(tables_by_name.get('integration_workspaces'))
    validate_queue(tables_by_name.get('conversation_queue'))
    validate_scheduler(tables_by_name.get('scheduled_tasks'))
    validate_timers(tables_by_name.get('timers'))
    validate_billing(tables_by_name.get('billing_wallets_and_ledger'))
    validate_tenant_users(tables_by_name.get('tenant_users'))
    validate_credentials(tables_by_name.get('auth_credentials'))
    validate_browser_observations(tables_by_name.get('browser_site_observations'))
    validate_desktop(tables_by_name.get('desktop_egress_preferences'))
    validate_records(tables_by_name.get('storage_records'))
    validate_project_brain(tables_by_name.get('project_brain_projects'))
    validate_daily_costs(tables_by_name.get('daily_cost_cache'))
    validate_recent_projects(tables_by_name.get('recent_projects'))
    validate_log_aggregates(tables_by_name.get('log_aggregates'))
    validate_paper_library(tables_by_name.get('paper_library'))
    validate_paper_notes(tables_by_name.get('paper_notes'))
    validate_plugin_manifests(tables_by_name.get('plugin_manifests'))
    validate_knowledge_documents(tables_by_name.get('knowledge_documents'))
    validate_paper_reports(tables_by_name.get('paper_reports'))
    validate_paper_translations(tables_by_name.get('paper_translations'))
    validate_paper_podcasts(tables_by_name.get('paper_podcasts'))
    validate_raw_archives(tables_by_name.get('raw_archives'))
    validate_research_artifacts(tables_by_name.get('research_artifacts'))
    validate_research_workspaces(tables_by_name.get('research_workspaces'))
    validate_optimizer_proposals(tables_by_name.get('optimizer_proposals'))
    validate_optimizer_actions(tables_by_name.get('optimizer_actions'))
    validate_rate_limits(tables_by_name.get('rate_limit_events'))
    validate_providers(tables_by_name.get('byo_providers'))
    validate_artifacts(tables_by_name.get('chat_artifacts'), tables_by_name.get('tool_result_artifacts'))
    validate_model_routing(tables_by_name.get('model_routing'))
    validate_worker_jobs(tables_by_name.get('storage_worker_jobs'))
    validate_events(tables_by_name.get('task_events'))
    validate_archives(tables_by_name.get('compaction_archives'))
    validate_conversations(tables_by_name.get('conversations'))
    validate_turns(tables_by_name.get('conversation_turns'))
    return document
