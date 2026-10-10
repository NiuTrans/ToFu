//! Global legacy command identities retained within one explicit target tenant.
//! Source UUID and bounded accounting publish atomically with the existing
//! receipt/blob family. Replay requires an owner witness from the exact hashed
//! request payload; unknown ownership preserves conflict without returning data.
use crate::authority::AuthorityDatabase;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::LEGACY_RECEIPT_OWNER_ID;
use crate::generated_tofudb_ir::{MAX_LEGACY_RECEIPT_ROWS, MAX_LEGACY_RECEIPT_STORED_BYTES};
use crate::receipt::{command_receipt_key, ReceiptSnapshot};
use std::io;
use uuid::Uuid;

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn conflict() -> io::Error {
    io::Error::new(
        io::ErrorKind::WouldBlock,
        "legacy command identity or replay scope differs",
    )
}
fn metadata_key(tenant: u64) -> io::Result<EntityKey> {
    EntityKey::new(
        tenant,
        LEGACY_RECEIPT_OWNER_ID,
        "legacy_receipt_source",
        b"authority",
    )
}
fn decode_metadata(raw: &[u8]) -> io::Result<(Uuid, u64, u64)> {
    if raw.len() != 40 || &raw[..8] != b"TDBLRS01" {
        return Err(invalid("invalid legacy receipt source metadata"));
    }
    let source =
        Uuid::from_slice(&raw[8..24]).map_err(|_| invalid("invalid legacy receipt source UUID"))?;
    let count = u64::from_le_bytes(raw[24..32].try_into().unwrap());
    let bytes = u64::from_le_bytes(raw[32..40].try_into().unwrap());
    if source.is_nil()
        || count == 0
        || count > MAX_LEGACY_RECEIPT_ROWS
        || bytes > MAX_LEGACY_RECEIPT_STORED_BYTES
    {
        return Err(invalid("legacy receipt source metadata exceeds bounds"));
    }
    Ok((source, count, bytes))
}

pub(crate) fn restore(
    database: &mut AuthorityDatabase,
    tenant: u64,
    source: Uuid,
    key: [u8; 32],
    snapshot: ReceiptSnapshot,
) -> io::Result<bool> {
    if source.is_nil() {
        return Err(invalid("invalid legacy receipt source UUID"));
    }
    let mut tx = database.begin(tenant, LEGACY_RECEIPT_OWNER_ID)?;
    let metadata = metadata_key(tenant)?;
    let previous = database.entity_get(&mut tx, &metadata)?;
    let (count, bytes) = match previous.as_deref() {
        Some(raw) => {
            let (existing, count, bytes) = decode_metadata(raw)?;
            if existing != source {
                return Err(conflict());
            }
            (count, bytes)
        }
        None => (0, 0),
    };
    let size = snapshot.encoded_response.len() as u64;
    if let Some(existing) = database.receipt_snapshot(&mut tx, &key)? {
        if previous.is_none() {
            return Err(invalid("legacy receipt has no source metadata"));
        }
        if existing != snapshot {
            return Err(conflict());
        }
        database.receipt_restore_snapshot(&mut tx, key, snapshot)?;
        return Ok(false);
    }
    let count = count
        .checked_add(1)
        .filter(|v| *v <= MAX_LEGACY_RECEIPT_ROWS)
        .ok_or_else(|| invalid("legacy receipt row budget exhausted"))?;
    let bytes = bytes
        .checked_add(size)
        .filter(|v| *v <= MAX_LEGACY_RECEIPT_STORED_BYTES)
        .ok_or_else(|| invalid("legacy receipt byte budget exhausted"))?;
    database.receipt_restore_snapshot(&mut tx, key, snapshot)?;
    let mut encoded = b"TDBLRS01".to_vec();
    encoded.extend_from_slice(source.as_bytes());
    encoded.extend_from_slice(&count.to_le_bytes());
    encoded.extend_from_slice(&bytes.to_le_bytes());
    database.entity_put(&mut tx, metadata, encoded)?;
    database.commit(tx)?;
    Ok(true)
}

pub(crate) fn lookup(
    database: &AuthorityDatabase,
    tenant: u64,
    owner: u64,
    command_id: &str,
    operation: &str,
    request_digest: [u8; 32],
    owner_bound: bool,
) -> io::Result<Option<Vec<u8>>> {
    let mut tx = database.begin(tenant, LEGACY_RECEIPT_OWNER_ID)?;
    let key = EntityKey::new(
        tenant,
        LEGACY_RECEIPT_OWNER_ID,
        "command_receipt",
        &command_receipt_key(command_id)?,
    )?;
    if database.entity_get(&mut tx, &key)?.is_none() {
        return Ok(None);
    }
    let metadata = database
        .entity_get(&mut tx, &metadata_key(tenant)?)?
        .ok_or_else(|| invalid("legacy receipt has no source metadata"))?;
    decode_metadata(&metadata)?;
    if !owner_bound || owner == 0 || owner >= LEGACY_RECEIPT_OWNER_ID {
        return Err(conflict());
    }
    database.receipt_lookup(&mut tx, command_id, operation, request_digest)
}

pub(crate) mod migration;

#[cfg(test)]
mod tests;
