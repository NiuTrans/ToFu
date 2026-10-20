//! Embedded in-process transport: the full storage.v2 semantic surface
//! without the loopback socket.
//!
//! One `EmbeddedRuntime` mirrors `daemon::serve_supervised`'s lifecycle
//! (authority open, strict-mode WAL barrier driver, search projection,
//! maintenance scheduler, background packer, pack-adoption and rotation
//! drivers, graceful-stop WAL-window retirement) minus the listener, the
//! parent-lease monitor, and the readiness pipe. Every request executes
//! through `server::dispatch_one_request`, the single per-request pipeline
//! the framed connection loop also uses, so admission, read-replica lanes,
//! command receipts/outbox, rotation parking, and durability waits are
//! identical to the daemon's; transports differ only in framing.
//!
//! Durability is unchanged: commands still wait for the shared WAL barrier
//! before their response materializes. The JSON in/out envelope below is a
//! transport encoding, never a second semantic contract: payloads are the
//! exact storage.v2 operation payload bytes the wire client would send.

use std::io;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};

use crate::authority::AuthorityDatabase;
use crate::authority_gc::AuthorityGarbageCollectionBudget;
use crate::daemon::{
    checkpoint_wal_windows_for_fast_reopen, PackAdoptionDriver, RotationDriver,
};
use crate::generated_storage_operations::STORAGE_SCHEMA_VERSION;
use crate::maintenance::{MaintenanceScheduler, MaintenanceScope};
use crate::protocol::{Hello, Request, Response, AUTH_TOKEN_BYTES, PROTOCOL_VERSION};
use crate::server::{
    RequestDispatch, StorageV2Authenticator, StorageV2Stores,
};
use crate::turn_search_projection::TurnSearchProjection;

#[path = "embed_command_phases.rs"]
mod command_phases;
pub(crate) use command_phases::EmbeddedCommandOutcome;

fn io_other(message: impl std::fmt::Display) -> io::Error {
    io::Error::other(message.to_string())
}

/// What the embedded caller passes per request. `payload` must be the exact
/// JSON value the storage.v2 wire would carry for the operation (the
/// application adapter's `request_payload` output).
#[derive(Debug)]
pub struct EmbeddedRequest<'payload> {
    pub operation: String,
    pub payload: &'payload serde_json::Value,
    pub command_id: Option<String>,
    pub deadline_unix_ms: u64,
}

/// All semantic requests admit complete aggregates in either transport.
/// Frame size controls chunking, never operation eligibility.
/// Serialization checks length overflow; FFI also reserves its encoded bytes
/// against the handle's launch-probed queue budget before decoding/copying.
pub(crate) const MAX_EMBEDDED_PAYLOAD_BYTES: usize = crate::protocol::MAX_PAYLOAD_BYTES;

pub(crate) fn payload_limit_for_operation(_operation: &str) -> usize {
    usize::MAX
}

/// Apply checked encoded lengths and any explicit caller bound while growing.
/// Complete embedded requests use the addressable vector domain; normal frame
/// windows still select inline versus streamed admission.
struct CappedPayloadWriter {
    bytes: Vec<u8>,
    maximum_bytes: usize,
}

impl io::Write for CappedPayloadWriter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > self.maximum_bytes.saturating_sub(self.bytes.len()) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "embedded request payload exceeds the storage.v2 payload bound",
            ));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn serialize_payload(payload: &Value, maximum_bytes: usize) -> io::Result<Vec<u8>> {
    let mut writer = CappedPayloadWriter { bytes: Vec::new(), maximum_bytes };
    serde_json::to_writer(&mut writer, payload).map_err(|error| {
        match error.io_error_kind() {
            // The cap writer's own rejection, with its stable message.
            Some(kind) => io::Error::new(kind, error.to_string()),
            None => io::Error::new(io::ErrorKind::InvalidInput, error.to_string()),
        }
    })?;
    Ok(writer.bytes)
}

/// The in-process authority: one owner/tenant scope, one negotiated schema,
/// every driver the daemon runs.
#[cfg(feature = "native-runtime-checkpoints")]
#[path = "embedded_checkpoint.rs"]
mod checkpoint;

pub struct EmbeddedRuntime {
    database: Arc<Mutex<AuthorityDatabase>>,
    search_projection: Option<Arc<Mutex<TurnSearchProjection>>>,
    session: crate::server::StorageV2Session,
    scope: crate::semantic::AuthenticatedScope,
    maintenance: Option<MaintenanceScheduler>,
    background_packer: Option<crate::background_pack::BackgroundPacker>,
    archive_seal_driver: Option<crate::transcript_archive::ArchiveSealDriver>,
    wal_barrier_driver: Option<crate::wal::WalBarrierDriver>,
    adoption_driver: PackAdoptionDriver,
    rotation_driver: RotationDriver,
    online_gc_driver: Option<crate::authority_gc_online::OnlineGarbageCollectionDriver>,
    search_gc_driver: Option<crate::authority_gc_online::OnlineGarbageCollectionDriver<TurnSearchProjection>>,
    online_backup_driver: Option<crate::online_backup::OnlineBackupDriver>,
    authority_watch: crate::authority_watch::AuthorityContentionWatch,
    /// Immutable launch data and live coordinator handles borrowed by each
    /// dispatch; driver-owned observers report current component state.
    dispatch_bindings: crate::server::StorageV2Bindings,
    closed: Mutex<bool>,
    #[cfg(feature = "native-runtime-checkpoints")]
    checkpoint_calls: std::sync::RwLock<()>,
    pub(crate) resource_budget: crate::resource_probe::DaemonResourceBudget,
    /// When armed, `Drop` performs no engine operation: the FFI layer sets
    /// this on a poisoned handle (panic contained or execute watchdog
    /// fired), where the engine state is unknown and running the graceful
    /// stop could wedge or panic again. The allocation itself is still freed
    /// once the last reference goes away.
    drop_disarmed: std::sync::atomic::AtomicBool,
}

