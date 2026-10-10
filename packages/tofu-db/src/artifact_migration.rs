//! Offline chat-artifact and tool-result snapshot restoration for the
//! exclusive operator importer.
//!
//! Two independent sub-domains share this adapter because both route large
//! content through the same owner-bound blob staging inside one authority
//! transaction:
//!
//! - `chat_artifacts` preserves the exact metadata projection plus the exact
//!   content bytes under the native digest document, and rebuilds the dedupe,
//!   conversation, library, parent, path-head and per-conversation count
//!   indexes atomically. A deleted (`deleted_at > 0`) snapshot retains its
//!   document and dedupe claim but, exactly like a live delete, carries no
//!   library/parent/path-head index rows and its path head falls back to the
//!   newest live parent with the same path. The live create path recomputes
//!   identity (parent, version, count) instead of accepting the source
//!   projection, so migration writes the exact projection directly.
//! - `tool_result_artifacts` preserves the exact content bytes, media type
//!   and lifecycle timestamps and rebuilds the expiry index atomically. The
//!   live put path merges timestamps and restamps access time with the server
//!   clock, so it cannot preserve the exact source lifecycle.
//!
//! Both get operations return the exact snapshot spelling (metadata plus
//! content) and both scans enumerate identities owner-scoped and independent
//! of the source. Insert-or-match keeps replays idempotent: an identical
//! snapshot is read-only, a divergent occupant fails closed. Entry points:
//! the six `migration.chat.artifact.*`/`migration.tool.result.*` operations
//! dispatched from `migration_session`.

use serde::Deserialize;
use serde_json::{json, Value};
use std::io::{self, Cursor};

use crate::artifact::{path_from_source_ref, python_casefold};
use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::blob::BlobReference;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    ARTIFACT_CONVERSATION_INDEX_NAMESPACE, ARTIFACT_DEDUPE_NAMESPACE, ARTIFACT_DOCUMENT_NAMESPACE,
    ARTIFACT_LIBRARY_INDEX_NAMESPACE, ARTIFACT_PARENT_INDEX_NAMESPACE,
    ARTIFACT_PATH_HEAD_NAMESPACE, MAX_ARTIFACT_BYTES, MAX_TOOL_RESULT_ARTIFACT_BYTES,
    MIGRATION_SESSION_MAX_PAGE_ROWS, TOOL_RESULT_ARTIFACT_NAMESPACE,
    TOOL_RESULT_EXPIRY_INDEX_NAMESPACE,
};

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn snapshot_conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, message)
}

fn digest(parts: &[&[u8]]) -> [u8; 32] {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(b"tofu-db:artifact-key:v1\0");
    for part in parts {
        hasher.update(part.len().to_be_bytes());
        hasher.update(part);
    }
    hasher.finalize().into()
}

fn hex_digest(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let digest = Sha256::digest(bytes);
    let mut output = String::with_capacity(64);
    for byte in digest {
        output.push(HEX[(byte >> 4) as usize] as char);
        output.push(HEX[(byte & 0x0f) as usize] as char);
    }
    output
}

fn key(transaction: &AuthorityTransaction, namespace: &str, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        raw,
    )
}

fn hashed_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    parts: &[&[u8]],
) -> io::Result<EntityKey> {
    key(transaction, namespace, &digest(parts))
}

fn descending(value: u64) -> [u8; 8] {
    (u64::MAX - value).to_be_bytes()
}

fn identity_value(id: &str, conv_id: &str) -> io::Result<Vec<u8>> {
    serde_json::to_vec(&json!({"id": id, "conv_id": conv_id}))
        .map_err(|_| invalid_data("artifact identity cannot be encoded"))
}

fn materialize_blob(
    database: &AuthorityDatabase,
    transaction: &AuthorityTransaction,
    reference: BlobReference,
    maximum: usize,
) -> io::Result<Vec<u8>> {
    if reference.logical_bytes > maximum as u64 {
        return Err(invalid_data("artifact blob exceeds its logical bound"));
    }
    let mut reader = database.blob_reader(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        reference,
    )?;
    let mut output = Vec::with_capacity(reference.logical_bytes as usize);
    while let Some(chunk) = reader.next_chunk()? {
        if output.len().saturating_add(chunk.len()) > maximum {
            return Err(invalid_data(
                "artifact blob expanded beyond its logical bound",
            ));
        }
        output.extend_from_slice(&chunk);
    }
    Ok(output)
}

// ---------------------------------------------------------------------------
// chat_artifacts
// ---------------------------------------------------------------------------

