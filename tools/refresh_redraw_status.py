#!/usr/bin/env ffpython
"""Refresh effective review states without rebuilding redraw geometry."""

import csv
import json
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf


def main():
    with rf.REPORT.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        rows = list(reader)
    decisions = json.loads(rf.DECISIONS.read_text(encoding="utf-8"))
    for row in rows:
        rf.apply_manual_decision(row, decisions)
        if row.get("current_review_status") == "needs_rework":
            row["needs_manual_review"] = "true"
    with rf.REPORT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)
    checkpoint = ROOT / "checkpoints" / "redraw-v2"
    shutil.copy2(rf.REPORT, checkpoint / "redraw-report.csv")
    manifest_path = checkpoint / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["unresolved_glyphs"] = [row["glyph"] for row in rows if row["needs_manual_review"] == "true"]
    manifest["artifact_hashes"]["redraw-report.csv"] = rf.sha256(checkpoint / "redraw-report.csv")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("unresolved={}".format(len(manifest["unresolved_glyphs"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
