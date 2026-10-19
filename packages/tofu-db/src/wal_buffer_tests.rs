//! Real deterministic writes verify enqueue isolation, gaps, errors and close.
use super::*;
use crate::vfs::{DeterministicVfs, FaultAction, Operation};

fn log(capacity: usize) -> (Arc<DeterministicVfs>, ActiveLog, WalBarrier) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut log = ActiveLog::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let barrier = WalBarrier::default();
    barrier.state.lock().unwrap().writes.capacity = capacity;
    log.attach_barrier(&barrier);
    (vfs, log, barrier)
}

#[test]
fn enqueue_does_no_write_and_sync_cannot_credit_unwritten_or_failed_frames() {
    let (vfs, mut log, barrier) = log(65536);
    vfs.arm_operation_fault(
        Operation::Write,
        1,
        FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied),
    )
    .unwrap();
    log.append(1, [0; 32], b"queued bytes").unwrap();
    assert!(!vfs.operation_fault_fired().unwrap());
    assert_eq!(barrier.synced_len(), 0);
    assert_eq!(barrier.write_buffer_metrics()["queuedFrames"], 1);
    assert_eq!(
        barrier.sync_for_test(vfs.as_ref()).unwrap_err().kind(),
        io::ErrorKind::PermissionDenied
    );
    assert!(vfs.operation_fault_fired().unwrap());
    assert_eq!(barrier.synced_len(), 0);
    assert_eq!(barrier.write_buffer_metrics()["retainedFrameBytes"], 0);
    assert!(log.append(2, [0; 32], b"must refuse").is_err());
    assert!(barrier
        .wait_synced(
            log.len(),
            std::time::Instant::now() + Duration::from_secs(1)
        )
        .is_err());
}

#[test]
fn buffered_runs_and_large_synchronous_gaps_preserve_exact_framing_after_power_loss() {
    let (vfs, mut log, barrier) = log(1024);
    let first = b"first small".to_vec();
    let large = vec![17; 8192];
    let third = b"third small".to_vec();
    let hash = log.append(1, [0; 32], &first).unwrap();
    let hash = log.append(2, hash, &large).unwrap();
    log.append(3, hash, &third).unwrap();
    assert_eq!(barrier.write_buffer_metrics()["queuedFrames"], 2);
    assert_eq!(barrier.write_buffer_metrics()["synchronousFallbacks"], 1);
    barrier.sync_for_test(vfs.as_ref()).unwrap();
    assert_eq!(barrier.write_buffer_metrics()["writtenBatches"], 2);
    assert_eq!(barrier.write_buffer_metrics()["retainedFrameBytes"], 0);
    log.detach_barrier();
    drop(log);
    vfs.crash().unwrap();
    let mut recovered = ActiveLog::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let rows = recovered.recover_from(0, [0; 32], 3).unwrap().records;
    assert_eq!(
        rows.iter().map(|r| r.payload.clone()).collect::<Vec<_>>(),
        vec![first, large, third]
    );
}

#[test]
fn real_driver_close_drains_owned_frames_and_fsyncs_their_actual_prefix() {
    let (vfs, mut log, _) = log(0);
    let (barrier, driver) = WalBarrierDriver::start_with_write_buffer(
        vfs.clone(),
        log.path().to_path_buf(),
        Duration::from_millis(1),
        65536,
    )
    .unwrap();
    log.attach_barrier(&barrier);
    let mut parent = [0; 32];
    let mut expected = Vec::new();
    for n in 1..=100 {
        let bytes = format!("frame-{n}").into_bytes();
        parent = log.append(n, parent, &bytes).unwrap();
        expected.push(bytes);
    }
    driver.stop_and_join().unwrap();
    assert_eq!(barrier.synced_len(), log.len());
    assert_eq!(barrier.write_buffer_metrics()["writtenFrames"], 100);
    assert_eq!(barrier.write_buffer_metrics()["retainedFrameBytes"], 0);
    log.detach_barrier();
    drop(log);
    vfs.crash().unwrap();
    let mut recovered = ActiveLog::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    assert_eq!(
        recovered
            .recover_from(0, [0; 32], 100)
            .unwrap()
            .records
            .iter()
            .map(|r| r.payload.clone())
            .collect::<Vec<_>>(),
        expected
    );
}

