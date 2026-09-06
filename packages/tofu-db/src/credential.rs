//! Tenant-global bearer credential authority with compact authentication state.
//!
//! Public settings may spill to Blob, while authentication, touch, disable,
//! expiry, and revocation mutate only a small Entity record. Secret hashes are
//! exact-verified global locators and never enter a public projection.

use std::io;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    CREDENTIAL_CORE_NAMESPACE, CREDENTIAL_OWNER_COUNT_NAMESPACE, CREDENTIAL_OWNER_INDEX_NAMESPACE,
    CREDENTIAL_SECRET_INDEX_NAMESPACE, CREDENTIAL_SETTINGS_NAMESPACE, CREDENTIAL_STATE_NAMESPACE,
    MAX_CREDENTIALS_PER_OWNER_BOUNDARY, MAX_CREDENTIAL_DOCUMENT_BYTES,
    MAX_CREDENTIAL_RESPONSE_BYTES, MAX_CREDENTIAL_SCOPES, MAX_CREDENTIAL_TIMESTAMP_SECONDS,
};
use crate::versioned_document::{self, PutRequest};

const LOGICAL_NAMESPACE: &str = "auth_credentials";

#[derive(Clone, Debug)]
pub struct Boundary {
    pub owner_user_id: u64,
    pub tenant_label: String,
}

#[derive(Clone, Debug)]
pub struct CreateRequest {
    pub credential_id: String,
    pub boundary: Boundary,
    pub account_user_id: String,
    pub name: String,
    pub prefix: String,
    pub secret_hash: String,
    pub scopes: Vec<String>,
    pub rate_limit_rpm: u64,
    pub rate_limit_tpd: u64,
    pub created_at: f64,
    pub expires_at: Option<f64>,
    pub metadata: Value,
    pub physical_updated_at_ms: u64,
}

#[derive(Clone, Debug, Default)]
pub struct UpdateRequest {
    pub name: Option<String>,
    pub scopes: Option<Vec<String>>,
    pub rate_limit_rpm: Option<u64>,
    pub rate_limit_tpd: Option<u64>,
    pub expires_at: Option<Option<f64>>,
    pub disabled: Option<bool>,
    pub metadata: Option<Value>,
    pub physical_updated_at_ms: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Core {
    id: String,
    owner_user_id: u64,
    account_user_id: String,
    tenant_id: String,
    prefix: String,
    secret_hash: String,
    created_at: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Settings {
    id: String,
    name: String,
    scopes: Vec<String>,
    rate_limit_rpm: u64,
    rate_limit_tpd: u64,
    metadata: Value,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct State {
    id: String,
    last_used_at: Option<f64>,
    expires_at: Option<f64>,
    disabled: bool,
    revoked_at: Option<f64>,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::AlreadyExists, message)
}

fn digest(domain: &[u8], parts: &[&[u8]]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(domain);
    for part in parts {
        hasher.update(part.len().to_be_bytes());
        hasher.update(part);
    }
    hasher.finalize().into()
}

fn global_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    raw: &[u8],
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        namespace,
        raw,
    )
}

fn id_raw(id: &str) -> [u8; 32] {
    digest(b"tofu-db:credential-id:v1\0", &[id.as_bytes()])
}
fn core_key(transaction: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    global_key(transaction, CREDENTIAL_CORE_NAMESPACE, &id_raw(id))
}
fn state_key(transaction: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    global_key(transaction, CREDENTIAL_STATE_NAMESPACE, &id_raw(id))
}
fn settings_key(transaction: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    global_key(transaction, CREDENTIAL_SETTINGS_NAMESPACE, &id_raw(id))
}
fn secret_key(transaction: &AuthorityTransaction, secret_hash: &str) -> io::Result<EntityKey> {
    global_key(
        transaction,
        CREDENTIAL_SECRET_INDEX_NAMESPACE,
        &digest(b"tofu-db:credential-secret:v1\0", &[secret_hash.as_bytes()]),
    )
}

