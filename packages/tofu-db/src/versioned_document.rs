//! Versioned JSON documents stored inline or through owner-bound blob references.
//!
//! This module is the physical codec used by Schema IR tables that expose
//! optimistic versions. It never opens files directly: reads, blob staging,
//! and mutations stay inside one `AuthorityTransaction`.

use std::io::{self, Cursor};

use serde_json::{json, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::blob::{BlobId, BlobReference};
use crate::block::BlockId;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_ENTITY_INLINE_VALUE_BYTES, MAX_ENTITY_RANGE_ROWS, MAX_TRANSACTION_IR_LITERAL_BYTES,
};

const MAGIC: &[u8; 8] = b"TDBDOC01";
const VERSION: u32 = 1;
const INLINE: u8 = 0;
const BLOB: u8 = 1;
const FIXED_HEADER_BYTES: usize = 8 + 4 + 8 + 8 + 2 + 2 + 1 + 3 + 8;
const BLOB_REFERENCE_BYTES: usize = 32 + 32;
const MAX_PHYSICAL_LOGICAL_BYTES: u64 = 1024 * 1024 * 1024;

#[derive(Clone, Debug)]
struct Document {
    namespace: String,
    key: String,
    version: u64,
    updated_at_ms: u64,
    value: DocumentValue,
}

#[derive(Clone, Debug)]
enum DocumentValue {
    Inline(Vec<u8>),
    Blob(BlobReference),
}

pub(crate) struct PutRequest {
    pub key: EntityKey,
    pub namespace: String,
    pub logical_key: String,
    pub value_json: Vec<u8>,
    pub expected_version: Option<u64>,
    pub updated_at_ms: u64,
}

pub(crate) struct CloneRequest<'a> {
    pub source_key: &'a EntityKey,
    pub destination_key: EntityKey,
    pub namespace: &'a str,
    pub source_logical_key: &'a str,
    pub destination_logical_key: &'a str,
    pub updated_at_ms: u64,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn conflict() -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, "versioned document conflict")
}

fn push_u16(output: &mut Vec<u8>, value: usize) -> io::Result<()> {
    output.extend_from_slice(
        &u16::try_from(value)
            .map_err(|_| invalid_input("versioned document identity is too long"))?
            .to_le_bytes(),
    );
    Ok(())
}

fn take<'a>(input: &'a [u8], offset: &mut usize, count: usize) -> io::Result<&'a [u8]> {
    let end = offset
        .checked_add(count)
        .ok_or_else(|| invalid_data("versioned document offset overflow"))?;
    let value = input
        .get(*offset..end)
        .ok_or_else(|| invalid_data("truncated versioned document"))?;
    *offset = end;
    Ok(value)
}

fn read_u16(input: &[u8], offset: &mut usize) -> io::Result<usize> {
    Ok(u16::from_le_bytes(take(input, offset, 2)?.try_into().unwrap()) as usize)
}

impl Document {
    fn encode(&self) -> io::Result<Vec<u8>> {
        let logical_bytes = match &self.value {
            DocumentValue::Inline(value) => value.len() as u64,
            DocumentValue::Blob(reference) => reference.logical_bytes,
        };
        let payload_bytes = match &self.value {
            DocumentValue::Inline(value) => value.len(),
            DocumentValue::Blob(_) => BLOB_REFERENCE_BYTES,
        };
        let mut output = Vec::with_capacity(
            FIXED_HEADER_BYTES + self.namespace.len() + self.key.len() + payload_bytes,
        );
        output.extend_from_slice(MAGIC);
        output.extend_from_slice(&VERSION.to_le_bytes());
        output.extend_from_slice(&self.version.to_le_bytes());
        output.extend_from_slice(&self.updated_at_ms.to_le_bytes());
        push_u16(&mut output, self.namespace.len())?;
        push_u16(&mut output, self.key.len())?;
        output.push(match self.value {
            DocumentValue::Inline(_) => INLINE,
            DocumentValue::Blob(_) => BLOB,
        });
        output.extend_from_slice(&[0; 3]);
        output.extend_from_slice(&logical_bytes.to_le_bytes());
        output.extend_from_slice(self.namespace.as_bytes());
        output.extend_from_slice(self.key.as_bytes());
        match &self.value {
            DocumentValue::Inline(value) => output.extend_from_slice(value),
            DocumentValue::Blob(reference) => {
                output.extend_from_slice(&reference.blob_id.0);
                output.extend_from_slice(&reference.manifest_block_id.0);
            }
        }
        if output.len() > MAX_ENTITY_INLINE_VALUE_BYTES {
            return Err(invalid_input(
                "versioned document envelope exceeds inline bound",
            ));
        }
        Ok(output)
    }

