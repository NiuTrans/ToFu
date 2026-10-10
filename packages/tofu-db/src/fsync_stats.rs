//! Bounded process-wide durability and stage counters.
//! StageTimer records nested elapsed totals; stage_report and stage_nanos expose
//! diagnostic snapshots. Overlapping stages and worker time must not be summed
//! as disjoint foreground latency. Only fixed atomics are retained.

use std::sync::atomic::{AtomicU64, Ordering};

// Per-component stage attribution: the same 44 commit stages are also
// counted per thread component so a report can tell foreground connection
// work apart from background drivers (a FUSE drain step costs the same
// nanos but matters only when it lands on a foreground commit).
pub const COMPONENT_FOREGROUND: usize = 0;
pub const COMPONENT_ROTATION_DRIVER: usize = 1;
pub const COMPONENT_PACK_ADOPTION_DRIVER: usize = 2;
pub const COMPONENT_BACKGROUND_PACKER: usize = 3;
pub const COMPONENT_MAINTENANCE: usize = 4;
pub const COMPONENT_OTHER: usize = 5;
pub const COMPONENT_WAL_BARRIER: usize = 6;
const COMPONENT_COUNT: usize = 7;
pub const COMPONENT_NAMES: [&str; COMPONENT_COUNT] = [
    "foreground",
    "rotation_driver",
    "pack_adoption_driver",
    "background_packer",
    "maintenance",
    "other",
    "wal_barrier",
];

thread_local! {
    static CURRENT_COMPONENT: std::cell::Cell<usize> = const { std::cell::Cell::new(COMPONENT_FOREGROUND) };
}

/// Pin this thread's component for stage attribution. Connection workers
/// keep the default foreground; daemon drivers and maintenance call this
/// once at thread start.
pub fn set_current_component(component: usize) {
    assert!(component < COMPONENT_COUNT);
    CURRENT_COMPONENT.with(|cell| cell.set(component));
}

fn current_component() -> usize {
    CURRENT_COMPONENT.with(|cell| cell.get())
}

#[cfg(test)]
pub(crate) fn current_component_for_test() -> usize {
    current_component()
}

static PUBLISH_CONTROL: AtomicU64 = AtomicU64::new(0);
static WAL_APPEND: AtomicU64 = AtomicU64::new(0);
static FLUSH_DEFERRED: AtomicU64 = AtomicU64::new(0);
static CHECKPOINT: AtomicU64 = AtomicU64::new(0);
static CHECKPOINT_HISTORY_TAIL_BYTES: AtomicU64 = AtomicU64::new(0);
static MAX_CHECKPOINT_HISTORY_TAIL_BYTES: AtomicU64 = AtomicU64::new(0);
static ROTATION_WINDOW_TRANSACTIONS: AtomicU64 = AtomicU64::new(0);
static ROTATION_WINDOW_BYTES: AtomicU64 = AtomicU64::new(0);
static MAX_ROTATION_WINDOW_BYTES: AtomicU64 = AtomicU64::new(0);
static DROP_FLUSH_FAILURES: AtomicU64 = AtomicU64::new(0);
static HISTORY_DECODE_ATTEMPTS: AtomicU64 = AtomicU64::new(0);
static HISTORY_DECODE_SOURCE_BYTES: AtomicU64 = AtomicU64::new(0);
static HISTORY_DECODED_TRANSACTIONS: AtomicU64 = AtomicU64::new(0);
static WAL_ENCODED_BYTES: AtomicU64 = AtomicU64::new(0);
static PROJECTION_CHECKPOINT_BYTES: AtomicU64 = AtomicU64::new(0);
static PROJECTION_PATCH_BYTES: AtomicU64 = AtomicU64::new(0);
static PROJECTION_PATCHES_APPLIED: AtomicU64 = AtomicU64::new(0);
static OVERLAY_MERGE_COMMITS: AtomicU64 = AtomicU64::new(0);
static OVERLAY_MERGED_ENTRIES: AtomicU64 = AtomicU64::new(0);
static OVERLAY_SUPERSEDED_POSITIONS: AtomicU64 = AtomicU64::new(0);
static OVERLAY_MERGED_POSITIONS: AtomicU64 = AtomicU64::new(0);
static OVERLAY_MATERIALIZED_BYTES: AtomicU64 = AtomicU64::new(0);
static OVERLAY_MATERIALIZED_BLOCKS: AtomicU64 = AtomicU64::new(0);
static OVERLAY_WAL_PRESSURE_ATTEMPTS: AtomicU64 = AtomicU64::new(0);
static OVERLAY_WAL_PRESSURE_PENDING_ENTRIES: AtomicU64 = AtomicU64::new(0);

pub(crate) fn note_projection_checkpoint_bytes(bytes: usize) {
    PROJECTION_CHECKPOINT_BYTES.fetch_add(bytes as u64, Ordering::Relaxed);
    crate::request_profile::work(crate::request_profile::Work::ProjectionCheckpointBytes, bytes as u64);
}

pub(crate) fn note_projection_patch_bytes(bytes: usize) {
    PROJECTION_PATCH_BYTES.fetch_add(bytes as u64, Ordering::Relaxed);
    crate::request_profile::work(crate::request_profile::Work::ProjectionPatchBytes, bytes as u64);
}

pub(crate) fn note_projection_patch_applied() {
    PROJECTION_PATCHES_APPLIED.fetch_add(1, Ordering::Relaxed);
    crate::request_profile::work(crate::request_profile::Work::ProjectionPatchesApplied, 1);
}

pub(crate) fn note_history_decode_attempt(bytes: usize) {
    HISTORY_DECODE_ATTEMPTS.fetch_add(1, Ordering::Relaxed);
    HISTORY_DECODE_SOURCE_BYTES.fetch_add(bytes as u64, Ordering::Relaxed);
    crate::request_profile::work(crate::request_profile::Work::HistoryDecodeAttempts, 1);
    crate::request_profile::work(crate::request_profile::Work::HistoryDecodeSourceBytes, bytes as u64);
}

pub(crate) fn note_history_decoded_transactions(count: usize) {
    HISTORY_DECODED_TRANSACTIONS.fetch_add(count as u64, Ordering::Relaxed);
    crate::request_profile::work(crate::request_profile::Work::HistoryDecodedTransactions, count as u64);
}

/// Bytes accepted by WAL pwrite, before its durability barrier. This is engine
/// traffic, not physical device writes and not an acknowledged-durability count.
pub(crate) fn note_wal_encoded_bytes(bytes: u64) {
    crate::request_profile::work(crate::request_profile::Work::WalBytes, bytes);
    WAL_ENCODED_BYTES.fetch_add(bytes, Ordering::Relaxed);
}