fn boundary_raw(boundary: &Boundary) -> [u8; 32] {
    digest(
        b"tofu-db:credential-owner:v1\0",
        &[
            &boundary.owner_user_id.to_be_bytes(),
            boundary.tenant_label.as_bytes(),
        ],
    )
}
fn count_key(transaction: &AuthorityTransaction, boundary: &Boundary) -> io::Result<EntityKey> {
    global_key(
        transaction,
        CREDENTIAL_OWNER_COUNT_NAMESPACE,
        &boundary_raw(boundary),
    )
}
fn index_prefix(boundary: &Boundary) -> Vec<u8> {
    let mut raw = boundary_raw(boundary).to_vec();
    raw.push(b'c');
    raw
}
fn index_key(
    transaction: &AuthorityTransaction,
    boundary: &Boundary,
    created_at: f64,
    id: &str,
) -> io::Result<EntityKey> {
    let mut raw = index_prefix(boundary);
    raw.extend_from_slice(&(!created_at.to_bits()).to_be_bytes());
    for byte in id.bytes() {
        raw.extend_from_slice(&[!byte, 0]);
    }
    raw.push(u8::MAX);
    global_key(transaction, CREDENTIAL_OWNER_INDEX_NAMESPACE, &raw)
}

fn valid_timestamp(value: f64) -> bool {
    value.is_finite() && (0.0..=MAX_CREDENTIAL_TIMESTAMP_SECONDS).contains(&value)
}
fn valid_secret(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn valid_scopes(scopes: &[String], require_nonempty: bool) -> bool {
    (!require_nonempty || !scopes.is_empty())
        && scopes.len() <= MAX_CREDENTIAL_SCOPES
        && scopes
            .iter()
            .all(|v| !v.is_empty() && v.chars().count() <= 128)
        && scopes.windows(2).all(|w| w[0] < w[1])
}

fn read_json<T: for<'de> Deserialize<'de>>(raw: &[u8], message: &str) -> io::Result<T> {
    serde_json::from_slice(raw).map_err(|_| invalid_data(message))
}
fn read_core(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<Core>> {
    let core: Option<Core> = database
        .entity_get(transaction, &core_key(transaction, id)?)?
        .map(|raw| read_json(&raw, "credential core is malformed"))
        .transpose()?;
    if core.as_ref().is_some_and(|value| value.id != id) {
        return Err(invalid_data("credential ID digest collision"));
    }
    Ok(core)
}
fn read_state(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<State>> {
    database
        .entity_get(transaction, &state_key(transaction, id)?)?
        .map(|raw| read_json(&raw, "credential state is malformed"))
        .transpose()
}
fn read_settings(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<Settings>> {
    versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &settings_key(transaction, id)?,
        LOGICAL_NAMESPACE,
        id,
        TENANT_GLOBAL_OWNER_ID,
        MAX_CREDENTIAL_DOCUMENT_BYTES,
    )?
    .map(|raw| read_json(&raw, "credential settings are malformed"))
    .transpose()
}
fn write_state(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    state: &State,
) -> io::Result<()> {
    database.entity_put(
        transaction,
        state_key(transaction, &state.id)?,
        serde_json::to_vec(state)
            .map_err(|_| invalid_data("credential state cannot be encoded"))?,
    )
}
fn write_settings(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    settings: &Settings,
    physical_updated_at_ms: u64,
) -> io::Result<()> {
    let value_json = serde_json::to_vec(settings)
        .map_err(|_| invalid_input("credential settings cannot be encoded"))?;
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: settings_key(transaction, &settings.id)?,
            namespace: LOGICAL_NAMESPACE.to_owned(),
            logical_key: settings.id.clone(),
            value_json,
            expected_version: None,
            updated_at_ms: physical_updated_at_ms,
        },
        TENANT_GLOBAL_OWNER_ID,
        MAX_CREDENTIAL_DOCUMENT_BYTES,
    )?;
    Ok(())
}

fn public(core: &Core, settings: &Settings, state: &State) -> io::Result<Value> {
    if core.id != settings.id
        || core.id != state.id
        || core.owner_user_id == 0
        || core.id.is_empty()
        || core.id.chars().count() > 128
        || core.account_user_id.chars().count() > 256
        || core.tenant_id.chars().count() > 256
        || core.prefix.is_empty()
        || core.prefix.chars().count() > 32
        || !valid_secret(&core.secret_hash)
        || settings.name.chars().count() > 80
        || !valid_timestamp(core.created_at)
        || state.last_used_at.is_some_and(|v| !valid_timestamp(v))
        || state.expires_at.is_some_and(|v| !valid_timestamp(v))
        || state.revoked_at.is_some_and(|v| !valid_timestamp(v))
        || !settings.metadata.is_object()
        || !valid_scopes(&settings.scopes, false)
    {
        return Err(invalid_data("credential records disagree"));
    }
    Ok(
        json!({"id":core.id,"owner_user_id":core.owner_user_id,"account_user_id":core.account_user_id,"tenant_id":core.tenant_id,"name":settings.name,"prefix":core.prefix,"scopes":settings.scopes,"rate_limit_rpm":settings.rate_limit_rpm,"rate_limit_tpd":settings.rate_limit_tpd,"created_at":core.created_at,"last_used_at":state.last_used_at,"expires_at":state.expires_at,"disabled":state.disabled,"revoked_at":state.revoked_at,"metadata":settings.metadata}),
    )
}
fn encode(value: &Value) -> io::Result<Vec<u8>> {
    let raw = serde_json::to_vec(value)
        .map_err(|_| invalid_data("credential response cannot be encoded"))?;
    if raw.len() > MAX_CREDENTIAL_RESPONSE_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "credential response exceeds 8 MiB",
        ));
    }
    Ok(raw)
}
fn read_all(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<(Core, Settings, State)>> {
    let Some(core) = read_core(database, transaction, id)? else {
        return Ok(None);
    };
    let settings = read_settings(database, transaction, id)?
        .ok_or_else(|| invalid_data("credential settings are missing"))?;
    let state = read_state(database, transaction, id)?
        .ok_or_else(|| invalid_data("credential state is missing"))?;
    Ok(Some((core, settings, state)))
}

fn read_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    boundary: &Boundary,
) -> io::Result<usize> {
    let Some(raw) = database.entity_get(transaction, &count_key(transaction, boundary)?)? else {
        return Ok(0);
    };
    let value: Value = read_json(&raw, "credential owner count is malformed")?;
    if value["owner_user_id"] != boundary.owner_user_id
        || value["tenant_id"] != boundary.tenant_label
    {
        return Err(invalid_data("credential owner count boundary differs"));
    }
    value["count"]
        .as_u64()
        .and_then(|v| usize::try_from(v).ok())
        .filter(|v| *v <= MAX_CREDENTIALS_PER_OWNER_BOUNDARY)
        .ok_or_else(|| invalid_data("credential owner count exceeds its bound"))
}
fn write_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    boundary: &Boundary,
    count: usize,
) -> io::Result<()> {
    if count > MAX_CREDENTIALS_PER_OWNER_BOUNDARY {
        return Err(conflict("credential owner quota reached"));
    }
    database.entity_put(transaction,count_key(transaction,boundary)?,serde_json::to_vec(&json!({"owner_user_id":boundary.owner_user_id,"tenant_id":boundary.tenant_label,"count":count})).unwrap())
}

