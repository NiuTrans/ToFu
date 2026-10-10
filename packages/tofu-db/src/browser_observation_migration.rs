//! Offline browser site-observation snapshot restoration for the exclusive
//! operator importer.
//!
//! The source keeps one settled observation document per
//! (owner, origin, route_family, operation) plus one row per owner in
//! `storage_browser_site_observation_owners`. The live record path derives
//! confidence/counters from observation events and restamps every timestamp,
//! so it cannot preserve an exact source document. This adapter restores the
//! exact document, its versioned-document version and timestamp, the LRU
//! index row and the owner touch row inside one authority transaction.
//! Insert-or-match keeps replays idempotent: an identical snapshot is
//! read-only, a divergent occupant fails closed. Get returns the exact
//! snapshot spelling; scan enumerates identities independently of the source
//! and, on a full pass, requires the owner touch row to match the source.
//! Entry points: the three `migration.browser.observation.*` operations
//! dispatched from `migration_session`.

use serde::Deserialize;
use serde_json::{json, Value};
use std::io;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    BROWSER_OBSERVATION_OWNER_TOUCH_NAMESPACE, BROWSER_SITE_OBSERVATION_DOCUMENT_NAMESPACE,
    BROWSER_SITE_OBSERVATION_LRU_NAMESPACE, MAX_BROWSER_SITE_OBSERVATION_STORED_BYTES,
    MIGRATION_SESSION_MAX_PAGE_ROWS,
};
use crate::versioned_document;

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn snapshot_conflict() -> io::Error {
    io::Error::new(
        io::ErrorKind::WouldBlock,
        "browser observation snapshot differs",
    )
}

const LOGICAL_NAMESPACE: &str = "browser_site_observations";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RestoreRequest {
    origin: String,
    route_family: String,
    operation: String,
    observation: Value,
    version: u64,
    updated_at_ms: u64,
    owner_touched_at_ms: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GetRequest {
    origin: String,
    route_family: String,
    operation: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ScanRequest {
    cursor: Option<String>,
}

fn identity_digest(origin: &str, route_family: &str, operation: &str) -> [u8; 32] {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(b"tofu-db:browser-site-observation:v1\0");
    for value in [
        origin.as_bytes(),
        route_family.as_bytes(),
        operation.as_bytes(),
    ] {
        hasher.update((value.len() as u64).to_be_bytes());
        hasher.update(value);
    }
    hasher.finalize().into()
}

fn hex_digest(digest: &[u8; 32]) -> String {
    let mut encoded = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write as _;
        write!(&mut encoded, "{byte:02x}").expect("writing to String cannot fail");
    }
    encoded
}

fn document_key(transaction: &AuthorityTransaction, digest: &[u8; 32]) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        BROWSER_SITE_OBSERVATION_DOCUMENT_NAMESPACE,
        digest,
    )
}

fn lru_key(
    transaction: &AuthorityTransaction,
    observed_at_ms: u64,
    digest: &[u8; 32],
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity(40);
    raw.extend_from_slice(&observed_at_ms.to_be_bytes());
    raw.extend_from_slice(digest);
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        BROWSER_SITE_OBSERVATION_LRU_NAMESPACE,
        &raw,
    )
}

fn owner_touch_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        BROWSER_OBSERVATION_OWNER_TOUCH_NAMESPACE,
        b"touch",
    )
}

fn unsigned_field(document: &serde_json::Map<String, Value>, field: &str) -> io::Result<u64> {
    document
        .get(field)
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("browser site observation is malformed"))
}

