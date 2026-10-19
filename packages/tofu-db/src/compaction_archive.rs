//! Owner-scoped immutable compaction history for conversation lifecycle.
//!
//! Metadata, messages, summary, and receipt are separate versioned documents:
//! summary updates never rewrite transcript blobs, while same-owner clones
//! re-key content-addressed payloads without copying their bytes. A compact
//! chronological index serves bounded history lists without transcript reads.

use std::collections::BTreeSet;
use std::io;

use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    COMPACTION_ARCHIVE_CONVERSATION_INDEX_NAMESPACE, COMPACTION_ARCHIVE_COUNT_NAMESPACE,
    COMPACTION_ARCHIVE_DOCUMENT_NAMESPACE, COMPACTION_ARCHIVE_ID_CLAIM_NAMESPACE,
    COMPACTION_ARCHIVE_MESSAGES_NAMESPACE, COMPACTION_ARCHIVE_POST_MESSAGES_NAMESPACE,
    COMPACTION_ARCHIVE_RECEIPT_NAMESPACE, COMPACTION_ARCHIVE_SUMMARY_NAMESPACE,
    MAX_COMPACTION_ARCHIVES_PER_CONVERSATION, MAX_COMPACTION_ARCHIVE_DOCUMENT_BYTES,
    MAX_LEGACY_DOCUMENT_BYTES,
};

#[path = "compaction_search_source.rs"]
pub(crate) mod search_source;

#[path = "compaction_archive_migration.rs"]
pub(crate) mod migration;

const MAX_RECEIPT_BYTES: usize = 32 * 1024;

pub(crate) struct CreateRequest {
    pub archive_id: String,
    pub conversation_id: String,
    pub archive_scope: String,
    pub messages_json: Vec<u8>,
    pub summary: String,
    pub receipt_json: Vec<u8>,
    pub trigger: String,
    pub task_id: String,
    pub round_num: u64,
    pub model: String,
    pub tokens_before: u64,
    pub tokens_after: u64,
    pub msgs_before: u64,
    pub msgs_after: u64,
    pub reason: String,
    pub created_at_ms: u64,
    pub committed_at_ms: u64,
}

pub(crate) struct GetRequest {
    pub conversation_id: String,
    pub archive_id: String,
    pub archive_scope: String,
    pub include_messages: bool,
    pub include_post_messages: bool,
}

pub(crate) struct ListRequest {
    pub conversation_id: String,
    pub archive_scope: String,
    pub limit: usize,
}

pub(crate) struct UpdateSummaryRequest {
    pub archive_id: String,
    pub archive_scope: String,
    pub summary: String,
    pub tokens_after: u64,
    pub msgs_after: u64,
    pub receipt_json: Option<Vec<u8>>,
    pub post_messages_json: Option<Vec<u8>>,
    pub committed_at_ms: u64,
}

/// Index discriminator bound: the scoped-index key layout marks entries with
/// a nonzero u16 after the conversation prefix, so root entries must keep
/// `created_at_ms` inside 48 bits (year 10894) for the two layouts to stay
/// unambiguous within one conversation prefix.
pub(crate) const MAX_ARCHIVE_CREATED_AT_MS: u64 = 1 << 48;

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, message)
}

fn key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    logical_key: &[u8],
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        logical_key,
    )
}

fn global_claim_key(transaction: &AuthorityTransaction, archive_id: &str) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        COMPACTION_ARCHIVE_ID_CLAIM_NAMESPACE,
        archive_id.as_bytes(),
    )
}

fn conversation_prefix(conversation_id: &str) -> io::Result<Vec<u8>> {
    let bytes = conversation_id.as_bytes();
    let mut output = Vec::with_capacity(2 + bytes.len());
    output.extend_from_slice(
        &u16::try_from(bytes.len())
            .map_err(|_| invalid_input("conversation identity exceeds its bound"))?
            .to_be_bytes(),
    );
    output.extend_from_slice(bytes);
    Ok(output)
}

fn index_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    archive_scope: &str,
    created_at_ms: u64,
    archive_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = conversation_prefix(conversation_id)?;
    if !archive_scope.is_empty() {
        // Scoped entries sort after every root entry (their leading u16 is
        // nonzero while root created_at_ms stays below 2^48), so one
        // conversation prefix holds both shapes unambiguously.
        encoded.extend_from_slice(
            &u16::try_from(archive_scope.len())
                .map_err(|_| invalid_input("archive scope exceeds its bound"))?
                .to_be_bytes(),
        );
        encoded.extend_from_slice(archive_scope.as_bytes());
    }
    encoded.extend_from_slice(&created_at_ms.to_be_bytes());
    encoded.extend_from_slice(
        &u16::try_from(archive_id.len())
            .map_err(|_| invalid_input("archive identity exceeds its bound"))?
            .to_be_bytes(),
    );
    encoded.extend_from_slice(archive_id.as_bytes());
    key(
        transaction,
        COMPACTION_ARCHIVE_CONVERSATION_INDEX_NAMESPACE,
        &encoded,
    )
}

fn count_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    archive_scope: &str,
) -> io::Result<EntityKey> {
    // The root-scope key keeps its original bare layout; scoped counts hang
    // off the length-prefixed conversation form so the two never collide.
    let raw = if archive_scope.is_empty() {
        conversation_id.as_bytes().to_vec()
    } else {
        let mut raw = conversation_prefix(conversation_id)?;
        raw.extend_from_slice(archive_scope.as_bytes());
        raw
    };
    key(transaction, COMPACTION_ARCHIVE_COUNT_NAMESPACE, &raw)
}

