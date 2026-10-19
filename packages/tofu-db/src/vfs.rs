//! Bounded filesystem interface and deterministic durability simulator.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{self, IoSlice, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard};

use fs2::FileExt;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct OpenRequest {
    pub read: bool,
    pub write: bool,
    pub create: bool,
    pub create_new: bool,
    pub truncate: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FileKind {
    File,
    Directory,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct BoundedDirectoryEntries {
    pub entries: Vec<PathBuf>,
    pub has_more: bool,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct BoundedDirectoryMatches {
    pub matches: Vec<PathBuf>,
    pub scanned_entries: usize,
    pub has_more_entries: bool,
    pub has_more_matches: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Operation {
    Open,
    CreateDir,
    Read,
    Write,
    SetLen,
    SyncData,
    SyncAll,
    SyncDirectory,
    Rename,
    RemoveFile,
    Metadata,
    ReadDirectory,
    LockExclusive,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FaultAction {
    ErrorBefore(io::ErrorKind),
    ShortWrite(usize),
    DropSync,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FaultRule {
    pub operation_number: u64,
    pub action: FaultAction,
}

pub trait VfsFile: Send {
    fn len(&self) -> io::Result<u64>;
    fn is_empty(&self) -> io::Result<bool> {
        Ok(self.len()? == 0)
    }
    fn read_all(&mut self, maximum_bytes: usize) -> io::Result<Vec<u8>>;
    /// Reuse a length already observed on this handle as an allocation hint.
    /// The read must still enforce maximum_bytes against actual returned data.
    /// Wrappers keep their existing read/fault boundary through this default.
    fn read_all_with_observed_length(
        &mut self,
        maximum_bytes: usize,
        _observed_length: u64,
    ) -> io::Result<Vec<u8>> {
        self.read_all(maximum_bytes)
    }
    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()>;
    fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()>;
    /// Wrappers retain one existing write/fault boundary. Real files override
    /// this to submit borrowed frame segments without a temporary byte copy.
    fn write_all_vectored_at(&mut self, offset: u64, buffers: &mut [IoSlice<'_>]) -> io::Result<()> {
        let length = vectored_length(buffers)?;
        let mut bytes = Vec::with_capacity(length);
        for buffer in buffers { bytes.extend_from_slice(buffer); }
        self.write_all_at(offset, &bytes)
    }
    fn set_len(&mut self, length: u64) -> io::Result<()>;
    fn sync_data(&mut self) -> io::Result<()>;
    fn sync_all(&mut self) -> io::Result<()>;
    fn try_lock_exclusive(&mut self) -> io::Result<()>;
}

/// A read-only handle whose offsets are independent across callers. Wrappers
/// with mutable decrypt/read state may keep the ordinary VfsFile fallback.
pub trait VfsPositionedReader: Send + Sync {
    fn len(&self) -> io::Result<u64>;
    fn read_exact_at(&self, offset: u64, bytes: &mut [u8]) -> io::Result<()>;
}

/// Persists file data across one sync call that reports success without
/// reaching stable storage. Explicit sync errors still fail immediately.
pub(crate) fn sync_data_barrier(file: &mut dyn VfsFile) -> io::Result<()> {
    file.sync_data()?;
    file.sync_data()
}

/// Persists file data and metadata across one silently lost sync call.
pub(crate) fn sync_all_barrier(file: &mut dyn VfsFile) -> io::Result<()> {
    file.sync_all()?;
    file.sync_all()
}

/// Publishes namespace changes across one silently lost directory sync call.
pub(crate) fn sync_directory_barrier(vfs: &dyn Vfs, path: &Path) -> io::Result<()> {
    vfs.sync_directory(path)?;
    vfs.sync_directory(path)
}

/// Independently observed filesystem dimensions; unknown accounting is explicit.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct FilesystemCapacity {
    pub total_bytes: Option<u64>,
    pub available_bytes: Option<u64>,
    pub total_inodes: Option<u64>,
    pub available_inodes: Option<u64>,
}

pub trait Vfs: Send + Sync {
    /// Independent ordinary file handles share current lengths and may append
    /// disjoint WAL regions. Stateful/encrypting wrappers default to fallback.
    fn supports_buffered_wal_writes(&self) -> bool { false }

    fn filesystem_capacity(&self, _path: &Path) -> io::Result<FilesystemCapacity> {
        Ok(FilesystemCapacity::default())
    }
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>>;
    fn open_positioned_reader(&self, _path: &Path) -> io::Result<Option<Arc<dyn VfsPositionedReader>>> {
        Ok(None)
    }
    fn create_dir(&self, path: &Path) -> io::Result<()>;
    fn rename(&self, source: &Path, destination: &Path) -> io::Result<()>;
    fn remove_file(&self, path: &Path) -> io::Result<()>;
    fn metadata(&self, path: &Path) -> io::Result<FileKind>;
    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>>;
    fn read_directory_bounded(
        &self,
        path: &Path,
        maximum_entries: usize,
    ) -> io::Result<BoundedDirectoryEntries>;
    fn match_directory_prefix_bounded(
        &self,
        path: &Path,
        name_prefix: &str,
        maximum_entries_scanned: usize,
        maximum_matches: usize,
    ) -> io::Result<BoundedDirectoryMatches>;
    fn sync_directory(&self, path: &Path) -> io::Result<()>;
}

pub struct RealVfs;

struct RealFile {
    file: File,
    owns_exclusive_lock: bool,
}

impl Drop for RealFile {
    fn drop(&mut self) {
        if self.owns_exclusive_lock {
            // dup/fork aliases may outlive this lease until child exec. An
            // explicit unlock binds authority ownership to this guard, matching
            // SimulatedFile, rather than to the last inherited descriptor.
            let _ = FileExt::unlock(&self.file);
        }
    }
}

fn vectored_length(buffers: &[IoSlice<'_>]) -> io::Result<usize> {
    buffers.iter().try_fold(0_usize, |length, buffer| length.checked_add(buffer.len())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "vectored write length overflow")))
}

fn write_all_vectored(writer: &mut impl Write, mut buffers: &mut [IoSlice<'_>]) -> io::Result<()> {
    while !buffers.is_empty() {
        if buffers[0].is_empty() { buffers = &mut buffers[1..]; continue; }
        match writer.write_vectored(buffers) {
            Ok(0) => return Err(io::Error::new(io::ErrorKind::WriteZero, "vectored write made no progress")),
            Ok(written) => IoSlice::advance_slices(&mut buffers, written),
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

impl VfsFile for RealFile {
    fn len(&self) -> io::Result<u64> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::VFS_METADATA);
        Ok(self.file.metadata()?.len())
    }

    fn read_all(&mut self, maximum_bytes: usize) -> io::Result<Vec<u8>> {
        let length = VfsFile::len(self)?;
        self.read_all_with_observed_length(maximum_bytes, length)
    }

    fn read_all_with_observed_length(
        &mut self,
        maximum_bytes: usize,
        length: u64,
    ) -> io::Result<Vec<u8>> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::VFS_READ);
        crate::request_profile::work(crate::request_profile::Work::ReadBytes, length);
        if length > maximum_bytes as u64 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "file exceeds its read admission bound",
            ));
        }
        self.file.seek(SeekFrom::Start(0))?;
        let mut bytes = Vec::with_capacity(length as usize);
        Read::by_ref(&mut self.file).take(maximum_bytes.saturating_add(1) as u64)
            .read_to_end(&mut bytes)?;
        if bytes.len() > maximum_bytes {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "file exceeds its read admission bound",
            ));
        }
        Ok(bytes)
    }

    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::VFS_READ);
        self.file.seek(SeekFrom::Start(offset))?;
        crate::request_profile::work(crate::request_profile::Work::ReadBytes, bytes.len() as u64);
        self.file.read_exact(bytes)
    }

    fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::VFS_WRITE);
        self.file.seek(SeekFrom::Start(offset))?;
        crate::request_profile::work(crate::request_profile::Work::WriteBytes, bytes.len() as u64);
        self.file.write_all(bytes)
    }

    fn write_all_vectored_at(&mut self, offset: u64, buffers: &mut [IoSlice<'_>]) -> io::Result<()> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::VFS_WRITE);
        let length = vectored_length(buffers)?;
        self.file.seek(SeekFrom::Start(offset))?;
        crate::request_profile::work(crate::request_profile::Work::WriteBytes, length as u64);
        write_all_vectored(&mut self.file, buffers)
    }

    fn set_len(&mut self, length: u64) -> io::Result<()> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::VFS_MUTATE);
        self.file.set_len(length)
    }

    fn sync_data(&mut self) -> io::Result<()> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::VFS_SYNC);
        self.file.sync_data()
    }

    fn sync_all(&mut self) -> io::Result<()> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::VFS_SYNC);
        self.file.sync_all()
    }

    fn try_lock_exclusive(&mut self) -> io::Result<()> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::VFS_MUTATE);
        self.file.try_lock_exclusive()?;
        self.owns_exclusive_lock = true;
        Ok(())
    }
}

