//! Per-Turn usage rows of one conversation's main lane
//! (`conversation.turn_usage`, docs/STORAGE.md "Per-Turn usage projection").
//!
//! Entry point: main_lane. The compact activity index carries every main-lane
//! Turn's time (its projection `timestamp` read tolerantly, else its creation
//! time: the time `conversation.activity_dates` counts), so only Turns inside
//! the requested window are materialized, one at a time, and each is dropped
//! after its usage-bearing projection fields are copied. Authorities created
//! before that index read each legacy Turn once, like the activity fallback.
//! The response grows with priced Turns, never with transcript bytes; the
//! stored `usage` and `apiRounds` are only read.
use super::*;

/// Projection fields that attribute a Turn's usage to a model and provider.
const USAGE_ATTRIBUTION_FIELDS: [&str; 5] =
    ["model", "preset", "effort", "provider_id", "providerId"];

pub(crate) struct MainLaneUsage {
    pub message_count: u64,
    pub rows: Vec<Value>,
    pub title_fallback: String,
}

pub(crate) fn in_window(time: i128, window: (i64, i64)) -> bool {
    i128::from(window.0) <= time && time < i128::from(window.1)
}

/// First `characters` Unicode scalar values of a string `content`, else "".
pub(crate) fn content_prefix(message: &Map<String, Value>, characters: usize) -> String {
    message
        .get("content")
        .and_then(Value::as_str)
        .map(|content| content.chars().take(characters).collect())
        .unwrap_or_default()
}

/// One usage row from a public message or Turn projection, or None when it
/// carries no non-empty usage object.
pub(crate) fn usage_row(
    message: &Map<String, Value>,
    index: u64,
    timestamp: Option<Value>,
) -> Option<Value> {
    let usage = message
        .get("usage")
        .filter(|usage| usage.as_object().is_some_and(|usage| !usage.is_empty()))?;
    let mut row = Map::new();
    row.insert("index".to_owned(), Value::from(index));
    row.insert("usage".to_owned(), usage.clone());
    if let Some(timestamp) = timestamp {
        row.insert("timestamp".to_owned(), timestamp);
    }
    for field in USAGE_ATTRIBUTION_FIELDS {
        if let Some(value) = message.get(field) {
            row.insert(field.to_owned(), value.clone());
        }
    }
    Some(Value::Object(row))
}

fn timed_out() -> io::Error {
    io::Error::new(io::ErrorKind::TimedOut, "turn usage read deadline elapsed")
}

/// Push the usage row of one materialized Turn when its time is in the window.
fn push_turn_row(
    rows: &mut Vec<Value>,
    turn: &Map<String, Value>,
    index: u64,
    window: (i64, i64),
) -> io::Result<()> {
    let projection = turn
        .get("projection")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid_data("turn projection is malformed"))?;
    let time = effective_activity_timestamp(
        projection.get("timestamp"),
        turn.get("createdAt").and_then(Value::as_u64).unwrap_or(0),
    );
    // Zero is "no usable time": the caller places such a row by position.
    if time != 0 && !in_window(time, window) {
        return Ok(());
    }
    let timestamp = i64::try_from(time)
        .map_err(|_| invalid_data("turn usage time is outside the window domain"))?;
    if let Some(row) = usage_row(projection, index, Some(Value::from(timestamp))) {
        rows.push(row);
    }
    Ok(())
}

