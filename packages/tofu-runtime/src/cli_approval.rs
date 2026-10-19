//! Explicit host decisions mint existing exact native single-use receipts.
//! One joined control slot; no UI, delegation or durable approval authority.
use crate::cli_agent_control::Exchange;
use serde_json::{json, Value};
use std::{
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
    time::Instant,
};
use tofu_runtime::{
    agent::ApprovalDecision,
    approval::{Authority, Budget, Store},
    session::ExecutionSession,
    tool_contract::{AdmittedCall, Catalog, Permission},
};
fn failure() -> Value {
    json!({"kind":"host_approval","message":"native approval exchange refused"})
}
pub(crate) struct Approvals {
    runtime: Arc<tokio::runtime::Runtime>,
    control: Arc<Exchange>,
    schemas: Arc<Catalog>,
    store: Arc<Store>,
    owner: u64,
    task: String,
    epoch: String,
    version: String,
    frame_bytes: usize,
    sequence: AtomicU64,
}
impl Approvals {
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new(
        runtime: Arc<tokio::runtime::Runtime>,
        control: Arc<Exchange>,
        schemas: Arc<Catalog>,
        parent: Arc<ExecutionSession>,
        owner: u64,
        task: String,
        epoch: String,
        version: String,
        frame_bytes: usize,
        receipts: usize,
        identity_bytes: usize,
    ) -> Result<Self, Value> {
        let start = Instant::now();
        let store = Store::new(
            &parent,
            owner,
            &task,
            &epoch,
            Budget {
                receipts,
                identity_bytes,
            },
            Arc::new(move || start.elapsed()),
        )
        .map_err(|_| failure())?;
        Ok(Self {
            runtime,
            control,
            schemas,
            store,
            owner,
            task,
            epoch,
            version,
            frame_bytes,
            sequence: AtomicU64::new(0),
        })
    }
}
impl tofu_runtime::agent::Approvals for Approvals {
    fn decide(
        &self,
        session: &Arc<ExecutionSession>,
        call_id: &str,
        call: &AdmittedCall,
    ) -> Result<ApprovalDecision, Value> {
        if call.permission() == Permission::Read {
            return Ok(ApprovalDecision::NotRequired);
        }
        if !session.belongs_to(self.owner, &self.task)
            || call.owner() != self.owner
            || call.epoch() != self.epoch
        {
            return Err(failure());
        }
        let sequence = self
            .sequence
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |n| n.checked_add(1))
            .map_err(|_| failure())?;
        let id = format!("{}-approval-{sequence}", self.task);
        let remaining = session
            .snapshot()
            .map_err(|_| failure())?
            .deadline_remaining_nanos;
        if remaining.is_some_and(|remaining| remaining <= 0) {
            return Err(failure());
        }
        let remaining_ms = remaining.map(|remaining|
            u64::try_from((remaining as u128).div_ceil(1_000_000)).unwrap_or(u64::MAX));
        let mut request = json!({"version":self.version,"type":"approval_request","request_id":id,
            "remaining_ms":remaining_ms,
            "owner_user_id":self.owner,"task_id":self.task,"epoch":self.epoch,"call_id":call_id,
            "name":call.name(),"arguments":call.arguments(),"caller":call.caller()});
        if call.caller().is_none() {
            request.as_object_mut().unwrap().remove("caller");
        }
        self.schemas
            .validate_protocol(self.owner, "approval_request", request.clone())
            .map_err(|_| failure())?;
        let bytes = serde_json::to_vec(&request).map_err(|_| failure())?;
        if bytes.len() > self.frame_bytes {
            return Err(failure());
        }
        self.control.prepare(id, "approval_decision")?;
        let reply=self.runtime.block_on(async {
            let stop=async {tokio::select!{_=session.cancelled()=>{},_=crate::cli_agent_rounds::deadline(session)=>{}}};
            crate::cli_agent::publish_request(&self.runtime,&bytes,stop,&self.control).await.map_err(|_|failure())?;
            // Published approval owns its host waiter through decision/EOF.
            self.control.response().await
        })?;
        let authority = match reply["decision"].as_str() {
            Some("denied") => {
                return Ok(ApprovalDecision::Denied(json!({"kind":"approval_denied"})))
            }
            Some("human") => Authority::Human,
            Some("auto_write_mode") => Authority::AutoWriteMode,
            Some("agent_adjudicator") => Authority::AgentAdjudicator,
            Some("agent_git_guard_ack") => Authority::AgentGitGuardAck,
            Some("agent_git_guard_solo") => Authority::AgentGitGuardSolo,
            Some("git_safe_write") => Authority::GitSafeWrite,
            _ => return Err(failure()),
        };
        self.store
            .mint(call_id, call, authority)
            .map(ApprovalDecision::Approved)
            .map_err(|_| failure())
    }
}
