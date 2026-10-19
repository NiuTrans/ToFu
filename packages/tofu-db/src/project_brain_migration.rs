//! Offline Project Brain project-projection snapshot restoration for the
//! exclusive operator importer.
//!
//! The source keeps one settled row per (owner, project_key) with the exact
//! `projection_json`, `head_sequence`, `checkpoint_sequence` and
//! `updated_at_ms`. The live command path rebuilds the projection from event
//! replay and restamps `updated_at_ms` with the server clock, so it cannot
//! preserve the exact source projection. This adapter restores the exact
//! projection bytes and rebuilds the updated-desc index inside one authority
//! transaction; the projection must already agree with the declared head and
//! checkpoint sequences. Insert-or-match keeps replays idempotent: an
//! identical snapshot is read-only, a divergent occupant fails closed. Get
//! returns the exact snapshot spelling; scan enumerates project keys
//! owner-scoped and independent of the source. Entry points: the three
//! `migration.project.brain.project.*` operations dispatched from
//! `migration_session`.

use serde::Deserialize;
use serde_json::{json, Value};
use std::io;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_PROJECT_BRAIN_PROJECT_KEY_CHARACTERS,
    MIGRATION_SESSION_MAX_PAGE_ROWS, PROJECT_BRAIN_DOCUMENT_NAMESPACE,
    PROJECT_BRAIN_UPDATED_INDEX_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn snapshot_conflict() -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, "project brain snapshot differs")
}

const LOGICAL_NAMESPACE: &str = "project_brain_projects";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RestoreRequest {
    project_key: String,
    head_sequence: u64,
    checkpoint_sequence: u64,
    projection_json: String,
    updated_at_ms: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GetRequest {
    project_key: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ScanRequest {
    cursor: Option<String>,
}

fn project_digest(project_key: &str) -> [u8; 32] {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(b"tofu-db:project-brain:v1\0");
    hasher.update(project_key.as_bytes());
    hasher.finalize().into()
}

fn document_key(transaction: &AuthorityTransaction, project_key: &str) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PROJECT_BRAIN_DOCUMENT_NAMESPACE,
        &project_digest(project_key),
    )
}

fn updated_index_key(
    transaction: &AuthorityTransaction,
    project_key: &str,
    updated_at: u64,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity(40);
    raw.extend_from_slice(&(u64::MAX - updated_at).to_be_bytes());
    raw.extend_from_slice(&project_digest(project_key));
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PROJECT_BRAIN_UPDATED_INDEX_NAMESPACE,
        &raw,
    )
}

fn snapshot_value(request: &RestoreRequest) -> Value {
    json!({
        "project_key": request.project_key,
        "head_sequence": request.head_sequence,
        "checkpoint_sequence": request.checkpoint_sequence,
        "projection_json": request.projection_json,
        "updated_at_ms": request.updated_at_ms,
    })
}

fn validate_request(request: &RestoreRequest) -> io::Result<()> {
    if request.project_key.is_empty()
        || request.project_key.chars().count() > MAX_PROJECT_BRAIN_PROJECT_KEY_CHARACTERS
        || request.updated_at_ms == 0
    {
        return Err(invalid_input("invalid project brain snapshot metadata"));
    }
    let projection: Value = serde_json::from_str(&request.projection_json)
        .map_err(|_| invalid_input("project brain projection is not valid JSON"))?;
    let object = projection
        .as_object()
        .ok_or_else(|| invalid_input("project brain projection is not an object"))?;
    if object.get("version").and_then(Value::as_u64) != Some(1)
        || object.get("ownerUserId").and_then(Value::as_u64).is_none()
        || object.get("projectKey").and_then(Value::as_str) != Some(request.project_key.as_str())
        || object.get("headSequence").and_then(Value::as_u64) != Some(request.head_sequence)
        || object.get("checkpointSequence").and_then(Value::as_u64)
            != Some(request.checkpoint_sequence)
    {
        return Err(invalid_input(
            "project brain projection disagrees with its snapshot",
        ));
    }
    Ok(())
}

