//! Live per-conversation transcript archive (`contracts/tofudb_ir_v1.json`
//! `transcriptArchives`): a compact, append-maintained projection of the
//! public legacy-message stream so bulk hydration reads a handful of page
//! documents instead of materializing every turn document (~190 µs each).
//!
//! Lifecycle (`owner-scoped-live-tail`): `append_settled` appends the
//! already-built legacy message into the tail record in the same
//! transaction; when the tail crosses the target size it seals into an
//! immutable, digest-witnessed page. Every other lane mutation —
//! compact/delete/cleanup/projection updates — invalidates the archive
//! (cheap tombstone), and readers always fence on the maintained lane
//! count: any disagreement falls back to the exact per-document lane
//! read, so an untracked mutation can never serve a wrong answer.

use std::io;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_TRANSCRIPT_ARCHIVE_DECODED_BYTES, MAX_TRANSCRIPT_ARCHIVE_MESSAGES,
    MAX_TRANSCRIPT_ARCHIVE_PAGES, TRANSCRIPT_ARCHIVE_MANIFEST_NAMESPACE,
    TRANSCRIPT_ARCHIVE_PAGE_NAMESPACE, TRANSCRIPT_ARCHIVE_TAIL_TARGET_BYTES,
};

/// Page/tail records are versioned documents; sealed pages seal at the
/// tail target but the blob path admits the same ceiling as the tail
/// target itself (oversized single messages still seal one per page).
const MAX_TRANSCRIPT_ARCHIVE_PAGE_BYTES: usize = 64 * 1024 * 1024;

const FORMAT: &str = "tofu.transcript-archive.v1";
const TAIL_SUFFIX: &str = "\0tail";

/// Read-path observability for tests and metrics.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct TranscriptArchiveStats {
    pub served: u64,
    pub fallback: u64,
    pub sealed_pages: u64,
    pub invalidations: u64,
}

static STATS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
static FALLBACK: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
static SEALED: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
static INVALIDATED: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

pub(crate) fn stats() -> TranscriptArchiveStats {
    use std::sync::atomic::Ordering::Relaxed;
    TranscriptArchiveStats {
        served: STATS.load(Relaxed),
        fallback: FALLBACK.load(Relaxed),
        sealed_pages: SEALED.load(Relaxed),
        invalidations: INVALIDATED.load(Relaxed),
    }
}

// Optional process-wide diagnostic totals. These count accepted background
// commits, not a durable prefix or per-owner coverage certificate.
static ACCEPTED_PAGES: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
static ACCEPTED_MESSAGES: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
pub(crate) fn acceptance_profile() -> serde_json::Value {
    serde_json::json!({"acceptedPages":ACCEPTED_PAGES.load(Ordering::Relaxed),
        "acceptedMessages":ACCEPTED_MESSAGES.load(Ordering::Relaxed),
        "fixedCounterBytes":16,"scope":"process-wide accepted background commits while request profiling is enabled; not durable or per-owner coverage"})
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn manifest_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TRANSCRIPT_ARCHIVE_MANIFEST_NAMESPACE,
        conversation_id.as_bytes(),
    )
}

fn tail_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TRANSCRIPT_ARCHIVE_PAGE_NAMESPACE,
        format!("{conversation_id}{TAIL_SUFFIX}").as_bytes(),
    )
}

