//! Optional cache health never waits on the WAL barrier or hides its latch.
use super::*;
#[test]
fn query_result_cache_wal_health_refuses_contention_error_and_poison() {
    let barrier=WalBarrier::default();assert!(barrier.try_healthy());
    let held=barrier.state.lock().unwrap();assert!(!barrier.try_healthy());drop(held);
    barrier.inject_background_error_for_test(io::Error::other("cache health"));
    assert!(!barrier.try_healthy());
    let barrier=WalBarrier::default();let other=barrier.clone();
    assert!(std::thread::spawn(move||{let _guard=other.state.lock().unwrap();panic!("health poison");}).join().is_err());
    assert!(!barrier.try_healthy());
}