/// Newly staged page bytes referenced by a successfully published merge.
/// Reused durable blocks contribute references but no newly staged bytes.
pub(crate) fn note_overlay_merge(entries: usize, bytes: usize, blocks: usize, positions: usize, superseded: usize) {
    crate::request_profile::work(crate::request_profile::Work::OverlayBytes, bytes as u64);
    OVERLAY_MERGE_COMMITS.fetch_add(1, Ordering::Relaxed);
    OVERLAY_MERGED_ENTRIES.fetch_add(entries as u64, Ordering::Relaxed);
    OVERLAY_MERGED_POSITIONS.fetch_add(positions as u64, Ordering::Relaxed);
    OVERLAY_SUPERSEDED_POSITIONS.fetch_add(superseded as u64, Ordering::Relaxed);
    OVERLAY_MATERIALIZED_BYTES.fetch_add(bytes as u64, Ordering::Relaxed);
    OVERLAY_MATERIALIZED_BLOCKS.fetch_add(blocks as u64, Ordering::Relaxed);
}

pub(crate) fn note_overlay_wal_pressure(pending_entries: usize) {
    OVERLAY_WAL_PRESSURE_ATTEMPTS.fetch_add(1, Ordering::Relaxed);
    OVERLAY_WAL_PRESSURE_PENDING_ENTRIES.fetch_add(pending_entries as u64, Ordering::Relaxed);
}

pub fn note_publish_control() {
    PUBLISH_CONTROL.fetch_add(1, Ordering::Relaxed);
}

/// The Engine::drop CONTROL catch-up stays fire-and-forget by contract; a
/// failure is counted here (and logged at the call site) instead of retried.
pub fn note_drop_flush_failure() {
    DROP_FLUSH_FAILURES.fetch_add(1, Ordering::Relaxed);
}

pub fn note_wal_append() {
    WAL_APPEND.fetch_add(1, Ordering::Relaxed);
}

pub fn note_flush_deferred() {
    FLUSH_DEFERRED.fetch_add(1, Ordering::Relaxed);
}

pub fn note_checkpoint() {
    CHECKPOINT.fetch_add(1, Ordering::Relaxed);
}

/// Record bytes remaining after partial preparation finishes, excluding segment
/// headers. Count attempts even when later publication fails; fixed aggregates
/// expose synchronous tail work without retaining samples.
pub fn note_checkpoint_history_tail(bytes: u64) {
    CHECKPOINT_HISTORY_TAIL_BYTES.fetch_add(bytes, Ordering::Relaxed);
    MAX_CHECKPOINT_HISTORY_TAIL_BYTES.fetch_max(bytes, Ordering::Relaxed);
}

/// Captured window attempts, including a later failed encode/publication.
/// These bounded, content-free totals are distinct from the in-lock tail.
pub(crate) fn note_rotation_window(transactions: usize, bytes: usize) {
    ROTATION_WINDOW_TRANSACTIONS.fetch_add(transactions as u64, Ordering::Relaxed);
    ROTATION_WINDOW_BYTES.fetch_add(bytes as u64, Ordering::Relaxed);
    MAX_ROTATION_WINDOW_BYTES.fetch_max(bytes as u64, Ordering::Relaxed);
}

