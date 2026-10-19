//! Offline Attempt event restoration. Exact event bytes/timestamps share the
//! live event namespace; terminal parent ownership and sequence heads are
//! checked independently of active conversation/Turn visibility.
use super::*;
use crate::generated_tofudb_ir::MIGRATION_SESSION_MAX_PAGE_ROWS;
use serde::{Deserialize, Serialize};

#[derive(Deserialize, Serialize, PartialEq, Debug)]
#[serde(deny_unknown_fields)]
struct Snapshot {
    attempt_id: String,
    sequence: u64,
    conversation_id: String,
    turn_id: String,
    projection_revision: u64,
    #[serde(rename = "type")]
    event_type: String,
    event_json: String,
    created_at: u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Identity {
    attempt_id: String,
    sequence: u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Scan {
    cursor: Option<String>,
}
fn logical_key(id: &str, sequence: u64) -> String {
    format!("{id}\u{1f}{sequence:020}")
}
fn validate_metadata(row: &Snapshot) -> io::Result<()> {
    for text in [
        &row.attempt_id,
        &row.conversation_id,
        &row.turn_id,
        &row.event_type,
    ] {
        if text.is_empty() || text.contains('\0') || text.chars().count() > 1024 {
            return Err(invalid_input("invalid attempt event identity"));
        }
    }
    if row.sequence == 0
        || row.created_at == 0
        || row.event_json.len() > MAX_ATTEMPT_EVENT_DOCUMENT_BYTES
    {
        return Err(invalid_input("attempt event bounds exceeded"));
    }
    Ok(())
}
fn validate(row: &Snapshot) -> io::Result<()> {
    validate_metadata(row)?;
    let event: Value = serde_json::from_str(&row.event_json)
        .map_err(|_| invalid_input("attempt event JSON is invalid"))?;
    if event.get("attemptId").and_then(Value::as_str) != Some(&row.attempt_id)
        || event.get("seq").and_then(Value::as_u64) != Some(row.sequence)
        || event.get("conversationId").and_then(Value::as_str) != Some(&row.conversation_id)
        || event.get("turnId").and_then(Value::as_str) != Some(&row.turn_id)
        || event.get("projectionRevision").and_then(Value::as_u64) != Some(row.projection_revision)
        || event.get("type").and_then(Value::as_str) != Some(&row.event_type)
    {
        return Err(invalid_input("attempt event envelope identity differs"));
    }
    Ok(())
}
/// Session-scoped memo of validated event parents.
///
/// Offline-lane attempt snapshots are insert-or-match immutable: a divergent
/// re-import is rejected before any write, so once `parent()` proves a parent
/// attempt exists and names its Turn, that fact cannot change for the rest of
/// the exclusive session. Events stream clustered by attempt, so memoizing
/// the validation eliminates re-materializing the same parent document (and
/// its payload blob) for every one of an attempt's hundreds of events. The
/// memo is owned by `migration_session::run` and dies with the session; on
/// overflow it clears and degrades to ordinary re-validation.
#[derive(Default)]
pub(crate) struct ParentValidationMemo {
    validated: std::collections::HashMap<String, String>,
}

impl ParentValidationMemo {
    const CAPACITY: usize = 4096;

    fn key(conversation_id: &str, attempt_id: &str) -> String {
        format!("{conversation_id}\u{1f}{attempt_id}")
    }

    fn turn(&self, conversation_id: &str, attempt_id: &str) -> Option<&str> {
        self.validated
            .get(&Self::key(conversation_id, attempt_id))
            .map(String::as_str)
    }

    fn record(&mut self, conversation_id: &str, attempt_id: &str, turn_id: &str) {
        if self.validated.len() >= Self::CAPACITY {
            self.validated.clear();
        }
        self.validated.insert(
            Self::key(conversation_id, attempt_id),
            turn_id.to_owned(),
        );
    }
}

fn parent(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    row: &Snapshot,
    memo: &mut ParentValidationMemo,
) -> io::Result<()> {
    if let Some(turn_id) = memo.turn(&row.conversation_id, &row.attempt_id) {
        if turn_id != row.turn_id {
            return Err(invalid_input("attempt event parent Turn differs"));
        }
        return Ok(());
    }
    let attempt = super::attempt_migration::read_snapshot(
        database,
        tx,
        &row.conversation_id,
        &row.attempt_id,
    )?
    .ok_or_else(|| invalid_input("attempt event parent is missing"))?;
    if attempt.get("turn_id").and_then(Value::as_str) != Some(&row.turn_id) {
        return Err(invalid_input("attempt event parent Turn differs"));
    }
    memo.record(&row.conversation_id, &row.attempt_id, &row.turn_id);
    Ok(())
}
fn read(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
    sequence: u64,
    memo: &mut ParentValidationMemo,
) -> io::Result<Option<Snapshot>> {
    let key = attempt_event_key(tx, id, sequence)?;
    let Some(stored) = crate::versioned_document::read_snapshot_bounded(
        database,
        tx,
        &key,
        ATTEMPT_EVENT_DOCUMENT_IDENTITY,
        &logical_key(id, sequence),
        MAX_ATTEMPT_EVENT_DOCUMENT_BYTES,
    )?
    else {
        return Ok(None);
    };
    let event_json =
        String::from_utf8(stored.value_json).map_err(|_| invalid_data("invalid event UTF-8"))?;
    let event: Value =
        serde_json::from_str(&event_json).map_err(|_| invalid_data("invalid event JSON"))?;
    let text = |field| {
        event
            .get(field)
            .and_then(Value::as_str)
            .map(str::to_owned)
            .ok_or_else(|| invalid_data("event identity missing"))
    };
    let row = Snapshot {
        attempt_id: id.to_owned(),
        sequence,
        conversation_id: text("conversationId")?,
        turn_id: text("turnId")?,
        projection_revision: event
            .get("projectionRevision")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("event revision missing"))?,
        event_type: text("type")?,
        event_json,
        created_at: stored.updated_at_ms,
    };
    validate(&row)?;
    parent(database, tx, &row, memo)?;
    let head = decode_u64(
        database.entity_get(tx, &attempt_event_head_key(tx, id)?)?,
        "event head malformed",
    )?;
    if head < sequence
        || database
            .entity_get(tx, &attempt_event_key(tx, id, head)?)?
            .is_none()
    {
        return Err(invalid_data("event sequence head differs"));
    }
    Ok(Some(row))
}
fn restore(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    row: Snapshot,
    memo: &mut ParentValidationMemo,
) -> io::Result<bool> {
    validate(&row)?;
    parent(database, tx, &row, memo)?;
    if let Some(existing) = read(database, tx, &row.attempt_id, row.sequence, memo)? {
        if existing != row {
            return Err(conflict("attempt event snapshot differs"));
        }
        return Ok(false);
    }
    let head_key = attempt_event_head_key(tx, &row.attempt_id)?;
    let head = decode_u64(database.entity_get(tx, &head_key)?, "event head malformed")?;
    if head > 0
        && database
            .entity_get(tx, &attempt_event_key(tx, &row.attempt_id, head)?)?
            .is_none()
    {
        return Err(invalid_data(
            "event sequence head points to a missing event",
        ));
    }
    crate::versioned_document::put_with_blob_owner_bounded(
        database,
        tx,
        crate::versioned_document::PutRequest {
            key: attempt_event_key(tx, &row.attempt_id, row.sequence)?,
            namespace: ATTEMPT_EVENT_DOCUMENT_IDENTITY.to_owned(),
            logical_key: logical_key(&row.attempt_id, row.sequence),
            value_json: row.event_json.into_bytes(),
            expected_version: Some(0),
            updated_at_ms: row.created_at,
        },
        tx.owner_user_id(),
        MAX_ATTEMPT_EVENT_DOCUMENT_BYTES,
    )?;
    database.entity_put(tx, head_key, head.max(row.sequence).to_le_bytes().to_vec())?;
    Ok(true)
}
fn scan(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
    memo: &mut ParentValidationMemo,
) -> io::Result<Value> {
    let request: Scan =
        serde_json::from_value(payload).map_err(|_| invalid_input("invalid event scan"))?;
    let suffix = super::migration::cursor_start("attempt_events", request.cursor.as_deref())?;
    let start = entity_key(tx, ATTEMPT_EVENT_NAMESPACE, &suffix)?;
    let (_, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        ATTEMPT_EVENT_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (key, stored) in &page {
        let (_, raw) = crate::versioned_document::materialize_stored_document_bounded(
            database,
            tx.tenant_id(),
            tx.owner_user_id(),
            stored,
            ATTEMPT_EVENT_DOCUMENT_IDENTITY,
            MAX_ATTEMPT_EVENT_DOCUMENT_BYTES,
        )?;
        let event: Value =
            serde_json::from_slice(&raw).map_err(|_| invalid_data("invalid event catalog"))?;
        let id = event
            .get("attemptId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("event ID missing"))?;
        let sequence = event
            .get("seq")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("event sequence missing"))?;
        if *key != attempt_event_key(tx, id, sequence)?
            || read(database, tx, id, sequence, memo)?.is_none()
        {
            return Err(invalid_data("event catalog identity differs"));
        }
        rows.push(json!({"attempt_id":id,"sequence":sequence}));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let hex: String = page
            .last()
            .unwrap()
            .0
            .key_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        Some(format!("attempt_events:{hex}"))
    } else {
        None
    };
    Ok(json!({"rows":rows,"cursor":cursor}))
}
const MAX_BATCH_ROWS: usize = 256;
const MAX_BATCH_EVENT_BYTES: usize = 8 * 1024 * 1024;

pub(crate) fn execute(
    database: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    operation: &str,
    payload: Value,
    memo: &mut ParentValidationMemo,
) -> io::Result<Value> {
    let mut tx = database.begin_with_identity_claim_scopes(tenant, owner)?;
    match operation {
        "migration.attempt.event.restore" => {
            let row = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid event snapshot"))?;
            let inserted = restore(database, &mut tx, row, memo)?;
            if inserted {
                database.commit(tx)?;
            }
            Ok(json!({"inserted":inserted}))
        }
        "migration.attempt.event.restore_batch" => {
            // One transaction for a bounded run of rows: identical per-row
            // validation/parent fencing/insert-or-match semantics as the
            // single-row op, with the commit and round trip amortized across
            // the batch. A failed row aborts the whole batch, and idempotent
            // replay makes a partially-applied batch impossible to observe.
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct Batch {
                rows: Vec<Snapshot>,
            }
            let batch: Batch = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid event batch snapshot"))?;
            if batch.rows.is_empty()
                || batch.rows.len() > MAX_BATCH_ROWS
                || batch.rows.iter().map(|row| row.event_json.len()).sum::<usize>()
                    > MAX_BATCH_EVENT_BYTES
            {
                return Err(invalid_input("event batch exceeds its bounded range"));
            }
            let mut inserted = Vec::with_capacity(batch.rows.len());
            let mut any = false;
            for row in batch.rows {
                let one = restore(database, &mut tx, row, memo)?;
                any |= one;
                inserted.push(one);
            }
            if any {
                database.commit(tx)?;
            }
            Ok(json!({"inserted":inserted}))
        }
        "migration.attempt.event.get" => {
            let id: Identity = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid event identity"))?;
            let row = read(database, &mut tx, &id.attempt_id, id.sequence, memo)?;
            if row
                .as_ref()
                .is_some_and(|row| row.event_json.len() > MAX_LEGACY_DOCUMENT_BYTES)
            {
                return Err(invalid_input("oversized event requires chunk reads"));
            }
            Ok(serde_json::to_value(row)?)
        }
        "migration.attempt.event.chunk" => {
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct Chunk {
                attempt_id: String,
                sequence: u64,
                offset: usize,
            }
            let request: Chunk = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid event chunk read"))?;
            let Some(mut row) = read(database, &mut tx, &request.attempt_id, request.sequence, memo)?
            else {
                return Ok(Value::Null);
            };
            let event = std::mem::take(&mut row.event_json);
            if request.offset > event.len() {
                return Err(invalid_input("event chunk offset is past the end"));
            }
            let end = event.len().min(request.offset.saturating_add(
                crate::generated_tofudb_ir::MIGRATION_SESSION_MAX_SOURCE_CHUNK_BYTES,
            ));
            let hex: String = event.as_bytes()[request.offset..end]
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect();
            Ok(
                json!({"snapshot":row,"event_bytes":event.len(),"sha256":format!("{:x}",Sha256::digest(event.as_bytes())),"offset":request.offset,"hex":hex,"final":end==event.len()}),
            )
        }
        "migration.attempt.event.scan" => scan(database, &mut tx, payload, memo),
        _ => Err(invalid_input("unknown event migration operation")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Vfs};
    use std::{path::Path, sync::Arc};
    #[test]
    fn events_keep_gaps_across_reopen_and_use_live_visibility_fences() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        let root = Path::new("/events");
        vfs.create_dir(root).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut db = AuthorityDatabase::initialize_with_vfs(root, vfs.clone()).unwrap();
        let mut memo = ParentValidationMemo::default();
        let attempt = json!({"attempt_id":"a","conversation_id":"conv","turn_id":"turn",
            "command_id":"command","task_id":"task","operation":"generate","dispatch_mode":"",
            "queue_id":"","queue_state":"","status":"completed","base_projection_revision":1,
            "resume_anchor":{},"config":{},"error":{},"timing_trace":{},"created_at":1,
            "started_at":2,"settled_at":3,"superseded_at":null});
        super::super::attempt_migration::execute(
            &mut db,
            7,
            11,
            "migration.attempt.restore",
            attempt,
        )
        .unwrap();
        let mut expected = Vec::new();
        for sequence in [5, 11] {
            let mut event = json!({"attemptId":"a","seq":sequence,"conversationId":"conv","turnId":"turn",
                "projectionRevision":1,"type":"progress","payload":{"text":"retained"}});
            if sequence == 11 {
                event["payload"]["text"] = json!("x".repeat(9 * 1024 * 1024));
            }
            let mut row = json!({"attempt_id":"a","sequence":sequence,"conversation_id":"conv","turn_id":"turn",
                "projection_revision":1,"type":"progress","event_json":serde_json::to_string_pretty(&event).unwrap(),"created_at":10+sequence});
            if sequence == 5 {
                execute(&mut db, 7, 11, "migration.attempt.event.restore", row, &mut memo).unwrap();
            } else {
                let text = row["event_json"].as_str().unwrap().to_owned();
                row["event_json"] = json!("");
                let begin = json!({"snapshot":row,"event_bytes":text.len(),"sha256":format!("{:x}",Sha256::digest(text.as_bytes()))});
                let mut upload = UploadState::default();
                upload
                    .execute(
                        &mut db,
                        7,
                        11,
                        "migration.attempt.event.upload.begin",
                        begin.clone(),
                        &mut memo,
                    )
                    .unwrap();
                assert!(upload
                    .execute(
                        &mut db,
                        7,
                        11,
                        "migration.attempt.event.upload.finish",
                        json!({}),
                        &mut memo,
                    )
                    .is_err());
                let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
                assert!(read(&db, &mut tx, "a", 11, &mut memo).unwrap().is_none());
                upload
                    .execute(
                        &mut db,
                        7,
                        11,
                        "migration.attempt.event.upload.begin",
                        begin,
                        &mut memo,
                    )
                    .unwrap();
                let mut offset = 0;
                for chunk in text
                    .as_bytes()
                    .chunks(crate::generated_tofudb_ir::MIGRATION_SESSION_MAX_SOURCE_CHUNK_BYTES)
                {
                    let hex: String = chunk.iter().map(|byte| format!("{byte:02x}")).collect();
                    let request = json!({"offset":offset,"hex":hex});
                    let accepted = upload
                        .execute(
                            &mut db,
                            7,
                            11,
                            "migration.attempt.event.upload.append",
                            request.clone(),
                            &mut memo,
                        )
                        .unwrap();
                    assert_eq!(accepted["offset"], json!(offset + chunk.len()));
                    if offset == 0 {
                        assert_eq!(
                            upload
                                .execute(
                                    &mut db,
                                    7,
                                    11,
                                    "migration.attempt.event.upload.append",
                                    request,
                                    &mut memo,
                                )
                                .unwrap(),
                            accepted
                        );
                    }
                    offset += chunk.len();
                }
                upload
                    .execute(
                        &mut db,
                        7,
                        11,
                        "migration.attempt.event.upload.finish",
                        json!({}),
                        &mut memo,
                    )
                    .unwrap();
                assert!(upload.pending.is_none());
                let chunk = execute(
                    &mut db,
                    7,
                    11,
                    "migration.attempt.event.chunk",
                    json!({"attempt_id":"a","sequence":11,"offset":0}),
                    &mut memo,
                )
                .unwrap();
                assert_eq!(chunk["event_bytes"], json!(text.len()));
                assert_eq!(
                    chunk["sha256"],
                    json!(format!("{:x}", Sha256::digest(text.as_bytes())))
                );
                assert!(
                    chunk["hex"].as_str().unwrap().len()
                        <= 2 * crate::generated_tofudb_ir::MIGRATION_SESSION_MAX_SOURCE_CHUNK_BYTES
                );
            }
            expected.push(event);
        }
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert!(events_list(&db, &mut tx, "a", 0, 100, true, &mut || false)
            .unwrap()
            .is_none());
        drop(tx);
        drop(db);
        vfs.crash().unwrap();
        let mut db = AuthorityDatabase::open_with_vfs(root, vfs).unwrap();
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert!(read(&db, &mut tx, "a", 5, &mut memo).unwrap().is_some());
        crate::conversation_header::create(
            &db,
            &mut tx,
            &crate::conversation_header::CreateRequest {
                conversation_id: "conv".into(),
                title: "Conversation".into(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 1,
                updated_at_ms: 1,
                committed_at_ms: 1,
            },
        )
        .unwrap();
        db.commit(tx).unwrap();
        let turn = json!({"turn_id":"turn","conversation_id":"conv","presentation_id":"","lane_id":"main",
            "parent_turn_id":null,"ordinal":1,"actor":"assistant","kind":"reply","run_id":"",
            "status":"completed","current_attempt_id":"a","projection":{"content":"result"},
            "projection_revision":1,"settlement":{"status":"completed"},"created_at":1,"updated_at":3});
        super::super::migration::execute(&mut db, 7, 11, "migration.turn.restore", turn).unwrap();
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        let bytes = events_list(&db, &mut tx, "a", 0, 100, true, &mut || false)
            .unwrap()
            .unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&bytes).unwrap(),
            json!(expected)
        );
        let mut foreign = db.begin_with_identity_claim_scopes(7, 12).unwrap();
        assert!(read(&db, &mut foreign, "a", 5, &mut ParentValidationMemo::default()).unwrap().is_none());
        let key = attempt_event_head_key(&tx, "a").unwrap();
        db.entity_put(&mut tx, key, 99_u64.to_le_bytes().to_vec())
            .unwrap();
        db.commit(tx).unwrap();
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert!(read(&db, &mut tx, "a", 5, &mut memo).is_err());
    }
}

// Only the exclusive operator-session loop owns this volatile staging buffer.
// No authority mutation occurs until a complete hash-verified finish.
#[derive(Default)]
pub(crate) struct UploadState {
    pending: Option<PendingUpload>,
}
struct PendingUpload {
    row: Snapshot,
    size: usize,
    sha256: String,
    bytes: zeroize::Zeroizing<Vec<u8>>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct UploadBegin {
    snapshot: Snapshot,
    event_bytes: usize,
    sha256: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct UploadAppend {
    offset: usize,
    hex: String,
}
impl UploadState {
    pub(crate) fn has_pending(&self) -> bool {
        self.pending.is_some()
    }
    pub(crate) fn execute(
        &mut self,
        database: &mut AuthorityDatabase,
        tenant: u64,
        owner: u64,
        operation: &str,
        payload: Value,
        memo: &mut ParentValidationMemo,
    ) -> io::Result<Value> {
        match operation {
            "migration.attempt.event.upload.begin" => {
                let begin: UploadBegin = serde_json::from_value(payload)
                    .map_err(|_| invalid_input("invalid event upload"))?;
                if !begin.snapshot.event_json.is_empty()
                    || begin.event_bytes == 0
                    || begin.event_bytes > MAX_ATTEMPT_EVENT_DOCUMENT_BYTES
                    || begin.sha256.len() != 64
                    || !begin
                        .sha256
                        .bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                {
                    return Err(invalid_input("event upload size or hash is invalid"));
                }
                validate_metadata(&begin.snapshot)?;
                let mut tx = database.begin_with_identity_claim_scopes(tenant, owner)?;
                parent(database, &mut tx, &begin.snapshot, memo)?;
                if let Some(pending) = &self.pending {
                    if pending.row != begin.snapshot
                        || pending.size != begin.event_bytes
                        || pending.sha256 != begin.sha256
                    {
                        return Err(conflict("another event upload is pending"));
                    }
                    return Ok(json!({"offset":pending.bytes.len()}));
                }
                let mut bytes = Vec::new();
                bytes.try_reserve_exact(begin.event_bytes).map_err(|_| {
                    io::Error::new(io::ErrorKind::OutOfMemory, "event upload allocation failed")
                })?;
                self.pending = Some(PendingUpload {
                    row: begin.snapshot,
                    size: begin.event_bytes,
                    sha256: begin.sha256,
                    bytes: zeroize::Zeroizing::new(bytes),
                });
                Ok(json!({"offset":0}))
            }
            "migration.attempt.event.upload.append" => {
                let append: UploadAppend = serde_json::from_value(payload)
                    .map_err(|_| invalid_input("invalid event chunk"))?;
                let max_chunk =
                    crate::generated_tofudb_ir::MIGRATION_SESSION_MAX_SOURCE_CHUNK_BYTES;
                if append.hex.is_empty()
                    || append.hex.len() % 2 != 0
                    || append.hex.len() > max_chunk * 2
                {
                    return Err(invalid_input("event chunk exceeds its byte budget"));
                }
                let decode = |b: u8| match b {
                    b'0'..=b'9' => Some(b - b'0'),
                    b'a'..=b'f' => Some(b - b'a' + 10),
                    _ => None,
                };
                let chunk: Vec<u8> = append
                    .hex
                    .as_bytes()
                    .chunks_exact(2)
                    .map(|p| {
                        Ok(
                            decode(p[0]).ok_or_else(|| invalid_input("invalid event chunk hex"))?
                                * 16
                                + decode(p[1])
                                    .ok_or_else(|| invalid_input("invalid event chunk hex"))?,
                        )
                    })
                    .collect::<io::Result<_>>()?;
                let pending = self
                    .pending
                    .as_mut()
                    .ok_or_else(|| invalid_input("event upload is missing"))?;
                let end = append
                    .offset
                    .checked_add(chunk.len())
                    .filter(|end| *end <= pending.size)
                    .ok_or_else(|| invalid_input("event chunk offset exceeds upload"))?;
                if append.offset == pending.bytes.len() {
                    pending.bytes.extend_from_slice(&chunk);
                } else if pending.bytes.get(append.offset..end) != Some(chunk.as_slice()) {
                    return Err(conflict("event chunk offset or replay differs"));
                }
                Ok(json!({"offset":pending.bytes.len()}))
            }
            "migration.attempt.event.upload.finish" => {
                if payload != json!({}) {
                    return Err(invalid_input("invalid event finish"));
                }
                let mut pending = self
                    .pending
                    .take()
                    .ok_or_else(|| invalid_input("event upload is missing"))?;
                let hash = format!("{:x}", Sha256::digest(pending.bytes.as_slice()));
                if pending.bytes.len() != pending.size || hash != pending.sha256 {
                    return Err(invalid_data("event upload length or hash differs"));
                }
                pending.row.event_json = String::from_utf8(std::mem::take(&mut *pending.bytes))
                    .map_err(|_| invalid_input("event upload is not UTF-8"))?;
                let mut tx = database.begin_with_identity_claim_scopes(tenant, owner)?;
                let inserted = restore(database, &mut tx, pending.row, memo)?;
                if inserted {
                    database.commit(tx)?;
                }
                Ok(json!({"inserted":inserted}))
            }
            "migration.attempt.event.upload.abort" => {
                if payload != json!({}) {
                    return Err(invalid_input("invalid event abort"));
                }
                self.pending = None;
                Ok(json!({"aborted":true}))
            }
            _ => Err(invalid_input("unknown event upload operation")),
        }
    }
}

#[cfg(test)]
mod batch_tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Vfs};
    use std::{path::Path, sync::Arc};