fn now_unix_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()
        .and_then(|elapsed| u64::try_from(elapsed.as_millis()).ok())
        .unwrap_or(u64::MAX)
}

impl EmbeddedRuntime {
    /// Open an existing authority exactly like `tofu-db serve`, minus the
    /// listener. `auth_secret` is zeroized after token derivation, same as
    /// the daemon's `DaemonConfig`.
    pub fn open(
        data_dir: &Path,
        owner_id: u64,
        tenant_id: Option<u64>,
        search_projection_dir: Option<&Path>,
        auth_secret: &mut [u8],
    ) -> io::Result<Self> {
        let config = crate::daemon::DaemonConfig::new(
            data_dir, owner_id, tenant_id, auth_secret,
        )?;
        let config = match search_projection_dir {
            Some(path) => config.with_search_projection_dir(path)?,
            None => config,
        };
        Self::open_with_config(config)
    }

    fn open_with_config(
        mut config: crate::daemon::DaemonConfig,
    ) -> io::Result<Self> {
        #[cfg(test)]
        if config
            .data_dir
            .to_str()
            .is_some_and(|path| path.contains(test_fault::OPEN_PANIC_DIR_MARKER))
        {
            panic!("embedded test fault: forced open panic");
        }
        let at_rest_key = config.at_rest_key.take();
        let mut authority =
            AuthorityDatabase::open_with_key(&config.data_dir, at_rest_key)?;
        // Probe-sized residency and write-path budgets, exactly like the
        // daemon's serve_supervised: the embedded path serves the same host.
        authority.configure_hot_data_caches(
            config.resource_budget.block_cache_bytes,
            config.resource_budget.entity_page_cache_bytes,
        )?;
        authority.configure_write_path_budgets(
            config.resource_budget.overlay_budget_bytes,
            config.resource_budget.overlay_wal_watermark_bytes,
            config.resource_budget.checkpoint_materialize_batch_bytes,
            config.resource_budget.payload_segment_cache_bytes,
        )?;
        if config.wal_durability != crate::wal::WalDurability::Strict {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "embedded transport serves strict durability only",
            ));
        }
        let (barrier, barrier_driver) = crate::wal::WalBarrierDriver::start_with_write_buffer(
            authority.wal_vfs()?,
            authority.wal_path()?,
            Duration::from_millis(1),
            crate::wal::WalBarrierDriver::capacity_for_headroom(config.resource_budget.snapshot.memory_headroom_bytes),
        )?;
        authority.attach_wal_barrier(&barrier)?;
        if let Some(tenant) = config.scope.tenant_id {
            crate::conversation_header::trash_migration::require_no_pending(
                &authority,
                tenant,
                config.scope.owner_id,
            )?;
        }
        authority
            .configure_timer_live_capacity(config.resource_budget.timer_live_capacity)?;

        let authority_uuid = authority.authority_uuid();
        let foreground_admissions = authority.foreground_admissions();
        let canonical_json_cache = Some(authority.canonical_json_cache_observer());
        let decoded_chunk_cache = Some(authority.decoded_chunk_cache_observer());
        authority.configure_blob_codecs(config.resource_budget.snapshot)?;
        let blob_codec_pool = Some(authority.blob_codec_pool_observer()?);
        let published_queries = authority.enable_published_queries()?;
        let database = Arc::new(Mutex::new(authority));
        let search_projection = config
            .search_projection_dir
            .as_ref()
            .map(|path| {
                let projection = TurnSearchProjection::open_or_defer(
                    path, config.resource_budget.search_projection_maximum_bytes_per_owner, authority_uuid,
                )?;
                Ok::<_, io::Error>(Arc::new(Mutex::new(projection)))
            })
            .transpose()?;
        let authenticator = StorageV2Authenticator::new(&config.auth_token, config.scope)?;
        let session = authenticator.authenticate(&Hello {
            correlation_id: [7; 16],
            minimum_version: PROTOCOL_VERSION,
            maximum_version: PROTOCOL_VERSION,
            schema_ids: vec![STORAGE_SCHEMA_VERSION],
            auth_token: config.auth_token,
        })?;
        let background_packer = if std::env::var_os("TOFU_DB_DISABLE_BACKGROUND_PACK").is_some() {
            None
        } else {
            let database_guard = database
                .lock()
                .map_err(|_| io_other("authority mutex poisoned"))?;
            match crate::daemon::pack_pace_bytes_per_second_from_env() {
                Some(pace) => Some(database_guard.start_background_packer_paced(pace)?),
                None => {
                    let start_rate = match crate::resource_probe::volume_class(&config.data_dir) {
                        crate::resource_probe::VolumeClass::Network => {
                            crate::background_pack::ADAPTIVE_NETWORK_START_BYTES_PER_SECOND
                        }
                        _ => None,
                    };
                    Some(database_guard.start_background_packer_adaptive(start_rate)?)
                }
            }
        };
        let adoption_driver = PackAdoptionDriver::start(Arc::clone(&database))?;
        let rotation_barrier = Arc::new(crate::rotation_barrier::RotationBarrier::new());
        let rotation_driver =
            RotationDriver::start(Arc::clone(&database), Arc::clone(&rotation_barrier))?;
        let background_disabled = std::env::var_os("TOFU_DB_DISABLE_ONLINE_BACKGROUND").is_some();
        let archive_seal_driver = if background_disabled {
            None
        } else {
            Some(crate::transcript_archive::ArchiveSealDriver::start(
                &database,
                config.scope.tenant_id.unwrap_or(0),
                config.scope.owner_id,
            )?)
        };
        let online_gc_driver = if background_disabled {
            None
        } else {
            Some(crate::authority_gc_online::OnlineGarbageCollectionDriver::start(
                &database,
                AuthorityGarbageCollectionBudget::from_resource_budget(config.resource_budget),
                config.resource_budget.has_volume_pressure(),
            )?)
        };
        let search_gc_driver = if background_disabled {
            None
        } else {
            search_projection.as_ref().map(|projection| {
                crate::authority_gc_online::OnlineGarbageCollectionDriver::start(
                    projection,
                    AuthorityGarbageCollectionBudget::from_resource_budget(config.resource_budget),
                    config.resource_budget.has_volume_pressure(),
                )
            }).transpose()?
        };
        let online_backup_driver = if background_disabled {
            None
        } else {
            Some(crate::online_backup::OnlineBackupDriver::start(
                &database,
                crate::daemon::default_backup_dir(&config.data_dir)?,
                config.at_rest_key.clone(),
            )?)
        };
        // Same wedge detector as the serving daemon: an in-process
        // authority wedged by one holder is otherwise invisible to every
        // observer-handled health signal.
        let authority_watch = crate::authority_watch::AuthorityContentionWatch::start(&database, foreground_admissions)?;
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
        let control_observers = crate::control_ops::ControlObservers {
            canonical_json_cache,
        wal_write_buffer: Some(barrier.clone()),
        decoded_chunk_cache,
        blob_codec_pool,
        published_queries: Some(published_queries.observer()),
            listener: None,
            maintenance: maintenance.as_ref().map(MaintenanceScheduler::observer),
            packer: background_packer
                .as_ref()
                .map(crate::background_pack::BackgroundPacker::observer),
            search_gc: search_gc_driver.as_ref().map(|driver| driver.observer()),
            online_gc: online_gc_driver
                .as_ref()
                .map(crate::authority_gc_online::OnlineGarbageCollectionDriver::observer),
            online_backup: online_backup_driver
                .as_ref()
                .map(crate::online_backup::OnlineBackupDriver::observer),
            authority_watch: Some(authority_watch.observer()),
            wal_durability: Some("strict"),
            wal_flush_interval_milliseconds: Some(config.wal_flush_interval.as_millis() as u64),
            resource_budget: Some(crate::daemon::resource_budget_json(
                &config,
                search_projection.as_ref().is_some_and(|index| index.try_lock().is_ok_and(|index| index.available())),
            )),
        };
        Ok(Self {
            database,
            search_projection,
            session,
            scope: config.scope,
            maintenance,
            background_packer,
            archive_seal_driver,
            wal_barrier_driver: Some(barrier_driver),
            adoption_driver,
            rotation_driver,
            online_gc_driver,
            search_gc_driver,
            online_backup_driver,
            authority_watch,
            dispatch_bindings: crate::server::StorageV2Bindings {
                checkpoint_control: None,
                authority_admission: Arc::default(),
                published_queries: Some(published_queries),
                authority_gc_budget: AuthorityGarbageCollectionBudget::from_resource_budget(config.resource_budget),
                wal_barrier: Some(barrier),
                control_observers: Some(control_observers),
                rotation_barrier: Some(rotation_barrier),
            },
            closed: Mutex::new(false),
            #[cfg(feature = "native-runtime-checkpoints")]
            checkpoint_calls: std::sync::RwLock::new(()),
            resource_budget: config.resource_budget,
            drop_disarmed: std::sync::atomic::AtomicBool::new(false),
        })
    }

    /// Execute one storage.v2 operation through the shared dispatch
    /// pipeline. Returns the daemon-shaped response envelope as JSON:
    /// `{status, error_code, error_message, retryable, payload}` where
    /// `payload` is the operation's result value (null on error).
    pub fn execute_json(&self, request: &EmbeddedRequest<'_>) -> io::Result<Value> {
        self.execute_json_with_alive(request, &mut || true)
    }

    pub(crate) fn execute_json_with_alive(
        &self, request: &EmbeddedRequest<'_>, alive: &mut impl FnMut() -> bool,
    ) -> io::Result<Value> {
        self.execute_encoded_with_alive(request.operation.clone(), serialize_payload(
            request.payload, payload_limit_for_operation(&request.operation))?,
            request.command_id.clone(), request.deadline_unix_ms, alive)
    }

    /// The FFI envelope already contains JSON bytes. Keep them encoded until
    /// the shared semantic compiler parses and canonicalizes the request.
    pub(crate) fn execute_encoded_with_alive(
        &self, operation: String, payload: Vec<u8>, command_id: Option<String>,
        deadline_unix_ms: u64, alive: &mut impl FnMut() -> bool,
    ) -> io::Result<Value> {
        self.execute_encoded_response_with_alive(operation, payload, command_id, deadline_unix_ms, alive)
            .map(|response| response_to_json(&response))
    }

    /// Reuse an already executed, snapshot-certified query. Every lock is a
    /// try-lock, and the cache accepts only one-frame owned inputs/results.
    /// No engine/IR execution, I/O or caller pointer escapes this branch.
    pub(crate) fn try_cached_json(&self, operation: &str, payload: &[u8],
        command_id: Option<&str>, deadline_unix_ms: u64, transport_is_healthy: impl FnOnce() -> bool) -> Option<io::Result<Vec<u8>>> {
        if command_id.is_some() || payload.len() > crate::protocol::MAX_PAYLOAD_BYTES { return None; }
        let started = crate::request_profile::enabled().then(std::time::Instant::now);
        let view = self.dispatch_bindings.published_queries.as_ref()?;
        let cached = view.try_cached(crate::query_result_cache::Lookup {
            owner_id: self.scope.owner_id, tenant_id: self.scope.tenant_id,
            schema_id: STORAGE_SCHEMA_VERSION, operation, payload })?;
        // Empty/cold keys pay only the optional cache lookup. Health is still
        // mandatory before admission and before any cached bytes can escape.
        if self.database.is_poisoned() || *self.closed.try_lock().ok()?
            || self.dispatch_bindings.wal_barrier.is_none() || !transport_is_healthy() { return None; }
        // Correlation is not exposed by the C ABI JSON envelope. This local
        // nonzero ID still passes the identical protocol admission validator.
        let request = Request { correlation_id: [1; 16], deadline_unix_ms,
            owner_id: self.scope.owner_id, tenant_id: self.scope.tenant_id,
            command_id: None, schema_id: STORAGE_SCHEMA_VERSION,
            operation: operation.to_owned(), payload: payload.to_vec() };
        let admitted = self.session.admit(&request, now_unix_ms()).ok()?;
        if admitted.operation.kind != crate::generated_storage_operations::StorageOperationKind::Query { return None; }
        crate::fsync_stats::note_foreground_request();
        let _queue = crate::request_profile::QueueContext::completed_inline();
        let mut profile = started.and_then(|start| crate::request_profile::RequestGuard::start_at(operation, start));
        let response = crate::server::response::DispatchedResponse::successful(&request, cached.into_response());
        if let Some(profile) = profile.as_mut() { profile.finish("ok"); }
        drop(profile);
        Some(dispatched_response_to_encoded_json(&response))
    }

    pub(crate) fn execute_encoded_json_with_alive(
        &self, operation: String, payload: Vec<u8>, command_id: Option<String>,
        deadline_unix_ms: u64, alive: &mut impl FnMut() -> bool,
    ) -> io::Result<Vec<u8>> {
        let response = self.execute_encoded_response_with_alive(
            operation, payload, command_id, deadline_unix_ms, alive)?;
        dispatched_response_to_encoded_json(&response)
    }

    fn execute_encoded_response_with_alive(
        &self, operation: String, payload: Vec<u8>, command_id: Option<String>,
        deadline_unix_ms: u64, alive: &mut impl FnMut() -> bool,
    ) -> io::Result<crate::server::response::DispatchedResponse> {
        let wire_request=self.request_from_encoded(operation,payload,command_id,deadline_unix_ms)?;
        self.execute_wire_response_with_alive(wire_request,alive,true)
    }

    fn request_from_encoded(&self, operation:String, payload:Vec<u8>, command_id:Option<String>,
        deadline_unix_ms:u64) -> io::Result<Request> {
        #[cfg(test)]
        test_fault::inject(&operation);
        if *self.closed.lock().map_err(|_| io_other("embedded close state poisoned"))? {
            return Err(io::Error::new(io::ErrorKind::BrokenPipe, "embedded runtime is closed"));
        }
        if payload.len() > payload_limit_for_operation(&operation) {
            return Err(io::Error::new(io::ErrorKind::InvalidInput,
                "embedded request payload exceeds the storage.v2 payload bound"));
        }
        let wire_request = Request {
            correlation_id: *uuid::Uuid::new_v4().as_bytes(),
            deadline_unix_ms,
            owner_id: self.scope.owner_id,
            tenant_id: self.scope.tenant_id,
            command_id,
            schema_id: STORAGE_SCHEMA_VERSION,
            operation,
            payload,
        };
        if self.dispatch_bindings.wal_barrier.is_none() {
            return Err(io_other("embedded runtime lost its WAL barrier"));
        }
        Ok(wire_request)
    }

    fn execute_wire_response_with_alive(
        &self, wire_request: Request, alive: &mut impl FnMut() -> bool, allow_parallel: bool,
    ) -> io::Result<crate::server::response::DispatchedResponse> {
        let outcome = crate::server::profile_request_dispatch(&wire_request.operation, || {
            let stores = {
                let _bindings = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_BINDINGS);
                crate::request_profile::work(crate::request_profile::Work::EmbeddedBindingsReuse, 1);
                StorageV2Stores::borrowed(&self.database, self.search_projection.as_deref(), &self.dispatch_bindings)
            };
            let mut now = now_unix_ms;
            let admitted_at = now();
            crate::server::dispatch_one_request_inner_mode(&stores, &self.session, &wire_request,
                wire_request.payload.len() > MAX_EMBEDDED_PAYLOAD_BYTES,
                admitted_at, &mut now, alive, allow_parallel)
        })?;
        let response = match outcome {
            RequestDispatch::Responded(response) => response,
            RequestDispatch::ConnectionClosed => {
                return Err(io_other("embedded dispatch reported a closed connection"))
            }
        };
        Ok(response)
    }

    pub(crate) fn execute_serialized_fallback(&self,request:Request,alive:&mut impl FnMut()->bool)->io::Result<Vec<u8>> {
        if *self.closed.lock().map_err(|_|io_other("embedded close state poisoned"))? {
            return Err(io::Error::new(io::ErrorKind::BrokenPipe,"embedded runtime is closed"));
        }
        let response=self.execute_wire_response_with_alive(request,alive,false)?;
        dispatched_response_to_encoded_json(&response)
    }

    /// Graceful stop: join every driver, then retire both engines' active
    /// WAL windows exactly like the daemon's shutdown tail, so the next open
    /// replays ~nothing. Idempotent; also runs on Drop.
    pub fn close(&self) -> io::Result<()> {
        self.stop(true)
    }

    /// Ungraceful stop (the SIGKILL approximation for crash-recovery
    /// measurement): drivers are joined so the process stays testable, but
    /// the graceful WAL-window retirement/merge is skipped, so the next open
    /// replays the full pre-crash window exactly like a power loss.
    /// Idempotent.
    pub fn close_ungraceful(&self) -> io::Result<()> {
        self.stop(false)
    }

    /// Arms the drop disarm flag: after this, dropping the runtime frees the
    /// allocation but runs no engine operation. Used by the FFI layer when a
    /// handle is poisoned (contained panic / execute watchdog), where the
    /// engine state is unknown and a graceful stop could wedge or re-panic.
    pub(crate) fn disarm_drop(&self) {
        self.drop_disarmed
            .store(true, std::sync::atomic::Ordering::Relaxed);
    }

    fn stop(&self, retire_wal_windows: bool) -> io::Result<()> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_EMBEDDED_STOP_TOTAL);
        {
            let mut closed = self
                .closed
                .lock()
                .map_err(|_| io_other("embedded close state poisoned"))?;
            if *closed {
                return Ok(());
            }
            *closed = true;
        }
        // New checkpoint calls observe closed before effects; wait for entered
        // calls before retiring their WAL/maintenance drivers.
        #[cfg(feature = "native-runtime-checkpoints")]
        let _checkpoint_calls = self.checkpoint_calls.write()
            .map_err(|_| io_other("embedded checkpoint lifecycle poisoned"))?;
        let maintenance_result = embedded_stop_phase(crate::fsync_stats::STAGE_EMBEDDED_STOP_MAINTENANCE, || {
            self
                .maintenance
                .as_ref()
                .map(MaintenanceScheduler::stop_and_join)
                .transpose()
        });
        let packer_result = embedded_stop_phase(crate::fsync_stats::STAGE_EMBEDDED_STOP_PACKER, || {
            self
                .background_packer
                .as_ref()
                .map(crate::background_pack::BackgroundPacker::stop_and_join)
                .transpose()
        });
        let online_gc_result = embedded_stop_phase(crate::fsync_stats::STAGE_EMBEDDED_STOP_GC, || {
            self
                .online_gc_driver
                .as_ref()
                .map(crate::authority_gc_online::OnlineGarbageCollectionDriver::stop_and_join)
                .transpose()
        });
        let search_gc_result = self.search_gc_driver.as_ref()
            .map(crate::authority_gc_online::OnlineGarbageCollectionDriver::stop_and_join)
            .transpose();
        let online_backup_result = embedded_stop_phase(crate::fsync_stats::STAGE_EMBEDDED_STOP_BACKUP, || {
            self
                .online_backup_driver
                .as_ref()
                .map(crate::online_backup::OnlineBackupDriver::stop_and_join)
                .transpose()
        });
        let authority_watch_result = embedded_stop_phase(crate::fsync_stats::STAGE_EMBEDDED_STOP_WATCH, || {
            self.authority_watch.stop_and_join()
        });
        let archive_seal_result = embedded_stop_phase(crate::fsync_stats::STAGE_EMBEDDED_STOP_ARCHIVE, || {
            self
                .archive_seal_driver
                .as_ref()
                .map(crate::transcript_archive::ArchiveSealDriver::stop_and_join)
                .transpose()
        });
        let adoption_result = embedded_stop_phase(crate::fsync_stats::STAGE_EMBEDDED_STOP_ADOPTION, || {
            self.adoption_driver.stop_and_join()
        });
        let rotation_result = embedded_stop_phase(crate::fsync_stats::STAGE_EMBEDDED_STOP_ROTATION, || self.rotation_driver.stop_and_join());
        if retire_wal_windows {
            embedded_stop_phase(crate::fsync_stats::STAGE_EMBEDDED_STOP_CHECKPOINT, || checkpoint_wal_windows_for_fast_reopen(
                &self.database,
                self.search_projection.as_ref(),
            ));
        }
        let barrier_result = embedded_stop_phase(crate::fsync_stats::STAGE_EMBEDDED_STOP_BARRIER, || {
            self
                .wal_barrier_driver
                .as_ref()
                .map(crate::wal::WalBarrierDriver::stop_and_join)
                .transpose()
        });
        maintenance_result?;
        packer_result?;
        barrier_result?;
        online_gc_result?;
        search_gc_result?;
        online_backup_result?;
        archive_seal_result?;
        authority_watch_result?;
        adoption_result?;
        rotation_result?;
        Ok(())
    }
}

