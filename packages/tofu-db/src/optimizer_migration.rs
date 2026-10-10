//! Offline optimizer proposal/action-log snapshot restoration for the
//! exclusive operator importer.
//!
//! The source keeps settled rows in `optimizer_proposals` and
//! `optimizer_action_log`. The live create/record paths restamp physical
//! timestamps and synthesize counts/indexes from the request clock, so this
//! adapter writes the exact source documents and rebuilds the created,
//! status, status-projection, proposal, applied, active-applied and expiry
//! indexes plus both owner counts inside one authority transaction. An
//! action whose proposal is absent fails closed exactly like the live record
//! path. Insert-or-match keeps replays idempotent: an identical snapshot is
//! read-only, a divergent occupant fails closed. Get returns the exact
//! snapshot spelling; scan enumerates ids owner-scoped and independent of
//! the source. Entry points: the three `migration.optimizer.*` operations
//! dispatched from `migration_session`.

use serde::Deserialize;
use serde_json::{json, Value};
use std::io;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_OPTIMIZER_ACTIONS_PER_OWNER, MAX_OPTIMIZER_DOCUMENT_BYTES,
    MAX_OPTIMIZER_PROPOSALS_PER_OWNER, MIGRATION_SESSION_MAX_PAGE_ROWS,
    OPTIMIZER_ACTION_ACTIVE_APPLIED_INDEX_NAMESPACE, OPTIMIZER_ACTION_APPLIED_INDEX_NAMESPACE,
    OPTIMIZER_ACTION_COUNT_NAMESPACE, OPTIMIZER_ACTION_DOCUMENT_NAMESPACE,
    OPTIMIZER_ACTION_EXPIRY_INDEX_NAMESPACE, OPTIMIZER_ACTION_PROPOSAL_INDEX_NAMESPACE,
    OPTIMIZER_PROPOSAL_COUNT_NAMESPACE, OPTIMIZER_PROPOSAL_CREATED_INDEX_NAMESPACE,
    OPTIMIZER_PROPOSAL_DOCUMENT_NAMESPACE, OPTIMIZER_PROPOSAL_STATUS_INDEX_NAMESPACE,
    OPTIMIZER_PROPOSAL_STATUS_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn snapshot_conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, message)
}

const PROPOSAL_LOGICAL_NAMESPACE: &str = "optimizer_proposals";
const ACTION_LOGICAL_NAMESPACE: &str = "optimizer_action_log";
const COUNT_KEY: &[u8] = b"count";

fn owner_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    raw: &[u8],
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        raw,
    )
}

fn descending_text(output: &mut Vec<u8>, value: &str) {
    for byte in value.bytes() {
        output.extend_from_slice(&[!byte, 254]);
    }
    output.extend_from_slice(&[u8::MAX, u8::MAX]);
}

fn ascending_text(output: &mut Vec<u8>, value: &str) {
    for byte in value.bytes() {
        output.extend_from_slice(&[byte, 1]);
    }
    output.extend_from_slice(&[0, 0]);
}

fn ordered_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    first: &str,
    id: &str,
    descending: bool,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity((first.len() + id.len()) * 2 + 2);
    if descending {
        descending_text(&mut raw, first);
    } else {
        ascending_text(&mut raw, first);
    }
    ascending_text(&mut raw, id);
    owner_key(transaction, namespace, &raw)
}

fn proposal_action_index_key(
    transaction: &AuthorityTransaction,
    proposal_id: &str,
    applied_at: &str,
    id: &str,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity(proposal_id.len() * 2 + 1);
    ascending_text(&mut raw, proposal_id);
    descending_text(&mut raw, applied_at);
    ascending_text(&mut raw, id);
    owner_key(transaction, OPTIMIZER_ACTION_PROPOSAL_INDEX_NAMESPACE, &raw)
}

