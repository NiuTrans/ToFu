//! Authority-contention watchdog for the serving daemon.
//!
//! One dedicated `tofu-db-auth-watch` thread probes the authority mutex
//! with `try_lock` on a fixed cadence and tracks the longest CONTINUOUS
//! contention window — exactly what an admitted foreground request
//! experiences while queued behind a holder. It never blocks a request,
//! never takes the semantic layer, and its own hold is one immediate
//! acquire/release probe.
//!
//! Why this exists (2026-09-21 FUSE brownout): every other health signal
//! (listener, maintenance, packer, online drivers) answers from observer
//! handles and stayed green while one holder kept the authority mutex for
//! 13 minutes — a thread parked in uninterruptible mount I/O inside the
//! hold. Every foreground request, including snapshot-lane reads (their
//! pin registration needs one brief hold), died at its wire deadline until
//! an operator restarted the process. The watch converts that silent
//! wedge into an observable, classifier-clean signal: `system.health`
//! flips `ready` once continuous contention exceeds
//! [`AUTHORITY_WEDGE_THRESHOLD`], and the supervisor's existing liveness
//! path recycles the daemon inside seconds. Every engine phase is
//! crash-safe at any interruption point, so recycling a wedged holder is
//! the designed recovery, never data loss.
//!
//! Under sustained load the mutex can change hands between every probe
//! without ever appearing free at the sampling instants. Foreground lock
//! acquisitions publish an authority-local counter; advancing that counter
//! proves handoff and resets contention. A single stuck holder cannot advance
//! it. Poisoning always stays unhealthy regardless of recent progress.

use std::io;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, TryLockError};
use std::time::{Duration, Instant};

use crate::authority::AuthorityDatabase;

// Fixed diagnostic vocabulary: no caller identity, request payload or dynamic
// operation labels. Metrics aggregate completed holds across this process;
// they are not a per-authority correlation or a health/admission decision.
#[derive(Clone, Copy)]
#[repr(u8)]
enum BackgroundHold { RotationPrepare, RotationInstall, PackPrepare, PackInstall,
    OverlayCapture, OverlayPublish, MaintenanceRound, HistoryInstall,
    SearchPrepare, SearchAcknowledge, SearchActivation, SearchBackfill, SearchDirtyList,
    ArchiveCapture, ArchivePublish, RotationPartial, RotationHandover, SearchActivationCapture }
const BACKGROUND_HOLDS: [(BackgroundHold, &str); 18] = [
    (BackgroundHold::RotationPrepare, "rotation.prepare"),
    (BackgroundHold::RotationInstall, "rotation.install"),
    (BackgroundHold::PackPrepare, "pack.prepare"),
    (BackgroundHold::PackInstall, "pack.install"),
    (BackgroundHold::OverlayCapture, "overlay.capture"),
    (BackgroundHold::OverlayPublish, "overlay.publish"),
    (BackgroundHold::MaintenanceRound, "maintenance.round"),
    (BackgroundHold::HistoryInstall, "history.install"),
    (BackgroundHold::SearchPrepare, "search.prepare"),
    (BackgroundHold::SearchAcknowledge, "search.acknowledge"),
    (BackgroundHold::SearchActivation, "search.activation"),
    (BackgroundHold::SearchBackfill, "search.backfill"),
    (BackgroundHold::SearchDirtyList, "search.dirty-list"),
    (BackgroundHold::ArchiveCapture, "archive.capture"),
    (BackgroundHold::ArchivePublish, "archive.publish"),
    (BackgroundHold::RotationPartial, "rotation.partial"),
    (BackgroundHold::RotationHandover, "rotation.handover"),
    (BackgroundHold::SearchActivationCapture, "search.activation-capture"),
];
fn background_hold(operation: &str) -> Option<BackgroundHold> {
    BACKGROUND_HOLDS.iter().find_map(|(hold, name)| (*name == operation).then_some(*hold))
}

/// Fixed phase vocabulary of an attributed hold, in `HOLD_PHASE_NAMES` order.
/// Each instrumented stage that runs inside the hold charges its exclusive
/// time to one phase; time outside every instrumented stage is `Other`.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub(crate) enum HoldPhase { Decode, Execute, Read, Stage, Validate, Journal, Sync, Apply,
    Merge, Retention, Publish, Wait, Other }
pub(crate) const HOLD_PHASE_NAMES: [&str; 13] = ["decode", "execute", "read", "stage",
    "validate", "journal", "sync", "apply", "merge", "retention", "publish", "wait", "other"];

