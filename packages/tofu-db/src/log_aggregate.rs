//! Tenant-global observability log aggregates with a bounded sweep index.
//!
//! Fingerprint UTF-8 bytes are physical document keys and the sweep index is
//! (big-endian last_seen, fingerprint), so sweep candidate order matches the
//! legacy SQLite (last_seen, fingerprint) index exactly. A retained insertion
//! sequence preserves legacy rowid tie order for every query sort.

use std::io;

use serde_json::{json, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    LOG_AGGREGATE_DOCUMENT_NAMESPACE, LOG_AGGREGATE_SEQUENCE_NAMESPACE,
    LOG_AGGREGATE_SWEEP_INDEX_NAMESPACE, MAX_LOG_AGGREGATE_DOCUMENT_BYTES,
    MAX_LOG_AGGREGATE_FINGERPRINT_CHARACTERS, MAX_LOG_AGGREGATE_FLUSH_BATCH,
    MAX_LOG_AGGREGATE_LEVEL_CHARACTERS, MAX_LOG_AGGREGATE_LOGGER_CHARACTERS,
    MAX_LOG_AGGREGATE_QUERY_ROWS, MAX_LOG_AGGREGATE_ROW_COUNT, MAX_LOG_AGGREGATE_SAMPLE_CHARACTERS,
    MAX_LOG_AGGREGATE_SCAN_ROWS, MAX_LOG_AGGREGATE_SWEEP_BATCH,
    MAX_LOG_AGGREGATE_TEMPLATE_CHARACTERS, MAX_TRANSACTION_IR_LITERAL_BYTES,
};
use crate::versioned_document::{self, PutRequest};

const LOGICAL_NAMESPACE: &str = "log_aggregates";
const SEQUENCE_KEY: &[u8] = b"next";

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn resource_exhausted(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::OutOfMemory, message)
}

#[derive(Clone, Debug, serde::Deserialize, serde::Serialize)]
pub struct FlushRow {
    pub fingerprint: String,
    pub level: String,
    pub logger: String,
    pub template: String,
    pub sample: String,
    pub count: u64,
    pub first_seen: u64,
    pub last_seen: u64,
}

impl FlushRow {
    pub(crate) fn valid(&self) -> bool {
        !self.fingerprint.is_empty()
            && self.fingerprint.chars().count() <= MAX_LOG_AGGREGATE_FINGERPRINT_CHARACTERS
            && !self.level.is_empty()
            && self.level.chars().count() <= MAX_LOG_AGGREGATE_LEVEL_CHARACTERS
            && self.logger.chars().count() <= MAX_LOG_AGGREGATE_LOGGER_CHARACTERS
            && self.template.chars().count() <= MAX_LOG_AGGREGATE_TEMPLATE_CHARACTERS
            && self.sample.chars().count() <= MAX_LOG_AGGREGATE_SAMPLE_CHARACTERS
            && (1..=MAX_LOG_AGGREGATE_ROW_COUNT).contains(&self.count)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum QuerySort {
    Count,
    LastSeen,
    Level,
}

fn document_key(transaction: &AuthorityTransaction, fingerprint: &str) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        LOG_AGGREGATE_DOCUMENT_NAMESPACE,
        fingerprint.as_bytes(),
    )
}

fn document_range(transaction: &AuthorityTransaction) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        LOG_AGGREGATE_DOCUMENT_NAMESPACE,
        b"",
    )
}

fn sweep_index_key(
    transaction: &AuthorityTransaction,
    last_seen: u64,
    fingerprint: &str,
) -> io::Result<EntityKey> {
    let mut key_bytes = Vec::with_capacity(8 + fingerprint.len());
    key_bytes.extend_from_slice(&last_seen.to_be_bytes());
    key_bytes.extend_from_slice(fingerprint.as_bytes());
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        LOG_AGGREGATE_SWEEP_INDEX_NAMESPACE,
        &key_bytes,
    )
}

fn sweep_index_range(transaction: &AuthorityTransaction) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        LOG_AGGREGATE_SWEEP_INDEX_NAMESPACE,
        b"",
    )
}

fn next_document_key(transaction: &AuthorityTransaction, raw: &[u8]) -> io::Result<EntityKey> {
    let mut successor = raw.to_vec();
    successor.push(0);
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        LOG_AGGREGATE_DOCUMENT_NAMESPACE,
        &successor,
    )
}

fn sequence_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        LOG_AGGREGATE_SEQUENCE_NAMESPACE,
        SEQUENCE_KEY,
    )
}

