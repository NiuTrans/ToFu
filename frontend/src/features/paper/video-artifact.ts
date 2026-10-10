/** Paper video reader: one film, program-time chapters, narration and questions.
 * Entry points renderVideoArtifact/destroyVideoArtifact; generation and revisions
 * stay in video-runtime. Owns media, dialogs and listeners through one scope.
 */
import { featureRegistry } from '../../feature-registry';
import { t } from '../../i18n';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';
import { createButton, createDropdown, createDialog, createAlert } from '../../ui';
import { createMediaModelPicker, shortModelName } from './media-model-ui';
import { mediaNode as node, mediaArtifactUrl, downloadMediaArtifact, formatMediaTime } from './media-artifact';

interface Scene {
  scene_id: string; text?: string; on_screen?: string; has_video?: boolean;
  timeline_contract_version?: string; timeline_start_s?: number; timeline_end_s?: number;
}
interface VideoState {
  paperHash: string; status: string; lang: string; quality: string; artifactModel?: string;
  _doneTaskId?: string; taskId?: string; scenes?: Scene[]; scenesLoading?: boolean; scenesError?: string;
  regenSceneId?: string; revisionMessage?: string;
  quality_axis?: { degraded?: boolean; reason?: string };
  result?: { duration?: number; narrated?: boolean; audio?: { enabled?: boolean } };
}
interface Ports {
  _paperHash?: string; _activePaperId?: string; _paperFileName?: string;
  _videoGenerate?: (force: boolean) => unknown; _videoRegenScene?: (id: string) => unknown;
  _pvLoadScenes?: (refresh?: boolean) => unknown; _paperAskQuestion?: (question: string) => void;
  Api?: { motion?: { fileUrl: (id: string, kind?: string) => string; sceneFileUrl: (id: string, scene: string) => string } };
}
const ports = (): Ports => featureRegistry as unknown as Ports;
let mounted: { key: string; host: HTMLElement; scope: LifecycleScope; refresh: (state: VideoState, cacheBust: number) => void } | null = null;

/** Require the persisted normalized program clock; planned start/end is insufficient. */
export function videoChapterTimings(scenes: Scene[], duration: number): { start: number; end: number }[] | null {
  if (!scenes.length || !Number.isFinite(duration) || duration <= 0) return null;
  let cursor = 0;
  const result = [];
  for (const scene of scenes) {
    const start = scene.timeline_start_s, end = scene.timeline_end_s;
    if (scene.timeline_contract_version !== 'motion-timeline-v1'
        || typeof start !== 'number' || typeof end !== 'number' || !Number.isFinite(start) || !Number.isFinite(end)
        || Math.abs(start - cursor) > .001 || end <= start || end > duration + .25) return null;
    result.push({ start, end }); cursor = end;
  }
  return Math.abs(cursor - duration) <= .25 ? result : null;
}
export function destroyVideoArtifact(): void { mounted?.scope.destroy(); mounted = null; }