/// Existing opt-in durability diagnostics also expose local teardown spans.
/// Only fixed stage names and elapsed time are emitted, once per stopped phase.
fn embedded_stop_phase<T>(stage: usize, operation: impl FnOnce() -> T) -> T {
    let timer = crate::fsync_stats::StageTimer::new(stage);
    let diagnostic_start = std::env::var_os("TOFU_DB_FSYNC_STATS").map(|_| std::time::Instant::now());
    let result = operation();
    drop(timer);
    if let Some(started) = diagnostic_start {
        eprintln!("embedded_stop_phase={} elapsed_ns={}", crate::fsync_stats::STAGE_NAMES[stage], started.elapsed().as_nanos());
    }
    result
}

impl Drop for EmbeddedRuntime {
    fn drop(&mut self) {
        if self
            .drop_disarmed
            .load(std::sync::atomic::Ordering::Relaxed)
        {
            return;
        }
        let _ = self.close();
    }
}

/// Test-only fault switchboard for the FFI hardening tests (panic
/// containment, execute watchdog). Triggered by operation name so parallel
/// tests never cross-fire; real traffic never uses these operations.
#[cfg(test)]
pub(crate) mod test_fault {
    /// `execute_json` panics with a stable message.
    pub(crate) const PANIC_OPERATION: &str = "test.fault.panic";
    /// `execute_json` parks forever, simulating a wedged engine.
    pub(crate) const PARALLEL_OPERATION: &str = "test.fault.parallel";
    pub(crate) const DEMAND_BURST_OPERATION: &str = "test.fault.demand-burst-64";
    pub(crate) const PARK_OPERATION: &str = "test.fault.park";
    /// `open` panics with a stable message when the data-dir path carries
    /// this marker substring.
    pub(crate) const OPEN_PANIC_DIR_MARKER: &str = "embed-test-open-panic";