fn proposal_status_index_key(
    transaction: &AuthorityTransaction,
    status: &str,
    created_at: &str,
    id: &str,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity((status.len() + created_at.len() + id.len()) * 2 + 4);
    ascending_text(&mut raw, status);
    descending_text(&mut raw, created_at);
    ascending_text(&mut raw, id);
    owner_key(transaction, OPTIMIZER_PROPOSAL_STATUS_INDEX_NAMESPACE, &raw)
}

fn proposal_status_key(transaction: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    owner_key(
        transaction,
        OPTIMIZER_PROPOSAL_STATUS_NAMESPACE,
        id.as_bytes(),
    )
}

fn read_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
) -> io::Result<u64> {
    let key = owner_key(transaction, namespace, COUNT_KEY)?;
    match database.entity_get(transaction, &key)? {
        None => Ok(0),
        Some(raw) if raw.len() == 8 => Ok(u64::from_le_bytes(raw.try_into().unwrap())),
        Some(_) => Err(invalid_data("optimizer count is malformed")),
    }
}

fn write_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    count: u64,
) -> io::Result<()> {
    database.entity_put(
        transaction,
        owner_key(transaction, namespace, COUNT_KEY)?,
        count.to_le_bytes().to_vec(),
    )
}

fn valid_text(value: &str, maximum: usize, required: bool) -> bool {
    (!required || !value.is_empty()) && value.chars().count() <= maximum
}

#[derive(Clone, Debug, Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct ProposalSnapshot {
    id: String,
    created_at: String,
    title: String,
    rationale: String,
    action_type: String,
    action_args: String,
    severity: String,
    confidence: f64,
    evidence: String,
    status: String,
    status_reason: String,
}

impl ProposalSnapshot {
    fn valid(&self) -> bool {
        valid_text(&self.id, 128, true)
            && valid_text(&self.created_at, 64, true)
            && valid_text(&self.title, 500, false)
            && valid_text(&self.rationale, 4000, false)
            && valid_text(&self.action_type, 256, true)
            && valid_text(&self.action_args, 2_000_000, true)
            && valid_text(&self.severity, 64, false)
            && self.confidence.is_finite()
            && (0.0..=1.0).contains(&self.confidence)
            && valid_text(&self.evidence, 2_000_000, true)
            && valid_text(&self.status, 64, false)
            && valid_text(&self.status_reason, 500, false)
    }
}

#[derive(Clone, Debug, Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct ActionSnapshot {
    id: String,
    proposal_id: String,
    applied_at: String,
    expires_at: String,
    pre_metric: String,
    outcome_metric: String,
    outcome_recorded_at: String,
    reverted_at: String,
    revert_reason: String,
}

