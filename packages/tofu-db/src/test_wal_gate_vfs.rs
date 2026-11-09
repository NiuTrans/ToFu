//! Test-only gates on active-WAL physical writes and syncs. Wraps any VFS and
//! gates only `.wal` files, so pages, blocks and CONTROL pass straight through.
//! A gated operation reports `entered`, then waits for `release` before it
//! reaches the wrapped file; the caller observes exactly which thread stalls.
use crate::vfs::*;
use std::io::{self, IoSlice};
use std::path::{Path, PathBuf};
use std::sync::{mpsc, Arc, Mutex};
use std::time::Duration;

type Gate = Arc<Mutex<Option<(mpsc::Sender<()>, mpsc::Receiver<()>)>>>;

/// A stalled test may never release its gate; bound the wait so a failing
/// specification reports instead of hanging the suite.
const GATE_RELEASE_BOUND: Duration = Duration::from_secs(60);

pub(crate) struct WalGateVfs {
    inner: Arc<dyn Vfs>,
    write_gate: Gate,
    sync_gate: Gate,
}

impl WalGateVfs {
    pub(crate) fn new(inner: Arc<dyn Vfs>) -> Self {
        Self { inner, write_gate: Gate::default(), sync_gate: Gate::default() }
    }

    /// The next physical write to a `.wal` file stalls until released.
    pub(crate) fn block_next_wal_write(&self) -> (mpsc::Receiver<()>, mpsc::Sender<()>) {
        arm(&self.write_gate)
    }

    /// The next data sync of a `.wal` file stalls until released.
    pub(crate) fn block_next_wal_sync(&self) -> (mpsc::Receiver<()>, mpsc::Sender<()>) {
        arm(&self.sync_gate)
    }
}

fn arm(gate: &Gate) -> (mpsc::Receiver<()>, mpsc::Sender<()>) {
    let (entered, wait_entered) = mpsc::channel();
    let (release, released) = mpsc::channel();
    *gate.lock().unwrap() = Some((entered, released));
    (wait_entered, release)
}

fn pass(gate: &Gate) -> io::Result<()> {
    let armed = gate.lock().unwrap().take();
    if let Some((entered, release)) = armed {
        entered.send(()).map_err(io::Error::other)?;
        release.recv_timeout(GATE_RELEASE_BOUND).map_err(io::Error::other)?;
    }
    Ok(())
}

fn is_wal(path: &Path) -> bool {
    path.extension().is_some_and(|extension| extension == "wal")
}

impl Vfs for WalGateVfs {
    fn supports_buffered_wal_writes(&self) -> bool {
        self.inner.supports_buffered_wal_writes()
    }
    fn filesystem_capacity(&self, path: &Path) -> io::Result<FilesystemCapacity> {
        self.inner.filesystem_capacity(path)
    }
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
        let file = self.inner.open(path, request)?;
        if !is_wal(path) {
            return Ok(file);
        }
        Ok(Box::new(GatedFile {
            inner: file,
            write_gate: Arc::clone(&self.write_gate),
            sync_gate: Arc::clone(&self.sync_gate),
        }))
    }
    fn open_positioned_reader(&self, path: &Path) -> io::Result<Option<Arc<dyn VfsPositionedReader>>> {
        self.inner.open_positioned_reader(path)
    }
    fn create_dir(&self, path: &Path) -> io::Result<()> {
        self.inner.create_dir(path)
    }
    fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> {
        self.inner.rename(source, destination)
    }
    fn remove_file(&self, path: &Path) -> io::Result<()> {
        self.inner.remove_file(path)
    }
    fn metadata(&self, path: &Path) -> io::Result<FileKind> {
        self.inner.metadata(path)
    }
    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> {
        self.inner.read_directory(path)
    }
    fn read_directory_entries(&self, path: &Path) -> io::Result<Vec<DirectoryEntry>> {
        self.inner.read_directory_entries(path)
    }
    fn read_directory_bounded(&self, path: &Path, maximum: usize) -> io::Result<BoundedDirectoryEntries> {
        self.inner.read_directory_bounded(path, maximum)
    }
    fn match_directory_prefix_bounded(
        &self,
        path: &Path,
        prefix: &str,
        scanned: usize,
        matches: usize,
    ) -> io::Result<BoundedDirectoryMatches> {
        self.inner.match_directory_prefix_bounded(path, prefix, scanned, matches)
    }
    fn sync_directory(&self, path: &Path) -> io::Result<()> {
        self.inner.sync_directory(path)
    }
}

struct GatedFile {
    inner: Box<dyn VfsFile>,
    write_gate: Gate,
    sync_gate: Gate,
}

impl VfsFile for GatedFile {
    fn len(&self) -> io::Result<u64> {
        self.inner.len()
    }
    fn read_all(&mut self, maximum_bytes: usize) -> io::Result<Vec<u8>> {
        self.inner.read_all(maximum_bytes)
    }
    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        self.inner.read_exact_at(offset, bytes)
    }
    fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> {
        pass(&self.write_gate)?;
        self.inner.write_all_at(offset, bytes)
    }
    fn write_all_vectored_at(&mut self, offset: u64, buffers: &mut [IoSlice<'_>]) -> io::Result<()> {
        pass(&self.write_gate)?;
        self.inner.write_all_vectored_at(offset, buffers)
    }
    fn set_len(&mut self, length: u64) -> io::Result<()> {
        self.inner.set_len(length)
    }
    fn sync_data(&mut self) -> io::Result<()> {
        pass(&self.sync_gate)?;
        self.inner.sync_data()
    }
    fn sync_all(&mut self) -> io::Result<()> {
        self.inner.sync_all()
    }
    fn try_lock_exclusive(&mut self) -> io::Result<()> {
        self.inner.try_lock_exclusive()
    }
}