// Stage instrumentation for commit-path attribution; printed at daemon shutdown
// when TOFU_DB_FSYNC_STATS is set. All counters are lock-free atomics.
pub const STAGE_SEMANTIC: usize = 0;
pub const STAGE_PREPARE: usize = 1;
pub const STAGE_ENGINE_COMMIT: usize = 2;
pub const STAGE_APPLY: usize = 3;
pub const STAGE_APPLY_BATCH: usize = 4;
pub const STAGE_WAL: usize = 5;
pub const STAGE_ENCODE: usize = 6;
pub const STAGE_CARRIED: usize = 7;
pub const STAGE_IR_STEPS: usize = 8;
pub const STAGE_IR_COMMIT: usize = 9;
pub const STAGE_LOAD_NODE: usize = 10;
pub const STAGE_STORE_PAGE: usize = 11;
pub const STAGE_PUT_DEFERRED: usize = 12;
pub const STAGE_BLOCK_GET: usize = 13;
pub const STAGE_BUILDER: usize = 14;
pub const STAGE_CHECKPOINT_IF: usize = 15;
pub const STAGE_COMMIT_TAIL: usize = 16;
pub const STAGE_GET_PENDING: usize = 17;
pub const STAGE_GET_CACHE: usize = 18;
pub const STAGE_GET_UNCACHED_OK: usize = 19;
pub const STAGE_GET_UNCACHED_MISS: usize = 20;
pub const STAGE_PUT_STAGE: usize = 21;
pub const STAGE_DRAIN_STEP: usize = 22;
pub const STAGE_STEP_CHUNK: usize = 23;
pub const STAGE_STEP_MANIFEST: usize = 24;
pub const STAGE_CK_MATERIALIZE: usize = 25;
pub const STAGE_CK_SEGMENTS: usize = 26;
pub const STAGE_CK_MANIFEST: usize = 27;
pub const STAGE_CK_WAL: usize = 28;
pub const STAGE_CK_CONTROL: usize = 29;
pub const STAGE_CK_REMOVE: usize = 30;
pub const STAGE_FLUSH_DIRTY: usize = 31;
pub const STAGE_CK_DRAIN: usize = 32;
pub const STAGE_CK_CLEAR: usize = 33;
pub const STAGE_CK_WAIT: usize = 34;
pub const STAGE_CK_SWAP: usize = 35;
pub const STAGE_PACK_ADOPT: usize = 36;
pub const STAGE_HISTORY_STAGE: usize = 37;
pub const STAGE_CATALOG_PUBLISH: usize = 38;
pub const STAGE_CATALOG_WRITE: usize = 39;
pub const STAGE_CATALOG_INSTALL: usize = 40;
pub const STAGE_CATALOG_CONTROL: usize = 41;
pub const STAGE_PACK_RETIRE: usize = 42;
pub const STAGE_PACK_FILTER: usize = 43;
pub const STAGE_TA_CLAIMS: usize = 44;
pub const STAGE_TA_HEADER: usize = 45;
pub const STAGE_TA_LANE_READS: usize = 46;
pub const STAGE_TA_PROJECTION: usize = 47;
pub const STAGE_TA_DOCUMENT: usize = 48;
pub const STAGE_TA_INDEXES: usize = 49;
pub const STAGE_TA_DIRTY: usize = 50;
pub const STAGE_TA_SYNC: usize = 51;
pub const STAGE_TA_RECEIPT: usize = 52;
pub const STAGE_TA_OUTBOX: usize = 53;
pub const STAGE_TA_PREPARE_MERGE: usize = 54;
pub const STAGE_TA_PREPARE_FAST: usize = 55;
pub const STAGE_AUTHORITY_WAIT: usize = 56;
pub const STAGE_COMMAND_EXECUTE: usize = 57;
pub const STAGE_WAL_BARRIER_WAIT: usize = 58;
pub const STAGE_TURN_EVENT_LOAD: usize = 59;
pub const STAGE_TURN_EVENT_PROJECTION: usize = 60;
pub const STAGE_TURN_EVENT_STORE: usize = 61;
pub const STAGE_TURN_EVENT_PUBLISH: usize = 62;
pub const STAGE_OVERLAY_POINT: usize = 63;
pub const STAGE_OVERLAY_VIEW: usize = 64;
pub const STAGE_ROTATION_PARK: usize = 65;
pub const STAGE_BLOB_ENCODE: usize = 66;
pub const STAGE_BLOB_STAGE: usize = 67;
pub const STAGE_PARALLEL_PREPARE: usize = 68;
pub const STAGE_PARALLEL_COMMIT: usize = 69;
pub const STAGE_PARALLEL_FALLBACK: usize = 70;
pub const STAGE_PARALLEL_SNAPSHOT_BUSY: usize = 71;
pub const STAGE_PARALLEL_SNAPSHOT_ROTATION: usize = 72;
pub const STAGE_PARALLEL_PREPARATION_BUSY: usize = 73;
pub const STAGE_PARALLEL_PREPARATION_ROTATION: usize = 74;
pub const STAGE_PARALLEL_COMMIT_BUSY: usize = 75;
pub const STAGE_PARALLEL_COMMIT_ROTATION: usize = 76;
pub const STAGE_PARALLEL_SNAPSHOT_WAIT: usize = 77;
pub const STAGE_HISTORY_DECODE: usize = 78;
pub const STAGE_HISTORY_LOOKUP: usize = 79;
pub const STAGE_HISTORY_RANGE: usize = 80;
pub const STAGE_OVERLAY_REPLAY: usize = 81;
pub const STAGE_OVERLAY_SELECT: usize = 82;
pub const STAGE_OVERLAY_CARRY_BUDGET: usize = 83;
pub const STAGE_OVERLAY_PUBLISH: usize = 84;
pub const STAGE_CHECKPOINT_TOTAL: usize = 85;
pub const STAGE_VERIFY_CONTROL: usize = 86;
pub const STAGE_VERIFY_WAL: usize = 87;
pub const STAGE_VERIFY_OPEN: usize = 88;
pub const STAGE_VERIFY_MERGE: usize = 89;
pub const STAGE_VERIFY_PAYLOAD: usize = 90;
pub const STAGE_VERIFY_LOOSE: usize = 91;
pub const STAGE_VERIFY_GRAPH: usize = 92;
pub const STAGE_VERIFY_HISTORY: usize = 93;
pub const STAGE_VERIFY_CLOSE: usize = 94;
pub const STAGE_EMBEDDED_STOP_TOTAL: usize = 95;
pub const STAGE_EMBEDDED_STOP_MAINTENANCE: usize = 96;
pub const STAGE_EMBEDDED_STOP_PACKER: usize = 97;
pub const STAGE_EMBEDDED_STOP_GC: usize = 98;
pub const STAGE_EMBEDDED_STOP_BACKUP: usize = 99;
pub const STAGE_EMBEDDED_STOP_WATCH: usize = 100;
pub const STAGE_EMBEDDED_STOP_ARCHIVE: usize = 101;
pub const STAGE_EMBEDDED_STOP_ADOPTION: usize = 102;
pub const STAGE_EMBEDDED_STOP_ROTATION: usize = 103;
pub const STAGE_EMBEDDED_STOP_CHECKPOINT: usize = 104;
pub const STAGE_EMBEDDED_STOP_BARRIER: usize = 105;
pub const STAGE_PACK_FILTER_INCOMPLETE: usize = 106;
pub const STAGE_OVERLAY_PREPARE_CAPTURE: usize = 107;
pub const STAGE_OVERLAY_PREPARE_ENCODE: usize = 108;
pub const STAGE_OVERLAY_PREPARE_PUBLISH: usize = 109;
pub const STAGE_OVERLAY_PREPARE_STALE: usize = 110;
pub const STAGE_OVERLAY_PREPARE_BUDGET_WAIT: usize = 111;
pub const STAGE_PROJECTION_HEAD_MATERIALIZE: usize = 112;
pub const STAGE_PROJECTION_HEAD_CHECKPOINT: usize = 113;
pub const STAGE_PROJECTION_HEAD_PATCH_LOAD: usize = 114;
pub const STAGE_PROJECTION_HEAD_PATCH_APPLY: usize = 115;
pub const STAGE_WAL_BARRIER_SYNC: usize = 116;
pub const STAGE_WAL_BARRIER_RESCHEDULE: usize = 117;
pub const STAGE_ENTITY_PAGE_CACHE_READ_WAIT: usize = 118;
pub const STAGE_ENTITY_PAGE_SOURCE_READ: usize = 119;
pub const STAGE_ENTITY_PAGE_DECODE: usize = 120;
pub const STAGE_ENTITY_PAGE_CACHE_WRITE_WAIT: usize = 121;
pub const STAGE_BLOCK_SHARED_READ: usize = 122;
pub const STAGE_BLOCK_PENDING_WAIT: usize = 123;
pub const STAGE_BLOCK_CACHE_WAIT: usize = 124;
pub const STAGE_BLOCK_CACHE_ACCESS: usize = 125;
pub const STAGE_BLOCK_HISTORY_RESOLVE: usize = 126;
pub const STAGE_BLOCK_HISTORY_SOURCE_OPEN: usize = 127;
pub const STAGE_BLOCK_HISTORY_SOURCE_READ: usize = 128;
pub const STAGE_BLOCK_HISTORY_SOURCE_WAIT: usize = 129;
pub const STAGE_BLOCK_CONTENT_VERIFY: usize = 130;
pub const STAGE_BLOCK_MATERIALIZED_RESOLVE: usize = 131;
pub const STAGE_BLOCK_LOOSE_READ: usize = 132;
pub const STAGE_ROTATION_PREPARE_WAIT: usize = 133;
pub const STAGE_ROTATION_PREFIX_ENCODE: usize = 134;
pub const STAGE_ROTATION_INSTALL_WAIT: usize = 135;
pub const STAGE_ROTATION_WINDOW_CAPTURE: usize = 136;
pub const STAGE_ROTATION_WINDOW_ENCODE: usize = 137;
pub const STAGE_ROTATION_INSTALL_DIRTY_CONTROL: usize = 138;
pub const STAGE_ROTATION_INSTALL_WINDOW: usize = 139;
pub const STAGE_ROTATION_INSTALL_MANIFEST: usize = 140;
pub const STAGE_ROTATION_INSTALL_WAL: usize = 141;
pub const STAGE_ROTATION_INSTALL_CONTROL: usize = 142;
pub const STAGE_ROTATION_INSTALL_RELEASE: usize = 143;
pub const STAGE_ROTATION_GENERATION_HANDOVER: usize = 144;
pub const STAGE_ROTATION_PREDECESSOR_INSTALL: usize = 145;
pub const STAGE_EMBEDDED_ENVELOPE_DECODE: usize = 146;
pub const STAGE_EMBEDDED_RESPONSE_ENCODE: usize = 147;
pub const STAGE_EMBEDDED_WAIT_RESPONSE: usize = 148;
pub const STAGE_EMBEDDED_RESPONSE_COPY: usize = 149;
pub const STAGE_EMBEDDED_JOB_SUBMIT: usize = 150;
pub const STAGE_EMBEDDED_RESPONSE_DELIVER: usize = 151;
pub const STAGE_SEMANTIC_RESPONSE_VALIDATE: usize = 152;
pub const STAGE_SEMANTIC_TRANSACTION_COMPILE: usize = 153;
pub const STAGE_DOCUMENT_VALUE_MATERIALIZE: usize = 154;
pub const STAGE_DOCUMENT_JSON_CANONICAL: usize = 155;
pub const STAGE_DOCUMENT_PROJECT_ENCODE: usize = 156;
pub const STAGE_DOCUMENT_JSON_FALLBACK: usize = 157;
pub const STAGE_BLOB_MANIFEST_OPEN: usize = 158;
pub const STAGE_BLOB_CHUNK_SOURCE: usize = 159;
pub const STAGE_BLOB_CHUNK_DECODE: usize = 160;
pub const STAGE_BLOB_CHUNK_HASH: usize = 161;
pub const STAGE_BLOB_CONTENT_HASH: usize = 162;
pub const STAGE_BLOB_BUFFER_COLLECT: usize = 163;
pub const STAGE_BLOB_CHUNK_CACHE_ACCESS: usize = 164;
pub const STAGE_EMBEDDED_PAYLOAD_VALIDATE: usize = 165;
pub const STAGE_EMBEDDED_NUMERIC_COMPATIBILITY: usize = 166;
pub const STAGE_EMBEDDED_VALUE_FALLBACK: usize = 167;
pub const STAGE_EMBEDDED_ENVELOPE_BUILD: usize = 168;
pub const STAGE_EMBEDDED_PAYLOAD_PROOF: usize = 169;
pub const STAGE_BLOB_WHOLE_PROOF_LOOKUP: usize = 170;
pub const STAGE_BLOB_WHOLE_PROOF_DEPENDENCIES: usize = 171;
pub const STAGE_BLOB_WHOLE_PROOF_STORE: usize = 172;
pub const STAGE_PUBLISHED_READ_LOOKUP: usize = 173;
pub const STAGE_PUBLISHED_READ_PIN: usize = 174;
pub const STAGE_PUBLISHED_READ_CLONE: usize = 175;
pub const STAGE_PUBLISHED_READ_PUBLISH: usize = 176;
pub const STAGE_EMBEDDED_BINDINGS: usize = 177;
pub const STAGE_SNAPSHOT_PIN_RELEASE: usize = 178;
pub const STAGE_PIN_REGISTRY_WAIT: usize = 179;
pub const STAGE_PIN_BYTE_RESERVE: usize = 180;
pub const STAGE_PIN_BYTE_RELEASE: usize = 181;
pub const STAGE_EMBEDDED_REPLY_ACQUIRE: usize = 182;
pub const STAGE_EMBEDDED_REPLY_RECYCLE: usize = 183;
pub const STAGE_EMBEDDED_QUEUE_LOCK: usize = 184;
pub const STAGE_EMBEDDED_QUEUE_ADMIT: usize = 185;
pub const STAGE_EMBEDDED_QUEUE_NOTIFY: usize = 186;
pub const STAGE_EMBEDDED_HANDLE_LOCK: usize = 187;
pub const STAGE_IR_RESULT_EXTRACT: usize = 188;
pub const STAGE_IR_RECEIPT_RESPONSE_POLICY: usize = 189;
pub const STAGE_SEMANTIC_PAYLOAD_DECODE: usize = 190;
pub const STAGE_ENTITY_POINT_PREPARE: usize = 191;
pub const STAGE_ENTITY_POINT_RESOLVE: usize = 192;
pub const STAGE_ENTITY_POINT_RETAIN: usize = 193;
pub const STAGE_EMBEDDED_WORKER_QUEUE_LOCK: usize = 194;
pub const STAGE_EMBEDDED_WORKER_QUEUE_CLAIM: usize = 195;
pub const STAGE_EMBEDDED_WORKER_REGISTER: usize = 196;
pub const STAGE_EMBEDDED_WORKER_EXECUTE: usize = 197;
pub const STAGE_EMBEDDED_QUEUE_WAIT: usize = 198;
pub const STAGE_EMBEDDED_REPLY_HANDOFF: usize = 199;
pub const STAGE_PROTOCOL_FRAME_AFTER_PREFIX: usize = 200;
pub const STAGE_PROTOCOL_FRAME_DECODE: usize = 201;
pub const STAGE_PREPARED_BLOB_PUBLISH: usize = 202;
pub const STAGE_RECEIPT_ENCODE: usize = 203;
pub const STAGE_RECEIPT_COMPRESS: usize = 204;
const STAGE_COUNT: usize = 205;
pub const STAGE_NAMES: [&str; STAGE_COUNT] = [
    "semantic",
    "prepare",
    "engine_commit",
    "apply",
    "apply_batch",
    "wal",
    "encode",
    "carried",
    "ir_steps",
    "ir_commit",
    "load_node",
    "store_page",
    "put_deferred",
    "block_get",
    "builder",
    "checkpoint_if",
    "commit_tail",
    "get_pending",
    "get_cache",
    "get_unc_ok",
    "get_unc_miss",
    "put_stage",
    "drain_step",
    "step_chunk",
    "step_manifest",
    "ck_body",
    "ck_segments",
    "ck_manifest",
    "ck_wal",
    "ck_control",
    "ck_remove",
    "flush_dirty",
    "ck_drain",
    "ck_clear",
    "ck_wait",
    "ck_swap",
    "pack_adopt",
    "history_stage",
    "catalog_publish",
    "catalog_write",
    "catalog_install",
    "catalog_control",
    "pack_retire",
    "pack_filter",
    "ta_claims",
    "ta_header",
    "ta_lane_reads",
    "ta_projection",
    "ta_document",
    "ta_indexes",
    "ta_dirty",
    "ta_sync",
    "ta_receipt",
    "ta_outbox",
    "ta_prepare_merge",
    "ta_prepare_fast",
    "authority_wait",
    "command_execute",
    "wal_barrier_wait",
    "turn_event_load",
    "turn_event_projection",
    "turn_event_store",
    "turn_event_publish",
    "overlay_point",
    "overlay_view",
    "rotation_park",
    "blob_encode",
    "blob_stage",
    "parallel_prepare",
    "parallel_commit",
    "parallel_fallback",
    "parallel_snapshot_busy",
    "parallel_snapshot_rotation",
    "parallel_preparation_busy",
    "parallel_preparation_rotation",
    "parallel_commit_busy",
    "parallel_commit_rotation",
    "parallel_snapshot_wait",
    "history_decode",
    "history_lookup",
    "history_range",
    "overlay_replay",
    "overlay_select",
    "overlay_carry_budget",
    "overlay_publish",
    "checkpoint_total",
    "verify_control",
    "verify_wal",
    "verify_open",
    "verify_merge",
    "verify_payload",
    "verify_loose",
    "verify_graph",
    "verify_history",
    "verify_close",
    "embedded_stop_total",
    "embedded_stop_maintenance",
    "embedded_stop_packer",
    "embedded_stop_gc",
    "embedded_stop_backup",
    "embedded_stop_watch",
    "embedded_stop_archive",
    "embedded_stop_adoption",
    "embedded_stop_rotation",
    "embedded_stop_checkpoint",
    "embedded_stop_barrier",
    "pack_filter_incomplete",
    "overlay_prepare_capture",
    "overlay_prepare_encode",
    "overlay_prepare_publish",
    "overlay_prepare_stale",
    "overlay_prepare_budget_wait",
    "projection_head_materialize",
    "projection_head_checkpoint",
    "projection_head_patch_load",
    "projection_head_patch_apply",
    "wal_barrier_sync",
    "wal_barrier_reschedule",
    "entity_page_cache_read_wait",
    "entity_page_source_read",
    "entity_page_decode",
    "entity_page_cache_write_wait",
    "block_shared_read",
    "block_pending_wait",
    "block_cache_wait",
    "block_cache_access",
    "block_history_resolve",
    "block_history_source_open",
    "block_history_source_read",
    "block_history_source_wait",
    "block_content_verify",
    "block_materialized_resolve",
    "block_loose_read",
    "rotation_prepare_wait",
    "rotation_prefix_encode",
    "rotation_install_wait",
    "rotation_window_capture",
    "rotation_window_encode",
    "rotation_install_dirty_control",
    "rotation_install_window",
    "rotation_install_manifest",
    "rotation_install_wal",
    "rotation_install_control",
    "rotation_install_release",
    "rotation_generation_handover",
    "rotation_predecessor_install",
    "embedded_envelope_decode",
    "embedded_response_encode",
    "embedded_wait_response",
    "embedded_response_copy",
    "embedded_job_submit",
    "embedded_response_deliver",
    "semantic_response_validate",
    "semantic_transaction_compile",
    "document_value_materialize",
    "document_json_canonical",
    "document_project_encode",
    "document_json_fallback",
    "blob_manifest_open",
    "blob_chunk_source",
    "blob_chunk_decode",
    "blob_chunk_hash",
    "blob_content_hash",
    "blob_buffer_collect",
    "blob_chunk_cache_access",
    "embedded_payload_validate",
    "embedded_numeric_compatibility",
    "embedded_value_fallback",
    "embedded_envelope_build",
    "embedded_payload_proof",
    "blob_whole_proof_lookup",
    "blob_whole_proof_dependencies",
    "blob_whole_proof_store",
    "published_read_lookup",
    "published_read_pin",
    "published_read_clone",
    "published_read_publish",
    "embedded_bindings",
    "snapshot_pin_release",
    "pin_registry_wait",
    "pin_byte_reserve",
    "pin_byte_release",
    "embedded_reply_acquire",
    "embedded_reply_recycle",
    "embedded_queue_lock",
    "embedded_queue_admit",
    "embedded_queue_notify",
    "embedded_handle_lock",
    "ir_result_extract",
    "ir_receipt_response_policy",
    "semantic_payload_decode",
    "entity_point_prepare",
    "entity_point_resolve",
    "entity_point_retain",
    "embedded_worker_queue_lock",
    "embedded_worker_queue_claim",
    "embedded_worker_register",
    "embedded_worker_execute",
    "embedded_queue_wait",
    "embedded_reply_handoff",
    "protocol_frame_after_prefix",
    "protocol_frame_decode",
    "prepared_blob_publish",
    "receipt_encode",
    "receipt_compress",
];

