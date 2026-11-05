/** Surface lane discovery, viewport budgets and stable-identity window remapping.
 * Entry: prepareSurfaceWindows. Inputs/outputs are presentation data only;
 * DOM geometry, history requests, frame scheduling and mutation stay at Surface.
 * Dependencies: presentation types, surface-contracts and geometric constants. */
import type {
  ConversationLaneViewModel, ConversationNavigationIndex, ConversationViewModel,
} from '../presentation/view-model/types';
import type { ConversationRetainedTail, ConversationWindowOptions } from './surface-contracts';
import {
  CONVERSATION_BLOCK_INITIAL_MINIMUM, CONVERSATION_BLOCK_ESTIMATED_HEIGHT,
  CONVERSATION_BLOCK_OVERSCAN_VIEWPORTS, CONVERSATION_TURN_INITIAL_MINIMUM,
  CONVERSATION_TURN_ESTIMATED_HEIGHT,
} from './surface-windowing';

export interface SurfaceLaneWindow {
  start: number;
  end: number;
  atTail: boolean;
  firstMountedTurnId: string;
  lastMountedTurnId: string;
  loadedTurns: number;
}

export interface PreparedSurfaceWindows {
  lanesById: Map<string, ConversationLaneViewModel>;
  laneWindows: Map<string, SurfaceLaneWindow>;
  laneBudgets: Map<string, number>;
  laneCaps: Map<string, number>;
  viewportBlockBudget: number;
  retainedTails: string[];
}

const maxWindowStart = (total: number, maxTurns: number): number => Math.max(0, total - maxTurns);

// Serialized or externally assembled views retain the explicit graph as their
// source of truth. A copied/replaced lane cannot inherit an older lane's index.
const navigationForLane = (navigation: ConversationNavigationIndex | undefined,
  lane: ConversationLaneViewModel): ConversationNavigationIndex | undefined => (
  navigation?.lanes instanceof Map && navigation.turnLocations instanceof Map
    && navigation.lanes.get(lane.laneId) === lane ? navigation : undefined
);

const collectLanes = (viewModel: ConversationViewModel): {
  all: Map<string, ConversationLaneViewModel>;
  expanded: ConversationLaneViewModel[];
} => {
  const all = new Map<string, ConversationLaneViewModel>();
  const expanded: ConversationLaneViewModel[] = [];
  const visit = (lane: ConversationLaneViewModel, main = false): void => {
    if (all.has(lane.laneId)) return;
    all.set(lane.laneId, lane);
    if (!main && !lane.expanded) return;
    expanded.push(lane);
    const navigation = navigationForLane(viewModel.navigation, lane);
    if (navigation && lane.childLaneIds) {
      for (const id of lane.childLaneIds) {
        const branch = navigation.lanes.get(id);
        if (branch) visit(branch);
      }
      return;
    }
    for (const turn of lane.turns) {
      for (const branch of turn.branches) visit(branch);
    }
  };
  visit(viewModel.mainLane, true);
  for (const lane of viewModel.orphanLanes) visit(lane);
  return { all, expanded };
};

