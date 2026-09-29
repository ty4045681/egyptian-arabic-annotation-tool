import { useEffect, useRef, useState } from "react";
import { decodeWaveform, type MediaController } from "./mediaController";
import {
  WAVEFORM_COLLAPSED_PX,
  WAVEFORM_EXPANDED_PX,
  type WaveformSegment,
} from "./waveformConstants";
import styles from "./WaveformPanel.module.css";

function drawWaveform(
  canvas: HTMLCanvasElement,
  waveform: number[],
  segments: Array<{ start: number; end: number; id: number | string }>,
  duration: number,
  selectedIndex: number,
  currentTime: number,
  height: number,
): void {
  const dpr = Math.max(1, globalThis.devicePixelRatio || 1);
  const width = Math.max(300, canvas.clientWidth || 600);
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.scale(dpr, dpr);
  ctx.fillStyle = "#f2f4f7";
  ctx.fillRect(0, 0, width, height);
  if (!waveform.length || !(duration > 0)) return;
  const peak = waveform.reduce((m, v) => Math.max(m, Math.abs(v)), 1e-6);
  const mid = height / 2;
  segments.forEach((seg, index) => {
    const x1 = (seg.start / duration) * width;
    const x2 = (seg.end / duration) * width;
    ctx.fillStyle = index === selectedIndex ? "rgba(53, 83, 232, 0.18)" : "rgba(53, 83, 232, 0.06)";
    ctx.fillRect(x1, 0, Math.max(1, x2 - x1), height);
  });
  ctx.beginPath();
  const step = Math.max(1, Math.floor(waveform.length / width));
  for (let i = 0; i < waveform.length; i += step) {
    let sum = 0;
    let count = 0;
    for (let j = 0; j < step && i + j < waveform.length; j++) {
      const sample = waveform[i + j];
      if (sample !== undefined) {
        sum += sample;
        count++;
      }
    }
    const v = (sum / Math.max(1, count) / peak) * mid * 0.8;
    const x = (i / waveform.length) * width;
    if (i === 0) ctx.moveTo(x, mid - v);
    else ctx.lineTo(x, mid - v);
  }
  for (let i = waveform.length - 1; i >= 0; i -= step) {
    let sum = 0;
    let count = 0;
    for (let j = 0; j < step && i - j >= 0; j++) {
      const sample = waveform[i - j];
      if (sample !== undefined) {
        sum += sample;
        count++;
      }
    }
    const v = (sum / Math.max(1, count) / peak) * mid * 0.8;
    const x = (i / waveform.length) * width;
    ctx.lineTo(x, mid + v);
  }
  ctx.closePath();
  ctx.fillStyle = "rgba(53, 83, 232, 0.45)";
  ctx.fill();
  if (currentTime > 0) {
    const x = (currentTime / duration) * width;
    ctx.strokeStyle = "#b42318";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
    ctx.stroke();
  }
}

export function WaveformPanel({
  media,
  waveformB64,
  duration,
  segments,
}: {
  media: MediaController;
  waveformB64: string | null;
  duration: number;
  segments?: WaveformSegment[];
}): React.JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [expanded, setExpanded] = useState(false);
  const height = expanded ? WAVEFORM_EXPANDED_PX : WAVEFORM_COLLAPSED_PX;
  // Media attach now happens in the parent effect (so the DOM <audio> wins),
  // which runs AFTER this child's first effect. Track the attached task via
  // the media subscription so the waveform is committed once the task is
  // known — otherwise the samples never reach the controller (N05).
  const [attachedTaskId, setAttachedTaskId] = useState<string | null>(
    () => media.attachedTaskId,
  );
  useEffect(
    () =>
      media.subscribe(() => {
        setAttachedTaskId((previous) => {
          const next = media.attachedTaskId;
          return previous === next ? previous : next;
        });
      }),
    [media],
  );

  useEffect(() => {
    if (!attachedTaskId) return;
    const generation = media.beginWaveformLoad(attachedTaskId);
    media.commitWaveform(attachedTaskId, generation, decodeWaveform(waveformB64));
  }, [media, attachedTaskId, waveformB64]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const render = (): void => {
      const snapshot = media.getSnapshot();
      // Read regions live so edited bounds (synced into the controller by
      // the page, R08) redraw without remounting the panel.
      const regions =
        segments ??
        media.segmentList().map((seg) => ({ start: seg.start, end: seg.end, id: seg.id }));
      drawWaveform(
        canvas,
        media.loadedWaveform,
        regions,
        duration,
        snapshot.selectedIndex,
        snapshot.currentTime,
        height,
      );
    };
    render();
    const unsubscribe = media.subscribe(render);
    const onResize = (): void => render();
    globalThis.addEventListener("resize", onResize);
    return () => {
      unsubscribe();
      globalThis.removeEventListener("resize", onResize);
    };
  }, [media, duration, height, waveformB64, segments, attachedTaskId]);

  const seekFromClick = (event: React.MouseEvent<HTMLCanvasElement>): void => {
    const canvas = canvasRef.current;
    if (!canvas || !(duration > 0)) return;
    const rect = canvas.getBoundingClientRect();
    const ratio = (event.clientX - rect.left) / Math.max(1, rect.width);
    const time = ratio * duration;
    const list =
      segments ??
      media.segmentList().map((seg) => ({ start: seg.start, end: seg.end, id: seg.id }));
    const index = list.findIndex((seg) => time >= seg.start && time <= seg.end);
    if (index >= 0) {
      media.playSegment(index);
    } else {
      media.seekTo(time);
    }
  };

  return (
    <section aria-label="Waveform" data-testid="waveform-panel" className={styles.panel}>
      <div className={styles.header}>
        <span className={styles.title}>Waveform</span>
        <button
          type="button"
          aria-expanded={expanded}
          aria-label={expanded ? "Collapse waveform" : "Expand waveform"}
          onClick={() => setExpanded((previous) => !previous)}
          className={styles.expandButton}
        >
          {expanded ? "Collapse" : "Expand"}
        </button>
      </div>
      <canvas
        ref={canvasRef}
        data-testid="waveform-canvas"
        onClick={seekFromClick}
        style={{ height: `${String(height)}px` }}
        className={styles.canvas}
      />
    </section>
  );
}