    pub(crate) fn inject(operation: &str) {
        if operation == PARALLEL_OPERATION {
            static GATE: std::sync::OnceLock<(std::sync::Mutex<usize>, std::sync::Condvar)> = std::sync::OnceLock::new();
            let (count, ready) = GATE.get_or_init(Default::default);
            let mut count = count.lock().unwrap();
            *count += 1;
            ready.notify_all();
            let (count, _) = ready.wait_timeout_while(count, std::time::Duration::from_secs(2), |n| *n < 2).unwrap();
            assert!(*count >= 2, "embedded execution is still serialized");
        }
        if operation == DEMAND_BURST_OPERATION {
            static GATE: std::sync::OnceLock<(std::sync::Mutex<usize>, std::sync::Condvar)> = std::sync::OnceLock::new();
            let (count, ready) = GATE.get_or_init(Default::default);
            let mut count = count.lock().unwrap();
            *count += 1;
            ready.notify_all();
            let (count, _) = ready.wait_timeout_while(count, std::time::Duration::from_secs(5), |n| *n < 64).unwrap();
            assert!(*count >= 64, "embedded demand burst waited behind worker count admission");
        }
        if operation == PANIC_OPERATION {
            panic!("embedded test fault: forced panic");
        }
        if operation == PARK_OPERATION {
            std::thread::park();
        }
    }
}

