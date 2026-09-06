//! Bounded owner-scoped authority for passive browser site observations.
//!
//! Composite identities can exceed the Entity key limit, so documents use a
//! domain-separated digest and retain the exact identity for collision checks.
//! A covering owner-local LRU index makes expiry and the 200-document capacity
//! enforceable with one bounded page and no document-namespace scan.

use std::io;

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    BROWSER_SITE_OBSERVATION_DOCUMENT_NAMESPACE, BROWSER_SITE_OBSERVATION_LRU_NAMESPACE,
    MAX_BROWSER_SITE_OBSERVATIONS_PER_OWNER, MAX_BROWSER_SITE_OBSERVATION_BYTES,
    MAX_BROWSER_SITE_OBSERVATION_STORED_BYTES,
};
use crate::versioned_document::{self, PutRequest};

const LOGICAL_NAMESPACE: &str = "browser_site_observations";
const RETENTION_MS: u64 = 30 * 24 * 60 * 60 * 1_000;
const MAX_EXPIRED_PER_RECORD: usize = 64;

pub(crate) struct Identity {
    pub origin: String,
    pub route_family: String,
    pub operation: String,
}

pub(crate) struct RecordRequest {
    pub identity: Identity,
    pub outcome: String,
    pub observed_at_ms: u64,
    pub observation_json: Option<Vec<u8>>,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn identity_digest(identity: &Identity) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(b"tofu-db:browser-site-observation:v1\0");
    for value in [
        identity.origin.as_bytes(),
        identity.route_family.as_bytes(),
        identity.operation.as_bytes(),
    ] {
        hasher.update((value.len() as u64).to_be_bytes());
        hasher.update(value);
    }
    hasher.finalize().into()
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
    let mut key = Vec::with_capacity(40);
    key.extend_from_slice(&observed_at_ms.to_be_bytes());
    key.extend_from_slice(digest);
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        BROWSER_SITE_OBSERVATION_LRU_NAMESPACE,
        &key,
    )
}

fn lru_range(transaction: &AuthorityTransaction) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        BROWSER_SITE_OBSERVATION_LRU_NAMESPACE,
        b"",
    )
}

fn lru_value(identity: &Identity, digest: &[u8; 32], expires_at_ms: u64) -> io::Result<Vec<u8>> {
    serde_json::to_vec(&json!({
        "digest": hex_digest(digest),
        "expires_at_ms": expires_at_ms,
        "origin": identity.origin,
        "route_family": identity.route_family,
        "operation": identity.operation
    }))
    .map_err(|_| invalid_input("browser site observation LRU value cannot be encoded"))
}

fn decode_hex_digest(value: &str) -> io::Result<[u8; 32]> {
    if value.len() != 64 {
        return Err(invalid_data("browser site observation digest is malformed"));
    }
    let mut digest = [0_u8; 32];
    for (index, byte) in digest.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&value[index * 2..index * 2 + 2], 16)
            .map_err(|_| invalid_data("browser site observation digest is malformed"))?;
    }
    Ok(digest)
}

fn decode_document(bytes: &[u8], expected: Option<&Identity>) -> io::Result<Map<String, Value>> {
    let document = serde_json::from_slice::<Value>(bytes)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("browser site observation is malformed"))?;
    for field in ["origin", "route_family", "operation"] {
        if !document.get(field).is_some_and(Value::is_string) {
            return Err(invalid_data(
                "browser site observation identity is malformed",
            ));
        }
    }
    if let Some(expected) = expected {
        if document["origin"] != expected.origin
            || document["route_family"] != expected.route_family
            || document["operation"] != expected.operation
        {
            return Err(invalid_data("browser site observation digest collision"));
        }
    }
    Ok(document)
}

fn materialize(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    identity: &Identity,
) -> io::Result<Option<(u64, Map<String, Value>)>> {
    let digest = identity_digest(identity);
    let key = document_key(transaction, &digest)?;
    let Some(raw) = versioned_document::get(
        database,
        transaction,
        &key,
        LOGICAL_NAMESPACE,
        &hex_digest(&digest),
    )?
    else {
        return Ok(None);
    };
    let envelope: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("browser site observation envelope is malformed"))?;
    let version = envelope
        .get("version")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("browser site observation version is malformed"))?;
    let value = envelope
        .get("value")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid_data("browser site observation value is malformed"))?;
    let bytes = serde_json::to_vec(value)
        .map_err(|_| invalid_data("browser site observation cannot be encoded"))?;
    Ok(Some((version, decode_document(&bytes, Some(identity))?)))
}

