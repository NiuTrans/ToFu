//! Immutable flattened graph and invocation-local control cursor.
//! Topology and next-action selection live here; callbacks, context, journal,
//! role effects and presentation remain explicitly owned by the caller.
use serde_json::Value;
use std::collections::{HashMap, VecDeque};
#[path = "generated_flow_abi.rs"]
pub mod contract;
use contract::*;

pub struct Node {
    pub id: String,
    pub node_type: String,
    pub kind: String,
}
pub struct Input {
    pub owner_user_id: u64,
    pub tenant_id: String,
    pub task_id: String,
    pub nodes: Vec<Node>,
    pub edges: Vec<[String; 2]>,
}
impl Input {
    pub fn parse(value: Value) -> Result<Self, u32> {
        let object = value.as_object().ok_or(STATUS_INVALID_INPUT)?;
        if object.len() != 5 {
            return Err(STATUS_INVALID_INPUT);
        }
        let text = |value: &Value| {
            value
                .as_str()
                .map(str::to_owned)
                .ok_or(STATUS_INVALID_INPUT)
        };
        let nodes = value["nodes"]
            .as_array()
            .ok_or(STATUS_INVALID_INPUT)?
            .iter()
            .map(|node| {
                if node.as_object().is_none_or(|node| node.len() != 3) {
                    return Err(STATUS_INVALID_INPUT);
                }
                Ok(Node {
                    id: text(&node["id"])?,
                    node_type: text(&node["node_type"])?,
                    kind: text(&node["kind"])?,
                })
            })
            .collect::<Result<Vec<_>, _>>()?;
        let edges = value["edges"]
            .as_array()
            .ok_or(STATUS_INVALID_INPUT)?
            .iter()
            .map(|edge| {
                let edge = edge.as_array().ok_or(STATUS_INVALID_INPUT)?;
                if edge.len() != 2 {
                    return Err(STATUS_INVALID_INPUT);
                }
                Ok([text(&edge[0])?, text(&edge[1])?])
            })
            .collect::<Result<Vec<_>, _>>()?;
        Ok(Self {
            owner_user_id: value["owner_user_id"]
                .as_u64()
                .ok_or(STATUS_INVALID_INPUT)?,
            tenant_id: text(&value["tenant_id"])?,
            task_id: text(&value["task_id"])?,
            nodes,
            edges,
        })
    }
}
pub struct Graph {
    pub nodes: Vec<Node>,
    pub forward: Vec<Vec<usize>>,
    pub reverse: Vec<Vec<usize>>,
    // Identity is explicit even though this pure kernel performs no effects.
    pub owner_user_id: u64,
    pub tenant_id: String,
    pub task_id: String,
}
impl Graph {
    pub fn new(input: Input) -> Result<Self, u32> {
        if input.owner_user_id == 0 || input.task_id.is_empty() {
            return Err(STATUS_INVALID_INPUT);
        }
        let mut ids = HashMap::with_capacity(input.nodes.len());
        for (index, node) in input.nodes.iter().enumerate() {
            if node.id.is_empty() || ids.insert(node.id.as_str(), index).is_some() {
                return Err(STATUS_INVALID_INPUT);
            }
        }
        let mut forward = vec![Vec::new(); input.nodes.len()];
        let mut reverse = vec![Vec::new(); input.nodes.len()];
        for [source, target] in &input.edges {
            // Match flattened navigation: dangling edges grant no successor.
            if let (Some(&a), Some(&b)) = (ids.get(source.as_str()), ids.get(target.as_str())) {
                forward[a].push(b);
                reverse[b].push(a);
            }
        }
        Ok(Self {
            nodes: input.nodes,
            forward,
            reverse,
            owner_user_id: input.owner_user_id,
            tenant_id: input.tenant_id,
            task_id: input.task_id,
        })
    }
    pub fn next(&self, node: usize) -> Option<usize> {
        self.forward.get(node)?.first().copied()
    }
    pub fn start(&self) -> Result<usize, u32> {
        self.nodes
            .iter()
            .position(|node| node.kind == "start")
            .or_else(|| self.reverse.iter().position(Vec::is_empty))
            .ok_or(STATUS_INVALID_INPUT)
    }
    pub fn reachable(&self, start: usize, avoid: Option<usize>) -> Vec<bool> {
        let mut seen = vec![false; self.nodes.len()];
        let mut stack = vec![start];
        while let Some(node) = stack.pop() {
            if node >= seen.len() || seen[node] || (Some(node) == avoid && node != start) {
                continue;
            }
            seen[node] = true;
            stack.extend(
                self.forward[node]
                    .iter()
                    .copied()
                    .filter(|next| Some(*next) != avoid),
            );
        }
        seen
    }
    pub fn can_reach(&self, start: usize, target: usize, avoid: Option<usize>) -> bool {
        // The Python oracle recognizes target before applying the avoid fence.
        if start == target {
            return true;
        }
        let seen = self.reachable(start, avoid);
        seen.iter()
            .enumerate()
            .any(|(node, reached)| *reached && self.forward[node].contains(&target))
    }
    fn distances(&self, start: usize) -> Vec<u64> {
        let mut distances = vec![1 << 30; self.nodes.len()];
        let mut queue = VecDeque::from([start]);
        if start < distances.len() {
            distances[start] = 0;
        }
        while let Some(node) = queue.pop_front() {
            if node >= distances.len() {
                continue;
            }
            for &next in &self.forward[node] {
                if distances[next] == 1 << 30 {
                    distances[next] = distances[node] + 1;
                    queue.push_back(next);
                }
            }
        }
        distances
    }
    pub fn distance(&self, start: usize, target: usize) -> u64 {
        self.distances(start)
            .get(target)
            .copied()
            .unwrap_or(1 << 30)
    }
    pub fn loop_parts(&self, node: usize) -> (Option<usize>, Option<usize>) {
        let mut body = None;
        let mut exit = None;
        for &next in &self.forward[node] {
            if self.can_reach(next, node, Some(node)) {
                body = body.or(Some(next));
            } else {
                exit = exit.or(Some(next));
            }
        }
        body = body.or_else(|| self.next(node));
        exit = exit.or_else(|| {
            self.forward[node]
                .iter()
                .copied()
                .find(|next| Some(*next) != body)
        });
        (body, exit)
    }
    pub fn loop_planner(&self, node: usize, body: Option<usize>) -> Option<usize> {
        self.reverse[node].iter().copied().find(|&before| {
            self.nodes[before].node_type == "role"
                && !body.is_some_and(|body| self.can_reach(body, before, Some(node)))
        })
    }
    pub fn barrier(&self, branches: &[usize]) -> Option<usize> {
        let (&first, rest) = branches.split_first()?;
        let mut common = self.reachable(first, None);
        for &branch in rest {
            let reachable = self.reachable(branch, None);
            for (shared, reached) in common.iter_mut().zip(reachable) {
                *shared &= reached;
            }
        }
        let has_barrier = common
            .iter()
            .enumerate()
            .any(|(i, shared)| *shared && self.nodes[i].kind == "barrier");
        let distances = self.distances(first);
        // Stable definition order defeats nondeterministic set-order ties.
        (0..self.nodes.len())
            .filter(|&i| common[i] && (!has_barrier || self.nodes[i].kind == "barrier"))
            .min_by_key(|&i| (distances[i], i))
    }
}