fn document_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    archive_id: &str,
) -> io::Result<EntityKey> {
    key(transaction, namespace, archive_id.as_bytes())
}

fn decode_count(value: Option<Vec<u8>>) -> io::Result<u64> {
    match value {
        None => Ok(0),
        Some(value) if value.len() == 8 => Ok(u64::from_le_bytes(value.try_into().unwrap())),
        Some(_) => Err(invalid_data("compaction archive count is malformed")),
    }
}

fn claim_value(owner_user_id: u64, archive_scope: &str, conversation_id: &str) -> Vec<u8> {
    // v2: owner(8) + u16 scope_len + scope + conversation. v1 claims predate
    // scopes and could only name the root scope, so they stay readable.
    let mut value = Vec::with_capacity(10 + archive_scope.len() + conversation_id.len());
    value.extend_from_slice(&owner_user_id.to_be_bytes());
    value.extend_from_slice(&(archive_scope.len() as u16).to_be_bytes());
    value.extend_from_slice(archive_scope.as_bytes());
    value.extend_from_slice(conversation_id.as_bytes());
    value
}

/// Decode a claim into (owner, scope, conversation), accepting the v1
/// scope-less layout (root scope only) and the v2 layout.
fn parse_claim(value: &[u8]) -> Option<(u64, String, String)> {
    if value.len() < 8 {
        return None;
    }
    let owner = u64::from_be_bytes(value[..8].try_into().ok()?);
    if value.len() >= 10 {
        let scope_len = u16::from_be_bytes(value[8..10].try_into().ok()?) as usize;
        if value.len() == 10 + scope_len + 1 || value.len() > 10 + scope_len {
            let scope = std::str::from_utf8(&value[10..10 + scope_len]).ok()?;
            let conversation = std::str::from_utf8(&value[10 + scope_len..]).ok()?;
            if !conversation.is_empty() {
                return Some((owner, scope.to_owned(), conversation.to_owned()));
            }
        }
    }
    // v1: owner + conversation, always the root scope. A v2 claim for the
    // root scope has an identical encoding when the conversation is empty,
    // which the conversation-identity bounds forbid.
    let conversation = std::str::from_utf8(&value[8..]).ok()?;
    if conversation.is_empty() {
        return None;
    }
    Some((owner, String::new(), conversation.to_owned()))
}

fn claim_matches(
    value: &[u8],
    owner_user_id: u64,
    archive_scope: &str,
    conversation_id: &str,
) -> bool {
    parse_claim(value).is_some_and(|(owner, scope, conversation)| {
        owner == owner_user_id && scope == archive_scope && conversation == conversation_id
    })
}

fn load_value(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    archive_id: &str,
) -> io::Result<Option<(Value, u64)>> {
    let key = document_key(transaction, namespace, archive_id)?;
    let Some(encoded) =
        crate::versioned_document::get_with_blob_owner_bounded(database, transaction, &key, namespace, archive_id, transaction.owner_user_id(), MAX_COMPACTION_ARCHIVE_DOCUMENT_BYTES.saturating_add(512))?
    else {
        return Ok(None);
    };
    let envelope: Value = serde_json::from_slice(&encoded)
        .map_err(|_| invalid_data("archive document envelope is malformed"))?;
    let version = envelope
        .get("version")
        .and_then(Value::as_u64)
        .filter(|version| *version > 0)
        .ok_or_else(|| invalid_data("archive document version is malformed"))?;
    let value = envelope
        .get("value")
        .cloned()
        .ok_or_else(|| invalid_data("archive document value is missing"))?;
    Ok(Some((value, version)))
}

fn put_value(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    archive_id: &str,
    value: &Value,
    expected_version: u64,
    committed_at_ms: u64,
) -> io::Result<()> {
    crate::versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: document_key(transaction, namespace, archive_id)?,
            namespace: namespace.to_owned(),
            logical_key: archive_id.to_owned(),
            value_json: serde_json::to_vec(value)
                .map_err(|_| invalid_data("archive value cannot be encoded"))?,
            expected_version: Some(expected_version),
            updated_at_ms: committed_at_ms,
        },
        transaction.owner_user_id(),
        MAX_COMPACTION_ARCHIVE_DOCUMENT_BYTES,
    )?;
    Ok(())
}

fn core_object(value: Value) -> io::Result<Map<String, Value>> {
    value
        .as_object()
        .cloned()
        .ok_or_else(|| invalid_data("archive metadata is malformed"))
}

fn summary_text(value: Value) -> io::Result<String> {
    value
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| invalid_data("archive summary is malformed"))
}

fn receipt_object(value: Value) -> io::Result<Map<String, Value>> {
    value
        .as_object()
        .cloned()
        .ok_or_else(|| invalid_data("archive receipt is malformed"))
}

fn string_field<'a>(core: &'a Map<String, Value>, field: &str) -> io::Result<&'a str> {
    core.get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("archive string field is malformed"))
}

fn u64_field(core: &Map<String, Value>, field: &str) -> io::Result<u64> {
    core.get(field)
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("archive integer field is malformed"))
}

