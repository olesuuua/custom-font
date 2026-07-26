#!/usr/bin/env ffpython
"""Reconcile live QA decisions into the targeted-repair report/checkpoint."""

from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf
import repair_redraw_v4 as v4
import repair_almost_done_glyphs as targeted


def main() -> int:
    with rf.REPORT.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        rows = list(reader)
    if not fields:
        raise RuntimeError("redraw report has no header")

    payload = json.loads(rf.DECISIONS.read_text(encoding="utf-8"))
    decisions = payload.get("decisions", {})
    accepted = []
    blocked = []
    for row in rows:
        rf.apply_manual_decision(row, payload)
        status = row["manual_status"]
        if status == "pass":
            if row["automatic_pass"] != "true" and row["effective_status"] != "manual-pass":
                blocked.append(row["glyph"])
                row["current_review_status"] = "needs_rework"
            else:
                row["current_review_status"] = "pass"
                accepted.append(row["glyph"])
        elif status in ("almost_done", "needs_rework"):
            row["current_review_status"] = status
        elif row["automatic_pass"] == "true":
            row["current_review_status"] = "automatic_pass"
        else:
            row["current_review_status"] = "unreviewed"
    if blocked:
        raise RuntimeError("structurally blocked manual passes: " + ", ".join(blocked))

    tmp = rf.REPORT.with_suffix(rf.REPORT.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)
    tmp.replace(rf.REPORT)

    shutil.copy2(rf.REPORT, targeted.CHECKPOINT / "redraw-report.csv")
    shutil.copy2(rf.DECISIONS, targeted.CHECKPOINT / "redraw-manual-decisions.json")
    manifest_path = targeted.CHECKPOINT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["manual_pass_glyphs"] = sorted(accepted)
    manifest["manual_pass_count"] = len(accepted)
    manifest["automatic_pass_count"] = sum(
        row["current_review_status"] == "automatic_pass" for row in rows
    )
    manifest["unresolved_glyphs"] = [
        row["glyph"] for row in rows if row["needs_manual_review"] == "true"
    ]
    manifest["artifact_hashes"]["redraw-report.csv"] = v4.sha256(
        targeted.CHECKPOINT / "redraw-report.csv"
    )
    manifest["artifact_hashes"]["redraw-manual-decisions.json"] = v4.sha256(
        targeted.CHECKPOINT / "redraw-manual-decisions.json"
    )
    v4.atomic_json(manifest_path, manifest)
    print(json.dumps({
        "manual_passes": len(accepted),
        "automatic_passes": manifest["automatic_pass_count"],
        "unresolved": len(manifest["unresolved_glyphs"]),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
