//! Tenant-global account authority with split immutable profiles and compact mutable state.
//!
//! Email and account identifiers are tenant-global claims. Large metadata lives in a
//! blob-capable profile, while role, status, and login updates touch only a small entity.

use std::io;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_TENANT_USER_DOCUMENT_BYTES, MAX_TENANT_USER_LIST_ROWS, MAX_TENANT_USER_LIST_SCAN_ROWS,
    MAX_TENANT_USER_RESPONSE_BYTES, TENANT_USER_CREATED_INDEX_NAMESPACE,
    TENANT_USER_EMAIL_INDEX_NAMESPACE, TENANT_USER_OWNER_SEQUENCE_NAMESPACE,
    TENANT_USER_PROFILE_NAMESPACE, TENANT_USER_STATE_NAMESPACE, TENANT_USER_STATUS_INDEX_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

const LOGICAL_NAMESPACE: &str = "tenant_users";
const OWNER_SEQUENCE_KEY: &[u8] = b"owner_user_id";

#[derive(Clone, Debug)]
pub struct CreateRequest {
    pub user_id: String,
    pub email: String,
    pub password_hash: String,
    pub display_name: String,
    pub role: String,
    pub created_at: u64,
    pub metadata: Value,
    pub physical_updated_at_ms: u64,
}

#[derive(Clone, Debug)]
pub enum Selector {
    UserId(String),
    Email(String),
}

#[derive(Clone, Debug)]
pub struct ListRequest {
    pub status: Option<String>,
    pub limit: usize,
    pub offset: usize,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Profile {
    id: String,
    email: String,
    password_hash: String,
    display_name: String,
    created_at: u64,
    email_verified: bool,
    metadata: Value,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct State {
    id: String,
    owner_user_id: u64,
    role: String,
    status: String,
    last_login_at: u64,
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

fn profile_key(transaction: &AuthorityTransaction, user_id: &str) -> io::Result<EntityKey> {
    global_key(
        transaction,
        TENANT_USER_PROFILE_NAMESPACE,
        user_id.as_bytes(),
    )
}

fn state_key(transaction: &AuthorityTransaction, user_id: &str) -> io::Result<EntityKey> {
    global_key(transaction, TENANT_USER_STATE_NAMESPACE, user_id.as_bytes())
}

fn email_digest(email: &str) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(b"tofu-db:tenant-user-email:v1\0");
    hasher.update(email.as_bytes());
    hasher.finalize().into()
}

fn email_key(transaction: &AuthorityTransaction, email: &str) -> io::Result<EntityKey> {
    global_key(
        transaction,
        TENANT_USER_EMAIL_INDEX_NAMESPACE,
        &email_digest(email),
    )
}

fn descending_identity(created_at: u64, user_id: &str) -> Vec<u8> {
    let mut raw = Vec::with_capacity(9 + user_id.len() * 2);
    raw.extend_from_slice(&(!created_at).to_be_bytes());
    for byte in user_id.bytes() {
        raw.extend_from_slice(&[!byte, 0]);
    }
    raw.push(u8::MAX);
    raw
}

fn created_key(
    transaction: &AuthorityTransaction,
    created_at: u64,
    user_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = vec![b'u'];
    raw.extend_from_slice(&descending_identity(created_at, user_id));
    global_key(transaction, TENANT_USER_CREATED_INDEX_NAMESPACE, &raw)
}

fn status_key(
    transaction: &AuthorityTransaction,
    status: &str,
    created_at: u64,
    user_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity(status.len() + 1 + 9 + user_id.len() * 2);
    raw.extend_from_slice(status.as_bytes());
    raw.push(0);
    raw.extend_from_slice(&descending_identity(created_at, user_id));
    global_key(transaction, TENANT_USER_STATUS_INDEX_NAMESPACE, &raw)
}

fn valid_role(value: &str) -> bool {
    matches!(value, "user" | "admin")
}
fn valid_status(value: &str) -> bool {
    matches!(value, "active" | "suspended" | "deleted")
}

fn read_profile(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    user_id: &str,
) -> io::Result<Option<Profile>> {
    versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &profile_key(transaction, user_id)?,
        LOGICAL_NAMESPACE,
        user_id,
        TENANT_GLOBAL_OWNER_ID,
        MAX_TENANT_USER_DOCUMENT_BYTES,
    )?
    .map(|raw| {
        serde_json::from_slice(&raw).map_err(|_| invalid_data("tenant user profile is malformed"))
    })
    .transpose()
}

fn read_state(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    user_id: &str,
) -> io::Result<Option<State>> {
    database
        .entity_get(transaction, &state_key(transaction, user_id)?)?
        .map(|raw| {
            serde_json::from_slice(&raw).map_err(|_| invalid_data("tenant user state is malformed"))
        })
        .transpose()
}

pub(crate) fn account_is_active_owner(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    user_id: &str,
    owner_user_id: u64,
) -> io::Result<bool> {
    Ok(read_state(database, transaction, user_id)?
        .is_some_and(|state| state.owner_user_id == owner_user_id && state.status == "active"))
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
            .map_err(|_| invalid_data("tenant user state cannot be encoded"))?,
    )
}

