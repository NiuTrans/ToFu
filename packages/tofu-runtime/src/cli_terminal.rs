//! Frozen terminal persistence over exclusive post-settlement stdio. The live
//! control reader is joined first; no model/tool work repeats during host retry.
use crate::{cli_agent::Input, cli_agent_control::ReaderLease};
use serde_json::json;
use std::{
    os::fd::{AsRawFd, RawFd},
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use tofu_runtime::{
    task::{PortError, TerminalCandidate, TerminalPersistence},
    tool_contract::Catalog,
};
struct Stdout(i32);
impl AsRawFd for Stdout {
    fn as_raw_fd(&self) -> RawFd {
        1
    }
}
impl Drop for Stdout {
    fn drop(&mut self) {
        unsafe {
            libc::fcntl(1, libc::F_SETFL, self.0);
        }
    }
}
pub(crate) struct Persistence {
    pub runtime: Arc<tokio::runtime::Runtime>,
    pub reader: Arc<ReaderLease>,
    pub schemas: Arc<Catalog>,
    pub version: String,
    pub operation: String,
    pub bytes: usize,
    pub grace: Duration,
    pub attempts: usize,
    pub retry: AtomicBool,
    pub ordinal: AtomicUsize,
    pub input: Mutex<Option<Input>>,
    pub checkpoint_suspended: Option<Arc<AtomicBool>>,
}
impl TerminalPersistence for Persistence {
    fn persist_once(&self, candidate: &TerminalCandidate) -> Result<(), PortError> {
        self.retry.store(false, Ordering::SeqCst);
        let ordinal = self.ordinal.fetch_add(1, Ordering::SeqCst) + 1;
        if ordinal > self.attempts {
            return Err(PortError);
        }
        let receipt = &candidate.receipt;
        let mut value = json!({"version":self.version,"type":"terminal_request","request_id":self.operation,"owner_user_id":candidate.identity.owner_user_id,"task_id":candidate.identity.task_id,"attempt":ordinal,"event":candidate.event(),"receipt":{"execution_id":receipt.execution_id,"requested_outcome":receipt.requested_outcome.as_str(),"outcome":receipt.outcome.as_str(),"invariants_satisfied":receipt.invariants_satisfied,"resource_count":receipt.resource_dispositions.len()}});
        if let Some(marker) = &self.checkpoint_suspended {
            value["checkpoint_suspended"] = json!(marker.load(Ordering::SeqCst)
                && candidate.status == tofu_runtime::task::TaskStatus::Aborted);
        }
        self.schemas
            .validate(
                candidate.identity.owner_user_id,
                "terminal_request",
                value.clone(),
            )
            .map_err(|_| PortError)?;
        let mut bytes = serde_json::to_vec(&value).map_err(|_| PortError)?;
        if bytes.len() > self.bytes {
            return Err(PortError);
        }
        bytes.push(b'\n');
        let mut carrier = self.input.lock().map_err(|_| PortError)?;
        if carrier.is_none() {
            let tail = self.reader.take_tail().map_err(|_| PortError)?;
            let _enter = self.runtime.enter();
            *carrier = Some(Input::with_tail(tail).map_err(|_| PortError)?);
        }
        let reply = self.runtime.block_on(async {
            let until = tokio::time::Instant::now()
                .checked_add(self.grace)
                .ok_or(PortError)?;
            tokio::time::timeout_at(until, async {
                let flags = unsafe { libc::fcntl(1, libc::F_GETFL) };
                if flags < 0
                    || unsafe { libc::fcntl(1, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0
                {
                    return Err(PortError);
                }
                let out = tokio::io::unix::AsyncFd::new(Stdout(flags)).map_err(|_| PortError)?;
                let mut offset = 0;
                while offset < bytes.len() {
                    let mut ready = out.writable().await.map_err(|_| PortError)?;
                    match ready.try_io(|fd| {
                        let n = unsafe {
                            libc::write(
                                fd.get_ref().as_raw_fd(),
                                bytes[offset..].as_ptr().cast(),
                                bytes.len() - offset,
                            )
                        };
                        if n < 0 {
                            Err(std::io::Error::last_os_error())
                        } else {
                            Ok(n as usize)
                        }
                    }) {
                        Ok(Ok(0)) => return Err(PortError),
                        Ok(Ok(n)) => offset += n,
                        Ok(Err(_)) => return Err(PortError),
                        Err(_) => {}
                    }
                }
                drop(out);
                loop {
                    if tokio::time::Instant::now() >= until {
                        return Err(PortError);
                    }
                    let reply = carrier
                        .as_mut()
                        .unwrap()
                        .frame(self.bytes)
                        .await
                        .map_err(|_| PortError)?;
                    if reply["type"] == "cancel" {
                        self.schemas
                            .validate(candidate.identity.owner_user_id, "cancel", reply)
                            .map_err(|_| PortError)?;
                        tokio::task::yield_now().await;
                        continue;
                    }
                    self.schemas
                        .validate(
                            candidate.identity.owner_user_id,
                            "terminal_ack",
                            reply.clone(),
                        )
                        .map_err(|_| PortError)?;
                    if reply["request_id"] != self.operation || reply["attempt"] != ordinal {
                        return Err(PortError);
                    }
                    return Ok(reply);
                }
            })
            .await
            .map_err(|_| PortError)?
        });
        match reply {
            Ok(reply) if reply["accepted"] == true && reply["retry"] == false => {
                *carrier = None;
                Ok(())
            }
            Ok(reply)
                if reply["accepted"] == false
                    && reply["retry"] == true
                    && ordinal < self.attempts =>
            {
                self.retry.store(true, Ordering::SeqCst);
                Err(PortError)
            }
            _ => {
                *carrier = None;
                Err(PortError)
            }
        }
    }
}
