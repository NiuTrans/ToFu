//! Supervised pre-authority process boundary for the storage.v2 server.
//!
//! This module opens an explicitly initialized authority, publishes one
//! credential-free readiness envelope, and ties listener lifetime to EOF on
//! the inherited empty stdin pipe. It does not authorize application cutover.

use std::io::{self, Read, Write};
use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use sha2::{Digest, Sha256};
use zeroize::Zeroize;

use crate::authority::AuthorityDatabase;

use crate::authority_gc::{
    AuthorityGarbageCollectionBudget, MAX_AUTHORITY_GC_ORPHAN_PAYLOAD_SEGMENT_FILES_REMOVED,
    MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED, MAX_AUTHORITY_GC_PAYLOAD_SEGMENT_FILES_SCANNED,
    MAX_AUTHORITY_GC_TEMPORARY_BLOCK_FILES_REMOVED, MIN_AUTHORITY_GC_PAYLOAD_COMPACTION_BLOCKS,
};
use crate::background_pack::BackgroundPacker;
use crate::generated_storage_operations::STORAGE_SCHEMA_VERSION;
use crate::generated_storage_v2::{
    MAX_CONNECTION_REUSE_TARGET, MAX_DAEMON_AUTH_SECRET_BYTES,
    MIN_DAEMON_AUTH_SECRET_BYTES, PROTOCOL_VERSION,
};
use crate::listener::{LoopbackListenerConfig, LoopbackListenerMetrics, LoopbackServer};
use crate::maintenance::{MaintenanceScheduler, MaintenanceSchedulerConfig, MaintenanceScope};
use crate::resource_probe::{
    probe_launch_resources, DaemonResourceBudget, CONNECTION_STACK_BYTES,
    MIN_WRITABLE_VOLUME_FREE_BYTES,
};
use crate::semantic::AuthenticatedScope;
use crate::server::{FrameAdmissionBudget, StorageV2Authenticator};
use crate::turn_search_projection::TurnSearchProjection;

const AUTH_TOKEN_DOMAIN: &[u8] = b"tofu.storage.v2.auth-token\0";
const MAX_READINESS_BYTES: usize = 4_096;

pub struct DaemonConfig {
    pub(crate) data_dir: PathBuf,
    pub(crate) scope: AuthenticatedScope,
    pub(crate) auth_token: [u8; 32],
    pub(crate) listener: LoopbackListenerConfig,
    pub(crate) resource_budget: DaemonResourceBudget,
    pub(crate) maintenance: MaintenanceSchedulerConfig,
    pub(crate) search_projection_dir: Option<PathBuf>,
    pub(crate) at_rest_key: Option<crate::at_rest::AtRestKey>,
    pub(crate) backup_dir: Option<std::path::PathBuf>,
    pub(crate) fastpath: Option<crate::fastpath::FastpathConfig>,
    pub(crate) wal_durability: crate::wal::WalDurability,
    pub(crate) wal_flush_interval: std::time::Duration,
}

impl std::fmt::Debug for DaemonConfig {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("DaemonConfig")
            .field("data_dir", &self.data_dir)
            .field("scope", &self.scope)
            .field("auth_token", &"<redacted>")
            .field("listener", &self.listener)
            .field("resource_budget", &self.resource_budget)
            .field("maintenance", &self.maintenance)
            .field("search_projection_dir", &self.search_projection_dir)
            .finish()
    }
}

impl DaemonConfig {
    pub fn new(
        data_dir: &Path,
        owner_id: u64,
        tenant_id: Option<u64>,
        auth_secret: &mut [u8],
    ) -> io::Result<Self> {
        if !data_dir.is_absolute() {
            auth_secret.zeroize();
            return Err(invalid_input("tofu-db authority path must be absolute"));
        }
        if owner_id == 0 || tenant_id == Some(0) {
            auth_secret.zeroize();
            return Err(invalid_input("tofu-db daemon scope IDs must be positive"));
        }
        let auth_token_result = derive_auth_token(auth_secret);
        auth_secret.zeroize();
        let auth_token = auth_token_result?;
        let mut resource_budget = DaemonResourceBudget::from_snapshot_with_volume_class(
            probe_launch_resources(data_dir),
            Some(crate::resource_probe::volume_class(data_dir)),
        );
        enforce_writable_volume(resource_budget)?;
        let maintenance = MaintenanceSchedulerConfig::from_resource_budget(resource_budget);
        Ok(Self {
            data_dir: data_dir.to_path_buf(),
            scope: AuthenticatedScope {
                owner_id,
                tenant_id,
            },
            auth_token,
            listener: LoopbackListenerConfig {
                initial_connection_reservations: resource_budget.maximum_connections,
                connection_stack_bytes: resource_budget.connection_stack_bytes,
                ..LoopbackListenerConfig::default()
            },
            resource_budget,
            maintenance,
            search_projection_dir: None,
            at_rest_key: None,
            backup_dir: None,
            fastpath: None,
            wal_durability: crate::wal::WalDurability::default(),
            wal_flush_interval: DEFAULT_WAL_FLUSH_INTERVAL,
        })
    }

    /// Selects the active-log durability policy. `strict` (the default) keeps
    /// the per-commit barrier. `normal` acknowledges after the WAL write and
    /// lets a bounded background flusher plus every publication point perform
    /// barriers, capping the acknowledged-loss window at `interval` — an
    /// explicit bounded-RPO consent, never a silent default.
    pub fn with_wal_durability(
        mut self,
        durability: crate::wal::WalDurability,
        interval: std::time::Duration,
    ) -> io::Result<Self> {
        if durability == crate::wal::WalDurability::Normal
            && interval > crate::wal::MAX_WAL_FLUSH_INTERVAL
        {
            return Err(invalid_input(
                "WAL flush interval must not exceed 1000 ms in Normal durability",
            ));
        }
        self.wal_durability = durability;
        self.wal_flush_interval = interval;
        Ok(self)
    }

    /// Supplies the opt-in block-at-rest encryption key for the authority
    /// open path. `None` keeps the plaintext real VFS.
    pub fn with_fastpath(mut self, base: &Path, interval_ms: Option<u64>) -> io::Result<Self> {
        if self.backup_dir.is_some() {
            return Err(invalid_input("native fastpath owns its recovery shadow; --backup-dir must be omitted"));
        }
        self.fastpath = Some(crate::fastpath::FastpathConfig::new(&self.data_dir, base, interval_ms)?);
        Ok(self)
    }

    pub fn with_backup_dir(mut self, backup_dir: Option<std::path::PathBuf>) -> Self {
        self.backup_dir = backup_dir;
        self
    }

    pub fn with_at_rest_key(mut self, key: Option<crate::at_rest::AtRestKey>) -> Self {
        self.at_rest_key = key;
        self
    }

    pub fn with_search_projection_dir(mut self, projection_dir: &Path) -> io::Result<Self> {
        if self.scope.tenant_id.is_none() {
            return Err(invalid_input(
                "turn-search projection requires an explicit tenant ID",
            ));
        }
        validate_projection_path(&self.data_dir, projection_dir)?;
        self.search_projection_dir = Some(projection_dir.to_path_buf());
        Ok(self)
    }
}

impl Drop for DaemonConfig {
    fn drop(&mut self) {
        self.auth_token.zeroize();
    }
}

pub const DEFAULT_WAL_FLUSH_INTERVAL: std::time::Duration =
    std::time::Duration::from_millis(10);

