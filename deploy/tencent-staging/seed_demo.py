"""Seed synthetic audio only into the explicitly named staging database.

Run from the project root with the staging app environment loaded:
    uv run --no-sync python deploy/tencent-staging/seed_demo.py
No ASR, real audio, or production database is involved.
"""
from __future__ import annotations

import json
import math
import struct
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from db import close_pool, db_conn  # noqa: E402
from preprocess_store import store_preprocessed_task  # noqa: E402


def main():
    config = json.loads((ROOT / "config.json").read_text())
    audio_root = Path(config["audio_dir"])
    if audio_root != Path("/data/annotation/staging-audio"):
        raise SystemExit("Refusing to seed outside the staging audio directory")
    with db_conn() as conn:
        if conn.execute("SELECT current_database()").fetchone()[0] != "annotation_tool_staging":
            raise SystemExit("Refusing to seed a non-staging database")
        folder = audio_root / "synthetic-demo"
        folder.mkdir(parents=True, exist_ok=True)
        for index in range(1, 9):
            name = f"demo-{index:02d}.wav"
            rate, duration = 16000, 6
            samples = [
                int(4000 * math.sin(2 * math.pi * (220 + index * 55) * n / rate))
                if (n // (rate // 2)) % 2 == 0 else 0
                for n in range(rate * duration)
            ]
            path = folder / name
            if not path.exists():
                with wave.open(str(path), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(rate)
                    wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
            waveform = samples[::48]
            result = store_preprocessed_task(
                conn, rel_path=f"synthetic-demo/{name}", filename=name,
                folder="synthetic-demo", duration=float(duration),
                category="synthetic-test",
                task_extra={"synthetic_demo": True},
                segments=[
                    {"id": n + 1, "start": float(n * 3), "end": float((n + 1) * 3),
                     "duration": 3.0, "asr_text": "Synthetic tone for deployment testing",
                     "text": "", "exclude_from_training": True}
                    for n in range(2)
                ],
                waveform_payload=struct.pack(f"<{len(waveform)}h", *waveform),
            )
            print(name, result["action"])
    close_pool()


if __name__ == "__main__":
    main()
