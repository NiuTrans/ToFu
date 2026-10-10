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
    MAX_ENTITY_KEY_BYTES, MAX_PAPER_REPORT_DOCUMENT_BYTES, MAX_PAPER_REPORT_EXCERPT_HASHES,
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

// ---------------------------------------------------------------------------
// Offline migration adapter. Source `paper_reports`/`paper_translations`
// rows carry no version column, so the snapshot is exactly the row fields.
// The internal physical timestamp is derived deterministically from the
// snapshot content so an identical replay encodes to an identical envelope
// and is read-only; a divergent occupant fails closed. Report bodies and
// translation text route through the blob-capable document path without
// truncation. Get reconstructs the exact source snapshot; scan enumerates
// the owner-scoped catalogs independently and cross-checks the owner count.
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ReportRestoreRequest {
    paper_hash: String,
    lang: String,
    report: String,
    model: String,
    meta: Map<String, Value>,
    created_at: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TranslationRestoreRequest {
    paper_hash: String,
    lang: String,
    text: String,
    model: String,
    created_at: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ArtifactGetRequest {
    paper_hash: String,
    lang: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ArtifactScanRequest {
    cursor: Option<String>,
}

fn artifact_conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, message)
}

fn deterministic_updated_at_ms<T: Serialize>(value: &T) -> io::Result<u64> {
    use sha2::{Digest, Sha256};
    let raw = serde_json::to_vec(value)
        .map_err(|_| invalid_data("paper artifact document cannot be encoded"))?;
    let digest = Sha256::digest(raw);
    Ok(u64::from_be_bytes(digest[..8].try_into().unwrap()) | 1)
}

fn parse_report_restore(payload: Value) -> io::Result<ReportRestoreRequest> {
    let request: ReportRestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid paper report restore request"))?;
    validate_report_scope(&request.paper_hash, &request.lang)?;
    validate_text(&request.report, 10_000_000, false)?;
    validate_text(&request.model, 512, false)?;
    if request.created_at == 0 {
        return Err(invalid_input("invalid paper report snapshot metadata"));
    }
    let meta_bytes = serde_json::to_vec(&request.meta)
        .map_err(|_| invalid_input("paper report metadata cannot be encoded"))?
        .len();
    if meta_bytes > MAX_PAPER_REPORT_META_BYTES {
        return Err(exhausted("paper report metadata exceeds its bound"));
    }
    Ok(request)
}

fn parse_translation_restore(payload: Value) -> io::Result<TranslationRestoreRequest> {
    let request: TranslationRestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid paper translation restore request"))?;
    validate_text(&request.paper_hash, 128, true)?;
    validate_text(&request.lang, 128, true)?;
    validate_text(&request.text, 2_000_000, false)?;
    validate_text(&request.model, 512, false)?;
    if request.text.len() > 4_000_000 {
        return Err(payload_too_large(
            "paper translation exceeds its UTF-8 bound",
        ));
    }
    if request.created_at == 0 {
        return Err(invalid_input("invalid paper translation snapshot metadata"));
    }
    Ok(request)
}

fn report_snapshot_value(core: &ReportCore, meta: &ReportMeta) -> Value {
    json!({
        "paper_hash": core.paper_hash,
        "lang": core.lang,
        "report": core.report,
        "model": core.model,
        "meta": Value::Object(meta.meta.clone()),
        "created_at": core.created_at,
    })
}

fn translation_snapshot_value(document: &Translation) -> Value {
    json!({
        "paper_hash": document.paper_hash,
        "lang": document.lang,
        "text": document.text,
        "model": document.model,
        "created_at": document.created_at,
    })
}

fn verify_report_restore(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    core: &ReportCore,
    meta: &ReportMeta,
) -> io::Result<()> {
    let Some((observed_core, observed_meta)) = get_report(db, tx, &core.paper_hash, &core.lang)?
    else {
        return Err(invalid_data("paper report snapshot document is missing"));
    };
    if observed_core.paper_hash != core.paper_hash
        || observed_core.lang != core.lang
        || observed_core.report != core.report
        || observed_core.model != core.model
        || observed_core.created_at != core.created_at
        || observed_meta.meta != meta.meta
    {
        return Err(invalid_data("paper report snapshot readback differs"));
    }
    let latest = db.entity_get(
        tx,
        &latest_key(tx, &core.paper_hash, core.created_at, &core.lang)?,
    )?;
    if latest.as_deref() != Some(core.lang.as_bytes()) {
        return Err(invalid_data("paper report latest index differs"));
    }
    let presence = db.entity_get(
        tx,
        &owner_key(
            tx,
            PAPER_REPORT_PRESENCE_NAMESPACE,
            core.paper_hash.as_bytes(),
        )?,
    )?;
    if presence.is_none() {
        return Err(invalid_data("paper report presence is missing"));
    }
    if read_count(
        db,
        tx,
        PAPER_REPORT_COUNT_NAMESPACE,
        MAX_PAPER_REPORT_ROWS_PER_OWNER,
    )? == 0
    {
        return Err(invalid_data("paper report count is missing"));
    }
    Ok(())
}

fn report_restore(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request = parse_report_restore(payload)?;
    let core = ReportCore {
        paper_hash: request.paper_hash.clone(),
        lang: request.lang.clone(),
        report: request.report.clone(),
        model: request.model.clone(),
        created_at: request.created_at,
    };
    let meta = ReportMeta {
        paper_hash: request.paper_hash.clone(),
        lang: request.lang.clone(),
        meta: request.meta.clone(),
    };
    if let Some((observed_core, observed_meta)) =
        get_report(db, tx, &request.paper_hash, &request.lang)?
    {
        if observed_core.report != core.report
            || observed_core.model != core.model
            || observed_core.created_at != core.created_at
            || observed_meta.meta != meta.meta
        {
            return Err(artifact_conflict("paper report snapshot differs"));
        }
        verify_report_restore(db, tx, &core, &meta)?;
        return Ok(false);
    }
    let count = read_count(
        db,
        tx,
        PAPER_REPORT_COUNT_NAMESPACE,
        MAX_PAPER_REPORT_ROWS_PER_OWNER,
    )?;
    if count == MAX_PAPER_REPORT_ROWS_PER_OWNER {
        return Err(exhausted("paper report owner capacity is exhausted"));
    }
    let logical = identity(&request.paper_hash, &request.lang)?;
    let core_updated = deterministic_updated_at_ms(&core)?;
    store(
        db,
        tx,
        DocumentWrite {
            key: report_key(
                tx,
                PAPER_REPORT_CORE_NAMESPACE,
                &request.paper_hash,
                &request.lang,
            )?,
            namespace: REPORT_CORE_IDENTITY,
            logical_key: &logical,
            value: &core,
            updated_at_ms: core_updated,
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
                &request.paper_hash,
                &request.lang,
            )?,
            namespace: REPORT_META_IDENTITY,
            logical_key: &logical,
            value: &meta,
            updated_at_ms: deterministic_updated_at_ms(&meta)?,
            maximum: MAX_PAPER_REPORT_META_BYTES,
        },
    )?;
    db.entity_put(
        tx,
        latest_key(tx, &request.paper_hash, request.created_at, &request.lang)?,
        request.lang.as_bytes().to_vec(),
    )?;
    db.entity_put(
        tx,
        owner_key(
            tx,
            PAPER_REPORT_PRESENCE_NAMESPACE,
            request.paper_hash.as_bytes(),
        )?,
        vec![1],
    )?;
    write_count(
        db,
        tx,
        PAPER_REPORT_COUNT_NAMESPACE,
        count + 1,
        MAX_PAPER_REPORT_ROWS_PER_OWNER,
    )?;
    verify_report_restore(db, tx, &core, &meta)?;
    Ok(true)
}