fn page_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    start: u64,
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TRANSCRIPT_ARCHIVE_PAGE_NAMESPACE,
        format!("{conversation_id}\0{start:020}").as_bytes(),
    )
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct ArchivePage {
    start: u64,
    count: u64,
    bytes: u64,
    sha256: String,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
struct ArchiveManifest {
    sealed_messages: u64,
    decoded_bytes: u64,
    pages: Vec<ArchivePage>,
}

fn decode_manifest(raw: &[u8]) -> io::Result<ArchiveManifest> {
    let value: Value = serde_json::from_slice(raw)
        .map_err(|_| invalid_data("transcript archive manifest is not valid JSON"))?;
    if value.get("format").and_then(Value::as_str) != Some(FORMAT) {
        return Err(invalid_data("transcript archive manifest format differs"));
    }
    let uint = |field: &str| -> io::Result<u64> {
        value
            .get(field)
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("transcript archive manifest counter is malformed"))
    };
    let mut pages = Vec::new();
    let mut covered = 0_u64;
    for page in value
        .get("pages")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid_data("transcript archive manifest pages are malformed"))?
    {
        let start = page
            .get("start")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("transcript archive page start is malformed"))?;
        let count = page
            .get("count")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("transcript archive page count is malformed"))?;
        let bytes = page
            .get("bytes")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("transcript archive page bytes are malformed"))?;
        let sha256 = page
            .get("sha256")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("transcript archive page digest is malformed"))?;
        if start != covered
            || count == 0
            || bytes == 0
            || sha256.len() != 64
            || !sha256.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            return Err(invalid_data("transcript archive page directory is discontinuous"));
        }
        covered += count;
        pages.push(ArchivePage {
            start,
            count,
            bytes,
            sha256: sha256.to_owned(),
        });
    }
    let sealed_messages = uint("sealedMessages")?;
    let decoded_bytes = uint("decodedBytes")?;
    if covered > sealed_messages {
        return Err(invalid_data("transcript archive totals differ"));
    }
    Ok(ArchiveManifest {
        sealed_messages,
        decoded_bytes,
        pages,
    })
}

fn encode_manifest(manifest: &ArchiveManifest) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "format": FORMAT,
        "sealedMessages": manifest.sealed_messages,
        "decodedBytes": manifest.decoded_bytes,
        "pages": manifest
            .pages
            .iter()
            .map(|page| json!({
                "start": page.start,
                "count": page.count,
                "bytes": page.bytes,
                "sha256": page.sha256,
            }))
            .collect::<Vec<_>>(),
    }))
    .expect("transcript archive manifest is encodable")
}

fn load_manifest_for(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<Option<ArchiveManifest>> {
    let Some(raw) = vd_read(
        database,
        transaction,
        &manifest_key(transaction, conversation_id)?,
        TRANSCRIPT_ARCHIVE_MANIFEST_NAMESPACE,
    )?
    else {
        return Ok(None);
    };
    decode_manifest(&raw).map(Some)
}

fn vd_put(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: EntityKey,
    namespace: &str,
    logical_key: String,
    value_json: Vec<u8>,
    updated_at_ms: u64,
) -> io::Result<()> {
    let value_len = value_json.len();
    crate::versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: key.clone(),
            namespace: namespace.to_owned(),
            logical_key,
            value_json,
            expected_version: None,
            updated_at_ms,
        },
        transaction.owner_user_id(),
        MAX_TRANSCRIPT_ARCHIVE_PAGE_BYTES,
    )
    .map(|_| ())
}

#[allow(dead_code)]
fn vd_put_dead(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: EntityKey,
    namespace: &str,
    logical_key: String,
    value_json: Vec<u8>,
    updated_at_ms: u64,
) -> io::Result<()> {
    crate::versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key,
            namespace: namespace.to_owned(),
            logical_key,
            value_json,
            expected_version: None,
            updated_at_ms,
        },
        transaction.owner_user_id(),
        MAX_TRANSCRIPT_ARCHIVE_PAGE_BYTES,
    )?;
    Ok(())
}

fn vd_read(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
    namespace: &str,
) -> io::Result<Option<Vec<u8>>> {
    let Some(stored) = database.entity_get(transaction, key)? else {
        return Ok(None);
    };
    let (_, bytes) = crate::versioned_document::materialize_stored_document_bounded(
        database,
        transaction.tenant_id(),
        transaction.owner_user_id(),
        &stored,
        namespace,
        MAX_TRANSCRIPT_ARCHIVE_PAGE_BYTES,
    )?;
    Ok(Some(bytes))
}

