//! Owner-scoped chat artifacts and reconstructible large tool-result blobs.
//!
//! Metadata stays in bounded Entity records while content is stored in immutable,
//! owner-bound blobs. Every secondary key retains the exact logical identity so
//! digest collisions fail closed instead of aliasing user data.

use std::io::{self, Cursor};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::blob::BlobReference;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    ARTIFACT_CONVERSATION_INDEX_NAMESPACE, ARTIFACT_DEDUPE_NAMESPACE, ARTIFACT_DOCUMENT_NAMESPACE,
    ARTIFACT_LIBRARY_INDEX_NAMESPACE, ARTIFACT_PARENT_INDEX_NAMESPACE,
    ARTIFACT_PATH_HEAD_NAMESPACE, MAX_ARTIFACT_BYTES, MAX_ARTIFACT_ROWS_PER_CONVERSATION,
    MAX_TOOL_RESULT_ARTIFACT_BYTES, MAX_TOOL_RESULT_PRUNE_ROWS, MAX_TOOL_RESULT_RANGE_BYTES,
    MAX_TOOL_RESULT_TTL_MILLISECONDS, TOOL_RESULT_ARTIFACT_NAMESPACE,
    TOOL_RESULT_EXPIRY_INDEX_NAMESPACE,
};

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, message)
}

fn digest(parts: &[&[u8]]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(b"tofu-db:artifact-key:v1\0");
    for part in parts {
        hasher.update(part.len().to_be_bytes());
        hasher.update(part);
    }
    hasher.finalize().into()
}

fn hex_digest(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let digest = Sha256::digest(bytes);
    let mut output = String::with_capacity(64);
    for byte in digest {
        output.push(HEX[(byte >> 4) as usize] as char);
        output.push(HEX[(byte & 0x0f) as usize] as char);
    }
    output
}

pub(crate) fn python_casefold(value: &str) -> String {
    debug_assert_eq!(
        crate::generated_unicode_casefold::PYTHON_UNICODE_CASEFOLD_VERSION,
        (15, 0, 0)
    );
    debug_assert_eq!(
        crate::generated_unicode_casefold::PYTHON_UNICODE_CASEFOLD_MAPPING_COUNT,
        1530
    );
    let mut output = String::with_capacity(value.len());
    for character in value.chars() {
        if let Some(mapped) = crate::generated_unicode_casefold::full_casefold_mapping(character) {
            output.push_str(mapped);
        } else {
            output.push(character);
        }
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

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
struct StoredBlobReference {
    blob_id: [u8; 32],
    manifest_block_id: [u8; 32],
    logical_bytes: u64,
}

impl From<BlobReference> for StoredBlobReference {
    fn from(value: BlobReference) -> Self {
        Self {
            blob_id: value.blob_id.0,
            manifest_block_id: value.manifest_block_id.0,
            logical_bytes: value.logical_bytes,
        }
    }
}

impl From<StoredBlobReference> for BlobReference {
    fn from(value: StoredBlobReference) -> Self {
        Self {
            blob_id: crate::blob::BlobId(value.blob_id),
            manifest_block_id: crate::block::BlockId(value.manifest_block_id),
            logical_bytes: value.logical_bytes,
        }
    }
}

fn materialize_blob(
    database: &AuthorityDatabase,
    transaction: &AuthorityTransaction,
    reference: StoredBlobReference,
    maximum: usize,
) -> io::Result<Vec<u8>> {
    if reference.logical_bytes > maximum as u64 {
        return Err(invalid_data("artifact blob exceeds its logical bound"));
    }
    let mut reader = database.blob_reader(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        reference.into(),
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

#[derive(Clone, Debug, Deserialize, Serialize)]
struct ArtifactDocument {
    id: String,
    conv_id: String,
    task_id: String,
    msg_id: String,
    source: String,
    source_ref: Value,
    format: String,
    title: String,
    content_sha256: String,
    size_bytes: u64,
    version: u64,
    parent_id: String,
    pinned: bool,
    meta: Value,
    created_at: u64,
    deleted_at: u64,
    content: StoredBlobReference,
}

impl ArtifactDocument {
    fn validate(&self) -> io::Result<()> {
        if self.id.is_empty()
            || self.id.chars().count() > 256
            || self.conv_id.is_empty()
            || self.conv_id.chars().count() > 512
            || self.source.is_empty()
            || self.source.chars().count() > 256
            || !matches!(self.format.as_str(), "markdown" | "html" | "svg")
            || self.title.chars().count() > 300
            || self.task_id.chars().count() > 512
            || self.msg_id.chars().count() > 512
            || self.parent_id.chars().count() > 256
            || self.version == 0
            || self.size_bytes > MAX_ARTIFACT_BYTES as u64
            || self.content.logical_bytes != self.size_bytes
            || !self.source_ref.is_object()
            || !self.meta.is_object()
        {
            return Err(invalid_data("stored artifact document is invalid"));
        }
        Ok(())
    }

    fn projection(&self, content: Option<&str>) -> Value {
        let mut value = json!({
            "id": self.id, "conv_id": self.conv_id, "task_id": self.task_id,
            "msg_id": self.msg_id, "source": self.source, "source_ref": self.source_ref,
            "format": self.format, "title": self.title,
            "content_sha256": self.content_sha256, "size_bytes": self.size_bytes,
            "version": self.version, "parent_id": self.parent_id,
            "pinned": self.pinned, "meta": self.meta, "created_at": self.created_at,
        });
        if let Some(content) = content {
            value["content"] = Value::String(content.to_owned());
        }
        value
    }
}

#[derive(Clone, Debug)]
pub struct CreateRequest {
    pub artifact_id: String,
    pub conv_id: String,
    pub task_id: String,
    pub msg_id: String,
    pub source: String,
    pub source_ref: Value,
    pub format: String,
    pub title: String,
    pub content: String,
    pub parent_id: String,
    pub meta: Value,
    pub created_at: u64,
}

fn document_key(transaction: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    hashed_key(transaction, ARTIFACT_DOCUMENT_NAMESPACE, &[id.as_bytes()])
}

fn read_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<ArtifactDocument>> {
    let Some(raw) = database.entity_get(transaction, &document_key(transaction, id)?)? else {
        return Ok(None);
    };
    let document: ArtifactDocument =
        serde_json::from_slice(&raw).map_err(|_| invalid_data("artifact document is malformed"))?;
    document.validate()?;
    if document.id != id {
        return Err(invalid_data(
            "artifact document identity differs from its key",
        ));
    }
    Ok(Some(document))
}

fn put_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &ArtifactDocument,
) -> io::Result<()> {
    document.validate()?;
    database.entity_put(
        transaction,
        document_key(transaction, &document.id)?,
        serde_json::to_vec(document)
            .map_err(|_| invalid_data("artifact document cannot be encoded"))?,
    )
}

fn identity_value(id: &str, conv_id: &str) -> io::Result<Vec<u8>> {
    serde_json::to_vec(&json!({"id": id, "conv_id": conv_id}))
        .map_err(|_| invalid_data("artifact identity cannot be encoded"))
}

fn decode_identity(raw: &[u8], conv_id: Option<&str>) -> io::Result<String> {
    let value: Value = serde_json::from_slice(raw)
        .map_err(|_| invalid_data("artifact index identity is malformed"))?;
    let id = value
        .get("id")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("artifact index identity is missing"))?;
    if conv_id
        .is_some_and(|expected| value.get("conv_id").and_then(Value::as_str) != Some(expected))
    {
        return Err(invalid_data("artifact index conversation differs"));
    }
    Ok(id.to_owned())
}

fn path_from_source_ref(value: &Value) -> Option<&str> {
    value
        .get("path")
        .and_then(Value::as_str)
        .filter(|path| !path.is_empty())
}

fn count_key(transaction: &AuthorityTransaction, conv_id: &str) -> io::Result<EntityKey> {
    hashed_key(
        transaction,
        "artifact_conversation_counts",
        &[conv_id.as_bytes()],
    )
}

fn read_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conv_id: &str,
) -> io::Result<u64> {
    let raw = database.entity_get(transaction, &count_key(transaction, conv_id)?)?;
    raw.map_or(Ok(0), |bytes| {
        let array: [u8; 8] = bytes
            .try_into()
            .map_err(|_| invalid_data("artifact count is malformed"))?;
        Ok(u64::from_le_bytes(array))
    })
}

fn write_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conv_id: &str,
    count: u64,
) -> io::Result<()> {
    database.entity_put(
        transaction,
        count_key(transaction, conv_id)?,
        count.to_le_bytes().to_vec(),
    )
}

