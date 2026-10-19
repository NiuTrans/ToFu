//! Per-call HTML text projection, with an iterative arena and no retained cache.
//! The tokenizer implements the pinned Python-owner profile; vocabulary/entity
//! data and output fixtures derive from that owner. No storage or provider I/O.
#[path = "generated_html_projection.rs"]
#[rustfmt::skip]
mod contract;
#[path = "html_tokenizer.rs"]
mod tokenizer;
use std::collections::HashMap;

#[derive(Debug, PartialEq, Eq)]
pub enum Error {
    NumericReference,
    ProjectionBudget,
    Allocation,
}

/// Exact Python owner/tokenizer used to generate the retained parity contract.
pub struct Profile {
    pub python_version: &'static str,
    pub tokenizer_sha256: &'static str,
    pub owner_sha256: &'static str,
}
pub const PROFILE: Profile = Profile {
    python_version: contract::PYTHON_VERSION,
    tokenizer_sha256: contract::TOKENIZER_SHA256,
    owner_sha256: contract::OWNER_SHA256,
};

enum Child {
    Element(usize),
    Text(String),
}
struct Node {
    name: String,
    children: Vec<Child>,
}
struct TextIndex {
    joined: String,
    spans: Vec<(usize, usize)>,
}
impl TextIndex {
    fn get(&self, id: usize) -> &str {
        let (start, end) = self.spans[id];
        &self.joined[start..end]
    }
}
struct Projection {
    nodes: Vec<Node>,
    stack: Vec<usize>,
    pending: String,
    void_ends: HashMap<String, usize>,
    open_counts: HashMap<String, usize>,
    preserved: usize,
    projection_budget: usize,
    transformed_bytes: usize,
}
impl Projection {
    fn new(projection_budget: usize) -> Self {
        Self {
            nodes: vec![Node {
                name: String::new(),
                children: vec![],
            }],
            stack: vec![0],
            pending: String::new(),
            void_ends: HashMap::new(),
            open_counts: HashMap::new(),
            preserved: 0,
            projection_budget,
            transformed_bytes: 0,
        }
    }
    fn text(&mut self, value: &str) {
        self.pending.push_str(value);
    }
    fn flush(&mut self) {
        if self.pending.is_empty() {
            return;
        }
        let mut text = std::mem::take(&mut self.pending);
        if self.preserved == 0 && text.bytes().all(tokenizer::space) {
            text = if text.contains('\n') { "\n" } else { " " }.into();
        }
        self.nodes[*self.stack.last().unwrap()]
            .children
            .push(Child::Text(text));
    }
    fn start(&mut self, tag: &str, empty: bool) {
        self.flush();
        let id = self.nodes.len();
        self.nodes.push(Node {
            name: tag.into(),
            children: vec![],
        });
        self.nodes[*self.stack.last().unwrap()]
            .children
            .push(Child::Element(id));
        if contract::VOID.contains(&tag) {
            if !empty {
                *self.void_ends.entry(tag.into()).or_default() += 1;
            }
        } else {
            self.stack.push(id);
            *self.open_counts.entry(tag.into()).or_default() += 1;
            self.preserved += usize::from(matches!(tag, "pre" | "textarea"));
            if empty {
                self.end(tag);
            }
        }
    }
    fn end(&mut self, tag: &str) {
        if let Some(count) = self.void_ends.get_mut(tag) {
            if *count > 0 {
                *count -= 1;
                return;
            }
        }
        self.flush();
        if self.open_counts.get(tag).copied().unwrap_or(0) == 0 {
            return;
        }
        let at = self
            .stack
            .iter()
            .rposition(|id| self.nodes[*id].name == tag)
            .unwrap();
        for id in self.stack.drain(at..) {
            let name = &self.nodes[id].name;
            *self.open_counts.get_mut(name).unwrap() -= 1;
            self.preserved -= usize::from(matches!(name.as_str(), "pre" | "textarea"));
        }
    }
    fn text_nodes(&self, root: usize, strip: bool, separator: &str) -> Result<String, Error> {
        let mut pending: Vec<_> = self.nodes[root].children.iter().rev().collect();
        let mut output = String::new();
        let mut first = true;
        while let Some(child) = pending.pop() {
            match child {
                Child::Element(id) => pending.extend(self.nodes[*id].children.iter().rev()),
                Child::Text(text) => {
                    let value = if strip {
                        text.trim_matches(contract::whitespace)
                    } else {
                        text
                    };
                    if !value.is_empty() {
                        if !first {
                            append(&mut output, separator, self.projection_budget)?;
                        }
                        append(&mut output, value, self.projection_budget)?;
                        first = false;
                    }
                }
            }
        }
        Ok(output)
    }
    fn children(&self, id: usize) -> Vec<usize> {
        self.nodes[id]
            .children
            .iter()
            .filter_map(|child| match child {
                Child::Element(id) => Some(*id),
                Child::Text(_) => None,
            })
            .collect()
    }
    fn stripped_index(&self) -> Result<TextIndex, Error> {
        // A subtree is a contiguous range in the joined text. Index it once so
        // deeply nested empty cells never trigger quadratic descendant walks.
        enum Visit<'a> {
            Enter(usize),
            Exit(usize, usize),
            Text(&'a str),
        }
        let mut index = TextIndex {
            joined: String::new(),
            spans: vec![(0, 0); self.nodes.len()],
        };
        let mut visits = vec![Visit::Enter(0)];
        while let Some(visit) = visits.pop() {
            match visit {
                Visit::Enter(id) => {
                    visits.push(Visit::Exit(id, index.joined.len()));
                    visits.extend(
                        self.nodes[id]
                            .children
                            .iter()
                            .rev()
                            .map(|child| match child {
                                Child::Element(id) => Visit::Enter(*id),
                                Child::Text(text) => Visit::Text(text),
                            }),
                    );
                }
                Visit::Exit(id, before) => {
                    if index.joined.len() > before {
                        index.spans[id] = (before + usize::from(before > 0), index.joined.len());
                    }
                }
                Visit::Text(text) => {
                    let text = text.trim_matches(contract::whitespace);
                    if !text.is_empty() {
                        if !index.joined.is_empty() {
                            append(&mut index.joined, " ", self.projection_budget)?;
                        }
                        append(&mut index.joined, text, self.projection_budget)?;
                    }
                }
            }
        }
        Ok(index)
    }
    fn replace(&mut self, id: usize, text: String) -> Result<(), Error> {
        self.transformed_bytes = self
            .transformed_bytes
            .checked_add(text.len())
            .filter(|bytes| *bytes <= self.projection_budget)
            .ok_or(Error::ProjectionBudget)?;
        self.nodes[id].children = vec![Child::Text(text)];
        Ok(())
    }
    fn finish(mut self) -> Result<String, Error> {
        self.flush();
        // Hidden subtrees are detached before any heading/table text is read.
        let mut has_headings = false;
        let mut has_rows = false;
        let mut stack = vec![0];
        while let Some(id) = stack.pop() {
            let name = self.nodes[id].name.as_bytes();
            has_headings |= name.len() == 2 && name[0] == b'h' && matches!(name[1], b'1'..=b'6');
            has_rows |= name == b"tr";
            let children = std::mem::take(&mut self.nodes[id].children);
            self.nodes[id].children = children
                .into_iter()
                .filter(|child| match child {
                    Child::Element(id) => {
                        !contract::HIDDEN.contains(&self.nodes[*id].name.as_str())
                    }
                    Child::Text(_) => true,
                })
                .collect();
            stack.extend(self.children(id));
        }
        // An outer transformed node already captured its descendants. Work on
        // detached descendants cannot change that immutable text, so skip it.
        for headings in [true, false] {
            if (headings && !has_headings) || (!headings && !has_rows) {
                continue;
            }
            let index = self.stripped_index()?;
            let mut stack = vec![0];
            while let Some(id) = stack.pop() {
                let name = &self.nodes[id].name;
                if headings
                    && name.len() == 2
                    && name.as_bytes()[0] == b'h'
                    && matches!(name.as_bytes()[1], b'1'..=b'6')
                {
                    let prefix = "#".repeat((name.as_bytes()[1] - b'0') as usize);
                    let mut text = String::new();
                    append(&mut text, &prefix, self.projection_budget)?;
                    append(&mut text, " ", self.projection_budget)?;
                    append(&mut text, index.get(id), self.projection_budget)?;
                    self.replace(id, text)?;
                } else if !headings && name == "tr" {
                    let mut descendants = self.children(id);
                    descendants.reverse();
                    let mut cells = 0;
                    let mut row = String::new();
                    while let Some(cell) = descendants.pop() {
                        if matches!(self.nodes[cell].name.as_str(), "th" | "td") {
                            append(
                                &mut row,
                                if cells == 0 { "| " } else { " | " },
                                self.projection_budget,
                            )?;
                            for ch in index.get(cell).chars() {
                                if ch == '|' {
                                    append(&mut row, "\\", self.projection_budget)?;
                                }
                                append(
                                    &mut row,
                                    ch.encode_utf8(&mut [0; 4]),
                                    self.projection_budget,
                                )?;
                            }
                            cells += 1;
                        }
                        descendants.extend(self.children(cell).into_iter().rev());
                    }
                    if cells != 0 {
                        append(&mut row, " |", self.projection_budget)?;
                        self.replace(id, row)?;
                    } else {
                        stack.extend(self.children(id).into_iter().rev());
                    }
                } else {
                    stack.extend(self.children(id).into_iter().rev());
                }
            }
        }
        normalize(&self.text_nodes(0, false, "\n")?)
    }
}

