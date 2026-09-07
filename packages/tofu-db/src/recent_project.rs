//! Owner-scoped recent-project navigation state.
//!
//! Project paths may exceed the Entity key bound, so documents use a
//! domain-separated digest key and retain the exact path in a blob-capable
//! versioned document. An exact count bounds full-list reads and lets clear
//! retire the complete physical namespace without walking every row.

use std::cmp::Reverse;
use std::io;

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_RECENT_PROJECTS_PER_OWNER, RECENT_PROJECT_COUNT_NAMESPACE,
    RECENT_PROJECT_DOCUMENT_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

const LOGICAL_NAMESPACE: &str = "recent_projects";
const COUNT_KEY: &[u8] = b"count";

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn resource_exhausted(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::OutOfMemory, message)
}

fn path_digest(path: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(b"tofu-db:recent-project-path:v1\0");
    hasher.update(path.as_bytes());
    let digest = hasher.finalize();
    let mut encoded = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write as _;
        write!(&mut encoded, "{byte:02x}").expect("writing to String cannot fail");
    }
    encoded
}

fn document_key(transaction: &AuthorityTransaction, path: &str) -> io::Result<(EntityKey, String)> {
    let digest = path_digest(path);
    Ok((
        EntityKey::new(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            RECENT_PROJECT_DOCUMENT_NAMESPACE,
            digest.as_bytes(),
        )?,
        digest,
    ))
}

fn document_range(transaction: &AuthorityTransaction) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        RECENT_PROJECT_DOCUMENT_NAMESPACE,
        b"",
    )
}

fn count_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        RECENT_PROJECT_COUNT_NAMESPACE,
        COUNT_KEY,
    )
}

fn read_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<usize> {
    let key = count_key(transaction)?;
    let Some(raw) = database.entity_get(transaction, &key)? else {
        return Ok(0);
    };
    let bytes: [u8; 8] = raw
        .try_into()
        .map_err(|_| invalid_data("recent-project count is malformed"))?;
    let count = usize::try_from(u64::from_le_bytes(bytes))
        .map_err(|_| invalid_data("recent-project count overflows this platform"))?;
    if count > MAX_RECENT_PROJECTS_PER_OWNER {
        return Err(invalid_data("recent-project count exceeds its bound"));
    }
    Ok(count)
}

fn write_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    count: usize,
) -> io::Result<()> {
    if count > MAX_RECENT_PROJECTS_PER_OWNER {
        return Err(resource_exhausted("recent-project capacity is exhausted"));
    }
    let key = count_key(transaction)?;
    database.entity_put(
        transaction,
        key,
        u64::try_from(count)
            .map_err(|_| invalid_input("recent-project count overflow"))?
            .to_le_bytes()
            .to_vec(),
    )
}

fn projected_value(raw: &[u8], expected_path: Option<&str>) -> io::Result<Value> {
    let projected: Value = serde_json::from_slice(raw)
        .map_err(|_| invalid_data("recent-project projection is malformed"))?;
    let value = projected
        .get("value")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid_data("recent-project value is not an object"))?;
    let path = value
        .get("path")
        .and_then(Value::as_str)
        .filter(|path| !path.is_empty())
        .ok_or_else(|| invalid_data("recent-project path is malformed"))?;
    if expected_path.is_some_and(|expected| path != expected) {
        return Err(invalid_data("recent-project digest collision"));
    }
    let count = value
        .get("count")
        .and_then(Value::as_u64)
        .filter(|count| *count > 0)
        .ok_or_else(|| invalid_data("recent-project use count is malformed"))?;
    let last_used = value
        .get("last_used")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("recent-project timestamp is malformed"))?;
    Ok(json!({"path": path, "count": count, "last_used": last_used}))
}

fn touch_one(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    path: &str,
    last_used: u64,
    updated_at_ms: u64,
) -> io::Result<Value> {
    let (key, logical_key) = document_key(transaction, path)?;
    let current =
        versioned_document::get(database, transaction, &key, LOGICAL_NAMESPACE, &logical_key)?;
    let (current_count, is_new) = match current {
        Some(raw) => {
            let value = projected_value(&raw, Some(path))?;
            (value["count"].as_u64().unwrap(), false)
        }
        None => (0, true),
    };
    if is_new {
        let count = read_count(database, transaction)?;
        write_count(
            database,
            transaction,
            count
                .checked_add(1)
                .ok_or_else(|| resource_exhausted("recent-project capacity is exhausted"))?,
        )?;
    }
    let use_count = current_count
        .checked_add(1)
        .ok_or_else(|| invalid_data("recent-project use count overflow"))?;
    let value = json!({"path": path, "count": use_count, "last_used": last_used});
    versioned_document::put(
        database,
        transaction,
        PutRequest {
            key,
            namespace: LOGICAL_NAMESPACE.to_owned(),
            logical_key,
            value_json: serde_json::to_vec(&value)
                .map_err(|_| invalid_input("recent-project value cannot be encoded"))?,
            expected_version: None,
            updated_at_ms,
        },
    )?;
    Ok(value)
}

