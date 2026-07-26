#!/usr/bin/env ffpython
"""Reconcile post-serialization v3 hashes, decisions, report, and manifest."""

from __future__ import annotations

import csv
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf
import repair_redraw_v3 as v3


def main():
    frozen = json.loads((v3.REVIEWED / "manifest.json").read_text(encoding="utf-8"))
    with rf.REPORT.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle); fields = reader.fieldnames; rows = list(reader)
    decisions_payload = json.loads(rf.DECISIONS.read_text(encoding="utf-8"))
    decisions = decisions_payload["decisions"]
    output = fontforge.open(str(rf.OUTPUT_SFD))
    now = datetime.now(timezone.utc).isoformat()
    changed = []
    try:
        for row in rows:
            name = row["glyph"]
            current_hash = rf.layer_hash(output[name].foreground)
            frozen_hash = frozen.get("repair_queue", {}).get(name, {}).get("candidate_hash")
            if not frozen_hash or current_hash == frozen_hash:
                continue
            changed.append(name)
            row["changed_in_v3"] = "true"
            row["current_review_status"] = "needs_rework"
            if row.get("repair_method") == "reviewed-v2":
                row["repair_method"] = "reviewed-v2-reserialized"
            decisions[name] = {
                "status": "needs_rework", "source_hash": row["source_hash"],
                "candidate_hash": current_hash, "updated_at": now,
                "previous_review_status": row.get("previous_review_status", "needs_rework"),
            }
            rf.apply_manual_decision(row, {"decisions": decisions})
    finally:
        output.close()
    with rf.REPORT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)
    v3.atomic_json(rf.DECISIONS, decisions_payload)
    shutil.copy2(rf.REPORT, v3.V3 / "redraw-report.csv")
    shutil.copy2(rf.DECISIONS, v3.V3 / "redraw-manual-decisions.json")
    manifest_path = v3.V3 / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["changed_glyphs"] = sorted(set(manifest.get("changed_glyphs", [])) | set(changed))
    manifest["unresolved_glyphs"] = [row["glyph"] for row in rows if row["needs_manual_review"] == "true"]
    manifest["artifact_hashes"]["redraw-report.csv"] = v3.sha256(v3.V3 / "redraw-report.csv")
    manifest["artifact_hashes"]["redraw-manual-decisions.json"] = v3.sha256(v3.V3 / "redraw-manual-decisions.json")
    v3.atomic_json(manifest_path, manifest)
    print(json.dumps({"serialized_changes": changed, "unresolved": len(manifest["unresolved_glyphs"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
