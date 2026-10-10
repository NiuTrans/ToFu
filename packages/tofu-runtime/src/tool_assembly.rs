//! Response-local tool occurrence assembly, following the canonical SSE owner.
//! Owns upstream-index aliases, unindexed continuation and name-fragment logic;
//! caller normalization/flood admission and tool execution are separate owners.
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Arc,
};
#[derive(Default)]
pub(crate) struct Call {
    pub id: String,
    pub name: String,
    pub arguments: String,
    pub caller: Option<Value>,
    pub extra_content: Option<Value>,
}
pub(crate) struct Delta {
    pub index: Option<u64>,
    pub id: Option<String>,
    pub name: Option<String>,
    pub arguments: Option<String>,
    pub caller: Option<Value>,
    pub caller_present: bool,
    pub extra_content: Option<Value>,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Error {
    Capacity,
    CallerChanged,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Footprint {
    pub positions: usize,
    pub index_aliases: usize,
}
pub(crate) struct Assembly {
    pub calls: BTreeMap<u64, Call>,
    aliases: BTreeMap<u64, u64>,
    unindexed: Option<u64>,
    capacity: usize,
    names: Arc<BTreeSet<String>>,
}
impl Assembly {
    pub fn new(capacity: usize, names: Arc<BTreeSet<String>>) -> Self {
        Self {
            calls: BTreeMap::new(),
            aliases: BTreeMap::new(),
            unindexed: None,
            capacity,
            names,
        }
    }
    pub fn footprint(&self) -> Footprint {
        Footprint {
            positions: self.calls.len(),
            index_aliases: self.aliases.len(),
        }
    }
    fn next(&self) -> Result<u64, Error> {
        self.calls
            .last_key_value()
            .map_or(Ok(0), |(key, _)| key.checked_add(1).ok_or(Error::Capacity))
    }
    fn ensure(&mut self, position: u64) -> Result<(), Error> {
        if !self.calls.contains_key(&position) {
            if self.calls.len() >= self.capacity {
                return Err(Error::Capacity);
            }
            self.calls.insert(position, Call::default());
        }
        Ok(())
    }
    fn split(&mut self, index: Option<u64>) -> Result<u64, Error> {
        let position = self.next()?;
        self.ensure(position)?;
        self.unindexed = Some(position);
        if let Some(index) = index {
            self.aliases.insert(index, position);
        }
        Ok(position)
    }
    pub fn feed(&mut self, delta: Delta) -> Result<u64, Error> {
        let mut position = match delta.index {
            Some(index) => self.aliases.get(&index).copied().unwrap_or(index),
            None => {
                let position = self.unindexed.map_or_else(|| self.next(), Ok)?;
                self.unindexed = Some(position);
                position
            }
        };
        self.ensure(position)?;
        let incoming_id = delta.id.as_deref().unwrap_or("");
        let previous = &self.calls[&position];
        if !incoming_id.is_empty() && !previous.id.is_empty() && incoming_id != previous.id {
            position = self.split(delta.index)?;
        }
        let previous = &self.calls[&position];
        let incoming_name = delta.name.as_deref().unwrap_or("");
        let name_changed = !incoming_name.is_empty()
            && !previous.name.is_empty()
            && incoming_name != previous.name;
        let fragment = if name_changed && previous.arguments.is_empty() {
            let joined = format!("{}{}", previous.name, incoming_name);
            self.names.is_empty()
                || (!self.names.contains(&previous.name)
                    && self
                        .names
                        .range::<str, _>((
                            std::ops::Bound::Included(joined.as_str()),
                            std::ops::Bound::Unbounded,
                        ))
                        .next()
                        .is_some_and(|name| name.starts_with(&joined)))
        } else {
            false
        };
        if name_changed && !fragment {
            // Identity belongs to the new name occurrence; do not overwrite the old
            // slot before splitting, including an old occurrence with no provider ID.
            position = self.split(delta.index)?;
        }
        let slot = self.calls.get_mut(&position).unwrap();
        let retransmitted = delta.name.as_ref().is_some_and(|name| name == &slot.name)
            && delta
                .arguments
                .as_ref()
                .is_some_and(|arguments| !arguments.is_empty() && arguments == &slot.arguments);
        if !incoming_id.is_empty() {
            slot.id = incoming_id.into();
        }
        if delta.caller_present {
            if slot.caller.is_some() && slot.caller != delta.caller {
                return Err(Error::CallerChanged);
            }
            slot.caller = delta.caller;
        }
        if let Some(extra) = delta.extra_content {
            slot.extra_content = Some(extra);
        }
        if !incoming_name.is_empty() {
            if fragment {
                slot.name.push_str(incoming_name);
            } else {
                slot.name = incoming_name.into();
            }
        }
        if let Some(arguments) = delta.arguments {
            if !retransmitted {
                slot.arguments.push_str(&arguments);
            }
        }
        Ok(position)
    }
}
pub fn wire_tool_names(tools: &Value) -> BTreeSet<String> {
    fn collect(tools: &Value, names: &mut BTreeSet<String>) {
        let Some(tools) = tools.as_array() else {
            return;
        };
        for tool in tools {
            if !tool.is_object() {
                continue;
            }
            if tool["type"] == "namespace" {
                collect(&tool["tools"], names);
                continue;
            }
            let candidate = tool["function"]["name"]
                .as_str()
                .filter(|name| !name.is_empty())
                .or_else(|| {
                    if tool["type"] != "tool_search" && tool["type"] != "programmatic_tool_calling"
                    {
                        tool["name"].as_str().filter(|name| !name.is_empty())
                    } else {
                        None
                    }
                });
            if let Some(name) = candidate {
                names.insert(name.into());
            }
        }
    }
    let mut names = BTreeSet::new();
    collect(tools, &mut names);
    names
}