fn validate_observation(
    request: &RestoreRequest,
    document: &serde_json::Map<String, Value>,
) -> io::Result<u64> {
    if document.get("origin").and_then(Value::as_str) != Some(request.origin.as_str())
        || document.get("route_family").and_then(Value::as_str)
            != Some(request.route_family.as_str())
        || document.get("operation").and_then(Value::as_str) != Some(request.operation.as_str())
    {
        return Err(invalid_input(
            "browser observation identity differs from its document",
        ));
    }
    for field in [
        "last_observed_at_ms",
        "expires_at_ms",
        "visit_count",
        "confidence_milli",
    ] {
        unsigned_field(document, field)?;
    }
    let observed = unsigned_field(document, "last_observed_at_ms")?;
    if observed == 0
        || unsigned_field(document, "expires_at_ms")? <= observed
        || request.version == 0
        || request.updated_at_ms == 0
    {
        return Err(invalid_input("invalid browser observation snapshot"));
    }
    Ok(observed)
}

fn parse_restore(payload: Value) -> io::Result<(RestoreRequest, serde_json::Map<String, Value>)> {
    let request: RestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid browser observation restore request"))?;
    let document = request
        .observation
        .as_object()
        .cloned()
        .ok_or_else(|| invalid_input("browser observation must be an object"))?;
    validate_observation(&request, &document)?;
    Ok((request, document))
}

fn restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let (request, document) = parse_restore(payload)?;
    let digest = identity_digest(&request.origin, &request.route_family, &request.operation);
    let existing = read_snapshot(
        database,
        transaction,
        &GetRequest {
            origin: request.origin.clone(),
            route_family: request.route_family.clone(),
            operation: request.operation.clone(),
        },
    )?;
    if let Some(existing) = existing {
        if existing["observation"] == request.observation
            && existing["version"] == request.version
            && existing["updated_at_ms"] == request.updated_at_ms
        {
            return advance_owner_touch(database, transaction, request.owner_touched_at_ms);
        }
        return Err(snapshot_conflict());
    }
    let value_json = serde_json::to_vec(&Value::Object(document))
        .map_err(|_| invalid_input("browser observation cannot be encoded"))?;
    if value_json.len() > MAX_BROWSER_SITE_OBSERVATION_STORED_BYTES {
        return Err(invalid_input(
            "browser site observation stored document exceeds its bound",
        ));
    }
    versioned_document::restore_snapshot(
        database,
        transaction,
        versioned_document::SnapshotDocument {
            key: document_key(transaction, &digest)?,
            namespace: LOGICAL_NAMESPACE.to_owned(),
            logical_key: hex_digest(&digest),
            version: request.version,
            updated_at_ms: request.updated_at_ms,
            value_json,
            blob_owner_user_id: transaction.owner_user_id(),
        },
    )?;
    let observed = request.observation["last_observed_at_ms"]
        .as_u64()
        .expect("validated observation timestamp");
    let expires_at_ms = request.observation["expires_at_ms"]
        .as_u64()
        .expect("validated observation expiry");
    database.entity_put(
        transaction,
        lru_key(transaction, observed, &digest)?,
        serde_json::to_vec(&json!({
            "digest": hex_digest(&digest),
            "expires_at_ms": expires_at_ms,
            "origin": request.origin,
            "route_family": request.route_family,
            "operation": request.operation,
        }))
        .map_err(|_| invalid_data("browser observation LRU value cannot be encoded"))?,
    )?;
    let _ = advance_owner_touch(database, transaction, request.owner_touched_at_ms)?;
    Ok(true)
}

fn advance_owner_touch(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    touched_at_ms: u64,
) -> io::Result<bool> {
    if touched_at_ms == 0 {
        return Ok(false);
    }
    let key = owner_touch_key(transaction)?;
    let current = match database.entity_get(transaction, &key)? {
        None => 0,
        Some(raw) if raw.len() == 8 => u64::from_le_bytes(raw.try_into().unwrap()),
        Some(_) => return Err(invalid_data("browser observation owner touch is malformed")),
    };
    if current >= touched_at_ms {
        return Ok(false);
    }
    database.entity_put(transaction, key, touched_at_ms.to_le_bytes().to_vec())?;
    Ok(true)
}