/// The phase a stage charges, or None when it inherits its enclosing span's
/// phase (generic writes, wrappers, counters). Physical reads, fsyncs and
/// waits keep their own phase whatever their purpose.
pub(crate) fn hold_phase_of_stage(stage: usize) -> Option<HoldPhase> {
    use crate::fsync_stats::*;
    use crate::request_profile as profile;
    Some(match stage {
        STAGE_SEMANTIC_TRANSACTION_COMPILE | STAGE_SEMANTIC_PAYLOAD_DECODE
        | STAGE_PROTOCOL_FRAME_AFTER_PREFIX | STAGE_PROTOCOL_FRAME_DECODE
        | STAGE_EMBEDDED_ENVELOPE_DECODE | STAGE_EMBEDDED_PAYLOAD_VALIDATE
        | STAGE_EMBEDDED_NUMERIC_COMPATIBILITY | STAGE_EMBEDDED_VALUE_FALLBACK
        | STAGE_EMBEDDED_PAYLOAD_PROOF | profile::PARALLEL_COMPILE => HoldPhase::Decode,
        STAGE_SEMANTIC | STAGE_IR_STEPS | STAGE_COMMAND_EXECUTE | STAGE_PARALLEL_PREPARE
        | STAGE_TURN_EVENT_LOAD | STAGE_TURN_EVENT_PROJECTION | STAGE_TURN_EVENT_STORE
        | STAGE_TA_CLAIMS | STAGE_TA_HEADER | STAGE_TA_LANE_READS | STAGE_TA_PROJECTION
        | STAGE_TA_DOCUMENT | STAGE_TA_INDEXES | STAGE_TA_DIRTY | STAGE_TA_SYNC
        | STAGE_PROJECTION_HEAD_MATERIALIZE | STAGE_PROJECTION_HEAD_CHECKPOINT
        | STAGE_PROJECTION_HEAD_PATCH_APPLY | STAGE_DOCUMENT_VALUE_MATERIALIZE
        | STAGE_DOCUMENT_JSON_CANONICAL | STAGE_DOCUMENT_PROJECT_ENCODE
        | STAGE_DOCUMENT_JSON_FALLBACK | profile::QUERY_EXECUTE | profile::TURN_PUBLIC
        | profile::TURN_SNAPSHOT_ENCODE | profile::TURN_SNAPSHOT_WINDOW
        | profile::TURN_SNAPSHOT_ATTEMPTS => HoldPhase::Execute,
        STAGE_LOAD_NODE | STAGE_BLOCK_GET | STAGE_GET_PENDING | STAGE_GET_CACHE
        | STAGE_GET_UNCACHED_OK | STAGE_GET_UNCACHED_MISS | STAGE_OVERLAY_POINT
        | STAGE_OVERLAY_VIEW | STAGE_HISTORY_DECODE | STAGE_HISTORY_LOOKUP | STAGE_HISTORY_RANGE
        | STAGE_PROJECTION_HEAD_PATCH_LOAD | STAGE_ENTITY_PAGE_SOURCE_READ
        | STAGE_ENTITY_PAGE_DECODE | STAGE_BLOCK_SHARED_READ | STAGE_BLOCK_CACHE_ACCESS
        | STAGE_BLOCK_HISTORY_RESOLVE | STAGE_BLOCK_HISTORY_SOURCE_OPEN
        | STAGE_BLOCK_HISTORY_SOURCE_READ | STAGE_BLOCK_CONTENT_VERIFY
        | STAGE_BLOCK_MATERIALIZED_RESOLVE | STAGE_BLOCK_LOOSE_READ | STAGE_BLOB_MANIFEST_OPEN
        | STAGE_BLOB_CHUNK_SOURCE | STAGE_BLOB_CHUNK_DECODE | STAGE_BLOB_CHUNK_HASH
        | STAGE_BLOB_CONTENT_HASH | STAGE_BLOB_BUFFER_COLLECT | STAGE_BLOB_CHUNK_CACHE_ACCESS
        | STAGE_BLOB_WHOLE_PROOF_LOOKUP | STAGE_BLOB_WHOLE_PROOF_DEPENDENCIES
        | STAGE_BLOB_WHOLE_PROOF_STORE | STAGE_PUBLISHED_READ_LOOKUP | STAGE_PUBLISHED_READ_PIN
        | STAGE_PUBLISHED_READ_CLONE | STAGE_ENTITY_POINT_PREPARE | STAGE_ENTITY_POINT_RESOLVE
        | STAGE_ENTITY_POINT_RETAIN | STAGE_PACK_FILTER | STAGE_PACK_FILTER_INCOMPLETE
        | profile::VFS_OPEN | profile::VFS_READ | profile::VFS_METADATA | profile::VFS_DIRECTORY
        | profile::DOCUMENT_MATERIALIZE => HoldPhase::Read,
        STAGE_STORE_PAGE | STAGE_PUT_DEFERRED | STAGE_PUT_STAGE | STAGE_BLOB_ENCODE
        | STAGE_BLOB_STAGE | STAGE_PREPARED_BLOB_PUBLISH
        | STAGE_AUTHORITY_COMMIT_PRIVATE_BLOB_PUBLISH | STAGE_AUTHORITY_COMMIT_STREAM_PREPARE
        | profile::BLOB_CODEC_COMPRESS | profile::STREAM_SEGMENT_ENCODE
        | profile::STREAM_SEGMENT_FRESH_PUBLISH => HoldPhase::Stage,
        STAGE_AUTHORITY_COMMIT_VALIDATE_REBASE | STAGE_OVERLAY_VALIDATE_HEAD_VERSIONS
        | STAGE_OVERLAY_VALIDATE_ORIGINAL_BLIND_VERSIONS
        | STAGE_OVERLAY_VALIDATE_RANGE_WITNESSES => HoldPhase::Validate,
        STAGE_ENGINE_COMMIT | STAGE_BUILDER | STAGE_ENCODE | STAGE_CARRIED | STAGE_WAL
        | STAGE_COMMIT_TAIL => HoldPhase::Journal,
        STAGE_WAL_BARRIER_WAIT | STAGE_WAL_BARRIER_SYNC | STAGE_CK_WAIT
        | profile::VFS_SYNC => HoldPhase::Sync,
        STAGE_PREPARE | STAGE_APPLY | STAGE_TA_PREPARE_FAST => HoldPhase::Apply,
        STAGE_APPLY_BATCH | STAGE_CHECKPOINT_IF | STAGE_DRAIN_STEP | STAGE_STEP_CHUNK
        | STAGE_STEP_MANIFEST | STAGE_CK_MATERIALIZE | STAGE_CK_SEGMENTS | STAGE_CK_MANIFEST
        | STAGE_CK_WAL | STAGE_CK_CONTROL | STAGE_FLUSH_DIRTY | STAGE_CK_DRAIN | STAGE_CK_CLEAR
        | STAGE_CK_SWAP | STAGE_PACK_ADOPT | STAGE_HISTORY_STAGE | STAGE_CATALOG_PUBLISH
        | STAGE_CATALOG_WRITE | STAGE_CATALOG_INSTALL | STAGE_CATALOG_CONTROL
        | STAGE_TA_PREPARE_MERGE | STAGE_OVERLAY_REPLAY | STAGE_OVERLAY_SELECT
        | STAGE_OVERLAY_CARRY_BUDGET | STAGE_OVERLAY_PUBLISH | STAGE_CHECKPOINT_TOTAL
        | STAGE_OVERLAY_PREPARE_CAPTURE | STAGE_OVERLAY_PREPARE_ENCODE
        | STAGE_OVERLAY_PREPARE_PUBLISH | STAGE_ROTATION_PREFIX_ENCODE
        | STAGE_ROTATION_WINDOW_CAPTURE | STAGE_ROTATION_WINDOW_ENCODE
        | STAGE_ROTATION_INSTALL_DIRTY_CONTROL | STAGE_ROTATION_INSTALL_WINDOW
        | STAGE_ROTATION_INSTALL_MANIFEST | STAGE_ROTATION_INSTALL_WAL
        | STAGE_ROTATION_INSTALL_CONTROL | STAGE_ROTATION_INSTALL_RELEASE
        | STAGE_ROTATION_GENERATION_HANDOVER
        | STAGE_ROTATION_PREDECESSOR_INSTALL => HoldPhase::Merge,
        STAGE_CK_REMOVE | STAGE_PACK_RETIRE | STAGE_RETENTION_PRUNE => HoldPhase::Retention,
        STAGE_TURN_EVENT_PUBLISH | STAGE_TA_RECEIPT | STAGE_TA_OUTBOX | STAGE_RECEIPT_ENCODE
        | STAGE_RECEIPT_COMPRESS | STAGE_AUTHORITY_COMMIT_INDEXES
        | STAGE_AUTHORITY_COMMIT_OUTBOX_CAPTURE | STAGE_IR_RESULT_EXTRACT
        | STAGE_IR_RECEIPT_RESPONSE_POLICY | STAGE_SEMANTIC_RESPONSE_VALIDATE
        | STAGE_EMBEDDED_RESPONSE_ENCODE | STAGE_EMBEDDED_ENVELOPE_BUILD
        | STAGE_PUBLISHED_READ_PUBLISH => HoldPhase::Publish,
        STAGE_AUTHORITY_WAIT | STAGE_ENTITY_PAGE_CACHE_READ_WAIT
        | STAGE_ENTITY_PAGE_CACHE_WRITE_WAIT | STAGE_BLOCK_PENDING_WAIT | STAGE_BLOCK_CACHE_WAIT
        | STAGE_BLOCK_HISTORY_SOURCE_WAIT | STAGE_PIN_REGISTRY_WAIT | STAGE_PARALLEL_SNAPSHOT_WAIT
        | STAGE_OVERLAY_PREPARE_BUDGET_WAIT | STAGE_ROTATION_PARK | STAGE_ROTATION_PREPARE_WAIT
        | STAGE_ROTATION_INSTALL_WAIT | STAGE_EMBEDDED_WAIT_RESPONSE | STAGE_EMBEDDED_QUEUE_WAIT
        | profile::FIFO_WAIT | profile::MUTEX_WAIT | profile::BLOB_CODEC_WAIT => HoldPhase::Wait,
        _ => return None,
    })
}
struct HoldCounters { calls: AtomicU64, nanos: AtomicU64, maximum: AtomicU64 }
impl HoldCounters {
    const fn new() -> Self { Self { calls: AtomicU64::new(0), nanos: AtomicU64::new(0), maximum: AtomicU64::new(0) } }
    fn record(&self, nanos: u64) {
        self.nanos.fetch_add(nanos, Ordering::Relaxed);
        self.maximum.fetch_max(nanos, Ordering::Relaxed);
        self.calls.fetch_add(1, Ordering::Relaxed);
    }
}
static HOLD_COUNTERS: [HoldCounters; BACKGROUND_HOLDS.len()] = [const { HoldCounters::new() }; BACKGROUND_HOLDS.len()];
pub(crate) fn hold_profile() -> serde_json::Value {
    if !crate::request_profile::enabled() { return serde_json::json!({"enabled": false}); }
    let phases: Vec<_> = BACKGROUND_HOLDS.iter().map(|(hold, name)| {
        let counters = &HOLD_COUNTERS[*hold as usize];
        serde_json::json!({"phase": name, "calls": counters.calls.load(Ordering::Relaxed),
            "totalNanos": counters.nanos.load(Ordering::Relaxed),
            "maximumNanos": counters.maximum.load(Ordering::Relaxed)})
    }).collect();
    serde_json::json!({"enabled": true, "contract": "tofudb.authority-holds/v1",
        "scope": "process lifetime; completed instrumented background holds; wall time, not CPU; counters may advance during snapshot; phases are not additive",
        "fixedCounterBytes": std::mem::size_of_val(&HOLD_COUNTERS),
        "archiveAcceptance":crate::transcript_archive::acceptance_profile(), "phases": phases})
}