fn read_snapshot(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    project_key: &str,
) -> io::Result<Option<RestoreRequest>> {
    let key = document_key(transaction, project_key)?;
    let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &key,
        LOGICAL_NAMESPACE,
        project_key,
        transaction.owner_user_id(),
        usize::MAX,
    )?
    else {
        return Ok(None);
    };
    let value: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("project brain document is malformed"))?;
    let mut object = value
        .as_object()
        .cloned()
        .ok_or_else(|| invalid_data("project brain document is malformed"))?;
    let updated_at_ms = object
        .remove("_updatedAt")
        .and_then(|value| value.as_u64())
        .ok_or_else(|| invalid_data("project brain document is malformed"))?;
    if object.get("ownerUserId").and_then(Value::as_u64) != Some(transaction.owner_user_id())
        || object.get("projectKey").and_then(Value::as_str) != Some(project_key)
    {
        return Err(invalid_data("project brain projection ownership mismatch"));
    }
    let head_sequence = object
        .get("headSequence")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("project brain document is malformed"))?;
    let checkpoint_sequence = object
        .get("checkpointSequence")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("project brain document is malformed"))?;
    let projection_json = serde_json::to_string(&Value::Object(object))
        .map_err(|_| invalid_data("project brain document cannot be encoded"))?;
    Ok(Some(RestoreRequest {
        project_key: project_key.to_owned(),
        head_sequence,
        checkpoint_sequence,
        projection_json,
        updated_at_ms,
    }))
}

fn restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: RestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid project brain restore request"))?;
    validate_request(&request)?;
    if let Some(existing) = read_snapshot(database, transaction, &request.project_key)? {
        if snapshot_value(&existing) == snapshot_value(&request) {
            return Ok(false);
        }
        return Err(snapshot_conflict());
    }
    let projection: Value = serde_json::from_str(&request.projection_json)
        .map_err(|_| invalid_input("project brain projection is not valid JSON"))?;
    let mut stored = projection
        .as_object()
        .cloned()
        .ok_or_else(|| invalid_input("project brain projection is not an object"))?;
    stored.insert("_updatedAt".to_owned(), Value::from(request.updated_at_ms));
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: document_key(transaction, &request.project_key)?,
            namespace: LOGICAL_NAMESPACE.to_owned(),
            logical_key: request.project_key.clone(),
            value_json: serde_json::to_vec(&Value::Object(stored))
                .map_err(|_| invalid_input("project brain projection cannot be encoded"))?,
            expected_version: None,
            updated_at_ms: request.updated_at_ms.max(1),
        },
        transaction.owner_user_id(),
        usize::MAX,
    )?;
    database.entity_put(
        transaction,
        updated_index_key(transaction, &request.project_key, request.updated_at_ms)?,
        request.project_key.as_bytes().to_vec(),
    )?;
    let written = read_snapshot(database, transaction, &request.project_key)?
        .ok_or_else(|| invalid_data("project brain snapshot is missing"))?;
    if snapshot_value(&written) != snapshot_value(&request) {
        return Err(invalid_data("project brain snapshot readback differs"));
    }
    Ok(true)
}

fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: GetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid project brain get request"))?;
    match read_snapshot(database, transaction, &request.project_key)? {
        None => Ok(Value::Null),
        Some(snapshot) => Ok(snapshot_value(&snapshot)),
    }
}

/// The versioned-document envelope stores the logical key inside its binary
/// header; extract it without decoding the value payload.
fn versioned_logical_key(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    physical: &EntityKey,
) -> io::Result<Option<String>> {
    let Some(raw) = database.entity_get(transaction, physical)? else {
        return Ok(None);
    };
    // Binary envelope: 8-byte magic, 4-byte format, 8-byte document version,
    // 8-byte updated_at_ms, u16 namespace length, u16 key length, 1-byte
    // storage kind, 3 reserved bytes, 8-byte logical length.
    if raw.len() < 44 || &raw[..8] != b"TDBDOC01" {
        return Err(invalid_data("project brain catalog document is malformed"));
    }
    let namespace_length = u16::from_le_bytes([raw[28], raw[29]]) as usize;
    let key_length = u16::from_le_bytes([raw[30], raw[31]]) as usize;
    let start = 44usize
        .checked_add(namespace_length)
        .ok_or_else(|| invalid_data("project brain catalog document is malformed"))?;
    let key_bytes = raw
        .get(start..start + key_length)
        .ok_or_else(|| invalid_data("project brain catalog document is malformed"))?;
    let key = std::str::from_utf8(key_bytes)
        .map_err(|_| invalid_data("project brain catalog document is malformed"))?;
    Ok(Some(key.to_owned()))
}