fn metadata(
    core: &Map<String, Value>,
    summary: &str,
    receipt: &Map<String, Value>,
) -> io::Result<Value> {
    let task_model = string_field(core, "model")?;
    Ok(json!({
        "schemaVersion": "tofu.compaction-archive/v3",
        "id": string_field(core, "archive_id")?,
        "convId": string_field(core, "conversation_id")?,
        "createdAt": u64_field(core, "created_at_ms")?,
        "snapshotKind": "pre_compaction_transcript",
        "trigger": string_field(core, "trigger")?,
        "taskId": string_field(core, "task_id")?,
        "roundNum": u64_field(core, "round_num")?,
        "model": task_model,
        "taskModel": task_model,
        "tokensBefore": u64_field(core, "tokens_before")?,
        "tokensAfter": u64_field(core, "tokens_after")?,
        "tokenCountKind": "estimated",
        "msgsBefore": u64_field(core, "msgs_before")?,
        "msgsAfter": u64_field(core, "msgs_after")?,
        "reason": string_field(core, "reason")?,
        "payloadSize": u64_field(core, "payload_size")?,
        "payloadSizeUnit": "bytes",
        "summaryPreview": summary.chars().take(240).collect::<String>(),
        "hasSummary": !summary.is_empty(),
        "hasReceipt": !receipt.is_empty(),
        "resultStatus": receipt.get("status").and_then(Value::as_str).unwrap_or("legacy"),
        "resultStrategy": receipt.get("strategy").and_then(Value::as_str).unwrap_or(""),
    }))
}

fn archive_scope_of(core: &Map<String, Value>) -> io::Result<String> {
    match core.get("archive_scope") {
        None => Ok(String::new()),
        Some(Value::String(scope)) if scope.chars().count() <= 1024 => Ok(scope.clone()),
        _ => Err(invalid_data("archive scope is malformed")),
    }
}

fn scoped_index_prefix(conversation_id: &str, archive_scope: &str) -> io::Result<Vec<u8>> {
    let mut prefix = conversation_prefix(conversation_id)?;
    if !archive_scope.is_empty() {
        prefix.extend_from_slice(
            &u16::try_from(archive_scope.len())
                .map_err(|_| invalid_input("archive scope exceeds its bound"))?
                .to_be_bytes(),
        );
        prefix.extend_from_slice(archive_scope.as_bytes());
    }
    Ok(prefix)
}

/// Scan exactly one scope's chronological index and cross-check its exact
/// count record. The root scope's entries are a leading run of the
/// conversation prefix (zero u16 discriminator); scoped entries sort behind
/// them under their own sub-prefixes.
fn scan_index(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    archive_scope: &str,
) -> io::Result<Vec<(EntityKey, Vec<u8>)>> {
    let prefix = scoped_index_prefix(conversation_id, archive_scope)?;
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        COMPACTION_ARCHIVE_CONVERSATION_INDEX_NAMESPACE,
        &prefix,
    )?;
    let rows = database.entity_scan(
        transaction,
        &start,
        &end,
        MAX_COMPACTION_ARCHIVES_PER_CONVERSATION,
    )?;
    let discriminator_at = prefix.len();
    if rows.len() == MAX_COMPACTION_ARCHIVES_PER_CONVERSATION {
        // A full page may hide further entries behind the platform scan
        // bound; probe one row past the last key so an over-cap index always
        // fails closed instead of silently truncating.
        let mut probe_raw = rows
            .last()
            .expect("full page is nonempty")
            .0
            .key_bytes()
            .to_vec();
        probe_raw.push(0);
        let probe_start = key(
            transaction,
            COMPACTION_ARCHIVE_CONVERSATION_INDEX_NAMESPACE,
            &probe_raw,
        )?;
        let overflow = database
            .entity_scan(transaction, &probe_start, &end, 1)?
            .into_iter()
            .any(|(probe_key, _)| {
                // Behind a full root page, a scoped entry (nonzero
                // discriminator) is a neighbor, not overflow; a scoped
                // scan's sub-prefix admits no such neighbor.
                !archive_scope.is_empty()
                    || probe_key
                        .key_bytes()
                        .get(discriminator_at..discriminator_at + 2)
                        == Some(&[0u8, 0u8][..])
            });
        if overflow {
            return Err(invalid_data(
                "compaction archive index exceeds its conversation bound",
            ));
        }
    }
    let rows = if archive_scope.is_empty() {
        // The root scan's sub-prefix also covers every scoped sub-prefix
        // (they differ only in the discriminator bytes), so stop at the
        // first scoped entry instead of leaking another scope's archives.
        let discriminator_at = conversation_prefix(conversation_id)?.len();
        let cutoff = rows
            .iter()
            .position(|(index_key, _)| {
                let key = index_key.key_bytes();
                key.len() < discriminator_at + 2
                    || key[discriminator_at..discriminator_at + 2] != [0, 0]
            })
            .unwrap_or(rows.len());
        rows[..cutoff].to_vec()
    } else {
        rows
    };
    if rows.len() > MAX_COMPACTION_ARCHIVES_PER_CONVERSATION {
        return Err(invalid_data(
            "compaction archive index exceeds its conversation bound",
        ));
    }
    let exact_count = decode_count(
        database.entity_get(transaction, &count_key(transaction, conversation_id, archive_scope)?)?,
    )?;
    if exact_count > MAX_COMPACTION_ARCHIVES_PER_CONVERSATION as u64
        || exact_count != rows.len() as u64
    {
        return Err(invalid_data(
            "compaction archive count differs from its index",
        ));
    }
    Ok(rows)
}

