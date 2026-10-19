//! Round-owned model visibility after context preparation. Execution authority
//! and generation parameters remain frozen. Read guards bind counting, aliases
//! and serialization to one exact projection through the physical attempt.
use serde_json::{Map, Value};
use std::{ops::Deref, sync::{RwLock, RwLockReadGuard}};

struct Projection { round: usize, template: Map<String, Value> }
pub struct PreparedToolWire {
    base: Map<String, Value>,
    catalog: Map<String, Value>,
    current: RwLock<Option<Projection>>,
}
pub struct TemplateGuard<'a>(RwLockReadGuard<'a, Option<Projection>>);
impl Deref for TemplateGuard<'_> {
    type Target = Map<String, Value>;
    fn deref(&self) -> &Self::Target { &self.0.as_ref().expect("validated projection").template }
}
impl PreparedToolWire {
    pub fn new(base: Map<String, Value>, catalog: Map<String, Value>) -> Result<Self, &'static str> {
        if base.contains_key("tools") || base.contains_key("messages") { return Err("competing model projection"); }
        Ok(Self {base, catalog, current:RwLock::new(None)})
    }
    pub fn install(&self, round: usize, tools: &Value) -> Result<(), &'static str> {
        let projected = crate::model_tool_wire::project(&self.catalog, Some(tools))?;
        let mut current=self.current.write().map_err(|_|"model projection poisoned")?;
        if current.as_ref().is_some_and(|value| round<=value.round) { return Err("model projection round reused"); }
        if let Some(value)=current.as_mut() {
            if value.template.get("tools")==Some(tools) { value.round=round;return Ok(()); }
        }
        let mut template=self.base.clone();
        template.insert("tools".into(),Value::Array(projected));
        *current=Some(Projection {round,template});
        Ok(())
    }
    pub fn template(&self) -> Result<TemplateGuard<'_>, &'static str> {
        let guard=self.current.read().map_err(|_|"model projection poisoned")?;
        if guard.is_none() { return Err("model projection not prepared"); }
        Ok(TemplateGuard(guard))
    }
}