/// A bounded-frequency diagnostic for completed slow authority holds. No user
/// payloads are recorded. The threshold limits one holder to at most one line
/// per second; a stuck holder remains visible through the independent watcher.
/// A reporting hold attributes its own thread's instrumented spans, so the line
/// names how its elapsed time split across the phases inside the hold.
pub(crate) struct AuthorityHoldTimer<'a> {
    operation: &'a str,
    started: Option<Instant>,
    warn: bool,
    hold: Option<BackgroundHold>,
    attribution: Option<crate::request_profile::HoldToken>,
}

impl<'a> AuthorityHoldTimer<'a> {
    pub(crate) fn start(operation: &'a str) -> Self {
        let attribution = crate::request_profile::begin_hold();
        Self { operation, started: Some(Instant::now()), warn: true,
            hold: if crate::request_profile::enabled() { background_hold(operation) } else { None },
            attribution }
    }
}

impl<'a> AuthorityHoldTimer<'a> {
    /// Counter-only observer: no clock read or extra warning when disabled.
    pub(crate) fn probe(operation: &'a str) -> Self {
        let hold = if crate::request_profile::enabled() { background_hold(operation) } else { None };
        Self { operation, started: hold.map(|_| Instant::now()), warn: false, hold, attribution: None }
    }
}