impl ActionSnapshot {
    fn valid(&self) -> bool {
        valid_text(&self.id, 128, true)
            && valid_text(&self.proposal_id, 128, true)
            && valid_text(&self.applied_at, 64, true)
            && valid_text(&self.expires_at, 64, true)
            && valid_text(&self.pre_metric, 2_000_000, true)
            && valid_text(&self.outcome_metric, 2_000_000, false)
            && valid_text(&self.outcome_recorded_at, 64, false)
            && valid_text(&self.reverted_at, 64, false)
            && valid_text(&self.revert_reason, 500, false)
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RestoreRequest {
    table: String,
    document: Value,
    updated_at_ms: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GetRequest {
    table: String,
    id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ScanRequest {
    table: String,
    cursor: Option<String>,
}

fn document_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    id: &str,
) -> io::Result<EntityKey> {
    owner_key(transaction, namespace, id.as_bytes())
}

fn read_proposal(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<ProposalSnapshot>> {
    let key = document_key(transaction, OPTIMIZER_PROPOSAL_DOCUMENT_NAMESPACE, id)?;
    let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &key,
        PROPOSAL_LOGICAL_NAMESPACE,
        id,
        transaction.owner_user_id(),
        MAX_OPTIMIZER_DOCUMENT_BYTES,
    )?
    else {
        return Ok(None);
    };
    let mut value: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("optimizer proposal is malformed"))?;
    let object = value
        .as_object_mut()
        .ok_or_else(|| invalid_data("optimizer proposal is malformed"))?;
    if object.remove("user_id").and_then(|value| value.as_u64())
        != Some(transaction.owner_user_id())
    {
        return Err(invalid_data("optimizer proposal identity differs"));
    }
    let snapshot: ProposalSnapshot = serde_json::from_value(value)
        .map_err(|_| invalid_data("optimizer proposal is malformed"))?;
    if snapshot.id != id || !snapshot.valid() {
        return Err(invalid_data("optimizer proposal identity differs"));
    }
    Ok(Some(snapshot))
}

fn read_action(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<ActionSnapshot>> {
    let key = document_key(transaction, OPTIMIZER_ACTION_DOCUMENT_NAMESPACE, id)?;
    let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &key,
        ACTION_LOGICAL_NAMESPACE,
        id,
        transaction.owner_user_id(),
        MAX_OPTIMIZER_DOCUMENT_BYTES,
    )?
    else {
        return Ok(None);
    };
    let mut value: Value =
        serde_json::from_slice(&raw).map_err(|_| invalid_data("optimizer action is malformed"))?;
    let object = value
        .as_object_mut()
        .ok_or_else(|| invalid_data("optimizer action is malformed"))?;
    if object.remove("user_id").and_then(|value| value.as_u64())
        != Some(transaction.owner_user_id())
    {
        return Err(invalid_data("optimizer action identity differs"));
    }
    let snapshot: ActionSnapshot =
        serde_json::from_value(value).map_err(|_| invalid_data("optimizer action is malformed"))?;
    if snapshot.id != id || !snapshot.valid() {
        return Err(invalid_data("optimizer action identity differs"));
    }
    Ok(Some(snapshot))
}

fn put_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    logical_namespace: &str,
    id: &str,
    value: Value,
    updated_at_ms: u64,
) -> io::Result<()> {
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: document_key(transaction, namespace, id)?,
            namespace: logical_namespace.to_owned(),
            logical_key: id.to_owned(),
            value_json: serde_json::to_vec(&value)
                .map_err(|_| invalid_data("optimizer document cannot be encoded"))?,
            expected_version: None,
            updated_at_ms: updated_at_ms.max(1),
        },
        transaction.owner_user_id(),
        MAX_OPTIMIZER_DOCUMENT_BYTES,
    )
    .map(|_| ())
}

fn restore_proposal(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: Value,
    updated_at_ms: u64,
) -> io::Result<bool> {
    let proposal: ProposalSnapshot = serde_json::from_value(document)
        .map_err(|_| invalid_input("invalid optimizer proposal snapshot"))?;
    if !proposal.valid() {
        return Err(invalid_input("invalid optimizer proposal snapshot"));
    }
    if let Some(existing) = read_proposal(database, transaction, &proposal.id)? {
        return if serde_json::to_value(&existing).unwrap()
            == serde_json::to_value(&proposal).unwrap()
        {
            Ok(false)
        } else {
            Err(snapshot_conflict("optimizer proposal snapshot differs"))
        };
    }
    let count = read_count(database, transaction, OPTIMIZER_PROPOSAL_COUNT_NAMESPACE)?;
    if count >= MAX_OPTIMIZER_PROPOSALS_PER_OWNER as u64 {
        return Err(invalid_input("optimizer owner row bound is exhausted"));
    }
    let mut stored = serde_json::to_value(&proposal).unwrap();
    stored
        .as_object_mut()
        .expect("proposal serializes to an object")
        .insert(
            "user_id".to_owned(),
            Value::from(transaction.owner_user_id()),
        );
    write_count(
        database,
        transaction,
        OPTIMIZER_PROPOSAL_COUNT_NAMESPACE,
        count + 1,
    )?;
    put_document(
        database,
        transaction,
        OPTIMIZER_PROPOSAL_DOCUMENT_NAMESPACE,
        PROPOSAL_LOGICAL_NAMESPACE,
        &proposal.id,
        stored,
        updated_at_ms,
    )?;
    for key in [
        ordered_key(
            transaction,
            OPTIMIZER_PROPOSAL_CREATED_INDEX_NAMESPACE,
            &proposal.created_at,
            &proposal.id,
            true,
        )?,
        proposal_status_index_key(
            transaction,
            &proposal.status,
            &proposal.created_at,
            &proposal.id,
        )?,
    ] {
        database.entity_put(transaction, key, proposal.id.as_bytes().to_vec())?;
    }
    database.entity_put(
        transaction,
        proposal_status_key(transaction, &proposal.id)?,
        proposal.status.as_bytes().to_vec(),
    )?;
    let written = read_proposal(database, transaction, &proposal.id)?
        .ok_or_else(|| invalid_data("optimizer proposal snapshot is missing"))?;
    if serde_json::to_value(&written).unwrap() != serde_json::to_value(&proposal).unwrap() {
        return Err(invalid_data("optimizer proposal snapshot readback differs"));
    }
    Ok(true)
}

