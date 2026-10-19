//! Complete-payload publication avoids cold optional dedup reader I/O.
//! Generic reference validation and exact WAL recovery retain their fences.
use super::*;
use crate::vfs::{DeterministicVfs, FaultAction, Operation};

#[test]
fn cold_history_blob_publication_carries_exact_bytes_while_generic_source_errors_survive() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    let path = Path::new("/data");
    vfs.create_dir(path).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut engine = Engine::initialize_with_vfs(path, vfs.clone()).unwrap();
    let payloads = [vec![11; 8192], vec![23; 8192], vec![37; 8192]];
    engine
        .commit_transaction(
            b"seed",
            &payloads.iter().map(Vec::as_slice).collect::<Vec<_>>(),
        )
        .unwrap();
    engine.attach_rotation_driver();
    let snapshot = match engine.try_prepare_rotation_checkpoint().unwrap() {
        RotationCheckpointPreparation::Ready(snapshot) => snapshot,
        other => panic!("expected indexed rotation: {other:?}"),
    };
    let outcome = encode_rotation_segments(&snapshot);
    engine
        .finish_rotation_checkpoint(snapshot, outcome)
        .unwrap();
    engine.blocks.flush_pending().unwrap();
    engine.blocks.set_cache_capacity_bytes(0).unwrap();
    let manifest = engine.history_manifest.as_ref().unwrap().clone();
    let root = crate::history_payload::RoutingRoot::decode(
        &engine
            .read_block(manifest.payload_index_root_id.unwrap())
            .unwrap(),
    )
    .unwrap();
    engine
        .blocks
        .install_history_payload_catalog(&manifest, Some(root))
        .unwrap();

    vfs.arm_operation_fault(
        Operation::Read,
        1,
        FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied),
    )
    .unwrap();
    let first = engine.write_blob_block(&payloads[0]).unwrap();
    let captured =
        crate::block::PreparedBlockSource::capture_shared(Arc::new(payloads[1].clone())).unwrap();
    let second = engine.write_prepared_blob_block(&captured).unwrap();
    assert!(
        !vfs.operation_fault_fired().unwrap(),
        "complete Blob bytes must not require cold history reads"
    );
    assert!(engine.blocks.pending_payload(first).unwrap().is_some());
    assert!(engine.blocks.pending_payload(second).unwrap().is_some());
    assert_eq!(
        engine.write_block(&payloads[2]).unwrap_err().kind(),
        io::ErrorKind::PermissionDenied
    );
    assert!(
        vfs.operation_fault_fired().unwrap(),
        "generic source validation must still observe real I/O failure"
    );

    vfs.arm_fault(None).unwrap();
    engine
        .commit_references(b"restaged", &[first, second])
        .unwrap();
    drop(engine);
    vfs.crash().unwrap();
    let recovered = Engine::open_with_vfs(path, vfs.clone()).unwrap();
    assert_eq!(recovered.read_block(first).unwrap(), payloads[0]);
    assert_eq!(recovered.read_block(second).unwrap(), payloads[1]);
}
