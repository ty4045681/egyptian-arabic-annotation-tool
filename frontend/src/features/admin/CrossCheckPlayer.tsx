import {
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type Ref,
} from "react";
import { Alert, Button } from "antd";
import {
  PauseOutlined,
  CaretRightOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { z } from "zod";
import { useAdminQuery } from "./api";
import { decodeWaveform } from "../workspace/media/mediaController";
import type { CrossDetail } from "./schemas";
import type { ReviewRow } from "./crossCheckReviewModel";

export type ReviewPlayer = {
  toggle: () => void;
  playRange: (start: number, end: number) => void;
  pause: () => void;
};
const waveformSchema = z.object({ waveform_b64: z.string().nullable() });
function timestamp(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${(seconds % 60).toFixed(1).padStart(4, "0")}`;
}

export function CrossCheckPlayer({
  data,
  rows,
  follow,
  rate,
  active,
  onActive,
  playerRef,
}: {
  data: CrossDetail;
  rows: ReviewRow[];
  follow: boolean;
  rate: number;
  active: string;
  onActive: (id: string, scroll: boolean) => void;
  playerRef: Ref<ReviewPlayer>;
}): React.JSX.Element {
  const audio = useRef<HTMLAudioElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const stopAt = useRef<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [error, setError] = useState("");
  const waveform = useAdminQuery(
    `/api/admin/waveform/${encodeURIComponent(data.task_id)}`,
    waveformSchema,
  );
  const samples = useMemo(
    () => decodeWaveform(waveform.data?.waveform_b64),
    [waveform.data],
  );
  const total = Math.max(data.duration_seconds, 0);
  function play() {
    void audio.current
      ?.play()
      .catch(() =>
        setError("Audio could not play. Check the connection and retry."),
      );
  }
  function toggle() {
    stopAt.current = null;
    if (audio.current?.paused) play();
    else audio.current?.pause();
  }
  function seek(seconds: number) {
    if (!audio.current) return;
    audio.current.currentTime = Math.max(0, Math.min(total, seconds));
    stopAt.current = null;
    setTime(audio.current.currentTime);
  }
  useImperativeHandle(playerRef, () => ({
    toggle,
    playRange: (start, end) => {
      seek(start);
      stopAt.current = end;
      play();
    },
    pause: () => audio.current?.pause(),
  }));
  useEffect(() => {
    if (audio.current) audio.current.playbackRate = rate;
  }, [rate]);
  useEffect(() => {
    const element = audio.current;
    return () => {
      element?.pause();
      element?.removeAttribute("src");
      element?.load();
    };
  }, []);
  useEffect(() => {
    if (!playing) return;
    let frame = 0;
    let last = 0;
    function tick(now: number) {
      const element = audio.current;
      if (!element) return;
      if (stopAt.current !== null && element.currentTime >= stopAt.current) {
        element.pause();
        element.currentTime = stopAt.current;
        stopAt.current = null;
      }
      if (now - last > 50) {
        setTime(element.currentTime);
        last = now;
        if (follow) {
          const row = rows.find(
            (item) =>
              item.start <= element.currentTime &&
              item.end > element.currentTime,
          );
          if (row && row.id !== active) onActive(row.id, true);
        }
      }
      frame = requestAnimationFrame(tick);
    }
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [playing, follow, active, rows, onActive]);
  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    function draw() {
      if (!element) return;
      const context = element.getContext("2d");
      if (!context) return;
      const width = element.clientWidth;
      const height = 64;
      const ratio = window.devicePixelRatio || 1;
      element.width = width * ratio;
      element.height = height * ratio;
      context.scale(ratio, ratio);
      context.clearRect(0, 0, width, height);
      context.strokeStyle = getComputedStyle(element).color;
      context.lineWidth = 1;
      context.beginPath();
      for (let x = 0; x < width; x += 2) {
        const from = Math.floor((x / Math.max(1, width)) * samples.length);
        const to = Math.max(
          from + 1,
          Math.floor(((x + 2) / Math.max(1, width)) * samples.length),
        );
        let peak = 0;
        for (let i = from; i < to; i++)
          peak = Math.max(peak, Math.abs(samples[i] ?? 0));
        const amplitude = Math.max(0.5, peak * 26);
        context.moveTo(x, height / 2 - amplitude);
        context.lineTo(x, height / 2 + amplitude);
      }
      context.stroke();
    }
    const observer = new ResizeObserver(draw);
    observer.observe(element);
    draw();
    return () => observer.disconnect();
  }, [samples]);
  return (
    <div className="cc-review-media">
      <audio
        ref={audio}
        id="ccAudio"
        preload="auto"
        src={data.audio_url}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onError={() =>
          setError("Audio is unavailable. Check the connection and retry.")
        }
        onCanPlay={() => setError("")}
      />
      <div className="cc-review-player">
        <Button
          type="primary"
          shape="circle"
          className="cc-play-button"
          icon={playing ? <PauseOutlined /> : <CaretRightOutlined />}
          aria-label={playing ? "Pause audio" : "Play audio"}
          onClick={toggle}
        />
        <div
          className="cc-waveform"
          role="slider"
          tabIndex={0}
          aria-label="Audio position"
          aria-valuemin={0}
          aria-valuemax={total}
          aria-valuenow={Math.min(total, time)}
          aria-valuetext={`${timestamp(time)} of ${timestamp(total)}`}
          onClick={(event) => {
            const bounds = event.currentTarget.getBoundingClientRect();
            seek(((event.clientX - bounds.left) / bounds.width) * total);
          }}
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
              event.preventDefault();
              seek(time + (event.key === "ArrowRight" ? 5 : -5));
            }
          }}
        >
          {total > 0 &&
            rows.map((row) => (
              <span
                key={row.id}
                className={`cc-wave-region ${row.different ? "cc-wave-difference" : ""} ${active === row.id ? "cc-wave-active" : ""}`}
                style={{
                  left: `${(row.start / total) * 100}%`,
                  width: `${(Math.max(0, row.end - row.start) / total) * 100}%`,
                }}
              />
            ))}
          <canvas ref={canvas} aria-hidden="true" />
          <span
            className="cc-wave-cursor"
            style={{
              left: `${total ? Math.min(100, (time / total) * 100) : 0}%`,
            }}
          />
          {!samples.length && (
            <span className="cc-wave-note">
              {waveform.isPending ? "Loading waveform…" : "Segment timeline"}
            </span>
          )}
        </div>
        <span className="cc-player-time">
          {timestamp(time)} / {timestamp(total)}
        </span>
      </div>
      {error && (
        <Alert
          type="error"
          title={error}
          action={
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={() => {
                audio.current?.load();
                play();
              }}
            >
              Retry audio
            </Button>
          }
        />
      )}
      {waveform.isError && (
        <Alert
          type="warning"
          title="Waveform unavailable. Audio playback and segment seeking are still available."
          action={
            <Button size="small" onClick={() => void waveform.refetch()}>
              Retry waveform
            </Button>
          }
        />
      )}
    </div>
  );
}
