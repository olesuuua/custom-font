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
import simplify_font as sf
SOURCE = ROOT / "qa" / "assets" / "original.ttf"
FINAL = ROOT / "qa" / "assets" / "simplified.ttf"
REVIEW = ROOT / "qa" / "assets" / "review-candidates.ttf"
CLEANUP = ROOT / "qa" / "assets" / "cleanup-attempts.ttf"
REPORT = ROOT / "qa" / "assets" / "glyph-report.csv"
CHECKPOINT = ROOT / "checkpoints" / "simplified-v126"
CURVATURE = ROOT / "qa" / "assets" / "curvature-candidates.ttf"
CURVATURE_SFD = ROOT / "fontforge" / "curvature-candidates.sfd"
CURVATURE_REPORT = ROOT / "qa" / "assets" / "curvature-report.csv"
CURVATURE_REVIEW = ROOT / "qa" / "assets" / "curvature-review.ttf"
CURVATURE_REVIEW_SFD = ROOT / "fontforge" / "curvature-review.sfd"
CURVATURE_V1 = ROOT / "checkpoints" / "curvature-v1"
COMPLETE_STATUSES = {
    "safe-restored-full", "safe-restored-local", "already-source-curved",
}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def points(glyph):
    return sum(len(contour) for contour in glyph.foreground)


def off_curve_points(glyph):
    return sum(1 for contour in glyph.foreground for point in contour if not point.on_curve)


def invalid_quadratic_handles(glyph):
    invalid = 0
    for contour in glyph.foreground:
        values = list(contour)
        for index, control in enumerate(values):
            if control.on_curve:
                continue
            previous = values[(index - 1) % len(values)]
            following = values[(index + 1) % len(values)]
            if not previous.on_curve or not following.on_curve:
                invalid += 1
                continue
            chord_x = following.x - previous.x
            chord_y = following.y - previous.y
            chord_square = chord_x * chord_x + chord_y * chord_y
            if chord_square <= 1e-9:
                invalid += 1
                continue
            left = ((control.x - previous.x) * chord_x + (control.y - previous.y) * chord_y) / chord_square
            right = ((following.x - control.x) * chord_x + (following.y - control.y) * chord_y) / chord_square
            if left <= 0.02 or right <= 0.02:
                invalid += 1
    return invalid


def standard_gate(row, prefix=""):
    field = lambda name: row[prefix + name]
    return (
        float(field("mse")) <= sf.MAX_MSE
        and float(field("ink_iou")) >= sf.MIN_INK_IOU
        and float(field("false_positive_ink")) <= sf.MAX_FALSE_POSITIVE_INK
        and float(field("false_negative_ink")) <= sf.MAX_FALSE_NEGATIVE_INK
        and int(field("component_delta")) == 0
        and int(field("counter_delta")) == 0
    )


def hard_budget_gate(row, prefix=""):
    field = lambda name: row[prefix + name]
    return (
        float(field("mse")) <= sf.HARD_BUDGET_MAX_MSE
        and float(field("ink_iou")) >= sf.HARD_BUDGET_MIN_INK_IOU
        and float(field("false_positive_ink")) <= sf.HARD_BUDGET_MAX_FALSE_POSITIVE_INK
        and float(field("false_negative_ink")) <= sf.HARD_BUDGET_MAX_FALSE_NEGATIVE_INK
        and int(field("component_delta")) == 0
        and int(field("counter_delta")) == 0
    )


def outline_signature(glyph):
    return tuple(
        tuple((round(point.x, 4), round(point.y, 4), bool(point.on_curve)) for point in contour)
        for contour in glyph.foreground
    )


