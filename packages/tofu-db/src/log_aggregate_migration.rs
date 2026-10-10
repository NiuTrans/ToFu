//! Offline log-aggregate snapshot restoration for the exclusive operator
//! importer.
//!
//! The source keeps one settled row per fingerprint in `log_aggregates`.
//! The live flush path merges counters and restamps `last_seen`/sample from
//! new events, so it cannot preserve the exact source row. This adapter
//! writes the exact document, advances the retained insertion sequence past
//! every imported row, and rebuilds the sweep index inside one authority
//! transaction. Insert-or-match keeps replays idempotent: an identical
//! snapshot is read-only, a divergent occupant fails closed. Get returns the
//! exact snapshot spelling; scan enumerates fingerprints independently of
//! the source. Entry points: the three `migration.log.aggregate.*`
//! operations dispatched from `migration_session`.

use serde::Deserialize;
use serde_json::{json, Value};
use std::io;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    LOG_AGGREGATE_DOCUMENT_NAMESPACE, LOG_AGGREGATE_SEQUENCE_NAMESPACE,
    LOG_AGGREGATE_SWEEP_INDEX_NAMESPACE, MAX_LOG_AGGREGATE_DOCUMENT_BYTES,
    MAX_LOG_AGGREGATE_FINGERPRINT_CHARACTERS, MAX_LOG_AGGREGATE_LEVEL_CHARACTERS,
    MAX_LOG_AGGREGATE_LOGGER_CHARACTERS, MAX_LOG_AGGREGATE_ROW_COUNT,
    MAX_LOG_AGGREGATE_SAMPLE_CHARACTERS, MAX_LOG_AGGREGATE_TEMPLATE_CHARACTERS,
    MIGRATION_SESSION_MAX_PAGE_ROWS,
};
use crate::versioned_document::{self, PutRequest};

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn snapshot_conflict() -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, "log aggregate snapshot differs")
}

const LOGICAL_NAMESPACE: &str = "log_aggregates";
const SEQUENCE_KEY: &[u8] = b"next";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RestoreRequest {
    fingerprint: String,
    level: String,
    logger: String,
    template: String,
    sample: String,
    count: u64,
    first_seen: u64,
    last_seen: u64,
    updated_at_ms: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GetRequest {
    fingerprint: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ScanRequest {
    cursor: Option<String>,
}

fn document_key(transaction: &AuthorityTransaction, fingerprint: &str) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        LOG_AGGREGATE_DOCUMENT_NAMESPACE,
        fingerprint.as_bytes(),
    )
}

fn sweep_index_key(
    transaction: &AuthorityTransaction,
    last_seen: u64,
    fingerprint: &str,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity(8 + fingerprint.len());
    raw.extend_from_slice(&last_seen.to_be_bytes());
    raw.extend_from_slice(fingerprint.as_bytes());
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        LOG_AGGREGATE_SWEEP_INDEX_NAMESPACE,
        &raw,
    )
}

fn sequence_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        LOG_AGGREGATE_SEQUENCE_NAMESPACE,
        SEQUENCE_KEY,
    )
}

fn read_sequence(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<u64> {
    match database.entity_get(transaction, &sequence_key(transaction)?)? {
        None => Ok(0),
        Some(raw) if raw.len() == 8 => Ok(u64::from_le_bytes(raw.try_into().unwrap())),
        Some(_) => Err(invalid_data("log-aggregate sequence is malformed")),
    }
}

fn snapshot_value(request: &RestoreRequest) -> Value {
    json!({
        "fingerprint": request.fingerprint,
        "level": request.level,
        "logger": request.logger,
        "template": request.template,
        "sample": request.sample,
        "count": request.count,
        "first_seen": request.first_seen,
        "last_seen": request.last_seen,
    })
}

fn validate_request(request: &RestoreRequest) -> bool {
    !request.fingerprint.is_empty()
        && request.fingerprint.chars().count() <= MAX_LOG_AGGREGATE_FINGERPRINT_CHARACTERS
        && !request.level.is_empty()
        && request.level.chars().count() <= MAX_LOG_AGGREGATE_LEVEL_CHARACTERS
        && request.logger.chars().count() <= MAX_LOG_AGGREGATE_LOGGER_CHARACTERS
        && request.template.chars().count() <= MAX_LOG_AGGREGATE_TEMPLATE_CHARACTERS
        && request.sample.chars().count() <= MAX_LOG_AGGREGATE_SAMPLE_CHARACTERS
        && (1..=MAX_LOG_AGGREGATE_ROW_COUNT).contains(&request.count)
        && request.first_seen > 0
        && request.last_seen >= request.first_seen
        && request.updated_at_ms > 0
}

fn read_snapshot(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    fingerprint: &str,
) -> io::Result<Option<Value>> {
    let key = document_key(transaction, fingerprint)?;
    let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &key,
        LOGICAL_NAMESPACE,
        fingerprint,
        transaction.owner_user_id(),
        MAX_LOG_AGGREGATE_DOCUMENT_BYTES,
    )?
    else {
        return Ok(None);
    };
    let document: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("log-aggregate document is malformed"))?;
    if document.get("fingerprint").and_then(Value::as_str) != Some(fingerprint) {
        return Err(invalid_data("log-aggregate document identity differs"));
    }
    for field in ["level", "logger", "template", "sample"] {
        if !document.get(field).is_some_and(Value::is_string) {
            return Err(invalid_data("log-aggregate document is malformed"));
        }
    }
    for field in ["count", "first_seen", "last_seen", "seq"] {
        if !document.get(field).is_some_and(Value::is_u64) {
            return Err(invalid_data("log-aggregate document is malformed"));
        }
    }
    let snapshot = read_snapshot_envelope(database, transaction, &key, fingerprint)?;
    Ok(Some(json!({
        "snapshot": {
            "fingerprint": fingerprint,
            "level": document["level"],
            "logger": document["logger"],
            "template": document["template"],
            "sample": document["sample"],
            "count": document["count"],
            "first_seen": document["first_seen"],
            "last_seen": document["last_seen"],
        },
        "updated_at_ms": snapshot,
    })))
}