#[derive(Clone, Copy)]
pub(crate) enum ParallelFallbackPhase { Snapshot, Preparation, Commit }

impl ParallelFallbackPhase {
    fn stage(self, error: &std::io::Error) -> usize {
        let rotation = crate::engine::is_rotation_backpressure(error);
        match (self, rotation) {
            (Self::Snapshot, false) => STAGE_PARALLEL_SNAPSHOT_BUSY,
            (Self::Snapshot, true) => STAGE_PARALLEL_SNAPSHOT_ROTATION,
            (Self::Preparation, false) => STAGE_PARALLEL_PREPARATION_BUSY,
            (Self::Preparation, true) => STAGE_PARALLEL_PREPARATION_ROTATION,
            (Self::Commit, false) => STAGE_PARALLEL_COMMIT_BUSY,
            (Self::Commit, true) => STAGE_PARALLEL_COMMIT_ROTATION,
        }
    }
}

/// Count the branch actually taken, without interpreting every WouldBlock as
/// an OCC conflict. These are event counts (zero elapsed time), not spans.
pub(crate) fn note_parallel_fallback(phase: ParallelFallbackPhase, error: &std::io::Error) {
    crate::request_profile::fallback();
    note_stage_elapsed(STAGE_PARALLEL_FALLBACK, 0);
    note_stage_elapsed(phase.stage(error), 0);
}

