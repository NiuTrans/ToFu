//! Bounded native search pages and owner/query/projection-bound continuations.
//! Reads only the disposable projection. A cursor holds keys and word bits,
//! never transcript text; generation changes restart the pending conversation.

use super::*;
use crate::generated_conversation_search::{
    CANDIDATE_SCAN_PAGE, CHUNK_SCAN_BATCH, CHUNK_SCAN_PAGE, CURSOR_BYTES, HEADER_SCAN_BATCH,
    MAX_PAGE_RESULTS, QUERY_CHARACTERS, QUERY_TERMS,
};
use base64::{engine::general_purpose::URL_SAFE, Engine as _};
use serde::{Deserialize, Serialize};
use std::collections::{HashSet, VecDeque};

// Gram-posting candidate filter bounds: at most this many posting-list
// entries read per query and this many gram row lookups per candidate set;
// either exhaustion degrades the set to "unfiltered", never to an incomplete
// answer.
const GRAM_POSTING_READ_BUDGET: usize = 4_096;
const GRAM_SCANS_PER_SET: usize = 8;
const GRAM_ACTIVATE_AFTER_BARREN: usize = 2;

// Query-side gram digests mirror the emission widths: one width per term,
// chosen exactly like the bloom query path (plus width 1 for lone characters,
// which the bloom cannot filter but the posting index can).
fn posting_query_digests(value: &str) -> Vec<(u8, [u8; 32])> {
    let width = match value.chars().count() {
        0 => return Vec::new(),
        1 => 1,
        2 => 2,
        _ => 3,
    };
    let mut digests: Vec<(u8, [u8; 32])> = gram_ranges(value, width)
        .map(|(start, end)| {
            let gram = &value.as_bytes()[start..end];
            // Structural bytes (spaces, punctuation, underscores) discriminate
            // far better than alphanumerics in word-ish text; digits count as
            // ordinary content here, not as separators.
            let rareness = gram
                .iter()
                .filter(|byte| !byte.is_ascii_alphanumeric())
                .count() as u8;
            (rareness, gram_digest(gram))
        })
        .collect();
    // Rarest grams first: a discriminating gram shrinks the intersection in
    // one scan. Digest order ties keep the scan deterministic.
    digests.sort_unstable_by(|left, right| {
        right
            .0
            .cmp(&left.0)
            .then_with(|| left.1.cmp(&right.1))
    });
    digests.dedup_by_key(|(_, digest)| *digest);
    digests
}

/// Intersect the posting lists of every gram of every term. The result is a
/// superset of the conversations any term can appear in (a conversation whose
/// title, id, or a chunk contains a term emitted every one of the term's
/// grams), or None when the read budget forces the caller back to the
/// unfiltered walk. Stable IDs resolve complete generation digests in an
/// owner-scoped dictionary. Exact verification rejects gram false candidates.
fn gram_candidate_set(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    terms: &[&str],
    budget: &mut usize,
    deadline_elapsed: &mut impl FnMut() -> bool,
) -> io::Result<Option<HashSet<u64>>> {
    let mut digests = Vec::new();
    for term in terms {
        digests.extend(posting_query_digests(term));
    }
    digests.sort_unstable_by(|left, right| {
        right
            .0
            .cmp(&left.0)
            .then_with(|| left.1.cmp(&right.1))
    });
    digests.dedup_by_key(|(_, digest)| *digest);
    let mut set: Option<HashSet<u64>> = None;
    for (_, digest) in digests.into_iter().take(GRAM_SCANS_PER_SET) {
        deadline(deadline_elapsed)?;
        let posting_key = key(transaction, GRAM_NAMESPACE, &digest)?;
        let Some(raw) = database.entity_get(transaction, &posting_key)? else {
            return Ok(Some(HashSet::new()));
        };
        let entries = match decode_gram_list(&raw)? {
            GramList::Omni => continue,
            GramList::Entries(entries) => entries,
        };
        if *budget < entries.len() {
            return Ok(None);
        }
        *budget -= entries.len();
        let gram_set: HashSet<u64> = entries.into_iter().collect();
        set = Some(match set {
            None => gram_set,
            Some(mut current) => {
                current.retain(|fingerprint| gram_set.contains(fingerprint));
                current
            }
        });
        // Only an empty intersection short-circuits: the set is computed
        // lazily once a query has gone barren, so spending up to the scan
        // cap to discriminate it fully always beats walking on with a loose
        // candidate list.
        if set.as_ref().unwrap().is_empty() {
            break;
        }
    }
    Ok(set)
}

#[derive(Debug, Serialize)]
pub struct ConversationSearchPage {
    pub items: Vec<ConversationSearchHit>,
    pub next_cursor: Option<String>,
    pub incomplete: bool,
}

#[derive(Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Continuation {
    version: u8,
    binding: String,
    phase: u8,
    after: Option<(u64, String)>,
    pending: Option<Pending>,
}