impl Vfs for RealVfs {
    fn supports_buffered_wal_writes(&self) -> bool { true }
    fn filesystem_capacity(&self, path: &Path) -> io::Result<FilesystemCapacity> {
        let mut probe = path;
        loop {
            match fs::metadata(probe) {
                Ok(_) => break,
                Err(error) if error.kind() == io::ErrorKind::NotFound => {
                    probe = probe.parent().ok_or(error)?;
                }
                Err(error) => return Err(error),
            }
        }
        #[cfg(unix)]
        {
            use std::os::unix::ffi::OsStrExt;
            let path = std::ffi::CString::new(probe.as_os_str().as_bytes())
                .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "filesystem path contains NUL"))?;
            let mut info = std::mem::MaybeUninit::<libc::statvfs>::uninit();
            // SAFETY: path is NUL terminated and info points to writable statvfs storage.
            if unsafe { libc::statvfs(path.as_ptr(), info.as_mut_ptr()) } != 0 {
                return Err(io::Error::last_os_error());
            }
            // SAFETY: a successful statvfs initialized the complete structure.
            let info = unsafe { info.assume_init() };
            let bytes_known = info.f_blocks > 0 && info.f_frsize > 0;
            let inodes_known = info.f_files > 0;
            Ok(FilesystemCapacity {
                total_bytes: bytes_known.then(|| (info.f_blocks as u64).saturating_mul(info.f_frsize as u64)),
                available_bytes: bytes_known.then(|| (info.f_bavail as u64).saturating_mul(info.f_frsize as u64)),
                total_inodes: inodes_known.then_some(info.f_files as u64),
                available_inodes: inodes_known.then_some(info.f_favail as u64),
            })
        }
        #[cfg(not(unix))]
        {
            Ok(FilesystemCapacity { total_bytes: fs2::total_space(probe).ok(), available_bytes: fs2::available_space(probe).ok(), ..FilesystemCapacity::default() })
        }
    }

    fn open_positioned_reader(&self, path: &Path) -> io::Result<Option<Arc<dyn VfsPositionedReader>>> {
        #[cfg(unix)]
        {
            let _open = crate::request_profile::Probe::new(crate::request_profile::VFS_OPEN);
            let file = OpenOptions::new().read(true).open(path)?;
            Ok(Some(Arc::new(RealFile { file, owns_exclusive_lock: false })))
        }
        #[cfg(not(unix))]
        { let _ = path; Ok(None) }
    }
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::VFS_OPEN);
        let file = OpenOptions::new()
            .read(request.read)
            .write(request.write)
            .create(request.create)
            .create_new(request.create_new)
            .truncate(request.truncate)
            .open(path)?;
        Ok(Box::new(RealFile {
            file,
            owns_exclusive_lock: false,
        }))
    }

    fn create_dir(&self, path: &Path) -> io::Result<()> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::VFS_MUTATE);
        fs::create_dir(path)
    }

    fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::VFS_MUTATE);
        fs::rename(source, destination)
    }

    fn remove_file(&self, path: &Path) -> io::Result<()> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::VFS_MUTATE);
        fs::remove_file(path)
    }

    fn metadata(&self, path: &Path) -> io::Result<FileKind> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::VFS_METADATA);
        let metadata = fs::symlink_metadata(path)?;
        if metadata.file_type().is_symlink() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "VFS path may not be a symbolic link",
            ));
        }
        if metadata.file_type().is_file() {
            Ok(FileKind::File)
        } else if metadata.file_type().is_dir() {
            Ok(FileKind::Directory)
        } else {
            Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "VFS path has an unsupported file kind",
            ))
        }
    }

    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::VFS_DIRECTORY);
        let mut entries = Vec::new();
        for entry in fs::read_dir(path)? {
            entries.push(entry?.path());
        }
        entries.sort();
        Ok(entries)
    }

    fn read_directory_bounded(
        &self,
        path: &Path,
        maximum_entries: usize,
    ) -> io::Result<BoundedDirectoryEntries> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::VFS_DIRECTORY);
        if maximum_entries == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "directory entry bound must be nonzero",
            ));
        }
        let mut entries = Vec::with_capacity(maximum_entries);
        let mut has_more = false;
        for entry in fs::read_dir(path)? {
            if entries.len() == maximum_entries {
                has_more = true;
                break;
            }
            entries.push(entry?.path());
        }
        entries.sort();
        Ok(BoundedDirectoryEntries { entries, has_more })
    }

    fn match_directory_prefix_bounded(
        &self,
        path: &Path,
        name_prefix: &str,
        maximum_entries_scanned: usize,
        maximum_matches: usize,
    ) -> io::Result<BoundedDirectoryMatches> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::VFS_DIRECTORY);
        if name_prefix.is_empty() || maximum_entries_scanned == 0 || maximum_matches == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "directory match bounds and prefix must be nonzero",
            ));
        }
        let mut result = BoundedDirectoryMatches::default();
        for entry in fs::read_dir(path)? {
            if result.scanned_entries == maximum_entries_scanned {
                result.has_more_entries = true;
                break;
            }
            let candidate = entry?.path();
            result.scanned_entries += 1;
            if candidate
                .file_name()
                .and_then(|value| value.to_str())
                .is_some_and(|name| name.starts_with(name_prefix))
            {
                if result.matches.len() < maximum_matches {
                    result.matches.push(candidate);
                    if result.matches.len() == maximum_matches {
                        result.has_more_entries = true;
                        break;
                    }
                } else {
                    result.has_more_matches = true;
                }
            }
        }
        result.matches.sort();
        Ok(result)
    }

    fn sync_directory(&self, path: &Path) -> io::Result<()> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::VFS_SYNC);
        #[cfg(unix)]
        {
            File::open(path)?.sync_all()
        }
        #[cfg(not(unix))]
        {
            let _ = path;
            Ok(())
        }
    }
}

