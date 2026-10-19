//! Borrowed Chat content normalization. Plain whitespace folds within text
//! runs; images, cache markers and opaque blocks retain their exact values.
use serde_core::{ser::SerializeSeq, Serialize};
use serde_json::Value;
use std::borrow::Cow;

pub(crate) fn blank(text: &str) -> bool {
    text.chars()
        .all(|c| crate::execution_contract::CHAT_TEXT_WHITESPACE.contains(&(c as u32)))
}
pub(crate) fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}
pub(crate) enum Block<'a> {
    Borrowed(&'a Value),
    Text(Cow<'a, str>),
}
impl Block<'_> {
    fn text(&self) -> Option<&str> {
        match self {
            Self::Text(text) => Some(text),
            Self::Borrowed(value)
                if value["type"] == "text" && value.as_object().is_some_and(|o| o.len() == 2) =>
            {
                value["text"].as_str()
            }
            _ => None,
        }
    }
    fn append(&mut self, suffix: &str) {
        let mut text = self.text().unwrap().to_owned();
        text.push_str(suffix);
        *self = Self::Text(Cow::Owned(text));
    }
}
impl Serialize for Block<'_> {
    fn serialize<S: serde_core::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            Self::Borrowed(value) => value.serialize(serializer),
            Self::Text(text) => {
                use serde_core::ser::SerializeMap;
                let mut map = serializer.serialize_map(Some(2))?;
                map.serialize_entry("type", "text")?;
                map.serialize_entry("text", text.as_ref())?;
                map.end()
            }
        }
    }
}
pub(crate) enum Content<'a> {
    Absent,
    Raw(&'a Value),
    Text(Cow<'a, str>),
    Blocks(Vec<Block<'a>>),
}
impl<'a> Content<'a> {
    pub fn new(value: Option<&'a Value>) -> Self {
        match value {
            None => Self::Absent,
            Some(Value::String(text)) => Self::Text(Cow::Borrowed(text)),
            Some(Value::Array(blocks)) if !blocks.is_empty() => {
                let mut out: Vec<Block<'a>> = Vec::with_capacity(blocks.len());
                let mut pending = String::new();
                let mut previous_plain = false;
                for block in blocks {
                    let plain = block.as_object().is_some_and(|o| {
                        o.len() == 2 && block["type"] == "text" && block["text"].is_string()
                    });
                    if !plain {
                        if previous_plain && !pending.is_empty() {
                            out.last_mut().unwrap().append(&pending);
                        }
                        pending.clear();
                        previous_plain = false;
                        if !(block["type"] == "text"
                            && (!truthy(block.get("text"))
                                || block["text"].as_str().is_some_and(blank)))
                        {
                            out.push(Block::Borrowed(block));
                        }
                        continue;
                    }
                    let text = block["text"].as_str().unwrap();
                    if blank(text) {
                        pending.push_str(text);
                        continue;
                    }
                    if previous_plain {
                        if !pending.is_empty() {
                            out.last_mut().unwrap().append(&pending);
                        }
                        pending.clear();
                        out.push(Block::Borrowed(block));
                    } else if pending.is_empty() {
                        out.push(Block::Borrowed(block));
                    } else {
                        pending.push_str(text);
                        out.push(Block::Text(Cow::Owned(std::mem::take(&mut pending))));
                    }
                    previous_plain = true;
                }
                if previous_plain && !pending.is_empty() {
                    out.last_mut().unwrap().append(&pending);
                }
                if out.is_empty() {
                    Self::Text(Cow::Borrowed(""))
                } else {
                    Self::Blocks(out)
                }
            }
            Some(value) => Self::Raw(value),
        }
    }
    pub fn empty(&self) -> bool {
        match self {
            Self::Absent | Self::Raw(Value::Null) => true,
            Self::Text(text) => blank(text),
            Self::Raw(Value::Array(a)) => a.is_empty(),
            Self::Blocks(blocks) => blocks.is_empty(),
            _ => false,
        }
    }
    fn into_blocks(self) -> Vec<Block<'a>> {
        match self {
            Self::Text(text) if text.is_empty() => Vec::new(),
            Self::Text(text) => vec![Block::Text(text)],
            Self::Blocks(blocks) => blocks,
            Self::Absent | Self::Raw(Value::Null) => Vec::new(),
            Self::Raw(Value::Array(blocks)) => blocks.iter().map(Block::Borrowed).collect(),
            Self::Raw(value) => vec![Block::Borrowed(value)],
        }
    }
    pub fn merge(&mut self, next: Self) -> Result<(), &'static str> {
        let as_merge_content = |content| match content {
            Self::Raw(value) if !truthy(Some(value)) => Self::Text(Cow::Borrowed("")),
            other => other,
        };
        let left = as_merge_content(std::mem::replace(self, Self::Absent));
        let next = as_merge_content(next);
        if matches!(left, Self::Blocks(_) | Self::Raw(Value::Array(_)))
            || matches!(next, Self::Blocks(_) | Self::Raw(Value::Array(_)))
        {
            let mut blocks = left.into_blocks();
            blocks.extend(next.into_blocks());
            *self = Self::Blocks(blocks);
        } else {
            let text = |content| match content {
                Self::Text(text) => Ok(text),
                Self::Absent | Self::Raw(Value::Null) => Ok(Cow::Borrowed("")),
                _ => Err("Chat adjacency has unsupported content"),
            };
            let left = text(left)?;
            let right = text(next)?;
            if left.is_empty() {
                *self = Self::Text(right);
            } else if right.is_empty() {
                *self = Self::Text(left);
            } else {
                let mut joined = left.into_owned();
                joined.push_str("\n\n");
                joined.push_str(&right);
                *self = Self::Text(Cow::Owned(joined));
            }
        }
        Ok(())
    }
    pub fn collapse(&mut self) {
        if let Self::Blocks(blocks) = self {
            if !blocks.is_empty() && blocks.iter().all(|block| block.text().is_some()) {
                let bytes = blocks.iter().map(|b| b.text().unwrap().len()).sum();
                let mut text = String::with_capacity(bytes);
                for block in blocks {
                    text.push_str(block.text().unwrap());
                }
                *self = Self::Text(Cow::Owned(text));
            }
        }
    }
}
impl Serialize for Content<'_> {
    fn serialize<S: serde_core::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            Self::Absent => serializer.serialize_unit(),
            Self::Raw(value) => value.serialize(serializer),
            Self::Text(text) => serializer.serialize_str(text),
            Self::Blocks(blocks) => {
                let mut sequence = serializer.serialize_seq(Some(blocks.len()))?;
                for block in blocks {
                    sequence.serialize_element(block)?;
                }
                sequence.end()
            }
        }
    }
}