#[derive(Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Pending {
    generation: String,
    #[serde(with = "cursor_bytes")]
    after: Vec<u8>,
    mask: u32,
    phrase: bool,
    #[serde(with = "cursor_bytes")]
    best: Vec<u8>,
}

mod cursor_bytes {
    use super::*;
    pub fn serialize<S: serde::Serializer>(value: &[u8], serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&URL_SAFE.encode(value))
    }
    pub fn deserialize<'de, D: serde::Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Vec<u8>, D::Error> {
        let text = String::deserialize(deserializer)?;
        let bytes = URL_SAFE.decode(text).map_err(serde::de::Error::custom)?;
        if bytes.len() > MAX_ID_BYTES + 64 {
            return Err(serde::de::Error::custom("search key exceeds bound"));
        }
        Ok(bytes)
    }
}

fn deadline(check: &mut impl FnMut() -> bool) -> io::Result<()> {
    if check() {
        return Err(io::Error::new(
            io::ErrorKind::TimedOut,
            "conversation search deadline elapsed",
        ));
    }
    Ok(())
}

fn normalize(request: &ConversationSearchRequest) -> io::Result<String> {
    if request.limit == 0
        || request.limit > MAX_PAGE_RESULTS
        || request.snippet_radius > 400
        || request.query.contains('\0')
        || request.query.chars().count() > QUERY_CHARACTERS
    {
        return Err(invalid_input("invalid conversation search bounds"));
    }
    let query = request.query.trim().to_lowercase();
    if query.chars().count() > QUERY_CHARACTERS || query.split_whitespace().count() > QUERY_TERMS {
        return Err(invalid_input("invalid conversation search query"));
    }
    Ok(query)
}

fn continuation(value: Option<&str>, binding: String, all_bits: u32) -> io::Result<Continuation> {
    let Some(value) = value.filter(|v| !v.is_empty()) else {
        return Ok(Continuation {
            version: 2,
            binding,
            ..Default::default()
        });
    };
    let bad = || invalid_input("invalid conversation search cursor");
    if value.len() > CURSOR_BYTES {
        return Err(bad());
    }
    let bytes = URL_SAFE.decode(value).map_err(|_| bad())?;
    let state: Continuation = serde_json::from_slice(&bytes).map_err(|_| bad())?;
    if state.version != 2
        || state.binding != binding
        || state.phase > 1
        || state
            .after
            .as_ref()
            .is_some_and(|(_, id)| id.is_empty() || id.len() > MAX_ID_BYTES || id.contains('\0'))
        || state.pending.as_ref().is_some_and(|p| {
            state.after.is_none()
                || p.generation.is_empty()
                || p.generation.len() > MAX_GENERATION_BYTES
                || p.after.len() > MAX_ID_BYTES + 64
                || p.best.len() > MAX_ID_BYTES + 64
                || p.mask & !all_bits != 0
        })
    {
        return Err(bad());
    }
    Ok(state)
}

fn page(
    items: Vec<ConversationSearchHit>,
    state: &Continuation,
    incomplete: bool,
) -> io::Result<ConversationSearchPage> {
    let bytes = serde_json::to_vec(state)
        .map_err(|_| invalid_data("search cursor serialization failed"))?;
    let cursor = URL_SAFE.encode(bytes);
    if cursor.len() > CURSOR_BYTES {
        return Err(exhausted("search cursor exceeds its budget"));
    }
    Ok(ConversationSearchPage {
        items,
        next_cursor: Some(cursor),
        incomplete,
    })
}

fn rank(meta: &TurnMeta, phrase: bool) -> (bool, bool, bool, u64, &str, u32) {
    (
        !phrase,
        meta.role != "user",
        meta.match_field != "originalContent",
        meta.ordinal,
        meta.turn_id.as_str(),
        meta.chunk_index,
    )
}

impl OpenTurnSearchProjection {
    pub fn search(
        &self,
        request: &ConversationSearchRequest,
    ) -> io::Result<Vec<ConversationSearchHit>> {
        self.search_until(request, || false)
    }

    pub fn search_until(
        &self,
        request: &ConversationSearchRequest,
        deadline_elapsed: impl FnMut() -> bool,
    ) -> io::Result<Vec<ConversationSearchHit>> {
        Ok(self
            .search_page_until(request, None, deadline_elapsed)?
            .items)
    }

