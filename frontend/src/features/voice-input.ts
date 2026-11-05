/** Composer voice entry point: boot/refresh, action, projection and disposal.
 * Shared audio capabilities own reads; lazy voice/recording owns media lifetime.
 * The retained voice section only binds these commands until boot migrates.
 */
import {featureRegistry} from '../feature-registry';
import {narrowModelRoutingChangedFrame} from '../core/frame-identity';
import {createLifecycleScope, type LifecycleScope} from '../lifecycle';
import type {I18nKey} from '../i18n';
import {audioCapabilities, needsWaveEncoding} from './voice/audio-capabilities';
import type {VoiceRecording, VoiceState, VoiceFailure} from './voice/recording';

interface VoiceRuntime {
  Api: {audio: {transcribe(data: FormData, options: {signal: AbortSignal}): Promise<unknown>}};
  t?: (key: I18nKey) => string;
  showToast?: (text: string, kind: string) => void;
  _frameIsOurs?: (owner: unknown) => boolean;
  pushSubscribe?: (channel: string, task: string, callback: (frame: unknown) => void) => void;
  pushUnsubscribe?: (channel: string, task: string, callback: (frame: unknown) => void) => void;
  pushOnReconnect?: (callback: () => void) => (() => void);
  __VOICE_DEBUG?: boolean;
}

const runtime = () => featureRegistry as unknown as VoiceRuntime;
let lifecycle: LifecycleScope | null = null;
let recording: VoiceRecording | null = null;
let opening: Promise<void> | null = null;
let state: VoiceState = 'idle';
let destroyed = false;

function translated(key: I18nKey, fallback: string): string {
  const value = runtime().t?.(key);
  return value && value !== key ? value : fallback;
}

function browserCanRecord(): boolean {
  return typeof window.navigator.mediaDevices?.getUserMedia === 'function'
    && typeof window.MediaRecorder === 'function';
}

function render(): void {
  const button = document.getElementById('micBtn') as HTMLButtonElement | null;
  if (!button) return;
  const busy = state === 'requesting' || state === 'transcribing';
  const active = state !== 'idle' || opening !== null;
  button.style.display = !destroyed && (active || browserCanRecord() && audioCapabilities.current?.available) ? '' : 'none';
  button.classList.toggle('recording', state === 'recording');
  button.classList.toggle('transcribing', state === 'transcribing');
  button.disabled = busy || opening !== null;
  button.setAttribute('aria-busy', String(busy || opening !== null));
  button.title = state === 'recording' ? translated('voice.recording', 'Recording · click to stop')
    : state === 'transcribing' ? translated('voice.transcribing', 'Transcribing…')
    : translated('voice.tooltip', 'Voice input');
}

function failureReason(value: unknown): string {
  const source = value as {body?: {error?: unknown}; error?: unknown; message?: unknown} | null;
  let reason = source?.body?.error ?? source?.error ?? source?.message;
  if (reason && typeof reason === 'object') {
    const detail = reason as {message?: unknown; detail?: unknown; error?: unknown};
    reason = detail.message || detail.detail || detail.error;
  }
  return typeof reason === 'string' ? reason.trim() : '';
}

function showFailure(kind: VoiceFailure, error?: unknown): void {
  const messages = {
    permission: ['voice.micDenied', 'Microphone access denied'],
    recorder: ['voice.noMic', 'Recording is not supported in this browser'],
    transcription: ['voice.failed', 'Transcription failed'],
    empty: ['voice.empty', 'No speech detected'],
  } as const;
  const [key, fallback] = messages[kind];
  const message = translated(key, fallback);
  const reason = kind === 'transcription' ? failureReason(error) : '';
  if (error) console.warn('[Voice] ' + kind + ' failed:', error);
  try { runtime().showToast?.(reason ? `${message}: ${reason}` : message, kind === 'empty' ? 'info' : 'error'); }
  catch (failure) { console.warn('[Voice] toast failed:', failure); }
}