fn conversation_index_key(
    transaction: &AuthorityTransaction,
    document: &ArtifactDocument,
) -> io::Result<EntityKey> {
    let mut raw = digest(&[document.conv_id.as_bytes()]).to_vec();
    raw.extend_from_slice(&descending(document.created_at));
    raw.extend_from_slice(&digest(&[document.id.as_bytes()]));
    key(transaction, ARTIFACT_CONVERSATION_INDEX_NAMESPACE, &raw)
}

fn library_index_key(
    transaction: &AuthorityTransaction,
    document: &ArtifactDocument,
) -> io::Result<EntityKey> {
    let mut raw = vec![u8::from(!document.pinned)];
    raw.extend_from_slice(&descending(document.created_at));
    raw.extend_from_slice(&digest(&[document.id.as_bytes()]));
    key(transaction, ARTIFACT_LIBRARY_INDEX_NAMESPACE, &raw)
}

fn parent_index_key(
    transaction: &AuthorityTransaction,
    document: &ArtifactDocument,
) -> io::Result<EntityKey> {
    let mut raw = digest(&[document.parent_id.as_bytes()]).to_vec();
    raw.extend_from_slice(&document.version.to_be_bytes());
    raw.extend_from_slice(&document.created_at.to_be_bytes());
    raw.extend_from_slice(&digest(&[document.id.as_bytes()]));
    key(transaction, ARTIFACT_PARENT_INDEX_NAMESPACE, &raw)
}

