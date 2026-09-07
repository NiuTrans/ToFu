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
    COMPACTION_ARCHIVE_MESSAGES_NAMESPACE, COMPACTION_ARCHIVE_RECEIPT_NAMESPACE,
    COMPACTION_ARCHIVE_SUMMARY_NAMESPACE, MAX_COMPACTION_ARCHIVES_PER_CONVERSATION,
    MAX_TRANSACTION_IR_LITERAL_BYTES,
};

const MAX_RECEIPT_BYTES: usize = 32 * 1024;

pub(crate) struct CreateRequest {
    pub archive_id: String,
    pub conversation_id: String,
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
    pub include_messages: bool,
}

pub(crate) struct ListRequest {
    pub conversation_id: String,
    pub limit: usize,
}

pub(crate) struct UpdateSummaryRequest {
    pub archive_id: String,
    pub summary: String,
    pub tokens_after: u64,
    pub msgs_after: u64,
    pub receipt_json: Option<Vec<u8>>,
    pub committed_at_ms: u64,
}

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
    created_at_ms: u64,
    archive_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = conversation_prefix(conversation_id)?;
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

fn count_key(transaction: &AuthorityTransaction, conversation_id: &str) -> io::Result<EntityKey> {
    key(
        transaction,
        COMPACTION_ARCHIVE_COUNT_NAMESPACE,
        conversation_id.as_bytes(),
    )
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

fn claim_value(owner_user_id: u64, conversation_id: &str) -> Vec<u8> {
    let mut value = Vec::with_capacity(8 + conversation_id.len());
    value.extend_from_slice(&owner_user_id.to_be_bytes());
    value.extend_from_slice(conversation_id.as_bytes());
    value
}

fn claim_matches(value: &[u8], owner_user_id: u64, conversation_id: &str) -> bool {
    value.len() == 8 + conversation_id.len()
        && value[..8] == owner_user_id.to_be_bytes()
        && value[8..] == *conversation_id.as_bytes()
}

fn load_value(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    archive_id: &str,
) -> io::Result<Option<(Value, u64)>> {
    let key = document_key(transaction, namespace, archive_id)?;
    let Some(encoded) =
        crate::versioned_document::get(database, transaction, &key, namespace, archive_id)?
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
    crate::versioned_document::put(
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

fn scan_index(
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
    let exact_count =
        decode_count(database.entity_get(transaction, &count_key(transaction, conversation_id)?)?)?;
    if exact_count > MAX_COMPACTION_ARCHIVES_PER_CONVERSATION as u64
        || exact_count != rows.len() as u64
    {
        return Err(invalid_data(
            "compaction archive count differs from its index",
        ));
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
    index_key: EntityKey,
    archive_id: &str,
) -> io::Result<()> {
    for namespace in [
        COMPACTION_ARCHIVE_DOCUMENT_NAMESPACE,
        COMPACTION_ARCHIVE_MESSAGES_NAMESPACE,
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
    if !claim_matches(&claim, transaction.owner_user_id(), conversation_id) {
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
    if request.messages_json.len() > MAX_TRANSACTION_IR_LITERAL_BYTES
        || request.receipt_json.len() > MAX_RECEIPT_BYTES
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
    let count_key = count_key(transaction, &request.conversation_id)?;
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
            request.created_at_ms,
            &request.archive_id,
        )?,
        request.archive_id.as_bytes().to_vec(),
    )?;
    database.entity_put(
        transaction,
        claim_key,
        claim_value(transaction.owner_user_id(), &request.conversation_id),
    )?;
    database.entity_put(transaction, count_key, (count + 1).to_le_bytes().to_vec())?;
    serde_json::to_vec(&json!({"created": true, "archiveId": request.archive_id}))
        .map_err(|_| invalid_data("archive create response cannot be encoded"))
}

pub(crate) fn list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &ListRequest,
) -> io::Result<Vec<u8>> {
    crate::conversation_header::require_active(database, transaction, &request.conversation_id)?;
    let rows = scan_index(database, transaction, &request.conversation_id)?;
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
    if response.len() > MAX_TRANSACTION_IR_LITERAL_BYTES {
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
    if string_field(&core, "conversation_id")? != request.conversation_id {
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
    let response = if request.include_messages {
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
        json!({"archive": archive, "messages": messages})
    } else {
        json!({"archive": archive})
    };
    let encoded = serde_json::to_vec(&response)
        .map_err(|_| invalid_data("archive get response cannot be encoded"))?;
    if encoded.len() > MAX_TRANSACTION_IR_LITERAL_BYTES {
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
    if claim.len() < 8 || claim[..8] != transaction.owner_user_id().to_be_bytes() {
        return Ok(br#"{"updated":false}"#.to_vec());
    }
    let conversation_id = std::str::from_utf8(&claim[8..])
        .map_err(|_| invalid_data("archive claim conversation is malformed"))?;
    if !crate::conversation_header::is_active(database, transaction, conversation_id)? {
        return Ok(br#"{"updated":false}"#.to_vec());
    }
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
) -> io::Result<Vec<u8>> {
    let rows = scan_index(database, transaction, conversation_id)?;
    let deleted = rows.len();
    for (index_key, value) in rows {
        delete_one(
            database,
            transaction,
            conversation_id,
            index_key,
            &archive_id_from_index(&value)?,
        )?;
    }
    database.entity_delete(transaction, count_key(transaction, conversation_id)?)?;
    serde_json::to_vec(&json!({"deleted": deleted}))
        .map_err(|_| invalid_data("archive deletion response cannot be encoded"))
}

pub(crate) fn prune(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    keep: usize,
) -> io::Result<Vec<u8>> {
    let rows = scan_index(database, transaction, conversation_id)?;
    let stale = rows.len().saturating_sub(keep);
    for (index_key, value) in rows.into_iter().take(stale) {
        delete_one(
            database,
            transaction,
            conversation_id,
            index_key,
            &archive_id_from_index(&value)?,
        )?;
    }
    let count_key = count_key(transaction, conversation_id)?;
    if stale > 0 {
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
    let rows = scan_index(database, transaction, source_conversation_id)?;
    if !scan_index(database, transaction, destination_conversation_id)?.is_empty() {
        return Err(invalid_data("clone destination already has archive state"));
    }
    let mut sources = BTreeSet::new();
    let mut destinations = BTreeSet::new();
    for (_, value) in &rows {
        let source_id = archive_id_from_index(value)?;
        if !sources.insert(source_id.clone()) {
            return Err(invalid_data("source archive index repeats an identity"));
        }
        let source_claim = database
            .entity_get(transaction, &global_claim_key(transaction, &source_id)?)?
            .ok_or_else(|| invalid_data("source archive claim is missing"))?;
        if !claim_matches(
            &source_claim,
            transaction.owner_user_id(),
            source_conversation_id,
        ) {
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
        database.entity_put(
            transaction,
            index_key(
                transaction,
                destination_conversation_id,
                created_at_ms,
                &destination_id,
            )?,
            destination_id.as_bytes().to_vec(),
        )?;
        database.entity_put(
            transaction,
            global_claim_key(transaction, &destination_id)?,
            claim_value(transaction.owner_user_id(), destination_conversation_id),
        )?;
    }
    if !rows.is_empty() {
        database.entity_put(
            transaction,
            count_key(transaction, destination_conversation_id)?,
            (rows.len() as u64).to_le_bytes().to_vec(),
        )?;
    }
    Ok(rows.len())
}
