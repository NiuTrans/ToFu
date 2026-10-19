/** Demand-only artifact previews and scene selection. Polls never replace a playing video or unchanged thumbnail. */
import { button, node, setText } from './dom';
import { phrase, ready, terminal, resourcePath, type WorkAction, type WorkState, type WorkUnit, type WorkUnitPorts } from './contracts';
function reconcile(parent: HTMLElement, children: HTMLElement[]): void {
  children.forEach((child, index) => { if (parent.children[index] !== child) parent.insertBefore(child, parent.children[index] || null); });
  while (parent.children.length > children.length) parent.lastElementChild?.remove();
}
export function createMediaView(taskId: string, ports: WorkUnitPorts, act: (action: WorkAction) => void) {
  const doc = ports.document, t = ports.text;
  const root = node(doc, 'div', 'ptool-wu-media');
  const gallery = node(doc, 'div', 'ptool-wu-gallery');
  const more = node(doc, 'details', 'ptool-wu-more-pages');
  const moreLabel = node(doc, 'summary', '');
  const extra = node(doc, 'div', 'ptool-wu-gallery ptool-wu-gallery-all'); more.append(moreLabel, extra);
  const previewNote = node(doc, 'p', 'ptool-wu-caption');
  const film = node(doc, 'div', 'ptool-wu-film'); film.hidden = true;
  const cover = node(doc, 'div', 'ptool-wu-video-cover');
  const filmTitle = node(doc, 'p', 'ptool-wu-video-title');
  const play = button(doc, t('workUnit.play', 'Play video'), () => openVideo()); cover.append(filmTitle, play);
  const player = doc.createElement('video'); player.controls = true; player.playsInline = true; player.preload = 'none'; player.hidden = true;
  const mediaError = node(doc, 'p', 'ptool-wu-error'); mediaError.setAttribute('role', 'status');
  player.addEventListener('error', () => setText(mediaError, t('workUnit.mediaError', 'Preview could not be loaded. You can retry or download the file.')));
  film.append(cover, player, mediaError);
  const scenes = node(doc, 'details', 'ptool-wu-scenes'); scenes.hidden = true;
  const scenesLabel = node(doc, 'summary', '');
  const sceneRows = node(doc, 'div', 'ptool-wu-scene-list');
  const sceneMessage = node(doc, 'p', 'ptool-wu-caption'); sceneMessage.setAttribute('role', 'status');
  const retryScenes = button(doc, t('workUnit.retry', 'Retry'), () => act({ name: 'scenes' })); retryScenes.hidden = true;
  scenes.append(scenesLabel, sceneRows, sceneMessage, retryScenes);
  let current: WorkUnit | null = null, sceneFingerprint = '', filePath = '', disposed = false;
  const thumbs = new Map<number, { root: HTMLElement; image?: HTMLImageElement; label: HTMLElement }>();
  scenes.addEventListener('toggle', () => { if (scenes.open && current && ready(current)) act({ name: 'scenes' }); });
  root.append(gallery, more, previewNote, film, scenes);
  function openVideo(url = filePath ? ports.resolveUrl(filePath) : ''): void {
    if (disposed || !current || !ready(current) || !url) return;
    film.hidden = false; cover.hidden = true; player.hidden = false;
    if (player.getAttribute('src') !== url) { if (!player.paused) player.pause(); player.src = url; }
    else player.load(); // Explicitly reopening a revised film revalidates the served bytes.
    setText(mediaError, ''); player.focus();
    void player.play().catch(() => { if (!disposed) setText(mediaError, t('workUnit.mediaError', 'Preview could not be loaded. You can retry or download the file.')); });
  }
  function releaseVideo(): void {
    if (!player.hasAttribute('src')) return;
    if (!player.paused) player.pause();
    player.removeAttribute('src'); player.load(); player.hidden = true; cover.hidden = false;
  }
  function pageThumb(n: number, rendered: boolean, unit: WorkUnit): HTMLElement {
    const path = resourcePath(unit.pages?.href);
    let thumb = thumbs.get(n);
    if (thumb && Boolean(thumb.image) !== rendered) { thumb.root.remove(); thumbs.delete(n); thumb = undefined; }
    const caption = phrase(t, 'wu.pageN', 'Page {n}', { n });
    if (!thumb) {
      const root = rendered
        ? button(doc, '', () => ports.openSlides(taskId, n, Math.max(n, current?.pages?.total || n)))
        : node(doc, 'div', '');
      root.classList.add('ptool-wu-thumb'); root.dataset.wuPage = String(n);
      const label = node(doc, 'span', 'ptool-wu-thumb-n', String(n));
      let image: HTMLImageElement | undefined;
      if (rendered) { image = doc.createElement('img'); image.loading = 'lazy'; image.decoding = 'async'; image.alt = caption;
        image.addEventListener('error', () => root.classList.add('ptool-wu-thumb-error'));
        image.addEventListener('load', () => root.classList.remove('ptool-wu-thumb-error')); root.append(image); }
      else root.classList.add('ptool-wu-thumb-pending');
      root.append(label); thumb = { root, image, label }; thumbs.set(n, thumb);
    }
    thumb.root.setAttribute('aria-label', caption); thumb.root.title = caption;
    if (thumb.image && path.includes('{n}')) {
      const url = ports.resolveUrl(path.replace('{n}', String(n)) + '?v=' + (Number(unit.pages?.rev?.[String(n)]) || 1));
      if (thumb.image.getAttribute('src') !== url) thumb.image.src = url;
      thumb.image.alt = caption;
    }
    return thumb.root;
  }
  return { root, open(): void {
    if (!current) return;
    if (current.kind === 'slides-deck' && current.pages?.rendered.length) ports.openSlides(taskId, current.pages.rendered[0], current.pages.total);
    else openVideo();
  }, update(unit: WorkUnit, state: WorkState): void {
    current = unit;
    const pageInfo = unit.pages, path = resourcePath(pageInfo?.href);
    const rendered = new Set((Array.isArray(pageInfo?.rendered) ? pageInfo.rendered : []).filter(n => Number.isInteger(n) && n >= 1 && n <= 99));
    const total = Math.min(99, Math.max(0, Number(pageInfo?.total) || 0));
    const pageNumbers = path.includes('{n}') && rendered.size
      ? [...new Set([...rendered, ...(!terminal(unit) ? Array.from({ length: total }, (_, i) => i + 1) : [])])].sort((a, b) => a - b) : [];
    const nodes = pageNumbers.map(n => pageThumb(n, rendered.has(n), unit));
    reconcile(gallery, nodes.slice(0, 3)); reconcile(extra, nodes.slice(3));
    for (const [n, thumb] of thumbs) if (!pageNumbers.includes(n)) { thumb.root.remove(); thumbs.delete(n); }
    gallery.hidden = !nodes.length; more.hidden = nodes.length <= 3;
    setText(moreLabel, phrase(t, 'workUnit.allPages', 'View all {n} pages', { n: pageNumbers.length }));
    previewNote.hidden = !nodes.length;
    setText(previewNote, ready(unit) ? t('workUnit.selectPage', 'Select a page to preview or revise it.') : terminal(unit)
      ? t('workUnit.retainedPreview', 'Existing previews are retained. The deliverable is incomplete.')
      : t('workUnit.partialPreview', 'Work in progress. These previews are not the final deliverable.'));
    filePath = ready(unit) && unit.kind === 'motion-video'
      ? resourcePath(unit.artifacts?.find(a => a.label === 'mp4')?.href) : '';
    film.hidden = !filePath; if (!filePath) releaseVideo();
    setText(filmTitle, unit.title || t('workUnit.video', 'Video')); setText(play, t('workUnit.play', 'Play video'));
    scenes.hidden = !ready(unit) || unit.kind !== 'motion-video';
    setText(scenesLabel, t('workUnit.scenes', 'View and revise scenes'));
    retryScenes.hidden = !state.scenesError; retryScenes.disabled = Boolean(state.scenesLoading);
    setText(retryScenes, t('workUnit.retry', 'Retry'));
    setText(sceneMessage, state.scenesLoading ? t('workUnit.loadingScenes', 'Loading scenes…') : state.scenesError
      ? t('workUnit.scenesError', 'Scenes could not be loaded. Try again.') : state.scenes?.length === 0 ? t('workUnit.noScenes', 'No scene previews are available.') : '');
    const next = JSON.stringify([state.scenes, t('wu.sceneEdit', 'Edit')]);
    if (next !== sceneFingerprint) {
      sceneFingerprint = next; sceneRows.replaceChildren();
      for (const [i, scene] of (state.scenes || []).slice(0, 24).entries()) {
        const row = node(doc, 'div', 'ptool-wu-scene'); row.dataset.wuScene = scene.scene_id;
        const title = phrase(t, 'workUnit.sceneN', 'Scene {n}', { n: i + 1 });
        row.append(node(doc, 'span', 'ptool-wu-scene-title', title));
        if (Number.isFinite(scene.content_duration_s)) row.append(node(doc, 'span', 'ptool-wu-caption', `${scene.content_duration_s}s`));
        if (scene.has_video) row.append(button(doc, t('workUnit.preview', 'Preview'), () => openVideo(ports.sceneFileUrl(taskId, scene.scene_id))));
        const edit = button(doc, t('wu.sceneEdit', 'Edit'), () => ports.prefill(phrase(t, 'workUnit.editScenePrompt', 'Revise scene {scene} in video task {id}: ', { id: taskId, scene: scene.scene_id })));
        const regen = button(doc, t('wu.sceneRegen', 'Re-render'), () => act({ name: 'regenerate', sceneId: scene.scene_id })); regen.dataset.wuRegenerate = scene.scene_id;
        const message = node(doc, 'p', 'ptool-wu-action-message'); message.setAttribute('role', 'status');
        row.append(edit, regen, message); sceneRows.append(row);
      }
    }
    for (const row of sceneRows.querySelectorAll<HTMLElement>('[data-wu-scene]')) {
      const operation = state.operations.get('regenerate:' + row.dataset.wuScene);
      row.querySelector<HTMLButtonElement>('[data-wu-regenerate]')!.disabled = Boolean(operation?.busy || operation?.accepted);
      setText(row.querySelector('.ptool-wu-action-message')!, operation?.error || (operation?.accepted ? t('workUnit.revisionAccepted', 'Revision accepted; progress is shown below.') : ''));
    }
  }, dispose(): void { disposed = true; releaseVideo(); thumbs.clear(); } };
}
