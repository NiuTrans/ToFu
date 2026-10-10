//! Native exact, task-bound single-use approval receipts. This owner records
//! trusted upstream decisions; it never decides human/Auto/delegate permission.
//! No durable state, waiting UI, background timer or retained argument tree.
pub use crate::execution_contract::ApprovalAuthority as Authority;
use crate::{
    execution_contract::{ApprovalAuthority, APPROVAL_RECEIPT_TTL_SECONDS},
    session::ExecutionSession,
    tool_contract::AdmittedCall,
    tool_policy::approval_fingerprint,
};
use std::{
    collections::{BTreeMap, VecDeque},
    sync::{Arc, Mutex, Weak},
    time::Duration,
};
pub type Clock = Arc<dyn Fn() -> Duration + Send + Sync>;
#[derive(Clone, Copy)]
pub struct Budget {
    pub receipts: usize,
    pub identity_bytes: usize,
}
#[derive(Debug, PartialEq, Eq)]
pub enum Error {
    Binding,
    Closed,
    Capacity,
    StateUnavailable,
    Expired,
    Missing,
    Arguments,
}
struct Entry {
    call_id: String,
    signature: [u8; 32],
    at: Duration,
    authority: ApprovalAuthority,
    bytes: usize,
}
struct State {
    next: u64,
    entries: BTreeMap<u64, Entry>,
    ids: BTreeMap<String, u64>,
    order: VecDeque<u64>,
    bytes: usize,
    closed: bool,
}
pub struct Store {
    owner: u64,
    task_id: String,
    epoch: String,
    parent: Weak<ExecutionSession>,
    budget: Budget,
    clock: Clock,
    state: Mutex<State>,
}
/// Private, non-cloneable receipt capability; display metadata cannot construct it.
pub struct Ticket {
    store: Arc<Store>,
    id: u64,
}
impl Store {
    pub fn new(
        parent: &Arc<ExecutionSession>,
        owner: u64,
        task_id: &str,
        epoch: &str,
        budget: Budget,
        clock: Clock,
    ) -> Result<Arc<Self>, Error> {
        if !parent.belongs_to(owner, task_id)
            || epoch.is_empty()
            || budget.receipts == 0
            || budget.identity_bytes == 0
        {
            return Err(Error::Binding);
        }
        Ok(Arc::new(Self {
            owner,
            task_id: task_id.into(),
            epoch: epoch.into(),
            parent: Arc::downgrade(parent),
            budget,
            clock,
            state: Mutex::new(State {
                next: 0,
                entries: BTreeMap::new(),
                ids: BTreeMap::new(),
                order: VecDeque::new(),
                bytes: 0,
                closed: false,
            }),
        }))
    }
    fn live(&self) -> Result<(), Error> {
        let parent = self.parent.upgrade().ok_or(Error::Closed)?;
        let snapshot = parent.snapshot().map_err(|_| Error::StateUnavailable)?;
        if snapshot.phase.is_terminal()
            || snapshot.phase == crate::execution_contract::ExecutionPhase::Settling
            || snapshot.cancel_requested
            || snapshot.deadline_remaining_nanos.is_some_and(|n| n <= 0)
        {
            return Err(Error::Closed);
        }
        Ok(())
    }
    pub fn mint(
        self: &Arc<Self>,
        call_id: &str,
        call: &AdmittedCall,
        authority: Authority,
    ) -> Result<Ticket, Error> {
        self.live()?;
        if call_id.is_empty() || call.owner() != self.owner || call.epoch() != self.epoch {
            return Err(Error::Binding);
        }
        let bytes = call_id
            .len()
            .checked_mul(2)
            .and_then(|n| n.checked_add(std::mem::size_of::<Entry>() + 16))
            .ok_or(Error::Capacity)?;
        if bytes > self.budget.identity_bytes {
            return Err(Error::Capacity);
        }
        let signature =
            approval_fingerprint(call.name(), call.arguments()).map_err(|_| Error::Arguments)?;
        let at = (self.clock)();
        let mut state = self.state.lock().map_err(|_| Error::StateUnavailable)?;
        if state.closed {
            return Err(Error::Closed);
        }
        Self::expire(&mut state, at);
        if let Some(id) = state.ids.get(call_id).copied() {
            Self::remove(&mut state, id);
        }
        while state.entries.len() >= self.budget.receipts
            || state
                .bytes
                .checked_add(bytes)
                .is_none_or(|n| n > self.budget.identity_bytes)
        {
            let id = *state.order.front().ok_or(Error::Capacity)?;
            Self::remove(&mut state, id);
        }
        let id = state.next.checked_add(1).ok_or(Error::Capacity)?;
        state.next = id;
        state.bytes += bytes;
        state.ids.insert(call_id.into(), id);
        state.order.push_back(id);
        state.entries.insert(
            id,
            Entry {
                call_id: call_id.into(),
                signature,
                at,
                authority,
                bytes,
            },
        );
        Ok(Ticket {
            store: self.clone(),
            id,
        })
    }
    fn remove(state: &mut State, id: u64) -> Option<Entry> {
        let entry = state.entries.remove(&id)?;
        state.ids.remove(&entry.call_id);
        state.order.retain(|key| *key != id);
        state.bytes -= entry.bytes;
        Some(entry)
    }
    fn expire(state: &mut State, now: Duration) {
        let ttl = Duration::from_secs(APPROVAL_RECEIPT_TTL_SECONDS as u64);
        let stale: Vec<_> = state
            .entries
            .iter()
            .filter_map(|(id, e)| {
                now.checked_sub(e.at)
                    .filter(|age| *age <= ttl)
                    .is_none()
                    .then_some(*id)
            })
            .collect();
        for id in stale {
            Self::remove(state, id);
        }
    }
    pub fn revoke(&self, call_id: &str) -> Result<bool, Error> {
        let mut state = self.state.lock().map_err(|_| Error::StateUnavailable)?;
        Ok(state
            .ids
            .get(call_id)
            .copied()
            .and_then(|id| Self::remove(&mut state, id))
            .is_some())
    }
    pub fn close(&self) -> Result<(), Error> {
        let mut state = self.state.lock().map_err(|_| Error::StateUnavailable)?;
        state.closed = true;
        state.entries.clear();
        state.ids.clear();
        state.order.clear();
        state.bytes = 0;
        Ok(())
    }
    pub fn retained(&self) -> Result<(usize, usize), Error> {
        let at = (self.clock)();
        let mut state = self.state.lock().map_err(|_| Error::StateUnavailable)?;
        Self::expire(&mut state, at);
        Ok((state.entries.len(), state.bytes))
    }
}
impl Ticket {
    pub(crate) fn attach_matches(&self, call_id: &str, call: &AdmittedCall) -> bool {
        if self.store.owner != call.owner() || self.store.epoch != call.epoch() {
            return false;
        }
        let Ok(signature) = approval_fingerprint(call.name(), call.arguments()) else {
            return false;
        };
        self.store
            .state
            .lock()
            .ok()
            .and_then(|state| {
                state
                    .entries
                    .get(&self.id)
                    .map(|e| e.call_id == call_id && e.signature == signature)
            })
            .unwrap_or(false)
    }
    pub(crate) fn consume(
        self,
        parent_id: Option<&str>,
        call_id: &str,
        call: &AdmittedCall,
    ) -> Result<Authority, Error> {
        self.store.live()?;
        if parent_id != Some(self.store.task_id.as_str())
            || call.owner() != self.store.owner
            || call.epoch() != self.store.epoch
        {
            return Err(Error::Binding);
        }
        let now = (self.store.clock)();
        let mut state = self
            .store
            .state
            .lock()
            .map_err(|_| Error::StateUnavailable)?;
        if state.closed {
            return Err(Error::Closed);
        }
        let entry = Store::remove(&mut state, self.id).ok_or(Error::Missing)?;
        let age = now.checked_sub(entry.at).ok_or(Error::Expired)?;
        if age > Duration::from_secs(APPROVAL_RECEIPT_TTL_SECONDS as u64) {
            return Err(Error::Expired);
        }
        let signature =
            approval_fingerprint(call.name(), call.arguments()).map_err(|_| Error::Arguments)?;
        if entry.call_id != call_id || entry.signature != signature {
            return Err(Error::Binding);
        }
        Ok(entry.authority)
    }
}
