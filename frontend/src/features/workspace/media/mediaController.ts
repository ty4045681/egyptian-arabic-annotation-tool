/**
 * Media controller (work package F, workspace/media).
 *
 * Owns exactly one HTMLAudioElement per task outside React. timeupdate and
 * animation frames only update the playback cursor/time display through a
 * dedicated subscription — text rows never re-render on playback ticks.
 * Switching tasks or dispose() pauses, detaches every listener and cancels
 * the frame loop. Late waveform responses are fenced by task generation so
 * they can never overwrite a newer task.
 */

export interface MediaSegment {
  id: number | string;
  start: number;
  end: number;
}

export interface MediaCursor {
  taskId: string | null;
  currentTime: number;
  duration: number;
  playing: boolean;
  /** Index of the segment currently being played (-1 when idle). */
  playingIndex: number;
  /** Index of the segment under the playhead (highlight linkage). */
  selectedIndex: number;
  stopAt: number | null;
}

export type CursorListener = (cursor: MediaCursor) => void;

export interface AudioElementLike {
  src: string;
  currentTime: number;
  duration: number;
  paused: boolean;
  dataset: { stop?: string };
  play: () => Promise<void>;
  pause: () => void;
  load: () => void;
  addEventListener: (type: string, listener: () => void) => void;
  removeEventListener: (type: string, listener: () => void) => void;
}

export interface MediaDeps {
  createAudio: () => AudioElementLike;
  requestFrame: (callback: () => void) => number;
  cancelFrame: (handle: number) => void;
}

function defaultDeps(): MediaDeps {
  const doc = globalThis.document;
  return {
    createAudio: () => doc.createElement("audio") as unknown as AudioElementLike,
    requestFrame: (callback) => globalThis.requestAnimationFrame(callback),
    cancelFrame: (handle) => globalThis.cancelAnimationFrame(handle),
  };
}

export class MediaController {
  private deps: MediaDeps;
  private audio: AudioElementLike | null = null;
  private taskId: string | null = null;
  private segments: MediaSegment[] = [];
  private duration = 0;
  private playingIndex = -1;
  private selectedIndex = -1;
  private stopAt: number | null = null;
  private frameHandle: number | null = null;
  private listeners = new Set<CursorListener>();
  private waveformGeneration = 0;
  private waveform: number[] = [];
  private waveformTaskId: string | null = null;
  private detach: Array<() => void> = [];
  private boundElement: AudioElementLike | null = null;
  private disposed = false;

  constructor(deps?: Partial<MediaDeps>) {
    this.deps = { ...defaultDeps(), ...deps };
  }

  /**
   * Adopt a page-rendered <audio> element so the task always has exactly one
   * audio instance shared by the transport bar and every segment row. Must
   * be called before attach(); child effects run before parent effects, so a
   * transport-level ref binding always wins over the controller fallback.
   */
  bindElement(element: AudioElementLike): void {
    if (this.audio || this.disposed) return;
    this.boundElement = element;
  }

  get attachedTaskId(): string | null {
    return this.taskId;
  }

  get audioInstance(): AudioElementLike | null {
    return this.audio;
  }

  get loadedWaveformTaskId(): string | null {
    return this.waveformTaskId;
  }

  get loadedWaveform(): number[] {
    return [...this.waveform];
  }

  segmentList(): MediaSegment[] {
    return [...this.segments];
  }

  /** Attach a task; reuses the audio element only when the task is unchanged. */
  attach(taskId: string, audioUrl: string, segments: MediaSegment[], duration: number): void {
    if (this.disposed) return;
    if (this.taskId === taskId && this.audio) {
      this.segments = [...segments];
      this.duration = duration;
      this.emit();
      return;
    }
    this.teardownAudio();
    this.taskId = taskId;
    this.segments = [...segments];
    this.duration = duration;
    this.playingIndex = -1;
    this.selectedIndex = -1;
    this.stopAt = null;
    const audio = this.boundElement ?? this.deps.createAudio();
    this.boundElement = null;
    this.audio = audio;
    audio.src = audioUrl;
    const onTime = () => this.handleTimeUpdate();
    const onPlay = () => this.emit();
    const onPause = () => {
      this.stopFrames();
      this.emit();
    };
    const onEnded = () => this.handleEnded();
    audio.addEventListener("timeupdate", onTime);
    audio.addEventListener("play", onPlay);
    audio.addEventListener("pause", onPause);
    audio.addEventListener("ended", onEnded);
    this.detach = [
      () => audio.removeEventListener("timeupdate", onTime),
      () => audio.removeEventListener("play", onPlay),
      () => audio.removeEventListener("pause", onPause),
      () => audio.removeEventListener("ended", onEnded),
    ];
    this.emit();
  }

  /**
   * Refresh playback bounds after segment edits without touching the audio
   * instance. Time edits flow editor store -> this method -> cursor emit,
   * so play-segment stop points, highlight linkage and the waveform always
   * follow the edited times instead of the load-time snapshot.
   */
  updateSegments(segments: MediaSegment[], duration: number): void {
    if (this.disposed) return;
    this.segments = segments.map((seg) => ({ ...seg }));
    this.duration = duration;
    if (this.selectedIndex >= segments.length) {
      this.selectedIndex = segments.length - 1;
    }
    if (this.playingIndex >= segments.length || this.playingIndex < 0) {
      this.playingIndex = -1;
      this.stopAt = null;
    } else if (this.stopAt !== null) {
      const current = segments[this.playingIndex];
      this.stopAt = current ? current.end : null;
    }
    this.emit();
  }

