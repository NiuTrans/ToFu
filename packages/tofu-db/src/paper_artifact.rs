//! Bounded owner-scoped paper report and translation authority.
//!
//! Report bodies and mutable metadata are separate blob-capable documents so
//! second-pass accounting never rewrites multi-megabyte report text. Compact
//! chronological indexes serve latest reads; all other access is direct by
//! the length-prefixed logical identity.

use std::collections::{BTreeMap, BTreeSet};
use std::io;

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_PAPER_REPORT_DOCUMENT_BYTES, MAX_PAPER_REPORT_EXCERPT_HASHES,
    MAX_PAPER_REPORT_EXCERPT_TEXT_CHARACTERS, MAX_PAPER_REPORT_META_BYTES,
    MAX_PAPER_REPORT_REOPEN_SIBLINGS, MAX_PAPER_REPORT_RESPONSE_BYTES,
    MAX_PAPER_REPORT_ROWS_PER_OWNER, MAX_PAPER_TRANSLATION_DOCUMENT_BYTES,
    MAX_PAPER_TRANSLATION_RESPONSE_BYTES, MAX_PAPER_TRANSLATION_ROWS_PER_OWNER,
    PAPER_REPORT_CORE_NAMESPACE, PAPER_REPORT_COUNT_NAMESPACE, PAPER_REPORT_LATEST_INDEX_NAMESPACE,
    PAPER_REPORT_META_NAMESPACE, PAPER_REPORT_PRESENCE_NAMESPACE,
    PAPER_TRANSLATION_COUNT_NAMESPACE, PAPER_TRANSLATION_DOCUMENT_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

const REPORT_CORE_IDENTITY: &str = "paper_report_core";
const REPORT_META_IDENTITY: &str = "paper_report_meta";
const TRANSLATION_IDENTITY: &str = "paper_translation";
const COUNT_KEY: &[u8] = b"count";
const TOKEN_KEYS: [&str; 5] = [
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
];

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn exhausted(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::OutOfMemory, message)
}
fn payload_too_large(message: &str) -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidInput,
        format!("storage_payload_too_large:{message}"),
    )
}

fn validate_text(value: &str, maximum: usize, required: bool) -> io::Result<()> {
    if (required && value.is_empty()) || value.chars().count() > maximum {
        return Err(invalid_input("invalid paper artifact text field"));
    }
    Ok(())
}

fn owner_key(tx: &AuthorityTransaction, namespace: &str, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(tx.tenant_id(), tx.owner_user_id(), namespace, raw)
}

fn push_text(raw: &mut Vec<u8>, value: &str) -> io::Result<()> {
    raw.extend_from_slice(
        &u16::try_from(value.len())
            .map_err(|_| invalid_input("paper artifact identity is too long"))?
            .to_be_bytes(),
    );
    raw.extend_from_slice(value.as_bytes());
    Ok(())
}

fn identity(paper_hash: &str, lang: &str) -> io::Result<Vec<u8>> {
    let mut raw = Vec::with_capacity(paper_hash.len() + lang.len() + 4);
    push_text(&mut raw, paper_hash)?;
    push_text(&mut raw, lang)?;
    Ok(raw)
}

fn report_key(
    tx: &AuthorityTransaction,
    namespace: &str,
    paper_hash: &str,
    lang: &str,
) -> io::Result<EntityKey> {
    owner_key(tx, namespace, &identity(paper_hash, lang)?)
}

fn translation_key(
    tx: &AuthorityTransaction,
    paper_hash: &str,
    lang: &str,
) -> io::Result<EntityKey> {
    report_key(tx, PAPER_TRANSLATION_DOCUMENT_NAMESPACE, paper_hash, lang)
}

fn latest_prefix(paper_hash: &str) -> io::Result<Vec<u8>> {
    let mut raw = Vec::new();
    push_text(&mut raw, paper_hash)?;
    Ok(raw)
}

fn latest_key(
    tx: &AuthorityTransaction,
    paper_hash: &str,
    created_at: u64,
    lang: &str,
) -> io::Result<EntityKey> {
    let mut raw = latest_prefix(paper_hash)?;
    raw.extend_from_slice(&(!created_at).to_be_bytes());
    raw.extend_from_slice(lang.as_bytes());
    owner_key(tx, PAPER_REPORT_LATEST_INDEX_NAMESPACE, &raw)
}