const MAX_CHAT_ARTIFACT_ROWS_PER_CONVERSATION: u64 = 128;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ChatRestoreRequest {
    id: String,
    conv_id: String,
    task_id: String,
    msg_id: String,
    source: String,
    source_ref: Value,
    format: String,
    title: String,
    content: String,
    content_sha256: String,
    size_bytes: u64,
    version: u64,
    parent_id: String,
    pinned: bool,
    meta: Value,
    created_at: u64,
    deleted_at: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ChatGetRequest {
    id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ChatScanRequest {
    cursor: Option<String>,
}

fn valid_chat_request(request: &ChatRestoreRequest) -> bool {
    !request.id.is_empty()
        && request.id.chars().count() <= 256
        && !request.conv_id.is_empty()
        && request.conv_id.chars().count() <= 512
        && request.task_id.chars().count() <= 512
        && request.msg_id.chars().count() <= 512
        && !request.source.is_empty()
        && request.source.chars().count() <= 256
        && matches!(request.format.as_str(), "markdown" | "html" | "svg")
        && request.title.chars().count() <= 300
        && request.parent_id.chars().count() <= 256
        && request.version > 0
        && (request.version == 1) == request.parent_id.is_empty()
        && request.content_sha256.len() == 64
        && request
            .content_sha256
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        && request.content_sha256 == hex_digest(request.content.as_bytes())
        && request.content.len() <= MAX_ARTIFACT_BYTES
        && request.size_bytes == request.content.len() as u64
        && request.source_ref.is_object()
        && request.meta.is_object()
        && request.created_at > 0
}

fn chat_document_json(request: &ChatRestoreRequest, content: &BlobReference) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "id": request.id,
        "conv_id": request.conv_id,
        "task_id": request.task_id,
        "msg_id": request.msg_id,
        "source": request.source,
        "source_ref": request.source_ref,
        "format": request.format,
        "title": request.title,
        "content_sha256": request.content_sha256,
        "size_bytes": request.size_bytes,
        "version": request.version,
        "parent_id": request.parent_id,
        "pinned": request.pinned,
        "meta": request.meta,
        "created_at": request.created_at,
        "deleted_at": request.deleted_at,
        "content": {
            "blob_id": content.blob_id.0,
            "manifest_block_id": content.manifest_block_id.0,
            "logical_bytes": content.logical_bytes,
        },
    }))
    .expect("chat artifact document always encodes")
}

fn chat_document_key(transaction: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    hashed_key(transaction, ARTIFACT_DOCUMENT_NAMESPACE, &[id.as_bytes()])
}

fn chat_conversation_index_key(
    transaction: &AuthorityTransaction,
    request: &ChatRestoreRequest,
) -> io::Result<EntityKey> {
    let mut raw = digest(&[request.conv_id.as_bytes()]).to_vec();
    raw.extend_from_slice(&descending(request.created_at));
    raw.extend_from_slice(&digest(&[request.id.as_bytes()]));
    key(transaction, ARTIFACT_CONVERSATION_INDEX_NAMESPACE, &raw)
}

fn chat_library_index_key(
    transaction: &AuthorityTransaction,
    request: &ChatRestoreRequest,
) -> io::Result<EntityKey> {
    let mut raw = vec![u8::from(!request.pinned)];
    raw.extend_from_slice(&descending(request.created_at));
    raw.extend_from_slice(&digest(&[request.id.as_bytes()]));
    key(transaction, ARTIFACT_LIBRARY_INDEX_NAMESPACE, &raw)
}

fn chat_parent_index_key(
    transaction: &AuthorityTransaction,
    request: &ChatRestoreRequest,
) -> io::Result<EntityKey> {
    let mut raw = digest(&[request.parent_id.as_bytes()]).to_vec();
    raw.extend_from_slice(&request.version.to_be_bytes());
    raw.extend_from_slice(&request.created_at.to_be_bytes());
    raw.extend_from_slice(&digest(&[request.id.as_bytes()]));
    key(transaction, ARTIFACT_PARENT_INDEX_NAMESPACE, &raw)
}

fn chat_count_key(transaction: &AuthorityTransaction, conv_id: &str) -> io::Result<EntityKey> {
    hashed_key(
        transaction,
        "artifact_conversation_counts",
        &[conv_id.as_bytes()],
    )
}

fn read_chat_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conv_id: &str,
) -> io::Result<u64> {
    match database.entity_get(transaction, &chat_count_key(transaction, conv_id)?)? {
        None => Ok(0),
        Some(raw) if raw.len() == 8 => Ok(u64::from_le_bytes(raw.try_into().unwrap())),
        Some(_) => Err(invalid_data("artifact count is malformed")),
    }
}