fn response_to_json(response: &Response) -> Value {
    let payload = if response.status == 0 {
        serde_json::from_slice::<Value>(&response.payload).unwrap_or(Value::Null)
    } else {
        Value::Null
    };
    json!({
        "status": response.status,
        "error_code": response.error_code,
        "error_message": response.error_message,
        "retryable": response.retryable,
        "payload": payload,
    })
}

/// Validate with the same serde number/UTF-8/recursion rules as Value, without
/// allocating a second payload tree. Float parsing preserves round-trip rules;
/// wide integer tokens and integer negative zero need Value's normalization.
struct PayloadShape { has_float_representation: bool }
impl<'de> serde::Deserialize<'de> for PayloadShape {
    fn deserialize<D: serde::Deserializer<'de>>(decoder: D) -> Result<Self, D::Error> {
        struct ShapeVisitor;
        impl<'de> serde::de::Visitor<'de> for ShapeVisitor {
            type Value = PayloadShape;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result { f.write_str("a JSON value") }
            fn visit_unit<E: serde::de::Error>(self) -> Result<Self::Value, E> { Ok(PayloadShape { has_float_representation: false }) }
            fn visit_bool<E: serde::de::Error>(self, _: bool) -> Result<Self::Value, E> { self.visit_unit() }
            fn visit_i64<E: serde::de::Error>(self, _: i64) -> Result<Self::Value, E> { self.visit_unit() }
            fn visit_u64<E: serde::de::Error>(self, _: u64) -> Result<Self::Value, E> { self.visit_unit() }
            fn visit_f64<E: serde::de::Error>(self, _: f64) -> Result<Self::Value, E> { Ok(PayloadShape { has_float_representation: true }) }
            fn visit_str<E: serde::de::Error>(self, _: &str) -> Result<Self::Value, E> { self.visit_unit() }
            fn visit_seq<A: serde::de::SeqAccess<'de>>(self, mut values: A) -> Result<Self::Value, A::Error> {
                let mut has_float_representation = false;
                while let Some(value) = values.next_element::<PayloadShape>()? {
                    has_float_representation |= value.has_float_representation;
                }
                Ok(PayloadShape { has_float_representation })
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(self, mut values: A) -> Result<Self::Value, A::Error> {
                let mut has_float_representation = false;
                while values.next_key::<PayloadKey>()?.is_some() {
                    has_float_representation |= values.next_value::<PayloadShape>()?.has_float_representation;
                }
                Ok(PayloadShape { has_float_representation })
            }
        }
        decoder.deserialize_any(ShapeVisitor)
    }
}