/// Dedicated pack-adoption driver. Under multi-writer pressure the
/// maintenance scheduler defers its rounds almost every tick (35/37
/// deferrals measured at 8 writers), so adoption through
/// `publish_completed_pack_if_capacity` starves: the completed-pack slot
/// stays occupied, the background packer idles, staged payloads accumulate
/// into a ~59 MiB tail, and the next WAL rotation drains it synchronously
/// for ~750 ms inside the authority mutex (the medium-FUSE swarm p99).
/// This worker drives the three-phase adoption end to end — cheap
/// prepare/finish under the mutex, the whole fsync chain outside it — so
/// the packer never blocks on adoption and rotation-time backlog stays
/// near zero. The engine's three-phase lane semantics are pinned by
/// `engine::tests::prepared_adoption_*`; this loop is plumbing only.
pub(crate) fn run_pack_adoption_driver(
    database: Arc<Mutex<AuthorityDatabase>>,
    stopping: Arc<std::sync::atomic::AtomicBool>,
) {
    use std::sync::atomic::Ordering;
    crate::fsync_stats::set_current_component(crate::fsync_stats::COMPONENT_PACK_ADOPTION_DRIVER);
    let mut hot_sample = crate::fsync_stats::foreground_hotness_sampler();
    while !stopping.load(Ordering::Acquire) {
        if crate::fsync_stats::defer_background_work(&mut hot_sample) {
            let backlog = database
                .lock()
                .ok()
                .and_then(|guard| {
                    guard
                        .writer_engine()
                        .ok()
                        .and_then(|engine| engine.block_store().unpublished_background_segment_ids().ok())
                })
                .map_or(0, |ids| ids.len());
            if backlog < 2 {
                std::thread::sleep(std::time::Duration::from_millis(100));
                continue;
            }
        }
        let mut prepared = {
            let mut guard = match database.lock() {
                Ok(guard) => guard,
                Err(_) => return,
            };
            let _hold = crate::authority_watch::AuthorityHoldTimer::probe("pack.prepare");
            match guard.try_prepare_pack_adoption() {
                Ok(crate::engine::PackAdoptionPreparation::Ready(prepared)) => prepared,
                Ok(crate::engine::PackAdoptionPreparation::Empty)
                | Ok(crate::engine::PackAdoptionPreparation::Busy) => {
                    drop(_hold);
                    drop(guard);
                    std::thread::sleep(std::time::Duration::from_millis(25));
                    continue;
                }
                Err(_) => {
                    drop(_hold);
                    drop(guard);
                    std::thread::sleep(std::time::Duration::from_millis(250));
                    continue;
                }
            }
        };
        let outcome = crate::engine::publish_prepared_pack_adoption(&mut prepared);
        let mut guard = match database.lock() {
            Ok(guard) => guard,
            Err(_) => return,
        };
        let result = {
            let _hold = crate::authority_watch::AuthorityHoldTimer::start("pack.install");
            guard.finish_pack_adoption(prepared, outcome)
        };
        if let Err(error) = result {
            crate::engine_log::error(
                "pack_adoption",
                &format!("pack adoption failed: {error}"),
            );
            drop(guard);
            std::thread::sleep(std::time::Duration::from_millis(250));
        }
    }
}

/// Dedicated rotation-checkpoint driver. At multi-writer rates the WAL
/// rotation threshold is reached every few seconds, and the synchronous
/// checkpoint writes the unprepared history tail (~58 MiB) inside the
/// authority mutex (~443 ms `ck_segments` on FUSE — the medium-swarm p99).
/// This worker runs the bulk segment encoding of each prepared rotation
/// outside the mutex (phase 2) and installs the small checkpoint tail
/// under it (phase 3). The engine's snapshot/lane semantics are pinned by
/// `engine::tests::*rotation*`; this loop is plumbing only.
pub(crate) fn run_rotation_driver(
    database: Arc<Mutex<AuthorityDatabase>>,
    stopping: Arc<std::sync::atomic::AtomicBool>,
    rotation_barrier: std::sync::Arc<crate::rotation_barrier::RotationBarrier>,
) {
    use std::sync::atomic::Ordering;
    crate::fsync_stats::set_current_component(crate::fsync_stats::COMPONENT_ROTATION_DRIVER);
    while !stopping.load(Ordering::Acquire) {
        let prepared = {
            // Blocking acquisition: a try_lock poll starves under writer
            // convoys (freshly arriving writers always beat the sleeping
            // driver), delaying rotation start until the hard ceiling turns
            // into client-visible backpressure. The probe itself is a quick
            // in-memory take, and no mutex holder ever waits on this driver.
            let waiting = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ROTATION_PREPARE_WAIT);
            let mut guard = match database.lock() {
                Ok(guard) => guard,
                Err(_) => return,
            };
            drop(waiting);
            let _hold = crate::authority_watch::AuthorityHoldTimer::start("rotation.prepare");
            guard.take_prepared_rotation()
        };
        let Ok(Some(work)) = prepared else {
            // Event-driven start: prepare sites signal this barrier, so the
            // bounded park is only the liveness fallback (a missed signal
            // costs one slice, never correctness — the probe is re-taken).
            let since = rotation_barrier.generation();
            rotation_barrier.park(since, std::time::Duration::from_millis(10));
            continue;
        };
        let (prepared, outcome) = match work {
            crate::engine::RotationWork::Fresh(prepared) => {
                let _encode = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ROTATION_PREFIX_ENCODE);
                // Encode outside the authority mutex (the take-scope guard
                // is already dropped): a parked `Deferred` pair skips this
                // step entirely, which is the whole point of the slot. The
                // next-generation WAL file is also pre-created here, off the
                // hold, shrinking the install's in-hold fsync chain.
                if let Err(error) = crate::engine::prestage_rotation_wal(&prepared) {
                    crate::engine_log::warn(
                        "rotation_checkpoint",
                        &format!(
                            "next-generation WAL pre-stage failed (install falls back to in-hold create): {error}"
                        ),
                    );
                }
                let outcome = crate::engine::encode_rotation_segments(&prepared);
                (prepared, outcome)
            }
            crate::engine::RotationWork::Deferred(prepared, outcome) => {
                if let Err(error) = crate::engine::prestage_rotation_wal(&prepared) {
                    crate::engine_log::warn(
                        "rotation_checkpoint",
                        &format!(
                            "next-generation WAL pre-stage failed (install falls back to in-hold create): {error}"
                        ),
                    );
                }
                (prepared, Ok(outcome))
            }
        };
        let waiting = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ROTATION_INSTALL_WAIT);
        let mut guard = match database.lock() {
            Ok(guard) => guard,
            Err(_) => return,
        };
        drop(waiting);
        let publication = {
            let _hold = crate::authority_watch::AuthorityHoldTimer::start("rotation.install");
            guard.prepare_rotation_publication(prepared, outcome)
        };
        drop(guard);
        let result = match publication {
            Ok(crate::engine::RotationPublication::Prepared(prepared)) => {
                let outcome = crate::engine::publish_rotation_publication(&prepared);
                let mut guard = match database.lock() {
                    Ok(guard) => guard,
                    Err(_) => return,
                };
                let _hold = crate::authority_watch::AuthorityHoldTimer::start("rotation.install");
                guard.finish_rotation_publication(prepared, outcome)
            }
            Ok(crate::engine::RotationPublication::Complete(sequence)) => Ok(sequence),
            Err(error) => Err(error),
        };
        let transient_defer = matches!(
            result.as_ref().err(),
            Some(error) if error.kind() == io::ErrorKind::WouldBlock
        );
        // Drain retries are driver progress, not a reopened write lane.
        // Only publication or a terminal failure releases request waiters.
        if transient_defer {
            rotation_barrier.signal_install();
        } else {
            rotation_barrier.signal_completion();
        }
        if let Err(error) = result {
            if transient_defer {
                // A publication/defer round trip is ordinary flow control,
                // not a failure: re-probe promptly instead of parking the
                // lane behind an error backoff (2026-09-20 swarm-p99).
                std::thread::sleep(std::time::Duration::from_millis(5));
            } else {
                crate::engine_log::error(
                    "rotation_checkpoint",
                    &format!("rotation checkpoint failed: {error}"),
                );
                std::thread::sleep(std::time::Duration::from_millis(250));
            }
        }
    }
}