fn count_key(tx: &AuthorityTransaction, namespace: &str) -> io::Result<EntityKey> {
    owner_key(tx, namespace, COUNT_KEY)
}

fn read_count(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    namespace: &str,
    maximum: usize,
) -> io::Result<usize> {
    let Some(raw) = db.entity_get(tx, &count_key(tx, namespace)?)? else {
        return Ok(0);
    };
    let bytes: [u8; 8] = raw
        .try_into()
        .map_err(|_| invalid_data("paper artifact count is malformed"))?;
    let count = usize::try_from(u64::from_le_bytes(bytes))
        .map_err(|_| invalid_data("paper artifact count overflows"))?;
    if count > maximum {
        return Err(invalid_data("paper artifact count exceeds its bound"));
    }
    Ok(count)
}

fn write_count(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    namespace: &str,
    count: usize,
    maximum: usize,
) -> io::Result<()> {
    if count > maximum {
        return Err(exhausted("paper artifact owner capacity is exhausted"));
    }
    db.entity_put(
        tx,
        count_key(tx, namespace)?,
        u64::try_from(count)
            .map_err(|_| invalid_data("paper artifact count overflows"))?
            .to_le_bytes()
            .to_vec(),
    )
}

#[derive(Clone, Debug)]
pub struct ReportPut {
    pub paper_hash: String,
    pub lang: String,
    pub report: String,
    pub model: String,
    pub meta: Map<String, Value>,
    pub created_at: u64,
    pub physical_updated_at_ms: u64,
}

#[derive(Clone, Debug)]
pub struct TranslationPut {
    pub paper_hash: String,
    pub lang: String,
    pub text: String,
    pub model: String,
    pub created_at: u64,
    pub physical_updated_at_ms: u64,
}

#[derive(Clone, Debug)]
pub enum Request {
    ReportUpsert(Box<ReportPut>),
    ReportGet {
        paper_hash: String,
        lang: String,
        max_report_characters: Option<usize>,
    },
    ReportResolve {
        paper_hash: String,
        preferred_lang: String,
        fallback_lang: Option<String>,
    },
    ReportReopen {
        paper_hash: String,
        preferred_lang: String,
        fallback_lang: Option<String>,
        sibling_langs_by_base: BTreeMap<String, Vec<String>>,
    },
    ReportExcerpts {
        paper_hashes: Vec<String>,
        lang: String,
        max_report_characters: usize,
    },
    ReportLatest {
        paper_hash: String,
    },
    ReportSecondPassMerge {
        paper_hash: String,
        lang: String,
        name: String,
        entry: Map<String, Value>,
        physical_updated_at_ms: u64,
    },
    ReportSecondPassAccumulate {
        paper_hash: String,
        lang: String,
        name: String,
        usage: BTreeMap<String, u64>,
        cost_cny: f64,
        cost_usd: f64,
        physical_updated_at_ms: u64,
    },
    TranslationUpsert(Box<TranslationPut>),
    TranslationGet {
        paper_hash: String,
        lang: String,
    },
}

impl Request {
    pub fn mutates_state(&self) -> bool {
        matches!(
            self,
            Self::ReportUpsert(_)
                | Self::ReportSecondPassMerge { .. }
                | Self::ReportSecondPassAccumulate { .. }
                | Self::TranslationUpsert(_)
        )
    }