/// Scan the whole conversation index across every scope (clone reads the
/// complete history; the legacy authority applies one 1,000-archive cap to
/// the clone source regardless of scope).
fn scan_index_all(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<Vec<(EntityKey, Vec<u8>)>> {
    let prefix = conversation_prefix(conversation_id)?;
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        COMPACTION_ARCHIVE_CONVERSATION_INDEX_NAMESPACE,
        &prefix,
    )?;
    let rows = database.entity_scan(
        transaction,
        &start,
        &end,
        MAX_COMPACTION_ARCHIVES_PER_CONVERSATION,
    )?;
    if rows.len() == MAX_COMPACTION_ARCHIVES_PER_CONVERSATION {
        let mut probe_raw = rows
            .last()
            .expect("full page is nonempty")
            .0
            .key_bytes()
            .to_vec();
        probe_raw.push(0);
        let probe_start = key(
            transaction,
            COMPACTION_ARCHIVE_CONVERSATION_INDEX_NAMESPACE,
            &probe_raw,
        )?;
        if !database
            .entity_scan(transaction, &probe_start, &end, 1)?
            .is_empty()
        {
            return Err(invalid_data(
                "compaction archive index exceeds its conversation bound",
            ));
        }
    }
    Ok(rows)
}

fn archive_id_from_index(value: &[u8]) -> io::Result<String> {
    std::str::from_utf8(value)
        .ok()
        .filter(|value| !value.is_empty() && value.chars().count() <= 128)
        .map(str::to_owned)
        .ok_or_else(|| invalid_data("compaction archive index is malformed"))
}

fn load_metadata(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    archive_id: &str,
    conversation_id: &str,
) -> io::Result<Value> {
    let core = load_value(
        database,
        transaction,
        COMPACTION_ARCHIVE_DOCUMENT_NAMESPACE,
        archive_id,
    )?
    .ok_or_else(|| invalid_data("archive metadata is missing"))?
    .0;
    let core = core_object(core)?;
    if string_field(&core, "archive_id")? != archive_id
        || string_field(&core, "conversation_id")? != conversation_id
    {
        return Err(invalid_data("archive metadata identity is inconsistent"));
    }
    let summary = load_value(
        database,
        transaction,
        COMPACTION_ARCHIVE_SUMMARY_NAMESPACE,
        archive_id,
    )?
    .ok_or_else(|| invalid_data("archive summary is missing"))?
    .0;
    let receipt = load_value(
        database,
        transaction,
        COMPACTION_ARCHIVE_RECEIPT_NAMESPACE,
        archive_id,
    )?
    .ok_or_else(|| invalid_data("archive receipt is missing"))?
    .0;
    metadata(&core, &summary_text(summary)?, &receipt_object(receipt)?)
}

fn delete_one(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    archive_scope: &str,
    index_key: EntityKey,
    archive_id: &str,
) -> io::Result<()> {
    for namespace in [
        COMPACTION_ARCHIVE_DOCUMENT_NAMESPACE,
        COMPACTION_ARCHIVE_MESSAGES_NAMESPACE,
        COMPACTION_ARCHIVE_POST_MESSAGES_NAMESPACE,
        COMPACTION_ARCHIVE_SUMMARY_NAMESPACE,
        COMPACTION_ARCHIVE_RECEIPT_NAMESPACE,
    ] {
        database.entity_delete(
            transaction,
            document_key(transaction, namespace, archive_id)?,
        )?;
    }
    let claim_key = global_claim_key(transaction, archive_id)?;
    let claim = database
        .entity_get(transaction, &claim_key)?
        .ok_or_else(|| invalid_data("compaction archive claim is missing"))?;
    if !claim_matches(&claim, transaction.owner_user_id(), archive_scope, conversation_id) {
        return Err(invalid_data("compaction archive claim is inconsistent"));
    }
    database.entity_delete(transaction, claim_key)?;
    database.entity_delete(transaction, index_key)?;
    Ok(())
}

pub(crate) fn create(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &CreateRequest,
) -> io::Result<Vec<u8>> {
    crate::conversation_header::require_active(database, transaction, &request.conversation_id)?;
    require_archive_scope(database, transaction, &request.archive_scope, &request.conversation_id)?;
    create_contents(database, transaction, request)
}

/// A nonempty archive scope requires the exact registered child birth
/// evidence (legacy `_archive_scope`); the root scope is always open.
fn require_archive_scope(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    archive_scope: &str,
    conversation_id: &str,
) -> io::Result<()> {
    if !archive_scope.is_empty() {
        crate::invocation::require_child(database, transaction, archive_scope, conversation_id)?;
    }
    Ok(())
}

