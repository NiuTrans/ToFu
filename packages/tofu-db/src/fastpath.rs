//! Opt-in native local authority with asynchronous verified snapshot replication.
//!
//! One source lease fences the retained durable seed for the entire serving
//! lifetime. FASTPATH makes classic opens fail closed; recovery uses only a
//! complete, same-lineage native backup. Search projections remain disposable.
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::at_rest::AtRestKey;
use crate::backup;
use crate::control::{ControlFile, ControlState};
use crate::engine::{acquire_lease, Engine};
use crate::vfs::{
    sync_all_barrier, sync_directory_barrier, FileKind, OpenRequest, RealVfs, Vfs, VfsFile,
};

const MARKER: &str = "FASTPATH";
const PENDING_MARKER: &str = "FASTPATH_PENDING";
pub use crate::generated_storage_v2::{
    MAX_NATIVE_FASTPATH_SYNC_INTERVAL_MS, MIN_NATIVE_FASTPATH_SYNC_INTERVAL_MS,
};
use crate::generated_storage_v2::{
    MIN_NATIVE_FASTPATH_FSYNC_SPEEDUP, NATIVE_FASTPATH_MARKER_MAX_BYTES,
};
const MARKER_LIMIT: u64 = NATIVE_FASTPATH_MARKER_MAX_BYTES;
const FORMAT: &str = "tofu.native-fastpath.v1";

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct FastpathConfig {
    pub durable_authority: PathBuf,
    pub local_authority: PathBuf,
    pub shadow_directory: PathBuf,
    pub sync_interval_ms: Option<u64>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Marker {
    format: String,
    authority_uuid: [u8; 16],
    local_authority: PathBuf,
    shadow_directory: PathBuf,
    seed_sequence: u64,
    seed_hash: [u8; 32],
    interval_ms: u64,
}

/// Retained by both daemon and replication worker, including bounded-stop
/// detachment: a late publisher must never outlive this source authority lease.
pub(crate) struct FastpathLease {
    _source_lease: Mutex<Box<dyn VfsFile>>,
    pub config: FastpathConfig,
    pub interval: Duration,
    pub shadow_sequence: u64,
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

impl FastpathConfig {
    pub fn new(durable: &Path, local_base: &Path, interval_ms: Option<u64>) -> io::Result<Self> {
        if !durable.is_absolute() || !local_base.is_absolute() {
            return Err(invalid("native fastpath paths must be absolute"));
        }
        if interval_ms.is_some_and(|value| {
            !(MIN_NATIVE_FASTPATH_SYNC_INTERVAL_MS..=MAX_NATIVE_FASTPATH_SYNC_INTERVAL_MS)
                .contains(&value)
        }) {
            return Err(invalid("native fastpath interval must be 1000..3600000 ms"));
        }
        let durable = fs::canonicalize(durable)?;
        // First activation requires an existing explicit base. A recorded
        // active selection may recreate its lost base only after path/volume
        // admission, before the same-lineage native restore.
        let base = backup::resolved_candidate(local_base)?;
        if !base.exists() && !durable.join(MARKER).exists() {
            return Err(invalid(
                "first native fastpath activation requires an existing local base",
            ));
        }
        backup::require_disjoint_paths(
            &durable,
            &base,
            "fastpath base overlaps durable authority",
        )?;
        let identity = durable
            .to_str()
            .ok_or_else(|| invalid("fastpath path must be UTF-8"))?;
        let namespace = format!("{:x}", Sha256::digest(identity.as_bytes()));
        let local_authority = base.join("tofudb-fastpath").join(&namespace);
        let shadow_directory = durable
            .parent()
            .ok_or_else(|| invalid("authority has no parent"))?
            .join("tofudb-fastpath-shadow")
            .join(namespace);
        backup::require_disjoint_paths(
            &local_authority,
            &shadow_directory,
            "fastpath front overlaps shadow",
        )?;
        // Reject an existing symlink leaf rather than resolving two deployments
        // onto one authority. Native restore owns the absent leaf's creation.
        let namespace_parent = local_authority.parent().unwrap();
        if namespace_parent.exists() && fs::canonicalize(namespace_parent)? != namespace_parent {
            return Err(invalid(
                "native fastpath namespace must not contain symlinks",
            ));
        }
        if fs::symlink_metadata(&local_authority).is_ok()
            && fs::canonicalize(&local_authority)? != local_authority
        {
            return Err(invalid(
                "native fastpath namespace must not contain symlinks",
            ));
        }
        if shadow_directory.exists() && fs::canonicalize(&shadow_directory)? != shadow_directory {
            return Err(invalid("native fastpath shadow must not contain symlinks"));
        }
        let config = Self {
            durable_authority: durable,
            local_authority,
            shadow_directory,
            sync_interval_ms: interval_ms,
        };
        if !base.exists() {
            config.validate_marker(&read_marker(&config.durable_authority.join(MARKER))?)?;
            let mut parent = base.as_path();
            while !parent.exists() {
                parent = parent
                    .parent()
                    .ok_or_else(|| invalid("local base has no ancestor"))?;
            }
            if crate::resource_probe::volume_class(parent)
                != crate::resource_probe::VolumeClass::Local
            {
                return Err(invalid(
                    "lost native fastpath base requires an observed local volume",
                ));
            }
            require_nonvolatile_local_volume(parent)?;
            create_directory_durable(&base)?;
        }
        Ok(config)
    }

    #[cfg(test)]
    pub(crate) fn prepare_for_test(
        &self,
        key: Option<AtRestKey>,
    ) -> io::Result<Arc<FastpathLease>> {
        self.prepare_with_probe(key, false)
    }

    pub(crate) fn prepare(&self, key: Option<AtRestKey>) -> io::Result<Arc<FastpathLease>> {
        self.prepare_with_probe(key, true)
    }

    fn prepare_with_probe(
        &self,
        key: Option<AtRestKey>,
        probe: bool,
    ) -> io::Result<Arc<FastpathLease>> {
        let path = self.durable_authority.join(MARKER);
        if probe
            && crate::resource_probe::volume_class(
                self.local_authority.parent().unwrap().parent().unwrap(),
            ) != crate::resource_probe::VolumeClass::Local
        {
            return Err(invalid(
                "native fastpath requires an observed local filesystem",
            ));
        }
        if probe {
            require_nonvolatile_local_volume(
                self.local_authority.parent().unwrap().parent().unwrap(),
            )?;
        }
        if path.exists() {
            let lease = acquire_lease(&self.durable_authority, &RealVfs)?;
            let marker = read_marker(&path)?;
            self.validate_marker(&marker)?;
            let mut control = ControlFile::open(&self.durable_authority)?;
            validate_seed(&control.read_current()?, &marker)?;
            validate_shadow(
                &self.shadow_directory,
                Uuid::from_bytes(marker.authority_uuid),
                key.clone(),
            )?;
            if !self.local_authority.exists() || self.local_authority.join("RESTORE").exists() {
                create_directory_durable(self.local_authority.parent().unwrap())?;
                backup::restore_latest_backup_with_key(
                    &self.shadow_directory,
                    &self.local_authority,
                    key.clone(),
                )?;
                // Native restore syncs the leaf; the tier owns publication of
                // that leaf in its namespaced parent before readiness.
                sync_directory_barrier(&RealVfs, self.local_authority.parent().unwrap())?;
            }
            let shadow_sequence =
                validate_front(&self.local_authority, &self.shadow_directory, &marker, key)?;
            return Ok(Arc::new(FastpathLease {
                _source_lease: Mutex::new(lease),
                config: self.clone(),
                interval: Duration::from_millis(
                    self.sync_interval_ms.unwrap_or(marker.interval_ms),
                ),
                shadow_sequence,
            }));
        }
        // First activation is offline under the ordinary native process lease.
        // Any interruption before marker publication leaves the classic authority
        // usable. Unfamiliar/partial fronts are never overwritten or removed.
        let pending_path = self.durable_authority.join(PENDING_MARKER);
        let pending = if pending_path.exists() {
            Some(read_marker(&pending_path)?)
        } else {
            None
        };
        if self.local_authority.exists() || (self.shadow_directory.exists() && pending.is_none()) {
            return Err(invalid(
                "unclaimed fastpath artifacts exist; verify/export them before activation",
            ));
        }
        let mut source = Engine::open_with_key(&self.durable_authority, key.clone())?;
        if let Some(marker) = pending.as_ref() {
            self.validate_marker(marker)?;
            validate_seed(source.state(), marker)?;
        }
        if probe {
            measure_candidate(
                &self.durable_authority,
                self.local_authority.parent().unwrap().parent().unwrap(),
            )?;
        }
        let bytes = backup::bounded_directory_bytes(&self.durable_authority)?;
        let retained = if pending.is_some() && self.shadow_directory.exists() {
            backup::bounded_directory_bytes(&self.shadow_directory)?
        } else {
            0
        };
        require_capacity(&self.shadow_directory, bytes.saturating_sub(retained))?;
        require_capacity(
            self.local_authority.parent().unwrap().parent().unwrap(),
            bytes,
        )?;
        if pending.is_none() {
            let marker = Marker {
                format: FORMAT.into(),
                authority_uuid: *source.state().authority_uuid.as_bytes(),
                local_authority: self.local_authority.clone(),
                shadow_directory: self.shadow_directory.clone(),
                seed_sequence: source.state().durable_sequence,
                seed_hash: source.state().root_hash,
                interval_ms: self
                    .sync_interval_ms
                    .unwrap_or(MIN_NATIVE_FASTPATH_SYNC_INTERVAL_MS),
            };
            publish_marker(&pending_path, &marker)?;
        }
        create_directory_durable(&self.shadow_directory)?;
        let started = Instant::now();
        backup::create_incremental_backup_with_key(
            &mut source,
            &self.shadow_directory,
            key.clone(),
        )?;
        let interval_ms = self.sync_interval_ms.unwrap_or_else(|| {
            // No overlapping queue of whole-corpus walks: the observed initial
            // copy cost derives cadence up as well as down. Explicit overrides win.
            (started.elapsed().as_millis() as u64)
                .saturating_mul(2)
                .clamp(
                    MIN_NATIVE_FASTPATH_SYNC_INTERVAL_MS,
                    MAX_NATIVE_FASTPATH_SYNC_INTERVAL_MS,
                )
        });
        let marker = Marker {
            format: FORMAT.into(),
            authority_uuid: *source.state().authority_uuid.as_bytes(),
            local_authority: self.local_authority.clone(),
            shadow_directory: self.shadow_directory.clone(),
            seed_sequence: source.state().durable_sequence,
            seed_hash: source.state().root_hash,
            interval_ms,
        };
        validate_shadow(
            &self.shadow_directory,
            Uuid::from_bytes(marker.authority_uuid),
            key.clone(),
        )?;
        // Publish the relocation fence before restoring. A killed restore
        // resumes through the native RESTORE marker, never the stale seed.
        publish_marker(&path, &marker)?;
        // ACTIVE is durable before the private preparation receipt is retired.
        fs::remove_file(&pending_path)?;
        sync_directory_barrier(&RealVfs, &self.durable_authority)?;
        drop(source);
        self.prepare_with_probe(key, false)
    }

    fn validate_marker(&self, marker: &Marker) -> io::Result<()> {
        if marker.format != FORMAT
            || marker.local_authority != self.local_authority
            || marker.shadow_directory != self.shadow_directory
            || !(MIN_NATIVE_FASTPATH_SYNC_INTERVAL_MS..=MAX_NATIVE_FASTPATH_SYNC_INTERVAL_MS)
                .contains(&marker.interval_ms)
        {
            return Err(invalid(
                "native fastpath selection differs from its recorded lineage; retire/export first",
            ));
        }
        Ok(())
    }
}

fn validate_seed(state: &ControlState, marker: &Marker) -> io::Result<()> {
    if state.authority_uuid != Uuid::from_bytes(marker.authority_uuid)
        || state.durable_sequence != marker.seed_sequence
        || state.root_hash != marker.seed_hash
    {
        return Err(invalid(
            "native fastpath durable seed changed; refusing split authority",
        ));
    }
    Ok(())
}

fn validate_shadow(
    path: &Path,
    uuid: Uuid,
    key: Option<AtRestKey>,
) -> io::Result<backup::BackupManifest> {
    let vfs = crate::at_rest::destination_vfs(&[path], key);
    let _lease = backup::acquire_backup_lease(path, vfs.as_ref())?;
    backup::ensure_no_prune(path, vfs.as_ref())?;
    let manifests = backup::existing_manifests(&path.join("snapshots"), vfs.as_ref())?;
    if manifests.is_empty()
        || manifests
            .iter()
            .any(|manifest| manifest.authority_uuid != uuid)
    {
        return Err(invalid(
            "native fastpath shadow is missing or belongs to another authority",
        ));
    }
    manifests
        .into_iter()
        .max_by_key(|manifest| {
            (
                manifest.durable_sequence,
                manifest.source_control_generation,
            )
        })
        .ok_or_else(|| invalid("native fastpath has no complete shadow generation"))
}

fn validate_front(
    front: &Path,
    shadow: &Path,
    marker: &Marker,
    key: Option<AtRestKey>,
) -> io::Result<u64> {
    let source = validate_shadow(shadow, Uuid::from_bytes(marker.authority_uuid), key.clone())?;
    let engine = Engine::open_with_key(front, key)?;
    let state = engine.state();
    if state.authority_uuid != Uuid::from_bytes(marker.authority_uuid)
        || state.durable_sequence < source.durable_sequence
        || (state.durable_sequence == source.durable_sequence
            && state.root_hash != source.root_hash)
    {
        return Err(invalid(
            "native fastpath front is foreign, stale, or diverged; refusing fallback",
        ));
    }
    if state.durable_sequence > source.durable_sequence && source.durable_sequence > 0 {
        let ancestor = engine
            .transaction_at(source.durable_sequence)?
            .ok_or_else(|| invalid("native fastpath cannot prove shadow ancestry"))?;
        if ancestor.record_hash != source.root_hash {
            return Err(invalid(
                "native fastpath shadow is not an ancestor of the surviving front",
            ));
        }
    }
    Ok(source.durable_sequence)
}

fn read_marker(path: &Path) -> io::Result<Marker> {
    read_marker_with_vfs(path, &RealVfs)
}

fn read_marker_with_vfs(path: &Path, vfs: &dyn Vfs) -> io::Result<Marker> {
    let mut file = vfs.open(
        path,
        OpenRequest {
            read: true,
            ..OpenRequest::default()
        },
    )?;
    let length = file.len()?;
    if length > MARKER_LIMIT {
        return Err(invalid("native fastpath marker too large"));
    }
    let mut bytes = vec![0; length as usize];
    file.read_exact_at(0, &mut bytes)?;
    serde_json::from_slice(&bytes)
        .map_err(|error| invalid(format!("invalid native fastpath marker: {error}")))
}

fn publish_marker(path: &Path, marker: &Marker) -> io::Result<()> {
    publish_marker_with_vfs(path, marker, &RealVfs)
}

fn publish_marker_with_vfs(path: &Path, marker: &Marker, vfs: &dyn Vfs) -> io::Result<()> {
    let temp = path.with_file_name(format!(".fastpath-{}", Uuid::new_v4()));
    let bytes = serde_json::to_vec(marker).map_err(|error| invalid(error.to_string()))?;
    if bytes.len() as u64 > MARKER_LIMIT {
        return Err(invalid("native fastpath marker too large"));
    }
    let mut file = vfs.open(
        &temp,
        OpenRequest {
            write: true,
            create_new: true,
            ..OpenRequest::default()
        },
    )?;
    file.write_all_at(0, &bytes)?;
    sync_all_barrier(file.as_mut())?;
    vfs.rename(&temp, path)?;
    sync_directory_barrier(vfs, path.parent().unwrap())
}

pub(crate) fn require_classic_authority(data_dir: &Path, vfs: &dyn Vfs) -> io::Result<()> {
    match vfs.metadata(&data_dir.join(MARKER)) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
        Ok(_) => Err(invalid("native FASTPATH authority is active; use the recorded fastpath or export/retire it, never open the stale seed")),
    }
}

fn create_directory_durable(path: &Path) -> io::Result<()> {
    create_directory_durable_with_vfs(path, &RealVfs)
}

fn create_directory_durable_with_vfs(path: &Path, vfs: &dyn Vfs) -> io::Result<()> {
    let mut missing = Vec::new();
    let mut ancestor = path;
    loop {
        match vfs.metadata(ancestor) {
            Ok(FileKind::Directory) => break,
            Ok(_) => {
                return Err(invalid(
                    "native fastpath directory ancestor is not a directory",
                ))
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                missing.push(ancestor.to_path_buf());
                ancestor = ancestor
                    .parent()
                    .ok_or_else(|| invalid("directory has no ancestor"))?;
            }
            Err(error) => return Err(error),
        }
    }
    for directory in missing.into_iter().rev() {
        match vfs.create_dir(&directory) {
            Ok(()) => {}
            Err(error)
                if error.kind() == io::ErrorKind::AlreadyExists
                    && vfs.metadata(&directory)? == FileKind::Directory => {}
            Err(error) => return Err(error),
        }
        sync_directory_barrier(vfs, directory.parent().unwrap())?;
        sync_directory_barrier(vfs, &directory)?;
    }
    Ok(())
}

fn require_capacity(path: &Path, bytes: u64) -> io::Result<()> {
    let mut ancestor = path;
    while !ancestor.exists() {
        ancestor = ancestor
            .parent()
            .ok_or_else(|| invalid("capacity path has no ancestor"))?;
    }
    let available = crate::resource_probe::probe_launch_resources(ancestor)
        .volume_free_bytes
        .ok_or_else(|| invalid("native fastpath requires observed free space"))?;
    let reserve = (bytes / 20).max(crate::resource_probe::MIN_WRITABLE_VOLUME_FREE_BYTES);
    if available < bytes.saturating_add(reserve) {
        return Err(io::Error::new(
            io::ErrorKind::StorageFull,
            "native fastpath lacks copy space and reserve",
        ));
    }
    Ok(())
}

fn require_nonvolatile_local_volume(path: &Path) -> io::Result<()> {
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::ffi::OsStrExt;
        let path = std::ffi::CString::new(path.as_os_str().as_bytes())
            .map_err(|_| invalid("native fastpath path contains NUL"))?;
        let mut information = std::mem::MaybeUninit::<libc::statfs>::uninit();
        // The kernel initializes the structure only on success; the private
        // CString remains alive through the syscall.
        if unsafe { libc::statfs(path.as_ptr(), information.as_mut_ptr()) } != 0 {
            return Err(io::Error::last_os_error());
        }
        let information = unsafe { information.assume_init() };
        if matches!(
            (information.f_type as u64) & 0xffff_ffff,
            0x0102_1994 | 0x8584_58f6
        ) {
            return Err(invalid(
                "native fastpath cannot acknowledge durable commits on tmpfs or ramfs",
            ));
        }
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = path;
    }
    Ok(())
}

