/**
 * Pure presentation policy for projected tool rounds.
 *
 * Responsibility: classify tool families, select stable icon keys, normalize
 * status text, and distinguish image/swarm modes without reading DOM, globals,
 * or mutable browser state. Entry points are the exported predicates plus
 * `toolRoundIconKey`, `imageGenerationMode`, program-row normalization, and
 * `plainToolStatus`.
 * Dependencies: none; callers provide Conversation Sync projection values.
 */

type UnknownRecord = Readonly<Record<string, unknown>>;

const EMPTY_RECORD: UnknownRecord = Object.freeze({});

function record(value: unknown): UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : EMPTY_RECORD;
}

function toolName(round: unknown): string {
  const value = record(round).toolName;
  return typeof value === 'string' ? value : '';
}

function stringField(round: unknown, field: string): string {
  const value = record(round)[field];
  return typeof value === 'string' ? value : '';
}

export const PROJECT_TOOL_PRESENTATION_NAMES: readonly string[] = Object.freeze([
  'read_files',
  'inspect_image',
  'list_dir',
  'grep_search',
  'find_files',
  'write_file',
  'edit_file',
  'apply_diff',
  'apply_diffs',
  'insert_content',
  'insert_contents',
  'create_project',
  'run_command',
]);

export const BROWSER_TOOL_PRESENTATION_NAMES: readonly string[] = Object.freeze([
  'browser_list_tabs',
  'browser_read_page',
  'browser_research_page',
  'browser_devtools',
  'browser_execute_js',
  'browser_screenshot',
  'browser_click',
  'browser_type',
  'browser_press_key',
  'browser_navigate',
  'browser_close_tab',
  'browser_get_cookies',
  'browser_get_history',
  'browser_menu_click',
  'browser_fill_form',
  'browser_preview_page',
]);

export const CONVERSATION_METADATA_TOOL_NAMES: readonly string[] = Object.freeze([
  'project_board_read',
  'project_board_post',
  'project_board_claim',
  'project_board_complete',
  'project_board_block',
  'project_charter_read',
  'project_charter_propose',
  'project_charter_commit',
  'list_conversations',
  'get_conversation',
  'project_peer_status',
  'project_feed_read',
  'project_message',
  'project_intervene',
  'integration_checkpoint',
  'integration_submit',
  'integration_status',
  'project_claim_path',
  'project_release_path',
  'project_commit',
]);

export const MOTION_TOOL_PRESENTATION_NAMES: readonly string[] = Object.freeze([
  'motion_video_env_check',
  'motion_video_storyboard_check',
  'motion_video_check',
  'motion_video_render',
  'motion_video_probe',
  'motion_video_concat',
  'motion_video_narrate',
  'motion_video_mux',
  'produce_video',
  'produce_report',
  'produce_research',
  'produce_slides',
  'edit_slides',
]);

const PROJECT_TOOLS = new Set(PROJECT_TOOL_PRESENTATION_NAMES);
const BROWSER_TOOLS = new Set(BROWSER_TOOL_PRESENTATION_NAMES);
const CONVERSATION_METADATA_TOOLS = new Set(CONVERSATION_METADATA_TOOL_NAMES);
const MOTION_TOOLS = new Set(MOTION_TOOL_PRESENTATION_NAMES);

export type ToolRoundDisplay = Readonly<{
  iconName: string;
  label: string;
  color: string;
}>;

function frozenDisplay(
  label: string,
  color: string,
  iconName = '',
): ToolRoundDisplay {
  return Object.freeze({ iconName, label, color });
}