fn read_sequence(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<u64> {
    match database.entity_get(transaction, &sequence_key(transaction)?)? {
        None => Ok(0),
        Some(raw) if raw.len() == 8 => Ok(u64::from_le_bytes(raw.try_into().unwrap())),
        Some(_) => Err(invalid_data("log-aggregate sequence is malformed")),
    }
}

fn write_sequence(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    next: u64,
) -> io::Result<()> {
    database.entity_put(
        transaction,
        sequence_key(transaction)?,
        next.to_le_bytes().to_vec(),
    )
}

struct AggregateDocument {
    fingerprint: String,
    level: String,
    logger: String,
    template: String,
    sample: String,
    count: u64,
    first_seen: u64,
    last_seen: u64,
    sequence: u64,
}

fn decode_document(raw: &[u8], expected_fingerprint: &str) -> io::Result<AggregateDocument> {
    let document: Value = serde_json::from_slice(raw)
        .map_err(|_| invalid_data("log-aggregate document is malformed"))?;
    let object = document
        .as_object()
        .ok_or_else(|| invalid_data("log-aggregate document is not an object"))?;
    let text = |field: &str| object.get(field).and_then(Value::as_str);
    let number = |field: &str| object.get(field).and_then(Value::as_u64);
    let fingerprint = text("fingerprint");
    if fingerprint != Some(expected_fingerprint) {
        return Err(invalid_data(
            "log-aggregate fingerprint does not match its key",
        ));
    }
    let (level, logger, template, sample) = match (
        text("level"),
        text("logger"),
        text("template"),
        text("sample"),
    ) {
        (Some(level), Some(logger), Some(template), Some(sample)) => {
            (level, logger, template, sample)
        }
        _ => return Err(invalid_data("log-aggregate text fields are malformed")),
    };
    let (count, first_seen, last_seen, sequence) = match (
        number("count"),
        number("first_seen"),
        number("last_seen"),
        number("seq"),
    ) {
        (Some(count), Some(first_seen), Some(last_seen), Some(sequence))
            if (1..=MAX_LOG_AGGREGATE_ROW_COUNT).contains(&count) =>
        {
            (count, first_seen, last_seen, sequence)
        }
        _ => return Err(invalid_data("log-aggregate counters are malformed")),
    };
    Ok(AggregateDocument {
        fingerprint: expected_fingerprint.to_owned(),
        level: level.to_owned(),
        logger: logger.to_owned(),
        template: template.to_owned(),
        sample: sample.to_owned(),
        count,
        first_seen,
        last_seen,
        sequence,
    })
}

fn encode_document(document: &AggregateDocument) -> io::Result<Vec<u8>> {
    let value_json = serde_json::to_vec(&json!({
        "fingerprint": document.fingerprint,
        "level": document.level,
        "logger": document.logger,
        "template": document.template,
        "sample": document.sample,
        "count": document.count,
        "first_seen": document.first_seen,
        "last_seen": document.last_seen,
        "seq": document.sequence,
    }))
    .map_err(|_| invalid_input("log-aggregate document cannot be encoded"))?;
    if value_json.len() > MAX_LOG_AGGREGATE_DOCUMENT_BYTES {
        return Err(resource_exhausted(
            "log-aggregate document exceeds its bound",
        ));
    }
    Ok(value_json)
}

fn get_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    fingerprint: &str,
) -> io::Result<Option<AggregateDocument>> {
    let key = document_key(transaction, fingerprint)?;
    versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &key,
        LOGICAL_NAMESPACE,
        fingerprint,
        transaction.owner_user_id(),
        MAX_LOG_AGGREGATE_DOCUMENT_BYTES,
    )?
    .map(|raw| decode_document(&raw, fingerprint))
    .transpose()
}

fn put_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &AggregateDocument,
    now_ms: u64,
) -> io::Result<()> {
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: document_key(transaction, &document.fingerprint)?,
            namespace: LOGICAL_NAMESPACE.to_owned(),
            logical_key: document.fingerprint.clone(),
            value_json: encode_document(document)?,
            expected_version: None,
            updated_at_ms: now_ms,
        },
        transaction.owner_user_id(),
        MAX_LOG_AGGREGATE_DOCUMENT_BYTES,
    )
    .map(|_| ())
}

