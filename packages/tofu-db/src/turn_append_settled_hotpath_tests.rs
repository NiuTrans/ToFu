    use super::*;

    fn append(
        database: &mut AuthorityDatabase,
        conversation_id: &str,
        turn_id: &str,
        projection: Value,
        created_at_ms: u64,
    ) -> Value {
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let response = append_settled(
            database,
            &mut transaction,
            &AppendSettledRequest {
                conversation_id: conversation_id.to_owned(),
                actor: "human".to_owned(),
                status: "completed".to_owned(),
                projection_json: serde_json::to_vec(&projection).unwrap(),
                projection_timestamp: None,
                settlement_json:
                    br#"{"outcome":"completed","cause":"ingested","resumeOptions":[]}"#.to_vec(),
                lane_id: "main".to_owned(),
                command_id: format!("{turn_id}-command"),
                kind: "message".to_owned(),
                run_id: String::new(),
                turn_id: turn_id.to_owned(),
                attempt_id: None,
                created_at_ms,
                committed_at_ms: created_at_ms,
                defaults: TurnDefaults {
                    allow_create: true,
                    title: "Hot path".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 1_000,
                },
            },
        )
        .unwrap();
        database.commit(transaction).unwrap();
        serde_json::from_slice(&response).unwrap()
    }

    #[test]
    fn receipt_producer_rejects_raw_projection_injection_before_returning_proof() {
        let directory = tempfile::tempdir().unwrap();
        let database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let before = database.wal_len().unwrap();
        for projection in [b"{}},\"ok\":false,\"turn\":{}".as_slice(), b"{\"content\":\"\\uD800\"}", b"{\"content\":1e309}"] {
            let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            let request = AppendSettledRequest {
                conversation_id: "bad-projection".into(), actor: "human".into(), status: "completed".into(),
                projection_json: projection.to_vec(), projection_timestamp: None,
                settlement_json: br#"{"outcome":"completed"}"#.to_vec(), lane_id: "main".into(),
                command_id: "bad-command".into(), kind: "message".into(), run_id: String::new(),
                turn_id: "bad-turn".into(), attempt_id: None, created_at_ms: 1000, committed_at_ms: 1000,
                defaults: TurnDefaults { allow_create: true, title: "Bad".into(), settings_json: b"{}".to_vec(), created_at_ms: 1000 },
            };
            assert!(append_settled_with_receipt_proof(&database, &mut transaction, &request).is_err());
            drop(transaction);
            assert_eq!(database.wal_len().unwrap(), before, "rejected raw input publishes no transaction");
        }
    }

    #[test]
    fn append_settled_parent_identity_and_lane_index_come_from_stored_envelopes() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let first = append(
            &mut database,
            "conversation",
            "turn-a",
            json!({"content": "first"}),
            2_000,
        );
        assert_eq!(first["turn"]["parentTurnId"], Value::Null);
        assert_eq!(first["conversationRevision"], 1);
        let second = append(
            &mut database,
            "conversation",
            "turn-b",
            json!({"content": "second"}),
            3_000,
        );
        assert_eq!(second["turn"]["parentTurnId"], "turn-a");
        assert_eq!(second["turn"]["ordinal"], 1);
        assert_eq!(second["conversationRevision"], 2);

        // The lane index value must be byte-identical to the stored document
        // envelope (the hot path reuses the put's envelope instead of a
        // re-read; this pins the equivalence).
        let mut inspect = database.begin(7, 11).unwrap();
        let document_key = turn_key(&inspect, "conversation", "turn-b").unwrap();
        let document = database
            .entity_get(&mut inspect, &document_key)
            .unwrap()
            .expect("turn document exists");
        let lane_prefix = lane_prefix("conversation", "main").unwrap();
        let mut lane_key_bytes = lane_prefix.clone();
        lane_key_bytes.extend_from_slice(&1_u64.to_be_bytes());
        let lane_index_key =
            entity_key(&inspect, TURN_LANE_INDEX_NAMESPACE, &lane_key_bytes).unwrap();
        let lane_value = database
            .entity_get(&mut inspect, &lane_index_key)
            .unwrap()
            .expect("lane index entry exists");
        assert_eq!(document, lane_value);
        assert_eq!(
            crate::versioned_document::stored_document_logical_key(&lane_value, DOCUMENT_IDENTITY)
                .unwrap(),
            "conversation\u{1f}turn-b",
        );
    }

    #[test]
    fn append_settled_parent_identity_skips_blob_materialization() {
        // A large projection pushes the predecessor document behind a blob
        // reference; the parent lookup reads only the stored envelope header,
        // so it must still resolve the exact turn identity.
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let large = "x".repeat(64 * 1024);
        append(
            &mut database,
            "conversation",
            "turn-large",
            json!({"content": large}),
            2_000,
        );
        {
            let mut inspect = database.begin(7, 11).unwrap();
            let large_key = turn_key(&inspect, "conversation", "turn-large").unwrap();
            let stored = database
                .entity_get(&mut inspect, &large_key)
                .unwrap()
                .expect("large turn document exists");
            assert!(
                crate::versioned_document::stored_blob_reference(&stored)
                    .unwrap()
                    .is_some(),
                "fixture projection must be blob-backed for this test to mean anything"
            );
        }
        let follower = append(
            &mut database,
            "conversation",
            "turn-follower",
            json!({"content": "after large"}),
            3_000,
        );
        assert_eq!(follower["turn"]["parentTurnId"], "turn-large");
    }
