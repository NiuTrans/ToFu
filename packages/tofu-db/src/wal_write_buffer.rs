//! Reconstructible framed WAL writes owned by the existing barrier worker.
//! The byte budget retains queued/inflight copies; overflow uses ordinary
//! synchronous writes, never admission/rejection. No durability credit here.
use crate::vfs::VfsFile;
use std::collections::VecDeque;
use std::io::{self, IoSlice};

#[derive(Default, Debug)]
pub(super) struct WriteBuffer {
    pub capacity: usize,
    pub retained_bytes: usize,
    pub peak_bytes: usize,
    pub pending: VecDeque<WriteFrame>,
    pub queued_frames: u64,
    pub written_frames: u64,
    pub written_batches: u64,
    pub written_bytes: u64,
    pub write_nanos: u64,
    pub synchronous_fallbacks: u64,
}
pub(super) struct WriteFrame {
    pub offset: u64,
    pub bytes: Vec<u8>,
}
impl std::fmt::Debug for WriteFrame {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("WriteFrame")
            .field("offset", &self.offset)
            .field("bytes", &self.bytes.len())
            .finish()
    }
}
impl WriteBuffer {
    pub fn queue(&mut self, offset: u64, parts: &[&[u8]]) -> io::Result<bool> {
        let bytes = parts
            .iter()
            .try_fold(0usize, |n, p| n.checked_add(p.len()))
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "WAL write buffer accounting overflow",
                )
            })?;
        if self.capacity == 0 || bytes > self.capacity.saturating_sub(self.retained_bytes) {
            self.synchronous_fallbacks += 1;
            return Ok(false);
        }
        let mut owned = Vec::new();
        if owned.try_reserve_exact(bytes).is_err() {
            self.synchronous_fallbacks += 1;
            return Ok(false);
        }
        for part in parts {
            owned.extend_from_slice(part);
        }
        if self.pending.try_reserve(1).is_err() {
            self.synchronous_fallbacks += 1;
            return Ok(false);
        }
        self.pending.push_back(WriteFrame {
            offset,
            bytes: owned,
        });
        self.retained_bytes += bytes;
        self.peak_bytes = self.peak_bytes.max(self.retained_bytes);
        self.queued_frames += 1;
        Ok(true)
    }
    pub fn take(&mut self) -> VecDeque<WriteFrame> {
        std::mem::take(&mut self.pending)
    }
    pub fn release_counts(
        &mut self,
        bytes: usize,
        frames: usize,
        success: bool,
        batches: usize,
        nanos: u64,
    ) {
        self.retained_bytes -= bytes;
        self.write_nanos = self.write_nanos.saturating_add(nanos);
        if success {
            self.written_frames += frames as u64;
            self.written_batches += batches as u64;
            self.written_bytes += bytes as u64;
        }
    }
}

/// Synchronous budget fallbacks can leave gaps between queued frames. Only
/// contiguous runs are vectored together; every byte keeps its original offset.
pub(super) fn write_frames(
    file: &mut dyn VfsFile,
    frames: &VecDeque<WriteFrame>,
) -> io::Result<usize> {
    let mut start = 0;
    let mut batches = 0;
    while start < frames.len() {
        let mut end = start + 1;
        let mut next = frames[start].offset + frames[start].bytes.len() as u64;
        while end < frames.len() && frames[end].offset == next {
            next += frames[end].bytes.len() as u64;
            end += 1;
        }
        let mut buffers = frames
            .iter()
            .skip(start)
            .take(end - start)
            .map(|f| IoSlice::new(&f.bytes))
            .collect::<Vec<_>>();
        file.write_all_vectored_at(frames[start].offset, &mut buffers)?;
        batches += 1;
        start = end;
    }
    Ok(batches)
}