#[derive(Clone)]
pub struct DeterministicVfs {
    shared: Arc<Mutex<SimulatedState>>,
}

struct SimulatedState {
    working_files: BTreeMap<PathBuf, Vec<u8>>,
    synced_files: BTreeMap<PathBuf, Vec<u8>>,
    durable_files: BTreeMap<PathBuf, Vec<u8>>,
    working_directories: BTreeSet<PathBuf>,
    durable_directories: BTreeSet<PathBuf>,
    locked_files: BTreeSet<PathBuf>,
    operation_count: u64,
    faults: Vec<FaultRule>,
    trace: Vec<Operation>,
    #[cfg(test)]
    operation_fault: Option<(Operation, usize, FaultAction, bool)>,
    #[cfg(test)]
    capacity: FilesystemCapacity,
}

struct SimulatedFile {
    path: PathBuf,
    readable: bool,
    writable: bool,
    owns_exclusive_lock: bool,
    shared: Arc<Mutex<SimulatedState>>,
}

fn poisoned() -> io::Error {
    io::Error::other("deterministic VFS state lock was poisoned")
}

fn parent(path: &Path) -> io::Result<&Path> {
    path.parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "VFS path has no parent"))
}

fn state(shared: &Arc<Mutex<SimulatedState>>) -> io::Result<MutexGuard<'_, SimulatedState>> {
    shared.lock().map_err(|_| poisoned())
}