/// Thread owner for [`run_rotation_driver`].
pub(crate) struct RotationDriver {
    stopping: Arc<std::sync::atomic::AtomicBool>,
    worker: Mutex<Option<std::thread::JoinHandle<()>>>,
}

impl RotationDriver {
    pub(crate) fn start(
        database: Arc<Mutex<AuthorityDatabase>>,
        rotation_barrier: std::sync::Arc<crate::rotation_barrier::RotationBarrier>,
    ) -> io::Result<Self> {
        {
            let mut guard = database
                .lock()
                .map_err(|_| io::Error::other("authority mutex poisoned"))?;
            guard.attach_rotation_driver()?;
            guard.attach_rotation_wakeup(std::sync::Arc::clone(&rotation_barrier))?;
        }
        let stopping = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let worker_stopping = Arc::clone(&stopping);
        let worker = std::thread::Builder::new()
            .name("tofu-db-rotation".to_owned())
            .stack_size(512 * 1024)
            .spawn(move || run_rotation_driver(database, worker_stopping, rotation_barrier))?;
        Ok(Self {
            stopping,
            worker: Mutex::new(Some(worker)),
        })
    }

    pub(crate) fn stop_and_join(&self) -> io::Result<()> {
        self.stopping
            .store(true, std::sync::atomic::Ordering::Release);
        let worker = self
            .worker
            .lock()
            .map_err(|_| io::Error::other("rotation driver state poisoned"))?
            .take();
        if let Some(worker) = worker {
            worker
                .join()
                .map_err(|_| io::Error::other("rotation driver panicked"))?;
        }
        Ok(())
    }
}

/// Thread owner for [`run_pack_adoption_driver`].
pub(crate) struct PackAdoptionDriver {
    stopping: Arc<std::sync::atomic::AtomicBool>,
    worker: Mutex<Option<std::thread::JoinHandle<()>>>,
}

impl PackAdoptionDriver {
    pub(crate) fn start(database: Arc<Mutex<AuthorityDatabase>>) -> io::Result<Self> {
        let stopping = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let worker_stopping = Arc::clone(&stopping);
        let worker = std::thread::Builder::new()
            .name("tofu-db-pack-adopt".to_owned())
            .stack_size(512 * 1024)
            .spawn(move || run_pack_adoption_driver(database, worker_stopping))?;
        Ok(Self {
            stopping,
            worker: Mutex::new(Some(worker)),
        })
    }

    pub(crate) fn stop_and_join(&self) -> io::Result<()> {
        self.stopping
            .store(true, std::sync::atomic::Ordering::Release);
        let worker = self
            .worker
            .lock()
            .map_err(|_| io::Error::other("pack adoption driver state poisoned"))?
            .take();
        if let Some(worker) = worker {
            worker
                .join()
                .map_err(|_| io::Error::other("pack adoption driver panicked"))?;
        }
        Ok(())
    }
}

pub fn derive_auth_token(secret: &[u8]) -> io::Result<[u8; 32]> {
    if !(MIN_DAEMON_AUTH_SECRET_BYTES..=MAX_DAEMON_AUTH_SECRET_BYTES).contains(&secret.len())
        || !secret.is_ascii()
    {
        return Err(invalid_input(
            "TOFU_STORAGE_TOKEN must contain 32..256 ASCII bytes",
        ));
    }
    let mut digest = Sha256::new();
    digest.update(AUTH_TOKEN_DOMAIN);
    digest.update(secret);
    Ok(digest.finalize().into())
}

/// Graceful-stop WAL retirement on both engines (main authority + search
/// projection). The overlay is merged before each checkpoint — a checkpoint
/// must never publish with pending deltas. An empty committed window is not
/// an error; every real failure is logged and swallowed because the worst
/// case is the status quo: the next open replays the WAL.
pub(crate) fn checkpoint_wal_windows_for_fast_reopen(
    database: &Arc<Mutex<AuthorityDatabase>>,
    search_projection: Option<&Arc<Mutex<TurnSearchProjection>>>,
) {
    let mut guard = match database.lock() {
        Ok(guard) => guard,
        Err(_) => {
            crate::engine_log::warn(
                "shutdown_checkpoint",
                "authority mutex poisoned; skipping shutdown WAL checkpoint",
            );
            return;
        }
    };
    if let Err(error) = crate::engine_log::lifecycle_step("shutdown_timing", "authority_overlay_merge", || guard.merge_entity_overlay_fully()) {
        crate::engine_log::warn(
            "shutdown_checkpoint",
            &format!("overlay merge before shutdown checkpoint failed: {error}"),
        );
    }
    match crate::engine_log::lifecycle_step("shutdown_timing", "authority_checkpoint", || guard.checkpoint_wal_window()) {
        Ok(_) => {}
        Err(error) if error.kind() == io::ErrorKind::InvalidInput => {}
        Err(error) => {
            crate::engine_log::warn(
                "shutdown_checkpoint",
                &format!("authority shutdown checkpoint failed: {error}"),
            );
        }
    }
    drop(guard);
    if let Some(projection) = search_projection {
        let mut guard = match projection.lock() {
            Ok(guard) => guard,
            Err(_) => {
                crate::engine_log::warn(
                    "shutdown_checkpoint",
                    "search projection mutex poisoned; skipping its shutdown \
                     WAL checkpoint",
                );
                return;
            }
        };
        match crate::engine_log::lifecycle_step("shutdown_timing", "search_checkpoint", || guard.checkpoint_wal_window_for_shutdown()) {
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::InvalidInput => {}
            Err(error) => {
                crate::engine_log::warn(
                    "shutdown_checkpoint",
                    &format!("search projection shutdown checkpoint failed: {error}"),
                );
            }
        }
    }
}

/// Default online-backup location: a sibling of the authority directory so
/// the two paths are always disjoint (backup.rs `require_disjoint_paths`).
pub(crate) fn default_backup_dir(data_dir: &std::path::Path) -> io::Result<std::path::PathBuf> {
    let parent = data_dir.parent().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "authority directory has no parent for the default backup directory",
        )
    })?;
    Ok(parent.join("backups").join("tofudb"))
}

