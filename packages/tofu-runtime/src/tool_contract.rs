//! Compiled request-owned ToolContractV2 argument validation and explicit
//! execution authorization. Authority: lib/tools/contracts.py. This implements
//! its rules, not a general JSON Schema engine or curated argument repair.
use crate::{
    execution_contract::{
        ToolContractCode as Code, TOOL_CONTRACT_NEXT_ACTION, TOOL_CONTRACT_VERSION,
    },
    tool_policy::{normalize_caller, CallerError},
};
use serde_json::{json, Value};
use std::{
    cmp::Ordering,
    collections::{BTreeMap, BTreeSet},
};
#[derive(Debug, Clone, PartialEq)]
pub struct ContractError {
    pub code: Code,
    pub message: String,
    pub path: String,
    pub retryable: bool,
    pub next_action: String,
}
impl ContractError {
    fn new(code: Code, message: String, path: &str) -> Self {
        Self {
            code,
            message,
            path: path.into(),
            retryable: true,
            next_action: TOOL_CONTRACT_NEXT_ACTION.into(),
        }
    }
    pub fn to_value(&self) -> Value {
        json!({"code":self.code.as_str(),"message":self.message,"path":self.path,"retryable":self.retryable,"nextAction":self.next_action})
    }
}
pub use crate::execution_contract::ToolPermission as Permission;
pub trait Authorization: Send + Sync {
    /// Composition extends this from the canonical ToolSpec receipt inventory.
    /// Basic permission never substitutes for an exact approval receipt.
    fn requires_receipt(
        &self,
        _name: &str,
        permission: Permission,
        _arguments: &Value,
        _caller: Option<&Value>,
    ) -> bool {
        permission == Permission::Approval
    }