fn verify_translation_restore(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    document: &Translation,
) -> io::Result<()> {
    let logical = identity(&document.paper_hash, &document.lang)?;
    let observed: Option<Translation> = load(
        db,
        tx,
        &translation_key(tx, &document.paper_hash, &document.lang)?,
        TRANSLATION_IDENTITY,
        &logical,
        MAX_PAPER_TRANSLATION_DOCUMENT_BYTES,
    )?;
    let Some(observed) = observed else {
        return Err(invalid_data(
            "paper translation snapshot document is missing",
        ));
    };
    if observed.paper_hash != document.paper_hash
        || observed.lang != document.lang
        || observed.text != document.text
        || observed.model != document.model
        || observed.created_at != document.created_at
    {
        return Err(invalid_data("paper translation snapshot readback differs"));
    }
    if read_count(
        db,
        tx,
        PAPER_TRANSLATION_COUNT_NAMESPACE,
        MAX_PAPER_TRANSLATION_ROWS_PER_OWNER,
    )? == 0
    {
        return Err(invalid_data("paper translation count is missing"));
    }
    Ok(())
}

fn translation_restore(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request = parse_translation_restore(payload)?;
    let document = Translation {
        paper_hash: request.paper_hash.clone(),
        lang: request.lang.clone(),
        text: request.text.clone(),
        model: request.model.clone(),
        created_at: request.created_at,
    };
    let logical = identity(&request.paper_hash, &request.lang)?;
    let existing: Option<Translation> = load(
        db,
        tx,
        &translation_key(tx, &request.paper_hash, &request.lang)?,
        TRANSLATION_IDENTITY,
        &logical,
        MAX_PAPER_TRANSLATION_DOCUMENT_BYTES,
    )?;
    if let Some(observed) = existing {
        if observed.text != document.text
            || observed.model != document.model
            || observed.created_at != document.created_at
        {
            return Err(artifact_conflict("paper translation snapshot differs"));
        }
        verify_translation_restore(db, tx, &document)?;
        return Ok(false);
    }
    let count = read_count(
        db,
        tx,
        PAPER_TRANSLATION_COUNT_NAMESPACE,
        MAX_PAPER_TRANSLATION_ROWS_PER_OWNER,
    )?;
    if count == MAX_PAPER_TRANSLATION_ROWS_PER_OWNER {
        return Err(exhausted("paper translation owner capacity is exhausted"));
    }
    store(
        db,
        tx,
        DocumentWrite {
            key: translation_key(tx, &request.paper_hash, &request.lang)?,
            namespace: TRANSLATION_IDENTITY,
            logical_key: &logical,
            value: &document,
            updated_at_ms: deterministic_updated_at_ms(&document)?,
            maximum: MAX_PAPER_TRANSLATION_DOCUMENT_BYTES,
        },
    )?;
    write_count(
        db,
        tx,
        PAPER_TRANSLATION_COUNT_NAMESPACE,
        count + 1,
        MAX_PAPER_TRANSLATION_ROWS_PER_OWNER,
    )?;
    verify_translation_restore(db, tx, &document)?;
    Ok(true)
}