/// Seals the provided message window into one immutable page. The window
/// must start at the archive's current sealed count and be built from the
/// lane by the caller's exact materialization path — the archive never
/// interprets turn documents itself.
pub(crate) fn maybe_seal(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_message_count: u64,
    pending: Vec<Value>,
    updated_at_ms: u64,
) -> io::Result<()> {
    let mut manifest = load_manifest_for(database, transaction, conversation_id)?.unwrap_or_default();
    if manifest.sealed_messages > lane_message_count {
        // The lane shrank without an invalidation: fence by rebuilding.
        return invalidate(database, transaction, conversation_id);
    }
    if pending.is_empty() {
        return Ok(());
    }
    let pending_count = pending.len() as u64;
    let page_bytes = serde_json::to_vec(&pending)
        .map_err(|_| invalid_data("transcript archive page is not encodable"))?;
    {
        let key = page_key(transaction, conversation_id, manifest.sealed_messages)?;
        vd_put(
            database,
            transaction,
            key.clone(),
            TRANSCRIPT_ARCHIVE_PAGE_NAMESPACE,
            String::from_utf8(key.key_bytes().to_vec()).expect("archive keys are UTF-8"),
            page_bytes.clone(),
            updated_at_ms,
        )?;
    }
    SEALED.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    manifest.pages.push(ArchivePage {
        start: manifest.sealed_messages,
        count: pending_count,
        bytes: page_bytes.len() as u64,
        sha256: blake3::hash(&page_bytes).to_hex().to_string(),
    });
    manifest.decoded_bytes = manifest
        .decoded_bytes
        .checked_add(page_bytes.len() as u64)
        .ok_or_else(|| invalid_data("transcript archive byte count overflow"))?;
    manifest.sealed_messages = manifest
        .sealed_messages
        .checked_add(pending_count)
        .ok_or_else(|| invalid_data("transcript archive message count overflow"))?;
    if manifest.pages.len() > MAX_TRANSCRIPT_ARCHIVE_PAGES
        || manifest.sealed_messages > MAX_TRANSCRIPT_ARCHIVE_MESSAGES as u64
        || manifest.decoded_bytes > MAX_TRANSCRIPT_ARCHIVE_DECODED_BYTES as u64
    {
        // Over the archive bounds: invalidate instead of erroring the
        // business commit; readers fall back to the exact lane read.
        return invalidate(database, transaction, conversation_id);
    }
    {
        let key = manifest_key(transaction, conversation_id)?;
        vd_put(
            database,
            transaction,
            key.clone(),
            TRANSCRIPT_ARCHIVE_MANIFEST_NAMESPACE,
            String::from_utf8(key.key_bytes().to_vec()).expect("archive keys are UTF-8"),
            encode_manifest(&manifest),
            updated_at_ms,
        )
    }
}

