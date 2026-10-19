//! Opt-in, bounded request cost attribution; exported through system.metrics.
//! Reuses fsync_stats spans, subtracts children, and counts work on this thread.
//! Only catalog operation names and local sample numbers are retained: never
//! request IDs, owners, keys, paths, payloads, or arbitrary error messages.
//! No I/O and no authority acquisition. Collection happens after dispatch.
//! Overlay head/original-blind/range probes add three fixed Stage slots
//! (72 bytes per Sample and operation aggregate); reported active/retained
//! budgets derive from these structure sizes. Five document/Turn read stages
//! add 120 bytes per Sample/operation aggregate; no per-key labels are created.

use std::cell::RefCell;
use std::marker::PhantomData;
use std::rc::Rc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Instant;
use serde_json::{json, Value};

const BASE: usize = crate::fsync_stats::STAGE_NAMES.len();
pub(crate) const FIFO_WAIT: usize = BASE;
pub(crate) const MUTEX_WAIT: usize = BASE + 1;
pub(crate) const AUTHORITY_HOLD: usize = BASE + 2;
pub(crate) const REPLICA_BUILD: usize = BASE + 3;
pub(crate) const PARALLEL_COMPILE: usize = BASE + 4;
pub(crate) const QUERY_EXECUTE: usize = BASE + 5;
pub(crate) const VFS_OPEN: usize = BASE + 6;
pub(crate) const VFS_READ: usize = BASE + 7;
pub(crate) const VFS_WRITE: usize = BASE + 8;
pub(crate) const VFS_SYNC: usize = BASE + 9;
pub(crate) const VFS_METADATA: usize = BASE + 10;
pub(crate) const VFS_DIRECTORY: usize = BASE + 11;
pub(crate) const VFS_MUTATE: usize = BASE + 12;
pub(crate) const BLOB_CODEC_WAIT: usize = BASE + 13;
pub(crate) const BLOB_CODEC_COMPRESS: usize = BASE + 14;
pub(crate) const STREAM_SEGMENT_ENCODE: usize = BASE + 15;
pub(crate) const STREAM_SEGMENT_FRESH_PUBLISH: usize = BASE + 16;
pub(crate) const DOCUMENT_MATERIALIZE: usize = BASE + 17;
pub(crate) const TURN_PUBLIC: usize = BASE + 18;
pub(crate) const TURN_SNAPSHOT_ENCODE: usize = BASE + 19;
pub(crate) const TURN_SNAPSHOT_WINDOW: usize = BASE + 20;
pub(crate) const TURN_SNAPSHOT_ATTEMPTS: usize = BASE + 21;
const EXTRA_NAMES: [&str; 22] = ["fifo_wait", "mutex_wait", "authority_hold",
    "replica_build", "parallel_compile", "query_execute", "vfs_open", "vfs_read",
    "vfs_write", "vfs_sync", "vfs_metadata", "vfs_directory", "vfs_mutate",
    "blob_codec_wait", "blob_codec_compress", "stream_segment_encode", "stream_segment_fresh_publish", "document_materialize", "turn_public", "turn_snapshot_encode", "turn_snapshot_window", "turn_snapshot_attempts"];
const STAGES: usize = BASE + EXTRA_NAMES.len();
const MAX_DEPTH: usize = 64;
const SLOW_SAMPLES: usize = 32;
const HISTOGRAM_BUCKETS: usize = 64;

#[derive(Clone, Copy)]
pub(crate) enum Work { EntityGet, EntityReadReuse, EntityOverlayRead, EntityTreeRead,
    WalBytes, OverlayBytes, ReadBytes, WriteBytes, PointConflict, BlindWriteConflict, RangeConflict, RootConflict, AbandonedPreparationNanos, HistoryDecodeAttempts, HistoryDecodeSourceBytes, HistoryDecodedTransactions, ProjectionCheckpointBytes, ProjectionPatchBytes, ProjectionPatchesApplied, JsonBorrowedStrings, JsonBorrowedStringBytes, JsonResponseProofReuse, JsonResponseProofBytes, JsonCanonicalProofReuse, JsonCanonicalProofBytes, JsonCanonicalProofMiss, BlobDecodedBytes, BlobContentHashBytes, BlobCollectedBytes, BlobChunkProofReuse, BlobChunkProofBytes, BlobWholeProofReuse, BlobWholeProofBytes, BlobWholeProofMiss, BlobCurrentEncodedBytes, PublishedReadReuse, PublishedReadFallback, PublishedReadPublication, EmbeddedBindingsReuse, PinRegistryAcquire, PinBudgetReserve, PinBudgetRelease, PinReleaseNotifications, EmbeddedReplyReuse, EmbeddedReplyCreate, IrResultMovedBytes, IrResultCopiedBytes, SemanticPayloadDecodedBytes, EntityPointRetainedBytes, ReceiptPolicyProofReuse, ColdHistoryDedupeBypass, FreshStreamSegments, FreshStreamBytes }
