//! Offline desktop egress-preference snapshot restoration for the exclusive
//! operator importer.
//!
//! The source keeps one tenant-scoped row per owner in
//! `storage_desktop_egress_preferences` (`agent_id`, `updated_at_ms`). The
//! native authority stores a single canonical preference document under the
//! fixed `desktop_egress_preferences` namespace. The live
//! `desktop.egress_agent.set` operation always stamps `updated_at_ms` with the
//! server clock, so it cannot preserve an exact source timestamp; this adapter
//! writes the canonical document with the source timestamp directly, then
//! verifies it through the same decode the live get path returns.
//! Insert-or-match keeps replays idempotent: an identical snapshot is
//! read-only, a divergent occupant fails closed. Entry points: the three
//! `migration.desktop.egress.*` operations dispatched from `migration_session`.

use serde::Deserialize;
use serde_json::{json, Value};
use std::io;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{DESKTOP_PREFERENCE_NAMESPACE, MAX_DESKTOP_AGENT_ID_CHARACTERS};

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn snapshot_conflict() -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, "desktop egress snapshot differs")
}

const PREFERENCE_KEY: &[u8] = b"preference";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RestoreRequest {
    agent_id: String,
    updated_at_ms: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GetRequest {}

fn preference_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        DESKTOP_PREFERENCE_NAMESPACE,
        PREFERENCE_KEY,
    )
}

fn parse_restore(payload: Value) -> io::Result<RestoreRequest> {
    let request: RestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid desktop egress restore request"))?;
    if request.agent_id.chars().count() > MAX_DESKTOP_AGENT_ID_CHARACTERS
        || request.updated_at_ms == 0
    {
        return Err(invalid_input("invalid desktop egress snapshot metadata"));
    }
    Ok(request)
}

/// The canonical native document; identical to what the live set path writes
/// for the same agent id, except the timestamp is the preserved source value.
fn native_document(request: &RestoreRequest) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "present": true,
        "agent_id": request.agent_id,
        "updated_at_ms": request.updated_at_ms,
    }))
    .expect("desktop egress document always encodes")
}

fn read_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Option<RestoreRequest>> {
    let Some(raw) = database.entity_get(transaction, &preference_key(transaction)?)? else {
        return Ok(None);
    };
    let value: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("desktop egress document is malformed"))?;
    let object = value
        .as_object()
        .filter(|object| object.len() == 3)
        .ok_or_else(|| invalid_data("desktop egress document is malformed"))?;
    if object.get("present").and_then(Value::as_bool) != Some(true) {
        return Err(invalid_data("desktop egress document is not present"));
    }
    let agent_id = object
        .get("agent_id")
        .and_then(Value::as_str)
        .filter(|agent| agent.chars().count() <= MAX_DESKTOP_AGENT_ID_CHARACTERS)
        .ok_or_else(|| invalid_data("desktop egress agent id is malformed"))?;
    let updated_at_ms = object
        .get("updated_at_ms")
        .and_then(Value::as_u64)
        .filter(|timestamp| *timestamp > 0)
        .ok_or_else(|| invalid_data("desktop egress timestamp is malformed"))?;
    Ok(Some(RestoreRequest {
        agent_id: agent_id.to_owned(),
        updated_at_ms,
    }))
}

fn restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request = parse_restore(payload)?;
    let key = preference_key(transaction)?;
    let document = native_document(&request);
    match database.entity_get(transaction, &key)? {
        None => {
            database.entity_put(transaction, key, document)?;
            Ok(true)
        }
        Some(existing) if existing == document => Ok(false),
        Some(_) => Err(snapshot_conflict()),
    }
}

fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let _: GetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid desktop egress get request"))?;
    match read_document(database, transaction)? {
        None => Ok(Value::Null),
        Some(request) => Ok(json!({
            "agent_id": request.agent_id,
            "updated_at_ms": request.updated_at_ms,
        })),
    }
}

fn scan(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let _: GetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid desktop egress scan request"))?;
    let key = preference_key(transaction)?;
    let present = database.entity_get(transaction, &key)?.is_some();
    let rows = if present {
        vec![json!({"owner_user_id": transaction.owner_user_id()})]
    } else {
        Vec::new()
    };
    // Single tenant-scoped record: the catalog never pages.
    Ok(json!({"rows": rows, "cursor": Value::Null}))
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
        "migration.desktop.egress.restore" => {
            let inserted = restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.desktop.egress.get" => get(database, &mut transaction, payload),
        "migration.desktop.egress.scan" => scan(database, &mut transaction, payload),
        _ => Err(invalid_input("unknown desktop egress migration operation")),
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

    fn snapshot() -> Value {
        json!({"agent_id": "egress-1", "updated_at_ms": 424242})
    }

    #[test]
    fn restore_preserves_exact_state_across_reopen_and_is_idempotent() {
        let (vfs, mut database) = prepared();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.desktop.egress.restore",
                snapshot()
            )
            .unwrap()["inserted"],
            true
        );
        drop(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        // An identical replay is read-only; a divergent snapshot fails closed.
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.desktop.egress.restore",
                snapshot()
            )
            .unwrap()["inserted"],
            false
        );
        let get = execute(
            &mut database,
            7,
            11,
            "migration.desktop.egress.get",
            json!({}),
        )
        .unwrap();
        assert_eq!(
            get,
            json!({"agent_id": "egress-1", "updated_at_ms": 424242})
        );
        let divergent = json!({"agent_id": "other", "updated_at_ms": 424242});
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.desktop.egress.restore",
            divergent
        )
        .is_err());
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.desktop.egress.get",
                json!({})
            )
            .unwrap(),
            json!({"agent_id": "egress-1", "updated_at_ms": 424242})
        );
    }

    #[test]
    fn scan_enumerates_only_the_owner_scoped_document() {
        let (_vfs, mut database) = prepared();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.desktop.egress.scan",
                json!({})
            )
            .unwrap()["rows"],
            json!([])
        );
        execute(
            &mut database,
            7,
            11,
            "migration.desktop.egress.restore",
            snapshot(),
        )
        .unwrap();
        let page = execute(
            &mut database,
            7,
            11,
            "migration.desktop.egress.scan",
            json!({}),
        )
        .unwrap();
        assert_eq!(page["rows"], json!([{"owner_user_id": 11}]));
        assert_eq!(page["cursor"], Value::Null);
        // A different owner sees an empty catalog.
        assert_eq!(
            execute(
                &mut database,
                7,
                12,
                "migration.desktop.egress.scan",
                json!({})
            )
            .unwrap()["rows"],
            json!([])
        );
    }

    #[test]
    fn invalid_metadata_and_frames_fail_before_any_write() {
        let (_vfs, mut database) = prepared();
        for payload in [
            json!({"agent_id": "x", "updated_at_ms": 0}),
            json!({"agent_id": "x"}),
            json!({"agent_id": "x", "updated_at_ms": 1, "extra": true}),
        ] {
            assert!(execute(
                &mut database,
                7,
                11,
                "migration.desktop.egress.restore",
                payload
            )
            .is_err());
        }
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.desktop.egress.scan",
                json!({})
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