fn before(state: &mut SimulatedState, operation: Operation) -> io::Result<Option<FaultAction>> {
    state.operation_count = state
        .operation_count
        .checked_add(1)
        .ok_or_else(|| io::Error::other("VFS operation counter overflow"))?;
    state.trace.push(operation);
    let action = state
        .faults
        .iter()
        .find(|fault| fault.operation_number == state.operation_count)
        .map(|fault| fault.action);
    #[cfg(test)]
    let action = if let Some((target, remaining, injected, fired)) = &mut state.operation_fault {
        if *target == operation && !*fired {
            *remaining -= 1;
            if *remaining == 0 {
                *fired = true;
                Some(*injected)
            } else {
                action
            }
        } else {
            action
        }
    } else {
        action
    };
    if let Some(FaultAction::ErrorBefore(kind)) = action {
        return Err(io::Error::new(kind, "deterministic VFS injected error"));
    }
    if matches!(action, Some(FaultAction::ShortWrite(_))) && operation != Operation::Write {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "ShortWrite fault did not target a write",
        ));
    }
    if matches!(action, Some(FaultAction::DropSync))
        && !matches!(
            operation,
            Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
        )
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "DropSync fault did not target a sync",
        ));
    }
    Ok(action)
}

impl DeterministicVfs {
    #[cfg(test)]
    pub(crate) fn set_filesystem_capacity(&self, capacity: FilesystemCapacity) -> io::Result<()> {
        state(&self.shared)?.capacity = capacity;
        Ok(())
    }

    pub fn new(fault: Option<FaultRule>) -> Self {
        let root = PathBuf::from("/");
        Self {
            shared: Arc::new(Mutex::new(SimulatedState {
                working_files: BTreeMap::new(),
                synced_files: BTreeMap::new(),
                durable_files: BTreeMap::new(),
                working_directories: BTreeSet::from([root.clone()]),
                durable_directories: BTreeSet::from([root]),
                locked_files: BTreeSet::new(),
                operation_count: 0,
                faults: fault.into_iter().collect(),
                trace: Vec::new(),
                #[cfg(test)]
                operation_fault: None,
                #[cfg(test)]
                capacity: FilesystemCapacity::default(),
            })),
        }
    }

    /// Independent crash image for repeatable fault campaigns. Reusing one
    /// durable seed keeps authority UUIDs and content-derived directory layouts
    /// identical; no volatile bytes, live locks, counters or faults are copied.
    #[cfg(test)]
    pub(crate) fn fork_durable(&self) -> io::Result<Self> {
        let source = state(&self.shared)?;
        Ok(Self {
            shared: Arc::new(Mutex::new(SimulatedState {
                working_files: source.durable_files.clone(),
                synced_files: source.durable_files.clone(),
                durable_files: source.durable_files.clone(),
                working_directories: source.durable_directories.clone(),
                durable_directories: source.durable_directories.clone(),
                locked_files: BTreeSet::new(),
                operation_count: 0,
                faults: Vec::new(),
                trace: Vec::new(),
                #[cfg(test)]
                operation_fault: None,
                #[cfg(test)]
                capacity: FilesystemCapacity::default(),
            })),
        })
    }

    pub fn crash(&self) -> io::Result<()> {
        let mut state = state(&self.shared)?;
        state.working_files = state.durable_files.clone();
        state.synced_files = state.durable_files.clone();
        state.working_directories = state.durable_directories.clone();
        state.locked_files.clear();
        Ok(())
    }

    pub fn arm_fault(&self, fault: Option<FaultRule>) -> io::Result<()> {
        self.arm_faults(fault.as_slice())
    }

    /// Arms several faults at once for correlated-loss certification: each
    /// rule fires when the operation counter reaches its number, so two
    /// dropped syncs in one crash window model a power failure that loses
    /// more than one barrier call. Rules are stored sorted by operation
    /// number; duplicate numbers collapse to the first rule.
    pub fn arm_faults(&self, faults: &[FaultRule]) -> io::Result<()> {
        let mut state = state(&self.shared)?;
        state.operation_count = 0;
        state.trace.clear();
        #[cfg(test)]
        {
            state.operation_fault = None;
        }
        let mut rules = faults.to_vec();
        rules.sort_by_key(|rule| rule.operation_number);
        rules.dedup_by_key(|rule| rule.operation_number);
        state.faults = rules;
        Ok(())
    }

    /// Targets the nth operation of one kind when randomized segment paths
    /// change interleaving. Callers must assert firing before claiming coverage.
    #[cfg(test)]
    pub(crate) fn arm_operation_fault(
        &self,
        operation: Operation,
        occurrence: usize,
        action: FaultAction,
    ) -> io::Result<()> {
        assert!(occurrence > 0);
        self.arm_fault(None)?;
        state(&self.shared)?.operation_fault = Some((operation, occurrence, action, false));
        Ok(())
    }

    #[cfg(test)]
    pub(crate) fn operation_fault_fired(&self) -> io::Result<bool> {
        Ok(state(&self.shared)?
            .operation_fault
            .is_some_and(|(_, _, _, fired)| fired))
    }

    pub fn trace(&self) -> io::Result<Vec<Operation>> {
        Ok(state(&self.shared)?.trace.clone())
    }

    pub fn durable_file(&self, path: &Path) -> io::Result<Option<Vec<u8>>> {
        Ok(state(&self.shared)?.durable_files.get(path).cloned())
    }
}

impl Vfs for DeterministicVfs {
    fn supports_buffered_wal_writes(&self) -> bool { true }
    fn filesystem_capacity(&self, _path: &Path) -> io::Result<FilesystemCapacity> {
        #[cfg(test)]
        { Ok(state(&self.shared)?.capacity) }
        #[cfg(not(test))]
        { Ok(FilesystemCapacity::default()) }
    }

