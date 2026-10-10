/** Podcast artifact reader: native playback, measured chapters and transcript.
 * Entry points renderPodcastArtifact/destroyPodcastArtifact. Reuses generation,
 * model selection and Q&A owners; owns one transient media/listener lifecycle.
 */
import { featureRegistry } from '../../feature-registry';
import { t } from '../../i18n';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';
import { createButton, createDropdown, createAlert } from '../../ui';
import { createMediaModelPicker, shortModelName } from './media-model-ui';
import { mediaNode as node, mediaArtifactUrl as mediaUrl, downloadMediaArtifact as download, formatMediaTime } from './media-artifact';
export { formatMediaTime } from './media-artifact';

interface Segment { text: string; section?: string; speaker?: string }
interface Timing { index: number; startSec: number; endSec: number }
interface Artifact {
  script?: { title?: string; segments?: Segment[] };
  meta?: Record<string, unknown>;
  audioUrl?: string; durationSec?: number; scriptOnly?: boolean;
}
interface PodcastState {
  paperHash: string; status: string; mode: string; lang: string;
  artifactModel?: string; data?: Artifact;
}
interface Ports {
  _paperHash?: string; _activePaperId?: string;
  _podcastGenerate?: (force: boolean) => unknown;
  _podcastExportScript?: () => void;
  _paperAskQuestion?: (question: string) => void;
  apiUrl?: (url: string) => string;
}
const ports = (): Ports => featureRegistry as unknown as Ports;
let mounted: { key: string; host: HTMLElement; scope: LifecycleScope; seek: (index: number) => void } | null = null;

const sections = {
  cold_open: 'paper.studioSectionOpening', roadmap: 'paper.studioSectionRoadmap',
  problem: 'paper.studioSectionProblem', method: 'paper.studioSectionMethod',
  experiments: 'paper.studioSectionExperiments', strengths: 'paper.studioSectionStrengths',
  weaknesses: 'paper.studioSectionWeaknesses', landscape: 'paper.studioSectionLandscape',
  figure_walkthrough: 'paper.studioSectionFigures', recap: 'paper.studioSectionRecap',
} as const;
const sectionLabel = (value = ''): string => {
  const key = sections[value as keyof typeof sections];
  return key ? t(key) : value;
};
/** Admit the complete measured receipt, never scale model estimates to audio. */
export function podcastTimings(data: Artifact): Timing[] | null {
  const receipt = data.meta?.timelineV1 as Record<string, unknown> | undefined;
  const segments = data.script?.segments ?? [];
  if (!receipt || receipt.format !== 'paper.podcast.timeline.v1' || receipt.basis !== 'measured_pcm'
      || !Array.isArray(receipt.segments) || receipt.segments.length !== segments.length
      || !Number.isFinite(receipt.durationSec) || Number(receipt.durationSec) <= 0) return null;
  let previousEnd = 0;
  const result: Timing[] = [];
  for (const [index, raw] of receipt.segments.entries()) {
    if (!raw || typeof raw !== 'object') return null;
    const row = raw as Timing;
    if (row.index !== index || !Number.isFinite(row.startSec) || !Number.isFinite(row.endSec)
        || row.startSec < previousEnd || row.endSec <= row.startSec || row.endSec > Number(receipt.durationSec) + .001) return null;
    previousEnd = row.endSec;
    result.push(row);
  }
  return result.length ? result : null;
}

export function destroyPodcastArtifact(): void { mounted?.scope.destroy(); mounted = null; }
export function seekPodcastArtifact(index: number): void { mounted?.seek(index); }