#[test]
fn parallel_fallback_distinguishes_phase_and_rotation_without_guessing_conflicts() {
    use ParallelFallbackPhase::*;
    let busy = std::io::Error::new(std::io::ErrorKind::WouldBlock, "retryable admission");
    let rotation = crate::engine::rotation_backpressure();
    for (phase, ordinary_stage, rotation_stage) in [
        (Snapshot, STAGE_PARALLEL_SNAPSHOT_BUSY, STAGE_PARALLEL_SNAPSHOT_ROTATION),
        (Preparation, STAGE_PARALLEL_PREPARATION_BUSY, STAGE_PARALLEL_PREPARATION_ROTATION),
        (Commit, STAGE_PARALLEL_COMMIT_BUSY, STAGE_PARALLEL_COMMIT_ROTATION),
    ] {
        assert_eq!(phase.stage(&busy), ordinary_stage);
        assert_eq!(phase.stage(&rotation), rotation_stage);
    }
}
static STAGE_NANOS: [AtomicU64; STAGE_COUNT] = [const { AtomicU64::new(0) }; STAGE_COUNT];
static STAGE_CALLS: [AtomicU64; STAGE_COUNT] = [const { AtomicU64::new(0) }; STAGE_COUNT];
static COMPONENT_STAGE_NANOS: [[AtomicU64; STAGE_COUNT]; COMPONENT_COUNT] =
    [const { [const { AtomicU64::new(0) }; STAGE_COUNT] }; COMPONENT_COUNT];
