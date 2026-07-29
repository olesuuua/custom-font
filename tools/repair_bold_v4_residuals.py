#!/usr/bin/env ffpython
"""Remove serialization residuals and repair the two colon dots."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".font-deps"), str(ROOT / "tools")]

import bold_curve_helpers as curves
import repair_bold_v4 as v4

BOLD = ROOT / "fontforge" / "bold.sfd"
REGULAR = ROOT / "fontforge" / "redrawn.sfd"
OUT = ROOT / "qa" / "assets" / "bold-v4-residuals.json"
SOLID = ("X", "uni0449", "uni2074", "uni2084")


def center(contour):
    box = contour.boundingBox()
    return (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0


def main():
    bold = fontforge.open(str(BOLD))
    regular = fontforge.open(str(REGULAR))
    rows = {}
    try:
        for name in SOLID:
            before = bold[name].foreground.dup()
            largest = v4.drop_extra_holes(v4.largest_only(before) if hasattr(v4, "largest_only") else before, 0)[0]
            contours = list(before)
            largest_contour = max(contours, key=v4.contour_area)
            layer = fontforge.layer()
            layer.is_quadratic = before.is_quadratic
            layer += largest_contour.dup()
            sets = v4.layer_sets(layer, error=.35, spacing=1.5)
            candidate = curves.curve_layer(
                sets, (20,), tension=.27, corner_angle=48.0
            )
            try:
                candidate = v4.scratch_cleanup(bold, name, candidate, .5)
            except Exception:
                pass
            if v4.structural(candidate)["intersections"]:
                candidate = curves.curve_layer(
                    sets, (16,), tension=.25, corner_angle=52.0
                )
            bold[name].foreground = candidate
            metrics = v4.comparison(before, candidate)
            rows[name] = {
                "status": "ok",
                "method": "largest-solid-contour-geometric-refit",
                "old_points": v4.point_count(before),
                "new_points": v4.point_count(candidate),
                "iou_64": metrics[64]["iou"],
                "iou_128": metrics[128]["iou"],
            }

        name = "colon"
        before = bold[name].foreground.dup()
        current = list(before)
        regular_contours = list(regular[name].foreground)
        available = set(range(len(regular_contours)))
        replacements = {}
        dots = []
        for index, contour in enumerate(current):
            if sum(point.on_curve for point in contour) != 4:
                continue
            current_center = center(contour)
            choices = [
                (
                    math.hypot(
                        center(regular_contours[j])[0] - current_center[0],
                        center(regular_contours[j])[1] - current_center[1],
                    ),
                    j,
                )
                for j in available
            ]
            _distance, match = min(choices)
            available.remove(match)
            target = v4.raster_centroid(regular_contours[match])
            box = contour.boundingBox()
            diameter = max(box[2] - box[0], box[3] - box[1]) * .95
            replacements[index] = v4.ellipse(target[0], target[1], diameter)
            dots.append({
                "center_x": target[0], "center_y": target[1],
                "new_diameter": diameter,
            })
        result = fontforge.layer()
        result.is_quadratic = True
        for index, contour in enumerate(current):
            result += replacements.get(index, contour).dup()
        bold[name].foreground = result
        metrics = v4.comparison(before, result)
        rows[name] = {
            "status": "ok",
            "method": "regular-ink-centroid-four-anchor-dot-95pct",
            "dots": dots,
            "old_points": v4.point_count(before),
            "new_points": v4.point_count(result),
            "iou_64": metrics[64]["iou"],
            "iou_128": metrics[128]["iou"],
        }
        bold.save(str(BOLD))
    finally:
        bold.close()
        regular.close()
    payload = {"version": "bold-v4-residuals", "glyphs": rows}
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"changed": len(rows)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
