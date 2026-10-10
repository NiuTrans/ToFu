//! Exclusive operator import and independent source-fenced receipt catalogs.
//! The operator authorizes the tenant; its owner is never a historical owner.
use super::*;
use crate::generated_tofudb_ir::MIGRATION_SESSION_MAX_PAGE_ROWS;
use crate::receipt::MAX_STORED_RECEIPT_BYTES;
use crate::receipt_migration::{digest, hex, unhex};
use serde::Deserialize;
use serde_json::{json, Value};

#[derive(Deserialize)]
struct Request {
    tenant_id: u64,
    source_authority_uuid: String,
    #[serde(flatten)]
    action: Action,
}

#[derive(Deserialize)]
#[serde(untagged)]
enum Action {
    Restore {
        command_key: String,
        operation: String,
        request_digest: String,
        response_hex: String,
        committed_at_ms: u64,
    },
    Get {
        command_key: String,
    },
    Scan {
        cursor: Option<String>,
    },
}

pub(crate) fn execute(
    database: &mut AuthorityDatabase,
    tenant: u64,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    // Validate per-operation keys before deserialization: untagged variants
    // otherwise accept a shorter shape and silently discard restore fields.
    let fields: &[&str] = match operation {
        "migration.legacy.receipt.restore" => &[
            "command_key",
            "operation",
            "request_digest",
            "response_hex",
            "committed_at_ms",
        ],
        "migration.legacy.receipt.get" => &["command_key"],
        "migration.legacy.receipt.scan" => &["cursor"],
        "migration.legacy.receipt.stats" => &[],
        _ => return Err(invalid("unknown legacy receipt migration operation")),
    };
    let object = payload
        .as_object()
        .ok_or_else(|| invalid("invalid legacy receipt request"))?;
    if object.keys().any(|key| {
        key != "tenant_id" && key != "source_authority_uuid" && !fields.contains(&key.as_str())
    }) {
        return Err(invalid("unknown legacy receipt request field"));
    }
    let input: Request =
        serde_json::from_value(payload).map_err(|_| invalid("invalid legacy receipt request"))?;
    if input.tenant_id != tenant {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "legacy receipt tenant differs",
        ));
    }
    let source = Uuid::parse_str(&input.source_authority_uuid)
        .map_err(|_| invalid("invalid legacy receipt source UUID"))?;
    if source.is_nil() {
        return Err(invalid("invalid legacy receipt source UUID"));
    }
    if let Action::Restore {
        command_key,
        operation: stored_operation,
        request_digest,
        response_hex,
        committed_at_ms,
    } = input.action
    {
        if operation != "migration.legacy.receipt.restore" {
            return Err(invalid("invalid legacy receipt action"));
        }
        let inserted = restore(
            database,
            tenant,
            source,
            digest(&command_key)?,
            ReceiptSnapshot {
                operation: stored_operation,
                request_digest: digest(&request_digest)?,
                encoded_response: unhex(&response_hex, MAX_STORED_RECEIPT_BYTES)?,
                committed_at_ms,
            },
        )?;
        return Ok(json!({"inserted": inserted}));
    }
    let mut tx = database.begin(tenant, LEGACY_RECEIPT_OWNER_ID)?;
    let metadata = database.entity_get(&mut tx, &metadata_key(tenant)?)?;
    let (count, bytes) = match metadata.as_deref() {
        Some(raw) => {
            let (existing, count, bytes) = decode_metadata(raw)?;
            if existing != source {
                return Err(conflict());
            }
            (count, bytes)
        }
        None => (0, 0),
    };
    match (operation, input.action) {
        ("migration.legacy.receipt.get", Action::Get { command_key }) => {
            let snapshot = database.receipt_snapshot(&mut tx, &digest(&command_key)?)?;
            if snapshot.is_some() && metadata.is_none() {
                return Err(invalid("legacy receipt has no source metadata"));
            }
            Ok(snapshot.map(|row| json!({"tenant_id":tenant, "source_authority_uuid":source.to_string(),
                "command_key":command_key, "operation":row.operation, "request_digest":hex(&row.request_digest),
                "response_hex":hex(&row.encoded_response), "committed_at_ms":row.committed_at_ms})).unwrap_or(Value::Null))
        }
        ("migration.legacy.receipt.stats", Action::Scan { cursor: None }) => Ok(
            json!({"tenant_id":tenant,"source_authority_uuid":source.to_string(),"rows":count,"stored_bytes":bytes}),
        ),
        ("migration.legacy.receipt.scan", Action::Scan { cursor }) => {
            let mut start = match cursor {
                None => Vec::new(),
                Some(value) => {
                    let prefix = format!("legacy-receipts:{source}:");
                    digest(
                        value
                            .strip_prefix(&prefix)
                            .ok_or_else(|| invalid("invalid legacy receipt cursor"))?,
                    )?
                    .to_vec()
                }
            };
            if !start.is_empty() {
                start.push(0);
            }
            let first = EntityKey::new(tenant, LEGACY_RECEIPT_OWNER_ID, "command_receipt", &start)?;
            let (_, end) =
                EntityKey::prefix_range(tenant, LEGACY_RECEIPT_OWNER_ID, "command_receipt", b"")?;
            let page =
                database.entity_scan(&mut tx, &first, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
            if !page.is_empty() && metadata.is_none() {
                return Err(invalid("legacy receipt has no source metadata"));
            }
            let mut rows = Vec::with_capacity(page.len());
            for (key, _) in &page {
                if key.key_bytes().len() != 32 {
                    return Err(invalid("invalid legacy receipt catalog key"));
                }
                rows.push(json!({"tenant_id":tenant,"source_authority_uuid":source.to_string(),"command_key":hex(key.key_bytes())}));
            }
            let cursor = (page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS).then(|| {
                format!(
                    "legacy-receipts:{source}:{}",
                    hex(page.last().unwrap().0.key_bytes())
                )
            });
            Ok(json!({"rows":rows,"cursor":cursor}))
        }
        _ => Err(invalid("invalid legacy receipt action")),
    }
}