fn write_chat_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &ChatRestoreRequest,
) -> io::Result<()> {
    let encoded = request.content.as_bytes();
    let reference =
        database.stage_blob(transaction, &mut Cursor::new(encoded), encoded.len() as u64)?;
    let document = chat_document_json(request, &reference);
    database.entity_put(
        transaction,
        chat_document_key(transaction, &request.id)?,
        document,
    )?;
    let identity = identity_value(&request.id, &request.conv_id)?;
    database.entity_put(
        transaction,
        hashed_key(
            transaction,
            ARTIFACT_DEDUPE_NAMESPACE,
            &[
                python_casefold(&request.conv_id).as_bytes(),
                request.content_sha256.as_bytes(),
            ],
        )?,
        identity.clone(),
    )?;
    database.entity_put(
        transaction,
        chat_conversation_index_key(transaction, request)?,
        identity.clone(),
    )?;
    if request.deleted_at == 0 {
        database.entity_put(
            transaction,
            chat_library_index_key(transaction, request)?,
            identity.clone(),
        )?;
        if let Some(path) = path_from_source_ref(&request.source_ref) {
            database.entity_put(
                transaction,
                hashed_key(
                    transaction,
                    ARTIFACT_PATH_HEAD_NAMESPACE,
                    &[
                        python_casefold(&request.conv_id).as_bytes(),
                        path.as_bytes(),
                    ],
                )?,
                identity.clone(),
            )?;
        }
    }
    if !request.parent_id.is_empty() {
        database.entity_put(
            transaction,
            chat_parent_index_key(transaction, request)?,
            identity,
        )?;
    }
    if request.deleted_at != 0 {
        if let Some(path) = path_from_source_ref(&request.source_ref) {
            // Mirror the live delete fallback: the path head drops to the newest
            // live parent sharing the path, or is removed entirely.
            let head_key = hashed_key(
                transaction,
                ARTIFACT_PATH_HEAD_NAMESPACE,
                &[
                    python_casefold(&request.conv_id).as_bytes(),
                    path.as_bytes(),
                ],
            )?;
            let mut replacement: Option<Vec<u8>> = None;
            if !request.parent_id.is_empty() {
                if let Some(raw) = database.entity_get(
                    transaction,
                    &chat_document_key(transaction, &request.parent_id)?,
                )? {
                    let parent: Value = serde_json::from_slice(&raw)
                        .map_err(|_| invalid_data("artifact document is malformed"))?;
                    let live = parent.get("deleted_at").and_then(Value::as_u64) == Some(0)
                        && parent.get("source_ref").and_then(path_from_source_ref) == Some(path)
                        && parent.get("conv_id").and_then(Value::as_str)
                            == Some(request.conv_id.as_str());
                    if live {
                        replacement = Some(identity_value(&request.parent_id, &request.conv_id)?);
                    }
                }
            }
            match replacement {
                Some(value) => database.entity_put(transaction, head_key, value)?,
                None => database.entity_delete(transaction, head_key)?,
            }
        }
    }
    let count = read_chat_count(database, transaction, &request.conv_id)?;
    if count >= MAX_CHAT_ARTIFACT_ROWS_PER_CONVERSATION {
        return Err(invalid_input("artifact conversation quota reached"));
    }
    database.entity_put(
        transaction,
        chat_count_key(transaction, &request.conv_id)?,
        (count + 1).to_le_bytes().to_vec(),
    )
}

