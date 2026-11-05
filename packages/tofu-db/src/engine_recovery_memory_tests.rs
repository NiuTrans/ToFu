// Recovery ownership regression: measure requested Rust bytes in a fresh
// process through the existing optional allocator diagnostics, then verify
// every recovered transaction. This is not an RSS or admission-limit test.

#[cfg(feature = "allocator-diagnostics")]
#[test]
fn recovery_buffer_memory_child() {
    let Some(directory) = std::env::var_os("TOFUDB_RECOVERY_MEMORY_CHILD") else { return; };
    let engine = Engine::open(Path::new(&directory)).unwrap();
    let peak = crate::EMBEDDED_ALLOCATOR.peak_bytes();
    let retained = crate::EMBEDDED_ALLOCATOR.current_bytes();
    let encoded_bytes: usize = engine.committed_transactions().iter()
        .map(|transaction| transaction.encoded_envelope.len()).sum();
    assert_eq!(engine.state.durable_sequence, 256);
    assert_eq!(engine.committed_transactions().len(), 256);
    for index in 0..256_u64 {
        let transaction = engine.transaction_at(index + 1).unwrap().unwrap();
        assert_eq!(transaction.sequence, index + 1);
        assert_eq!(transaction.envelope.inline_payload, vec![index as u8; 128 * 1024]);
        let decoded = TransactionEnvelope::decode_skipping_carried(&transaction.encoded_envelope).unwrap();
        assert_eq!(decoded, transaction.envelope);
    }
    println!("recovery_buffer_memory={}", serde_json::json!({
        "encodedBytes": encoded_bytes, "peakRequestedBytes": peak,
        "retainedRequestedBytes": retained, "recoveredTransactions": 256,
    }));
}

#[cfg(feature = "allocator-diagnostics")]
#[test]
fn recovery_buffer_memory_does_not_retain_a_third_copy_of_inline_backlog() {
    struct ChildGuard(Option<std::process::Child>);
    impl Drop for ChildGuard {
        fn drop(&mut self) {
            if let Some(child) = self.0.as_mut() { let _ = child.kill(); let _ = child.wait(); }
        }
    }
    let directory = tempfile::tempdir().unwrap();
    let mut engine = Engine::initialize(directory.path()).unwrap();
    engine.attach_rotation_driver();
    for index in 0..256_u16 {
        engine.commit(&vec![index as u8; 128 * 1024]).unwrap();
    }
    drop(engine);
    // Seeding and other suite workers cannot contaminate a fresh child's
    // allocator peak. No global counter is reset under concurrent allocation.
    // A file also preserves a large assertion failure without filling a pipe
    // while the parent waits for exit. It is outside the disposable authority.
    let log = tempfile::NamedTempFile::new().unwrap();
    let mut child = ChildGuard(Some(std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "engine::tests::recovery_buffer_memory_child", "--nocapture"])
        .env("TOFUDB_RECOVERY_MEMORY_CHILD", directory.path())
        .stdout(log.as_file().try_clone().unwrap()).stderr(log.as_file().try_clone().unwrap())
        .spawn().unwrap()));
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(120);
    while child.0.as_mut().unwrap().try_wait().unwrap().is_none() {
        assert!(std::time::Instant::now() < deadline, "recovery child timed out");
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    let status = child.0.take().unwrap().wait().unwrap();
    let stdout = std::fs::read_to_string(log.path()).unwrap();
    assert!(status.success(), "recovery child failed: {stdout}");
    let report: serde_json::Value = stdout.lines().find_map(|line|
        line.strip_prefix("recovery_buffer_memory=").map(|json| serde_json::from_str(json).unwrap()))
        .expect("missing completed recovery measurement");
    eprintln!("recovery_buffer_memory={report}");
    let encoded = report["encodedBytes"].as_u64().unwrap();
    assert!(encoded >= 32 * 1024 * 1024);
    assert_eq!(report["recoveredTransactions"], 256);
    // Decoded inline content and its exact logged representation remain
    // necessary. Allow frame scratch/metadata, but not another full backlog.
    assert!(report["peakRequestedBytes"].as_u64().unwrap() < encoded * 5 / 2 + 8 * 1024 * 1024,
        "recovery retained a redundant full-backlog copy: {report}");
}

#[test]
fn recovery_keeps_exact_valid_prefix_when_a_later_reference_is_missing() {
    for durable_tail in [false, true] {
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        for byte in [41, 42] { engine.commit(&vec![byte; 64 * 1024]).unwrap(); }
        let expected = engine.committed_transactions().to_vec();
        let parent_hash = expected.last().unwrap().record_hash;
        drop(engine);
        let wal_path = directory.path().join("active.wal");
        let prefix_bytes = std::fs::read(&wal_path).unwrap();
        let invalid_envelope = TransactionEnvelope {
            block_ids: vec![BlockId::for_payload(b"missing suffix source")],
            inline_payload: b"invalid suffix".to_vec(),
            authority_state_update: None,
        }.encode().unwrap();
        let tail_hash = {
            let mut wal = ActiveLog::open(directory.path()).unwrap();
            wal.append(3, parent_hash, &invalid_envelope).unwrap()
        };
        if durable_tail {
            let mut control = ControlFile::open(directory.path()).unwrap();
            let mut state = control.read_current().unwrap();
            state.generation += 1;
            state.durable_sequence = 3;
            state.root_hash = tail_hash;
            control.publish(&state).unwrap();
        }
        let control_path = directory.path().join("CONTROL");
        let control_before = std::fs::read(&control_path).unwrap();
        let wal_before = std::fs::read(&wal_path).unwrap();
        if durable_tail {
            assert_eq!(Engine::open(directory.path()).err().unwrap().kind(), io::ErrorKind::InvalidData);
            assert_eq!(std::fs::read(control_path).unwrap(), control_before);
            assert_eq!(std::fs::read(wal_path).unwrap(), wal_before);
        } else {
            let mut reopened = Engine::open(directory.path()).unwrap();
            assert_eq!(reopened.state.durable_sequence, 2);
            assert_eq!(reopened.committed_transactions(), expected.as_slice());
            assert_eq!(std::fs::read(wal_path).unwrap(), prefix_bytes);
            reopened.checkpoint().unwrap();
            drop(reopened);
            let reopened = Engine::open(directory.path()).unwrap();
            assert_eq!(reopened.transaction_snapshot().unwrap(), expected);
        }
    }
}
