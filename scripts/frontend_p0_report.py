#!/usr/bin/env python3
"""Build a local P0 screenshot gallery and verify evidence references."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/plans/frontend-rebuild-p0")
    args = parser.parse_args()
    output = args.output.resolve()
    manifests = []
    for path in sorted(output.glob("capture-*.json")):
        data = json.loads(path.read_text())
        if isinstance(data, dict) and "screenshots" in data:
            manifests.append((path.stem.removeprefix("capture-"), data))
    measurements, assets = [], []
    sections, rows = [], []
    for group, manifest in manifests:
        cards = []
        for shot in manifest["screenshots"]:
            image = output / shot["file"]
            if not image.is_file():
                raise RuntimeError(f"Missing screenshot: {image}")
            assets.append({"file": shot["file"], "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                           "bytes": image.stat().st_size})
            g = shot["geometry"]
            v = g["viewport"]
            transcript = g["boxes"].get('textarea[data-text="0"]')
            input_visible = bool(transcript and transcript["visible"] and
                transcript["x"] < v["width"] and transcript["right"] > 0 and
                transcript["y"] < v["height"] and transcript["bottom"] > 0)
            measurement = {"file": shot["file"], "group": group, "viewport": v,
                "document_width": g["document"]["width"],
                "horizontal_overflow_px": max(0, g["document"]["width"] - v["width"]),
                "first_transcript": transcript, "first_transcript_in_initial_viewport": input_visible,
                "rendered_transcripts": g["transcriptCount"]}
            measurements.append(measurement)
            meta = f"{v['width']}×{v['height']} · document width {g['document']['width']}px"
            if transcript:
                meta += f" · first transcript x={transcript['x']:.0f}, y={transcript['y']:.0f}"
            title = Path(shot["file"]).stem
            cards.append(f'<article><a href="{html.escape(shot["file"])}"><img loading="lazy" '
                f'src="{html.escape(shot["file"])}" alt="{html.escape(title)}"></a>'
                f'<h3>{html.escape(title)}</h3><p>{html.escape(meta)}</p>'
                f'<p>{html.escape(shot["note"])}</p><code>{html.escape(shot["url"])}</code></article>')
            rows.append(f'| [{title}]({shot["file"]}) | {group} | {meta} | {shot["note"]} |')
        sections.append(f'<section id="{html.escape(group)}"><h2>{html.escape(group)}</h2>'
                        f'<div class="grid">{"".join(cards)}</div></section>')
    source = json.loads((output / "source-manifest.json").read_text())
    checks = []
    for name, info in source["files"].items():
        current = (ROOT / name).read_bytes()
        original = subprocess.check_output(["git", "show", f"{source['commit']}:{name}"], cwd=ROOT)
        checks.append({"file": name,
            "matches_baseline_commit": current == original,
            "matches_recorded_sha256": hashlib.sha256(current).hexdigest() == info["sha256"]})
    names = [a["file"] for a in assets]
    if len(names) != len(set(names)):
        raise RuntimeError("Duplicate screenshot references")
    verification = {"screenshots": len(assets), "all_screenshots_exist": True,
        "application_source_unchanged": all(c["matches_baseline_commit"] and c["matches_recorded_sha256"] for c in checks),
        "source_checks": checks, "screenshot_assets": assets}
    for name, data in [("layout-measurements.json", measurements), ("evidence-verification.json", verification)]:
        (output / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    (output / "screenshots.md").write_text(
        "# P0 截图索引\n\n当前源码 + 一次性合成数据；链接打开原图。"
        "尺寸是 CSS viewport；full-page 截图的文件高度可能更大。\n\n"
        "| 截图 | 场景 | 布局测量 | 说明 |\n| --- | --- | --- | --- |\n" + "\n".join(rows) + "\n")
    gallery = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>P0 · 当前前端截图基线</title><style>
*{box-sizing:border-box}body{font:15px/1.65 system-ui,sans-serif;color:#20252b;background:#f5f6f8;margin:0}
main{max-width:1540px;margin:auto;padding:32px}h1{margin:0 0 8px}h2{margin:28px 0 12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,360px),1fr));gap:20px}
article{background:white;border:1px solid #d9dde3;border-radius:6px;padding:12px;min-width:0}
img{width:100%;height:260px;object-fit:contain;object-position:top;background:#eee;display:block}
h3{font-size:14px;overflow-wrap:anywhere}p{margin:8px 0;color:#4d5661}code{overflow-wrap:anywhere;font-size:12px}
a{color:#1853a0}nav{display:flex;flex-wrap:wrap;gap:18px;margin:18px 0}
</style><main><h1>P0 · 当前前端截图基线</h1>
<p>当前 main 源码，隔离 PostgreSQL 与合成音频。点击图片查看原图；这是现状记录，不是重设计预览。</p>
<p><a href="README.md">交付说明</a> · <a href="issues.md">问题清单</a> · <a href="screenshots.md">截图索引</a></p>'''
    gallery += '<nav>' + ''.join(f'<a href="#{html.escape(g)}">{html.escape(g)}</a>' for g, _ in manifests) + '</nav>'
    gallery += ''.join(sections) + '</main></html>\n'
    (output / "gallery.html").write_text(gallery)
    print(json.dumps({k: verification[k] for k in ("screenshots", "all_screenshots_exist", "application_source_unchanged")}, ensure_ascii=False))
    return 0 if verification["application_source_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