/// Tombstones the archive. Any lane mutation other than `maintain_append`
/// must call this in the same transaction; the fallback read is always
/// correct, so the mutation itself never needs archive-aware surgery.
/// Orphaned sealed pages are left for the online GC's dead-block sweep.
pub(crate) fn invalidate(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<()> {
    INVALIDATED.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    database.entity_delete(transaction, manifest_key(transaction, conversation_id)?)?;
    database.entity_delete(transaction, tail_key(transaction, conversation_id)?)
}

/// The archive's sealed coverage; reads combine these sealed pages with
/// the caller's exact bounded lane read of the recent unsealed tail.
pub(crate) struct SealedCoverage {
    pub sealed_messages: u64,
    pub pages: Vec<(u64, u64)>,
}

fn load_archive(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_message_count: u64,
) -> io::Result<Option<ArchiveManifest>> {
    let Some(manifest) = load_manifest_for(database, transaction, conversation_id)?
    else {
        FALLBACK.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        return Ok(None);
    };
    if manifest.sealed_messages > lane_message_count {
        FALLBACK.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        return Ok(None);
    }
    Ok(Some(manifest))
}

/// The archive's sealed coverage for a conversation, or `None` when no
/// archive (or only a partial one worth ignoring) exists.
pub(crate) fn sealed_coverage(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_message_count: u64,
) -> io::Result<Option<SealedCoverage>> {
    let Some(manifest) = load_archive(database, transaction, conversation_id, lane_message_count)?
    else {
        return Ok(None);
    };
    if manifest.sealed_messages == 0 {
        return Ok(None);
    }
    Ok(Some(SealedCoverage {
        sealed_messages: manifest.sealed_messages,
        pages: manifest
            .pages
            .iter()
            .map(|page| (page.start, page.count))
            .collect(),
    }))
}

fn read_page(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    page: &ArchivePage,
) -> io::Result<Vec<Value>> {
    let stored = database.entity_get(
        transaction, &page_key(transaction, conversation_id, page.start)?,
    )?.ok_or_else(|| invalid_data("transcript archive page is missing"))?;
    let (_, decoded, bytes) = crate::versioned_document::materialize_stored_document_value_and_bytes_bounded(
        database, transaction.tenant_id(), transaction.owner_user_id(), &stored,
        TRANSCRIPT_ARCHIVE_PAGE_NAMESPACE, MAX_TRANSCRIPT_ARCHIVE_PAGE_BYTES,
    )?;
    if bytes.len() as u64 != page.bytes
        || blake3::hash(&bytes).to_hex().to_string() != page.sha256
    {
        return Err(invalid_data("transcript archive page digest differs"));
    }
    // Vec<Value> requires an actual source array. Value's reserved RawValue
    // marker can turn a source object into an array during decoding.
    if bytes.iter().find(|byte| !byte.is_ascii_whitespace()) != Some(&b'[') {
        return Err(invalid_data("transcript archive page is not valid JSON"));
    }
    let Value::Array(messages) = decoded else {
        return Err(invalid_data("transcript archive page is not valid JSON"));
    };
    if messages.len() as u64 != page.count
        || messages
            .iter()
            .any(|message| !message.is_object())
    {
        return Err(invalid_data("transcript archive page message shape is malformed"));
    }
    Ok(messages)
}

/// Archive-backed sealed-prefix read: every sealed message in order.
/// Returns `None` when the archive is absent, fenced, or corrupt — the
/// caller's exact lane read is always the fallback.
pub(crate) fn read_sealed(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_message_count: u64,
) -> io::Result<Option<Vec<Value>>> {
    let Some(manifest) = load_archive(database, transaction, conversation_id, lane_message_count)?
    else {
        return Ok(None);
    };
    let mut messages = Vec::with_capacity(manifest.sealed_messages as usize);
    for page in &manifest.pages {
        messages.extend(read_page(database, transaction, conversation_id, page)?);
    }
    if messages.len() as u64 != manifest.sealed_messages {
        FALLBACK.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        return Ok(None);
    }
    STATS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    Ok(Some(messages))
}

/// Archive-backed bounded window: same fencing as `read_full`, with
/// `legacy_messages`'s exact `[start, end)` semantics on the maintained
/// total (window 0 means full; `before` clips from the left).
pub(crate) fn read_window(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    window: usize,
    before: Option<i64>,
    lane_message_count: u64,
) -> io::Result<Option<(Vec<Value>, usize, usize, usize)>> {
    let Some(archive) = load_archive(database, transaction, conversation_id, lane_message_count)?
    else {
        return Ok(None);
    };
    let total = archive.sealed_messages as usize;
    let before = before
        .filter(|value| *value >= 0)
        .map(|value| (value as usize).min(total))
        .unwrap_or(total);
    let end = before.min(total);
    let start = if window == 0 { 0 } else { end.saturating_sub(window) };
    let mut messages = Vec::with_capacity(end - start);
    for page in &archive.pages {
        let page_end = (page.start + page.count) as usize;
        if page_end <= start || page.start as usize >= end {
            continue;
        }
        for (offset, message) in read_page(database, transaction, conversation_id, page)?
            .into_iter()
            .enumerate()
        {
            let index = page.start as usize + offset;
            if index >= start && index < end {
                messages.push(message);
            }
        }
    }
    if messages.len() != end - start {
        FALLBACK.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        return Ok(None);
    }
    STATS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    Ok(Some((messages, total, start, end)))
}

#[cfg(test)]
mod tests;

/// Dedicated `tofu-db-archive-seal` worker: seals pending transcript
/// windows into immutable pages off the foreground path. Window
/// materialization and compression run on an accounted preparation replica.
/// Publication validates source and manifest witnesses through ordinary OCC.
/// Empty pages are idle, so the worker parks after catching up.
pub struct ArchiveSealDriver {
    stop: Arc<AtomicBool>,
    thread: Mutex<Option<std::thread::JoinHandle<()>>>,
}

const SEAL_ROUND_MESSAGES: u64 = 128;
const SEAL_ROUND_CONVERSATIONS: usize = 8;
const SEAL_POLL_INTERVAL: Duration = Duration::from_millis(250);
const SEAL_DRIVER_STACK_BYTES: usize = 1024 * 1024;

impl ArchiveSealDriver {
    pub fn start(
        database: &Arc<Mutex<AuthorityDatabase>>,
        tenant_id: u64,
        owner_id: u64,
    ) -> io::Result<Self> {
        let stop = Arc::new(AtomicBool::new(false));
        let thread = std::thread::Builder::new()
            .name("tofu-db-archive-seal".to_owned())
            .stack_size(SEAL_DRIVER_STACK_BYTES)
            .spawn({
                let stop = Arc::clone(&stop);
                let database = Arc::clone(database);
                move || run_archive_seal_worker(database, tenant_id, owner_id, stop)
            })?;
        Ok(Self {
            stop,
            thread: Mutex::new(Some(thread)),
        })
    }

    pub fn stop_and_join(&self) -> io::Result<()> {
        self.stop.store(true, Ordering::Release);
        let thread = self
            .thread
            .lock()
            .map_err(|_| io::Error::other("archive seal driver lock poisoned"))?
            .take();
        if let Some(thread) = thread {
            thread
                .join()
                .map_err(|_| io::Error::other("archive seal driver panicked"))?;
        }
        Ok(())
    }
}

fn enumerate_conversations(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    after: &mut Vec<u8>,
    maximum: usize,
) -> io::Result<Vec<String>> {
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        crate::generated_tofudb_ir::CONVERSATION_DOCUMENT_NAMESPACE,
        &[],
    )?;
    let mut conversations = Vec::new();
    let scan_start = if after.is_empty() {
        start
    } else {
        crate::turn::after_key(
            &EntityKey::new(
                transaction.tenant_id(),
                transaction.owner_user_id(),
                crate::generated_tofudb_ir::CONVERSATION_DOCUMENT_NAMESPACE,
                after.as_slice(),
            )?,
            transaction,
            crate::generated_tofudb_ir::CONVERSATION_DOCUMENT_NAMESPACE,
        )?
    };
    for (key, _) in database.entity_scan(transaction, &scan_start, &end, maximum + 1)? {
        conversations.push(
            String::from_utf8(key.key_bytes().to_vec())
                .map_err(|_| invalid_data("conversation key is not UTF-8"))?,
        );
    }
    if conversations.len() > maximum {
        // The lookahead row was not processed; resume after the last
        // returned row so the next batch still visits that lookahead.
        conversations.truncate(maximum);
        *after = conversations
            .last()
            .expect("archive enumeration batch is non-empty")
            .as_bytes()
            .to_vec();
    } else {
        after.clear();
    }
    Ok(conversations)
}