impl Drop for AuthorityHoldTimer<'_> {
    fn drop(&mut self) {
        let Some(started) = self.started else { return; };
        let elapsed = started.elapsed();
        let nanos = u64::try_from(elapsed.as_nanos()).unwrap_or(u64::MAX);
        let reported = self.warn && elapsed >= Duration::from_secs(1);
        let attribution = self.attribution.take()
            .map(|token| crate::request_profile::finish_hold(token, nanos, reported));
        if let Some(hold) = self.hold {
            HOLD_COUNTERS[hold as usize].record(nanos);
        }
        if reported {
            crate::engine_log::warn("authority_hold",
                &hold_message(self.operation, elapsed, attribution.as_ref()));
        }
    }
}

fn milliseconds(nanos: u64) -> String { format!("{:.1}", nanos as f64 / 1e6) }

/// `operation=<op> elapsed_ms=<n> phases=<phase>:<ms>,... stages=<stage>:<ms>,...`
/// Both lists are exclusive milliseconds in descending order; `phases` sums to
/// the elapsed time. A hold nested in another attributed hold on the same
/// thread prints no lists: the outer hold's line carries its spans.
fn hold_message(operation: &str, elapsed: Duration,
    attribution: Option<&crate::request_profile::HoldReport>) -> String {
    let mut message = format!("operation={operation} elapsed_ms={}", elapsed.as_millis());
    let Some(report) = attribution else { return message; };
    let mut phases: Vec<_> = HOLD_PHASE_NAMES.iter().zip(report.phases)
        .filter(|(_, nanos)| *nanos != 0).collect();
    phases.sort_by(|a, b| b.1.cmp(&a.1));
    let list = |entries: &mut dyn Iterator<Item = (&str, u64)>| entries
        .map(|(name, nanos)| format!("{name}:{}", milliseconds(nanos))).collect::<Vec<_>>().join(",");
    message.push_str(" phases=");
    message.push_str(&list(&mut phases.into_iter().map(|(name, nanos)| (*name, nanos))));
    if !report.stages.is_empty() {
        message.push_str(" stages=");
        message.push_str(&list(&mut report.stages.iter().copied()));
    }
    if report.invalid_spans != 0 {
        message.push_str(&format!(" invalid_spans={}", report.invalid_spans));
    }
    message
}

