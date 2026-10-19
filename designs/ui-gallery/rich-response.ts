/** Design-only scaffold entry. The component is the actual production renderer. */
import { renderRichResponse, disposeRichResponse } from '../../frontend/src/rich-response/renderer';
import { createButton, createDialog } from '../../frontend/src/ui';
import fixture from './rich-response.fixture.json';

import timelineFixture from './live-photo-timeline.fixture.json';

import componentsFixture from './rich-components.fixture.json';
import { renderTextBlock, disposeRichMarkdown } from '../../frontend/src/conversation/ui/renderers/text-blocks';
import { createMarkdownRenderer } from '../../frontend/src/markdown';
import DOMPurify from 'dompurify';
import { marked } from 'marked';

import { createChoiceInteractionPorts } from '../../frontend/src/conversation/application/choice-interaction';
import { conversationSyncApi } from '../../frontend/src/api/conversation-sync.generated';

const host = document.getElementById('rich-preview')!;
const supplied = document.getElementById('rich-response-payload')?.textContent;
const payload = supplied ? JSON.parse(supplied) : fixture;
const toolbar = document.getElementById('gallery-actions')!;
for (const theme of ['tofu', 'light', 'dark']) {
  const control = createButton({label:theme,size:'sm'},document);
  control.addEventListener('click',()=>{document.documentElement.dataset.theme=theme;document.body.dataset.theme=theme;});
  toolbar.append(control);
}
renderRichResponse(host, payload, {conversationId:'visual-owned',openArtifact(id) {
  const dialog = createDialog({title:'Recorded evidence',body:`Artifact reference: ${id}. This gallery demonstrates a read-only intent; production delegates it to the artifact panel.`});
  document.body.append(dialog.el);
  void dialog.open().then(()=>dialog.el.remove());
}});
const inline = document.createElement('section');
inline.id = 'rich-inline-preview';
host.after(inline);
const markdown = createMarkdownRenderer({
  getEnvironment: () => ({ document, parser: { parse: source => marked.parse(source, { async: false }) as string },
    sanitizer: DOMPurify, highlighter: null, mathRenderer: null, basePath: '', projectActive: false }),
  maximumCacheEntries: 8, ensureMath: () => {},
});
renderTextBlock(inline, {
  kind: 'text', blockId: 'inline-timeline', terminal: true, deliverable: true,
  markdown: 'Inline assistant timing diagram\n\n```tofu-ui\n' + JSON.stringify(timelineFixture) + '\n```',
} as Parameters<typeof renderTextBlock>[1], { renderSafeMarkdownHtml: (source, options) => markdown.render(source, options) });
const components = document.createElement('section');
components.id = 'rich-components-preview';
inline.after(components);
renderTextBlock(components, {
  kind: 'text', blockId: 'inline-components', terminal: true, deliverable: true,
  markdown: 'A supplied source for this design [[cite:sources/guide]].\n\n```tofu-ui\n' + JSON.stringify(componentsFixture) + '\n```',
} as Parameters<typeof renderTextBlock>[1], { renderSafeMarkdownHtml: (source, options) => markdown.render(source, options) });
const choiceScope = document.getElementById('choice-scope')?.textContent;
const choiceHost = document.createElement('section'); choiceHost.id = 'rich-choices-preview'; components.after(choiceHost);
if (choiceScope) {
  const scope = JSON.parse(choiceScope) as { conversationId: string; turnId: string; mode?:string };
  const choices = createChoiceInteractionPorts({ ...scope,
    readTurn: async (conv, turn, signal) => (await conversationSyncApi.turn(conv, turn, { signal })).turn,
    createAttempt: async (conv, turn, body, signal) => {
      try { return await conversationSyncApi.createAttempt(conv, turn, body as Parameters<typeof conversationSyncApi.createAttempt>[2], { signal }); }
      catch (error) { console.error('Choice command validation:', error); throw error; }
    },
    commandId: () => crypto.randomUUID(),
  });
  renderRichResponse(choiceHost, { version: 1, title: 'Live owned question', blocks: [
    { type: scope.mode === 'batch' ? 'form_prompt' : 'choice_prompt', id: 'choose-scope', title: 'Choose scope', interactionId: 'hg_browser' },
  ] }, { conversationId: scope.conversationId, choices });
}
window.addEventListener('pagehide', () => {
  const body = inline.querySelector<HTMLElement>('.md-content');
  if (body) disposeRichMarkdown(body);

  const componentBody = components.querySelector<HTMLElement>('.md-content');
  if (componentBody) disposeRichMarkdown(componentBody);
  markdown.dispose();

  disposeRichResponse(choiceHost);
}, { once: true });
window.addEventListener('pagehide',()=>disposeRichResponse(host),{once:true});