const EXPLICIT_TOOL_ROUND_DISPLAYS: Readonly<Record<string, ToolRoundDisplay>> =
  Object.freeze({
    web_search: frozenDisplay('Searching', '#60a5fa'),
    search_knowledge: frozenDisplay('Knowledge', '#22c55e'),
    search_tools: frozenDisplay('Tool Search', '#818cf8'),
    fetch_url: frozenDisplay('Fetching', '#34d399'),
    spawn_agents: frozenDisplay('Swarm', '#f59e0b'),
    await_agents: frozenDisplay('Awaiting Swarm', '#f59e0b'),
    get_agent_result: frozenDisplay('Agent Result', '#f59e0b'),
    create_memory: frozenDisplay('Memory', '#a78bfa'),
    schedule_task: frozenDisplay('Schedule', '#fb923c'),
    timer_create: frozenDisplay('Timer Watcher', '#a855f7', 'timer'),
    timer_manage: frozenDisplay('Timer', '#a855f7', 'timer'),
    bash_exec: frozenDisplay('Running', '#f472b6', 'play'),
    desktop_click: frozenDisplay('Desktop', '#94a3b8'),
    desktop_type: frozenDisplay('Desktop', '#94a3b8', 'keyboard'),
    desktop_screenshot: frozenDisplay('Desktop', '#94a3b8'),
    generate_image: frozenDisplay('Image', '#e879f9'),
    ask_human: frozenDisplay('Guidance', '#a5b4fc'),
    produce_video: frozenDisplay('Video', '#f472b6'),
    produce_report: frozenDisplay('Report', '#f472b6'),
    produce_research: frozenDisplay('Research', '#f472b6'),
    produce_slides: frozenDisplay('Slides', '#38bdf8'),
    edit_slides: frozenDisplay('Edit Slide', '#38bdf8'),
    todo_write: frozenDisplay('Checklist', '#34d399'),
  });

export const EXPLICIT_TOOL_ROUND_DISPLAY_NAMES: readonly string[] =
  Object.freeze(Object.keys(EXPLICIT_TOOL_ROUND_DISPLAYS));

const PROJECT_DISPLAY = frozenDisplay('Project', '#60a5fa');
const BROWSER_DISPLAY = frozenDisplay('Browser', '#38bdf8');
const MOTION_DISPLAY = frozenDisplay('Video', '#f472b6');

export function explicitToolRoundDisplay(
  value: unknown,
): ToolRoundDisplay | null {
  if (typeof value !== 'string') return null;
  return EXPLICIT_TOOL_ROUND_DISPLAYS[value] || null;
}

/** Resolve the label/color policy consumed by retained HTML adapters. */
export function toolRoundDisplay(round: unknown): ToolRoundDisplay {
  const name = toolName(round);
  const explicit = explicitToolRoundDisplay(name);
  if (explicit) return explicit;
  if (isFetchToolRound(round)) {
    return EXPLICIT_TOOL_ROUND_DISPLAYS.fetch_url;
  }
  if (isSearchToolRound(round)) {
    return EXPLICIT_TOOL_ROUND_DISPLAYS.web_search;
  }
  if (isSwarmToolRound(round)) {
    return EXPLICIT_TOOL_ROUND_DISPLAYS.spawn_agents;
  }
  if (isProjectToolRound(round)) return PROJECT_DISPLAY;
  if (isBrowserToolRound(round)) return BROWSER_DISPLAY;
  if (isMotionToolRound(round)) return MOTION_DISPLAY;
  const readableName = (name || 'tool').replace(/_/g, ' ');
  return {
    iconName: '',
    label: readableName.charAt(0).toUpperCase() + readableName.slice(1),
    color: '#94a3b8',
  };
}

const PROJECT_ICON_KEYS: Readonly<Record<string, string>> = Object.freeze({
  read_files: 'file',
  inspect_image: 'zoomimg',
  list_dir: 'folder',
  grep_search: 'search',
  find_files: 'find',
  write_file: 'write',
  edit_file: 'diff',
  apply_diff: 'diff',
  apply_diffs: 'diff',
  insert_content: 'insert',
  insert_contents: 'insert',
  create_project: 'folder',
  run_command: 'terminal',
});

const BROWSER_ICON_KEYS: Readonly<Record<string, string>> = Object.freeze({
  browser_list_tabs: 'tabs',
  browser_read_page: 'read',
  browser_research_page: 'research',
  browser_devtools: 'devtools',
  browser_execute_js: 'js',
  browser_screenshot: 'screenshot',
  browser_get_cookies: 'cookie',
  browser_get_history: 'history',
  browser_close_tab: 'close',
  browser_navigate: 'navigate',
  browser_preview_page: 'screenshot',
  browser_click: 'click',
  browser_type: 'type',
  browser_press_key: 'key',
  browser_menu_click: 'menu',
  browser_fill_form: 'form',
});

