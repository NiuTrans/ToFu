//! Explicit persistent stdio worker over the existing agent composition.
//! One fixed owner, monotonic run scope, joined readiness and lazy immutable
//! encoder reuse. Task/provider/account/control objects remain per invocation.
use serde_json::{json, Value};
use std::{io, sync::{Arc, OnceLock}};
use tofu_runtime::{tool_contract::Catalog, token_ingress::{Encoder, Encoding}};

pub(crate) struct Encoders {
    cl100k: OnceLock<Arc<Encoder>>,
    o200k: OnceLock<Arc<Encoder>>,
}
impl Encoders {
    fn new() -> Self { Self { cl100k: OnceLock::new(), o200k: OnceLock::new() } }
    pub(crate) fn get(&self, encoding: Encoding) -> Arc<Encoder> {
        let slot = match encoding { Encoding::Cl100k => &self.cl100k, Encoding::O200k => &self.o200k };
        slot.get_or_init(|| Encoder::new(encoding)).clone()
    }
    fn loaded(&self) -> usize {
        [&self.cl100k, &self.o200k].iter().filter(|slot| slot.get().is_some_and(|encoder| encoder.loaded())).count()
    }
}
pub(crate) struct Scope {
    pub owner: u64,
    pub sequence: u64,
    pub version: String,
    pub schemas: Arc<Catalog>,
}
impl Scope {
    pub(crate) fn wrap(&self, bytes: &[u8]) -> Result<Vec<u8>, serde_json::Error> {
        let mut result = serde_json::to_vec(&json!({"version":self.version,"type":"worker_event",
            "owner_user_id":self.owner,"sequence":self.sequence}))?;
        result.pop(); result.extend_from_slice(b",\"frame\":"); result.extend_from_slice(bytes); result.push(b'}');
        Ok(result)
    }
    pub(crate) fn control_overhead(&self) -> io::Result<usize> {
        serde_json::to_vec(&json!({"version":self.version,"type":"worker_control",
            "owner_user_id":self.owner,"sequence":self.sequence,"frame":{}}))
            .map(|bytes|bytes.len()-2).map_err(|_|io::ErrorKind::InvalidData.into())
    }
    pub(crate) fn unwrap(&self, value: Value, frame_limit: usize) -> io::Result<Value> {
        let mut value = self.schemas.validate_protocol(self.owner, "worker_control", value)
            .map_err(|_| io::Error::from(io::ErrorKind::InvalidData))?;
        if value["owner_user_id"].as_u64() != Some(self.owner) || value["sequence"].as_u64() != Some(self.sequence) {
            return Err(io::ErrorKind::InvalidData.into());
        }
        let frame = value.as_object_mut().and_then(|object| object.remove("frame"))
            .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidData))?;
        if serde_json::to_vec(&frame).map_err(|_|io::Error::from(io::ErrorKind::InvalidData))?.len()>frame_limit {
            return Err(io::ErrorKind::InvalidData.into());
        }
        Ok(frame)
    }
}
pub(crate) fn run() -> Result<(), Box<dyn std::error::Error>> {
    let limit: usize = std::env::args().nth(2).ok_or("missing worker frame limit")?.parse()?;
    let owner: u64 = std::env::args().nth(3).ok_or("missing worker owner")?.parse()?;
    if limit == 0 || limit == usize::MAX || owner == 0 { return Err("invalid worker authority/bounds".into()); }
    let contract: Value = serde_json::from_str(include_str!("../../../contracts/native_agent_stdio_v1.schema.json"))?;
    let version = contract["$defs"]["worker_run"]["properties"]["version"]["enum"][0]
        .as_str().ok_or("missing worker version")?.to_owned();
    let documents = contract["$defs"].as_object().ok_or("missing worker schemas")?.iter()
        .filter(|(name, _)| name.starts_with("worker_"))
        .map(|(name, schema)| (name.clone(), json!({"name":name,
            "contractVersion":tofu_runtime::execution_contract::TOOL_CONTRACT_VERSION,"arguments_schema":schema}))).collect();
    let schemas = Arc::new(Catalog::compile(owner, "native-worker", &Value::Object(documents))
        .map_err(|_| "invalid worker schema")?);
    let runtime = Arc::new(tofu_runtime::reactor::build()?);
    let encoders = Encoders::new();
    let mut sequence = 0u64;
    loop {
        let ready = json!({"version":version,"type":"worker_ready","owner_user_id":owner,
            "sequence":sequence,"cached_encodings":encoders.loaded()});
        schemas.validate_protocol(owner,"worker_ready",ready.clone()).map_err(|_|"invalid worker readiness")?;
        println!("{ready}");
        let mut input = { let _enter = runtime.enter(); crate::cli_agent::Input::new()? };
        let mut frame = match runtime.block_on(input.frame(limit)) {
            Ok(frame) => frame,
            Err(error) if error.kind() == io::ErrorKind::UnexpectedEof && input.is_empty() => return Ok(()),
            Err(error) => return Err(error.into()),
        };
        let next = sequence.checked_add(1).ok_or("worker sequence exhausted")?;
        let kind = frame["type"].as_str().ok_or("missing worker command")?.to_owned();
        if !matches!(kind.as_str(), "worker_run" | "worker_shutdown") { return Err("invalid idle worker command".into()); }
        frame = schemas.validate_protocol(owner, &kind, frame).map_err(|_| "worker ingress refused")?;
        if frame["owner_user_id"].as_u64() != Some(owner) || frame["sequence"].as_u64() != Some(next) {
            return Err("worker invocation authority differs".into());
        }
        if kind == "worker_shutdown" {
            if !input.is_empty() { return Err("worker shutdown has trailing frames".into()); }
            drop(input); drop(encoders);
            let closed = json!({"version":version,"type":"worker_closed","owner_user_id":owner,"sequence":next});
            schemas.validate_protocol(owner,"worker_closed",closed.clone()).map_err(|_|"invalid worker close")?;
            println!("{closed}"); return Ok(());
        }
        let config = frame.as_object_mut().and_then(|object|object.remove("configuration")).ok_or("missing worker configuration")?;
        if config["owner_user_id"].as_u64() != Some(owner) { return Err("worker task owner differs".into()); }
        input.set_scope(Arc::new(Scope { owner, sequence:next, version:version.clone(), schemas:schemas.clone() }));
        let tail = crate::cli_agent::run_configuration(runtime.clone(), input, config, Some(&encoders))?;
        if !tail.is_empty() { return Err("worker has unresolved trailing control bytes".into()); }
        sequence = next;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn envelope_overhead_does_not_reduce_or_expand_inner_control_budget() {
        let contract: Value=serde_json::from_str(include_str!("../../../contracts/native_agent_stdio_v1.schema.json")).unwrap();
        let documents=contract["$defs"].as_object().unwrap().iter().filter(|(name,_)|name.starts_with("worker_"))
            .map(|(name,schema)|(name.clone(),json!({"name":name,"contractVersion":tofu_runtime::execution_contract::TOOL_CONTRACT_VERSION,"arguments_schema":schema}))).collect();
        let scope=Scope{owner:7,sequence:9,version:contract["$defs"]["worker_control"]["properties"]["version"]["enum"][0].as_str().unwrap().into(),
            schemas:Arc::new(Catalog::compile(7,"worker-test",&Value::Object(documents)).unwrap())};
        let inner=json!({"version":"tofu.native-agent-stdio/v1","type":"cancel"});
        let frame=json!({"version":scope.version,"type":"worker_control","owner_user_id":7,"sequence":9,"frame":inner});
        let limit=serde_json::to_vec(&inner).unwrap().len();
        assert_eq!(serde_json::to_vec(&frame).unwrap().len(),limit+scope.control_overhead().unwrap());
        assert_eq!(scope.unwrap(frame.clone(),limit).unwrap(),inner);
        assert!(scope.unwrap(frame.clone(),limit-1).is_err());
        let mut stale=frame.clone();stale["sequence"]=json!(8);assert!(scope.unwrap(stale,limit).is_err());
        let mut foreign=frame;foreign["owner_user_id"]=json!(8);assert!(scope.unwrap(foreign,limit).is_err());
    }
}
