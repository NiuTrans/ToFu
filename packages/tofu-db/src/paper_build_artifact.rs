//! Retained paper-build uploads, leases and publication inside authority transactions.
//!
//! Payloads use the generic record/blob codec. A tenant-wide bounded reservation
//! index serializes capacity changes through transaction witnesses. Research CAS
//! calls `retain_research_build` before publishing its workspace; no worker, file
//! cache or request receipt owns artifact bytes.

use std::collections::BTreeMap;
use std::io;

use base64::engine::{general_purpose, GeneralPurpose, GeneralPurposeConfig};
use base64::Engine as _;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_paper_build as limits;
use crate::paper_build_receipt::{self as receipt, encode_bounded, invalid, lower_hex};
use crate::versioned_document::{self, PutRequest};

const META: &str = "paper.build.artifact.v1";
const CHUNK: &str = "paper.build.chunk.v1";
const DOCUMENT: &str = "paper.build.document.v1";
const MAX_DOCUMENT_BYTES: usize = limits::RETAINED_CHUNK_BYTES.div_ceil(3) * 4 + 8192;
const BASE64: GeneralPurpose = GeneralPurpose::new(
    &base64::alphabet::STANDARD,
    GeneralPurposeConfig::new().with_decode_allow_trailing_bits(true),
);

fn corrupt(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, message)
}
fn missing() -> io::Error {
    io::Error::new(
        io::ErrorKind::NotFound,
        "retained paper build is unavailable",
    )
}

