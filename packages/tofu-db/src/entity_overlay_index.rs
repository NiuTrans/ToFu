//! Reconstructible overlay indexes; no wire format or independent authority.
//!
//! Two copy-on-write AVL roots order shared records by (key, version) and
//! (version, key). Capturing a view clones one root; updates copy only shared
//! search paths. Deterministic balancing bounds hostile sorted-key workloads.
//! Records and nodes charge their live allocation to the authority's counter.

use std::cmp::Ordering;
use std::sync::{Arc, atomic::{AtomicUsize, Ordering::Relaxed}};

const ARC_HEADER_BYTES: usize = 2 * std::mem::size_of::<usize>();

#[derive(Debug)]
pub(super) struct Record {
    pub key: Vec<u8>,
    pub value: Option<Vec<u8>>,
    pub sequence: u64,
    resident: Arc<AtomicUsize>,
    bytes: usize,
}

impl Record {
    pub fn new(key: Vec<u8>, value: Option<Vec<u8>>, sequence: u64,
        resident: &Arc<AtomicUsize>) -> Arc<Self> {
        let bytes = std::mem::size_of::<Self>() + ARC_HEADER_BYTES + key.capacity()
            + value.as_ref().map_or(0, Vec::capacity);
        resident.fetch_add(bytes, Relaxed);
        Arc::new(Self { key, value, sequence, resident: Arc::clone(resident), bytes })
    }

    pub fn pending_bytes(&self) -> usize { self.bytes + 2 * NODE_BYTES }
}

impl Drop for Record {
    fn drop(&mut self) { self.resident.fetch_sub(self.bytes, Relaxed); }
}

type Link = Option<Arc<Node>>;
const NODE_BYTES: usize = std::mem::size_of::<Node>() + ARC_HEADER_BYTES;

#[derive(Debug)]
struct Node {
    record: Arc<Record>,
    left: Link,
    right: Link,
    height: u16,
}

impl Node {
    fn new(record: Arc<Record>) -> Arc<Self> {
        record.resident.fetch_add(NODE_BYTES, Relaxed);
        Arc::new(Self { record, left: None, right: None, height: 1 })
    }
    fn refresh(&mut self) { self.height = 1 + height(&self.left).max(height(&self.right)); }
}

impl Clone for Node {
    fn clone(&self) -> Self {
        self.record.resident.fetch_add(NODE_BYTES, Relaxed);
        Self { record: Arc::clone(&self.record), left: self.left.clone(),
            right: self.right.clone(), height: self.height }
    }
}

impl Drop for Node {
    fn drop(&mut self) { self.record.resident.fetch_sub(NODE_BYTES, Relaxed); }
}

fn height(link: &Link) -> u16 { link.as_ref().map_or(0, |node| node.height) }
fn balance(node: &Node) -> i32 { i32::from(height(&node.left)) - i32::from(height(&node.right)) }

fn rotate_left(root: &mut Arc<Node>) {
    let mut next = Arc::make_mut(root).right.take().expect("AVL right child");
    Arc::make_mut(root).right = Arc::make_mut(&mut next).left.take();
    Arc::make_mut(root).refresh();
    let previous = std::mem::replace(root, next);
    Arc::make_mut(root).left = Some(previous);
    Arc::make_mut(root).refresh();
}

fn rotate_right(root: &mut Arc<Node>) {
    let mut next = Arc::make_mut(root).left.take().expect("AVL left child");
    Arc::make_mut(root).left = Arc::make_mut(&mut next).right.take();
    Arc::make_mut(root).refresh();
    let previous = std::mem::replace(root, next);
    Arc::make_mut(root).right = Some(previous);
    Arc::make_mut(root).refresh();
}

fn rebalance(root: &mut Arc<Node>) {
    Arc::make_mut(root).refresh();
    if balance(root) > 1 {
        if balance(root.left.as_ref().unwrap()) < 0 {
            rotate_left(Arc::make_mut(root).left.as_mut().unwrap());
        }
        rotate_right(root);
    } else if balance(root) < -1 {
        if balance(root.right.as_ref().unwrap()) > 0 {
            rotate_right(Arc::make_mut(root).right.as_mut().unwrap());
        }
        rotate_left(root);
    }
}

fn compare<const BY_SEQUENCE: bool>(left: &Record, right: &Record) -> Ordering {
    if BY_SEQUENCE { left.sequence.cmp(&right.sequence).then_with(|| left.key.cmp(&right.key)) }
    else { left.key.cmp(&right.key).then_with(|| left.sequence.cmp(&right.sequence)) }
}

#[derive(Clone, Debug, Default)]
pub(super) struct Index<const BY_SEQUENCE: bool> { root: Link }

impl<const BY_SEQUENCE: bool> Index<BY_SEQUENCE> {
    pub fn insert(&mut self, record: Arc<Record>) {
        fn insert<const S: bool>(link: &mut Link, record: Arc<Record>) {
            let Some(root) = link else { *link = Some(Node::new(record)); return; };
            match compare::<S>(&record, &root.record) {
                Ordering::Less => insert::<S>(&mut Arc::make_mut(root).left, record),
                Ordering::Greater => insert::<S>(&mut Arc::make_mut(root).right, record),
                Ordering::Equal => panic!("duplicate overlay position"),
            }
            rebalance(root);
        }
        insert::<BY_SEQUENCE>(&mut self.root, record);
    }

