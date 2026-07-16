#!/usr/bin/env python3
"""Verify generated outlines, report counts, and preserved font metadata."""

import csv
import hashlib
import json
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".font-deps"))
from fontTools.ttLib import TTFont
SOURCE = ROOT / "qa" / "assets" / "original.ttf"
FINAL = ROOT / "qa" / "assets" / "simplified.ttf"
REVIEW = ROOT / "qa" / "assets" / "review-candidates.ttf"
CLEANUP = ROOT / "qa" / "assets" / "cleanup-attempts.ttf"
REPORT = ROOT / "qa" / "assets" / "glyph-report.csv"
CHECKPOINT = ROOT / "checkpoints" / "simplified-v1"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def points(glyph):
    return sum(len(contour) for contour in glyph.foreground)


def main():
    rows = list(csv.DictReader(REPORT.open(encoding="utf-8")))
    source_tt = TTFont(str(SOURCE), recalcBBoxes=False, recalcTimestamp=False)
    final_tt = TTFont(str(FINAL), recalcBBoxes=False, recalcTimestamp=False)
    review_tt = TTFont(str(REVIEW), recalcBBoxes=False, recalcTimestamp=False)
    cleanup_tt = TTFont(str(CLEANUP), recalcBBoxes=False, recalcTimestamp=False)
    source = fontforge.open(str(SOURCE))
    final = fontforge.open(str(FINAL))
    review = fontforge.open(str(REVIEW))
    cleanup = fontforge.open(str(CLEANUP))
    source_order = [glyph.glyphname for glyph in source.glyphs()]
    final_order = [glyph.glyphname for glyph in final.glyphs()]
    review_order = [glyph.glyphname for glyph in review.glyphs()]
    cleanup_order = [glyph.glyphname for glyph in cleanup.glyphs()]
    errors = []
    manifest_path = CHECKPOINT / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if sha256(SOURCE).lower() != manifest["source"]["sha256"].lower():
            errors.append("checkpoint source hash differs")
        for filename, expected in manifest["files"].items():
            path = CHECKPOINT / filename
            if not path.exists() or sha256(path).lower() != expected.lower():
                errors.append("checkpoint hash differs: {}".format(filename))
    if source_order != final_order or source_order != review_order or source_order != cleanup_order:
        errors.append("glyph order differs")
    if len(rows) != len(source_order):
        errors.append("report row count differs from glyph count")
    if source_tt["hmtx"].metrics != final_tt["hmtx"].metrics:
        errors.append("production hmtx metrics differ")
    if source_tt["hmtx"].metrics != review_tt["hmtx"].metrics:
        errors.append("review hmtx metrics differ")
    if source_tt["hmtx"].metrics != cleanup_tt["hmtx"].metrics:
        errors.append("cleanup hmtx metrics differ")
    for row in rows:
        name = row["glyph"]
        before, after = source[name], final[name]
        if before.unicode != after.unicode:
            errors.append("{} Unicode changed".format(name))
        if before.width != after.width:
            errors.append("{} advance width changed".format(name))
        actual = points(after)
        if row["final_points"] and actual != int(row["final_points"]):
            errors.append("{} final point count {} != {}".format(name, actual, row["final_points"]))
        if row["rebuild_status"] in ("rebuilt-accepted", "overlap-cleaned") and actual > 80:
            errors.append("{} accepted with {} points".format(name, actual))
        if row.get("quality_action") == "accept-rebuilt":
            gates = (
                float(row["mse"]) <= 0.018,
                float(row["ink_iou"]) >= 0.90,
                float(row["false_positive_ink"]) <= 0.075,
                float(row["false_negative_ink"]) <= 0.075,
                int(row["component_delta"]) == 0,
                int(row["counter_delta"]) == 0,
            )
            if not all(gates):
                errors.append("{} accepted while failing a quality gate".format(name))
        if row.get("candidate_points"):
            candidate_actual = points(review[name])
            if candidate_actual != int(row["candidate_points"]):
                errors.append("{} candidate point count {} != {}".format(name, candidate_actual, row["candidate_points"]))
    print("glyphs={}; errors={}".format(len(rows), len(errors)))
    for error in errors[:50]:
        print(error)
    source.close()
    final.close()
    source_tt.close()
    final_tt.close()
    review_tt.close()
    cleanup_tt.close()
    try:
        review.close()
    except RuntimeError:
        pass
    try:
        cleanup.close()
    except RuntimeError:
        pass
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