/// `(lane sequence, ordinal)` of every indexed Turn whose time is in the
/// window, or None when this lane predates the activity index.
fn indexed_selection(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    prefix: &[u8],
    lane_count: u64,
    window: (i64, i64),
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<Option<Vec<(u64, u64)>>> {
    let (mut start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_ACTIVITY_INDEX_NAMESPACE,
        prefix,
    )?;
    let mut selected = Vec::new();
    let mut indexed_rows = 0_u64;
    for page in 0..MAX_SCAN_PAGES {
        if deadline_elapsed() {
            return Err(timed_out());
        }
        let rows = database.entity_scan(transaction, &start, &end, INDEX_PAGE_ROWS)?;
        if rows.is_empty() {
            break;
        }
        let row_count = rows.len();
        let continuation = after_key(
            &rows.last().expect("nonempty Turn activity page").0,
            transaction,
            TURN_ACTIVITY_INDEX_NAMESPACE,
        )?;
        for (key, encoded) in rows {
            let ordinal = key
                .key_bytes()
                .strip_prefix(prefix)
                .and_then(|suffix| <[u8; 8]>::try_from(suffix).ok())
                .map(u64::from_be_bytes)
                .ok_or_else(|| invalid_data("Turn activity index key is malformed"))?;
            let time = decode_activity_index_value(&encoded)?;
            if time == 0 || in_window(time, window) {
                selected.push((indexed_rows, ordinal));
            }
            indexed_rows += 1;
        }
        if row_count < INDEX_PAGE_ROWS {
            break;
        }
        if page + 1 == MAX_SCAN_PAGES {
            return Err(crate::resource_exhaustion::domain_bound(
                "Turn usage scan exceeds its page budget",
            ));
        }
        start = continuation;
    }
    if indexed_rows == lane_count {
        return Ok(Some(selected));
    }
    if indexed_rows != 0 {
        return Err(invalid_data(
            "Turn activity timestamp index count is inconsistent",
        ));
    }
    Ok(None)
}

/// Every Turn of a lane without activity index rows, read once in order.
fn legacy_rows(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    prefix: &[u8],
    window: (i64, i64),
    rows: &mut Vec<Value>,
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<u64> {
    let (mut start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_LANE_INDEX_NAMESPACE,
        prefix,
    )?;
    let mut position = 0_u64;
    for page in 0..MAX_SCAN_PAGES {
        let entries = database.entity_scan(transaction, &start, &end, INDEX_PAGE_ROWS)?;
        if entries.is_empty() {
            break;
        }
        let entry_count = entries.len();
        let continuation = after_key(
            &entries.last().expect("nonempty Turn lane page").0,
            transaction,
            TURN_LANE_INDEX_NAMESPACE,
        )?;
        for (_, stored) in entries {
            if deadline_elapsed() {
                return Err(timed_out());
            }
            let turn = materialize_turn(database, transaction, &stored)?;
            push_turn_row(rows, &turn, position, window)?;
            position += 1;
        }
        if entry_count < INDEX_PAGE_ROWS {
            break;
        }
        if page + 1 == MAX_SCAN_PAGES {
            return Err(crate::resource_exhaustion::domain_bound(
                "legacy Turn usage scan exceeds its page budget",
            ));
        }
        start = continuation;
    }
    Ok(position)
}

/// Usage rows of the main-lane Turns whose time is in `window`, or None when
/// the conversation has no main-lane Turn (a frozen transcript or none).
pub(crate) fn main_lane(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    window: (i64, i64),
    title_fallback_characters: usize,
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<Option<MainLaneUsage>> {
    let prefix = lane_prefix(conversation_id, "main")?;
    let (lane_start, lane_end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_LANE_INDEX_NAMESPACE,
        &prefix,
    )?;
    let Some((_, first)) = database
        .entity_scan(transaction, &lane_start, &lane_end, 1)?
        .into_iter()
        .next()
    else {
        return Ok(None);
    };
    let lane_state = read_lane_state(database, transaction, conversation_id, "main")?;
    let lane_count = lane_count_of(
        database,
        transaction,
        conversation_id,
        "main",
        lane_state.as_ref(),
    )?;
    if lane_count > MAX_LIST_ROWS as u64 {
        return Err(crate::resource_exhaustion::domain_bound(
            "Turn usage scan exceeds 10000 rows per conversation",
        ));
    }
    let title_fallback = if title_fallback_characters == 0 {
        String::new()
    } else {
        let turn = materialize_turn(database, transaction, &first)?;
        let projection = turn
            .get("projection")
            .and_then(Value::as_object)
            .ok_or_else(|| invalid_data("turn projection is malformed"))?;
        content_prefix(projection, title_fallback_characters)
    };
    let mut rows = Vec::new();
    // A lane without a maintained count predates the index: read it exactly.
    let selection = if lane_count == 0 {
        None
    } else {
        indexed_selection(
            database,
            transaction,
            &prefix,
            lane_count,
            window,
            deadline_elapsed,
        )?
    };
    let message_count = match selection {
        Some(selection) => {
            for (index, ordinal) in selection {
                if deadline_elapsed() {
                    return Err(timed_out());
                }
                let stored = database
                    .entity_get(
                        transaction,
                        &lane_index_key(transaction, conversation_id, "main", ordinal)?,
                    )?
                    .ok_or_else(|| invalid_data("Turn activity index has no lane entry"))?;
                let turn = materialize_turn(database, transaction, &stored)?;
                push_turn_row(&mut rows, &turn, index, window)?;
            }
            lane_count
        }
        None => {
            let read = legacy_rows(
                database,
                transaction,
                &prefix,
                window,
                &mut rows,
                deadline_elapsed,
            )?;
            if lane_count != 0 && read != lane_count {
                return Err(invalid_data("Turn lane count is inconsistent"));
            }
            read
        }
    };
    Ok(Some(MainLaneUsage {
        message_count,
        rows,
        title_fallback,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn append(database: &AuthorityDatabase, write: &mut AuthorityTransaction,
              ordinal: u64, actor: &str, projection: Value, created_at_ms: u64) {
        append_settled(
            database,
            write,
            &AppendSettledRequest {
                conversation_id: "usage-conversation".to_owned(),
                actor: actor.to_owned(),
                status: "completed".to_owned(),
                projection_json: serde_json::to_vec(&projection).unwrap(),
                projection_timestamp: None,
                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: format!("usage-command-{ordinal}"),
                kind: "fixture".to_owned(),
                run_id: String::new(),
                turn_id: format!("usage-turn-{ordinal}"),
                attempt_id: None,
                created_at_ms,
                committed_at_ms: created_at_ms,
                defaults: TurnDefaults {
                    allow_create: false,
                    title: String::new(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms,
                },
            },
        )
        .unwrap();
    }

    fn read(database: &AuthorityDatabase) -> io::Result<Option<MainLaneUsage>> {
        let mut read = database.begin(7, 11).unwrap();
        main_lane(database, &mut read, "usage-conversation", (150, 280), 5, &mut || false)
    }

    #[test]
    fn main_lane_usage_selects_window_rows_without_bodies_and_legacy_lanes_match() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut write = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::create(
            &database,
            &mut write,
            &crate::conversation_header::CreateRequest {
                conversation_id: "usage-conversation".to_owned(),
                title: String::new(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 1,
                updated_at_ms: 1,
                committed_at_ms: 1,
            },
        )
        .unwrap();
        append(&database, &mut write, 0, "human",
               json!({"content": "first question", "timestamp": 100}), 100);
        append(&database, &mut write, 1, "assistant", json!({
            "content": "x".repeat(20_000), "timestamp": 200,
            "usage": {"prompt_tokens": 7}, "apiRounds": [{"round": 1}],
            "model": "model-x", "providerId": "provider-y",
        }), 200);
        append(&database, &mut write, 2, "assistant",
               json!({"timestamp": 300, "usage": {"prompt_tokens": 9}}), 210);
        // No projection time: the Turn's creation time places it.
        append(&database, &mut write, 3, "assistant",
               json!({"usage": {"prompt_tokens": 11}}), 260);
        database.commit(write).unwrap();
        let expected = json!([
            {"index": 1, "timestamp": 200, "usage": {"prompt_tokens": 7},
             "model": "model-x", "providerId": "provider-y"},
            {"index": 3, "timestamp": 260, "usage": {"prompt_tokens": 11}},
        ]);

        let indexed = read(&database).unwrap().unwrap();
        assert_eq!(indexed.message_count, 4);
        assert_eq!(indexed.title_fallback, "first");
        assert_eq!(Value::Array(indexed.rows), expected);

        let mut remove = database.begin(7, 11).unwrap();
        for ordinal in 0..4 {
            let key = activity_index_key(&remove, "usage-conversation", ordinal).unwrap();
            database.entity_delete(&mut remove, key).unwrap();
        }
        database.commit(remove).unwrap();
        let legacy = read(&database).unwrap().unwrap();
        assert_eq!(legacy.message_count, 4);
        assert_eq!(Value::Array(legacy.rows), expected);

        let mut partial = database.begin(7, 11).unwrap();
        let key = activity_index_key(&partial, "usage-conversation", 0).unwrap();
        database
            .entity_put(&mut partial, key, encode_activity_index_value(100))
            .unwrap();
        database.commit(partial).unwrap();
        assert_eq!(
            read(&database).err().map(|error| error.kind()),
            Some(io::ErrorKind::InvalidData)
        );
    }
}
