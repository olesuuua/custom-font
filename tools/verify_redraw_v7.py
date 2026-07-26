#!/usr/bin/env ffpython
"""Verify redraw-v7 scope, metrics, structure, point types, and review state."""

import csv
import json
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".font-deps"))
sys.path.insert(0, str(ROOT / "tools"))
from fontTools.ttLib import TTFont

import redraw_font as rf
import repair_redraw_v4 as v4
import repair_redraw_v7 as v7


def points(glyph):
    return sum(len(contour) for contour in glyph.foreground)


def layer_from_contours(contours):
    layer = fontforge.layer()
    layer.is_quadratic = False
    for contour in contours:
        layer += contour.dup()
    return layer


def normalized_signature(layer):
    points = [point for contour in layer for point in contour]
    left = min(point.x for point in points)
    right = max(point.x for point in points)
    bottom = min(point.y for point in points)
    top = max(point.y for point in points)
    width = max(1.0, right - left)
    height = max(1.0, top - bottom)
    return tuple(tuple(
        (round((point.x - left) / width, 2),
         round((point.y - bottom) / height, 2),
         bool(point.on_curve), int(point.type))
        for point in contour
    ) for contour in layer)


def main():
    errors = []
    with (v7.V7 / "redraw-report.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        rows = {row["glyph"]: row for row in csv.DictReader(handle)}
    decisions = json.loads(
        (v7.V7 / "redraw-manual-decisions.json").read_text(encoding="utf-8")
    )["decisions"]
    manifest = json.loads((v7.V7 / "manifest.json").read_text(encoding="utf-8"))
    base = fontforge.open(str(v7.V6 / "redrawn.sfd"))
    output = fontforge.open(str(v7.V7 / "redrawn.sfd"))
    source = fontforge.open(str(ROOT / "fontforge" / "original.sfd"))
    base_tt = TTFont(str(v7.V6 / "redrawn.ttf"), recalcBBoxes=False, recalcTimestamp=False)
    output_tt = TTFont(str(v7.V7 / "redrawn.ttf"), recalcBBoxes=False, recalcTimestamp=False)
    changed = set()
    try:
        if len(rows) != 334:
            errors.append("glyph count differs")
        if base_tt.getGlyphOrder() != output_tt.getGlyphOrder():
            errors.append("glyph order differs")
        if base_tt.getBestCmap() != output_tt.getBestCmap():
            errors.append("cmap differs")
        base_metrics = base_tt["hmtx"].metrics
        output_metrics = output_tt["hmtx"].metrics
        metric_changes = {
            name for name in base_metrics if base_metrics[name] != output_metrics[name]
        }
        if metric_changes != {"uni0414"}:
            errors.append("unexpected hmtx changes: " + ",".join(sorted(metric_changes)))
        if output_metrics["uni0414"] != output_metrics["D"]:
            errors.append("uni0414 metrics do not match D")
        if output["uni0414"].width != output["D"].width:
            errors.append("uni0414 SFD width does not match D")
        if rf.layer_hash(output["uni0414"].foreground) != rf.layer_hash(output["D"].foreground):
            errors.append("uni0414 outline does not exactly match D")

        for name, row in rows.items():
            differs = (
                rf.layer_hash(base[name].foreground)
                != rf.layer_hash(output[name].foreground)
            )
            if differs:
                changed.add(name)
            if differs and name not in v7.TARGETS:
                errors.append(name + " changed outside v7 targets")
            if points(output[name]) != int(row["redrawn_points"]):
                errors.append(name + " point count/report mismatch")
            if rf.layer_hash(output[name].foreground) != row["candidate_hash"]:
                errors.append(name + " hash mismatch")
            if name not in v7.TARGETS:
                continue
            if points(output[name]) > v7.CEILING:
                errors.append(name + " exceeds point ceiling")
            reference_sets = (
                v4.point_sets(output["D"].foreground)
                if name == "uni0414"
                else v4.point_sets(source[name].foreground)
            )
            candidate = v7.evaluate(
                reference_sets, output[name].foreground.dup(), "verify",
                normalize=False,
            )
            if name != "uni0414" and not v7.structurally_clean(candidate):
                errors.append(name + " is structurally invalid")
            expected = "pass" if name == "uni0414" else "needs_rework"
            decision = decisions.get(name, {})
            if (
                decision.get("status") != expected
                or decision.get("source_hash") != row["source_hash"]
                or decision.get("candidate_hash") != row["candidate_hash"]
            ):
                errors.append(name + " review decision mismatch")
            before = rf.point_type_counts(base[name].foreground)
            after = rf.point_type_counts(output[name].foreground)
            if name != "uni0414":
                if after["corner"] >= before["corner"]:
                    errors.append(name + " did not reduce serialized corner points")
                if after["curve"] + after["hvcurve"] <= before["curve"] + before["hvcurve"]:
                    errors.append(name + " did not increase serialized curve points")
            if row.get("changed_in_v7") != "true":
                errors.append(name + " missing v7 marker")

        if changed != v7.TARGETS:
            errors.append("changed glyph set differs from v7 targets")
        if set(manifest.get("changed_glyphs", [])) != v7.TARGETS:
            errors.append("manifest changed glyph set differs")

        bars = []
        eights = []
        for name in sorted(v7.FRACTIONS):
            _numerator, bar, denominator = v7.split_fraction(output[name].foreground)
            denominator.sort(key=lambda contour: (
                -(v7.contour_bbox(contour)[2] - v7.contour_bbox(contour)[0])
                * (v7.contour_bbox(contour)[3] - v7.contour_bbox(contour)[1])
            ))
            denominator = [denominator[0]] + sorted(
                denominator[1:],
                key=lambda contour: sum(v7.contour_bbox(contour)[1::2]) / 2.0,
            )
            bars.append(normalized_signature(layer_from_contours([bar])))
            signature = normalized_signature(layer_from_contours(denominator))
            signature = tuple(
                tuple(point[:2] for point in contour) for contour in signature
            )
            eights.append(tuple(
                min(values[index:] + values[:index]
                    for values in (contour, tuple(reversed(contour)))
                    for index in range(len(values)))
                for contour in signature
            ))
            for counter in denominator[1:]:
                counts = rf.point_type_counts(layer_from_contours([counter]))
                if counts["corner"]:
                    errors.append(name + " denominator counter contains corner points")
        if len(set(bars)) != 1:
            errors.append("fraction bars are not identical")
        if len(set(eights)) != 1:
            errors.append("denominator eights are not identical")
    finally:
        base.close()
        output.close()
        source.close()
        base_tt.close()
        output_tt.close()
    print("glyphs=334; changed={}; errors={}".format(len(changed), len(errors)))
    for error in errors:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
