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
use crate::generated_storage_operations::STORAGE_SCHEMA_VERSION;
use crate::generated_storage_v2::{
    MAX_DAEMON_AUTH_SECRET_BYTES, MAX_IN_FLIGHT_FRAMES, MIN_DAEMON_AUTH_SECRET_BYTES,
    PROTOCOL_VERSION,
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
    data_dir: PathBuf,
    scope: AuthenticatedScope,
    auth_token: [u8; 32],
    listener: LoopbackListenerConfig,
    resource_budget: DaemonResourceBudget,
    maintenance: MaintenanceSchedulerConfig,
    search_projection_dir: Option<PathBuf>,
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
        let resource_budget = DaemonResourceBudget::from_snapshot(probe_launch_resources(data_dir));
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
                maximum_connections: resource_budget.maximum_connections,
                connection_stack_bytes: resource_budget.connection_stack_bytes,
                ..LoopbackListenerConfig::default()
            },
            resource_budget,
            maintenance,
            search_projection_dir: None,
        })
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

pub fn serve_supervised<R: Read + Send + 'static, W: Write>(
    config: DaemonConfig,
    parent_lease_reader: R,
    readiness_writer: &mut W,
) -> io::Result<LoopbackListenerMetrics> {
    let mut authority = AuthorityDatabase::open(&config.data_dir)?;
    authority.configure_timer_live_capacity(config.resource_budget.timer_live_capacity)?;
    let database = Arc::new(Mutex::new(authority));
    let search_projection_configured = config.search_projection_dir.is_some();
    let search_projection = config.search_projection_dir.as_ref().and_then(|path| {
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
        result
            .ok()
            .map(|projection| Arc::new(Mutex::new(projection)))
    });
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
        config.resource_budget,
        config.maintenance,
        usize::from(config.scope.tenant_id.is_some()),
        search_projection_configured,
        search_projection.is_some(),
    )?;
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
    let result = listener.run_with_search_and_gc_budget(
        database,
        search_projection,
        authenticator,
        FrameAdmissionBudget::new(
            MAX_IN_FLIGHT_FRAMES,
            config.resource_budget.maximum_frame_bytes,
        )?,
        AuthorityGarbageCollectionBudget::from_resource_budget(config.resource_budget),
        || {
            if let Some(observer) = maintenance_observer.as_ref() {
                observer.require_healthy()?;
            }
            parent_lease.poll_alive()
        },
    );
    let maintenance_result = maintenance
        .as_ref()
        .map(MaintenanceScheduler::stop_and_join)
        .transpose();
    let parent_result = parent_lease.join_if_finished();
    let listener_metrics = result?;
    maintenance_result?;
    parent_result?;
    Ok(listener_metrics)
}

fn write_readiness(
    writer: &mut impl Write,
    port: u16,
    resource_budget: DaemonResourceBudget,
    maintenance: MaintenanceSchedulerConfig,
    registered_maintenance_scopes: usize,
    search_projection_configured: bool,
    search_projection_available: bool,
) -> io::Result<()> {
    let authority_gc = AuthorityGarbageCollectionBudget::from_resource_budget(resource_budget);
    let mut encoded = serde_json::to_vec(&serde_json::json!({
        "type": "storage.ready",
        "protocol": PROTOCOL_VERSION,
        "port": port,
        "backend": "tofudb",
        "schemaId": STORAGE_SCHEMA_VERSION,
        "preAuthority": true,
        "resourceBudget": {
            "logicalCpus": resource_budget.snapshot.logical_cpus,
            "memoryCapacityBytes": resource_budget.snapshot.memory_capacity_bytes,
            "memoryHeadroomBytes": resource_budget.snapshot.memory_headroom_bytes,
            "volumeFreeBytes": resource_budget.snapshot.volume_free_bytes,
            "minimumWritableVolumeFreeBytes": MIN_WRITABLE_VOLUME_FREE_BYTES,
            "maximumConnections": resource_budget.maximum_connections,
            "maximumFrameBytes": resource_budget.maximum_frame_bytes,
            "connectionStackBytes": resource_budget.connection_stack_bytes,
            "searchProjectionMaximumBytesPerOwner": resource_budget.search_projection_maximum_bytes_per_owner,
            "timerLiveCapacity": resource_budget.timer_live_capacity,
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
        },
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::resource_probe::LaunchResourceSnapshot;

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
        AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut secret = [b'y'; 32];
        let config = DaemonConfig::new(directory.path(), 11, Some(7), &mut secret).unwrap();
        let mut readiness = Vec::new();
        let metrics =
            serve_supervised(config, io::Cursor::new(Vec::new()), &mut readiness).unwrap();
        assert_eq!(metrics, LoopbackListenerMetrics::default());
        assert!(readiness.len() <= MAX_READINESS_BYTES);
        let document: serde_json::Value = serde_json::from_slice(&readiness).unwrap();
        assert_eq!(document["type"], "storage.ready");
        assert_eq!(document["protocol"], PROTOCOL_VERSION);
        assert_eq!(document["backend"], "tofudb");
        assert_eq!(document["preAuthority"], true);
        assert!(document["port"].as_u64().unwrap() > 0);
        assert!(document["resourceBudget"]["maximumConnections"]
            .as_u64()
            .is_some_and(|value| (1..=64).contains(&value)));
        assert!(document["resourceBudget"]["maximumFrameBytes"]
            .as_u64()
            .is_some_and(|value| (8 * 1024 * 1024..=128 * 1024 * 1024).contains(&value)));
        assert!(document["resourceBudget"]["timerLiveCapacity"]
            .as_u64()
            .is_some_and(|value| (1..=64).contains(&value)));
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
