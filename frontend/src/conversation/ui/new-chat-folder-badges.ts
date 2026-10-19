/**
 * Present new-chat folder context in the retained shell's two badge slots.
 * Entry: paintNewChatFolderBadges; dependencies: injected Document and metadata.
 * Folder metadata is untrusted, including historical colors from old servers.
 * No HTML parser, arbitrary CSS, persistence, or folder/defaults authority.
 */
export function paintNewChatFolderBadges(
  document: Document,
  folder: { name: string; color?: unknown } | null,
): void {
  if (!folder) return;
  const candidate = typeof folder.color === 'string' ? folder.color : '';
  // Match the backend's strict hex3/hex6 grammar; equality excludes trailing newlines.
  const color = candidate.match(/^#[0-9a-f]{3}(?:[0-9a-f]{3})?$/i)?.[0] === candidate
    ? candidate : '';

  const topbar = document.getElementById('topbarTitle');
  if (topbar) {
    const badge = document.createElement('span');
    badge.className = 'topbar-folder-badge';
    badge.style.color = color || 'var(--text-tertiary)';
    badge.textContent = `● ${folder.name}`;
    topbar.append(document.createTextNode(' '), badge);
  }

  const welcomeTitle = document.querySelector('#welcome .tofu-brand');
  if (welcomeTitle) {
    const badge = document.createElement('div');
    badge.className = 'welcome-folder-badge';
    const dot = document.createElement('span');
    dot.className = 'welcome-folder-dot';
    dot.style.color = color || 'var(--text-tertiary)';
    dot.textContent = '●';
    badge.append(dot, document.createTextNode(` ${folder.name}`));
    welcomeTitle.after(badge);
  }
}