    pub fn validate(&self) -> io::Result<usize> {
        match self {
            Self::ReportUpsert(value) => {
                validate_report_scope(&value.paper_hash, &value.lang)?;
                validate_text(&value.report, 10_000_000, false)?;
                validate_text(&value.model, 512, false)?;
                if value.physical_updated_at_ms == 0 {
                    return Err(invalid_input("invalid paper report timestamp"));
                }
                let meta_bytes = serde_json::to_vec(&value.meta)
                    .map_err(|_| invalid_input("paper report metadata cannot be encoded"))?
                    .len();
                if meta_bytes > MAX_PAPER_REPORT_META_BYTES {
                    return Err(exhausted("paper report metadata exceeds its bound"));
                }
                Ok(value.paper_hash.len() + value.lang.len() + value.model.len() + meta_bytes)
            }
            Self::ReportGet {
                paper_hash,
                lang,
                max_report_characters,
            } => {
                validate_report_scope(paper_hash, lang)?;
                if max_report_characters.is_some_and(|value| {
                    !(1..=MAX_PAPER_REPORT_EXCERPT_TEXT_CHARACTERS).contains(&value)
                }) {
                    return Err(invalid_input("invalid report projection bound"));
                }
                Ok(paper_hash.len() + lang.len())
            }
            Self::ReportResolve {
                paper_hash,
                preferred_lang,
                fallback_lang,
            } => {
                validate_report_scope(paper_hash, preferred_lang)?;
                if let Some(lang) = fallback_lang {
                    validate_text(lang, 64, true)?;
                }
                Ok(paper_hash.len()
                    + preferred_lang.len()
                    + fallback_lang.as_ref().map_or(0, String::len))
            }
            Self::ReportReopen {
                paper_hash,
                preferred_lang,
                fallback_lang,
                sibling_langs_by_base,
            } => {
                validate_report_scope(paper_hash, preferred_lang)?;
                let mut allowed = BTreeSet::from([preferred_lang.as_str()]);
                if let Some(lang) = fallback_lang {
                    validate_text(lang, 64, true)?;
                    allowed.insert(lang);
                }
                let mut count = 0usize;
                for (base, siblings) in sibling_langs_by_base {
                    if !allowed.contains(base.as_str()) {
                        return Err(invalid_input("invalid paper report sibling base"));
                    }
                    count = count
                        .checked_add(siblings.len())
                        .ok_or_else(|| invalid_input("paper report sibling count overflow"))?;
                    if count > MAX_PAPER_REPORT_REOPEN_SIBLINGS {
                        return Err(invalid_input("too many paper report siblings"));
                    }
                    let mut seen = BTreeSet::new();
                    for lang in siblings {
                        validate_text(lang, 64, true)?;
                        if lang.trim() != lang || !seen.insert(lang) {
                            return Err(invalid_input("paper report siblings are not normalized"));
                        }
                    }
                }
                Ok(paper_hash.len() + preferred_lang.len() + count * 64)
            }
            Self::ReportExcerpts {
                paper_hashes,
                lang,
                max_report_characters,
            } => {
                validate_text(lang, 64, true)?;
                if paper_hashes.len() > MAX_PAPER_REPORT_EXCERPT_HASHES
                    || !(1..=MAX_PAPER_REPORT_EXCERPT_TEXT_CHARACTERS)
                        .contains(max_report_characters)
                {
                    return Err(invalid_input("invalid paper report excerpts bound"));
                }
                let mut seen = BTreeSet::new();
                let mut bytes = lang.len();
                for hash in paper_hashes {
                    validate_text(hash, 128, true)?;
                    if hash.trim() != hash || !seen.insert(hash) {
                        return Err(invalid_input("paper report hashes are not normalized"));
                    }
                    bytes += hash.len();
                }
                Ok(bytes)
            }
            Self::ReportLatest { paper_hash } => {
                validate_text(paper_hash, 128, true)?;
                Ok(paper_hash.len())
            }
            Self::ReportSecondPassMerge {
                paper_hash,
                lang,
                name,
                entry,
                physical_updated_at_ms,
            } => {
                validate_report_scope(paper_hash, lang)?;
                validate_text(name, 64, true)?;
                if *physical_updated_at_ms == 0 {
                    return Err(invalid_input("invalid paper report timestamp"));
                }
                let bytes = serde_json::to_vec(entry)
                    .map_err(|_| invalid_input("paper pass entry cannot be encoded"))?
                    .len();
                if bytes > MAX_PAPER_REPORT_META_BYTES {
                    return Err(exhausted("paper pass entry exceeds its bound"));
                }
                Ok(paper_hash.len() + lang.len() + name.len() + bytes)
            }
            Self::ReportSecondPassAccumulate {
                paper_hash,
                lang,
                name,
                usage,
                cost_cny,
                cost_usd,
                physical_updated_at_ms,
            } => {
                validate_report_scope(paper_hash, lang)?;
                validate_text(name, 64, true)?;
                if usage.keys().any(|key| !TOKEN_KEYS.contains(&key.as_str()))
                    || usage.values().any(|value| *value > 10_000_000_000)
                    || !cost_cny.is_finite()
                    || !cost_usd.is_finite()
                    || !(0.0..=1_000_000_000.0).contains(cost_cny)
                    || !(0.0..=1_000_000_000.0).contains(cost_usd)
                    || *physical_updated_at_ms == 0
                {
                    return Err(invalid_input("invalid paper pass accumulation"));
                }
                Ok(paper_hash.len() + lang.len() + name.len())
            }
            Self::TranslationUpsert(value) => {
                validate_text(&value.paper_hash, 128, true)?;
                validate_text(&value.lang, 128, true)?;
                validate_text(&value.text, 2_000_000, false)?;
                validate_text(&value.model, 512, false)?;
                if value.text.len() > 4_000_000 {
                    return Err(payload_too_large(
                        "paper translation exceeds its UTF-8 bound",
                    ));
                }
                if value.physical_updated_at_ms == 0 {
                    return Err(invalid_input("invalid paper translation timestamp"));
                }
                Ok(value.paper_hash.len() + value.lang.len() + value.model.len())
            }
            Self::TranslationGet { paper_hash, lang } => {
                validate_text(paper_hash, 128, true)?;
                validate_text(lang, 128, true)?;
                Ok(paper_hash.len() + lang.len())
            }
        }
    }
}