pub fn serve_supervised<R: Read + Send + 'static, W: Write>(
    mut config: DaemonConfig,
    parent_lease_reader: R,
    readiness_writer: &mut W,
) -> io::Result<LoopbackListenerMetrics> {
    if config.fastpath.is_some() && std::env::var_os("TOFU_DB_DISABLE_ONLINE_BACKGROUND").is_some() {
        return Err(invalid_input("native fastpath requires online replication"));
    }
    if let Some(fastpath) = config.fastpath.as_ref() {
        if config.backup_dir.is_some() { return Err(invalid_input("native fastpath owns its recovery shadow")); }
        if config.wal_durability != crate::wal::WalDurability::Strict {
            return Err(invalid_input("native fastpath requires strict local durability"));
        }
        if let Some(projection) = config.search_projection_dir.as_ref() {
            validate_projection_path(&fastpath.local_authority, projection)?;
        }
    }
    let fastpath_lease = config.fastpath.as_ref()
        .map(|fastpath| fastpath.prepare(config.at_rest_key.clone())).transpose()?;
    if let Some(lease) = fastpath_lease.as_ref() {
        if let Some(fastpath) = config.fastpath.as_mut() {
            fastpath.sync_interval_ms = Some(lease.interval.as_millis() as u64);
        }
        config.data_dir = lease.config.local_authority.clone();
        config.backup_dir = Some(lease.config.shadow_directory.clone());
        // Local commits have the strict native durability barrier. The durable
        // shadow has its separate, observable asynchronous snapshot loss window.
        config.wal_durability = crate::wal::WalDurability::Strict;
        let mut snapshot = config.resource_budget.snapshot;
        // Preserve the single launch CPU/memory probe and explicit overrides;
        // capacity admission separately observes the actual local disk.
        snapshot.volume_free_bytes = Some(fs2::available_space(&config.data_dir)?);
        config.resource_budget = DaemonResourceBudget::from_snapshot_with_volume_class(
            snapshot, Some(crate::resource_probe::volume_class(&config.data_dir)));
        config.maintenance = MaintenanceSchedulerConfig::from_resource_budget(config.resource_budget);
    }
    let at_rest_key = config.at_rest_key.clone();
    let mut authority = crate::engine_log::lifecycle_step("startup_timing", "authority_open",
        || AuthorityDatabase::open_with_key(&config.data_dir, at_rest_key))?;
    // Probe-sized hot-data residency before any read traffic: the legacy
    // fixed defaults (32/16 MiB) thrash on large hosts, and every miss on a
    // network volume is a round trip (2026-09-22 incident).
    authority.configure_hot_data_caches(
        config.resource_budget.block_cache_bytes,
        config.resource_budget.entity_page_cache_bytes,
    )?;
    // Probe-derived write-path budgets (overlay triggers, materialization
    // step, segment index cache) before any commit traffic.
    authority.configure_write_path_budgets(
        config.resource_budget.overlay_budget_bytes,
        config.resource_budget.overlay_wal_watermark_bytes,
        config.resource_budget.checkpoint_materialize_batch_bytes,
        config.resource_budget.payload_segment_cache_bytes,
    )?;
    let wal_barrier_driver = if config.wal_durability == crate::wal::WalDurability::Normal {
        authority.set_wal_durability(config.wal_durability)?;
        authority.start_wal_flusher(config.wal_flush_interval)?;
        None
    } else {
        // Strict durability keeps its acknowledgement contract, but the
        // barrier itself moves to a coordinator thread: every connection's
        // commits post their witness and wait outside the authority mutex,
        // so concurrent commits share one fsync (group-barrier pipelining).
        let (barrier, driver) = crate::wal::WalBarrierDriver::start(
            std::sync::Arc::new(crate::vfs::RealVfs),
            authority.wal_path()?,
            std::time::Duration::from_millis(1),
        )?;
        authority.attach_wal_barrier(&barrier)?;
        Some((barrier, driver))
    };
    if let Some(tenant) = config.scope.tenant_id {
        crate::conversation_header::trash_migration::require_no_pending(
            &authority,
            tenant,
            config.scope.owner_id,
        )?;
    }
    let authority_uuid = authority.authority_uuid();
    authority.configure_timer_live_capacity(config.resource_budget.timer_live_capacity)?;

    let foreground_admissions = authority.foreground_admissions();
    let canonical_json_cache = Some(authority.canonical_json_cache_observer());
        let decoded_chunk_cache = Some(authority.decoded_chunk_cache_observer());
    let published_queries = authority.enable_published_queries()?;
    let database = Arc::new(Mutex::new(authority));
    let search_projection = crate::engine_log::lifecycle_step("startup_timing", "search_projection_open", || {
        config.search_projection_dir.as_ref().map(|path| {
        let result = if path.exists() {
            TurnSearchProjection::open(
                path,
                config
                    .resource_budget
                    .search_projection_maximum_bytes_per_owner,
            )
        } else {
            TurnSearchProjection::initialize(
                path,
                config
                    .resource_budget
                    .search_projection_maximum_bytes_per_owner,
            )
        };
        let mut projection = result?;
        projection.require_source_admission(authority_uuid);
        Ok::<_, io::Error>(Arc::new(Mutex::new(projection)))
    }).transpose()
    })?;
    let authenticator = Arc::new(StorageV2Authenticator::new(
        &config.auth_token,
        config.scope,
    )?);
    let listener = LoopbackServer::bind(
        SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0),
        config.listener,
    )?;
    let address = listener.local_addr()?;
    let parent_lease = ParentLeaseMonitor::spawn(parent_lease_reader)?;
    write_readiness(
        readiness_writer,
        address.port(),
        authority_uuid,
        &config,
        search_projection.is_some(),
    )?;
    // Bounded page-cache warm-up off the readiness path: sequential payload
    // reads convert cold network-volume fault-in into mount readahead so the
    // client's warm-up pass and first user reads complete in seconds.
    let _prefetch = crate::prefetch::start(config.data_dir.clone());
    // Creation happens only after readiness is durable on the control pipe;
    // the worker itself waits one full interval before its first root read.
    let maintenance = config
        .scope
        .tenant_id
        .map(|tenant_id| {
            MaintenanceScope::new(tenant_id, config.scope.owner_id).and_then(|scope| {
                MaintenanceScheduler::start_with_search(
                    Arc::clone(&database),
                    search_projection.clone(),
                    vec![scope],
                    config.maintenance,
                )
            })
        })
        .transpose()?;
    let maintenance_observer = maintenance.as_ref().map(MaintenanceScheduler::observer);
    // Packing staged blocks into payload segments costs an 8 MiB write +
    // fsync; doing it here keeps that latency off every rotation commit.
    // TOFU_DB_PACK_BYTES_PER_SECOND paces that write rate (0/unset =
    // unpaced): network mounts should set a rate strictly below the mount's
    // usable bandwidth so segment fsyncs never starve foreground round trips.
    let background_packer = if std::env::var_os("TOFU_DB_DISABLE_BACKGROUND_PACK").is_some() {
        None
    } else {
        let database = database
            .lock()
            .map_err(|_| io::Error::other("authority mutex poisoned"))?;
        match pack_pace_bytes_per_second_from_env() {
            // An explicit rate pins the fixed escape hatch.
            Some(pace_bytes_per_second) => {
                Some(database.start_background_packer_paced(pace_bytes_per_second)?)
            }
            // Unset: the adaptive feedback loop tiers the rate from
            // foreground latency; network volumes start paced, local ones
            // start unpaced and clamp down only on measured pain.
            None => {
                let start_rate = match crate::resource_probe::volume_class(&config.data_dir) {
                    crate::resource_probe::VolumeClass::Network => {
                        crate::background_pack::ADAPTIVE_NETWORK_START_BYTES_PER_SECOND
                    }
                    _ => None,
                };
                Some(database.start_background_packer_adaptive(start_rate)?)
            }
        }
    };
    let packer_observer = background_packer.as_ref().map(BackgroundPacker::observer);
    // Keep adoption off the starvation-prone maintenance cadence: the
    // dedicated driver publishes completed packs through the mutex-free
    // three-phase lane so the packer never stalls behind it.
    let adoption_driver = PackAdoptionDriver::start(Arc::clone(&database))?;
    // The rotation driver runs the bulk history-segment encoding of each
    // rotation outside the authority mutex; only the small checkpoint tail
    // (drain, final segment, CONTROL, swap, retire) stays inside.
    let rotation_barrier = std::sync::Arc::new(crate::rotation_barrier::RotationBarrier::new());
    let rotation_driver =
        RotationDriver::start(Arc::clone(&database), Arc::clone(&rotation_barrier))?;
    // The online collector reclaims dead blocks/segments in two-mark epochs
    // (docs/TOFUDB_ONLINE_GC.md); physical space is recovered without any
    // operator action and without ever blocking the foreground.
    let online_gc_driver = if std::env::var_os("TOFU_DB_DISABLE_ONLINE_BACKGROUND").is_some() {
        None
    } else {
        Some(crate::authority_gc_online::OnlineGarbageCollectionDriver::start(
            &database,
            AuthorityGarbageCollectionBudget::from_resource_budget(config.resource_budget),
            config.resource_budget.has_volume_pressure(),
        )?)
    };
    let online_gc_observer = online_gc_driver.as_ref().map(|driver| driver.observer());
    // Seals pending transcript windows into immutable pages off the
    // foreground path; hydration reads pages instead of per-turn documents.
    let archive_seal_driver =
        if std::env::var_os("TOFU_DB_DISABLE_ONLINE_BACKGROUND").is_some() {
            None
        } else {
            Some(crate::transcript_archive::ArchiveSealDriver::start(
                &database,
                config.scope.tenant_id.unwrap_or(0),
                config.scope.owner_id,
            )?)
        };
    // The online backup driver anchors a checkpoint under a short hold and
    // copies incrementally off-mutex; `system.backup` rings its doorbell.
    let backup_dir = match config.backup_dir.clone() {
        Some(dir) => dir,
        None => default_backup_dir(&config.data_dir)?,
    };
    let online_backup_driver = if std::env::var_os("TOFU_DB_DISABLE_ONLINE_BACKGROUND").is_some() {
        None
    } else {
        Some(match fastpath_lease.as_ref() {
            Some(lease) => crate::online_backup::OnlineBackupDriver::start_fastpath(
                &database, backup_dir, config.at_rest_key.clone(), Arc::clone(lease))?,
            None => crate::online_backup::OnlineBackupDriver::start(
                &database, backup_dir, config.at_rest_key.clone())?,
        })
    };
    let online_backup_observer = online_backup_driver.as_ref().map(|driver| driver.observer());
    // The authority-contention watch is the wedge detector: every other
    // health signal answers from observer handles and stays green while a
    // holder wedges the authority mutex; this one reports exactly the
    // continuous-contention window foreground requests experience. It is
    // not background work, so the online-background kill switch does not
    // apply.
    let authority_watch = crate::authority_watch::AuthorityContentionWatch::start(&database, foreground_admissions)?;
    let authority_watch_observer = Some(authority_watch.observer());
    let control_observers = crate::control_ops::ControlObservers {
        canonical_json_cache,
        decoded_chunk_cache,
        published_queries: Some(published_queries.observer()),
        listener: Some(listener.observer()),
        maintenance: maintenance_observer.clone(),
        packer: packer_observer.clone(),
        online_gc: online_gc_observer.clone(),
        online_backup: online_backup_observer.clone(),
        authority_watch: authority_watch_observer.clone(),
        wal_durability: Some(match config.wal_durability {
            crate::wal::WalDurability::Strict => "strict",
            crate::wal::WalDurability::Normal => "normal",
        }),
        wal_flush_interval_milliseconds: Some(config.wal_flush_interval.as_millis() as u64),
        resource_budget: Some(resource_budget_json(
            &config,
            search_projection.is_some(),
        )),
    };
    // The run consumes both Arcs; the shutdown tail retires their WAL
    // windows afterwards, so keep one cheap clone of each alive.
    let database_for_shutdown = Arc::clone(&database);
    let search_projection_for_shutdown = search_projection.clone();
    let result = listener.run_with_published_queries(
        database,
        search_projection,
        authenticator,
        FrameAdmissionBudget::new(
            config.resource_budget.maximum_frame_bytes,
        )?,
        AuthorityGarbageCollectionBudget::from_resource_budget(config.resource_budget),
        wal_barrier_driver.as_ref().map(|(barrier, _)| barrier.clone()),
        control_observers,
        rotation_barrier,
        Some(published_queries),
        || {
            if let Some(observer) = maintenance_observer.as_ref() {
                observer.require_healthy()?;
            }
            if let Some(observer) = packer_observer.as_ref() {
                observer.require_healthy()?;
            }
            parent_lease.poll_alive()
        },
    );
    let maintenance_result = maintenance
        .as_ref()
        .map(|driver| crate::engine_log::lifecycle_step("shutdown_timing", "maintenance", || driver.stop_and_join()))
        .transpose();
    // Stop the collector before the shutdown merge/checkpoint tail so its
    // try_lock holds never contend with the final checkpoint commit.
    let online_gc_result = online_gc_driver
        .map(|driver| crate::engine_log::lifecycle_step("shutdown_timing", "online_gc", || driver.stop_and_join()))
        .transpose();
    let archive_seal_result = archive_seal_driver
        .map(|driver| crate::engine_log::lifecycle_step("shutdown_timing", "archive_seal", || driver.stop_and_join()))
        .transpose();
    let online_backup_result = online_backup_driver
        .map(|driver| crate::engine_log::lifecycle_step("shutdown_timing", "online_backup", || driver.stop_and_join()))
        .transpose();
    // Stop the watch with the other drivers; it holds nothing, so its
    // bounded join never stalls the shutdown tail.
    let authority_watch_result = crate::engine_log::lifecycle_step("shutdown_timing", "authority_watch", || authority_watch.stop_and_join());
    let packer_result = background_packer
        .as_ref()
        .map(|driver| crate::engine_log::lifecycle_step("shutdown_timing", "background_packer", || driver.stop_and_join()))
        .transpose();
    let adoption_result = crate::engine_log::lifecycle_step("shutdown_timing", "adoption", || adoption_driver.stop_and_join());
    let rotation_result = crate::engine_log::lifecycle_step("shutdown_timing", "rotation", || rotation_driver.stop_and_join());
    let parent_result = parent_lease.join_if_finished();
    // Every writer lane except the group barrier is now quiescent. Retire
    // both engines' active WAL windows BEFORE joining the barrier driver:
    // the shutdown merge/checkpoint commit waits on that barrier, and
    // joining it first made every overlay-enabled shutdown pay the 30 s
    // barrier-wait deadline ("WAL barrier wait exceeded its deadline").
    // Boot-time WAL replay of the pre-shutdown window (tens of MiB on a
    // network mount) was the dominant startup cost. Best-effort — any
    // failure only means the next boot replays exactly what it replays
    // today, and a hard kill never reaches here.
    checkpoint_wal_windows_for_fast_reopen(
        &database_for_shutdown,
        search_projection_for_shutdown.as_ref(),
    );
    let _barrier_result = wal_barrier_driver
        .as_ref()
        .map(|(_, driver)| driver.stop_and_join())
        .transpose();
    if std::env::var_os("TOFU_DB_FSYNC_STATS").is_some() {
        eprintln!("fsync_stats: {}", crate::fsync_stats::report());
        eprintln!("resource_exhaustion_reasons: {:?}", crate::fsync_stats::resource_exhaustion_snapshot());
        eprintln!("stage_stats:\n{}", crate::fsync_stats::stage_report());
        eprintln!(
            "component_stage_stats:\n{}",
            crate::fsync_stats::component_stage_report()
        );
        eprintln!(
            "packer_metrics: {:?}",
            background_packer.as_ref().map(BackgroundPacker::metrics)
        );
        eprintln!(
            "maintenance_metrics: {:?}",
            maintenance_observer
                .as_ref()
                .map(|observer| observer.metrics())
        );
        eprintln!("listener_metrics: {result:?}");
    }
    let listener_metrics = result?;
    maintenance_result?;
    online_gc_result?;
    archive_seal_result?;
    online_backup_result?;
    authority_watch_result?;
    packer_result?;
    adoption_result?;
    rotation_result?;
    parent_result?;
    Ok(listener_metrics)
}