    fn decode(input: &[u8]) -> io::Result<Self> {
        if input.len() < FIXED_HEADER_BYTES || &input[..8] != MAGIC {
            return Err(invalid_data("versioned document magic mismatch"));
        }
        let mut offset = 8;
        let format_version = u32::from_le_bytes(take(input, &mut offset, 4)?.try_into().unwrap());
        let version = u64::from_le_bytes(take(input, &mut offset, 8)?.try_into().unwrap());
        let updated_at_ms = u64::from_le_bytes(take(input, &mut offset, 8)?.try_into().unwrap());
        let namespace_bytes = read_u16(input, &mut offset)?;
        let key_bytes = read_u16(input, &mut offset)?;
        let storage = take(input, &mut offset, 1)?[0];
        let reserved = take(input, &mut offset, 3)?;
        let logical_bytes = u64::from_le_bytes(take(input, &mut offset, 8)?.try_into().unwrap());
        if format_version != VERSION || version == 0 || updated_at_ms == 0 || reserved != [0; 3] {
            return Err(invalid_data("invalid versioned document header"));
        }
        let namespace = std::str::from_utf8(take(input, &mut offset, namespace_bytes)?)
            .map_err(|_| invalid_data("versioned document namespace is not UTF-8"))?
            .to_owned();
        let key = std::str::from_utf8(take(input, &mut offset, key_bytes)?)
            .map_err(|_| invalid_data("versioned document key is not UTF-8"))?
            .to_owned();
        let value = match storage {
            INLINE => {
                let length = usize::try_from(logical_bytes)
                    .map_err(|_| invalid_data("versioned document length overflow"))?;
                DocumentValue::Inline(take(input, &mut offset, length)?.to_vec())
            }
            BLOB if logical_bytes <= MAX_PHYSICAL_LOGICAL_BYTES => {
                let blob_id = BlobId(take(input, &mut offset, 32)?.try_into().unwrap());
                let manifest_block_id = BlockId(take(input, &mut offset, 32)?.try_into().unwrap());
                DocumentValue::Blob(BlobReference {
                    blob_id,
                    manifest_block_id,
                    logical_bytes,
                })
            }
            _ => return Err(invalid_data("invalid versioned document storage kind")),
        };
        if offset != input.len() {
            return Err(invalid_data("versioned document has trailing bytes"));
        }
        Ok(Self {
            namespace,
            key,
            version,
            updated_at_ms,
            value,
        })
    }
}

fn materialize_value(
    database: &AuthorityDatabase,
    tenant_id: u64,
    owner_user_id: u64,
    value: &DocumentValue,
    maximum_bytes: usize,
) -> io::Result<Vec<u8>> {
    let bytes = match value {
        DocumentValue::Inline(value) => value.clone(),
        DocumentValue::Blob(reference) => {
            let mut reader =
                database.reachable_blob_reader(tenant_id, owner_user_id, *reference)?;
            let mut output = Vec::with_capacity(reference.logical_bytes as usize);
            while let Some(chunk) = reader.next_chunk()? {
                if output.len() + chunk.len() > maximum_bytes {
                    return Err(invalid_data(
                        "versioned document blob exceeds response bound",
                    ));
                }
                output.extend_from_slice(&chunk);
            }
            output
        }
    };
    Ok(bytes)
}

pub(crate) fn materialize_stored_document(
    database: &AuthorityDatabase,
    tenant_id: u64,
    owner_user_id: u64,
    stored: &[u8],
    namespace: &str,
) -> io::Result<(String, Vec<u8>)> {
    let document = Document::decode(stored)?;
    if document.namespace != namespace {
        return Err(invalid_data("versioned document identity mismatch"));
    }
    let value = materialize_value(
        database,
        tenant_id,
        owner_user_id,
        &document.value,
        MAX_TRANSACTION_IR_LITERAL_BYTES,
    )?;
    serde_json::from_slice::<Value>(&value)
        .map_err(|_| invalid_data("versioned document value is not JSON"))?;
    Ok((document.key, value))
}

