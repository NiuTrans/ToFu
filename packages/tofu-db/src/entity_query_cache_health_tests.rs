//! Cached bytes need live publication and source health, but no physical pin.
use super::*;
#[test]
fn query_result_cache_publication_health_and_generation_are_nonblocking() {
    let directory=tempfile::tempdir().unwrap();
    let mut writer=EntityDatabase::initialize(directory.path()).unwrap();
    let view=writer.enable_published_queries().unwrap();
    assert_eq!(view.with_healthy_current_snapshot(|snapshot|snapshot),Some(writer.current_snapshot()));
    let held=view.state.write().unwrap();
    assert!(view.with_healthy_current_snapshot(|_|()).is_none());drop(held);
    writer.writer_engine().unwrap().restart_flag().store(true,Ordering::Release);
    assert!(view.with_healthy_current_snapshot(|_|()).is_none());
    writer.writer_engine().unwrap().restart_flag().store(false,Ordering::Release);
    assert!(view.with_healthy_current_snapshot(|_|()).is_some());
    let old=writer.current_snapshot();
    let mut tx=writer.begin(7,11).unwrap();
    writer.put(&mut tx,crate::entity::EntityKey::new(7,11,"health",b"k").unwrap(),b"bytes".to_vec()).unwrap();
    writer.commit(tx).unwrap();
    let current=view.with_healthy_current_snapshot(|snapshot|snapshot).unwrap();
    assert_ne!(old,current);assert_eq!(current,writer.current_snapshot());
    writer.merge_overlay_fully().unwrap();
    assert_eq!(view.with_healthy_current_snapshot(|snapshot|snapshot),Some(writer.current_snapshot()));
    assert_eq!(writer.snapshot_pin_metrics().unwrap().active_handles,0);
    view.poison_for_test();assert!(view.with_healthy_current_snapshot(|_|()).is_none());
}