pub(crate) fn create(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: CreateRequest,
) -> io::Result<Vec<u8>> {
    let encoded = request.content.as_bytes();
    if request.artifact_id.is_empty()
        || request.artifact_id.chars().count() > 256
        || request.conv_id.is_empty()
        || request.conv_id.chars().count() > 512
        || request.task_id.chars().count() > 512
        || request.msg_id.chars().count() > 512
        || request.source.is_empty()
        || request.source.chars().count() > 256
        || !matches!(request.format.as_str(), "markdown" | "html" | "svg")
        || request.title.chars().count() > 300
        || request.parent_id.chars().count() > 256
        || encoded.len() > MAX_ARTIFACT_BYTES
        || !request.source_ref.is_object()
        || !request.meta.is_object()
    {
        return Err(invalid_input("invalid artifact content or metadata"));
    }
    if read_document(database, transaction, &request.artifact_id)?.is_some() {
        return Err(conflict("artifact ID already exists"));
    }
    let content_sha256 = hex_digest(encoded);
    let dedupe_key = hashed_key(
        transaction,
        ARTIFACT_DEDUPE_NAMESPACE,
        &[request.conv_id.as_bytes(), content_sha256.as_bytes()],
    )?;
    if let Some(raw) = database.entity_get(transaction, &dedupe_key)? {
        let existing_id = decode_identity(&raw, Some(&request.conv_id))?;
        let existing = read_document(database, transaction, &existing_id)?
            .ok_or_else(|| invalid_data("artifact dedupe target is missing"))?;
        if existing.deleted_at == 0 && existing.content_sha256 == content_sha256 {
            return serde_json::to_vec(
                &json!({"created": false, "artifact": existing.projection(None)}),
            )
            .map_err(|_| invalid_data("artifact response cannot be encoded"));
        }
    }
    let count = read_count(database, transaction, &request.conv_id)?;
    if count >= MAX_ARTIFACT_ROWS_PER_CONVERSATION as u64 {
        return Err(conflict("artifact conversation quota reached"));
    }
    let mut parent_id = request.parent_id;
    let mut version = 1;
    if parent_id.is_empty() {
        if let Some(path) = path_from_source_ref(&request.source_ref) {
            let head_key = hashed_key(
                transaction,
                ARTIFACT_PATH_HEAD_NAMESPACE,
                &[request.conv_id.as_bytes(), path.as_bytes()],
            )?;
            if let Some(raw) = database.entity_get(transaction, &head_key)? {
                let head = decode_identity(&raw, Some(&request.conv_id))?;
                let parent = read_document(database, transaction, &head)?
                    .ok_or_else(|| invalid_data("artifact path head is missing"))?;
                if parent.deleted_at == 0 {
                    parent_id = parent.id;
                    version = parent
                        .version
                        .checked_add(1)
                        .ok_or_else(|| invalid_data("artifact version overflow"))?;
                }
            }
        }
    }
    if !parent_id.is_empty() && version == 1 {
        let parent = read_document(database, transaction, &parent_id)?
            .ok_or_else(|| invalid_input("artifact parent is missing"))?;
        if parent.deleted_at != 0 || parent.conv_id != request.conv_id {
            return Err(invalid_input(
                "artifact parent is outside the live conversation",
            ));
        }
        version = parent
            .version
            .checked_add(1)
            .ok_or_else(|| invalid_data("artifact version overflow"))?;
    }
    let content =
        database.stage_blob(transaction, &mut Cursor::new(encoded), encoded.len() as u64)?;
    let document = ArtifactDocument {
        id: request.artifact_id,
        conv_id: request.conv_id,
        task_id: request.task_id,
        msg_id: request.msg_id,
        source: request.source,
        source_ref: request.source_ref,
        format: request.format,
        title: request.title,
        content_sha256,
        size_bytes: encoded.len() as u64,
        version,
        parent_id,
        pinned: false,
        meta: request.meta,
        created_at: request.created_at,
        deleted_at: 0,
        content: content.into(),
    };
    document
        .validate()
        .map_err(|_| invalid_input("invalid artifact document"))?;
    put_document(database, transaction, &document)?;
    let identity = identity_value(&document.id, &document.conv_id)?;
    database.entity_put(transaction, dedupe_key, identity.clone())?;
    database.entity_put(
        transaction,
        conversation_index_key(transaction, &document)?,
        identity.clone(),
    )?;
    database.entity_put(
        transaction,
        library_index_key(transaction, &document)?,
        identity.clone(),
    )?;
    if !document.parent_id.is_empty() {
        database.entity_put(
            transaction,
            parent_index_key(transaction, &document)?,
            identity.clone(),
        )?;
    }
    if let Some(path) = path_from_source_ref(&document.source_ref) {
        database.entity_put(
            transaction,
            hashed_key(
                transaction,
                ARTIFACT_PATH_HEAD_NAMESPACE,
                &[document.conv_id.as_bytes(), path.as_bytes()],
            )?,
            identity,
        )?;
    }
    write_count(database, transaction, &document.conv_id, count + 1)?;
    serde_json::to_vec(&json!({"created": true, "artifact": document.projection(None)}))
        .map_err(|_| invalid_data("artifact response cannot be encoded"))
}

pub(crate) fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    artifact_id: &str,
    include_content: bool,
) -> io::Result<Option<Vec<u8>>> {
    let Some(document) = read_document(database, transaction, artifact_id)? else {
        return Ok(None);
    };
    if document.deleted_at != 0 {
        return Ok(None);
    }
    let content = if include_content {
        Some(
            String::from_utf8(materialize_blob(
                database,
                transaction,
                document.content,
                MAX_ARTIFACT_BYTES,
            )?)
            .map_err(|_| invalid_data("artifact content is not UTF-8"))?,
        )
    } else {
        None
    };
    serde_json::to_vec(&document.projection(content.as_deref()))
        .map(Some)
        .map_err(|_| invalid_data("artifact response cannot be encoded"))
}