pub(crate) fn touch(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    path: &str,
    last_used: u64,
    updated_at_ms: u64,
) -> io::Result<Vec<u8>> {
    serde_json::to_vec(&touch_one(
        database,
        transaction,
        path,
        last_used,
        updated_at_ms,
    )?)
    .map_err(|_| invalid_data("recent-project response cannot be encoded"))
}

pub(crate) fn touch_many(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    paths: &[String],
    last_used: u64,
    updated_at_ms: u64,
) -> io::Result<Vec<u8>> {
    for path in paths {
        touch_one(database, transaction, path, last_used, updated_at_ms)?;
    }
    serde_json::to_vec(&json!({"touched": paths.len()}))
        .map_err(|_| invalid_data("recent-project batch response cannot be encoded"))
}

pub(crate) fn list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Vec<u8>> {
    let expected_count = read_count(database, transaction)?;
    let (start, end) = document_range(transaction)?;
    let raw = versioned_document::list(
        database,
        transaction,
        &start,
        &end,
        LOGICAL_NAMESPACE,
        MAX_RECENT_PROJECTS_PER_OWNER,
    )?;
    let documents: Vec<Value> = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("recent-project list projection is malformed"))?;
    if documents.len() != expected_count {
        return Err(invalid_data(
            "recent-project count does not match its documents",
        ));
    }
    let mut projects = documents
        .iter()
        .map(|document| {
            let raw = serde_json::to_vec(document)
                .map_err(|_| invalid_data("recent-project document cannot be encoded"))?;
            projected_value(&raw, None)
        })
        .collect::<io::Result<Vec<_>>>()?;
    projects.sort_by(|left, right| left["path"].as_str().cmp(&right["path"].as_str()));
    projects.sort_by_key(|project| Reverse(project["last_used"].as_u64().unwrap()));
    serde_json::to_vec(&projects)
        .map_err(|_| invalid_data("recent-project list response cannot be encoded"))
}

pub(crate) fn relink(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    old_path: &str,
    new_path: &str,
    updated_at_ms: u64,
) -> io::Result<Value> {
    let (old_key, old_logical_key) = document_key(transaction, old_path)?;
    let current = versioned_document::get(
        database,
        transaction,
        &old_key,
        LOGICAL_NAMESPACE,
        &old_logical_key,
    )?
    .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "Old path is not in recent projects"))?;
    let value = projected_value(&current, Some(old_path))?;
    let mut count = value["count"].as_u64().unwrap();
    let mut last_used = value["last_used"].as_u64().unwrap();

    let (new_key, new_logical_key) = document_key(transaction, new_path)?;
    let existing = versioned_document::get(
        database,
        transaction,
        &new_key,
        LOGICAL_NAMESPACE,
        &new_logical_key,
    )?;
    if let Some(existing) = existing {
        let existing_value = projected_value(&existing, Some(new_path))?;
        count = count
            .checked_add(existing_value["count"].as_u64().unwrap())
            .ok_or_else(|| invalid_data("recent-project use count overflow"))?;
        last_used = last_used.max(existing_value["last_used"].as_u64().unwrap());
        let total = read_count(database, transaction)?;
        write_count(
            database,
            transaction,
            total
                .checked_sub(1)
                .ok_or_else(|| invalid_data("recent-project count underflow"))?,
        )?;
    }
    let new_value = json!({"path": new_path, "count": count, "last_used": last_used});
    versioned_document::put(
        database,
        transaction,
        PutRequest {
            key: new_key,
            namespace: LOGICAL_NAMESPACE.to_owned(),
            logical_key: new_logical_key,
            value_json: serde_json::to_vec(&new_value)
                .map_err(|_| invalid_input("recent-project value cannot be encoded"))?,
            expected_version: None,
            updated_at_ms,
        },
    )?;
    versioned_document::delete(
        database,
        transaction,
        old_key,
        LOGICAL_NAMESPACE,
        &old_logical_key,
        None,
    )?;
    Ok(new_value)
}

pub(crate) fn clear(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Vec<u8>> {
    let count = read_count(database, transaction)?;
    if count > 0 {
        let (start, end) = document_range(transaction)?;
        database.entity_retire_range(transaction, &start, &end)?;
        write_count(database, transaction, 0)?;
    }
    serde_json::to_vec(&json!({"deleted": count}))
        .map_err(|_| invalid_data("recent-project clear response cannot be encoded"))
}