fn append(output: &mut String, value: &str, budget: usize) -> Result<(), Error> {
    if output
        .len()
        .checked_add(value.len())
        .is_none_or(|size| size > budget)
    {
        return Err(Error::ProjectionBudget);
    }
    output
        .try_reserve(value.len())
        .map_err(|_| Error::Allocation)?;
    output.push_str(value);
    Ok(())
}

fn normalize(text: &str) -> Result<String, Error> {
    let mut output = String::new();
    output
        .try_reserve_exact(text.len())
        .map_err(|_| Error::Allocation)?;
    let mut pending_space = String::new();
    let mut newlines = 0;
    for ch in text.chars() {
        if matches!(ch, ' ' | '\t') {
            if newlines == 0 {
                pending_space.push(ch);
            }
        } else if ch == '\n' {
            pending_space.clear();
            if newlines < 2 {
                output.push(ch);
            }
            newlines += 1;
        } else {
            output.push_str(&pending_space);
            pending_space.clear();
            output.push(ch);
            newlines = 0;
        }
    }
    output.push_str(&pending_space);
    let end = output.trim_end_matches(contract::whitespace).len();
    output.truncate(end);
    let start = output.len() - output.trim_start_matches(contract::whitespace).len();
    output.drain(..start);
    Ok(output)
}

/// Project valid UTF-8 HTML under the generated canonical tokenizer profile.
/// No recursion is used, including destruction of arbitrarily deep source trees.
pub fn project(source: &str) -> Result<String, Error> {
    project_bounded(source, usize::MAX)
}

/// Limit each text assembly and the aggregate generated heading/table text.
/// This is a caller-supplied projection budget, not a process RSS limit. Input
/// bytes and arena nodes are separate source-sized working memory. Refusal never
/// publishes partial output or silently truncates a document.
pub fn project_bounded(source: &str, projection_budget: usize) -> Result<String, Error> {
    let mut projection = Projection::new(projection_budget);
    tokenizer::parse(source, &mut projection)?;
    projection.finish()
}
