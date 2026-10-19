//! Saved paper identities, requested generations and reading CAS.
//! Entry points: Request::parse/validate and execute inside one authority transaction.
//! Public schema/limits are generated; generic record/blob storage owns documents,
//! and paper_build_artifact owns the coupled retained pin. No filesystem lookup,
//! resident worker, catalog scan or implicit owner is involved.

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::io::{self, Write};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_paper_build as limits;
use crate::generated_tofudb_ir::MAX_LEGACY_DOCUMENT_BYTES;
use crate::paper_build_artifact::{self as retained, UploadWitness};
use crate::paper_build_receipt::{encode_bounded, equal, invalid, lower_hex, validate_definition};
use crate::versioned_document::{self, PutRequest};

const DOCUMENT: &str = "paper.document.v1";
const SOURCE: &str = "paper.document.source.v1";
const CURRENT: &str = "paper.document.current.v1";
const MAX_RECORD_BYTES: usize = limits::DOCUMENT_MAX_JSON_BYTES;

fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, message)
}
fn corrupt() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "invalid saved paper document")
}
fn missing() -> io::Error {
    io::Error::new(io::ErrorKind::NotFound, "paper document is unavailable")
}
fn text<'a>(payload: &'a Map<String, Value>, key: &str, maximum: usize) -> io::Result<&'a str> {
    payload
        .get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty() && s.chars().count() <= maximum)
        .ok_or_else(|| invalid("invalid paper document text"))
}
fn integer(payload: &Map<String, Value>, key: &str, minimum: u64) -> io::Result<u64> {
    payload
        .get(key)
        .and_then(Value::as_u64)
        .filter(|n| *n >= minimum)
        .ok_or_else(|| invalid("invalid paper document integer"))
}
fn validate_document_id(id: &str) -> io::Result<&str> {
    if lower_hex(id, 64, 64) {
        Ok(id)
    } else {
        Err(invalid("invalid logical paper document identity"))
    }
}
fn document_id(payload: &Map<String, Value>) -> io::Result<&str> {
    validate_document_id(text(payload, "document_id", 64)?)
}

#[derive(Clone, Debug)]
pub enum Operation {
    Register,
    Resolve,
    Get,
    Current,
    Request,
    Finish,
    Abandon,
    Reading,
}
#[derive(Clone, Debug)]
pub struct Request {
    pub operation: Operation,
    pub payload: Map<String, Value>,
    pub now_unix_ms: u64,
}
impl Request {
    pub(crate) fn parse(
        name: &str,
        payload: Map<String, Value>,
        now_unix_ms: u64,
    ) -> io::Result<Self> {
        let operation = match name {
            "paper.document.register" => Operation::Register,
            "paper.document.resolve" => Operation::Resolve,
            "paper.document.get" => Operation::Get,
            "paper.document.current" => Operation::Current,
            "paper.document.request" => Operation::Request,
            "paper.document.finish" => Operation::Finish,
            "paper.document.abandon" => Operation::Abandon,
            "paper.document.reading" => Operation::Reading,
            _ => return Err(invalid("unknown paper document operation")),
        };
        let request = Self {
            operation,
            payload,
            now_unix_ms,
        };
        request.validate()?;
        Ok(request)
    }
    pub fn mutates_state(&self) -> bool {
        !matches!(
            self.operation,
            Operation::Resolve | Operation::Get | Operation::Current
        )
    }
    pub fn validate(&self) -> io::Result<usize> {
        let p = &self.payload;
        let bytes = encode_bounded(p, MAX_LEGACY_DOCUMENT_BYTES - 8)?.len() + 8;
        integer(p, "user_id", 1)?;
        if self.now_unix_ms == 0
            || self
                .now_unix_ms
                .checked_add(limits::DOCUMENT_REQUEST_SECONDS as u64 * 1000)
                .is_none()
        {
            return Err(invalid("invalid paper document clock"));
        }
        match self.operation {
            Operation::Register => validate_definition(
                p.get("source").unwrap_or(&Value::Null),
                "PaperStoredSourceV1",
            )?,
            Operation::Resolve => {
                let sources = p
                    .get("sources")
                    .and_then(Value::as_array)
                    .ok_or_else(|| invalid("paper source lookup requires an array"))?;
                for source in sources {
                    validate_definition(source, "PaperDocumentV1")?;
                }
            }
            Operation::Current => {
                text(p, "conversation_id", 160)?;
            }
            _ => {
                document_id(p)?;
                match self.operation {
                    Operation::Request => {
                        text(p, "conversation_id", 160)?;
                        text(p, "request_key", 64)?;
                    }
                    Operation::Finish => {
                        integer(p, "generation", 1)?;
                        validate_definition(
                            p.get("receipt").unwrap_or(&Value::Null),
                            "PaperBuildReceiptV1",
                        )?;
                    }
                    Operation::Abandon => {
                        integer(p, "generation", 1)?;
                    }
                    Operation::Reading => {
                        integer(p, "expected_revision", 0)?;
                        let reading = json!({"expected_revision":p.get("expected_revision"), "state":p.get("state"),
                                             "writer_id":p.get("writer_id"), "sequence":p.get("sequence")});
                        validate_definition(&reading, "PaperReadingStateRequestV1")?;
                        python_json(&p["state"], limits::READING_STATE_BYTES)?;
                    }
                    _ => (),
                }
            }
        }
        Ok(bytes)
    }
}

