import { useEffect, useRef, useState } from "react";
import type { AudioElementLike, MediaController, MediaCursor } from "./mediaController";
import styles from "./AudioTransport.module.css";

function formatClock(totalSeconds: number): string {
  const safe = Number.isFinite(totalSeconds) ? Math.max(0, totalSeconds) : 0;
  const minutes = Math.floor(safe / 60);
  const seconds = Math.floor(safe % 60);
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export function AudioTransport({ media }: { media: MediaController }): React.JSX.Element {
  const [cursor, setCursor] = useState<MediaCursor>(() => media.getSnapshot());
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => media.subscribe(setCursor), [media]);

  useEffect(() => {
    if (audioRef.current) {
      media.bindElement(audioRef.current as unknown as AudioElementLike);
    }
  }, [media]);

  const toggle = (): void => {
    if (cursor.playing) {
      media.pause();
      return;
    }
    const index = cursor.playingIndex >= 0 ? cursor.playingIndex : Math.max(0, cursor.selectedIndex);
    media.resume(index);
  };

  return (
    <div aria-label="Audio playback" data-testid="audio-transport" className={styles.bar}>
      <button
        type="button"
        onClick={toggle}
        aria-pressed={cursor.playing}
        aria-label={cursor.playing ? "Pause audio" : "Play audio"}
        data-testid="transport-toggle"
        data-audio-play=""
        className={styles.toggle}
      >
        {cursor.playing ? "⏸ Pause" : "▶ Play"}
      </button>
      <span dir="ltr" data-testid="transport-time" className={styles.time}>
        {formatClock(cursor.currentTime)} / {formatClock(cursor.duration)}
      </span>
      <audio ref={audioRef} data-testid="workspace-audio" aria-label="Task audio" className={styles.player} />
    </div>
  );
}