const WORK_NAMES: [&str; 53] = ["entity_get", "entity_read_reuse", "entity_overlay_read",
    "entity_tree_read", "wal_encoded_bytes", "overlay_materialized_bytes",
    "vfs_read_requested_bytes", "vfs_write_requested_bytes", "point_conflict", "blind_write_conflict", "range_conflict", "root_conflict", "abandoned_preparation_nanos", "history_decode_attempts", "history_decode_source_bytes", "history_decoded_transactions", "projection_checkpoint_bytes", "projection_patch_bytes", "projection_patches_applied", "json_borrowed_strings", "json_borrowed_string_bytes", "json_response_proof_reuse", "json_response_proof_bytes", "json_canonical_proof_reuse", "json_canonical_proof_bytes", "json_canonical_proof_miss", "blob_decoded_bytes", "blob_content_hash_bytes", "blob_collected_bytes", "blob_chunk_proof_reuse", "blob_chunk_proof_bytes", "blob_whole_proof_reuse", "blob_whole_proof_bytes", "blob_whole_proof_miss", "blob_current_encoded_bytes", "published_read_reuse", "published_read_fallback", "published_read_publication", "embedded_bindings_reuse", "pin_registry_acquire", "pin_budget_reserve_ops", "pin_budget_release_ops", "pin_release_notifications", "embedded_reply_reuse", "embedded_reply_create", "ir_result_moved_bytes", "ir_result_copied_bytes", "semantic_payload_decoded_bytes", "entity_point_retained_bytes", "receipt_policy_proof_reuse", "cold_history_dedupe_bypass", "fresh_stream_segments", "fresh_stream_bytes"];

static CONFIG: OnceLock<u64> = OnceLock::new();
static SEQUENCE: AtomicU64 = AtomicU64::new(0);
static SELECTED: AtomicU64 = AtomicU64::new(0);
static DROPPED: AtomicU64 = AtomicU64::new(0);
static COLLECTOR: OnceLock<Mutex<Collector>> = OnceLock::new();
thread_local! {
    static ACTIVE: RefCell<Option<Box<RequestState>>> = const { RefCell::new(None) };
    static REPLY_REUSED: std::cell::Cell<Option<bool>> = const { std::cell::Cell::new(None) };
    static QUEUE: std::cell::Cell<Option<u64>> = const { std::cell::Cell::new(None) };
}

fn sample_every() -> u64 {
    *CONFIG.get_or_init(|| std::env::var("TOFU_DB_REQUEST_PROFILE_EVERY")
        .ok().and_then(|s| s.parse::<u64>().ok()).unwrap_or(0))
}
pub(crate) fn enabled() -> bool { sample_every() != 0 }

fn operation_name(name: &str) -> &'static str {
    crate::generated_storage_operations::STORAGE_OPERATIONS.iter()
        .find(|op| op.name == name).map(|op| op.name).unwrap_or("uncataloged")
}

#[derive(Clone, Copy, Default)]
struct Stage { calls: u64, inclusive: u64, exclusive: u64 }
#[derive(Clone, Copy)]
struct Frame { id: u64, stage: usize, children: u64 }