fn account_active(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    core: &Core,
) -> io::Result<bool> {
    if core.account_user_id.is_empty() {
        Ok(true)
    } else {
        crate::tenant_user::account_is_active_owner(
            database,
            transaction,
            &core.account_user_id,
            core.owner_user_id,
        )
    }
}
fn lookup_secret(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    secret_hash: &str,
) -> io::Result<Option<String>> {
    let Some(raw) = database.entity_get(transaction, &secret_key(transaction, secret_hash)?)?
    else {
        return Ok(None);
    };
    let value: Value = read_json(&raw, "credential secret index is malformed")?;
    if value["secret_hash"] != secret_hash {
        return Err(invalid_data("credential secret digest collision"));
    }
    value["credential_id"]
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| invalid_data("credential secret index ID is malformed"))
        .map(Some)
}

pub(crate) fn create(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &CreateRequest,
    only_if_owner_empty: bool,
) -> io::Result<Option<Vec<u8>>> {
    if request.credential_id.is_empty()
        || request.credential_id.chars().count() > 128
        || request.boundary.owner_user_id == 0
        || request.boundary.tenant_label.chars().count() > 256
        || request.account_user_id.chars().count() > 256
        || request.name.chars().count() > 80
        || request.prefix.is_empty()
        || request.prefix.chars().count() > 32
        || !valid_secret(&request.secret_hash)
        || !valid_scopes(&request.scopes, true)
        || !valid_timestamp(request.created_at)
        || request.expires_at.is_some_and(|v| !valid_timestamp(v))
        || !request.metadata.is_object()
        || request.physical_updated_at_ms == 0
    {
        return Err(invalid_input("invalid credential create request"));
    }
    let count = read_count(database, transaction, &request.boundary)?;
    if only_if_owner_empty && count > 0 {
        return Ok(None);
    }
    if count >= MAX_CREDENTIALS_PER_OWNER_BOUNDARY {
        return Err(conflict("credential owner quota reached"));
    }
    if !request.account_user_id.is_empty()
        && !crate::tenant_user::account_is_active_owner(
            database,
            transaction,
            &request.account_user_id,
            request.boundary.owner_user_id,
        )?
    {
        return Err(conflict(
            "credential account is missing inactive or differently owned",
        ));
    }
    if read_core(database, transaction, &request.credential_id)?.is_some()
        || lookup_secret(database, transaction, &request.secret_hash)?.is_some()
    {
        return Err(conflict("credential already exists"));
    }
    let core = Core {
        id: request.credential_id.clone(),
        owner_user_id: request.boundary.owner_user_id,
        account_user_id: request.account_user_id.clone(),
        tenant_id: request.boundary.tenant_label.clone(),
        prefix: request.prefix.clone(),
        secret_hash: request.secret_hash.clone(),
        created_at: request.created_at,
    };
    let settings = Settings {
        id: request.credential_id.clone(),
        name: request.name.clone(),
        scopes: request.scopes.clone(),
        rate_limit_rpm: request.rate_limit_rpm,
        rate_limit_tpd: request.rate_limit_tpd,
        metadata: request.metadata.clone(),
    };
    let state = State {
        id: request.credential_id.clone(),
        last_used_at: None,
        expires_at: request.expires_at,
        disabled: false,
        revoked_at: None,
    };
    database.entity_put(
        transaction,
        core_key(transaction, &core.id)?,
        serde_json::to_vec(&core).unwrap(),
    )?;
    write_settings(
        database,
        transaction,
        &settings,
        request.physical_updated_at_ms,
    )?;
    write_state(database, transaction, &state)?;
    database.entity_put(
        transaction,
        secret_key(transaction, &core.secret_hash)?,
        serde_json::to_vec(&json!({"secret_hash":core.secret_hash,"credential_id":core.id}))
            .unwrap(),
    )?;
    database.entity_put(
        transaction,
        index_key(transaction, &request.boundary, core.created_at, &core.id)?,
        core.id.as_bytes().to_vec(),
    )?;
    write_count(database, transaction, &request.boundary, count + 1)?;
    encode(&public(&core, &settings, &state)?).map(Some)
}

