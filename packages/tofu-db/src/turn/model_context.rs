//! Main-lane context selection with revision-proven text continuations.
//! Reads compact authority indexes before materializing only the new suffix;
//! a Turn with more than one projection page travels as its page-0 head.
//! Header walks stream one engine index page at a time: the v2 digest covers
//! every header through the target while only the response page is retained,
//! so lane length is never a ceiling (docs/TOFUDB_CONTEXT_READ.md).
use super::*;
mod frontier;
mod sha_state;
use sha_state::ShaState;
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

fn cursor_outside() -> io::Error {
    invalid_input("context page cursor is outside the selected suffix")
}

/// One compact main-lane header; `target` marks the exact target Turn.
struct Header {
    key: EntityKey,
    value: Value,
    target: bool,
}

impl Header {
    fn is(&self, turn_id: Option<&str>) -> bool {
        turn_id.is_some() && self.value.get("turnId").and_then(Value::as_str) == turn_id
    }
}

/// Ordered main-lane headers from `start` (the lane head when absent) through
/// the exact target. It holds one engine index page and has no page-count
/// cap: the lane's own history is the only length.
struct HeaderWalk {
    next: EntityKey,
    end: EntityKey,
    target_key: EntityKey,
    page: std::vec::IntoIter<(EntityKey, Vec<u8>)>,
    finished: bool,
}

impl HeaderWalk {
    fn new(
        transaction: &AuthorityTransaction,
        conversation: &str,
        start: Option<EntityKey>,
        target_ordinal: u64,
    ) -> io::Result<Self> {
        let target_key =
            lane_compaction_index_key(transaction, conversation, "main", target_ordinal)?;
        let next = match start {
            Some(key) => key,
            None => {
                EntityKey::prefix_range(
                    transaction.tenant_id(),
                    transaction.owner_user_id(),
                    TURN_LANE_COMPACTION_INDEX_NAMESPACE,
                    &lane_prefix(conversation, "main")?,
                )?
                .0
            }
        };
        let end = target_key.clone().exact_range()?.1;
        Ok(Self {
            next,
            end,
            target_key,
            page: Vec::new().into_iter(),
            finished: false,
        })
    }