fn report_migration_get(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: ArtifactGetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid paper report get request"))?;
    validate_report_scope(&request.paper_hash, &request.lang)?;
    match get_report(db, tx, &request.paper_hash, &request.lang)? {
        None => Ok(Value::Null),
        Some((core, meta)) => Ok(report_snapshot_value(&core, &meta)),
    }
}

fn translation_migration_get(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: ArtifactGetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid paper translation get request"))?;
    validate_text(&request.paper_hash, 128, true)?;
    validate_text(&request.lang, 128, true)?;
    let logical = identity(&request.paper_hash, &request.lang)?;
    let value: Option<Translation> = load(
        db,
        tx,
        &translation_key(tx, &request.paper_hash, &request.lang)?,
        TRANSLATION_IDENTITY,
        &logical,
        MAX_PAPER_TRANSLATION_DOCUMENT_BYTES,
    )?;
    match value {
        None => Ok(Value::Null),
        Some(document) => Ok(translation_snapshot_value(&document)),
    }
}

fn artifact_scan(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
    namespace: &str,
    count_namespace: &str,
    maximum: usize,
    prefix: &str,
) -> io::Result<Value> {
    let request: ArtifactScanRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid paper artifact scan request"))?;
    let mut suffix = Vec::new();
    if let Some(cursor) = request.cursor.as_deref() {
        let encoded = cursor
            .strip_prefix(prefix)
            .ok_or_else(|| invalid_input("invalid paper artifact catalog cursor"))?;
        if encoded.len() > MAX_ENTITY_KEY_BYTES * 2 || !encoded.len().is_multiple_of(2) {
            return Err(invalid_input("invalid paper artifact catalog cursor"));
        }
        let mut raw = encoded
            .as_bytes()
            .as_chunks::<2>()
            .0
            .iter()
            .map(|pair| {
                u8::from_str_radix(
                    std::str::from_utf8(pair)
                        .map_err(|_| invalid_input("invalid paper artifact catalog cursor"))?,
                    16,
                )
                .map_err(|_| invalid_input("invalid paper artifact catalog cursor"))
            })
            .collect::<io::Result<Vec<_>>>()?;
        if raw.len() < MAX_ENTITY_KEY_BYTES {
            raw.push(0);
        } else {
            let index = raw
                .iter()
                .rposition(|b| *b != 255)
                .ok_or_else(|| invalid_input("paper artifact catalog cursor has no successor"))?;
            raw[index] += 1;
            raw.truncate(index + 1);
        }
        suffix = raw;
    }
    let start = owner_key(tx, namespace, &suffix)?;
    let (_, end) = EntityKey::prefix_range(tx.tenant_id(), tx.owner_user_id(), namespace, b"")?;
    let page = db.entity_scan(
        tx,
        &start,
        &end,
        crate::generated_tofudb_ir::MIGRATION_SESSION_MAX_PAGE_ROWS,
    )?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, _) in &page {
        let raw = physical.key_bytes();
        let (paper_hash, consumed) = read_scoped_text(raw)?;
        let (lang, _) = read_scoped_text(&raw[consumed..])?;
        rows.push(json!({"paper_hash": paper_hash, "lang": lang}));
    }
    let cursor = if page.len() == crate::generated_tofudb_ir::MIGRATION_SESSION_MAX_PAGE_ROWS {
        let encoded: String = page
            .last()
            .unwrap()
            .0
            .key_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        Some(format!("{prefix}{encoded}"))
    } else {
        None
    };
    // Count self-proof is only sound for a complete single-page catalog;
    // multi-page completeness is checked by the importer's row accounting.
    if request.cursor.is_none()
        && cursor.is_none()
        && rows.len() != read_count(db, tx, count_namespace, maximum)?
    {
        return Err(invalid_data("paper artifact count and catalog differ"));
    }
    Ok(json!({"rows": rows, "cursor": cursor}))
}

