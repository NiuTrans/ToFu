//! Exclusive offline trash import: one owner-scoped pending capsule, a durable
//! seal, then range-pin publication from that committed seal. Public serving
//! refuses pending state. Queries independently inspect the pinned history.
use super::*;
use crate::generated_tofudb_ir::{
    MAX_MIGRATION_TRASH_STAGE_BYTES, MAX_MIGRATION_TRASH_TURNS, MIGRATION_SESSION_MAX_PAGE_ROWS,
    MIGRATION_TRASH_STAGE_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};
use serde::{Deserialize, Serialize};

#[derive(Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Source {
    header: Value,
    deleted_at_ms: u64,
    msg_count: u64,
    turn_count: usize,
    frozen: Value,
}
impl Source {
    fn id(&self) -> io::Result<&str> {
        let id = self
            .header
            .get("id")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("trash header identity missing"))?;
        valid_id(id)?;
        Ok(id)
    }
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Stage {
    source: Source,
    sealed: bool,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    conversation_id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TurnGet {
    conversation_id: String,
    id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Scan {
    cursor: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TurnScan {
    conversation_id: String,
    cursor: Option<String>,
}
fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn valid_id(id: &str) -> io::Result<()> {
    if id.is_empty() || id.contains('\0') || id.chars().count() > 256 {
        return Err(invalid("invalid trash identity"));
    }
    Ok(())
}
fn stage_key(tx: &AuthorityTransaction) -> io::Result<EntityKey> {
    key(
        tx.tenant_id(),
        tx.owner_user_id(),
        MIGRATION_TRASH_STAGE_NAMESPACE,
        b"pending",
    )
}
fn load_stage(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
) -> io::Result<Option<(Stage, u64)>> {
    let Some(raw) = versioned_document::get(
        db,
        tx,
        &stage_key(tx)?,
        MIGRATION_TRASH_STAGE_NAMESPACE,
        "pending",
    )?
    else {
        return Ok(None);
    };
    let envelope: Value = serde_json::from_slice(&raw)?;
    let version = envelope["version"]
        .as_u64()
        .filter(|v| *v > 0)
        .ok_or_else(|| invalid_data("trash stage version invalid"))?;
    let stage: Stage = serde_json::from_value(envelope["value"].clone())?;
    stage.source.id()?;
    Ok(Some((stage, version)))
}
fn save_stage(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    stage: &Stage,
    version: u64,
) -> io::Result<()> {
    let raw = serde_json::to_vec(stage)?;
    if raw.len() > MAX_MIGRATION_TRASH_STAGE_BYTES {
        return Err(invalid("trash stage byte budget exceeded"));
    }
    versioned_document::put_returning_envelope(
        db,
        tx,
        PutRequest {
            key: stage_key(tx)?,
            namespace: MIGRATION_TRASH_STAGE_NAMESPACE.to_owned(),
            logical_key: "pending".to_owned(),
            value_json: raw,
            expected_version: Some(version),
            updated_at_ms: stage.source.deleted_at_ms,
        },
    )?;
    Ok(())
}
pub(crate) fn require_no_pending(
    db: &AuthorityDatabase,
    tenant: u64,
    owner: u64,
) -> io::Result<()> {
    let mut tx = db.begin_with_identity_claim_scopes(tenant, owner)?;
    let pending_key = stage_key(&tx)?;
    if db.entity_get(&mut tx, &pending_key)?.is_some() {
        return Err(io::Error::new(
            io::ErrorKind::WouldBlock,
            "offline trash migration is incomplete; resume import before serving",
        ));
    }
    Ok(())
}
fn history_snapshot(
    db: &AuthorityDatabase,
    tenant: u64,
    owner: u64,
    id: &str,
) -> io::Result<Option<(AuthorityTransaction, u64, bool)>> {
    valid_id(id)?;
    let mut live = db.begin_with_identity_claim_scopes(tenant, owner)?;
    let metadata_key = trash_metadata_key(&live, id)?;
    if let Some(raw) = db.entity_get(&mut live, &metadata_key)? {
        let metadata: Value = serde_json::from_slice(&raw)?;
        let at = metadata["deletedAt"]
            .as_u64()
            .ok_or_else(|| invalid_data("trash deletion timestamp invalid"))?;
        if metadata["conversationId"].as_str() != Some(id) {
            return Err(invalid_data("trash metadata identity differs"));
        }
        let pin = trash_pin_id(&live, id);
        let mut snapshot = db
            .begin_persistent_snapshot(tenant, owner, &pin)?
            .ok_or_else(|| invalid_data("trash capsule missing"))?;
        let count = metadata["turnCount"]
            .as_u64()
            .ok_or_else(|| invalid_data("trash metadata Turn count invalid"))?;
        if crate::turn::conversation_turn_count(db, &mut snapshot, id)? != count {
            return Err(invalid_data("trash metadata and pinned Turn count differ"));
        }
        let age = trash_age_index_key(&live, id, at)?;
        if db.entity_get(&mut live, &age)?.as_deref() != Some(id.as_bytes()) {
            return Err(invalid_data("trash retention index differs"));
        }
        return Ok(Some((snapshot, at, true)));
    }
    if let Some((stage, _)) = load_stage(db, &mut live)? {
        if stage.source.id()? == id {
            return Ok(Some((live, stage.source.deleted_at_ms, false)));
        }
    }
    Ok(None)
}
fn verify_turns(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
    expected: usize,
) -> io::Result<()> {
    let mut cursor: Option<String> = None;
    let mut count = 0usize;
    loop {
        let page = crate::turn::migration::scan_conversation(db, tx, id, cursor.as_deref())?;
        for row in page["rows"]
            .as_array()
            .ok_or_else(|| invalid_data("trash Turn catalog malformed"))?
        {
            count += 1;
            if count > expected || count > MAX_MIGRATION_TRASH_TURNS {
                return Err(invalid_data("trash physical Turn catalog differs"));
            }
            let turn = crate::turn::migration::snapshot_in_transaction(
                db,
                tx,
                id,
                row["id"]
                    .as_str()
                    .ok_or_else(|| invalid_data("trash Turn identity malformed"))?,
            )?;
            if !turn["current_attempt_id"].is_null()
                || matches!(turn["status"].as_str(), Some("pending" | "running"))
            {
                return Err(invalid("trash contains a live Turn"));
            }
        }
        cursor = page["cursor"].as_str().map(str::to_owned);
        if cursor.is_none() {
            break;
        }
    }
    if count != expected || crate::turn::conversation_turn_count(db, tx, id)? != count as u64 {
        return Err(invalid_data("trash Turn counts differ"));
    }
    Ok(())
}
fn verify_source(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    source: &Source,
    sealed: bool,
) -> io::Result<()> {
    let id = source.id()?;
    require_active(db, tx, id)?;
    if migration::restore_in_transaction(db, tx, source.header.clone())? {
        return Err(invalid_data("trash header disappeared"));
    }
    verify_turns(db, tx, id, source.turn_count)?;
    if crate::frozen_transcript::verified_snapshot(db, tx, id)? != source.frozen {
        return Err(invalid_data("trash frozen transcript differs"));
    }
    if sealed && load_header(db, tx, id)?.unwrap().0["msg_count"].as_u64() != Some(source.msg_count)
    {
        return Err(invalid_data("sealed trash message count differs"));
    }
    Ok(())
}
fn get(db: &AuthorityDatabase, tenant: u64, owner: u64, id: &str) -> io::Result<Value> {
    let Some((mut tx, at, complete)) = history_snapshot(db, tenant, owner, id)? else {
        return Ok(Value::Null);
    };
    let (header, _) =
        load_header(db, &mut tx, id)?.ok_or_else(|| invalid_data("trash header missing"))?;
    let count = crate::turn::conversation_turn_count(db, &mut tx, id)?;
    if count > MAX_MIGRATION_TRASH_TURNS as u64 {
        return Err(invalid_data("trash Turn count exceeds budget"));
    }
    verify_turns(db, &mut tx, id, count as usize)?;
    let frozen = crate::frozen_transcript::verified_snapshot(db, &mut tx, id)?;
    Ok(
        json!({"header":header,"deleted_at_ms":at,"turn_count":count,"frozen":frozen,"complete":complete}),
    )
}
fn begin(db: &mut AuthorityDatabase, tenant: u64, owner: u64, payload: Value) -> io::Result<Value> {
    let source: Source =
        serde_json::from_value(payload).map_err(|_| invalid("invalid trash source"))?;
    let id = source.id()?.to_owned();
    if source.deleted_at_ms == 0 || source.turn_count > MAX_MIGRATION_TRASH_TURNS {
        return Err(invalid("trash source exceeds limits"));
    }
    let mut tx = db.begin_with_identity_claim_scopes(tenant, owner)?;
    if let Some((stage, _)) = load_stage(db, &mut tx)? {
        if stage.source != source {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "another or divergent trash capsule is pending",
            ));
        }
        return Ok(json!({"complete":false,"sealed":stage.sealed}));
    }
    let trash_key = trash_metadata_key(&tx, &id)?;
    if db.entity_get(&mut tx, &trash_key)?.is_some() {
        let Some((mut snapshot, at, complete)) = history_snapshot(db, tenant, owner, &id)? else {
            return Err(invalid_data("trash capsule missing"));
        };
        if !complete || at != source.deleted_at_ms {
            return Err(invalid_data("trash deletion timestamp differs"));
        }
        verify_source(db, &mut snapshot, &source, true)?;
        return Ok(json!({"complete":true,"sealed":true}));
    }
    // Claim the active staging header and marker atomically. A crash cannot
    // expose an unmarked partial header to resumed active-domain verification.
    if !migration::restore_in_transaction(db, &mut tx, source.header.clone())? {
        return Err(io::Error::new(
            io::ErrorKind::WouldBlock,
            "trash identity already active",
        ));
    }
    save_stage(
        db,
        &mut tx,
        &Stage {
            source,
            sealed: false,
        },
        0,
    )?;
    db.commit(tx)?;
    Ok(json!({"complete":false,"sealed":false}))
}
fn finish(db: &mut AuthorityDatabase, tenant: u64, owner: u64, id: &str) -> io::Result<Value> {
    valid_id(id)?;
    let mut tx = db.begin_with_identity_claim_scopes(tenant, owner)?;
    let Some((mut stage, version)) = load_stage(db, &mut tx)? else {
        let existing = get(db, tenant, owner, id)?;
        if existing["complete"].as_bool() == Some(true) {
            return Ok(json!({"complete":true}));
        }
        return Err(invalid("trash begin missing"));
    };
    if stage.source.id()? != id {
        return Err(invalid("trash pending identity differs"));
    }
    verify_source(db, &mut tx, &stage.source, stage.sealed)?;
    if !stage.sealed {
        set_imported_message_count(db, &mut tx, id, stage.source.msg_count)?;
        stage.sealed = true;
        save_stage(db, &mut tx, &stage, version)?;
        db.commit(tx)?;
        // Range pins must capture this committed seal, never pending writes.
        tx = db.begin_with_identity_claim_scopes(tenant, owner)?;
        verify_source(db, &mut tx, &stage.source, true)?;
    }
    delete(
        db,
        &mut tx,
        &DeleteRequest {
            conversation_id: id.to_owned(),
            deleted_at_ms: stage.source.deleted_at_ms,
        },
    )?;
    let pending = stage_key(&tx)?;
    db.entity_delete(&mut tx, pending)?;
    db.commit(tx)?;
    Ok(json!({"complete":true}))
}
pub(crate) fn execute(
    db: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    match operation {
        "migration.trash.begin" => begin(db, tenant, owner, payload),
        "migration.trash.finish" | "migration.trash.get" => {
            let p: Request = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid trash identity request"))?;
            if operation.ends_with("finish") {
                finish(db, tenant, owner, &p.conversation_id)
            } else {
                get(db, tenant, owner, &p.conversation_id)
            }
        }
        "migration.trash.turn.get" => {
            let p: TurnGet = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid trash Turn request"))?;
            let Some((mut tx, _, _)) = history_snapshot(db, tenant, owner, &p.conversation_id)?
            else {
                return Ok(Value::Null);
            };
            crate::turn::migration::snapshot_in_transaction(db, &mut tx, &p.conversation_id, &p.id)
        }
        "migration.trash.turn.scan" => {
            let p: TurnScan =
                serde_json::from_value(payload).map_err(|_| invalid("invalid trash Turn scan"))?;
            let Some((mut tx, _, _)) = history_snapshot(db, tenant, owner, &p.conversation_id)?
            else {
                return Ok(json!({"rows":[],"cursor":null}));
            };
            crate::turn::migration::scan_conversation(
                db,
                &mut tx,
                &p.conversation_id,
                p.cursor.as_deref(),
            )
        }
        "migration.trash.scan" => {
            let p: Scan =
                serde_json::from_value(payload).map_err(|_| invalid("invalid trash scan"))?;
            let mut tx = db.begin_with_identity_claim_scopes(tenant, owner)?;
            let mut suffix = Vec::new();
            if let Some(cursor) = p.cursor {
                let id = cursor
                    .strip_prefix("trash:")
                    .ok_or_else(|| invalid("invalid trash cursor"))?;
                valid_id(id)?;
                suffix.extend_from_slice(id.as_bytes());
                suffix.push(0);
            }
            let start = key(
                tenant,
                owner,
                CONVERSATION_TRASH_METADATA_NAMESPACE,
                &suffix,
            )?;
            let (_, end) =
                EntityKey::prefix_range(tenant, owner, CONVERSATION_TRASH_METADATA_NAMESPACE, b"")?;
            let page = db.entity_scan(&mut tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
            let mut rows = Vec::with_capacity(page.len());
            for (key, _) in &page {
                let id = std::str::from_utf8(key.key_bytes())
                    .map_err(|_| invalid_data("trash catalog identity invalid"))?;
                valid_id(id)?;
                rows.push(json!({"conversation_id":id}));
            }
            let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
                Some(format!(
                    "trash:{}",
                    rows.last().unwrap()["conversation_id"].as_str().unwrap()
                ))
            } else {
                None
            };
            let pending = load_stage(db, &mut tx)?
                .map(|(stage, _)| stage.source.id().map(str::to_owned))
                .transpose()?;
            Ok(json!({"rows":rows,"cursor":cursor,"pending_conversation_id":pending}))
        }
        _ => Err(invalid("unknown trash migration operation")),
    }
}

#[cfg(test)]
#[path = "trash_migration_tests.rs"]
mod tests;
