//! Live-edit byte publication and narrowly evidenced legacy steer recovery.
//! The Turn owner retains scope, identity, projection and transaction fences;
//! this module never rewrites user content or accepts unexplained size drift.
use super::*;

pub(super) fn refresh_live_projection(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    turn: &Map<String, Value>,
    projection: &Map<String, Value>,
) -> io::Result<()> {
    if !matches!(turn.get("status").and_then(Value::as_str), Some("pending" | "running")) {
        return Ok(());
    }
    let attempt_id = turn.get("currentAttemptId").and_then(Value::as_str)
        .ok_or_else(|| invalid_data("live projection attempt identity is missing"))?;
    let attempt = load_attempt_for_update(database, transaction, attempt_id)?
        .ok_or_else(|| invalid_data("live projection attempt is missing"))?;
    if !attempt_is_live(&attempt.document)
        || turn.get("conversationId").and_then(Value::as_str) != Some(attempt.conversation_id.as_str())
        || turn.get("turnId") != attempt.document.get("turnId")
    {
        return Err(invalid_data("live projection attempt target differs"));
    }
    let created_at = attempt.document.get("createdAt").and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("attempt recovery timestamp is malformed"))?;
    let bytes = serde_json::to_vec(projection)
        .map_err(|_| invalid_data("turn projection cannot be encoded"))?.len();
    put_legacy_recovery_index(database, transaction, created_at, &attempt.document, bytes)
}

/// The shared constructor also recognizes exactly the pending records the old
/// steer writer appended. Preview length is the existing steer wire contract.
pub(super) fn pending_steer_record(command_id: &str, text: &str, author: Option<&Value>) -> Value {
    let mut record = json!({
        "blockId": format!("injection:user-steer:{command_id}"),
        "commandId": command_id,
        "count": 1,
        "previews": [{"text": text.chars().take(1200).collect::<String>()}],
        "deliveryState": "pending"
    });
    if let Some(author) = author {
        record.as_object_mut().unwrap().insert("author".to_owned(), author.clone());
    }
    record
}

/// Older steer commits retired the head and appended records without updating
/// the recovery size. Prove precisely that suffix against the latest retained
/// event's revision AND independent byte witness. Missing evidence, other
/// mutations, malformed records and arbitrary index corruption stay fenced.
pub(super) fn witnessed_legacy_steer_suffix(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    attempt_id: &str,
    loaded: &LoadedTurnForAttempt,
    projection: &Map<String, Value>,
    indexed_bytes: usize,
) -> io::Result<bool> {
    if loaded.projection_head.is_some() { return Ok(false); }
    let Some(records) = projection.get("_userSteerInjects").and_then(Value::as_array) else {
        return Ok(false);
    };
    let head = decode_u64(database.entity_get(transaction,
        &attempt_event_head_key(transaction, attempt_id)?)?, "attempt event head is malformed")?;
    if head == 0 { return Ok(false); }
    let Some((bytes, _)) = crate::versioned_document::get_value_with_version_bounded(
        database, transaction, &attempt_event_key(transaction, attempt_id, head)?,
        ATTEMPT_EVENT_DOCUMENT_IDENTITY, &format!("{attempt_id}\u{1f}{head:020}"),
        MAX_ATTEMPT_EVENT_DOCUMENT_BYTES,
    )? else { return Ok(false); };
    let event: Value = serde_json::from_slice(&bytes)
        .map_err(|_| invalid_data("attempt recovery event is malformed"))?;
    if event.get("attemptId").and_then(Value::as_str) != Some(attempt_id)
        || event.get("seq").and_then(Value::as_u64) != Some(head)
        || event.get("conversationId") != loaded.document.get("conversationId")
        || event.get("turnId") != loaded.document.get("turnId")
    {
        return Err(invalid_data("attempt recovery event identity differs"));
    }
    let Some(event_revision) = event.get("projectionRevision").and_then(Value::as_u64) else {
        return Ok(false);
    };
    if event["payload"]["projectionBytes"].as_u64() != Some(indexed_bytes as u64)
        || event["payload"]["projectionPatch"]["targetRevision"].as_u64() != Some(event_revision)
    { return Ok(false); }
    let Some(added) = loaded.document.get("projectionRevision").and_then(Value::as_u64)
        .and_then(|revision| revision.checked_sub(event_revision))
        .and_then(|added| usize::try_from(added).ok())
        .filter(|added| *added > 0 && *added <= records.len())
    else { return Ok(false); };
    let retained = records.len() - added;
    let mut identities = BTreeSet::new();
    for (index, record) in records.iter().enumerate() {
        let Some(command_id) = record.get("commandId").and_then(Value::as_str)
            .filter(|value| !value.is_empty()) else { return Ok(false); };
        if !identities.insert(command_id) { return Ok(false); }
        if index < retained { continue; }
        let Some(text) = record.get("previews").and_then(Value::as_array)
            .and_then(|previews| previews.first()).and_then(|preview| preview.get("text"))
            .and_then(Value::as_str) else { return Ok(false); };
        if record.get("author").is_some_and(|author| !author.is_object())
            || *record != pending_steer_record(command_id, text, record.get("author"))
        { return Ok(false); }
    }
    // This private candidate is only evidence. Recovery always keeps the full
    // committed projection, including every appended directive.
    let mut previous = projection.clone();
    previous.insert("_userSteerInjects".to_owned(), json!(&records[..retained]));
    let size_matches = |value: &Map<String, Value>| -> io::Result<bool> {
        Ok(serde_json::to_vec(value)
            .map_err(|_| invalid_data("turn recovery projection cannot be encoded"))?.len() == indexed_bytes)
    };
    if size_matches(&previous)? { return Ok(true); }
    if retained == 0 {
        previous.remove("_userSteerInjects");
        return size_matches(&previous);
    }
    Ok(false)
}