fn measure_candidate(durable: &Path, local: &Path) -> io::Result<()> {
    require_nonvolatile_local_volume(local)?;
    if crate::resource_probe::volume_class(local) != crate::resource_probe::VolumeClass::Local {
        return Err(invalid(
            "native fastpath requires an observed local filesystem",
        ));
    }
    let measure = |directory: &Path| -> io::Result<Duration> {
        let path = directory.join(format!(".native-fastpath-probe-{}", Uuid::new_v4()));
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)?;
        let result = (|| {
            let mut samples = Vec::with_capacity(16);
            for _ in 0..16 {
                let start = Instant::now();
                file.write_all(&[0; 4096])?;
                file.sync_all()?;
                samples.push(start.elapsed());
            }
            samples.sort();
            Ok(samples[8])
        })();
        // Only this invocation's exclusive-create scratch artifact is reclaimed.
        let cleanup = fs::remove_file(&path);
        match result {
            Ok(value) => {
                cleanup?;
                Ok(value)
            }
            Err(error) => Err(error),
        }
    };
    let slow = measure(durable)?;
    let fast = measure(local)?;
    if slow < fast.saturating_mul(MIN_NATIVE_FASTPATH_FSYNC_SPEEDUP) {
        return Err(invalid(
            "native fastpath fsync probe did not show a 3x improvement",
        ));
    }
    eprintln!(
        "native_fastpath_probe durable_fsync_us={} local_fsync_us={}",
        slow.as_micros(),
        fast.as_micros()
    );
    Ok(())
}