fn public_value(profile: &Profile, state: &State) -> io::Result<Value> {
    if profile.id != state.id
        || state.owner_user_id < 2
        || !valid_role(&state.role)
        || !valid_status(&state.status)
        || !profile.metadata.is_object()
    {
        return Err(invalid_data("tenant user profile and state disagree"));
    }
    Ok(json!({
        "id": profile.id, "owner_user_id": state.owner_user_id, "email": profile.email,
        "display_name": profile.display_name, "role": state.role, "status": state.status,
        "created_at": profile.created_at, "last_login_at": state.last_login_at,
        "email_verified": profile.email_verified, "metadata": profile.metadata,
    }))
}

fn encode(value: &Value) -> io::Result<Vec<u8>> {
    let encoded = serde_json::to_vec(value)
        .map_err(|_| invalid_data("tenant user response cannot be encoded"))?;
    if encoded.len() > MAX_TENANT_USER_RESPONSE_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "tenant user response exceeds 8 MiB",
        ));
    }
    Ok(encoded)
}

fn read_public(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    user_id: &str,
) -> io::Result<Option<Value>> {
    let Some(profile) = read_profile(database, transaction, user_id)? else {
        return Ok(None);
    };
    let state = read_state(database, transaction, user_id)?
        .ok_or_else(|| invalid_data("tenant user state is missing"))?;
    public_value(&profile, &state).map(Some)
}

fn resolve_email(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    email: &str,
) -> io::Result<Option<String>> {
    let Some(raw) = database.entity_get(transaction, &email_key(transaction, email)?)? else {
        return Ok(None);
    };
    let value: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("tenant user email index is malformed"))?;
    if value["email"] != email {
        return Err(invalid_data("tenant user email digest collision"));
    }
    value["user_id"]
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| invalid_data("tenant user email index ID is malformed"))
        .map(Some)
}

fn allocate_owner(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<u64> {
    let key = global_key(
        transaction,
        TENANT_USER_OWNER_SEQUENCE_NAMESPACE,
        OWNER_SEQUENCE_KEY,
    )?;
    let next = match database.entity_get(transaction, &key)? {
        None => 2,
        Some(raw) if raw.len() == 8 => u64::from_le_bytes(raw.try_into().unwrap()),
        Some(_) => return Err(invalid_data("tenant user owner sequence is malformed")),
    };
    if next < 2 {
        return Err(invalid_data("tenant user owner sequence is invalid"));
    }
    database.entity_put(
        transaction,
        key,
        next.checked_add(1)
            .ok_or_else(|| invalid_data("tenant user owner sequence overflow"))?
            .to_le_bytes()
            .to_vec(),
    )?;
    Ok(next)
}

pub(crate) fn create(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &CreateRequest,
) -> io::Result<Vec<u8>> {
    if request.user_id.is_empty()
        || request.user_id.chars().count() > 256
        || request.email.chars().count() > 320
        || request.password_hash.chars().count() > 512
        || request.display_name.chars().count() > 256
        || !valid_role(&request.role)
        || !request.metadata.is_object()
        || request.physical_updated_at_ms == 0
    {
        return Err(invalid_input("invalid tenant user create request"));
    }
    if database
        .entity_get(transaction, &profile_key(transaction, &request.user_id)?)?
        .is_some()
    {
        return Err(conflict("tenant user ID already exists"));
    }
    let email_claim = email_key(transaction, &request.email)?;
    if database.entity_get(transaction, &email_claim)?.is_some() {
        return Err(conflict("tenant user email already exists"));
    }
    let owner_user_id = allocate_owner(database, transaction)?;
    let profile = Profile {
        id: request.user_id.clone(),
        email: request.email.clone(),
        password_hash: request.password_hash.clone(),
        display_name: request.display_name.clone(),
        created_at: request.created_at,
        email_verified: false,
        metadata: request.metadata.clone(),
    };
    let state = State {
        id: request.user_id.clone(),
        owner_user_id,
        role: request.role.clone(),
        status: "active".to_owned(),
        last_login_at: 0,
    };
    let value_json = serde_json::to_vec(&profile)
        .map_err(|_| invalid_input("tenant user profile cannot be encoded"))?;
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: profile_key(transaction, &request.user_id)?,
            namespace: LOGICAL_NAMESPACE.to_owned(),
            logical_key: request.user_id.clone(),
            value_json,
            expected_version: Some(0),
            updated_at_ms: request.physical_updated_at_ms,
        },
        TENANT_GLOBAL_OWNER_ID,
        MAX_TENANT_USER_DOCUMENT_BYTES,
    )?;
    write_state(database, transaction, &state)?;
    database.entity_put(
        transaction,
        email_claim,
        serde_json::to_vec(&json!({"email": request.email, "user_id": request.user_id})).unwrap(),
    )?;
    database.entity_put(
        transaction,
        created_key(transaction, request.created_at, &request.user_id)?,
        request.user_id.as_bytes().to_vec(),
    )?;
    database.entity_put(
        transaction,
        status_key(transaction, "active", request.created_at, &request.user_id)?,
        request.user_id.as_bytes().to_vec(),
    )?;
    encode(&public_value(&profile, &state)?)
}