struct RequestState {
    sample: Sample,
    frames: Vec<Frame>,
    next_span: u64,
    covered: u64,
}
impl RequestState {
    fn enter(&mut self, stage: usize) -> Option<u64> {
        if self.frames.len() == MAX_DEPTH {
            self.sample.invalid_spans += 1;
            return None;
        }
        self.next_span += 1;
        self.frames.push(Frame { id: self.next_span, stage, children: 0 });
        Some(self.next_span)
    }
    fn exit(&mut self, id: u64, elapsed: u64) {
        // Crossing lifetimes are not a tree. Never manufacture a valid
        // exclusive-cost claim when a timer is dropped out of order.
        if self.frames.last().map(|f| f.id) != Some(id) {
            self.sample.invalid_spans += 1;
            return;
        }
        let frame = self.frames.pop().unwrap();
        let stage = &mut self.sample.stages[frame.stage];
        stage.calls += 1;
        stage.inclusive += elapsed;
        stage.exclusive += elapsed.saturating_sub(frame.children);
        if frame.children > elapsed { self.sample.invalid_spans += 1; }
        if let Some(parent) = self.frames.last_mut() { parent.children += elapsed; }
        else { self.covered += elapsed; }
    }
    fn finish(mut self, nanos: u64, outcome: &'static str) -> Sample {
        self.sample.dispatch_nanos = nanos;
        self.sample.outcome = outcome;
        self.sample.invalid_spans += self.frames.len() as u64;
        if self.covered > nanos { self.sample.invalid_spans += 1; }
        self.sample.unattributed = nanos.saturating_sub(self.covered);
        self.sample
    }
}

#[derive(Clone)]
struct Sample {
    sequence: u64, operation: &'static str, outcome: &'static str,
    dispatch_nanos: u64, queue_nanos: Option<u64>, unattributed: u64, invalid_spans: u64,
    stages: [Stage; STAGES], work: [u64; WORK_NAMES.len()],
}
impl Sample {
    fn new(sequence: u64, operation: &'static str, queue_nanos: Option<u64>) -> Self {
        Self { sequence, operation, outcome: "unwound", dispatch_nanos: 0, queue_nanos,
            unattributed: 0, invalid_spans: 0, stages: [Stage::default(); STAGES],
            work: [0; WORK_NAMES.len()] }
    }
}

/// Thread-bound token prevents moving a timed span onto an unrelated request.
pub(crate) struct SpanToken { request: u64, span: u64, _thread: PhantomData<Rc<()>> }
pub(crate) fn enter(stage: usize) -> Option<SpanToken> {
    ACTIVE.with(|active| {
        let mut active = active.borrow_mut();
        let state = active.as_mut()?;
        let span = state.enter(stage)?;
        Some(SpanToken { request: state.sample.sequence, span, _thread: PhantomData })
    })
}
pub(crate) fn exit(token: Option<SpanToken>, nanos: u64) {
    let Some(token) = token else { return; };
    ACTIVE.with(|active| {
        if let Some(state) = active.borrow_mut().as_mut() {
            if state.sample.sequence == token.request { state.exit(token.span, nanos); }
        }
    });
}
pub(crate) fn fallback() {
    ACTIVE.with(|active| {
        if let Some(state) = active.borrow_mut().as_mut() {
            state.sample.work[Work::AbandonedPreparationNanos as usize] +=
                state.sample.stages[crate::fsync_stats::STAGE_PARALLEL_PREPARE].inclusive;
        }
    });
}
pub(crate) fn event(stage: usize) {
    ACTIVE.with(|active| {
        if let Some(state) = active.borrow_mut().as_mut() { state.sample.stages[stage].calls += 1; }
    });
}
pub(crate) fn work(counter: Work, amount: u64) {
    ACTIVE.with(|active| {
        if let Some(state) = active.borrow_mut().as_mut() { state.sample.work[counter as usize] += amount; }
    });
}

pub(crate) struct Probe(Option<(SpanToken, Instant)>);
impl Probe {
    pub(crate) fn new(stage: usize) -> Self {
        Self(enter(stage).map(|token| (token, Instant::now())))
    }
}
impl Drop for Probe {
    fn drop(&mut self) {
        if let Some((token, start)) = self.0.take() { exit(Some(token), start.elapsed().as_nanos() as u64); }
    }
}

/// Embedded enqueue-to-worker delay is adjacent to, NOT part of dispatch time.
pub(crate) struct QueueContext(Option<u64>, Option<bool>, PhantomData<Rc<()>>);
impl QueueContext {
    pub(crate) fn completed_inline() -> Self { Self::with_context(Some(0), None) }
    pub(crate) fn with_reply_reuse(nanos: Option<u64>, reused: bool) -> Self { Self::with_context(nanos, Some(reused)) }
    fn with_context(nanos: Option<u64>, reused: Option<bool>) -> Self {
        Self(QUEUE.with(|q| q.replace(nanos)), REPLY_REUSED.with(|q| q.replace(reused)), PhantomData)
    }
    #[cfg(test)]
    pub(crate) fn new(nanos: Option<u64>) -> Self { Self::with_context(nanos, None) }
}
impl Drop for QueueContext { fn drop(&mut self) { QUEUE.with(|q| q.set(self.0)); REPLY_REUSED.with(|q| q.set(self.1)); } }