fn restore_action(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: Value,
    updated_at_ms: u64,
) -> io::Result<bool> {
    let action: ActionSnapshot = serde_json::from_value(document)
        .map_err(|_| invalid_input("invalid optimizer action snapshot"))?;
    if !action.valid() {
        return Err(invalid_input("invalid optimizer action snapshot"));
    }
    if let Some(existing) = read_action(database, transaction, &action.id)? {
        return if serde_json::to_value(&existing).unwrap() == serde_json::to_value(&action).unwrap()
        {
            Ok(false)
        } else {
            Err(snapshot_conflict("optimizer action snapshot differs"))
        };
    }
    // The live record path fails closed when its proposal is missing; the
    // migration snapshot carries the same referential constraint.
    if read_proposal(database, transaction, &action.proposal_id)?.is_none() {
        return Err(invalid_input("Optimizer proposal does not exist"));
    }
    let count = read_count(database, transaction, OPTIMIZER_ACTION_COUNT_NAMESPACE)?;
    if count >= MAX_OPTIMIZER_ACTIONS_PER_OWNER as u64 {
        return Err(invalid_input("optimizer owner row bound is exhausted"));
    }
    let mut stored = serde_json::to_value(&action).unwrap();
    stored
        .as_object_mut()
        .expect("action serializes to an object")
        .insert(
            "user_id".to_owned(),
            Value::from(transaction.owner_user_id()),
        );
    write_count(
        database,
        transaction,
        OPTIMIZER_ACTION_COUNT_NAMESPACE,
        count + 1,
    )?;
    put_document(
        database,
        transaction,
        OPTIMIZER_ACTION_DOCUMENT_NAMESPACE,
        ACTION_LOGICAL_NAMESPACE,
        &action.id,
        stored,
        updated_at_ms,
    )?;
    for key in [
        ordered_key(
            transaction,
            OPTIMIZER_ACTION_APPLIED_INDEX_NAMESPACE,
            &action.applied_at,
            &action.id,
            true,
        )?,
        proposal_action_index_key(
            transaction,
            &action.proposal_id,
            &action.applied_at,
            &action.id,
        )?,
    ] {
        database.entity_put(transaction, key, action.id.as_bytes().to_vec())?;
    }
    if action.reverted_at.is_empty() {
        database.entity_put(
            transaction,
            ordered_key(
                transaction,
                OPTIMIZER_ACTION_ACTIVE_APPLIED_INDEX_NAMESPACE,
                &action.applied_at,
                &action.id,
                true,
            )?,
            action.id.as_bytes().to_vec(),
        )?;
        database.entity_put(
            transaction,
            ordered_key(
                transaction,
                OPTIMIZER_ACTION_EXPIRY_INDEX_NAMESPACE,
                &action.expires_at,
                &action.id,
                false,
            )?,
            serde_json::to_vec(&json!({
                "id": action.id,
                "proposal_id": action.proposal_id,
                "expires_at": action.expires_at,
            }))
            .map_err(|_| invalid_data("optimizer expiry index cannot be encoded"))?,
        )?;
    }
    let written = read_action(database, transaction, &action.id)?
        .ok_or_else(|| invalid_data("optimizer action snapshot is missing"))?;
    if serde_json::to_value(&written).unwrap() != serde_json::to_value(&action).unwrap() {
        return Err(invalid_data("optimizer action snapshot readback differs"));
    }
    Ok(true)
}