fn read_chat_snapshot(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<ChatRestoreRequest>> {
    let Some(raw) = database.entity_get(transaction, &chat_document_key(transaction, id)?)? else {
        return Ok(None);
    };
    let value: Value =
        serde_json::from_slice(&raw).map_err(|_| invalid_data("artifact document is malformed"))?;
    let object = value
        .as_object()
        .filter(|object| object.len() == 17)
        .ok_or_else(|| invalid_data("artifact document is malformed"))?;
    let text = |name: &str| -> io::Result<String> {
        object
            .get(name)
            .and_then(Value::as_str)
            .map(str::to_owned)
            .ok_or_else(|| invalid_data("artifact document is malformed"))
    };
    let unsigned = |name: &str| -> io::Result<u64> {
        object
            .get(name)
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("artifact document is malformed"))
    };
    let reference = decode_blob_reference(object.get("content"))?;
    let content = materialize_blob(database, transaction, reference, MAX_ARTIFACT_BYTES)?;
    let content =
        String::from_utf8(content).map_err(|_| invalid_data("artifact content is not UTF-8"))?;
    Ok(Some(ChatRestoreRequest {
        id: text("id")?,
        conv_id: text("conv_id")?,
        task_id: text("task_id")?,
        msg_id: text("msg_id")?,
        source: text("source")?,
        source_ref: object
            .get("source_ref")
            .cloned()
            .filter(Value::is_object)
            .ok_or_else(|| invalid_data("artifact document is malformed"))?,
        format: text("format")?,
        title: text("title")?,
        content,
        content_sha256: text("content_sha256")?,
        size_bytes: unsigned("size_bytes")?,
        version: unsigned("version")?,
        parent_id: text("parent_id")?,
        pinned: object
            .get("pinned")
            .and_then(Value::as_bool)
            .ok_or_else(|| invalid_data("artifact document is malformed"))?,
        meta: object
            .get("meta")
            .cloned()
            .filter(Value::is_object)
            .ok_or_else(|| invalid_data("artifact document is malformed"))?,
        created_at: unsigned("created_at")?,
        deleted_at: unsigned("deleted_at")?,
    }))
}

/// Metadata-only round trip used inside the staging transaction: blob chunks
/// become readable only after the transaction commits, so the readback here
/// validates every non-content field plus the blob reference length.
fn read_chat_metadata(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<Value>> {
    let Some(raw) = database.entity_get(transaction, &chat_document_key(transaction, id)?)? else {
        return Ok(None);
    };
    let value: Value =
        serde_json::from_slice(&raw).map_err(|_| invalid_data("artifact document is malformed"))?;
    let mut object = value
        .as_object()
        .cloned()
        .ok_or_else(|| invalid_data("artifact document is malformed"))?;
    let reference = decode_blob_reference(object.get("content"))?;
    object.insert("content".to_owned(), Value::from(reference.logical_bytes));
    Ok(Some(Value::Object(object)))
}

fn chat_metadata_value(request: &ChatRestoreRequest) -> Value {
    let mut value = chat_snapshot_value(request);
    let object = value.as_object_mut().expect("snapshot is an object");
    object.insert("content".to_owned(), Value::from(request.size_bytes));
    value
}

fn chat_restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: ChatRestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid chat artifact restore request"))?;
    if !valid_chat_request(&request) {
        return Err(invalid_input("invalid chat artifact snapshot"));
    }
    if let Some(existing) = read_chat_snapshot(database, transaction, &request.id)? {
        if chat_snapshot_value(&existing) == chat_snapshot_value(&request) {
            return Ok(false);
        }
        return Err(snapshot_conflict("chat artifact snapshot differs"));
    }
    if !request.parent_id.is_empty() {
        let Some(parent) = read_chat_snapshot(database, transaction, &request.parent_id)? else {
            return Err(invalid_input("chat artifact parent is missing"));
        };
        if parent.conv_id != request.conv_id {
            return Err(invalid_input(
                "chat artifact parent is outside the conversation",
            ));
        }
    }
    // A live dedupe claim under a different identity must never alias.
    if let Some(raw) = database.entity_get(
        transaction,
        &hashed_key(
            transaction,
            ARTIFACT_DEDUPE_NAMESPACE,
            &[
                python_casefold(&request.conv_id).as_bytes(),
                request.content_sha256.as_bytes(),
            ],
        )?,
    )? {
        if raw != identity_value(&request.id, &request.conv_id)? {
            return Err(snapshot_conflict("chat artifact dedupe claim differs"));
        }
    }
    write_chat_document(database, transaction, &request)?;
    let written = read_chat_metadata(database, transaction, &request.id)?
        .ok_or_else(|| invalid_data("chat artifact snapshot is missing"))?;
    if written != chat_metadata_value(&request) {
        return Err(invalid_data("chat artifact snapshot readback differs"));
    }
    Ok(true)
}

fn chat_snapshot_value(request: &ChatRestoreRequest) -> Value {
    json!({
        "id": request.id,
        "conv_id": request.conv_id,
        "task_id": request.task_id,
        "msg_id": request.msg_id,
        "source": request.source,
        "source_ref": request.source_ref,
        "format": request.format,
        "title": request.title,
        "content": request.content,
        "content_sha256": request.content_sha256,
        "size_bytes": request.size_bytes,
        "version": request.version,
        "parent_id": request.parent_id,
        "pinned": request.pinned,
        "meta": request.meta,
        "created_at": request.created_at,
        "deleted_at": request.deleted_at,
    })
}