pub(crate) struct RequestGuard { start: Instant, selected: bool, outcome: &'static str,
    _thread: PhantomData<Rc<()>> }
impl RequestGuard {
    /// An owned result-cache hit has already proved it will complete here.
    /// Attribute its bounded lookup prelude without sampling failed probes a
    /// second time when they later enter the ordinary worker dispatch.
    pub(crate) fn start_at(operation: &str, started_at: Instant) -> Option<Self> {
        let mut guard = Self::start(operation)?;
        guard.start = started_at;
        Some(guard)
    }
    pub(crate) fn start(operation: &str) -> Option<Self> {
        let every = sample_every();
        if every == 0 || crate::control_ops::is_control_operation(operation) { return None; }
        let sequence = SEQUENCE.fetch_add(1, Ordering::Relaxed) + 1;
        if (sequence - 1) % every != 0 { return None; }
        SELECTED.fetch_add(1, Ordering::Relaxed);
        Self::begin(sequence, operation_name(operation))
    }
    fn begin(sequence: u64, operation: &'static str) -> Option<Self> {
        let start = Instant::now();
        let selected = ACTIVE.with(|active| {
            let mut active = active.borrow_mut();
            if active.is_some() { return false; }
            let mut sample = Sample::new(sequence, operation, QUEUE.with(|q| q.get()));
            if let Some(reused) = REPLY_REUSED.with(|q| q.get()) {
                sample.work[if reused { Work::EmbeddedReplyReuse } else { Work::EmbeddedReplyCreate } as usize] = 1;
            }
            *active = Some(Box::new(RequestState {
                sample,
                frames: Vec::with_capacity(MAX_DEPTH), next_span: 0, covered: 0,
            }));
            true
        });
        if !selected { DROPPED.fetch_add(1, Ordering::Relaxed); return None; }
        Some(Self { start, selected, outcome: "unwound", _thread: PhantomData })
    }
    pub(crate) fn finish(&mut self, outcome: &'static str) { self.outcome = outcome; }
}
impl Drop for RequestGuard {
    fn drop(&mut self) {
        if !self.selected { return; }
        let state = ACTIVE.with(|active| active.borrow_mut().take());
        if let Some(state) = state {
            let sample = state.finish(self.start.elapsed().as_nanos() as u64, self.outcome);
            // Never stall storage for diagnostics. A contended snapshot/merge
            // loses a sample and makes that loss explicit in the report.
            if let Ok(mut collector) = COLLECTOR.get_or_init(Default::default).try_lock() {
                collector.add(sample);
            } else { DROPPED.fetch_add(1, Ordering::Relaxed); }
        }
    }
}

#[derive(Clone)]
struct Aggregate { total: Sample, requests: u64, errors: u64,
    histogram: [u64; HISTOGRAM_BUCKETS], queue_samples: u64, maximum: u64 }
impl Aggregate {
    fn new(operation: &'static str) -> Self { Self { total: Sample::new(0, operation, None),
        requests: 0, errors: 0, histogram: [0; HISTOGRAM_BUCKETS], queue_samples: 0, maximum: 0 } }
    fn add(&mut self, sample: &Sample) {
        self.requests += 1;
        self.errors += u64::from(sample.outcome != "ok");
        self.maximum = self.maximum.max(sample.dispatch_nanos);
        let bucket = (64 - sample.dispatch_nanos.saturating_sub(1).leading_zeros() as usize).min(63);
        self.histogram[bucket] += 1;
        self.total.dispatch_nanos += sample.dispatch_nanos;
        self.total.unattributed += sample.unattributed;
        self.total.invalid_spans += sample.invalid_spans;
        if let Some(queue) = sample.queue_nanos {
            *self.total.queue_nanos.get_or_insert(0) += queue;
            self.queue_samples += 1;
        }
        for (a, b) in self.total.stages.iter_mut().zip(&sample.stages) {
            a.calls += b.calls; a.inclusive += b.inclusive; a.exclusive += b.exclusive;
        }
        for (a, b) in self.total.work.iter_mut().zip(&sample.work) { *a += b; }
    }
}
#[derive(Clone, Default)]
struct Collector { operations: Vec<Aggregate>, slow: Vec<Sample> }
impl Collector {
    fn add(&mut self, sample: Sample) {
        let index = self.operations.iter().position(|a| a.total.operation == sample.operation)
            .unwrap_or_else(|| { self.operations.push(Aggregate::new(sample.operation)); self.operations.len() - 1 });
        self.operations[index].add(&sample);
        if self.slow.len() < SLOW_SAMPLES { self.slow.push(sample); }
        else if let Some((index, slowest_floor)) = self.slow.iter().enumerate().min_by_key(|(_, s)| s.dispatch_nanos) {
            if sample.dispatch_nanos > slowest_floor.dispatch_nanos { self.slow[index] = sample; }
        }
    }
}

