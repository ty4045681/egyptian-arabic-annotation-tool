#!/usr/bin/env python3
"""Export current published annotations from PostgreSQL to Excel.

Run with:
    uv run python export.py
    uv run python export.py --output annotation_export.xlsx
"""

from __future__ import annotations

import argparse
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill

from db import db_conn

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_OUTPUT = SCRIPT_DIR / "annotation_export.xlsx"


def export_xlsx(output: Path) -> int:
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
    for value in ["User", "Folder", "Audio", "Duration (s)", "Status"]:
        cell = WriteOnlyCell(ws, value=value)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center")
        headers.append(cell)
    ws.append(headers)

    count = 0
    with db_conn() as conn:
        # Named server-side cursor keeps memory stable for 100k+ rows.
        with conn.cursor(name="annotation_export", row_factory=None) as cur:
            cur.itersize = 2000
            cur.execute(
                """SELECT u.username, t.folder, t.filename, t.duration, t.status
                   FROM annotation_tasks t
                   JOIN annotation_versions v ON v.id = t.current_published_version_id
                   JOIN annotators u ON u.id = v.submitted_by_user_id
                   WHERE t.status IN ('annotated', 'skipped')
                   ORDER BY t.folder, t.filename"""
            )
            for username, folder, filename, duration, status in cur:
                ws.append([username, folder, filename, round(float(duration or 0), 1), status])
                count += 1

    output.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output)
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Export annotation statistics to Excel")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    count = export_xlsx(args.output.resolve())
    print(f"Exported {count} rows to {args.output.resolve()}")


if __name__ == "__main__":
    main()