    fn allow(
        &self,
        owner: u64,
        name: &str,
        permission: Permission,
        arguments: &Value,
        caller: Option<&Value>,
    ) -> bool;
}
pub struct DenyAll;
impl Authorization for DenyAll {
    fn allow(&self, _: u64, _: &str, _: Permission, _: &Value, _: Option<&Value>) -> bool {
        false
    }
}
#[derive(Debug)]
pub enum AdmissionError {
    WrongOwner,
    Validation(ContractError),
    Caller(CallerError),
    Denied,
}
pub struct AdmittedCall {
    owner: u64,
    name: String,
    arguments: Value,
    caller: Option<Value>,
    epoch: String,
    permission: Permission,
    approval: Option<(String, crate::approval::Ticket)>,
    model_call_id: Option<String>,
}
impl AdmittedCall {
    pub(crate) fn with_model_call_id(mut self, id: &str) -> Self {
        self.model_call_id = Some(id.into());
        self
    }
    pub fn with_approval(
        mut self,
        call_id: &str,
        ticket: crate::approval::Ticket,
    ) -> Result<Self, AdmissionError> {
        if self.approval.is_some() || !ticket.attach_matches(call_id, &self) {
            return Err(AdmissionError::Denied);
        }
        self.approval = Some((call_id.into(), ticket));
        Ok(self)
    }
    pub fn permission(&self) -> Permission {
        self.permission
    }
    pub fn owner(&self) -> u64 {
        self.owner
    }
    pub fn name(&self) -> &str {
        &self.name
    }
    pub fn arguments(&self) -> &Value {
        &self.arguments
    }
    pub fn caller(&self) -> Option<&Value> {
        self.caller.as_ref()
    }
    pub fn epoch(&self) -> &str {
        &self.epoch
    }
}
#[derive(Clone)]
struct Property {
    name: String,
    node: Option<Node>,
    default: Option<Value>,
}
#[derive(Clone)]
struct Node {
    types: Vec<String>,
    enumeration: Option<Vec<Value>>,
    min_length: i64,
    max_length: Option<i64>,
    minimum: Option<Value>,
    maximum: Option<Value>,
    min_items: Option<i64>,
    max_items: Option<i64>,
    items: Option<Box<Node>>,
    properties: Vec<Property>,
    property_names: BTreeSet<String>,
    required: Vec<String>,
    deny_extras: bool,
}
fn schema_error() -> ContractError {
    let mut error = ContractError::new(
        Code::InvalidToolContract,
        "Malformed execution arguments schema.".into(),
        "$",
    );
    error.retryable = false;
    error.next_action = "Do not execute this tool; report the contract error.".into();
    error
}
fn integer(v: &Value) -> Result<i64, ContractError> {
    match v {
        Value::Number(n) => n
            .as_i64()
            .or_else(|| {
                n.as_f64()
                    .filter(|f| f.is_finite() && *f >= i64::MIN as f64 && *f < i64::MAX as f64)
                    .map(|f| f as i64)
            })
            .ok_or_else(schema_error),
        Value::String(s) => s.trim().parse().map_err(|_| schema_error()),
        Value::Bool(b) => Ok(*b as i64),
        _ => Err(schema_error()),
    }
}
impl Node {
    fn compile(schema: &Value, depth: usize) -> Result<Self, ContractError> {
        if depth > 128 || !schema.is_object() {
            return Err(schema_error());
        }
        let types = match &schema["type"] {
            Value::String(s) if !s.is_empty() => vec![s.clone()],
            Value::Array(a) => a
                .iter()
                .filter_map(|v| match v {
                    Value::String(s) if !s.is_empty() => Some(s.clone()),
                    Value::Null | Value::Bool(false) => None,
                    _ => Some(v.to_string()),
                })
                .collect(),
            Value::Null | Value::Bool(false) => Vec::new(),
            v => vec![v.to_string()],
        };
        let enumeration = match schema.get("enum") {
            None => None,
            Some(Value::Array(a)) => Some(a.clone()),
            _ => return Err(schema_error()),
        };
        let mut properties = Vec::new();
        let mut property_names = BTreeSet::new();
        if let Some(v) = schema.get("properties").filter(|v| !v.is_null()) {
            let object = v.as_object().ok_or_else(schema_error)?;
            for (name, child) in object {
                let node = if child.is_object() {
                    Some(Self::compile(child, depth + 1)?)
                } else {
                    None
                };
                properties.push(Property {
                    name: name.clone(),
                    default: if node.is_some() {
                        child.get("default").cloned()
                    } else {
                        None
                    },
                    node,
                });
                property_names.insert(name.clone());
            }
        }
        let required = match schema.get("required") {
            None | Some(Value::Null) => Vec::new(),
            Some(Value::Array(a)) => a
                .iter()
                .map(|v| v.as_str().map(str::to_owned).ok_or_else(schema_error))
                .collect::<Result<Vec<_>, _>>()?,
            _ => return Err(schema_error()),
        };
        let get = |key: &str| -> Result<Option<i64>, ContractError> {
            schema
                .get(key)
                .filter(|v| !v.is_null())
                .map(integer)
                .transpose()
        };
        for key in ["minimum", "maximum"] {
            if schema
                .get(key)
                .is_some_and(|v| !v.is_number() && !v.is_boolean())
            {
                return Err(schema_error());
            }
        }
        Ok(Self {
            types,
            enumeration,
            min_length: get("minLength")?.unwrap_or(0),
            max_length: get("maxLength")?,
            minimum: schema.get("minimum").cloned(),
            maximum: schema.get("maximum").cloned(),
            min_items: get("minItems")?,
            max_items: get("maxItems")?,
            items: if schema["items"].is_object() {
                Some(Box::new(Self::compile(&schema["items"], depth + 1)?))
            } else {
                None
            },
            properties,
            property_names,
            required,
            deny_extras: schema["additionalProperties"] == false,
        })
    }
    fn validate(&self, mut value: Value, path: &str, required_null_is_missing: bool) -> Result<Value, ContractError> {
        if !self.types.is_empty() && !self.types.iter().any(|t| matches_type(&value, t)) {
            return Err(ContractError::new(
                Code::InvalidArgumentType,
                format!(
                    "Invalid type at {path}; expected {}.",
                    self.types.join(" | ")
                ),
                path,
            ));
        }
        if self
            .enumeration
            .as_ref()
            .is_some_and(|a| !a.iter().any(|v| python_equal(v, &value)))
        {
            return Err(ContractError::new(
                Code::InvalidArgumentValue,
                format!("Invalid value at {path}."),
                path,
            ));
        }
        if let Some(text) = value.as_str() {
            let length = text.chars().count() as i128;
            if length < self.min_length as i128
                || self.max_length.is_some_and(|max| length > max as i128)
            {
                let allowed = self.max_length.map_or_else(
                    || format!(">= {}", self.min_length),
                    |max| format!("{}–{max}", self.min_length),
                );
                return Err(ContractError::new(
                    Code::InvalidArgumentLength,
                    format!("Invalid length at {path} (got {length} chars; allowed {allowed})."),
                    path,
                ));
            }
        }
        if value.is_number() {
            if let Some(min) = &self.minimum {
                if numeric_compare(&value, min) == Some(Ordering::Less) {
                    return Err(ContractError::new(
                        Code::InvalidArgumentValue,
                        format!(
                            "Value below minimum at {path} (got {}; allowed >= {}).",
                            python_number(&value),
                            python_number(min)
                        ),
                        path,
                    ));
                }
            }
            if let Some(max) = &self.maximum {
                if numeric_compare(&value, max) == Some(Ordering::Greater) {
                    return Err(ContractError::new(
                        Code::InvalidArgumentValue,
                        format!(
                            "Value above maximum at {path} (got {}; allowed <= {}).",
                            python_number(&value),
                            python_number(max)
                        ),
                        path,
                    ));
                }
            }
        }
        if let Some(array) = value.as_array_mut() {
            if self
                .min_items
                .is_some_and(|min| (array.len() as i128) < min as i128)
            {
                return Err(ContractError::new(
                    Code::TooFewItems,
                    format!("Too few items at {path}."),
                    path,
                ));
            }
            if self
                .max_items
                .is_some_and(|max| (array.len() as i128) > max as i128)
            {
                return Err(ContractError::new(
                    Code::TooManyItems,
                    format!("Too many items at {path}."),
                    path,
                ));
            }
            if let Some(child) = &self.items {
                for (i, value) in array.iter_mut().enumerate() {
                    *value = child.validate(std::mem::take(value), &format!("{path}[{i}]"), required_null_is_missing)?;
                }
            }
        }
        if let Some(object) = value.as_object_mut() {
            let missing: Vec<_> = self
                .required
                .iter()
                .filter(|name| object.get(*name).is_none_or(|value| {
                    value.is_null() && required_null_is_missing
                }))
                .cloned()
                .collect();
            if !missing.is_empty() {
                return Err(ContractError::new(
                    Code::MissingRequiredArguments,
                    format!("Missing required arguments: {}", missing.join(", ")),
                    path,
                ));
            }
            if self.deny_extras {
                let mut extras: Vec<_> = object
                    .keys()
                    .filter(|key| !self.property_names.contains(*key))
                    .cloned()
                    .collect();
                extras.sort();
                if !extras.is_empty() {
                    return Err(ContractError::new(
                        Code::UnknownArguments,
                        format!("Unknown arguments: {}", extras.join(", ")),
                        path,
                    ));
                }
            }
            for property in &self.properties {
                if !object.contains_key(&property.name) {
                    if let Some(default) = &property.default {
                        object.insert(property.name.clone(), default.clone());
                    }
                }
                if let (Some(node), Some(value)) = (&property.node, object.get_mut(&property.name))
                {
                    *value =
                        node.validate(std::mem::take(value), &format!("{path}.{}", property.name), required_null_is_missing)?;
                }
            }
        }
        Ok(value)
    }
}
fn matches_type(v: &Value, t: &str) -> bool {
    match t {
        "object" => v.is_object(),
        "array" => v.is_array(),
        "string" => v.is_string(),
        "integer" => v.as_number().is_some_and(|n| n.is_i64() || n.is_u64()),
        "number" => v.is_number(),
        "boolean" => v.is_boolean(),
        "null" => v.is_null(),
        _ => true,
    }
}
fn int_value(v: &Value) -> Option<i128> {
    match v {
        Value::Bool(b) => Some(*b as i128),
        Value::Number(n) => n
            .as_i64()
            .map(i128::from)
            .or_else(|| n.as_u64().map(i128::from)),
        _ => None,
    }
}
fn numeric_compare(a: &Value, b: &Value) -> Option<Ordering> {
    match (int_value(a), int_value(b)) {
        (Some(a), Some(b)) => Some(a.cmp(&b)),
        (Some(a), None) => {
            let b = b.as_f64()?;
            if b >= i128::MAX as f64 {
                return Some(Ordering::Less);
            }
            if b < i128::MIN as f64 {
                return Some(Ordering::Greater);
            }
            let floor = b.floor() as i128;
            let cmp = a.cmp(&floor);
            Some(if cmp == Ordering::Equal && b.fract() != 0.0 {
                Ordering::Less
            } else {
                cmp
            })
        }
        (None, Some(_)) => numeric_compare(b, a).map(Ordering::reverse),
        (None, None) => a.as_f64()?.partial_cmp(&b.as_f64()?),
    }
}
fn python_equal(a: &Value, b: &Value) -> bool {
    if (a.is_number() || a.is_boolean()) && (b.is_number() || b.is_boolean()) {
        return numeric_compare(a, b) == Some(Ordering::Equal);
    }
    match (a, b) {
        (Value::Array(a), Value::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(a, b)| python_equal(a, b))
        }
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len()
                && a.iter()
                    .all(|(k, a)| b.get(k).is_some_and(|b| python_equal(a, b)))
        }
        _ => a == b,
    }
}
fn python_number(v: &Value) -> String {
    if let Some(value) = v.as_bool() {
        return if value { "True" } else { "False" }.into();
    }
    if let Some(i) = int_value(v) {
        return i.to_string();
    }
    let value = v.as_f64().unwrap();
    let raw = format!("{value:?}");
    if let Some((mantissa, exponent)) = raw.split_once('e') {
        let exponent: i32 = exponent.parse().unwrap();
        format!(
            "{mantissa}e{}{abs:02}",
            if exponent < 0 { "-" } else { "+" },
            abs = exponent.abs()
        )
    } else {
        raw
    }
}
struct Entry {
    node: Node,
    permission: Option<Permission>,
}
pub struct Catalog {
    owner: u64,
    epoch: String,
    entries: BTreeMap<String, Entry>,
}
impl Catalog {
    pub(crate) fn belongs_to(&self, owner: u64, epoch: &str) -> bool {
        self.owner == owner && self.epoch == epoch
    }
    pub fn compile(owner: u64, epoch: &str, documents: &Value) -> Result<Self, ContractError> {
        if owner == 0 || epoch.trim().is_empty() {
            return Err(schema_error());
        }
        let documents = documents.as_object().ok_or_else(schema_error)?;
        let mut entries = BTreeMap::new();
        for (name, document) in documents {
            if name.is_empty()
                || !name
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
            {
                return Err(schema_error());
            }
            if document["contractVersion"] != TOOL_CONTRACT_VERSION
                || document["name"] != *name
                || !document["arguments_schema"].is_object()
            {
                return Err(schema_error());
            }
            let permission = match &document["permission"] {
                Value::Null => None,
                Value::String(value) => Some(match value.as_str() {
                    "read" => Permission::Read,
                    "write" => Permission::Write,
                    "approval" => Permission::Approval,
                    "external" => Permission::External,
                    _ => return Err(schema_error()),
                }),
                _ => return Err(schema_error()),
            };
            entries.insert(
                name.clone(),
                Entry {
                    node: Node::compile(&document["arguments_schema"], 0)?,
                    permission,
                },
            );
        }
        Ok(Self {
            owner,
            epoch: epoch.into(),
            entries,
        })
    }
    pub fn validate(&self, owner: u64, name: &str, value: Value) -> Result<Value, AdmissionError> {
        self.validate_with_required_null_policy(owner, name, value, true)
    }
    /// Wire schemas treat explicit null as a value; it still must match type.
    /// This does not grant tool dispatch authority or change tool normalization.
    pub fn validate_protocol(&self, owner: u64, name: &str, value: Value) -> Result<Value, AdmissionError> {
        self.validate_with_required_null_policy(owner, name, value, false)
    }
    fn validate_with_required_null_policy(&self, owner: u64, name: &str, value: Value, required_null_is_missing: bool) -> Result<Value, AdmissionError> {
        if owner != self.owner {
            return Err(AdmissionError::WrongOwner);
        }
        let entry = self.entries.get(name).ok_or_else(|| {
            let mut e = ContractError::new(
                Code::ToolContractUnavailable,
                format!("No executable ToolContractV2 is available for '{name}'."),
                "$",
            );
            e.next_action = "Search tools again and use a tool from the current tool epoch.".into();
            AdmissionError::Validation(e)
        })?;
        let value = entry
            .node
            .validate(value, "$", required_null_is_missing)
            .map_err(AdmissionError::Validation)?;
        if !value.is_object() {
            return Err(AdmissionError::Validation(ContractError::new(
                Code::InvalidArgumentType,
                "Tool arguments must be an object.".into(),
                "$",
            )));
        }
        Ok(value)
    }
    pub fn admit(
        &self,
        owner: u64,
        name: &str,
        value: Value,
        caller: &Value,
        authorization: &dyn Authorization,
    ) -> Result<AdmittedCall, AdmissionError> {
        let arguments = self.validate(owner, name, value)?;
        let caller = normalize_caller(caller, true).map_err(AdmissionError::Caller)?;
        let permission = self.entries[name]
            .permission
            .ok_or(AdmissionError::Denied)?;
        if !matches!(
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| authorization.allow(
                owner,
                name,
                permission,
                &arguments,
                caller.as_ref()
            ))),
            Ok(true)
        ) {
            return Err(AdmissionError::Denied);
        }
        Ok(AdmittedCall {
            owner,
            name: name.into(),
            arguments,
            caller,
            epoch: self.epoch.clone(),
            permission,
            approval: None,
            model_call_id: None,
        })
    }
}