pub(crate) fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    boundary: &Boundary,
    id: &str,
) -> io::Result<Option<Vec<u8>>> {
    let Some((core, settings, state)) = read_all(database, transaction, id)? else {
        return Ok(None);
    };
    if core.owner_user_id != boundary.owner_user_id
        || core.tenant_id != boundary.tenant_label
        || state.revoked_at.is_some()
    {
        return Ok(None);
    }
    encode(&public(&core, &settings, &state)?).map(Some)
}
pub(crate) fn exists(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    boundary: &Boundary,
) -> io::Result<Vec<u8>> {
    encode(&json!({"exists":read_count(database,transaction,boundary)?>0}))
}

pub(crate) fn list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    boundary: &Boundary,
) -> io::Result<Vec<u8>> {
    let count = read_count(database, transaction, boundary)?;
    let prefix = index_prefix(boundary);
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        CREDENTIAL_OWNER_INDEX_NAMESPACE,
        &prefix,
    )?;
    let rows = database.entity_scan(
        transaction,
        &start,
        &end,
        MAX_CREDENTIALS_PER_OWNER_BOUNDARY,
    )?;
    if rows.len() != count {
        return Err(invalid_data("credential owner index count differs"));
    }
    let mut values = Vec::with_capacity(rows.len());
    let mut total = 2usize;
    for (_, raw) in rows {
        let id = std::str::from_utf8(&raw)
            .map_err(|_| invalid_data("credential owner index is malformed"))?;
        let (core, settings, state) = read_all(database, transaction, id)?
            .ok_or_else(|| invalid_data("credential owner index target is missing"))?;
        if core.owner_user_id != boundary.owner_user_id
            || core.tenant_id != boundary.tenant_label
            || state.revoked_at.is_some()
        {
            return Err(invalid_data("credential owner index target differs"));
        }
        let value = public(&core, &settings, &state)?;
        let bytes = serde_json::to_vec(&value).unwrap().len();
        total = total
            .checked_add(bytes + usize::from(!values.is_empty()))
            .filter(|v| *v <= MAX_CREDENTIAL_RESPONSE_BYTES)
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::OutOfMemory, "credential list exceeds 8 MiB")
            })?;
        values.push(value);
    }
    encode(&Value::Array(values))
}

