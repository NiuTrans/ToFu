//! Commit-owned worker and conversation hints; no semantic transaction or log.
//! One generic wait owner provides gap detection, coalescing and joined close.
use crate::{protocol::{Request, Response}, semantic::AuthenticatedScope};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{collections::{BTreeMap, BTreeSet}, io, sync::{Arc, Condvar, Mutex, atomic::{AtomicBool, Ordering}}, time::Duration};
use uuid::Uuid;

#[path = "generated_worker_control.rs"]
mod contract;
pub(crate) use contract::{WATCH_OPERATION, CONVERSATION_WATCH_OPERATION};

#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
struct Cursor {
    epoch: String,
    #[serde(flatten)]
    generations: BTreeMap<String, u64>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WatchRequest { cursor: Option<Cursor> }

struct State<H> {
    cursor: Cursor,
    closed: bool,
    waiters: BTreeMap<Uuid, BTreeSet<H>>,
}

struct CommitWatch<H> {
    state: Mutex<State<H>>, changed: Condvar,
    format: &'static str, hint_key: &'static str, hint_generation: &'static str,
}

impl<H: Clone + Ord + Serialize> CommitWatch<H> {
    fn new(generations: &[&str], format: &'static str,
           hint_key: &'static str, hint_generation: &'static str) -> Self {
        Self { state: Mutex::new(State {
            cursor: Cursor { epoch: Uuid::new_v4().to_string(),
                generations: generations.iter().map(|k| (k.to_string(), 0)).collect() },
            closed: false, waiters: BTreeMap::new(),
        }), changed: Condvar::new(), format, hint_key, hint_generation }
    }

    fn publish(&self, changed: &[&str], hints: &BTreeSet<H>) {
        if changed.is_empty() { return; }
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if state.closed { return; }
        if state.cursor.generations.values().any(|v| *v == u64::MAX) {
            state.cursor.epoch = Uuid::new_v4().to_string();
            state.cursor.generations.values_mut().for_each(|v| *v = 0);
        }
        for key in changed { *state.cursor.generations.get_mut(*key).expect("declared generation") += 1; }
        for observed in state.waiters.values_mut() { observed.extend(hints.iter().cloned()); }
        self.changed.notify_all();
    }

    fn close(&self) {
        self.state.lock().unwrap_or_else(|e| e.into_inner()).closed = true;
        self.changed.notify_all();
    }

    #[cfg(test)]
    fn watch(&self, request: WatchRequest, timeout: Duration) -> io::Result<Vec<u8>> {
        self.watch_cancelled(request, timeout, None)
    }

    fn notify_cancel(&self) {
        let _state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        self.changed.notify_all();
    }

    fn watch_cancelled(&self, request: WatchRequest, timeout: Duration,
                       cancellation: Option<&AtomicBool>) -> io::Result<Vec<u8>> {
        let cancelled = || cancellation.is_some_and(|flag| flag.load(Ordering::Acquire));
        let mut state = self.state.lock().map_err(|_| io::Error::other("commit watch poisoned"))?;
        if request.cursor.as_ref().is_some_and(|old|
            old.generations.keys().ne(state.cursor.generations.keys())) {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "invalid commit watch cursor"));
        }
        let mut observed: Option<Vec<H>> = None;
        if request.cursor.as_ref() == Some(&state.cursor) && !state.closed && !cancelled() {
            let key = Uuid::new_v4();
            state.waiters.insert(key, BTreeSet::new());
            let (next, _) = self.changed.wait_timeout_while(state, timeout, |state|
                !state.closed && !cancelled() && request.cursor.as_ref() == Some(&state.cursor))
                .map_err(|_| io::Error::other("commit watch poisoned"))?;
            state = next;
            let hints = state.waiters.remove(&key).unwrap_or_default();
            if request.cursor.as_ref().is_some_and(|old| old.epoch == state.cursor.epoch) {
                observed = Some(hints.into_iter().collect());
            }
        } else if request.cursor.as_ref().is_some_and(|old|
            old.epoch == state.cursor.epoch
            && old.generations[self.hint_generation] == state.cursor.generations[self.hint_generation]) {
            observed = Some(Vec::new());
        }
        if state.closed { return Err(io::Error::other("commit control closed")); }
        if cancelled() { return Err(io::Error::new(io::ErrorKind::Interrupted, "commit watch cancelled")); }
        serde_json::to_vec(&json!({"format":self.format,
            "cursor":state.cursor, self.hint_key:observed})).map_err(io::Error::other)
    }
}

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "camelCase")]
struct ConversationHint { user_id: u64, conversation_id: String }

pub(crate) struct WorkerControl {
    workers: CommitWatch<String>, conversations: CommitWatch<ConversationHint>,
}

impl WorkerControl {
    pub(crate) fn new() -> Arc<Self> {
        Arc::new(Self {
            workers: CommitWatch::new(&["queue", "cancel"], contract::FORMAT, "cancelTasks", "cancel"),
            conversations: CommitWatch::new(&["change"], contract::CONVERSATION_FORMAT, "conversations", "change"),
        })
    }

    pub(crate) fn publish(&self, queue: bool, cancelled: &BTreeSet<String>) {
        let mut changed = Vec::new();
        if queue { changed.push("queue"); }
        if !cancelled.is_empty() { changed.push("cancel"); }
        self.workers.publish(&changed, cancelled);
    }