static COMPONENT_STAGE_CALLS: [[AtomicU64; STAGE_COUNT]; COMPONENT_COUNT] =
    [const { [const { AtomicU64::new(0) }; STAGE_COUNT] }; COMPONENT_COUNT];

pub struct StageTimer {
    profile: Option<crate::request_profile::SpanToken>,
    stage: usize,
    start: std::time::Instant,
}

impl StageTimer {
    /// Const specialization removes even inactive timer storage from hot
    /// unprofiled cache reads while keeping their data algorithm shared.
    #[inline(always)]
    pub(crate) fn conditional<const MEASURE: bool>(stage: usize) -> Option<Self> {
        if MEASURE { Some(Self::new(stage)) } else { None }
    }

    /// Fine-grained read probes share the existing launch-time profiling
    /// opt-in. Disabled diagnostics avoid clock reads and counter contention.
    pub(crate) fn diagnostic(stage: usize) -> Option<Self> {
        crate::request_profile::enabled().then(|| Self::new(stage))
    }

    pub fn new(stage: usize) -> Self {
        Self {
            stage,
            start: std::time::Instant::now(),
            profile: crate::request_profile::enter(stage),
        }
    }
}

impl Drop for StageTimer {
    fn drop(&mut self) {
        let nanos = self.start.elapsed().as_nanos() as u64;
        crate::request_profile::exit(self.profile.take(), nanos);
        STAGE_NANOS[self.stage].fetch_add(nanos, Ordering::Relaxed);
        STAGE_CALLS[self.stage].fetch_add(1, Ordering::Relaxed);
        let component = current_component();
        COMPONENT_STAGE_NANOS[component][self.stage].fetch_add(nanos, Ordering::Relaxed);
        COMPONENT_STAGE_CALLS[component][self.stage].fetch_add(1, Ordering::Relaxed);
    }
}

pub fn stage_report() -> String {
    let mut out = String::new();
    for (index, name) in STAGE_NAMES.iter().enumerate() {
        let nanos = STAGE_NANOS[index].load(Ordering::Relaxed);
        let calls = STAGE_CALLS[index].load(Ordering::Relaxed);
        out.push_str(&format!(
            " {name}: calls={calls} total={:.1}ms avg={:.1}us\n",
            nanos as f64 / 1e6,
            nanos as f64 / 1e3 / calls.max(1) as f64
        ));
    }
    out
}

pub fn stage_nanos(stage: usize) -> u64 {
    STAGE_NANOS[stage].load(Ordering::Relaxed)
}

pub fn stage_calls(stage: usize) -> u64 {
    STAGE_CALLS[stage].load(Ordering::Relaxed)
}

pub fn component_stage_nanos(component: usize, stage: usize) -> u64 {
    COMPONENT_STAGE_NANOS[component][stage].load(Ordering::Relaxed)
}

pub fn component_stage_calls(component: usize, stage: usize) -> u64 {
    COMPONENT_STAGE_CALLS[component][stage].load(Ordering::Relaxed)
}

pub fn note_stage_elapsed(stage: usize, nanos: u64) {
    if nanos == 0 { crate::request_profile::event(stage); }
    STAGE_NANOS[stage].fetch_add(nanos, Ordering::Relaxed);
    STAGE_CALLS[stage].fetch_add(1, Ordering::Relaxed);
    let component = current_component();
    COMPONENT_STAGE_NANOS[component][stage].fetch_add(nanos, Ordering::Relaxed);
    COMPONENT_STAGE_CALLS[component][stage].fetch_add(1, Ordering::Relaxed);
}

/// Per-component totals for one stage family, rendered beside the flat
/// report so foreground cost is never confused with background progress.
pub fn component_stage_report() -> String {
    let mut out = String::new();
    for (component, name) in COMPONENT_NAMES.iter().enumerate() {
        let total_nanos: u64 = (0..STAGE_COUNT)
            .map(|stage| component_stage_nanos(component, stage))
            .sum();
        if total_nanos == 0 {
            continue;
        }
        out.push_str(&format!(" [{name}]: total={:.1}ms\n", total_nanos as f64 / 1e6));
        let mut stages = (0..STAGE_COUNT)
            .map(|stage| {
                (
                    STAGE_NAMES[stage],
                    component_stage_calls(component, stage),
                    component_stage_nanos(component, stage),
                )
            })
            .filter(|(_, calls, _)| *calls > 0)
            .collect::<Vec<_>>();
        stages.sort_by_key(|(_, _, nanos)| std::cmp::Reverse(*nanos));
        for (stage, calls, nanos) in stages.into_iter().take(6) {
            out.push_str(&format!(
                "   {stage}: calls={calls} total={:.1}ms avg={:.1}us\n",
                nanos as f64 / 1e6,
                nanos as f64 / 1e3 / calls.max(1) as f64
            ));
        }
    }
    out
}

