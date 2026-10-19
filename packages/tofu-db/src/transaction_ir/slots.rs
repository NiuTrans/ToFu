//! Demand-owned sparse values for interpreter slot identifiers.
//! Identity is a usize; storage grows with touched slots, never the largest
//! identifier. Read misses do not allocate. Mutation inserts one default cell.

use std::collections::BTreeMap;
use std::ops::{Index, IndexMut};

pub(super) struct SparseSlotValues<T: Default> {
    values: BTreeMap<usize, T>,
    missing: T,
}

impl<T: Default> SparseSlotValues<T> {
    pub(super) fn new() -> Self {
        Self { values: BTreeMap::new(), missing: T::default() }
    }
    pub(super) fn get(&self, index: usize) -> Option<&T> {
        self.values.get(&index)
    }
    pub(super) fn get_mut(&mut self, index: usize) -> Option<&mut T> {
        self.values.get_mut(&index)
    }
    pub(super) fn get_or_insert(&mut self, index: usize) -> &mut T {
        self.values.entry(index).or_default()
    }
    #[cfg(test)]
    pub(super) fn len(&self) -> usize { self.values.len() }
}

impl<T: Default> Index<usize> for SparseSlotValues<T> {
    type Output = T;
    fn index(&self, index: usize) -> &T {
        self.get(index).unwrap_or(&self.missing)
    }
}
impl<T: Default> IndexMut<usize> for SparseSlotValues<T> {
    fn index_mut(&mut self, index: usize) -> &mut T {
        self.get_or_insert(index)
    }
}

#[cfg(test)]
mod tests {
    use super::SparseSlotValues;

    #[test]
    fn absent_extreme_identifiers_read_default_without_allocating() {
        let values = SparseSlotValues::<Option<usize>>::new();
        assert_eq!(values[usize::MAX], None);
        assert_eq!(values[0], None);
        assert_eq!(values.get(usize::MAX), None);
        assert_eq!(values.len(), 0);
    }

    #[test]
    fn writes_allocate_only_distinct_touched_identifiers() {
        let mut values = SparseSlotValues::<Option<usize>>::new();
        values[usize::MAX] = Some(7);
        values[0] = Some(11);
        values[usize::MAX] = Some(13);
        assert_eq!(values[usize::MAX], Some(13));
        assert_eq!(values[0], Some(11));
        assert_eq!(values.len(), 2);
    }

    #[test]
    fn mutable_miss_does_not_create_a_slot() {
        let mut values = SparseSlotValues::<Option<usize>>::new();
        assert_eq!(values.get_mut(usize::MAX), None);
        assert_eq!(values.len(), 0);
        *values.get_or_insert(usize::MAX) = Some(17);
        *values.get_mut(usize::MAX).unwrap() = Some(19);
        assert_eq!(values[usize::MAX], Some(19));
        assert_eq!(values.len(), 1);
    }

    #[test]
    fn stored_values_are_owned_and_released_with_the_table() {
        use std::sync::{Arc, atomic::{AtomicUsize, Ordering}};
        #[derive(Default)]
        struct Value(Option<Arc<AtomicUsize>>);
        impl Drop for Value {
            fn drop(&mut self) {
                if let Some(counter) = &self.0 { counter.fetch_add(1, Ordering::SeqCst); }
            }
        }
        let drops = Arc::new(AtomicUsize::new(0));
        {
            let mut values = SparseSlotValues::<Value>::new();
            values[usize::MAX] = Value(Some(drops.clone()));
            values[42] = Value(Some(drops.clone()));
            assert_eq!(drops.load(Ordering::SeqCst), 0);
        }
        assert_eq!(drops.load(Ordering::SeqCst), 2);
    }
}
