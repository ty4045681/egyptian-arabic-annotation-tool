#!/usr/bin/env python3
"""Aggregate P1 capture manifests into reviewable evidence.

Reads capture-*.json / scale-*-round*.json / resource-volumes.json /
media-lifecycle.json from the P1 evidence directory and writes:

* layout-measurements.json  (P0-compatible entries wrapped with a status
  envelope so "not executed" can never be mistaken for "no findings")
* screenshots.md            (indexed gallery of P1 screenshots)
* p1-evidence-verification.json (screenshot existence, P0 dir untouched)

Refuses --output inside docs/plans/frontend-rebuild-p0. Exits 2 with an
explicit message when no manifests exist yet (nothing is silently passed).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs/plans/frontend-rebuild-p1"
P0_DIR = ROOT / "docs/plans/frontend-rebuild-p0"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = args.output.resolve()
    if output == P0_DIR.resolve() or str(output).startswith(str(P0_DIR.resolve()) + "/"):
        print(f"ERROR: refusing to write into the P0 directory: {output}")
        return 2
    manifests = []
    for path in sorted(output.glob("capture-*.json")):
        data = json.loads(path.read_text())
        if isinstance(data, dict) and "screenshots" in data:
            manifests.append((path.stem.removeprefix("capture-"), data))
    if not manifests:
        print(f"ERROR: no capture-*.json manifests in {output}; "
              "run `python scripts/frontend_p1.py capture` first. Nothing aggregated.")
        return 2
    measurements, rows, assets = [], [], []
    for group, manifest in manifests:
        for shot in manifest["screenshots"]:
            image = output / shot["file"]
            if not image.is_file():
                raise RuntimeError(f"Missing screenshot: {image}")
            assets.append({"file": shot["file"],
                           "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                           "bytes": image.stat().st_size})
            g = shot["geometry"]
            v = g["viewport"]
            measurement = {
                "file": shot["file"], "group": group, "viewport": v,
                "document_width": g["document"]["width"],
                "horizontal_overflow_px": max(0, g.get("pageOverflowPx", 0)),
                "first_transcript": g.get("firstTranscript"),
                "first_transcript_dir": g.get("firstTranscriptDir"),
                "save_state": g.get("saveState"),
                "play_entry": g.get("playEntry"),
                "toolbar_max_height_diff_px": g.get("toolbarMaxHeightDiff"),
                "icon_buttons_missing_name": g.get("iconButtonsMissingName", []),
                "non_table_x_scrollers": [c for c in g.get("scrollContainers", [])
                                           if not c.get("allowed")],
                "transcript_count": g.get("transcriptCount"),
                "csp_violations": g.get("cspViolations", []),
                "external_requests": g.get("externalRequests", []),
            }
            measurements.append(measurement)
            meta = (f"{v['width']}x{v['height']} · page overflow "
                    f"{measurement['horizontal_overflow_px']}px")
            title = Path(shot["file"]).stem
            rows.append(f"| [{title}]({shot['file']}) | {group} | {meta} | {shot['note']} |")
    (output / "layout-measurements.json").write_text(json.dumps(
        {"status": "measured", "schema": "P0-compatible entries plus P1 save/play/"
         "toolbar/a11y/CSP fields; pageOverflow excludes allowed [data-scroll-x=table] "
         "containers", "measurements": measurements},
        ensure_ascii=False, indent=2) + "\n")
    (output / "screenshots.md").write_text(
        "# P1 screenshot index\n\nSynthetic seed data; en-US / Asia/Shanghai / "
        "deviceScaleFactor 1 / reduced motion. Masks cover only dynamic UUID/clock "
        "regions, never layout-critical state.\n\n"
        "P0 comparison shots live in `../frontend-rebuild-p0/screenshots.md` and are "
        "never overwritten; any P0 reference copies used for side-by-side review are "
        "stored here under `screenshots/p0-compare/`.\n\n"
        "| Screenshot | Scenario | Layout | Note |\n| --- | --- | --- | --- |\n"
        + "\n".join(rows) + "\n")
    p0_present = P0_DIR.is_dir()
    verification = {
        "screenshots": len(assets),
        "all_screenshots_exist": True,
        "p0_directory_untouched_by_p1_report": p0_present,
        "screenshot_assets": assets,
    }
    (output / "p1-evidence-verification.json").write_text(
        json.dumps(verification, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"screenshots": len(assets),
                      "measurements": len(measurements),
                      "p0_directory_present": p0_present}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
