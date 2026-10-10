//! Offline rate-limit event snapshot restoration for the exclusive operator
//! importer.
//!
//! The source keeps one row per event in `storage_rate_limit_events`
//! (`event_id`, `endpoint`, `client_key`, `occurred_at_ms`, `expires_at_ms`).
//! The native authority stores a fixed nine-node radix-256 timestamp counter
//! path per bucket, a global expiry index, and a monotonic bucket clock. The
//! live `rate_limit.record_and_check` operation always stamps `occurred_at_ms`
//! and `expires_at_ms` from the server clock, so a replay through it cannot
//! preserve the exact source timestamps and would not be insert-or-match
//! idempotent. This adapter rebuilds the bucket, event, expiry index row, and
//! the full counter path in the same authority transaction with the exact
//! source timestamps, advancing the bucket's monotonic clock to the greatest
//! restored timestamp. Insert-or-match fails closed on a divergent occupant,
//! get reconstructs the exact source snapshot, and scan enumerates the
//! owner-scoped event catalog independently, cross-checking it against the
//! radix counter totals. Entry points: the three
//! `migration.rate.limit.event.*` operations dispatched from
//! `migration_session`.

use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::io;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_RATE_LIMIT_CLIENT_KEY_CHARACTERS, MAX_RATE_LIMIT_ENDPOINT_CHARACTERS,
    MAX_RATE_LIMIT_EVENT_ID_CHARACTERS, MIGRATION_SESSION_MAX_PAGE_ROWS,
    RATE_LIMIT_EVENT_NAMESPACE,
};
use crate::rate_limit::{
    self, add_path_deltas, adjust_count, bucket_digest, bucket_key, decode_bucket, decode_event,
    encode_bucket, encode_event, event_key, expiry_key, Bucket,
};

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn snapshot_conflict() -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, "rate limit snapshot differs")
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RestoreRequest {
    event_id: String,
    endpoint: String,
    client_key: String,
    occurred_at_ms: u64,
    expires_at_ms: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GetRequest {
    event_id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ScanRequest {
    cursor: Option<String>,
}

fn valid_metadata(request: &RestoreRequest) -> bool {
    !request.endpoint.is_empty()
        && request.endpoint.chars().count() <= MAX_RATE_LIMIT_ENDPOINT_CHARACTERS
        && !request.client_key.is_empty()
        && request.client_key.chars().count() <= MAX_RATE_LIMIT_CLIENT_KEY_CHARACTERS
        && !request.event_id.is_empty()
        && request.event_id.chars().count() <= MAX_RATE_LIMIT_EVENT_ID_CHARACTERS
        && request.occurred_at_ms > 0
        && request.expires_at_ms > request.occurred_at_ms
}

fn parse_restore(payload: Value) -> io::Result<RestoreRequest> {
    let request: RestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid rate limit restore request"))?;
    if !valid_metadata(&request) {
        return Err(invalid_input("invalid rate limit snapshot metadata"));
    }
    Ok(request)
}

fn read_event(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    event_id: &str,
) -> io::Result<Option<(Vec<u8>, [u8; 32], u64, u64)>> {
    let key = event_key(transaction, event_id)?;
    let Some(raw) = database.entity_get(transaction, &key)? else {
        return Ok(None);
    };
    let (digest, occurred_at_ms, expires_at_ms) = decode_event(&raw)?;
    Ok(Some((raw, digest, occurred_at_ms, expires_at_ms)))
}

fn restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request = parse_restore(payload)?;
    let digest = bucket_digest(&request.endpoint, &request.client_key);
    let encoded = encode_event(&digest, request.occurred_at_ms, request.expires_at_ms);
    // Insert-or-match against the exact stored event encoding; a divergent
    // occupant fails closed instead of overwriting.
    if let Some((existing, _, _, _)) = read_event(database, transaction, &request.event_id)? {
        if existing != encoded {
            return Err(snapshot_conflict());
        }
        return Ok(false);
    }
    // Establish or advance the bucket's monotonic clock. A restored event
    // never moves the clock backward, preserving the native invariant.
    let bucket_entity = bucket_key(transaction, &digest)?;
    let bucket = match database.entity_get(transaction, &bucket_entity)? {
        Some(raw) => {
            let mut bucket = decode_bucket(&raw)?;
            if bucket.endpoint != request.endpoint || bucket.client_key != request.client_key {
                return Err(invalid_data("rate limit bucket digest collision"));
            }
            bucket.last_now_ms = bucket.last_now_ms.max(request.occurred_at_ms);
            bucket
        }
        None => Bucket {
            endpoint: request.endpoint.clone(),
            client_key: request.client_key.clone(),
            last_now_ms: request.occurred_at_ms,
        },
    };
    database.entity_put(transaction, bucket_entity, encode_bucket(&bucket)?)?;
    database.entity_put(
        transaction,
        event_key(transaction, &request.event_id)?,
        encoded.clone(),
    )?;
    database.entity_put(
        transaction,
        expiry_key(transaction, request.expires_at_ms, &request.event_id)?,
        encoded,
    )?;
    // Rebuild the radix counter path for this event in the same transaction.
    let mut deltas = BTreeMap::new();
    add_path_deltas(&mut deltas, digest, request.occurred_at_ms, 1)?;
    for ((path_digest, prefix), delta) in deltas {
        adjust_count(database, transaction, &path_digest, &prefix, delta)?;
    }
    // Read back through the decode path before publication.
    let Some((stored, _, _, _)) = read_event(database, transaction, &request.event_id)? else {
        return Err(invalid_data("rate limit snapshot event is missing"));
    };
    if stored != encode_event(&digest, request.occurred_at_ms, request.expires_at_ms) {
        return Err(invalid_data("rate limit snapshot readback differs"));
    }
    Ok(true)
}

fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: GetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid rate limit get request"))?;
    let Some((_, digest, occurred_at_ms, expires_at_ms)) =
        read_event(database, transaction, &request.event_id)?
    else {
        return Ok(Value::Null);
    };
    // Resolve the bucket so the exact endpoint/client key round-trips.
    let bucket_entity = bucket_key(transaction, &digest)?;
    let raw = database
        .entity_get(transaction, &bucket_entity)?
        .ok_or_else(|| invalid_data("rate limit event references a missing bucket"))?;
    let bucket = decode_bucket(&raw)?;
    Ok(json!({
        "event_id": request.event_id,
        "endpoint": bucket.endpoint,
        "client_key": bucket.client_key,
        "occurred_at_ms": occurred_at_ms,
        "expires_at_ms": expires_at_ms,
    }))
}