fn validate_report_scope(paper_hash: &str, lang: &str) -> io::Result<()> {
    validate_text(paper_hash, 128, true)?;
    validate_text(lang, 64, true)
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct ReportCore {
    paper_hash: String,
    lang: String,
    report: String,
    model: String,
    created_at: u64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct ReportMeta {
    paper_hash: String,
    lang: String,
    meta: Map<String, Value>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct Translation {
    paper_hash: String,
    lang: String,
    text: String,
    model: String,
    created_at: u64,
}

fn load<T: for<'de> Deserialize<'de>>(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    key: &EntityKey,
    namespace: &str,
    logical_key: &[u8],
    maximum: usize,
) -> io::Result<Option<T>> {
    let logical = hex(logical_key);
    let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
        db,
        tx,
        key,
        namespace,
        &logical,
        tx.owner_user_id(),
        maximum,
    )?
    else {
        return Ok(None);
    };
    serde_json::from_slice(&raw)
        .map(Some)
        .map_err(|_| invalid_data("paper artifact document is malformed"))
}

struct DocumentWrite<'a, T> {
    key: EntityKey,
    namespace: &'a str,
    logical_key: &'a [u8],
    value: &'a T,
    updated_at_ms: u64,
    maximum: usize,
}

fn store<T: Serialize>(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: DocumentWrite<'_, T>,
) -> io::Result<()> {
    let logical = hex(request.logical_key);
    let expected = db
        .entity_get(tx, &request.key)?
        .as_deref()
        .map(|raw| versioned_document::stored_document_version(raw, request.namespace, &logical))
        .transpose()?
        .unwrap_or(0);
    let raw = serde_json::to_vec(request.value)
        .map_err(|_| invalid_data("paper artifact document cannot be encoded"))?;
    if raw.len() > request.maximum {
        return Err(exhausted("paper artifact document exceeds its byte bound"));
    }
    versioned_document::put_with_blob_owner_bounded(
        db,
        tx,
        PutRequest {
            key: request.key,
            namespace: request.namespace.to_owned(),
            logical_key: logical,
            value_json: raw,
            expected_version: Some(expected),
            updated_at_ms: request.updated_at_ms,
        },
        tx.owner_user_id(),
        request.maximum,
    )?;
    Ok(())
}

fn hex(raw: &[u8]) -> String {
    raw.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn get_report(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    paper_hash: &str,
    lang: &str,
) -> io::Result<Option<(ReportCore, ReportMeta)>> {
    let logical = identity(paper_hash, lang)?;
    let Some(core): Option<ReportCore> = load(
        db,
        tx,
        &report_key(tx, PAPER_REPORT_CORE_NAMESPACE, paper_hash, lang)?,
        REPORT_CORE_IDENTITY,
        &logical,
        MAX_PAPER_REPORT_DOCUMENT_BYTES,
    )?
    else {
        return Ok(None);
    };
    let meta: ReportMeta = load(
        db,
        tx,
        &report_key(tx, PAPER_REPORT_META_NAMESPACE, paper_hash, lang)?,
        REPORT_META_IDENTITY,
        &logical,
        MAX_PAPER_REPORT_META_BYTES,
    )?
    .ok_or_else(|| invalid_data("paper report metadata is missing"))?;
    if core.paper_hash != paper_hash
        || core.lang != lang
        || meta.paper_hash != paper_hash
        || meta.lang != lang
    {
        return Err(invalid_data("paper report identity differs"));
    }
    Ok(Some((core, meta)))
}

fn projection(core: &ReportCore, meta: &ReportMeta, max_chars: Option<usize>) -> Value {
    let bounded = max_chars.map_or_else(
        || core.report.clone(),
        |limit| core.report.chars().take(limit).collect(),
    );
    json!({"user_id": null, "paper_hash": core.paper_hash, "lang": core.lang, "report": bounded, "model": if max_chars.is_some() { "" } else { &core.model }, "meta": if max_chars.is_some() { json!({}) } else { Value::Object(meta.meta.clone()) }, "created_at": core.created_at})
}

fn with_user(mut value: Value, user: u64) -> Value {
    value["user_id"] = Value::from(user);
    value
}

fn encode(value: &Value, maximum: usize) -> io::Result<Vec<u8>> {
    let raw = serde_json::to_vec(value)
        .map_err(|_| invalid_data("paper artifact response cannot be encoded"))?;
    if raw.len() > maximum {
        return Err(exhausted("paper artifact response exceeds its bound"));
    }
    Ok(raw)
}

fn report_upsert(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    value: &ReportPut,
) -> io::Result<Vec<u8>> {
    let previous = get_report(db, tx, &value.paper_hash, &value.lang)?;
    let count = read_count(
        db,
        tx,
        PAPER_REPORT_COUNT_NAMESPACE,
        MAX_PAPER_REPORT_ROWS_PER_OWNER,
    )?;
    if previous.is_none() && count == MAX_PAPER_REPORT_ROWS_PER_OWNER {
        return Err(exhausted("paper report owner capacity is exhausted"));
    }
    if let Some((core, _)) = &previous {
        db.entity_delete(
            tx,
            latest_key(tx, &core.paper_hash, core.created_at, &core.lang)?,
        )?;
    }
    let logical = identity(&value.paper_hash, &value.lang)?;
    let core = ReportCore {
        paper_hash: value.paper_hash.clone(),
        lang: value.lang.clone(),
        report: value.report.clone(),
        model: value.model.clone(),
        created_at: value.created_at,
    };
    let meta = ReportMeta {
        paper_hash: value.paper_hash.clone(),
        lang: value.lang.clone(),
        meta: value.meta.clone(),
    };
    store(
        db,
        tx,
        DocumentWrite {
            key: report_key(
                tx,
                PAPER_REPORT_CORE_NAMESPACE,
                &value.paper_hash,
                &value.lang,
            )?,
            namespace: REPORT_CORE_IDENTITY,
            logical_key: &logical,
            value: &core,
            updated_at_ms: value.physical_updated_at_ms,
            maximum: MAX_PAPER_REPORT_DOCUMENT_BYTES,
        },
    )?;
    store(
        db,
        tx,
        DocumentWrite {
            key: report_key(
                tx,
                PAPER_REPORT_META_NAMESPACE,
                &value.paper_hash,
                &value.lang,
            )?,
            namespace: REPORT_META_IDENTITY,
            logical_key: &logical,
            value: &meta,
            updated_at_ms: value.physical_updated_at_ms,
            maximum: MAX_PAPER_REPORT_META_BYTES,
        },
    )?;
    db.entity_put(
        tx,
        latest_key(tx, &value.paper_hash, value.created_at, &value.lang)?,
        value.lang.as_bytes().to_vec(),
    )?;
    db.entity_put(
        tx,
        owner_key(
            tx,
            PAPER_REPORT_PRESENCE_NAMESPACE,
            value.paper_hash.as_bytes(),
        )?,
        vec![1],
    )?;
    if previous.is_none() {
        write_count(
            db,
            tx,
            PAPER_REPORT_COUNT_NAMESPACE,
            count + 1,
            MAX_PAPER_REPORT_ROWS_PER_OWNER,
        )?;
    }
    Ok(br#"{"saved":true}"#.to_vec())
}

fn report_latest(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    paper_hash: &str,
) -> io::Result<Option<(ReportCore, ReportMeta)>> {
    let (start, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        PAPER_REPORT_LATEST_INDEX_NAMESPACE,
        &latest_prefix(paper_hash)?,
    )?;
    let rows = db.entity_scan(tx, &start, &end, 1)?;
    let Some((index_key, raw)) = rows.first() else {
        return Ok(None);
    };
    let lang = std::str::from_utf8(raw)
        .map_err(|_| invalid_data("paper report latest index is malformed"))?;
    validate_text(lang, 64, true)
        .map_err(|_| invalid_data("paper report latest index is malformed"))?;
    let Some((core, meta)) = get_report(db, tx, paper_hash, lang)? else {
        return Err(invalid_data("paper report latest index has no report"));
    };
    let expected = latest_key(tx, paper_hash, core.created_at, &core.lang)?;
    if expected != *index_key {
        return Err(invalid_data(
            "paper report latest index does not match its report",
        ));
    }
    Ok(Some((core, meta)))
}

fn resolve(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    hash: &str,
    preferred: &str,
    fallback: Option<&str>,
) -> io::Result<Option<(ReportCore, ReportMeta)>> {
    if let Some(value) = get_report(db, tx, hash, preferred)? {
        return Ok(Some(value));
    }
    if let Some(lang) = fallback.filter(|lang| *lang != preferred) {
        return get_report(db, tx, hash, lang);
    }
    Ok(None)
}

fn integer(value: Option<&Value>) -> u64 {
    value
        .and_then(|v| match v {
            Value::Number(n) => n.as_u64(),
            _ => None,
        })
        .unwrap_or(0)
}
fn number(value: Option<&Value>) -> f64 {
    value
        .and_then(|v| match v {
            Value::Number(n) => n.as_f64(),
            _ => None,
        })
        .unwrap_or(0.0)
}
fn camel(token: &str) -> &'static str {
    match token {
        "prompt_tokens" => "promptTokens",
        "completion_tokens" => "completionTokens",
        "cache_read_tokens" => "cacheReadTokens",
        "cache_write_tokens" => "cacheWriteTokens",
        _ => "reasoningTokens",
    }
}

fn recompute_totals(meta: &mut Map<String, Value>) {
    let passes = meta.get("secondPasses").and_then(Value::as_object);
    let mut total = Map::new();
    for key in TOKEN_KEYS {
        let mut sum = integer(meta.get(camel(key)));
        if let Some(passes) = passes {
            for pass in passes.values() {
                sum = sum.saturating_add(integer(pass.get("usage").and_then(|u| u.get(key))));
            }
        }
        total.insert(key.to_owned(), Value::from(sum));
    }
    meta.insert("totalUsage".to_owned(), Value::Object(total));
    for field in ["costCny", "costUsd"] {
        let mut sum = number(meta.get(field));
        if let Some(passes) = meta.get("secondPasses").and_then(Value::as_object) {
            for pass in passes.values() {
                sum += number(pass.get(field));
            }
        }
        if sum != 0.0 {
            if let Some(value) = serde_json::Number::from_f64(sum) {
                meta.insert(format!("totalCost{}", &field[4..]), Value::Number(value));
            }
        }
    }
}

fn update_meta(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    hash: &str,
    lang: &str,
    name: &str,
    mutation: impl FnOnce(&mut Map<String, Value>),
    updated_at_ms: u64,
) -> io::Result<Vec<u8>> {
    let Some((_, mut document)) = get_report(db, tx, hash, lang)? else {
        return Ok(br#"{"found":false,"meta":null}"#.to_vec());
    };
    let passes = document
        .meta
        .entry("secondPasses")
        .or_insert_with(|| json!({}));
    if !passes.is_object() {
        *passes = json!({});
    }
    let passes = passes.as_object_mut().expect("secondPasses normalized");
    let entry = passes.entry(name.to_owned()).or_insert_with(|| json!({}));
    if !entry.is_object() {
        *entry = json!({});
    }
    mutation(entry.as_object_mut().expect("pass entry normalized"));
    recompute_totals(&mut document.meta);
    let logical = identity(hash, lang)?;
    store(
        db,
        tx,
        DocumentWrite {
            key: report_key(tx, PAPER_REPORT_META_NAMESPACE, hash, lang)?,
            namespace: REPORT_META_IDENTITY,
            logical_key: &logical,
            value: &document,
            updated_at_ms,
            maximum: MAX_PAPER_REPORT_META_BYTES,
        },
    )?;
    encode(
        &json!({"found": true, "meta": document.meta}),
        MAX_PAPER_REPORT_RESPONSE_BYTES,
    )
}

fn translation_upsert(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    value: &TranslationPut,
) -> io::Result<Vec<u8>> {
    let logical = identity(&value.paper_hash, &value.lang)?;
    let key = translation_key(tx, &value.paper_hash, &value.lang)?;
    let exists: Option<Translation> = load(
        db,
        tx,
        &key,
        TRANSLATION_IDENTITY,
        &logical,
        MAX_PAPER_TRANSLATION_DOCUMENT_BYTES,
    )?;
    let count = read_count(
        db,
        tx,
        PAPER_TRANSLATION_COUNT_NAMESPACE,
        MAX_PAPER_TRANSLATION_ROWS_PER_OWNER,
    )?;
    if exists.is_none() && count == MAX_PAPER_TRANSLATION_ROWS_PER_OWNER {
        return Err(exhausted("paper translation owner capacity is exhausted"));
    }
    let document = Translation {
        paper_hash: value.paper_hash.clone(),
        lang: value.lang.clone(),
        text: value.text.clone(),
        model: value.model.clone(),
        created_at: value.created_at,
    };
    store(
        db,
        tx,
        DocumentWrite {
            key,
            namespace: TRANSLATION_IDENTITY,
            logical_key: &logical,
            value: &document,
            updated_at_ms: value.physical_updated_at_ms,
            maximum: MAX_PAPER_TRANSLATION_DOCUMENT_BYTES,
        },
    )?;
    if exists.is_none() {
        write_count(
            db,
            tx,
            PAPER_TRANSLATION_COUNT_NAMESPACE,
            count + 1,
            MAX_PAPER_TRANSLATION_ROWS_PER_OWNER,
        )?;
    }
    Ok(br#"{"saved":true}"#.to_vec())
}

pub fn execute(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    request.validate()?;
    match request {
        Request::ReportUpsert(value) => report_upsert(db, tx, value).map(Some),
        Request::ReportGet {
            paper_hash,
            lang,
            max_report_characters,
        } => get_report(db, tx, paper_hash, lang)?
            .map(|(core, meta)| {
                encode(
                    &with_user(
                        projection(&core, &meta, *max_report_characters),
                        tx.owner_user_id(),
                    ),
                    MAX_PAPER_REPORT_RESPONSE_BYTES,
                )
            })
            .transpose(),
        Request::ReportResolve {
            paper_hash,
            preferred_lang,
            fallback_lang,
        } => resolve(db, tx, paper_hash, preferred_lang, fallback_lang.as_deref())?
            .map(|(core, meta)| {
                encode(
                    &with_user(projection(&core, &meta, None), tx.owner_user_id()),
                    MAX_PAPER_REPORT_RESPONSE_BYTES,
                )
            })
            .transpose(),
        Request::ReportReopen {
            paper_hash,
            preferred_lang,
            fallback_lang,
            sibling_langs_by_base,
        } => {
            let resolved = resolve(db, tx, paper_hash, preferred_lang, fallback_lang.as_deref())?;
            let mut siblings = Vec::new();
            if let Some((core, _)) = &resolved {
                if let Some(langs) = sibling_langs_by_base.get(&core.lang) {
                    for lang in langs {
                        if let Some((s_core, s_meta)) = get_report(db, tx, paper_hash, lang)? {
                            siblings.push(with_user(
                                projection(&s_core, &s_meta, None),
                                tx.owner_user_id(),
                            ));
                        }
                    }
                }
            }
            let report = resolved
                .map(|(core, meta)| with_user(projection(&core, &meta, None), tx.owner_user_id()))
                .unwrap_or(Value::Null);
            encode(
                &json!({"report": report, "siblings": siblings}),
                MAX_PAPER_REPORT_RESPONSE_BYTES,
            )
            .map(Some)
        }
        Request::ReportExcerpts {
            paper_hashes,
            lang,
            max_report_characters,
        } => {
            let mut rows = Vec::new();
            for hash in paper_hashes {
                if let Some((core, _)) = get_report(db, tx, hash, lang)? {
                    rows.push(json!({"user_id": tx.owner_user_id(), "paper_hash": hash, "lang": lang, "report": core.report.chars().take(*max_report_characters).collect::<String>(), "created_at": core.created_at}));
                }
            }
            rows.sort_by(|a, b| a["paper_hash"].as_str().cmp(&b["paper_hash"].as_str()));
            encode(&Value::Array(rows), MAX_PAPER_REPORT_RESPONSE_BYTES).map(Some)
        }
        Request::ReportLatest { paper_hash } => report_latest(db, tx, paper_hash)?
            .map(|(core, meta)| {
                encode(
                    &with_user(projection(&core, &meta, None), tx.owner_user_id()),
                    MAX_PAPER_REPORT_RESPONSE_BYTES,
                )
            })
            .transpose(),
        Request::ReportSecondPassMerge {
            paper_hash,
            lang,
            name,
            entry,
            physical_updated_at_ms,
        } => update_meta(
            db,
            tx,
            paper_hash,
            lang,
            name,
            |target| *target = entry.clone(),
            *physical_updated_at_ms,
        )
        .map(Some),
        Request::ReportSecondPassAccumulate {
            paper_hash,
            lang,
            name,
            usage,
            cost_cny,
            cost_usd,
            physical_updated_at_ms,
        } => update_meta(
            db,
            tx,
            paper_hash,
            lang,
            name,
            |entry| {
                let previous = entry.get("usage").and_then(Value::as_object);
                let mut next = Map::new();
                for key in TOKEN_KEYS {
                    next.insert(
                        key.to_owned(),
                        Value::from(
                            integer(previous.and_then(|v| v.get(key)))
                                + usage.get(key).copied().unwrap_or(0),
                        ),
                    );
                }
                entry.insert("usage".to_owned(), Value::Object(next));
                entry.insert(
                    "calls".to_owned(),
                    Value::from(integer(entry.get("calls")) + 1),
                );
                for (field, increment) in [("costCny", *cost_cny), ("costUsd", *cost_usd)] {
                    let total = number(entry.get(field)) + increment;
                    if total != 0.0 {
                        entry.insert(field.to_owned(), Value::from(total));
                    }
                }
            },
            *physical_updated_at_ms,
        )
        .map(Some),
        Request::TranslationUpsert(value) => translation_upsert(db, tx, value).map(Some),
        Request::TranslationGet { paper_hash, lang } => {
            let logical = identity(paper_hash, lang)?;
            let value: Option<Translation> = load(
                db,
                tx,
                &translation_key(tx, paper_hash, lang)?,
                TRANSLATION_IDENTITY,
                &logical,
                MAX_PAPER_TRANSLATION_DOCUMENT_BYTES,
            )?;
            value.map(|value| encode(&json!({"user_id": tx.owner_user_id(), "paper_hash": value.paper_hash, "lang": value.lang, "text": value.text, "model": value.model, "created_at": value.created_at}), MAX_PAPER_TRANSLATION_RESPONSE_BYTES)).transpose()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn identities_and_latest_order_are_prefix_safe() {
        assert_ne!(identity("a", "bc").unwrap(), identity("ab", "c").unwrap());
        let dir = tempfile::tempdir().unwrap();
        let db = AuthorityDatabase::initialize(dir.path()).unwrap();
        let tx = db.begin(7, 11).unwrap();
        assert!(
            latest_key(&tx, "paper", 20, "z").unwrap() < latest_key(&tx, "paper", 10, "a").unwrap()
        );
        assert!(
            latest_key(&tx, "paper", 20, "aa").unwrap()
                < latest_key(&tx, "paper", 20, "z").unwrap()
        );
    }

    #[test]
    fn translation_utf8_byte_limit_uses_the_payload_error_contract() {
        let request = Request::TranslationUpsert(Box::new(TranslationPut {
            paper_hash: "paper".to_owned(),
            lang: "zh".to_owned(),
            text: "😀".repeat(1_000_001),
            model: "translator".to_owned(),
            created_at: 1,
            physical_updated_at_ms: 1,
        }));
        let error = request.validate().unwrap_err();
        assert!(error.to_string().starts_with("storage_payload_too_large:"));
    }
}