fn sample_json(sample: &Sample) -> Value {
    let names = crate::fsync_stats::STAGE_NAMES.iter().chain(EXTRA_NAMES.iter());
    let stages: Vec<_> = names.zip(&sample.stages).filter(|(_, s)| s.calls != 0)
        .map(|(name, s)| json!({"stage": name, "calls": s.calls,
            "inclusiveNanos": s.inclusive, "exclusiveNanos": s.exclusive})).collect();
    let work: serde_json::Map<String, Value> = WORK_NAMES.iter().zip(sample.work)
        .map(|(name, value)| (name.to_string(), json!(value))).collect();
    json!({"sample": sample.sequence, "operation": sample.operation, "outcome": sample.outcome,
        "dispatchNanos": sample.dispatch_nanos, "embeddedQueueNanos": sample.queue_nanos,
        "unattributedNanos": sample.unattributed, "invalidSpans": sample.invalid_spans,
        "stages": stages, "work": work})
}

fn aggregate_json(a: &Aggregate) -> Value {
    let mut value = sample_json(&a.total);
    // Totals combine successes and errors. A synthetic Sample's initial
    // outcome/sequence is not an observed request and must not escape here.
    let object = value.as_object_mut().expect("sample JSON object");
    object.remove("outcome");
    object.remove("sample");
    value["kind"] = json!("aggregate");
        value["requests"] = json!(a.requests); value["errors"] = json!(a.errors);
        value["dispatchLog2NanosHistogram"] = json!(a.histogram.as_slice());
        value["maxDispatchNanos"] = json!(a.maximum);
        value["embeddedQueueSamples"] = json!(a.queue_samples);
        value
}

pub(crate) fn snapshot() -> Value {
    let every = sample_every();
    if every == 0 { return json!({"enabled": false}); }
    let Ok(locked) = COLLECTOR.get_or_init(Default::default).try_lock() else {
        return json!({"enabled": true, "available": false});
    };
    let retained_bytes = locked.operations.capacity() * std::mem::size_of::<Aggregate>()
        + locked.slow.capacity() * std::mem::size_of::<Sample>();
    let collector = locked.clone();
    drop(locked);
    let operations: Vec<_> = collector.operations.iter().map(|a| {
        aggregate_json(a)
    }).collect();
    json!({"enabled": true, "available": true, "contract": "tofudb.request-profile/v1",
        "scope": "process lifetime; completed sampled dispatches; wall time, not CPU time",
        "sampleEvery": every, "seen": SEQUENCE.load(Ordering::Relaxed),
        "selected": SELECTED.load(Ordering::Relaxed), "dropped": DROPPED.load(Ordering::Relaxed),
        "maximumSlowSamples": SLOW_SAMPLES, "maximumSpanDepth": MAX_DEPTH,
        "activeRequestBytes": std::mem::size_of::<RequestState>() + MAX_DEPTH * std::mem::size_of::<Frame>(),
        "retainedBytes": retained_bytes,
        "maximumOperationLabels": crate::generated_storage_operations::STORAGE_OPERATIONS.len() + 1,
        "authorityHolds": crate::authority_watch::hold_profile(),
        "operations": operations, "slowSamples": collector.slow.iter().map(sample_json).collect::<Vec<_>>()})
}

#[cfg(test)]
#[path = "request_profile_tests.rs"]
mod tests;