fn validated(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    secret_hash: &str,
    now: f64,
) -> io::Result<Option<(Core, Settings, State)>> {
    if !valid_secret(secret_hash) || !valid_timestamp(now) {
        return Err(invalid_input("invalid credential validation request"));
    }
    let Some(id) = lookup_secret(database, transaction, secret_hash)? else {
        return Ok(None);
    };
    let all = read_all(database, transaction, &id)?
        .ok_or_else(|| invalid_data("credential secret target is missing"))?;
    if all.0.secret_hash != secret_hash {
        return Err(invalid_data("credential secret target differs"));
    }
    if all.2.disabled
        || all.2.revoked_at.is_some()
        || all.2.expires_at.is_some_and(|expiry| expiry <= now)
        || !account_active(database, transaction, &all.0)?
    {
        return Ok(None);
    }
    Ok(Some(all))
}
pub(crate) fn validate(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    secret_hash: &str,
    now: f64,
) -> io::Result<Option<Vec<u8>>> {
    validated(database, transaction, secret_hash, now)?
        .map(|(c, s, t)| encode(&public(&c, &s, &t)?))
        .transpose()
}
pub(crate) fn authenticate(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    secret_hash: &str,
    now: f64,
) -> io::Result<Option<Vec<u8>>> {
    let Some((c, s, mut t)) = validated(database, transaction, secret_hash, now)? else {
        return Ok(None);
    };
    t.last_used_at = Some(now);
    write_state(database, transaction, &t)?;
    encode(&public(&c, &s, &t)?).map(Some)
}
pub(crate) fn identify(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    secret_hash: &str,
) -> io::Result<Option<Vec<u8>>> {
    if !valid_secret(secret_hash) {
        return Err(invalid_input("invalid credential secret hash"));
    }
    let Some(id) = lookup_secret(database, transaction, secret_hash)? else {
        return Ok(None);
    };
    let (c, s, t) = read_all(database, transaction, &id)?
        .ok_or_else(|| invalid_data("credential secret target is missing"))?;
    encode(&public(&c, &s, &t)?).map(Some)
}

