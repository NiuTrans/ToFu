/** Lazy browser audio encoder. Owns one prewarmed AudioContext per recording;
 * encode preserves the existing 16 kHz mono PCM16 conversion and raw fallback.
 * Depends only on browser media APIs, never on application or routing state.
 */
export interface DecodedAudio {
  sampleRate: number;
  numberOfChannels: number;
  length: number;
  getChannelData(channel: number): Float32Array;
}

export function audioBufferToWave(audio: DecodedAudio, targetRate: number): ArrayBuffer {
  const channels = audio.numberOfChannels || 1;
  const mono = new Float32Array(audio.length);
  for (let channel = 0; channel < channels; channel++) {
    const data = audio.getChannelData(channel);
    for (let index = 0; index < audio.length; index++) mono[index] += data[index] / channels;
  }
  const rate = targetRate || audio.sampleRate;
  const length = Math.max(1, Math.round(audio.length * rate / audio.sampleRate));
  const samples = new Float32Array(length);
  if (length === audio.length) samples.set(mono);
  else {
    const ratio = audio.length / length;
    for (let index = 0; index < length; index++) {
      const position = index * ratio, left = Math.floor(position), fraction = position - left;
      const a = mono[left] || 0, b = mono[left + 1] ?? a;
      samples[index] = a + (b - a) * fraction;
    }
  }
  const buffer = new ArrayBuffer(44 + length * 2);
  const view = new DataView(buffer);
  const write = (offset: number, text: string) => {
    for (let index = 0; index < text.length; index++) view.setUint8(offset + index, text.charCodeAt(index));
  };
  write(0, 'RIFF'); view.setUint32(4, 36 + length * 2, true);
  write(8, 'WAVE'); write(12, 'fmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  write(36, 'data'); view.setUint32(40, length * 2, true);
  for (let index = 0; index < length; index++) {
    const sample = Math.max(-1, Math.min(1, samples[index]));
    view.setInt16(44 + index * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }
  return buffer;
}

export interface WaveEncoder {
  prewarm(): void;
  encode(blob: Blob): Promise<Blob>;
  destroy(): void;
}

export function createWaveEncoder(ports: {
  createContext(): AudioContext | null;
  blob(data: ArrayBuffer): Blob;
  debug(): boolean;
  now(): number;
  log: Pick<Console, 'warn' | 'info'>;
}): WaveEncoder {
  let context: AudioContext | null = null;
  let destroyed = false;
  const warn = (error: unknown) => ports.log.warn('[Voice] AudioContext unavailable:', error);
  function prewarm(): void {
    if (destroyed || context) return;
    try {
      context = ports.createContext();
      if (context?.state === 'suspended') void Promise.resolve(context.resume()).catch(warn);
    } catch (error) { warn(error); }
  }
  return {
    prewarm,
    async encode(blob) {
      if (destroyed || typeof blob.arrayBuffer !== 'function') return blob;
      const shared = !!context, start = ports.now();
      prewarm();
      const decoder = context;
      if (!decoder) return blob;
      try {
        const acquired = ports.now(), bytes = await blob.arrayBuffer(), buffered = ports.now();
        if (destroyed) return blob;
        const audio = await decoder.decodeAudioData(bytes), decoded = ports.now();
        if (destroyed) return blob;
        const wave = audioBufferToWave(audio, 16000), encoded = ports.now();
        if (ports.debug()) ports.log.info(
          '[Voice][timing] size=%dB shared=%s acquireCtx=%.1fms arrayBuffer=%.1fms decodeAudioData=%.1fms resample=%.1fms total=%.1fms',
          blob.size, shared, acquired - start, buffered - acquired, decoded - buffered, encoded - decoded, encoded - start);
        return ports.blob(wave);
      } catch (error) {
        if (!destroyed) ports.log.warn('[Voice] WAV encode failed, sending original:', error);
        return blob;
      }
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      const owned = context;
      context = null;
      try { if (owned) void Promise.resolve(owned.close()).catch(warn); }
      catch (error) { warn(error); }
    },
  };
}