pub(crate) fn stored_document_version(
    stored: &[u8],
    namespace: &str,
    logical_key: &str,
) -> io::Result<u64> {
    let document = Document::decode(stored)?;
    if document.namespace != namespace || document.key != logical_key {
        return Err(invalid_data("versioned document identity mismatch"));
    }
    Ok(document.version)
}

pub(crate) fn stored_document_logical_bytes(stored: &[u8], namespace: &str) -> io::Result<u64> {
    let document = Document::decode(stored)?;
    if document.namespace != namespace {
        return Err(invalid_data("versioned document identity mismatch"));
    }
    Ok(match document.value {
        DocumentValue::Inline(value) => value.len() as u64,
        DocumentValue::Blob(reference) => reference.logical_bytes,
    })
}

fn project_document(
    database: &AuthorityDatabase,
    tenant_id: u64,
    owner_user_id: u64,
    document: &Document,
    include_key: bool,
    maximum_bytes: usize,
) -> io::Result<Vec<u8>> {
    let value: Value = serde_json::from_slice(&materialize_value(
        database,
        tenant_id,
        owner_user_id,
        &document.value,
        maximum_bytes,
    )?)
    .map_err(|_| invalid_data("versioned document value is not JSON"))?;
    let projected = if include_key {
        json!({"key": document.key, "value": value, "version": document.version, "updated_at_ms": document.updated_at_ms})
    } else {
        json!({"value": value, "version": document.version, "updated_at_ms": document.updated_at_ms})
    };
    let response = serde_json::to_vec(&projected)
        .map_err(|_| invalid_data("versioned document response failed"))?;
    if response.len() > maximum_bytes {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "versioned document response exceeds 8 MiB",
        ));
    }
    Ok(response)
}

pub(crate) fn stored_blob_reference(stored: &[u8]) -> io::Result<Option<BlobReference>> {
    if !stored.starts_with(MAGIC) {
        return Ok(None);
    }
    match Document::decode(stored)?.value {
        DocumentValue::Inline(_) => Ok(None),
        DocumentValue::Blob(reference) => Ok(Some(reference)),
    }
}

/// Re-key an immutable logical document while retaining its content-addressed
/// blob reference. This is used only for same-owner semantic clones; callers
/// must separately establish destination identity and lifecycle invariants.
pub(crate) fn clone_stored_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: CloneRequest<'_>,
) -> io::Result<()> {
    if request.updated_at_ms == 0 {
        return Err(invalid_input("cloned document timestamp is zero"));
    }
    let source = database
        .entity_get(transaction, request.source_key)?
        .ok_or_else(|| invalid_data("cloned source document is missing"))?;
    let source = Document::decode(&source)?;
    if source.namespace != request.namespace || source.key != request.source_logical_key {
        return Err(invalid_data("cloned source document identity mismatch"));
    }
    if database
        .entity_get(transaction, &request.destination_key)?
        .is_some()
    {
        return Err(conflict());
    }
    database.entity_put(
        transaction,
        request.destination_key,
        Document {
            namespace: request.namespace.to_owned(),
            key: request.destination_logical_key.to_owned(),
            version: 1,
            updated_at_ms: request.updated_at_ms,
            value: source.value,
        }
        .encode()?,
    )
}

pub(crate) fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
    namespace: &str,
    logical_key: &str,
) -> io::Result<Option<Vec<u8>>> {
    get_with_blob_owner(
        database,
        transaction,
        key,
        namespace,
        logical_key,
        transaction.owner_user_id(),
    )
}