struct PayloadKey;
impl<'de> serde::Deserialize<'de> for PayloadKey {
    fn deserialize<D: serde::Deserializer<'de>>(decoder: D) -> Result<Self, D::Error> {
        struct KeyVisitor;
        impl<'de> serde::de::Visitor<'de> for KeyVisitor {
            type Value = PayloadKey;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result { f.write_str("a JSON object key") }
            fn visit_str<E: serde::de::Error>(self, _: &str) -> Result<Self::Value, E> { Ok(PayloadKey) }
        }
        decoder.deserialize_str(KeyVisitor)
    }
}

/// Only run after serde has validated the complete payload. Normal floating
/// tokens keep their JSON type and binary64 value on the client. Wide integer
/// tokens would instead turn into Python arbitrary-precision integers, while
/// Value converted them to f64; integer -0 also changes type. Retain the old
/// renderer for those cases. SIMD string skipping avoids rescanning large text
/// byte-by-byte just because its surrounding record contains a float.
fn payload_has_noncanonical_integer(payload: &[u8]) -> bool {
    let mut cursor = 0;
    while cursor < payload.len() {
        match payload[cursor] {
            b'"' => {
                cursor += 1;
                while cursor < payload.len() {
                    let Some(offset) = memchr::memchr2(b'"', b'\\', &payload[cursor..]) else { return false; };
                    cursor += offset;
                    if payload[cursor] == b'"' { cursor += 1; break; }
                    cursor += 2; // The validated escape and its following byte.
                }
            }
            b'-' | b'0'..=b'9' => {
                let start = cursor;
                cursor += 1;
                while cursor < payload.len() && matches!(payload[cursor], b'0'..=b'9' | b'.' | b'e' | b'E' | b'+' | b'-') { cursor += 1; }
                let token = &payload[start..cursor];
                if !token.iter().any(|byte| matches!(byte, b'.' | b'e' | b'E')) {
                    if token == b"-0" { return true; }
                    let integer = std::str::from_utf8(token).expect("validated JSON number is ASCII");
                    if if token[0] == b'-' { integer.parse::<i64>().is_err() } else { integer.parse::<u64>().is_err() } { return true; }
                }
            }
            _ => cursor += 1,
        }
    }
    false
}

pub(crate) fn dispatched_response_to_encoded_json(
    response: &crate::server::response::DispatchedResponse,
) -> io::Result<Vec<u8>> {
    if let Some(payload) = response.canonical_payload() {
        let _encode = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_RESPONSE_ENCODE);
        let _proof = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_PAYLOAD_PROOF);
        return encode_response_envelope(response, payload);
    }
    response_to_encoded_json(response)
}

