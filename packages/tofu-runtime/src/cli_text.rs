//! Awaited atomic text frames over the existing slot; no store/model authority.
use serde_json::json;
use std::{
    future::Future,
    pin::Pin,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
};
use tofu_runtime::{
    http::{TextDeliveryError, TextObserver},
    provider::TextChannel,
    session::ExecutionSession,
};

pub(crate) struct Text {
    runtime: Arc<tokio::runtime::Runtime>,
    session: Arc<ExecutionSession>,
    owner: u64,
    task: String,
    version: String,
    piece_bytes: usize,
    sequence: AtomicU64,
    control: Arc<crate::cli_agent_control::Exchange>,
}
impl Text {
    pub(crate) fn new(
        runtime: Arc<tokio::runtime::Runtime>,
        session: Arc<ExecutionSession>,
        owner: u64,
        task: String,
        version: String,
        frame_bytes: usize,
        control: Arc<crate::cli_agent_control::Exchange>,
    ) -> Result<Self, TextDeliveryError> {
        let atomic = unsafe { libc::fpathconf(1, libc::_PC_PIPE_BUF) };
        if atomic <= 0 {
            return Err(TextDeliveryError);
        }
        let empty = json!({"version":version,"type":"text_delta","request_id":format!("{task}-text-{}",u64::MAX),"owner_user_id":owner,
            "task_id":task,"sequence":u64::MAX,"channel":"reasoning","text":""});
        let overhead = serde_json::to_vec(&empty)
            .map_err(|_| TextDeliveryError)?
            .len()
            + 1;
        let piece_bytes = frame_bytes
            .min(atomic as usize)
            .checked_sub(overhead)
            .ok_or(TextDeliveryError)?
            / 6;
        if piece_bytes < 4 {
            return Err(TextDeliveryError);
        }
        Ok(Self {
            runtime,
            session,
            owner,
            task,
            version,
            piece_bytes,
            sequence: AtomicU64::new(0),
            control,
        })
    }
}
impl TextObserver for Text {
    fn observe<'a>(
        &'a self,
        channel: TextChannel,
        text: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<(), TextDeliveryError>> + Send + 'a>> {
        Box::pin(async move {
            let channel = match channel {
                TextChannel::Content => "content",
                TextChannel::Reasoning => "reasoning",
            };
            let mut remaining = text;
            while !remaining.is_empty() {
                let mut end = remaining.len().min(self.piece_bytes);
                while !remaining.is_char_boundary(end) {
                    end -= 1;
                }
                let sequence = self
                    .sequence
                    .fetch_update(Ordering::AcqRel, Ordering::Acquire, |n| n.checked_add(1))
                    .map_err(|_| TextDeliveryError)?
                    + 1;
                let id = format!("{}-text-{sequence}", self.task);
                let bytes = serde_json::to_vec(&json!({"version":self.version,"type":"text_delta",
                    "request_id":id,
                    "owner_user_id":self.owner,"task_id":self.task,"sequence":sequence,
                    "channel":channel,"text":&remaining[..end]}))
                .map_err(|_| TextDeliveryError)?;
                let stop = async {
                    tokio::select! {biased;_=self.session.cancelled()=>{},
                    _=crate::cli_agent_rounds::deadline(&self.session)=>{}}
                };
                self.control
                    .prepare(id, "text_ack")
                    .map_err(|_| TextDeliveryError)?;
                crate::cli_agent::publish_request(&self.runtime, &bytes, stop, &self.control)
                    .await
                    .map_err(|_| TextDeliveryError)?;
                // A published frame owns a host callback. Stop cannot abandon
                // its ACK; EOF/fault closes the existing joined reader instead.
                let reply = self
                    .control
                    .response()
                    .await
                    .map_err(|_| TextDeliveryError)?;
                if reply["accepted"] != true {
                    return Err(TextDeliveryError);
                }
                remaining = &remaining[end..];
            }
            Ok(())
        })
    }
}
