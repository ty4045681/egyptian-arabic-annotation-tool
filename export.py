#!/usr/bin/env python3
"""Export current published annotations from PostgreSQL to Excel.

Run with:
    uv run python export.py
    uv run python export.py --output annotation_export.xlsx
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill

from annotation_metadata.contracts import TaskFilter
from annotation_metadata.export_metadata import (
    EXCEL_HEADERS, applied_filter_context, excel_column_values, parse_task_filter,
)
from annotation_metadata.queries import metadata_filter_sql
from annotation_quality.queries import (
    credited_annotator_sql, latest_quality_round_lateral_sql,
    training_export_eligible_sql,
)
from db import db_conn

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_OUTPUT = SCRIPT_DIR / "annotation_export.xlsx"


def export_xlsx(output: Path, *, filters=None) -> int:
    task_filter = filters or TaskFilter()
    meta_sql, meta_params = metadata_filter_sql(task_filter)
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Annotations")
    ws.freeze_panes = "A2"
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 32
    ws.column_dimensions["C"].width = 48
    ws.column_dimensions["D"].width = 16
    ws.column_dimensions["E"].width = 14

    fill = PatternFill(start_color="1e1e2e", end_color="1e1e2e", fill_type="solid")
    font = Font(bold=True, size=11, color="ffffff")
    headers = []
    for value in EXCEL_HEADERS:
        cell = WriteOnlyCell(ws, value=value)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center")
        headers.append(cell)
    ws.append(headers)

    count = 0
    credited = credited_annotator_sql()
    quality_join = latest_quality_round_lateral_sql()
    eligible_sql, eligible_params = training_export_eligible_sql()
    with db_conn() as conn:
        # Named server-side cursor keeps memory stable for 100k+ rows.
        with conn.cursor(name="annotation_export", row_factory=None) as cur:
            cur.itersize = 2000
            cur.execute(
                f"""SELECT u.username, t.folder, t.filename, t.duration, t.status, t.id,
                           q.state, ({eligible_sql}) AS training_eligible
                   FROM annotation_tasks t
                   JOIN annotation_versions v ON v.id = t.current_published_version_id
                   LEFT JOIN annotators u ON u.id = {credited}
                   {quality_join}
                   WHERE t.status IN ('annotated', 'skipped')
                     AND ({meta_sql})
                   ORDER BY t.folder, t.filename""",
                (*eligible_params, *meta_params),
            )
            rows = list(cur)
        from annotation_metadata.repository import metadata_summaries
        with conn.cursor() as summary_cur:
            summaries = metadata_summaries(
                summary_cur, [row[5] for row in rows], filters=task_filter,
            )
        for (username, folder, filename, duration, status, task_id,
             quality_state, training_eligible) in rows:
            summary = summaries.get(str(task_id), {})
            ws.append([
                username, folder, filename, round(float(duration or 0), 1), status,
                *excel_column_values(summary),
                quality_state or "none",
                "yes" if training_eligible else "no",
            ])
            count += 1

    output.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output)
    return count


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export published annotation statistics to Excel with explicit "
            "source scene/confidence/batch and human verification columns. "
            "Default includes every published annotated/skipped task."
        )
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source-scene")
    parser.add_argument("--source-confidence")
    parser.add_argument("--batch-code")
    parser.add_argument("--review-status")
    parser.add_argument("--prediction-scene")
    parser.add_argument("--human-scene")
    args = parser.parse_args()
    filters = parse_task_filter({
        "source_scene": args.source_scene,
        "source_confidence": args.source_confidence,
        "batch_code": args.batch_code,
        "review_status": args.review_status,
        "prediction_scene": args.prediction_scene,
        "human_scene": args.human_scene,
    })
    count = export_xlsx(args.output.resolve(), filters=filters)
    print(f"Exported {count} rows to {args.output.resolve()}")
    print(json.dumps(
        {"applied_filters": applied_filter_context(filters)},
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