/// Materialize only the stored JSON value without the generic versioned-
/// document projection envelope. Semantic aggregates that own revision
/// metadata separately use this path so an exactly 8 MiB logical value does
/// not overflow merely because a physical version is added to its response.
pub(crate) fn get_value(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
    namespace: &str,
    logical_key: &str,
) -> io::Result<Option<Vec<u8>>> {
    let Some(raw) = database.entity_get(transaction, key)? else {
        return Ok(None);
    };
    let document = Document::decode(&raw)?;
    if document.namespace != namespace || document.key != logical_key {
        return Err(invalid_data("versioned document identity mismatch"));
    }
    let value = materialize_value(
        database,
        transaction.tenant_id(),
        transaction.owner_user_id(),
        &document.value,
        MAX_TRANSACTION_IR_LITERAL_BYTES,
    )?;
    serde_json::from_slice::<Value>(&value)
        .map_err(|_| invalid_data("versioned document value is not JSON"))?;
    Ok(Some(value))
}

pub(crate) fn get_with_blob_owner(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
    namespace: &str,
    logical_key: &str,
    blob_owner_user_id: u64,
) -> io::Result<Option<Vec<u8>>> {
    get_with_blob_owner_bounded(
        database,
        transaction,
        key,
        namespace,
        logical_key,
        blob_owner_user_id,
        MAX_TRANSACTION_IR_LITERAL_BYTES,
    )
}

pub(crate) fn get_with_blob_owner_bounded(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
    namespace: &str,
    logical_key: &str,
    blob_owner_user_id: u64,
    maximum_bytes: usize,
) -> io::Result<Option<Vec<u8>>> {
    let Some(raw) = database.entity_get(transaction, key)? else {
        return Ok(None);
    };
    let document = Document::decode(&raw)?;
    if document.namespace != namespace || document.key != logical_key {
        return Err(invalid_data("versioned document identity mismatch"));
    }
    project_document(
        database,
        transaction.tenant_id(),
        blob_owner_user_id,
        &document,
        false,
        maximum_bytes,
    )
    .map(Some)
}

pub(crate) fn get_value_with_blob_owner_bounded(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
    namespace: &str,
    logical_key: &str,
    blob_owner_user_id: u64,
    maximum_bytes: usize,
) -> io::Result<Option<Vec<u8>>> {
    let Some(raw) = database.entity_get(transaction, key)? else {
        return Ok(None);
    };
    let document = Document::decode(&raw)?;
    if document.namespace != namespace || document.key != logical_key {
        return Err(invalid_data("versioned document identity mismatch"));
    }
    let value = materialize_value(
        database,
        transaction.tenant_id(),
        blob_owner_user_id,
        &document.value,
        maximum_bytes,
    )?;
    serde_json::from_slice::<Value>(&value)
        .map_err(|_| invalid_data("versioned document value is not JSON"))?;
    Ok(Some(value))
}

pub(crate) fn put(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: PutRequest,
) -> io::Result<Vec<u8>> {
    put_with_blob_owner(database, transaction, request, transaction.owner_user_id())
}

pub(crate) fn put_with_blob_owner(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: PutRequest,
    blob_owner_user_id: u64,
) -> io::Result<Vec<u8>> {
    put_with_blob_owner_bounded(
        database,
        transaction,
        request,
        blob_owner_user_id,
        MAX_TRANSACTION_IR_LITERAL_BYTES,
    )
}