fn read_snapshot(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &GetRequest,
) -> io::Result<Option<Value>> {
    let digest = identity_digest(&request.origin, &request.route_family, &request.operation);
    let key = document_key(transaction, &digest)?;
    let Some(snapshot) = versioned_document::read_snapshot(
        database,
        transaction,
        &key,
        LOGICAL_NAMESPACE,
        &hex_digest(&digest),
    )?
    else {
        return Ok(None);
    };
    let document: Value = serde_json::from_slice(&snapshot.value_json)
        .map_err(|_| invalid_data("browser site observation is malformed"))?;
    if document.get("origin").and_then(Value::as_str) != Some(request.origin.as_str())
        || document.get("route_family").and_then(Value::as_str)
            != Some(request.route_family.as_str())
        || document.get("operation").and_then(Value::as_str) != Some(request.operation.as_str())
    {
        return Err(invalid_data("browser site observation digest collision"));
    }
    Ok(Some(json!({
        "observation": document,
        "version": snapshot.version,
        "updated_at_ms": snapshot.updated_at_ms,
    })))
}

fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: GetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid browser observation get request"))?;
    match read_snapshot(database, transaction, &request)? {
        None => Ok(Value::Null),
        Some(found) => Ok(json!({
            "origin": request.origin,
            "route_family": request.route_family,
            "operation": request.operation,
            "observation": found["observation"],
            "version": found["version"],
            "updated_at_ms": found["updated_at_ms"],
        })),
    }
}