export function prepareSurfaceWindows(
  viewModel: ConversationViewModel,
  previousWindows: ReadonlyMap<string, SurfaceLaneWindow>,
  windowOptions: Required<Pick<ConversationWindowOptions, 'maxTurns' | 'batchSize' | 'maxBlocksPerTurn'>>,
  measurement: { viewportHeight: number; geometric: boolean; following?: boolean; reset: boolean },
  initialTail: ConversationRetainedTail | null = null,
): PreparedSurfaceWindows {
  const retainedTails: string[] = [];
  const lanes = collectLanes(viewModel);
  const lanesById = lanes.all;
  const laneWindows = new Map<string, SurfaceLaneWindow>();

  const laneBudgets = new Map<string, number>();
  const laneCount = Math.max(1, lanes.expanded.length);
  // Read geometry once before DOM reconciliation, never between insertions.
  const { viewportHeight } = measurement;
  const viewportBlockBudget = viewportHeight > 0
    ? Math.min(windowOptions.maxBlocksPerTurn, Math.max(
      CONVERSATION_BLOCK_INITIAL_MINIMUM,
      Math.ceil(viewportHeight / CONVERSATION_BLOCK_ESTIMATED_HEIGHT)
        * CONVERSATION_BLOCK_OVERSCAN_VIEWPORTS,
    ))
    : windowOptions.maxBlocksPerTurn;
  const visibleTurnBudget = viewportHeight > 0
    ? Math.min(windowOptions.maxTurns, Math.max(
      CONVERSATION_TURN_INITIAL_MINIMUM,
      Math.ceil(viewportHeight / CONVERSATION_TURN_ESTIMATED_HEIGHT)
        * CONVERSATION_BLOCK_OVERSCAN_VIEWPORTS,
    ))
    : windowOptions.maxTurns;
  const base = Math.floor(visibleTurnBudget / laneCount);
  let remainder = visibleTurnBudget % laneCount;
  const capShare = Math.floor(windowOptions.maxTurns / laneCount);
  const laneCaps = new Map<string, number>();
  for (const lane of lanes.expanded) {
    const budget = base + (remainder > 0 ? 1 : 0);
    if (remainder > 0) remainder -= 1;
    laneBudgets.set(lane.laneId, budget);
    laneCaps.set(lane.laneId, Math.max(budget, capShare));
  }
  // Scroll demand measures geometry only where a viewport is attached.
  const { geometric } = measurement;
  for (const [laneId, lane] of lanes.all) {
    const maxTurns = laneBudgets.get(laneId) ?? 0;
    const cap = laneCaps.get(laneId) ?? 0;
    const total = lane.turns.length;
    const tailStart = maxWindowStart(total, maxTurns);
    const previous = measurement.reset ? undefined : previousWindows.get(laneId);
    const atTail = !previous ? true
      : measurement.following ?? previous.atTail;
    const navigation = navigationForLane(viewModel.navigation, lane);
    const indexOf = (turnId: string): number => {
      if (!turnId) return -1;
      if (navigation) {
        const location = navigation.turnLocations.get(turnId);
        return location?.laneId === laneId && lane.turns[location.index]?.turnId === turnId
          ? location.index : -1;
      }
      return lane.turns.findIndex(turn => turn.turnId === turnId);
    };
    let start = tailStart;
    let end = total;
    if (previous) {
      const first = indexOf(previous.firstMountedTurnId);
      const last = indexOf(previous.lastMountedTurnId);
      const count = Math.max(1, previous.end - previous.start);
      if (atTail) {
        // Residency retires far Turns itself. Snapping a grown tail window
        // back to the budget would unmount and remount every streamed frame;
        // re-expanding a retired one remounted and retired the same Turns on
        // every render. Newly loaded Turns still fill an unretired window.
        if (geometric && first >= 0) {
          const retired = previous.start > maxWindowStart(previous.loadedTurns, maxTurns);
          start = retired ? first : Math.min(first, tailStart);
          if (retired && first > tailStart) retainedTails.push(laneId);
        }
      } else if (first >= 0) {
        start = first;
        end = last >= first ? last + 1 : first + count;
        // A remote page landed above a window resting on the loaded top:
        // the reader asked for it, so part of it mounts immediately.
        if (previous.start === 0 && first > 0 && total > previous.loadedTurns) {
          start = Math.max(0, first - Math.min(windowOptions.batchSize,
            Math.max(1, Math.floor(maxTurns / 2))));
        }
      } else {
        start = Math.min(previous.start, tailStart);
        end = start + count;
      }
    } else if (initialTail && geometric && laneId === viewModel.mainLane.laneId
        && lane.turns[total - 1]?.turnId === initialTail.lastTurnId) {
      // A revisited conversation starts at the previously retired range when
      // its newest Turn is unchanged; Surface checks the remaining geometry.
      const first = indexOf(initialTail.firstTurnId);
      if (first > tailStart) {
        start = first;
        retainedTails.push(laneId);
      }
    }
    start = Math.max(0, Math.min(start, total));
    end = Math.max(start, Math.min(end, total));
    if (end - start > cap) {
      if (atTail) start = end - cap;
      else end = start + cap;
    }
    if (maxTurns <= 0) {
      start = total;
      end = total;
    }
    laneWindows.set(laneId, {
      start,
      end,
      atTail,
      firstMountedTurnId: previous?.firstMountedTurnId ?? '',
      lastMountedTurnId: previous?.lastMountedTurnId ?? '',
      loadedTurns: total,
    });
  }
  return { lanesById, laneWindows, laneBudgets, laneCaps, viewportBlockBudget, retainedTails };
}
