//! Provider-neutral bounded SSE byte framing. Provider consumers decide stop and
//! completion; close is intentional cutoff, finalize validates actual EOF.
use crate::execution_contract::MAX_SSE_PENDING_DIAGNOSTICS;
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Event {
    pub data: String,
    pub event: String,
    pub event_id: String,
    pub retry_ms: Option<u32>,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Issue {
    InvalidUtf8,
    EventTooLarge,
    TruncatedEvent,
}
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Issues {
    pub count: u64,
    pub diagnostics: Vec<Issue>,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Error {
    InvalidBudget,
    Closed,
}
pub struct Framer {
    max_bytes: usize,
    closed: bool,
    start: bool,
    swallow_lf: bool,
    utf8: [u8; 4],
    utf8_len: usize,
    utf8_expected: usize,
    line: String,
    line_content: bool,
    data: Vec<String>,
    saw_data: bool,
    event: String,
    id: String,
    retry: Option<u32>,
    bytes: usize,
    discard: bool,
    issues: Issues,
}
impl Framer {
    pub fn new(max_event_bytes: usize) -> Result<Self, Error> {
        if max_event_bytes == 0 {
            return Err(Error::InvalidBudget);
        }
        Ok(Self {
            max_bytes: max_event_bytes,
            closed: false,
            start: true,
            swallow_lf: false,
            utf8: [0; 4],
            utf8_len: 0,
            utf8_expected: 0,
            line: String::new(),
            line_content: false,
            data: Vec::new(),
            saw_data: false,
            event: String::new(),
            id: String::new(),
            retry: None,
            bytes: 0,
            discard: false,
            issues: Issues::default(),
        })
    }
    fn issue(&mut self, kind: Issue) {
        self.issues.count = self.issues.count.saturating_add(1);
        if self.issues.diagnostics.len() < MAX_SSE_PENDING_DIAGNOSTICS {
            self.issues.diagnostics.push(kind);
        }
    }
    pub fn drain_issues(&mut self) -> Issues {
        std::mem::take(&mut self.issues)
    }
    fn reset(&mut self) {
        self.line = String::new();
        self.line_content = false;
        self.data = Vec::new();
        self.saw_data = false;
        self.event = String::new();
        self.id = String::new();
        self.retry = None;
        self.bytes = 0;
        self.discard = false;
    }
    fn invalidate(&mut self) {
        self.discard = true;
        self.line = String::new();
        self.data = Vec::new();
        self.saw_data = false;
    }
    fn add_bytes(&mut self, n: usize) {
        self.bytes = self.bytes.saturating_add(n);
        if self.bytes > self.max_bytes && !self.discard {
            self.issue(Issue::EventTooLarge);
            self.invalidate();
        }
    }
    fn line_end(&mut self) -> Option<Event> {
        let empty = !self.line_content;
        self.line_content = false;
        let line = std::mem::take(&mut self.line);
        if !empty {
            if !self.discard {
                self.add_bytes(1);
                if !self.discard {
                    let (field, value) = line
                        .split_once(':')
                        .map(|(f, v)| (f, v.strip_prefix(' ').unwrap_or(v)))
                        .unwrap_or((&line, ""));
                    match field {
                        "data" => {
                            self.saw_data = true;
                            self.data.push(value.into());
                        }
                        "event" => self.event = value.into(),
                        "id" if !value.contains('\0') => self.id = value.into(),
                        "retry"
                            if !value.is_empty() && value.bytes().all(|b| b.is_ascii_digit()) =>
                        {
                            let digits = value.trim_start_matches('0');
                            if digits.len() <= 10 {
                                if let Ok(n) = if digits.is_empty() {
                                    Ok(0)
                                } else {
                                    digits.parse::<u32>()
                                } {
                                    if n <= i32::MAX as u32 {
                                        self.retry = Some(n);
                                    }
                                }
                            }
                        }
                        _ => {}
                    }
                }
            }
            return None;
        }
        let event = if !self.discard && self.saw_data {
            Some(Event {
                data: self.data.join("\n"),
                event: std::mem::take(&mut self.event),
                event_id: std::mem::take(&mut self.id),
                retry_ms: self.retry,
            })
        } else {
            None
        };
        self.reset();
        event
    }
    fn character(&mut self, c: char) -> Option<Event> {
        if self.start {
            self.start = false;
            if c == '\u{feff}' {
                return None;
            }
        }
        if self.swallow_lf {
            self.swallow_lf = false;
            if c == '\n' {
                return None;
            }
        }
        if c == '\r' || c == '\n' {
            self.swallow_lf = c == '\r';
            return self.line_end();
        }
        self.line_content = true;
        if !self.discard {
            self.add_bytes(c.len_utf8());
            if !self.discard {
                self.line.push(c);
            }
        }
        None
    }
    fn byte(&mut self, b: u8) -> Option<Event> {
        if self.utf8_len > 0 {
            let first = self.utf8[0];
            let second = self.utf8_len == 1;
            let valid = (0x80..=0xbf).contains(&b)
                && !(second
                    && ((first == 0xe0 && b < 0xa0)
                        || (first == 0xed && b >= 0xa0)
                        || (first == 0xf0 && b < 0x90)
                        || (first == 0xf4 && b >= 0x90)));
            if !valid {
                self.issue(Issue::InvalidUtf8);
                self.invalidate();
                self.utf8_len = 0;
                return self.byte(b);
            }
            self.utf8[self.utf8_len] = b;
            self.utf8_len += 1;
            if self.utf8_len == self.utf8_expected {
                let c = std::str::from_utf8(&self.utf8[..self.utf8_len])
                    .expect("validated UTF-8")
                    .chars()
                    .next()
                    .unwrap();
                self.utf8_len = 0;
                return self.character(c);
            }
            return None;
        }
        if b < 0x80 {
            return self.character(b as char);
        }
        self.utf8_expected = match b {
            0xc2..=0xdf => 2,
            0xe0..=0xef => 3,
            0xf0..=0xf4 => 4,
            _ => {
                self.issue(Issue::InvalidUtf8);
                self.invalidate();
                return None;
            }
        };
        self.utf8[0] = b;
        self.utf8_len = 1;
        None
    }
    /// Consume through the first consumer stop; return its exact byte boundary.
    /// No allocation, decoding or diagnostics inspect the remaining trailer.
    pub fn feed_until<F>(&mut self, chunk: &[u8], mut consume: F) -> Result<usize, Error>
    where
        F: FnMut(Event, Issues) -> bool,
    {
        if self.closed {
            return Err(Error::Closed);
        }
        for (index, b) in chunk.iter().enumerate() {
            if let Some(event) = self.byte(*b) {
                let issues = self.drain_issues();
                let decision = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    consume(event, issues)
                }));
                let stop = match decision {
                    Ok(stop) => stop,
                    Err(payload) => {
                        self.close();
                        std::panic::resume_unwind(payload);
                    }
                };
                if stop {
                    self.close();
                    return Ok(index + 1);
                }
            }
        }
        Ok(chunk.len())
    }
    pub fn close(&mut self) {
        self.closed = true;
        self.utf8_len = 0;
        self.reset();
    }
    pub fn finalize(&mut self) {
        if self.closed {
            return;
        }
        self.closed = true;
        if self.utf8_len > 0 {
            self.issue(Issue::InvalidUtf8);
            self.discard = true;
        }
        if self.discard || self.saw_data || self.line.split(':').next() == Some("data") {
            self.issue(Issue::TruncatedEvent);
        }
        self.utf8_len = 0;
        self.reset();
    }
}
