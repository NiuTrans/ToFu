//! Commit-owned worker wake hints, independent of semantic admission and WAL
//! receipts. A waiter holds only this condition; no authority lock or journal.
use crate::{protocol::{Request, Response}, semantic::AuthenticatedScope};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{collections::{BTreeMap, BTreeSet}, io, sync::{Arc, Condvar, Mutex},
          time::Duration};
use uuid::Uuid;

#[path = "generated_worker_control.rs"]
mod contract;
pub(crate) use contract::WATCH_OPERATION;

#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Cursor { epoch: String, queue: u64, cancel: u64 }

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WatchRequest { cursor: Option<Cursor> }

struct State {
    cursor: Cursor,
    closed: bool,
    waiters: BTreeMap<Uuid, BTreeSet<String>>,
}

pub(crate) struct WorkerControl { state: Mutex<State>, changed: Condvar }

impl WorkerControl {
    pub(crate) fn new() -> Arc<Self> {
        Arc::new(Self { state: Mutex::new(State {
            cursor: Cursor { epoch: Uuid::new_v4().to_string(), queue: 0, cancel: 0 },
            closed: false, waiters: BTreeMap::new(),
        }), changed: Condvar::new() })
    }

    pub(crate) fn publish(&self, queue: bool, cancelled: &BTreeSet<String>) {
        if !queue && cancelled.is_empty() { return; }
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if state.closed { return; }
        if state.cursor.queue == u64::MAX || state.cursor.cancel == u64::MAX {
            state.cursor = Cursor { epoch: Uuid::new_v4().to_string(), queue: 0, cancel: 0 };
        }
        state.cursor.queue += u64::from(queue);
        state.cursor.cancel += u64::from(!cancelled.is_empty());
        for tasks in state.waiters.values_mut() { tasks.extend(cancelled.iter().cloned()); }
        self.changed.notify_all();
    }

    pub(crate) fn close(&self) {
        self.state.lock().unwrap_or_else(|e| e.into_inner()).closed = true;
        self.changed.notify_all();
    }

    fn watch(&self, request: WatchRequest, timeout: Duration) -> io::Result<Vec<u8>> {
        let mut state = self.state.lock().map_err(|_| io::Error::other("worker watch poisoned"))?;
        let mut cancelled: Option<Vec<String>> = None;
        if request.cursor.as_ref() == Some(&state.cursor) && !state.closed {
            let key = Uuid::new_v4();
            state.waiters.insert(key, BTreeSet::new());
            let (next, _) = self.changed.wait_timeout_while(state, timeout, |state|
                !state.closed && request.cursor.as_ref() == Some(&state.cursor))
                .map_err(|_| io::Error::other("worker watch poisoned"))?;
            state = next;
            let tasks = state.waiters.remove(&key).unwrap_or_default();
            if request.cursor.as_ref().is_some_and(|old| old.epoch == state.cursor.epoch) {
                cancelled = Some(tasks.into_iter().collect());
            }
        } else if request.cursor.as_ref().is_some_and(|old|
            old.epoch == state.cursor.epoch && old.cancel == state.cursor.cancel) {
            cancelled = Some(Vec::new());
        }
        if state.closed { return Err(io::Error::other("worker control closed")); }
        serde_json::to_vec(&json!({"format":contract::FORMAT,
            "cursor":state.cursor, "cancelTasks":cancelled})).map_err(io::Error::other)
    }

    pub(crate) fn respond(&self, request: &Request, schema_id: u32, now: u64,
                          scope: AuthenticatedScope) -> Response {
        let result = (|| {
            if request.schema_id != schema_id || request.command_id.is_some()
                || request.owner_id != scope.owner_id || request.tenant_id != scope.tenant_id {
                return Err(io::Error::new(io::ErrorKind::PermissionDenied, "worker watch binding differs"));
            }
            if request.deadline_unix_ms <= now {
                return Err(io::Error::new(io::ErrorKind::TimedOut, "worker watch deadline elapsed"));
            }
            let payload: serde_json::Value = serde_json::from_slice(&request.payload).map_err(io::Error::other)?;
            if payload.as_object().is_none_or(|v| v.len() != 1 || !v.contains_key("cursor")) {
                return Err(io::Error::new(io::ErrorKind::InvalidInput, "worker watch requires cursor"));
            }
            let watch = serde_json::from_value(payload).map_err(io::Error::other)?;
            self.watch(watch, Duration::from_millis(request.deadline_unix_ms - now))
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
    fn gap_survives_without_notification_history() {
        let control = WorkerControl::new();
        let first: serde_json::Value = serde_json::from_slice(
            &control.watch(WatchRequest {cursor: None}, Duration::ZERO).unwrap()).unwrap();
        control.publish(true, &BTreeSet::from(["task".into()]));
        let next: serde_json::Value = serde_json::from_slice(&control.watch(
            WatchRequest {cursor: Some(serde_json::from_value(first["cursor"].clone()).unwrap())},
            Duration::ZERO).unwrap()).unwrap();
        assert_eq!(next["cursor"]["queue"], 1);
        assert!(next["cancelTasks"].is_null());
        assert!(control.state.lock().unwrap().waiters.is_empty());
    }
    #[test]
    fn shutdown_releases_wait_without_authority_lock() {
        let control = WorkerControl::new();
        let cursor = control.state.lock().unwrap().cursor.clone();
        let waiting = control.clone();
        let thread = std::thread::spawn(move ||
            waiting.watch(WatchRequest {cursor: Some(cursor)}, Duration::from_secs(60)));
        control.close();
        assert!(thread.join().unwrap().is_err());
    }
}