fn chat_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: ChatGetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid chat artifact get request"))?;
    match read_chat_snapshot(database, transaction, &request.id)? {
        None => Ok(Value::Null),
        Some(snapshot) => Ok(chat_snapshot_value(&snapshot)),
    }
}

fn chat_scan(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: ChatScanRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid chat artifact scan request"))?;
    let mut start = Vec::new();
    if let Some(cursor) = request.cursor.as_deref() {
        let encoded = cursor
            .strip_prefix("chat_artifacts:")
            .ok_or_else(|| invalid_input("invalid chat artifact catalog cursor"))?;
        let raw = hex_decode(encoded)?;
        if raw.is_empty() || raw.len() > 64 {
            return Err(invalid_input("invalid chat artifact catalog cursor"));
        }
        start = raw;
        start.push(0);
    }
    let start = key(transaction, ARTIFACT_DOCUMENT_NAMESPACE, &start)?;
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        ARTIFACT_DOCUMENT_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(transaction, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, raw) in &page {
        let value: Value = serde_json::from_slice(raw)
            .map_err(|_| invalid_data("artifact catalog document is malformed"))?;
        let id = value
            .get("id")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("artifact catalog document is malformed"))?;
        if &chat_document_key(transaction, id)? != physical {
            return Err(invalid_data("artifact catalog identity differs"));
        }
        rows.push(json!({"id": id}));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        Some(format!(
            "chat_artifacts:{}",
            page.last()
                .expect("a full page has a last row")
                .0
                .key_bytes()
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>()
        ))
    } else {
        None
    };
    Ok(json!({"rows": rows, "cursor": cursor}))
}

fn decode_blob_reference(value: Option<&Value>) -> io::Result<BlobReference> {
    let blob = value
        .and_then(Value::as_object)
        .filter(|blob| blob.len() == 3)
        .ok_or_else(|| invalid_data("artifact blob reference is malformed"))?;
    let read_digest = |name: &str| -> io::Result<[u8; 32]> {
        let bytes = blob
            .get(name)
            .and_then(Value::as_array)
            .filter(|bytes| bytes.len() == 32)
            .ok_or_else(|| invalid_data("artifact blob reference is malformed"))?;
        let mut output = [0_u8; 32];
        for (index, byte) in bytes.iter().enumerate() {
            output[index] = byte
                .as_u64()
                .and_then(|value| u8::try_from(value).ok())
                .ok_or_else(|| invalid_data("artifact blob reference is malformed"))?;
        }
        Ok(output)
    };
    Ok(BlobReference {
        blob_id: crate::blob::BlobId(read_digest("blob_id")?),
        manifest_block_id: crate::block::BlockId(read_digest("manifest_block_id")?),
        logical_bytes: blob
            .get("logical_bytes")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("artifact blob reference is malformed"))?,
    })
}

fn hex_decode(encoded: &str) -> io::Result<Vec<u8>> {
    if encoded.len() > 128 || !encoded.len().is_multiple_of(2) {
        return Err(invalid_input("invalid migration catalog cursor"));
    }
    encoded
        .as_bytes()
        .as_chunks::<2>()
        .0
        .iter()
        .map(|pair| {
            let text = std::str::from_utf8(pair)
                .map_err(|_| invalid_input("invalid migration catalog cursor"))?;
            u8::from_str_radix(text, 16)
                .map_err(|_| invalid_input("invalid migration catalog cursor"))
        })
        .collect()
}

// ---------------------------------------------------------------------------
// tool_result_artifacts
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ToolRestoreRequest {
    content_sha256: String,
    content: String,
    media_type: String,
    size_bytes: u64,
    created_at_ms: u64,
    expires_at_ms: u64,
    last_accessed_at_ms: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ToolGetRequest {
    content_sha256: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ToolScanRequest {
    cursor: Option<String>,
}

fn tool_document_key(
    transaction: &AuthorityTransaction,
    digest_hex: &str,
) -> io::Result<EntityKey> {
    hashed_key(
        transaction,
        TOOL_RESULT_ARTIFACT_NAMESPACE,
        &[digest_hex.as_bytes()],
    )
}

fn tool_expiry_key(
    transaction: &AuthorityTransaction,
    expires_at_ms: u64,
    digest_hex: &str,
) -> io::Result<EntityKey> {
    let mut raw = expires_at_ms.to_be_bytes().to_vec();
    raw.extend_from_slice(&digest(&[digest_hex.as_bytes()]));
    key(transaction, TOOL_RESULT_EXPIRY_INDEX_NAMESPACE, &raw)
}

fn valid_tool_request(request: &ToolRestoreRequest) -> bool {
    request.content_sha256.len() == 64
        && request
            .content_sha256
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        && request.content_sha256 == hex_digest(request.content.as_bytes())
        && !request.content.is_empty()
        && request.content.len() <= MAX_TOOL_RESULT_ARTIFACT_BYTES
        && request.size_bytes == request.content.len() as u64
        && !request.media_type.trim().is_empty()
        && request.media_type.chars().count() <= 128
        && request.created_at_ms > 0
        && request.expires_at_ms > request.created_at_ms
        && request.last_accessed_at_ms > 0
}

fn tool_document_json(request: &ToolRestoreRequest, content: &BlobReference) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "content_sha256": request.content_sha256,
        "media_type": request.media_type,
        "size_bytes": request.size_bytes,
        "created_at_ms": request.created_at_ms,
        "expires_at_ms": request.expires_at_ms,
        "last_accessed_at_ms": request.last_accessed_at_ms,
        "content": {
            "blob_id": content.blob_id.0,
            "manifest_block_id": content.manifest_block_id.0,
            "logical_bytes": content.logical_bytes,
        },
    }))
    .expect("tool-result document always encodes")
}