fn read_snapshot_envelope(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
    fingerprint: &str,
) -> io::Result<u64> {
    let snapshot = versioned_document::read_snapshot(
        database,
        transaction,
        key,
        LOGICAL_NAMESPACE,
        fingerprint,
    )?
    .ok_or_else(|| invalid_data("log-aggregate document is missing"))?;
    Ok(snapshot.updated_at_ms)
}

fn restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: RestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid log aggregate restore request"))?;
    if !validate_request(&request) {
        return Err(invalid_input("invalid log aggregate snapshot"));
    }
    if let Some(existing) = read_snapshot(database, transaction, &request.fingerprint)? {
        if existing["snapshot"] == snapshot_value(&request) {
            return Ok(false);
        }
        return Err(snapshot_conflict());
    }
    // Advance the retained insertion sequence so later live flushes keep a
    // deterministic order ahead of every imported row.
    let next = read_sequence(database, transaction)?
        .checked_add(1)
        .ok_or_else(|| invalid_data("log-aggregate sequence overflow"))?;
    database.entity_put(
        transaction,
        sequence_key(transaction)?,
        next.to_le_bytes().to_vec(),
    )?;
    let document = json!({
        "fingerprint": request.fingerprint,
        "level": request.level,
        "logger": request.logger,
        "template": request.template,
        "sample": request.sample,
        "count": request.count,
        "first_seen": request.first_seen,
        "last_seen": request.last_seen,
        "seq": next - 1,
    });
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: document_key(transaction, &request.fingerprint)?,
            namespace: LOGICAL_NAMESPACE.to_owned(),
            logical_key: request.fingerprint.clone(),
            value_json: serde_json::to_vec(&document)
                .map_err(|_| invalid_data("log-aggregate document cannot be encoded"))?,
            expected_version: None,
            updated_at_ms: request.updated_at_ms,
        },
        transaction.owner_user_id(),
        MAX_LOG_AGGREGATE_DOCUMENT_BYTES,
    )?;
    database.entity_put(
        transaction,
        sweep_index_key(transaction, request.last_seen, &request.fingerprint)?,
        vec![1],
    )?;
    let written = read_snapshot(database, transaction, &request.fingerprint)?
        .ok_or_else(|| invalid_data("log aggregate snapshot is missing"))?;
    if written["snapshot"] != snapshot_value(&request) {
        return Err(invalid_data("log aggregate snapshot readback differs"));
    }
    Ok(true)
}

fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: GetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid log aggregate get request"))?;
    match read_snapshot(database, transaction, &request.fingerprint)? {
        None => Ok(Value::Null),
        Some(found) => {
            let mut snapshot = found["snapshot"].clone();
            snapshot
                .as_object_mut()
                .expect("snapshot is an object")
                .insert("updated_at_ms".to_owned(), found["updated_at_ms"].clone());
            Ok(snapshot)
        }
    }
}