/// Probe cadence. Cheap enough that 40 probes fit the wedge threshold;
/// slow enough that the probe itself is noise on the scheduler.
const WATCH_POLL_INTERVAL: Duration = Duration::from_millis(250);
/// Continuous-contention bound before the daemon reports itself wedged.
/// Twice the default foreground wire deadline (5 s): any single hold
/// longer than this has already failed every request that arrived during
/// it, so reporting (and supervisor recycling) is strictly better than
/// waiting for the holder.
pub const AUTHORITY_WEDGE_THRESHOLD: Duration = Duration::from_secs(10);
const WATCH_STACK_BYTES: usize = 256 * 1024;
/// Bounded stop join, mirroring the other drivers: the probe thread holds
/// nothing, so a slow exit never stalls shutdown.
const WATCH_STOP_JOIN_BUDGET: Duration = Duration::from_secs(2);

struct SharedState {
    /// Watch-base milliseconds of the current contention window's start;
    /// 0 means "not contended". Readers compute now - stored.
    contention_started_millis: AtomicU64,
    base: Instant,
}

#[derive(Clone, Copy)]
enum Probe { Free, Busy, Poisoned }

impl SharedState {
    fn record_probe(&self, now: u64, probe: Probe, progressed: bool) {
        if matches!(probe, Probe::Free) || (matches!(probe, Probe::Busy) && progressed) {
            self.contention_started_millis.store(0, Ordering::Release);
        } else {
            let _ = self.contention_started_millis.compare_exchange(
                0, now.max(1), Ordering::AcqRel, Ordering::Acquire);
        }
    }

    fn elapsed_millis(&self) -> u64 {
        self.base.elapsed().as_millis() as u64
    }

    fn contended_milliseconds(&self) -> u64 {
        let started = self.contention_started_millis.load(Ordering::Acquire);
        if started == 0 {
            return 0;
        }
        self.elapsed_millis().saturating_sub(started)
    }
}

/// Cloneable read model for `system.health` / `system.metrics`.
#[derive(Clone)]
pub struct AuthorityContentionObserver {
    shared: Arc<SharedState>,
    stopped: Arc<AtomicBool>,
}

impl AuthorityContentionObserver {
    /// Milliseconds of the CURRENT continuous contention window (0 when
    /// the last probe acquired immediately).
    pub fn contended_milliseconds(&self) -> u64 {
        self.shared.contended_milliseconds()
    }

