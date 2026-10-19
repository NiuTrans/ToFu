/** Selection-to-branch handoff. The injected Turn command is the sole write
 * authority; opening the acknowledged lane is fenced against navigation.
 */
export interface SelectionBranchPorts {
  create(): Promise<{ lane?: { laneId?: string } }>;
  isCurrent(): boolean;
  open(laneId: string): void;
  error(error: unknown): void;
}

export async function createSelectionBranch(ports: SelectionBranchPorts): Promise<boolean> {
  let response: Awaited<ReturnType<SelectionBranchPorts['create']>>;
  try {
    response = await ports.create();
  } catch (error) {
    if (ports.isCurrent()) ports.error(error);
    return false;
  }
  // Creation has already committed. A presentation failure must not invite a
  // retry that creates another lane; the acknowledged lane remains in history.
  if (response.lane?.laneId && ports.isCurrent()) {
    try { ports.open(response.lane.laneId); }
    catch (error) { ports.error(error); }
  }
  return true;
}
