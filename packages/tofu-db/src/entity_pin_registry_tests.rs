//! Exact byte accounting and owned-root lifecycle contracts for snapshot pins.
use super::*;
use std::sync::{mpsc, Barrier};
use std::time::Duration;

fn fixture() -> (Arc<SnapshotPinRegistry>, Arc<SnapshotPin>) {
    let registry = Arc::new(SnapshotPinRegistry::default());
    let root = Arc::new(
        pin_snapshot(
            &registry,
            EntitySnapshot {
                sequence: 0,
                root: None,
            },
        )
        .unwrap(),
    );
    (registry, root)
}

#[test]
fn concurrent_leases_cross_retired_quota_and_release_exact_bytes() {
    let (registry, root) = fixture();
    let reserved = Arc::new(Barrier::new(9));
    let repay = Arc::new(Barrier::new(9));
    std::thread::scope(|scope| {
        for index in 0..8 {
            let mut lease = root.lease();
            let reserved = Arc::clone(&reserved);
            let repay = Arc::clone(&repay);
            scope.spawn(move || {
                let bytes = LEGACY_AGGREGATE_TRANSACTION_BYTES / 8
                    + usize::from(index < LEGACY_AGGREGATE_TRANSACTION_BYTES % 8);
                lease.reserve_transaction_bytes(bytes).unwrap();
                reserved.wait();
                repay.wait();
            });
        }
        reserved.wait();
        assert_eq!(
            registry.retained_bytes(),
            LEGACY_AGGREGATE_TRANSACTION_BYTES
        );
        let mut extra = root.lease();
        extra.reserve_transaction_bytes(1).unwrap();
        assert_eq!(extra.retained_transaction_bytes, 1);
        assert_eq!(registry.retained_bytes(), LEGACY_AGGREGATE_TRANSACTION_BYTES + 1);
        drop(extra);
        repay.wait();
    });
    assert_eq!(registry.retained_bytes(), 0);
    assert_eq!(registry.lock().unwrap().active_handles, 1);
    drop(root);
    assert_eq!(registry.lock().unwrap().active_handles, 0);
}

#[test]
fn lease_repayment_does_not_wait_for_topology_mutex() {
    let (registry, root) = fixture();
    let mut lease = root.lease();
    lease.reserve_transaction_bytes(1024).unwrap();
    let (done, received) = mpsc::channel();
    std::thread::scope(|scope| {
        let topology = registry.lock().unwrap();
        scope.spawn(move || {
            drop(lease);
            done.send(()).unwrap();
        });
        let result = received.recv_timeout(Duration::from_secs(2));
        let bytes = registry.retained_bytes();
        assert_eq!(topology.active_handles, 1);
        drop(topology);
        result.unwrap();
        assert_eq!(bytes, 0);
    });
}

#[test]
fn aggregate_byte_overflow_is_atomic_and_leases_repay_exactly() {
    let (registry, root) = fixture();
    let mut first = root.lease();
    first.reserve_transaction_bytes(usize::MAX - 1).unwrap();
    let mut second = root.lease();
    assert_eq!(second.reserve_transaction_bytes(2).unwrap_err().kind(), io::ErrorKind::InvalidData);
    assert_eq!(second.retained_transaction_bytes, 0);
    assert_eq!(registry.retained_bytes(), usize::MAX - 1);
    second.reserve_transaction_bytes(1).unwrap();
    assert_eq!(registry.retained_bytes(), usize::MAX);
    drop(first);
    assert_eq!(registry.retained_bytes(), 1);
    drop(second);
    assert_eq!(registry.retained_bytes(), 0);
}

#[test]
fn concurrent_root_owners_have_no_count_quota_and_release_exactly() {
    let (registry, root) = fixture();
    let entered = Arc::new(Barrier::new(97));
    let release = Arc::new(Barrier::new(97));
    let started = std::time::Instant::now();
    std::thread::scope(|scope| {
        for sequence in 1..=96 {
            let registry = Arc::clone(&registry);
            let entered = Arc::clone(&entered);
            let release = Arc::clone(&release);
            scope.spawn(move || {
                let pin = pin_snapshot(&registry, EntitySnapshot { sequence, root: None }).unwrap();
                entered.wait();
                release.wait();
                drop(pin);
            });
        }
        entered.wait();
        let mut writer = pin_snapshot(&registry, EntitySnapshot { sequence: 97, root: None }).unwrap();
        {
            let state = registry.lock().unwrap();
            assert_eq!(state.active_handles, 98);
            assert_eq!(state.snapshots.len(), 98);
            assert!(state.snapshots.contains_key(&root.snapshot));
        }
        writer.repin(EntitySnapshot { sequence: 98, root: None }).unwrap();
        eprintln!("snapshot ownership: 96 simultaneous roots plus writer/root, {:?}", started.elapsed());
        drop(writer);
        release.wait();
    });
    assert_eq!(registry.lock().unwrap().active_handles, 1);
    drop(root);
    assert!(registry.lock().unwrap().snapshots.is_empty());
}

#[test]
fn escaped_lease_retains_gc_root_until_last_owner_drops() {
    let (registry, root) = fixture();
    let mut lease = root.lease();
    lease.reserve_transaction_bytes(4096).unwrap();
    drop(root);
    assert_eq!(registry.lock().unwrap().active_handles, 1);
    assert_eq!(registry.lock().unwrap().snapshots.len(), 1);
    drop(lease);
    assert_eq!(registry.retained_bytes(), 0);
    assert_eq!(registry.lock().unwrap().active_handles, 0);
    assert!(registry.lock().unwrap().snapshots.is_empty());
}

#[test]
fn poisoned_registry_never_grants_new_root_or_byte_ownership() {
    let (registry, root) = fixture();
    let poisoned = std::panic::catch_unwind(|| {
        let _held = registry.lock().unwrap();
        panic!("injected root registry poison");
    });
    assert!(poisoned.is_err());
    assert_eq!(pin_snapshot(&registry, root.snapshot).unwrap_err().kind(), io::ErrorKind::Other);
    let mut lease = root.lease();
    assert_eq!(lease.reserve_transaction_bytes(1).unwrap_err().kind(), io::ErrorKind::Other);
    assert_eq!(registry.retained_bytes(), 0);
}

#[test]
fn root_and_handle_overflow_do_not_partially_register() {
    let (registry, root) = fixture();
    {
        let mut state = registry.lock().unwrap();
        state.active_handles = usize::MAX;
    }
    assert_eq!(pin_snapshot(&registry, EntitySnapshot { sequence: 1, root: None }).unwrap_err().kind(), io::ErrorKind::InvalidData);
    {
        let mut state = registry.lock().unwrap();
        assert_eq!(state.snapshots.len(), 1);
        state.active_handles = 1;
        *state.snapshots.get_mut(&root.snapshot).unwrap() = usize::MAX;
    }
    assert_eq!(pin_snapshot(&registry, root.snapshot).unwrap_err().kind(), io::ErrorKind::InvalidData);
    {
        let mut state = registry.lock().unwrap();
        assert_eq!(state.active_handles, 1);
        assert_eq!(state.snapshots[&root.snapshot], usize::MAX);
        *state.snapshots.get_mut(&root.snapshot).unwrap() = 1;
    }
    drop(root);
    assert_eq!(registry.lock().unwrap().active_handles, 0);
}
