//! Request-local indexed merge heap for long ordinary BPE pieces.
//! Each byte-start owns at most one live heap entry; adjacent merges update
//! only affected neighbors. Rank then byte-start preserves canonical left ties.
//! Compact u32 offsets cover ordinary pieces; usize offsets preserve larger
//! representable inputs. No stale-entry backlog, cache, pool or global state.
use rustc_hash::FxHashMap;
use std::{borrow::Borrow, hash::Hash};
use crate::token_pair_ranks::PairRanks;

// Paired crossover measurements retain the linear path for medium pieces.
pub(crate) const MIN_PIECE_BYTES: usize = 1024;

trait Offset: Copy + Eq {
    const ABSENT: Self;
    fn from_index(value: usize) -> Self;
    fn index(self) -> usize;
}
impl Offset for u32 {
    const ABSENT: Self = u32::MAX;
    fn from_index(value: usize) -> Self { value as u32 }
    fn index(self) -> usize { self as usize }
}
impl Offset for usize {
    const ABSENT: Self = usize::MAX;
    fn from_index(value: usize) -> Self { value }
    fn index(self) -> usize { self }
}
struct Node<I> { next: I, previous: I, heap_slot: I, rank: u32 }
struct Merger<'a,I,K> {
    piece: &'a [u8],
    ranks: &'a FxHashMap<K,u32>,
    pairs: &'a PairRanks,
    nodes: Vec<Node<I>>,
    heap: Vec<I>,
}
impl<I:Offset,K:Borrow<[u8]> + Eq + Hash> Merger<'_,I,K> {
    fn less(&self,left:usize,right:usize) -> bool {
        let (left,right)=(self.heap[left].index(),self.heap[right].index());
        (self.nodes[left].rank,left)<(self.nodes[right].rank,right)
    }
    fn swap(&mut self,left:usize,right:usize) {
        self.heap.swap(left,right);
        self.nodes[self.heap[left].index()].heap_slot=I::from_index(left);
        self.nodes[self.heap[right].index()].heap_slot=I::from_index(right);
    }
    fn up(&mut self,mut slot:usize) -> usize {
        while slot>0 {
            let parent=(slot-1)/2;
            if !self.less(slot,parent) {break;}
            self.swap(slot,parent);slot=parent;
        }
        slot
    }
    fn down(&mut self,mut slot:usize) {
        loop {
            let left=slot*2+1;
            if left>=self.heap.len() {break;}
            let right=left+1;
            let child=if right<self.heap.len() && self.less(right,left) {right} else {left};
            if !self.less(child,slot) {break;}
            self.swap(slot,child);slot=child;
        }
    }
    fn remove(&mut self,index:usize) {
        let slot=self.nodes[index].heap_slot;
        if slot==I::ABSENT {return;}
        let slot=slot.index();
        let last=self.heap.len()-1;
        self.swap(slot,last);self.heap.pop();
        self.nodes[index].heap_slot=I::ABSENT;
        if slot<self.heap.len() {let slot=self.up(slot);self.down(slot);}
    }
    fn rank(&self,index:usize) -> u32 {
        let right=self.nodes[index].next.index();
        if right>=self.piece.len() {return u32::MAX;}
        let end=self.nodes[right].next.index();
        *self.ranks.get(&self.piece[index..end]).unwrap_or(&u32::MAX)
    }
    fn update(&mut self,index:usize) {
        self.nodes[index].rank=self.rank(index);
        if self.nodes[index].rank==u32::MAX {self.remove(index);return;}
        let slot=self.nodes[index].heap_slot;
        let slot=if slot==I::ABSENT {
            let slot=self.heap.len();self.heap.push(I::from_index(index));
            self.nodes[index].heap_slot=I::from_index(slot);slot
        } else {slot.index()};
        let slot=self.up(slot);self.down(slot);
    }
    fn run(mut self) -> usize {
        for index in 0..self.piece.len()-1 {
            self.nodes[index].rank=self.pairs.get(self.piece[index],self.piece[index+1]);
            if self.nodes[index].rank!=u32::MAX {
                self.nodes[index].heap_slot=I::from_index(self.heap.len());
                self.heap.push(I::from_index(index));
            }
        }
        for slot in (0..self.heap.len()/2).rev() {self.down(slot);}
        let mut tokens=self.piece.len();
        while let Some(&index)=self.heap.first() {
            let index=index.index();
            let right=self.nodes[index].next.index();
            let end=self.nodes[right].next;
            let previous=self.nodes[index].previous;
            self.remove(right);
            self.nodes[index].next=end;
            if end.index()<self.piece.len() {self.nodes[end.index()].previous=I::from_index(index);}
            self.nodes[right].next=I::ABSENT;
            self.update(index);
            if previous!=I::ABSENT {self.update(previous.index());}
            tokens-=1;
        }
        tokens
    }
}
fn merge<I:Offset,K:Borrow<[u8]> + Eq + Hash>(piece:&[u8],ranks:&FxHashMap<K,u32>,pairs:&PairRanks) -> usize {
    let nodes=(0..piece.len()).map(|index|Node {
        next:I::from_index(index+1),previous:if index==0 {I::ABSENT} else {I::from_index(index-1)},
        heap_slot:I::ABSENT,rank:u32::MAX,
    }).collect();
    Merger::<I,K>{piece,ranks,pairs,nodes,heap:Vec::with_capacity(piece.len())}.run()
}
pub(crate) fn count<K:Borrow<[u8]> + Eq + Hash>(piece:&[u8],ranks:&FxHashMap<K,u32>,pairs:&PairRanks) -> usize {
    if piece.len()<u32::MAX as usize {merge::<u32,_>(piece,ranks,pairs)} else {merge::<usize,_>(piece,ranks,pairs)}
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn compact_and_wide_heaps_match_canonical_merges_including_ties() {
        let mut random=0xb971_20ab_1234_5678u64;
        for case in 0..500 {
            random=random.wrapping_mul(6364136223846793005).wrapping_add(1);
            let length=2+(random as usize%255);
            let mut piece=Vec::with_capacity(length);
            let mut ranks=FxHashMap::default();
            for byte in 0u8..=255 {ranks.insert(vec![byte],byte as u32);}
            for _ in 0..length {
                random=random.wrapping_mul(6364136223846793005).wrapping_add(1);
                piece.push((random>>32) as u8%8);
            }
            for size in 2..=8 {
                for word in piece.windows(size) {
                    random=random.wrapping_mul(6364136223846793005).wrapping_add(1);
                    if random%3!=0 {ranks.insert(word.to_vec(),(random>>32) as u32%13);}
                }
            }
            let expected=tiktoken_rs::byte_pair_split(&piece,&ranks).len();
            let pairs=PairRanks::from_ranks(&ranks);
            assert_eq!(merge::<u32,_>(&piece,&ranks,&pairs),expected,"compact case {case}");
            assert_eq!(merge::<usize,_>(&piece,&ranks,&pairs),expected,"wide case {case}");
        }
    }
    #[test]
    fn indexed_scratch_capacity_has_one_entry_per_byte() {
        assert_eq!(std::mem::size_of::<Node<u32>>(),16);
        assert_eq!(std::mem::size_of::<u32>(),4);
        assert_eq!(std::mem::size_of::<Node<usize>>(),4*std::mem::size_of::<usize>());
    }
}