fn hex_digest(digest: &[u8; 32]) -> String {
    let mut encoded = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write as _;
        write!(&mut encoded, "{byte:02x}").expect("writing to String cannot fail");
    }
    encoded
}

fn public_document(document: &Map<String, Value>) -> io::Result<Value> {
    let mut projected = document.clone();
    projected.remove("_physical_version");
    Ok(Value::Object(projected))
}

pub(crate) fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    identity: &Identity,
    now_ms: u64,
) -> io::Result<Option<Vec<u8>>> {
    let Some((_, document)) = materialize(database, transaction, identity)? else {
        return Ok(None);
    };
    let expires_at_ms = document
        .get("expires_at_ms")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("browser site observation expiry is malformed"))?;
    if expires_at_ms <= now_ms {
        return Ok(None);
    }
    serde_json::to_vec(&public_document(&document)?)
        .map(Some)
        .map_err(|_| invalid_data("browser site observation response cannot be encoded"))
}

fn validated_success(request: &RecordRequest) -> io::Result<Map<String, Value>> {
    let raw = request
        .observation_json
        .as_ref()
        .ok_or_else(|| invalid_input("successful observation is missing"))?;
    if raw.len() > MAX_BROWSER_SITE_OBSERVATION_BYTES {
        return Err(invalid_input("browser site observation exceeds 4096 bytes"));
    }
    let source = serde_json::from_slice::<Value>(raw)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_input("successful observation must be an object"))?;
    if source.get("schema_version").and_then(Value::as_u64) != Some(1) {
        return Err(invalid_input("unsupported browser site observation schema"));
    }
    let strategy = source.get("strategy").and_then(Value::as_str).unwrap_or("");
    if !matches!(
        strategy,
        "token_gated_api" | "hydrated_state" | "captured_api" | "rendered_dom"
    ) {
        return Err(invalid_input("invalid browser site observation strategy"));
    }
    let hints = source
        .get("api_hints")
        .and_then(Value::as_array)
        .filter(|hints| hints.len() <= 5)
        .ok_or_else(|| invalid_input("invalid browser site observation hints"))?;
    for hint in hints {
        validate_hint(hint)?;
    }
    let anti_bot_vendor = source
        .get("anti_bot_vendor")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !matches!(
        anti_bot_vendor,
        "" | "aliyun_waf" | "cloudflare" | "akamai" | "geetest"
    ) {
        return Err(invalid_input(
            "invalid browser site observation anti-bot vendor",
        ));
    }
    let auth_signal = source
        .get("auth_signal")
        .and_then(Value::as_str)
        .unwrap_or("none");
    if !matches!(auth_signal, "none" | "challenge") {
        return Err(invalid_input(
            "invalid browser site observation auth signal",
        ));
    }
    let elapsed_ms = source
        .get("elapsed_ms")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    if elapsed_ms > 120_000 {
        return Err(invalid_input(
            "invalid browser site observation elapsed time",
        ));
    }
    let hint_used = source.get("capture_hint_used").map_or(Ok(false), |value| {
        value
            .as_bool()
            .ok_or_else(|| invalid_input("invalid capture-hint metric"))
    })?;
    let hint_matched = source
        .get("capture_hint_matched")
        .map_or(Ok(false), |value| {
            value
                .as_bool()
                .ok_or_else(|| invalid_input("invalid capture-hint metric"))
        })?;
    if hint_matched && !hint_used {
        return Err(invalid_input("capture hint matched without use"));
    }
    Ok(json!({
        "schema_version": 1,
        "strategy": strategy,
        "api_hints": hints,
        "anti_bot_vendor": anti_bot_vendor,
        "auth_signal": auth_signal,
        "last_elapsed_ms": elapsed_ms,
        "payload_bytes": raw.len(),
        "capture_hint_used": hint_used,
        "capture_hint_matched": hint_matched
    })
    .as_object()
    .unwrap()
    .clone())
}