    /// Whether continuous contention has exceeded the wedge threshold.
    pub fn wedged(&self) -> bool {
        self.contended_milliseconds() >= AUTHORITY_WEDGE_THRESHOLD.as_millis() as u64
    }

    pub fn require_healthy(&self) -> io::Result<()> {
        if self.stopped.load(Ordering::Acquire) {
            return Err(io::Error::other(
                "authority contention watch stopped unexpectedly",
            ));
        }
        Ok(())
    }
}

#[cfg(test)]
impl AuthorityContentionObserver {
    /// Observer with a synthetic contention window, for health-payload
    /// tests that cannot construct an engine fixture.
    pub(crate) fn for_test(contended_milliseconds: u64) -> Self {
        // The base sits `contended_milliseconds` in the past and the window
        // starts 1 ms after it, so the observer reads ≈ the requested
        // window without depending on wall-clock setup order.
        let shared = Arc::new(SharedState {
            contention_started_millis: AtomicU64::new(0),
            base: Instant::now()
                .checked_sub(Duration::from_millis(contended_milliseconds))
                .unwrap_or_else(Instant::now),
        });
        if contended_milliseconds > 0 {
            shared
                .contention_started_millis
                .store(1, Ordering::Release);
        }
        Self {
            shared,
            stopped: Arc::new(AtomicBool::new(false)),
        }
    }
}

/// Dedicated watch thread. Started once per daemon next to the other
/// drivers; `stop_and_join` follows their bounded-join contract.
pub struct AuthorityContentionWatch {
    stop: Arc<AtomicBool>,
    thread: Mutex<Option<std::thread::JoinHandle<()>>>,
    observer: AuthorityContentionObserver,
}

impl AuthorityContentionWatch {
    pub fn start(database: &Arc<Mutex<AuthorityDatabase>>, foreground_admissions: Arc<AtomicU64>) -> io::Result<Self> {
        let shared = Arc::new(SharedState {
            contention_started_millis: AtomicU64::new(0),
            base: Instant::now(),
        });
        let stop = Arc::new(AtomicBool::new(false));
        let stopped = Arc::new(AtomicBool::new(false));
        let thread = std::thread::Builder::new()
            .name("tofu-db-auth-watch".to_owned())
            .stack_size(WATCH_STACK_BYTES)
            .spawn({
                let shared = Arc::clone(&shared);
                let stop = Arc::clone(&stop);
                let stopped = Arc::clone(&stopped);
                let database = Arc::clone(database);
                move || {
                    let mut previous_progress = foreground_admissions.load(Ordering::Relaxed);
                    loop {
                        if stop.load(Ordering::Acquire) {
                            break;
                        }
                        let probe = match database.try_lock() {
                            Ok(guard) => { drop(guard); Probe::Free },
                            Err(TryLockError::WouldBlock) => Probe::Busy,
                            Err(TryLockError::Poisoned(_)) => Probe::Poisoned,
                        };
                        let progress = foreground_admissions.load(Ordering::Relaxed);
                        shared.record_probe(shared.elapsed_millis(), probe, progress != previous_progress);
                        previous_progress = progress;
                        std::thread::park_timeout(WATCH_POLL_INTERVAL);
                    }
                    stopped.store(true, Ordering::Release);
                }
            })?;
        Ok(Self {
            stop,
            thread: Mutex::new(Some(thread)),
            observer: AuthorityContentionObserver { shared, stopped },
        })
    }

    pub fn observer(&self) -> AuthorityContentionObserver {
        self.observer.clone()
    }

