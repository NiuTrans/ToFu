//! Owner/tenant-explicit receipt snapshots on the exclusive operator pipe.
//! Preserve hashed identity, request digest, exact stored response and commit
//! clock through the live atomic receipt/blob/family publisher. No ownership
//! inference or request-digest normalization happens at this boundary.

use serde::Deserialize;
use serde_json::{json, Value};
use std::io;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::MIGRATION_SESSION_MAX_PAGE_ROWS;
use crate::receipt::{ReceiptSnapshot, MAX_STORED_RECEIPT_BYTES};

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

pub(crate) fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

pub(crate) fn unhex(value: &str, maximum: usize) -> io::Result<Vec<u8>> {
    if value.len() > maximum * 2
        || !value.len().is_multiple_of(2)
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(invalid("invalid receipt snapshot hex"));
    }
    value
        .as_bytes()
        .chunks_exact(2)
        .map(|pair| {
            u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16)
                .map_err(|_| invalid("invalid receipt snapshot hex"))
        })
        .collect()
}

pub(crate) fn digest(value: &str) -> io::Result<[u8; 32]> {
    unhex(value, 32)?
        .try_into()
        .map_err(|_| invalid("invalid receipt snapshot digest"))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Identity {
    tenant_id: u64,
    owner_user_id: u64,
    command_key: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Snapshot {
    tenant_id: u64,
    owner_user_id: u64,
    command_key: String,
    operation: String,
    request_digest: String,
    response_hex: String,
    committed_at_ms: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Scan {
    tenant_id: u64,
    owner_user_id: u64,
    cursor: Option<String>,
}

fn scope(tx: &AuthorityTransaction, tenant: u64, owner: u64) -> io::Result<()> {
    if tenant != tx.tenant_id() || owner != tx.owner_user_id() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "receipt snapshot scope differs",
        ));
    }
    Ok(())
}

fn document(tx: &AuthorityTransaction, key: &[u8; 32], snapshot: ReceiptSnapshot) -> Value {
    json!({"tenant_id": tx.tenant_id(), "owner_user_id": tx.owner_user_id(),
           "command_key": hex(key), "operation": snapshot.operation,
           "request_digest": hex(&snapshot.request_digest),
           "response_hex": hex(&snapshot.encoded_response),
           "committed_at_ms": snapshot.committed_at_ms})
}

pub(crate) fn execute(
    database: &mut AuthorityDatabase,
    mut tx: AuthorityTransaction,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    match operation {
        "migration.command.receipt.restore" => {
            let input: Snapshot = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid receipt restore request"))?;
            scope(&tx, input.tenant_id, input.owner_user_id)?;
            let key = digest(&input.command_key)?;
            let snapshot = ReceiptSnapshot {
                operation: input.operation,
                request_digest: digest(&input.request_digest)?,
                encoded_response: unhex(&input.response_hex, MAX_STORED_RECEIPT_BYTES)?,
                committed_at_ms: input.committed_at_ms,
            };
            let inserted = database.receipt_restore_snapshot(&mut tx, key, snapshot)?;
            if inserted {
                database.commit(tx)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.command.receipt.get" => {
            let input: Identity = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid receipt get request"))?;
            scope(&tx, input.tenant_id, input.owner_user_id)?;
            let key = digest(&input.command_key)?;
            Ok(database
                .receipt_snapshot(&mut tx, &key)?
                .map(|snapshot| document(&tx, &key, snapshot))
                .unwrap_or(Value::Null))
        }
        "migration.command.receipt.scan" => {
            let input: Scan = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid receipt scan request"))?;
            scope(&tx, input.tenant_id, input.owner_user_id)?;
            let mut start = match input.cursor {
                None => Vec::new(),
                Some(cursor) => digest(
                    cursor
                        .strip_prefix("receipts:")
                        .ok_or_else(|| invalid("invalid receipt catalog cursor"))?,
                )?
                .to_vec(),
            };
            if !start.is_empty() {
                start.push(0);
            }
            let first = EntityKey::new(
                tx.tenant_id(),
                tx.owner_user_id(),
                "command_receipt",
                &start,
            )?;
            let (_, end) = EntityKey::prefix_range(
                tx.tenant_id(),
                tx.owner_user_id(),
                "command_receipt",
                b"",
            )?;
            let page =
                database.entity_scan(&mut tx, &first, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
            let mut rows = Vec::with_capacity(page.len());
            for (key, _) in &page {
                let raw: [u8; 32] = key.key_bytes().try_into().map_err(|_| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        "receipt catalog key is malformed",
                    )
                })?;
                rows.push(
                    json!({"tenant_id": tx.tenant_id(), "owner_user_id": tx.owner_user_id(),
                                 "command_key": hex(&raw)}),
                );
            }
            let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
                Some(format!(
                    "receipts:{}",
                    hex(page.last().unwrap().0.key_bytes())
                ))
            } else {
                None
            };
            Ok(json!({"rows": rows, "cursor": cursor}))
        }
        _ => Err(invalid("unknown receipt migration operation")),
    }
}

#[cfg(test)]
#[path = "receipt_migration_tests.rs"]
mod tests;