// Offline snapshots may retain archives whose conversation is in trash or
// absent. Live entry points still require the active parent above; all reads
// keep that same parent fence, so restoring history cannot expose it early.
fn create_contents(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &CreateRequest,
) -> io::Result<Vec<u8>> {
    let messages: Value = serde_json::from_slice(&request.messages_json)
        .map_err(|_| invalid_input("archive messages are invalid"))?;
    let messages = messages
        .as_array()
        .filter(|items| {
            items.iter().all(|item| {
                item.as_object().is_some_and(|message| {
                    !message.contains_key("_tofuArchivedMessageCodec")
                        && !message.contains_key("_tofuStorageProjectionCodec")
                })
            })
        })
        .ok_or_else(|| invalid_input("archive messages must be an array of objects"))?;
    if request.messages_json.len() > MAX_COMPACTION_ARCHIVE_DOCUMENT_BYTES
        || request.receipt_json.len() > MAX_RECEIPT_BYTES
        || request.created_at_ms >= MAX_ARCHIVE_CREATED_AT_MS
    {
        return Err(invalid_input("archive payload exceeds its bound"));
    }
    let receipt: Value = serde_json::from_slice(&request.receipt_json)
        .map_err(|_| invalid_input("archive receipt is invalid"))?;
    let receipt = receipt
        .as_object()
        .ok_or_else(|| invalid_input("archive receipt must be an object"))?;
    let claim_key = global_claim_key(transaction, &request.archive_id)?;
    if let Some(claim) = database.entity_get(transaction, &claim_key)? {
        if !claim_matches(
            &claim,
            transaction.owner_user_id(),
            &request.archive_scope,
            &request.conversation_id,
        ) {
            return Err(conflict("archive id has a conflicting payload"));
        }
        let existing = load_value(
            database,
            transaction,
            COMPACTION_ARCHIVE_MESSAGES_NAMESPACE,
            &request.archive_id,
        )?
        .ok_or_else(|| invalid_data("claimed archive messages are missing"))?
        .0;
        if existing != Value::Array(messages.clone()) {
            return Err(conflict("archive id has a conflicting payload"));
        }
        return serde_json::to_vec(&json!({"created": false, "archiveId": request.archive_id}))
            .map_err(|_| invalid_data("archive create response cannot be encoded"));
    }
    let count_key = count_key(transaction, &request.conversation_id, &request.archive_scope)?;
    let count = decode_count(database.entity_get(transaction, &count_key)?)?;
    if count >= MAX_COMPACTION_ARCHIVES_PER_CONVERSATION as u64 {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "compaction archive collection is full",
        ));
    }
    let core = json!({
        "archive_id": request.archive_id,
        "conversation_id": request.conversation_id,
        "archive_scope": request.archive_scope,
        "trigger": request.trigger,
        "task_id": request.task_id,
        "round_num": request.round_num,
        "model": request.model,
        "tokens_before": request.tokens_before,
        "tokens_after": request.tokens_after,
        "msgs_before": request.msgs_before,
        "msgs_after": request.msgs_after,
        "reason": request.reason,
        "payload_size": request.messages_json.len(),
        "created_at_ms": request.created_at_ms,
    });
    put_value(
        database,
        transaction,
        COMPACTION_ARCHIVE_DOCUMENT_NAMESPACE,
        &request.archive_id,
        &core,
        0,
        request.committed_at_ms,
    )?;
    put_value(
        database,
        transaction,
        COMPACTION_ARCHIVE_MESSAGES_NAMESPACE,
        &request.archive_id,
        &Value::Array(messages.clone()),
        0,
        request.committed_at_ms,
    )?;
    put_value(
        database,
        transaction,
        COMPACTION_ARCHIVE_SUMMARY_NAMESPACE,
        &request.archive_id,
        &Value::String(request.summary.clone()),
        0,
        request.committed_at_ms,
    )?;
    put_value(
        database,
        transaction,
        COMPACTION_ARCHIVE_RECEIPT_NAMESPACE,
        &request.archive_id,
        &Value::Object(receipt.clone()),
        0,
        request.committed_at_ms,
    )?;
    database.entity_put(
        transaction,
        index_key(
            transaction,
            &request.conversation_id,
            &request.archive_scope,
            request.created_at_ms,
            &request.archive_id,
        )?,
        request.archive_id.as_bytes().to_vec(),
    )?;
    database.entity_put(
        transaction,
        claim_key,
        claim_value(
            transaction.owner_user_id(),
            &request.archive_scope,
            &request.conversation_id,
        ),
    )?;
    database.entity_put(transaction, count_key, (count + 1).to_le_bytes().to_vec())?;
    crate::search_dirty::mark(
        database,
        transaction,
        crate::generated_tofudb_ir::CONVERSATION_SEARCH_DIRTY_NAMESPACE,
        &request.conversation_id,
    )?;
    serde_json::to_vec(&json!({"created": true, "archiveId": request.archive_id}))
        .map_err(|_| invalid_data("archive create response cannot be encoded"))
}

pub(crate) fn list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &ListRequest,
) -> io::Result<Vec<u8>> {
    crate::conversation_header::require_active(database, transaction, &request.conversation_id)?;
    require_archive_scope(database, transaction, &request.archive_scope, &request.conversation_id)?;
    let rows = scan_index(database, transaction, &request.conversation_id, &request.archive_scope)?;
    let mut archives = Vec::with_capacity(rows.len().min(request.limit));
    let mut identities = BTreeSet::new();
    for (_, value) in rows.into_iter().take(request.limit) {
        let archive_id = archive_id_from_index(&value)?;
        if !identities.insert(archive_id.clone()) {
            return Err(invalid_data("archive index repeats an identity"));
        }
        archives.push(load_metadata(
            database,
            transaction,
            &archive_id,
            &request.conversation_id,
        )?);
    }
    let response = serde_json::to_vec(&json!({"archives": archives}))
        .map_err(|_| invalid_data("archive list response cannot be encoded"))?;
    if response.len() > MAX_LEGACY_DOCUMENT_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "archive list response exceeds 8 MiB",
        ));
    }
    Ok(response)
}

