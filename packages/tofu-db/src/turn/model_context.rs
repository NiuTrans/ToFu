//! Main-lane context selection with revision-proven text continuations.
//! Reads compact authority indexes before materializing only the new suffix.
use super::*;
use sha2::{Digest, Sha256};
mod frontier;
mod sha_state;
pub(crate) use frontier::{before_header_write, certify as certify_frontier};

#[cfg(test)]
thread_local! { static SCANNED_HEADERS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) }; }

fn header_encoding(header: &Value) -> io::Result<Vec<u8>> {
    let revision = header
        .get("modelContextRevision")
        .or_else(|| header.get("projectionRevision"))
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("context header revision is malformed"))?;
    let mut bytes = serde_json::to_vec(&json!([
        header.get("turnId"),
        header.get("parentTurnId"),
        header.get("status"),
        header.get("currentAttemptId"),
        revision
    ]))
    .map_err(|_| invalid_data("context header cannot be encoded"))?;
    bytes.push(b'\n');
    Ok(bytes)
}

/// Bounded ordered scan through the exact target. A certified prefix supplies
/// its checked count and ordinal; absence starts at the beginning of the lane.
fn scan_headers(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation: &str,
    from: Option<u64>,
    target_ordinal: u64,
    target_id: &str,
    prefix_count: usize,
) -> io::Result<Option<Vec<Value>>> {
    let prefix = lane_prefix(conversation, "main")?;
    let (begin, _) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_LANE_COMPACTION_INDEX_NAMESPACE,
        &prefix,
    )?;
    let mut start = match from {
        Some(ordinal) => lane_compaction_index_key(transaction, conversation, "main", ordinal)?,
        None => begin,
    };
    let end = lane_compaction_index_key(transaction, conversation, "main", target_ordinal)?
        .exact_range()?
        .1;
    let mut headers = Vec::new();
    for _ in 0..MAX_SCAN_PAGES {
        let entries = database.entity_scan(transaction, &start, &end, INDEX_PAGE_ROWS)?;
        if entries.is_empty() {
            break;
        }
        start = after_key(
            &entries.last().unwrap().0,
            transaction,
            TURN_LANE_COMPACTION_INDEX_NAMESPACE,
        )?;
        for (key, bytes) in entries {
            let header: Value = serde_json::from_slice(&bytes)
                .map_err(|_| invalid_data("context header is malformed"))?;
            #[cfg(test)]
            SCANNED_HEADERS.with(|count| count.set(count.get() + 1));
            let found = header.get("turnId").and_then(Value::as_str) == Some(target_id);
            headers.push(header);
            if prefix_count.saturating_add(headers.len()) > MAX_LIST_ROWS {
                return Ok(None);
            }
            if found {
                if key
                    != lane_compaction_index_key(transaction, conversation, "main", target_ordinal)?
                {
                    return Err(invalid_data("Model context target order changed"));
                }
                return Ok(Some(headers));
            }
        }
    }
    Err(invalid_data("Model context target order changed"))
}

fn read_turn(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
) -> io::Result<Option<(Value, usize)>> {
    get_public_value(database, transaction, conversation_id, turn_id)?
        .map(|value| {
            let (value, bytes) = crate::response_json::normalize_value_with_size(
                value,
                crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES,
            )
            .map_err(|error| {
                if error.io_error_kind() == Some(io::ErrorKind::OutOfMemory) {
                    io::Error::new(
                        io::ErrorKind::OutOfMemory,
                        "turn response exceeds its bounded streamed payload",
                    )
                } else {
                    invalid_data("context Turn is malformed")
                }
            })?;
            Ok((value, bytes))
        })
        .transpose()
}

fn response(value: Value) -> io::Result<crate::response_json::EncodedResponse> {
    let encoded = crate::response_json::EncodedResponse::from_value(value)
        .map_err(|_| invalid_data("turn response cannot be encoded"))?;
    if encoded.len() > crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "turn response exceeds its bounded streamed payload",
        ));
    }
    Ok(encoded)
}

