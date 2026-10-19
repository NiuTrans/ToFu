//! Authority-owned compression contexts borrowed by independent preparations.
//! Entry: ChunkEncoder; blob.rs owns the unchanged chunk format and verification.
//! Only pool metadata is locked. Leases and idle contexts die with their owner;
//! no TLS cache, worker, admission queue or durable state is introduced.
use serde_json::{json, Value};
use std::io;
use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Arc, Mutex,
};
use zstd::bulk::Compressor;

const LEAN_IDLE_BYTES: usize = 8 * crate::blob::BLOB_CHUNK_BYTES;
const IDLE_HEADROOM_DIVISOR: u64 = 128;

struct Pool {
    idle: Vec<Compressor<'static>>,
    idle_bytes: usize,
    context_limit: usize,
    byte_limit: usize,
}
pub(crate) struct ChunkEncoder {
    pool: Mutex<Pool>,
    poisoned: AtomicBool,
    active: AtomicU64,
    peak_active: AtomicU64,
    created: AtomicU64,
    reused: AtomicU64,
}
impl Default for ChunkEncoder {
    fn default() -> Self {
        Self {
            pool: Mutex::new(Pool {
                idle: Vec::new(),
                idle_bytes: 0,
                context_limit: 1,
                byte_limit: LEAN_IDLE_BYTES,
            }),
            poisoned: AtomicBool::new(false),
            active: AtomicU64::new(0),
            peak_active: AtomicU64::new(0),
            created: AtomicU64::new(0),
            reused: AtomicU64::new(0),
        }
    }
}

struct CodecLease<'a> {
    owner: &'a ChunkEncoder,
    codec: Option<Compressor<'static>>,
}
impl Drop for CodecLease<'_> {
    fn drop(&mut self) {
        let mut codec = self.codec.take().expect("live codec lease");
        if std::thread::panicking() {
            self.owner.poisoned.store(true, Ordering::Release);
        }
        let bytes = codec.context_mut().sizeof();
        self.owner.active.fetch_sub(1, Ordering::Relaxed);
        let Ok(mut pool) = self.owner.pool.lock() else {
            self.owner.poisoned.store(true, Ordering::Release);
            return;
        };
        if !self.owner.poisoned.load(Ordering::Acquire)
            && pool.idle.len() < pool.context_limit
            && bytes <= pool.byte_limit.saturating_sub(pool.idle_bytes)
        {
            pool.idle_bytes += bytes;
            pool.idle.push(codec);
        }
    }
}

impl ChunkEncoder {
    fn require_live(&self) -> io::Result<()> {
        if self.poisoned.load(Ordering::Acquire) {
            Err(io::Error::other("blob codec is poisoned"))
        } else {
            Ok(())
        }
    }
    fn lease(&self) -> io::Result<CodecLease<'_>> {
        self.require_live()?;
        let wait = crate::request_profile::Probe::new(crate::request_profile::BLOB_CODEC_WAIT);
        let cached = {
            let mut pool = self
                .pool
                .lock()
                .map_err(|_| io::Error::other("blob codec is poisoned"))?;
            let mut cached = pool.idle.pop();
            if let Some(codec) = cached.as_mut() {
                pool.idle_bytes -= codec.context_mut().sizeof();
            }
            cached
        };
        drop(wait);
        let codec = match cached {
            Some(codec) => {
                self.reused.fetch_add(1, Ordering::Relaxed);
                codec
            }
            None => {
                let codec = Compressor::new(3)?;
                self.created.fetch_add(1, Ordering::Relaxed);
                codec
            }
        };
        self.require_live()?;
        let active = self.active.fetch_add(1, Ordering::Relaxed) + 1;
        self.peak_active.fetch_max(active, Ordering::Relaxed);
        Ok(CodecLease {
            owner: self,
            codec: Some(codec),
        })
    }

    pub(crate) fn configure(
        &self,
        snapshot: crate::resource_probe::LaunchResourceSnapshot,
    ) -> io::Result<()> {
        let mut pool = self
            .pool
            .lock()
            .map_err(|_| io::Error::other("blob codec is poisoned"))?;
        pool.context_limit = snapshot.logical_cpus.unwrap_or(1).max(1);
        pool.byte_limit = snapshot
            .memory_headroom_bytes
            .map(|bytes| usize::try_from(bytes / IDLE_HEADROOM_DIVISOR).unwrap_or(usize::MAX))
            .unwrap_or(LEAN_IDLE_BYTES);
        while pool.idle.len() > pool.context_limit || pool.idle_bytes > pool.byte_limit {
            let Some(mut codec) = pool.idle.pop() else {
                break;
            };
            pool.idle_bytes -= codec.context_mut().sizeof();
        }
        if pool.idle.is_empty() {
            pool.idle.shrink_to_fit();
        }
        Ok(())
    }

    pub(crate) fn encode(&self, raw: &[u8]) -> io::Result<Vec<u8>> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_BLOB_ENCODE);
        if raw.is_empty() || raw.len() > crate::blob::BLOB_CHUNK_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "blob chunk is empty or exceeds 1 MiB",
            ));
        }
        let mut lease = self.lease()?;
        let compress =
            crate::request_profile::Probe::new(crate::request_profile::BLOB_CODEC_COMPRESS);
        let compressed = lease.codec.as_mut().unwrap().compress(raw)?;
        drop(compress);
        // Retain the lease through framing: a panic anywhere in encoding must
        // preserve the original shared encoder's fail-closed poison boundary.
        let frame = crate::blob::encode_compressed_chunk(raw, &compressed);
        drop(lease);
        self.require_live()?;
        frame
    }

    pub(crate) fn observer(self: &Arc<Self>) -> BlobCodecPoolObserver {
        BlobCodecPoolObserver(Arc::clone(self))
    }
    fn metrics(&self) -> Value {
        let Ok(pool) = self.pool.lock() else {
            return json!({"available": false});
        };
        json!({"available": true, "contract": "tofudb.blob-codec-pool/v1",
            "activeContexts": self.active.load(Ordering::Relaxed),
            "peakActiveContexts": self.peak_active.load(Ordering::Relaxed),
            "createdContexts": self.created.load(Ordering::Relaxed),
            "reusedContexts": self.reused.load(Ordering::Relaxed),
            "idleContexts": pool.idle.len(), "idleContextBytes": pool.idle_bytes,
            "idleContextLimit": pool.context_limit, "idleByteBudget": pool.byte_limit,
            "poisoned": self.poisoned.load(Ordering::Acquire),
            "scope": "authority lifetime; zstd-reported idle workspace bytes; active workspace and allocator metadata excluded"})
    }
}

#[derive(Clone)]
pub struct BlobCodecPoolObserver(Arc<ChunkEncoder>);
impl BlobCodecPoolObserver {
    pub fn metrics(&self) -> Value {
        self.0.metrics()
    }
}

#[cfg(test)]
#[path = "blob_codec_tests.rs"]
mod tests;