    fn open_positioned_reader(&self, path: &Path) -> io::Result<Option<Arc<dyn VfsPositionedReader>>> {
        // Use ordinary admission and the same Open fault before sharing a
        // cursor-free simulated handle; every read still records its fault.
        self.open(path, OpenRequest { read: true, ..OpenRequest::default() })?;
        Ok(Some(Arc::new(SimulatedFile {
            shared: Arc::clone(&self.shared), path: path.to_owned(),
            readable: true, writable: false, owns_exclusive_lock: false,
        })))
    }
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
        let mut state = state(&self.shared)?;
        before(&mut state, Operation::Open)?;
        if !state.working_directories.contains(parent(path)?) {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "VFS parent is missing",
            ));
        }
        let exists = state.working_files.contains_key(path);
        if request.create_new && exists {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "VFS file exists",
            ));
        }
        if !(exists || request.create || request.create_new) {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                format!("VFS file is missing: {}", path.display()),
            ));
        }
        if !exists || request.truncate {
            state.working_files.insert(path.to_path_buf(), Vec::new());
        }
        Ok(Box::new(SimulatedFile {
            path: path.to_path_buf(),
            readable: request.read,
            writable: request.write,
            owns_exclusive_lock: false,
            shared: Arc::clone(&self.shared),
        }))
    }

    fn create_dir(&self, path: &Path) -> io::Result<()> {
        let mut state = state(&self.shared)?;
        before(&mut state, Operation::CreateDir)?;
        if !state.working_directories.contains(parent(path)?) {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "VFS parent is missing",
            ));
        }
        if !state.working_directories.insert(path.to_path_buf()) {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "VFS directory exists",
            ));
        }
        Ok(())
    }

    fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> {
        let mut state = state(&self.shared)?;
        before(&mut state, Operation::Rename)?;
        if state.working_files.contains_key(destination) {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "VFS rename destination exists",
            ));
        }
        let bytes = state
            .working_files
            .remove(source)
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "VFS source is missing"))?;
        state.working_files.insert(destination.to_path_buf(), bytes);
        if let Some(synced) = state.synced_files.remove(source) {
            state.synced_files.insert(destination.to_path_buf(), synced);
        }
        Ok(())
    }

    fn remove_file(&self, path: &Path) -> io::Result<()> {
        let mut state = state(&self.shared)?;
        before(&mut state, Operation::RemoveFile)?;
        state
            .working_files
            .remove(path)
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "VFS file is missing"))?;
        state.synced_files.remove(path);
        Ok(())
    }

    fn metadata(&self, path: &Path) -> io::Result<FileKind> {
        let mut state = state(&self.shared)?;
        before(&mut state, Operation::Metadata)?;
        if state.working_files.contains_key(path) {
            Ok(FileKind::File)
        } else if state.working_directories.contains(path) {
            Ok(FileKind::Directory)
        } else {
            Err(io::Error::new(
                io::ErrorKind::NotFound,
                "VFS path is missing",
            ))
        }
    }

    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> {
        let mut state = state(&self.shared)?;
        before(&mut state, Operation::ReadDirectory)?;
        if !state.working_directories.contains(path) {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "VFS directory is missing",
            ));
        }
        let mut entries: Vec<_> = state
            .working_files
            .keys()
            .chain(state.working_directories.iter())
            .filter(|candidate| candidate.parent() == Some(path))
            .cloned()
            .collect();
        entries.sort();
        Ok(entries)
    }

    fn read_directory_bounded(
        &self,
        path: &Path,
        maximum_entries: usize,
    ) -> io::Result<BoundedDirectoryEntries> {
        if maximum_entries == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "directory entry bound must be nonzero",
            ));
        }
        let mut state = state(&self.shared)?;
        before(&mut state, Operation::ReadDirectory)?;
        if !state.working_directories.contains(path) {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "VFS directory is missing",
            ));
        }
        let mut entries = state
            .working_files
            .keys()
            .chain(state.working_directories.iter())
            .filter(|candidate| candidate.parent() == Some(path))
            .take(maximum_entries + 1)
            .cloned()
            .collect::<Vec<_>>();
        entries.sort();
        let has_more = entries.len() > maximum_entries;
        entries.truncate(maximum_entries);
        Ok(BoundedDirectoryEntries { entries, has_more })
    }

    fn match_directory_prefix_bounded(
        &self,
        path: &Path,
        name_prefix: &str,
        maximum_entries_scanned: usize,
        maximum_matches: usize,
    ) -> io::Result<BoundedDirectoryMatches> {
        if name_prefix.is_empty() || maximum_entries_scanned == 0 || maximum_matches == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "directory match bounds and prefix must be nonzero",
            ));
        }
        let mut state = state(&self.shared)?;
        before(&mut state, Operation::ReadDirectory)?;
        if !state.working_directories.contains(path) {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "VFS directory is missing",
            ));
        }
        let mut result = BoundedDirectoryMatches::default();
        for candidate in state
            .working_files
            .keys()
            .chain(state.working_directories.iter())
            .filter(|candidate| candidate.parent() == Some(path))
        {
            if result.scanned_entries == maximum_entries_scanned {
                result.has_more_entries = true;
                break;
            }
            result.scanned_entries += 1;
            if candidate
                .file_name()
                .and_then(|value| value.to_str())
                .is_some_and(|name| name.starts_with(name_prefix))
            {
                if result.matches.len() < maximum_matches {
                    result.matches.push(candidate.clone());
                    if result.matches.len() == maximum_matches {
                        result.has_more_entries = true;
                        break;
                    }
                } else {
                    result.has_more_matches = true;
                }
            }
        }
        result.matches.sort();
        Ok(result)
    }

    fn sync_directory(&self, path: &Path) -> io::Result<()> {
        let mut state = state(&self.shared)?;
        if matches!(
            before(&mut state, Operation::SyncDirectory)?,
            Some(FaultAction::DropSync)
        ) {
            return Ok(());
        }
        if !state.working_directories.contains(path) {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "VFS directory is missing",
            ));
        }
        if !state.durable_directories.contains(path) {
            // fsync on a newly created directory does not publish that
            // directory's name in its parent. The parent must be synced first.
            return Ok(());
        }
        let working_file_names: BTreeSet<_> = state
            .working_files
            .keys()
            .filter(|candidate| candidate.parent() == Some(path))
            .cloned()
            .collect();
        let durable_file_names: Vec<_> = state
            .durable_files
            .keys()
            .filter(|candidate| candidate.parent() == Some(path))
            .cloned()
            .collect();
        for stale in durable_file_names {
            if !working_file_names.contains(&stale) {
                state.durable_files.remove(&stale);
            }
        }
        for name in working_file_names {
            if let Some(bytes) = state.synced_files.get(&name).cloned() {
                state.durable_files.insert(name, bytes);
            }
        }
        let child_directories: Vec<_> = state
            .working_directories
            .iter()
            .filter(|candidate| candidate.parent() == Some(path))
            .cloned()
            .collect();
        state.durable_directories.extend(child_directories);
        Ok(())
    }
}