    pub fn remove(&mut self, record: &Record) {
        fn remove<const S: bool>(link: &mut Link, record: &Record) {
            let Some(root) = link else { panic!("missing overlay position"); };
            match compare::<S>(record, &root.record) {
                Ordering::Less => remove::<S>(&mut Arc::make_mut(root).left, record),
                Ordering::Greater => remove::<S>(&mut Arc::make_mut(root).right, record),
                Ordering::Equal => {
                    if root.left.is_none() { *link = Arc::make_mut(root).right.take(); return; }
                    if root.right.is_none() { *link = Arc::make_mut(root).left.take(); return; }
                    let mut next = root.right.as_deref().unwrap();
                    while let Some(left) = next.left.as_deref() { next = left; }
                    let successor = Arc::clone(&next.record);
                    Arc::make_mut(root).record = Arc::clone(&successor);
                    remove::<S>(&mut Arc::make_mut(root).right, &successor);
                }
            }
            rebalance(root);
        }
        remove::<BY_SEQUENCE>(&mut self.root, record);
    }

    /// Last record satisfying a monotonic prefix predicate, in logarithmic work.
    pub fn floor(&self, mut at_or_before: impl FnMut(&Record) -> bool) -> Option<&Arc<Record>> {
        let mut current = self.root.as_deref();
        let mut found = None;
        while let Some(node) = current {
            if at_or_before(&node.record) { found = Some(&node.record); current = node.right.as_deref(); }
            else { current = node.left.as_deref(); }
        }
        found
    }

    /// Seek to a suffix (forward) or prefix (reverse), borrowing all payloads.
    pub fn cursor(&self, reverse: bool, mut included: impl FnMut(&Record) -> bool) -> Cursor<'_> {
        let mut stack = Vec::with_capacity(height(&self.root) as usize);
        let mut current = self.root.as_deref();
        while let Some(node) = current {
            if included(&node.record) {
                stack.push(node);
                current = if reverse { node.right.as_deref() } else { node.left.as_deref() };
            } else {
                current = if reverse { node.left.as_deref() } else { node.right.as_deref() };
            }
        }
        Cursor { stack, reverse }
    }
}

pub(super) struct Cursor<'a> { stack: Vec<&'a Node>, reverse: bool }

impl<'a> Iterator for Cursor<'a> {
    type Item = &'a Arc<Record>;
    fn next(&mut self) -> Option<Self::Item> {
        let node = self.stack.pop()?;
        let mut next = if self.reverse { node.left.as_deref() } else { node.right.as_deref() };
        while let Some(child) = next {
            self.stack.push(child);
            next = if self.reverse { child.right.as_deref() } else { child.left.as_deref() };
        }
        Some(&node.record)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn point_lookup_and_snapshot_capture_do_not_walk_the_backlog() {
        let resident = Arc::new(AtomicUsize::new(0));
        let mut index = Index::<false>::default();
        for sequence in 0..12_000u64 {
            index.insert(Record::new(sequence.to_be_bytes().to_vec(), None, sequence, &resident));
        }
        let before = resident.load(Relaxed);
        let pinned = index.clone();
        assert_eq!(resident.load(Relaxed), before);
        assert!(Arc::ptr_eq(index.root.as_ref().unwrap(), pinned.root.as_ref().unwrap()));
        for key in [0u64, 500, 6_000, 11_999, 12_001] {
            let mut probes = 0;
            let found = pinned.floor(|record| { probes += 1; record.key.as_slice() <= key.to_be_bytes().as_slice() });
            assert_eq!(found.unwrap().sequence, key.min(11_999));
            assert!(probes <= 2 * 14, "lookup visited {probes} of 12,000 positions");
        }
        index.insert(Record::new(12_000u64.to_be_bytes().to_vec(), None, 12_000, &resident));
        let payload_charge = std::mem::size_of::<Record>() + ARC_HEADER_BYTES + 8;
        assert!(resident.load(Relaxed) - before <= payload_charge + 2 * 14 * NODE_BYTES);
        assert_eq!(pinned.floor(|_| true).unwrap().sequence, 11_999);
        drop(index); drop(pinned);
        assert_eq!(resident.load(Relaxed), 0);
    }

    fn check(node: &Link, lower: Option<&Record>, upper: Option<&Record>) -> usize {
        let Some(node) = node else { return 0; };
        if let Some(lower) = lower { assert_eq!(compare::<false>(lower, &node.record), Ordering::Less); }
        if let Some(upper) = upper { assert_eq!(compare::<false>(&node.record, upper), Ordering::Less); }
        assert_eq!(node.height, 1 + height(&node.left).max(height(&node.right)));
        assert!(balance(node).abs() <= 1);
        1 + check(&node.left, lower, Some(&node.record)) + check(&node.right, Some(&node.record), upper)
    }

    #[test]
    fn indexed_paths_balance_sorted_and_permuted_mutations_without_changing_pins() {
        let resident = Arc::new(AtomicUsize::new(0));
        for sorted in [true, false] {
            let mut index = Index::<false>::default();
            let mut records = Vec::new();
            for n in 0..2048u64 {
                let key = if sorted { n } else { (n * 1597) % 2048 };
                let record = Record::new(key.to_be_bytes().to_vec(), Some(vec![1; 17]), n, &resident);
                index.insert(Arc::clone(&record));
                records.push(record);
                assert_eq!(check(&index.root, None, None), records.len());
            }
            let pinned = index.clone();
            assert!(Arc::ptr_eq(index.root.as_ref().unwrap(), pinned.root.as_ref().unwrap()));
            for (n, record) in records.iter().enumerate() {
                index.remove(record);
                assert_eq!(check(&index.root, None, None), records.len() - n - 1);
            }
            assert_eq!(check(&pinned.root, None, None), 2048);
            drop(pinned); drop(records); drop(index);
            assert_eq!(resident.load(Relaxed), 0);
        }
    }
}