fn read_scoped_text(input: &[u8]) -> io::Result<(String, usize)> {
    if input.len() < 2 {
        return Err(invalid_data("paper artifact catalog key is truncated"));
    }
    let length = u16::from_be_bytes([input[0], input[1]]) as usize;
    if input.len() < 2 + length {
        return Err(invalid_data("paper artifact catalog key is truncated"));
    }
    let text = String::from_utf8(input[2..2 + length].to_vec())
        .map_err(|_| invalid_data("paper artifact catalog key is not UTF-8"))?;
    Ok((text, 2 + length))
}

pub(crate) fn report_migration_execute(
    database: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    let mut transaction = database.begin(tenant, owner)?;
    match operation {
        "migration.paper.report.restore" => {
            let inserted = report_restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.paper.report.get" => report_migration_get(database, &mut transaction, payload),
        "migration.paper.report.scan" => artifact_scan(
            database,
            &mut transaction,
            payload,
            PAPER_REPORT_CORE_NAMESPACE,
            PAPER_REPORT_COUNT_NAMESPACE,
            MAX_PAPER_REPORT_ROWS_PER_OWNER,
            "paper_report:",
        ),
        _ => Err(invalid_input("unknown paper report migration operation")),
    }
}

pub(crate) fn translation_migration_execute(
    database: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    let mut transaction = database.begin(tenant, owner)?;
    match operation {
        "migration.paper.translation.restore" => {
            let inserted = translation_restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.paper.translation.get" => {
            translation_migration_get(database, &mut transaction, payload)
        }
        "migration.paper.translation.scan" => artifact_scan(
            database,
            &mut transaction,
            payload,
            PAPER_TRANSLATION_DOCUMENT_NAMESPACE,
            PAPER_TRANSLATION_COUNT_NAMESPACE,
            MAX_PAPER_TRANSLATION_ROWS_PER_OWNER,
            "paper_translation:",
        ),
        _ => Err(invalid_input(
            "unknown paper translation migration operation",
        )),
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

    fn report_snapshot(hash: &str, lang: &str, body: &str) -> Value {
        json!({
            "paper_hash": hash,
            "lang": lang,
            "report": body,
            "model": "model",
            "meta": {"origin": "test"},
            "created_at": 424242,
        })
    }

    #[test]
    fn report_migration_restore_preserves_exact_state_and_is_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(dir.path()).unwrap();
        assert_eq!(
            report_migration_execute(
                &mut database,
                7,
                11,
                "migration.paper.report.restore",
                report_snapshot("paper", "en", "body"),
            )
            .unwrap()["inserted"],
            true
        );
        assert_eq!(
            report_migration_execute(
                &mut database,
                7,
                11,
                "migration.paper.report.restore",
                report_snapshot("paper", "en", "body"),
            )
            .unwrap()["inserted"],
            false
        );
        let get = report_migration_execute(
            &mut database,
            7,
            11,
            "migration.paper.report.get",
            json!({"paper_hash": "paper", "lang": "en"}),
        )
        .unwrap();
        assert_eq!(get["report"], "body");
        assert_eq!(get["created_at"], 424242);
        let mut divergent = report_snapshot("paper", "en", "changed");
        divergent["meta"] = json!({"origin": "test"});
        assert!(report_migration_execute(
            &mut database,
            7,
            11,
            "migration.paper.report.restore",
            divergent
        )
        .is_err());
    }

    #[test]
    fn report_migration_large_body_uses_blob_path_without_truncation() {
        let dir = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(dir.path()).unwrap();
        let body = "x".repeat(64 * 1024);
        report_migration_execute(
            &mut database,
            7,
            11,
            "migration.paper.report.restore",
            report_snapshot("paper", "en", &body),
        )
        .unwrap();
        let get = report_migration_execute(
            &mut database,
            7,
            11,
            "migration.paper.report.get",
            json!({"paper_hash": "paper", "lang": "en"}),
        )
        .unwrap();
        assert_eq!(get["report"], body);
    }

    #[test]
    fn translation_migration_restore_preserves_exact_state_and_is_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(dir.path()).unwrap();
        let snapshot = json!({
            "paper_hash": "paper", "lang": "zh", "text": "translated",
            "model": "translator", "created_at": 424242,
        });
        assert_eq!(
            translation_migration_execute(
                &mut database,
                7,
                11,
                "migration.paper.translation.restore",
                snapshot.clone(),
            )
            .unwrap()["inserted"],
            true
        );
        assert_eq!(
            translation_migration_execute(
                &mut database,
                7,
                11,
                "migration.paper.translation.restore",
                snapshot,
            )
            .unwrap()["inserted"],
            false
        );
        let get = translation_migration_execute(
            &mut database,
            7,
            11,
            "migration.paper.translation.get",
            json!({"paper_hash": "paper", "lang": "zh"}),
        )
        .unwrap();
        assert_eq!(get["text"], "translated");
        let mut divergent = report_snapshot("paper", "zh", "ignored");
        divergent["text"] = json!("changed");
        divergent["created_at"] = json!(424242);
        divergent.as_object_mut().unwrap().remove("report");
        divergent.as_object_mut().unwrap().remove("meta");
        assert!(translation_migration_execute(
            &mut database,
            7,
            11,
            "migration.paper.translation.restore",
            divergent
        )
        .is_err());
    }

    #[test]
    fn report_and_translation_scans_enumerate_owner_catalogs() {
        let dir = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(dir.path()).unwrap();
        report_migration_execute(
            &mut database,
            7,
            11,
            "migration.paper.report.restore",
            report_snapshot("paper", "en", "body"),
        )
        .unwrap();
        translation_migration_execute(
            &mut database,
            7,
            11,
            "migration.paper.translation.restore",
            json!({
                "paper_hash": "paper", "lang": "zh", "text": "t",
                "model": "m", "created_at": 1,
            }),
        )
        .unwrap();
        let report_page = report_migration_execute(
            &mut database,
            7,
            11,
            "migration.paper.report.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(report_page["rows"].as_array().unwrap().len(), 1);
        let translation_page = translation_migration_execute(
            &mut database,
            7,
            11,
            "migration.paper.translation.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(translation_page["rows"].as_array().unwrap().len(), 1);
        assert_eq!(
            translation_migration_execute(
                &mut database,
                7,
                12,
                "migration.paper.translation.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