fn validate_hint(hint: &Value) -> io::Result<()> {
    let hint = hint
        .as_object()
        .filter(|hint| hint.len() == 6)
        .ok_or_else(|| invalid_input("invalid browser site observation hint fields"))?;
    let required = [
        "method",
        "origin",
        "path_template",
        "shape_summary",
        "score",
        "passive_only",
    ];
    if required.iter().any(|field| !hint.contains_key(*field))
        || hint.get("passive_only").and_then(Value::as_bool) != Some(true)
        || !matches!(
            hint.get("method").and_then(Value::as_str),
            Some("GET" | "HEAD" | "POST" | "PUT" | "PATCH" | "DELETE")
        )
        || !hint
            .get("origin")
            .and_then(Value::as_str)
            .is_some_and(valid_origin)
        || !hint
            .get("path_template")
            .and_then(Value::as_str)
            .is_some_and(valid_route_family)
    {
        return Err(invalid_input("invalid browser site observation hint"));
    }
    let shape = hint
        .get("shape_summary")
        .and_then(Value::as_object)
        .filter(|shape| shape.len() <= 12)
        .ok_or_else(|| invalid_input("invalid browser site observation hint shape"))?;
    if shape.iter().any(|(key, value)| {
        key.is_empty()
            || key.chars().count() > 240
            || sensitive_key(key)
            || !value.as_str().is_some_and(valid_shape_descriptor)
    }) {
        return Err(invalid_input("invalid browser site observation hint shape"));
    }
    let score = hint
        .get("score")
        .and_then(Value::as_f64)
        .filter(|score| score.is_finite() && (0.0..=1.0).contains(score));
    if score.is_none() {
        return Err(invalid_input("invalid browser site observation hint score"));
    }
    Ok(())
}

fn sensitive_key(key: &str) -> bool {
    if key == "$.[sensitive]" {
        return false;
    }
    let lowered = key.to_ascii_lowercase();
    [
        "token",
        "secret",
        "password",
        "passwd",
        "authorization",
        "cookie",
        "credential",
        "session",
        "ticket",
        "sso",
        "apikey",
        "api_key",
        "api-key",
    ]
    .iter()
    .any(|needle| lowered.contains(needle))
}

fn valid_shape_descriptor(value: &str) -> bool {
    if matches!(
        value,
        "null" | "boolean" | "number" | "string" | "object" | "object(empty)"
    ) {
        return true;
    }
    for (prefix, suffix) in [
        ("string(len=", ")"),
        ("array(", ")"),
        ("reached ", "-entry budget"),
    ] {
        if let Some(number) = value
            .strip_prefix(prefix)
            .and_then(|value| value.strip_suffix(suffix))
        {
            return !number.is_empty() && number.bytes().all(|byte| byte.is_ascii_digit());
        }
    }
    false
}

pub(crate) fn valid_origin(value: &str) -> bool {
    if value.chars().count() > 512 {
        return false;
    }
    let authority = value
        .strip_prefix("https://")
        .or_else(|| value.strip_prefix("http://"));
    authority.is_some_and(|authority| {
        let host_present = if let Some(bracketed) = authority.strip_prefix('[') {
            bracketed.split_once(']').is_some_and(|(host, suffix)| {
                !host.is_empty() && (suffix.is_empty() || suffix.starts_with(':'))
            })
        } else {
            !authority.split(':').next().unwrap_or("").is_empty()
        };
        host_present
            && !authority.contains(['/', '?', '#', '@'])
            && !authority.chars().any(char::is_whitespace)
    })
}

pub(crate) fn valid_route_family(value: &str) -> bool {
    if value.chars().count() > 512 || !value.starts_with('/') || value.contains(['?', '#']) {
        return false;
    }
    let mut prior = "";
    for (index, segment) in value[1..].split('/').enumerate() {
        let valid = (value == "/" && segment.is_empty())
            || matches!(segment, "{segment}" | "{truncated}")
            || (segment.len() <= 40
                && segment.as_bytes()[0].is_ascii_lowercase()
                && segment
                    .bytes()
                    .all(|byte| byte.is_ascii_lowercase() || byte == b'_' || byte == b'-'));
        if !valid
            || (index > 0
                && matches!(
                    prior,
                    "account"
                        | "accounts"
                        | "document"
                        | "documents"
                        | "employee"
                        | "employees"
                        | "item"
                        | "items"
                        | "member"
                        | "members"
                        | "order"
                        | "orders"
                        | "org"
                        | "orgs"
                        | "organization"
                        | "organizations"
                        | "people"
                        | "person"
                        | "profile"
                        | "profiles"
                        | "project"
                        | "projects"
                        | "team"
                        | "teams"
                        | "user"
                        | "users"
                )
                && segment != "{segment}")
        {
            return false;
        }
        prior = segment;
    }
    true
}

pub(crate) fn valid_identity(origin: &str, route_family: &str, operation: &str) -> bool {
    valid_origin(origin)
        && valid_route_family(route_family)
        && !operation.is_empty()
        && operation.chars().count() <= 64
}