pub use contract::Step;
pub struct Cursor {
    current: Option<usize>,
    stop: Option<usize>,
    remaining: u64,
    pending: Option<(usize, u32)>,
    pub closed: bool,
}
impl Cursor {
    pub fn new(start: Option<usize>, stop: Option<usize>, budget: u64) -> Self {
        Self {
            current: start,
            stop,
            remaining: budget,
            pending: None,
            closed: false,
        }
    }
    pub fn step(&mut self, graph: &Graph) -> Result<Step, u32> {
        if self.closed {
            return Err(STATUS_UNKNOWN_HANDLE);
        }
        if self.pending.is_some() {
            return Err(STATUS_INVALID_STATE);
        }
        while let Some(current) = self.current {
            if Some(current) == self.stop {
                break;
            }
            self.remaining = self.remaining.checked_sub(1).ok_or(STATUS_NODE_BUDGET)?;
            let node = &graph.nodes[current];
            if node.kind == "stop" {
                break;
            }
            if node.kind == "start"
                || (node.kind == "barrier"
                    && node.node_type != "role"
                    && node.node_type != "subflow")
            {
                self.current = graph.next(current);
                continue;
            }
            let action = match node.node_type.as_str() {
                "role" => ACTION_ROLE,
                "subflow" => ACTION_SUBFLOW,
                _ => match node.kind.as_str() {
                    "parallel" => ACTION_PARALLEL,
                    "fanout" => ACTION_FANOUT,
                    "loop" => ACTION_LOOP,
                    "branch" => ACTION_BRANCH,
                    "artifact" => ACTION_ARTIFACT,
                    "human" => ACTION_HUMAN,
                    _ => ACTION_UNKNOWN,
                },
            };
            self.pending = Some((current, action));
            return Ok(Step {
                node: current as u64,
                next: graph.next(current).map_or(NONE, |i| i as u64),
                action,
            });
        }
        self.current = None;
        Ok(Step {
            node: NONE,
            next: NONE,
            action: ACTION_HALT,
        })
    }
    pub fn advance(&mut self, graph: &Graph, target: Option<usize>) -> Result<(), u32> {
        if self.closed {
            return Err(STATUS_UNKNOWN_HANDLE);
        }
        let (node, action) = self.pending.ok_or(STATUS_INVALID_STATE)?;
        let next = match action {
            ACTION_BRANCH => {
                if !target.is_some_and(|target| graph.forward[node].contains(&target))
                    && !(target.is_none() && graph.forward[node].is_empty())
                {
                    return Err(STATUS_INVALID_INPUT);
                }
                target
            }
            ACTION_LOOP => graph.loop_parts(node).1,
            ACTION_PARALLEL => graph
                .barrier(&graph.forward[node])
                .and_then(|barrier| graph.next(barrier)),
            _ => graph.next(node),
        };
        if target != next {
            return Err(STATUS_INVALID_INPUT);
        }
        self.current = next;
        self.pending = None;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn graph() -> Graph {
        Graph::new(Input::parse(serde_json::json!({"owner_user_id":7,"tenant_id":"t","task_id":"task",
            "nodes":[{"id":"s","node_type":"control","kind":"start"},{"id":"a","node_type":"role","kind":""},
            {"id":"b","node_type":"role","kind":""},{"id":"e","node_type":"control","kind":"stop"}],
            "edges":[["s","a"],["a","b"],["b","e"]]})).unwrap()).unwrap()
    }
    #[test]
    fn cursor_never_reissues_an_unacknowledged_role_or_accepts_a_foreign_jump() {
        let graph = graph();
        let mut cursor = Cursor::new(Some(0), None, 20);
        assert_eq!(
            cursor.step(&graph).unwrap(),
            Step {
                node: 1,
                next: 2,
                action: ACTION_ROLE
            }
        );
        assert_eq!(cursor.step(&graph), Err(STATUS_INVALID_STATE));
        assert_eq!(cursor.advance(&graph, Some(3)), Err(STATUS_INVALID_INPUT));
        cursor.advance(&graph, Some(2)).unwrap();
        assert_eq!(cursor.step(&graph).unwrap().node, 2);
        cursor.advance(&graph, Some(3)).unwrap();
        assert_eq!(cursor.step(&graph).unwrap().action, ACTION_HALT);
    }
    #[test]
    fn source_stop_and_budget_fences_precede_role_publication() {
        let graph = graph();
        let mut cursor = Cursor::new(Some(0), Some(1), 20);
        assert_eq!(cursor.step(&graph).unwrap().action, ACTION_HALT);
        let mut cursor = Cursor::new(Some(0), None, 1);
        assert_eq!(cursor.step(&graph), Err(STATUS_NODE_BUDGET));
    }
}