    pub fn search_page_until(
        &self,
        request: &ConversationSearchRequest,
        cursor: Option<&str>,
        mut deadline_elapsed: impl FnMut() -> bool,
    ) -> io::Result<ConversationSearchPage> {
        let query = normalize(request)?;

        if !self.owner_admitted(request.tenant_id, request.owner_user_id) {
            return Ok(ConversationSearchPage {
                items: Vec::new(),
                next_cursor: None,
                incomplete: false,
            });
        }
        let mut words: Vec<&str> = Vec::new();
        for word in query.split_whitespace() {
            if !words.contains(&word) {
                words.push(word);
            }
        }
        let all_bits = u32::MAX.checked_shr(32 - words.len() as u32).unwrap_or(0);
        let word_blooms: Vec<_> = words.iter().map(|word| bloom_query_bits(word)).collect();
        // One snapshot per query: the search used to open a fresh
        // transaction per scan batch (~40 begins per page, each paying
        // witness setup plus an overlay merge view). A single read
        // transaction is both cheaper and strictly more consistent —
        // cursor paging semantics are unchanged (cursors are key-based,
        // not snapshot-based). The cursor's owner binding rides the same
        // snapshot instead of a separate transaction.
        let mut transaction = self
            .database
            .begin(request.tenant_id, request.owner_user_id)?;
        let binding = digest(
            b"tofu-db:conversation-search:cursor:v2\0",
            &[
                self.database.authority_uuid().as_bytes(),
                &self.owner_binding_in(&mut transaction)?,
                &request.tenant_id.to_be_bytes(),
                &request.owner_user_id.to_be_bytes(),
                query.as_bytes(),
            ],
        );
        let mut state = continuation(cursor, URL_SAFE.encode(binding), all_bits)?;
        let mut items = Vec::new();
        if query.chars().count() < 2 {
            return Ok(ConversationSearchPage {
                items,
                next_cursor: None,
                incomplete: false,
            });
        }
        // The posting index serves candidate sets only in its READY state;
        // every other state (legacy store, post-wipe backfill, budget-degraded)
        // takes the exact legacy full-walk path. Conversation generations
        // whose emission overflowed a write bound are always candidates.
        let mut gram_index_ready =
            read_gram_state(&self.database, &mut transaction)? == Some(GRAM_STATE_READY);
        let mut gram_excluded: HashSet<u64> = HashSet::new();
        if gram_index_ready {
            let (start, end) = EntityKey::prefix_range(
                request.tenant_id,
                request.owner_user_id,
                GRAM_EXCLUSION_NAMESPACE,
                b"",
            )?;
            let rows = self
                .database
                .entity_scan(&mut transaction, &start, &end, GRAM_EXCLUSION_SCAN_CAP)?;
            if rows.len() == GRAM_EXCLUSION_SCAN_CAP {
                // Too many excluded generations to trust the index: walk.
                gram_index_ready = false;
            }
            for (stored_key, _) in &rows {
                let raw = stored_key.key_bytes();
                if raw.len() != 32 {
                    return Err(invalid_data("search gram exclusion key is malformed"));
                }
                gram_excluded.insert(u64::from_be_bytes(raw[..8].try_into().unwrap()));
            }
        }
        let mut gram_sets: [Option<Option<HashSet<u64>>>; 2] = [None, None];
        let mut gram_budget = GRAM_POSTING_READ_BUDGET;
        // Lazy activation: a candidate set is computed only after this many
        // consecutive conversations in the current phase matched nothing.
        // Queries that match early and often (the walk is already optimal
        // there) never pay for the index; barren queries activate it after a
        // handful of conversations and skip the rest.
        let mut barren = 0usize;
        let mut headers_read = 0;
        let mut chunks_read = 0;
        let mut headers = VecDeque::new();
        let mut headers_exhausted = false;
        while state.phase <= u8::from(words.len() > 1) {
            deadline(&mut deadline_elapsed)?;
            if headers_read >= CANDIDATE_SCAN_PAGE || chunks_read >= CHUNK_SCAN_PAGE {
                return page(items, &state, true);
            }
            let header = if state.pending.is_some() {
                let (time, id) = state.after.as_ref().unwrap();
                let stored_key = header_key(&transaction, *time, id)?;
                let raw = self.database.entity_get(&mut transaction, &stored_key)?;
                match raw {
                    Some(raw) => decode_header(&raw)?,
                    None => {
                        state.pending = None;
                        continue;
                    }
                }
            } else if let Some(header) = headers.pop_front() {
                header
            } else if headers_exhausted {
                state.phase += 1;
                state.after = None;
                headers_exhausted = false;
                barren = 0;
                continue;
            } else {
                let (mut start, end) = EntityKey::prefix_range(
                    request.tenant_id,
                    request.owner_user_id,
                    HEADER_NAMESPACE,
                    b"",
                )?;
                if let Some((time, id)) = &state.after {
                    let mut next = header_key(&transaction, *time, id)?.key_bytes().to_vec();
                    next.push(0);
                    start = key(&transaction, HEADER_NAMESPACE, &next)?;
                }
                let batch = HEADER_SCAN_BATCH.min(CANDIDATE_SCAN_PAGE - headers_read);
                let rows = self
                    .database
                    .entity_scan(&mut transaction, &start, &end, batch)?;
                headers_exhausted = rows.len() < batch;
                for (stored_key, raw) in rows {
                    let header = decode_header(&raw)?;
                    if stored_key
                        != header_key(&transaction, header.updated_at_ms, &header.conversation_id)?
                    {
                        return Err(invalid_data("search header order key is malformed"));
                    }
                    headers.push_back(header);
                }
                continue;
            };
            headers_read += 1;
            // Gram-posting candidate filter. A conversation outside this
            // phase's candidate set cannot satisfy the phase predicate:
            // phase-0 requires the phrase (whose every gram was emitted)
            // in the title, id, or one chunk; phase-1 requires every word
            // (again with every gram emitted) somewhere across them. The
            // skipped conversation's state effects are exactly those of a
            // non-matching one, so pages stay byte-identical while its meta
            // namespace is never opened. A resumed conversation (matching
            // pending generation) always runs its exact legacy scan.
            if gram_index_ready
                && state
                    .pending
                    .as_ref()
                    .is_none_or(|pending| pending.generation != header.generation)
            {
                let phase = usize::from(state.phase);
                if gram_sets[phase].is_none() && barren >= GRAM_ACTIVATE_AFTER_BARREN {
                    let terms: &[&str] = if phase == 0 { &[query.as_str()] } else { &words };
                    gram_sets[phase] = Some(gram_candidate_set(
                        &self.database,
                        &mut transaction,
                        terms,
                        &mut gram_budget,
                        &mut deadline_elapsed,
                    )?);
                }
                if let Some(candidates) = gram_sets[phase].as_ref().and_then(|set| set.as_ref())
                {
                    let skip = if candidates.is_empty() && gram_excluded.is_empty() {
                        true
                    } else {
                        let fingerprint = u64::from_be_bytes(
                            generation_digest(&header.conversation_id, &header.generation)
                                [..8]
                                .try_into()
                                .unwrap(),
                        );
                        let generation_id = generation_dictionary::lookup(
                            &self.database, &mut transaction,
                            &generation_digest(&header.conversation_id, &header.generation),
                        )?.ok_or_else(|| invalid_data("published search generation has no stable ID"))?;
                        !gram_excluded.contains(&fingerprint)
                            && !candidates.contains(&generation_id)
                    };
                    if skip {
                        state.pending = None;
                        state.after =
                            Some((header.updated_at_ms, header.conversation_id.clone()));
                        continue;
                    }
                }
            }
            let title = header.title.to_lowercase();
            let title_hit = title.contains(&query);
            let id_hit = header.conversation_id.to_lowercase().contains(&query);
            let mut pending = state
                .pending
                .take()
                .filter(|p| p.generation == header.generation)
                .unwrap_or_else(|| Pending {
                    generation: header.generation.clone(),
                    mask: words.iter().enumerate().fold(0, |bits, (i, word)| {
                        bits | (u32::from(title.contains(word)) << i)
                    }),
                    ..Default::default()
                });
            state.after = Some((header.updated_at_ms, header.conversation_id.clone()));
            let mut best: Option<(TurnMeta, String, bool)> = None;
            if !pending.best.is_empty() {
                best =
                    Some(self.cursor_chunk(request, &header, &pending.best, &query, words[0])?);
            }
            let mut complete = title_hit || id_hit || !header.has_chunks;
            pending.phrase |= title_hit || id_hit;
            while !complete {
                deadline(&mut deadline_elapsed)?;
                let budget = CHUNK_SCAN_BATCH.min(CHUNK_SCAN_PAGE - chunks_read);
                if budget == 0 {
                    state.pending = Some(pending);
                    return page(items, &state, true);
                }
                let (mut start, end) = generation_range(
                    &transaction,
                    TURN_META_NAMESPACE,
                    &header.conversation_id,
                    &header.generation,
                )?;
                if !pending.after.is_empty() {
                    if !pending.after.starts_with(&generation_prefix(
                        &header.conversation_id,
                        &header.generation,
                    )) {
                        return Err(invalid_input("search cursor escaped generation"));
                    }
                    let mut next = pending.after.clone();
                    next.push(0);
                    start = key(&transaction, TURN_META_NAMESPACE, &next)?;
                }
                let rows = self
                    .database
                    .entity_scan(&mut transaction, &start, &end, budget)?;
                chunks_read += rows.len();
                for (stored_key, raw) in &rows {
                    deadline(&mut deadline_elapsed)?;
                    let meta = decode_meta(raw)?;
                    if meta.conversation_id != header.conversation_id
                        || meta.generation != header.generation
                    {
                        return Err(invalid_data("search generation digest collision"));
                    }
                    pending.after = stored_key.key_bytes().to_vec();
                    if !word_blooms
                        .iter()
                        .any(|bits| bloom_might_contain(&meta.bloom, bits))
                    {
                        continue;
                    }
                    let text = self.load_text(&mut transaction, &meta)?;
                    let lowered = text.to_lowercase();
                    let phrase = lowered.contains(&query);
                    pending.phrase |= phrase;
                    for (i, word) in words.iter().enumerate() {
                        pending.mask |= u32::from(lowered.contains(word)) << i;
                    }
                    if (phrase || lowered.contains(words[0]))
                        && best.as_ref().is_none_or(|(old, _, old_phrase)| {
                            rank(&meta, phrase) < rank(old, *old_phrase)
                        })
                    {
                        pending.best = stored_key.key_bytes().to_vec();
                        best = Some((meta, text, phrase));
                    }
                }
                complete = rows.len() < budget;
            }
            let matched = if state.phase == 0 {
                pending.phrase
            } else {
                !pending.phrase && pending.mask == all_bits
            };
            if matched {
                let (snippet, role, match_field) = if title_hit || (best.is_none() && !id_hit) {
                    (String::new(), None, "title".to_owned())
                } else if id_hit {
                    (header.conversation_id.clone(), None, "id".to_owned())
                } else {
                    let (meta, text, phrase) = best.unwrap();
                    let term = if phrase { query.as_str() } else { words[0] };
                    (
                        make_snippet(&text, term, request.snippet_radius),
                        Some(meta.role),
                        meta.match_field,
                    )
                };
                items.push(ConversationSearchHit {
                    id: header.conversation_id,
                    snippet,
                    role,
                    match_field,
                });
            }
            if items.len() >= request.limit {
                return page(items, &state, false);
            }
            if matched {
                barren = 0;
            } else {
                barren += 1;
            }
        }
        Ok(ConversationSearchPage {
            items,
            next_cursor: None,
            incomplete: false,
        })
    }