#[test]
fn a_generation_cannot_retire_unwritten_owned_frames() {
    let (_, mut log, barrier) = log(65536);
    log.append(1, [0; 32], b"pending").unwrap();
    barrier.note_rotated(PathBuf::from("/data/foreign-generation"));
    assert!(barrier.barrier_error().is_some());
    assert!(barrier
        .wait_synced(
            log.len(),
            std::time::Instant::now() + Duration::from_secs(1)
        )
        .is_err());
}

#[test]
fn attached_recovery_drains_real_worker_before_reading_its_logical_prefix() {
    let (vfs, mut log, _) = log(0);
    let (barrier, driver) = WalBarrierDriver::start_with_write_buffer(
        vfs.clone(), log.path().to_path_buf(), Duration::from_millis(1), 65536).unwrap();
    log.attach_barrier(&barrier);
    log.append(1, [0; 32], b"recover actual queued source").unwrap();
    assert_eq!(log.recover_from(0, [0; 32], 1).unwrap().records[0].payload,
        b"recover actual queued source");
    assert_eq!(barrier.synced_len(), log.len());
    driver.stop_and_join().unwrap();
}

#[test]
fn failed_sync_never_credits_successfully_written_buffered_frames() {
    let (vfs, mut log, barrier) = log(65536);
    log.append(1, [0; 32], b"not acknowledged").unwrap();
    vfs.arm_operation_fault(Operation::SyncData, 1,
        FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied)).unwrap();
    assert!(barrier.sync_for_test(vfs.as_ref()).is_err());
    assert_eq!(barrier.synced_len(), 0);
    assert_eq!(barrier.write_buffer_metrics()["writtenFrames"], 1);
    assert_eq!(barrier.write_buffer_metrics()["retainedFrameBytes"], 0);
    // Retaining the failed barrier prevents an inline retry on another handle.
    log.detach_barrier();
    assert!(log.sync_pending().is_err());
    assert!(log.append(2, [0; 32], b"must refuse").is_err());
}

#[test]
fn encrypted_authority_keeps_original_writes_and_never_queues_plaintext() {
    let directory = tempfile::tempdir().unwrap();
    let authority = crate::authority::AuthorityDatabase::initialize_with_key(directory.path(),
        Some(crate::at_rest::AtRestKey::from_key([93; 32]))).unwrap();
    assert!(!authority.wal_vfs().unwrap().supports_buffered_wal_writes());
    let (barrier, driver) = WalBarrierDriver::start_with_write_buffer(
        authority.wal_vfs().unwrap(), authority.wal_path().unwrap(),
        Duration::from_millis(1), 65536).unwrap();
    assert_eq!(barrier.write_buffer_metrics()["enabled"], false);
    driver.stop_and_join().unwrap();
}

#[test]
fn normal_durability_keeps_physical_append_before_return() {
    let (vfs, mut log, barrier) = log(65536);
    log.set_durability(WalDurability::Normal);
    vfs.arm_operation_fault(Operation::Write, 1,
        FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied)).unwrap();
    assert_eq!(log.append(1, [0; 32], b"ordinary normal append").unwrap_err().kind(),
        io::ErrorKind::PermissionDenied);
    assert_eq!(barrier.write_buffer_metrics()["queuedFrames"], 0);
}

