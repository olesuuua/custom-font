#!/usr/bin/env python
"""Freeze the current QA decisions into the redraw-v9 checkpoint."""

from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf
import repair_redraw_v8 as v8
import repair_redraw_v9 as v9


def main() -> int:
    report_path = ROOT / "qa" / "assets" / "redraw-report.csv"
    decisions_path = ROOT / "qa" / "redraw-manual-decisions.json"
    with report_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    if not fields:
        raise RuntimeError("redraw-v9 report has no header")

    payload = json.loads(decisions_path.read_text(encoding="utf-8"))
    for row in rows:
        rf.apply_manual_decision(row, payload)
        status = row.get("manual_status", "")
        if status in ("pass", "almost_done", "needs_rework"):
            row["current_review_status"] = status
        elif row.get("automatic_pass") == "true":
            row["current_review_status"] = "automatic_pass"
        else:
            row["current_review_status"] = "unreviewed"
        row["needs_manual_review"] = (
            "true"
            if row["current_review_status"]
            in ("unreviewed", "almost_done", "needs_rework")
            else "false"
        )

    temporary = report_path.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(report_path)

    shutil.copy2(report_path, v9.V9 / "redraw-report.csv")
    shutil.copy2(decisions_path, v9.V9 / "redraw-manual-decisions.json")
    manifest_path = v9.V9 / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["manual_pass_glyphs"] = sorted(
        row["glyph"] for row in rows
        if row["current_review_status"] == "pass"
    )
    manifest["manual_pass_count"] = len(manifest["manual_pass_glyphs"])
    manifest["unresolved_glyphs"] = sorted(
        row["glyph"] for row in rows
        if row["current_review_status"]
        in ("unreviewed", "almost_done", "needs_rework")
    )
    manifest["artifact_hashes"]["redraw-report.csv"] = v8.sha256(
        v9.V9 / "redraw-report.csv"
    )
    manifest["artifact_hashes"]["redraw-manual-decisions.json"] = v8.sha256(
        v9.V9 / "redraw-manual-decisions.json"
    )
    v9.atomic_json(manifest_path, manifest)
    target_statuses = {
        name: payload["decisions"][name]["status"]
        for name in sorted(v9.TARGETS)
    }
    print(json.dumps({
        "manual_passes": manifest["manual_pass_count"],
        "target_statuses": target_statuses,
        "unresolved": len(manifest["unresolved_glyphs"]),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