pub(crate) fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &GetRequest,
) -> io::Result<Option<Vec<u8>>> {
    crate::conversation_header::require_active(database, transaction, &request.conversation_id)?;
    require_archive_scope(database, transaction, &request.archive_scope, &request.conversation_id)?;
    let Some((core, _)) = load_value(
        database,
        transaction,
        COMPACTION_ARCHIVE_DOCUMENT_NAMESPACE,
        &request.archive_id,
    )?
    else {
        return Ok(None);
    };
    let core = core_object(core)?;
    if string_field(&core, "conversation_id")? != request.conversation_id
        || archive_scope_of(&core)? != request.archive_scope
    {
        return Ok(None);
    }
    let summary = load_value(
        database,
        transaction,
        COMPACTION_ARCHIVE_SUMMARY_NAMESPACE,
        &request.archive_id,
    )?
    .ok_or_else(|| invalid_data("archive summary is missing"))?
    .0;
    let summary = summary_text(summary)?;
    let receipt = load_value(
        database,
        transaction,
        COMPACTION_ARCHIVE_RECEIPT_NAMESPACE,
        &request.archive_id,
    )?
    .ok_or_else(|| invalid_data("archive receipt is missing"))?
    .0;
    let receipt = receipt_object(receipt)?;
    let mut archive = metadata(&core, &summary, &receipt)?
        .as_object()
        .cloned()
        .expect("archive metadata is an object");
    archive.insert("summary".to_owned(), Value::String(summary));
    archive.insert("receipt".to_owned(), Value::Object(receipt));
    archive.insert(
        "messagesCount".to_owned(),
        Value::from(u64_field(&core, "msgs_before")?),
    );
    let mut response = Map::new();
    if request.include_messages {
        let messages = load_value(
            database,
            transaction,
            COMPACTION_ARCHIVE_MESSAGES_NAMESPACE,
            &request.archive_id,
        )?
        .ok_or_else(|| invalid_data("archive messages are missing"))?
        .0;
        let count = messages
            .as_array()
            .ok_or_else(|| invalid_data("archive messages are malformed"))?
            .len();
        archive.insert("messagesCount".to_owned(), Value::from(count));
        response.insert("messages".to_owned(), messages);
    }
    if request.include_post_messages {
        let post_messages = load_value(
            database,
            transaction,
            COMPACTION_ARCHIVE_POST_MESSAGES_NAMESPACE,
            &request.archive_id,
        )?
        .map(|(value, _)| value)
        .unwrap_or(Value::Null);
        response.insert("postMessages".to_owned(), post_messages);
    }
    response.insert("archive".to_owned(), Value::Object(archive));
    let encoded = serde_json::to_vec(&Value::Object(response))
        .map_err(|_| invalid_data("archive get response cannot be encoded"))?;
    if encoded.len() > MAX_COMPACTION_ARCHIVE_DOCUMENT_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "archive response exceeds 8 MiB",
        ));
    }
    Ok(Some(encoded))
}