    pub fn stop_and_join(&self) -> io::Result<()> {
        self.stop.store(true, Ordering::Release);
        let thread = self
            .thread
            .lock()
            .map_err(|_| io::Error::other("authority watch thread lock poisoned"))?
            .take();
        if let Some(thread) = thread {
            thread.thread().unpark();
            let deadline = Instant::now() + WATCH_STOP_JOIN_BUDGET;
            loop {
                if thread.is_finished() {
                    break;
                }
                if Instant::now() >= deadline {
                    return Ok(());
                }
                std::thread::sleep(Duration::from_millis(25));
            }
            thread
                .join()
                .map_err(|_| io::Error::other("authority contention watch panicked"))?;
        }
        self.observer.stopped.store(true, Ordering::Release);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn completed_hold_counters_keep_total_and_largest_concurrent_duration() {
        let counters = Arc::new(HoldCounters::new());
        let threads: Vec<_> = (1..=4).map(|nanos| {
            let counters = Arc::clone(&counters);
            std::thread::spawn(move || { for _ in 0..1000 { counters.record(nanos); } })
        }).collect();
        for thread in threads { thread.join().unwrap(); }
        assert_eq!(counters.calls.load(Ordering::Relaxed), 4000);
        assert_eq!(counters.nanos.load(Ordering::Relaxed), 10000);
        assert_eq!(counters.maximum.load(Ordering::Relaxed), 4);
        assert!(background_hold("turn.append_settled").is_none());
        assert!(background_hold("private owner/key/payload").is_none());
        for (hold, name) in BACKGROUND_HOLDS { assert_eq!(background_hold(name).unwrap() as u8, hold as u8); }
        eprintln!("authority_hold_resources: counters={} timer={}",
            std::mem::size_of_val(&HOLD_COUNTERS), std::mem::size_of::<AuthorityHoldTimer<'_>>());
    }

    #[test]
    fn slow_hold_line_names_exclusive_phases_then_stages_in_descending_order() {
        let mut phases = [0; HOLD_PHASE_NAMES.len()];
        phases[HoldPhase::Read as usize] = 1_500_200_000;
        phases[HoldPhase::Journal as usize] = 99_800_000;
        phases[HoldPhase::Other as usize] = 400_000_000;
        phases[HoldPhase::Sync as usize] = 400_000_000;
        let report = crate::request_profile::HoldReport { phases,
            stages: vec![("get_unc_ok", 1_400_000_000), ("wal", 99_800_000)], invalid_spans: 0 };
        assert_eq!(hold_message("turn.event.record", Duration::from_millis(2_400), Some(&report)),
            "operation=turn.event.record elapsed_ms=2400 phases=read:1500.2,sync:400.0,other:400.0,\
             journal:99.8 stages=get_unc_ok:1400.0,wal:99.8");
        assert_eq!(hold_message("rotation.install", Duration::from_millis(1_250), None),
            "operation=rotation.install elapsed_ms=1250");
        let crossing = crate::request_profile::HoldReport { phases: [0; HOLD_PHASE_NAMES.len()],
            stages: Vec::new(), invalid_spans: 2 };
        assert!(hold_message("x", Duration::from_secs(1), Some(&crossing)).ends_with(" invalid_spans=2"));
    }

    #[test]
    fn reporting_holds_own_their_threads_attribution_and_probes_never_do() {
        let stage = crate::fsync_stats::STAGE_WAL;
        {
            let _probe = AuthorityHoldTimer::probe("maintenance.round");
            assert!(crate::request_profile::enter(stage).is_none());
        }
        {
            let hold = AuthorityHoldTimer::start("turn.event.record");
            assert!(hold.attribution.is_some());
            let span = crate::request_profile::enter(stage);
            assert!(span.is_some());
            crate::request_profile::exit(span, 1);
            let nested = AuthorityHoldTimer::start("overlay.publish");
            assert!(nested.attribution.is_none());
            drop(nested);
            assert!(crate::request_profile::enter(stage).is_some(), "the outer hold still attributes");
        }
        assert!(crate::request_profile::enter(stage).is_none());
        std::thread::spawn(move || assert!(crate::request_profile::enter(stage).is_none()))
            .join().unwrap();
    }

    #[test]
    fn every_stage_name_has_one_phase_or_inherits() {
        // Phases come from the fixed vocabulary; reads, fsyncs and waits are
        // physical and never inherit their caller's phase.
        let names = crate::fsync_stats::STAGE_NAMES.len();
        for stage in 0..names + 32 {
            if let Some(phase) = hold_phase_of_stage(stage) {
                assert!((phase as usize) < HOLD_PHASE_NAMES.len());
                assert_ne!(phase, HoldPhase::Other, "Other is only the uncovered remainder");
            }
        }
        for stage in [crate::request_profile::VFS_SYNC, crate::fsync_stats::STAGE_WAL_BARRIER_WAIT] {
            assert_eq!(hold_phase_of_stage(stage), Some(HoldPhase::Sync));
        }
        assert_eq!(hold_phase_of_stage(crate::request_profile::VFS_READ), Some(HoldPhase::Read));
        assert_eq!(hold_phase_of_stage(crate::fsync_stats::STAGE_RETENTION_PRUNE), Some(HoldPhase::Retention));
        assert_eq!(hold_phase_of_stage(crate::request_profile::VFS_WRITE), None);
        assert_eq!(hold_phase_of_stage(crate::fsync_stats::STAGE_IR_COMMIT), None);
    }

    #[test]
    fn busy_sampling_with_handoffs_never_becomes_a_false_wedge() {
        let shared = SharedState { contention_started_millis: AtomicU64::new(0), base: Instant::now() };
        // Forty busy probes span the actual production wedge threshold.
        // Progress comes from successful foreground acquisitions, not arrivals.
        for now in (250..=30_000).step_by(250) {
            shared.record_probe(now, Probe::Busy, true);
            assert_eq!(shared.contention_started_millis.load(Ordering::Acquire), 0);
        }
        shared.record_probe(30_250, Probe::Busy, false);
        shared.record_probe(41_000, Probe::Busy, false);
        assert_eq!(shared.contention_started_millis.load(Ordering::Acquire), 30_250);
        shared.record_probe(41_250, Probe::Busy, true);
        assert_eq!(shared.contention_started_millis.load(Ordering::Acquire), 0);
        shared.record_probe(42_000, Probe::Poisoned, true);
        shared.record_probe(53_000, Probe::Poisoned, true);
        assert_eq!(shared.contention_started_millis.load(Ordering::Acquire), 42_000);
    }

    #[test]
    fn independent_authorities_cannot_mask_each_others_wedge() {
        let first = tempfile::tempdir().unwrap();
        let second = tempfile::tempdir().unwrap();
        let a = AuthorityDatabase::initialize(first.path()).unwrap();
        let b = AuthorityDatabase::initialize(second.path()).unwrap();
        let progress_a = a.foreground_admissions();
        let progress_b = b.foreground_admissions();
        a.note_foreground_admission();
        assert_eq!(progress_a.load(Ordering::Relaxed), 1);
        assert_eq!(progress_b.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn observer_reports_zero_contention_when_free() {
        let shared = Arc::new(SharedState {
            contention_started_millis: AtomicU64::new(0),
            base: Instant::now(),
        });
        assert_eq!(shared.contended_milliseconds(), 0);
    }

    #[test]
    fn contention_window_accumulates_until_cleared() {
        let shared = Arc::new(SharedState {
            contention_started_millis: AtomicU64::new(0),
            base: Instant::now(),
        });
        let now = shared.elapsed_millis();
        let _ = shared.contention_started_millis.compare_exchange(
            0,
            now.max(1),
            Ordering::AcqRel,
            Ordering::Acquire,
        );
        // Millisecond-floor truncation shaves up to 2 ms off the window;
        // assert well inside the slept interval.
        std::thread::sleep(Duration::from_millis(15));
        let observed = shared.contended_milliseconds();
        assert!(observed >= 5, "observed={observed}");
        shared.contention_started_millis.store(0, Ordering::Release);
        assert_eq!(shared.contended_milliseconds(), 0);
    }

    #[test]
    fn watch_thread_tracks_a_held_mutex() {
        // A raw Mutex<()> exercises the same try_lock protocol shape; the
        // production type is Mutex<AuthorityDatabase>, unconstructible
        // here without an engine fixture.
        let raw: Arc<Mutex<()>> = Arc::new(Mutex::new(()));
        let shared = Arc::new(SharedState {
            contention_started_millis: AtomicU64::new(0),
            base: Instant::now(),
        });
        let stop = Arc::new(AtomicBool::new(false));
        let probe = {
            let shared = Arc::clone(&shared);
            let stop = Arc::clone(&stop);
            let raw = Arc::clone(&raw);
            std::thread::spawn(move || loop {
                if stop.load(Ordering::Acquire) {
                    break;
                }
                match raw.try_lock() {
                    Ok(guard) => {
                        drop(guard);
                        shared.contention_started_millis.store(0, Ordering::Release);
                    }
                    Err(_) => {
                        let now = shared.elapsed_millis();
                        let _ = shared.contention_started_millis.compare_exchange(
                            0,
                            now.max(1),
                            Ordering::AcqRel,
                            Ordering::Acquire,
                        );
                    }
                }
                std::thread::sleep(Duration::from_millis(5));
            })
        };
        std::thread::sleep(Duration::from_millis(20));
        assert_eq!(shared.contended_milliseconds(), 0);
        let held = raw.lock().unwrap();
        std::thread::sleep(Duration::from_millis(40));
        let observed = shared.contended_milliseconds();
        assert!(observed >= 20, "observed={observed}");
        drop(held);
        std::thread::sleep(Duration::from_millis(30));
        assert_eq!(shared.contended_milliseconds(), 0);
        stop.store(true, Ordering::Release);
        probe.join().unwrap();
    }
}