/// The probe-derived launch budgets, shared verbatim by the readiness
/// envelope and the authenticated `system.health`/`system.metrics` control
/// operations so every observation surface reports the same numbers.
pub(crate) fn resource_budget_json(
    config: &DaemonConfig,
    search_projection_available: bool,
) -> serde_json::Value {
    let resource_budget = config.resource_budget;
    let maintenance = config.maintenance;
    let registered_maintenance_scopes = usize::from(config.scope.tenant_id.is_some());
    let search_projection_configured = config.search_projection_dir.is_some();
    let authority_gc = AuthorityGarbageCollectionBudget::from_resource_budget(resource_budget);
    serde_json::json!({
        "logicalCpus": resource_budget.snapshot.logical_cpus,
        "memoryCapacityBytes": resource_budget.snapshot.memory_capacity_bytes,
        "memoryHeadroomBytes": resource_budget.snapshot.memory_headroom_bytes,
        "volumeFreeBytes": resource_budget.snapshot.volume_free_bytes,
        "minimumWritableVolumeFreeBytes": MIN_WRITABLE_VOLUME_FREE_BYTES,
        "maximumConnections": resource_budget.maximum_connections,
        "maximumFrameBytes": resource_budget.maximum_frame_bytes,
        "eventBatchComposition": crate::indexed_stream::event_batch_composition_budget(),
        "embeddedRequestMemoryBytes": resource_budget.embedded_request_memory_bytes(),
        "connectionStackBytes": resource_budget.connection_stack_bytes,
        "sessionIoTimeoutMilliseconds": config.listener.io_timeout.as_millis(),
        "sessionIdleTimeoutMilliseconds": config.listener.idle_timeout.as_millis(),
        "searchProjectionMaximumBytesPerOwner": resource_budget.search_projection_maximum_bytes_per_owner,
        "timerLiveCapacity": resource_budget.timer_live_capacity,
        "blockCacheBytes": resource_budget.block_cache_bytes,
        "canonicalJsonProofCacheBytes": resource_budget.block_cache_bytes / crate::canonical_json_bytes::cache::HOT_CACHE_BUDGET_DIVISOR,
        "wholeBlobProofCacheBytes": resource_budget.block_cache_bytes / crate::blob::decode_cache::whole_cache_budget_divisor(),
        "decodedBlobChunkCacheBytes": resource_budget.block_cache_bytes / crate::blob::decode_cache::HOT_CACHE_BUDGET_DIVISOR,
        "entityPageCacheBytes": resource_budget.entity_page_cache_bytes,
        "overlayBudgetBytes": resource_budget.overlay_budget_bytes,
        "overlayWalWatermarkBytes": resource_budget.overlay_wal_watermark_bytes,
        "checkpointMaterializeBatchBytes": resource_budget.checkpoint_materialize_batch_bytes,
        "payloadSegmentCacheBytes": resource_budget.payload_segment_cache_bytes,
        "searchProjectionConfigured": search_projection_configured,
        "searchProjectionAvailable": search_projection_available,
        "leanFallback": resource_budget.used_lean_fallback,
        "maximumMaintenanceWorkers": usize::from(registered_maintenance_scopes > 0),
        "maximumMaintenanceScopes": maintenance.maximum_scopes(),
        "registeredMaintenanceScopes": registered_maintenance_scopes,
        "maintenanceIdleIntervalMilliseconds": maintenance.idle_interval().as_millis(),
        "maintenanceWorkerStackBytes": maintenance.worker_stack_bytes(),
        "historyRetainedSegments": maintenance.history_retained_segments(),
        "authorityGcMaximumVictimBytes": authority_gc.maximum_victim_bytes,
        "authorityGcMaximumTemporaryBytes": authority_gc.maximum_temporary_bytes,
        "authorityGcMaximumBlocksPerRound": authority_gc.maximum_blocks_per_round,
        "authorityGcMaximumPayloadSegmentsScanned": MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED,
        "authorityGcMaximumPayloadSegmentFilesScanned": MAX_AUTHORITY_GC_PAYLOAD_SEGMENT_FILES_SCANNED,
        "authorityGcMaximumOrphanPayloadSegmentFilesRemoved": MAX_AUTHORITY_GC_ORPHAN_PAYLOAD_SEGMENT_FILES_REMOVED,
        "authorityGcMinimumPayloadCompactionBlocks": MIN_AUTHORITY_GC_PAYLOAD_COMPACTION_BLOCKS,
        "authorityGcMaximumTemporaryBlockFilesRemoved": MAX_AUTHORITY_GC_TEMPORARY_BLOCK_FILES_REMOVED,
    })
}

