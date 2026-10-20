//! Test-only cooperative-stop injection during real storage reads.
//! Wraps the deterministic VFS without replacing its fault/durability model.
use crate::vfs::*;
use std::{
    io,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc,
    },
};

type Gate = std::sync::Mutex<Option<(std::sync::mpsc::Sender<()>, std::sync::mpsc::Receiver<()>)>>;

pub(crate) struct StopVfs {
    pub inner: DeterministicVfs,
    pub stop: Arc<AtomicBool>,
    reads: Arc<AtomicUsize>,
    at: Arc<AtomicUsize>,
    open_gate: Gate,
    read_gate: Arc<Gate>,
}

impl StopVfs {
    pub fn new() -> Self {
        Self {
            inner: DeterministicVfs::new(None),
            stop: Arc::new(AtomicBool::new(false)),
            reads: Arc::new(AtomicUsize::new(0)),
            at: Arc::new(AtomicUsize::new(usize::MAX)),
            open_gate: std::sync::Mutex::new(None),
            read_gate: Arc::new(std::sync::Mutex::new(None)),
        }
    }
    pub fn arm(&self, at: usize) {
        self.reads.store(0, Ordering::SeqCst);
        self.stop.store(false, Ordering::Release);
        self.at.store(at, Ordering::SeqCst);
    }
    pub fn reads(&self) -> usize {
        self.reads.load(Ordering::SeqCst)
    }
    pub fn block_next_read(&self) -> (std::sync::mpsc::Receiver<()>, std::sync::mpsc::Sender<()>) {
        let (entered, wait) = std::sync::mpsc::channel();
        let (release, released) = std::sync::mpsc::channel();
        *self.read_gate.lock().unwrap() = Some((entered, released));
        (wait, release)
    }
    pub fn block_next_open(&self) -> (std::sync::mpsc::Receiver<()>, std::sync::mpsc::Sender<()>) {
        let (entered, wait) = std::sync::mpsc::channel();
        let (release, released) = std::sync::mpsc::channel();
        *self.open_gate.lock().unwrap() = Some((entered, released));
        (wait, release)
    }
}

impl Vfs for StopVfs {
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
        let gate = self.open_gate.lock().unwrap().take();
        if let Some((entered, release)) = gate {
            entered.send(()).map_err(io::Error::other)?;
            release
                .recv_timeout(std::time::Duration::from_secs(5))
                .map_err(io::Error::other)?;
        }
        Ok(Box::new(StopFile {
            inner: self.inner.open(path, request)?,
            stop: Arc::clone(&self.stop),
            reads: Arc::clone(&self.reads),
            at: Arc::clone(&self.at),
            read_gate: Arc::clone(&self.read_gate),
        }))
    }
    fn filesystem_capacity(&self, path: &Path) -> io::Result<FilesystemCapacity> {
        self.inner.filesystem_capacity(path)
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
    fn read_directory_bounded(
        &self,
        path: &Path,
        maximum: usize,
    ) -> io::Result<BoundedDirectoryEntries> {
        self.inner.read_directory_bounded(path, maximum)
    }
    fn match_directory_prefix_bounded(
        &self,
        path: &Path,
        prefix: &str,
        scanned: usize,
        matches: usize,
    ) -> io::Result<BoundedDirectoryMatches> {
        self.inner
            .match_directory_prefix_bounded(path, prefix, scanned, matches)
    }
    fn sync_directory(&self, path: &Path) -> io::Result<()> {
        self.inner.sync_directory(path)
    }
}

struct StopFile {
    inner: Box<dyn VfsFile>,
    stop: Arc<AtomicBool>,
    reads: Arc<AtomicUsize>,
    at: Arc<AtomicUsize>,
    read_gate: Arc<Gate>,
}
impl StopFile {
    fn read(&self) -> io::Result<()> {
        let gate = self.read_gate.lock().unwrap().take();
        if let Some((entered, release)) = gate {
            entered.send(()).map_err(io::Error::other)?;
            release
                .recv_timeout(std::time::Duration::from_secs(5))
                .map_err(io::Error::other)?;
        }
        if self.reads.fetch_add(1, Ordering::SeqCst) + 1 == self.at.load(Ordering::SeqCst) {
            self.stop.store(true, Ordering::Release);
        }
        Ok(())
    }
}
impl VfsFile for StopFile {
    fn len(&self) -> io::Result<u64> {
        self.inner.len()
    }
    fn read_all(&mut self, maximum: usize) -> io::Result<Vec<u8>> {
        self.read()?;
        self.inner.read_all(maximum)
    }
    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        self.read()?;
        self.inner.read_exact_at(offset, bytes)
    }
    fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> {
        self.inner.write_all_at(offset, bytes)
    }
    fn set_len(&mut self, len: u64) -> io::Result<()> {
        self.inner.set_len(len)
    }
    fn sync_data(&mut self) -> io::Result<()> {
        self.inner.sync_data()
    }
    fn sync_all(&mut self) -> io::Result<()> {
        self.inner.sync_all()
    }
    fn try_lock_exclusive(&mut self) -> io::Result<()> {
        self.inner.try_lock_exclusive()
    }
}