fn response_to_encoded_json(response: &Response) -> io::Result<Vec<u8>> {
    let _encode = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_RESPONSE_ENCODE);
    let shape = (response.status == 0).then(|| {
        let _validate = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_PAYLOAD_VALIDATE);
        serde_json::from_slice::<PayloadShape>(&response.payload)
    });
    if matches!(&shape, Some(Ok(PayloadShape { has_float_representation: true })))
        && {
            let _numeric = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_NUMERIC_COMPATIBILITY);
            payload_has_noncanonical_integer(&response.payload)
        } {
        let _fallback = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_VALUE_FALLBACK);
        return serde_json::to_vec(&response_to_json(response)).map_err(io::Error::other);
    }
    let payload = if matches!(&shape, Some(Ok(_))) { response.payload.as_slice() } else { b"null" };
    encode_response_envelope(response, payload)
}

// Raw payload insertion has exactly two callers: immutable canonical evidence,
// or the original complete PayloadShape/numeric compatibility validation.
fn encode_response_envelope(response: &Response, payload: &[u8]) -> io::Result<Vec<u8>> {
    let _envelope = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_ENVELOPE_BUILD);
    #[derive(serde::Serialize)]
    struct Header<'a> { error_code: &'a Option<String>, error_message: &'a Option<String> }
    let mut encoded = Vec::with_capacity(payload.len().saturating_add(128));
    serde_json::to_writer(&mut encoded, &Header { error_code: &response.error_code, error_message: &response.error_message })
        .map_err(io::Error::other)?;
    // The metadata is serialized by serde. Only fixed envelope punctuation and
    // a completely validated JSON value are appended as raw bytes.
    encoded.pop();
    encoded.extend_from_slice(b",\"payload\":");
    encoded.extend_from_slice(payload);
    encoded.extend_from_slice(b",\"retryable\":");
    encoded.extend_from_slice(if response.retryable { b"true" } else { b"false" });
    encoded.extend_from_slice(b",\"status\":");
    serde_json::to_writer(&mut encoded, &response.status).map_err(io::Error::other)?;
    encoded.push(b'}');
    Ok(encoded)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encoded_response_preserves_value_envelopes_and_invalid_payload_policy() {
        let mut payloads = vec![
            b"null".to_vec(), b"true".to_vec(), b"18446744073709551615".to_vec(),
            b"18446744073709551616".to_vec(), b"1.000e-9".to_vec(), b"1e400".to_vec(),
            b"-0".to_vec(), b"-0.0".to_vec(), b"-9223372036854775809".to_vec(),
            br#"[18446744073709551615,1.25,"18446744073709551616\"-0"]"#.to_vec(),
            br#"[18446744073709551616,1.25]"#.to_vec(),
            br#"{"duplicate":1,"duplicate":2,"text":"\u4e2d\n\""}"#.to_vec(),
            br#"{"x":[false,null,{"decimal":1.5}]}"#.to_vec(),
            br#"{"key\u0000": "\ud800"}"#.to_vec(),
            b"[1,]".to_vec(), b"true false".to_vec(), vec![b'"', 255, b'"'],
            serde_json::to_vec(&json!({"large": "x\n中".repeat(100_000)})).unwrap(),
        ];
        for depth in [1, 126, 127, 128, 129, 256] {
            payloads.push(format!("{}0{}", "[".repeat(depth), "]".repeat(depth)).into_bytes());
        }
        for payload in payloads {
            for status in [0, 1] {
                let response = Response { correlation_id: [0; 16], schema_id: STORAGE_SCHEMA_VERSION,
                    payload: payload.clone(), status, error_code: Some("error-中".into()),
                    error_message: Some("quoted \" / newline\n".into()), retryable: status != 0 };
                let encoded = response_to_encoded_json(&response).unwrap();
                #[derive(serde::Deserialize)]
                #[serde(deny_unknown_fields)]
                struct Envelope<'a> {
                    status: u16, error_code: Option<String>, error_message: Option<String>,
                    retryable: bool, #[serde(borrow)] payload: &'a serde_json::value::RawValue,
                }
                // Decode the envelope separately: its extra level must not
                // reduce the payload's pre-existing serde recursion allowance.
                let envelope: Envelope<'_> = serde_json::from_slice(&encoded).unwrap();
                assert_eq!(envelope.status, response.status);
                assert_eq!(envelope.error_code, response.error_code);
                assert_eq!(envelope.error_message, response.error_message);
                assert_eq!(envelope.retryable, response.retryable);
                let decoded: Value = serde_json::from_str(envelope.payload.get()).unwrap();
                assert_eq!(decoded, response_to_json(&response)["payload"], "status={status}, payload bytes={}", payload.len());
            }
        }
    }

    fn system_now_plus(millis: u64) -> u64 {
        now_unix_ms() + millis
    }

    #[test]
    fn payload_serialization_enforces_the_wire_ceiling() {
        // A JSON string of N chars serializes to N + 2 bytes (quotes).
        let just_under = Value::String("x".repeat(MAX_EMBEDDED_PAYLOAD_BYTES - 2));
        let encoded = serialize_payload(&just_under, MAX_EMBEDDED_PAYLOAD_BYTES).unwrap();
        assert_eq!(encoded.len(), MAX_EMBEDDED_PAYLOAD_BYTES);

        let one_over = Value::String("x".repeat(MAX_EMBEDDED_PAYLOAD_BYTES - 1));
        let error = serialize_payload(&one_over, MAX_EMBEDDED_PAYLOAD_BYTES).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        assert!(error.to_string().contains("payload bound"));

        // A payload far beyond the cap is rejected by the bounded writer
        // itself, so the buffer never grows to the full document size.
        let huge = Value::String("x".repeat(4 * MAX_EMBEDDED_PAYLOAD_BYTES));
        let error = serialize_payload(&huge, MAX_EMBEDDED_PAYLOAD_BYTES).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);

        // Non-string payloads serialize identically to the uncapped path.
        let document = json!({"nested": [1, 2.5, "three"], "ok": true});
        assert_eq!(
            serialize_payload(&document, MAX_EMBEDDED_PAYLOAD_BYTES).unwrap(),
            serde_json::to_vec(&document).unwrap()
        );
        let invalid = json!({"key": Value::Null});
        assert!(serialize_payload(&invalid, MAX_EMBEDDED_PAYLOAD_BYTES).is_ok());
    }

    #[test]
    fn large_payload_serialization_is_independent_of_operation() {
        let payload = Value::String("x".repeat(MAX_EMBEDDED_PAYLOAD_BYTES));
        for operation in ["swarm.agent.save", "turn.projection.update", "raw_archive.put", "record.put", "system.health", "plugin.row.put", "unknown"] {
            let encoded = serialize_payload(&payload, payload_limit_for_operation(operation)).unwrap();
            assert_eq!(encoded.len(), MAX_EMBEDDED_PAYLOAD_BYTES + 2);
        }
        let demand = Value::String("x".repeat(70 * 1024 * 1024));
        let encoded = serialize_payload(&demand, payload_limit_for_operation("task_results.checkpoint")).unwrap();
        assert_eq!(encoded.len(), 70 * 1024 * 1024 + 2);
    }

    #[test]
    fn embedded_round_trips_the_same_semantics_as_the_framed_path() {
        let directory = tempfile::tempdir().unwrap();
        // A not-yet-existing projection path OUTSIDE the authority (the
        // daemon's projection/authority separation rule): the runtime
        // initializes it like `tofu-db init` + `serve` on a fresh deployment.
        let projection_parent = tempfile::tempdir().unwrap();
        let projection = projection_parent.path().join("projection");
        {
            let mut authority = AuthorityDatabase::initialize(directory.path()).unwrap();
            let mut transaction = authority.begin(7, 11).unwrap();
            authority
                .entity_put(
                    &mut transaction,
                    crate::entity::EntityKey::new(7, 11, "seed", b"k").unwrap(),
                    b"seeded".to_vec(),
                )
                .unwrap();
            authority.commit(transaction).unwrap();
            drop(authority);
        }
        let mut secret = *b"embedded-transport-test-secret-0001";
        let runtime = EmbeddedRuntime::open(
            directory.path(),
            11,
            Some(7),
            Some(&projection),
            &mut secret,
        )
        .unwrap();
        let put = runtime.execute_json(&EmbeddedRequest {
            operation: "record.put".to_owned(),
            payload: &json!({
                "namespace": "embed", "key": "k/1",
                "value": {"n": 1}, "user_id": 11,
            }),
            command_id: Some("embed-cmd-1".to_owned()),
            deadline_unix_ms: system_now_plus(5_000),
        });
        let put = put.unwrap();
        assert_eq!(put["status"], json!(0), "put failed: {put}");
        let get = runtime
            .execute_json(&EmbeddedRequest {
                operation: "record.get".to_owned(),
                payload: &json!({"namespace": "embed", "key": "k/1", "user_id": 11}),
                command_id: None,
                deadline_unix_ms: system_now_plus(5_000),
            })
            .unwrap();
        assert_eq!(get["status"], json!(0), "get failed: {get}");
        assert_eq!(get["payload"]["value"], json!({"n": 1}));
        assert_eq!(get["payload"]["version"], json!(1));
        // Command replay: the same command id answers from its receipt
        // instead of double-applying, exactly like the wire path.
        let replay = runtime
            .execute_json(&EmbeddedRequest {
                operation: "record.put".to_owned(),
                payload: &json!({
                    "namespace": "embed", "key": "k/1",
                    "value": {"n": 1}, "user_id": 11,
                }),
                command_id: Some("embed-cmd-1".to_owned()),
                deadline_unix_ms: system_now_plus(5_000),
            })
            .unwrap();
        assert_eq!(replay["status"], json!(0), "receipt replay failed: {replay}");
        // Unknown operations classify identically to the daemon.
        let unknown = runtime
            .execute_json(&EmbeddedRequest {
                operation: "no.such.operation".to_owned(),
                payload: &json!({}),
                command_id: None,
                deadline_unix_ms: system_now_plus(5_000),
            })
            .unwrap();
        assert_ne!(unknown["status"], json!(0));
        assert!(unknown["error_code"].as_str().is_some());
        // Control operations answer over the embedded transport with the
        // same launch budgets the daemon's readiness envelope carries, so
        // the supervisor's health preflight and pool sizing work unchanged.
        let health = runtime
            .execute_json(&EmbeddedRequest {
                operation: "system.health".to_owned(),
                payload: &json!({}),
                command_id: None,
                deadline_unix_ms: system_now_plus(5_000),
            })
            .unwrap();
        assert_eq!(health["status"], json!(0), "health failed: {health}");
        assert_eq!(health["payload"]["ready"], json!(true));
        assert_eq!(health["payload"]["walDurability"], json!("strict"));
        assert_eq!(
            health["payload"]["components"]["listener"],
            json!({"configured": false, "healthy": true})
        );
        assert!(health["payload"]["resourceBudget"]["maximumConnections"].is_u64());
        let metrics = runtime
            .execute_json(&EmbeddedRequest {
                operation: "system.metrics".to_owned(),
                payload: &json!({}),
                command_id: None,
                deadline_unix_ms: system_now_plus(5_000),
            })
            .unwrap();
        assert_eq!(metrics["status"], json!(0), "metrics failed: {metrics}");
        runtime.close().unwrap();
        // A second graceful close is a no-op.
        runtime.close().unwrap();
        drop(runtime);
        // Reopen replays ~nothing and keeps the data.
        let mut authority = AuthorityDatabase::open(directory.path()).unwrap();
        let mut read = authority.begin(7, 11).unwrap();
        assert!(
            authority
                .entity_get(
                    &mut read,
                    &crate::entity::EntityKey::new(7, 11, "seed", b"k").unwrap(),
                )
                .unwrap()
                .is_some()
        );
    }
}

#[cfg(test)]
#[path = "embed_query_cache_tests.rs"]
mod query_cache_tests;
