/**
 * Conversation role-avatar assets.
 *
 * Responsibility: build the four cache-busted role image snippets used by
 * the typed conversation renderer. Entry point: `createRoleAvatarIcons`.
 * Dependency: the application base path supplied by composition.
 */

export const ROLE_AVATAR_ICON_VERSION = '20260402b';

export interface RoleAvatarIcons {
  plannerHtml: string;
  criticHtml: string;
  workerHtml: string;
  userHtml: string;
}

function imageHtml(iconBase: string, file: string, alt: string): string {
  return `<img src="${iconBase}/${file}?v=${ROLE_AVATAR_ICON_VERSION}" `
    + `alt="${alt}" style="width:100%;height:100%;display:block">`;
}

/** Create one immutable role-avatar set for the active application base. */
export function createRoleAvatarIcons(basePath: unknown): RoleAvatarIcons {
  const prefix = typeof basePath === 'string' ? basePath : '';
  const iconBase = `${prefix}/static/icons`;
  return Object.freeze({
    plannerHtml: imageHtml(iconBase, 'tofu-planner.svg', 'Planner'),
    criticHtml: imageHtml(iconBase, 'tofu-critic.svg', 'Critic'),
    workerHtml: imageHtml(iconBase, 'tofu-worker.svg', 'Worker'),
    userHtml: imageHtml(iconBase, 'onigiri.svg', 'You'),
  });
}