fn write_readiness(
    writer: &mut impl Write,
    port: u16,
    authority_uuid: uuid::Uuid,
    config: &DaemonConfig,
    search_projection_available: bool,
) -> io::Result<()> {
    let mut encoded = serde_json::to_vec(&serde_json::json!({
        "type": "storage.ready",
        "authorityUuid": authority_uuid.to_string(),
        "protocol": PROTOCOL_VERSION,
        "port": port,
        "backend": "tofudb",
        "schemaId": STORAGE_SCHEMA_VERSION,
        "preAuthority": true,
        "walDurability": match config.wal_durability {
            crate::wal::WalDurability::Strict => "strict",
            crate::wal::WalDurability::Normal => "normal",
        },
        "walFlushIntervalMilliseconds": config.wal_flush_interval.as_millis(),
        "resourceBudget": resource_budget_json(config, search_projection_available),
        "nativeFastpath": config.fastpath,
    }))
    .map_err(|error| io::Error::other(format!("encode tofu-db readiness: {error}")))?;
    if encoded.len() + 1 > MAX_READINESS_BYTES {
        return Err(io::Error::other("tofu-db readiness exceeded its bound"));
    }
    encoded.push(b'\n');
    writer.write_all(&encoded)?;
    writer.flush()
}

fn validate_projection_path(authority_dir: &Path, projection_dir: &Path) -> io::Result<()> {
    if !projection_dir.is_absolute() {
        return Err(invalid_input(
            "turn-search projection path must be absolute",
        ));
    }
    if projection_dir
        .components()
        .any(|component| matches!(component, Component::CurDir | Component::ParentDir))
    {
        return Err(invalid_input(
            "turn-search projection path must not contain traversal",
        ));
    }
    let authority = std::fs::canonicalize(authority_dir)?;
    let projection = if projection_dir.exists() {
        std::fs::canonicalize(projection_dir)?
    } else {
        let parent = projection_dir.parent().ok_or_else(|| {
            invalid_input("turn-search projection path must have a persistent parent")
        })?;
        let name = projection_dir
            .file_name()
            .ok_or_else(|| invalid_input("turn-search projection path must name a directory"))?;
        std::fs::canonicalize(parent)?.join(name)
    };
    if authority == projection
        || authority.starts_with(&projection)
        || projection.starts_with(&authority)
    {
        return Err(invalid_input(
            "turn-search projection must be outside the authority directory",
        ));
    }
    Ok(())
}

