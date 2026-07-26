#!/usr/bin/env ffpython
"""Verify redraw-v8 scope, serialized curves, spacing, components, and metadata."""

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
import repair_redraw_v8 as v8


def points(glyph):
    return sum(len(contour) for contour in glyph.foreground)


def normalized_geometry(layer):
    values = [point for contour in layer for point in contour]
    left = min(point.x for point in values)
    right = max(point.x for point in values)
    bottom = min(point.y for point in values)
    top = max(point.y for point in values)
    width = max(1.0, right - left)
    height = max(1.0, top - bottom)
    contours = []
    for contour in layer:
        sequence = tuple(
            (round((point.x - left) / width, 3),
             round((point.y - bottom) / height, 3),
             bool(point.on_curve))
            for point in contour
        )
        variants = (
            sequence,
            tuple(reversed(sequence)),
        )
        contours.append(min(
            values[index:] + values[:index]
            for values in variants
            for index in range(len(values))
        ))
    return tuple(contours)


def contour_hash(contours):
    return rf.layer_hash(v8.contour_layer(contours))


def main():
    errors = []
    with (v8.V8 / "redraw-report.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        rows = {row["glyph"]: row for row in csv.DictReader(handle)}
    decisions = json.loads(
        (v8.V8 / "redraw-manual-decisions.json").read_text(encoding="utf-8")
    )["decisions"]
    manifest = json.loads((v8.V8 / "manifest.json").read_text(encoding="utf-8"))
    base = fontforge.open(str(v8.V7 / "redrawn.sfd"))
    output = fontforge.open(str(v8.V8 / "redrawn.sfd"))
    source = fontforge.open(str(ROOT / "fontforge" / "original.sfd"))
    base_tt = TTFont(str(v8.V7 / "redrawn.ttf"), recalcBBoxes=False, recalcTimestamp=False)
    output_tt = TTFont(str(v8.V8 / "redrawn.ttf"), recalcBBoxes=False, recalcTimestamp=False)
    changed = set()
    try:
        if len(rows) != 334:
            errors.append("glyph count differs")
        if base_tt.getGlyphOrder() != output_tt.getGlyphOrder():
            errors.append("glyph order differs")
        if base_tt.getBestCmap() != output_tt.getBestCmap():
            errors.append("cmap differs")
        if base_tt["hmtx"].metrics != output_tt["hmtx"].metrics:
            errors.append("horizontal metrics differ")
        for tag in ("kern", "GDEF", "GPOS", "GSUB"):
            if (tag in base_tt) != (tag in output_tt):
                errors.append(tag + " presence differs")
            elif tag in base_tt and base_tt[tag].compile(base_tt) != output_tt[tag].compile(output_tt):
                errors.append(tag + " differs")

        for name, row in rows.items():
            differs = (
                rf.layer_hash(base[name].foreground)
                != rf.layer_hash(output[name].foreground)
            )
            if differs:
                changed.add(name)
            if differs and name not in v8.TARGETS:
                errors.append(name + " changed outside v8 targets")
            if points(output[name]) != int(row["redrawn_points"]):
                errors.append(name + " point count/report mismatch")
            if rf.layer_hash(output[name].foreground) != row["candidate_hash"]:
                errors.append(name + " candidate hash mismatch")
            if name not in v8.TARGETS:
                continue
            if points(output[name]) > v8.CEILING:
                errors.append(name + " exceeds point ceiling")
            reference = v4.point_sets(source[name].foreground)
            candidate = v8.evaluate(
                reference, output[name].foreground.dup(), "verify",
                normalize=False,
            )
            if not v8.structurally_clean(candidate):
                errors.append(name + " is structurally invalid")
            decision = decisions.get(name, {})
            expected_status = "pass" if name == "dong" else "needs_rework"
            if (
                decision.get("status") != expected_status
                or decision.get("source_hash") != row["source_hash"]
                or decision.get("candidate_hash") != row["candidate_hash"]
            ):
                errors.append(name + " review decision mismatch")
            before = rf.point_type_counts(base[name].foreground)
            after = rf.point_type_counts(output[name].foreground)
            if after["curve"] + after["hvcurve"] <= before["curve"] + before["hvcurve"]:
                errors.append(name + " did not increase serialized curve anchors")
            if after["corner"] >= before["corner"]:
                errors.append(name + " did not reduce serialized corner anchors")
            on_curve = after["corner"] + after["curve"] + after["hvcurve"] + after["tangent"]
            if int(row.get("on_curve_points") or -1) != on_curve:
                errors.append(name + " on-curve report mismatch")
            if int(row.get("control_points") or -1) != after["off"]:
                errors.append(name + " control-point report mismatch")
            if row.get("changed_in_v8") != "true":
                errors.append(name + " missing v8 marker")
            smooth_ratio = v8.spacing_ratio(output[name].foreground, curved_only=True)
            if smooth_ratio > 2.0:
                errors.append(name + " smooth-run spacing ratio exceeds 2.0")
            if abs(float(row.get("v8_spacing_ratio") or 0.0) - smooth_ratio) > 1e-5:
                errors.append(name + " spacing-ratio report mismatch")

        if changed != v8.TARGETS:
            errors.append("changed glyph set differs from v8 targets")
        if set(manifest.get("changed_glyphs", [])) != v8.TARGETS:
            errors.append("manifest changed glyph set differs")

        outer_signatures = []
        for name in sorted(v8.FRACTIONS):
            base_num, base_bar, base_den = v8.v7.split_fraction(base[name].foreground)
            out_num, out_bar, out_den = v8.v7.split_fraction(output[name].foreground)
            base_den = v8.sort_denominator(base_den)
            out_den = v8.sort_denominator(out_den)
            outer = v8.contour_layer([out_den[0]])
            counts = rf.point_type_counts(outer)
            if counts["corner"] or counts["curve"] + counts["hvcurve"] != 8:
                errors.append(name + " outer eight is not eight smooth curve anchors")
            ratio = v8.spacing_ratio(outer, curved_only=False)
            if ratio > 1.35:
                errors.append(name + " outer-eight spacing ratio exceeds 1.35")
            outer_signatures.append(normalized_geometry(outer))
            if contour_hash(base_den[1:]) != contour_hash(out_den[1:]):
                errors.append(name + " inner counters changed")
            if contour_hash([base_bar]) != contour_hash([out_bar]):
                errors.append(name + " fraction bar changed")
            if name != "threeeighths" and contour_hash([base_num]) != contour_hash([out_num]):
                errors.append(name + " numerator changed")
        if len(set(outer_signatures)) != 1:
            errors.append("registered outer-eight masters differ")

        base_n = base["uni041D"].foreground
        out_n = output["uni041D"].foreground
        def outside_connector_signature(layer):
            signature = []
            for contour_index, contour in enumerate(layer):
                for point in contour:
                    in_connector = 270 <= point.x <= 690 and 300 <= point.y <= 490
                    if not in_connector:
                        signature.append((
                            contour_index, round(point.x, 4), round(point.y, 4),
                            bool(point.on_curve), int(point.type),
                        ))
            return signature
        if outside_connector_signature(base_n) != outside_connector_signature(out_n):
            errors.append("uni041D changed outside middle connector")
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