def main():
    rows = list(csv.DictReader(REPORT.open(encoding="utf-8")))
    source_tt = TTFont(str(SOURCE), recalcBBoxes=False, recalcTimestamp=False)
    final_tt = TTFont(str(FINAL), recalcBBoxes=False, recalcTimestamp=False)
    review_tt = TTFont(str(REVIEW), recalcBBoxes=False, recalcTimestamp=False)
    cleanup_tt = TTFont(str(CLEANUP), recalcBBoxes=False, recalcTimestamp=False)
    curvature_tt = TTFont(str(CURVATURE), recalcBBoxes=False, recalcTimestamp=False)
    curvature_review_tt = TTFont(str(CURVATURE_REVIEW), recalcBBoxes=False, recalcTimestamp=False)
    source = fontforge.open(str(SOURCE))
    final = fontforge.open(str(FINAL))
    frozen_sfd = fontforge.open(str(CHECKPOINT / "simplified.sfd"))
    review = fontforge.open(str(REVIEW))
    cleanup = fontforge.open(str(CLEANUP))
    curvature = fontforge.open(str(CURVATURE))
    curvature_sfd = fontforge.open(str(CURVATURE_SFD))
    curvature_review = fontforge.open(str(CURVATURE_REVIEW))
    curvature_review_sfd = fontforge.open(str(CURVATURE_REVIEW_SFD))
    curvature_rows = list(csv.DictReader(CURVATURE_REPORT.open(encoding="utf-8-sig")))
    curvature_by_name = {row["glyph"]: row for row in curvature_rows}
    source_order = [glyph.glyphname for glyph in source.glyphs()]
    final_order = [glyph.glyphname for glyph in final.glyphs()]
    review_order = [glyph.glyphname for glyph in review.glyphs()]
    cleanup_order = [glyph.glyphname for glyph in cleanup.glyphs()]
    curvature_order = [glyph.glyphname for glyph in curvature.glyphs()]
    curvature_review_order = [glyph.glyphname for glyph in curvature_review.glyphs()]
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
    curvature_v1_manifest = CURVATURE_V1 / "manifest.json"
    if not curvature_v1_manifest.exists():
        errors.append("curvature-v1 archive manifest is missing")
    else:
        manifest = json.loads(curvature_v1_manifest.read_text(encoding="utf-8"))
        for filename, expected in manifest.get("files", {}).items():
            path = CURVATURE_V1 / filename
            if not path.exists() or sha256(path).lower() != expected.lower():
                errors.append("curvature-v1 archive hash differs: {}".format(filename))
    if source_order != final_order or source_order != review_order or source_order != cleanup_order:
        errors.append("glyph order differs")
    if source_order != curvature_order or source_order != curvature_review_order:
        errors.append("curvature glyph order differs")
    if len(rows) != len(source_order):
        errors.append("report row count differs from glyph count")
    if source_tt["hmtx"].metrics != final_tt["hmtx"].metrics:
        errors.append("production hmtx metrics differ")
    if source_tt["hmtx"].metrics != review_tt["hmtx"].metrics:
        errors.append("review hmtx metrics differ")
    if source_tt["hmtx"].metrics != cleanup_tt["hmtx"].metrics:
        errors.append("cleanup hmtx metrics differ")
    if source_tt["hmtx"].metrics != curvature_tt["hmtx"].metrics:
        errors.append("curvature hmtx metrics differ")
    if source_tt["hmtx"].metrics != curvature_review_tt["hmtx"].metrics:
        errors.append("curvature review hmtx metrics differ")
    source_cmap = source_tt.getBestCmap()
    if source_cmap != curvature_tt.getBestCmap() or source_cmap != curvature_review_tt.getBestCmap():
        errors.append("curvature Unicode mappings differ")
    if len(curvature_rows) != len(source_order):
        errors.append("curvature report row count differs from glyph count")
    targeted_rows = [row for row in curvature_rows if row.get("curvature_targeted") == "true"]
    if len(targeted_rows) != 213:
        errors.append("expected 213 curvature targets, found {}".format(len(targeted_rows)))
    for current, frozen in (
        (FINAL, CHECKPOINT / "simplified.ttf"),
        (ROOT / "fontforge" / "simplified.sfd", CHECKPOINT / "simplified.sfd"),
        (REPORT, CHECKPOINT / "glyph-report.csv"),
    ):
        if sha256(current) != sha256(frozen):
            errors.append("v126 production baseline changed: {}".format(current.name))
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
            if not (
                standard_gate(row)
                or hard_budget_gate(row)
                or (
                    name in sf.EMERGENCY_BUDGET_GLYPHS
                    and actual <= sf.MAX_POINTS
                    and int(row["component_delta"]) == 0
                    and int(row["counter_delta"]) == 0
                )
            ):
                errors.append("{} accepted while failing a quality gate".format(name))
        if row.get("candidate_points"):
            candidate_actual = points(review[name])
            if candidate_actual != int(row["candidate_points"]):
                errors.append("{} candidate point count {} != {}".format(name, candidate_actual, row["candidate_points"]))
        curvature_row = curvature_by_name.get(name)
        if not curvature_row:
            errors.append("{} missing from curvature report".format(name))
            continue
        curvature_actual = points(curvature[name])
        raw_actual = points(curvature_review[name])
        if curvature_actual > sf.MAX_POINTS:
            errors.append("{} curvature candidate has {} points".format(name, curvature_actual))
        if curvature_actual != int(curvature_row["curvature_points"]):
            errors.append("{} curvature point count {} != {}".format(name, curvature_actual, curvature_row["curvature_points"]))
        if raw_actual > sf.MAX_POINTS:
            errors.append("{} raw curvature review has {} points".format(name, raw_actual))
        if raw_actual != int(curvature_row["raw_points"]):
            errors.append("{} raw point count {} != {}".format(name, raw_actual, curvature_row["raw_points"]))
        if outline_signature(curvature_sfd[name]) != outline_signature(curvature_review_sfd[name]):
            errors.append("{} safe review outline differs from raw diagnostic outline".format(name))
        safe_intersections = sum(1 for contour in curvature[name].foreground if contour.selfIntersects())
        baseline_intersections = sum(1 for contour in final[name].foreground if contour.selfIntersects())
        if safe_intersections > baseline_intersections:
            errors.append("{} safe curvature introduced a self-intersection".format(name))
        if len(curvature_sfd[name].foreground) != len(frozen_sfd[name].foreground):
            errors.append("{} curvature contour count differs from v126".format(name))
        if curvature_row["curvature_targeted"] != "true":
            if outline_signature(curvature[name]) != outline_signature(after):
                errors.append("{} non-target curvature outline changed".format(name))
        else:
            status = curvature_row.get("complete_status", "")
            if status not in COMPLETE_STATUSES:
                errors.append("{} has unresolved complete status {}".format(name, status or "<empty>"))
            if off_curve_points(curvature_sfd[name]) <= 0:
                errors.append("{} complete restoration has no off-curve points".format(name))
            if int(curvature_row.get("safe_off_curve_points") or 0) <= 0:
                errors.append("{} report has no safe off-curve points".format(name))
            if curvature_row.get("handle_valid") != "true":
                errors.append("{} reports invalid handles".format(name))
            if curvature_row.get("complete_audit_status") != "pass":
                errors.append("{} did not pass the complete high-resolution audit".format(name))
            if status != "already-source-curved" and int(curvature_row.get("represented_curve_runs") or 0) <= 0:
                errors.append("{} introduced no source-supported curve run".format(name))
            if status != "already-source-curved" and float(
                    curvature_row.get("curvature_source_fit_improvement") or 0) < 0.20:
                errors.append("{} source-fit improvement is below 20%".format(name))
            if status != "already-source-curved" and invalid_quadratic_handles(curvature_sfd[name]):
                errors.append("{} has a reversed or invalid quadratic handle".format(name))
            if curvature_row.get("complete_mapping_mode") not in (
                "direct-contour", "visible-boundary", "preserved-valid",
            ):
                errors.append("{} has invalid mapping mode {}".format(
                    name, curvature_row.get("complete_mapping_mode", "<empty>")))
    print("glyphs={}; errors={}".format(len(rows), len(errors)))
    for error in errors[:50]:
        print(error)
    source.close()
    final.close()
    source_tt.close()
    final_tt.close()
    review_tt.close()
    cleanup_tt.close()
    curvature_tt.close()
    curvature_review_tt.close()
    try:
        review.close()
    except RuntimeError:
        pass
    try:
        cleanup.close()
    except RuntimeError:
        pass
    for font in (curvature, curvature_sfd, curvature_review, curvature_review_sfd, frozen_sfd):
        try:
            font.close()
        except RuntimeError:
            pass
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