pub(crate) fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    selector: &Selector,
) -> io::Result<Option<Vec<u8>>> {
    let user_id = match selector {
        Selector::UserId(id) => Some(id.clone()),
        Selector::Email(email) => resolve_email(database, transaction, email)?,
    };
    user_id
        .map(|id| read_public(database, transaction, &id))
        .transpose()?
        .flatten()
        .map(|value| encode(&value))
        .transpose()
}

pub(crate) fn authentication(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    email: &str,
) -> io::Result<Option<Vec<u8>>> {
    let Some(user_id) = resolve_email(database, transaction, email)? else {
        return Ok(None);
    };
    let profile = read_profile(database, transaction, &user_id)?
        .ok_or_else(|| invalid_data("tenant user email profile is missing"))?;
    let state = read_state(database, transaction, &user_id)?
        .ok_or_else(|| invalid_data("tenant user state is missing"))?;
    encode(
        &json!({"user": public_value(&profile, &state)?, "password_hash": profile.password_hash}),
    )
    .map(Some)
}

pub(crate) fn list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &ListRequest,
) -> io::Result<Vec<u8>> {
    if !(1..=MAX_TENANT_USER_LIST_ROWS).contains(&request.limit)
        || request
            .offset
            .checked_add(request.limit)
            .is_none_or(|n| n > MAX_TENANT_USER_LIST_SCAN_ROWS)
    {
        return Err(invalid_input("tenant user list exceeds its bounded scan"));
    }
    let (namespace, prefix) = match &request.status {
        Some(status) => {
            if !valid_status(status) {
                return Err(invalid_input("invalid tenant user status"));
            }
            (TENANT_USER_STATUS_INDEX_NAMESPACE, {
                let mut p = status.as_bytes().to_vec();
                p.push(0);
                p
            })
        }
        None => (TENANT_USER_CREATED_INDEX_NAMESPACE, vec![b'u']),
    };
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        namespace,
        &prefix,
    )?;
    let rows = database.entity_scan(transaction, &start, &end, request.offset + request.limit)?;
    let mut values = Vec::with_capacity(rows.len().saturating_sub(request.offset));
    let mut response_bytes = 2_usize;
    for (_, raw) in rows.into_iter().skip(request.offset) {
        let user_id = std::str::from_utf8(&raw)
            .map_err(|_| invalid_data("tenant user list index is malformed"))?;
        let value = read_public(database, transaction, user_id)?
            .ok_or_else(|| invalid_data("tenant user list profile is missing"))?;
        let value_bytes = serde_json::to_vec(&value)
            .map_err(|_| invalid_data("tenant user list row cannot be encoded"))?
            .len();
        response_bytes = response_bytes
            .checked_add(value_bytes + usize::from(!values.is_empty()))
            .filter(|bytes| *bytes <= MAX_TENANT_USER_RESPONSE_BYTES)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "tenant user list response exceeds 8 MiB",
                )
            })?;
        values.push(value);
    }
    encode(&Value::Array(values))
}

