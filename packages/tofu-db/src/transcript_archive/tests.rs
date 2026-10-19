//! Transcript archive tests: seal-window equivalence, fencing, invalidation,
//! and digest enforcement for the sealed-page store.

use super::*;
use crate::authority::AuthorityDatabase;
use crate::entity::EntityKey;

fn authority() -> AuthorityDatabase {
    let vfs: std::sync::Arc<dyn crate::vfs::Vfs> =
        std::sync::Arc::new(crate::vfs::DeterministicVfs::new(None));
    crate::vfs::Vfs::create_dir(vfs.as_ref(), std::path::Path::new("/data")).unwrap();
    crate::vfs::Vfs::sync_directory(vfs.as_ref(), std::path::Path::new("/")).unwrap();
    AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs).unwrap()
}

fn pending_messages(conversation: &str, start: u64, count: u64) -> Vec<Value> {
    (start..start + count)
        .map(|index| {
            json!({
                "content": format!("message {index}"),
                "role": "user",
                "_turnId": format!("{conversation}-turn-{index:03}"),
                "_attemptId": null,
                "_turnActor": "human",
                "_turnKind": "ingested",
                "_turnLaneId": "main",
                "_turnStatus": "completed",
                "_turnSettlement": {"outcome": "completed"},
                "_projectionRevision": 1,
                "_commandPending": null,
                "timestamp": 1_700_000_000 + index,
            })
        })
        .collect()
}

fn seal_to(
    database: &mut AuthorityDatabase,
    conversation: &str,
    lane_count: u64,
    sealed_so_far: u64,
) {
    let pending = pending_messages(conversation, sealed_so_far, lane_count - sealed_so_far);
    let mut transaction = database.begin(1, 7).unwrap();
    maybe_seal(
        &database,
        &mut transaction,
        conversation,
        lane_count,
        pending,
        1_700_000_001,
    )
    .unwrap();
    database.commit(transaction).unwrap();
}

#[test]
fn sealed_prefix_serves_identical_messages_with_digest_pages() {
    let mut database = authority();
    seal_to(&mut database, "conv-a", 80, 0);
    let coverage = {
        let mut transaction = database.begin(1, 7).unwrap();
        sealed_coverage(&database, &mut transaction, "conv-a", 80).unwrap()
    };
    assert_eq!(coverage.unwrap().sealed_messages, 80);
    let mut transaction = database.begin(1, 7).unwrap();
    let messages = read_sealed(&database, &mut transaction, "conv-a", 80)
        .unwrap()
        .expect("sealed archive must serve");
    assert_eq!(messages.len(), 80);
    assert_eq!(messages[3]["content"], json!("message 3"));
    assert_eq!(messages[79]["_turnId"], json!("conv-a-turn-079"));
}

#[test]
fn single_message_seals_into_one_page() {
    let mut database = authority();
    let pending = pending_messages("conv-b", 0, 1);
    let mut transaction = database.begin(1, 7).unwrap();
    maybe_seal(
        &database,
        &mut transaction,
        "conv-b",
        1,
        pending,
        1_700_000_001,
    )
    .unwrap();
    database.commit(transaction).unwrap();
    {
        let before = stats().sealed_pages;
        assert!(before >= 1, "a single pending message must seal into a page");
    }
    let mut transaction = database.begin(1, 7).unwrap();
    let messages = read_sealed(&database, &mut transaction, "conv-b", 1)
        .unwrap()
        .expect("a single pending message must seal into a page");
    assert_eq!(messages.len(), 1);
    assert_eq!(messages[0]["content"], json!("message 0"));
}

#[test]
fn shrunk_lane_invalidates_instead_of_serving_stale_pages() {
    let mut database = authority();
    seal_to(&mut database, "conv-c", 80, 0);
    let invalidations_before = stats().invalidations;
    let mut transaction = database.begin(1, 7).unwrap();
    // A mutation the archive did not track (lane shrank): the seal check
    // rebuilds via invalidation rather than serving stale pages.
    maybe_seal(
        &database,
        &mut transaction,
        "conv-c",
        10,
        Vec::new(),
        1_700_000_002,
    )
    .unwrap();
    database.commit(transaction).unwrap();
    assert!(stats().invalidations > invalidations_before);
    let mut transaction = database.begin(1, 7).unwrap();
    assert!(
        read_sealed(&database, &mut transaction, "conv-c", 80)
            .unwrap()
            .is_none()
    );
}