    pub(crate) fn publish_conversations<'a>(&self, user_id: u64, keys: impl Iterator<Item=&'a String>) {
        let hints: BTreeSet<_> = keys.map(|key| ConversationHint {
            user_id, conversation_id: key.clone(),
        }).collect();
        if !hints.is_empty() { self.conversations.publish(&["change"], &hints); }
    }

    pub(crate) fn close(&self) {
        self.workers.close();
        self.conversations.close();
    }

    pub(crate) fn notify_cancel(&self) {
        self.workers.notify_cancel();
        self.conversations.notify_cancel();
    }

    pub(crate) fn respond(&self, request: &Request, schema_id: u32, now: u64,
                          scope: AuthenticatedScope) -> Response {
        self.respond_cancelled(request, schema_id, now, scope, None)
    }

    pub(crate) fn respond_cancelled(&self, request: &Request, schema_id: u32, now: u64,
                          scope: AuthenticatedScope, cancellation: Option<&AtomicBool>) -> Response {
        let result = (|| {
            if request.schema_id != schema_id || request.command_id.is_some()
                || request.owner_id != scope.owner_id || request.tenant_id != scope.tenant_id {
                return Err(io::Error::new(io::ErrorKind::PermissionDenied, "commit watch binding differs"));
            }
            if request.deadline_unix_ms <= now {
                return Err(io::Error::new(io::ErrorKind::TimedOut, "commit watch deadline elapsed"));
            }
            let payload: serde_json::Value = serde_json::from_slice(&request.payload).map_err(io::Error::other)?;
            if payload.as_object().is_none_or(|v| v.len() != 1 || !v.contains_key("cursor")) {
                return Err(io::Error::new(io::ErrorKind::InvalidInput, "commit watch requires cursor"));
            }
            let watch = serde_json::from_value(payload).map_err(io::Error::other)?;
            let timeout = Duration::from_millis(request.deadline_unix_ms - now);
            match request.operation.as_str() {
                WATCH_OPERATION => self.workers.watch_cancelled(watch, timeout, cancellation),
                CONVERSATION_WATCH_OPERATION => self.conversations.watch_cancelled(watch, timeout, cancellation),
                _ => Err(io::Error::new(io::ErrorKind::InvalidInput, "unknown commit watch")),
            }
        })();
        let (status, payload, error_code, error_message) = match result {
            Ok(value) => (0, value, None, None),
            Err(error) => (1, Vec::new(), Some("worker_control_refused".into()), Some(error.to_string())),
        };
        Response { correlation_id: request.correlation_id, schema_id, payload, status,
                   error_code, error_message, retryable: false }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cancel_wakes_only_its_wait_without_publication() {
        let control = WorkerControl::new();
        let cursor = control.conversations.state.lock().unwrap().cursor.clone();
        let cancelled = Arc::new(AtomicBool::new(false));
        let waiting = control.clone();
        let flag = cancelled.clone();
        let expected = cursor.clone();
        let thread = std::thread::spawn(move || waiting.conversations.watch_cancelled(
            WatchRequest { cursor: Some(expected) }, Duration::from_secs(60), Some(&flag)));
        // Also covers cancellation before enrollment: the flag is persistent,
        // and notification takes the same condition lock as the predicate.
        cancelled.store(true, Ordering::Release);
        control.notify_cancel();
        assert_eq!(thread.join().unwrap().unwrap_err().kind(), io::ErrorKind::Interrupted);
        assert!(control.conversations.state.lock().unwrap().waiters.is_empty());
        let value: serde_json::Value = serde_json::from_slice(&control.conversations.watch(
            WatchRequest { cursor: Some(cursor) }, Duration::ZERO).unwrap()).unwrap();
        assert_eq!(value["cursor"]["change"], 0);
        assert_eq!(value["conversations"], json!([]));
    }

    #[test]
    fn conversation_gap_is_independent_and_history_free() {
        let control = WorkerControl::new();
        let initial = control.conversations.state.lock().unwrap().cursor.clone();
        control.publish(true, &BTreeSet::from(["worker".into()]));
        let unchanged: serde_json::Value = serde_json::from_slice(&control.conversations.watch(
            WatchRequest { cursor: Some(initial.clone()) }, Duration::ZERO).unwrap()).unwrap();
        assert_eq!(unchanged["cursor"]["change"], 0);
        assert_eq!(unchanged["conversations"], json!([]));
        control.publish_conversations(41, [&"one".to_string()].into_iter());
        let changed: serde_json::Value = serde_json::from_slice(&control.conversations.watch(
            WatchRequest { cursor: Some(initial) }, Duration::ZERO).unwrap()).unwrap();
        assert_eq!(changed["cursor"]["change"], 1);
        assert!(changed["conversations"].is_null());
        assert!(control.conversations.state.lock().unwrap().waiters.is_empty());
        control.close();
        assert!(control.conversations.watch(WatchRequest { cursor: None }, Duration::ZERO).is_err());
    }
    #[test]
    fn gap_survives_without_notification_history() {
        let control = WorkerControl::new();
        let first: serde_json::Value = serde_json::from_slice(
            &control.workers.watch(WatchRequest {cursor: None}, Duration::ZERO).unwrap()).unwrap();
        control.publish(true, &BTreeSet::from(["task".into()]));
        let next: serde_json::Value = serde_json::from_slice(&control.workers.watch(
            WatchRequest {cursor: Some(serde_json::from_value(first["cursor"].clone()).unwrap())},
            Duration::ZERO).unwrap()).unwrap();
        assert_eq!(next["cursor"]["queue"], 1);
        assert!(next["cancelTasks"].is_null());
        assert!(control.workers.state.lock().unwrap().waiters.is_empty());
    }
    #[test]
    fn shutdown_releases_wait_without_authority_lock() {
        let control = WorkerControl::new();
        let cursor = control.workers.state.lock().unwrap().cursor.clone();
        let waiting = control.clone();
        let thread = std::thread::spawn(move ||
            waiting.workers.watch(WatchRequest {cursor: Some(cursor)}, Duration::from_secs(60)));
        control.close();
        assert!(thread.join().unwrap().is_err());
    }
}