fn number(document: &Map<String, Value>, field: &str) -> io::Result<u64> {
    document
        .get(field)
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("browser site observation counter is malformed"))
}

fn delete_indexed_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    lru_key: EntityKey,
    document_key: EntityKey,
) -> io::Result<()> {
    database.entity_delete(transaction, lru_key)?;
    let digest: [u8; 32] = document_key
        .key_bytes()
        .try_into()
        .map_err(|_| invalid_data("browser observation key is malformed"))?;
    let deleted = versioned_document::delete(
        database,
        transaction,
        document_key,
        LOGICAL_NAMESPACE,
        &hex_digest(&digest),
        None,
    )?;
    if deleted != br#"{"deleted":true}"# {
        return Err(invalid_data(
            "browser site observation LRU target is missing",
        ));
    }
    Ok(())
}

fn prune_lru(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    now_ms: u64,
) -> io::Result<()> {
    let (start, end) = lru_range(transaction)?;
    let rows = database.entity_scan(
        transaction,
        &start,
        &end,
        MAX_BROWSER_SITE_OBSERVATIONS_PER_OWNER + 1,
    )?;
    let mut retained = Vec::with_capacity(rows.len());
    let mut expired = Vec::new();
    for (key, covering_bytes) in rows {
        let covering = serde_json::from_slice::<Value>(&covering_bytes)
            .ok()
            .and_then(|value| value.as_object().cloned())
            .ok_or_else(|| invalid_data("browser site observation LRU value is malformed"))?;
        let digest =
            decode_hex_digest(covering.get("digest").and_then(Value::as_str).unwrap_or(""))?;
        let document_key = document_key(transaction, &digest)?;
        let identity_order = (
            covering["origin"].as_str().unwrap_or("").to_owned(),
            covering["route_family"].as_str().unwrap_or("").to_owned(),
            covering["operation"].as_str().unwrap_or("").to_owned(),
        );
        if key.key_bytes().len() != 40
            || key.key_bytes()[8..] != digest
            || !valid_identity(&identity_order.0, &identity_order.1, &identity_order.2)
        {
            return Err(invalid_data(
                "browser site observation LRU identity is malformed",
            ));
        }
        let last_observed_at_ms = u64::from_be_bytes(
            key.key_bytes()
                .get(..8)
                .ok_or_else(|| invalid_data("browser observation LRU key is malformed"))?
                .try_into()
                .unwrap(),
        );
        let entry = (
            key,
            document_key,
            number(&covering, "expires_at_ms")?,
            last_observed_at_ms,
            identity_order,
        );
        if entry.2 <= now_ms {
            expired.push(entry);
        } else {
            retained.push(entry);
        }
    }
    expired.sort_by(|left, right| {
        (left.2, &left.4 .0, &left.4 .1, &left.4 .2).cmp(&(
            right.2,
            &right.4 .0,
            &right.4 .1,
            &right.4 .2,
        ))
    });
    if expired.len() > MAX_EXPIRED_PER_RECORD {
        retained.extend(expired.drain(MAX_EXPIRED_PER_RECORD..));
    }
    for (key, document_key, _, _, _) in expired {
        delete_indexed_document(database, transaction, key, document_key)?;
    }
    retained.sort_by(|left, right| {
        (left.3, &left.4 .0, &left.4 .1, &left.4 .2).cmp(&(
            right.3,
            &right.4 .0,
            &right.4 .1,
            &right.4 .2,
        ))
    });
    let overflow = retained
        .len()
        .saturating_sub(MAX_BROWSER_SITE_OBSERVATIONS_PER_OWNER);
    for (key, document_key, _, _, _) in retained.into_iter().take(overflow) {
        delete_indexed_document(database, transaction, key, document_key)?;
    }
    Ok(())
}

