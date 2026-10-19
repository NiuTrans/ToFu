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
    MAX_ENTITY_INLINE_VALUE_BYTES, MAX_ENTITY_RANGE_ROWS, MAX_MIGRATION_DOCUMENT_BYTES,
};

pub(crate) fn record_composite_key(namespace: &str, key: &str) -> io::Result<Vec<u8>> {
    let namespace_bytes = namespace.as_bytes();
    let mut composite = Vec::with_capacity(2 + namespace_bytes.len() + key.len());
    composite.extend_from_slice(
        &u16::try_from(namespace_bytes.len())
            .map_err(|_| invalid_input("record namespace exceeds its bound"))?
            .to_be_bytes(),
    );
    composite.extend_from_slice(namespace_bytes);
    composite.extend_from_slice(key.as_bytes());
    Ok(composite)
}

pub(crate) fn record_key(
    tenant_id: u64,
    owner_user_id: u64,
    namespace: &str,
    key: &str,
) -> io::Result<EntityKey> {
    EntityKey::new(
        tenant_id,
        owner_user_id,
        crate::generated_tofudb_ir::RECORD_DOCUMENT_NAMESPACE,
        &record_composite_key(namespace, key)?,
    )
}

#[path = "versioned_document_stream.rs"]
pub(crate) mod streaming;

const MAGIC: &[u8; 8] = b"TDBDOC01";
const VERSION: u32 = 1;
const INLINE: u8 = 0;
const BLOB: u8 = 1;
const FIXED_HEADER_BYTES: usize = 8 + 4 + 8 + 8 + 2 + 2 + 1 + 3 + 8;
const BLOB_REFERENCE_BYTES: usize = 32 + 32;
const MAX_PHYSICAL_LOGICAL_BYTES: u64 = 1024 * 1024 * 1024;

#[derive(Clone, Debug)]
struct Document<'a> {
    namespace: std::borrow::Cow<'a, str>,
    key: std::borrow::Cow<'a, str>,
    version: u64,
    updated_at_ms: u64,
    value: DocumentValue<'a>,
}