pub(crate) fn put_with_blob_owner_bounded(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: PutRequest,
    blob_owner_user_id: u64,
    maximum_bytes: usize,
) -> io::Result<Vec<u8>> {
    let PutRequest {
        key,
        namespace,
        logical_key,
        value_json,
        expected_version,
        updated_at_ms,
    } = request;
    if updated_at_ms == 0 || value_json.len() > maximum_bytes {
        return Err(invalid_input("invalid versioned document mutation"));
    }
    serde_json::from_slice::<Value>(&value_json)
        .map_err(|_| invalid_input("versioned document value is not JSON"))?;
    let current = database.entity_get(transaction, &key)?;
    let actual = current
        .as_deref()
        .map(Document::decode)
        .transpose()?
        .map_or(0, |document| document.version);
    if expected_version.is_some_and(|expected| expected != actual) {
        return Err(conflict());
    }
    let version = actual
        .checked_add(1)
        .ok_or_else(|| invalid_data("versioned document version overflow"))?;
    let inline_envelope_bytes =
        FIXED_HEADER_BYTES + namespace.len() + logical_key.len() + value_json.len();
    let value = if inline_envelope_bytes <= MAX_ENTITY_INLINE_VALUE_BYTES {
        DocumentValue::Inline(value_json)
    } else {
        let logical_bytes = value_json.len() as u64;
        let reference = if blob_owner_user_id == transaction.owner_user_id() {
            database.stage_blob(transaction, &mut Cursor::new(value_json), logical_bytes)?
        } else if blob_owner_user_id == crate::conversation_header::TENANT_GLOBAL_OWNER_ID {
            database.stage_tenant_global_blob(
                transaction,
                &mut Cursor::new(value_json),
                logical_bytes,
            )?
        } else {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "versioned document blob owner is outside the transaction scope",
            ));
        };
        DocumentValue::Blob(reference)
    };
    database.entity_put(
        transaction,
        key,
        Document {
            namespace,
            key: logical_key.clone(),
            version,
            updated_at_ms,
            value,
        }
        .encode()?,
    )?;
    serde_json::to_vec(
        &json!({"key": logical_key, "version": version, "updated_at_ms": updated_at_ms}),
    )
    .map_err(|_| invalid_data("versioned document response failed"))
}

pub(crate) fn delete(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: EntityKey,
    namespace: &str,
    logical_key: &str,
    expected_version: Option<u64>,
) -> io::Result<Vec<u8>> {
    let current = database.entity_get(transaction, &key)?;
    if let Some(raw) = current {
        let document = Document::decode(&raw)?;
        if document.namespace != namespace || document.key != logical_key {
            return Err(invalid_data("versioned document identity mismatch"));
        }
        if expected_version.is_some_and(|expected| expected != document.version) {
            return Err(conflict());
        }
        database.entity_delete(transaction, key)?;
        Ok(br#"{"deleted":true}"#.to_vec())
    } else {
        Ok(br#"{"deleted":false}"#.to_vec())
    }
}

pub(crate) fn list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    start: &EntityKey,
    end: &EntityKey,
    namespace: &str,
    limit: usize,
) -> io::Result<Vec<u8>> {
    list_with_blob_owner_bounded(
        database,
        transaction,
        start,
        end,
        namespace,
        limit,
        ListProjectionBounds {
            blob_owner_user_id: transaction.owner_user_id(),
            maximum_bytes: MAX_TRANSACTION_IR_LITERAL_BYTES,
        },
    )
}

pub(crate) struct ListProjectionBounds {
    pub(crate) blob_owner_user_id: u64,
    pub(crate) maximum_bytes: usize,
}

pub(crate) fn list_with_blob_owner_bounded(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    start: &EntityKey,
    end: &EntityKey,
    namespace: &str,
    limit: usize,
    bounds: ListProjectionBounds,
) -> io::Result<Vec<u8>> {
    let mut rows = Vec::with_capacity(limit);
    let mut page_start = start.clone();
    while rows.len() < limit {
        let page_limit = (limit - rows.len()).min(MAX_ENTITY_RANGE_ROWS);
        let page = database.entity_scan(transaction, &page_start, end, page_limit)?;
        let full = page.len() == page_limit;
        let next = page
            .last()
            .map(|(key, _)| key.clone().exact_range())
            .transpose()?;
        rows.extend(page);
        if !full || rows.len() == limit {
            break;
        }
        page_start = next
            .ok_or_else(|| invalid_data("versioned document pagination lost continuation"))?
            .1;
    }
    let mut output = Vec::from(b"[".as_slice());
    for (index, (_, raw)) in rows.into_iter().enumerate() {
        let document = Document::decode(&raw)?;
        if document.namespace != namespace {
            return Err(invalid_data("versioned document namespace mismatch"));
        }
        let projected = project_document(
            database,
            transaction.tenant_id(),
            bounds.blob_owner_user_id,
            &document,
            true,
            bounds.maximum_bytes,
        )?;
        let additional = projected.len() + usize::from(index > 0) + 1;
        if output.len() + additional > bounds.maximum_bytes {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "versioned document page exceeds 8 MiB",
            ));
        }
        if index > 0 {
            output.push(b',');
        }
        output.extend_from_slice(&projected);
    }
    output.push(b']');
    Ok(output)
}
