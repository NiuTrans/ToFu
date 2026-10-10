//! Main-lane context selection with revision-proven text continuations.
//! Reads compact authority indexes before materializing only the new suffix.
use super::*;
use sha2::{Digest, Sha256};

pub(crate) fn read(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    replay: bool,
    after_turn_id: Option<&str>,
) -> io::Result<Option<Vec<u8>>> {
    let Some(target_bytes) = get(database, transaction, conversation_id, turn_id)? else {
        return Ok(None);
    };
    let target: Value = serde_json::from_slice(&target_bytes)
        .map_err(|_| invalid_data("context target is malformed"))?;
    if target.get("laneId").and_then(Value::as_str) != Some("main") {
        return encode_response(&json!({"supported": false})).map(Some);
    }
    let prefix = lane_prefix(conversation_id, "main")?;
    let (mut start, end) = EntityKey::prefix_range(
        transaction.tenant_id(), transaction.owner_user_id(),
        TURN_LANE_COMPACTION_INDEX_NAMESPACE, &prefix,
    )?;
    let mut headers = Vec::new();
    let mut found = false;
    for _ in 0..MAX_SCAN_PAGES {
        let entries = database.entity_scan(transaction, &start, &end, INDEX_PAGE_ROWS)?;
        if entries.is_empty() { break; }
        let next = after_key(&entries.last().unwrap().0, transaction,
                             TURN_LANE_COMPACTION_INDEX_NAMESPACE)?;
        for (_, bytes) in entries {
            let header: Value = serde_json::from_slice(&bytes)
                .map_err(|_| invalid_data("context header is malformed"))?;
            found = header.get("turnId").and_then(Value::as_str) == Some(turn_id);
            headers.push(header);
            if headers.len() > MAX_LIST_ROWS {
                return encode_response(&json!({"supported": false})).map(Some);
            }
            if found { break; }
        }
        if found { break; }
        start = next;
    }
    if !found { return Err(invalid_data("Model context target order changed")); }
    let cached: Option<Value> = if replay {
        crate::model_continuation::execute(database, transaction,
            &crate::model_continuation::Request::Get {
                conversation_id: conversation_id.to_owned(), lane_id: "main".to_owned(),
            })?.map(|bytes| serde_json::from_slice(&bytes)
                    .map_err(|_| invalid_data("context continuation is malformed")))
              .transpose()?
    } else { None };
    let proof = cached.as_ref().and_then(|value| value.get("historyProof"));
    let prior_count = proof.and_then(|value| value.get("count"))
        .and_then(Value::as_u64).and_then(|value| usize::try_from(value).ok());
    let terminal = cached.as_ref().and_then(|value| value.get("turns"))
        .and_then(Value::as_array).filter(|values| values.len() == 1)
        .and_then(|values| values.first());
    let usable = proof.and_then(|value| value.get("version")).and_then(Value::as_u64) == Some(2)
        && prior_count.is_some_and(|count| count < headers.len() - 1) && terminal.is_some();
    let mut continuation_status = if !replay {
        "replay_disabled"
    } else if cached.is_none() {
        "checkpoint_missing"
    } else if proof.is_none() {
        "legacy_checkpoint"
    } else if proof.and_then(|value| value.get("version")).and_then(Value::as_u64) != Some(2) {
        "legacy_proof"
    } else if !usable {
        "checkpoint_not_before_target"
    } else {
        "history_changed"
    };
    let mut digest = Sha256::new();
    let mut prior_matches = false;
    for (index, header) in headers[..headers.len() - 1].iter().enumerate() {
        if usable && prior_count == Some(index) {
            prior_matches = proof.and_then(|value| value.get("digest")).and_then(Value::as_str)
                == Some(format!("{:x}", digest.clone().finalize()).as_str());
        }
        let context_revision = header.get("modelContextRevision")
            .or_else(|| header.get("projectionRevision")).and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("context header revision is malformed"))?;
        let encoded = serde_json::to_vec(&json!([
            header.get("turnId"), header.get("parentTurnId"), header.get("status"),
            header.get("currentAttemptId"), context_revision
        ])).map_err(|_| invalid_data("context header cannot be encoded"))?;
        digest.update(encoded);
        digest.update(b"\n");
    }
    let mut covered = 0;
    if prior_matches {

        continuation_status = "terminal_changed";
        let count = prior_count.unwrap();
        let witness = terminal.unwrap();
        if headers[count].get("turnId") == witness.get(0) {
            let final_id = witness.get(0).and_then(Value::as_str)
                .ok_or_else(|| invalid_data("context witness is malformed"))?;
            if let Some(bytes) = get(database, transaction, conversation_id, final_id)? {
                let row: Map<String, Value> = serde_json::from_slice(&bytes)
                    .map_err(|_| invalid_data("context terminal is malformed"))?;
                if crate::model_continuation::settled_witness(&row) == *witness {
                    covered = count + 1;

                    continuation_status = "available";
                }
            }
        }
    }
    let selected = &headers[covered..];
    let start = match after_turn_id {
        Some(cursor) => selected.iter().position(|header| header.get("turnId").and_then(Value::as_str) == Some(cursor))
            .map(|position| position + 1)
            .filter(|position| *position < selected.len())
            .ok_or_else(|| invalid_input("context page cursor is outside the selected suffix"))?,
        None => 0,
    };
    let continuation = if covered > 0 || cached.as_ref().is_some_and(|value| value.get("historyProof").is_none()) {
        cached
    } else { None };
    let continuation_bytes = continuation.as_ref().map_or(0, |value| serde_json::to_vec(value)
        .map(|encoded| encoded.len()).unwrap_or(crate::model_continuation::MAX_PROJECTION_BYTES));
    let mut turns = Vec::new();
    let mut page_bytes = 0_usize;
    let mut next_turn_id = None;
    // Reserve the actual continuation plus metadata; keep the baseline
    // budget consistent with the SQL authority and the wire contract.
    let page_limit = (crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES
        - crate::model_continuation::MAX_PROJECTION_BYTES - 128 * 1024)
        .min(crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES
            .saturating_sub(continuation_bytes + 128 * 1024));
    for header in &selected[start..] {
        let id = header.get("turnId").and_then(Value::as_str)
            .ok_or_else(|| invalid_data("context header lacks identity"))?;
        let bytes = get(database, transaction, conversation_id, id)?
            .ok_or_else(|| invalid_data("context Turn disappeared"))?;
        if !turns.is_empty() && page_bytes.saturating_add(bytes.len()) > page_limit {
            next_turn_id = turns.last().and_then(|row: &Value| row.get("turnId"))
                .and_then(Value::as_str).map(str::to_owned);
            break;
        }
        page_bytes = page_bytes.saturating_add(bytes.len());
        turns.push(serde_json::from_slice::<Value>(&bytes)
            .map_err(|_| invalid_data("context Turn is malformed"))?);
    }
    let eligible = turns.iter().all(|row| match row.get("projection")
            .and_then(|value| value.get("attachments")) {
        None | Some(Value::Null) => true,
        Some(Value::Array(refs)) => refs.is_empty(),
        _ => false,
    });
    encode_response(&json!({
        "supported": true, "covered": covered, "turns": turns,
        "nextTurnId": next_turn_id,
        "continuation": continuation,
        "continuationStatus": continuation_status,
        "historyProof": {"version": 2, "count": headers.len() - 1,
                          "digest": format!("{:x}", digest.finalize())},
        "proofEligible": eligible,
    })).map(Some)
}