/** Dictation follows the existing shared composer; it never submits a turn. */
function injectText(text: string): void {
  const input = document.getElementById('userInput') as HTMLTextAreaElement | null;
  if (!input) return;
  const start = input.selectionStart ?? input.value.length, end = input.selectionEnd ?? start;
  const before = input.value.slice(0, start), after = input.value.slice(end);
  const space = before && !/\s$/.test(before) && !/^\s/.test(text) ? ' ' : '';
  input.value = before + space + text + after;
  input.selectionStart = input.selectionEnd = (before + space + text).length;
  input.focus();
  input.dispatchEvent(new window.Event('input', {bubbles: true}));
}

const audioExtensions: Record<string, string> = {
  'audio/wav': 'wav', 'audio/webm': 'webm', 'audio/ogg': 'ogg',
  'audio/mpeg': 'mp3', 'audio/mp4': 'm4a', 'audio/flac': 'flac',
};

function transcribe(blob: Blob, signal: AbortSignal): Promise<unknown> {
  const data = new window.FormData();
  const extension = audioExtensions[blob.type.split(';')[0].trim()] || 'webm';
  data.append('file', blob, `recording.${extension}`);
  return runtime().Api.audio.transcribe(data, {signal});
}

function timingEnabled(): boolean {
  try { return !!runtime().__VOICE_DEBUG || window.localStorage.getItem('tofu.voice.debug') === '1'; }
  catch { return false; }
}

async function loadRecording(): Promise<void> {
  const owner = await import('./voice/recording');
  if (destroyed || !browserCanRecord() || !audioCapabilities.current?.available) return;
  recording = new owner.VoiceRecording({
    acquireStream: () => window.navigator.mediaDevices.getUserMedia({audio: true}),
    createRecorder: stream => new window.MediaRecorder(stream),
    createEncoder: () => owner.createWaveEncoder({
      createContext: () => {
        const Context = window.AudioContext || (window as unknown as {webkitAudioContext?: typeof AudioContext}).webkitAudioContext;
        return Context ? new Context() : null;
      },
      blob: bytes => new window.Blob([bytes], {type: 'audio/wav'}),
      debug: timingEnabled, now: () => window.performance.now(), log: console,
    }),
    needsEncoding: () => needsWaveEncoding(audioCapabilities.current),
    blob: (chunks, type) => new window.Blob(chunks, {type}),
    transcribe, transcript: injectText, failure: showFailure,
    stateChanged: next => { state = next; render(); },
    cleanupError: error => console.warn('[Voice] media cleanup failed:', error),
  });
  recording.toggle();
}

export async function toggleVoiceInput(): Promise<void> {
  if (destroyed || opening) return;
  if (recording && state !== 'idle') { recording.toggle(); return; }
  if (!browserCanRecord()) { showFailure('recorder'); return; }
  if (!audioCapabilities.current?.available) return;
  if (recording) { recording.toggle(); return; }
  opening = loadRecording();
  render();
  try { await opening; }
  catch (error) { if (!destroyed) showFailure('recorder', error); }
  finally { opening = null; render(); }
}

function subscribeRouting(scope: LifecycleScope): void {
  const bridge = runtime();
  const onFrame = (frame: unknown) => {
    const routing = narrowModelRoutingChangedFrame(frame);
    if (routing && runtime()._frameIsOurs?.(routing.userId) === true) {
      void audioCapabilities.routingChanged(routing.rev);
    }
  };
  if (bridge.pushSubscribe && bridge.pushUnsubscribe) {
    bridge.pushSubscribe('notify', '*', onFrame);
    scope.add(() => bridge.pushUnsubscribe?.('notify', '*', onFrame));
  }
  const unsubscribe = bridge.pushOnReconnect?.(() => { void audioCapabilities.invalidate(); });
  if (unsubscribe) scope.add(unsubscribe);
}

export async function initVoiceInput(): Promise<void> {
  if (destroyed || !document.getElementById('micBtn')) return;
  if (!lifecycle) {
    lifecycle = createLifecycleScope(window);
    lifecycle.add(audioCapabilities.subscribe(value => {
      if (!value?.available) recording?.retirePermission();
      render();
    }));
    subscribeRouting(lifecycle);
  }
  if (browserCanRecord()) await audioCapabilities.refresh();
  render();
}

export function destroyVoiceInput(): void {
  if (destroyed) return;
  destroyed = true;
  lifecycle?.destroy(); lifecycle = null;
  recording?.destroy(); recording = null;
  audioCapabilities.destroy();
  render();
}
