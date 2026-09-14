from __future__ import annotations

import json
import tarfile

import numpy as np
import soundfile as sf

from scripts import export_top_annotators_tar as exporter


def test_tar_contains_source_path_mapping(tmp_path):
    audio_dir = tmp_path / "source"
    source = audio_dir / "original_folder" / "original.wav"
    source.parent.mkdir(parents=True)
    stereo = np.zeros((3200, 2), dtype=np.float32)
    sf.write(source, stereo, exporter.TARGET_SAMPLE_RATE, subtype="PCM_16")

    segment = exporter.Segment(
        rank=1,
        username="annotator",
        task_id="12345678-9abc-4def-8123-456789abcdef",
        rel_path="original_folder/original.wav",
        segment_id=7,
        start_s=0.025,
        end_s=0.125,
        text="human text",
        asr_text="original asr",
    )
    staging = tmp_path / "staging"
    staging.mkdir()

    items, mappings, converted = exporter.create_dataset(
        staging, audio_dir.resolve(), [segment]
    )
    exporter.write_json(staging / "data.json", items)
    exporter.write_json(staging / "source_path_mapping.json", mappings)
    frames = exporter.verify_staging(staging, items, mappings)

    assert converted == 0
    assert frames == 1600
    assert set(items[0]) == {"audio", "text", "asr_text"}
    assert mappings == [
        {
            "audio": items[0]["audio"],
            "source_rel_path": "original_folder/original.wav",
            "source_absolute_path": str(source.resolve()),
        }
    ]

    output = tmp_path / "dataset.tar"
    exporter.create_tar(staging, output)
    with tarfile.open(output, "r") as archive:
        members = {member.name for member in archive.getmembers()}
        archived_items = json.load(archive.extractfile("data.json"))
        archived_mappings = json.load(
            archive.extractfile("source_path_mapping.json")
        )

    assert "data.json" in members
    assert "source_path_mapping.json" in members
    assert items[0]["audio"] in members
    assert archived_items == items
    assert archived_mappings == mappings
