//! Linux bounded stdio client-tool composition over the native agent core.
//! Trusted startup declares owner/provider/read tools; no route/store authority.
use crate::cli_http::{duration, number, size, Output};
use serde_json::{json, Value};
use std::{
    io::{self},
    os::fd::AsRawFd,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
};
use tofu_runtime::{
    agent::{Agent, Feedback, ToolFailure},
    agent_http::{Configuration, HttpProvider},
    http::{Cancellation, Transport},
    session::Registry as Sessions,
    task::{Task, TaskIdentity},
    tool_contract::*,
};
struct Stdin(i32);
impl AsRawFd for Stdin {
    fn as_raw_fd(&self) -> i32 {
        0
    }
}
impl Drop for Stdin {
    fn drop(&mut self) {
        unsafe {
            libc::fcntl(0, libc::F_SETFL, self.0);
        }
    }
}
struct Stdout(i32);
impl AsRawFd for Stdout {
    fn as_raw_fd(&self) -> i32 {
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
pub(crate) async fn publish_request(
    runtime: &tokio::runtime::Runtime,
    bytes: &[u8],
    cancelled: impl std::future::Future<Output = ()>,
    control: &crate::cli_agent_control::Exchange,
) -> io::Result<()> {
    publish_frame(runtime, bytes, cancelled, Some(control)).await
}
pub(crate) async fn publish_preparation(
    runtime: &tokio::runtime::Runtime, bytes: &[u8],
    cancelled: impl std::future::Future<Output = ()>,
    control: &crate::cli_agent_control::Exchange,
    session: &tofu_runtime::session::ExecutionSession,
) -> io::Result<()> {
    publish_frame_mode(runtime, bytes, cancelled, Some(control), true, crate::cli_agent_rounds::deadline(session)).await
}
pub(crate) async fn publish_frame(
    runtime: &tokio::runtime::Runtime,
    bytes: &[u8],
    cancelled: impl std::future::Future<Output = ()>,
    control: Option<&crate::cli_agent_control::Exchange>,
) -> io::Result<()> {
    publish_frame_mode(runtime, bytes, cancelled, control, false, std::future::pending()).await
}
async fn publish_frame_mode(
    runtime: &tokio::runtime::Runtime, bytes: &[u8],
    cancelled: impl std::future::Future<Output = ()>,
    control: Option<&crate::cli_agent_control::Exchange>, allow_large: bool,
    deadline: impl std::future::Future<Output = ()>,
) -> io::Result<()> {
    let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
    if unsafe { libc::fstat(1, stat.as_mut_ptr()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    if unsafe { stat.assume_init() }.st_mode & libc::S_IFMT != libc::S_IFIFO {
        return Err(io::ErrorKind::InvalidInput.into());
    }
    let atomic = unsafe { libc::fpathconf(1, libc::_PC_PIPE_BUF) };
    let mut frame = bytes.to_vec();
    frame.push(b'\n');
    if atomic <= 0 || (!allow_large && frame.len() > atomic as usize) {
        return Err(io::ErrorKind::InvalidData.into());
    }
    let flags = unsafe { libc::fcntl(1, libc::F_GETFL) };
    if flags < 0 {
        return Err(io::Error::last_os_error());
    }
    if unsafe { libc::fcntl(1, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(io::Error::last_os_error());
    }
    let fd = {
        let _enter = runtime.enter();
        tokio::io::unix::AsyncFd::new(Stdout(flags))?
    };
    tokio::pin!(cancelled, deadline);
    let mut offset = 0;
    let mut interrupted = false;
    loop {
        let mut ready = tokio::select! {
            biased;
            _ = &mut deadline => return Err(io::ErrorKind::TimedOut.into()),
            _ = &mut cancelled, if !interrupted => {
                if offset == 0 || !allow_large { return Err(io::ErrorKind::Interrupted.into()); }
                // A partially published exclusive frame remains owned. Finish it
                // under the original task deadline so ordinary Stop keeps framing.
                interrupted = true;
                continue;
            },
            ready = fd.writable() => ready?,
        };
        match ready.try_io(|fd| {
            let n=unsafe {libc::write(fd.get_ref().as_raw_fd(),frame[offset..].as_ptr().cast(),frame.len()-offset)};
            if n<0 {Err(io::Error::last_os_error())}
            else if n==0 || (!allow_large && n as usize != frame.len()) {Err(io::ErrorKind::WriteZero.into())}
            else {Ok(n as usize)}
        }) {
            Ok(Ok(n)) => {
                offset += n;
                if offset==frame.len() {
                    if let Some(control)=control {control.published().map_err(|_|io::Error::from(io::ErrorKind::InvalidData))?;}
                    return Ok(());
                }
            },
            Ok(Err(error))=>return Err(error),
            Err(_)=>{},
        }
    }
}

pub(crate) struct Input {
    fd: tokio::io::unix::AsyncFd<Stdin>,
    bytes: Vec<u8>,
}
impl Input {
    pub(crate) fn new() -> io::Result<Self> {
        let flags = unsafe { libc::fcntl(0, libc::F_GETFL) };
        if flags < 0 {
            return Err(io::Error::last_os_error());
        }
        if unsafe { libc::fcntl(0, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
            return Err(io::Error::last_os_error());
        }
        let stdin = Stdin(flags);
        let fd = tokio::io::unix::AsyncFd::new(stdin)?;
        Ok(Self { fd, bytes: vec![] })
    }
    pub(crate) fn tail(self) -> Vec<u8> {
        let Self { fd, bytes } = self;
        drop(fd);
        bytes
    }
    pub(crate) fn with_tail(bytes: Vec<u8>) -> io::Result<Self> {
        let mut input = Self::new()?;
        input.bytes = bytes;
        Ok(input)
    }
    pub(crate) async fn frame(&mut self, limit: usize) -> io::Result<Value> {
        loop {
            if let Some(end) = self.bytes.iter().position(|b| *b == b'\n') {
                if end > limit {
                    return Err(io::ErrorKind::InvalidData.into());
                }
                let value = serde_json::from_slice(&self.bytes[..end])
                    .map_err(|_| io::Error::from(io::ErrorKind::InvalidData))?;
                self.bytes.drain(..=end);
                return Ok(value);
            }
            if self.bytes.len() > limit {
                return Err(io::ErrorKind::InvalidData.into());
            }
            let mut ready = self.fd.readable().await?;
            let mut chunk = [0u8; 8192];
            let length = chunk
                .len()
                .min(limit.saturating_sub(self.bytes.len()).saturating_add(1));
            match ready.try_io(|fd| {
                let n = unsafe {
                    libc::read(fd.get_ref().as_raw_fd(), chunk.as_mut_ptr().cast(), length)
                };
                if n < 0 {
                    Err(io::Error::last_os_error())
                } else {
                    Ok(n as usize)
                }
            }) {
                Ok(Ok(0)) => return Err(io::ErrorKind::UnexpectedEof.into()),
                Ok(Ok(n)) => self.bytes.extend_from_slice(&chunk[..n]),
                Ok(Err(e)) => return Err(e),
                Err(_) => continue,
            }
        }
    }
}
struct HostPolicy { owner: u64, writes: bool }
impl Authorization for HostPolicy {
    fn requires_receipt(&self, _: &str, p: Permission, _: &Value, _: Option<&Value>) -> bool {
        p != Permission::Read
    }
    fn allow(&self, owner: u64, _: &str, p: Permission, _: &Value, _: Option<&Value>) -> bool {
        owner == self.owner && (p == Permission::Read || self.writes)
    }
}
struct ClientTool {
    name: String,
    version: String,
    task_id: String,
    sequence: Arc<AtomicU64>,
    runtime: Arc<tokio::runtime::Runtime>,
    control: Arc<crate::cli_agent_control::Exchange>,
    schemas: Arc<Catalog>,
    frame_bytes: usize,
    joined_host: bool,
}
impl Handler for ClientTool {
    fn invoke(&self, context: InvocationContext<'_>, arguments: &Value) -> Result<Value, Value> {
        let fail = || json!({"kind":"client_tool_protocol","message":"native client tool exchange refused"});
        let sequence = self
            .sequence
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_add(1))
            .map_err(|_| fail())?;
        let mut request = json!({"version":self.version,"type":"tool_request","request_id":format!("{}-{sequence}",self.task_id),"owner_user_id":context.owner,"task_id":self.task_id,"name":self.name,"arguments":arguments});
        request["model_round"] = context.model_round.ok_or_else(fail)?.into();
        if let Some(id) = context.approved_call_id { request["call_id"] = id.into(); }
        if let Some(caller) = context.caller { request["caller"] = caller.clone(); }
        self.schemas
            .validate(context.owner, "tool_request", request.clone())
            .map_err(|_| fail())?;
        let encoded = serde_json::to_vec(&request).map_err(|_| fail())?;
        if encoded.len() > self.frame_bytes {
            return Err(fail());
        }
        self.control.prepare(
            request["request_id"].as_str().ok_or_else(fail)?.into(),
            "tool_result",
        )?;
        self.runtime
            .block_on(publish_request(
                &self.runtime,
                &encoded,
                context.runtime.cancelled(),
                &self.control,
            ))
            .map_err(|_| fail())?;
        // A published request owns external work. Keep the existing reader
        // until its result/EOF; parent Stop cannot detach the host invocation.
        let response = if self.joined_host || context.approved_call_id.is_some() {
            self.runtime.block_on(self.control.response())?
        } else {
            self.runtime.block_on(async {tokio::select!{biased;
                _=context.runtime.cancelled()=>Err(fail()),value=self.control.response()=>value
            }})?
        };
        let response = self
            .schemas
            .validate(context.owner, "tool_result", response)
            .map_err(|_| fail())?;
        if response["request_id"] != request["request_id"] {
            return Err(fail());
        }
        if response.get("failed") == Some(&Value::Bool(true)) {
            return Err(json!({"kind":"host_tool","message":"host tool execution failed"}));
        }
        Ok(response["content"].clone())
    }
}
struct Text { multimodal: bool }
impl Feedback for Text {
    fn model_content(&self,result:Result<Value,ToolFailure>,limit:usize)->Result<Value,Value>{
        match result {
            Ok(value @ Value::Array(_)) if self.multimodal=>{
                tofu_runtime::model_content::validate(&value).map_err(|_|json!({"kind":"native_tool_model_content","message":"native tool content refused"}))?;
                Ok(value)
            }
            other=>self.content(other,limit).map(Value::String),
        }
    }
    fn content(&self, result: Result<Value, ToolFailure>, _: usize) -> Result<String, Value> {
        match result {
            Ok(Value::String(text)) => Ok(text),
            Err(ToolFailure::Admission(_)) => Ok("Tool call refused by native admission".into()),
            Err(ToolFailure::Dispatch(DispatchError::Failed(reason))) => Err(reason),
            Err(ToolFailure::ApprovalDenied(_)) => Ok("Tool call denied by owner approval policy".into()),
            _ => Err(
                json!({"kind":"native_client_tool_failure","message":"native client tool failed"}),
            ),
        }
    }
}
pub fn run() -> Result<(), Box<dyn std::error::Error>> {
    let limit: usize = std::env::args()
        .nth(2)
        .ok_or("missing configuration frame limit")?
        .parse()?;
    if limit == 0 || limit == usize::MAX {
        return Err("invalid configuration frame limit".into());
    }
    let runtime = Arc::new(
        tokio::runtime::Builder::new_current_thread()
            .max_blocking_threads(1)
            .enable_all()
            .build()?,
    );
    let mut input = {
        let _enter = runtime.enter();
        Input::new()?
    };
    let config = runtime.block_on(input.frame(limit))?;
    let policy = crate::cli_http::policy(&config)?;
    if policy.connections == 0 {
        return Err("invalid connection capacity".into());
    }
    // Rebind the exclusively owned stdin carrier to the configured runtime.
    // The bootstrap reactor started no provider/blocking work.
    let Input { fd, bytes } = input;
    let stdin = fd.into_inner();
    drop(runtime);
    let runtime = Arc::new(
        tokio::runtime::Builder::new_current_thread()
            .max_blocking_threads(policy.connections)
            .enable_all()
            .build()?,
    );
    let input = {
        let _enter = runtime.enter();
        Input {
            fd: tokio::io::unix::AsyncFd::new(stdin)?,
            bytes,
        }
    };
    let owner = number(&config, "owner_user_id")?;
    if owner == 0 {
        return Err("invalid owner".into());
    }
    let task_id = config["task_id"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or("missing task_id")?;
    let epoch = config["epoch"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or("missing epoch")?;
    let contract: Value = serde_json::from_str(include_str!(
        "../../../contracts/native_agent_stdio_v1.schema.json"
    ))?;
    let version = contract["$id"]
        .as_str()
        .ok_or("invalid stdio contract")?
        .to_owned();
    let schemas=Arc::new(Catalog::compile(owner,epoch,&Value::Object(contract["$defs"].as_object().ok_or("invalid frame schemas")?.iter().map(|(name,schema)|(name.clone(),json!({"name":name,"contractVersion":tofu_runtime::execution_contract::TOOL_CONTRACT_VERSION,"arguments_schema":schema}))).collect())).map_err(|_|"invalid frame contract")?);
    schemas
        .validate(owner, "configure", config.clone())
        .map_err(|_| "invalid native agent configuration frame")?;
    if !config["proxy"].is_null() && !config["proxy"].is_string() {
        return Err("invalid proxy".into());
    }
    let tools = config["tools"].as_object().ok_or("missing tool catalog")?;
    if tools.values().any(|tool| tool["permission"] != "read") && config.get("approval_control").is_none() {
        return Err("native non-read tools require explicit host approval control".into());
    }
    let catalog =
        Catalog::compile(owner, epoch, &config["tools"]).map_err(|_| "invalid tool catalog")?;
    let mut template = config["template"]
        .as_object()
        .ok_or("missing template")?
        .clone();
    if template.contains_key("tools") {
        return Err("tools derive from compiled catalog".into());
    }
    template.insert("tools".into(),Value::Array(tofu_runtime::execution_contract::native_model_tools(tools)));
    let messages = config["messages"]
        .as_array()
        .ok_or("missing messages")?
        .clone();
    let frame_bytes = size(&config, "tool_frame_bytes")?;
    let preparation_bytes = if config["round_preparation"] == true {
        if config["round_control"] != true { return Err("round preparation requires round control".into()); }
        size(&config, "round_preparation_bytes")?
    } else { frame_bytes };
    let checkpoint_bytes = if config["checkpoint_control"]==true {size(&config,"checkpoint_frame_bytes")?}else{frame_bytes};
    if frame_bytes == 0 {
        return Err("invalid tool frame limit".into());
    }
    let transport = {
        let _enter = runtime.enter();
        Transport::new(
            config["endpoint"].as_str().ok_or("missing endpoint")?,
            config["proxy"].as_str(),
            policy,
        )
        .map_err(|_| "invalid transport")?
    };
    let cancellation = Cancellation::new();
    let provider = HttpProvider::new(
        &runtime,
        &transport,
        Configuration {
            template,
            headers: crate::cli_http::headers(&config)?,
            budget: tofu_runtime::provider::Budget {
                event_bytes: size(&config, "event_bytes")?,
                output_bytes: size(&config, "output_bytes")?,
                tool_calls: size(&config, "tool_calls")?,
            },
            request_bytes: size(&config, "request_bytes")?,
            identical_tool_limit: config["identical_tool_limit"]
                .as_u64()
                .map(usize::try_from)
                .transpose()?,
        },
        &cancellation,
    )
    .map_err(|_| "invalid provider composition")?;
    if config.get("responses_profile").is_some() && config["provider_protocol"] != "responses" {
        return Err("Responses profile requires Responses protocol".into());
    }
    let provider = if config["provider_protocol"] == "anthropic" {
        provider.with_anthropic(tofu_runtime::anthropic::Budget {
            blocks: tofu_runtime::execution_contract::ANTHROPIC_MAX_BLOCK_INDEX + 1,
            retained_bytes: size(&config, "output_bytes")?,
        }).map_err(|_|"invalid Anthropic provider composition")?
    } else if config["provider_protocol"] == "responses" {
        provider.with_responses_profile(tofu_runtime::responses::Budget {
            items: tofu_runtime::execution_contract::RESPONSES_MAX_OUTPUT_INDEX + 1,
            retained_bytes: size(&config, "output_bytes")?,
        }, config["responses_reasoning_replay"].as_bool().unwrap_or(false),
        tofu_runtime::responses_request::Profile::parse(config["responses_profile"].as_str().unwrap_or("compatible"))?)
        .map_err(|_|"invalid Responses provider composition")?
    } else { provider };
    let external_model = config.get("model_control").is_some();
    if external_model != config.get("model_counter").is_some() || (external_model && (config.get("token_admission").is_some() || (config["checkpoint_control"] == true && config["checkpoint_shared_account"] != true))) {
        return Err("model control requires one counter/authority and explicit external recovery".into());
    }
    if external_model { schemas.validate(owner,"model_control",config["model_control"].clone()).map_err(|_|"invalid model control")?; }
    if config["checkpoint_shared_account"] == true && (!external_model || config["checkpoint_control"] != true) {
        return Err("shared checkpoint requires host account and checkpoint control".into());
    }
    let mut external_counter = None;
    let mut checkpoint_account=None;
    let mut checkpoint_account_policy=None;
    let provider = if let Some(profile) = config.get("token_admission").or_else(|| config.get("model_counter")) {
        schemas
            .validate(owner, if external_model {"model_counter"} else {"token_admission"}, profile.clone())
            .map_err(|_| "invalid token admission")?;
        let encoding = match profile["encoding"].as_str() {
            Some("cl100k_base") => tofu_runtime::token_ingress::Encoding::Cl100k,
            Some("o200k_base") => tofu_runtime::token_ingress::Encoding::O200k,
            _ => return Err("unsupported token encoding".into()),
        };
        let output = match profile["output_field"].as_str() {
            Some("max_tokens") => tofu_runtime::agent_http::OutputField::MaxTokens,
            Some("max_completion_tokens") => {
                tofu_runtime::agent_http::OutputField::MaxCompletionTokens
            }
            _ => return Err("unsupported output field".into()),
        };
        let cap = profile
            .get("total_tokens")
            .map(|v| v.as_u64().ok_or("invalid token cap"))
            .transpose()?;
        let counter = tofu_runtime::token_ingress::Counter::new(
            owner,
            task_id.into(),
            config["template"]["model"]
                .as_str()
                .ok_or("missing model")?
                .into(),
            tofu_runtime::token_ingress::Encoder::new(encoding),
            tofu_runtime::token_ingress::Bounds {
                input_bytes: size(profile, "input_bytes")?,
                text_bytes: size(profile, "text_bytes")?,
                tool_json_bytes: size(profile, "tool_json_bytes")?,
                depth: size(profile, "depth")?,
            },
        )
        .map_err(|_| "invalid token bounds")?;
        let counter: Arc<dyn tofu_runtime::agent_http::InputCounter> = Arc::new(crate::cli_token_counter::Worker {
            counter, runtime:runtime.clone(), stack_bytes:size(profile,"worker_stack_bytes")?,
        });
        if external_model {
            external_counter = Some((counter, output));
            CliProvider::Raw(provider)
        } else {
        let account = tofu_runtime::model_account::Account::new(
            owner,
            task_id.into(),
            cap,
            tofu_runtime::model_account::Bounds {
                actors: size(profile, "actor_records")?,
                attempts: size(profile, "attempt_records")?,
                identity_bytes: size(profile, "identity_bytes")?,
            },
            None,
        )
        .map_err(|_| "invalid native account")?;
        checkpoint_account=Some(account.clone());
        checkpoint_account_policy=Some(json!({"encoding":profile["encoding"],"output_field":profile["output_field"],"total_tokens":cap}));
            CliProvider::Counted(provider.with_account(account,owner,task_id.into(),counter,output)
                .map_err(|_| "invalid counted provider")?)
        }
    } else { CliProvider::Raw(provider) };
    let sessions = Sessions::new();
    let relative = duration(&config, "deadline_ms")?;
    let deadline_unix_ms = config.get("deadline_unix_ms").map(|value|
        value.as_u64().ok_or("invalid absolute deadline")
    ).transpose()?;
    let session = if let Some(deadline) = deadline_unix_ms {
        let observed = std::time::Instant::now();
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| "absolute deadline clock unavailable")?;
        let remaining = std::time::Duration::from_millis(deadline).saturating_sub(now).min(relative);
        sessions.start_observed(task_id, "native_agent", owner, "", remaining, observed)
    } else {
        sessions.start(task_id, "native_agent", owner, "", Some(relative))
    }.map_err(|_| "invalid session")?;
    let (control, reader) = crate::cli_agent_control::start(
        runtime.clone(),
        input,
        session.clone(),
        schemas.clone(),
        owner,
        frame_bytes.max(preparation_bytes).max(checkpoint_bytes),
        cancellation.clone(),
    );
    let release = reader.clone();
    let rollback = reader.clone();
    session
        .bind_acquired(
            tofu_runtime::execution_contract::ResourceName::Upstream,
            0,
            false,
            Box::new(move |_| {
                release
                    .finish()
                    .map_err(|_| tofu_runtime::session::ReleaseError)
            }),
            Box::new(move || {
                let _ = rollback.finish();
            }),
            None,
        )
        .map_err(|_| "client control binding failed")?;
    let sequence = Arc::new(AtomicU64::new(0));
    let handlers = Registry::new(
        owner,
        epoch,
        tools
            .keys()
            .map(|name| {
                (
                    name.clone(),
                    Arc::new(ClientTool {
                        name: name.clone(),
                        version: version.clone(),
                        task_id: task_id.into(),
                        sequence: sequence.clone(),
                        runtime: runtime.clone(),
                        control: control.clone(),
                        schemas: schemas.clone(),
                        frame_bytes,
                        joined_host: config.get("tool_result_join") == Some(&Value::Bool(true)),
                    }) as Arc<dyn Handler>,
                )
            })
            .collect(),
    )
    .map_err(|_| "invalid tool registry")?;
    let provider = if config.get("text_progress") == Some(&Value::Bool(true)) {
        provider.with_text_observer(Arc::new(crate::cli_text::Text::new(
            runtime.clone(), session.clone(), owner, task_id.into(), version.clone(), frame_bytes,control.clone(),
        ).map_err(|_| "invalid text progress budget")?))
    } else { provider };
    let provider = if let Some((counter,output)) = external_counter {
        let CliProvider::Raw(provider) = provider else { return Err("invalid model source".into()); };
        let port = Arc::new(crate::cli_model_control::Port {
            runtime:runtime.clone(),control:control.clone(),schemas:schemas.clone(),owner,
            task:task_id.into(),version:version.clone(),frame_bytes,
            ack:duration(&config["model_control"],"ack_ms")?,ordinal:std::sync::atomic::AtomicU64::new(0),
        });
        CliProvider::Counted(provider.with_model_admission(Arc::new(crate::cli_model_control::Authority(port)),owner,task_id.into(),counter,output)
            .map_err(|_|"invalid host model authority")?)
    } else {provider};
    let provider = crate::cli_agent_control::ControlledProvider {
        inner: provider,
        control: control.clone(),
    };
    let output = Arc::new(Output(Mutex::new(None)));
    let task_session = session.clone();
    let checkpoint_suspended = if config["checkpoint_terminal_fence"] == true {
        if config["checkpoint_control"] != true || config.get("terminal_persistence").is_none() {
            return Err("checkpoint terminal fence requires checkpoint and persistence policies".into());
        }
        Some(Arc::new(std::sync::atomic::AtomicBool::new(false)))
    } else { None };
    let persistence = if let Some(profile) = config.get("terminal_persistence") {
        schemas
            .validate(owner, "terminal_persistence", profile.clone())
            .map_err(|_| "invalid persistence profile")?;
        Some(Arc::new(crate::cli_terminal::Persistence {
            runtime: runtime.clone(),
            reader: reader.clone(),
            schemas: schemas.clone(),
            version: version.clone(),
            operation: profile["operation_id"]
                .as_str()
                .ok_or("missing terminal operation")?
                .into(),
            bytes: size(profile, "frame_bytes")?,
            grace: duration(profile, "ack_ms")?,
            attempts: size(profile, "attempts")?,
            retry: std::sync::atomic::AtomicBool::new(false),
            ordinal: std::sync::atomic::AtomicUsize::new(0),
            input: Mutex::new(None),
            checkpoint_suspended: checkpoint_suspended.clone(),
        }))
    } else {
        None
    };
    let task = Task::new(
        TaskIdentity {
            task_id: task_id.into(),
            owner_user_id: owner,
        },
        session,
        persistence
            .clone()
            .map(|p| p as Arc<dyn tofu_runtime::task::TerminalPersistence>),
        output.clone(),
    )
    .map_err(|_| "invalid task")?;
    if config["round_preparation"] == true && config["round_control"] != true {
        return Err("round preparation requires round control".into());
    }
    let progress_limits = if config["progress_control"] == true {
        if config["round_control"] != true { return Err("progress requires round control".into()); }
        let read=|key:&str|->Result<usize,Box<dyn std::error::Error>> {Ok(usize::try_from(config[key].as_u64().ok_or("invalid progress limit")?)?)};
        let limits=tofu_runtime::progress::Limits {no_progress_rounds:read("no_progress_rounds")?,nonretryable_failure_rounds:read("nonretryable_failure_rounds")?};
        if limits.no_progress_rounds==0 && limits.nonretryable_failure_rounds==0 {return Err("progress requires an enabled breaker".into());}
        limits
    } else {tofu_runtime::progress::Limits::default()};
    if config["checkpoint_control"]==true && (config["round_control"]!=true || config["checkpoint_binding"].as_str().is_none_or(|v|v.is_empty())) {return Err("checkpoint composition requires round policy and binding".into());}
    let rounds = crate::cli_agent_rounds::Rounds {
        session: task_session.clone(),
        runtime: runtime.clone(),
        control: control.clone(),
        schemas: schemas.clone(),
        version: version.clone(),
        owner,
        task_id: task_id.into(),
        epoch: epoch.into(),
        checkpoint_binding: config.get("checkpoint_binding").and_then(Value::as_str).map(String::from),
        checkpoint_account,
        checkpoint_account_policy,
        shared_counter_policy: (config["checkpoint_shared_account"] == true).then(|| json!({"encoding":config["model_counter"]["encoding"],"output_field":config["model_counter"]["output_field"],"wire_model":config["template"]["model"]})),
        deadline_unix_ms,
        checkpoint_suspended,
        frame_bytes: preparation_bytes.max(checkpoint_bytes),
        progress_limits,
        prepare_history: config["round_preparation"] == true,
        preparation_deadline: (config["round_preparation"] == true || config["progress_control"] == true || config["checkpoint_control"]==true).then(||Arc::new(std::sync::OnceLock::new())),
    };
    let approvals = if let Some(profile) = config.get("approval_control") {
        schemas.validate(owner,"approval_control",profile.clone()).map_err(|_|"invalid approval profile")?;
        Some(crate::cli_approval::Approvals::new(
            runtime.clone(),control.clone(),schemas.clone(),task_session.clone(),
            owner,task_id.into(),epoch.into(),version.clone(),frame_bytes,
            size(profile,"receipts")?,size(profile,"identity_bytes")?,
        ).map_err(|_|"invalid approval composition")?)
    } else {None};
    let authorization = HostPolicy { owner, writes: approvals.is_some() };
    let agent = Agent {
        provider: &provider,
        catalog: &catalog,
        handlers: &handlers,
        authorization: &authorization,
        feedback: &Text {multimodal:config["tool_feedback"]=="multimodal"},
        sessions: &sessions,
        epoch,
        budget: tofu_runtime::agent::Budget {
            rounds: size(&config, "rounds")?,
            tool_calls: size(&config, "tool_calls")?,
            retained_bytes: size(&config, "retained_bytes")?,
            feedback_bytes: size(&config, "feedback_bytes")?,
        },
        approvals: approvals.as_ref().map(|p|p as &dyn tofu_runtime::agent::Approvals),
        round_admission: if config["round_control"] == true {
            Some(&rounds)
        } else {
            None
        },
        checkpoints: if config["checkpoint_control"]==true {Some(&rounds)}else{None},
        progress: if config["progress_control"] == true {Some(&rounds)} else {None},
        scheduling: None,
    };
    let mut finished = agent.run(&task, owner, task_id, messages);
    while matches!(
        finished,
        Err(tofu_runtime::task::TaskError::PersistencePending)
    ) && persistence
        .as_ref()
        .is_some_and(|p| p.retry.swap(false, std::sync::atomic::Ordering::SeqCst))
    {
        finished = task.finish(owner, None, None);
    }
    let candidate = finished.map_err(|_| "native terminal debt")?;
    if sessions
        .active_count()
        .map_err(|_| "session state unavailable")?
        != 0
    {
        return Err("native session cleanup failed".into());
    }
    println!(
        "{}",
        output
            .0
            .lock()
            .map_err(|_| "terminal output unavailable")?
            .as_ref()
            .ok_or("missing native terminal")?
    );
    if candidate.error.is_some() {
        return Err("native agent failed".into());
    }
    Ok(())
}

enum CliProvider<'a> {
    Raw(tofu_runtime::agent_http::HttpProvider<'a>),
    Counted(tofu_runtime::agent_http::BudgetedHttpProvider<'a>),
}
impl CliProvider<'_> {
    fn with_text_observer(self, observer: Arc<dyn tofu_runtime::http::TextObserver>) -> Self {
        match self {
            Self::Raw(provider) => Self::Raw(provider.with_text_observer(observer)),
            Self::Counted(provider) => Self::Counted(provider.with_text_observer(observer)),
        }
    }
}
impl tofu_runtime::agent::Provider for CliProvider<'_> {
    fn round(
        &self,
        messages: &[Value],
        session: &Arc<tofu_runtime::session::ExecutionSession>,
    ) -> Result<tofu_runtime::provider::StreamResult, tofu_runtime::agent::ProviderError> {
        match self {
            Self::Raw(p) => p.round(messages, session),
            Self::Counted(p) => p.round(messages, session),
        }
    }
}