/// Handler registry is frozen to the same request owner/epoch as admission.
/// Schema validity never supplies a handler or bypasses final authorization.
pub struct InvocationContext<'a> {
    pub owner: u64,
    pub epoch: &'a str,
    pub caller: Option<&'a Value>,
    /// Zero-based native model round; absent outside an agent batch.
    pub model_round: Option<usize>,
    /// Original agent occurrence identity; grants no execution authority.
    pub model_call_id: Option<&'a str>,
    /// Present only after the exact single-use native receipt was consumed.
    pub approved_call_id: Option<&'a str>,
    pub runtime: crate::tool_execution::Context<'a>,
}
pub trait Handler: Send + Sync {
    fn invoke(&self, context: InvocationContext<'_>, arguments: &Value) -> Result<Value, Value>;
}
#[derive(Debug)]
pub enum DispatchError {
    WrongScope,
    Denied,
    ApprovalRequired,
    HandlerUnavailable,
    Failed(Value),
    Panicked,
    Cancelled,
    TimedOut,
    ResourceInvariant,
    ParentClosing,
    Session(crate::session::SessionError),
}
pub struct Registry {
    owner: u64,
    epoch: String,
    handlers: BTreeMap<String, std::sync::Arc<dyn Handler>>,
    sessions: crate::session::Registry,
    next_call: std::sync::atomic::AtomicU64,
}
impl Registry {
    pub(crate) fn belongs_to(&self, owner: u64, epoch: &str) -> bool {
        self.owner == owner && self.epoch == epoch
    }
    pub fn new(
        owner: u64,
        epoch: &str,
        handlers: Vec<(String, std::sync::Arc<dyn Handler>)>,
    ) -> Result<Self, DispatchError> {
        if owner == 0 || epoch.trim().is_empty() {
            return Err(DispatchError::WrongScope);
        }
        let mut frozen = BTreeMap::new();
        for (name, handler) in handlers {
            if frozen.insert(name, handler).is_some() {
                return Err(DispatchError::WrongScope);
            }
        }
        Ok(Self {
            owner,
            epoch: epoch.into(),
            handlers: frozen,
            sessions: crate::session::Registry::new(),
            next_call: std::sync::atomic::AtomicU64::new(0),
        })
    }
    pub fn active_invocations(&self) -> Result<usize, crate::session::SessionError> {
        self.sessions.active_count()
    }
    pub fn dispatch(
        &self,
        call: AdmittedCall,
        authorization: &dyn Authorization,
    ) -> Result<Value, DispatchError> {
        if call.owner != self.owner || call.epoch != self.epoch {
            return Err(DispatchError::WrongScope);
        }
        let ordinal = self
            .next_call
            .fetch_update(
                std::sync::atomic::Ordering::Relaxed,
                std::sync::atomic::Ordering::Relaxed,
                |value| value.checked_add(1),
            )
            .map_err(|_| DispatchError::WrongScope)?;
        let scope = crate::tool_execution::Scope::new(
            &self.sessions,
            self.owner,
            &format!("{}:{ordinal}", self.epoch),
            "",
            None,
        )
        .map_err(DispatchError::Session)?;
        self.dispatch_scoped(call, authorization, &scope)
            .map_err(|error| match error {
                crate::tool_execution::StartError::Session(error) => DispatchError::Session(error),
                crate::tool_execution::StartError::WrongOwner
                | crate::tool_execution::StartError::AlreadyDispatched => DispatchError::WrongScope,
            })?
            .into_result()
    }
    pub fn dispatch_scoped(
        &self,
        call: AdmittedCall,
        authorization: &dyn Authorization,
        scope: &crate::tool_execution::Scope,
    ) -> Result<crate::tool_execution::Outcome, crate::tool_execution::StartError> {
        self.dispatch_scoped_in_round(call, authorization, scope, None)
    }
    /// Dispatch with the model round owned by the admitted agent batch.
    pub fn dispatch_scoped_in_round(
        &self,
        call: AdmittedCall,
        authorization: &dyn Authorization,
        scope: &crate::tool_execution::Scope,
        model_round: Option<usize>,
    ) -> Result<crate::tool_execution::Outcome, crate::tool_execution::StartError> {
        if call.owner != self.owner || call.epoch != self.epoch {
            return Err(crate::tool_execution::StartError::WrongOwner);
        }
        scope.run(call.owner, |runtime| {
            self.invoke_checked(call, authorization, runtime, model_round)
        })
    }
    fn invoke_checked(
        &self,
        mut call: AdmittedCall,
        authorization: &dyn Authorization,
        runtime: crate::tool_execution::Context<'_>,
        model_round: Option<usize>,
    ) -> Result<Value, DispatchError> {
        if call.owner != self.owner || call.epoch != self.epoch {
            return Err(DispatchError::WrongScope);
        }
        let allowed = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            authorization.allow(
                call.owner,
                &call.name,
                call.permission,
                &call.arguments,
                call.caller.as_ref(),
            )
        }));
        if !matches!(allowed, Ok(true)) {
            return Err(DispatchError::Denied);
        }
        let handler = self
            .handlers
            .get(&call.name)
            .ok_or(DispatchError::HandlerUnavailable)?;
        let requires = match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            authorization.requires_receipt(
                &call.name,
                call.permission,
                &call.arguments,
                call.caller.as_ref(),
            )
        })) {
            Ok(required) => required,
            Err(_) => return Err(DispatchError::Denied),
        };
        if requires && call.approval.is_none() {
            return Err(DispatchError::ApprovalRequired);
        }
        let approved_call_id = if let Some((call_id, ticket)) = call.approval.take() {
            runtime.ensure_active()?;
            ticket
                .consume(runtime.parent_id(), &call_id, &call)
                .map_err(|_| DispatchError::ApprovalRequired)?;
            Some(call_id)
        } else {
            None
        };
        runtime.begin_dispatch()?;
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            handler.invoke(
                InvocationContext {
                    owner: call.owner,
                    epoch: &call.epoch,
                    caller: call.caller.as_ref(),
                    model_round,
                    model_call_id: call.model_call_id.as_deref(),
                    approved_call_id: approved_call_id.as_deref(),
                    runtime,
                },
                &call.arguments,
            )
        })) {
            Ok(result) => result.map_err(DispatchError::Failed),
            Err(_) => Err(DispatchError::Panicked),
        }
    }
}