fn run_archive_seal_worker(
    database: Arc<Mutex<AuthorityDatabase>>,
    tenant_id: u64,
    owner_id: u64,
    stop: Arc<AtomicBool>,
) {
    // Archive sealing is background maintenance, including its commit activity.
    // The foreground-pressure sampler must not treat it as a user request.
    crate::fsync_stats::set_current_component(crate::fsync_stats::COMPONENT_MAINTENANCE);
    let mut cursor = Vec::new();
    loop {
        if stop.load(Ordering::Acquire) {
            return;
        }
        let mut did_work = false;
        if let Ok(conversations) = (|| -> io::Result<Vec<String>> {
            let replica = {
                let guard = database
                    .lock()
                    .map_err(|_| io::Error::other("authority mutex poisoned"))?;
                let _hold = crate::authority_watch::AuthorityHoldTimer::probe("archive.capture");
                guard.build_read_replica()?
            };
            let mut transaction = replica.begin(tenant_id, owner_id)?;
            enumerate_conversations(&replica, &mut transaction, &mut cursor, SEAL_ROUND_CONVERSATIONS)
        })() {
            for conversation_id in conversations {
                if stop.load(Ordering::Acquire) {
                    return;
                }
                if seal_one_conversation(&database, tenant_id, owner_id, &conversation_id)
                    .is_ok_and(|sealed| sealed)
                {
                    did_work = true;
                }
            }
        }
        if !did_work {
            std::thread::sleep(SEAL_POLL_INTERVAL * 4);
        }
        // Catch-up mode: a round that sealed anything starts the next pass
        // immediately so fresh bursts seal inside ~1 s.
    }
}