fn enforce_writable_volume(resource_budget: DaemonResourceBudget) -> io::Result<()> {
    if resource_budget.has_volume_pressure() {
        return Err(io::Error::new(
            io::ErrorKind::StorageFull,
            "tofu-db authority volume lacks two WAL windows of writable headroom",
        ));
    }
    Ok(())
}

struct ParentLeaseState {
    alive: AtomicBool,
    failure: Mutex<Option<(io::ErrorKind, String)>>,
}

struct ParentLeaseMonitor {
    state: Arc<ParentLeaseState>,
    worker: Mutex<Option<JoinHandle<()>>>,
}

impl ParentLeaseMonitor {
    fn spawn(mut reader: impl Read + Send + 'static) -> io::Result<Self> {
        let state = Arc::new(ParentLeaseState {
            alive: AtomicBool::new(true),
            failure: Mutex::new(None),
        });
        let worker_state = Arc::clone(&state);
        let worker = thread::Builder::new()
            .name("tofu-db-parent-lease".to_owned())
            .stack_size(CONNECTION_STACK_BYTES / 2)
            .spawn(move || {
                let mut byte = [0_u8; 1];
                loop {
                    match reader.read(&mut byte) {
                        Ok(0) => break,
                        Ok(_) => {
                            record_parent_lease_failure(
                                &worker_state,
                                io::Error::new(
                                    io::ErrorKind::InvalidData,
                                    "tofu-db parent lease pipe must remain empty",
                                ),
                            );
                            break;
                        }
                        Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                        Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(25));
                        }
                        Err(error) => {
                            record_parent_lease_failure(&worker_state, error);
                            break;
                        }
                    }
                }
                worker_state.alive.store(false, Ordering::Release);
            })?;
        Ok(Self {
            state,
            worker: Mutex::new(Some(worker)),
        })
    }

    fn poll_alive(&self) -> io::Result<bool> {
        if self.state.alive.load(Ordering::Acquire) {
            return Ok(true);
        }
        let failure = self
            .state
            .failure
            .lock()
            .map_err(|_| io::Error::other("tofu-db parent lease state is poisoned"))?;
        if let Some((kind, message)) = failure.as_ref() {
            return Err(io::Error::new(*kind, message.clone()));
        }
        Ok(false)
    }

    fn join_if_finished(&self) -> io::Result<()> {
        if self.state.alive.load(Ordering::Acquire) {
            return Ok(());
        }
        let worker = self
            .worker
            .lock()
            .map_err(|_| io::Error::other("tofu-db parent lease worker is poisoned"))?
            .take();
        if worker.is_some_and(|worker| worker.join().is_err()) {
            return Err(io::Error::other("tofu-db parent lease worker panicked"));
        }
        Ok(())
    }
}