pub(crate) fn read(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    replay: bool,
    after_turn_id: Option<&str>,
) -> io::Result<Option<crate::response_json::EncodedResponse>> {
    let Some((target, target_bytes)) = read_turn(database, transaction, conversation_id, turn_id)?
    else {
        return Ok(None);
    };
    if target.get("laneId").and_then(Value::as_str) != Some("main") {
        return response(json!({"supported": false})).map(Some);
    }
    let mut target = Some((target, target_bytes));
    let mut cached: Option<Value> = if replay {
        crate::model_continuation::execute(
            database,
            transaction,
            &crate::model_continuation::Request::Get {
                conversation_id: conversation_id.to_owned(),
                lane_id: "main".to_owned(),
            },
        )?
        .map(|bytes| {
            serde_json::from_slice(&bytes)
                .map_err(|_| invalid_data("context continuation is malformed"))
        })
        .transpose()?
    } else {
        None
    };
    if let Some(response) = read_certified_suffix(
        database,
        transaction,
        conversation_id,
        turn_id,
        after_turn_id,
        &mut target,
        &mut cached,
    )? {
        return Ok(Some(response));
    }
    let target_ordinal = target
        .as_ref()
        .unwrap()
        .0
        .get("ordinal")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("context target ordinal is malformed"))?;
    let Some(headers) = scan_headers(
        database,
        transaction,
        conversation_id,
        None,
        target_ordinal,
        turn_id,
        0,
    )?
    else {
        return response(json!({"supported":false})).map(Some);
    };
    let proof = cached.as_ref().and_then(|value| value.get("historyProof"));
    let prior_count = proof
        .and_then(|value| value.get("count"))
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok());
    let terminal = cached
        .as_ref()
        .and_then(|value| value.get("turns"))
        .and_then(Value::as_array)
        .filter(|values| values.len() == 1)
        .and_then(|values| values.first());
    let usable = proof
        .and_then(|value| value.get("version"))
        .and_then(Value::as_u64)
        == Some(2)
        && prior_count.is_some_and(|count| count < headers.len() - 1)
        && terminal.is_some();
    let mut continuation_status = if !replay {
        "replay_disabled"
    } else if cached.is_none() {
        "checkpoint_missing"
    } else if proof.is_none() {
        "legacy_checkpoint"
    } else if proof
        .and_then(|value| value.get("version"))
        .and_then(Value::as_u64)
        != Some(2)
    {
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
            prior_matches = proof
                .and_then(|value| value.get("digest"))
                .and_then(Value::as_str)
                == Some(format!("{:x}", digest.clone().finalize()).as_str());
        }
        digest.update(header_encoding(header)?);
    }
    let mut covered = 0;
    if prior_matches {
        continuation_status = "terminal_changed";
        let count = prior_count.unwrap();
        let witness = terminal.unwrap();
        if headers[count].get("turnId") == witness.get(0) {
            let final_id = witness
                .get(0)
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_data("context witness is malformed"))?;
            if let Some((value, _)) = read_turn(database, transaction, conversation_id, final_id)? {
                let row = value
                    .as_object()
                    .ok_or_else(|| invalid_data("context terminal is malformed"))?;
                if crate::model_continuation::settled_witness(row) == *witness {
                    covered = count + 1;

                    continuation_status = "available";
                }
            }
        }
    }
    let continuation = if covered > 0
        || cached
            .as_ref()
            .is_some_and(|value| value.get("historyProof").is_none())
    {
        cached
    } else {
        None
    };
    let proof_count = headers.len() - 1;
    finish_response(
        database,
        transaction,
        conversation_id,
        turn_id,
        after_turn_id,
        target,
        &headers[covered..],
        covered,
        proof_count,
        format!("{:x}", digest.finalize()),
        continuation_status,
        continuation,
    )
    .map(Some)
}