fn scan(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: ScanRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid browser observation scan request"))?;
    let mut start = Vec::new();
    if let Some(cursor) = request.cursor.as_deref() {
        let encoded = cursor
            .strip_prefix("browser_observations:")
            .ok_or_else(|| invalid_input("invalid browser observation catalog cursor"))?;
        if encoded.len() != 64 || !encoded.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(invalid_input("invalid browser observation catalog cursor"));
        }
        start = encoded
            .as_bytes()
            .as_chunks::<2>()
            .0
            .iter()
            .map(|pair| {
                u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16)
                    .map_err(|_| invalid_input("invalid browser observation catalog cursor"))
            })
            .collect::<io::Result<Vec<u8>>>()?;
        start.push(0);
    }
    let start = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        BROWSER_SITE_OBSERVATION_DOCUMENT_NAMESPACE,
        &start,
    )?;
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        BROWSER_SITE_OBSERVATION_DOCUMENT_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(transaction, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, _) in &page {
        let envelope = crate::versioned_document::read_snapshot(
            database,
            transaction,
            physical,
            LOGICAL_NAMESPACE,
            &hex_digest(
                physical
                    .key_bytes()
                    .try_into()
                    .map_err(|_| invalid_data("browser observation catalog key is malformed"))?,
            ),
        )?
        .ok_or_else(|| invalid_data("browser observation catalog target is missing"))?;
        let document: Value = serde_json::from_slice(&envelope.value_json)
            .map_err(|_| invalid_data("browser observation catalog document is malformed"))?;
        let (origin, route_family, operation) = (
            document
                .get("origin")
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_data("browser observation catalog is malformed"))?,
            document
                .get("route_family")
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_data("browser observation catalog is malformed"))?,
            document
                .get("operation")
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_data("browser observation catalog is malformed"))?,
        );
        if &document_key(
            transaction,
            &identity_digest(origin, route_family, operation),
        )? != physical
        {
            return Err(invalid_data("browser observation catalog identity differs"));
        }
        rows.push(json!({
            "origin": origin,
            "route_family": route_family,
            "operation": operation,
        }));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        Some(format!(
            "browser_observations:{}",
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
    let owner_touched_at_ms =
        match database.entity_get(transaction, &owner_touch_key(transaction)?)? {
            None => 0,
            Some(raw) if raw.len() == 8 => u64::from_le_bytes(raw.try_into().unwrap()),
            Some(_) => return Err(invalid_data("browser observation owner touch is malformed")),
        };
    Ok(json!({
        "rows": rows,
        "cursor": cursor,
        "owner_touched_at_ms": owner_touched_at_ms,
    }))
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
        "migration.browser.observation.restore" => {
            let inserted = restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.browser.observation.get" => get(database, &mut transaction, payload),
        "migration.browser.observation.scan" => scan(database, &mut transaction, payload),
        _ => Err(invalid_input(
            "unknown browser observation migration operation",
        )),
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

    fn snapshot(origin: &str) -> Value {
        json!({
            "origin": origin,
            "route_family": "api",
            "operation": "list",
            "observation": {
                "schema_version": 1,
                "origin": origin,
                "route_family": "api",
                "operation": "list",
                "strategy": "captured_api",
                "api_hints": [],
                "anti_bot_vendor": "",
                "auth_signal": "none",
                "status": "active",
                "confidence_milli": 500,
                "visit_count": 3,
                "successful_visits": 3,
                "hinted_visits": 0,
                "hint_match_visits": 0,
                "consecutive_failures": 0,
                "last_outcome": "success",
                "last_elapsed_ms": 40,
                "last_verified_at_ms": 1000,
                "last_observed_at_ms": 1000,
                "expires_at_ms": 1000_u64 + 30 * 24 * 60 * 60 * 1000_u64,
                "payload_bytes": 128,
            },
            "version": 7,
            "updated_at_ms": 424242,
            "owner_touched_at_ms": 424242,
        })
    }

    #[test]
    fn restore_preserves_exact_state_across_reopen_and_is_idempotent() {
        let (vfs, mut database) = prepared();
        let snapshot = snapshot("https://example.com");
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.browser.observation.restore",
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
                "migration.browser.observation.restore",
                snapshot.clone()
            )
            .unwrap()["inserted"],
            false
        );
        let got = execute(
            &mut database,
            7,
            11,
            "migration.browser.observation.get",
            json!({
                "origin": "https://example.com",
                "route_family": "api",
                "operation": "list",
            }),
        )
        .unwrap();
        assert_eq!(got["observation"]["visit_count"], 3);
        assert_eq!(got["version"], 7);
        assert_eq!(got["updated_at_ms"], 424242);
        let mut divergent = snapshot.clone();
        divergent["observation"]["visit_count"] = json!(4);
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.browser.observation.restore",
            divergent
        )
        .is_err());
    }

    #[test]
    fn live_get_and_owner_scope_see_restored_documents() {
        let (_vfs, mut database) = prepared();
        execute(
            &mut database,
            7,
            11,
            "migration.browser.observation.restore",
            snapshot("https://example.com"),
        )
        .unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        let live = crate::browser_site_observation::get(
            &database,
            &mut transaction,
            &crate::browser_site_observation::Identity {
                origin: "https://example.com".into(),
                route_family: "api".into(),
                operation: "list".into(),
            },
            1500,
        )
        .unwrap()
        .expect("restored observation is visible to the live get path");
        let live: Value = serde_json::from_slice(&live).unwrap();
        assert_eq!(live["confidence_milli"], 500);
        drop(transaction);
        let page = execute(
            &mut database,
            7,
            11,
            "migration.browser.observation.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(page["rows"].as_array().unwrap().len(), 1);
        assert_eq!(page["owner_touched_at_ms"], 424242);
        let other = execute(
            &mut database,
            7,
            12,
            "migration.browser.observation.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(other["rows"], json!([]));
        assert_eq!(other["owner_touched_at_ms"], 0);
    }

    #[test]
    fn invalid_snapshots_fail_before_any_write() {
        let (_vfs, mut database) = prepared();
        let mut bad = snapshot("https://example.com");
        bad["observation"]["visit_count"] = json!("three");
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.browser.observation.restore",
            bad
        )
        .is_err());
        let mut bad = snapshot("https://example.com");
        bad["version"] = json!(0);
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.browser.observation.restore",
            bad
        )
        .is_err());
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.browser.observation.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
