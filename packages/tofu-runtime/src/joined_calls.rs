//! Shared withdraw-and-join lifetime for opaque native ABI handles.
//! Values outlive admitted calls; callers retain executable code through close.
use std::sync::{Arc, Condvar, Mutex};
pub(crate) struct Calls {
    pub(crate) closed: bool,
    pub(crate) active: usize,
}
pub(crate) struct Joined<T, const CLOSED: u32, const INTERNAL: u32> {
    pub(crate) value: T,
    pub(crate) calls: Mutex<Calls>,
    changed: Condvar,
}
impl<T, const CLOSED: u32, const INTERNAL: u32> Joined<T, CLOSED, INTERNAL> {
    pub(crate) fn new(value: T) -> Self {
        Self {
            value,
            calls: Mutex::new(Calls {
                closed: false,
                active: 0,
            }),
            changed: Condvar::new(),
        }
    }
    pub(crate) fn enter(self: &Arc<Self>) -> Result<Lease<T, CLOSED, INTERNAL>, u32> {
        let mut calls = self.calls.lock().map_err(|_| INTERNAL)?;
        if calls.closed {
            return Err(CLOSED);
        }
        calls.active = calls.active.checked_add(1).ok_or(INTERNAL)?;
        Ok(Lease(self.clone()))
    }
    pub(crate) fn close(&self) -> Result<(), u32> {
        let mut calls = self.calls.lock().map_err(|_| INTERNAL)?;
        calls.closed = true;
        while calls.active != 0 {
            calls = self.changed.wait(calls).map_err(|_| INTERNAL)?;
        }
        Ok(())
    }
}
pub(crate) struct Lease<T, const CLOSED: u32, const INTERNAL: u32>(pub(crate) Arc<Joined<T, CLOSED, INTERNAL>>);
impl<T, const CLOSED: u32, const INTERNAL: u32> Drop for Lease<T, CLOSED, INTERNAL> {
    fn drop(&mut self) {
        if let Ok(mut calls) = self.0.calls.lock() {
            calls.active -= 1;
            self.0.changed.notify_all();
        }
    }
}