fn cursor_start(cursor: Option<&str>) -> io::Result<Vec<u8>> {
    let Some(cursor) = cursor else {
        return Ok(Vec::new());
    };
    let encoded = cursor
        .strip_prefix("rate_limit:")
        .ok_or_else(|| invalid_input("invalid rate limit catalog cursor"))?;
    let mut raw = encoded.as_bytes().to_vec();
    if raw.is_empty() {
        return Err(invalid_input("invalid rate limit catalog cursor"));
    }
    raw.push(0);
    Ok(raw)
}

fn scan(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: ScanRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid rate limit scan request"))?;
    let suffix = cursor_start(request.cursor.as_deref())?;
    let start = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        RATE_LIMIT_EVENT_NAMESPACE,
        &suffix,
    )?;
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        RATE_LIMIT_EVENT_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(transaction, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, _) in &page {
        let event_id = std::str::from_utf8(physical.key_bytes())
            .map_err(|_| invalid_data("rate limit catalog key is malformed"))?;
        rows.push(json!({"event_id": event_id}));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let last = rows
            .last()
            .and_then(|row| row.get("event_id").and_then(Value::as_str))
            .ok_or_else(|| invalid_data("rate limit catalog page is malformed"))?;
        Some(format!("rate_limit:{last}"))
    } else {
        None
    };
    // On a full pass the enumerated catalog must agree with the radix
    // counter roots: every bucket's total counter equals its event count.
    if cursor.is_none() {
        // Events in one bucket share a single radix root counter, so sum each
        // distinct bucket's root exactly once and compare to the event count.
        let mut digests = std::collections::BTreeSet::new();
        for row in &rows {
            let event_id = row["event_id"].as_str().unwrap();
            let Some((_, digest, _, _)) = read_event(database, transaction, event_id)? else {
                return Err(invalid_data("rate limit catalog event is missing"));
            };
            digests.insert(digest);
        }
        let mut total = 0_u64;
        for digest in digests {
            total = total
                .checked_add(rate_limit::read_count(database, transaction, &digest, b"")?)
                .ok_or_else(|| invalid_data("rate limit counter total overflow"))?;
        }
        if total != rows.len() as u64 {
            return Err(invalid_data("rate limit counter total and catalog differ"));
        }
    }
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
        "migration.rate.limit.event.restore" => {
            let inserted = restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.rate.limit.event.get" => get(database, &mut transaction, payload),
        "migration.rate.limit.event.scan" => scan(database, &mut transaction, payload),
        _ => Err(invalid_input("unknown rate limit migration operation")),
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

    fn snapshot(event_id: &str, occurred: u64, expires: u64) -> Value {
        json!({
            "event_id": event_id,
            "endpoint": "/v1/chat",
            "client_key": "198.51.100.8",
            "occurred_at_ms": occurred,
            "expires_at_ms": expires,
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
                "migration.rate.limit.event.restore",
                snapshot("event-1", 1_000, 61_000)
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
                "migration.rate.limit.event.restore",
                snapshot("event-1", 1_000, 61_000)
            )
            .unwrap()["inserted"],
            false
        );
        let get = execute(
            &mut database,
            7,
            11,
            "migration.rate.limit.event.get",
            json!({"event_id": "event-1"}),
        )
        .unwrap();
        assert_eq!(get["endpoint"], "/v1/chat");
        assert_eq!(get["client_key"], "198.51.100.8");
        assert_eq!(get["occurred_at_ms"], 1_000);
        assert_eq!(get["expires_at_ms"], 61_000);
        // A divergent snapshot fails closed.
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.rate.limit.event.restore",
            snapshot("event-1", 2_000, 62_000)
        )
        .is_err());
    }

    #[test]
    fn radix_counter_and_expiry_index_rebuilt_atomically() {
        let (_vfs, mut database) = prepared();
        for (id, occurred) in [
            ("event-1", 1_000_u64),
            ("event-2", 2_000),
            ("event-3", 3_000),
        ] {
            execute(
                &mut database,
                7,
                11,
                "migration.rate.limit.event.restore",
                snapshot(id, occurred, occurred + 60_000),
            )
            .unwrap();
        }
        // The radix root counter for the bucket equals the event count.
        let mut transaction = database.begin(7, 11).unwrap();
        let digest = bucket_digest("/v1/chat", "198.51.100.8");
        assert_eq!(
            crate::rate_limit::read_count(&database, &mut transaction, &digest, b"").unwrap(),
            3
        );
        // The expiry index row exists and decodes to the same event encoding.
        let expiry = expiry_key(&transaction, 61_000, "event-1").unwrap();
        let stored = database
            .entity_get(&mut transaction, &expiry)
            .unwrap()
            .unwrap();
        assert_eq!(stored, encode_event(&digest, 1_000, 61_000));
        // The bucket monotonic clock advanced to the greatest timestamp.
        let bucket_entity = bucket_key(&transaction, &digest).unwrap();
        let bucket_raw = database
            .entity_get(&mut transaction, &bucket_entity)
            .unwrap()
            .unwrap();
        assert_eq!(decode_bucket(&bucket_raw).unwrap().last_now_ms, 3_000);
    }

    #[test]
    fn scan_enumerates_events_and_agrees_with_counter_total() {
        let (_vfs, mut database) = prepared();
        for (id, occurred) in [
            ("event-1", 1_000_u64),
            ("event-2", 2_000),
            ("event-3", 3_000),
        ] {
            execute(
                &mut database,
                7,
                11,
                "migration.rate.limit.event.restore",
                snapshot(id, occurred, occurred + 60_000),
            )
            .unwrap();
        }
        let page = execute(
            &mut database,
            7,
            11,
            "migration.rate.limit.event.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(page["rows"].as_array().unwrap().len(), 3);
        assert_eq!(page["cursor"], Value::Null);
        // A different owner sees an empty catalog.
        assert_eq!(
            execute(
                &mut database,
                7,
                12,
                "migration.rate.limit.event.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }

    #[test]
    fn invalid_metadata_fails_before_any_write() {
        let (_vfs, mut database) = prepared();
        for payload in [
            json!({"event_id":"e","endpoint":"","client_key":"k","occurred_at_ms":1,"expires_at_ms":2}),
            json!({"event_id":"e","endpoint":"p","client_key":"k","occurred_at_ms":0,"expires_at_ms":2}),
            json!({"event_id":"e","endpoint":"p","client_key":"k","occurred_at_ms":5,"expires_at_ms":5}),
            json!({"event_id":"e","endpoint":"p","client_key":"k","occurred_at_ms":1,"expires_at_ms":2,"extra":0}),
        ] {
            assert!(execute(
                &mut database,
                7,
                11,
                "migration.rate.limit.event.restore",
                payload
            )
            .is_err());
        }
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.rate.limit.event.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