fn record_parent_lease_failure(state: &ParentLeaseState, error: io::Error) {
    if let Ok(mut failure) = state.failure.lock() {
        *failure = Some((error.kind(), error.to_string()));
    }
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

/// Parse a `TOFU_DB_PACK_BYTES_PER_SECOND` value into a bounded pacing rate.
///
/// Accepts an integer bytes/sec, optionally with a binary unit suffix
/// (`KiB`/`MiB`/`GiB`, case-insensitive). `0` means unpaced. Returns `None`
/// when the value is not a non-negative integer rate.
fn parse_pack_pace_bytes_per_second(raw: &str) -> Option<u64> {
    const UNITS: [(&str, u64); 3] = [
        ("kib", 1 << 10),
        ("mib", 1 << 20),
        ("gib", 1 << 30),
    ];
    let trimmed = raw.trim();
    let lower = trimmed.to_ascii_lowercase();
    let (digits, multiplier) = UNITS
        .iter()
        .find_map(|(suffix, unit)| {
            lower
                .strip_suffix(suffix)
                .map(|prefix| (prefix.trim(), *unit))
        })
        .unwrap_or((lower.as_str(), 1));
    digits
        .parse::<u64>()
        .ok()
        .and_then(|value| value.checked_mul(multiplier))
}

/// Resolve the packer's pacing rate without ever refusing storage startup.
///
/// The knob only throttles a background optimization; a malformed value
/// killing the boot sequence turns a tuning hint into an outage (and the
/// readiness envelope is already published by this point, so the supervisor
/// can only report an opaque health-preflight failure). Invalid values
/// degrade to the unpaced default and excessive ones clamp to the hard
/// ceiling, each with one stderr warning.
pub(crate) fn pack_pace_bytes_per_second_from_env() -> Option<u64> {
    let raw = std::env::var("TOFU_DB_PACK_BYTES_PER_SECOND").ok()?;
    match parse_pack_pace_bytes_per_second(&raw) {
        Some(rate) if rate <= crate::background_pack::MAX_PACK_PACE_BYTES_PER_SECOND => {
            Some(rate).filter(|rate| *rate > 0)
        }
        Some(_) => {
            crate::engine_log::warn(
                "config",
                "TOFU_DB_PACK_BYTES_PER_SECOND exceeds the 16 GiB/s ceiling; \
                 clamping to the ceiling",
            );
            Some(crate::background_pack::MAX_PACK_PACE_BYTES_PER_SECOND)
        }
        None => {
            crate::engine_log::warn(
                "config",
                &format!(
                    "ignoring invalid TOFU_DB_PACK_BYTES_PER_SECOND value {raw:?}; \
                     expected an integer bytes/sec with optional KiB/MiB/GiB suffix, or unset. \
                     Background packing falls back to the adaptive controller"
                ),
            );
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::resource_probe::LaunchResourceSnapshot;

    #[test]
    fn pack_pace_parsing_accepts_integers_and_binary_units() {
        assert_eq!(parse_pack_pace_bytes_per_second("0"), Some(0));
        assert_eq!(parse_pack_pace_bytes_per_second("1024"), Some(1024));
        assert_eq!(parse_pack_pace_bytes_per_second("64MiB"), Some(64 << 20));
        assert_eq!(parse_pack_pace_bytes_per_second("64mib"), Some(64 << 20));
        assert_eq!(parse_pack_pace_bytes_per_second(" 128 KiB "), Some(128 << 10));
        assert_eq!(parse_pack_pace_bytes_per_second("1GiB"), Some(1 << 30));
        assert_eq!(parse_pack_pace_bytes_per_second(""), None);
        assert_eq!(parse_pack_pace_bytes_per_second("fast"), None);
        assert_eq!(parse_pack_pace_bytes_per_second("64MB"), None);
        assert_eq!(parse_pack_pace_bytes_per_second("-1"), None);
        assert_eq!(parse_pack_pace_bytes_per_second("1.5GiB"), None);
        assert_eq!(parse_pack_pace_bytes_per_second("18446744073709551615GiB"), None);
    }

    #[test]
    fn auth_derivation_is_bounded_domain_separated_and_redacted() {
        let secret = [b'x'; 32];
        let token = derive_auth_token(&secret).unwrap();
        assert_ne!(token, secret);
        assert_eq!(token, derive_auth_token(&secret).unwrap());
        assert!(derive_auth_token(&[b'x'; 31]).is_err());
        assert!(derive_auth_token(&[0xff; 32]).is_err());

        let directory = tempfile::tempdir().unwrap();
        let mut mutable_secret = secret;
        let config = DaemonConfig::new(directory.path(), 1, None, &mut mutable_secret).unwrap();
        assert_eq!(mutable_secret, [0; 32]);
        assert!(format!("{config:?}").contains("<redacted>"));
        assert!(!format!("{config:?}").contains(&format!("{token:?}")));

        let mut invalid_secret = [b'q'; 31];
        assert!(DaemonConfig::new(directory.path(), 1, None, &mut invalid_secret).is_err());
        assert_eq!(invalid_secret, [0; 31]);
    }

    #[test]
    fn supervised_serve_opens_existing_authority_and_emits_bounded_readiness() {
        let directory = tempfile::tempdir().unwrap();
        let authority_uuid = AuthorityDatabase::initialize(directory.path())
            .unwrap()
            .authority_uuid();
        let mut secret = [b'y'; 32];
        let config = DaemonConfig::new(directory.path(), 11, Some(7), &mut secret).unwrap();
        let mut readiness = Vec::new();
        let metrics =
            serve_supervised(config, io::Cursor::new(Vec::new()), &mut readiness).unwrap();
        assert_eq!(metrics, LoopbackListenerMetrics::default());
        assert!(readiness.len() <= MAX_READINESS_BYTES);
        let document: serde_json::Value = serde_json::from_slice(&readiness).unwrap();
        assert_eq!(document["type"], "storage.ready");
        assert_eq!(document["authorityUuid"], authority_uuid.to_string());
        assert_eq!(document["protocol"], PROTOCOL_VERSION);
        assert_eq!(document["backend"], "tofudb");
        assert_eq!(document["preAuthority"], true);
        assert_eq!(document["resourceBudget"]["eventBatchComposition"]["contract"],
            crate::generated_storage_v2::NATIVE_EVENT_BATCH_COMPOSITION_CONTRACT);
        assert_eq!(document["resourceBudget"]["eventBatchComposition"]["maximumCarriedPayloadBytes"],
            crate::transaction::MAX_CARRIED_PAYLOAD_BYTES);
        assert!(document["port"].as_u64().unwrap() > 0);
        assert!(document["resourceBudget"]["maximumConnections"]
            .as_u64()
            .is_some_and(|value| (1..=MAX_CONNECTION_REUSE_TARGET as u64).contains(&value)));
        assert!(document["resourceBudget"]["maximumFrameBytes"]
            .as_u64()
            .is_some_and(|value| value >= 8 * 1024 * 1024));
        assert_eq!(document["resourceBudget"]["timerLiveCapacity"], 0);
        assert!(
            document["resourceBudget"]["searchProjectionMaximumBytesPerOwner"]
                .as_u64()
                .is_some_and(|value| (128 * 1024 * 1024..=4 * 1024 * 1024 * 1024).contains(&value))
        );
        assert_eq!(
            document["resourceBudget"]["searchProjectionConfigured"],
            false
        );
        assert_eq!(
            document["resourceBudget"]["searchProjectionAvailable"],
            false
        );
        assert_eq!(
            document["resourceBudget"]["minimumWritableVolumeFreeBytes"],
            MIN_WRITABLE_VOLUME_FREE_BYTES
        );
        assert_eq!(document["resourceBudget"]["maximumMaintenanceWorkers"], 1);
        assert_eq!(document["resourceBudget"]["registeredMaintenanceScopes"], 1);
        assert!(document["resourceBudget"]["maximumMaintenanceScopes"]
            .as_u64()
            .is_some_and(|value| (1..=64).contains(&value)));
    }

    #[test]
    fn supervised_serve_never_initializes_a_missing_authority() {
        let directory = tempfile::tempdir().unwrap();
        let missing = directory.path().join("missing");
        let mut secret = [b'z'; 32];
        let config = DaemonConfig::new(&missing, 1, None, &mut secret).unwrap();
        let mut readiness = Vec::new();
        assert!(serve_supervised(config, io::Cursor::new(Vec::new()), &mut readiness).is_err());
        assert!(readiness.is_empty());
        assert!(!missing.exists());
    }

    #[test]
    fn writable_daemon_refuses_observed_volume_pressure() {
        let budget = DaemonResourceBudget::from_snapshot(LaunchResourceSnapshot {
            logical_cpus: Some(1),
            memory_capacity_bytes: Some(1024 * 1024 * 1024),
            memory_headroom_bytes: Some(512 * 1024 * 1024),
            volume_free_bytes: Some(MIN_WRITABLE_VOLUME_FREE_BYTES - 1),
        });
        assert_eq!(
            enforce_writable_volume(budget).unwrap_err().kind(),
            io::ErrorKind::StorageFull
        );
    }

    #[test]
    fn search_projection_path_is_explicit_tenant_scoped_and_separate() {
        let directory = tempfile::tempdir().unwrap();
        let authority = directory.path().join("authority");
        let projection = directory.path().join("projection");
        AuthorityDatabase::initialize(&authority).unwrap();

        let mut no_tenant_secret = [b'p'; 32];
        assert!(
            DaemonConfig::new(&authority, 1, None, &mut no_tenant_secret)
                .unwrap()
                .with_search_projection_dir(&projection)
                .is_err()
        );

        let mut secret = [b'p'; 32];
        let config = DaemonConfig::new(&authority, 1, Some(1), &mut secret).unwrap();
        assert!(config.with_search_projection_dir(&authority).is_err());

        let mut secret = [b'p'; 32];
        let config = DaemonConfig::new(&authority, 1, Some(1), &mut secret).unwrap();
        assert!(config
            .with_search_projection_dir(&authority.join("projection"))
            .is_err());

        let mut secret = [b'p'; 32];
        let config = DaemonConfig::new(&authority, 1, Some(1), &mut secret).unwrap();
        assert!(config.with_search_projection_dir(&projection).is_ok());
    }

    #[test]
    fn supervised_serve_opens_configured_disposable_search_projection() {
        let directory = tempfile::tempdir().unwrap();
        let authority = directory.path().join("authority");
        let projection = directory.path().join("projection");
        AuthorityDatabase::initialize(&authority).unwrap();
        let mut secret = [b's'; 32];
        let config = DaemonConfig::new(&authority, 11, Some(7), &mut secret)
            .unwrap()
            .with_search_projection_dir(&projection)
            .unwrap();
        let mut readiness = Vec::new();

        serve_supervised(config, io::Cursor::new(Vec::new()), &mut readiness).unwrap();

        let document: serde_json::Value = serde_json::from_slice(&readiness).unwrap();
        assert_eq!(
            document["resourceBudget"]["searchProjectionConfigured"],
            true
        );
        assert_eq!(
            document["resourceBudget"]["searchProjectionAvailable"],
            true
        );
        assert!(projection.join("CONTROL").exists());
    }
}
