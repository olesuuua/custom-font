#!/usr/bin/env ffpython
"""Migrate reviewed v3 decisions to hash-bound Bold v4 decisions."""
from __future__ import annotations

import csv
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path[:0] = [str(ROOT / ".font-deps"), str(ROOT / "tools")]
import repair_bold_v4 as v4

OLD = ROOT / "checkpoints" / "bold-v3-reviewed"
REPORT = ROOT / "qa" / "assets" / "bold-report.csv"
DECISIONS = ROOT / "qa" / "bold-manual-decisions.json"
READY = ROOT / "qa" / "bold-ready-to-pass.json"
VERSION = "bold-manual-review-v4"
METRICS = "bold-metrics-v4"


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_write(path, value):
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.stem + "-", suffix=".json", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    with REPORT.open(encoding="utf-8-sig", newline="") as handle:
        current = {row["glyph"]: row for row in csv.DictReader(handle)}
    with (OLD / "bold-report.csv").open(encoding="utf-8-sig", newline="") as handle:
        previous = {row["glyph"]: row for row in csv.DictReader(handle)}
    old_values = load(OLD / "bold-manual-decisions.json")["decisions"]
    old_font = fontforge.open(str(OLD / "bold.sfd"))
    new_font = fontforge.open(str(ROOT / "fontforge" / "bold.sfd"))
    migrated, ready = {}, {}
    changed = invalidated = 0
    now = datetime.now(timezone.utc).isoformat()
    try:
        for name, decision in old_values.items():
            if name not in current or name not in previous:
                continue
            row, old_row = current[name], previous[name]
            outline_changed = old_row["bold_hash"] != row["bold_hash"]
            if not outline_changed:
                migrated[name] = {
                    **decision,
                    "regular_hash": row["regular_hash"],
                    "base_hash": row["base_hash"],
                    "bold_hash": row["bold_hash"],
                    "metrics_version": METRICS,
                }
                continue
            changed += 1
            scores = {64: 0.0, 128: 0.0}
            qualifies = False
            if decision.get("status") == "almost_done" and not row["manual_blockers"]:
                metrics = v4.comparison(
                    old_font[name].foreground, new_font[name].foreground
                )
                scores = {64: metrics[64]["iou"], 128: metrics[128]["iou"]}
                qualifies = scores[64] >= .995 and scores[128] >= .995
            if qualifies:
                migrated[name] = {
                    "status": "almost_done",
                    "regular_hash": row["regular_hash"],
                    "base_hash": row["base_hash"],
                    "bold_hash": row["bold_hash"],
                    "metrics_version": METRICS,
                    "updated_at": now,
                    "ready_to_pass": True,
                    "previous_bold_hash": old_row["bold_hash"],
                    "repair_iou_64": scores[64],
                    "repair_iou_128": scores[128],
                }
                ready[name] = {
                    "previous_status": "almost_done",
                    "repair_iou_64": scores[64],
                    "repair_iou_128": scores[128],
                    "bold_hash": row["bold_hash"],
                }
            else:
                invalidated += 1
    finally:
        old_font.close()
        new_font.close()
    atomic_write(DECISIONS, {
        "version": VERSION, "metrics_version": METRICS, "decisions": migrated
    })
    atomic_write(READY, {
        "version": "bold-ready-to-pass-v2",
        "metrics_version": METRICS,
        "created_at": now,
        "glyphs": ready,
    })
    print(json.dumps({
        "preserved_or_rebound": len(migrated),
        "changed_reviewed": changed,
        "ready_to_pass": len(ready),
        "invalidated": invalidated,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