fn tool_snapshot_value(request: &ToolRestoreRequest) -> Value {
    json!({
        "content_sha256": request.content_sha256,
        "content": request.content,
        "media_type": request.media_type,
        "size_bytes": request.size_bytes,
        "created_at_ms": request.created_at_ms,
        "expires_at_ms": request.expires_at_ms,
        "last_accessed_at_ms": request.last_accessed_at_ms,
    })
}

fn read_tool_snapshot(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    digest_hex: &str,
) -> io::Result<Option<ToolRestoreRequest>> {
    let Some(raw) =
        database.entity_get(transaction, &tool_document_key(transaction, digest_hex)?)?
    else {
        return Ok(None);
    };
    let value: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("tool-result document is malformed"))?;
    let object = value
        .as_object()
        .filter(|object| object.len() == 7)
        .ok_or_else(|| invalid_data("tool-result document is malformed"))?;
    let text = |name: &str| -> io::Result<String> {
        object
            .get(name)
            .and_then(Value::as_str)
            .map(str::to_owned)
            .ok_or_else(|| invalid_data("tool-result document is malformed"))
    };
    let unsigned = |name: &str| -> io::Result<u64> {
        object
            .get(name)
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("tool-result document is malformed"))
    };
    let reference = decode_blob_reference(object.get("content"))?;
    let content = materialize_blob(
        database,
        transaction,
        reference,
        MAX_TOOL_RESULT_ARTIFACT_BYTES,
    )?;
    let content =
        String::from_utf8(content).map_err(|_| invalid_data("tool-result content is not UTF-8"))?;
    Ok(Some(ToolRestoreRequest {
        content_sha256: text("content_sha256")?,
        content,
        media_type: text("media_type")?,
        size_bytes: unsigned("size_bytes")?,
        created_at_ms: unsigned("created_at_ms")?,
        expires_at_ms: unsigned("expires_at_ms")?,
        last_accessed_at_ms: unsigned("last_accessed_at_ms")?,
    }))
}

fn tool_restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: ToolRestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid tool result restore request"))?;
    if !valid_tool_request(&request) {
        return Err(invalid_input("invalid tool result snapshot"));
    }
    if let Some(existing) = read_tool_snapshot(database, transaction, &request.content_sha256)? {
        if tool_snapshot_value(&existing) == tool_snapshot_value(&request) {
            return Ok(false);
        }
        return Err(snapshot_conflict("tool result snapshot differs"));
    }
    let encoded = request.content.as_bytes();
    let reference =
        database.stage_blob(transaction, &mut Cursor::new(encoded), encoded.len() as u64)?;
    database.entity_put(
        transaction,
        tool_document_key(transaction, &request.content_sha256)?,
        tool_document_json(&request, &reference),
    )?;
    database.entity_put(
        transaction,
        tool_expiry_key(transaction, request.expires_at_ms, &request.content_sha256)?,
        request.content_sha256.as_bytes().to_vec(),
    )?;
    // Same-transaction readback validates metadata plus the blob reference;
    // content bytes are verified by the sha256 identity and the committed
    // read path.
    let raw = database
        .entity_get(
            transaction,
            &tool_document_key(transaction, &request.content_sha256)?,
        )?
        .ok_or_else(|| invalid_data("tool result snapshot is missing"))?;
    let value: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("tool-result document is malformed"))?;
    let mut object = value
        .as_object()
        .cloned()
        .ok_or_else(|| invalid_data("tool-result document is malformed"))?;
    let reference = decode_blob_reference(object.get("content"))?;
    object.insert("content".to_owned(), Value::from(reference.logical_bytes));
    let mut expected = tool_snapshot_value(&request);
    expected
        .as_object_mut()
        .expect("snapshot is an object")
        .insert("content".to_owned(), Value::from(request.size_bytes));
    if Value::Object(object) != expected {
        return Err(invalid_data("tool result snapshot readback differs"));
    }
    Ok(true)
}