pub fn report() -> String {
    format!(
        "publish_control={} wal_append={} flush_deferred={} checkpoint={} checkpoint_history_tail_bytes={} max_checkpoint_history_tail_bytes={} drop_flush_failures={} wal_encoded_bytes={} overlay_merge_commits={} overlay_merged_entries={} overlay_merged_positions={} overlay_superseded_positions={} overlay_materialized_bytes={} overlay_materialized_blocks={} overlay_wal_pressure_attempts={} overlay_wal_pressure_pending_entries={}",
        PUBLISH_CONTROL.load(Ordering::Relaxed),
        WAL_APPEND.load(Ordering::Relaxed),
        FLUSH_DEFERRED.load(Ordering::Relaxed),
        CHECKPOINT.load(Ordering::Relaxed),
        CHECKPOINT_HISTORY_TAIL_BYTES.load(Ordering::Relaxed),
        MAX_CHECKPOINT_HISTORY_TAIL_BYTES.load(Ordering::Relaxed),
        DROP_FLUSH_FAILURES.load(Ordering::Relaxed),
        WAL_ENCODED_BYTES.load(Ordering::Relaxed),
        OVERLAY_MERGE_COMMITS.load(Ordering::Relaxed),
        OVERLAY_MERGED_ENTRIES.load(Ordering::Relaxed),
        OVERLAY_MERGED_POSITIONS.load(Ordering::Relaxed),
        OVERLAY_SUPERSEDED_POSITIONS.load(Ordering::Relaxed),
        OVERLAY_MATERIALIZED_BYTES.load(Ordering::Relaxed),
        OVERLAY_MATERIALIZED_BLOCKS.load(Ordering::Relaxed),
        OVERLAY_WAL_PRESSURE_ATTEMPTS.load(Ordering::Relaxed),
        OVERLAY_WAL_PRESSURE_PENDING_ENTRIES.load(Ordering::Relaxed)
    )
}

/// The same fixed counters as a serializable snapshot for `system.metrics`.
#[derive(Clone, Copy, Debug, Default, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DurabilityCounters {
    pub publish_control: u64,
    pub wal_append: u64,
    pub flush_deferred: u64,
    pub checkpoint: u64,
    pub checkpoint_history_tail_bytes: u64,
    pub max_checkpoint_history_tail_bytes: u64,
    pub rotation_window_transactions: u64,
    pub rotation_window_bytes: u64,
    pub max_rotation_window_bytes: u64,
    pub drop_flush_failures: u64,
    pub wal_encoded_bytes: u64,
    pub projection_checkpoint_bytes: u64,
    pub projection_patch_bytes: u64,
    pub projection_patches_applied: u64,
    pub history_decode_attempts: u64,
    pub history_decode_source_bytes: u64,
    pub history_decoded_transactions: u64,
    pub overlay_merge_commits: u64,
    pub overlay_merged_entries: u64,
    pub overlay_merged_positions: u64,
    pub overlay_superseded_positions: u64,
    pub overlay_materialized_bytes: u64,
    pub overlay_materialized_blocks: u64,
    pub overlay_wal_pressure_attempts: u64,
    pub overlay_wal_pressure_pending_entries: u64,
}

