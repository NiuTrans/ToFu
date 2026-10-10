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
enum HoldPhase { RotationPrepare, RotationInstall, PackPrepare, PackInstall,
    OverlayCapture, OverlayPublish, MaintenanceRound, HistoryInstall,
    SearchPrepare, SearchAcknowledge, SearchActivation, SearchBackfill, SearchDirtyList,
    ArchiveCapture, ArchivePublish, RotationPartial, RotationHandover }
const HOLD_PHASES: [(HoldPhase, &str); 17] = [
    (HoldPhase::RotationPrepare, "rotation.prepare"),
    (HoldPhase::RotationInstall, "rotation.install"),
    (HoldPhase::PackPrepare, "pack.prepare"),
    (HoldPhase::PackInstall, "pack.install"),
    (HoldPhase::OverlayCapture, "overlay.capture"),
    (HoldPhase::OverlayPublish, "overlay.publish"),
    (HoldPhase::MaintenanceRound, "maintenance.round"),
    (HoldPhase::HistoryInstall, "history.install"),
    (HoldPhase::SearchPrepare, "search.prepare"),
    (HoldPhase::SearchAcknowledge, "search.acknowledge"),
    (HoldPhase::SearchActivation, "search.activation"),
    (HoldPhase::SearchBackfill, "search.backfill"),
    (HoldPhase::SearchDirtyList, "search.dirty-list"),
    (HoldPhase::ArchiveCapture, "archive.capture"),
    (HoldPhase::ArchivePublish, "archive.publish"),
    (HoldPhase::RotationPartial, "rotation.partial"),
    (HoldPhase::RotationHandover, "rotation.handover"),
];
fn hold_phase(operation: &str) -> Option<HoldPhase> {
    HOLD_PHASES.iter().find_map(|(phase, name)| (*name == operation).then_some(*phase))
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
static HOLD_COUNTERS: [HoldCounters; HOLD_PHASES.len()] = [const { HoldCounters::new() }; HOLD_PHASES.len()];
pub(crate) fn hold_profile() -> serde_json::Value {
    if !crate::request_profile::enabled() { return serde_json::json!({"enabled": false}); }
    let phases: Vec<_> = HOLD_PHASES.iter().map(|(phase, name)| {
        let counters = &HOLD_COUNTERS[*phase as usize];
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
pub(crate) struct AuthorityHoldTimer<'a> {
    operation: &'a str,
    started: Option<Instant>,
    warn: bool,
    phase: Option<HoldPhase>,
}

impl<'a> AuthorityHoldTimer<'a> {
    pub(crate) fn start(operation: &'a str) -> Self {
        Self { operation, started: Some(Instant::now()), warn: true,
            phase: if crate::request_profile::enabled() { hold_phase(operation) } else { None } }
    }
}

impl<'a> AuthorityHoldTimer<'a> {
    /// Counter-only observer: no clock read or extra warning when disabled.
    pub(crate) fn probe(operation: &'a str) -> Self {
        let phase = if crate::request_profile::enabled() { hold_phase(operation) } else { None };
        Self { operation, started: phase.map(|_| Instant::now()), warn: false, phase }
    }
}

impl Drop for AuthorityHoldTimer<'_> {
    fn drop(&mut self) {
        let Some(started) = self.started else { return; };
        let elapsed = started.elapsed();
        if let Some(phase) = self.phase {
            HOLD_COUNTERS[phase as usize].record(u64::try_from(elapsed.as_nanos()).unwrap_or(u64::MAX));
        }
        if self.warn && elapsed >= Duration::from_secs(1) {
            crate::engine_log::warn("authority_hold", &format!(
                "operation={} elapsed_ms={}", self.operation, elapsed.as_millis()));
        }
    }
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
                        std::thread::sleep(WATCH_POLL_INTERVAL);
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
        assert!(hold_phase("turn.append_settled").is_none());
        assert!(hold_phase("private owner/key/payload").is_none());
        for (phase, name) in HOLD_PHASES { assert_eq!(hold_phase(name).unwrap() as u8, phase as u8); }
        eprintln!("authority_hold_resources: counters={} timer={}",
            std::mem::size_of_val(&HOLD_COUNTERS), std::mem::size_of::<AuthorityHoldTimer<'_>>());
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