#[derive(Clone, Debug)]
enum DocumentValue<'a> {
    Inline(std::borrow::Cow<'a, [u8]>),
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

/// Fully materialized logical snapshot state, with an explicit physical
/// placement supplied by its domain adapter and no imported block references.
pub struct SnapshotDocument {
    pub key: EntityKey,
    pub namespace: String,
    pub logical_key: String,
    pub version: u64,
    pub updated_at_ms: u64,
    pub value_json: Vec<u8>,
    pub blob_owner_user_id: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SnapshotRestoreOutcome {
    Inserted,
    AlreadyMatches,
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

impl<'a> Document<'a> {
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

    fn decode(input: &'a [u8]) -> io::Result<Self> {
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
            .into();
        let key = std::str::from_utf8(take(input, &mut offset, key_bytes)?)
            .map_err(|_| invalid_data("versioned document key is not UTF-8"))?
            .into();
        let value = match storage {
            INLINE => {
                let length = usize::try_from(logical_bytes)
                    .map_err(|_| invalid_data("versioned document length overflow"))?;
                DocumentValue::Inline(take(input, &mut offset, length)?.into())
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

/// Mutable bytes consume and discard the completed blob witness.
enum MaterializedDocumentValue<'a> {
    Inline(&'a [u8]),
    Blob(crate::blob::VerifiedBlobBytes),
}
impl MaterializedDocumentValue<'_> {
    fn bytes(&self) -> &[u8] {
        match self { Self::Inline(bytes) => bytes, Self::Blob(blob) => blob.bytes() }
    }
    fn into_bytes(self) -> Vec<u8> {
        match self {
            Self::Inline(bytes) => bytes.to_vec(),
            Self::Blob(blob) => blob.into_bytes(),
        }
    }
}

fn materialize_value(
    database: &AuthorityDatabase,
    tenant_id: u64,
    owner_user_id: u64,
    value: &DocumentValue,
    maximum_bytes: usize,
) -> io::Result<Vec<u8>> {
    materialize_value_with_witness(database, tenant_id, owner_user_id, value, maximum_bytes)
        .map(MaterializedDocumentValue::into_bytes)
}

fn materialize_value_with_witness<'a>(
    database: &AuthorityDatabase,
    tenant_id: u64,
    owner_user_id: u64,
    value: &'a DocumentValue<'_>,
    maximum_bytes: usize,
) -> io::Result<MaterializedDocumentValue<'a>> {
    match value {
        DocumentValue::Inline(value) => {
            if value.len() > maximum_bytes {
                return Err(invalid_data(
                    "versioned document inline exceeds response bound",
                ));
            }
            Ok(MaterializedDocumentValue::Inline(value.as_ref()))
        }
        DocumentValue::Blob(reference) => {
            let reader = database.reachable_blob_reader(tenant_id, owner_user_id, *reference)?;
            reader
                .materialize_verified(maximum_bytes, || {
                    invalid_data("versioned document blob exceeds response bound")
                })
                .map(MaterializedDocumentValue::Blob)
        }
    }
}

pub(crate) fn materialize_stored_document(
    database: &AuthorityDatabase,
    tenant_id: u64,
    owner_user_id: u64,
    stored: &[u8],
    namespace: &str,
) -> io::Result<(String, Vec<u8>)> {
    materialize_stored_document_bounded(
        database,
        tenant_id,
        owner_user_id,
        stored,
        namespace,
        usize::MAX,
    )
}

/// Project a scanned stored document into (value, version, updated_at_ms),
/// materializing blob-carried values under the caller's owner scope.
pub(crate) fn project_stored_document(
    database: &AuthorityDatabase,
    tenant_id: u64,
    owner_user_id: u64,
    stored: &[u8],
    namespace: &str,
    maximum_bytes: usize,
) -> io::Result<(Vec<u8>, u64, u64)> {
    let document = Document::decode(stored)?;
    if document.namespace != namespace {
        return Err(invalid_data("versioned document identity mismatch"));
    }
    let value = materialize_value(
        database,
        tenant_id,
        owner_user_id,
        &document.value,
        maximum_bytes,
    )?;
    Ok((value, document.version, document.updated_at_ms))
}

fn materialize_stored_bytes_bounded(
    database: &AuthorityDatabase,
    tenant_id: u64,
    owner_user_id: u64,
    stored: &[u8],
    namespace: &str,
    maximum_bytes: usize,
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
        maximum_bytes,
    )?;
    Ok((document.key.into_owned(), value))
}

pub(crate) fn materialize_stored_document_bounded(
    database: &AuthorityDatabase,
    tenant_id: u64,
    owner_user_id: u64,
    stored: &[u8],
    namespace: &str,
    maximum_bytes: usize,
) -> io::Result<(String, Vec<u8>)> {
    let (key, value) = materialize_stored_bytes_bounded(database, tenant_id, owner_user_id, stored, namespace, maximum_bytes)?;
    // Byte consumers need the same grammar/depth fence, not a discarded tree.
    crate::response_json::validate(&value)
        .map_err(|_| invalid_data("versioned document value is not JSON"))?;
    Ok((key, value))
}

/// Same scope, namespace, authenticated blob and size checks as byte reads;
/// transfer the one parsed JSON value to consumers that would parse it again.
pub(crate) fn materialize_stored_document_value_bounded(
    database: &AuthorityDatabase,
    tenant_id: u64,
    owner_user_id: u64,
    stored: &[u8],
    namespace: &str,
    maximum_bytes: usize,
) -> io::Result<(String, Value, usize)> {
    let _profile = crate::request_profile::Probe::new(crate::request_profile::DOCUMENT_MATERIALIZE);
    let document = Document::decode(stored)?;
    if document.namespace != namespace {
        return Err(invalid_data("versioned document identity mismatch"));
    }
    let materialized = materialize_value_with_witness(
        database, tenant_id, owner_user_id, &document.value, maximum_bytes)?;
    let bytes = materialized.bytes();
    let decoded = serde_json::from_slice::<Value>(bytes)
        .map_err(|_| invalid_data("versioned document value is not JSON"))?;
    Ok((document.key.into_owned(), decoded, bytes.len()))
}

/// One scoped read and one parse; retain the original bytes when a consumer
/// must verify their digest as well as consume the decoded JSON value.
pub(crate) fn materialize_stored_document_value_and_bytes_bounded(
    database: &AuthorityDatabase,
    tenant_id: u64,
    owner_user_id: u64,
    stored: &[u8],
    namespace: &str,
    maximum_bytes: usize,
) -> io::Result<(String, Value, Vec<u8>)> {
    let (key, bytes) = materialize_stored_bytes_bounded(
        database, tenant_id, owner_user_id, stored, namespace, maximum_bytes,
    )?;
    let decoded = serde_json::from_slice::<Value>(&bytes)
        .map_err(|_| invalid_data("versioned document value is not JSON"))?;
    Ok((key, decoded, bytes))
}

/// Materialize a stored document already located by a physically scoped scan.
/// The TofuSQL catalog decodes a row's logical key parts from the physical
/// entity key, so the physical namespace is authoritative; the document
/// envelope's `namespace` field is a writer-chosen domain label that varies
/// per logical record namespace and is not a stable per-table constant. This
/// validates only the recorded logical key against the scan's trailing key
/// part and the value against the byte budget.
pub(crate) fn materialize_stored_document_for_catalog(
    database: &AuthorityDatabase,
    tenant_id: u64,
    owner_user_id: u64,
    stored: &[u8],
    logical_key: &str,
    maximum_bytes: usize,
) -> io::Result<Vec<u8>> {
    let document = Document::decode(stored)?;
    if document.key != logical_key {
        return Err(invalid_data("versioned document identity mismatch"));
    }
    let value = materialize_value(
        database,
        tenant_id,
        owner_user_id,
        &document.value,
        maximum_bytes,
    )?;
    serde_json::from_slice::<Value>(&value)
        .map_err(|_| invalid_data("versioned document value is not JSON"))?;
    Ok(value)
}

/// The TofuSQL catalog validates an envelope reached through an index
/// namespace against the writer's recorded logical key, which is not
/// recoverable from the physical index key alone (lane index rows record the
/// document table's `{conversation}\u{1f}{turn}` key). This surfaces the
/// recorded key without the per-namespace label check the hot identity path
/// enforces.
pub(crate) fn stored_document_logical_key_for_catalog(stored: &[u8]) -> io::Result<String> {
    Ok(Document::decode(stored)?.key.into_owned())
}

/// Validated physical identity for owners admitting multiple explicit codecs.
/// This does not fetch or decode the document's JSON/blob value.
pub(crate) fn stored_document_namespace(stored: &[u8]) -> io::Result<String> {
    Ok(Document::decode(stored)?.namespace.into_owned())
}

/// Header-only projection: the document's logical key without decoding or
/// fetching the value (blob-backed values included). Hot index paths that
/// only need the identity avoid materializing the full JSON document.
pub(crate) fn stored_document_logical_key(stored: &[u8], namespace: &str) -> io::Result<String> {
    let document = Document::decode(stored)?;
    if document.namespace != namespace {
        return Err(invalid_data("versioned document identity mismatch"));
    }
    Ok(document.key.into_owned())
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

/// Immutable producer-owned bytes and a certificate for the outer JSON object.
/// The serde_json default accepts at most 127 nested containers; this envelope
/// adds one. Legacy fallback and bodies at the edge retain semantic validation.
pub(crate) struct DocumentProjection {
    bytes: Vec<u8>,
    maximum_container_depth: Option<usize>,
}
impl DocumentProjection {
    pub(crate) fn json_proven(&self) -> bool {
        self.maximum_container_depth.is_some_and(|depth|
            depth <= crate::response_json::MAX_VALIDATED_CONTAINER_DEPTH)
    }
    pub(crate) fn into_bytes(self) -> Vec<u8> {
        self.bytes
    }
}
fn project_document(
    database: &AuthorityDatabase, tenant_id: u64, owner_user_id: u64,
    document: &Document, include_key: bool, maximum_bytes: usize,
) -> io::Result<Vec<u8>> {
    project_document_with_proof(database, tenant_id, owner_user_id, document, include_key, maximum_bytes)
        .map(DocumentProjection::into_bytes)
}

fn project_document_with_proof(
    database: &AuthorityDatabase,
    tenant_id: u64,
    owner_user_id: u64,
    document: &Document,
    include_key: bool,
    maximum_bytes: usize,
) -> io::Result<DocumentProjection> {
    let value = {
        let _materialize = crate::fsync_stats::StageTimer::diagnostic(
            crate::fsync_stats::STAGE_DOCUMENT_VALUE_MATERIALIZE);
        materialize_value_with_witness(database, tenant_id, owner_user_id, &document.value, maximum_bytes)?
    };
    let value = {
        let _canonical = crate::fsync_stats::StageTimer::diagnostic(
            crate::fsync_stats::STAGE_DOCUMENT_JSON_CANONICAL);
        match &value {
            MaterializedDocumentValue::Inline(bytes) => crate::canonical_json_bytes::canonicalize_with_depth(bytes),
            MaterializedDocumentValue::Blob(blob) => database.canonicalize_verified_blob(blob),
        }.map_err(|_| invalid_data("versioned document value is not JSON"))?
    };
    let response = {
        let _encode = crate::fsync_stats::StageTimer::diagnostic(
            crate::fsync_stats::STAGE_DOCUMENT_PROJECT_ENCODE);
        encode_document_projection(document, include_key, value.bytes(), maximum_bytes)?
    };
    if response.len() > maximum_bytes {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "versioned document response exceeds 8 MiB",
        ));
    }
    Ok(DocumentProjection { bytes: response, maximum_container_depth: value.maximum_container_depth().map(|depth| depth + 1) })
}


/// The body was strictly parsed and matched Value's canonical serialization.
/// Preserve the previous sorted metadata keys and numeric/string serializer.
fn encode_document_projection(
    document: &Document, include_key: bool, value: &[u8], maximum_bytes: usize,
) -> io::Result<Vec<u8>> {
    let capacity = value.len().saturating_add(document.key.len().saturating_mul(6))
        .saturating_add(96).min(maximum_bytes.saturating_add(1));
    let mut output = Vec::with_capacity(capacity);
    let response_error = |_| invalid_data("versioned document response failed");
    if include_key {
        output.extend_from_slice(b"{\"key\":");
        serde_json::to_writer(&mut output, &document.key).map_err(response_error)?;
        output.extend_from_slice(b",\"updated_at_ms\":");
    } else {
        output.extend_from_slice(b"{\"updated_at_ms\":");
    }
    serde_json::to_writer(&mut output, &document.updated_at_ms).map_err(response_error)?;
    output.extend_from_slice(b",\"value\":");
    output.extend_from_slice(value);
    output.extend_from_slice(b",\"version\":");
    serde_json::to_writer(&mut output, &document.version).map_err(response_error)?;
    output.push(b'}');
    Ok(output)
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
            namespace: request.namespace.to_owned().into(),
            key: request.destination_logical_key.to_owned().into(),
            version: 1,
            updated_at_ms: request.updated_at_ms,
            value: source.value,
        }
        .encode()?,
    )
}