/// Capture a writable preparation snapshot, then perform the whole logical
/// page build off the foreground mutex. The existing commit boundary checks
/// every source/manifest read before publishing private blobs. A changed lane
/// or archive defers this page instead of overwriting concurrent mutations.
fn seal_one_conversation(
    database: &Arc<Mutex<AuthorityDatabase>>,
    tenant_id: u64,
    owner_id: u64,
    conversation_id: &str,
) -> io::Result<bool> {
    let replica = {
        let guard = database.try_lock().map_err(|_| {
            io::Error::new(
                io::ErrorKind::WouldBlock,
                "archive seal replica hold contended",
            )
        })?;
        let _hold = crate::authority_watch::AuthorityHoldTimer::probe("archive.capture");
        guard.build_preparation_replica()?
    };
    let Some(prepared) = prepare_one_conversation(&replica, tenant_id, owner_id, conversation_id)?
    else {
        return Ok(false);
    };
    drop(replica);
    let mut guard = database.try_lock().map_err(|_| {
        io::Error::new(
            io::ErrorKind::WouldBlock,
            "archive seal publish hold contended",
        )
    })?;
    let _hold = crate::authority_watch::AuthorityHoldTimer::probe("archive.publish");
    guard.commit(prepared.transaction)?;
    if crate::request_profile::enabled() {
        ACCEPTED_PAGES.fetch_add(1, Ordering::Relaxed);
        ACCEPTED_MESSAGES.fetch_add(prepared.messages, Ordering::Relaxed);
    }
    Ok(true)
}

/// All materialization, JSON encoding and Blob compression belongs to the
/// private snapshot. Its transaction retains both source witnesses and the
/// accounted buffers until the writer commits or discards the page.
struct PreparedArchivePage {
    transaction: AuthorityTransaction,
    messages: u64,
}

fn prepare_one_conversation(
    replica: &AuthorityDatabase,
    tenant_id: u64,
    owner_id: u64,
    conversation_id: &str,
) -> io::Result<Option<PreparedArchivePage>> {
    let mut transaction = replica.begin(tenant_id, owner_id)?;
    let lane_count =
        match crate::turn::read_lane_state(replica, &mut transaction, conversation_id, "main")? {
            Some(state) => state.count.unwrap_or(0),
            None => 0,
        };
    let manifest =
        load_manifest_for(replica, &mut transaction, conversation_id)?.unwrap_or_default();
    let sealed = manifest.sealed_messages;
    if sealed >= lane_count {
        return Ok(None);
    }
    let execution_epoch =
        crate::conversation_header::execution_epoch(replica, &mut transaction, conversation_id)?;
    let end = (sealed + SEAL_ROUND_MESSAGES).min(lane_count);
    let pending = crate::turn::materialize_lane_range(
        replica,
        &mut transaction,
        conversation_id,
        sealed,
        end,
        execution_epoch,
    )?;
    let messages = pending.len() as u64;
    if messages == 0 {
        return Ok(None);
    }
    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_millis() as u64)
        .unwrap_or(1)
        .max(1);
    maybe_seal(
        replica,
        &mut transaction,
        conversation_id,
        lane_count,
        pending,
        now_ms,
    )?;
    Ok(Some(PreparedArchivePage {
        transaction,
        messages,
    }))
}
