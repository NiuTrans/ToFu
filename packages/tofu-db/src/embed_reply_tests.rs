//! Reply ownership/error contracts while allocation experiments stay rejected.
use super::*;
use std::sync::{Arc, atomic::{AtomicUsize, Ordering}};
#[derive(Debug)]
struct Tracked(Arc<AtomicUsize>);
impl Drop for Tracked { fn drop(&mut self) { self.0.fetch_add(1, Ordering::Relaxed); } }
#[test]
fn discarded_job_disconnects_immediately_without_a_retained_sender() {
    let (sender, receiver) = fresh::<usize>(); drop(sender);
    assert_eq!(receiver.recv_timeout(Duration::ZERO), Err(mpsc::RecvTimeoutError::Disconnected));
}
#[test]
fn timeout_retirement_drops_late_value_and_cannot_cross_request_boundaries() {
    let (late, old) = fresh::<Tracked>();
    assert!(matches!(old.recv_timeout(Duration::ZERO), Err(mpsc::RecvTimeoutError::Timeout))); drop(old);
    let drops = Arc::new(AtomicUsize::new(0));
    drop(late.send(Tracked(Arc::clone(&drops))).expect_err("retired receiver"));
    assert_eq!(drops.load(Ordering::Relaxed), 1);
    let (sender, next) = fresh::<usize>(); sender.send(222).unwrap();
    assert_eq!(next.recv_timeout(Duration::ZERO), Ok(222));
}
#[test]
fn scope_drop_releases_an_unconsumed_owned_response() {
    let (sender, receiver) = fresh::<Tracked>(); let drops = Arc::new(AtomicUsize::new(0));
    sender.send(Tracked(Arc::clone(&drops))).unwrap(); drop(receiver);
    assert_eq!(drops.load(Ordering::Relaxed), 1);
}