impl VfsFile for SimulatedFile {
    fn len(&self) -> io::Result<u64> {
        let mut state = state(&self.shared)?;
        before(&mut state, Operation::Metadata)?;
        Ok(state
            .working_files
            .get(&self.path)
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "VFS file is missing"))?
            .len() as u64)
    }

    fn read_all(&mut self, maximum_bytes: usize) -> io::Result<Vec<u8>> {
        if !self.readable {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "VFS file is not readable",
            ));
        }
        let mut state = state(&self.shared)?;
        before(&mut state, Operation::Read)?;
        let bytes = state
            .working_files
            .get(&self.path)
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "VFS file is missing"))?;
        if bytes.len() > maximum_bytes {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "file exceeds its read admission bound",
            ));
        }
        Ok(bytes.clone())
    }

    fn read_exact_at(&mut self, offset: u64, output: &mut [u8]) -> io::Result<()> {
        self.read_exact_at_shared(offset, output)
    }

    fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> {
        if !self.writable {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "VFS file is not writable",
            ));
        }
        let mut state = state(&self.shared)?;
        let action = before(&mut state, Operation::Write)?;
        let write_len = match action {
            Some(FaultAction::ShortWrite(maximum)) => maximum.min(bytes.len()),
            Some(FaultAction::DropSync) => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "DropSync fault targeted a write",
                ));
            }
            _ => bytes.len(),
        };
        let offset = usize::try_from(offset)
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "VFS offset overflow"))?;
        let file = state
            .working_files
            .get_mut(&self.path)
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "VFS file is missing"))?;
        let end = offset
            .checked_add(write_len)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "VFS write overflow"))?;
        if file.len() < end {
            file.resize(end, 0);
        }
        file[offset..end].copy_from_slice(&bytes[..write_len]);
        if write_len != bytes.len() {
            return Err(io::Error::new(
                io::ErrorKind::WriteZero,
                "deterministic VFS injected short write",
            ));
        }
        Ok(())
    }

    fn set_len(&mut self, length: u64) -> io::Result<()> {
        if !self.writable {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "VFS file is not writable",
            ));
        }
        let mut state = state(&self.shared)?;
        before(&mut state, Operation::SetLen)?;
        let length = usize::try_from(length)
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "VFS length overflow"))?;
        state
            .working_files
            .get_mut(&self.path)
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "VFS file is missing"))?
            .resize(length, 0);
        Ok(())
    }

    fn sync_data(&mut self) -> io::Result<()> {
        self.sync(Operation::SyncData)
    }

    fn sync_all(&mut self) -> io::Result<()> {
        self.sync(Operation::SyncAll)
    }

    fn try_lock_exclusive(&mut self) -> io::Result<()> {
        let mut state = state(&self.shared)?;
        before(&mut state, Operation::LockExclusive)?;
        if !state.locked_files.insert(self.path.clone()) {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "deterministic VFS file is already locked",
            ));
        }
        self.owns_exclusive_lock = true;
        Ok(())
    }
}

impl Drop for SimulatedFile {
    fn drop(&mut self) {
        if self.owns_exclusive_lock {
            if let Ok(mut state) = self.shared.lock() {
                state.locked_files.remove(&self.path);
            }
        }
    }
}

