//! Fresh stream staging does no old-source probing and retains WAL errors.
use super::*;
use crate::vfs::{DeterministicVfs, FaultAction, Operation, Vfs};

fn database() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let db = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    (vfs, db)
}

#[test]
fn a_new_stream_segment_never_probes_old_files_before_its_wal_ack() {
    for operation in [Operation::Read, Operation::Metadata] {
        let (vfs, mut db) = database();
        let key = StreamKey::new(7, 11, "events", b"one").unwrap();
        let expected = vec![StreamEvent::new(1, "delta", vec![3; 8192]).unwrap()];
        let mut tx = db.begin(7, 11).unwrap();
        db.stream_append(&mut tx, key.clone(), 1, expected.clone())
            .unwrap();
        vfs.arm_operation_fault(
            operation,
            1,
            FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied),
        )
        .unwrap();
        db.commit(tx).unwrap();
        assert!(
            !vfs.operation_fault_fired().unwrap(),
            "new segments should use fresh staging: {operation:?}"
        );
        assert!(!vfs.trace().unwrap().contains(&operation));
        vfs.arm_fault(None).unwrap();
        drop(db);
        vfs.crash().unwrap();
        let recovered = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        assert_eq!(recovered.stream_next_sequence(7, 11, &key).unwrap(), 2);
        assert_eq!(
            recovered.stream_read(7, 11, &key, 1, 10).unwrap().events[0].event,
            expected[0]
        );
    }
}

#[test]
fn fresh_stream_staging_keeps_failed_wal_writes_unacknowledged_and_requires_reopen() {
    let (vfs, mut db) = database();
    let key = StreamKey::new(7, 11, "events", b"one").unwrap();
    let mut tx = db.begin(7, 11).unwrap();
    db.stream_append(
        &mut tx,
        key.clone(),
        1,
        vec![StreamEvent::new(1, "delta", vec![3; 8192]).unwrap()],
    )
    .unwrap();
    vfs.arm_operation_fault(
        Operation::Write,
        1,
        FaultAction::ErrorBefore(io::ErrorKind::Other),
    )
    .unwrap();
    assert!(db.commit(tx).is_err());
    assert!(vfs.operation_fault_fired().unwrap());
    vfs.arm_fault(None).unwrap();
    let next = db.begin(7, 11);
    assert!(
        next.is_err(),
        "ambiguous WAL writes must poison the live authority"
    );
    drop(db);
    vfs.crash().unwrap();
    let recovered = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    assert_eq!(recovered.stream_next_sequence(7, 11, &key).unwrap(), 1);
}