#[allow(clippy::too_many_arguments)]
fn finish_response(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    after_turn_id: Option<&str>,
    mut target: Option<(Value, usize)>,
    selected: &[Value],
    covered: usize,
    proof_count: usize,
    digest: String,
    continuation_status: &str,
    continuation: Option<Value>,
) -> io::Result<crate::response_json::EncodedResponse> {
    let start = match after_turn_id {
        Some(cursor) => selected
            .iter()
            .position(|header| header.get("turnId").and_then(Value::as_str) == Some(cursor))
            .map(|position| position + 1)
            .filter(|position| *position < selected.len())
            .ok_or_else(|| invalid_input("context page cursor is outside the selected suffix"))?,
        None => 0,
    };
    let continuation_bytes = continuation.as_ref().map_or(0, |value| {
        serde_json::to_vec(value)
            .map(|encoded| encoded.len())
            .unwrap_or(crate::model_continuation::MAX_PROJECTION_BYTES)
    });
    let mut turns = Vec::new();
    let mut page_bytes = 0_usize;
    let mut next_turn_id = None;
    // Reserve the actual continuation plus metadata; keep the baseline
    // budget consistent with the SQL authority and the wire contract.
    let page_limit = (crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES
        - crate::model_continuation::MAX_PROJECTION_BYTES
        - 128 * 1024)
        .min(
            crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES
                .saturating_sub(continuation_bytes + 128 * 1024),
        );
    for header in &selected[start..] {
        let id = header
            .get("turnId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("context header lacks identity"))?;
        let (value, bytes) = if id == turn_id {
            target
                .take()
                .ok_or_else(|| invalid_data("context target repeats"))?
        } else {
            read_turn(database, transaction, conversation_id, id)?
                .ok_or_else(|| invalid_data("context Turn disappeared"))?
        };
        if !turns.is_empty() && page_bytes.saturating_add(bytes) > page_limit {
            next_turn_id = turns
                .last()
                .and_then(|row: &Value| row.get("turnId"))
                .and_then(Value::as_str)
                .map(str::to_owned);
            break;
        }
        page_bytes = page_bytes.saturating_add(bytes);
        turns.push(value);
    }
    let eligible = turns.iter().all(|row| {
        match row
            .get("projection")
            .and_then(|value| value.get("attachments"))
        {
            None | Some(Value::Null) => true,
            Some(Value::Array(refs)) => refs.is_empty(),
            _ => false,
        }
    });
    let mut result = json!({
        "supported": true, "covered": covered,
        "nextTurnId": next_turn_id,
        "continuationStatus": continuation_status,
        "historyProof": {"version": 2, "count": proof_count, "digest": digest},
        "proofEligible": eligible,
    });
    // Move the private payloads; json! would serialize borrowed expressions to
    // another Value, cloning every tool-result string in the selected history.
    result["turns"] = Value::Array(turns);
    result["continuation"] = continuation.unwrap_or(Value::Null);
    response(result)
}

/// A certificate authenticates only headers before its terminal. We still
/// materialize and verify that terminal and every new header from one snapshot.
fn read_certified_suffix(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation: &str,
    target_id: &str,
    after: Option<&str>,
    target: &mut Option<(Value, usize)>,
    cached: &mut Option<Value>,
) -> io::Result<Option<crate::response_json::EncodedResponse>> {
    let Some(checkpoint) = cached.as_ref() else {
        return Ok(None);
    };
    let Some(cert) = frontier::load(database, transaction, conversation, "main")? else {
        return Ok(None);
    };
    let target_ordinal = target
        .as_ref()
        .unwrap()
        .0
        .get("ordinal")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("context target ordinal is malformed"))?;
    if cert.ordinal >= target_ordinal || !cert.matches(checkpoint) {
        return Ok(None);
    }
    let Some((terminal, _)) = read_turn(database, transaction, conversation, &cert.terminal_id)?
    else {
        return Ok(None);
    };
    let terminal = terminal
        .as_object()
        .ok_or_else(|| invalid_data("context terminal is malformed"))?;
    if terminal.get("ordinal").and_then(Value::as_u64) != Some(cert.ordinal)
        || crate::model_continuation::settled_witness(terminal) != checkpoint["turns"][0]
    {
        return Ok(None);
    }
    let Some(headers) = scan_headers(
        database,
        transaction,
        conversation,
        Some(cert.ordinal),
        target_ordinal,
        target_id,
        cert.count,
    )?
    else {
        return response(json!({"supported":false})).map(Some);
    };
    if headers
        .first()
        .and_then(|h| h.get("turnId"))
        .and_then(Value::as_str)
        != Some(cert.terminal_id.as_str())
    {
        return Ok(None);
    }
    let mut state = cert.state;
    for header in &headers[..headers.len() - 1] {
        state.update(&header_encoding(header)?)?;
    }
    finish_response(
        database,
        transaction,
        conversation,
        target_id,
        after,
        target.take(),
        &headers[1..],
        cert.count + 1,
        cert.count + headers.len() - 1,
        state.digest(),
        "available",
        cached.take(),
    )
    .map(Some)
}

#[cfg(test)]
mod tests;
