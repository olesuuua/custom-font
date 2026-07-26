#!/usr/bin/env ffpython
"""Verify redraw-v9 scope, components, corners, width metrics, and metadata."""

from __future__ import annotations

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
import repair_redraw_v8 as v8
import repair_redraw_v9 as v9


def points(glyph):
    return sum(len(contour) for contour in glyph.foreground)


def contour_hash(contours):
    return rf.layer_hash(v8.contour_layer(contours))


def normalized_geometry(layer):
    all_points = [point for contour in layer for point in contour]
    left = min(point.x for point in all_points)
    right = max(point.x for point in all_points)
    bottom = min(point.y for point in all_points)
    top = max(point.y for point in all_points)
    width = max(1.0, right - left)
    height = max(1.0, top - bottom)
    contours = []
    for contour in layer:
        sequence = tuple(
            (
                round((point.x - left) / width, 2),
                round((point.y - bottom) / height, 2),
                bool(point.on_curve),
            )
            for point in contour
        )
        variants = (sequence, tuple(reversed(sequence)))
        contours.append(min(
            values[index:] + values[:index]
            for values in variants for index in range(len(values))
        ))
    return tuple(contours)


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


def main():
    errors = []
    with (v9.V9 / "redraw-report.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        rows = {row["glyph"]: row for row in csv.DictReader(handle)}
    decisions = json.loads(
        (v9.V9 / "redraw-manual-decisions.json").read_text(encoding="utf-8")
    )["decisions"]
    manifest = json.loads((v9.V9 / "manifest.json").read_text(encoding="utf-8"))
    base = fontforge.open(str(v9.V8 / "redrawn.sfd"))
    output = fontforge.open(str(v9.V9 / "redrawn.sfd"))
    prior7 = fontforge.open(str(v9.V7 / "redrawn.sfd"))
    source = fontforge.open(str(ROOT / "fontforge" / "original.sfd"))
    base_tt = TTFont(
        str(v9.V8 / "redrawn.ttf"), recalcBBoxes=False, recalcTimestamp=False
    )
    output_tt = TTFont(
        str(v9.V9 / "redrawn.ttf"), recalcBBoxes=False, recalcTimestamp=False
    )
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
            elif (
                tag in base_tt
                and base_tt[tag].compile(base_tt)
                != output_tt[tag].compile(output_tt)
            ):
                errors.append(tag + " differs")

        for name, row in rows.items():
            differs = (
                rf.layer_hash(base[name].foreground)
                != rf.layer_hash(output[name].foreground)
            )
            if differs:
                changed.add(name)
            if differs and name not in v9.TARGETS:
                errors.append(name + " changed outside v9 targets")
            if points(output[name]) != int(row["redrawn_points"]):
                errors.append(name + " point count/report mismatch")
            if rf.layer_hash(output[name].foreground) != row["candidate_hash"]:
                errors.append(name + " candidate hash mismatch")
            if name not in v9.TARGETS:
                continue
            if points(output[name]) > v9.CEILING:
                errors.append(name + " exceeds point ceiling")
            candidate = v9.evaluate(
                source, name, output[name].foreground.dup(), "verify"
            )
            if not v8.structurally_clean(candidate):
                errors.append(name + " is structurally invalid")
            decision = decisions.get(name, {})
            expected_status = row.get("current_review_status")
            if (
                expected_status not in ("pass", "almost_done", "needs_rework")
                or decision.get("status") != expected_status
                or decision.get("source_hash") != row["source_hash"]
                or decision.get("candidate_hash") != row["candidate_hash"]
            ):
                errors.append(name + " review decision mismatch")
            counts = rf.point_type_counts(output[name].foreground)
            on_curve = sum(
                counts[key] for key in
                ("corner", "curve", "hvcurve", "tangent")
            )
            if int(row.get("on_curve_points") or -1) != on_curve:
                errors.append(name + " on-curve report mismatch")
            if int(row.get("control_points") or -1) != counts["off"]:
                errors.append(name + " control-point report mismatch")
            if row.get("changed_in_v9") != "true":
                errors.append(name + " missing v9 marker")
            cv = float(row.get("v9_width_cv") or 1.0)
            jump = float(row.get("v9_max_adjacent_width_change") or 1.0)
            if cv > .120001:
                errors.append(name + " width CV exceeds 12 percent")
            if jump > .150001:
                errors.append(name + " adjacent width change exceeds 15 percent")
            if name != "uni0401":
                baseline = v9.evaluate(
                    source, name, base[name].foreground.dup(), "baseline"
                )
                for size in (32, 64, 128):
                    if (
                        candidate["metrics"]["per_size"][size]["ink_iou"]
                        < baseline["metrics"]["per_size"][size]["ink_iou"] - .005
                    ):
                        errors.append(name + " raster regression at " + str(size))

        if changed != v9.TARGETS:
            errors.append("changed glyph set differs from v9 targets")
        if set(manifest.get("changed_glyphs", [])) != v9.TARGETS:
            errors.append("manifest changed glyph set differs")
        expected_unresolved = sorted(
            name for name, row in rows.items()
            if row.get("current_review_status")
            in ("unreviewed", "almost_done", "needs_rework")
        )
        if manifest.get("unresolved_glyphs") != expected_unresolved:
            errors.append("manifest unresolved glyph list differs")
        expected_passes = sorted(
            name for name, row in rows.items()
            if row.get("current_review_status") == "pass"
        )
        if manifest.get("manual_pass_glyphs") != expected_passes:
            errors.append("manifest manual pass glyph list differs")
        if manifest.get("manual_pass_count") != len(expected_passes):
            errors.append("manifest manual pass count differs")
        if decisions.get("dong", {}).get("status") != "pass":
            errors.append("dong pass was not preserved")
        if rf.layer_hash(base["dong"].foreground) != rf.layer_hash(
            output["dong"].foreground
        ):
            errors.append("dong outline changed")

        base_e = list(base["uni0401"].foreground)
        out_e = list(output["uni0401"].foreground)
        base_e_fixed = [
            contour for contour in base_e
            if len(contour) != max(len(value) for value in base_e)
        ]
        out_e_fixed = [
            contour for contour in out_e
            if len(contour) != max(len(value) for value in out_e)
        ]
        if contour_hash(base_e_fixed) != contour_hash(out_e_fixed):
            errors.append("uni0401 dots or inner openings changed")
        base_e_types = rf.point_type_counts(base["uni0401"].foreground)
        out_e_types = rf.point_type_counts(output["uni0401"].foreground)
        if (
            out_e_types["curve"] + out_e_types["hvcurve"]
            <= base_e_types["curve"] + base_e_types["hvcurve"]
        ):
            errors.append("uni0401 did not retain more serialized curves")

        if outside_connector_signature(
            base["uni041D"].foreground
        ) != outside_connector_signature(output["uni041D"].foreground):
            errors.append("uni041D changed outside the connector")

        for name, low, high in (
            ("uni20B4", 210.0, 390.0),
            ("uni20BA", 275.0, 465.0),
        ):
            serialized_corners = sum(
                1 for contour in output[name].foreground for point in contour
                if (
                    point.on_curve and point.type == fontforge.splineCorner
                    and low <= point.y <= high
                )
            )
            required = int(rows[name].get("v9_junction_corner_count") or 0)
            if required <= 0 or serialized_corners < required:
                errors.append(name + " lost protected junction corners")

        expected_points = {
            "oneeighth": 79, "threeeighths": 100,
            "fiveeighths": 91, "seveneighths": 97,
        }
        outer_signatures = []
        source_master = v9.source_outer_master(source)
        for name in sorted(v9.FRACTIONS):
            base_num, base_bar, base_den = v7.split_fraction(
                base[name].foreground
            )
            out_num, out_bar, out_den = v7.split_fraction(
                output[name].foreground
            )
            base_den = v8.sort_denominator(base_den)
            out_den = v8.sort_denominator(out_den)
            if points(output[name]) != expected_points[name]:
                errors.append(name + " fraction point budget differs")
            outer = v8.contour_layer([out_den[0]])
            counts = rf.point_type_counts(outer)
            if (
                point_count := sum(len(contour) for contour in outer)
            ) != 24:
                errors.append(name + " outer eight is not 24 points")
            if counts["corner"] or counts["curve"] + counts["hvcurve"] != 8:
                errors.append(name + " outer eight is not eight smooth anchors")
            outer_signatures.append(normalized_geometry(outer))
            if contour_hash(base_den[1:]) != contour_hash(out_den[1:]):
                errors.append(name + " inner counters changed")
            if len(out_bar) != 12:
                errors.append(name + " slash is not 12 points")
            bar_reference = v4.point_sets(v8.contour_layer([base_bar]))
            bar_candidate = v8.evaluate(
                bar_reference, v8.contour_layer([out_bar]),
                "slash-equivalence", normalize=False,
            )
            if bar_candidate["metrics"]["per_size"][128]["ink_iou"] < .98:
                errors.append(name + " slash visual equivalence below 98 percent")
            if name == "threeeighths":
                prior_num, _bar, _den = v7.split_fraction(
                    prior7[name].foreground
                )
                if contour_hash([prior_num]) != contour_hash([out_num]):
                    errors.append("threeeighths complete v7 numerator not restored")
            elif contour_hash([base_num]) != contour_hash([out_num]):
                errors.append(name + " numerator changed")

            registered_source = v4.registered_to_bbox(
                source_master, v8.contour_bbox(base_den[0])
            )
            reference_sets = v4.point_sets(registered_source)
            bbox = rf.sf.padded_bbox(rf.sf.bbox_of_sets(reference_sets))
            before_p95, _before_max = rf.boundary_distances(
                reference_sets,
                rf.metric_layer_point_sets(
                    v8.contour_layer([base_den[0]])
                ),
                bbox,
            )
            after_p95, _after_max = rf.boundary_distances(
                reference_sets, rf.metric_layer_point_sets(outer), bbox
            )
            if after_p95 >= before_p95:
                errors.append(name + " outer eight did not move toward source")
        if len(set(outer_signatures)) != 1:
            errors.append("registered outer-eight masters differ")
    finally:
        base.close()
        output.close()
        prior7.close()
        source.close()
        base_tt.close()
        output_tt.close()

    print("glyphs=334; changed={}; errors={}".format(len(changed), len(errors)))
    for error in errors:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