#[test]
fn tampered_page_digest_fails_closed() {
    let mut database = authority();
    seal_to(&mut database, "conv-d", 65, 0);
    let page_key_for_test = {
        let mut key_transaction = database.begin(1, 7).unwrap();
        page_key(&key_transaction, "conv-d", 0).unwrap()
    };
    // Corrupt the manifest's digest for the sealed page (the manifest
    // itself stays valid JSON; the page content is untouched).
    let manifest_key_for_test = {
        let mut key_transaction = database.begin(1, 7).unwrap();
        manifest_key(&key_transaction, "conv-d").unwrap()
    };
    let mut transaction = database.begin(1, 7).unwrap();
    let manifest_bytes = vd_read(
        &database,
        &mut transaction,
        &manifest_key_for_test,
        TRANSCRIPT_ARCHIVE_MANIFEST_NAMESPACE,
    )
    .unwrap()
    .unwrap();
    let mut manifest = decode_manifest(&manifest_bytes).unwrap().unwrap();
    manifest.pages[0].sha256 = "00".repeat(32);
    vd_put(
        &database,
        &mut transaction,
        manifest_key_for_test.clone(),
        TRANSCRIPT_ARCHIVE_MANIFEST_NAMESPACE,
        String::from_utf8(manifest_key_for_test.key_bytes().to_vec()).unwrap(),
        encode_manifest(&manifest),
        1_700_000_002,
    )
    .unwrap();
    database.commit(transaction).unwrap();
    let mut transaction = database.begin(1, 7).unwrap();
    assert!(
        read_sealed(&database, &mut transaction, "conv-d", 65).is_err(),
        "a tampered page digest must fail closed"
    );
}