pub(crate) fn record(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &RecordRequest,
    updated_at_ms: u64,
) -> io::Result<Option<Vec<u8>>> {
    let digest = identity_digest(&request.identity);
    let current = materialize(database, transaction, &request.identity)?;
    if request.outcome != "success" && current.is_none() {
        return Ok(None);
    }
    let previous = current.as_ref().map(|(_, document)| document);
    let mut next = if request.outcome == "success" {
        let success = validated_success(request)?;
        let same_strategy =
            previous.is_some_and(|previous| previous.get("strategy") == success.get("strategy"));
        let confidence = if same_strategy {
            number(previous.unwrap(), "confidence_milli")?
                .saturating_add(100)
                .min(1_000)
        } else if previous.is_none() {
            500
        } else {
            400
        };
        let previous_number = |field| previous.map_or(Ok(0), |document| number(document, field));
        json!({
            "schema_version": 1,
            "origin": request.identity.origin,
            "route_family": request.identity.route_family,
            "operation": request.identity.operation,
            "strategy": success["strategy"],
            "api_hints": success["api_hints"],
            "anti_bot_vendor": success["anti_bot_vendor"],
            "auth_signal": success["auth_signal"],
            "status": "active",
            "confidence_milli": confidence,
            "visit_count": previous_number("visit_count")?.saturating_add(1),
            "successful_visits": previous_number("successful_visits")?.saturating_add(1),
            "hinted_visits": previous_number("hinted_visits")?.saturating_add(u64::from(success["capture_hint_used"].as_bool().unwrap())),
            "hint_match_visits": previous_number("hint_match_visits")?.saturating_add(u64::from(success["capture_hint_matched"].as_bool().unwrap())),
            "consecutive_failures": 0,
            "last_outcome": "success",
            "last_elapsed_ms": success["last_elapsed_ms"],
            "last_verified_at_ms": request.observed_at_ms,
            "last_observed_at_ms": request.observed_at_ms,
            "expires_at_ms": request.observed_at_ms.checked_add(RETENTION_MS).ok_or_else(|| invalid_input("browser observation expiry overflows"))?,
            "payload_bytes": success["payload_bytes"]
        }).as_object().unwrap().clone()
    } else {
        let previous = previous.unwrap();
        let structural = matches!(request.outcome.as_str(), "structure_mismatch" | "not_found");
        let not_observed = request.outcome == "not_observed";
        let failures = number(previous, "consecutive_failures")?
            .saturating_add(u64::from(structural || not_observed));
        let penalty = if structural {
            250
        } else if not_observed {
            100
        } else {
            0
        };
        let mut document = previous.clone();
        document.insert(
            "confidence_milli".to_owned(),
            Value::from(number(previous, "confidence_milli")?.saturating_sub(penalty)),
        );
        document.insert(
            "visit_count".to_owned(),
            Value::from(number(previous, "visit_count")?.saturating_add(1)),
        );
        document.insert("consecutive_failures".to_owned(), Value::from(failures));
        document.insert(
            "last_outcome".to_owned(),
            Value::String(request.outcome.clone()),
        );
        document.insert(
            "last_observed_at_ms".to_owned(),
            Value::from(request.observed_at_ms),
        );
        document.insert(
            "expires_at_ms".to_owned(),
            Value::from(
                request
                    .observed_at_ms
                    .checked_add(RETENTION_MS)
                    .ok_or_else(|| invalid_input("browser observation expiry overflows"))?,
            ),
        );
        if request.outcome == "auth_challenge" {
            document.insert(
                "auth_signal".to_owned(),
                Value::String("challenge".to_owned()),
            );
        }
        if failures >= 3 {
            document.insert("status".to_owned(), Value::String("quarantined".to_owned()));
        }
        document
    };
    let old_observed = previous
        .map(|document| number(document, "last_observed_at_ms"))
        .transpose()?;
    let key = document_key(transaction, &digest)?;
    let logical_key = hex_digest(&digest);
    let value_json = serde_json::to_vec(&Value::Object(next.clone()))
        .map_err(|_| invalid_input("browser observation cannot be encoded"))?;
    if value_json.len() > MAX_BROWSER_SITE_OBSERVATION_STORED_BYTES {
        return Err(invalid_input(
            "browser site observation stored document exceeds 8192 bytes",
        ));
    }
    versioned_document::put(
        database,
        transaction,
        PutRequest {
            key: key.clone(),
            namespace: LOGICAL_NAMESPACE.to_owned(),
            logical_key: logical_key.clone(),
            value_json,
            expected_version: None,
            updated_at_ms,
        },
    )?;
    if let Some(old_observed) = old_observed {
        database.entity_delete(transaction, lru_key(transaction, old_observed, &digest)?)?;
    }
    let expires_at_ms = number(&next, "expires_at_ms")?;
    database.entity_put(
        transaction,
        lru_key(transaction, request.observed_at_ms, &digest)?,
        lru_value(&request.identity, &digest, expires_at_ms)?,
    )?;
    prune_lru(database, transaction, request.observed_at_ms)?;
    let still_present = database.entity_get(transaction, &key)?.is_some();
    if !still_present {
        return Ok(None);
    }
    next.remove("_physical_version");
    serde_json::to_vec(&Value::Object(next))
        .map(Some)
        .map_err(|_| invalid_data("browser observation response cannot be encoded"))
}
