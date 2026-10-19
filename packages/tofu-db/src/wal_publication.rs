//! Captured WAL coverage for off-authority physical publications.
//! Entry: ActiveLog::publication_cover; consumers are rotation, history pruning
//! and pack adoption. Buffered bytes must cross their generation-bound worker
//! witness before the original lost-sync barrier and any CONTROL publication.
use super::*;

pub(crate) struct PublicationWalCover {
    path: PathBuf,
    vfs: Arc<dyn Vfs>,
    worker_witness: Option<(WalBarrier, WalWitness)>,
}

impl ActiveLog {
    pub(crate) fn publication_cover(&self, vfs: Arc<dyn Vfs>) -> io::Result<Option<PublicationWalCover>> {
        if self.unsynced_bytes() == 0 { return Ok(None); }
        let worker_witness = self.barrier.as_ref().map(|barrier| {
            Ok::<_, io::Error>((barrier.clone(), barrier.witness(self.len)?))
        }).transpose()?;
        Ok(Some(PublicationWalCover { path: self.path.clone(), vfs, worker_witness }))
    }
}

impl PublicationWalCover {
    pub(crate) fn sync(&self) -> io::Result<()> {
        if let Some((barrier, witness)) = &self.worker_witness {
            let _wait = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_WAL_BARRIER_WAIT);
            barrier.wait_witness(witness, std::time::Instant::now() + MAX_BARRIER_WAIT)?;
        }
        let mut file = self.vfs.open(&self.path, OpenRequest { write: true, ..Default::default() })?;
        crate::vfs::sync_data_barrier(file.as_mut())
    }
}