fn scan_index(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    prefix: &[u8],
    limit: usize,
) -> io::Result<Vec<ArtifactDocument>> {
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        prefix,
    )?;
    let rows = database.entity_scan(transaction, &start, &end, limit)?;
    let mut documents = Vec::with_capacity(rows.len());
    for (_, raw) in rows {
        let id = decode_identity(&raw, None)?;
        documents.push(
            read_document(database, transaction, &id)?
                .ok_or_else(|| invalid_data("artifact index target is missing"))?,
        );
    }
    Ok(documents)
}

pub(crate) fn list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conv_id: &str,
    include_deleted: bool,
) -> io::Result<Vec<u8>> {
    let count = read_count(database, transaction, conv_id)? as usize;
    let documents = scan_index(
        database,
        transaction,
        ARTIFACT_CONVERSATION_INDEX_NAMESPACE,
        &digest(&[conv_id.as_bytes()]),
        count.saturating_add(1),
    )?;
    if documents.len() != count {
        return Err(invalid_data("artifact count and index differ"));
    }
    let values: Vec<_> = documents
        .into_iter()
        .filter(|row| include_deleted || row.deleted_at == 0)
        .map(|row| row.projection(None))
        .collect();
    serde_json::to_vec(&values).map_err(|_| invalid_data("artifact list cannot be encoded"))
}

pub(crate) fn versions(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    artifact_id: &str,
) -> io::Result<Vec<u8>> {
    let Some(mut current) = read_document(database, transaction, artifact_id)? else {
        return Ok(b"[]".to_vec());
    };
    if current.deleted_at != 0 {
        return Ok(b"[]".to_vec());
    }
    let mut ancestors = Vec::new();
    for _ in 0..MAX_ARTIFACT_ROWS_PER_CONVERSATION {
        ancestors.push(current.clone());
        if current.parent_id.is_empty() {
            break;
        }
        let Some(parent) = read_document(database, transaction, &current.parent_id)? else {
            break;
        };
        if parent.deleted_at != 0 || ancestors.iter().any(|row| row.id == parent.id) {
            break;
        }
        current = parent;
    }
    ancestors.reverse();
    let mut chain = ancestors;
    while chain.len() < MAX_ARTIFACT_ROWS_PER_CONVERSATION {
        let current = chain.last().expect("artifact version chain has a root");
        let prefix = digest(&[current.id.as_bytes()]);
        let children = scan_index(
            database,
            transaction,
            ARTIFACT_PARENT_INDEX_NAMESPACE,
            &prefix,
            1,
        )?;
        let Some(child) = children.into_iter().next() else {
            break;
        };
        if child.deleted_at != 0 || chain.iter().any(|row| row.id == child.id) {
            break;
        }
        chain.push(child);
    }
    serde_json::to_vec(
        &chain
            .into_iter()
            .map(|row| row.projection(None))
            .collect::<Vec<_>>(),
    )
    .map_err(|_| invalid_data("artifact versions cannot be encoded"))
}

pub(crate) fn library(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    limit: usize,
) -> io::Result<Vec<u8>> {
    if limit == 0 || limit > 200 {
        return Err(invalid_input("invalid artifact library limit"));
    }
    let mut documents = scan_index(
        database,
        transaction,
        ARTIFACT_LIBRARY_INDEX_NAMESPACE,
        b"",
        limit,
    )?;
    documents.retain(|row| row.deleted_at == 0);
    serde_json::to_vec(
        &documents
            .into_iter()
            .map(|row| row.projection(None))
            .collect::<Vec<_>>(),
    )
    .map_err(|_| invalid_data("artifact library cannot be encoded"))
}