fn update_state(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    user_id: &str,
    role: Option<&str>,
    status: Option<&str>,
    last_login_at: Option<u64>,
) -> io::Result<Option<Vec<u8>>> {
    let Some(mut state) = read_state(database, transaction, user_id)? else {
        return Ok(None);
    };
    let profile = read_profile(database, transaction, user_id)?
        .ok_or_else(|| invalid_data("tenant user profile is missing"))?;
    if let Some(role) = role {
        if !valid_role(role) {
            return Err(invalid_input("invalid tenant user role"));
        }
        state.role = role.to_owned();
    }
    if let Some(status) = status {
        if !valid_status(status) {
            return Err(invalid_input("invalid tenant user status"));
        }
        database.entity_delete(
            transaction,
            status_key(transaction, &state.status, profile.created_at, user_id)?,
        )?;
        state.status = status.to_owned();
        database.entity_put(
            transaction,
            status_key(transaction, status, profile.created_at, user_id)?,
            user_id.as_bytes().to_vec(),
        )?;
    }
    if let Some(value) = last_login_at {
        state.last_login_at = value;
    }
    write_state(database, transaction, &state)?;
    encode(&public_value(&profile, &state)?).map(Some)
}

pub(crate) fn set_role(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    user_id: &str,
    role: &str,
) -> io::Result<Option<Vec<u8>>> {
    update_state(database, transaction, user_id, Some(role), None, None)
}
pub(crate) fn set_status(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    user_id: &str,
    status: &str,
) -> io::Result<Option<Vec<u8>>> {
    update_state(database, transaction, user_id, None, Some(status), None)
}
pub(crate) fn record_login(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    user_id: &str,
    last_login_at: u64,
) -> io::Result<Vec<u8>> {
    let updated = update_state(
        database,
        transaction,
        user_id,
        None,
        None,
        Some(last_login_at),
    )?
    .is_some();
    encode(&json!({"updated": updated}))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(user_id: &str, email: &str, created_at: u64) -> CreateRequest {
        CreateRequest {
            user_id: user_id.to_owned(),
            email: email.to_owned(),
            password_hash: String::new(),
            display_name: String::new(),
            role: "user".to_owned(),
            created_at,
            metadata: json!({}),
            physical_updated_at_ms: 1,
        }
    }

    #[test]
    fn account_round_trip_and_mutable_state_do_not_rewrite_profile() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut create_tx = database.begin_with_identity_claim_scopes(7, 1).unwrap();
        let response = create(
            &database,
            &mut create_tx,
            &CreateRequest {
                user_id: "account-a".to_owned(),
                email: "owner@example.com".to_owned(),
                password_hash: "secret-hash".to_owned(),
                display_name: "  Owner  ".to_owned(),
                role: "user".to_owned(),
                created_at: 100,
                metadata: json!({"large": "x".repeat(16_000)}),
                physical_updated_at_ms: 1,
            },
        )
        .unwrap();
        assert!(!String::from_utf8(response).unwrap().contains("secret-hash"));
        database.commit(create_tx).unwrap();

        let mut update_tx = database.begin_with_identity_claim_scopes(7, 1).unwrap();
        let stored_profile_key = profile_key(&update_tx, "account-a").unwrap();
        let profile_before = database
            .entity_get(&mut update_tx, &stored_profile_key)
            .unwrap()
            .unwrap();
        let updated = set_status(&database, &mut update_tx, "account-a", "suspended")
            .unwrap()
            .unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&updated).unwrap()["status"],
            "suspended"
        );
        let profile_after = database
            .entity_get(&mut update_tx, &stored_profile_key)
            .unwrap()
            .unwrap();
        assert_eq!(profile_before, profile_after);
        database.commit(update_tx).unwrap();

        let mut read_tx = database.begin_with_identity_claim_scopes(7, 1).unwrap();
        let found = get(
            &database,
            &mut read_tx,
            &Selector::Email("owner@example.com".to_owned()),
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&found).unwrap()["status"],
            "suspended"
        );
    }

    #[test]
    fn owner_allocation_email_uniqueness_and_descending_indexes_are_exact() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for candidate in [
            request("account-a", "a@example.com", 100),
            request("account-b", "b@example.com", 200),
        ] {
            let mut transaction = database.begin_with_identity_claim_scopes(7, 1).unwrap();
            create(&database, &mut transaction, &candidate).unwrap();
            database.commit(transaction).unwrap();
        }
        let mut read = database.begin_with_identity_claim_scopes(7, 1).unwrap();
        let listed = list(
            &database,
            &mut read,
            &ListRequest {
                status: None,
                limit: 10,
                offset: 0,
            },
        )
        .unwrap();
        let listed: Value = serde_json::from_slice(&listed).unwrap();
        assert_eq!(listed[0]["id"], "account-b");
        assert_eq!(listed[0]["owner_user_id"], 3);
        assert_eq!(listed[1]["id"], "account-a");
        assert_eq!(listed[1]["owner_user_id"], 2);
        drop(read);

        let mut duplicate = database.begin_with_identity_claim_scopes(7, 1).unwrap();
        let error = create(
            &database,
            &mut duplicate,
            &request("account-c", "a@example.com", 300),
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::AlreadyExists);
    }
}