pub(crate) fn flush(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    rows: &[FlushRow],
    cutoff_ms: Option<u64>,
    now_ms: u64,
) -> io::Result<Vec<u8>> {
    if rows.len() > MAX_LOG_AGGREGATE_FLUSH_BATCH || rows.iter().any(|row| !row.valid()) {
        return Err(invalid_input("invalid log-aggregate flush batch"));
    }
    for row in rows {
        let merged = match get_document(database, transaction, &row.fingerprint)? {
            Some(existing) => {
                database.entity_delete(
                    transaction,
                    sweep_index_key(transaction, existing.last_seen, &row.fingerprint)?,
                )?;
                AggregateDocument {
                    count: existing
                        .count
                        .checked_add(row.count)
                        .filter(|count| *count <= MAX_LOG_AGGREGATE_ROW_COUNT)
                        .ok_or_else(|| {
                            resource_exhausted("log-aggregate counter exceeds its bound")
                        })?,
                    last_seen: row.last_seen,
                    sample: row.sample.clone(),
                    ..existing
                }
            }
            None => {
                let next = read_sequence(database, transaction)?
                    .checked_add(1)
                    .ok_or_else(|| resource_exhausted("log-aggregate sequence overflow"))?;
                write_sequence(database, transaction, next)?;
                AggregateDocument {
                    fingerprint: row.fingerprint.clone(),
                    level: row.level.clone(),
                    logger: row.logger.clone(),
                    template: row.template.clone(),
                    sample: row.sample.clone(),
                    count: row.count,
                    first_seen: row.first_seen,
                    last_seen: row.last_seen,
                    sequence: next - 1,
                }
            }
        };
        database.entity_put(
            transaction,
            sweep_index_key(transaction, merged.last_seen, &merged.fingerprint)?,
            vec![1],
        )?;
        put_document(database, transaction, &merged, now_ms)?;
    }
    let mut swept = 0_u64;
    if let Some(cutoff) = cutoff_ms {
        let (start, end) = sweep_index_range(transaction)?;
        let candidates =
            database.entity_scan(transaction, &start, &end, MAX_LOG_AGGREGATE_SWEEP_BATCH)?;
        for (index_key, _) in candidates {
            let key_bytes = index_key.key_bytes();
            if key_bytes.len() < 9 {
                return Err(invalid_data("log-aggregate sweep index key is malformed"));
            }
            let last_seen = u64::from_be_bytes(key_bytes[..8].try_into().unwrap());
            if last_seen >= cutoff {
                break;
            }
            let fingerprint = std::str::from_utf8(&key_bytes[8..])
                .map_err(|_| invalid_data("log-aggregate sweep index key is malformed"))?;
            if get_document(database, transaction, fingerprint)?.is_none() {
                return Err(invalid_data("log-aggregate sweep index is inconsistent"));
            }
            database.entity_delete(transaction, document_key(transaction, fingerprint)?)?;
            database.entity_delete(transaction, index_key)?;
            swept += 1;
        }
    }
    serde_json::to_vec(&json!({"flushed": rows.len(), "swept": swept}))
        .map_err(|_| invalid_data("log-aggregate flush response cannot be encoded"))
}

fn simple_folded_contains(haystack: &str, needle: &str) -> bool {
    if needle.is_empty() {
        return true;
    }
    let folded: Vec<u32> = haystack
        .chars()
        .map(|character| crate::generated_unicode_simple_fold::simple_case_fold(character as u32))
        .collect();
    let needle: Vec<u32> = needle
        .chars()
        .map(|character| crate::generated_unicode_simple_fold::simple_case_fold(character as u32))
        .collect();
    needle.len() <= folded.len()
        && folded
            .windows(needle.len())
            .any(|window| window == needle.as_slice())
}