/// Offline lossless exit: export the surviving front (including its WAL tail)
/// to a NEW authority directory. The old seed, marker and shadow are retained;
/// callers explicitly select the verified new authority root after export.
pub fn export(
    durable: &Path,
    target: &Path,
    key: Option<AtRestKey>,
) -> io::Result<backup::BackupManifest> {
    let durable = fs::canonicalize(durable)?;
    let _lease = acquire_lease(&durable, &RealVfs)?;
    let marker = read_marker(&durable.join(MARKER))?;
    if marker.format != FORMAT {
        return Err(invalid("unknown native fastpath marker"));
    }
    backup::require_disjoint_paths(&durable, target, "export overlaps durable seed")?;
    backup::require_disjoint_paths(
        &marker.local_authority,
        target,
        "export overlaps local authority",
    )?;
    if target.exists() {
        return Err(invalid("native fastpath export requires an absent target"));
    }
    validate_seed(&ControlFile::open(&durable)?.read_current()?, &marker)?;
    validate_shadow(
        &marker.shadow_directory,
        Uuid::from_bytes(marker.authority_uuid),
        key.clone(),
    )?;
    if marker.local_authority.exists() {
        validate_front(
            &marker.local_authority,
            &marker.shadow_directory,
            &marker,
            key.clone(),
        )?;
        let mut front = Engine::open_with_key(&marker.local_authority, key.clone())?;
        // Restore resets physical CONTROL generations. An unchanged logical
        // commit already has a complete shadow: preserve its existing manifest
        // identity instead of publishing a different physical witness at the
        // same sequence/hash filename.
        let shadow = validate_shadow(
            &marker.shadow_directory,
            Uuid::from_bytes(marker.authority_uuid),
            key.clone(),
        )?;
        if front.state().durable_sequence > shadow.durable_sequence {
            backup::create_incremental_backup_with_key(
                &mut front,
                &marker.shadow_directory,
                key.clone(),
            )?;
        }
    }
    backup::restore_latest_backup_with_key(&marker.shadow_directory, target, key)
}

#[cfg(test)]
mod tests;