pub(crate) fn touch(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    boundary: &Boundary,
    id: &str,
    used_at: f64,
    touch_if_before: f64,
) -> io::Result<Vec<u8>> {
    if !valid_timestamp(used_at) || !valid_timestamp(touch_if_before) || touch_if_before > used_at {
        return Err(invalid_input("invalid credential touch request"));
    }
    let Some((c, _s, mut t)) = read_all(database, transaction, id)? else {
        return encode(&json!({"touched":false}));
    };
    if c.owner_user_id != boundary.owner_user_id
        || c.tenant_id != boundary.tenant_label
        || t.disabled
        || t.revoked_at.is_some()
        || t.expires_at.is_some_and(|v| v <= used_at)
        || !account_active(database, transaction, &c)?
        || t.last_used_at.is_some_and(|v| v >= touch_if_before)
    {
        return encode(&json!({"touched":false}));
    }
    t.last_used_at = Some(used_at);
    write_state(database, transaction, &t)?;
    encode(&json!({"touched":true}))
}

pub(crate) fn update(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    boundary: &Boundary,
    id: &str,
    request: &UpdateRequest,
) -> io::Result<Option<Vec<u8>>> {
    let Some((c, mut s, mut t)) = read_all(database, transaction, id)? else {
        return Ok(None);
    };
    if c.owner_user_id != boundary.owner_user_id
        || c.tenant_id != boundary.tenant_label
        || t.revoked_at.is_some()
    {
        return Ok(None);
    };
    let mut settings_changed = false;
    if let Some(v) = &request.name {
        s.name = v.clone();
        settings_changed = true
    }
    if let Some(v) = &request.scopes {
        s.scopes = v.clone();
        settings_changed = true
    }
    if let Some(v) = request.rate_limit_rpm {
        s.rate_limit_rpm = v;
        settings_changed = true
    }
    if let Some(v) = request.rate_limit_tpd {
        s.rate_limit_tpd = v;
        settings_changed = true
    }
    if let Some(v) = &request.metadata {
        s.metadata = v.clone();
        settings_changed = true
    }
    if let Some(v) = request.expires_at {
        t.expires_at = v
    }
    if let Some(v) = request.disabled {
        t.disabled = v
    }
    if !valid_scopes(&s.scopes, false)
        || s.name.chars().count() > 80
        || !s.metadata.is_object()
        || t.expires_at.is_some_and(|v| !valid_timestamp(v))
        || request.physical_updated_at_ms == 0
    {
        return Err(invalid_input("invalid credential update request"));
    }
    if settings_changed {
        write_settings(database, transaction, &s, request.physical_updated_at_ms)?
    }
    if request.expires_at.is_some() || request.disabled.is_some() {
        write_state(database, transaction, &t)?
    }
    encode(&public(&c, &s, &t)?).map(Some)
}

