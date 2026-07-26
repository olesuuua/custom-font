#!/usr/bin/env python3
"""Verify cubic redraw artifacts, strict gates, metadata, and manual overrides."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import fontforge


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".font-deps"))
from fontTools.ttLib import TTFont

import redraw_font as rf


def points(glyph):
    return sum(len(contour) for contour in glyph.foreground)


def main():
    required = [rf.OUTPUT_SFD, rf.OUTPUT_TTF, rf.REVIEW_SFD, rf.REVIEW_TTF, rf.REPORT, rf.DECISIONS]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError("missing redraw artifacts: {}".format(" ".join(missing)))
    with rf.REPORT.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    decisions = json.loads(rf.DECISIONS.read_text(encoding="utf-8"))
    checkpoint = ROOT / "checkpoints" / "redraw-v3"
    reviewed_checkpoint = ROOT / "checkpoints" / "redraw-reviewed-v2"
    manifest_path = checkpoint / "manifest.json"
    source_tt = TTFont(str(rf.SOURCE_TTF), recalcBBoxes=False, recalcTimestamp=False)
    output_tt = TTFont(str(rf.OUTPUT_TTF), recalcBBoxes=False, recalcTimestamp=False)
    source = fontforge.open(str(rf.SOURCE_SFD))
    original = fontforge.open(str(rf.SOURCE_TTF))
    output = fontforge.open(str(rf.OUTPUT_SFD))
    review = fontforge.open(str(rf.REVIEW_SFD))
    errors = []
    try:
        if not manifest_path.exists():
            errors.append("checkpoint manifest is missing")
        else:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for filename, expected in manifest.get("artifact_hashes", {}).items():
                path = checkpoint / filename
                if not path.exists() or rf.sha256(path) != expected:
                    errors.append("checkpoint hash differs: {}".format(filename))
            reviewed_manifest = json.loads((reviewed_checkpoint / "manifest.json").read_text(encoding="utf-8"))
            reviewed_font = fontforge.open(str(reviewed_checkpoint / "redrawn.sfd"))
            try:
                for name, lock in reviewed_manifest["locked_glyphs"].items():
                    if rf.layer_hash(output[name].foreground) != lock["candidate_hash"]:
                        errors.append("{} locked outline changed".format(name))
                    if rf.layer_hash(reviewed_font[name].foreground) != lock["candidate_hash"]:
                        errors.append("{} reviewed-v2 lock hash differs".format(name))
            finally:
                reviewed_font.close()
        order = source_tt.getGlyphOrder()
        if output_tt.getGlyphOrder() != order:
            errors.append("glyph order differs")
        if source_tt.getBestCmap() != output_tt.getBestCmap():
            errors.append("Unicode cmap differs")
        if source_tt["hmtx"].metrics != output_tt["hmtx"].metrics:
            errors.append("horizontal metrics differ")
        if len(rows) != len(order):
            errors.append("report has {} rows; expected {}".format(len(rows), len(order)))
        by_name = {row["glyph"]: row for row in rows}
        source_names = {glyph.glyphname for glyph in source.glyphs()}
        for name in order:
            if name not in by_name:
                errors.append("{} missing report row".format(name))
                continue
            row = by_name[name]
            glyph = output[name]
            actual = points(glyph)
            if actual != int(row["redrawn_points"] or 0):
                errors.append("{} point count differs".format(name))
            if actual > rf.MAX_POINTS:
                errors.append("{} exceeds 100 points".format(name))
            if row["simple"] == "true" and actual > rf.SIMPLE_MAX_POINTS:
                errors.append("{} simple glyph is not below 50".format(name))
            if rf.layer_hash(glyph.foreground) != row["candidate_hash"]:
                errors.append("{} candidate hash differs".format(name))
            reference = source[name] if name in source_names else original[name]
            if rf.layer_hash(reference.foreground) != row["source_hash"]:
                errors.append("{} source hash differs".format(name))
            if rf.layer_hash(review[name].foreground) != row["candidate_hash"]:
                errors.append("{} review outline differs".format(name))
            if row["automatic_pass"] == "true" and int(row["source_points"] or 0) > 0:
                for size in rf.RASTER_SIZES:
                    if float(row["mse_{}".format(size)]) > rf.MAX_MSE:
                        errors.append("{} auto-pass MSE failure at {}".format(name, size))
                    if float(row["ink_iou_{}".format(size)]) < rf.MIN_IOU:
                        errors.append("{} auto-pass IoU failure at {}".format(name, size))
                    if float(row["false_positive_ink_{}".format(size)]) > rf.MAX_FALSE_INK:
                        errors.append("{} auto-pass extra ink at {}".format(name, size))
                    if float(row["false_negative_ink_{}".format(size)]) > rf.MAX_FALSE_INK:
                        errors.append("{} auto-pass missing ink at {}".format(name, size))
                if row["failure_reasons"]:
                    errors.append("{} auto-pass has failure reasons".format(name))
            if row["effective_status"] == "manual-pass":
                forbidden = {
                    reason for reason in row["failure_reasons"].split(";")
                    if reason.startswith("topology-") or reason in (
                        "point-budget", "self-intersection", "invalid-handles"
                    )
                }
                if forbidden:
                    errors.append("{} manual pass overrides structural failure".format(name))
            if row.get("changed_in_v3") == "true":
                decision = decisions.get("decisions", {}).get(name, {})
                if decision.get("status") != "needs_rework":
                    errors.append("{} changed candidate was not returned to needs_rework".format(name))
                if decision.get("candidate_hash") != row["candidate_hash"]:
                    errors.append("{} changed decision hash is stale".format(name))
        known = set(by_name)
        for name, decision in decisions.get("decisions", {}).items():
            if name not in known:
                errors.append("unknown manual decision {}".format(name))
            if decision.get("status") not in ("pass", "almost_done", "needs_rework"):
                errors.append("{} has invalid manual status".format(name))
        for name in ("uni20A6",):
            row = by_name[name]
            if int(row["self_intersections"] or 0) or row["topology_status"] != "match":
                errors.append("{} still has a structural failure".format(name))
            if int(row["redrawn_points"] or 0) > rf.MAX_POINTS:
                errors.append("{} exceeds point ceiling".format(name))
        reviewed_rows = {}
        with (reviewed_checkpoint / "redraw-report.csv").open(encoding="utf-8-sig", newline="") as handle:
            reviewed_rows = {row["glyph"]: row for row in csv.DictReader(handle)}
        for name, target in {
            "quoteleft": 1, "quoteright": 1, "quotesinglbase": 1,
            "quotereversed": 1, "quotedblbase": 2,
        }.items():
            if int(by_name[name]["redrawn_contours"] or 0) != target:
                errors.append("{} does not have required {} contour(s)".format(name, target))
        for name in ("uni2078", "uni2088"):
            row = by_name[name]
            iou = float(row.get("raw_ink_iou_128") or row["ink_iou_128"])
            missing = float(row.get("raw_false_negative_ink_128") or row["false_negative_ink_128"])
            if iou <= .601 or iou < .75:
                errors.append("{} did not materially improve IoU".format(name))
            if missing >= .20:
                errors.append("{} still has excessive missing ink".format(name))
            if float(row.get("envelope_delta") or 1) > .05:
                errors.append("{} envelope differs by more than 5%".format(name))
        arrow = by_name["arrowdblright"]
        if arrow.get("donor") != "arrowdblleft" or "mirror-x" not in arrow.get("repair_method", ""):
            errors.append("arrowdblright is not the required arrowdblleft mirror")
        if arrow.get("current_review_status") != "needs_rework":
            errors.append("arrowdblright is not queued for manual review")
        for name, old in reviewed_rows.items():
            if old.get("current_review_status") == "almost_done" and by_name[name].get("changed_in_v3") != "true":
                if by_name[name].get("current_review_status") != "almost_done":
                    errors.append("{} unchanged almost-done status was lost".format(name))
    finally:
        source.close(); original.close(); output.close(); review.close()
        source_tt.close(); output_tt.close()
    print("glyphs={}; errors={}".format(len(rows), len(errors)))
    for error in errors[:100]:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