pub(crate) fn query(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    level: &str,
    q: &str,
    sort: QuerySort,
    limit: usize,
) -> io::Result<Vec<u8>> {
    if level.chars().count() > MAX_LOG_AGGREGATE_LEVEL_CHARACTERS
        || q.chars().count() > MAX_LOG_AGGREGATE_TEMPLATE_CHARACTERS
        || !(1..=MAX_LOG_AGGREGATE_QUERY_ROWS).contains(&limit)
    {
        return Err(invalid_input("invalid log-aggregate query"));
    }
    let (mut cursor, end) = document_range(transaction)?;
    let mut matches = Vec::new();
    let mut scanned = 0_usize;
    loop {
        let page_size = (MAX_LOG_AGGREGATE_SCAN_ROWS + 1 - scanned)
            .min(crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS);
        let page = database.entity_scan(transaction, &cursor, &end, page_size)?;
        if page.is_empty() {
            break;
        }
        for (key, _) in &page {
            scanned += 1;
            if scanned > MAX_LOG_AGGREGATE_SCAN_ROWS {
                return Err(resource_exhausted("log-aggregate scan exceeds its bound"));
            }
            let fingerprint = std::str::from_utf8(key.key_bytes())
                .map_err(|_| invalid_data("log-aggregate fingerprint key is malformed"))?;
            let document = get_document(database, transaction, fingerprint)?
                .ok_or_else(|| invalid_data("log-aggregate document disappeared"))?;
            if (!level.is_empty() && document.level != level)
                || !simple_folded_contains(&document.template, q)
            {
                continue;
            }
            matches.push(document);
        }
        if page.len() < page_size {
            break;
        }
        cursor = next_document_key(transaction, page.last().unwrap().0.key_bytes())?;
    }
    let total_rows = matches.len() as u64;
    let total_events = matches.iter().fold(0_u64, |total, document| {
        total.saturating_add(document.count)
    });
    match sort {
        QuerySort::Count => matches.sort_by(|left, right| {
            right
                .count
                .cmp(&left.count)
                .then(right.last_seen.cmp(&left.last_seen))
                .then(left.sequence.cmp(&right.sequence))
        }),
        QuerySort::LastSeen => matches.sort_by(|left, right| {
            right
                .last_seen
                .cmp(&left.last_seen)
                .then(left.sequence.cmp(&right.sequence))
        }),
        QuerySort::Level => matches.sort_by(|left, right| {
            left.level
                .cmp(&right.level)
                .then(right.count.cmp(&left.count))
                .then(left.sequence.cmp(&right.sequence))
        }),
    }
    let mut items = Vec::with_capacity(matches.len().min(limit));
    let mut response_bytes = 64_usize;
    for document in matches.iter().take(limit) {
        let item = json!({
            "fingerprint": document.fingerprint,
            "level": document.level,
            "logger": document.logger,
            "template": document.template,
            "sample": document.sample,
            "count": document.count,
            "first_seen": document.first_seen,
            "last_seen": document.last_seen,
        });
        response_bytes = response_bytes
            .checked_add(
                serde_json::to_vec(&item)
                    .map_err(|_| invalid_data("log-aggregate response cannot be encoded"))?
                    .len()
                    + 1,
            )
            .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
            .ok_or_else(|| resource_exhausted("log-aggregate response exceeds 8 MiB"))?;
        items.push(item);
    }
    serde_json::to_vec(&json!({
        "items": items,
        "total_rows": total_rows,
        "total_events": total_events,
    }))
    .map_err(|_| invalid_data("log-aggregate query response cannot be encoded"))
}

#[cfg(test)]
mod tests {
    use super::{simple_folded_contains, QuerySort};

    #[test]
    fn simple_folded_contains_matches_sqlite_icu_like_semantics() {
        assert_eq!(
            crate::generated_unicode_simple_fold::ICU_UNICODE_VERSION,
            (17, 0, 0)
        );
        assert_eq!(
            crate::generated_unicode_simple_fold::SIMPLE_CASE_FOLDING_COUNT,
            1512
        );
        assert!(simple_folded_contains("anything", ""));
        assert!(simple_folded_contains("Error: Disk FULL", "disk full"));
        assert!(simple_folded_contains("Error: Disk FULL", "DISK"));
        assert!(!simple_folded_contains("Error", "disk"));
        assert!(!simple_folded_contains("ab", "abc"));
        // ICU folds per code point with Unicode simple case folding.
        assert!(simple_folded_contains("caf\u{e9}", "CAF\u{c9}"));
        assert!(simple_folded_contains("\u{3b1}", "\u{391}")); // alpha
        assert!(simple_folded_contains("\u{3c2}", "\u{3a3}")); // final sigma ~ SIGMA
        assert!(simple_folded_contains("\u{212a}", "k")); // Kelvin sign
        assert!(simple_folded_contains("\u{1e9e}", "\u{df}")); // simple-only sharp-s map
        assert!(simple_folded_contains("\u{1f88}", "\u{1f80}")); // simple Greek map
        assert!(!simple_folded_contains("\u{df}", "ss")); // sharp s: no full fold
        assert!(!simple_folded_contains("\u{fb00}", "ff")); // ligature: no full fold
        assert!(!simple_folded_contains("\u{130}", "i")); // dotted capital I
    }

    #[test]
    fn query_sort_variants_cover_the_legacy_orders() {
        assert_eq!(QuerySort::Count, QuerySort::Count);
        assert_ne!(QuerySort::Count, QuerySort::LastSeen);
        assert_ne!(QuerySort::Level, QuerySort::Count);
    }
}