fn tool_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: ToolGetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid tool result get request"))?;
    match read_tool_snapshot(database, transaction, &request.content_sha256)? {
        None => Ok(Value::Null),
        Some(snapshot) => Ok(tool_snapshot_value(&snapshot)),
    }
}

fn tool_scan(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: ToolScanRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid tool result scan request"))?;
    let mut start = Vec::new();
    if let Some(cursor) = request.cursor.as_deref() {
        let encoded = cursor
            .strip_prefix("tool_results:")
            .ok_or_else(|| invalid_input("invalid tool result catalog cursor"))?;
        let raw = hex_decode(encoded)?;
        if raw.is_empty() || raw.len() > 64 {
            return Err(invalid_input("invalid tool result catalog cursor"));
        }
        start = raw;
        start.push(0);
    }
    let start = key(transaction, TOOL_RESULT_ARTIFACT_NAMESPACE, &start)?;
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TOOL_RESULT_ARTIFACT_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(transaction, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, raw) in &page {
        let value: Value = serde_json::from_slice(raw)
            .map_err(|_| invalid_data("tool-result catalog document is malformed"))?;
        let digest_hex = value
            .get("content_sha256")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("tool-result catalog document is malformed"))?;
        if &tool_document_key(transaction, digest_hex)? != physical {
            return Err(invalid_data("tool-result catalog identity differs"));
        }
        rows.push(json!({"content_sha256": digest_hex}));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        Some(format!(
            "tool_results:{}",
            page.last()
                .expect("a full page has a last row")
                .0
                .key_bytes()
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>()
        ))
    } else {
        None
    };
    Ok(json!({"rows": rows, "cursor": cursor}))
}

