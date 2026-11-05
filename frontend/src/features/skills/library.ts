/** Project choices for the skill library; selection never changes chat context. */
import { featureRegistry } from '../../feature-registry';
import type { AppDialogController } from '../../dialog-controller';
import type { I18nKey } from '../../i18n';

export interface SkillProject { path: string; exists?: boolean }
export interface SkillDestination { scope: 'global' | 'project'; projectPath: string }
type Bindings = {
  Api?: { project?: {
    recentList(): Promise<{ projects?: SkillProject[] } | null>;
    recentAdd(path: string): Promise<unknown>;
  } };
  t?: (key: I18nKey, values?: Record<string, unknown>) => string;
  showChoice?: AppDialogController['showChoice'];
  showPrompt?: AppDialogController['showPrompt'];
};
const bindings = () => featureRegistry as unknown as Bindings;
const t = (key: I18nKey, values?: Record<string, unknown>) => bindings().t?.(key, values) || key;

export async function skillProjects(): Promise<SkillProject[]> {
  const result = await bindings().Api?.project?.recentList();
  if (!Array.isArray(result?.projects)) throw new Error(t('skills.projectsUnavailable'));
  const seen = new Set<string>();
  return result.projects.filter((row) => {
    if (!row || typeof row.path !== 'string' || !row.path || seen.has(row.path)) return false;
    seen.add(row.path);
    return true;
  });
}

export async function addSkillProject(): Promise<boolean> {
  const path = await bindings().showPrompt?.(t('skills.addProjectHint'), {
    title: t('skills.addProject'), placeholder: '/path/to/project',
  });
  if (!path?.trim()) return false;
  const api = bindings().Api?.project;
  if (!api) throw new Error(t('skills.projectsUnavailable'));
  await api.recentAdd(path.trim());
  return true;
}

/**
 * Skills are owner-global (owner decision 2026-10-31): a package installed or
 * moved into one project was invisible to every other conversation, and a
 * same-id project copy received edits other conversations never saw. Every
 * install and move therefore targets the personal (global) library; the
 * server lets the global package win its id over project copies. The
 * signature is kept so callers still receive an explicit destination.
 */
export async function chooseSkillDestination(_name: string, _title: I18nKey = 'skills.installScopePromptTitle'): Promise<SkillDestination | null> {
  return { scope: 'global', projectPath: '' };
}