#[test]
fn page_decode_preserves_source_array_shape_and_noncanonical_bytes() {
    for (bytes, accepted) in [
        (br#" {"$serde_json::private::RawValue":"[{}]"} "#.as_slice(), false),
        (br#" [ { "content": "message", "role": "user" } ] "#.as_slice(), true),
    ] {
        let mut database = authority();
        seal_to(&mut database, "source-shape", 1, 0);
        let mut write = database.begin(1, 7).unwrap();
        let mut manifest = load_manifest_for(&database, &mut write, "source-shape").unwrap().unwrap();
        manifest.pages[0].bytes = bytes.len() as u64;
        manifest.pages[0].sha256 = blake3::hash(bytes).to_hex().to_string();
        manifest.decoded_bytes = bytes.len() as u64;
        for (key, namespace, value) in [
            (page_key(&write, "source-shape", 0).unwrap(), TRANSCRIPT_ARCHIVE_PAGE_NAMESPACE, bytes.to_vec()),
            (manifest_key(&write, "source-shape").unwrap(), TRANSCRIPT_ARCHIVE_MANIFEST_NAMESPACE, encode_manifest(&manifest)),
        ] {
            let logical_key = String::from_utf8(key.key_bytes().to_vec()).unwrap();
            vd_put(&database, &mut write, key, namespace, logical_key, value, 1_700_000_002).unwrap();
        }
        database.commit(write).unwrap();
        let mut read = database.begin(1, 7).unwrap();
        let result = read_sealed(&database, &mut read, "source-shape", 1);
        assert_eq!(result.is_ok(), accepted);
        if accepted {
            assert_eq!(result.unwrap().unwrap(), vec![json!({"content":"message", "role":"user"})]);
        } else {
            assert_eq!(result.unwrap_err().to_string(), "transcript archive page is not valid JSON");
        }
    }
}

#[test]
fn windowed_sealed_read_uses_exact_window_semantics() {
    let mut database = authority();
    seal_to(&mut database, "conv-e", 80, 0);
    let mut transaction = database.begin(1, 7).unwrap();
    let (messages, total, start, end) = read_window(
        &database,
        &mut transaction,
        "conv-e",
        5,
        Some(30),
        80,
    )
    .unwrap()
    .expect("sealed archive must serve the window");
    assert_eq!((total, start, end), (80, 25, 30));
    assert_eq!(messages.len(), 5);
    assert_eq!(messages[0]["content"], json!("message 25"));
    assert_eq!(messages[4]["content"], json!("message 29"));
}

#[test]
fn zero_window_reads_all_sealed_messages_and_respects_before() {
    let mut database = authority();
    seal_to(&mut database, "full-window", 80, 0);
    for (before, expected_end) in [(None, 80), (Some(30), 30), (Some(0), 0), (Some(-1), 80)] {
        let mut read = database.begin(1, 7).unwrap();
        let (messages, total, start, end) = read_window(
            &database, &mut read, "full-window", 0, before, 80,
        ).unwrap().expect("full sentinel must use sealed messages");
        assert_eq!((total, start, end), (80, 0, expected_end));
        assert_eq!(messages, pending_messages("full-window", 0, expected_end as u64));
    }
}

#[test]
fn lane_range_crosses_index_page_boundary_without_truncating() {
    use crate::turn::AppendSettledRequest;
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let committed_at_ms = 1_700_000_000_u64;
    let mut transaction = database.begin_with_identity_claim_scopes(1, 7).unwrap();
    for index in 0..1_005_u64 {
        crate::turn::append_settled(
            &database,
            &mut transaction,
            &AppendSettledRequest {
                conversation_id: "seal-live".to_owned(),
                actor: "human".to_owned(),
                status: "completed".to_owned(),
                projection_json: format!(
                    r#"{{"content":"live sealed message {index}"}}"#
                )
                .into_bytes(),
                projection_timestamp: None,
                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: format!("seal-live-{index}"),
                kind: "ingested".to_owned(),
                run_id: String::new(),
                turn_id: format!("seal-live-turn-{index:03}"),
                attempt_id: None,
                created_at_ms: committed_at_ms + index,
                committed_at_ms: committed_at_ms + index,
                defaults: crate::conversation_header::TurnDefaults {
                    allow_create: index == 0,
                    title: "live".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: committed_at_ms,
                },
            },
        )
        .unwrap();
    }
    database.commit(transaction).unwrap();
    let mut read = database.begin(1, 7).unwrap();
    let messages = crate::turn::materialize_lane_range(
        &database, &mut read, "seal-live", 998, 1_005, 0,
    ).unwrap();
    assert_eq!(messages.len(), 7);
    assert_eq!(messages[0]["content"], json!("live sealed message 998"));
    assert_eq!(messages[6]["content"], json!("live sealed message 1004"));
}

#[test]
fn full_history_combines_sealed_prefix_and_live_tail_exactly() {
    use crate::turn::AppendSettledRequest;
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let committed_at_ms = 1_700_000_000_u64;
    for index in 0..20_u64 {
        let mut transaction = database.begin_with_identity_claim_scopes(1, 7).unwrap();
        crate::turn::append_settled(
            &database,
            &mut transaction,
            &AppendSettledRequest {
                conversation_id: "seal-live".to_owned(),
                actor: "human".to_owned(),
                status: "completed".to_owned(),
                projection_json: format!(
                    r#"{{"content":"live sealed message {index}"}}"#
                )
                .into_bytes(),
                projection_timestamp: None,
                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: format!("seal-live-{index}"),
                kind: "ingested".to_owned(),
                run_id: String::new(),
                turn_id: format!("seal-live-turn-{index:03}"),
                attempt_id: None,
                created_at_ms: committed_at_ms + index,
                committed_at_ms: committed_at_ms + index,
                defaults: crate::conversation_header::TurnDefaults {
                    allow_create: index == 0,
                    title: "live".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: committed_at_ms,
                },
            },
        )
        .unwrap();
        database.commit(transaction).unwrap();
        if index == 9 {
            let mut read = database.begin(1, 7).unwrap();
            let (pending, _, _, _) = crate::turn::legacy_messages(
                &database, &mut read, "seal-live", 0, None, &mut || false,
            ).unwrap();
            let mut seal = database.begin(1, 7).unwrap();
            maybe_seal(&database, &mut seal, "seal-live", 10, pending, committed_at_ms + index).unwrap();
            database.commit(seal).unwrap();
        }
    }
    let mut read = database.begin(1, 7).unwrap();
    assert_eq!(sealed_coverage(&database, &mut read, "seal-live", 20).unwrap().unwrap().sealed_messages, 10);
    let cases = [(0, None), (0, Some(8)), (0, Some(15)), (0, Some(0)), (5, Some(15))];
    let mut expired = database.begin(1, 7).unwrap();
    assert_eq!(crate::turn::legacy_messages(
        &database, &mut expired, "seal-live", 0, None, &mut || true,
    ).unwrap_err().kind(), std::io::ErrorKind::TimedOut);
    let archived: Vec<_> = cases.iter().map(|(window, before)| {
        let mut read = database.begin(1, 7).unwrap();
        crate::turn::legacy_messages(&database, &mut read, "seal-live", *window, *before, &mut || false).unwrap()
    }).collect();
    assert_eq!(archived[0].0.len(), 20);
    assert_eq!(archived[0].0[19]["content"], json!("live sealed message 19"));
    let mut mutation = database.begin(1, 7).unwrap();
    invalidate(&database, &mut mutation, "seal-live").unwrap();
    database.commit(mutation).unwrap();
    for ((window, before), expected) in cases.iter().zip(archived) {
        let mut read = database.begin(1, 7).unwrap();
        let fallback = crate::turn::legacy_messages(&database, &mut read, "seal-live", *window, *before, &mut || false).unwrap();
        assert_eq!(expected, fallback, "window={window} before={before:?}");
    }
}

#[test]
fn seal_one_conversation_seals_and_read_sealed_serves_from_a_live_authority() {
    use crate::turn::AppendSettledRequest;
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let committed_at_ms = 1_700_000_000_u64;
    for index in 0..20_u64 {
        let mut transaction = database.begin_with_identity_claim_scopes(1, 7).unwrap();
        crate::turn::append_settled(
            &database,
            &mut transaction,
            &AppendSettledRequest {
                conversation_id: "seal-live".to_owned(),
                actor: "human".to_owned(),
                status: "completed".to_owned(),
                projection_json: format!(
                    r#"{{"content":"live sealed message {index}"}}"#
                )
                .into_bytes(),
                projection_timestamp: None,
                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: format!("seal-live-{index}"),
                kind: "ingested".to_owned(),
                run_id: String::new(),
                turn_id: format!("seal-live-turn-{index:03}"),
                attempt_id: None,
                created_at_ms: committed_at_ms + index,
                committed_at_ms: committed_at_ms + index,
                defaults: crate::conversation_header::TurnDefaults {
                    allow_create: index == 0,
                    title: "live".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: committed_at_ms,
                },
            },
        )
        .unwrap();
        database.commit(transaction).unwrap();
    }
    let database = std::sync::Arc::new(std::sync::Mutex::new(database));
    let outcome = seal_one_conversation(&database, 1, 7, "seal-live");
    eprintln!("seal outcome: {outcome:?}");
    outcome.unwrap();
    let coverage = {
        let guard = database.lock().unwrap();
        let mut transaction = guard.begin(1, 7).unwrap();
        sealed_coverage(&*guard, &mut transaction, "seal-live", 20).unwrap()
    };
    assert_eq!(coverage.unwrap().sealed_messages, 20);
    let guard = database.lock().unwrap();
    let mut transaction = guard.begin(1, 7).unwrap();
    let messages = read_sealed(&*guard, &mut transaction, "seal-live", 20)
        .unwrap()
        .expect("sealed archive must serve");
    assert_eq!(messages.len(), 20);
    assert_eq!(messages[0]["content"], json!("live sealed message 0"));
    assert_eq!(messages[19]["_turnId"], json!("seal-live-turn-019"));
    for (before, expected_end) in [(None, 20), (Some(8), 8), (Some(0), 0)] {
        let mut read = guard.begin(1, 7).unwrap();
        let (full, total, start, end) = crate::turn::legacy_messages(
            &guard, &mut read, "seal-live", 0, before, &mut || false,
        ).unwrap();
        assert_eq!((total, start, end), (20, 0, expected_end));
        assert_eq!(full, messages[..expected_end]);
    }
}

#[test]
fn enumerate_conversations_walks_the_catalog_with_a_cursor() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    for name in ["conv-1", "conv-2", "conv-3"] {
        let mut transaction = database.begin_with_identity_claim_scopes(1, 7).unwrap();
        database
            .entity_put(
                &mut transaction,
                EntityKey::new(1, 7, crate::generated_tofudb_ir::CONVERSATION_DOCUMENT_NAMESPACE, name.as_bytes())
                    .unwrap(),
                b"{}".to_vec(),
            )
            .unwrap();
        database.commit(transaction).unwrap();
    }
    let mut after = Vec::new();
    let mut transaction = database.begin(1, 7).unwrap();
    let conversations = enumerate_conversations(&database, &mut transaction, &mut after, 8).unwrap();
    eprintln!("enumerated: {conversations:?} after={after:?}");
    assert!(conversations.contains(&"conv-1".to_owned()), "{conversations:?}");
}

#[test]
fn archive_seal_driver_seals_live_conversations_in_the_background() {
    use crate::turn::AppendSettledRequest;
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let committed_at_ms = 1_700_000_000_u64;
    for index in 0..10_u64 {
        let mut transaction = database.begin_with_identity_claim_scopes(1, 7).unwrap();
        crate::turn::append_settled(
            &database,
            &mut transaction,
            &AppendSettledRequest {
                conversation_id: "driver-live".to_owned(),
                actor: "human".to_owned(),
                status: "completed".to_owned(),
                projection_json: format!(
                    r#"{{"content":"driver sealed message {index}"}}"#
                )
                .into_bytes(),
                projection_timestamp: None,
                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: format!("driver-live-{index}"),
                kind: "ingested".to_owned(),
                run_id: String::new(),
                turn_id: format!("driver-live-turn-{index:03}"),
                attempt_id: None,
                created_at_ms: committed_at_ms + index,
                committed_at_ms: committed_at_ms + index,
                defaults: crate::conversation_header::TurnDefaults {
                    allow_create: index == 0,
                    title: "driver".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: committed_at_ms,
                },
            },
        )
        .unwrap();
        database.commit(transaction).unwrap();
    }
    let database = std::sync::Arc::new(std::sync::Mutex::new(database));
    let driver = ArchiveSealDriver::start(&database, 1, 7).unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(20);
    let mut sealed = 0_u64;
    while std::time::Instant::now() < deadline {
        {
            let guard = database.lock().unwrap();
            let mut transaction = guard.begin(1, 7).unwrap();
            if let Some(coverage) =
                sealed_coverage(&*guard, &mut transaction, "driver-live", 10).unwrap()
            {
                sealed = coverage.sealed_messages;
                if sealed == 10 {
                    break;
                }
            }
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    driver.stop_and_join().unwrap();
    assert_eq!(sealed, 10, "the driver must seal the live conversation");
}

#[test]
fn driver_seals_under_the_daemons_scope_shape() {
    use crate::turn::AppendSettledRequest;
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let committed_at_ms = 1_700_000_000_u64;
    for index in 0..10_u64 {
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::turn::append_settled(
            &database,
            &mut transaction,
            &AppendSettledRequest {
                conversation_id: "daemon-shape".to_owned(),
                actor: "human".to_owned(),
                status: "completed".to_owned(),
                projection_json: format!(r#"{{"content":"shape {index}"}}"#).into_bytes(),
                projection_timestamp: None,
                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: format!("shape-{index}"),
                kind: "ingested".to_owned(),
                run_id: String::new(),
                turn_id: format!("shape-turn-{index:03}"),
                attempt_id: None,
                created_at_ms: committed_at_ms + index,
                committed_at_ms: committed_at_ms + index,
                defaults: crate::conversation_header::TurnDefaults {
                    allow_create: index == 0,
                    title: "shape".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: committed_at_ms,
                },
            },
        )
        .unwrap();
        database.commit(transaction).unwrap();
    }
    let database = std::sync::Arc::new(std::sync::Mutex::new(database));
    let driver = ArchiveSealDriver::start(&database, 7, 11).unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(20);
    let mut sealed = 0_u64;
    while std::time::Instant::now() < deadline {
        {
            let guard = database.lock().unwrap();
            let mut transaction = guard.begin(7, 11).unwrap();
            if let Some(coverage) =
                sealed_coverage(&*guard, &mut transaction, "daemon-shape", 10).unwrap()
            {
                sealed = coverage.sealed_messages;
                if sealed == 10 {
                    break;
                }
            }
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    driver.stop_and_join().unwrap();
    assert_eq!(sealed, 10, "driver must seal under the daemon scope shape");
    let guard = database.lock().unwrap();
    let mut transaction = guard.begin(7, 11).unwrap();
    let messages = read_sealed(&*guard, &mut transaction, "daemon-shape", 10)
        .unwrap()
        .expect("archive must serve after daemon-shape sealing");
    assert_eq!(messages.len(), 10);
    assert_eq!(messages[0]["content"], json!("shape 0"));
}

fn append_preparation_message(database: &mut AuthorityDatabase, index: u64) {
    let mut transaction = database.begin_with_identity_claim_scopes(1, 7).unwrap();
    crate::turn::append_settled(
        database,
        &mut transaction,
        &crate::turn::AppendSettledRequest {
            conversation_id: "prepared-archive".to_owned(),
            turn_id: format!("prepared-{index}"),
            actor: "human".to_owned(),
            kind: "ingested".to_owned(),
            lane_id: "main".to_owned(),
            projection_json: serde_json::to_vec(
                &json!({"content": format!("{index}-{}", "message".repeat(4096)), "role":"user"}),
            )
            .unwrap(),
            settlement_json: br#"{"outcome":"completed"}"#.to_vec(),
            command_id: format!("prepared-command-{index}"),
            status: "completed".to_owned(),
            projection_timestamp: None,
            run_id: String::new(),
            attempt_id: None,
            created_at_ms: 1_700_000_000 + index,
            committed_at_ms: 1_700_000_000 + index,
            defaults: crate::conversation_header::TurnDefaults {
                allow_create: index == 0,
                title: "prepared archive".to_owned(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 1_700_000_000,
            },
        },
    )
    .unwrap();
    database.commit(transaction).unwrap();
}

fn prepared_archive_seed() -> (Arc<crate::vfs::DeterministicVfs>, AuthorityDatabase) {
    use crate::vfs::Vfs;
    let vfs = Arc::new(crate::vfs::DeterministicVfs::new(None));
    vfs.create_dir(std::path::Path::new("/data")).unwrap();
    vfs.sync_directory(std::path::Path::new("/")).unwrap();
    let mut database =
        AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone()).unwrap();
    database.set_write_overlay_for_test(true);
    append_preparation_message(&mut database, 0);
    append_preparation_message(&mut database, 1);
    (vfs, database)
}

#[test]
fn prepared_archive_keeps_private_bytes_and_allows_unrelated_foreground_commit() {
    use crate::vfs::Operation;
    let (vfs, database) = prepared_archive_seed();
    let database = Arc::new(Mutex::new(database));
    let replica = database
        .lock()
        .unwrap()
        .build_preparation_replica()
        .unwrap();
    vfs.arm_fault(None).unwrap();
    let transaction = prepare_one_conversation(&replica, 1, 7, "prepared-archive")
        .unwrap()
        .unwrap()
        .transaction;
    assert!(!vfs.trace().unwrap().contains(&Operation::Write));
    drop(replica);
    let mut foreground = database
        .try_lock()
        .expect("page preparation must release the authority");
    let mut unrelated = foreground.begin(1, 7).unwrap();
    foreground
        .entity_put(
            &mut unrelated,
            EntityKey::new(1, 7, "other", b"foreground").unwrap(),
            b"progress".to_vec(),
        )
        .unwrap();
    foreground.commit(unrelated).unwrap();
    foreground.commit(transaction).unwrap();
    let mut read = foreground.begin(1, 7).unwrap();
    let sealed = read_sealed(&foreground, &mut read, "prepared-archive", 2)
        .unwrap()
        .unwrap();
    assert_eq!(sealed.len(), 2);
    assert_eq!(
        sealed[1]["content"],
        json!(format!("1-{}", "message".repeat(4096)))
    );
    drop(read);
    assert_eq!(
        foreground
            .snapshot_pin_metrics()
            .unwrap()
            .retained_transaction_bytes,
        0
    );
}

#[test]
fn retired_archive_format_falls_back_and_rebuilds_from_authoritative_turns() {
    let (_, mut database) = prepared_archive_seed();
    let replica = database.build_preparation_replica().unwrap();
    let prepared = prepare_one_conversation(&replica, 1, 7, "prepared-archive").unwrap().unwrap();
    database.commit(prepared.transaction).unwrap();
    let mut transaction = database.begin(1, 7).unwrap();
    let key = manifest_key(&transaction, "prepared-archive").unwrap();
    let bytes = vd_read(&database, &mut transaction, &key, TRANSCRIPT_ARCHIVE_MANIFEST_NAMESPACE).unwrap().unwrap();
    let mut old: Value = serde_json::from_slice(&bytes).unwrap();
    old["format"] = json!(crate::generated_tofudb_ir::TRANSCRIPT_ARCHIVE_RETIRED_FORMAT);
    vd_put(&database, &mut transaction, key.clone(), TRANSCRIPT_ARCHIVE_MANIFEST_NAMESPACE,
        String::from_utf8(key.key_bytes().to_vec()).unwrap(), serde_json::to_vec(&old).unwrap(), 1_700_000_010).unwrap();
    database.commit(transaction).unwrap();
    let mut read = database.begin(1, 7).unwrap();
    assert!(read_sealed(&database, &mut read, "prepared-archive", 2).unwrap().is_none());
    let (exact, count, _, _) = crate::turn::legacy_messages(&database, &mut read,
        "prepared-archive", 0, None, &mut || false).unwrap();
    assert_eq!(count, 2);
    assert!(exact[1]["content"].as_str().unwrap().starts_with("1-message"));
    drop(read);
    let replica = database.build_preparation_replica().unwrap();
    let prepared = prepare_one_conversation(&replica, 1, 7, "prepared-archive").unwrap().unwrap();
    database.commit(prepared.transaction).unwrap();
    let mut read = database.begin(1, 7).unwrap();
    assert_eq!(read_sealed(&database, &mut read, "prepared-archive", 2).unwrap().unwrap(), exact);
    let encoded = vd_read(&database, &mut read, &key, TRANSCRIPT_ARCHIVE_MANIFEST_NAMESPACE).unwrap().unwrap();
    assert_eq!(serde_json::from_slice::<Value>(&encoded).unwrap()["format"], FORMAT);
}

#[test]
fn prepared_archive_rejects_a_changed_source_before_publishing_any_private_bytes() {
    use crate::vfs::Operation;
    for invalidate_manifest in [false, true] {
        let (vfs, mut database) = prepared_archive_seed();
        let replica = database.build_preparation_replica().unwrap();
        let transaction = prepare_one_conversation(&replica, 1, 7, "prepared-archive")
            .unwrap()
            .unwrap()
            .transaction;
        drop(replica);
        if invalidate_manifest {
            let mut update = database.begin(1, 7).unwrap();
            invalidate(&database, &mut update, "prepared-archive").unwrap();
            database.commit(update).unwrap();
        } else {
            append_preparation_message(&mut database, 2);
        }
        vfs.arm_fault(None).unwrap();
        assert_eq!(
            database.commit(transaction).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        assert!(!vfs.trace().unwrap().contains(&Operation::Write));
        assert_eq!(
            database
                .snapshot_pin_metrics()
                .unwrap()
                .retained_transaction_bytes,
            0
        );
        let replica = database.build_preparation_replica().unwrap();
        let fresh = prepare_one_conversation(&replica, 1, 7, "prepared-archive")
            .unwrap()
            .unwrap()
            .transaction;
        drop(replica);
        database.commit(fresh).unwrap();
        let mut read = database.begin(1, 7).unwrap();
        let count = if invalidate_manifest { 2 } else { 3 };
        assert_eq!(
            read_sealed(&database, &mut read, "prepared-archive", count)
                .unwrap()
                .unwrap()
                .len(),
            count as usize
        );
    }
}

#[test]
fn prepared_archive_commit_faults_reopen_without_partial_page_or_manifest() {
    use crate::vfs::{FaultAction, FaultRule, Operation};
    let (vfs, mut database) = prepared_archive_seed();
    let replica = database.build_preparation_replica().unwrap();
    let transaction = prepare_one_conversation(&replica, 1, 7, "prepared-archive")
        .unwrap()
        .unwrap()
        .transaction;
    drop(replica);
    vfs.arm_fault(None).unwrap();
    database.commit(transaction).unwrap();
    let trace = vfs.trace().unwrap();
    for (index, operation) in trace.iter().enumerate() {
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
        if *operation == Operation::Write {
            actions.push(FaultAction::ShortWrite(7));
        }
        if matches!(
            operation,
            Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
        ) {
            actions.push(FaultAction::DropSync);
        }
        for action in actions {
            let (vfs, mut database) = prepared_archive_seed();
            let replica = database.build_preparation_replica().unwrap();
            let transaction = prepare_one_conversation(&replica, 1, 7, "prepared-archive")
                .unwrap()
                .unwrap()
                .transaction;
            drop(replica);
            let lost_sync = matches!(action, FaultAction::DropSync);
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action,
            }))
            .unwrap();
            let acknowledged = database.commit(transaction).is_ok();
            let reopened = AuthorityDatabase::open_with_vfs(
                std::path::Path::new("/data"),
                Arc::new(vfs.fork_durable().unwrap()),
            )
            .unwrap();
            let mut read = reopened.begin(1, 7).unwrap();
            let page = read_sealed(&reopened, &mut read, "prepared-archive", 2).unwrap();
            if acknowledged && !lost_sync {
                assert!(page.is_some());
            }
            if let Some(page) = page {
                assert_eq!(page.len(), 2);
                assert_eq!(
                    page[0]["content"],
                    json!(format!("0-{}", "message".repeat(4096)))
                );
            }
        }
    }
}

#[test]
fn enumeration_visits_every_conversation_once_across_lookahead_pages() {
    for count in [0, 1, 8, 9, 16, 17, 50] {
        let mut database = authority();
        let expected: Vec<String> = (0..count).map(|index| format!("conv-{index:03}")).collect();
        let mut write = database.begin_with_identity_claim_scopes(1, 7).unwrap();
        for name in &expected {
            database.entity_put(&mut write, EntityKey::new(1, 7,
                crate::generated_tofudb_ir::CONVERSATION_DOCUMENT_NAMESPACE,
                name.as_bytes()).unwrap(), b"{}".to_vec()).unwrap();
        }
        if count != 0 { database.commit(write).unwrap(); }
        let mut other = database.begin_with_identity_claim_scopes(1, 8).unwrap();
        database.entity_put(&mut other, EntityKey::new(1, 8,
            crate::generated_tofudb_ir::CONVERSATION_DOCUMENT_NAMESPACE,
            b"other-owner").unwrap(), b"{}".to_vec()).unwrap();
        database.commit(other).unwrap();
        for maximum in [1, 8] {
            let mut cursor = Vec::new();
            let mut actual = Vec::new();
            for _ in 0..=count {
                let mut read = database.begin(1, 7).unwrap();
                let page = enumerate_conversations(&database, &mut read, &mut cursor, maximum).unwrap();
                assert!(page.len() <= maximum);
                actual.extend(page);
                if cursor.is_empty() { break; }
            }
            assert!(cursor.is_empty(), "scan did not complete");
            assert_eq!(actual, expected, "count={count}, maximum={maximum}");
        }
    }
}

#[test]
fn archive_worker_attributes_background_activity_without_changing_its_caller() {
    let database = std::sync::Arc::new(std::sync::Mutex::new(authority()));
    let caller_component = crate::fsync_stats::current_component_for_test();
    let worker_component = std::thread::spawn(move || {
        assert_eq!(crate::fsync_stats::current_component_for_test(),
                   crate::fsync_stats::COMPONENT_FOREGROUND);
        run_archive_seal_worker(database, 1, 1,
            std::sync::Arc::new(std::sync::atomic::AtomicBool::new(true)));
        crate::fsync_stats::current_component_for_test()
    }).join().unwrap();
    assert_eq!(worker_component, crate::fsync_stats::COMPONENT_MAINTENANCE);
    assert_eq!(crate::fsync_stats::current_component_for_test(), caller_component);
}
