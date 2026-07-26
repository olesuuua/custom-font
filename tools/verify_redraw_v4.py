#!/usr/bin/env ffpython
"""Verify redraw-v4 locks, per-glyph ceilings, structure, donors, and metadata."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".font-deps")); sys.path.insert(0, str(ROOT / "tools"))
from fontTools.ttLib import TTFont
import redraw_font as rf
import repair_redraw_v4 as v4


def points(glyph): return sum(len(contour) for contour in glyph.foreground)


def main():
    checkpoint = ROOT / "checkpoints" / "redraw-v4"
    reviewed = ROOT / "checkpoints" / "redraw-reviewed-v3"
    manifest = json.loads((checkpoint / "manifest.json").read_text(encoding="utf-8"))
    frozen = json.loads((reviewed / "manifest.json").read_text(encoding="utf-8"))
    with rf.REPORT.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    by_name = {row["glyph"]: row for row in rows}
    decisions = json.loads(rf.DECISIONS.read_text(encoding="utf-8"))["decisions"]
    source_tt = TTFont(str(rf.SOURCE_TTF), recalcBBoxes=False, recalcTimestamp=False)
    output_tt = TTFont(str(rf.OUTPUT_TTF), recalcBBoxes=False, recalcTimestamp=False)
    output = fontforge.open(str(rf.OUTPUT_SFD)); review_font = fontforge.open(str(rf.REVIEW_SFD))
    frozen_font = fontforge.open(str(reviewed / "redrawn.sfd"))
    errors = []
    try:
        if len(rows) != 334 or manifest.get("glyph_count") != 334: errors.append("glyph count differs")
        if len(frozen["locked_glyphs"]) != 290: errors.append("locked count is not 290")
        if source_tt.getGlyphOrder() != output_tt.getGlyphOrder(): errors.append("glyph order differs")
        if source_tt.getBestCmap() != output_tt.getBestCmap(): errors.append("cmap differs")
        if source_tt["hmtx"].metrics != output_tt["hmtx"].metrics: errors.append("metrics differ")
        for filename, expected in manifest["artifact_hashes"].items():
            if rf.sha256(checkpoint / filename) != expected:
                errors.append("checkpoint hash differs: " + filename)
        for name, lock in frozen["locked_glyphs"].items():
            if rf.layer_hash(output[name].foreground) != lock["candidate_hash"]:
                errors.append(name + " locked outline changed")
            if rf.layer_hash(frozen_font[name].foreground) != lock["candidate_hash"]:
                errors.append(name + " frozen lock differs")
        for name, row in by_name.items():
            actual = points(output[name]); ceiling = 120 if name in v4.FRACTIONS else 100
            if actual != int(row["redrawn_points"] or 0): errors.append(name + " point count differs")
            if actual > ceiling: errors.append(name + " exceeds point ceiling")
            if actual > 100 and name not in v4.FRACTIONS: errors.append(name + " unauthorized 120-point exception")
            if rf.layer_hash(output[name].foreground) != row["candidate_hash"]:
                errors.append(name + " candidate hash differs")
            if rf.layer_hash(review_font[name].foreground) != row["candidate_hash"]:
                errors.append(name + " review outline differs")
            if row.get("changed_in_v4") == "true":
                audit = v4.direction_audit(output[name].foreground)
                if audit["changed"] or not audit["idempotent"] or audit["degenerate"]:
                    errors.append(name + " direction is not normalized")
                if int(row["self_intersections"] or 0) or int(row["invalid_handles"] or 0):
                    errors.append(name + " changed outline is structurally invalid")
                if row["topology_status"] != "match": errors.append(name + " changed topology mismatch")
                decision = decisions.get(name, {})
                valid_review = (
                    decision.get("status") in ("pass", "almost_done", "needs_rework")
                    and decision.get("source_hash") == row["source_hash"]
                    and decision.get("candidate_hash") == row["candidate_hash"]
                    and decision.get("status") == row.get("current_review_status")
                )
                if not valid_review:
                    errors.append(name + " changed decision is stale or unreconciled")
        if len(output["Euro"].foreground) != 1: errors.append("Euro must have one contour")
        if len(output["uni20AA"].foreground) != 2: errors.append("uni20AA must have two contours")
        for name, (donor, degrees) in v4.DIAGONAL_DONORS.items():
            row = by_name[name]
            if row.get("repair_method") != "paired-diagonal-donor":
                errors.append(name + " did not use paired donor")
            if row.get("donor_glyphs") != donor or "rotate({})".format(degrees) not in row.get("donor_transform", ""):
                errors.append(name + " donor provenance differs")
        old_rows = {}
        with (reviewed / "redraw-report.csv").open(encoding="utf-8-sig", newline="") as handle:
            old_rows = {row["glyph"]: row for row in csv.DictReader(handle)}
        for name in ("twothirds", "fiveeighths"):
            row = by_name[name]
            if float(row["raw_ink_iou_128_v4"]) <= float(old_rows[name]["ink_iou_128"]):
                errors.append(name + " did not improve raw IoU")
            if int(row["self_intersections"] or 0) or row["topology_status"] != "match":
                errors.append(name + " fraction structure remains invalid")
        for name, old in old_rows.items():
            if old.get("current_review_status") == "almost_done" and by_name[name].get("changed_in_v4") != "true":
                if by_name[name].get("current_review_status") != "almost_done":
                    errors.append(name + " unchanged almost-done status was lost")
    finally:
        output.close(); review_font.close(); frozen_font.close(); source_tt.close(); output_tt.close()
    print("glyphs={}; errors={}".format(len(rows), len(errors)))
    for error in errors[:100]: print(error)
    return 1 if errors else 0


if __name__ == "__main__": raise SystemExit(main())