export function renderVideoArtifact(host: HTMLElement, state: VideoState, cacheBust = 0): void {
  const taskId = state._doneTaskId || state.taskId || '';
  const key = JSON.stringify([ports()._activePaperId, state.paperHash, taskId, state.lang, state.artifactModel, state.result, state.quality_axis]);
  if (mounted?.host === host && mounted.key === key && host.isConnected) { mounted.refresh(state, cacheBust); return; }
  destroyVideoArtifact();
  const scope = createLifecycleScope(), paperId = ports()._activePaperId, hash = state.paperHash;
  const current = (): boolean => !scope.signal.aborted && ports()._paperHash === hash && ports()._activePaperId === paperId;
  const api = ports().Api?.motion;
  const filmUrl = taskId && api ? mediaArtifactUrl(api.fileUrl(taskId)) : '';
  const main = node('article', 'paper-studio-reader paper-video-reader');
  const header = node('header', 'paper-studio-header');
  const title = ports()._paperFileName?.replace(/\.pdf$/i, '') || t('paper.videoHeroTitle');
  header.append(node('p', 'paper-studio-eyebrow', t('paper.studioVideoOverview')),
    node('h1', 'paper-studio-title', title), node('p', 'paper-studio-provenance', t('paper.studioReportSource')));
  const metadata = node('div', 'paper-studio-metadata');
  if (state.artifactModel) {
    const badge = node('span', '', shortModelName(state.artifactModel)); badge.id = 'videoModelBadge';
    badge.title = t('paper.mediaModelTitle'); metadata.append(badge);
  }
  if (Number(state.result?.duration) > 0) metadata.append(node('span', '', formatMediaTime(Number(state.result?.duration))));
  if (state.result?.narrated === false) metadata.append(node('span', '', t('paper.videoSilent')));
  header.append(metadata); main.append(header);
  if (state.quality_axis?.degraded) main.append(createAlert({ variant: 'warning', title: t('paper.videoDegraded'),
    body: state.quality_axis.reason || undefined }).el);
  const film = node('video', 'paper-studio-film'); film.id = 'paperVideoPlayer';
  film.controls = true; film.preload = 'metadata'; film.playsInline = true; film.setAttribute('aria-label', title);
  const feedback = node('p', 'paper-studio-feedback'); feedback.setAttribute('role', 'status');
  const playFilm = (): void => { void film.play().catch((error: unknown) => {
    // Native pause/reload interrupts a pending play promise without a media failure.
    if (current() && (error as { name?: string })?.name !== 'AbortError') feedback.textContent = t('paper.studioVideoFailed');
  }); };
  scope.listen(film, 'playing', () => { if (current()) feedback.textContent = ''; });
  scope.listen(film, 'canplay', () => { if (current()) feedback.textContent = ''; });
  if (filmUrl) {
    film.src = filmUrl;
    const player = node('section', 'paper-studio-player'); player.append(film);
    const controls = node('div', 'paper-studio-controls');
    const speed = createDropdown({ label: t('paper.studioSpeed', { speed: 1 }),
      items: [.75, 1, 1.25, 1.5, 2].map(rate => ({ id: String(rate), label: `${rate}×` })),
      onSelect: value => { if (current()) film.playbackRate = Number(value); },
    });
    scope.listen(film, 'ratechange', () => {
      const button = speed.el.querySelector('button');
      if (current() && button) button.textContent = t('paper.studioSpeed', { speed: film.playbackRate });
    });
    controls.append(speed.el, createButton({ label: t('paper.videoDownload'), onClick: () => {
      if (current()) downloadMediaArtifact(film.src, `paper-video-${hash.slice(0, 8)}.mp4`);
    } }));
    if (state.result?.narrated) controls.append(createButton({ label: t('paper.videoDownloadSrt'), onClick: () => {
      if (current() && api) downloadMediaArtifact(mediaArtifactUrl(api.fileUrl(taskId, 'srt')), `paper-video-${hash.slice(0, 8)}.srt`);
    } }));
    if (state.result?.audio?.enabled) controls.append(createButton({ label: t('paper.videoDownloadAudioAttribution'), onClick: () => {
      if (current() && api) downloadMediaArtifact(mediaArtifactUrl(api.fileUrl(taskId, 'audio-attribution')), 'paper-video-audio-attribution.txt');
    } }));
    player.append(controls, feedback); main.append(player);
    scope.add(() => speed.close(false));
    scope.add(() => { film.pause(); film.removeAttribute('src'); film.load(); });
    scope.listen(film, 'error', () => { if (current()) feedback.textContent = t('paper.studioVideoFailed'); });
  } else main.append(createAlert({ variant: 'warning', title: t('paper.studioVideoFailed') }).el);
  const chapterHost = node('section', 'paper-studio-chapters'); main.append(chapterHost);
  const transcript = node('section', 'paper-studio-transcript'); main.append(transcript);
  const actions = node('footer', 'paper-studio-controls');
  const picker = createMediaModelPicker('video'); scope.add(() => picker.close(false));
  actions.append(picker.el, createButton({ label: t('paper.podcastRegenerate'), onClick: () => { if (current()) void ports()._videoGenerate?.(true); } }));
  main.append(node('p', 'paper-studio-note', t('paper.studioNextModel')), actions);
  let sceneScope: LifecycleScope | null = null, sceneKey = '', revision = 0;
  let latestState = state;
  let timings: ReturnType<typeof videoChapterTimings> = null;
  let rows: HTMLElement[] = [], chapterRows: HTMLElement[] = [], buttons: HTMLButtonElement[] = [];
  const highlight = (): void => {
    if (!current()) return;
    const active = timings?.findIndex(row => film.currentTime >= row.start && film.currentTime < row.end) ?? -1;
    rows.forEach((row, index) => row.classList.toggle('is-active', index === active));
    chapterRows.forEach((row, index) => row.classList.toggle('is-active', index === active));
    buttons.forEach((button, index) => { if (index === active) button.setAttribute('aria-current', 'true'); else button.removeAttribute('aria-current'); });
  };
  scope.listen(film, 'timeupdate', highlight);
  scope.add(() => sceneScope?.destroy());
  const refresh = (next: VideoState, bust: number): void => {
    if (!current()) return;
    latestState = next;
    if (bust && bust !== revision && filmUrl) {
      revision = bust; const time = film.currentTime, rate = film.playbackRate, playing = !film.paused;
      film.pause();
      const url = new URL(filmUrl); url.searchParams.set('v', String(bust)); film.src = url.href;
      film.addEventListener('loadedmetadata', () => { if (current() && revision === bust) {
        film.currentTime = Math.min(time, Math.max(0, film.duration - .01)); film.playbackRate = rate;
        if (playing) playFilm();
      } }, { once: true, signal: scope.signal });
    }
    const declaredDuration = Number(next.result?.duration);
    const duration = declaredDuration > 0 ? declaredDuration : film.duration;
    const signature = JSON.stringify([duration, next.scenes, next.scenesLoading, next.scenesError, next.regenSceneId, next.revisionMessage]);
    if (signature === sceneKey) return;
    sceneKey = signature; sceneScope?.destroy(); sceneScope = createLifecycleScope();
    const children = sceneScope;
    let closePreview: (() => void) | null = null;
    children.add(() => closePreview?.());
    const scenes = next.scenes ?? [];
    timings = videoChapterTimings(scenes, duration); rows = []; chapterRows = []; buttons = [];
    chapterHost.replaceChildren(node('h2', 'paper-studio-section-title', t('paper.studioVideoChapters')));
    transcript.replaceChildren();
    if (next.revisionMessage) chapterHost.append(createAlert({ variant: 'info', title: next.revisionMessage }).el);
    if (next.scenesError) {
      chapterHost.append(createAlert({ variant: 'warning', title: t('paper.studioScenesFailed') }).el,
        createButton({ label: t('paper.podcastRecheck'), onClick: () => { if (current()) void ports()._pvLoadScenes?.(); } }));
    }
    if (!scenes.length) { chapterHost.append(node('p', 'paper-studio-note', t(next.scenesLoading ? 'paper.studioScenesLoading' : 'paper.studioScenesEmpty'))); return; }
    if (!timings) chapterHost.append(node('p', 'paper-studio-note', t('paper.studioVideoNoTiming')));
    const list = node('ol', 'paper-studio-chapter-list');
    transcript.append(node('h2', 'paper-studio-section-title', t('paper.studioNarration')));
    const seek = (index: number): void => {
      if (!current() || children.signal.aborted || !filmUrl || !timings?.[index]) return;
      try { film.currentTime = timings[index].start; playFilm(); }
      catch { feedback.textContent = t('paper.studioSeekFailed'); }
    };
    scenes.forEach((scene, index) => {
      const label = scene.on_screen?.trim() || t('paper.studioSceneTitle', { index: index + 1 });
      const card = node('li', 'paper-studio-chapter'); card.dataset.scene = scene.scene_id; chapterRows.push(card);
      card.append(node('h3', 'paper-studio-chapter-title', `${String(index + 1).padStart(2, '0')} · ${label}`));
      if (timings && filmUrl) {
        const button = createButton({ label: t('paper.studioWatchChapter', { time: formatMediaTime(timings[index].start) }), onClick: () => seek(index) });
        button.classList.add('paper-studio-chapter-play'); button.setAttribute('aria-label', `${label} · ${button.textContent}`);
        buttons.push(button); card.append(button);
      }
      card.append(node('p', 'paper-studio-excerpt', (scene.text || '').slice(0, 180) + ((scene.text?.length ?? 0) > 180 ? '…' : '')));
      const controls = node('div', 'paper-studio-controls');
      if (scene.text) controls.append(createButton({ label: t('paper.studioAsk'), variant: 'ghost', size: 'sm', onClick: () => {
        if (current() && !children.signal.aborted) ports()._paperAskQuestion?.(t('paper.studioAskPassage', { passage: scene.text!.slice(0, 600) }));
      } }));
      controls.append(createButton({ label: t(next.regenSceneId === scene.scene_id ? 'paper.videoRegening' : 'paper.studioScenePreview'),
        variant: 'ghost', size: 'sm', disabled: Boolean(next.regenSceneId), onClick: () => {
          if (!current() || children.signal.aborted || !api) return;
          closePreview?.();
          const body = node('div', 'paper-studio-scene-preview');
          const preview = node('video', 'paper-studio-film'); preview.controls = true; preview.preload = 'metadata'; preview.playsInline = true;
          const previewUrl = scene.has_video ? mediaArtifactUrl(api.sceneFileUrl(taskId, scene.scene_id)) : '';
          if (previewUrl) { const url = new URL(previewUrl); if (revision) url.searchParams.set('v', String(revision)); preview.src = url.href; body.append(preview); }
          else body.append(createAlert({ variant: 'info', title: t('paper.studioPreviewUnavailable') }).el);
          body.append(node('p', 'paper-studio-note', t('paper.studioPreviewHint')), node('p', 'paper-studio-excerpt', scene.text || ''));
          const wasPlaying = !film.paused; film.pause();
          const dialog = createDialog({ title: label, body, actions: [{ id: 'close', label: t('paper.studioClosePreview') },
            { id: 'regen', label: t('paper.videoRegen') }], onClose: action => {
              closePreview = null;
              preview.pause(); preview.removeAttribute('src'); preview.load(); dialog.el.remove();
              if (current() && !children.signal.aborted) {
                if (action === 'regen') void ports()._videoRegenScene?.(scene.scene_id);
                else if (wasPlaying) playFilm();
              }
            } });
          document.body.append(dialog.el);
          closePreview = () => { dialog.close(); preview.pause(); preview.removeAttribute('src'); preview.load(); dialog.el.remove(); closePreview = null; };
          void dialog.open();
        } }));
      card.append(controls); list.append(card);
      const row = node('article', 'paper-studio-segment'); row.dataset.scene = scene.scene_id;
      row.append(node('div', 'paper-studio-segment-section', label), node('p', '', scene.text || '')); rows.push(row); transcript.append(row);
    });
    chapterHost.append(list); highlight();
  };
  scope.listen(film, 'loadedmetadata', () => refresh(latestState, 0));
  mounted = { key, host, scope, refresh }; host.replaceChildren(main); scope.add(() => main.remove()); refresh(state, cacheBust);
}
Object.assign(featureRegistry, { _renderVideoArtifact: renderVideoArtifact, _destroyVideoArtifact: destroyVideoArtifact });
