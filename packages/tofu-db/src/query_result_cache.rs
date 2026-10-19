//! Reconstructible results certified by the common query executor. Lookup is
//! nonblocking and exact across scope, schema, operation, payload and snapshot.
//! PublishedQueryView supplies the current health/generation proof; this module
//! never reads storage, grants admission or pins a root. A miss changes no answer.
use crate::{entity::EntitySnapshot, protocol::Request, transaction_ir::IrResponse};
use std::{
    collections::BTreeMap,
    hash::{Hash, Hasher},
    sync::{Arc, Mutex},
};

const PROBATION_BUDGET_DIVISOR: usize = 64;
const MINIMUM_RESULT_ACCOUNTED_BYTES: usize = 512;

/// Admission controls copying cost only. Every returned result still requires
/// an exact current snapshot, regardless of how its repeated-use hint is scoped.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Admission {
    RepeatedKey,
    RepeatedSnapshot,
}

#[derive(Clone, Copy, Hash)]
pub(crate) struct Lookup<'a> {
    pub owner_id: u64,
    pub tenant_id: Option<u64>,
    pub schema_id: u32,
    pub operation: &'a str,
    pub payload: &'a [u8],
}
impl<'a> From<&'a Request> for Lookup<'a> {
    fn from(request: &'a Request) -> Self {
        Self {
            owner_id: request.owner_id,
            tenant_id: request.tenant_id,
            schema_id: request.schema_id,
            operation: &request.operation,
            payload: &request.payload,
        }
    }
}
impl Lookup<'_> {
    fn within_frame_bounds(self) -> bool {
        self.operation.len() <= crate::protocol::MAX_OPERATION_BYTES
            && self.payload.len() <= crate::protocol::MAX_PAYLOAD_BYTES
    }
    fn hash_key(self) -> u64 {
        let mut hash = std::collections::hash_map::DefaultHasher::new();
        self.hash(&mut hash);
        hash.finish()
    }
}
struct Entry {
    owner_id: u64,
    tenant_id: Option<u64>,
    schema_id: u32,
    operation: String,
    payload: Vec<u8>,
    snapshot: EntitySnapshot,
    response: Arc<IrResponse>,
    bytes: usize,
    hot: bool,
}
impl Entry {
    fn matches(&self, key: Lookup<'_>) -> bool {
        self.owner_id == key.owner_id
            && self.tenant_id == key.tenant_id
            && self.schema_id == key.schema_id
            && self.operation == key.operation
            && self.payload == key.payload
    }
}
#[derive(Default)]
struct State {
    entries: BTreeMap<u64, Entry>,
    capacity: usize,
    bytes: usize,
    cursor: u64,
    hits: u64,
    misses: u64,
    evictions: u64,
    probation: Vec<u64>,
    probation_observations: usize,
    admission_deferrals: u64,
}
impl State {
    fn result_capacity(&self) -> usize {
        self.capacity - self.capacity / PROBATION_BUDGET_DIVISOR
    }
    fn probation_bytes(&self) -> usize {
        self.probation.capacity() * std::mem::size_of::<u64>()
    }
    fn probation_window_requests(&self) -> usize {
        // Two observations of any fitting result working set must fit before
        // aging. An exact short FIFO can permanently reject a larger hot set.
        (self.capacity / MINIMUM_RESULT_ACCOUNTED_BYTES)
            .saturating_mul(2)
            .max(1)
    }
    fn admit_repeated(&mut self, hash: u64) -> bool {
        let budget = self.capacity / PROBATION_BUDGET_DIVISOR;
        if self.probation.is_empty() {
            // A four-position Bloom doorkeeper uses 1/128..1/64 of the cache
            // budget. Allocate lazily and fall back on allocation uncertainty.
            let words = (budget / (2 * std::mem::size_of::<u64>()))
                .max(1)
                .checked_next_power_of_two();
            let Some(words) = words.filter(|words| *words <= budget / std::mem::size_of::<u64>())
            else {
                return false;
            };
            if self.probation.try_reserve_exact(words).is_err() {
                return false;
            }
            if self.probation_bytes() > budget {
                self.probation = Vec::new();
                return false;
            }
            self.probation.resize(words, 0);
        }
        if self.probation_observations >= self.probation_window_requests() {
            self.probation.fill(0);
            self.probation_observations = 0;
        }
        let mask = (self.probation.len() * 64 - 1) as u64;
        let stride = hash.rotate_right(29) | 1;
        let mut seen = true;
        for lane in 0..4u64 {
            let bit = hash.wrapping_add(lane.wrapping_mul(stride)) & mask;
            let word = &mut self.probation[(bit / 64) as usize];
            let flag = 1u64 << (bit % 64);
            seen &= *word & flag != 0;
            *word |= flag;
        }
        self.probation_observations += 1;
        if !seen {
            self.admission_deferrals = self.admission_deferrals.saturating_add(1);
        }
        // False positives permit only an extra admission. Exact request/snapshot
        // equality and immutable response proof still decide every answer.
        seen
    }
    fn remove(&mut self, key: u64) {
        if let Some(entry) = self.entries.remove(&key) {
            self.bytes -= entry.bytes;
            self.evictions = self.evictions.saturating_add(1);
        }
    }
}
pub(crate) struct QueryResultCache {
    state: Mutex<State>,
}
impl QueryResultCache {
    pub(crate) fn new(capacity: usize) -> Self {
        Self {
            state: Mutex::new(State {
                capacity,
                ..State::default()
            }),
        }
    }
    pub(crate) fn configure(&self, capacity: usize) {
        if let Ok(mut state) = self.state.lock() {
            state.capacity = capacity;
            state.probation = Vec::new();
            state.probation_observations = 0;
            while state.bytes > state.result_capacity() {
                let Some(key) = state.entries.first_key_value().map(|(key, _)| *key) else {
                    break;
                };
                state.remove(key);
            }
        }
    }
    pub(crate) fn candidate(&self, key: Lookup<'_>) -> Option<(EntitySnapshot, Arc<IrResponse>)> {
        if !key.within_frame_bounds() {
            return None;
        }
        let mut state = self.state.try_lock().ok()?;
        if key.payload.len().saturating_add(key.operation.len()) > state.capacity / 16 {
            return None;
        }
        // A streaming first-use workload has no admitted results; skip hashing.
        if state.entries.is_empty() {
            state.misses = state.misses.saturating_add(1);
            return None;
        }
        let found = state
            .entries
            .get_mut(&key.hash_key())
            .filter(|entry| entry.matches(key))
            .map(|entry| {
                entry.hot = true;
                (entry.snapshot, Arc::clone(&entry.response))
            });
        if found.is_some() {
            state.hits = state.hits.saturating_add(1);
        } else {
            state.misses = state.misses.saturating_add(1);
        }
        found
    }
    #[cfg(test)]
    fn get(&self, key: Lookup<'_>, snapshot: EntitySnapshot) -> Option<Arc<IrResponse>> {
        self.candidate(key)
            .filter(|(stored, _)| *stored == snapshot)
            .map(|(_, response)| response)
    }
    pub(crate) fn insert(
        &self,
        key: Lookup<'_>,
        snapshot: EntitySnapshot,
        response_len: usize,
        admission: Admission,
        copy_response: impl FnOnce() -> Option<IrResponse>,
    ) {
        if !key.within_frame_bounds() || response_len > crate::protocol::MAX_PAYLOAD_BYTES {
            return;
        }
        let Ok(mut state) = self.state.try_lock() else {
            return;
        };
        let Some(bytes) = MINIMUM_RESULT_ACCOUNTED_BYTES
            .checked_add(key.operation.len())
            .and_then(|v| v.checked_add(key.payload.len()))
            .and_then(|v| v.checked_add(response_len))
        else {
            return;
        };
        if bytes > state.capacity / 16 {
            return;
        }
        let hash = key.hash_key();
        let known_hot = state
            .entries
            .get(&hash)
            .is_some_and(|entry| entry.matches(key)
                && (admission == Admission::RepeatedKey || entry.snapshot == snapshot));
        let admission_hash = match admission {
            Admission::RepeatedKey => hash,
            Admission::RepeatedSnapshot => {
                let mut scoped = std::collections::hash_map::DefaultHasher::new();
                hash.hash(&mut scoped);
                snapshot.sequence.hash(&mut scoped);
                snapshot.root.hash(&mut scoped);
                scoped.finish()
            }
        };
        // A snapshot queried once after each write has no reusable result.
        // Its old entry must not turn a stale candidate into a hot-copy hint.
        // Bloom false positives permit occasional copying, never stale answers.
        if !known_hot && !state.admit_repeated(admission_hash) {
            return;
        }
        let Some(response) = copy_response() else {
            return;
        };
        if !response.snapshot_stable()
            || !response.json_proven()
            || response.capacity() != response_len
        {
            return;
        }
        state.remove(hash); // A hash collision can evict, never return another key.
                            // At most eight CLOCK probes; inability to cache never rejects a query.
        for _ in 0..8 {
            if state.bytes <= state.result_capacity().saturating_sub(bytes) {
                break;
            }
            let key = state
                .entries
                .range((
                    std::ops::Bound::Excluded(state.cursor),
                    std::ops::Bound::Unbounded,
                ))
                .next()
                .or_else(|| state.entries.first_key_value())
                .map(|(key, _)| *key);
            let Some(key) = key else {
                break;
            };
            state.cursor = key;
            if state.entries.get(&key).is_some_and(|entry| entry.hot) {
                state.entries.get_mut(&key).unwrap().hot = false;
            } else {
                state.remove(key);
            }
        }
        if state.bytes > state.result_capacity().saturating_sub(bytes) {
            return;
        }
        let entry = Entry {
            owner_id: key.owner_id,
            tenant_id: key.tenant_id,
            schema_id: key.schema_id,
            operation: key.operation.to_owned(),
            payload: key.payload.to_vec(),
            snapshot,
            response: Arc::new(response),
            bytes,
            hot: true,
        };
        state.entries.insert(hash, entry);
        state.bytes += bytes;
    }
    pub(crate) fn metrics(&self) -> serde_json::Value {
        let Ok(state) = self.state.try_lock() else {
            return serde_json::json!({"available": false});
        };
        serde_json::json!({"available": true, "capacityBytes": state.capacity,
            "accountedBytes": state.bytes + state.probation_bytes(), "resultAccountedBytes": state.bytes,
            "probationAccountedBytes": state.probation_bytes(), "probationObservations": state.probation_observations,
            "probationWindowRequests": state.probation_window_requests(),
            "admissionDeferrals": state.admission_deferrals, "entries": state.entries.len(),
            "lookupHits": state.hits, "lookupMisses": state.misses, "evictions": state.evictions})
    }
}

#[cfg(test)]
#[path = "query_result_cache_tests.rs"]
mod tests;