fn scan(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: ScanRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid log aggregate scan request"))?;
    let mut start = Vec::new();
    if let Some(cursor) = request.cursor.as_deref() {
        let encoded = cursor
            .strip_prefix("log_aggregates:")
            .ok_or_else(|| invalid_input("invalid log aggregate catalog cursor"))?;
        if encoded.is_empty() || encoded.len() > 1024 {
            return Err(invalid_input("invalid log aggregate catalog cursor"));
        }
        start = encoded.as_bytes().to_vec();
        start.push(0);
    }
    let start = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        LOG_AGGREGATE_DOCUMENT_NAMESPACE,
        &start,
    )?;
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        LOG_AGGREGATE_DOCUMENT_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(transaction, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, _) in &page {
        let fingerprint = std::str::from_utf8(physical.key_bytes())
            .map_err(|_| invalid_data("log-aggregate catalog key is malformed"))?;
        if read_snapshot(database, transaction, fingerprint)?.is_none() {
            return Err(invalid_data("log-aggregate catalog target is missing"));
        }
        rows.push(json!({"fingerprint": fingerprint}));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        rows.last()
            .and_then(|row| row.get("fingerprint").and_then(Value::as_str))
            .map(|fingerprint| format!("log_aggregates:{fingerprint}"))
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
        "migration.log.aggregate.restore" => {
            let inserted = restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.log.aggregate.get" => get(database, &mut transaction, payload),
        "migration.log.aggregate.scan" => scan(database, &mut transaction, payload),
        _ => Err(invalid_input("unknown log aggregate migration operation")),
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

    fn snapshot(fingerprint: &str) -> Value {
        json!({
            "fingerprint": fingerprint,
            "level": "ERROR",
            "logger": "app",
            "template": "disk full: {}",
            "sample": "disk full: /dev/sda",
            "count": 5,
            "first_seen": 1000,
            "last_seen": 2000,
            "updated_at_ms": 424242,
        })
    }

    #[test]
    fn restore_preserves_exact_state_across_reopen_and_is_idempotent() {
        let (vfs, mut database) = prepared();
        let snapshot = snapshot("fp-1");
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.log.aggregate.restore",
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
                "migration.log.aggregate.restore",
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
                "migration.log.aggregate.get",
                json!({"fingerprint": "fp-1"})
            )
            .unwrap(),
            json!({
                "fingerprint": "fp-1",
                "level": "ERROR",
                "logger": "app",
                "template": "disk full: {}",
                "sample": "disk full: /dev/sda",
                "count": 5,
                "first_seen": 1000,
                "last_seen": 2000,
                "updated_at_ms": 424242,
            })
        );
        let mut divergent = snapshot.clone();
        divergent["count"] = json!(6);
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.log.aggregate.restore",
            divergent
        )
        .is_err());
    }

    #[test]
    fn live_query_and_later_flush_share_the_sequence() {
        let (_vfs, mut database) = prepared();
        execute(
            &mut database,
            7,
            11,
            "migration.log.aggregate.restore",
            snapshot("fp-1"),
        )
        .unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        let queried = crate::log_aggregate::query(
            &database,
            &mut transaction,
            "ERROR",
            "",
            crate::log_aggregate::QuerySort::Count,
            10,
        )
        .unwrap();
        let queried: Value = serde_json::from_slice(&queried).unwrap();
        assert_eq!(queried["total_rows"], 1);
        // A later live flush merges against the imported row.
        crate::log_aggregate::flush(
            &database,
            &mut transaction,
            &[crate::log_aggregate::FlushRow {
                fingerprint: "fp-1".into(),
                level: "ERROR".into(),
                logger: "app".into(),
                template: "disk full: {}".into(),
                sample: "disk full: /dev/sdb".into(),
                count: 2,
                first_seen: 1000,
                last_seen: 3000,
            }],
            None,
            424243,
        )
        .unwrap();
        database.commit(transaction).unwrap();
        let got = execute(
            &mut database,
            7,
            11,
            "migration.log.aggregate.get",
            json!({"fingerprint": "fp-1"}),
        )
        .unwrap();
        assert_eq!(got["count"], 7);
        assert_eq!(got["last_seen"], 3000);
        assert_eq!(got["updated_at_ms"], 424243);
    }

    #[test]
    fn invalid_snapshots_fail_before_any_write() {
        let (_vfs, mut database) = prepared();
        for payload in [
            json!({"fingerprint": "", "level": "ERROR", "logger": "", "template": "",
                   "sample": "", "count": 1, "first_seen": 1, "last_seen": 1,
                   "updated_at_ms": 1}),
            json!({"fingerprint": "fp", "level": "ERROR", "logger": "", "template": "",
                   "sample": "", "count": 0, "first_seen": 1, "last_seen": 1,
                   "updated_at_ms": 1}),
        ] {
            assert!(execute(
                &mut database,
                7,
                11,
                "migration.log.aggregate.restore",
                payload
            )
            .is_err());
        }
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.log.aggregate.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
