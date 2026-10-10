//! Snapshot topology and aggregate transaction-byte accounting. Registration
//! and GC reachability use the original mutex. Exact byte
//! reservations use one atomic counter on that same database-owned registry;
//! leases can repay bytes without entering GC's critical section.
use super::{io, SnapshotPinState};
use std::sync::{atomic::{AtomicUsize, Ordering}, LockResult, Mutex, MutexGuard};

#[derive(Debug, Default)]
pub(crate) struct SnapshotPinRegistry {
    topology: Mutex<SnapshotPinState>,
    retained: AtomicUsize,
}
impl SnapshotPinRegistry {
    pub(super) fn lock(&self) -> LockResult<MutexGuard<'_, SnapshotPinState>> {
        let _wait =
            crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_PIN_REGISTRY_WAIT);
        crate::request_profile::work(crate::request_profile::Work::PinRegistryAcquire, 1);
        self.topology.lock()
    }
    pub(super) fn retained_bytes(&self) -> usize {
        self.retained.load(Ordering::Relaxed)
    }
    pub(super) fn reserve_bytes(&self, bytes: usize) -> io::Result<()> {
        let _reserve =
            crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_PIN_BYTE_RESERVE);
        crate::request_profile::work(crate::request_profile::Work::PinBudgetReserve, 1);
        if self.topology.is_poisoned() {
            return Err(io::Error::other("entity snapshot pin registry is poisoned"));
        }
        // No payload publication relies on this counter. CAS alone linearizes
        // exact observations; topology and payload visibility keep their own locks.
        self.retained
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |retained| {
                retained
                    .checked_add(bytes)
            })
            .map_err(|_| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "aggregate entity transaction byte accounting overflow",
                )
            })?;
        Ok(())
    }
    pub(super) fn release_bytes(&self, bytes: usize) -> bool {
        let _release =
            crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_PIN_BYTE_RELEASE);
        crate::request_profile::work(crate::request_profile::Work::PinBudgetRelease, 1);
        // Keep the existing fail-closed poison boundary, including Drop's
        // best-effort accounting behavior. Zero-byte leases need no CAS.
        if self.topology.is_poisoned() {
            return false;
        }
        if bytes != 0 {
            let _ = self
                .retained
                .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |retained| {
                    Some(retained.saturating_sub(bytes))
                });
        }
        true
    }
}
