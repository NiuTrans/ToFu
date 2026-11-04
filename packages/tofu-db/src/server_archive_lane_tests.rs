// Executable specifications for raw-archive puts and log-aggregate flushes on
// the certified command lane: cold reads run off the writer, the commit is
// ordinary OCC, and every refusal retries exactly on the writer.
// Included in server::tests to reuse its session and dispatch fixtures.
mod archive_lane {
    use super::*;
    use crate::vfs::{FaultAction, FaultRule, Operation};
    use std::path::Path;

    const NOW: u64 = 2_000;

    struct Parent {
        conversation: &'static str,
        task: &'static str,
        attempt: String,
        turn: String,
    }

    /// Stored zlib blocks: archive bytes are opaque to the engine, and debug
    /// builds would otherwise spend the fault matrix compressing noise.
    fn zlib_base64(raw: &[u8]) -> String {
        use base64::Engine;
        use std::io::Write;
        let mut encoder =
            flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::none());
        encoder.write_all(raw).unwrap();
        base64::engine::general_purpose::STANDARD.encode(encoder.finish().unwrap())
    }

    fn seed_parent(database: &mut AuthorityDatabase, session: &StorageV2Session,
        conversation: &'static str, task: &'static str) -> Parent {
        let pair = dispatched(database, session, &retention_command("turn.create_pair", json!({
            "conversation_id": conversation, "user_id": 11, "command_id": format!("{conversation}-pair"),
            "input_projection": {"content": "capture"}, "conversation_defaults": {"allowCreate": true},
            "now": 1_000}), &format!("{conversation}-pair-receipt")), 1_000);
        let pair = pair.get("value").unwrap_or(&pair);
        let identity = |pointer: &str| pair.pointer(pointer).and_then(Value::as_str)
            .unwrap_or_else(|| panic!("create_pair response: {pair}")).to_owned();
        let (attempt, turn) = (identity("/attempt/attemptId"), identity("/turn/turnId"));
        for (operation, payload) in [
            ("turn.attempt.claim", json!({"attempt_id": attempt, "dispatch_owner_id": "test", "user_id": 11})),
            ("turn.attempt.bind", json!({"attempt_id": attempt, "task_id": task,
                "dispatch_owner_id": "test", "user_id": 11})),
            ("turn.attempt.start", json!({"attempt_id": attempt, "task_id": task, "user_id": 11})),
        ] {
            dispatched(database, session,
                &retention_command(operation, payload, &format!("{conversation}-{operation}")), 1_001);
        }
        Parent { conversation, task, attempt, turn }
    }

    /// Deterministic incompressible bytes.
    fn noise(bytes: usize) -> Vec<u8> {
        (0_u64..).flat_map(|block| Sha256::digest(block.to_le_bytes())).take(bytes).collect()
    }

    fn archive_put(parent: &Parent, archive: &str, round: u64, budget_bytes: u64, command_id: &str) -> Request {
        archive_put_with_response(parent, archive, round, budget_bytes, command_id,
            format!("data: response {archive}\n\n").into_bytes())
    }

    fn archive_put_with_response(parent: &Parent, archive: &str, round: u64, budget_bytes: u64,
        command_id: &str, response_body: Vec<u8>) -> Request {
        let request_body = format!("provider request {archive}").into_bytes();
        retention_command("raw_archive.put", json!({
            "archive_id": archive, "conversation_id": parent.conversation, "turn_id": parent.turn,
            "attempt_id": parent.attempt, "task_id": parent.task, "user_id": 11, "round_num": round,
            "transport_attempt": 0,
            "request_blob_b64": zlib_base64(&request_body), "response_blob_b64": zlib_base64(&response_body),
            "request_bytes": request_body.len(), "response_bytes": response_body.len(),
            "request_sha256": format!("{:x}", Sha256::digest(&request_body)),
            "response_sha256": format!("{:x}", Sha256::digest(&response_body)),
            "integrity": "complete", "truncation_reason": "", "summary": {"text": "Provider exchange"},
            "budget_bytes": budget_bytes, "min_free_bytes": 0, "available_free_bytes": 0}), command_id)
    }

    fn stored_bytes(put: &Request) -> u64 {
        use base64::Engine;
        let payload: Value = serde_json::from_slice(&put.payload).unwrap();
        ["request_blob_b64", "response_blob_b64"].iter().map(|field| base64::engine::general_purpose::STANDARD
            .decode(payload[*field].as_str().unwrap()).unwrap().len() as u64).sum()
    }

    fn log_flush(rows: &[(&str, u64)], cutoff_ms: Option<u64>, command_id: &str) -> Request {
        retention_command("log_aggregate.flush", json!({"rows": rows.iter().map(|(fingerprint, last_seen)| json!({
            "fingerprint": fingerprint, "level": "error", "logger": "alpha", "template": "disk full",
            "sample": command_id, "count": 2, "first_seen": 1_000, "last_seen": last_seen}))
            .collect::<Vec<_>>(), "cutoff_ms": cutoff_ms}), command_id)
    }

    fn turn_event(parent: &Parent, label: &str) -> Request {
        retention_command("turn.event.record", json!({"attempt_id": parent.attempt, "task_id": parent.task,
            "user_id": 11, "slim": true, "content": format!("streamed {label}"), "thinking": "running",
            "now": 1_950}), &format!("archive-event-{label}"))
    }

    fn pin_write_path(database: &mut AuthorityDatabase, outbox: bool) {
        database.set_write_overlay_for_test(true);
        database.set_entity_tombstone_reclaim_override_for_test(Some(true));
        if outbox {
            database.configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024).unwrap();
        }
    }

    /// One owner with two active conversations, three retained archives in
    /// the first and two aggregates. Strict commits make every seed durable.
    struct Seed {
        vfs: DeterministicVfs,
        outbox: bool,
        archive: Parent,
        other: Parent,
    }

    fn seed_authority(session: &StorageV2Session, outbox: bool) -> Seed {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        pin_write_path(&mut database, outbox);
        let archive = seed_parent(&mut database, session, "archive-conversation", "archive-task");
        let other = seed_parent(&mut database, session, "other-conversation", "other-task");
        for index in 0..3 {
            let name = format!("old-{index}");
            dispatched(&mut database, session,
                &archive_put(&archive, &name, index + 1, 1 << 20, &format!("put-{name}")), 1_100 + index);
        }
        dispatched(&mut database, session, &log_flush(&[("stale.fingerprint", 1_000),
            ("seed.fingerprint", 1_100)], None, "seed-flush"), 1_100);
        // Pages, not the replayed WAL overlay, hold the seed after reopen.
        database.checkpoint_for_test().unwrap();
        drop(database);
        Seed { vfs: vfs.fork_durable().unwrap(), outbox, archive, other }
    }

    impl Seed {
        /// An independent authority over the seed's durable image: every page
        /// and block starts cold. The first deferred write after open
        /// republishes the root, so prepared range witnesses start after it.
        fn open(&self, session: &StorageV2Session) -> (Arc<DeterministicVfs>, AuthorityDatabase) {
            let vfs = Arc::new(self.vfs.fork_durable().unwrap());
            let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            pin_write_path(&mut database, self.outbox);
            dispatched(&mut database, session, &retention_command("record.put", json!({
                "namespace": "archive-prime", "key": "steady", "value": {"prime": true}}), "archive-prime"), 1_900);
            (vfs, database)
        }

        /// The byte budget holds only the newest seeded archive beside this
        /// one, so the put reclaims the two oldest archives first.
        fn reclaiming_put(&self, command_id: &str) -> Request {
            self.reclaiming_put_with_response(command_id, b"data: response fresh\n\n".to_vec())
        }

        fn reclaiming_put_with_response(&self, command_id: &str, response_body: Vec<u8>) -> Request {
            let put = |budget| archive_put_with_response(&self.archive, "fresh", 4, budget, command_id,
                response_body.clone());
            put(stored_bytes(&archive_put(&self.archive, "old-2", 3, 1, "")) + stored_bytes(&put(1)))
        }
    }

    fn sweeping_flush() -> Request {
        log_flush(&[("seed.fingerprint", 1_300), ("fresh.fingerprint", 1_300)], Some(1_050), "flush-fresh")
    }

    fn state(database: &mut AuthorityDatabase, session: &StorageV2Session) -> Value {
        let mut state = serde_json::Map::new();
        for (name, operation, payload) in [
            ("archiveTask", "raw_archive.list", json!({"user_id": 11, "task_id": "archive-task", "limit": 64})),
            ("otherTask", "raw_archive.list", json!({"user_id": 11, "task_id": "other-task", "limit": 64})),
            ("freshRequest", "raw_archive.read", json!({"user_id": 11, "task_id": "archive-task",
                "archive_id": "fresh", "part": "request"})),
            ("rivalResponse", "raw_archive.read", json!({"user_id": 11, "task_id": "other-task",
                "archive_id": "rival", "part": "response"})),
            ("aggregates", "log_aggregate.query", json!({"sort": "last_seen", "limit": 100})),
            ("foreground", "record.list", json!({"namespace": "archive-foreground", "prefix": "", "limit": 100})),
        ] {
            state.insert(name.to_owned(), dispatched(database, session, &request(operation, 11, payload), NOW + 100));
        }
        Value::Object(state)
    }

    fn archive_ids(listing: &Value) -> Vec<&str> {
        listing["archives"].as_array().unwrap().iter()
            .map(|archive| archive["archiveId"].as_str().unwrap()).collect()
    }

    fn storage_reads(trace: &[Operation]) -> usize {
        trace.iter().filter(|operation| matches!(operation,
            Operation::Open | Operation::Read | Operation::Metadata | Operation::ReadDirectory)).count()
    }

    fn prepare_on_lane(stores: &StorageV2Stores<'_>, session: &StorageV2Session, command: &Request)
        -> crate::transaction_ir::PreparedTransactionIr {
        let admitted = session.admit(command, NOW).unwrap();
        let ParallelPreparationOutcome::Prepared(prepared) =
            prepare_parallel_command(stores, session, admitted, &mut || NOW, &mut || true).unwrap()
            else { panic!("{} must prepare on the snapshot lane", command.operation) };
        prepared
    }

    fn finish_on_lane(stores: &StorageV2Stores<'_>, session: &StorageV2Session, command: &Request,
        prepared: crate::transaction_ir::PreparedTransactionIr) -> Option<DispatchedResponse> {
        let admitted = session.admit(command, NOW).unwrap();
        match finish_parallel_command(stores, session, admitted, prepared, &mut || NOW, &mut || true).unwrap() {
            Some(RequestDispatch::Responded(response)) => Some(response),
            Some(RequestDispatch::ConnectionClosed) => panic!("{} disconnected", command.operation),
            None => None,
        }
    }

    fn writer_retry(stores: &StorageV2Stores<'_>, session: &StorageV2Session, command: &Request) -> DispatchedResponse {
        let RequestDispatch::Responded(response) = dispatch_one_request_inner_mode(
            stores, session, command, false, NOW, &mut || NOW, &mut || true, false).unwrap()
            else { panic!("{} writer retry disconnected", command.operation) };
        response
    }

    #[test]
    fn archive_put_and_log_flush_prepare_off_writer_beside_live_writes_and_match_serial() {
        let session = authenticator().authenticate(&hello()).unwrap();
        for outbox in [false, true] {
            let seed = seed_authority(&session, outbox);
            let (_, mut control) = seed.open(&session);
            let (_, subject) = seed.open(&session);
            let database = Mutex::new(subject);
            let stores = StorageV2Stores::new(&database, None);
            let commands = [
                seed.reclaiming_put("put-fresh"),
                // Archive identity replay under a new command ID.
                archive_put(&seed.archive, "old-2", 3, 1 << 20, "put-old-2-again"),
                // Command receipt replay of the first put.
                seed.reclaiming_put("put-fresh"),
                sweeping_flush(),
            ];
            for (index, command) in commands.iter().enumerate() {
                let live = [
                    retention_command("record.put", json!({"namespace": "archive-foreground",
                        "key": format!("k{index}"), "value": {"v": index}}), &format!("archive-live-{index}")),
                    turn_event(&seed.other, &format!("live-{index}")),
                ];
                let expected = session.dispatch(&mut control, command, NOW);
                assert_eq!(expected.status, 0, "{:?}", expected.error_message);
                for write in &live {
                    dispatched(&mut control, &session, write, NOW);
                }
                let wal_before = database.lock().unwrap().wal_len().unwrap();
                let prepared = prepare_on_lane(&stores, &session, command);
                assert_eq!(database.lock().unwrap().wal_len().unwrap(), wal_before);
                for write in &live {
                    dispatched(&mut database.lock().unwrap(), &session, write, NOW);
                }
                let response = finish_on_lane(&stores, &session, command, prepared).unwrap_or_else(|| panic!(
                    "{} conflicted with unrelated live writes (outbox={outbox})", command.operation));
                assert_eq!(response.status, 0, "{:?}", response.error_message);
                assert_eq!(response.payload, expected.payload, "{} #{index}", command.operation);
                assert_eq!(database.lock().unwrap().snapshot_pin_metrics().unwrap().active_handles, 0);
            }
            let replay: Value = serde_json::from_slice(&session.dispatch(&mut control, &commands[1], NOW).payload).unwrap();
            assert_eq!(replay["idempotentReplay"], true);
            let state = state(&mut database.lock().unwrap(), &session);
            assert_eq!(state, self::state(&mut control, &session));
            assert_eq!(archive_ids(&state["archiveTask"]), ["old-2", "fresh"]);
            assert_eq!(state["aggregates"]["total_rows"], 2);
            assert_eq!(state["aggregates"]["total_events"], 6);
            assert_eq!(state["foreground"].as_array().unwrap().len(), 4);
        }
    }

    #[test]
    fn prepared_archive_put_and_log_flush_refused_by_overlapping_commits_retry_exactly_on_writer() {
        let session = authenticator().authenticate(&hello()).unwrap();
        let seed = seed_authority(&session, true);
        let cases = [
            // The parent proof witnesses the streaming conversation's header and Turn.
            ("parent", seed.reclaiming_put("put-fresh"), turn_event(&seed.archive, "parent")),
            // The identity claim: the retry replays the archive instead of duplicating it.
            ("identity", seed.reclaiming_put("put-fresh"), seed.reclaiming_put("put-fresh-rival")),
            // Owner and tenant usage: the retry reclaims against the current usage.
            ("usage", seed.reclaiming_put("put-fresh"), archive_put(&seed.other, "rival", 1, 1 << 20, "put-rival")),
            // The same aggregate row: the retry merges both counts.
            ("aggregate", sweeping_flush(), log_flush(&[("seed.fingerprint", 1_250)], None, "flush-rival")),
        ];
        for (name, command, rival) in cases {
            let (_, mut control) = seed.open(&session);
            let (_, subject) = seed.open(&session);
            let database = Mutex::new(subject);
            let stores = StorageV2Stores::new(&database, None);
            let prepared = prepare_on_lane(&stores, &session, &command);
            dispatched(&mut database.lock().unwrap(), &session, &rival, NOW);
            let wal_before = database.lock().unwrap().wal_len().unwrap();
            assert!(finish_on_lane(&stores, &session, &command, prepared).is_none(), "{name}: stale preparation published");
            assert_eq!(database.lock().unwrap().wal_len().unwrap(), wal_before, "{name}");
            assert_eq!(database.lock().unwrap().snapshot_pin_metrics().unwrap().active_handles, 0, "{name}");
            let response = writer_retry(&stores, &session, &command);
            dispatched(&mut control, &session, &rival, NOW);
            let expected = session.dispatch(&mut control, &command, NOW);
            assert_eq!(response.status, 0, "{name}: {:?}", response.error_message);
            assert_eq!(response.payload, expected.payload, "{name}");
            let state = state(&mut database.lock().unwrap(), &session);
            assert_eq!(state, self::state(&mut control, &session), "{name}");
            let response: Value = serde_json::from_slice(&response.payload).unwrap();
            match name {
                "parent" => assert_eq!(archive_ids(&state["archiveTask"]), ["old-2", "fresh"]),
                "identity" => {
                    assert_eq!(response["idempotentReplay"], true);
                    assert_eq!(archive_ids(&state["archiveTask"]), ["old-2", "fresh"]);
                }
                "usage" => {
                    assert_eq!(archive_ids(&state["archiveTask"]), ["fresh"]);
                    assert_eq!(archive_ids(&state["otherTask"]), ["rival"]);
                }
                _ => {
                    assert_eq!(response, json!({"flushed": 2, "swept": 1}));
                    assert_eq!(state["aggregates"]["total_events"], 8);
                }
            }
        }
    }

    #[test]
    fn cold_archive_put_and_log_flush_read_storage_only_off_the_writer() {
        let session = authenticator().authenticate(&hello()).unwrap();
        let seed = seed_authority(&session, true);
        for (command, rival) in [
            (seed.reclaiming_put("put-fresh"), turn_event(&seed.archive, "parent")),
            (sweeping_flush(), log_flush(&[("seed.fingerprint", 1_250)], None, "flush-rival")),
        ] {
            // Exclusive lane: every cold read happens while the writer is held.
            let (vfs, mut exclusive) = seed.open(&session);
            vfs.arm_fault(None).unwrap();
            let expected = session.dispatch(&mut exclusive, &command, NOW);
            assert_eq!(expected.status, 0, "{:?}", expected.error_message);
            let exclusive_reads = storage_reads(&vfs.trace().unwrap());

            let (vfs, subject) = seed.open(&session);
            vfs.arm_fault(None).unwrap();
            drop(subject.build_preparation_replica().unwrap());
            let capture_reads = storage_reads(&vfs.trace().unwrap());
            let database = Mutex::new(subject);
            let stores = StorageV2Stores::new(&database, None);
            vfs.arm_fault(None).unwrap();
            let prepared = prepare_on_lane(&stores, &session, &command);
            let prepared_reads = storage_reads(&vfs.trace().unwrap());
            vfs.arm_fault(None).unwrap();
            let response = finish_on_lane(&stores, &session, &command, prepared).expect("unchallenged commit");
            let held = vfs.trace().unwrap();
            assert_eq!(response.status, 0, "{:?}", response.error_message);
            assert_eq!(response.payload, expected.payload);

            // A refused commit still warmed the shared caches for the writer retry.
            let (vfs, subject) = seed.open(&session);
            let database = Mutex::new(subject);
            let stores = StorageV2Stores::new(&database, None);
            let prepared = prepare_on_lane(&stores, &session, &command);
            dispatched(&mut database.lock().unwrap(), &session, &rival, NOW);
            assert!(finish_on_lane(&stores, &session, &command, prepared).is_none());
            vfs.arm_fault(None).unwrap();
            let retried = writer_retry(&stores, &session, &command);
            assert_eq!(retried.status, 0, "{:?}", retried.error_message);
            let retry_reads = storage_reads(&vfs.trace().unwrap());
            eprintln!("{}: exclusive hold storage reads={exclusive_reads}; capture={capture_reads}; \
                off-writer preparation={prepared_reads}; commit hold={} (trace {:?}); writer retry after refusal={retry_reads}",
                command.operation, storage_reads(&held), held);
            assert!(exclusive_reads > 0, "{}: the baseline never read cold storage", command.operation);
            assert_eq!(capture_reads, 0, "{}: snapshot capture read storage", command.operation);
            assert!(prepared_reads >= exclusive_reads, "{}: preparation skipped cold reads", command.operation);
            assert_eq!(storage_reads(&held), 0, "{}: commit hold read storage: {held:?}", command.operation);
            assert!(retry_reads < exclusive_reads, "{}: refused preparation did not warm the retry", command.operation);
        }
    }

    #[test]
    fn blocked_cold_archive_put_preparation_read_lets_a_foreground_write_commit() {
        let session = authenticator().authenticate(&hello()).unwrap();
        let vfs = Arc::new(crate::test_stop_vfs::StopVfs::new());
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let put = {
            let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            pin_write_path(&mut database, false);
            let parent = seed_parent(&mut database, &session, "archive-conversation", "archive-task");
            dispatched(&mut database, &session, &archive_put(&parent, "old-0", 1, 1 << 20, "put-old-0"), 1_100);
            database.checkpoint_for_test().unwrap();
            archive_put(&parent, "fresh", 2, 1 << 20, "put-fresh")
        };
        vfs.inner.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        pin_write_path(&mut database, false);
        dispatched(&mut database, &session, &retention_command("record.put", json!({
            "namespace": "archive-prime", "key": "steady", "value": {"prime": true}}), "archive-prime"), 1_900);
        let database = Mutex::new(database);
        let stores = StorageV2Stores::new(&database, None);
        let foreground = retention_command("record.put", json!({"namespace": "archive-foreground",
            "key": "during", "value": {"v": 1}}), "archive-foreground-during");
        let (entered, release) = vfs.block_next_read();
        let (entered, committed, outcome) = std::thread::scope(|threads| {
            let worker = threads.spawn(|| dispatch_one_request(
                &stores, &session, &put, false, NOW, &mut || NOW, &mut || true));
            let entered = entered.recv_timeout(Duration::from_secs(5));
            // Release before asserting: a failed assertion must not strand the worker.
            let committed = entered.is_ok() && match database.try_lock() {
                Ok(mut authority) => session.dispatch(&mut authority, &foreground, NOW).status == 0,
                Err(_) => false,
            };
            let _ = release.send(());
            (entered, committed, worker.join().unwrap())
        });
        assert!(entered.is_ok(), "the cold put never read storage");
        assert!(committed, "archive preparation read storage while holding the writer");
        let RequestDispatch::Responded(response) = outcome.unwrap() else { panic!("put disconnected") };
        assert_eq!(response.status, 0, "{:?}", response.error_message);
        let mut authority = database.lock().unwrap();
        let listing = dispatched(&mut authority, &session,
            &request("raw_archive.list", 11, json!({"user_id": 11, "task_id": "archive-task", "limit": 64})), NOW);
        assert_eq!(archive_ids(&listing), ["old-0", "fresh"]);
        assert_eq!(dispatched(&mut authority, &session, &request("record.list", 11,
            json!({"namespace": "archive-foreground", "prefix": "", "limit": 10})), NOW)
            .as_array().unwrap().len(), 1);
    }

    #[test]
    fn prepared_archive_put_and_log_flush_commit_faults_recover_all_or_nothing() {
        let session = authenticator().authenticate(&hello()).unwrap();
        let seed = seed_authority(&session, true);
        // The response tail inflates every stored chunk of the archive body.
        let observe = |database: &mut AuthorityDatabase, tail_offset: u64| {
            let mut observed = state(database, &session);
            observed["freshResponseTail"] = dispatched(database, &session, &request("raw_archive.read", 11,
                json!({"user_id": 11, "task_id": "archive-task", "archive_id": "fresh", "part": "response",
                    "offset": tail_offset, "limit": 64})), NOW + 100);
            observed
        };
        let run = |command: &Request, fault: Option<FaultRule>| {
            let (vfs, subject) = seed.open(&session);
            let database = Mutex::new(subject);
            let stores = StorageV2Stores::new(&database, None);
            let prepared = prepare_on_lane(&stores, &session, command);
            vfs.arm_fault(fault).unwrap();
            let admitted = session.admit(command, NOW).unwrap();
            let acknowledged = matches!(finish_parallel_command(&stores, &session, admitted, prepared,
                &mut || NOW, &mut || true), Ok(Some(RequestDispatch::Responded(ref response))) if response.status == 0);
            (acknowledged, vfs.trace().unwrap(), vfs.fork_durable().unwrap())
        };
        let mut cells = 0;
        // Three document chunks whose staged bytes just exceed the carried bound.
        let (carried, spilled) = (noise(24 * 1024), noise(2_300_000));
        for (command, tail_offset, spills) in [
            // The archive document is a private blob carried in the commit's WAL frame.
            (seed.reclaiming_put_with_response("put-fresh", carried.clone()), carried.len() as u64 - 64, false),
            // Beyond the carried bound the commit publishes the blob's blocks before the frame.
            (seed.reclaiming_put_with_response("put-fresh", spilled.clone()), spilled.len() as u64 - 64, true),
            (sweeping_flush(), 0, false),
        ] {
            let before = observe(&mut seed.open(&session).1, tail_offset);
            let (_, mut control) = seed.open(&session);
            let expected = session.dispatch(&mut control, &command, NOW);
            assert_eq!(expected.status, 0, "{:?}", expected.error_message);
            let after = observe(&mut control, tail_offset);
            assert_ne!(after, before);
            // Neither operation stores a command receipt: an archive put retry is
            // answered by its identity claim, a flush retry merges its rows again.
            let replay = (command.operation == "raw_archive.put").then(|| session.dispatch(&mut control, &command, NOW));
            if let Some(replay) = &replay {
                let replay: Value = serde_json::from_slice(&replay.payload).unwrap();
                assert_eq!(replay["idempotentReplay"], true);
            }
            let (acknowledged, trace, _) = run(&command, None);
            assert!(acknowledged, "{} did not commit on the lane", command.operation);
            eprintln!("{} (response tail at {tail_offset}): prepared commit trace {trace:?}", command.operation);
            assert_eq!(trace.contains(&Operation::Rename), spills, "{}: loose block publication", command.operation);
            for (index, operation) in trace.iter().enumerate() {
                let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
                match operation {
                    Operation::Write => actions.push(FaultAction::ShortWrite(7)),
                    Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory =>
                        actions.push(FaultAction::DropSync),
                    _ => {}
                }
                for action in actions {
                    let cell = format!("{} {operation:?}#{index} {action:?}", command.operation);
                    let (acknowledged, _, durable) = run(&command, Some(FaultRule {
                        operation_number: index as u64 + 1, action }));
                    let mut reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), Arc::new(durable))
                        .unwrap_or_else(|error| panic!("{cell}: reopen failed: {error}"));
                    pin_write_path(&mut reopened, true);
                    let recovered = observe(&mut reopened, tail_offset);
                    if acknowledged && action != FaultAction::DropSync {
                        assert_eq!(recovered, after, "{cell}: acknowledged commit lost");
                    } else {
                        assert!(recovered == before || recovered == after, "{cell}: torn recovery {recovered}");
                    }
                    let retry_answer = match (recovered == before, &replay) {
                        (true, _) => Some(&expected),
                        (false, Some(replay)) => Some(replay),
                        (false, None) => None,
                    };
                    if let Some(answer) = retry_answer {
                        let retried = session.dispatch(&mut reopened, &command, NOW);
                        assert_eq!(retried.status, 0, "{cell}: {:?}", retried.error_message);
                        assert_eq!(retried.payload, answer.payload, "{cell}");
                        assert_eq!(observe(&mut reopened, tail_offset), after, "{cell}");
                    }
                    cells += 1;
                }
            }
        }
        eprintln!("prepared archive/flush commit fault cells: {cells}");
        assert!(cells > 0);
    }

    /// Runs `work` as one attributed hold on this thread, as
    /// `AuthorityHoldTimer` does, returning its milliseconds and phase split.
    fn attributed<T>(work: impl FnOnce() -> T) -> (T, f64, Value) {
        let token = crate::request_profile::begin_hold().expect("no enclosing attributed hold");
        let started = std::time::Instant::now();
        let value = work();
        let nanos = started.elapsed().as_nanos() as u64;
        let report = crate::request_profile::finish_hold(token, nanos, false);
        let phases = crate::authority_watch::HOLD_PHASE_NAMES.iter().zip(report.phases)
            .filter(|(_, nanos)| *nanos != 0)
            .map(|(name, nanos)| ((*name).to_owned(), json!((nanos as f64 / 1e4).round() / 100.0)))
            .collect::<serde_json::Map<_, _>>();
        (value, nanos as f64 / 1e6, Value::Object(phases))
    }

    /// Paired hold measurement on the volume holding `std::env::temp_dir()`
    /// (point TMPDIR at a mount to measure it). Each arm reopens the authority
    /// with cold in-process caches and runs the same kind of reclaiming put
    /// and flush, either exclusively under the writer (the path before the
    /// lane admitted them) or on the command lane; the arm order alternates.
    /// Network volumes launch with Normal durability, so neither hold fsyncs.
    #[test]
    #[ignore = "explicit paired cold-volume hold measurement; writes only disposable authorities"]
    fn archive_put_and_log_flush_paired_hold_measurement() {
        const ARCHIVES: u64 = 48;
        const AGGREGATES: usize = 1_500;
        const FLUSH_ROWS: usize = 200;
        const PAIRS: u64 = 6;
        let session = authenticator().authenticate(&hello()).unwrap();
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("authority");
        let open = |initialize: bool| {
            let mut database = if initialize { AuthorityDatabase::initialize(&path) }
                else { AuthorityDatabase::open(&path) }.unwrap();
            pin_write_path(&mut database, false);
            database.set_wal_durability(crate::wal::WalDurability::Normal).unwrap();
            database
        };
        let body = |seed: u64| -> Vec<u8> { (0_u64..)
            .flat_map(|block| Sha256::digest([seed.to_le_bytes(), block.to_le_bytes()].concat()))
            .take(32 * 1024).collect() };
        let fingerprint = |index: usize| format!("fp-{index:04}");
        let flush = |indexes: Vec<usize>, last_seen: u64, cutoff_ms: Option<u64>, command_id: &str| {
            let names: Vec<String> = indexes.into_iter().map(fingerprint).collect();
            log_flush(&names.iter().map(|name| (name.as_str(), last_seen)).collect::<Vec<_>>(), cutoff_ms, command_id)
        };
        let (parent, budget) = {
            let mut database = open(true);
            let parent = seed_parent(&mut database, &session, "archive-conversation", "archive-task");
            let budget = ARCHIVES * stored_bytes(&archive_put_with_response(&parent, "a0000", 1, 1, "", body(0)));
            for index in 0..ARCHIVES {
                dispatched(&mut database, &session, &archive_put_with_response(&parent, &format!("a{index:04}"),
                    index + 1, budget, &format!("seed-put-{index}"), body(index)), 1_100 + index);
            }
            for (batch, start) in (0..AGGREGATES).step_by(500).enumerate() {
                dispatched(&mut database, &session, &flush((start..start + 500).collect(), 1_100, None,
                    &format!("seed-flush-{batch}")), 1_100);
            }
            database.checkpoint_for_test().unwrap();
            (parent, budget)
        };
        let mut rows = Vec::new();
        let mut holds: std::collections::BTreeMap<String, Vec<f64>> = Default::default();
        for pair in 0..PAIRS {
            for lane in if pair % 2 == 0 { [false, true] } else { [true, false] } {
                let tag = pair * 2 + u64::from(lane);
                // Same-sized bodies: each put reclaims exactly the oldest archive.
                let put = archive_put_with_response(&parent, &format!("p{tag:04}"), 100 + tag, budget,
                    &format!("measure-put-{tag}"), body(1_000 + tag));
                let rows_of_tag = (0..FLUSH_ROWS).map(|row| (row * 7 + tag as usize * 13) % AGGREGATES).collect();
                let flush = flush(rows_of_tag, 1_500 + tag, Some(1_000), &format!("measure-flush-{tag}"));
                for command in [&put, &flush] {
                    let mut database = open(false);
                    dispatched(&mut database, &session, &retention_command("record.put", json!({
                        "namespace": "archive-prime", "key": format!("k{tag}"), "value": {"prime": tag}}),
                        &format!("measure-prime-{tag}-{}", command.operation)), 1_900);
                    let database = Mutex::new(database);
                    let arm = if lane { "lane" } else { "exclusive" };
                    let row = if lane {
                        let stores = StorageV2Stores::new(&database, None);
                        let admitted = session.admit(command, NOW).unwrap();
                        let Ok(Some(ir)) = crate::semantic_executor::compile_parallel_transaction(admitted, NOW)
                            else { panic!("{} must compile for the lane", command.operation) };
                        let (replica, capture_ms, _) =
                            attributed(|| database.lock().unwrap().build_preparation_replica().unwrap());
                        let (prepared, prepare_ms, prepare_phases) =
                            attributed(|| prepare_parallel_ir(replica, &session, admitted, &ir, &mut || NOW).unwrap());
                        let ParallelPreparationOutcome::Prepared(prepared) = prepared
                            else { panic!("{} did not prepare", command.operation) };
                        let (response, commit_ms, commit_phases) =
                            attributed(|| finish_on_lane(&stores, &session, command, prepared));
                        let response = response.unwrap_or_else(|| panic!("{} prepared commit refused", command.operation));
                        assert_eq!(response.status, 0, "{:?}", response.error_message);
                        holds.entry(format!("{} prepare_ms", command.operation)).or_default().push(prepare_ms);
                        json!({"hold_ms": capture_ms + commit_ms, "capture_ms": capture_ms, "commit_ms": commit_ms,
                            "commit_phases": commit_phases, "prepare_ms": prepare_ms, "prepare_phases": prepare_phases})
                    } else {
                        // Admission runs before the writer is taken, as in dispatch.
                        let stores = StorageV2Stores::new(&database, None);
                        let admitted = session.admit(command, NOW).unwrap();
                        let (response, hold_ms, phases) = attributed(|| session.dispatch_admitted(
                            &mut database.lock().unwrap(), admitted, NOW, stores.authority_gc_budget));
                        assert_eq!(response.status, 0, "{:?}", response.error_message);
                        json!({"hold_ms": hold_ms, "phases": phases})
                    };
                    holds.entry(format!("{} {arm} hold_ms", command.operation)).or_default()
                        .push(row["hold_ms"].as_f64().unwrap());
                    rows.push(json!({"pair": pair, "arm": arm, "operation": command.operation, "measured": row}));
                }
            }
        }
        let median = |values: &Vec<f64>| {
            let mut sorted = values.clone();
            sorted.sort_by(f64::total_cmp);
            (sorted[sorted.len() / 2] * 100.0).round() / 100.0
        };
        eprintln!("ARCHIVE_LANE_HOLD_MEASUREMENT={}", json!({
            "source_sha256": env!("TOFUDB_SOURCE_SHA256"),
            "volume": format!("{:?}", crate::resource_probe::volume_class(&path)),
            "archives": ARCHIVES, "aggregates": AGGREGATES, "flush_rows": FLUSH_ROWS, "pairs": PAIRS,
            "median_ms": holds.iter().map(|(name, values)| (name.clone(), json!(median(values))))
                .collect::<serde_json::Map<_, _>>(),
            "rows": rows}));
    }
}