pub(crate) fn update_summary(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &UpdateSummaryRequest,
) -> io::Result<Vec<u8>> {
    let claim_key = global_claim_key(transaction, &request.archive_id)?;
    let Some(claim) = database.entity_get(transaction, &claim_key)? else {
        return Ok(br#"{"updated":false}"#.to_vec());
    };
    let Some((claim_owner, claim_scope, conversation_id)) = parse_claim(&claim) else {
        return Err(invalid_data("archive claim is malformed"));
    };
    if claim_owner != transaction.owner_user_id() || claim_scope != request.archive_scope {
        return Ok(br#"{"updated":false}"#.to_vec());
    }
    if !crate::conversation_header::is_active(database, transaction, &conversation_id)? {
        return Ok(br#"{"updated":false}"#.to_vec());
    }
    // The legacy authority revalidates the child's exact birth evidence here
    // (and only here, after proving the archive exists in this scope).
    require_archive_scope(database, transaction, &request.archive_scope, &conversation_id)?;
    let (core, core_version) = load_value(
        database,
        transaction,
        COMPACTION_ARCHIVE_DOCUMENT_NAMESPACE,
        &request.archive_id,
    )?
    .ok_or_else(|| invalid_data("claimed archive metadata is missing"))?;
    let mut core = core_object(core)?;
    let (_, summary_version) = load_value(
        database,
        transaction,
        COMPACTION_ARCHIVE_SUMMARY_NAMESPACE,
        &request.archive_id,
    )?
    .ok_or_else(|| invalid_data("archive summary is missing"))?;
    put_value(
        database,
        transaction,
        COMPACTION_ARCHIVE_SUMMARY_NAMESPACE,
        &request.archive_id,
        &Value::String(request.summary.clone()),
        summary_version,
        request.committed_at_ms,
    )?;
    if let Some(receipt_json) = &request.receipt_json {
        if receipt_json.len() > MAX_RECEIPT_BYTES {
            return Err(invalid_input("archive receipt exceeds 32 KiB"));
        }
        let receipt: Value = serde_json::from_slice(receipt_json)
            .map_err(|_| invalid_input("archive receipt is invalid"))?;
        let receipt = receipt_object(receipt)
            .map_err(|_| invalid_input("archive receipt must be an object"))?;
        let (_, version) = load_value(
            database,
            transaction,
            COMPACTION_ARCHIVE_RECEIPT_NAMESPACE,
            &request.archive_id,
        )?
        .ok_or_else(|| invalid_data("archive receipt is missing"))?;
        put_value(
            database,
            transaction,
            COMPACTION_ARCHIVE_RECEIPT_NAMESPACE,
            &request.archive_id,
            &Value::Object(receipt),
            version,
            request.committed_at_ms,
        )?;
    }
    if let Some(post_messages_json) = &request.post_messages_json {
        if post_messages_json.len() > MAX_COMPACTION_ARCHIVE_DOCUMENT_BYTES {
            return Err(invalid_input("archive post messages exceed their bound"));
        }
        let post_messages: Value = serde_json::from_slice(post_messages_json)
            .map_err(|_| invalid_input("archive post messages are invalid"))?;
        let post_messages = post_messages
            .as_array()
            .filter(|items| {
                items.iter().all(|item| {
                    item.as_object().is_some_and(|message| {
                        !message.contains_key("_tofuArchivedMessageCodec")
                            && !message.contains_key("_tofuStorageProjectionCodec")
                    })
                })
            })
            .ok_or_else(|| invalid_input("archive post messages must be an array of objects"))?;
        let post_version = load_value(
            database,
            transaction,
            COMPACTION_ARCHIVE_POST_MESSAGES_NAMESPACE,
            &request.archive_id,
        )?
        .map(|(_, version)| version)
        .unwrap_or(0);
        put_value(
            database,
            transaction,
            COMPACTION_ARCHIVE_POST_MESSAGES_NAMESPACE,
            &request.archive_id,
            &Value::Array(post_messages.clone()),
            post_version,
            request.committed_at_ms,
        )?;
    }
    core.insert("tokens_after".to_owned(), Value::from(request.tokens_after));
    core.insert("msgs_after".to_owned(), Value::from(request.msgs_after));
    put_value(
        database,
        transaction,
        COMPACTION_ARCHIVE_DOCUMENT_NAMESPACE,
        &request.archive_id,
        &Value::Object(core),
        core_version,
        request.committed_at_ms,
    )?;
    Ok(br#"{"updated":true}"#.to_vec())
}

pub(crate) fn delete_conversation(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    archive_scope: &str,
) -> io::Result<Vec<u8>> {
    crate::conversation_header::require_active(database, transaction, conversation_id)?;
    require_archive_scope(database, transaction, archive_scope, conversation_id)?;
    let rows = scan_index(database, transaction, conversation_id, archive_scope)?;
    let deleted = rows.len();
    for (index_key, value) in rows {
        delete_one(
            database,
            transaction,
            conversation_id,
            archive_scope,
            index_key,
            &archive_id_from_index(&value)?,
        )?;
    }
    if deleted > 0 {
        crate::search_dirty::mark(
            database,
            transaction,
            crate::generated_tofudb_ir::CONVERSATION_SEARCH_DIRTY_NAMESPACE,
            conversation_id,
        )?;
    }
    database.entity_delete(transaction, count_key(transaction, conversation_id, archive_scope)?)?;
    serde_json::to_vec(&json!({"deleted": deleted}))
        .map_err(|_| invalid_data("archive deletion response cannot be encoded"))
}

/// Conversation lifecycle purge removes the complete history of one
/// conversation across every scope; the operator-facing
/// `compaction_archive.delete_conversation` op deletes exactly one scope.
pub(crate) fn delete_conversation_all_scopes(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<Vec<u8>> {
    let rows = scan_index_all(database, transaction, conversation_id)?;
    let deleted = rows.len();
    let mut scopes = BTreeSet::new();
    for (index_key, value) in rows {
        let archive_id = archive_id_from_index(&value)?;
        let (core, _) = load_value(
            database,
            transaction,
            COMPACTION_ARCHIVE_DOCUMENT_NAMESPACE,
            &archive_id,
        )?
        .ok_or_else(|| invalid_data("archive metadata is missing"))?;
        let archive_scope = archive_scope_of(&core_object(core)?)?;
        scopes.insert(archive_scope.clone());
        delete_one(
            database,
            transaction,
            conversation_id,
            &archive_scope,
            index_key,
            &archive_id,
        )?;
    }
    if deleted > 0 {
        crate::search_dirty::mark(
            database,
            transaction,
            crate::generated_tofudb_ir::CONVERSATION_SEARCH_DIRTY_NAMESPACE,
            conversation_id,
        )?;
    }
    for scope in scopes {
        database.entity_delete(transaction, count_key(transaction, conversation_id, &scope)?)?;
    }
    serde_json::to_vec(&json!({"deleted": deleted}))
        .map_err(|_| invalid_data("archive deletion response cannot be encoded"))
}

pub(crate) fn prune(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    archive_scope: &str,
    keep: usize,
) -> io::Result<Vec<u8>> {
    crate::conversation_header::require_active(database, transaction, conversation_id)?;
    require_archive_scope(database, transaction, archive_scope, conversation_id)?;
    let rows = scan_index(database, transaction, conversation_id, archive_scope)?;
    let stale = rows.len().saturating_sub(keep);
    for (index_key, value) in rows.into_iter().take(stale) {
        delete_one(
            database,
            transaction,
            conversation_id,
            archive_scope,
            index_key,
            &archive_id_from_index(&value)?,
        )?;
    }
    let count_key = count_key(transaction, conversation_id, archive_scope)?;
    if stale > 0 {
        crate::search_dirty::mark(
            database,
            transaction,
            crate::generated_tofudb_ir::CONVERSATION_SEARCH_DIRTY_NAMESPACE,
            conversation_id,
        )?;
        database.entity_put(transaction, count_key, (keep as u64).to_le_bytes().to_vec())?;
    }
    serde_json::to_vec(&json!({"deleted": stale}))
        .map_err(|_| invalid_data("archive prune response cannot be encoded"))
}

pub(crate) fn clone_conversation_archives(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    source_conversation_id: &str,
    destination_conversation_id: &str,
    identity_seed: [u8; 32],
    committed_at_ms: u64,
) -> io::Result<usize> {
    let rows = scan_index_all(database, transaction, source_conversation_id)?;
    if !scan_index_all(database, transaction, destination_conversation_id)?.is_empty() {
        return Err(invalid_data("clone destination already has archive state"));
    }
    let mut sources = BTreeSet::new();
    let mut destinations = BTreeSet::new();
    let mut scope_counts = std::collections::BTreeMap::<String, u64>::new();
    for (_, value) in &rows {
        let source_id = archive_id_from_index(value)?;
        if !sources.insert(source_id.clone()) {
            return Err(invalid_data("source archive index repeats an identity"));
        }
        let source_claim = database
            .entity_get(transaction, &global_claim_key(transaction, &source_id)?)?
            .ok_or_else(|| invalid_data("source archive claim is missing"))?;
        let Some((claim_owner, _, claim_conversation)) = parse_claim(&source_claim) else {
            return Err(invalid_data("source archive claim is inconsistent"));
        };
        if claim_owner != transaction.owner_user_id()
            || claim_conversation != source_conversation_id
        {
            return Err(invalid_data("source archive claim is inconsistent"));
        }
        let destination_id = format!(
            "clone-archive-{}",
            crate::turn::stable_clone_identity(&identity_seed, b"archive", &source_id)
        );
        if !destinations.insert(destination_id.clone())
            || database
                .entity_get(
                    transaction,
                    &global_claim_key(transaction, &destination_id)?,
                )?
                .is_some()
        {
            return Err(conflict("cloned archive identity already exists"));
        }
    }
    for (_, index_value) in &rows {
        let source_id = archive_id_from_index(index_value)?;
        let destination_id = format!(
            "clone-archive-{}",
            crate::turn::stable_clone_identity(&identity_seed, b"archive", &source_id)
        );
        let (core, _) = load_value(
            database,
            transaction,
            COMPACTION_ARCHIVE_DOCUMENT_NAMESPACE,
            &source_id,
        )?
        .ok_or_else(|| invalid_data("source archive metadata is missing"))?;
        let mut core = core_object(core)?;
        if string_field(&core, "archive_id")? != source_id
            || string_field(&core, "conversation_id")? != source_conversation_id
        {
            return Err(invalid_data(
                "source archive metadata identity is inconsistent",
            ));
        }
        let created_at_ms = u64_field(&core, "created_at_ms")?;
        let archive_scope = archive_scope_of(&core)?;
        {
            let source_claim = database
                .entity_get(transaction, &global_claim_key(transaction, &source_id)?)?
                .ok_or_else(|| invalid_data("source archive claim is missing"))?;
            if !claim_matches(
                &source_claim,
                transaction.owner_user_id(),
                &archive_scope,
                source_conversation_id,
            ) {
                return Err(invalid_data("source archive claim is inconsistent"));
            }
        }
        let source_task_id = string_field(&core, "task_id")?.to_owned();
        let destination_task_id = if source_task_id.is_empty() {
            String::new()
        } else {
            format!(
                "clone-task-{}",
                crate::turn::stable_clone_identity(&identity_seed, b"task", &source_task_id)
            )
        };
        core.insert(
            "archive_id".to_owned(),
            Value::String(destination_id.clone()),
        );
        core.insert(
            "conversation_id".to_owned(),
            Value::String(destination_conversation_id.to_owned()),
        );
        core.insert(
            "task_id".to_owned(),
            Value::String(destination_task_id.clone()),
        );
        put_value(
            database,
            transaction,
            COMPACTION_ARCHIVE_DOCUMENT_NAMESPACE,
            &destination_id,
            &Value::Object(core),
            0,
            committed_at_ms,
        )?;
        for namespace in [
            COMPACTION_ARCHIVE_MESSAGES_NAMESPACE,
            COMPACTION_ARCHIVE_SUMMARY_NAMESPACE,
            COMPACTION_ARCHIVE_RECEIPT_NAMESPACE,
        ] {
            crate::versioned_document::clone_stored_document(
                database,
                transaction,
                crate::versioned_document::CloneRequest {
                    source_key: &document_key(transaction, namespace, &source_id)?,
                    destination_key: document_key(transaction, namespace, &destination_id)?,
                    namespace,
                    source_logical_key: &source_id,
                    destination_logical_key: &destination_id,
                    updated_at_ms: committed_at_ms,
                },
            )?;
        }
        // Post-compaction messages exist only for archives written after a
        // successful compression, so absence is normal and must not fail the
        // clone the way a missing messages/summary/receipt document would.
        if load_value(
            database,
            transaction,
            COMPACTION_ARCHIVE_POST_MESSAGES_NAMESPACE,
            &source_id,
        )?
        .is_some()
        {
            crate::versioned_document::clone_stored_document(
                database,
                transaction,
                crate::versioned_document::CloneRequest {
                    source_key: &document_key(
                        transaction,
                        COMPACTION_ARCHIVE_POST_MESSAGES_NAMESPACE,
                        &source_id,
                    )?,
                    destination_key: document_key(
                        transaction,
                        COMPACTION_ARCHIVE_POST_MESSAGES_NAMESPACE,
                        &destination_id,
                    )?,
                    namespace: COMPACTION_ARCHIVE_POST_MESSAGES_NAMESPACE,
                    source_logical_key: &source_id,
                    destination_logical_key: &destination_id,
                    updated_at_ms: committed_at_ms,
                },
            )?;
        }
        database.entity_put(
            transaction,
            index_key(
                transaction,
                destination_conversation_id,
                &archive_scope,
                created_at_ms,
                &destination_id,
            )?,
            destination_id.as_bytes().to_vec(),
        )?;
        database.entity_put(
            transaction,
            global_claim_key(transaction, &destination_id)?,
            claim_value(
                transaction.owner_user_id(),
                &archive_scope,
                destination_conversation_id,
            ),
        )?;
        *scope_counts.entry(archive_scope).or_insert(0) += 1;
    }
    for (scope, count) in scope_counts {
        database.entity_put(
            transaction,
            count_key(transaction, destination_conversation_id, &scope)?,
            count.to_le_bytes().to_vec(),
        )?;
    }
    Ok(rows.len())
}