    fn cursor_chunk(
        &self,
        request: &ConversationSearchRequest,
        header: &Header,
        raw_key: &[u8],
        query: &str,
        first: &str,
    ) -> io::Result<(TurnMeta, String, bool)> {
        if !raw_key.starts_with(&generation_prefix(
            &header.conversation_id,
            &header.generation,
        )) {
            return Err(invalid_input("search cursor escaped generation"));
        }
        let mut transaction = self
            .database
            .begin(request.tenant_id, request.owner_user_id)?;
        let stored_key = key(&transaction, TURN_META_NAMESPACE, raw_key)?;
        let raw = self
            .database
            .entity_get(&mut transaction, &stored_key)?
            .ok_or_else(|| invalid_input("search cursor chunk is unavailable"))?;
        let meta = decode_meta(&raw)?;
        let text = self.load_text(&mut transaction, &meta)?;
        let lower = text.to_lowercase();
        if meta.conversation_id != header.conversation_id
            || meta.generation != header.generation
            || !lower.contains(first)
        {
            return Err(invalid_input("search cursor chunk does not match"));
        }
        Ok((meta, text, lower.contains(query)))
    }
}

fn make_snippet(text: &str, term: &str, radius: usize) -> String {
    if radius == 0 {
        return String::new();
    }
    let lower = text.to_lowercase();
    let Some(byte_position) = lower.find(term) else {
        return String::new();
    };
    let position = lower[..byte_position].chars().count();
    let end_position = position + term.chars().count();
    let (mut start, mut end, mut offset) = (text.chars().count(), text.chars().count(), 0);
    for (index, character) in text.chars().enumerate() {
        let next = offset + character.to_lowercase().count();
        if offset <= position && position < next {
            start = index;
        }
        if offset < end_position && end_position <= next {
            end = index + 1;
            break;
        }
        offset = next;
    }
    let start = start.saturating_sub(radius);
    let snippet: String = text
        .chars()
        .skip(start)
        .take(end.saturating_add(radius) - start)
        .collect();
    format!("…{}…", snippet.replace('\n', " ").trim())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(query: &str) -> ConversationSearchRequest {
        ConversationSearchRequest {
            tenant_id: 7,
            owner_user_id: 11,
            query: query.into(),
            limit: 1,
            snippet_radius: 4,
        }
    }

    fn replace(
        projection: &mut OpenTurnSearchProjection,
        id: &str,
        generation: &str,
        time: u64,
        title: &str,
        rows: &[TurnSearchDocument],
    ) {
        projection
            .begin_conversation_rebuild(7, 11, id, generation, time)
            .unwrap();
        projection
            .set_conversation_title(7, 11, id, generation, title)
            .unwrap();
        for batch in rows.chunks(SOURCE_PAGE_ROWS) {
            projection
                .append_conversation_page(7, 11, id, generation, batch)
                .unwrap();
        }
        projection
            .finalize_conversation_rebuild(7, 11, id, generation)
            .unwrap();
    }

    fn document(index: u32, text: &str) -> TurnSearchDocument {
        TurnSearchDocument {
            turn_id: "turn".into(),
            ordinal: 1,
            chunk_index: index,
            role: "user".into(),
            match_field: "content".into(),
            search_text: text.into(),
        }
    }

    fn collect_pages(
        projection: &OpenTurnSearchProjection,
        query: &str,
        limit: usize,
    ) -> Vec<(Vec<ConversationSearchHit>, Option<String>, bool)> {
        let mut request = request(query);
        request.limit = limit;
        let mut pages = Vec::new();
        let mut cursor = None;
        for _ in 0..64 {
            let page = projection
                .search_page_until(&request, cursor.as_deref(), || false)
                .unwrap();
            cursor = page.next_cursor.clone();
            pages.push((page.items, cursor.clone(), page.incomplete));
            if cursor.is_none() {
                break;
            }
        }
        pages
    }

    fn publish_gram_index(projection: &mut OpenTurnSearchProjection) {
        let mut transaction = projection.database.begin(7, 11).unwrap();
        let stored_key = gram_state_key(&transaction).unwrap();
        projection
            .database
            .entity_put(&mut transaction, stored_key, vec![GRAM_STATE_READY])
            .unwrap();
        projection.database.commit(transaction).unwrap();
    }

    #[test]
    fn gram_index_matches_the_walk_byte_for_byte_across_a_query_battery() {
        let dir = tempfile::tempdir().unwrap();
        let mut projection = OpenTurnSearchProjection::initialize(dir.path(), 64 * 1024 * 1024).unwrap();
        replace(
            &mut projection,
            "alpha",
            "g",
            500,
            "gamma alpha",
            &[document(0, "needle"), document(1, "other text")],
        );
        replace(
            &mut projection,
            "phrase",
            "g",
            100,
            "",
            &[
                document(0, "alpha gamma phrase"),
                document(1, "second unrelated"),
            ],
        );
        replace(&mut projection, "title", "g", 200, "alpha gamma", &[]);
        replace(&mut projection, "cross", "g", 300, "alpha", &[document(0, "gamma")]);
        replace(
            &mut projection,
            "unicode",
            "g",
            400,
            "压缩包",
            &[document(0, "拖拽 压缩 test 界")],
        );
        replace(&mut projection, "empty", "g", 600, "", &[]);
        replace(
            &mut projection,
            "roles",
            "g",
            700,
            "",
            &[
                document(0, "assistant phrase"),
                document(1, "original phrase"),
            ],
        );
        let battery = [
            "alpha gamma",
            "needle",
            "压缩",
            "zz",
            "a b",
            "gamma alpha needle",
            "cross",
            "拖拽",
            "phrase",
            "roles",
            "界",
            "wqx",
            "te",
        ];
        let walk: Vec<_> = battery
            .iter()
            .map(|query| collect_pages(&projection, query, 2))
            .collect();
        publish_gram_index(&mut projection);
        for (query, expected) in battery.iter().zip(walk) {
            assert_eq!(
                &collect_pages(&projection, query, 2),
                &expected,
                "indexed pages diverge from the walk for {query:?}"
            );
        }
    }

    #[test]
    fn gram_index_paginates_identically_past_the_header_page_budget() {
        let dir = tempfile::tempdir().unwrap();
        let mut projection = OpenTurnSearchProjection::initialize(dir.path(), 64 * 1024 * 1024).unwrap();
        for index in 0..(CANDIDATE_SCAN_PAGE + 44) {
            replace(
                &mut projection,
                &format!("conversation-{index:04}"),
                "g",
                index as u64,
                if index % 3 == 0 { "commonword title" } else { "plain" },
                &[document(0, "commonword body")],
            );
        }
        for query in ["commonword", "commonword title", "wqabsent"] {
            for limit in [5, 200] {
                let walk = collect_pages(&projection, query, limit);
                publish_gram_index(&mut projection);
                assert_eq!(
                    collect_pages(&projection, query, limit),
                    walk,
                    "pagination diverges for {query:?} at limit {limit}"
                );
                // Restore the legacy walk for the next comparison.
                let mut transaction = projection.database.begin(7, 11).unwrap();
                let stored_key = gram_state_key(&transaction).unwrap();
                projection
                    .database
                    .entity_put(&mut transaction, stored_key, vec![GRAM_STATE_BUILDING])
                    .unwrap();
                projection.database.commit(transaction).unwrap();
            }
        }
    }

    #[test]
    fn gram_index_preserves_the_item_sequence_under_chunk_scan_pressure() {
        // Above the chunk scan page budget the legacy walk and the indexed
        // walk may cut pages at different boundaries (skipped conversations
        // charge no chunk reads), but the concatenated item sequence must
        // stay identical: the candidate filter only removes conversations
        // that cannot match.
        let dir = tempfile::tempdir().unwrap();
        let mut projection = OpenTurnSearchProjection::initialize(dir.path(), 64 * 1024 * 1024).unwrap();
        for index in 0..(CANDIDATE_SCAN_PAGE + 44) {
            let documents: Vec<TurnSearchDocument> = (0..3)
                .map(|chunk| {
                    if index % 97 == 0 && chunk == 1 {
                        document(chunk as u32, "needle here")
                    } else {
                        document(chunk as u32, "padding text")
                    }
                })
                .collect();
            replace(
                &mut projection,
                &format!("conversation-{index:04}"),
                "g",
                index as u64,
                "plain",
                &documents,
            );
        }
        let mut request = request("needle");
        request.limit = 200;
        let collect_items = |projection: &OpenTurnSearchProjection| {
            let mut items = Vec::new();
            let mut cursor = None;
            for _ in 0..64 {
                let page = projection
                    .search_page_until(&request, cursor.as_deref(), || false)
                    .unwrap();
                items.extend(page.items);
                cursor = page.next_cursor;
                if cursor.is_none() {
                    break;
                }
            }
            items
        };
        let walk = collect_items(&projection);
        assert_eq!(walk.len(), 4);
        publish_gram_index(&mut projection);
        assert_eq!(collect_items(&projection), walk);
    }

    #[test]
    fn posting_query_digests_choose_the_emission_widths() {
        for (value, width) in [
            ("a", 1),
            ("ab", 2),
            ("abc", 3),
            ("abcd", 3),
            ("压缩", 2),
            ("压缩包", 3),
        ] {
            let digests = posting_query_digests(value);
            let expected: Vec<[u8; 32]> = gram_ranges(value, width)
                .map(|(start, end)| gram_digest(&value.as_bytes()[start..end]))
                .collect();
            let mut actual: Vec<[u8; 32]> = digests.into_iter().map(|(_, digest)| digest).collect();
            let mut expected = expected;
            actual.sort_unstable();
            expected.sort_unstable();
            assert_eq!(actual, expected, "digest widths diverge for {value:?}");
        }
        assert!(posting_query_digests("").is_empty());
    }

    #[test]
    fn empty_generation_keeps_title_terms_and_replacement_text_exact() {
        let dir = tempfile::tempdir().unwrap();
        let mut projection =
            OpenTurnSearchProjection::initialize(dir.path(), 64 * 1024 * 1024).unwrap();
        replace(
            &mut projection,
            "conversation",
            "empty",
            1,
            "gamma alpha",
            &[],
        );
        let mut query = request("alpha gamma");
        let hits = projection.search(&query).unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].match_field, "title");
        query.query = "needle".into();
        assert!(projection.search(&query).unwrap().is_empty());
        replace(
            &mut projection,
            "conversation",
            "text",
            2,
            "gamma",
            &[document(0, "needle")],
        );
        assert_eq!(projection.search(&query).unwrap()[0].match_field, "content");
        replace(
            &mut projection,
            "conversation",
            "empty-again",
            3,
            "alpha",
            &[],
        );
        assert!(projection.search(&query).unwrap().is_empty());
        query.query = "alpha".into();
        assert_eq!(projection.search(&query).unwrap()[0].match_field, "title");
    }

    #[test]
    fn batched_headers_preserve_empty_page_boundary_and_exhaustion() {
        let dir = tempfile::tempdir().unwrap();
        let mut projection =
            OpenTurnSearchProjection::initialize(dir.path(), 64 * 1024 * 1024).unwrap();
        for i in 0..CANDIDATE_SCAN_PAGE + HEADER_SCAN_BATCH + 1 {
            replace(
                &mut projection,
                &format!("conversation-{i:04}"),
                "g",
                i as u64,
                if i < 2 { "needle" } else { "unrelated" },
                &[],
            );
        }
        let mut query = request("needle");
        query.limit = 2;
        let first = projection
            .search_page_until(&query, None, || false)
            .unwrap();
        assert!(first.items.is_empty() && first.incomplete);
        let second = projection
            .search_page_until(&query, first.next_cursor.as_deref(), || false)
            .unwrap();
        assert_eq!(
            second
                .items
                .iter()
                .map(|hit| hit.id.as_str())
                .collect::<Vec<_>>(),
            ["conversation-0001", "conversation-0000"]
        );
        let last = projection
            .search_page_until(&query, second.next_cursor.as_deref(), || false)
            .unwrap();
        assert!(last.items.is_empty() && last.next_cursor.is_none() && !last.incomplete);
    }

    #[test]
    fn query_bloom_preparation_preserves_all_unicode_substrings() {
        for text in [
            "a",
            "ab",
            "needle in a haystack",
            "压缩包拖拽",
            "İSTANBUL Straße",
        ] {
            let folded = text.to_lowercase();
            let mut bloom = [0; BLOOM_BYTES];
            bloom_add(&mut bloom, &folded);
            let characters: Vec<_> = folded.chars().collect();
            for start in 0..characters.len() {
                for end in start + 1..=characters.len() {
                    let term: String = characters[start..end].iter().collect();
                    assert!(
                        bloom_might_contain(&bloom, &bloom_query_bits(&term)),
                        "{term}"
                    );
                }
            }
        }
    }

    #[test]
    fn pages_preserve_phase_order_title_and_cross_chunk_terms_without_duplicates() {
        let dir = tempfile::tempdir().unwrap();
        let mut projection =
            OpenTurnSearchProjection::initialize(dir.path(), 64 * 1024 * 1024).unwrap();
        replace(
            &mut projection,
            "cross",
            "g",
            500,
            "alpha",
            &[document(0, "gamma")],
        );
        replace(
            &mut projection,
            "phrase",
            "g",
            100,
            "",
            &[document(0, "alpha gamma")],
        );
        replace(&mut projection, "title", "g", 200, "alpha gamma", &[]);
        let request = request("alpha gamma");
        let mut cursor = None;
        let mut hits = Vec::new();
        for _ in 0..10 {
            let page = projection
                .search_page_until(&request, cursor.as_deref(), || false)
                .unwrap();
            hits.extend(page.items);
            cursor = page.next_cursor;
            if cursor.is_none() {
                break;
            }
        }
        assert!(cursor.is_none());
        assert_eq!(
            hits.iter().map(|h| h.id.as_str()).collect::<Vec<_>>(),
            ["title", "phrase", "cross"]
        );
        assert_eq!(hits[0].match_field, "title");
        assert_eq!(hits[0].role, None);
        assert_eq!(hits[1].role.as_deref(), Some("user"));
        assert_eq!(hits[2].match_field, "title");
    }

    #[test]
    fn huge_conversation_continues_inside_chunks_and_restarts_changed_generation() {
        let dir = tempfile::tempdir().unwrap();
        let mut projection =
            OpenTurnSearchProjection::initialize(dir.path(), 64 * 1024 * 1024).unwrap();
        let mut documents: Vec<_> = (0..600)
            .map(|i| {
                let mut row = document(i, &format!("alpha{}", "界".repeat(4091)));
                row.turn_id = "t".repeat(MAX_ID_BYTES);
                row
            })
            .collect();
        documents[0].search_text = "alpha".into();
        documents[599].search_text = "gamma".into();
        replace(&mut projection, "long", "g1", 200, "", &documents);
        let mut request = request("alpha gamma");
        let first = projection
            .search_page_until(&request, None, || false)
            .unwrap();
        assert!(first.incomplete && first.items.is_empty());
        let cursor = first.next_cursor.unwrap();
        assert!(cursor.len() <= CURSOR_BYTES);
        for (owner, query) in [(12, "alpha gamma"), (11, "other")] {
            request.owner_user_id = owner;
            request.query = query.into();
            assert_eq!(
                projection
                    .search_page_until(&request, Some(&cursor), || false)
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::InvalidInput
            );
        }
        request.owner_user_id = 11;
        request.query = "alpha gamma".into();
        let mut cursor = Some(cursor);
        let mut hits = Vec::new();
        for _ in 0..10 {
            let page = projection
                .search_page_until(&request, cursor.as_deref(), || false)
                .unwrap();
            hits.extend(page.items);
            cursor = page.next_cursor;
            if cursor.is_none() {
                break;
            }
        }
        assert!(cursor.is_none());
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].snippet, "…alpha…");
        let first = projection
            .search_page_until(&request, None, || false)
            .unwrap();
        replace(
            &mut projection,
            "long",
            "g2",
            200,
            "",
            &[document(0, "gamma")],
        );
        let page = projection
            .search_page_until(&request, first.next_cursor.as_deref(), || false)
            .unwrap();
        assert!(page.items.is_empty() && page.next_cursor.is_none());
    }

    #[test]
    fn snippet_unicode_expansion_and_source_preference_match_contract() {
        assert_eq!(make_snippet("CAFÉ xxxxxxxxxxxxx", "café", 4), "…CAFÉ xxx…");
        assert_eq!(
            make_snippet("İabc NEEDLE tail", "needle", 2),
            "…c NEEDLE t…"
        );
        let dir = tempfile::tempdir().unwrap();
        let mut projection =
            OpenTurnSearchProjection::initialize(dir.path(), 64 * 1024 * 1024).unwrap();
        let mut documents = vec![
            document(0, "assistant phrase"),
            document(1, "original phrase"),
        ];
        documents[0].role = "assistant".into();
        documents[1].match_field = "originalContent".into();
        replace(&mut projection, "c", "g", 1, "", &documents);
        let hit = projection.search(&request("phrase")).unwrap().remove(0);
        assert_eq!(hit.match_field, "originalContent");
        assert_eq!(hit.role.as_deref(), Some("user"));
        for query in [
            "x\0y".to_owned(),
            "a".repeat(513),
            vec!["word"; 33].join(" "),
        ] {
            assert_eq!(
                projection.search(&request(&query)).unwrap_err().kind(),
                io::ErrorKind::InvalidInput
            );
        }
    }
}