fn text(payload: &Map<String, Value>, key: &str, maximum: usize) -> io::Result<String> {
    let value = payload
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid("missing paper artifact text"))?;
    if value.is_empty() || value.chars().count() > maximum {
        return Err(invalid("invalid paper artifact text"));
    }
    Ok(value.to_owned())
}
fn token(value: &str) -> io::Result<()> {
    if !lower_hex(value, 32, 32) {
        return Err(invalid("invalid paper artifact witness"));
    }
    Ok(())
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Identity {
    pub document_id: String,
    pub build_id: String,
}
impl Identity {
    fn parse(payload: &Map<String, Value>) -> io::Result<Self> {
        let identity = Self {
            document_id: text(payload, "document_id", 64)?,
            build_id: text(payload, "build_id", 32)?,
        };
        identity.validate()?;
        Ok(identity)
    }
    fn validate(&self) -> io::Result<()> {
        if !lower_hex(&self.document_id, 32, 64) || !lower_hex(&self.build_id, 32, 32) {
            return Err(invalid("invalid paper build identity"));
        }
        Ok(())
    }
    fn key(&self, tx: &AuthorityTransaction) -> String {
        format!(
            "{}:{}:{}",
            tx.owner_user_id(),
            self.document_id,
            self.build_id
        )
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct UploadWitness {
    pub identity: Identity,
    pub upload_id: String,
}
impl UploadWitness {
    pub(crate) fn parse(value: &Value) -> io::Result<Self> {
        let payload = value
            .as_object()
            .ok_or_else(|| invalid("invalid retained artifact proof"))?;
        Self::from_map(payload)
    }
    fn from_map(payload: &Map<String, Value>) -> io::Result<Self> {
        let result = Self {
            identity: Identity::parse(payload)?,
            upload_id: text(payload, "upload_id", 32)?,
        };
        result.validate()?;
        Ok(result)
    }
    pub(crate) fn validate(&self) -> io::Result<usize> {
        self.identity.validate()?;
        token(&self.upload_id)?;
        Ok(self.identity.document_id.len() + self.identity.build_id.len() + self.upload_id.len())
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LeaseOperation {
    Retain,
    Renew,
    Release,
}
/// The only constructor performs strict decoding and computes the immutable
/// canonical bytes and digest. No Deserialize implementation or mutable field
/// access can introduce an unvalidated chunk through a direct IR caller.
#[derive(Clone, Debug)]
pub struct ValidatedChunk {
    data: String,
    sha256: String,
    decoded_bytes: usize,
}

impl ValidatedChunk {
    pub fn decode(encoded: &str) -> io::Result<Self> {
        let bytes = decode_chunk(encoded)?;
        Ok(Self {
            data: general_purpose::STANDARD.encode(&bytes),
            sha256: format!("{:x}", Sha256::digest(&bytes)),
            decoded_bytes: bytes.len(),
        })
    }

    fn literal_bytes(&self) -> usize {
        self.data.len() + self.sha256.len() + std::mem::size_of::<u64>()
    }
}

#[derive(Serialize)]
struct ChunkDocument<'a> {
    data: &'a str,
    sha256: &'a str,
}

#[derive(Clone, Debug)]
pub enum Operation {
    Prepare {
        upload: UploadWitness,
        receipt: Value,
    },
    Append {
        upload: UploadWitness,
        sequence: u64,
        data: ValidatedChunk,
    },
    Seal {
        upload: UploadWitness,
    },
    Discard {
        upload: UploadWitness,
    },
    Lease {
        identity: Identity,
        lease_id: String,
        operation: LeaseOperation,
    },
    Read {
        identity: Identity,
        lease_id: String,
        sequence: u64,
    },
    Latest {
        document_id: String,
    },
    Prune,
}
#[derive(Clone, Debug)]
pub struct Request {
    pub operation: Operation,
    pub now_unix_ms: u64,
}
impl Request {
    pub(crate) fn parse(
        name: &str,
        object: &Map<String, Value>,
        now_unix_ms: u64,
    ) -> io::Result<Self> {
        let sequence = || {
            object
                .get("sequence")
                .and_then(Value::as_u64)
                .ok_or_else(|| invalid("invalid artifact sequence"))
        };
        let operation = match name {
            "paper.artifact.prepare" => Operation::Prepare {
                upload: UploadWitness::from_map(object)?,
                receipt: object
                    .get("receipt")
                    .cloned()
                    .ok_or_else(|| invalid("missing retained receipt"))?,
            },
            "paper.artifact.append" => Operation::Append {
                upload: UploadWitness::from_map(object)?,
                sequence: sequence()?,
                data: ValidatedChunk::decode(
                    object
                        .get("data")
                        .and_then(Value::as_str)
                        .ok_or_else(|| invalid("missing paper artifact chunk"))?,
                )?,
            },
            "paper.artifact.seal" => Operation::Seal {
                upload: UploadWitness::from_map(object)?,
            },
            "paper.artifact.discard" => Operation::Discard {
                upload: UploadWitness::from_map(object)?,
            },
            "paper.artifact.lease" => Operation::Lease {
                identity: Identity::parse(object)?,
                lease_id: text(object, "lease_id", 32)?,
                operation: match object.get("operation").and_then(Value::as_str) {
                    Some("retain") => LeaseOperation::Retain,
                    Some("renew") => LeaseOperation::Renew,
                    Some("release") => LeaseOperation::Release,
                    _ => return Err(invalid("invalid artifact lease operation")),
                },
            },
            "paper.artifact.read" => Operation::Read {
                identity: Identity::parse(object)?,
                lease_id: text(object, "lease_id", 32)?,
                sequence: sequence()?,
            },
            "paper.artifact.latest" => Operation::Latest {
                document_id: text(object, "document_id", 64)?,
            },
            "paper.artifact.prune" => Operation::Prune,
            _ => return Err(invalid("unknown retained artifact operation")),
        };
        let request = Self {
            operation,
            now_unix_ms,
        };
        request.validate()?;
        Ok(request)
    }
    pub fn mutates_state(&self) -> bool {
        !matches!(
            self.operation,
            Operation::Read { .. } | Operation::Latest { .. }
        )
    }
    pub fn validate(&self) -> io::Result<usize> {
        validate_clock(self.now_unix_ms)?;
        // Charge retained literal fields, as other typed IR steps do. The chunk
        // constructor bounds and validates its private immutable representation;
        // repeated IR admission never serializes or decodes its payload again.
        let bytes = match &self.operation {
            Operation::Prepare {
                upload,
                receipt: value,
            } => {
                let identity_bytes = upload.validate()?;
                receipt::validate(value)?;
                identity_bytes + encode_bounded(value, limits::RETAINED_RECEIPT_BYTES)?.len()
            }
            Operation::Append { upload, data, .. } => upload.validate()? + data.literal_bytes() + 8,
            Operation::Seal { upload } | Operation::Discard { upload } => upload.validate()?,
            Operation::Lease {
                identity, lease_id, ..
            }
            | Operation::Read {
                identity, lease_id, ..
            } => {
                identity.validate()?;
                token(lease_id)?;
                identity.document_id.len() + identity.build_id.len() + lease_id.len() + 8
            }
            Operation::Latest { document_id } => {
                if !lower_hex(document_id, 32, 64) {
                    return Err(invalid("invalid paper document identity"));
                }
                document_id.len()
            }
            Operation::Prune => 0,
        };
        bytes
            .checked_add(8)
            .filter(|bytes| *bytes <= MAX_DOCUMENT_BYTES + 1024)
            .ok_or_else(|| invalid("paper artifact literals exceed their byte bound"))
    }
}

fn validate_clock(now: u64) -> io::Result<()> {
    let maximum_lease_ms =
        limits::RETAINED_UPLOAD_SECONDS.max(limits::VIEWER_LEASE_SECONDS) as u64 * 1000;
    if now == 0 || now.checked_add(maximum_lease_ms).is_none() {
        return Err(invalid("invalid retained artifact clock"));
    }
    Ok(())
}

fn decode_chunk(encoded: &str) -> io::Result<Vec<u8>> {
    if encoded.is_empty() || encoded.len() > limits::RETAINED_CHUNK_BYTES.div_ceil(3) * 4 {
        return Err(invalid(
            "paper artifact chunk exceeds its encoded byte bound",
        ));
    }
    let bytes = BASE64
        .decode(encoded)
        .map_err(|_| invalid("invalid paper artifact chunk encoding"))?;
    if bytes.len() > limits::RETAINED_CHUNK_BYTES {
        return Err(invalid("paper artifact chunk exceeds its byte bound"));
    }
    Ok(bytes)
}

fn record_key(tx: &AuthorityTransaction, namespace: &str, key: &str) -> io::Result<EntityKey> {
    versioned_document::record_key(tx.tenant_id(), tx.owner_user_id(), namespace, key)
}
fn get(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    namespace: &str,
    key: &str,
) -> io::Result<Option<Value>> {
    let physical = record_key(tx, namespace, key)?;
    versioned_document::get_value_with_blob_owner_bounded(
        db,
        tx,
        &physical,
        namespace,
        key,
        tx.owner_user_id(),
        MAX_DOCUMENT_BYTES,
    )?
    .map(|bytes| {
        serde_json::from_slice::<Value>(&bytes)
            .map_err(|_| corrupt("invalid retained artifact document"))
            .and_then(|value| {
                if value.is_object() {
                    Ok(value)
                } else {
                    Err(corrupt("invalid retained artifact document"))
                }
            })
    })
    .transpose()
}
fn put<T: Serialize>(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    namespace: &str,
    key: &str,
    value: &T,
    now: u64,
) -> io::Result<()> {
    let physical = record_key(tx, namespace, key)?;
    versioned_document::put_with_blob_owner_bounded(
        db,
        tx,
        PutRequest {
            key: physical,
            namespace: namespace.to_owned(),
            logical_key: key.to_owned(),
            value_json: encode_bounded(value, MAX_DOCUMENT_BYTES)?,
            expected_version: None,
            updated_at_ms: now,
        },
        tx.owner_user_id(),
        MAX_DOCUMENT_BYTES,
    )?;
    Ok(())
}
fn delete(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    namespace: &str,
    key: &str,
) -> io::Result<()> {
    db.entity_delete(tx, record_key(tx, namespace, key)?)
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum State {
    Uploading,
    Sealed,
    Pinned,
    Retired,
    Reclaiming,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct Metadata {
    receipt: Value,
    upload_id: String,
    state: State,
    next_sequence: u64,
    readers: BTreeMap<String, u64>,
    expires_at_ms: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    gc_sequence: Option<u64>,
}
fn metadata(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    key: &str,
) -> io::Result<Option<Metadata>> {
    get(db, tx, META, key)?
        .map(|value| {
            let meta: Metadata = serde_json::from_value(value)
                .map_err(|_| corrupt("invalid retained artifact metadata"))?;
            receipt::validate(&meta.receipt)
                .map_err(|_| corrupt("invalid stored artifact receipt"))?;
            token(&meta.upload_id).map_err(|_| corrupt("invalid stored upload witness"))?;
            if meta.next_sequence > total_parts(&meta.receipt)?
                || meta.gc_sequence.unwrap_or(0) > meta.next_sequence
                || meta.readers.keys().any(|t| token(t).is_err())
            {
                return Err(corrupt("invalid retained artifact progress or readers"));
            }
            Ok(meta)
        })
        .transpose()
}

/// Reserved bytes the live budget accounts for one retained artifact, derived
/// from the stored receipt exactly as `prepare` accounted it. The migration
/// adapter cross-validates every restored reservation against this value.
pub(crate) fn migration_reserved_bytes(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    key: &str,
) -> io::Result<Option<u64>> {
    let Some(meta) = metadata(db, tx, key)? else {
        return Ok(None);
    };
    let reserved =
        receipt::validate(&meta.receipt).map_err(|_| corrupt("invalid stored artifact receipt"))?;
    Ok(Some(reserved as u64))
}
fn upload(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    witness: &UploadWitness,
    now: u64,
) -> io::Result<(String, Metadata)> {
    let key = witness.identity.key(tx);
    let meta = metadata(db, tx, &key)?.ok_or_else(missing)?;
    if meta.upload_id != witness.upload_id {
        return Err(conflict("paper artifact upload witness differs"));
    }
    if matches!(meta.state, State::Reclaiming)
        || (matches!(meta.state, State::Uploading | State::Sealed) && meta.expires_at_ms <= now)
    {
        return Err(conflict("paper artifact upload expired"));
    }
    Ok((key, meta))
}
fn artifacts(value: &Value) -> io::Result<&Vec<Value>> {
    value
        .get("artifacts")
        .and_then(Value::as_array)
        .ok_or_else(|| corrupt("invalid artifact list"))
}
fn size(value: &Value) -> io::Result<u64> {
    receipt::unsigned(&value["size_bytes"]).ok_or_else(|| corrupt("invalid artifact size"))
}
fn total_parts(value: &Value) -> io::Result<u64> {
    artifacts(value)?.iter().try_fold(0, |sum, artifact| {
        Ok(sum + size(artifact)?.div_ceil(limits::RETAINED_CHUNK_BYTES as u64))
    })
}
fn part_size(value: &Value, mut sequence: u64) -> io::Result<Option<usize>> {
    for artifact in artifacts(value)? {
        let size = size(artifact)?;
        let parts = size.div_ceil(limits::RETAINED_CHUNK_BYTES as u64);
        if sequence < parts {
            return Ok(Some(
                (size - sequence * limits::RETAINED_CHUNK_BYTES as u64)
                    .min(limits::RETAINED_CHUNK_BYTES as u64) as usize,
            ));
        }
        sequence -= parts;
    }
    Ok(None)
}
fn chunk_bytes(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    key: &str,
    sequence: u64,
) -> io::Result<Vec<u8>> {
    let value = get(db, tx, CHUNK, &format!("{key}:{sequence}"))?
        .ok_or_else(|| corrupt("missing artifact chunk"))?;
    let data = value["data"]
        .as_str()
        .ok_or_else(|| corrupt("invalid artifact chunk"))?;
    let bytes = decode_chunk(data).map_err(|_| corrupt("invalid artifact chunk encoding"))?;
    if value["sha256"] != format!("{:x}", Sha256::digest(&bytes)) {
        return Err(corrupt("artifact chunk digest differs"));
    }
    Ok(bytes)
}

pub(crate) fn budget_key(tx: &AuthorityTransaction, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        limits::BUDGET_NAMESPACE,
        raw,
    )
}
pub(crate) fn reservation_key(tx: &AuthorityTransaction, key: &str) -> io::Result<EntityKey> {
    budget_key(tx, format!("r:{key}").as_bytes())
}
pub(crate) fn usage(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
) -> io::Result<(u64, u64)> {
    let raw = db.entity_get(tx, &budget_key(tx, b"capacity")?)?;
    match raw {
        None => Ok((0, 0)),
        Some(raw) if raw.len() == 16 => {
            let count = u64::from_be_bytes(raw[..8].try_into().unwrap());
            let bytes = u64::from_be_bytes(raw[8..].try_into().unwrap());
            Ok((count, bytes))
        }
        _ => Err(corrupt("invalid artifact budget ledger")),
    }
}
fn set_usage(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    count: u64,
    bytes: u64,
) -> io::Result<()> {
    let mut raw = Vec::with_capacity(16);
    raw.extend_from_slice(&count.to_be_bytes());
    raw.extend_from_slice(&bytes.to_be_bytes());
    db.entity_put(tx, budget_key(tx, b"capacity")?, raw)
}

fn prepare(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    witness: &UploadWitness,
    value: &Value,
    now: u64,
) -> io::Result<Value> {
    let reserved = receipt::validate(value)? as u64;
    if receipt::unsigned(&value["owner_user_id"]) != Some(tx.owner_user_id())
        || value["document_id"] != witness.identity.document_id
        || value["build_id"] != witness.identity.build_id
    {
        return Err(invalid(
            "retained receipt does not match the successful build",
        ));
    }
    let key = witness.identity.key(tx);
    if metadata(db, tx, &key)?.is_some() {
        let (_, existing) = upload(db, tx, witness, now)?;
        if !receipt::equal(&existing.receipt, value) {
            return Err(conflict("an immutable paper build cannot be replaced"));
        }
        return Ok(json!({"next_sequence":existing.next_sequence,"state":existing.state}));
    }
    let (count, bytes) = usage(db, tx)?;
    if db.entity_get(tx, &reservation_key(tx, &key)?)?.is_some() {
        return Err(corrupt("artifact reservation lacks metadata"));
    }
    set_usage(db, tx,
        count.checked_add(1).ok_or_else(|| corrupt("artifact count overflow"))?,
        bytes.checked_add(reserved).ok_or_else(|| corrupt("artifact bytes overflow"))?)?;
    db.entity_put(
        tx,
        reservation_key(tx, &key)?,
        reserved.to_be_bytes().to_vec(),
    )?;
    put(
        db,
        tx,
        META,
        &key,
        &Metadata {
            receipt: value.clone(),
            upload_id: witness.upload_id.clone(),
            state: State::Uploading,
            next_sequence: 0,
            readers: BTreeMap::new(),
            expires_at_ms: now + limits::RETAINED_UPLOAD_SECONDS as u64 * 1000,
            gc_sequence: None,
        },
        now,
    )?;
    Ok(json!({"next_sequence":0,"state":"uploading"}))
}
fn append(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    witness: &UploadWitness,
    sequence: u64,
    data: &ValidatedChunk,
    now: u64,
) -> io::Result<Value> {
    let (key, mut meta) = upload(db, tx, witness, now)?;
    if part_size(&meta.receipt, sequence)? != Some(data.decoded_bytes) {
        return Err(invalid(
            "paper artifact chunk has invalid position or length",
        ));
    }
    let chunk = ChunkDocument {
        data: &data.data,
        sha256: &data.sha256,
    };
    let chunk_key = format!("{key}:{sequence}");
    if sequence < meta.next_sequence {
        let stored = get(db, tx, CHUNK, &chunk_key)?;
        if !stored
            .as_ref()
            .and_then(Value::as_object)
            .is_some_and(|stored| {
                stored.len() == 2
                    && stored.get("data").and_then(Value::as_str) == Some(chunk.data)
                    && stored.get("sha256").and_then(Value::as_str) == Some(chunk.sha256)
            })
        {
            return Err(conflict("paper artifact chunk retry differs"));
        }
        return Ok(json!({"next_sequence":meta.next_sequence}));
    }
    if sequence != meta.next_sequence || !matches!(meta.state, State::Uploading) {
        return Err(conflict("paper artifact chunks must arrive in sequence"));
    }
    put(db, tx, CHUNK, &chunk_key, &chunk, now)?;
    meta.next_sequence += 1;
    put(db, tx, META, &key, &meta, now)?;
    Ok(json!({"next_sequence":meta.next_sequence}))
}
fn seal(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    witness: &UploadWitness,
    now: u64,
) -> io::Result<Value> {
    let (key, mut meta) = upload(db, tx, witness, now)?;
    if !matches!(meta.state, State::Uploading) {
        return Ok(json!({"sealed":true}));
    }
    if meta.next_sequence != total_parts(&meta.receipt)? {
        return Err(conflict("paper artifact upload is incomplete"));
    }
    let mut sequence = 0;
    for artifact in artifacts(&meta.receipt)? {
        let mut remaining = size(artifact)?;
        let mut digest = Sha256::new();
        while remaining > 0 {
            let bytes = chunk_bytes(db, tx, &key, sequence)?;
            let expected = remaining.min(limits::RETAINED_CHUNK_BYTES as u64);
            if bytes.len() as u64 != expected {
                return Err(corrupt("retained artifact size changed"));
            }
            digest.update(&bytes);
            remaining -= expected;
            sequence += 1;
        }
        if artifact["sha256"] != format!("{:x}", digest.finalize()) {
            return Err(corrupt("retained artifact digest differs from receipt"));
        }
    }
    meta.state = State::Sealed;
    put(db, tx, META, &key, &meta, now)?;
    Ok(json!({"sealed":true}))
}

pub(crate) fn retain_research_build(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    reference: &Value,
    proof: &UploadWitness,
    now: u64,
) -> io::Result<()> {
    validate_clock(now)?;
    proof.validate()?;
    if !reference.is_object() {
        return Err(invalid("a retained build requires its upload witness"));
    }
    let (key, meta) = upload(db, tx, proof, now)?;
    let pdf = artifacts(&meta.receipt)?
        .iter()
        .find(|artifact| artifact["name"] == "document.pdf")
        .ok_or_else(|| conflict("retained PDF is missing"))?;
    if !matches!(meta.state, State::Sealed | State::Pinned)
        || meta.receipt["version_verified"] != true
        || reference["document_id"] != proof.identity.document_id
        || reference["build_id"] != proof.identity.build_id
        || reference["manifest_digest"] != meta.receipt["source_digest"]
        || reference["pdf_sha256"] != pdf["sha256"]
    {
        return Err(conflict(
            "research reference differs from its retained verified build",
        ));
    }
    pin_build(db, tx, proof, key, meta, now).map(|_| ())
}

/// Chat publication shares the exact pin/retirement transition with Research.
/// An unverified candidate cannot replace a previously verified manuscript.
pub(crate) fn retain_document_build(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    proof: &UploadWitness,
    now: u64,
) -> io::Result<bool> {
    validate_clock(now)?;
    proof.validate()?;
    let (key, meta) = upload(db, tx, proof, now)?;
    if !matches!(meta.state, State::Sealed | State::Pinned) {
        return Err(conflict("only a sealed paper build can be retained"));
    }
    pin_build(db, tx, proof, key, meta, now)
}

pub(crate) fn read_document_receipt(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    document_id: &str,
    build_id: &str,
) -> io::Result<Value> {
    let identity = Identity {
        document_id: document_id.into(),
        build_id: build_id.into(),
    };
    identity.validate()?;
    let meta = metadata(db, tx, &identity.key(tx))?.ok_or_else(missing)?;
    if !matches!(meta.state, State::Pinned | State::Retired) {
        return Err(missing());
    }
    Ok(meta.receipt)
}

pub(crate) fn latest_document_receipt(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    document_id: &str,
) -> io::Result<Value> {
    match get(
        db,
        tx,
        DOCUMENT,
        &format!("{}:{document_id}", tx.owner_user_id()),
    )? {
        Some(pin) => read_document_receipt(
            db,
            tx,
            document_id,
            pin["build_id"]
                .as_str()
                .ok_or_else(|| corrupt("invalid build pin"))?,
        ),
        None => Ok(Value::Null),
    }
}

fn pin_build(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    proof: &UploadWitness,
    key: String,
    mut meta: Metadata,
    now: u64,
) -> io::Result<bool> {
    let document_key = format!("{}:{}", tx.owner_user_id(), proof.identity.document_id);
    if let Some(prior) = get(db, tx, DOCUMENT, &document_key)? {
        let prior_build = prior["build_id"]
            .as_str()
            .filter(|s| lower_hex(s, 32, 32))
            .ok_or_else(|| corrupt("invalid artifact document pin"))?;
        if prior_build != proof.identity.build_id {
            let prior_key = format!("{document_key}:{prior_build}");
            let mut previous = metadata(db, tx, &prior_key)?.ok_or_else(missing)?;
            if previous.receipt["version_verified"] == true
                && meta.receipt["version_verified"] != true
            {
                return Ok(false);
            }
            previous.state = State::Retired;
            previous.expires_at_ms = now + limits::VIEWER_LEASE_SECONDS as u64 * 1000;
            put(db, tx, META, &prior_key, &previous, now)?;
        }
    }
    meta.state = State::Pinned;
    meta.expires_at_ms = 0;
    put(db, tx, META, &key, &meta, now)?;
    put(
        db,
        tx,
        DOCUMENT,
        &document_key,
        &json!({"build_id":proof.identity.build_id}),
        now,
    )?;
    Ok(true)
}

fn lease(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    identity: &Identity,
    lease_id: &str,
    operation: &LeaseOperation,
    now: u64,
) -> io::Result<Value> {
    let key = identity.key(tx);
    let Some(mut meta) = metadata(db, tx, &key)? else {
        return if matches!(operation, LeaseOperation::Release) {
            Ok(json!({"retained":false}))
        } else {
            Err(missing())
        };
    };
    meta.readers.retain(|_, expiry| *expiry > now);
    if matches!(operation, LeaseOperation::Release) {
        meta.readers.remove(lease_id);
    } else {
        let existing = meta.readers.contains_key(lease_id);
        if !matches!(meta.state, State::Pinned | State::Retired)
            || (matches!(meta.state, State::Retired)
                && meta.expires_at_ms <= now
                && meta.readers.is_empty())
            || (matches!(operation, LeaseOperation::Renew) && !existing)
        {
            return Err(missing());
        }
        meta.readers.insert(
            lease_id.to_owned(),
            now + limits::VIEWER_LEASE_SECONDS as u64 * 1000,
        );
    }
    put(db, tx, META, &key, &meta, now)?;
    Ok(json!({"retained":meta.readers.contains_key(lease_id),"receipt":meta.receipt}))
}
fn prune(db: &AuthorityDatabase, tx: &mut AuthorityTransaction, now: u64) -> io::Result<Value> {
    let prefix = format!("r:{}:", tx.owner_user_id());
    let cursor_key = budget_key(tx, format!("gc:{}", tx.owner_user_id()).as_bytes())?;
    let saved_cursor = db.entity_get(tx, &cursor_key)?;
    let cursor = saved_cursor.as_deref().unwrap_or(prefix.as_bytes());
    if !cursor.starts_with(prefix.as_bytes()) {
        return Err(corrupt("invalid owner reclamation cursor"));
    }
    let start = budget_key(tx, cursor)?;
    let end = budget_key(tx, format!("r:{};", tx.owner_user_id()).as_bytes())?;
    // Owner prefix is applied before the bounded range; other tenants/users
    // cannot consume this call's scan budget or conceal reclaimable records.
    let rows = db.entity_scan(tx, &start, &end, limits::RETAINED_GC_SCAN_ROWS)?;
    let full_page = rows.len() == limits::RETAINED_GC_SCAN_ROWS;
    let mut next_cursor = None;
    for (reservation, raw) in rows {
        let raw_key = reservation.key_bytes().to_vec();
        let mut successor = raw_key.clone();
        successor.push(0);
        next_cursor = Some(successor);
        let key = std::str::from_utf8(reservation.key_bytes())
            .map_err(|_| corrupt("invalid reservation key"))?
            .strip_prefix("r:")
            .ok_or_else(|| corrupt("invalid reservation prefix"))?;
        let mut meta = metadata(db, tx, key)?.ok_or_else(missing)?;
        if matches!(meta.state, State::Pinned)
            || meta.expires_at_ms > now
            || meta.readers.values().any(|expiry| *expiry > now)
        {
            continue;
        }
        meta.state = State::Reclaiming;
        let begin = meta.gc_sequence.unwrap_or(0);
        let end = meta
            .next_sequence
            .min(begin + limits::RETAINED_GC_CHUNKS as u64);
        for sequence in begin..end {
            delete(db, tx, CHUNK, &format!("{key}:{sequence}"))?;
        }
        if end == meta.next_sequence {
            let reserved = u64::from_be_bytes(
                raw.as_slice()
                    .try_into()
                    .map_err(|_| corrupt("invalid reservation bytes"))?,
            );
            let (count, bytes) = usage(db, tx)?;
            let next_count = count
                .checked_sub(1)
                .ok_or_else(|| corrupt("artifact count underflow"))?;
            let next_bytes = bytes
                .checked_sub(reserved)
                .ok_or_else(|| corrupt("artifact bytes underflow"))?;
            set_usage(db, tx, next_count, next_bytes)?;
            delete(db, tx, META, key)?;
            db.entity_delete(tx, reservation)?;
        } else {
            meta.gc_sequence = Some(end);
            put(db, tx, META, key, &meta, now)?;
            // Continue this same artifact until every chunk is reclaimed.
            next_cursor = Some(raw_key);
        }
        db.entity_put(tx, cursor_key, next_cursor.expect("selected row cursor"))?;
        return Ok(json!({"progress":true,"chunks_deleted":end-begin}));
    }
    if full_page {
        db.entity_put(tx, cursor_key, next_cursor.expect("full reclamation page"))?;
    } else {
        db.entity_delete(tx, cursor_key)?;
    }
    Ok(json!({"progress":full_page,"chunks_deleted":0}))
}

pub fn execute(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    request.validate()?;
    let now = request.now_unix_ms;
    let value = match &request.operation {
        Operation::Prepare { upload, receipt } => prepare(db, tx, upload, receipt, now)?,
        Operation::Append {
            upload,
            sequence,
            data,
        } => append(db, tx, upload, *sequence, data, now)?,
        Operation::Seal { upload } => seal(db, tx, upload, now)?,
        Operation::Discard { upload } => {
            let key = upload.identity.key(tx);
            if let Some(mut meta) = metadata(db, tx, &key)? {
                if meta.upload_id == upload.upload_id
                    && matches!(meta.state, State::Uploading | State::Sealed)
                {
                    meta.expires_at_ms = 0;
                    put(db, tx, META, &key, &meta, now)?;
                }
            }
            json!({"discarded":true})
        }
        Operation::Lease {
            identity,
            lease_id,
            operation,
        } => lease(db, tx, identity, lease_id, operation, now)?,
        Operation::Read {
            identity,
            lease_id,
            sequence,
        } => {
            let key = identity.key(tx);
            let meta = metadata(db, tx, &key)?.ok_or_else(missing)?;
            if !meta
                .readers
                .get(lease_id)
                .is_some_and(|expiry| *expiry > now)
            {
                return Err(missing());
            }
            if *sequence >= meta.next_sequence {
                return Err(invalid("retained artifact sequence is out of bounds"));
            }
            json!({"data":general_purpose::STANDARD.encode(chunk_bytes(db, tx, &key, *sequence)?)})
        }
        Operation::Latest { document_id } => {
            let Some(value) = get(
                db,
                tx,
                DOCUMENT,
                &format!("{}:{document_id}", tx.owner_user_id()),
            )?
            else {
                return Ok(None);
            };
            value
        }
        Operation::Prune => prune(db, tx, now)?,
    };
    Ok(Some(encode_bounded(&value, MAX_DOCUMENT_BYTES)?))
}