    /// The next header, or None after the target. Exhausting the range before
    /// the target means the target moved within this snapshot.
    fn next(
        &mut self,
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
        target_id: &str,
    ) -> io::Result<Option<Header>> {
        if self.finished {
            return Ok(None);
        }
        if self.page.as_slice().is_empty() {
            let entries = database.entity_scan(transaction, &self.next, &self.end, INDEX_PAGE_ROWS)?;
            let Some((last, _)) = entries.last() else {
                return Err(invalid_data("Model context target order changed"));
            };
            self.next = after_key(last, transaction, TURN_LANE_COMPACTION_INDEX_NAMESPACE)?;
            self.page = entries.into_iter();
        }
        let (key, bytes) = self.page.next().expect("a scanned header page is nonempty");
        let value: Value = serde_json::from_slice(&bytes)
            .map_err(|_| invalid_data("context header is malformed"))?;
        #[cfg(test)]
        SCANNED_HEADERS.with(|count| count.set(count.get() + 1));
        let target = value.get("turnId").and_then(Value::as_str) == Some(target_id);
        if target {
            if key != self.target_key {
                return Err(invalid_data("Model context target order changed"));
            }
            self.finished = true;
        }
        Ok(Some(Header { key, value, target }))
    }
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
                if error.io_error_kind().is_some_and(crate::resource_exhaustion::is_refusal_kind) {
                    crate::resource_exhaustion::domain_bound(
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

/// A Turn whose `turn.projection.page` pages are more than one travels as
/// `{"pagedTurn": head}`; the client reads its pages pinned to the head's
/// projection revision (docs/STORAGE.md, "Paged Turn storage", Decision 6).
fn context_row(turn: Value, bytes: usize) -> io::Result<(Value, usize)> {
    let (turn, paged) = super::projection_page::paged_head(turn)?;
    if !paged {
        return Ok((turn, bytes));
    }
    let mut row = Map::new();
    row.insert("pagedTurn".to_owned(), turn);
    crate::response_json::normalize_value_with_size(
        Value::Object(row),
        crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES,
    )
    .map_err(|_| invalid_data("context Turn head cannot be encoded"))
}

/// The Turn record of a response row; a paged Turn's row carries its head.
fn row_turn(row: &Value) -> &Value {
    row.get("pagedTurn").unwrap_or(row)
}

fn response(value: Value) -> io::Result<crate::response_json::EncodedResponse> {
    let encoded = crate::response_json::EncodedResponse::from_value(value)
        .map_err(|_| invalid_data("turn response cannot be encoded"))?;
    if encoded.len() > crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES {
        return Err(crate::resource_exhaustion::domain_bound(
            "turn response exceeds its bounded streamed payload",
        ));
    }
    Ok(encoded)
}

/// One bounded response page of the selected suffix, filled in lane order
/// after the caller's cursor. The target was read in this snapshot and is
/// moved in once.
struct Page<'a> {
    conversation: &'a str,
    target_id: &'a str,
    target: Option<(Value, usize)>,
    after: Option<&'a str>,
    limit: usize,
    turns: Vec<Value>,
    bytes: usize,
    full: bool,
}

impl<'a> Page<'a> {
    fn new(
        conversation: &'a str,
        target_id: &'a str,
        target: Option<(Value, usize)>,
        after: Option<&'a str>,
        continuation: Option<&Value>,
    ) -> Self {
        let continuation_bytes = continuation.map_or(0, |value| {
            serde_json::to_vec(value)
                .map(|encoded| encoded.len())
                .unwrap_or(crate::model_continuation::MAX_PROJECTION_BYTES)
        });
        // Reserve the actual continuation plus metadata; keep the baseline
        // budget consistent with the SQL authority and the wire contract.
        let limit = (crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES
            - crate::model_continuation::MAX_PROJECTION_BYTES
            - 128 * 1024)
            .min(
                crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES
                    .saturating_sub(continuation_bytes + 128 * 1024),
            );
        Self {
            conversation,
            target_id,
            target,
            after,
            limit,
            turns: Vec::new(),
            bytes: 0,
            full: false,
        }
    }

    /// Consume the next selected header: pass the cursor, then materialize
    /// Turns until the first one that no longer fits ends the page.
    fn offer(
        &mut self,
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
        header: &Header,
    ) -> io::Result<()> {
        if self.after.is_some() {
            if header.is(self.after) {
                if header.target {
                    return Err(cursor_outside());
                }
                self.after = None;
            }
            return Ok(());
        }
        if self.full {
            return Ok(());
        }
        let id = header
            .value
            .get("turnId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("context header lacks identity"))?;
        let (value, bytes) = if id == self.target_id {
            self.target
                .take()
                .ok_or_else(|| invalid_data("context target repeats"))?
        } else {
            read_turn(database, transaction, self.conversation, id)?
                .ok_or_else(|| invalid_data("context Turn disappeared"))?
        };
        let (value, bytes) = context_row(value, bytes)?;
        if !self.turns.is_empty() && self.bytes.saturating_add(bytes) > self.limit {
            self.full = true;
            return Ok(());
        }
        self.bytes = self.bytes.saturating_add(bytes);
        self.turns.push(value);
        Ok(())
    }

    fn respond(
        self,
        covered: u64,
        proof_count: u64,
        digest: String,
        continuation_status: &str,
        continuation: Option<Value>,
    ) -> io::Result<crate::response_json::EncodedResponse> {
        if self.after.is_some() {
            return Err(cursor_outside());
        }
        let next_turn_id = if self.full {
            self.turns
                .last()
                .and_then(|row| row_turn(row).get("turnId"))
                .and_then(Value::as_str)
                .map(str::to_owned)
        } else {
            None
        };
        let eligible = self.turns.iter().all(|row| {
            match row_turn(row)
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
        result["turns"] = Value::Array(self.turns);
        result["continuation"] = continuation.unwrap_or(Value::Null);
        response(result)
    }
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
    let target_ordinal = target
        .get("ordinal")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("context target ordinal is malformed"))?;
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
        target_ordinal,
        after_turn_id,
        &mut target,
        &mut cached,
    )? {
        return Ok(Some(response));
    }
    read_uncertified(
        database,
        transaction,
        conversation_id,
        turn_id,
        target_ordinal,
        replay,
        after_turn_id,
        target,
        cached,
    )
    .map(Some)
}

/// Full-prefix validation: one streamed digest walk over every header. The
/// checkpoint anchor (its proof count) decides coverage when the walk reaches
/// it; uncovered history is paged as it streams by. Only when a stale anchor
/// leaves the lane head uncovered is the page walked again, from its start.
#[allow(clippy::too_many_arguments)]
fn read_uncertified(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    target_ordinal: u64,
    replay: bool,
    after_turn_id: Option<&str>,
    mut target: Option<(Value, usize)>,
    cached: Option<Value>,
) -> io::Result<crate::response_json::EncodedResponse> {
    let proof = cached.as_ref().and_then(|value| value.get("historyProof"));
    let version2 = proof
        .and_then(|value| value.get("version"))
        .and_then(Value::as_u64)
        == Some(2);
    let proof_digest = proof
        .and_then(|value| value.get("digest"))
        .and_then(Value::as_str)
        .map(str::to_owned);
    let terminal = cached
        .as_ref()
        .and_then(|value| value.get("turns"))
        .and_then(Value::as_array)
        .filter(|values| values.len() == 1)
        .and_then(|values| values.first())
        .cloned();
    // Lane index of the checkpoint terminal; it can cover a prefix only when
    // the walk reaches it before the target.
    let anchor = proof
        .and_then(|value| value.get("count"))
        .and_then(Value::as_u64)
        .filter(|_| version2 && terminal.is_some());
    let mut continuation_status = if !replay {
        "replay_disabled"
    } else if cached.is_none() {
        "checkpoint_missing"
    } else if proof.is_none() {
        "legacy_checkpoint"
    } else if !version2 {
        "legacy_proof"
    } else {
        "checkpoint_not_before_target"
    };
    let legacy = cached
        .as_ref()
        .is_some_and(|value| value.get("historyProof").is_none());
    let mut page = anchor.is_none().then(|| {
        Page::new(
            conversation_id,
            turn_id,
            target.take(),
            after_turn_id,
            cached.as_ref().filter(|_| legacy),
        )
    });
    let mut walk = HeaderWalk::new(transaction, conversation_id, None, target_ordinal)?;
    let mut state = ShaState::default();
    let mut index = 0_u64;
    let mut covered = 0_u64;
    let mut cursor_key = None;
    while let Some(header) = walk.next(database, transaction, turn_id)? {
        match page.as_mut() {
            Some(page) => page.offer(database, transaction, &header)?,
            None if header.is(after_turn_id) => {
                if header.target {
                    return Err(cursor_outside());
                }
                cursor_key = Some(header.key.clone());
            }
            None => {}
        }
        if header.target {
            break;
        }
        if anchor == Some(index) {
            continuation_status = "history_changed";
            if proof_digest.as_deref() == Some(state.digest().as_str()) {
                continuation_status = "terminal_changed";
                let witness = terminal.as_ref().expect("an anchor has its terminal witness");
                if header.value.get("turnId") == witness.get(0) {
                    let final_id = witness
                        .get(0)
                        .and_then(Value::as_str)
                        .ok_or_else(|| invalid_data("context witness is malformed"))?;
                    if let Some((value, _)) =
                        read_turn(database, transaction, conversation_id, final_id)?
                    {
                        let row = value
                            .as_object()
                            .ok_or_else(|| invalid_data("context terminal is malformed"))?;
                        if crate::model_continuation::settled_witness(row) == *witness {
                            covered = index + 1;
                            continuation_status = "available";
                        }
                    }
                }
            }
            if covered > 0 {
                if cursor_key.is_some() {
                    return Err(cursor_outside());
                }
                page = Some(Page::new(
                    conversation_id,
                    turn_id,
                    target.take(),
                    after_turn_id,
                    cached.as_ref(),
                ));
            }
        }
        state.update(&header_encoding(&header.value)?)?;
        index += 1;
    }
    let page = match page {
        Some(page) => page,
        None => {
            // A proof-bearing checkpoint is never legacy: no continuation is returned.
            let start = match after_turn_id {
                Some(_) => Some(after_key(
                    &cursor_key.ok_or_else(cursor_outside)?,
                    transaction,
                    TURN_LANE_COMPACTION_INDEX_NAMESPACE,
                )?),
                None => None,
            };
            let mut page = Page::new(conversation_id, turn_id, target.take(), None, None);
            let mut rewalk = HeaderWalk::new(transaction, conversation_id, start, target_ordinal)?;
            while !page.full {
                let Some(header) = rewalk.next(database, transaction, turn_id)? else {
                    break;
                };
                page.offer(database, transaction, &header)?;
            }
            page
        }
    };
    let continuation = if covered > 0 || legacy { cached } else { None };
    page.respond(covered, index, state.digest(), continuation_status, continuation)
}

/// A certificate authenticates only headers before its terminal. We still
/// materialize and verify that terminal and every new header from one snapshot.
#[allow(clippy::too_many_arguments)]
fn read_certified_suffix(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation: &str,
    target_id: &str,
    target_ordinal: u64,
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
    let start = lane_compaction_index_key(transaction, conversation, "main", cert.ordinal)?;
    let mut walk = HeaderWalk::new(transaction, conversation, Some(start), target_ordinal)?;
    let Some(first) = walk.next(database, transaction, target_id)? else {
        return Ok(None);
    };
    if !first.is(Some(cert.terminal_id.as_str())) {
        return Ok(None);
    }
    let mut state = cert.state;
    state.update(&header_encoding(&first.value)?)?;
    let mut count = cert.count + 1;
    let mut page = Page::new(conversation, target_id, target.take(), after, Some(checkpoint));
    while let Some(header) = walk.next(database, transaction, target_id)? {
        page.offer(database, transaction, &header)?;
        if !header.target {
            state.update(&header_encoding(&header.value)?)?;
            count += 1;
        }
    }
    page.respond(cert.count + 1, count, state.digest(), "available", cached.take())
        .map(Some)
}

#[cfg(test)]
mod tests;