pub(crate) fn revoke(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    boundary: &Boundary,
    id: &str,
    revoked_at: f64,
) -> io::Result<Vec<u8>> {
    if !valid_timestamp(revoked_at) {
        return Err(invalid_input("invalid credential revocation time"));
    }
    let Some((c, s, mut t)) = read_all(database, transaction, id)? else {
        return encode(&json!({"revoked":false,"metadata":{}}));
    };
    if c.owner_user_id != boundary.owner_user_id
        || c.tenant_id != boundary.tenant_label
        || t.revoked_at.is_some()
    {
        return encode(&json!({"revoked":false,"metadata":{}}));
    }
    t.disabled = true;
    t.revoked_at = Some(revoked_at);
    write_state(database, transaction, &t)?;
    database.entity_delete(
        transaction,
        index_key(transaction, boundary, c.created_at, id)?,
    )?;
    let count = read_count(database, transaction, boundary)?
        .checked_sub(1)
        .ok_or_else(|| invalid_data("credential owner count underflow"))?;
    write_count(database, transaction, boundary, count)?;
    encode(&json!({"revoked":true,"metadata":s.metadata}))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(id: &str, secret_hash: &str, created_at: f64) -> CreateRequest {
        CreateRequest {
            credential_id: id.to_owned(),
            boundary: Boundary {
                owner_user_id: 11,
                tenant_label: "personal".to_owned(),
            },
            account_user_id: String::new(),
            name: "agent key".to_owned(),
            prefix: "tf_live_".to_owned(),
            secret_hash: secret_hash.to_owned(),
            scopes: vec!["read".to_owned(), "write".to_owned()],
            rate_limit_rpm: 60,
            rate_limit_tpd: 1_000,
            created_at,
            expires_at: None,
            metadata: json!({"large": "x".repeat(20_000)}),
            physical_updated_at_ms: 1,
        }
    }

    #[test]
    fn authentication_and_touch_rewrite_only_compact_state() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let secret = "a".repeat(64);
        let create_request = request("credential-a", &secret, 100.0);
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        create(&database, &mut transaction, &create_request, false).unwrap();
        database.commit(transaction).unwrap();

        let mut before_transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let before_key = settings_key(&before_transaction, "credential-a").unwrap();
        let before = database
            .entity_get(&mut before_transaction, &before_key)
            .unwrap()
            .unwrap();

        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        authenticate(&database, &mut transaction, &secret, 200.0).unwrap();
        database.commit(transaction).unwrap();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                &touch(
                    &database,
                    &mut transaction,
                    &create_request.boundary,
                    "credential-a",
                    300.0,
                    250.0,
                )
                .unwrap(),
            )
            .unwrap(),
            json!({"touched": true})
        );
        database.commit(transaction).unwrap();

        let mut after_transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let after_key = settings_key(&after_transaction, "credential-a").unwrap();
        let after = database
            .entity_get(&mut after_transaction, &after_key)
            .unwrap()
            .unwrap();
        assert_eq!(after, before);
        let authenticated = validate(&database, &mut after_transaction, &secret, 400.0)
            .unwrap()
            .unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&authenticated).unwrap()["last_used_at"],
            300.0
        );
    }

    #[test]
    fn account_status_is_witnessed_by_credential_validation() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 1).unwrap();
        let account = crate::tenant_user::create(
            &database,
            &mut transaction,
            &crate::tenant_user::CreateRequest {
                user_id: "account-a".to_owned(),
                email: "account-a@example.com".to_owned(),
                password_hash: "sealed".to_owned(),
                display_name: "Account".to_owned(),
                role: "user".to_owned(),
                created_at: 10,
                metadata: json!({}),
                physical_updated_at_ms: 1,
            },
        )
        .unwrap();
        database.commit(transaction).unwrap();
        let owner = serde_json::from_slice::<Value>(&account).unwrap()["owner_user_id"]
            .as_u64()
            .unwrap();
        let secret = "c".repeat(64);
        let mut create_request = request("credential-bound", &secret, 20.0);
        create_request.boundary.owner_user_id = owner;
        create_request.account_user_id = "account-a".to_owned();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 1).unwrap();
        create(&database, &mut transaction, &create_request, false).unwrap();
        database.commit(transaction).unwrap();

        let mut transaction = database.begin_with_identity_claim_scopes(7, 1).unwrap();
        assert!(validate(&database, &mut transaction, &secret, 30.0)
            .unwrap()
            .is_some());
        let mut transaction = database.begin_with_identity_claim_scopes(7, 1).unwrap();
        crate::tenant_user::set_status(&database, &mut transaction, "account-a", "suspended")
            .unwrap();
        database.commit(transaction).unwrap();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 1).unwrap();
        assert!(validate(&database, &mut transaction, &secret, 30.0)
            .unwrap()
            .is_none());
    }
}
