/** Recording command owner, loaded on the first microphone action. Each session
 * owns its permission request, recorder, chunks, encoder and transcription signal.
 * UI, browser construction and API calls enter through explicit ports.
 */
import type {WaveEncoder} from './wave-encoder';
export {createWaveEncoder} from './wave-encoder';

export type VoiceState = 'idle' | 'requesting' | 'recording' | 'transcribing';
export type VoiceFailure = 'permission' | 'recorder' | 'transcription' | 'empty';
export interface RecordingPorts {
  acquireStream(): Promise<MediaStream>;
  createRecorder(stream: MediaStream): MediaRecorder;
  createEncoder(): WaveEncoder;
  needsEncoding(): boolean;
  blob(chunks: Blob[], type: string): Blob;
  transcribe(blob: Blob, signal: AbortSignal): Promise<unknown>;
  stateChanged(state: VoiceState): void;
  transcript(text: string): void;
  failure(kind: VoiceFailure, error?: unknown): void;
  cleanupError(error: unknown): void;
}

interface Session {
  abort: AbortController;
  chunks: Blob[];
  stream?: MediaStream;
  recorder?: MediaRecorder;
  encoder?: WaveEncoder;
  processing?: boolean;
}

export class VoiceRecording {
  private session: Session | null = null;
  private state: VoiceState = 'idle';
  private disposed = false;
  constructor(private readonly ports: RecordingPorts) {}

  toggle(): void {
    if (this.disposed) return;
    if (this.state === 'recording') this.stop();
    else if (!this.session) void this.start();
  }

  private apply(state: VoiceState): void {
    this.state = state;
    this.ports.stateChanged(state);
  }

  private releaseStream(stream?: MediaStream): void {
    if (!stream) return;
    for (const track of stream.getTracks()) {
      try { track.stop(); } catch (error) { this.ports.cleanupError(error); }
    }
  }

  private release(session: Session): void {
    const recorder = session.recorder;
    session.recorder = undefined;
    if (recorder) {
      recorder.ondataavailable = null; recorder.onstop = null; recorder.onerror = null;
      try { if (recorder.state !== 'inactive') recorder.stop(); }
      catch (error) { this.ports.cleanupError(error); }
    }
    this.releaseStream(session.stream);
    session.stream = undefined;
    session.encoder?.destroy();
    session.encoder = undefined;
    session.chunks = [];
  }

  private finish(session: Session): void {
    if (this.session !== session) return;
    this.session = null;
    this.release(session);
    this.apply('idle');
  }

  private async start(): Promise<void> {
    const session: Session = {abort: new AbortController(), chunks: []};
    this.session = session;
    this.apply('requesting');
    let stream: MediaStream;
    try { stream = await this.ports.acquireStream(); }
    catch (error) {
      if (this.session === session) { this.ports.failure('permission', error); this.finish(session); }
      return;
    }
    if (this.session !== session) { this.releaseStream(stream); return; }
    session.stream = stream;
    try {
      if (this.ports.needsEncoding()) {
        session.encoder = this.ports.createEncoder(); session.encoder.prewarm();
      }
      const recorder = this.ports.createRecorder(stream);
      session.recorder = recorder;
      recorder.ondataavailable = event => {
        if (this.session === session && event.data?.size) session.chunks.push(event.data);
      };
      recorder.onstop = () => { void this.transcribe(session); };
      recorder.onerror = event => {
        if (this.session !== session) return;
        this.ports.failure('recorder', event); this.finish(session);
      };
      recorder.start();
      if (this.session === session && this.state === 'requesting') this.apply('recording');
    } catch (error) {
      this.ports.failure('recorder', error); this.finish(session);
    }
  }

  private stop(): void {
    const session = this.session;
    if (!session) return;
    // Lock repeated stop clicks while the recorder's final data/stop events arrive.
    this.apply('transcribing');
    try {
      if (session.recorder?.state !== 'inactive') session.recorder?.stop();
      else void this.transcribe(session);
    } catch (error) {
      this.ports.failure('recorder', error); this.finish(session);
    }
  }

  private async transcribe(session: Session): Promise<void> {
    if (this.session !== session || session.processing) return;
    session.processing = true;
    this.releaseStream(session.stream);
    session.stream = undefined;
    const blob = this.ports.blob(session.chunks, session.recorder?.mimeType || 'audio/webm');
    session.chunks = [];
    if (!blob.size) { this.finish(session); return; }
    this.apply('transcribing');
    try {
      let encoded = blob;
      if (this.ports.needsEncoding()) {
        session.encoder ??= this.ports.createEncoder();
        encoded = await session.encoder.encode(blob);
      }
      if (this.session !== session) return;
      const response = await this.ports.transcribe(encoded, session.abort.signal) as {ok?: boolean; text?: unknown} | null;
      if (this.session !== session) return;
      if (!response?.ok) this.ports.failure('transcription', response);
      else if (typeof response.text !== 'string' || !response.text.trim()) this.ports.failure('empty');
      else this.ports.transcript(response.text.trim());
    } catch (error) {
      if (this.session === session) this.ports.failure('transcription', error);
    } finally { this.finish(session); }
  }

  destroy(): void {
    if (this.disposed) return;
    this.disposed = true;
    const session = this.session;
    this.session = null;
    if (session) { session.abort.abort(); this.release(session); }
    this.apply('idle');
  }

  retirePermission(): void {
    // Configuration refresh cannot interrupt accepted recording/transcription.
    // Permission not yet granted owns no audio; retire it and release a late grant.
    if (this.state === 'requesting' && this.session) this.finish(this.session);
  }
}