impl SimulatedFile {
    fn sync(&mut self, operation: Operation) -> io::Result<()> {
        let mut state = state(&self.shared)?;
        if matches!(before(&mut state, operation)?, Some(FaultAction::DropSync)) {
            return Ok(());
        }
        let bytes = state
            .working_files
            .get(&self.path)
            .cloned()
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "VFS file is missing"))?;
        state.synced_files.insert(self.path.clone(), bytes.clone());
        if state.durable_files.contains_key(&self.path) {
            state.durable_files.insert(self.path.clone(), bytes);
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn observed_length_read_retains_actual_bounds_when_file_grows_or_shrinks() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("bounded-read");
        fs::write(&path, b"seed").unwrap();
        let mut file = RealVfs.open(&path, OpenRequest { read: true, ..OpenRequest::default() }).unwrap();
        let observed = file.len().unwrap();
        fs::write(&path, b"seed-grown").unwrap();
        assert_eq!(file.read_all_with_observed_length(16, observed).unwrap(), b"seed-grown");
        fs::write(&path, b"x").unwrap();
        assert_eq!(file.read_all_with_observed_length(16, observed).unwrap(), b"x");
        fs::write(&path, vec![b'x'; 256 * 1024]).unwrap();
        assert_eq!(file.read_all_with_observed_length(16, observed).unwrap_err().kind(), io::ErrorKind::InvalidData);
        assert_eq!(file.read_all(16).unwrap_err().kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn observed_length_default_preserves_simulated_read_faults() {
        let vfs = DeterministicVfs::new(Some(FaultRule {
            operation_number: 4,
            action: FaultAction::ErrorBefore(io::ErrorKind::Other),
        }));
        vfs.create_dir(Path::new("/data")).unwrap();
        let mut file = vfs.open(Path::new("/data/value"), writable(true)).unwrap();
        file.write_all_at(0, b"seed").unwrap();
        assert_eq!(file.read_all_with_observed_length(16, 4).unwrap_err().kind(), io::ErrorKind::Other);
        assert_eq!(vfs.trace().unwrap().last(), Some(&Operation::Read));
    }

    #[test]
    fn vectored_write_preserves_segment_boundaries_across_short_writes_and_interrupts() {
        struct ShortWriter { bytes: Vec<u8>, calls: usize, fail: Option<io::ErrorKind> }
        impl Write for ShortWriter {
            fn write(&mut self, _: &[u8]) -> io::Result<usize> { panic!("must submit vectored writes") }
            fn flush(&mut self) -> io::Result<()> { Ok(()) }
            fn write_vectored(&mut self, buffers: &[IoSlice<'_>]) -> io::Result<usize> {
                self.calls += 1;
                if self.calls == 1 { return Err(io::ErrorKind::Interrupted.into()); }
                if let Some(kind) = self.fail { return if kind == io::ErrorKind::WriteZero { Ok(0) } else { Err(kind.into()) }; }
                let mut written = 0;
                for buffer in buffers {
                    let take = buffer.len().min(3 - written);
                    self.bytes.extend_from_slice(&buffer[..take]); written += take;
                    if written == 3 { break; }
                }
                Ok(written)
            }
        }
        let mut writer = ShortWriter { bytes: Vec::new(), calls: 0, fail: None };
        let mut buffers = [IoSlice::new(b""), IoSlice::new(b"head"), IoSlice::new(b""), IoSlice::new(b"payload"), IoSlice::new(b"tail"), IoSlice::new(b"")];
        write_all_vectored(&mut writer, &mut buffers).unwrap();
        assert_eq!(writer.bytes, b"headpayloadtail");
        assert_eq!(writer.calls, 6);
        for kind in [io::ErrorKind::WriteZero, io::ErrorKind::PermissionDenied] {
            let mut writer = ShortWriter { bytes: Vec::new(), calls: 0, fail: Some(kind) };
            assert_eq!(write_all_vectored(&mut writer, &mut [IoSlice::new(b"x")]).unwrap_err().kind(), kind);
            assert!(writer.bytes.is_empty());
        }
    }

    use super::*;

    #[cfg(unix)]
    #[test]
    fn dropping_the_lease_unlocks_even_while_an_inherited_descriptor_remains_open() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("LOCK");
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        let inherited = file.try_clone().unwrap();
        let mut lease = RealFile {
            file,
            owns_exclusive_lock: false,
        };
        lease.try_lock_exclusive().unwrap();
        let request = OpenRequest {
            read: true,
            write: true,
            ..OpenRequest::default()
        };
        let mut next = RealVfs.open(&path, request).unwrap();
        assert_eq!(
            next.try_lock_exclusive().unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        drop(lease);
        // dup/fork share the open-file description. The lease owner, not the
        // last inherited descriptor, determines the authority lifetime.
        next.try_lock_exclusive().unwrap();
        drop(inherited);
        let mut third = RealVfs.open(&path, request).unwrap();
        assert_eq!(
            third.try_lock_exclusive().unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        drop(next);
        third.try_lock_exclusive().unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn a_live_child_descriptor_does_not_extend_the_parent_lease() {
        use std::io::{BufRead, BufReader};
        use std::process::{Command, Stdio};
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("LOCK");
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        let inherited = file.try_clone().unwrap();
        let mut lease = RealFile {
            file,
            owns_exclusive_lock: false,
        };
        lease.try_lock_exclusive().unwrap();
        // Stdio transfers a duplicate to the actual child without unsafe fork
        // callbacks in this multi-threaded test process. It performs no writes
        // to that descriptor and holds it until the parent closes stdin.
        let mut child = Command::new("/bin/sh")
            .args(["-c", "printf 'ready\\n'; read token; exit 0"])
            .env("LC_ALL", "C")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::from(inherited))
            .spawn()
            .unwrap();
        let mut readiness = String::new();
        let read = BufReader::new(child.stdout.as_mut().unwrap()).read_line(&mut readiness);
        let alive = child.try_wait();
        drop(lease);
        let request = OpenRequest {
            read: true,
            write: true,
            ..OpenRequest::default()
        };
        let acquired = RealVfs.open(&path, request).and_then(|mut file| {
            file.try_lock_exclusive()?;
            Ok(file)
        });
        drop(child.stdin.take());
        let exit = child.wait().unwrap();
        assert!(read.is_ok() && readiness == "ready\n");
        assert!(alive.unwrap().is_none());
        assert!(exit.success());
        assert!(
            acquired.is_ok(),
            "child retained lease: {:?}",
            acquired.err()
        );
    }

    fn writable(create_new: bool) -> OpenRequest {
        OpenRequest {
            read: true,
            write: true,
            create_new,
            ..OpenRequest::default()
        }
    }

    #[test]
    fn new_file_requires_file_and_directory_sync() {
        let vfs = DeterministicVfs::new(None);
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut file = vfs.open(Path::new("/data/block"), writable(true)).unwrap();
        file.write_all_at(0, b"durable").unwrap();
        file.sync_all().unwrap();
        vfs.crash().unwrap();
        assert_eq!(
            vfs.metadata(Path::new("/data/block")).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );

        let mut file = vfs.open(Path::new("/data/block"), writable(true)).unwrap();
        file.write_all_at(0, b"durable").unwrap();
        file.sync_all().unwrap();
        vfs.sync_directory(Path::new("/data")).unwrap();
        vfs.crash().unwrap();
        assert_eq!(
            vfs.durable_file(Path::new("/data/block")).unwrap().unwrap(),
            b"durable"
        );
    }

    #[test]
    fn dropped_sync_and_short_write_are_reproducible() {
        for action in [FaultAction::DropSync, FaultAction::ShortWrite(2)] {
            let run = || {
                let operation_number = match action {
                    FaultAction::DropSync => 5,
                    FaultAction::ShortWrite(_) => 4,
                    FaultAction::ErrorBefore(_) => unreachable!(),
                };
                let vfs = DeterministicVfs::new(Some(FaultRule {
                    operation_number,
                    action,
                }));
                vfs.create_dir(Path::new("/data")).unwrap();
                vfs.sync_directory(Path::new("/")).unwrap();
                let mut file = vfs.open(Path::new("/data/value"), writable(true)).unwrap();
                let _ = file.write_all_at(0, b"abcdef");
                let _ = file.sync_all();
                let _ = vfs.sync_directory(Path::new("/data"));
                let trace = vfs.trace().unwrap();
                vfs.crash().unwrap();
                (trace, vfs.durable_file(Path::new("/data/value")).unwrap())
            };
            let first = run();
            assert_eq!(first, run());
            match action {
                FaultAction::DropSync => assert_eq!(first.1, None),
                FaultAction::ShortWrite(_) => assert_eq!(first.1.unwrap(), b"ab"),
                FaultAction::ErrorBefore(_) => unreachable!(),
            }
        }
    }

    #[test]
    fn rename_requires_directory_sync() {
        let vfs = DeterministicVfs::new(None);
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut file = vfs
            .open(Path::new("/data/temporary"), writable(true))
            .unwrap();
        file.write_all_at(0, b"segment").unwrap();
        file.sync_all().unwrap();
        vfs.sync_directory(Path::new("/data")).unwrap();
        vfs.rename(Path::new("/data/temporary"), Path::new("/data/final"))
            .unwrap();
        vfs.crash().unwrap();
        assert!(vfs
            .durable_file(Path::new("/data/final"))
            .unwrap()
            .is_none());
        assert_eq!(
            vfs.durable_file(Path::new("/data/temporary"))
                .unwrap()
                .unwrap(),
            b"segment"
        );

        vfs.rename(Path::new("/data/temporary"), Path::new("/data/final"))
            .unwrap();
        vfs.sync_directory(Path::new("/data")).unwrap();
        vfs.crash().unwrap();
        assert_eq!(
            vfs.durable_file(Path::new("/data/final")).unwrap().unwrap(),
            b"segment"
        );
    }

    #[test]
    fn enospc_and_eio_before_write_publish_no_bytes() {
        for kind in [io::ErrorKind::StorageFull, io::ErrorKind::Other] {
            let vfs = DeterministicVfs::new(Some(FaultRule {
                operation_number: 4,
                action: FaultAction::ErrorBefore(kind),
            }));
            vfs.create_dir(Path::new("/data")).unwrap();
            vfs.sync_directory(Path::new("/")).unwrap();
            let mut file = vfs.open(Path::new("/data/value"), writable(true)).unwrap();
            assert_eq!(file.write_all_at(0, b"bytes").unwrap_err().kind(), kind);
            vfs.crash().unwrap();
            assert!(vfs
                .durable_file(Path::new("/data/value"))
                .unwrap()
                .is_none());
        }
    }

    #[test]
    fn synced_existing_file_survives_later_unsynced_write() {
        let vfs = DeterministicVfs::new(None);
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut file = vfs
            .open(Path::new("/data/control"), writable(true))
            .unwrap();
        file.write_all_at(0, b"old").unwrap();
        file.sync_all().unwrap();
        vfs.sync_directory(Path::new("/data")).unwrap();
        file.write_all_at(0, b"new").unwrap();
        vfs.crash().unwrap();
        assert_eq!(
            vfs.durable_file(Path::new("/data/control"))
                .unwrap()
                .unwrap(),
            b"old"
        );
    }
}

#[cfg(unix)]
impl VfsPositionedReader for RealFile {
    fn len(&self) -> io::Result<u64> { VfsFile::len(self) }
    fn read_exact_at(&self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        let _read = crate::request_profile::Probe::new(crate::request_profile::VFS_READ);
        crate::request_profile::work(crate::request_profile::Work::ReadBytes, bytes.len() as u64);
        std::os::unix::fs::FileExt::read_exact_at(&self.file, bytes, offset)
    }
}

impl VfsPositionedReader for SimulatedFile {
    fn len(&self) -> io::Result<u64> { VfsFile::len(self) }
    fn read_exact_at(&self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        self.read_exact_at_shared(offset, bytes)
    }
}

impl SimulatedFile {
    fn read_exact_at_shared(&self, offset: u64, output: &mut [u8]) -> io::Result<()> {
        if !self.readable {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "VFS file is not readable",
            ));
        }
        let mut state = state(&self.shared)?;
        before(&mut state, Operation::Read)?;
        let offset = usize::try_from(offset)
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "VFS offset overflow"))?;
        let end = offset
            .checked_add(output.len())
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "VFS read overflow"))?;
        let file = state
            .working_files
            .get(&self.path)
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "VFS file is missing"))?;
        let source = file.get(offset..end).ok_or_else(|| {
            io::Error::new(io::ErrorKind::UnexpectedEof, "VFS range is truncated")
        })?;
        output.copy_from_slice(source);
        Ok(())
    }

}