pub(crate) fn execute(
    database: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    let mut transaction = database.begin(tenant, owner)?;
    match operation {
        "migration.chat.artifact.restore" => {
            let inserted = chat_restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.chat.artifact.get" => chat_get(database, &mut transaction, payload),
        "migration.chat.artifact.scan" => chat_scan(database, &mut transaction, payload),
        "migration.tool.result.restore" => {
            let inserted = tool_restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.tool.result.get" => tool_get(database, &mut transaction, payload),
        "migration.tool.result.scan" => tool_scan(database, &mut transaction, payload),
        _ => Err(invalid_input("unknown artifact migration operation")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Vfs};
    use std::path::Path;
    use std::sync::Arc;

    fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        (vfs, database)
    }

    fn chat_snapshot(id: &str, content: &str, deleted_at: u64) -> Value {
        json!({
            "id": id,
            "conv_id": "conv-1",
            "task_id": "",
            "msg_id": "",
            "source": "chat",
            "source_ref": {"path": "notes/a.md"},
            "format": "markdown",
            "title": "A",
            "content": content,
            "content_sha256": hex_digest(content.as_bytes()),
            "size_bytes": content.len(),
            "version": 1,
            "parent_id": "",
            "pinned": false,
            "meta": {},
            "created_at": 424242,
            "deleted_at": deleted_at,
        })
    }

    fn tool_snapshot(content: &str) -> Value {
        json!({
            "content_sha256": hex_digest(content.as_bytes()),
            "content": content,
            "media_type": "text/plain",
            "size_bytes": content.len(),
            "created_at_ms": 1000,
            "expires_at_ms": 2000,
            "last_accessed_at_ms": 1500,
        })
    }

    #[test]
    fn chat_restore_preserves_exact_state_and_content_across_reopen() {
        let (vfs, mut database) = prepared();
        let snapshot = chat_snapshot("art-1", "# large\nbody", 0);
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.chat.artifact.restore",
                snapshot.clone()
            )
            .unwrap()["inserted"],
            true
        );
        drop(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.chat.artifact.restore",
                snapshot.clone()
            )
            .unwrap()["inserted"],
            false
        );
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.chat.artifact.get",
                json!({"id": "art-1"})
            )
            .unwrap(),
            snapshot
        );
        let mut divergent = snapshot.clone();
        divergent["title"] = json!("other");
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.chat.artifact.restore",
            divergent
        )
        .is_err());
    }

    #[test]
    fn chat_restore_rebuilds_indexes_for_live_reads() {
        let (_vfs, mut database) = prepared();
        execute(
            &mut database,
            7,
            11,
            "migration.chat.artifact.restore",
            chat_snapshot("art-1", "first", 0),
        )
        .unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        let listed = crate::artifact::list(&database, &mut transaction, "conv-1", false).unwrap();
        let value: Value = serde_json::from_slice(&listed).unwrap();
        assert_eq!(value.as_array().unwrap().len(), 1);
        let got = crate::artifact::get(&database, &mut transaction, "art-1", true).unwrap();
        let got: Value = serde_json::from_slice(&got.unwrap()).unwrap();
        assert_eq!(got["content"], "first");
    }

    #[test]
    fn chat_deleted_snapshot_keeps_document_but_drops_live_indexes() {
        let (_vfs, mut database) = prepared();
        execute(
            &mut database,
            7,
            11,
            "migration.chat.artifact.restore",
            chat_snapshot("art-1", "first", 0),
        )
        .unwrap();
        let mut removed = chat_snapshot("art-2", "second", 9000);
        removed["version"] = json!(2);
        removed["parent_id"] = json!("art-1");
        execute(
            &mut database,
            7,
            11,
            "migration.chat.artifact.restore",
            removed,
        )
        .unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        assert!(
            crate::artifact::get(&database, &mut transaction, "art-2", true)
                .unwrap()
                .is_none()
        );
        let listed = crate::artifact::list(&database, &mut transaction, "conv-1", false).unwrap();
        let value: Value = serde_json::from_slice(&listed).unwrap();
        assert_eq!(value.as_array().unwrap().len(), 1);
        // Migration get still returns the exact deleted snapshot.
        drop(transaction);
        let got = execute(
            &mut database,
            7,
            11,
            "migration.chat.artifact.get",
            json!({"id": "art-2"}),
        )
        .unwrap();
        assert_eq!(got["deleted_at"], 9000);
    }

    #[test]
    fn chat_scan_is_owner_scoped_and_owner_sees_only_own_catalog() {
        let (_vfs, mut database) = prepared();
        execute(
            &mut database,
            7,
            11,
            "migration.chat.artifact.restore",
            chat_snapshot("art-1", "first", 0),
        )
        .unwrap();
        let page = execute(
            &mut database,
            7,
            11,
            "migration.chat.artifact.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(page["rows"], json!([{"id": "art-1"}]));
        assert_eq!(page["cursor"], Value::Null);
        assert_eq!(
            execute(
                &mut database,
                7,
                12,
                "migration.chat.artifact.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }

    #[test]
    fn tool_restore_preserves_exact_content_and_lifecycle_across_reopen() {
        let (vfs, mut database) = prepared();
        let content = "x".repeat(64 * 1024);
        let snapshot = tool_snapshot(&content);
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.tool.result.restore",
                snapshot.clone()
            )
            .unwrap()["inserted"],
            true
        );
        drop(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.tool.result.restore",
                snapshot.clone()
            )
            .unwrap()["inserted"],
            false
        );
        let digest_hex = snapshot["content_sha256"].as_str().unwrap().to_owned();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.tool.result.get",
                json!({"content_sha256": digest_hex})
            )
            .unwrap(),
            snapshot
        );
        let page = execute(
            &mut database,
            7,
            11,
            "migration.tool.result.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(page["rows"].as_array().unwrap().len(), 1);
        // Live reads see the restored content before its expiry.
        let mut transaction = database.begin(7, 11).unwrap();
        let read = crate::artifact::tool_read(
            &database,
            &mut transaction,
            &format!("tool-result:{digest_hex}"),
            1501,
            0,
            content.len(),
        )
        .unwrap()
        .unwrap();
        let read: Value = serde_json::from_slice(&read).unwrap();
        assert_eq!(read["sizeBytes"], content.len() as u64);
        assert_eq!(read["coverage"], "complete");
    }

    #[test]
    fn invalid_snapshots_fail_before_any_write() {
        let (_vfs, mut database) = prepared();
        let mut bad = chat_snapshot("art-1", "first", 0);
        bad["content_sha256"] = json!("0".repeat(64));
        assert!(execute(&mut database, 7, 11, "migration.chat.artifact.restore", bad).is_err());
        let mut bad_tool = tool_snapshot("body");
        bad_tool["expires_at_ms"] = json!(1000);
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.tool.result.restore",
            bad_tool
        )
        .is_err());
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.chat.artifact.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.tool.result.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