fn restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: RestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid optimizer restore request"))?;
    if request.updated_at_ms == 0 {
        return Err(invalid_input("invalid optimizer snapshot metadata"));
    }
    match request.table.as_str() {
        "proposals" => restore_proposal(
            database,
            transaction,
            request.document,
            request.updated_at_ms,
        ),
        "actions" => restore_action(
            database,
            transaction,
            request.document,
            request.updated_at_ms,
        ),
        _ => Err(invalid_input("unknown optimizer migration table")),
    }
}

fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: GetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid optimizer get request"))?;
    match request.table.as_str() {
        "proposals" => Ok(read_proposal(database, transaction, &request.id)?
            .map(|proposal| serde_json::to_value(&proposal).unwrap())
            .unwrap_or(Value::Null)),
        "actions" => Ok(read_action(database, transaction, &request.id)?
            .map(|action| serde_json::to_value(&action).unwrap())
            .unwrap_or(Value::Null)),
        _ => Err(invalid_input("unknown optimizer migration table")),
    }
}

fn scan(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: ScanRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid optimizer scan request"))?;
    let namespace = match request.table.as_str() {
        "proposals" => OPTIMIZER_PROPOSAL_DOCUMENT_NAMESPACE,
        "actions" => OPTIMIZER_ACTION_DOCUMENT_NAMESPACE,
        _ => return Err(invalid_input("unknown optimizer migration table")),
    };
    let mut start = Vec::new();
    if let Some(cursor) = request.cursor.as_deref() {
        let encoded = cursor
            .strip_prefix("optimizer:")
            .ok_or_else(|| invalid_input("invalid optimizer catalog cursor"))?;
        if encoded.is_empty() || encoded.len() > 512 {
            return Err(invalid_input("invalid optimizer catalog cursor"));
        }
        start = encoded.as_bytes().to_vec();
        start.push(0);
    }
    let start = owner_key(transaction, namespace, &start)?;
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        b"",
    )?;
    let page = database.entity_scan(transaction, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, _) in &page {
        let id = std::str::from_utf8(physical.key_bytes())
            .map_err(|_| invalid_data("optimizer catalog key is malformed"))?;
        match request.table.as_str() {
            "proposals" => {
                if read_proposal(database, transaction, id)?.is_none() {
                    return Err(invalid_data("optimizer catalog target is missing"));
                }
            }
            _ => {
                if read_action(database, transaction, id)?.is_none() {
                    return Err(invalid_data("optimizer catalog target is missing"));
                }
            }
        }
        rows.push(json!({"id": id}));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        rows.last()
            .and_then(|row| row.get("id").and_then(Value::as_str))
            .map(|id| format!("optimizer:{id}"))
    } else {
        None
    };
    Ok(json!({"rows": rows, "cursor": cursor}))
}