fn scan(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: ScanRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid project brain scan request"))?;
    let mut start = Vec::new();
    if let Some(cursor) = request.cursor.as_deref() {
        let encoded = cursor
            .strip_prefix("project_brain:")
            .ok_or_else(|| invalid_input("invalid project brain catalog cursor"))?;
        if encoded.len() != 64 || !encoded.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(invalid_input("invalid project brain catalog cursor"));
        }
        start = encoded
            .as_bytes()
            .as_chunks::<2>()
            .0
            .iter()
            .map(|pair| {
                u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16)
                    .map_err(|_| invalid_input("invalid project brain catalog cursor"))
            })
            .collect::<io::Result<Vec<u8>>>()?;
        start.push(0);
    }
    let start = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PROJECT_BRAIN_DOCUMENT_NAMESPACE,
        &start,
    )?;
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PROJECT_BRAIN_DOCUMENT_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(transaction, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, _) in &page {
        // The physical key is the project digest; recover the project key
        // from the versioned document via the envelope's logical key.
        let Some(project_key) = versioned_logical_key(database, transaction, physical)? else {
            return Err(invalid_data("project brain catalog target is missing"));
        };
        let snapshot = read_snapshot(database, transaction, &project_key)?
            .ok_or_else(|| invalid_data("project brain catalog target is missing"))?;
        if &document_key(transaction, &snapshot.project_key)? != physical {
            return Err(invalid_data("project brain catalog identity differs"));
        }
        rows.push(json!({"project_key": snapshot.project_key}));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        Some(format!(
            "project_brain:{}",
            page.last()
                .expect("a full page has a last row")
                .0
                .key_bytes()
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>()
        ))
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
        "migration.project.brain.project.restore" => {
            let inserted = restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.project.brain.project.get" => get(database, &mut transaction, payload),
        "migration.project.brain.project.scan" => scan(database, &mut transaction, payload),
        _ => Err(invalid_input("unknown project brain migration operation")),
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

    fn snapshot(project_key: &str) -> Value {
        let projection = json!({
            "version": 1,
            "ownerUserId": 11,
            "projectKey": project_key,
            "headSequence": 42,
            "checkpointSequence": 7,
            "narratives": [],
        });
        json!({
            "project_key": project_key,
            "head_sequence": 42,
            "checkpoint_sequence": 7,
            "projection_json": projection.to_string(),
            "updated_at_ms": 424242,
        })
    }

    #[test]
    fn restore_preserves_exact_projection_across_reopen_and_is_idempotent() {
        let (vfs, mut database) = prepared();
        let snapshot = snapshot("proj-1");
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.project.brain.project.restore",
                snapshot.clone()
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
                "migration.project.brain.project.restore",
                snapshot.clone()
            )
            .unwrap()["inserted"],
            false
        );
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.project.brain.project.get",
                json!({"project_key": "proj-1"})
            )
            .unwrap(),
            snapshot
        );
        let mut divergent = snapshot.clone();
        divergent["head_sequence"] = json!(43);
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.project.brain.project.restore",
            divergent
        )
        .is_err());
    }

    #[test]
    fn scan_is_owner_scoped() {
        let (_vfs, mut database) = prepared();
        execute(
            &mut database,
            7,
            11,
            "migration.project.brain.project.restore",
            snapshot("proj-1"),
        )
        .unwrap();
        let page = execute(
            &mut database,
            7,
            11,
            "migration.project.brain.project.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(page["rows"], json!([{"project_key": "proj-1"}]));
        assert_eq!(
            execute(
                &mut database,
                7,
                12,
                "migration.project.brain.project.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }

    #[test]
    fn invalid_snapshots_fail_before_any_write() {
        let (_vfs, mut database) = prepared();
        let mut bad = snapshot("proj-1");
        bad["projection_json"] = json!("{\"version\":1}");
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.project.brain.project.restore",
            bad
        )
        .is_err());
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.project.brain.project.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