export function renderPodcastArtifact(host: HTMLElement, state: PodcastState): void {
  const data = state.data ?? {};
  const key = JSON.stringify([ports()._activePaperId, state.paperHash, state.mode, state.lang, state.artifactModel, state.status, data]);
  if (mounted?.host === host && mounted.key === key && host.isConnected) return;
  destroyPodcastArtifact();
  const scope = createLifecycleScope();
  const paperId = ports()._activePaperId;
  const paperHash = state.paperHash;
  const current = (): boolean => !scope.signal.aborted && ports()._paperHash === paperHash && ports()._activePaperId === paperId;
  const script = data.script ?? {};
  const segments = Array.isArray(script.segments) ? script.segments.filter(row => row && typeof row.text === 'string') : [];
  const timings = segments.length === script.segments?.length ? podcastTimings(data) : null;
  const audioUrl = state.status !== 'script_only' && !data.scriptOnly && data.audioUrl ? mediaUrl(data.audioUrl) : '';
  const main = node('article', 'paper-studio-reader');
  const header = node('header', 'paper-studio-header');
  header.append(node('p', 'paper-studio-eyebrow', t('paper.studioAudioOverview')),
    node('h1', 'paper-studio-title', script.title || t('paper.podcastHeroTitle')),
    node('p', 'paper-studio-provenance', t(String(data.meta?.source_kind ?? '').startsWith('report')
      ? 'paper.studioReportSource' : 'paper.studioPaperSource')));
  const metadata = node('div', 'paper-studio-metadata');
  metadata.append(node('span', '', `${t(state.mode === 'full' ? 'paper.podcastModeFullName' : 'paper.podcastModeShortName')} · ${state.lang}`));
  if (state.artifactModel) {
    const model = node('span', '', shortModelName(state.artifactModel)); model.id = 'podcastModelBadge';
    model.title = t('paper.mediaModelTitle'); metadata.append(model);
  }
  if (Number(data.durationSec) > 0) metadata.append(node('span', '',
    `${data.meta?.duration_estimated ? '≈ ' : ''}${formatMediaTime(Number(data.durationSec))}`));
  header.append(metadata); main.append(header);
  if (data.meta?.low_confidence) main.append(createAlert({ variant: 'warning', title: t('paper.podcastLowConfidence') }).el);
  if (!audioUrl) main.append(createAlert({ variant: 'info', title: t('paper.studioScriptOnly') }).el);

  const audio = node('audio', 'paper-studio-audio'); audio.id = 'podcastAudio';
  audio.controls = true; audio.preload = 'metadata'; audio.setAttribute('aria-label', script.title || t('paper.studioAudioOverview'));
  const player = node('section', 'paper-studio-player');
  const feedback = node('p', 'paper-studio-feedback'); feedback.setAttribute('role', 'status');
  const seek = (index: number): void => {
    if (!current() || !audioUrl || !timings?.[index]) return;
    try { audio.currentTime = timings[index].startSec; }
    catch { feedback.textContent = t('paper.studioSeekFailed'); }
  };
  mounted = { key, host, scope, seek };
  const rows: HTMLElement[] = [];
  const chapterButtons: { index: number; button?: HTMLButtonElement }[] = [];
  if (audioUrl) {
    audio.src = audioUrl; player.append(audio);
    const controls = node('div', 'paper-studio-controls');
    const speed = createDropdown({ label: t('paper.studioSpeed', { speed: 1 }),
      items: [.75, 1, 1.25, 1.5, 2].map(rate => ({ id: String(rate), label: `${rate}×` })),
      onSelect: value => {
        if (!current()) return;
        audio.playbackRate = Number(value);
        const button = speed.el.querySelector('button');
        if (button) button.textContent = t('paper.studioSpeed', { speed: value });
      },
    });
    let sleepTimer = 0;
    const sleep = createDropdown({ label: t('paper.podcastSleepTimer'),
      items: [0, 5, 10, 15, 30, 45].map(minutes => ({ id: String(minutes), label: minutes
        ? `${minutes} ${t('paper.podcastSleepMin')}` : t('paper.podcastSleepOff') })),
      onSelect: value => {
        if (!current()) return;
        window.clearTimeout(sleepTimer); sleepTimer = 0;
        const minutes = Number(value);
        feedback.textContent = minutes ? t('paper.studioSleepSet', { minutes }) : '';
        if (minutes) sleepTimer = window.setTimeout(() => {
          if (current()) { audio.pause(); feedback.textContent = t('paper.studioSleepFinished'); }
          sleepTimer = 0;
        }, minutes * 60_000);
      },
    });
    controls.append(speed.el, sleep.el, createButton({ label: t('paper.podcastDownloadAudio'),
      onClick: () => { if (current()) download(audioUrl, `paper-podcast-${state.mode}-${state.paperHash.slice(0, 8)}.${data.meta?.container === 'wav' ? 'wav' : 'mp3'}`); },
    }));
    player.append(controls, feedback); main.append(player);
    scope.add(() => { speed.close(false); sleep.close(false); window.clearTimeout(sleepTimer); });
    scope.add(() => { audio.pause(); audio.removeAttribute('src'); audio.load(); });
    scope.listen(audio, 'error', () => { feedback.textContent = t('paper.studioAudioFailed'); });
    scope.listen(audio, 'timeupdate', () => {
      if (!current() || !timings) return;
      const active = timings.findIndex(row => audio.currentTime >= row.startSec && audio.currentTime < row.endSec);
      rows.forEach((row, index) => row.classList.toggle('is-active', index === active));
      chapterButtons.forEach(({ index, button }, chapter) => {
        const end = chapterButtons[chapter + 1]?.index ?? segments.length;
        if (active >= index && active < end) button?.setAttribute('aria-current', 'true');
        else button?.removeAttribute('aria-current');
      });
    });
    if (!timings) main.append(node('p', 'paper-studio-note', t('paper.studioNoTiming')));
  }
  const chapters = node('section', 'paper-studio-chapters');
  chapters.append(node('h2', 'paper-studio-section-title', t('paper.studioChapters')));
  const chapterList = node('ol', 'paper-studio-chapter-list');
  segments.forEach((segment, index) => {
    if (index && segment.section === segments[index - 1].section) return;
    const item = node('li', 'paper-studio-chapter');
    const label = `${String(chapterButtons.length + 1).padStart(2, '0')} · ${sectionLabel(segment.section)}`;
    item.append(node('h3', 'paper-studio-chapter-title', label));
    if (timings && audioUrl) {
      const button = createButton({ label: t('paper.studioPlayChapter', { time: formatMediaTime(timings[index].startSec) }), onClick: () => {
        if (!current()) return;
        seek(index);
        void audio.play().catch(() => { if (current()) feedback.textContent = t('paper.studioAudioFailed'); });
      } });
      button.setAttribute('aria-label', `${label} · ${button.textContent}`);
      button.classList.add('paper-studio-chapter-play');
      chapterButtons.push({ index, button }); item.append(button);
    } else { chapterButtons.push({ index }); }
    item.append(node('p', 'paper-studio-excerpt', segment.text.slice(0, 180) + (segment.text.length > 180 ? '…' : '')));
    item.append(createButton({ label: t('paper.studioAsk'), variant: 'ghost', size: 'sm', onClick: () => {
      if (current()) ports()._paperAskQuestion?.(t('paper.studioAskPassage', { passage: segment.text.slice(0, 600) }));
    }})); chapterList.append(item);
  });
  chapters.append(chapterList); main.append(chapters);
  const transcript = node('section', 'paper-studio-transcript'); transcript.id = 'podcastTranscript';
  transcript.append(node('h2', 'paper-studio-section-title', t('paper.podcastTranscriptTitle')));
  segments.forEach((segment, index) => {
    const row = node('article', 'paper-studio-segment'); row.dataset.seg = String(index);
    const heading = node('div', 'paper-studio-segment-heading');
    heading.append(node('span', 'paper-studio-segment-section', sectionLabel(segment.section)));
    if (timings && audioUrl) heading.append(createButton({ label: formatMediaTime(timings[index].startSec), size: 'sm', variant: 'ghost', onClick: () => seek(index) }));
    row.append(heading, node('p', '', segment.text)); rows.push(row); transcript.append(row);
  }); main.append(transcript);
  const actions = node('footer', 'paper-studio-controls');
  const picker = createMediaModelPicker('podcast'); scope.add(() => picker.close(false));
  actions.append(picker.el, createButton({ label: t('paper.podcastExportScript'), onClick: () => { if (current()) ports()._podcastExportScript?.(); } }),
    createButton({ label: t('paper.podcastRegenerate'), onClick: () => { if (current()) void ports()._podcastGenerate?.(true); } }));
  main.append(node('p', 'paper-studio-note', t('paper.studioNextModel')), actions); host.replaceChildren(main);
  scope.add(() => main.remove());
}

Object.assign(featureRegistry, { _renderPodcastArtifact: renderPodcastArtifact,
  _destroyPodcastArtifact: destroyPodcastArtifact, _seekPodcastArtifact: seekPodcastArtifact });