pub(crate) fn pin(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    artifact_id: &str,
    pinned: bool,
) -> io::Result<Vec<u8>> {
    let Some(mut document) = read_document(database, transaction, artifact_id)? else {
        return Ok(br#"{"changed":false}"#.to_vec());
    };
    if document.deleted_at != 0 {
        return Ok(br#"{"changed":false}"#.to_vec());
    }
    database.entity_delete(transaction, library_index_key(transaction, &document)?)?;
    document.pinned = pinned;
    put_document(database, transaction, &document)?;
    database.entity_put(
        transaction,
        library_index_key(transaction, &document)?,
        identity_value(&document.id, &document.conv_id)?,
    )?;
    Ok(br#"{"changed":true}"#.to_vec())
}

pub(crate) fn delete(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    artifact_id: &str,
    deleted_at: u64,
) -> io::Result<Vec<u8>> {
    if deleted_at == 0 {
        return Err(invalid_input("artifact deletion time is zero"));
    }
    let Some(mut document) = read_document(database, transaction, artifact_id)? else {
        return Ok(br#"{"deleted":false}"#.to_vec());
    };
    if document.deleted_at != 0 {
        return Ok(br#"{"deleted":false}"#.to_vec());
    }
    database.entity_delete(transaction, library_index_key(transaction, &document)?)?;
    if !document.parent_id.is_empty() {
        database.entity_delete(transaction, parent_index_key(transaction, &document)?)?;
    }
    if let Some(path) = path_from_source_ref(&document.source_ref) {
        let head_key = hashed_key(
            transaction,
            ARTIFACT_PATH_HEAD_NAMESPACE,
            &[document.conv_id.as_bytes(), path.as_bytes()],
        )?;
        if let Some(raw) = database.entity_get(transaction, &head_key)? {
            if decode_identity(&raw, Some(&document.conv_id))? == document.id {
                let replacement = if document.parent_id.is_empty() {
                    None
                } else {
                    read_document(database, transaction, &document.parent_id)?.filter(|parent| {
                        parent.deleted_at == 0
                            && path_from_source_ref(&parent.source_ref) == Some(path)
                    })
                };
                if let Some(parent) = replacement {
                    database.entity_put(
                        transaction,
                        head_key,
                        identity_value(&parent.id, &parent.conv_id)?,
                    )?;
                } else {
                    database.entity_delete(transaction, head_key)?;
                }
            }
        }
    }
    document.deleted_at = deleted_at;
    put_document(database, transaction, &document)?;
    Ok(br#"{"deleted":true}"#.to_vec())
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct ToolResultDocument {
    content_sha256: String,
    media_type: String,
    size_bytes: u64,
    created_at_ms: u64,
    expires_at_ms: u64,
    last_accessed_at_ms: u64,
    content: StoredBlobReference,
}

/// Return the immutable content edge retained by an artifact Entity value.
/// Backup and authority GC call this while walking the current semantic root;
/// indexes deliberately return no edge.
pub(crate) fn stored_blob_reference(
    key: &EntityKey,
    value: &[u8],
) -> io::Result<Option<BlobReference>> {
    match key.namespace() {
        ARTIFACT_DOCUMENT_NAMESPACE => {
            let document: ArtifactDocument = serde_json::from_slice(value)
                .map_err(|_| invalid_data("artifact document is malformed"))?;
            document.validate()?;
            Ok(Some(document.content.into()))
        }
        TOOL_RESULT_ARTIFACT_NAMESPACE => {
            let document: ToolResultDocument = serde_json::from_slice(value)
                .map_err(|_| invalid_data("tool-result document is malformed"))?;
            if document.content.logical_bytes != document.size_bytes
                || document.size_bytes > MAX_TOOL_RESULT_ARTIFACT_BYTES as u64
            {
                return Err(invalid_data("tool-result blob reference is invalid"));
            }
            Ok(Some(document.content.into()))
        }
        _ => Ok(None),
    }
}

fn tool_key(transaction: &AuthorityTransaction, digest_hex: &str) -> io::Result<EntityKey> {
    hashed_key(
        transaction,
        TOOL_RESULT_ARTIFACT_NAMESPACE,
        &[digest_hex.as_bytes()],
    )
}

fn expiry_key(
    transaction: &AuthorityTransaction,
    expires_at_ms: u64,
    digest_hex: &str,
) -> io::Result<EntityKey> {
    let mut raw = expires_at_ms.to_be_bytes().to_vec();
    raw.extend_from_slice(&digest(&[digest_hex.as_bytes()]));
    key(transaction, TOOL_RESULT_EXPIRY_INDEX_NAMESPACE, &raw)
}

fn read_tool(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    digest_hex: &str,
) -> io::Result<Option<ToolResultDocument>> {
    let Some(raw) = database.entity_get(transaction, &tool_key(transaction, digest_hex)?)? else {
        return Ok(None);
    };
    let document: ToolResultDocument = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("tool-result document is malformed"))?;
    if document.content_sha256 != digest_hex
        || document.content.logical_bytes != document.size_bytes
        || document.size_bytes > MAX_TOOL_RESULT_ARTIFACT_BYTES as u64
    {
        return Err(invalid_data("tool-result document identity is invalid"));
    }
    Ok(Some(document))
}

fn parse_artifact_ref(value: &str) -> io::Result<&str> {
    let digest = value.strip_prefix("tool-result:").unwrap_or(value);
    if digest.len() != 64
        || !digest
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err(invalid_input("invalid tool artifact ref"));
    }
    Ok(digest)
}

pub(crate) fn tool_put(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    content: &str,
    media_type: &str,
    created_at_ms: u64,
    expires_at_ms: u64,
) -> io::Result<Vec<u8>> {
    let encoded = content.as_bytes();
    if encoded.len() > MAX_TOOL_RESULT_ARTIFACT_BYTES
        || created_at_ms == 0
        || expires_at_ms <= created_at_ms
        || expires_at_ms - created_at_ms > MAX_TOOL_RESULT_TTL_MILLISECONDS
        || media_type.chars().count() > 128
    {
        return Err(invalid_input("invalid tool-result artifact"));
    }
    let digest_hex = hex_digest(encoded);
    let effective_expiry;
    if let Some(mut current) = read_tool(database, transaction, &digest_hex)? {
        database.entity_delete(
            transaction,
            expiry_key(transaction, current.expires_at_ms, &digest_hex)?,
        )?;
        current.expires_at_ms = current.expires_at_ms.max(expires_at_ms);
        current.last_accessed_at_ms = current.last_accessed_at_ms.max(created_at_ms);
        effective_expiry = current.expires_at_ms;
        database.entity_put(
            transaction,
            tool_key(transaction, &digest_hex)?,
            serde_json::to_vec(&current)
                .map_err(|_| invalid_data("tool-result document cannot be encoded"))?,
        )?;
    } else {
        let reference =
            database.stage_blob(transaction, &mut Cursor::new(encoded), encoded.len() as u64)?;
        let document = ToolResultDocument {
            content_sha256: digest_hex.clone(),
            media_type: if media_type.trim().is_empty() {
                "text/plain".to_owned()
            } else {
                media_type.to_owned()
            },
            size_bytes: encoded.len() as u64,
            created_at_ms,
            expires_at_ms,
            last_accessed_at_ms: created_at_ms,
            content: reference.into(),
        };
        effective_expiry = expires_at_ms;
        database.entity_put(
            transaction,
            tool_key(transaction, &digest_hex)?,
            serde_json::to_vec(&document)
                .map_err(|_| invalid_data("tool-result document cannot be encoded"))?,
        )?;
    }
    database.entity_put(
        transaction,
        expiry_key(transaction, effective_expiry, &digest_hex)?,
        digest_hex.as_bytes().to_vec(),
    )?;
    serde_json::to_vec(&json!({"artifactRef": format!("tool-result:{digest_hex}"), "contentSha256": digest_hex, "sizeBytes": encoded.len(), "expiresAtMs": effective_expiry})).map_err(|_| invalid_data("tool-result response cannot be encoded"))
}

pub(crate) fn tool_read(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    artifact_ref: &str,
    now_ms: u64,
    offset: usize,
    limit: usize,
) -> io::Result<Option<Vec<u8>>> {
    if now_ms == 0 || limit == 0 || limit > MAX_TOOL_RESULT_RANGE_BYTES {
        return Err(invalid_input("invalid tool-result range"));
    }
    let digest_hex = parse_artifact_ref(artifact_ref)?;
    let Some(document) = read_tool(database, transaction, digest_hex)? else {
        return Ok(None);
    };
    if document.expires_at_ms <= now_ms {
        return Ok(None);
    }
    let encoded = materialize_blob(
        database,
        transaction,
        document.content,
        MAX_TOOL_RESULT_ARTIFACT_BYTES,
    )?;
    let mut start = offset.min(encoded.len());
    while start < encoded.len() && encoded[start] & 0xc0 == 0x80 {
        start += 1;
    }
    let mut end = encoded.len().min(start.saturating_add(limit));
    while end < encoded.len() && end > start && encoded[end] & 0xc0 == 0x80 {
        end -= 1;
    }
    if end == start && end < encoded.len() {
        end += 1;
        while end < encoded.len() && encoded[end] & 0xc0 == 0x80 {
            end += 1;
        }
    }
    let visible = std::str::from_utf8(&encoded[start..end])
        .map_err(|_| invalid_data("tool-result content is not UTF-8"))?;
    serde_json::to_vec(&json!({"artifactRef": format!("tool-result:{digest_hex}"), "content": visible, "offset": start, "nextCursor": if end < encoded.len() { Some(end.to_string()) } else { None }, "truncated": end < encoded.len(), "sizeBytes": document.size_bytes})).map(Some).map_err(|_| invalid_data("tool-result response cannot be encoded"))
}

pub(crate) fn tool_search(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    artifact_ref: &str,
    query: &str,
    now_ms: u64,
    cursor: usize,
    limit: usize,
) -> io::Result<Option<Vec<u8>>> {
    if query.is_empty() || query.chars().count() > 200 || now_ms == 0 || limit == 0 || limit > 20 {
        return Err(invalid_input("invalid tool-result search"));
    }
    let digest_hex = parse_artifact_ref(artifact_ref)?;
    let Some(document) = read_tool(database, transaction, digest_hex)? else {
        return Ok(None);
    };
    if document.expires_at_ms <= now_ms {
        return Ok(None);
    }
    let content = String::from_utf8(materialize_blob(
        database,
        transaction,
        document.content,
        MAX_TOOL_RESULT_ARTIFACT_BYTES,
    )?)
    .map_err(|_| invalid_data("tool-result content is not UTF-8"))?;
    // storage.v1 defines offsets over Python Unicode scalar positions. Build a
    // bounded character view once; 16 MiB is already the admitted blob ceiling.
    let characters: Vec<char> = content.chars().collect();
    let folded: Vec<char> = python_casefold(&content).chars().collect();
    let needle: Vec<char> = python_casefold(query).chars().collect();
    let mut position = cursor.min(characters.len());
    let mut items = Vec::new();
    while items.len() < limit {
        let Some(relative) = folded[position..]
            .windows(needle.len())
            .position(|window| window == needle.as_slice())
        else {
            position = characters.len();
            break;
        };
        let found = position + relative;
        let before = found.saturating_sub(160);
        let after = characters.len().min(found + query.chars().count() + 320);
        items.push(json!({
            "offset": found,
            "text": characters[before..after].iter().collect::<String>(),
        }));
        position = (found + query.chars().count().max(1)).max(after);
    }
    let has_more = position < folded.len()
        && folded[position..]
            .windows(needle.len())
            .any(|window| window == needle.as_slice());
    serde_json::to_vec(&json!({
        "artifactRef": format!("tool-result:{digest_hex}"),
        "query": query,
        "items": items,
        "nextCursor": if has_more { Some(position.to_string()) } else { None },
        "truncated": has_more,
        "sizeBytes": document.size_bytes,
    }))
    .map(Some)
    .map_err(|_| invalid_data("tool-result search response cannot be encoded"))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct ToolPruneProgress {
    pub deleted: usize,
    pub has_more: bool,
}

// The background worker and explicit storage.v2 maintenance operation share
// this one owner-scoped bounded mutation path.
pub(crate) fn tool_prune(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    now_ms: u64,
    limit: usize,
) -> io::Result<ToolPruneProgress> {
    if now_ms == 0 || limit == 0 || limit > MAX_TOOL_RESULT_PRUNE_ROWS {
        return Err(invalid_input("invalid tool-result prune bound"));
    }
    let (start, namespace_end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TOOL_RESULT_EXPIRY_INDEX_NAMESPACE,
        b"",
    )?;
    let end = now_ms
        .checked_add(1)
        .map(|exclusive| {
            key(
                transaction,
                TOOL_RESULT_EXPIRY_INDEX_NAMESPACE,
                &exclusive.to_be_bytes(),
            )
        })
        .transpose()?
        .unwrap_or(namespace_end);
    let mut cursor = start;
    let mut selected = 0_usize;
    let mut deleted = 0;
    while selected < limit {
        let maximum = (limit - selected).min(1_000);
        let rows = database.entity_scan(transaction, &cursor, &end, maximum)?;
        if rows.is_empty() {
            break;
        }
        for (index_key, raw) in &rows {
            let expires = u64::from_be_bytes(
                index_key
                    .key_bytes()
                    .get(..8)
                    .ok_or_else(|| invalid_data("tool-result expiry index is malformed"))?
                    .try_into()
                    .unwrap(),
            );
            let digest_hex = std::str::from_utf8(raw)
                .map_err(|_| invalid_data("tool-result expiry identity is malformed"))?;
            if expires > now_ms || expiry_key(transaction, expires, digest_hex)? != *index_key {
                return Err(invalid_data("tool-result expiry index identity differs"));
            }
            let document = read_tool(database, transaction, digest_hex)?
                .ok_or_else(|| invalid_data("tool-result expiry target is missing"))?;
            if document.expires_at_ms != expires {
                return Err(invalid_data("tool-result expiry index differs"));
            }
            database.entity_delete(transaction, tool_key(transaction, digest_hex)?)?;
            database.entity_delete(transaction, index_key.clone())?;
            deleted += 1;
        }
        selected += rows.len();
        if rows.len() < maximum {
            break;
        }
        let mut next_raw = rows.last().unwrap().0.key_bytes().to_vec();
        next_raw.push(0);
        cursor = key(transaction, TOOL_RESULT_EXPIRY_INDEX_NAMESPACE, &next_raw)?;
    }
    Ok(ToolPruneProgress {
        deleted,
        has_more: selected == limit,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
    use std::path::Path;
    use std::sync::Arc;

    fn request(id: &str, content: &str, created_at: u64) -> CreateRequest {
        CreateRequest {
            artifact_id: id.to_owned(),
            conv_id: "conversation".to_owned(),
            task_id: "task".to_owned(),
            msg_id: "message".to_owned(),
            source: "write_file".to_owned(),
            source_ref: json!({"path":"report.md"}),
            format: "markdown".to_owned(),
            title: "report.md".to_owned(),
            content: content.to_owned(),
            parent_id: String::new(),
            meta: json!({"words":2}),
            created_at,
        }
    }

    #[test]
    fn artifact_and_tool_result_blobs_round_trip_with_owner_isolation_and_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        let first: Value = serde_json::from_slice(
            &create(&database, &mut transaction, request("v1", "# first\n", 100)).unwrap(),
        )
        .unwrap();
        assert_eq!(first["artifact"]["version"], 1);
        let duplicate: Value = serde_json::from_slice(
            &create(
                &database,
                &mut transaction,
                request("duplicate", "# first\n", 101),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(duplicate["created"], false);
        let second: Value = serde_json::from_slice(
            &create(
                &database,
                &mut transaction,
                request("v2", "# second\n", 102),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(second["artifact"]["parent_id"], "v1");
        assert_eq!(second["artifact"]["version"], 2);
        let tool: Value = serde_json::from_slice(
            &tool_put(
                &database,
                &mut transaction,
                "αβγ needle tail",
                "text/plain",
                1_000,
                2_000,
            )
            .unwrap(),
        )
        .unwrap();
        let tool_ref = tool["artifactRef"].as_str().unwrap().to_owned();
        database.commit(transaction).unwrap();
        drop(database);

        let database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        let full: Value =
            serde_json::from_slice(&get(&database, &mut read, "v2", true).unwrap().unwrap())
                .unwrap();
        assert_eq!(full["content"], "# second\n");
        let range: Value = serde_json::from_slice(
            &tool_read(&database, &mut read, &tool_ref, 1_500, 1, 8)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(range["offset"], 2);
        assert_eq!(range["content"], "βγ nee");
        let mut foreign = database.begin(7, 12).unwrap();
        assert!(get(&database, &mut foreign, "v2", true).unwrap().is_none());
        assert!(tool_read(&database, &mut foreign, &tool_ref, 1_500, 0, 8)
            .unwrap()
            .is_none());
    }

    #[test]
    fn tool_result_expiry_extension_and_bounded_prune_are_atomic() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        let first: Value = serde_json::from_slice(
            &tool_put(&database, &mut transaction, "same", "text/plain", 100, 200).unwrap(),
        )
        .unwrap();
        let second: Value = serde_json::from_slice(
            &tool_put(
                &database,
                &mut transaction,
                "same",
                "ignored/type",
                150,
                300,
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(first["artifactRef"], second["artifactRef"]);
        assert_eq!(second["expiresAtMs"], 300);
        database.commit(transaction).unwrap();
        let mut prune = database.begin(7, 11).unwrap();
        let early = tool_prune(&database, &mut prune, 250, 1).unwrap();
        assert_eq!(early.deleted, 0);
        drop(prune);
        let mut prune = database.begin(7, 11).unwrap();
        let expired = tool_prune(&database, &mut prune, 300, 1).unwrap();
        assert_eq!(expired.deleted, 1);
        database.commit(prune).unwrap();
    }

    fn prepared_vfs() -> Arc<DeterministicVfs> {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        vfs.arm_fault(None).unwrap();
        vfs
    }

    fn initialized_database(vfs: Arc<DeterministicVfs>) -> AuthorityDatabase {
        let database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn fault_create(database: &mut AuthorityDatabase) -> io::Result<()> {
        let mut transaction = database.begin(7, 11)?;
        create(
            database,
            &mut transaction,
            request("fault-artifact", &"payload".repeat(4_000), 100),
        )?;
        database.commit(transaction)?;
        Ok(())
    }

    fn assert_fault_recovery(vfs: Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        let present = get(&database, &mut read, "fault-artifact", true)
            .unwrap()
            .is_some();
        drop(read);
        if !present {
            let mut write = database.begin(7, 11).unwrap();
            create(
                &database,
                &mut write,
                request("fault-artifact", &"payload".repeat(4_000), 100),
            )
            .unwrap();
            database.commit(write).unwrap();
        }
        let mut verify = database.begin(7, 11).unwrap();
        let rows: Value =
            serde_json::from_slice(&list(&database, &mut verify, "conversation", false).unwrap())
                .unwrap();
        assert_eq!(rows.as_array().unwrap().len(), 1);
        let full: Value = serde_json::from_slice(
            &get(&database, &mut verify, "fault-artifact", true)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(full["content"], "payload".repeat(4_000));
    }

    #[test]
    fn every_artifact_blob_commit_fault_recovers_one_complete_indexed_prefix() {
        let baseline_vfs = prepared_vfs();
        let mut baseline = initialized_database(baseline_vfs.clone());
        fault_create(&mut baseline).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = initialized_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = fault_create(&mut database);
            drop(database);
            assert_fault_recovery(vfs);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = initialized_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = fault_create(&mut database);
            drop(database);
            assert_fault_recovery(vfs);
        }
    }

    fn seed_expired_tool_result(database: &mut AuthorityDatabase) -> String {
        let mut transaction = database.begin(7, 11).unwrap();
        let response: Value = serde_json::from_slice(
            &tool_put(
                database,
                &mut transaction,
                "fault-prune-content",
                "text/plain",
                100,
                200,
            )
            .unwrap(),
        )
        .unwrap();
        database.commit(transaction).unwrap();
        response["artifactRef"].as_str().unwrap().to_owned()
    }

    fn fault_prune(database: &mut AuthorityDatabase) -> io::Result<()> {
        let mut transaction = database.begin(7, 11)?;
        let progress = tool_prune(database, &mut transaction, 300, 1)?;
        if progress.deleted != 1 {
            return Err(invalid_data("fault prune did not stage one deletion"));
        }
        database.commit(transaction)?;
        Ok(())
    }

    fn assert_prune_fault_recovery(vfs: Arc<DeterministicVfs>, artifact_ref: &str) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        let document_present = tool_read(&database, &mut read, artifact_ref, 150, 0, 64)
            .unwrap()
            .is_some();
        drop(read);
        let mut prune = database.begin(7, 11).unwrap();
        let progress = tool_prune(&database, &mut prune, 300, 1).unwrap();
        assert_eq!(progress.deleted, usize::from(document_present));
        if progress.deleted != 0 {
            database.commit(prune).unwrap();
        }
        let mut verify = database.begin(7, 11).unwrap();
        assert_eq!(
            tool_prune(&database, &mut verify, 300, 1).unwrap().deleted,
            0
        );
    }

    #[test]
    fn every_tool_result_prune_commit_fault_recovers_document_and_index_together() {
        let baseline_vfs = prepared_vfs();
        let mut baseline = initialized_database(baseline_vfs.clone());
        seed_expired_tool_result(&mut baseline);
        baseline_vfs.arm_fault(None).unwrap();
        fault_prune(&mut baseline).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = initialized_database(vfs.clone());
            let artifact_ref = seed_expired_tool_result(&mut database);
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = fault_prune(&mut database);
            drop(database);
            assert_prune_fault_recovery(vfs, &artifact_ref);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = initialized_database(vfs.clone());
            let artifact_ref = seed_expired_tool_result(&mut database);
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = fault_prune(&mut database);
            drop(database);
            assert_prune_fault_recovery(vfs, &artifact_ref);
        }
    }
}