  /**
   * Waveform payloads arrive async; only the latest task generation wins so
   * a slow response for task A can never overwrite task B's waveform.
   */
  beginWaveformLoad(taskId: string): number {
    this.waveformGeneration += 1;
    void taskId;
    return this.waveformGeneration;
  }

  commitWaveform(taskId: string, generation: number, waveform: number[]): boolean {
    if (generation !== this.waveformGeneration) return false;
    if (this.taskId !== null && taskId !== this.taskId) return false;
    this.waveform = [...waveform];
    this.waveformTaskId = taskId;
    return true;
  }

  playSegment(index: number): void {
    const audio = this.audio;
    const seg = this.segments[index];
    if (!audio || !seg) return;
    if (this.playingIndex >= 0 && this.playingIndex !== index) {
      // Leaving another segment: its progress resets via cursor emission.
    }
    this.playingIndex = index;
    this.selectedIndex = index;
    this.stopAt = seg.end;
    audio.currentTime = this.clampTime(seg.start);
    void audio.play()?.catch?.(() => undefined);
    this.startFrames();
    this.emit();
  }

  resume(index: number): void {
    const audio = this.audio;
    if (!audio || !this.segments[index]) return;
    this.playingIndex = index;
    this.stopAt = this.segments[index]?.end ?? null;
    void audio.play()?.catch?.(() => undefined);
    this.startFrames();
    this.emit();
  }

  pause(): void {
    this.audio?.pause();
    this.stopFrames();
    this.emit();
  }

  stopSegmentPlayback(): void {
    this.stopFrames();
    if (this.audio && !this.audio.paused) this.audio.pause();
    this.playingIndex = -1;
    this.stopAt = null;
    if (this.audio) this.audio.dataset.stop = "";
    this.emit();
  }

  seekTo(time: number): void {
    if (!this.audio) return;
    this.audio.currentTime = this.clampTime(time);
    this.emit();
  }

  select(index: number): void {
    if (this.selectedIndex === index) return;
    this.selectedIndex = index;
    this.emit();
  }

  subscribe(listener: CursorListener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  getSnapshot(): MediaCursor {
    return {
      taskId: this.taskId,
      currentTime: this.audio?.currentTime ?? 0,
      duration: this.duration,
      playing: this.audio ? !this.audio.paused : false,
      playingIndex: this.playingIndex,
      selectedIndex: this.selectedIndex >= 0 ? this.selectedIndex : this.indexAt(this.audio?.currentTime ?? 0),
      stopAt: this.stopAt,
    };
  }

  listenerCount(): number {
    return this.detach.length;
  }

  dispose(): void {
    this.disposed = true;
    this.stopFrames();
    this.teardownAudio();
    this.listeners.clear();
    this.taskId = null;
    this.segments = [];
    this.playingIndex = -1;
    this.selectedIndex = -1;
    this.stopAt = null;
  }

  private clampTime(time: number): number {
    if (!Number.isFinite(time)) return 0;
    if (this.duration > 0) return Math.min(Math.max(0, time), this.duration);
    return Math.max(0, time);
  }

  private indexAt(time: number): number {
    return this.segments.findIndex((seg) => time >= seg.start && time <= seg.end);
  }

  private handleTimeUpdate(): void {
    const audio = this.audio;
    if (!audio) return;
    const now = audio.currentTime;
    if (this.playingIndex >= 0) {
      const seg = this.segments[this.playingIndex];
      const stop = this.stopAt ?? seg?.end ?? null;
      if (seg && (now >= seg.end - 0.02 || (stop !== null && now >= stop))) {
        audio.pause();
        this.stopAt = null;
        this.playingIndex = -1;
        this.emit();
        return;
      }
    }
    const found = this.indexAt(now);
    if (found >= 0 && found !== this.selectedIndex) this.selectedIndex = found;
    this.emit();
  }

  private handleEnded(): void {
    this.playingIndex = -1;
    this.stopAt = null;
    this.stopFrames();
    this.emit();
  }

  private startFrames(): void {
    if (this.frameHandle !== null) return;
    const tick = (): void => {
      if (this.disposed || !this.audio || this.audio.paused) {
        this.frameHandle = null;
        return;
      }
      this.emit();
      this.frameHandle = this.deps.requestFrame(tick);
    };
    this.frameHandle = this.deps.requestFrame(tick);
  }

  private stopFrames(): void {
    if (this.frameHandle !== null) {
      this.deps.cancelFrame(this.frameHandle);
      this.frameHandle = null;
    }
  }

  private teardownAudio(): void {
    this.stopFrames();
    if (this.audio) {
      try {
        this.audio.pause();
      } catch {
        // Tearing down a broken element must not throw.
      }
      for (const remove of this.detach) {
        try {
          remove();
        } catch {
          // ignore
        }
      }
    }
    this.detach = [];
    this.audio = null;
  }

  private emit(): void {
    const snapshot = this.getSnapshot();
    this.listeners.forEach((listener) => {
      try {
        listener(snapshot);
      } catch {
        // Listener errors must never corrupt media state.
      }
    });
  }
}

/** Decode the base64 int16 waveform payload served with the assignment. */
export function decodeWaveform(encoded: string | null | undefined): number[] {
  if (!encoded) return [];
  try {
    const binary = globalThis.atob(encoded);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return Array.from(new Int16Array(bytes.buffer), (v) => v / 32767);
  } catch {
    return [];
  }
}