/// Hot-path header load: return the materialized value bytes and the physical
/// version in one entity read, without the projection envelope's intermediate
/// parse/serialize round-trip and without a validation parse (the caller
/// parses the bytes itself). Blob-carried values materialize exactly like the
/// generic projection path; identity and byte bounds are unchanged.
pub(crate) fn get_value_with_version(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
    namespace: &str,
    logical_key: &str,
) -> io::Result<Option<(Vec<u8>, u64)>> {
    get_value_with_version_bounded(database, transaction, key, namespace, logical_key, usize::MAX)
}

pub(crate) fn get_value_with_version_bounded(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
    namespace: &str,
    logical_key: &str,
    maximum_bytes: usize,
) -> io::Result<Option<(Vec<u8>, u64)>> {
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
        maximum_bytes,
    )?;
    Ok(Some((value, document.version)))
}

/// Hot-path header store for callers that hold the current physical version
/// from an earlier read *in the same transaction* and produced `value_json`
/// from an in-memory `serde_json::Value`. The earlier read already registered
/// the key's point witness, so skipping the CAS re-read leaves OCC conflict
/// detection unchanged (both observe the same transaction snapshot); skipping
/// the validation parse is safe because the bytes are machine-produced. No
/// response envelope is built: only the stored envelope bytes are returned.
pub(crate) fn put_known_version_returning_envelope(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: PutRequest,
    known_current_version: u64,
) -> io::Result<Vec<u8>> {
    let PutRequest {
        key,
        namespace,
        logical_key,
        value_json,
        expected_version,
        updated_at_ms,
    } = request;
    if updated_at_ms == 0 {
        return Err(invalid_input("invalid versioned document mutation"));
    }
    if expected_version.is_some_and(|expected| expected != known_current_version) {
        return Err(conflict());
    }
    let version = known_current_version
        .checked_add(1)
        .ok_or_else(|| invalid_data("versioned document version overflow"))?;
    let value = stage_document_value(
        database,
        transaction,
        &namespace,
        &logical_key,
        value_json,
        transaction.owner_user_id(),
    )?;
    let stored_envelope = Document {
        namespace: namespace.into(),
        key: logical_key.into(),
        version,
        updated_at_ms,
        value,
    }
    .encode()?;
    database.entity_put(transaction, key, stored_envelope.clone())?;
    Ok(stored_envelope)
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

/// Same owner/identity/materialization bounds as get; preserves the immutable
/// projection proof until the IR slot owns the bytes.
pub(crate) fn get_with_response_proof(
    database: &AuthorityDatabase, transaction: &mut AuthorityTransaction,
    key: &EntityKey, namespace: &str, logical_key: &str,
) -> io::Result<Option<DocumentProjection>> {
    get_projection_with_blob_owner_bounded(database, transaction, key, namespace,
        logical_key, transaction.owner_user_id(), usize::MAX)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;
    use std::sync::Arc;

    use crate::vfs::{DeterministicVfs, Vfs};

    const TENANT: u64 = 7;
    const OWNER: u64 = 11;

    #[test]
    fn canonical_proof_cache_does_not_survive_an_authority_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let key = document_key("reopen-cache");
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_hot_data_caches(32 * 1024 * 1024, 16 * 1024 * 1024)
            .unwrap();
        let mut transaction = database.begin(TENANT, OWNER).unwrap();
        put(
            &database,
            &mut transaction,
            put_request(
                "reopen-cache",
                serde_json::to_vec(&json!({"text":"x".repeat(32*1024)})).unwrap(),
                Some(0),
            ),
        )
        .unwrap();
        database.commit(transaction).unwrap();
        let observer = database.canonical_json_cache_observer();
        let mut query = database.begin(TENANT, OWNER).unwrap();
        let original =
            get_with_response_proof(&database, &mut query, &key, "conversations", "reopen-cache")
                .unwrap()
                .unwrap();
        drop(query);
        assert_eq!(observer.metrics().entries, 1);
        drop(database);
        let database = AuthorityDatabase::open(directory.path()).unwrap();
        database
            .configure_hot_data_caches(32 * 1024 * 1024, 16 * 1024 * 1024)
            .unwrap();
        assert_eq!(
            database.canonical_json_cache_observer().metrics().entries,
            0
        );
        let mut query = database.begin(TENANT, OWNER).unwrap();
        let reopened =
            get_with_response_proof(&database, &mut query, &key, "conversations", "reopen-cache")
                .unwrap()
                .unwrap();
        assert_eq!(reopened.bytes, original.bytes);
        assert_eq!(
            (
                database.canonical_json_cache_observer().metrics().misses,
                database.canonical_json_cache_observer().metrics().hits
            ),
            (1, 0)
        );
        // A retained anonymous observer owns the old bounded cache, but never
        // installs it into the new authority or changes its cold validation.
        assert_eq!(observer.metrics().entries, 1);
    }

    #[test]
    fn canonical_proof_cache_is_shared_by_replicas_and_never_bypasses_scope_or_changed_body() {
        let mut database = authority();
        database
            .configure_hot_data_caches(32 * 1024 * 1024, 16 * 1024 * 1024)
            .unwrap();
        let value = json!({"items":[1,true,null],"text":"x".repeat(32 * 1024)});
        let mut transaction = database.begin(TENANT, OWNER).unwrap();
        let envelope = put_with_blob_owner_bounded_returning_envelope(
            &database,
            &mut transaction,
            put_request("cached", serde_json::to_vec(&value).unwrap(), Some(0)),
            OWNER,
            usize::MAX,
        )
        .unwrap()
        .1;
        database.commit(transaction).unwrap();
        let document = Document::decode(&envelope).unwrap();
        assert!(matches!(document.value, DocumentValue::Blob(_)));
        let observer = database.canonical_json_cache_observer();
        let expected = serde_json::to_vec(
            &json!({"value":value,"version":1,"updated_at_ms":1_700_000_000_000u64}),
        )
        .unwrap();
        for source in [&database, &database.build_read_replica().unwrap()] {
            let output = project_document_with_proof(
                source,
                TENANT,
                OWNER,
                &document,
                false,
                usize::MAX,
            )
            .unwrap();
            assert!(output.json_proven());
            assert_eq!(output.bytes, expected);
        }
        assert_eq!(
            (
                observer.metrics().entries,
                observer.metrics().hits,
                observer.metrics().misses
            ),
            (1, 1, 1)
        );
        let before = observer.metrics();
        for (tenant, owner) in [(TENANT + 1, OWNER), (TENANT, OWNER + 1)] {
            assert_eq!(
                project_document_with_proof(
                    &database,
                    tenant,
                    owner,
                    &document,
                    false,
                    usize::MAX
                )
                .err()
                .unwrap()
                .kind(),
                io::ErrorKind::PermissionDenied
            );
        }
        assert_eq!(observer.metrics().hits, before.hits);
        assert_eq!(
            project_document_with_proof(&database, TENANT, OWNER, &document, false, 1)
                .err()
                .unwrap()
                .to_string(),
            "versioned document blob exceeds response bound"
        );
        assert_eq!(observer.metrics().hits, before.hits);
        let pinned = database.build_read_replica().unwrap();
        let mut transaction = database.begin(TENANT, OWNER).unwrap();
        let changed = json!({"items":[[[null]]],"text":"y".repeat(32 * 1024)});
        let envelope = put_with_blob_owner_bounded_returning_envelope(
            &database,
            &mut transaction,
            put_request("cached", serde_json::to_vec(&changed).unwrap(), Some(1)),
            OWNER,
            usize::MAX,
        )
        .unwrap()
        .1;
        database.commit(transaction).unwrap();
        let changed_document = Document::decode(&envelope).unwrap();
        let output = project_document_with_proof(
            &database,
            TENANT,
            OWNER,
            &changed_document,
            false,
            usize::MAX,
        )
        .unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&output.bytes).unwrap()["value"],
            changed
        );
        assert_eq!(
            (observer.metrics().entries, observer.metrics().misses),
            (2, 2)
        );
        // The actual pinned MVCC root still selects its older body/certificate.
        let mut query = pinned.begin(TENANT, OWNER).unwrap();
        let older = get_with_response_proof(
            &pinned,
            &mut query,
            &document_key("cached"),
            "conversations",
            "cached",
        )
        .unwrap()
        .unwrap();
        assert_eq!(older.bytes, expected);
    }

    #[test]
    fn canonical_proof_cache_reuses_only_body_depth_and_rechecks_each_response_envelope() {
        let mut database = authority();
        database
            .configure_hot_data_caches(32 * 1024 * 1024, 16 * 1024 * 1024)
            .unwrap();
        let bytes = format!(
            "{{\"a\":{}null{},\"z\":\"{}\"}}",
            "[".repeat(125),
            "]".repeat(125),
            "x".repeat(32 * 1024)
        )
        .into_bytes();
        let key = document_key("deep-cache");
        let mut transaction = database.begin(TENANT, OWNER).unwrap();
        put(
            &database,
            &mut transaction,
            PutRequest {
                key: key.clone(),
                namespace: "conversations".into(),
                logical_key: "deep-cache".into(),
                value_json: bytes,
                expected_version: Some(0),
                updated_at_ms: 1,
            },
        )
        .unwrap();
        database.commit(transaction).unwrap();
        let mut transaction = database.begin(TENANT, OWNER).unwrap();
        let initial = get_with_response_proof(
            &database,
            &mut transaction,
            &key,
            "conversations",
            "deep-cache",
        )
        .unwrap()
        .unwrap();
        assert!(initial.json_proven());
        let (start, end) = key.exact_range().unwrap();
        let list = list_direction_with_response_proof(
            &database,
            &mut transaction,
            &start,
            &end,
            "conversations",
            1,
            false,
        )
        .unwrap();
        assert!(!list.json_proven());
        assert!(crate::response_json::validate(&list.bytes).is_err());
        assert_eq!(
            (
                database.canonical_json_cache_observer().metrics().hits,
                database.canonical_json_cache_observer().metrics().misses
            ),
            (1, 1)
        );
    }

    fn authority() -> AuthorityDatabase {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        let dyn_vfs: Arc<dyn Vfs> = vfs.clone();
        AuthorityDatabase::initialize_with_vfs(Path::new("/data"), dyn_vfs).unwrap()
    }

    #[test]
    fn byte_and_decoded_readers_preserve_json_acceptance_source_and_bounds() {
        let database = authority();
        let mut cases = vec![
            br#" {"a":1,"a":2,"text":"\u4f60\n"} "#.to_vec(),
            b"[true,null,1.25e3,-0,18446744073709551616]".to_vec(),
            br#"{"$serde_json::private::RawValue":"null"}"#.to_vec(),
            br#"{"$serde_json::private::RawValue":"not json"}"#.to_vec(),
            b"[1,]".to_vec(), b"{}[]".to_vec(), b"1e309".to_vec(),
            b"\"\xff\"".to_vec(),
        ];
        for depth in 125..=129 {
            cases.push(format!("{}null{}", "[".repeat(depth), "]".repeat(depth)).into_bytes());
        }
        for bytes in cases {
            let expected = serde_json::from_slice::<Value>(&bytes);
            let stored = Document { namespace: "conversations".into(), key: "source".into(),
                version: 1, updated_at_ms: 1, value: DocumentValue::Inline(bytes.clone().into()),
            }.encode().unwrap();
            let byte_read = materialize_stored_document_bounded(
                &database, TENANT, OWNER, &stored, "conversations", bytes.len(),
            );
            let decoded_read = materialize_stored_document_value_and_bytes_bounded(
                &database, TENANT, OWNER, &stored, "conversations", bytes.len(),
            );
            assert_eq!(byte_read.is_ok(), expected.is_ok());
            assert_eq!(decoded_read.is_ok(), expected.is_ok());
            if let Ok(value) = expected {
                assert_eq!(byte_read.unwrap(), ("source".to_owned(), bytes.clone()));
                assert_eq!(decoded_read.unwrap(), ("source".to_owned(), value, bytes.clone()));
            } else {
                assert_eq!(byte_read.unwrap_err().kind(), io::ErrorKind::InvalidData);
                assert_eq!(decoded_read.unwrap_err().kind(), io::ErrorKind::InvalidData);
            }
            assert_eq!(materialize_stored_document_bounded(
                &database, TENANT, OWNER, &stored, "other", bytes.len(),
            ).unwrap_err().to_string(), "versioned document identity mismatch");
            assert_eq!(materialize_stored_document_value_and_bytes_bounded(
                &database, TENANT, OWNER, &stored, "conversations", bytes.len() - 1,
            ).unwrap_err().to_string(), "versioned document inline exceeds response bound");
        }
    }

    fn document_key(key: &str) -> EntityKey {
        EntityKey::new(TENANT, OWNER, "conversations", key.as_bytes()).unwrap()
    }

    fn put_request(key: &str, value_json: Vec<u8>, expected: Option<u64>) -> PutRequest {
        PutRequest {
            key: document_key(key),
            namespace: "conversations".to_owned(),
            logical_key: key.to_owned(),
            value_json,
            expected_version: expected,
            updated_at_ms: 1_700_000_000_000,
        }
    }

    #[test]
    fn list_response_proof_accounts_for_the_array_and_all_row_envelopes() {
        for depth in 0..=127 {
            let mut database = authority();
            let key = document_key("proof-row");
            let mut transaction = database.begin(TENANT, OWNER).unwrap();
            let document = Document {
                namespace: "proof".into(),
                key: "row".into(),
                version: 1,
                updated_at_ms: 1,
                value: DocumentValue::Inline(
                    format!("{}null{}", "[".repeat(depth), "]".repeat(depth)).into_bytes().into()),
            };
            database
                .entity_put(&mut transaction, key.clone(), document.encode().unwrap())
                .unwrap();
            database.commit(transaction).unwrap();
            let (start, end) = key.exact_range().unwrap();
            let mut transaction = database.begin(TENANT, OWNER).unwrap();
            let output = list_direction_with_response_proof(
                &database,
                &mut transaction,
                &start,
                &end,
                "proof",
                1,
                false,
            )
            .unwrap();
            let old_valid = serde_json::from_slice::<Value>(&output.bytes).is_ok();
            assert_eq!(output.json_proven(), old_valid, "body depth {depth}");
            assert_eq!(
                output.bytes,
                list_direction(&database, &mut transaction, &start, &end, "proof", 1, false).unwrap()
            );
        }
        let mut database = authority();
        let key = document_key("empty-proof");
        let (start, end) = key.clone().exact_range().unwrap();
        let mut transaction = database.begin(TENANT, OWNER).unwrap();
        let output = list_direction_with_response_proof(
            &database,
            &mut transaction,
            &start,
            &end,
            "proof",
            1,
            false,
        )
        .unwrap();
        assert!(output.json_proven());
        assert_eq!(output.bytes, b"[]");
        let document = Document {
            namespace: "proof".into(),
            key: "row".into(),
            version: 1,
            updated_at_ms: 1,
            value: DocumentValue::Inline(b" null ".to_vec().into()),
        };
        database
            .entity_put(&mut transaction, key.clone(), document.encode().unwrap())
            .unwrap();
        database.commit(transaction).unwrap();
        let mut transaction = database.begin(TENANT, OWNER).unwrap();
        let output = list_direction_with_response_proof(
            &database,
            &mut transaction,
            &start,
            &end,
            "proof",
            1,
            true,
        )
        .unwrap();
        assert!(!output.json_proven());
        assert!(crate::response_json::validate(&output.bytes).is_ok());
    }

    #[test]
    fn response_proof_preserves_the_full_value_recursion_budget() {
        let database = authority();
        for depth in 0..=140 {
            for (open, close) in [("[", "]"), ("{\"a\":", "}")] {
                let bytes = format!("{}null{}", open.repeat(depth), close.repeat(depth)).into_bytes();
                let document = Document {
                    namespace: "proof".into(),
                    key: "key\n😀".into(),
                    version: u64::MAX,
                    updated_at_ms: u64::MAX,
                    value: DocumentValue::Inline(bytes.into()),
                };
                for include_key in [false, true] {
                    match project_document_with_proof(
                        &database,
                        TENANT,
                        OWNER,
                        &document,
                        include_key,
                        8192,
                    ) {
                        Ok(projected) => {
                            let old_valid = serde_json::from_slice::<Value>(&projected.bytes).is_ok();
                            assert_eq!(
                                projected.json_proven(),
                                old_valid,
                                "depth {depth}, key {include_key}"
                            );
                            assert_eq!(
                                crate::response_json::validate(&projected.bytes).is_ok(),
                                old_valid
                            );
                        }
                        Err(_) => assert!(depth > crate::response_json::MAX_VALIDATED_CONTAINER_DEPTH),
                    }
                }
            }
        }
    }

    #[test]
    fn legacy_canonicalization_never_mints_a_response_proof() {
        let database = authority();
        for bytes in [
            b" null ".as_slice(),
            b"-0",
            br#"{"z":1,"a":2}"#,
            br#"{"a":1,"a":2}"#,
            br#"{"first":0,"$serde_json::private::RawValue":"not json"}"#,
        ] {
            let document = Document {
                namespace: "proof".into(),
                key: "key".into(),
                version: 1,
                updated_at_ms: 1,
                value: DocumentValue::Inline(bytes.to_vec().into()),
            };
            let output =
                project_document_with_proof(&database, TENANT, OWNER, &document, false, 8192).unwrap();
            assert!(!output.json_proven());
            assert_eq!(
                output.bytes,
                project_document(&database, TENANT, OWNER, &document, false, 8192).unwrap()
            );
        }
    }

    #[test]
    fn canonical_projection_preserves_exact_legacy_bytes_and_output_bounds() {
        let database = authority();
        let values: &[&[u8]] = &[
            b"null", b"-0", b"1e0", b"18446744073709551616",
            r#"{"a":[1,true,null,{"k":"你好😀"}],"b":"\n\\\""}"#.as_bytes(),
            br#"{"z":1,"a":2,"z":3}"#,
            br#"{"$serde_json::private::RawValue":"{\"z\":1,\"a\":2}"}"#,
        ];
        for bytes in values {
            for include_key in [false, true] {
                let document = Document { namespace: "projection".to_owned().into(), key: "key\n\\\"😀".to_owned().into(),
                    version: u64::MAX, updated_at_ms: u64::MAX, value: DocumentValue::Inline(bytes.to_vec().into()) };
                let value: Value = serde_json::from_slice(bytes).unwrap();
                let previous = if include_key {
                    json!({"key":document.key,"value":value,"version":document.version,"updated_at_ms":document.updated_at_ms})
                } else {
                    json!({"value":value,"version":document.version,"updated_at_ms":document.updated_at_ms})
                };
                let expected = serde_json::to_vec(&previous).unwrap();
                let actual = project_document(&database,TENANT,OWNER,&document,include_key,expected.len()).unwrap();
                assert_eq!(actual,expected);
                let error=project_document(&database,TENANT,OWNER,&document,include_key,expected.len()-1).unwrap_err();
                assert_eq!(error.kind(),io::ErrorKind::OutOfMemory);
                assert_eq!(error.to_string(),"versioned document response exceeds 8 MiB");
            }
        }
    }

    #[test]
    fn canonical_projection_rejects_malformed_inline_and_preserves_blob_scope_fences() {
        let database = authority();
        for bytes in [b"{".as_slice(),b"1e309",br#""\uD800""#,b"\"\xff\"",b"[1,]"] {
            let document=Document { namespace:"projection".to_owned().into(),key:"k".to_owned().into(),version:1,
                updated_at_ms:1,value:DocumentValue::Inline(bytes.to_vec().into()) };
            let error=project_document(&database,TENANT,OWNER,&document,true,1024).unwrap_err();
            assert_eq!(error.kind(),io::ErrorKind::InvalidData);
            assert_eq!(error.to_string(),"versioned document value is not JSON");
        }
        let value=json!({"array":(0..4096).collect::<Vec<_>>()});
        let mut transaction=database.begin(TENANT,OWNER).unwrap();
        let envelope=put_with_blob_owner_bounded_returning_envelope(&database,&mut transaction,
            put_request("large",serde_json::to_vec(&value).unwrap(),Some(0)),OWNER,
            usize::MAX).unwrap().1;
        let document=Document::decode(&envelope).unwrap();
        assert!(matches!(document.value,DocumentValue::Blob(_)));
        let mut oracle=document.clone();
        let logical=materialize_value(&database,TENANT,OWNER,&document.value,usize::MAX).unwrap();
        oracle.value=DocumentValue::Inline(logical.into());
        assert_eq!(project_document(&database,TENANT,OWNER,&document,true,usize::MAX).unwrap(),
            project_document(&database,TENANT,OWNER,&oracle,true,usize::MAX).unwrap());
        for (tenant,owner) in [(TENANT+1,OWNER),(TENANT,OWNER+1)] {
            assert_eq!(project_document(&database,tenant,owner,&document,true,usize::MAX).unwrap_err().kind(),
                io::ErrorKind::PermissionDenied);
        }
    }

    #[test]
    fn decoded_document_reads_keep_byte_bounds_identity_and_blob_scope() {
        for padding in [0, 32 * 1024] {
            let mut database = authority();
            let value = json!({"unicode": "追加🧈", "padding": "x".repeat(padding), "nested": [null, true, 12.5]});
            let mut tx = database.begin(TENANT, OWNER).unwrap();
            let (_, envelope) = put_with_blob_owner_bounded_returning_envelope(&database, &mut tx,
                put_request("conv", serde_json::to_vec(&value).unwrap(), Some(0)), OWNER,
                usize::MAX).unwrap();
            database.commit(tx).unwrap();
            let (key, bytes) = materialize_stored_document_bounded(&database, TENANT, OWNER,
                &envelope, "conversations", usize::MAX).unwrap();
            let (decoded_key, decoded, source_bytes) = materialize_stored_document_value_bounded(
                &database, TENANT, OWNER, &envelope, "conversations", bytes.len()).unwrap();
            assert_eq!(decoded_key, key);
            assert_eq!(decoded, value);
            assert_eq!(source_bytes, bytes.len());
            assert!(materialize_stored_document_value_bounded(&database, TENANT, OWNER,
                &envelope, "conversations", bytes.len() - 1).is_err());
            assert!(materialize_stored_document_value_bounded(&database, TENANT, OWNER,
                &envelope, "wrong-namespace", bytes.len()).is_err());
            if padding > 0 {
                for (tenant, owner) in [(TENANT + 1, OWNER), (TENANT, OWNER + 1)] {
                    let previous = materialize_stored_document_bounded(&database, tenant, owner,
                        &envelope, "conversations", bytes.len()).unwrap_err();
                    let current = materialize_stored_document_value_bounded(&database, tenant, owner,
                        &envelope, "conversations", bytes.len()).unwrap_err();
                    assert_eq!(current.kind(), previous.kind());
                }
            }
        }
        let database = authority();
        let malformed = Document { namespace: "conversations".to_owned().into(), key: "bad".to_owned().into(),
            version: 1, updated_at_ms: 1, value: DocumentValue::Inline(b"{".to_vec().into()) }.encode().unwrap();
        assert_eq!(materialize_stored_document_value_bounded(&database, TENANT, OWNER,
            &malformed, "conversations", 100).unwrap_err().kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn known_version_put_round_trips_value_and_envelope() {
        let mut database = authority();
        let mut tx = database
            .begin_with_identity_claim_scopes(TENANT, OWNER)
            .unwrap();
        let envelope = put_known_version_returning_envelope(
            &database,
            &mut tx,
            put_request("conv", br#"{"rev":0}"#.to_vec(), Some(0)),
            0,
        )
        .unwrap();
        // The returned envelope is exactly what a generic read decodes.
        assert_eq!(
            stored_document_version(&envelope, "conversations", "conv").unwrap(),
            1
        );
        let (value, version) = get_value_with_version(
            &database,
            &mut tx,
            &document_key("conv"),
            "conversations",
            "conv",
        )
        .unwrap()
        .unwrap();
        assert_eq!(value, br#"{"rev":0}"#);
        assert_eq!(version, 1);
        database.commit(tx).unwrap();

        let mut tx = database
            .begin_with_identity_claim_scopes(TENANT, OWNER)
            .unwrap();
        let (value, version) = get_value_with_version(
            &database,
            &mut tx,
            &document_key("conv"),
            "conversations",
            "conv",
        )
        .unwrap()
        .unwrap();
        assert_eq!(value, br#"{"rev":0}"#);
        assert_eq!(version, 1);
    }

    #[test]
    fn known_version_put_conflicts_on_a_version_mismatch() {
        let mut database = authority();
        let mut tx = database
            .begin_with_identity_claim_scopes(TENANT, OWNER)
            .unwrap();
        put_known_version_returning_envelope(
            &database,
            &mut tx,
            put_request("conv", br#"{"rev":0}"#.to_vec(), Some(0)),
            0,
        )
        .unwrap();
        // A caller whose expectation disagrees with the version it observed in
        // this transaction conflicts exactly like the generic CAS put.
        let stale = put_known_version_returning_envelope(
            &database,
            &mut tx,
            put_request("conv", br#"{"rev":1}"#.to_vec(), Some(0)),
            1,
        );
        assert_eq!(stale.unwrap_err().kind(), io::ErrorKind::WouldBlock);
        // The caller tracks its own staged writes (the settings-merge header
        // path stores twice per transaction this way).
        let advanced = put_known_version_returning_envelope(
            &database,
            &mut tx,
            put_request("conv", br#"{"rev":1}"#.to_vec(), Some(1)),
            1,
        )
        .unwrap();
        assert_eq!(
            stored_document_version(&advanced, "conversations", "conv").unwrap(),
            2
        );
    }

    #[test]
    fn document_demand_exceeds_former_default_quota_for_known_and_fresh_paths() {
        let directory = tempfile::tempdir().unwrap();
        let bytes = serde_json::to_vec(&json!({"text":"x".repeat(9 * 1024 * 1024),"keep":7})).unwrap();
        {
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            let mut tx = database.begin(TENANT, OWNER).unwrap();
            let known = put_known_version_returning_envelope(&database, &mut tx,
                put_request("large-known", bytes.clone(), Some(0)), 0).unwrap();
            assert!(matches!(Document::decode(&known).unwrap().value, DocumentValue::Blob(_)));
            let mut fresh = put_request("large-fresh", bytes.clone(), None);
            fresh.expected_version = None;
            put_fresh_prevalidated(&database, &mut tx, fresh).unwrap();
            database.commit(tx).unwrap();
        }
        let database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut tx = database.begin(TENANT, OWNER).unwrap();
        for logical_key in ["large-known", "large-fresh"] {
            let key = document_key(logical_key);
            let value = get_value_with_version_bounded(&database, &mut tx, &key,
                "conversations", logical_key, bytes.len() + 100).unwrap().unwrap();
            assert_eq!(value.1, 1);
            assert_eq!(blake3::hash(&value.0), blake3::hash(&bytes));
        }
    }

    #[test]
    fn known_version_put_rejects_invalid_mutation_metadata() {
        let mut database = authority();
        let mut tx = database
            .begin_with_identity_claim_scopes(TENANT, OWNER)
            .unwrap();
        let mut request = put_request("conv", br#"{"rev":0}"#.to_vec(), Some(0));
        request.updated_at_ms = 0;
        let zero_clock = put_known_version_returning_envelope(&database, &mut tx, request, 0);
        assert_eq!(zero_clock.unwrap_err().kind(), io::ErrorKind::InvalidInput);
        let request = put_request("conv", b"{}".to_vec(), Some(1));
        let stale = put_known_version_returning_envelope(&database, &mut tx, request, 0);
        assert_eq!(stale.unwrap_err().kind(), io::ErrorKind::WouldBlock);
    }

    #[test]
    fn get_value_with_version_enforces_identity() {
        let mut database = authority();
        let mut tx = database
            .begin_with_identity_claim_scopes(TENANT, OWNER)
            .unwrap();
        put_known_version_returning_envelope(
            &database,
            &mut tx,
            put_request("conv", br#"{"rev":0}"#.to_vec(), Some(0)),
            0,
        )
        .unwrap();
        let wrong_namespace =
            get_value_with_version(&database, &mut tx, &document_key("conv"), "records", "conv");
        assert_eq!(
            wrong_namespace.unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
        let wrong_key = get_value_with_version(
            &database,
            &mut tx,
            &document_key("conv"),
            "conversations",
            "other",
        );
        assert_eq!(wrong_key.unwrap_err().kind(), io::ErrorKind::InvalidData);
        assert!(get_value_with_version(
            &database,
            &mut tx,
            &document_key("missing"),
            "conversations",
            "missing"
        )
        .unwrap()
        .is_none());
    }
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
        usize::MAX,
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
        usize::MAX,
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
    get_projection_with_blob_owner_bounded(database, transaction, key, namespace,
        logical_key, blob_owner_user_id, maximum_bytes).map(|value| value.map(DocumentProjection::into_bytes))
}

fn get_projection_with_blob_owner_bounded(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
    namespace: &str,
    logical_key: &str,
    blob_owner_user_id: u64,
    maximum_bytes: usize,
) -> io::Result<Option<DocumentProjection>> {
    let Some(raw) = database.entity_get(transaction, key)? else {
        return Ok(None);
    };
    let document = Document::decode(&raw)?;
    if document.namespace != namespace || document.key != logical_key {
        return Err(invalid_data("versioned document identity mismatch"));
    }
    project_document_with_proof(
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

/// Fresh-insert variant of [`put`] for callers that serialized `value_json`
/// themselves inside the same transaction and that establish key freshness
/// through an in-transaction allocator invariant (e.g. the conversation sync
/// head, which hands out each sequence exactly once and advances atomically
/// with the insert).
///
/// Two costs of the generic path are dropped:
///
/// * the defensive JSON re-parse — the bytes are valid by construction
///   (serde serialization, optionally splicing raw documents that were
///   validated when they were encoded);
/// * the existence read behind `expected_version` — freshness is the
///   caller's invariant, so the insert is version 1 by definition.
///   Concurrency safety is unchanged: the commit-time write witness
///   (`entity write witness changed`) still fails closed when an
///   intervening commit created the same key, which is exactly how
///   duplicate sequence allocators are rejected through the head row.
///
/// The stored envelope — including the inline/blob placement decision — is
/// byte-identical to what [`put`] persists for a genuinely fresh key. If the
/// caller's freshness invariant is ever violated by store corruption, the
/// insert replaces the corrupt row instead of returning a conflict error;
/// every replay-visible byte stays internally consistent.
pub(crate) fn put_fresh_prevalidated(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: PutRequest,
) -> io::Result<()> {
    let PutRequest {
        key,
        namespace,
        logical_key,
        value_json,
        expected_version,
        updated_at_ms,
    } = request;
    if updated_at_ms == 0 {
        return Err(invalid_input("invalid versioned document mutation"));
    }
    // Never silently drop a version expectation: freshness is established by
    // the caller's allocator invariant, so an explicit expectation here is
    // always a caller bug.
    if expected_version.is_some() {
        return Err(invalid_input(
            "fresh versioned document insert carries a version expectation",
        ));
    }
    let value = stage_document_value(
        database,
        transaction,
        &namespace,
        &logical_key,
        value_json,
        transaction.owner_user_id(),
    )?;
    let stored_envelope = Document {
        namespace: namespace.into(),
        key: logical_key.into(),
        version: 1,
        updated_at_ms,
        value,
    }
    .encode()?;
    database.entity_put(transaction, key, stored_envelope)?;
    Ok(())
}

/// Owner-scoped envelope-returning variant of [`put`]; see
/// [`put_with_blob_owner_bounded_returning_envelope`].
pub(crate) fn put_returning_envelope(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: PutRequest,
) -> io::Result<(Vec<u8>, Vec<u8>)> {
    put_with_blob_owner_bounded_returning_envelope(
        database,
        transaction,
        request,
        transaction.owner_user_id(),
        usize::MAX,
    )
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
        usize::MAX,
    )
}

pub(crate) fn put_with_blob_owner_bounded(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: PutRequest,
    blob_owner_user_id: u64,
    maximum_bytes: usize,
) -> io::Result<Vec<u8>> {
    Ok(put_with_blob_owner_bounded_returning_envelope(
        database,
        transaction,
        request,
        blob_owner_user_id,
        maximum_bytes,
    )?
    .0)
}

/// Same mutation as [`put_with_blob_owner_bounded`], but also returns the
/// exact stored envelope bytes. Hot callers that stage the envelope into a
/// secondary index (turn lane/activity projections, conversation updated
/// index) reuse it instead of re-reading the entity they just wrote; the
/// bytes are identical by construction because the put is the only write to
/// that key inside the transaction.
pub(crate) fn put_with_blob_owner_bounded_returning_envelope(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: PutRequest,
    blob_owner_user_id: u64,
    maximum_bytes: usize,
) -> io::Result<(Vec<u8>, Vec<u8>)> {
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
    let value = stage_document_value(
        database,
        transaction,
        &namespace,
        &logical_key,
        value_json,
        blob_owner_user_id,
    )?;
    let stored_envelope = Document {
        namespace: namespace.into(),
        key: logical_key.clone().into(),
        version,
        updated_at_ms,
        value,
    }
    .encode()?;
    database.entity_put(transaction, key, stored_envelope.clone())?;
    let response = serde_json::to_vec(
        &json!({"key": logical_key, "version": version, "updated_at_ms": updated_at_ms}),
    )
    .map_err(|_| invalid_data("versioned document response failed"))?;
    Ok((response, stored_envelope))
}

fn stage_document_value(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    logical_key: &str,
    value_json: Vec<u8>,
    blob_owner_user_id: u64,
) -> io::Result<DocumentValue<'static>> {
    let inline_envelope_bytes =
        FIXED_HEADER_BYTES + namespace.len() + logical_key.len() + value_json.len();
    if inline_envelope_bytes <= MAX_ENTITY_INLINE_VALUE_BYTES {
        Ok(DocumentValue::Inline(value_json.into()))
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
        Ok(DocumentValue::Blob(reference))
    }
}

pub(crate) fn restore_snapshot(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: SnapshotDocument,
) -> io::Result<SnapshotRestoreOutcome> {
    if request.version == 0
        || request.updated_at_ms == 0
        || request.value_json.len() > MAX_MIGRATION_DOCUMENT_BYTES
        || request.namespace.len() > u16::MAX as usize
        || request.logical_key.len() > u16::MAX as usize
        || FIXED_HEADER_BYTES
            + request.namespace.len()
            + request.logical_key.len()
            + BLOB_REFERENCE_BYTES
            > MAX_ENTITY_INLINE_VALUE_BYTES
    {
        return Err(invalid_input(
            "invalid migration document metadata or byte bound",
        ));
    }
    if request.blob_owner_user_id != transaction.owner_user_id()
        && request.blob_owner_user_id != crate::conversation_header::TENANT_GLOBAL_OWNER_ID
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "migration blob owner is outside the transaction scope",
        ));
    }
    serde_json::from_slice::<Value>(&request.value_json)
        .map_err(|_| invalid_input("migration document value is not JSON"))?;
    if let Some(raw) = database.entity_get(transaction, &request.key)? {
        let current = Document::decode(&raw)?;
        if current.namespace != request.namespace || current.key != request.logical_key {
            return Err(invalid_data("migration document identity mismatch"));
        }
        if current.version != request.version
            || current.updated_at_ms != request.updated_at_ms
            || materialize_value(
                database,
                transaction.tenant_id(),
                request.blob_owner_user_id,
                &current.value,
                MAX_MIGRATION_DOCUMENT_BYTES,
            )? != request.value_json
        {
            return Err(conflict());
        }
        return Ok(SnapshotRestoreOutcome::AlreadyMatches);
    }
    let value = stage_document_value(
        database,
        transaction,
        &request.namespace,
        &request.logical_key,
        request.value_json,
        request.blob_owner_user_id,
    )?;
    let envelope = Document {
        namespace: request.namespace.into(),
        key: request.logical_key.into(),
        version: request.version,
        updated_at_ms: request.updated_at_ms,
        value,
    }
    .encode()?;
    database.entity_put(transaction, request.key, envelope)?;
    Ok(SnapshotRestoreOutcome::Inserted)
}

/// Offline snapshot readers retain the exact persisted JSON spelling and metadata.
pub(crate) fn read_snapshot(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
    namespace: &str,
    logical_key: &str,
) -> io::Result<Option<SnapshotDocument>> {
    read_snapshot_bounded(
        database,
        transaction,
        key,
        namespace,
        logical_key,
        MAX_MIGRATION_DOCUMENT_BYTES,
    )
}

pub(crate) fn read_snapshot_bounded(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
    namespace: &str,
    logical_key: &str,
    maximum_bytes: usize,
) -> io::Result<Option<SnapshotDocument>> {
    let Some(raw) = database.entity_get(transaction, key)? else {
        return Ok(None);
    };
    let document = Document::decode(&raw)?;
    if document.namespace != namespace || document.key != logical_key {
        return Err(invalid_data("migration document identity mismatch"));
    }
    Ok(Some(SnapshotDocument {
        key: key.clone(),
        namespace: document.namespace.into_owned(),
        logical_key: document.key.into_owned(),
        version: document.version,
        updated_at_ms: document.updated_at_ms,
        value_json: materialize_value(
            database,
            transaction.tenant_id(),
            transaction.owner_user_id(),
            &document.value,
            maximum_bytes,
        )?,
        blob_owner_user_id: transaction.owner_user_id(),
    }))
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
    list_direction(database, transaction, start, end, namespace, limit, false)
}

pub(crate) fn list_direction(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    start: &EntityKey,
    end: &EntityKey,
    namespace: &str,
    limit: usize,
    reverse: bool,
) -> io::Result<Vec<u8>> {
    list_direction_bounded(
        database,
        transaction,
        start,
        end,
        namespace,
        limit,
        ListProjectionBounds {
            blob_owner_user_id: transaction.owner_user_id(),
            maximum_bytes: usize::MAX,
        },
        reverse,
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
    list_direction_bounded(database, transaction, start, end, namespace, limit, bounds, false)
}

fn list_direction_bounded(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    start: &EntityKey,
    end: &EntityKey,
    namespace: &str,
    limit: usize,
    bounds: ListProjectionBounds,
    reverse: bool,
) -> io::Result<Vec<u8>> {
    list_direction_bounded_with_proof(database, transaction, start, end, namespace, limit, bounds, reverse, true)
        .map(DocumentProjection::into_bytes)
}

pub(crate) fn list_direction_with_response_proof(
    database: &AuthorityDatabase, transaction: &mut AuthorityTransaction,
    start: &EntityKey, end: &EntityKey, namespace: &str, limit: usize, reverse: bool,
) -> io::Result<DocumentProjection> {
    list_direction_bounded_with_proof(database, transaction, start, end, namespace, limit,
        ListProjectionBounds { blob_owner_user_id: transaction.owner_user_id(),
            maximum_bytes: usize::MAX }, reverse, true)
}

/// Enumerate authority versions without fetching blob-backed values or decoding JSON.
pub(crate) fn list_headers_direction_with_response_proof(
    database: &AuthorityDatabase, transaction: &mut AuthorityTransaction,
    start: &EntityKey, end: &EntityKey, namespace: &str, limit: usize, reverse: bool,
) -> io::Result<DocumentProjection> {
    list_direction_bounded_with_proof(database, transaction, start, end, namespace, limit,
        ListProjectionBounds { blob_owner_user_id: transaction.owner_user_id(),
            maximum_bytes: usize::MAX }, reverse, false)
}

fn list_direction_bounded_with_proof(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    start: &EntityKey,
    end: &EntityKey,
    namespace: &str,
    limit: usize,
    bounds: ListProjectionBounds,
    reverse: bool,
    include_value: bool,
) -> io::Result<DocumentProjection> {
    let mut rows = Vec::with_capacity(limit.min(MAX_ENTITY_RANGE_ROWS));
    let mut page_start = start.clone();
    let mut page_end = end.clone();
    while rows.len() < limit {
        let page_limit = (limit - rows.len()).min(MAX_ENTITY_RANGE_ROWS);
        let page = if reverse {
            database.entity_scan_reverse(transaction, start, &page_end, page_limit)?
        } else {
            database.entity_scan(transaction, &page_start, end, page_limit)?
        };
        if page.is_empty() { break; }
        let next = page
            .last()
            .map(|(key, _)| key.clone().exact_range())
            .transpose()?;
        rows.extend(page);
        if rows.len() == limit {
            break;
        }
        let boundary = next.ok_or_else(|| invalid_data("versioned document pagination lost continuation"))?;
        if reverse { page_end = boundary.0; } else { page_start = boundary.1; }
    }
    let mut output = Vec::from(b"[".as_slice());
    let mut maximum_container_depth = Some(1_usize);
    for (index, (_, raw)) in rows.into_iter().enumerate() {
        let document = Document::decode(&raw)?;
        if document.namespace != namespace {
            return Err(invalid_data("versioned document namespace mismatch"));
        }
        let projected = if include_value {
            project_document_with_proof(
                database,
                transaction.tenant_id(),
                bounds.blob_owner_user_id,
                &document,
                true,
                bounds.maximum_bytes,
            )?
        } else {
            DocumentProjection {
                bytes: serde_json::to_vec(&serde_json::json!({
                    "key": document.key,
                    "version": document.version,
                    "updated_at_ms": document.updated_at_ms,
                }))?,
                maximum_container_depth: Some(1),
            }
        };
        maximum_container_depth = maximum_container_depth.zip(projected.maximum_container_depth)
            .map(|(current,row)| current.max(row + 1));
        let additional = projected.bytes.len().checked_add(usize::from(index > 0))
            .and_then(|length| length.checked_add(1))
            .ok_or_else(|| invalid_input("versioned document page length overflow"))?;
        let next_length = output.len().checked_add(additional)
            .ok_or_else(|| invalid_input("versioned document page length overflow"))?;
        if next_length > bounds.maximum_bytes {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "versioned document page exceeds requested projection bytes",
            ));
        }
        output.try_reserve(additional)
            .map_err(|_| io::Error::new(io::ErrorKind::OutOfMemory, "cannot allocate complete versioned document page"))?;
        if index > 0 {
            output.push(b',');
        }
        output.extend_from_slice(&projected.bytes);
    }
    output.push(b']');
    Ok(DocumentProjection { bytes: output, maximum_container_depth })
}


#[cfg(test)]
mod borrowed_document_decode_tests {
    use super::*;
    #[test]
    fn borrowed_document_preserves_source_bytes_and_rejects_every_truncated_envelope() {
        let encoded = Document { namespace: "borrowed".into(), key: "escaped\n🧈".into(),
            version: 7, updated_at_ms: 17,
            value: DocumentValue::Inline(b"{\"text\":\"immutable\",\"items\":[1,null,true]}".to_vec().into()) }.encode().unwrap();
        let decoded = Document::decode(&encoded).unwrap();
        assert_eq!(decoded.encode().unwrap(), encoded);
        assert!(matches!(decoded.namespace, std::borrow::Cow::Borrowed(_)));
        assert!(matches!(decoded.key, std::borrow::Cow::Borrowed(_)));
        let DocumentValue::Inline(std::borrow::Cow::Borrowed(bytes)) = decoded.value else { panic!("inline decoding copied its source"); };
        let offset = bytes.as_ptr() as usize - encoded.as_ptr() as usize;
        assert_eq!(&encoded[offset..offset+bytes.len()], bytes);
        for length in 0..encoded.len() { assert!(Document::decode(&encoded[..length]).is_err(), "accepted truncated envelope at {length}"); }
        let mut trailing=encoded.clone();trailing.push(0);assert!(Document::decode(&trailing).is_err());
        for (offset,value) in [(8,2),(12,0),(20,0),(32,9),(33,1)] {
            let mut invalid=encoded.clone();invalid[offset]=value;assert!(Document::decode(&invalid).is_err(),"accepted malformed header at {offset}");
        }
    }
}