pub fn counters() -> DurabilityCounters {
    DurabilityCounters {
        publish_control: PUBLISH_CONTROL.load(Ordering::Relaxed),
        wal_append: WAL_APPEND.load(Ordering::Relaxed),
        flush_deferred: FLUSH_DEFERRED.load(Ordering::Relaxed),
        checkpoint: CHECKPOINT.load(Ordering::Relaxed),
        checkpoint_history_tail_bytes: CHECKPOINT_HISTORY_TAIL_BYTES.load(Ordering::Relaxed),
        rotation_window_transactions: ROTATION_WINDOW_TRANSACTIONS.load(Ordering::Relaxed),
        rotation_window_bytes: ROTATION_WINDOW_BYTES.load(Ordering::Relaxed),
        max_rotation_window_bytes: MAX_ROTATION_WINDOW_BYTES.load(Ordering::Relaxed),
        max_checkpoint_history_tail_bytes: MAX_CHECKPOINT_HISTORY_TAIL_BYTES
            .load(Ordering::Relaxed),
        drop_flush_failures: DROP_FLUSH_FAILURES.load(Ordering::Relaxed),
        wal_encoded_bytes: WAL_ENCODED_BYTES.load(Ordering::Relaxed),
        projection_checkpoint_bytes: PROJECTION_CHECKPOINT_BYTES.load(Ordering::Relaxed),
        projection_patch_bytes: PROJECTION_PATCH_BYTES.load(Ordering::Relaxed),
        projection_patches_applied: PROJECTION_PATCHES_APPLIED.load(Ordering::Relaxed),
        history_decode_attempts: HISTORY_DECODE_ATTEMPTS.load(Ordering::Relaxed),
        history_decode_source_bytes: HISTORY_DECODE_SOURCE_BYTES.load(Ordering::Relaxed),
        history_decoded_transactions: HISTORY_DECODED_TRANSACTIONS.load(Ordering::Relaxed),
        overlay_merge_commits: OVERLAY_MERGE_COMMITS.load(Ordering::Relaxed),
        overlay_merged_entries: OVERLAY_MERGED_ENTRIES.load(Ordering::Relaxed),
        overlay_merged_positions: OVERLAY_MERGED_POSITIONS.load(Ordering::Relaxed),
        overlay_superseded_positions: OVERLAY_SUPERSEDED_POSITIONS.load(Ordering::Relaxed),
        overlay_materialized_bytes: OVERLAY_MATERIALIZED_BYTES.load(Ordering::Relaxed),
        overlay_materialized_blocks: OVERLAY_MATERIALIZED_BLOCKS.load(Ordering::Relaxed),
        overlay_wal_pressure_attempts: OVERLAY_WAL_PRESSURE_ATTEMPTS.load(Ordering::Relaxed),
        overlay_wal_pressure_pending_entries: OVERLAY_WAL_PRESSURE_PENDING_ENTRIES.load(Ordering::Relaxed),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stage_timers_land_on_the_calling_threads_component() {
        let stage = STAGE_SEMANTIC;
        let before_foreground = component_stage_calls(COMPONENT_FOREGROUND, stage);
        let before_maintenance = component_stage_calls(COMPONENT_MAINTENANCE, stage);
        let before_total = stage_calls(stage);
        set_current_component(COMPONENT_MAINTENANCE);
        drop(StageTimer::new(stage));
        set_current_component(COMPONENT_FOREGROUND);
        drop(StageTimer::new(stage));
        assert_eq!(
            component_stage_calls(COMPONENT_MAINTENANCE, stage),
            before_maintenance + 1
        );
        assert_eq!(
            component_stage_calls(COMPONENT_FOREGROUND, stage),
            before_foreground + 1
        );
        assert_eq!(stage_calls(stage), before_total + 2);
        assert!(component_stage_report().contains("[foreground]"));
    }
}

/// Shared mount-aware background-I/O gate. Any background fsync issued
/// while the foreground is hot lands on user-visible latency at p99 (the
/// mount serializes concurrent fsyncs), so every background write stream —
/// packer, pack adoption, search projection, online GC, online backup —
/// consults this before publishing. Hotness covers both write commits AND
/// read/query requests: a read-heavy phase contends for the same mount,
/// so background work defers to genuine idle gaps. Sampling is lock-free
/// over the stage counters and valid within one sampling window.
static FOREGROUND_REQUESTS: std::sync::atomic::AtomicU64 =
    std::sync::atomic::AtomicU64::new(0);

/// Records one foreground request entering the dispatch path (any lane).
pub fn note_foreground_request() {
    FOREGROUND_REQUESTS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
}

static FOREGROUND_WAIT_NANOS: AtomicU64 = AtomicU64::new(0);

/// Dispatch admission latency, separate from request and commit activity.
pub(crate) struct ForegroundWait(std::time::Instant);
impl ForegroundWait {
    pub(crate) fn start() -> Self { Self(std::time::Instant::now()) }
}
impl Drop for ForegroundWait {
    fn drop(&mut self) {
        FOREGROUND_WAIT_NANOS.fetch_add(self.0.elapsed().as_nanos().min(u64::MAX as u128) as u64, Ordering::Relaxed);
    }
}

#[derive(Clone, Copy, Debug)]
struct ForegroundCounters { requests: u64, commits: u64, wait_nanos: u64 }
fn foreground_counters() -> ForegroundCounters {
    ForegroundCounters {
        requests: FOREGROUND_REQUESTS.load(Ordering::Relaxed),
        commits: component_stage_calls(COMPONENT_FOREGROUND, STAGE_ENGINE_COMMIT),
        wait_nanos: FOREGROUND_WAIT_NANOS.load(Ordering::Relaxed),
    }
}

#[derive(Debug)]
pub struct ForegroundSampler {
    sampled_at: std::time::Instant,
    counters: ForegroundCounters,
    hot: bool,
    write_pressure: bool,
    last_progress: std::time::Instant,
}
impl ForegroundSampler {
    fn observe(&mut self, now: std::time::Instant, counters: ForegroundCounters) -> bool {
        let elapsed = now.saturating_duration_since(self.sampled_at);
        if elapsed >= std::time::Duration::from_millis(250) {
            let requests = counters.requests.saturating_sub(self.counters.requests);
            let commits = counters.commits.saturating_sub(self.counters.commits);
            let wait = counters.wait_nanos.saturating_sub(self.counters.wait_nanos);
            self.write_pressure = commits as f64 / elapsed.as_secs_f64() >= 4.0
                || wait as f64 >= elapsed.as_nanos() as f64 * 0.05;
            self.hot = requests as f64 / elapsed.as_secs_f64() >= 4.0
                || self.write_pressure;
            self.sampled_at = now;
            self.counters = counters;
        }
        self.hot
    }
}

pub fn foreground_recently_hot(last: &mut ForegroundSampler) -> bool {
    last.observe(std::time::Instant::now(), foreground_counters())
}

/// A hot foreground can postpone optional maintenance for at most two
/// seconds per sampler. Callers still enforce their bounded work slices and
/// debt thresholds; this permit guarantees progress under sustained reads.
pub(crate) fn defer_background_work(last: &mut ForegroundSampler) -> bool {
    let hot = foreground_recently_hot(last);
    defer_for_pressure(last, hot)
}

/// Search indexing must keep up with reads of its own incomplete results.
/// Read traffic alone is not evidence of contention: defer for foreground
/// writes or measured admission waits, retaining the same fairness bound.
pub(crate) fn defer_search_projection(last: &mut ForegroundSampler) -> bool {
    foreground_recently_hot(last);
    defer_for_pressure(last, last.write_pressure)
}

fn defer_for_pressure(last: &mut ForegroundSampler, hot: bool) -> bool {
    let now = std::time::Instant::now();
    if !hot || now.duration_since(last.last_progress) >= std::time::Duration::from_secs(2) {
        last.last_progress = now;
        false
    } else { true }
}

pub fn foreground_hotness_sampler() -> ForegroundSampler {
    let now = std::time::Instant::now();
    ForegroundSampler { sampled_at: now, counters: foreground_counters(), hot: false, write_pressure: false, last_progress: now }
}

#[cfg(test)]
mod foreground_sampler_tests {
    use super::*;
    #[test]
    fn independent_deltas_and_hot_decision_survive_the_sample_window() {
        let now = std::time::Instant::now();
        let initial = ForegroundCounters { requests: 10000, commits: 2, wait_nanos: 0 };
        let mut sample = ForegroundSampler { sampled_at: now, counters: initial, hot: false, write_pressure: false, last_progress: now };
        assert!(!sample.observe(now + std::time::Duration::from_millis(250), initial));
        let writes = ForegroundCounters { commits: 4, ..initial };
        assert!(sample.observe(now + std::time::Duration::from_millis(500), writes));
        assert!(sample.write_pressure);
        assert!(sample.observe(now + std::time::Duration::from_millis(510), writes));
        assert!(!sample.observe(now + std::time::Duration::from_millis(750), writes));
        let reads = ForegroundCounters { requests: 10002, ..writes };
        assert!(sample.observe(now + std::time::Duration::from_millis(1000), reads));
        assert!(!sample.write_pressure, "light polling must not starve its search index");
    }
    #[test]
    fn foreground_wait_pressure_is_independent_of_request_rate() {
        let mut sample = foreground_hotness_sampler();
        let counters = ForegroundCounters { wait_nanos: sample.counters.wait_nanos + 20_000_000, ..sample.counters };
        assert!(sample.observe(sample.sampled_at + std::time::Duration::from_millis(250), counters));
        assert!(sample.write_pressure, "contended reads must still yield to the foreground");
    }
}


// Anonymous semantic resource causes have counts, not fake elapsed spans.
#[path = "resource_exhaustion.rs"]
mod resource_exhaustion;
pub(crate) use resource_exhaustion::{note_resource_exhaustion, resource_exhaustion_snapshot};