pub(crate) fn execute(
    database: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    let mut transaction = database.begin(tenant, owner)?;
    match operation {
        "migration.optimizer.restore" => {
            let inserted = restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.optimizer.get" => get(database, &mut transaction, payload),
        "migration.optimizer.scan" => scan(database, &mut transaction, payload),
        _ => Err(invalid_input("unknown optimizer migration operation")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Vfs};
    use std::path::Path;
    use std::sync::Arc;

    fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        (vfs, database)
    }

    fn proposal(id: &str) -> Value {
        json!({
            "id": id,
            "created_at": "2026-08-01T00:00:00Z",
            "title": "t",
            "rationale": "r",
            "action_type": "cleanup",
            "action_args": "{}",
            "severity": "low",
            "confidence": 0.5,
            "evidence": "e",
            "status": "pending_review",
            "status_reason": "",
        })
    }

    fn action(id: &str, proposal_id: &str) -> Value {
        json!({
            "id": id,
            "proposal_id": proposal_id,
            "applied_at": "2026-08-02T00:00:00Z",
            "expires_at": "2026-09-02T00:00:00Z",
            "pre_metric": "{}",
            "outcome_metric": "",
            "outcome_recorded_at": "",
            "reverted_at": "",
            "revert_reason": "",
        })
    }

    #[test]
    fn restore_preserves_exact_state_across_reopen_and_is_idempotent() {
        let (vfs, mut database) = prepared();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.optimizer.restore",
                json!({"table": "proposals", "document": proposal("p-1"), "updated_at_ms": 424242})
            )
            .unwrap()["inserted"],
            true
        );
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.optimizer.restore",
                json!({"table": "actions", "document": action("a-1", "p-1"), "updated_at_ms": 424243})
            )
            .unwrap()["inserted"],
            true
        );
        drop(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.optimizer.restore",
                json!({"table": "proposals", "document": proposal("p-1"), "updated_at_ms": 424242})
            )
            .unwrap()["inserted"],
            false
        );
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.optimizer.get",
                json!({"table": "proposals", "id": "p-1"})
            )
            .unwrap(),
            proposal("p-1")
        );
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.optimizer.get",
                json!({"table": "actions", "id": "a-1"})
            )
            .unwrap(),
            action("a-1", "p-1")
        );
        let mut divergent = proposal("p-1");
        divergent["title"] = json!("other");
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.optimizer.restore",
            json!({"table": "proposals", "document": divergent, "updated_at_ms": 424242})
        )
        .is_err());
    }

    #[test]
    fn live_lists_see_restored_proposals_and_actions() {
        let (_vfs, mut database) = prepared();
        execute(
            &mut database,
            7,
            11,
            "migration.optimizer.restore",
            json!({"table": "proposals", "document": proposal("p-1"), "updated_at_ms": 424242}),
        )
        .unwrap();
        execute(
            &mut database,
            7,
            11,
            "migration.optimizer.restore",
            json!({"table": "actions", "document": action("a-1", "p-1"), "updated_at_ms": 424243}),
        )
        .unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        let listed = crate::optimizer::execute(
            &database,
            &mut transaction,
            &crate::optimizer::Request::ProposalList {
                status: String::new(),
                limit: 10,
            },
        )
        .unwrap()
        .unwrap();
        let listed: Value = serde_json::from_slice(&listed).unwrap();
        assert_eq!(listed.as_array().unwrap().len(), 1);
        let actions = crate::optimizer::execute(
            &database,
            &mut transaction,
            &crate::optimizer::Request::ActionList {
                include_reverted: false,
                limit: 10,
            },
        )
        .unwrap()
        .unwrap();
        let actions: Value = serde_json::from_slice(&actions).unwrap();
        assert_eq!(actions.as_array().unwrap().len(), 1);
        drop(transaction);
        assert_eq!(
            execute(
                &mut database,
                7,
                12,
                "migration.optimizer.scan",
                json!({"table": "proposals", "cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }

    #[test]
    fn action_without_proposal_fails_closed() {
        let (_vfs, mut database) = prepared();
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.optimizer.restore",
            json!({"table": "actions", "document": action("a-1", "missing"), "updated_at_ms": 1})
        )
        .is_err());
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.optimizer.scan",
                json!({"table": "actions", "cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