#[test]
fn poisoned_buffer_state_refuses_detach_read_truncate_and_inline_fallback() {
    let (vfs, mut log, barrier) = log(65536);
    log.append(1, [0; 32], b"owned but physically unwritten").unwrap();
    let poisoned = barrier.clone();
    assert!(std::thread::spawn(move || {
        let _guard = poisoned.state.lock().unwrap();
        panic!("test: poison state with a retained unwritten frame");
    }).join().is_err());
    vfs.arm_operation_fault(Operation::Write, 1,
        FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied)).unwrap();
    log.detach_barrier();
    assert!(log.barrier.is_some(), "unknown buffer must keep its failed barrier");
    assert!(log.sync_pending().is_err());
    assert!(log.recover_from(0, [0; 32], 1).is_err());
    assert!(log.truncate(0).is_err());
    assert!(log.append(2, [0; 32], b"must refuse").is_err());
    assert!(!vfs.operation_fault_fired().unwrap(), "no inline physical retry");
}

#[test]
fn poisoned_driver_close_joins_worker_and_reports_unknown_drain_as_failure() {
    let (vfs, mut log, _) = log(0);
    let (barrier, driver) = WalBarrierDriver::start_with_write_buffer(
        vfs, log.path().to_path_buf(), Duration::from_millis(1), 65536).unwrap();
    log.attach_barrier(&barrier);
    log.append(1, [0; 32], b"close cannot certify unknown state").unwrap();
    let poisoned = barrier.clone();
    assert!(std::thread::spawn(move || {
        let _guard = poisoned.state.lock().unwrap();
        panic!("test: poison a real driver's buffer state");
    }).join().is_err());
    assert!(driver.stop_and_join().is_err());
    assert!(driver.worker.lock().unwrap().is_none(), "worker joined on failure");
    assert!(log.sync_pending().is_err());
}

#[test]
fn publication_cover_keeps_its_captured_prefix_when_later_frames_are_queued() {
    let (vfs, mut log, barrier) = log(65536);
    let hash = log.append(1, [0; 32], b"captured prefix").unwrap();
    let cover = log.publication_cover(vfs.clone()).unwrap().unwrap();
    barrier.sync_for_test(vfs.as_ref()).unwrap();
    log.append(2, hash, b"later unconfirmed suffix").unwrap();
    cover.sync().unwrap();
    assert!(barrier.write_buffer_metrics()["retainedFrameBytes"].as_u64().unwrap() > 0);
    std::mem::forget(log);
    vfs.crash().unwrap();
    let mut reopened = ActiveLog::open_with_vfs(Path::new("/data"), vfs).unwrap();
    let rows = reopened.recover_from(0, [0; 32], 1).unwrap().records;
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].payload, b"captured prefix");
}

#[test]
fn publication_cover_refuses_failed_worker_sync_without_a_physical_retry() {
    let (vfs, mut log, barrier) = log(65536);
    log.append(1, [0; 32], b"physically written but sync failed").unwrap();
    let cover = log.publication_cover(vfs.clone()).unwrap().unwrap();
    vfs.arm_operation_fault(Operation::SyncData, 1,
        FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied)).unwrap();
    assert!(barrier.sync_for_test(vfs.as_ref()).is_err());
    vfs.arm_operation_fault(Operation::SyncData, 1,
        FaultAction::ErrorBefore(io::ErrorKind::Other)).unwrap();
    assert!(cover.sync().is_err());
    assert!(!vfs.operation_fault_fired().unwrap(), "known failure cannot be retried on a second fd");
}

#[test]
fn publication_cover_preserves_normal_mode_bytes_after_one_silently_lost_sync() {
    let (vfs, mut log, _) = log(0);
    log.detach_barrier();
    log.set_durability(WalDurability::Normal);
    log.append(1, [0; 32], b"normal captured physical bytes").unwrap();
    let cover = log.publication_cover(vfs.clone()).unwrap().unwrap();
    vfs.arm_operation_fault(Operation::SyncData, 1, FaultAction::DropSync).unwrap();
    cover.sync().unwrap();
    assert!(vfs.operation_fault_fired().unwrap());
    drop(log);
    vfs.crash().unwrap();
    let mut reopened = ActiveLog::open_with_vfs(Path::new("/data"), vfs).unwrap();
    assert_eq!(reopened.recover_from(0, [0; 32], 1).unwrap().records[0].payload,
        b"normal captured physical bytes");
}
