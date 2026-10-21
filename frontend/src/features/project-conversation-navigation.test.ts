import { expect, it, vi } from 'vitest';
import { createProjectConversationNavigation } from './project-conversation-navigation';
function fixture() {
  const ids = new Set<string>();
  return { contains: (id: string) => ids.has(id), fetch: vi.fn(async (id: string) => ({ id })),
    merge: vi.fn((row: { id: string }) => { ids.add(row.id); }), navigate: vi.fn(), error: vi.fn() };
}
it('fetches an unloaded bound ID and merges a catalog shell before ordinary navigation', async () => {
  const owner = createProjectConversationNavigation(), ports = fixture();
  await owner.resolveMissing('coordinator', ports);
  expect(ports.fetch).toHaveBeenCalledWith('coordinator');
  expect(ports.merge).toHaveBeenCalledWith({ id: 'coordinator' });
  expect(ports.navigate).toHaveBeenCalledWith('coordinator');
  expect(owner.resolveMissing('coordinator', ports)).toBeNull();
  expect(ports.fetch).toHaveBeenCalledTimes(1);
});
it('does not steal navigation after the user selects a loaded chat or a draft', async () => {
  for (const draft of [true, false]) {
    const owner = createProjectConversationNavigation(), ports = fixture();
    let resolve!: (row: { id: string }) => void;
    ports.fetch.mockImplementation(() => new Promise(done => { resolve = done; }));
    const flight = owner.resolveMissing('old', ports);
    if (draft) owner.invalidate();
    else { ports.merge({ id: 'ordinary' }); owner.resolveMissing('ordinary', ports); }
    resolve({ id: 'old' }); await flight;
    expect(ports.navigate).not.toHaveBeenCalled();
    expect(ports.contains('old')).toBe(false);
  }
});
it('reports denied/removed metadata without manufacturing a local conversation', async () => {
  const owner = createProjectConversationNavigation(), ports = fixture();
  ports.fetch.mockRejectedValue(new Error('Access denied'));
  await owner.resolveMissing('private', ports);
  expect(ports.error).toHaveBeenCalledOnce();
  expect(ports.merge).not.toHaveBeenCalled(); expect(ports.navigate).not.toHaveBeenCalled();
});
