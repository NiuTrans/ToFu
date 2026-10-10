/** Retryable, lazy CodeMirror 6 bundle shared by paper source editors.
 * Mirrors pdf-loader: the heavy dependency graph loads on first use only, a
 * failure clears the cached promise so a later open retries, and callers
 * keep their read-only fallback while the bundle is unavailable. */
import type * as cmState from '@codemirror/state';
import type * as cmView from '@codemirror/view';
import type * as cmLanguage from '@codemirror/language';
import type * as cmCommands from '@codemirror/commands';
import type * as cmSearch from '@codemirror/search';
import type * as cmAutocomplete from '@codemirror/autocomplete';
import type { stex as stexMode } from '@codemirror/legacy-modes/mode/stex';

export interface CodeMirrorBundle {
  EditorState: typeof cmState.EditorState;
  EditorView: typeof cmView.EditorView;
  keymap: typeof cmView.keymap;
  lineNumbers: typeof cmView.lineNumbers;
  highlightActiveLine: typeof cmView.highlightActiveLine;
  highlightActiveLineGutter: typeof cmView.highlightActiveLineGutter;
  drawSelection: typeof cmView.drawSelection;
  history: typeof cmCommands.history;
  defaultKeymap: typeof cmCommands.defaultKeymap;
  historyKeymap: typeof cmCommands.historyKeymap;
  indentWithTab: typeof cmCommands.indentWithTab;
  search: typeof cmSearch.search;
  searchKeymap: typeof cmSearch.searchKeymap;
  highlightSelectionMatches: typeof cmSearch.highlightSelectionMatches;
  autocompletion: typeof cmAutocomplete.autocompletion;
  closeBrackets: typeof cmAutocomplete.closeBrackets;
  StreamLanguage: typeof cmLanguage.StreamLanguage;
  syntaxHighlighting: typeof cmLanguage.syntaxHighlighting;
  defaultHighlightStyle: typeof cmLanguage.defaultHighlightStyle;
  bracketMatching: typeof cmLanguage.bracketMatching;
  stex: typeof stexMode;
}

let loading: Promise<CodeMirrorBundle> | undefined;

export function ensureCodeMirror(): Promise<CodeMirrorBundle> {
  if (loading) return loading;
  loading = Promise.all([
    import('@codemirror/state'),
    import('@codemirror/view'),
    import('@codemirror/language'),
    import('@codemirror/commands'),
    import('@codemirror/search'),
    import('@codemirror/autocomplete'),
    import('@codemirror/legacy-modes/mode/stex'),
  ]).then(([state, view, language, commands, search, autocomplete, legacy]) => ({
    EditorState: state.EditorState,
    EditorView: view.EditorView,
    keymap: view.keymap,
    lineNumbers: view.lineNumbers,
    highlightActiveLine: view.highlightActiveLine,
    highlightActiveLineGutter: view.highlightActiveLineGutter,
    drawSelection: view.drawSelection,
    history: commands.history,
    defaultKeymap: commands.defaultKeymap,
    historyKeymap: commands.historyKeymap,
    indentWithTab: commands.indentWithTab,
    search: search.search,
    searchKeymap: search.searchKeymap,
    highlightSelectionMatches: search.highlightSelectionMatches,
    autocompletion: autocomplete.autocompletion,
    closeBrackets: autocomplete.closeBrackets,
    StreamLanguage: language.StreamLanguage,
    syntaxHighlighting: language.syntaxHighlighting,
    defaultHighlightStyle: language.defaultHighlightStyle,
    bracketMatching: language.bracketMatching,
    stex: legacy.stex,
  })).catch((error: unknown) => {
    loading = undefined;
    throw error;
  });
  return loading;
}