const MOTION_ICON_KEYS: Readonly<Record<string, string>> = Object.freeze({
  motion_video_env_check: 'mv_env',
  motion_video_storyboard_check: 'mv_storyboard',
  motion_video_check: 'mv_check',
  motion_video_render: 'mv_render',
  motion_video_probe: 'mv_probe',
  motion_video_concat: 'mv_concat',
  motion_video_narrate: 'mv_narrate',
  motion_video_mux: 'mv_mux',
  produce_video: 'mv_render',
  produce_report: 'mv_report',
  produce_research: 'mv_research',
  produce_slides: 'mv_report',
  edit_slides: 'mv_report',
});

export function plainToolStatus(value: unknown, fallback?: unknown): string {
  return String(value || fallback || '').replace(/^[✓✗⊘⏸]\s*/u, '').trim();
}

export function isFetchToolRound(round: unknown): boolean {
  const name = toolName(round);
  const query = stringField(round, 'query');
  return name === 'fetch_url'
    || query.startsWith('📄')
    || query.startsWith('🌐')
    || query.startsWith('📑');
}

export function isSearchToolRound(round: unknown): boolean {
  const name = toolName(round);
  return name === 'web_search' || name === 'search_knowledge';
}

export function isToolSearchRound(round: unknown): boolean {
  return toolName(round) === 'search_tools';
}

export function isCodeExecutionToolRound(round: unknown): boolean {
  return toolName(round) === 'code_exec';
}

export function isProjectToolRound(round: unknown): boolean {
  return PROJECT_TOOLS.has(toolName(round));
}

export function isBrowserToolRound(round: unknown): boolean {
  return BROWSER_TOOLS.has(toolName(round));
}

export function isImageGenerationToolRound(round: unknown): boolean {
  return toolName(round) === 'generate_image';
}

export function isConversationMetadataToolRound(round: unknown): boolean {
  return CONVERSATION_METADATA_TOOLS.has(toolName(round));
}

export function isMotionToolRound(round: unknown): boolean {
  return MOTION_TOOLS.has(toolName(round));
}

export function isSwarmToolRound(round: unknown): boolean {
  const value = record(round);
  if (!value._swarm) return false;
  const liveAgents = value._swarmAgents;
  const results = value.results;
  const snapshotAgents = record(value._swarmSnapshot).agents;
  return (Array.isArray(liveAgents) && liveAgents.length > 0)
    || (Array.isArray(results) && results.length > 0)
    || (Array.isArray(snapshotAgents) && snapshotAgents.length > 0);
}

export function imageGenerationMode(round: unknown): 'edit' | 'generate' {
  const results = record(round).results;
  const first = Array.isArray(results) ? record(results[0]) : EMPTY_RECORD;
  return first.imageMode === 'edit' ? 'edit' : 'generate';
}

/** Identify display-only ToolScript/OpenAI program parent rows. */
export function isProgramToolRound(round: unknown): boolean {
  return Boolean(record(round)._programSynthetic);
}

/** Normalize a program result for its retained HTML `<pre>` adapter. */
export function programDisplayValue(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'string') {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** Select the stable glyph key consumed by the retained SVG adapter. */
export function toolRoundIconKey(round: unknown): string {
  const name = toolName(round);
  if (isProjectToolRound(round)) return PROJECT_ICON_KEYS[name] || 'folder';
  if (isBrowserToolRound(round)) return BROWSER_ICON_KEYS[name] || 'tabs';
  if (isMotionToolRound(round)) return MOTION_ICON_KEYS[name] || 'mv_render';
  if (isToolSearchRound(round)) return 'search_tools';
  if (name === 'search_knowledge') return 'search_knowledge';
  if (isSearchToolRound(round)) return 'web_search';
  if (isFetchToolRound(round)) return 'fetch';
  if (isCodeExecutionToolRound(round)) return 'code_exec';
  return name || 'generic';
}
