//! Bounded filesystem interface and deterministic durability simulator.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
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
    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()>;
    fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()>;
    fn set_len(&mut self, length: u64) -> io::Result<()>;
    fn sync_data(&mut self) -> io::Result<()>;
    fn sync_all(&mut self) -> io::Result<()>;
    fn try_lock_exclusive(&mut self) -> io::Result<()>;
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

pub trait Vfs: Send + Sync {
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>>;
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

struct RealFile(File);

impl VfsFile for RealFile {
    fn len(&self) -> io::Result<u64> {
        Ok(self.0.metadata()?.len())
    }

    fn read_all(&mut self, maximum_bytes: usize) -> io::Result<Vec<u8>> {
        let length = self.len()?;
        if length > maximum_bytes as u64 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "file exceeds its read admission bound",
            ));
        }
        self.0.seek(SeekFrom::Start(0))?;
        let mut bytes = Vec::with_capacity(length as usize);
        self.0.read_to_end(&mut bytes)?;
        Ok(bytes)
    }

    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        self.0.seek(SeekFrom::Start(offset))?;
        self.0.read_exact(bytes)
    }

    fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> {
        self.0.seek(SeekFrom::Start(offset))?;
        self.0.write_all(bytes)
    }

    fn set_len(&mut self, length: u64) -> io::Result<()> {
        self.0.set_len(length)
    }

    fn sync_data(&mut self) -> io::Result<()> {
        self.0.sync_data()
    }

    fn sync_all(&mut self) -> io::Result<()> {
        self.0.sync_all()
    }

    fn try_lock_exclusive(&mut self) -> io::Result<()> {
        self.0.try_lock_exclusive()
    }
}

impl Vfs for RealVfs {
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
        let file = OpenOptions::new()
            .read(request.read)
            .write(request.write)
            .create(request.create)
            .create_new(request.create_new)
            .truncate(request.truncate)
            .open(path)?;
        Ok(Box::new(RealFile(file)))
    }

    fn create_dir(&self, path: &Path) -> io::Result<()> {
        fs::create_dir(path)
    }

    fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> {
        fs::rename(source, destination)
    }

    fn remove_file(&self, path: &Path) -> io::Result<()> {
        fs::remove_file(path)
    }

    fn metadata(&self, path: &Path) -> io::Result<FileKind> {
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
    fault: Option<FaultRule>,
    trace: Vec<Operation>,
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
        .fault
        .filter(|fault| fault.operation_number == state.operation_count)
        .map(|fault| fault.action);
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
                fault,
                trace: Vec::new(),
            })),
        }
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
        let mut state = state(&self.shared)?;
        state.operation_count = 0;
        state.trace.clear();
        state.fault = fault;
        Ok(())
    }

    pub fn trace(&self) -> io::Result<Vec<Operation>> {
        Ok(state(&self.shared)?.trace.clone())
    }

    pub fn durable_file(&self, path: &Path) -> io::Result<Option<Vec<u8>>> {
        Ok(state(&self.shared)?.durable_files.get(path).cloned())
    }
}

impl Vfs for DeterministicVfs {
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
                "VFS file is missing",
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
    use super::*;

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