    fn attempt_row(attempt: &str, sequence: u64) -> Value {
        let event = json!({"attemptId":attempt,"seq":sequence,"conversationId":"conv",
            "turnId":"turn","projectionRevision":1,"type":"progress","payload":{"n":sequence}});
        json!({"attempt_id":attempt,"sequence":sequence,"conversation_id":"conv","turn_id":"turn",
            "projection_revision":1,"type":"progress",
            "event_json":serde_json::to_string(&event).unwrap(),"created_at":10+sequence})
    }

    #[test]
    fn event_batch_restores_atomically_replays_and_memoizes_parents() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        let root = Path::new("/event-batch");
        vfs.create_dir(root).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut db = AuthorityDatabase::initialize_with_vfs(root, vfs).unwrap();
        super::super::attempt_migration::execute(
            &mut db, 7, 11, "migration.attempt.restore",
            json!({"attempt_id":"a","conversation_id":"conv","turn_id":"turn",
                "command_id":"command","task_id":"task","operation":"generate","dispatch_mode":"",
                "queue_id":"","queue_state":"","status":"completed","base_projection_revision":1,
                "resume_anchor":{},"config":{},"error":{},"timing_trace":{},"created_at":1,
                "started_at":2,"settled_at":3,"superseded_at":null}),
        )
        .unwrap();
        let mut memo = ParentValidationMemo::default();
        let rows = json!({"rows":[attempt_row("a",1),attempt_row("a",2),attempt_row("a",3)]});
        let inserted = execute(&mut db, 7, 11, "migration.attempt.event.restore_batch",
                               rows.clone(), &mut memo).unwrap();
        assert_eq!(inserted["inserted"], json!([true, true, true]));
        let replay = execute(&mut db, 7, 11, "migration.attempt.event.restore_batch",
                             rows, &mut memo).unwrap();
        assert_eq!(replay["inserted"], json!([false, false, false]));
        // A bad-parent row aborts the whole batch: nothing from it is readable.
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert!(execute(&mut db, 7, 11, "migration.attempt.event.restore_batch",
                        json!({"rows":[attempt_row("ghost",1)]}), &mut memo).is_err());
        assert!(read(&db, &mut tx, "a", 1, &mut memo).unwrap().is_some());
        assert!(read(&db, &mut tx, "ghost", 1, &mut memo).unwrap().is_none());
        assert!(execute(&mut db, 7, 11, "migration.attempt.event.restore_batch",
                        json!({"rows":[]}), &mut memo).is_err());
    }
}