/// Python json.dumps(sort_keys=True, separators=(',', ':'), ensure_ascii=True).
/// The same spelling owns source keys and completed-receipt retry digests.
pub(crate) fn python_json(value: &Value, maximum: usize) -> io::Result<Vec<u8>> {
    struct Bounded {
        bytes: Vec<u8>,
        maximum: usize,
    }
    impl Write for Bounded {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            if bytes.len() > self.maximum.saturating_sub(self.bytes.len()) {
                return Err(invalid("paper JSON exceeds its bound"));
            }
            if self.bytes.capacity() < self.bytes.len() + bytes.len() {
                let target = (self.bytes.len() + bytes.len())
                    .max(self.bytes.capacity().saturating_mul(2))
                    .min(self.maximum);
                self.bytes.reserve_exact(target - self.bytes.len());
            }
            self.bytes.extend_from_slice(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    struct PythonFormatter;
    impl serde_json::ser::Formatter for PythonFormatter {
        fn write_string_fragment<W: Write + ?Sized>(
            &mut self,
            writer: &mut W,
            fragment: &str,
        ) -> io::Result<()> {
            for ch in fragment.chars() {
                if ch < '\u{7f}' {
                    let mut bytes = [0; 4];
                    writer.write_all(ch.encode_utf8(&mut bytes).as_bytes())?;
                } else {
                    let mut units = [0; 2];
                    for unit in ch.encode_utf16(&mut units) {
                        write!(writer, "\\u{unit:04x}")?;
                    }
                }
            }
            Ok(())
        }
        fn write_f64<W: Write + ?Sized>(&mut self, writer: &mut W, value: f64) -> io::Result<()> {
            let text = format!("{value:?}");
            if let Some((mantissa, exponent)) = text.split_once('e') {
                let exponent: i32 = exponent
                    .parse()
                    .map_err(|_| invalid("invalid JSON exponent"))?;
                write!(writer, "{mantissa}e{exponent:+03}")
            } else {
                writer.write_all(text.as_bytes())
            }
        }
    }
    let mut writer = Bounded {
        bytes: Vec::new(),
        maximum,
    };
    value
        .serialize(&mut serde_json::Serializer::with_formatter(
            &mut writer,
            PythonFormatter,
        ))
        .map_err(|_| invalid("paper JSON exceeds its bound"))?;
    Ok(writer.bytes)
}
fn binding(source: &Value) -> io::Result<String> {
    let names: &[&str] = if source["source"] == "local" {
        &["source", "project_root", "main_file"]
    } else if source["source"] == "research" {
        &["source", "paper_hash", "lang"]
    } else {
        &["source", "mcp_server", "project_id", "main_file"]
    };
    let value: Map<String, Value> = names
        .iter()
        .map(|name| ((*name).into(), source[name].clone()))
        .collect();
    Ok(format!(
        "{:x}",
        Sha256::digest(python_json(&Value::Object(value), MAX_RECORD_BYTES)?)
    ))
}
fn get(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    namespace: &str,
    key: &str,
) -> io::Result<Option<Value>> {
    let physical =
        versioned_document::record_key(tx.tenant_id(), tx.owner_user_id(), namespace, key)?;
    versioned_document::get_value_with_blob_owner_bounded(
        db,
        tx,
        &physical,
        namespace,
        key,
        tx.owner_user_id(),
        MAX_RECORD_BYTES,
    )?
    .map(|bytes| serde_json::from_slice(&bytes).map_err(|_| corrupt()))
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
    versioned_document::put_with_blob_owner_bounded(
        db,
        tx,
        PutRequest {
            key: versioned_document::record_key(
                tx.tenant_id(),
                tx.owner_user_id(),
                namespace,
                key,
            )?,
            namespace: namespace.into(),
            logical_key: key.into(),
            value_json: encode_bounded(value, MAX_RECORD_BYTES)?,
            expected_version: None,
            updated_at_ms: now,
        },
        tx.owner_user_id(),
        MAX_RECORD_BYTES,
    )?;
    Ok(())
}
fn account_registration(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    association: bool,
) -> io::Result<()> {
    let key = EntityKey::new(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        limits::DOCUMENT_BUDGET_NAMESPACE,
        b"capacity",
    )?;
    let mut counts = match db.entity_get(tx, &key)? {
        None => [0u64; 2],
        Some(raw) if raw.len() == 16 => [
            u64::from_be_bytes(raw[..8].try_into().unwrap()),
            u64::from_be_bytes(raw[8..].try_into().unwrap()),
        ],
        _ => return Err(corrupt()),
    };
    let index = usize::from(association);
    counts[index] = counts[index].checked_add(1).ok_or_else(corrupt)?;
    let mut raw = Vec::with_capacity(16);
    for count in counts {
        raw.extend_from_slice(&count.to_be_bytes());
    }
    db.entity_put(tx, key, raw)
}

/// Read-only view of the tenant-global document budget ledger, with the same
/// bounds validation `reserve` applies. The migration adapter restores and
/// verifies the exact ledger through this single decoding authority.
pub(crate) fn budget_usage(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
) -> io::Result<Option<(u64, u64)>> {
    let key = EntityKey::new(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        limits::DOCUMENT_BUDGET_NAMESPACE,
        b"capacity",
    )?;
    let counts = match db.entity_get(tx, &key)? {
        None => return Ok(None),
        Some(raw) if raw.len() == 16 => [
            u64::from_be_bytes(raw[..8].try_into().unwrap()),
            u64::from_be_bytes(raw[8..].try_into().unwrap()),
        ],
        _ => return Err(corrupt()),
    };
    Ok(Some((counts[0], counts[1])))
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
enum Status {
    Idle,
    Pending,
    Ready,
    Failed,
    Interrupted,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct Document {
    document_id: String,
    source: Value,
    generation: u64,
    request_key: String,
    request_expires_at_ms: u64,
    status: Status,
    reading: Option<Value>,
    reading_revision: u64,
    completed_build_id: Option<String>,
    completed_receipt_digest: Option<String>,
    completed_published: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    reading_writer_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    reading_writer_base_revision: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    reading_writer_sequence: Option<u64>,
}
fn document(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<(String, Document)> {
    validate_document_id(id)?;
    let key = format!("{}:{id}", tx.owner_user_id());
    let row: Document = serde_json::from_value(get(db, tx, DOCUMENT, &key)?.ok_or_else(missing)?)
        .map_err(|_| corrupt())?;
    if row.document_id != id || row.source["document_id"] != id {
        return Err(corrupt());
    }
    validate_definition(&row.source, "PaperStoredSourceV1").map_err(|_| corrupt())?;
    if let Some(state) = &row.reading {
        validate_definition(state, "PaperReadingStateV1").map_err(|_| corrupt())?;
    }
    Ok((key, row))
}
fn projection(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    row: Document,
    now: u64,
) -> io::Result<Value> {
    let receipt = retained::latest_document_receipt(db, tx, &row.document_id)?;
    let status = if row.status == Status::Pending && row.request_expires_at_ms <= now {
        Status::Interrupted
    } else {
        row.status
    };
    Ok(
        json!({"document_id":row.document_id,"source":row.source,"generation":row.generation,
        "status":status,"receipt":receipt,"reading":row.reading,"reading_revision":row.reading_revision}),
    )
}
pub fn execute(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    request.validate()?;
    let p = &request.payload;
    let now = request.now_unix_ms;
    if integer(p, "user_id", 1)? != tx.owner_user_id() {
        return Err(invalid("paper document owner mismatch"));
    }
    let owner = tx.owner_user_id();
    let value = match request.operation {
        Operation::Register => {
            let mut source = p["source"].clone();
            let research = source["source"] == "research";
            if research {
                source = research_source(db, tx, source)?;
            }
            let supplied = source.as_object_mut().unwrap().remove("document_id");
            let key = format!("{owner}:{}", binding(&source)?);
            if let Some(existing) = get(db, tx, SOURCE, &key)? {
                let id = existing["document_id"].as_str().ok_or_else(corrupt)?;
                if supplied.as_ref().is_some_and(|value| value != id) {
                    return Err(conflict("paper source binding differs"));
                }
                serde_json::to_value(document(db, tx, id)?.1).map_err(|_| corrupt())?
            } else {
                if supplied.is_some() && !research {
                    return Err(conflict("paper source binding changed"));
                }
                account_registration(db, tx, false)?;
                let id = if research {
                    supplied
                        .as_ref()
                        .and_then(Value::as_str)
                        .ok_or_else(corrupt)?
                        .to_owned()
                } else {
                    format!(
                        "{}{}",
                        uuid::Uuid::new_v4().simple(),
                        uuid::Uuid::new_v4().simple()
                    )
                };
                if get(db, tx, DOCUMENT, &format!("{owner}:{id}"))?.is_some() {
                    return Err(conflict("paper document belongs to another source"));
                }
                source["document_id"] = json!(id);
                let row = Document {
                    document_id: id.clone(),
                    source,
                    generation: 0,
                    request_key: String::new(),
                    request_expires_at_ms: 0,
                    status: if research {
                        Status::Ready
                    } else {
                        Status::Idle
                    },
                    reading: None,
                    reading_revision: 0,
                    completed_build_id: None,
                    completed_receipt_digest: None,
                    completed_published: false,
                    reading_writer_id: None,
                    reading_writer_base_revision: None,
                    reading_writer_sequence: None,
                };
                put(db, tx, DOCUMENT, &format!("{owner}:{id}"), &row, now)?;
                put(db, tx, SOURCE, &key, &json!({"document_id":id}), now)?;
                serde_json::to_value(row).map_err(|_| corrupt())?
            }
        }
        Operation::Resolve => {
            let mut result = Vec::new();
            for source in p["sources"].as_array().unwrap() {
                result.push(
                    get(db, tx, SOURCE, &format!("{owner}:{}", binding(source)?))?
                        .map_or(Value::Null, |row| row["document_id"].clone()),
                );
            }
            Value::Array(result)
        }
        Operation::Current => {
            let key = format!("{owner}:{}", text(p, "conversation_id", 160)?);
            match get(db, tx, CURRENT, &key)? {
                None => Value::Null,
                Some(pointer) => {
                    let id = pointer["document_id"].as_str().ok_or_else(corrupt)?;
                    let row = document(db, tx, id)?.1;
                    projection(db, tx, row, now)?
                }
            }
        }
        _ => {
            // Reserve the new association first, matching legacy error ordering.
            // A missing document or later fault rolls this reservation back.
            let current_key = if matches!(request.operation, Operation::Request) {
                let key = format!("{owner}:{}", text(p, "conversation_id", 160)?);
                if get(db, tx, CURRENT, &key)?.is_none() {
                    account_registration(db, tx, true)?;
                }
                Some(key)
            } else {
                None
            };
            let (key, mut row) = document(db, tx, document_id(p)?)?;
            if row.source["source"] == "research"
                && matches!(
                    request.operation,
                    Operation::Request | Operation::Finish | Operation::Abandon
                )
            {
                return Err(conflict("research workspace owns manuscript publication"));
            }
            match request.operation {
                Operation::Get => projection(db, tx, row, now)?,
                Operation::Request => {
                    let request_key = text(p, "request_key", 64)?;
                    if row.status != Status::Pending
                        || row.request_key != request_key
                        || row.request_expires_at_ms <= now
                    {
                        row.generation = row.generation.checked_add(1).ok_or_else(corrupt)?;
                        row.request_key = request_key.into();
                        row.status = Status::Pending;
                        row.request_expires_at_ms =
                            now + limits::DOCUMENT_REQUEST_SECONDS as u64 * 1000;
                        row.completed_build_id = None;
                        row.completed_receipt_digest = None;
                        row.completed_published = false;
                        put(db, tx, DOCUMENT, &key, &row, now)?;
                    }
                    put(
                        db,
                        tx,
                        CURRENT,
                        current_key.as_ref().unwrap(),
                        &json!({"document_id":row.document_id}),
                        now,
                    )?;
                    json!({"generation":row.generation,"request_key":row.request_key})
                }
                Operation::Finish => finish(db, tx, p, key, row, now)?,
                Operation::Abandon => {
                    if integer(p, "generation", 1)? == row.generation
                        && row.status == Status::Pending
                    {
                        row.status = Status::Interrupted;
                        put(db, tx, DOCUMENT, &key, &row, now)?;
                    }
                    json!({"abandoned":row.status == Status::Interrupted})
                }
                Operation::Reading => reading(db, tx, p, key, row, now)?,
                _ => unreachable!(),
            }
        }
    };
    Ok(Some(encode_bounded(
        &value,
        MAX_LEGACY_DOCUMENT_BYTES,
    )?))
}
fn research_source(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    mut source: Value,
) -> io::Result<Value> {
    let bytes = crate::research::workspace_get(
        db,
        tx,
        source["paper_hash"].as_str().ok_or_else(corrupt)?,
        source["lang"].as_str().ok_or_else(corrupt)?,
    )?
    .ok_or_else(missing)?;
    let workspace: Value = serde_json::from_slice(&bytes).map_err(|_| corrupt())?;
    let reference = &workspace["workspace"]["compilation"]["build"];
    let id = reference["document_id"].as_str().ok_or_else(missing)?;
    let build = reference["build_id"].as_str().ok_or_else(missing)?;
    if source
        .get("document_id")
        .is_some_and(|supplied| supplied != id)
    {
        return Err(conflict("research manuscript identity changed"));
    }
    let receipt = retained::read_document_receipt(db, tx, id, build)?;
    source["document_id"] = receipt["document_id"].clone();
    source["main_file"] = receipt["main_file"].clone();
    source["label"] = receipt["main_file"].clone();
    Ok(source)
}
fn finish(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    p: &Map<String, Value>,
    key: String,
    mut row: Document,
    now: u64,
) -> io::Result<Value> {
    let receipt = &p["receipt"];
    if receipt["document_id"] != row.document_id
        || !equal(&receipt["owner_user_id"], &json!(tx.owner_user_id()))
    {
        return Err(invalid("build receipt belongs to another paper document"));
    }
    let stale = json!({"current":false,"published":false});
    if integer(p, "generation", 1)? != row.generation
        || row.status == Status::Interrupted
        || row.request_expires_at_ms <= now
    {
        return Ok(stale);
    }
    let digest = format!(
        "{:x}",
        Sha256::digest(python_json(receipt, MAX_LEGACY_DOCUMENT_BYTES)?)
    );
    if let Some(completed) = &row.completed_build_id {
        if receipt["build_id"] != *completed {
            return Ok(stale);
        }
        if row.completed_receipt_digest.as_ref() != Some(&digest) {
            return Err(conflict("completed paper build receipt changed"));
        }
        return Ok(json!({"current":true,"published":row.completed_published}));
    }
    let published = if receipt["status"] == "success" {
        let proof_value = p
            .get("proof")
            .ok_or_else(|| invalid("successful build requires retained evidence"))?;
        if proof_value["document_id"] != row.document_id
            || proof_value["build_id"] != receipt["build_id"]
        {
            return Err(invalid(
                "successful build requires matching retained evidence",
            ));
        }
        let proof = UploadWitness::parse(proof_value)?;
        let published = retained::retain_document_build(db, tx, &proof, now)?;
        if published
            && !equal(
                &retained::read_document_receipt(
                    db,
                    tx,
                    &row.document_id,
                    &proof.identity.build_id,
                )?,
                receipt,
            )
        {
            return Err(conflict("retained receipt differs from completed build"));
        }
        row.status = Status::Ready;
        published
    } else {
        row.status = Status::Failed;
        false
    };
    row.completed_build_id = receipt["build_id"].as_str().map(String::from);
    row.completed_receipt_digest = Some(digest);
    row.completed_published = published;
    put(db, tx, DOCUMENT, &key, &row, now)?;
    Ok(json!({"current":true,"published":published}))
}
fn reading(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    p: &Map<String, Value>,
    key: String,
    mut row: Document,
    now: u64,
) -> io::Result<Value> {
    let expected = integer(p, "expected_revision", 0)?;
    let writer = text(p, "writer_id", 32)?;
    let sequence = crate::paper_build_receipt::unsigned(&p["sequence"])
        .ok_or_else(|| invalid("invalid reading sequence"))?;
    let state = &p["state"];
    let same_writer = row.reading_writer_id.as_deref() == Some(writer)
        && row.reading_writer_base_revision == Some(expected);
    if same_writer && sequence <= row.reading_writer_sequence.ok_or_else(corrupt)? {
        if Some(sequence) == row.reading_writer_sequence
            && !row
                .reading
                .as_ref()
                .is_some_and(|prior| equal(prior, state))
        {
            return Err(conflict("reading sequence reused with different content"));
        }
        return Ok(json!({"revision":row.reading_revision,"state":row.reading}));
    }
    if !same_writer && row.reading_revision != expected {
        return Err(conflict("reading position changed in another viewer"));
    }
    retained::read_document_receipt(
        db,
        tx,
        &row.document_id,
        state["build_id"].as_str().unwrap(),
    )?;
    row.reading = Some(state.clone());
    row.reading_revision = row.reading_revision.checked_add(1).ok_or_else(corrupt)?;
    row.reading_writer_id = Some(writer.into());
    row.reading_writer_base_revision = Some(expected);
    row.reading_writer_sequence = Some(sequence);
    put(db, tx, DOCUMENT, &key, &row, now)?;
    Ok(json!({"revision":row.reading_revision,"state":row.reading}))
}
