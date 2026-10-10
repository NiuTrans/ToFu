//! Immutable paged pre-Turn transcripts, owned by a conversation and its owner.
//! Private import publishes pages before a complete manifest; live reads expose
//! only complete history. Range pins preserve deletion/restore, and clones share
//! owner-bound blob references through the versioned-document codec.
use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::*;
use crate::versioned_document::{self, PutRequest};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io;

pub(crate) mod upload;
fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn corrupt(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::AlreadyExists, message)
}
fn bounded(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::OutOfMemory, message)
}
fn validate_id(id: &str) -> io::Result<()> {
    if id.is_empty() || id.len() > 256 || id.contains('\0') {
        return Err(invalid("invalid frozen transcript identity"));
    }
    Ok(())
}
fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn key(tx: &AuthorityTransaction, namespace: &str, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(tx.tenant_id(), tx.owner_user_id(), namespace, raw)
}
fn prefix(id: &str) -> io::Result<Vec<u8>> {
    validate_id(id)?;
    let mut bytes = id.as_bytes().to_vec();
    bytes.push(0);
    Ok(bytes)
}
fn page_key(tx: &AuthorityTransaction, id: &str, index: usize) -> io::Result<EntityKey> {
    let mut bytes = prefix(id)?;
    bytes.extend_from_slice(&(index as u64).to_be_bytes());
    key(tx, FROZEN_TRANSCRIPT_PAGE_NAMESPACE, &bytes)
}
fn page_logical(id: &str, index: usize) -> String {
    format!("{id}\0{index:020}")
}
fn manifest_key(tx: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    validate_id(id)?;
    key(tx, FROZEN_TRANSCRIPT_MANIFEST_NAMESPACE, id.as_bytes())
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Begin {
    conversation_id: String,
    source_sha256: String,
    source_bytes: u64,
    created_at_ms: u64,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Page {
    start: usize,
    count: usize,
    bytes: usize,
    sha256: String,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    source: Begin,
    complete: bool,
    messages: usize,
    decoded_bytes: usize,
    pages: Vec<Page>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Identity {
    conversation_id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Restore {
    conversation_id: String,
    page_index: usize,
    start: usize,
    page_json: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Finish {
    conversation_id: String,
    page_count: usize,
    message_count: usize,
    decoded_bytes: usize,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Scan {
    cursor: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Chunk {
    conversation_id: String,
    page_index: usize,
    offset: usize,
}

fn validate_manifest(m: &Manifest) -> io::Result<()> {
    validate_id(&m.source.conversation_id)?;
    if !valid_digest(&m.source.source_sha256)
        || m.source.source_bytes > MAX_FROZEN_TRANSCRIPT_SOURCE_BYTES as u64
        || m.source.created_at_ms == 0
        || m.pages.len() > MAX_FROZEN_TRANSCRIPT_PAGES
        || m.messages > MAX_FROZEN_TRANSCRIPT_MESSAGES
        || m.decoded_bytes > MAX_FROZEN_TRANSCRIPT_DECODED_BYTES
    {
        return Err(corrupt("invalid frozen transcript manifest bounds"));
    }
    let mut count = 0usize;
    let mut bytes = 0usize;
    for p in &m.pages {
        if p.start != count
            || p.count == 0
            || p.count > MAX_FROZEN_TRANSCRIPT_MESSAGES
            || p.bytes == 0
            || p.bytes > MAX_FROZEN_TRANSCRIPT_PAGE_BYTES
            || !valid_digest(&p.sha256)
        {
            return Err(corrupt("invalid frozen transcript page directory"));
        }
        count = count
            .checked_add(p.count)
            .ok_or_else(|| corrupt("frozen transcript count overflow"))?;
        bytes = bytes
            .checked_add(p.bytes)
            .ok_or_else(|| corrupt("frozen transcript bytes overflow"))?;
    }
    if count != m.messages || bytes != m.decoded_bytes {
        return Err(corrupt("frozen transcript totals differ"));
    }
    Ok(())
}
fn load(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<(Manifest, u64)>> {
    let Some(raw) = versioned_document::read_snapshot(
        database,
        tx,
        &manifest_key(tx, id)?,
        FROZEN_TRANSCRIPT_MANIFEST_NAMESPACE,
        id,
    )?
    else {
        return Ok(None);
    };
    let manifest: Manifest = serde_json::from_slice(&raw.value_json)
        .map_err(|_| corrupt("invalid frozen transcript manifest"))?;
    validate_manifest(&manifest)?;
    if manifest.source.conversation_id != id {
        return Err(corrupt("frozen transcript manifest identity differs"));
    }
    Ok(Some((manifest, raw.version)))
}
fn save(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    m: &Manifest,
    version: u64,
) -> io::Result<()> {
    validate_manifest(m)?;
    versioned_document::put(
        database,
        tx,
        PutRequest {
            key: manifest_key(tx, &m.source.conversation_id)?,
            namespace: FROZEN_TRANSCRIPT_MANIFEST_NAMESPACE.into(),
            logical_key: m.source.conversation_id.clone(),
            value_json: serde_json::to_vec(m)?,
            expected_version: Some(version),
            updated_at_ms: m.source.created_at_ms,
        },
    )?;
    Ok(())
}
fn page_bytes(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    m: &Manifest,
    index: usize,
) -> io::Result<Vec<u8>> {
    let descriptor = m
        .pages
        .get(index)
        .ok_or_else(|| invalid("frozen transcript page is outside manifest"))?;
    let id = &m.source.conversation_id;
    let raw = versioned_document::read_snapshot_bounded(
        database,
        tx,
        &page_key(tx, id, index)?,
        FROZEN_TRANSCRIPT_PAGE_NAMESPACE,
        &page_logical(id, index),
        MAX_FROZEN_TRANSCRIPT_PAGE_BYTES,
    )?
    .ok_or_else(|| corrupt("frozen transcript page missing"))?;
    if raw.value_json.len() != descriptor.bytes || digest(&raw.value_json) != descriptor.sha256 {
        return Err(corrupt("frozen transcript page digest differs"));
    }
    Ok(raw.value_json)
}
fn parse_page(raw: &[u8]) -> io::Result<Vec<Value>> {
    if raw.len() > MAX_FROZEN_TRANSCRIPT_PAGE_BYTES {
        return Err(bounded("frozen transcript page exceeds byte budget"));
    }
    let messages: Vec<Value> = serde_json::from_slice(raw)
        .map_err(|_| invalid("frozen transcript page is not a message array"))?;
    if messages.is_empty()
        || messages.len() > MAX_FROZEN_TRANSCRIPT_MESSAGES
        || messages.iter().any(|m| {
            !m.is_object()
                || m.get("_tofuArchivedMessageCodec").is_some()
                || m.get("_tofuStorageProjectionCodec").is_some()
        })
    {
        return Err(invalid(
            "frozen transcript page contains invalid public messages",
        ));
    }
    Ok(messages)
}
fn restore_page(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
    index: usize,
    start: usize,
    raw: Vec<u8>,
) -> io::Result<bool> {
    crate::conversation_header::require_active(database, tx, id)?;
    let (mut m, version) =
        load(database, tx, id)?.ok_or_else(|| invalid("frozen transcript begin is missing"))?;
    let count = parse_page(&raw)?.len();
    let descriptor = Page {
        start,
        count,
        bytes: raw.len(),
        sha256: digest(&raw),
    };
    if index < m.pages.len() {
        if m.pages[index] != descriptor || page_bytes(database, tx, &m, index)? != raw {
            return Err(conflict("frozen transcript replay differs"));
        }
        return Ok(false);
    }
    if m.complete
        || index != m.pages.len()
        || index >= MAX_FROZEN_TRANSCRIPT_PAGES
        || start != m.messages
    {
        return Err(conflict("frozen transcript page order differs"));
    }
    m.messages = m
        .messages
        .checked_add(count)
        .ok_or_else(|| bounded("frozen transcript count overflow"))?;
    m.decoded_bytes = m
        .decoded_bytes
        .checked_add(raw.len())
        .ok_or_else(|| bounded("frozen transcript byte count overflow"))?;
    m.pages.push(descriptor);
    validate_manifest(&m)?;
    versioned_document::put_with_blob_owner_bounded(
        database,
        tx,
        PutRequest {
            key: page_key(tx, id, index)?,
            namespace: FROZEN_TRANSCRIPT_PAGE_NAMESPACE.into(),
            logical_key: page_logical(id, index),
            value_json: raw,
            expected_version: Some(0),
            updated_at_ms: m.source.created_at_ms,
        },
        tx.owner_user_id(),
        MAX_FROZEN_TRANSCRIPT_PAGE_BYTES,
    )?;
    save(database, tx, &m, version)?;
    Ok(true)
}

pub(crate) fn recoverable_ranges(
    tx: &AuthorityTransaction,
    id: &str,
) -> io::Result<Vec<(EntityKey, EntityKey)>> {
    Ok(vec![
        manifest_key(tx, id)?.exact_range()?,
        EntityKey::prefix_range(
            tx.tenant_id(),
            tx.owner_user_id(),
            FROZEN_TRANSCRIPT_PAGE_NAMESPACE,
            &prefix(id)?,
        )?,
    ])
}
pub(crate) fn messages(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
    window: usize,
    before: Option<i64>,
) -> io::Result<(Vec<Value>, usize, usize, usize)> {
    let Some((m, _)) = load(database, tx, id)? else {
        return Ok((Vec::new(), 0, 0, 0));
    };
    if !m.complete {
        return Err(corrupt("frozen transcript import is incomplete"));
    }
    let end = before.map_or(m.messages, |b| {
        usize::try_from(b.max(0))
            .unwrap_or(usize::MAX)
            .min(m.messages)
    });
    let start = if window == 0 {
        0
    } else {
        end.saturating_sub(window)
    };
    let mut result = Vec::new();
    let mut bytes = 2usize;
    for (index, p) in m.pages.iter().enumerate() {
        if p.start + p.count <= start || p.start >= end {
            continue;
        }
        let page = parse_page(&page_bytes(database, tx, &m, index)?)?;
        if page.len() != p.count {
            return Err(corrupt("frozen transcript page count differs"));
        }
        for (offset, message) in page.into_iter().enumerate() {
            if (start..end).contains(&(p.start + offset)) {
                bytes = bytes
                    .checked_add(serde_json::to_vec(&message)?.len() + 1)
                    .ok_or_else(|| bounded("frozen transcript response byte overflow"))?;
                if bytes > MAX_FROZEN_TRANSCRIPT_PAGE_BYTES {
                    return Err(bounded("frozen transcript response exceeds page budget"));
                }
                result.push(message);
            }
        }
    }
    if result.len() != end - start {
        return Err(corrupt("frozen transcript message range differs"));
    }
    Ok((result, m.messages, start, end))
}
pub(crate) fn clone_history(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    source: &str,
    destination: &str,
    at: u64,
) -> io::Result<usize> {
    let Some((mut m, _)) = load(database, tx, source)? else {
        return Ok(0);
    };
    if !m.complete {
        return Err(corrupt("cannot clone an incomplete frozen transcript"));
    }
    if load(database, tx, destination)?.is_some() {
        return Err(conflict("cloned frozen transcript already exists"));
    }
    for index in 0..m.pages.len() {
        versioned_document::clone_stored_document(
            database,
            tx,
            versioned_document::CloneRequest {
                source_key: &page_key(tx, source, index)?,
                destination_key: page_key(tx, destination, index)?,
                namespace: FROZEN_TRANSCRIPT_PAGE_NAMESPACE,
                source_logical_key: &page_logical(source, index),
                destination_logical_key: &page_logical(destination, index),
                updated_at_ms: at,
            },
        )?;
    }
    m.source.conversation_id = destination.into();
    m.source.created_at_ms = at;
    save(database, tx, &m, 0)?;
    Ok(m.messages)
}

fn verify_physical_pages(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    m: &Manifest,
) -> io::Result<()> {
    // Independent physical catalog count excludes unreferenced extra pages.
    let ranges = recoverable_ranges(tx, &m.source.conversation_id)?;
    let mut cursor = ranges[1].0.clone();
    let mut seen = 0usize;
    loop {
        let page =
            database.entity_scan(tx, &cursor, &ranges[1].1, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
        seen += page.len();
        if seen > m.pages.len() {
            return Err(corrupt("frozen transcript physical page catalog differs"));
        }
        if page.len() < MIGRATION_SESSION_MAX_PAGE_ROWS {
            break;
        }
        let mut next = page.last().unwrap().0.key_bytes().to_vec();
        next.push(0);
        cursor = key(tx, FROZEN_TRANSCRIPT_PAGE_NAMESPACE, &next)?;
    }
    if seen != m.pages.len() {
        return Err(corrupt("frozen transcript physical page catalog differs"));
    }
    for index in 0..m.pages.len() {
        if parse_page(&page_bytes(database, tx, &m, index)?)?.len() != m.pages[index].count {
            return Err(corrupt("frozen transcript page count differs"));
        }
    }
    Ok(())
}

/// Independent verification also works through an immutable trash range pin.
pub(crate) fn verified_snapshot(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Value> {
    let Some((m, _)) = load(database, tx, id)? else {
        return Ok(Value::Null);
    };
    if !m.complete {
        return Err(corrupt("frozen transcript is incomplete"));
    }
    verify_physical_pages(database, tx, &m)?;
    Ok(serde_json::to_value(m)?)
}

pub(crate) fn execute(
    database: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    let mut tx = database.begin_with_identity_claim_scopes(tenant, owner)?;
    match operation {
        "migration.frozen.transcript.begin" => {
            let source: Begin = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid frozen transcript begin"))?;
            crate::conversation_header::require_active(database, &mut tx, &source.conversation_id)?;
            if let Some((m, _)) = load(database, &mut tx, &source.conversation_id)? {
                if m.source != source {
                    return Err(conflict("frozen transcript source differs"));
                }
                return Ok(json!({"inserted":false}));
            }
            save(
                database,
                &mut tx,
                &Manifest {
                    source,
                    complete: false,
                    messages: 0,
                    decoded_bytes: 0,
                    pages: Vec::new(),
                },
                0,
            )?;
            database.commit(tx)?;
            Ok(json!({"inserted":true}))
        }
        "migration.frozen.transcript.restore" => {
            let p: Restore = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid frozen transcript page"))?;
            let inserted = restore_page(
                database,
                &mut tx,
                &p.conversation_id,
                p.page_index,
                p.start,
                p.page_json.into_bytes(),
            )?;
            if inserted {
                database.commit(tx)?;
            }
            Ok(json!({"inserted":inserted}))
        }
        "migration.frozen.transcript.finish" => {
            let p: Finish = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid frozen transcript finish"))?;
            let (mut m, v) = load(database, &mut tx, &p.conversation_id)?
                .ok_or_else(|| invalid("frozen transcript begin missing"))?;
            if (p.page_count, p.message_count, p.decoded_bytes)
                != (m.pages.len(), m.messages, m.decoded_bytes)
            {
                return Err(conflict("frozen transcript completion totals differ"));
            }
            verify_physical_pages(database, &mut tx, &m)?;
            if !m.complete {
                m.complete = true;
                save(database, &mut tx, &m, v)?;
                crate::conversation_header::set_frozen_message_count(
                    database,
                    &mut tx,
                    &p.conversation_id,
                    m.messages as u64,
                )?;
                database.commit(tx)?;
            }
            Ok(
                json!({"complete":true,"message_count":m.messages,"page_count":m.pages.len(),"decoded_bytes":m.decoded_bytes}),
            )
        }
        "migration.frozen.transcript.get" => {
            let p: Identity = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid frozen transcript identity"))?;
            Ok(load(database, &mut tx, &p.conversation_id)?
                .map(|(m, _)| serde_json::to_value(m))
                .transpose()?
                .unwrap_or(Value::Null))
        }
        "migration.frozen.transcript.scan" => {
            let p: Scan = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid frozen transcript scan"))?;
            let mut suffix = Vec::new();
            if let Some(cursor) = p.cursor {
                let id = cursor
                    .strip_prefix("frozen_transcripts:")
                    .ok_or_else(|| invalid("invalid frozen transcript cursor"))?;
                suffix = prefix(id)?;
            }
            let start = key(&tx, FROZEN_TRANSCRIPT_MANIFEST_NAMESPACE, &suffix)?;
            let (_, end) =
                EntityKey::prefix_range(tenant, owner, FROZEN_TRANSCRIPT_MANIFEST_NAMESPACE, b"")?;
            let page =
                database.entity_scan(&mut tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
            let mut rows = Vec::new();
            for (key, _) in &page {
                let id = std::str::from_utf8(key.key_bytes())
                    .map_err(|_| corrupt("invalid frozen transcript catalog identity"))?;
                load(database, &mut tx, id)?
                    .ok_or_else(|| corrupt("frozen transcript manifest missing"))?;
                rows.push(json!({"conversation_id":id}));
            }
            let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
                rows.last().map(|r| {
                    format!(
                        "frozen_transcripts:{}",
                        r["conversation_id"].as_str().unwrap()
                    )
                })
            } else {
                None
            };
            Ok(json!({"rows":rows,"cursor":cursor}))
        }
        "migration.frozen.transcript.chunk" => {
            let p: Chunk = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid frozen transcript chunk"))?;
            let (m, _) = load(database, &mut tx, &p.conversation_id)?
                .ok_or_else(|| invalid("frozen transcript missing"))?;
            let raw = page_bytes(database, &mut tx, &m, p.page_index)?;
            if p.offset > raw.len() {
                return Err(invalid("frozen transcript chunk offset exceeds page"));
            }
            let end = (p.offset + MIGRATION_SESSION_MAX_SOURCE_CHUNK_BYTES).min(raw.len());
            let hex: String = raw[p.offset..end]
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect();
            Ok(
                json!({"page":m.pages[p.page_index],"offset":p.offset,"hex":hex,"final":end==raw.len()}),
            )
        }
        _ => Err(invalid("unknown frozen transcript operation")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::conversation_header::{
        self as header, CloneRequest, CreateRequest, DeleteRequest, RestoreRequest,
    };
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
    use std::{path::Path, sync::Arc};
    fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/frozen")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut db =
            AuthorityDatabase::initialize_with_vfs(Path::new("/frozen"), vfs.clone()).unwrap();
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        header::create(
            &db,
            &mut tx,
            &CreateRequest {
                conversation_id: "conv".into(),
                title: "historic".into(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 100,
                updated_at_ms: 101,
                committed_at_ms: 101,
            },
        )
        .unwrap();
        db.commit(tx).unwrap();
        call(&mut db,"begin",json!({"conversation_id":"conv","source_sha256":"a".repeat(64),"source_bytes":12,"created_at_ms":100})).unwrap();
        (vfs, db)
    }
    fn call(db: &mut AuthorityDatabase, op: &str, payload: Value) -> io::Result<Value> {
        execute(
            db,
            7,
            11,
            &format!("migration.frozen.transcript.{op}"),
            payload,
        )
    }
    fn put(
        db: &mut AuthorityDatabase,
        index: usize,
        start: usize,
        messages: Value,
    ) -> io::Result<Value> {
        call(
            db,
            "restore",
            json!({"conversation_id":"conv","page_index":index,"start":start,"page_json":serde_json::to_string(&messages).unwrap()}),
        )
    }
    fn finish(db: &mut AuthorityDatabase) -> io::Result<Value> {
        let m = call(db, "get", json!({"conversation_id":"conv"}))?;
        call(
            db,
            "finish",
            json!({"conversation_id":"conv","page_count":m["pages"].as_array().unwrap().len(),"message_count":m["messages"],"decoded_bytes":m["decoded_bytes"]}),
        )
    }
    fn history(
        db: &AuthorityDatabase,
        id: &str,
        window: usize,
        before: Option<i64>,
    ) -> io::Result<Value> {
        let mut tx = db.begin_with_identity_claim_scopes(7, 11)?;
        let bytes = header::get(db, &mut tx, id, true, window, before, &mut || false)?
            .ok_or_else(|| invalid("missing conversation"))?;
        Ok(serde_json::from_slice(&bytes)?)
    }
    #[test]
    fn paged_history_finishes_reopens_and_preserves_window_counts() {
        let (vfs, mut db) = prepared();
        assert!(history(&db, "conv", 0, None).is_err());
        put(&mut db,0,0,json!([{"role":"user","content":"原文"},{"role":"assistant","content":"reply","meta":{"n":7}}])).unwrap();
        assert!(history(&db, "conv", 0, None).is_err());
        put(&mut db, 1, 2, json!([{"role":"user","content":"third"}])).unwrap();
        finish(&mut db).unwrap();
        drop(db);
        vfs.crash().unwrap();
        let mut db = AuthorityDatabase::open_with_vfs(Path::new("/frozen"), vfs).unwrap();
        assert_eq!(
            history(&db, "conv", 1, Some(2)).unwrap()["messages"],
            json!([{"role":"assistant","content":"reply","meta":{"n":7}}])
        );
        let full = history(&db, "conv", 0, None).unwrap();
        assert_eq!(full["messages"].as_array().unwrap().len(), 3);
        assert_eq!(full["metadata"]["msg_count"], 3);
        finish(&mut db).unwrap();
        assert_eq!(
            execute(
                &mut db,
                7,
                12,
                "migration.frozen.transcript.get",
                json!({"conversation_id":"conv"})
            )
            .unwrap(),
            Value::Null
        );
        assert!(execute(&mut db,7,12,"migration.frozen.transcript.begin",json!({"conversation_id":"conv","source_sha256":"a".repeat(64),"source_bytes":12,"created_at_ms":100})).is_err());
        assert_eq!(
            call(&mut db, "scan", json!({})).unwrap()["rows"],
            json!([{"conversation_id":"conv"}])
        );
    }
    #[test]
    fn history_survives_clone_and_recoverable_delete_without_body_copy() {
        let (_, mut db) = prepared();
        let original = json!([{"role":"assistant","content":"large".repeat(4000)}]);
        put(&mut db, 0, 0, original.clone()).unwrap();
        finish(&mut db).unwrap();
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        header::clone_conversation(
            &db,
            &mut tx,
            &CloneRequest {
                source_conversation_id: "conv".into(),
                destination_conversation_id: "copy".into(),
                title: None,
                identity_seed: [7; 32],
                committed_at_ms: 200,
            },
        )
        .unwrap();
        db.commit(tx).unwrap();
        assert_eq!(history(&db, "copy", 0, None).unwrap()["messages"], original);
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        let source_key = page_key(&tx, "conv", 0).unwrap();
        let clone_key = page_key(&tx, "copy", 0).unwrap();
        let source = db.entity_get(&mut tx, &source_key).unwrap().unwrap();
        let copied = db.entity_get(&mut tx, &clone_key).unwrap().unwrap();
        assert_eq!(
            versioned_document::stored_blob_reference(&source).unwrap(),
            versioned_document::stored_blob_reference(&copied).unwrap()
        );
        assert!(versioned_document::stored_blob_reference(&source)
            .unwrap()
            .is_some());
        drop(tx);
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        header::delete(
            &mut db,
            &mut tx,
            &DeleteRequest {
                conversation_id: "conv".into(),
                deleted_at_ms: 300,
            },
        )
        .unwrap();
        db.commit(tx).unwrap();
        assert!(history(&db, "conv", 0, None).is_err());
        assert_eq!(history(&db, "copy", 0, None).unwrap()["messages"], original);
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        header::restore(
            &mut db,
            &mut tx,
            &RestoreRequest {
                conversation_id: "conv".into(),
                committed_at_ms: 400,
            },
        )
        .unwrap();
        db.commit(tx).unwrap();
        assert_eq!(history(&db, "conv", 0, None).unwrap()["messages"], original);
    }
    #[test]
    fn page_replay_and_catalog_corruption_fail_closed() {
        let (_, mut db) = prepared();
        let content = json!([{"content":"exact"}]);
        assert!(put(&mut db, 1, 0, content.clone()).is_err());
        put(&mut db, 0, 0, content.clone()).unwrap();
        assert_eq!(
            put(&mut db, 0, 0, content).unwrap(),
            json!({"inserted":false})
        );
        assert!(put(&mut db, 0, 0, json!([{"content":"different"}])).is_err());
        assert!(put(&mut db, 1, 1, json!([{"_tofuArchivedMessageCodec":{}}])).is_err());
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        let key = page_key(&tx, "conv", 1).unwrap();
        db.entity_put(&mut tx, key, b"extra".to_vec()).unwrap();
        db.commit(tx).unwrap();
        assert!(finish(&mut db).is_err());
        assert!(history(&db, "conv", 0, None).is_err());
    }
    #[test]
    fn large_page_upload_is_bounded_resumable_and_not_visible_before_finish() {
        let (_, mut db) = prepared();
        let text =
            serde_json::to_vec(&json!([{"role":"assistant","content":"x".repeat(9*1024*1024)}]))
                .unwrap();
        let begin = json!({"conversation_id":"conv","page_index":0,"start":0,"bytes":text.len(),"sha256":digest(&text)});
        let mut upload = upload::UploadState::default();
        upload
            .execute(
                &mut db,
                7,
                11,
                "migration.frozen.transcript.upload.begin",
                begin.clone(),
            )
            .unwrap();
        assert!(upload
            .execute(
                &mut db,
                7,
                11,
                "migration.frozen.transcript.upload.finish",
                json!({})
            )
            .is_err());
        assert!(!upload.has_pending());
        upload
            .execute(
                &mut db,
                7,
                11,
                "migration.frozen.transcript.upload.begin",
                begin,
            )
            .unwrap();
        for (i, chunk) in text
            .chunks(MIGRATION_SESSION_MAX_SOURCE_CHUNK_BYTES)
            .enumerate()
        {
            let hex: String = chunk.iter().map(|b| format!("{b:02x}")).collect();
            let p = json!({"offset":i*MIGRATION_SESSION_MAX_SOURCE_CHUNK_BYTES,"hex":hex});
            let accepted = upload
                .execute(
                    &mut db,
                    7,
                    11,
                    "migration.frozen.transcript.upload.append",
                    p.clone(),
                )
                .unwrap();
            if i == 0 {
                assert_eq!(
                    upload
                        .execute(
                            &mut db,
                            7,
                            11,
                            "migration.frozen.transcript.upload.append",
                            p
                        )
                        .unwrap(),
                    accepted
                );
            }
        }
        upload
            .execute(
                &mut db,
                7,
                11,
                "migration.frozen.transcript.upload.finish",
                json!({}),
            )
            .unwrap();
        assert!(!upload.has_pending());
        assert!(history(&db, "conv", 0, None).is_err());
        finish(&mut db).unwrap();
        assert_eq!(
            history(&db, "conv", 1, None).unwrap()["messages"][0]["content"]
                .as_str()
                .unwrap()
                .len(),
            9 * 1024 * 1024
        );
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        let listed = header::list(
            &db,
            &mut tx,
            &header::ListRequest {
                project_path: None,
                title_contains: None,
                ids: None,
                updated_at_gte: None,
                updated_at_gt: None,
                created_at_lt: None,
                order: header::ListOrder::UpdatedDescending,
                settings_keys: None,
                include_messages: true,
                limit: 1,
            },
            &mut || false,
        )
        .unwrap();
        assert!(listed.len() > 8 * 1024 * 1024);
        assert_eq!(
            serde_json::from_slice::<Value>(&listed).unwrap()[0]["messages"][0]["content"]
                .as_str()
                .unwrap()
                .len(),
            9 * 1024 * 1024
        );
        drop(tx);
        let chunk = call(
            &mut db,
            "chunk",
            json!({"conversation_id":"conv","page_index":0,"offset":0}),
        )
        .unwrap();
        assert_eq!(chunk["page"]["sha256"], digest(&text));
    }
    #[test]
    fn historical_fractional_timestamps_keep_exact_binary_float_values() {
        let (_, mut db) = prepared();
        let raw = r#"[{"role":"assistant","toolRounds":[{"tStart":1786362450651.2947,"tEnd":1786362465218.7253}]}]"#;
        call(
            &mut db,
            "restore",
            json!({"conversation_id":"conv","page_index":0,"start":0,"page_json":raw}),
        )
        .unwrap();
        finish(&mut db).unwrap();
        let result = history(&db, "conv", 0, None).unwrap();
        let round = &result["messages"][0]["toolRounds"][0];
        assert_eq!(
            round["tStart"].as_f64().unwrap().to_bits(),
            1786362450651.2947_f64.to_bits()
        );
        assert_eq!(
            round["tEnd"].as_f64().unwrap().to_bits(),
            1786362465218.7253_f64.to_bits()
        );
    }

    #[test]
    fn every_page_and_completion_fault_recovers_an_atomic_manifest_prefix() {
        for completing in [false, true] {
            let run = |db: &mut AuthorityDatabase| {
                if completing {
                    finish(db)
                } else {
                    put(db, 0, 0, json!([{"content":"x".repeat(20000)}]))
                }
            };
            let prepare = || {
                let (vfs, mut db) = prepared();
                if completing {
                    put(&mut db, 0, 0, json!([{"content":"x".repeat(20000)}])).unwrap();
                }
                vfs.arm_fault(None).unwrap();
                (vfs, db)
            };
            let (vfs, mut db) = prepare();
            run(&mut db).unwrap();
            let trace = vfs.trace().unwrap();
            drop(db);
            for (offset, operation) in trace.iter().enumerate() {
                for action in [
                    FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
                    FaultAction::ShortWrite(7),
                ] {
                    if matches!(action, FaultAction::ShortWrite(_))
                        && operation != &Operation::Write
                    {
                        continue;
                    }
                    let (vfs, mut db) = prepare();
                    vfs.arm_fault(Some(FaultRule {
                        operation_number: offset as u64 + 1,
                        action,
                    }))
                    .unwrap();
                    let _ = run(&mut db);
                    drop(db);
                    vfs.crash().unwrap();
                    vfs.arm_fault(None).unwrap();
                    let mut db =
                        AuthorityDatabase::open_with_vfs(Path::new("/frozen"), vfs).unwrap();
                    let m = call(&mut db, "get", json!({"conversation_id":"conv"})).unwrap();
                    assert!(m["pages"].as_array().unwrap().len() <= 1);
                    if m["complete"] == true {
                        assert_eq!(
                            history(&db, "conv", 0, None).unwrap()["metadata"]["msg_count"],
                            1
                        );
                    }
                    put(&mut db, 0, 0, json!([{"content":"x".repeat(20000)}])).unwrap();
                    finish(&mut db).unwrap();
                    assert_eq!(
                        history(&db, "conv", 0, None).unwrap()["messages"]
                            .as_array()
                            .unwrap()
                            .len(),
                        1
                    );
                }
            }
        }
    }
}
