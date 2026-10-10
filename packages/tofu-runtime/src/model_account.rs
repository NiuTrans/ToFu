//! Bounded native invocation-tree model account: explicit owners, input/output
//! reservations, dispatch intent and observed settlement. No tokenizer, price,
//! repository or HTTP defaults. Callers apply the lease output cap to sealed wire
//! ingress before dispatch; session cancellation/deadlines remain caller-owned.
use crate::usage::{self, Observation};
use serde_json::Value;
use std::{
    collections::BTreeMap,
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{Arc, Mutex},
};
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Invalid,
    Owner,
    Capacity,
    Wait,
    Exhausted,
    UnknownUsage,
    Overflow,
    Observer,
    Busy,
}
#[derive(Clone, Copy, Debug)]
pub struct Bounds {
    pub actors: usize,
    pub attempts: usize,
    pub identity_bytes: usize,
}
#[derive(Clone, Copy, Debug)]
pub struct Counts {
    pub input: u64,
    pub output: u64,
    pub minimum_output: u64,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    DispatchIntent,
    Settled,
    Voided,
}
#[derive(Clone, Debug)]
pub struct Receipt {
    pub owner: u64,
    pub root_task_id: String,
    pub task_id: String,
    pub ordinal: u64,
    pub reserved: u64,
    pub phase: Phase,
    pub observed: Observation,
    pub account_tokens: u64,
}
pub trait Observer: Send + Sync {
    fn observe(&self, receipt: &Receipt) -> Result<(), Error>;
}
struct Actor {
    cap: Option<u64>,
    used: u64,
    unknown: bool,
    active: bool,
}
struct Attempt {
    task: String,
    reserved: u64,
    dispatched: bool,
}
struct State {
    actors: BTreeMap<String, Actor>,
    attempts: BTreeMap<u64, Attempt>,
    used: u64,
    unknown: bool,
    ordinal: u64,
    restored: bool,
    observations: usize,
    closed: bool,
}
pub struct Account {
    owner: u64,
    root: String,
    cap: Option<u64>,
    bounds: Bounds,
    state: Mutex<State>,
    changed: tokio::sync::Notify,
    observer: Option<Arc<dyn Observer>>,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Snapshot {
    pub tokens: u64,
    pub reserved: u64,
    pub pending: usize,
    pub complete: bool,
    pub pending_observations: usize,
    pub last_ordinal: u64,
}
pub struct Lease {
    account: Arc<Account>,
    ordinal: u64,
    output: u64,
    finished: bool,
}
impl Account {
    pub fn new(
        owner: u64,
        root: String,
        cap: Option<u64>,
        bounds: Bounds,
        observer: Option<Arc<dyn Observer>>,
    ) -> Result<Arc<Self>, Error> {
        if owner == 0
            || root.is_empty()
            || root.len() > bounds.identity_bytes
            || bounds.actors == 0
            || bounds.attempts == 0
        {
            return Err(Error::Invalid);
        }
        let mut actors = BTreeMap::new();
        actors.insert(
            root.clone(),
            Actor {
                cap: None,
                used: 0,
                unknown: false,
                active: true,
            },
        );
        Ok(Arc::new(Self {
            owner,
            root,
            cap,
            bounds,
            state: Mutex::new(State {
                actors,
                attempts: BTreeMap::new(),
                used: 0,
                unknown: false,
                ordinal: 0,
                restored: false,
                observations: 0,
                closed: false,
            }),
            changed: tokio::sync::Notify::new(),
            observer,
        }))
    }
    fn owner(&self, owner: u64) -> Result<(), Error> {
        if owner == self.owner {
            Ok(())
        } else {
            Err(Error::Owner)
        }
    }
    pub fn authorize(&self, owner: u64, task: &str) -> Result<(), Error> {
        self.owner(owner)?;
        let s = self.state.lock().map_err(|_| Error::Invalid)?;
        if s.closed || s.actors.get(task).is_none_or(|a| !a.active) {
            Err(Error::Owner)
        } else {
            Ok(())
        }
    }
    pub fn attach(&self, owner: u64, task: String, cap: Option<u64>) -> Result<(), Error> {
        self.owner(owner)?;
        if task.is_empty() || task.len() > self.bounds.identity_bytes {
            return Err(Error::Invalid);
        }
        let mut s = self.state.lock().map_err(|_| Error::Invalid)?;
        if s.closed || s.actors.contains_key(&task) {
            return Err(Error::Owner);
        }
        if s.actors.len() >= self.bounds.actors {
            return Err(Error::Capacity);
        }
        s.actors.insert(
            task,
            Actor {
                cap,
                used: 0,
                unknown: false,
                active: true,
            },
        );
        Ok(())
    }
    /// Revoke future work; pending requests retain their account until settled.
    pub fn revoke(&self, owner: u64, task: &str) -> Result<(), Error> {
        self.owner(owner)?;
        let mut s = self.state.lock().map_err(|_| Error::Invalid)?;
        s.actors.get_mut(task).ok_or(Error::Owner)?.active = false;
        drop(s);
        self.changed.notify_waiters();
        Ok(())
    }
    pub fn detach(&self, owner: u64, task: &str) -> Result<(), Error> {
        self.owner(owner)?;
        let mut s = self.state.lock().map_err(|_| Error::Invalid)?;
        if task == self.root || s.observations != 0 || s.attempts.values().any(|r| r.task == task) {
            return Err(Error::Busy);
        }
        s.actors.remove(task).ok_or(Error::Owner)?;
        drop(s);
        self.changed.notify_waiters();
        Ok(())
    }
    pub fn snapshot(&self, owner: u64) -> Result<Snapshot, Error> {
        self.owner(owner)?;
        let s = self.state.lock().map_err(|_| Error::Invalid)?;
        Ok(Snapshot {
            tokens: s.used,
            reserved: s.attempts.values().try_fold(0u64, |n, r| {
                n.checked_add(r.reserved).ok_or(Error::Overflow)
            })?,
            pending: s.attempts.len(),
            pending_observations: s.observations,
            complete: s.observations == 0
                && !s.unknown
                && s.attempts.is_empty()
                && s.actors.keys().all(|k| k == &self.root),
            last_ordinal: s.ordinal,
        })
    }
    pub fn restore(
        &self,
        owner: u64,
        root: &str,
        tokens: u64,
        ordinal: u64,
        complete: bool,
    ) -> Result<(), Error> {
        self.owner(owner)?;
        let mut s = self.state.lock().map_err(|_| Error::Invalid)?;
        if root != self.root
            || s.closed
            || s.restored
            || s.ordinal != 0
            || s.used != 0
            || s.unknown
            || s.actors.len() != 1
            || !s.attempts.is_empty()
        {
            return Err(Error::Invalid);
        }
        s.used = tokens;
        s.ordinal = ordinal;
        s.unknown = !complete;
        s.restored = true;
        let actor = s.actors.get_mut(root).unwrap();
        actor.used = tokens;
        actor.unknown = !complete;
        Ok(())
    }
    pub fn close(&self, owner: u64) -> Result<(), Error> {
        self.owner(owner)?;
        let mut s = self.state.lock().map_err(|_| Error::Invalid)?;
        if !s.attempts.is_empty() || s.observations != 0 {
            return Err(Error::Busy);
        }
        s.closed = true;
        drop(s);
        self.changed.notify_waiters();
        Ok(())
    }
    /// Atomic fail-fast probe. Wait means live leases can release headroom.
    pub fn try_reserve(
        self: &Arc<Self>,
        owner: u64,
        task: &str,
        c: Counts,
    ) -> Result<Lease, Error> {
        self.owner(owner)?;
        if c.minimum_output == 0 {
            return Err(Error::Invalid);
        }
        let mut s = self.state.lock().map_err(|_| Error::Invalid)?;
        let actor = s.actors.get(task).ok_or(Error::Owner)?;
        if s.closed || !actor.active {
            return Err(Error::Owner);
        }
        if s.observations != 0 {
            return Err(Error::Wait);
        }
        if (s.unknown && self.cap.is_some()) || (actor.unknown && actor.cap.is_some()) {
            return Err(Error::UnknownUsage);
        }
        if self.cap.is_some_and(|cap| s.used >= cap) {
            return Err(Error::Exhausted);
        }
        let uncapped = c.output == 0 && self.cap.is_none() && actor.cap.is_none();
        if c.output < c.minimum_output && !uncapped {
            return Err(Error::Exhausted);
        }
        let total = c.input.checked_add(c.output).ok_or(Error::Overflow)?;
        let pending = s.attempts.values().try_fold(0u64, |n, r| {
            n.checked_add(r.reserved).ok_or(Error::Overflow)
        })?;
        let local_pending = s
            .attempts
            .values()
            .filter(|r| r.task == task)
            .try_fold(0u64, |n, r| {
                n.checked_add(r.reserved).ok_or(Error::Overflow)
            })?;
        let mut available = total;
        if let Some(cap) = self.cap {
            available = available.min(cap.saturating_sub(s.used).saturating_sub(pending));
        }
        if let Some(cap) = actor.cap {
            available = available.min(cap.saturating_sub(actor.used).saturating_sub(local_pending));
        }
        let output = c.output.min(available.saturating_sub(c.input));
        if available < c.input || (output < c.minimum_output && !uncapped) {
            return Err(if s.attempts.is_empty() {
                Error::Exhausted
            } else {
                Error::Wait
            });
        }
        if s.attempts.len() >= self.bounds.attempts {
            return Err(Error::Wait);
        }
        let ordinal = s.ordinal.checked_add(1).ok_or(Error::Overflow)?;
        let reserved = c.input.checked_add(output).ok_or(Error::Overflow)?;
        pending.checked_add(reserved).ok_or(Error::Overflow)?;
        s.ordinal = ordinal;
        s.attempts.insert(
            ordinal,
            Attempt {
                task: task.into(),
                reserved,
                dispatched: false,
            },
        );
        Ok(Lease {
            account: self.clone(),
            ordinal,
            output,
            finished: false,
        })
    }
    pub async fn reserve(
        self: &Arc<Self>,
        owner: u64,
        task: &str,
        c: Counts,
        stop: impl std::future::Future<Output = ()>,
    ) -> Result<Lease, Error> {
        let work = async {
            loop {
                let changed = self.changed.notified();
                tokio::pin!(changed);
                changed.as_mut().enable();
                match self.try_reserve(owner, task, c) {
                    Err(Error::Wait) => changed.await,
                    result => return result,
                }
            }
        };
        tokio::select! {biased;_=stop=>Err(Error::Owner),result=work=>result}
    }
    fn observe(&self, r: &Receipt) -> Result<(), Error> {
        if let Some(observer) = &self.observer {
            catch_unwind(AssertUnwindSafe(|| observer.observe(r)))
                .map_err(|_| Error::Observer)?
                .map_err(|_| Error::Observer)?;
        }
        Ok(())
    }
    fn validate_dispatch(&self, s: &State, r: &Attempt) -> Result<(), Error> {
        if s.observations != 0 {
            return Err(Error::Wait);
        }
        let actor = s.actors.get(&r.task).ok_or(Error::Owner)?;
        if s.closed || !actor.active {
            return Err(Error::Owner);
        }
        if (s.unknown && self.cap.is_some()) || (actor.unknown && actor.cap.is_some()) {
            return Err(Error::UnknownUsage);
        }
        let pending = s.attempts.values().try_fold(0u64, |n, r| {
            n.checked_add(r.reserved).ok_or(Error::Overflow)
        })?;
        if self
            .cap
            .is_some_and(|cap| s.used.checked_add(pending).is_none_or(|n| n > cap))
        {
            return Err(Error::Exhausted);
        }
        if let Some(cap) = actor.cap {
            let pending = s
                .attempts
                .values()
                .filter(|other| other.task == r.task)
                .try_fold(0u64, |n, row| {
                    n.checked_add(row.reserved).ok_or(Error::Overflow)
                })?;
            if actor.used.checked_add(pending).is_none_or(|n| n > cap) {
                return Err(Error::Exhausted);
            }
        }
        Ok(())
    }
}
// Internal dispatch/settlement ports cannot forge another account's ordinal.
impl Lease {
    pub fn output_tokens(&self) -> u64 {
        self.output
    }
    pub fn ordinal(&self) -> u64 {
        self.ordinal
    }
    pub fn mark_dispatched(&mut self, cancelled: impl Fn() -> bool) -> Result<(), Error> {
        if self.finished || cancelled() {
            return Err(Error::Owner);
        }
        let account = &self.account;
        let receipt = {
            let mut s = account.state.lock().map_err(|_| Error::Invalid)?;
            let row = s.attempts.get(&self.ordinal).ok_or(Error::Owner)?;
            account.validate_dispatch(&s, row)?;
            if row.dispatched {
                return Ok(());
            }
            if account.observer.is_none() {
                s.attempts.get_mut(&self.ordinal).unwrap().dispatched = true;
                return Ok(());
            }
            let receipt = Receipt {
                owner: account.owner,
                root_task_id: account.root.clone(),
                task_id: row.task.clone(),
                ordinal: self.ordinal,
                reserved: row.reserved,
                phase: Phase::DispatchIntent,
                observed: Observation {
                    tokens: 0,
                    complete: false,
                },
                account_tokens: s.used,
            };
            s.observations += 1;
            receipt
        };
        let observed = account.observe(&receipt);
        {
            let mut s = account.state.lock().map_err(|_| Error::Invalid)?;
            s.observations -= 1;
        }
        account.changed.notify_waiters();
        observed?;
        if cancelled() {
            return Err(Error::Owner);
        }
        let mut s = account.state.lock().map_err(|_| Error::Invalid)?;
        let row = s.attempts.get(&self.ordinal).ok_or(Error::Owner)?;
        account.validate_dispatch(&s, row)?;
        s.attempts.get_mut(&self.ordinal).unwrap().dispatched = true;
        Ok(())
    }
    pub fn settle(&mut self, raw: Option<&Value>, unsent: bool) -> Result<(), Error> {
        if self.finished {
            return Ok(());
        }
        let account = &self.account;
        let (receipt, overflow) = {
            let mut s = account.state.lock().map_err(|_| Error::Invalid)?;
            let row = s.attempts.remove(&self.ordinal).ok_or(Error::Owner)?;
            self.finished = true;
            let voided = unsent && !row.dispatched;
            let observed = if voided {
                Observation {
                    tokens: 0,
                    complete: true,
                }
            } else {
                raw.and_then(|v| usage::observed_totals(v).ok())
                    .unwrap_or(Observation {
                        tokens: 0,
                        complete: false,
                    })
            };
            let mut overflow = false;
            if let Some(n) = s.used.checked_add(observed.tokens) {
                s.used = n;
            } else {
                overflow = true;
            }
            if let Some(actor) = s.actors.get_mut(&row.task) {
                if let Some(n) = actor.used.checked_add(observed.tokens) {
                    actor.used = n;
                } else {
                    overflow = true;
                }
                actor.unknown |= !observed.complete || overflow;
            }
            s.unknown |= !observed.complete || overflow;
            if account.observer.is_none() {
                drop(s);
                account.changed.notify_waiters();
                return if overflow {
                    Err(Error::Overflow)
                } else {
                    Ok(())
                };
            }
            s.observations += 1;
            (
                Receipt {
                    owner: account.owner,
                    root_task_id: account.root.clone(),
                    task_id: row.task,
                    ordinal: self.ordinal,
                    reserved: row.reserved,
                    phase: if voided {
                        Phase::Voided
                    } else {
                        Phase::Settled
                    },
                    observed,
                    account_tokens: s.used,
                },
                overflow,
            )
        };
        let result = account.observe(&receipt);
        {
            let mut s = account.state.lock().map_err(|_| Error::Invalid)?;
            s.observations -= 1;
            if result.is_err() {
                s.unknown = true;
                if let Some(a) = s.actors.get_mut(&receipt.task_id) {
                    a.unknown = true;
                }
            }
        }
        account.changed.notify_waiters();
        if overflow {
            Err(Error::Overflow)
        } else {
            result
        }
    }
}
impl Drop for Lease {
    fn drop(&mut self) {
        if !self.finished {
            let _ = self.settle(None, true);
        }
    }
}
