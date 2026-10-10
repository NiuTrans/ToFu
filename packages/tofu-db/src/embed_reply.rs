//! Fresh watchdog reply-channel lifetime and optional caller-edge diagnostics.
//! Entry point: fresh; ScopedReceiver preserves standard recv_timeout errors.
//! Dependencies: std mpsc, fsync_stats. Reuse experiments remain in evidence.
use std::sync::mpsc;
use std::time::Duration;

pub(super) struct ScopedReceiver<T>(Option<mpsc::Receiver<T>>);
pub(super) fn fresh<T>() -> (mpsc::Sender<T>, ScopedReceiver<T>) {
    let _acquire = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_REPLY_ACQUIRE);
    let (sender, receiver) = mpsc::channel();
    (sender, ScopedReceiver(Some(receiver)))
}
impl<T> ScopedReceiver<T> {
    pub(super) fn recv_timeout(&self, timeout: Duration) -> Result<T, mpsc::RecvTimeoutError> {
        self.0.as_ref().expect("receiver lives until scope drop").recv_timeout(timeout)
    }
}
impl<T> Drop for ScopedReceiver<T> {
    fn drop(&mut self) {
        let _recycle = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_REPLY_RECYCLE);
        // Drop inside the timer: field destruction after Drop::drop would
        // otherwise exclude receiver disposal from the lifetime anchor.
        drop(self.0.take());
    }
}
#[cfg(test)]
#[path = "embed_reply_tests.rs"]
mod tests;
