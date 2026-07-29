#!/usr/bin/env ffpython
"""Resolve remaining counters by geometric contour containment."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".font-deps"), str(ROOT / "tools")]

import bold_curve_helpers as curves
import repair_bold_v4 as v4

BOLD = ROOT / "fontforge" / "bold.sfd"
REGULAR = ROOT / "fontforge" / "redrawn.sfd"
INPUT = ROOT / "qa" / "assets" / "bold-v4-white-union.json"
WORK = ROOT / "qa" / "bold-v4-white-union-work"
OUT = ROOT / "qa" / "assets" / "bold-v4-counter-resolution.json"


def point_in_polygon(point, polygon):
    x, y = point
    inside = False
    for index, left in enumerate(polygon):
        right = polygon[(index + 1) % len(polygon)]
        if ((left[1] > y) != (right[1] > y)):
            crossing = (
                (right[0] - left[0]) * (y - left[1])
                / max(1e-12, right[1] - left[1]) + left[0]
            )
            if x < crossing:
                inside = not inside
    return inside


def contour_points(contour):
    sets = v4.layer_sets(contour, error=.35, spacing=2.0)
    return sets[0] if sets else [(point.x, point.y) for point in contour if point.on_curve]


def contained_indices(layer):
    contours = list(layer)
    polygons = [contour_points(contour) for contour in contours]
    boxes = [contour.boundingBox() for contour in contours]
    contained = []
    for index, (polygon, box) in enumerate(zip(polygons, boxes)):
        samples = polygon[::max(1, len(polygon) // 9)]
        for outer, (outer_polygon, outer_box) in enumerate(zip(polygons, boxes)):
            if index == outer:
                continue
            if not (
                outer_box[0] <= box[0] and outer_box[1] <= box[1]
                and outer_box[2] >= box[2] and outer_box[3] >= box[3]
            ):
                continue
            if sum(point_in_polygon(point, outer_polygon) for point in samples) > len(samples) / 2:
                contained.append(index)
                break
    return set(contained)


def clean_path(name):
    matches = sorted(WORK.glob("*-" + name.replace("/", "_")))
    return matches[0] / "clean.ttf" if matches else None


def main():
    bold = fontforge.open(str(BOLD))
    regular = fontforge.open(str(REGULAR))
    source = json.loads(INPUT.read_text(encoding="utf-8"))
    rows = {}
    try:
        for name, prior in source["glyphs"].items():
            if prior.get("status") != "blocked":
                continue
            path = clean_path(name)
            candidate = None
            if path and path.exists():
                clean = fontforge.open(str(path))
                candidate = clean[name].foreground.dup()
                clean.close()
            if candidate is None:
                candidate = bold[name].foreground.dup()
            before = bold[name].foreground.dup()
            required = v4.required_counters(regular[name].foreground, name)
            nested = contained_indices(candidate)
            preserve = set(sorted(
                nested, key=lambda index: v4.contour_area(list(candidate)[index]),
                reverse=True,
            )[:required])
            result = fontforge.layer()
            result.is_quadratic = candidate.is_quadratic
            for index, contour in enumerate(candidate):
                if index not in nested or index in preserve:
                    result += contour.dup()
            defects = v4.structural(result)
            if defects["intersections"]:
                sets = v4.layer_sets(result, error=.4, spacing=1.8)
                counts = tuple(min(26, max(8, len(points) // 7)) for points in sets)
                result = curves.curve_layer(
                    sets, counts, tension=.27, corner_angle=50.0
                )
            metrics = v4.comparison(regular[name].foreground, result)
            actual = max(
                metrics[size]["right_topology"][1] for size in (128, 256, 512)
            )
            defects = v4.structural(result)
            if actual > required or defects["intersections"]:
                rows[name] = {
                    "status": "blocked", "required": required, "actual": actual,
                    "nested": len(nested), "defects": defects,
                }
                continue
            bold[name].foreground = result
            delta = v4.comparison(before, result)
            rows[name] = {
                "status": "ok",
                "method": "contained-counter-resolution",
                "required": required, "actual": actual,
                "removed_contours": len(nested - preserve),
                "old_points": v4.point_count(before),
                "new_points": v4.point_count(result),
                "iou_64": delta[64]["iou"], "iou_128": delta[128]["iou"],
            }
        bold.save(str(BOLD))
    finally:
        bold.close()
        regular.close()
    payload = {
        "version": "bold-v4-counter-resolution",
        "accepted": sum(row.get("status") == "ok" for row in rows.values()),
        "blocked": sum(row.get("status") == "blocked" for row in rows.values()),
        "glyphs": rows,
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "accepted": payload["accepted"], "blocked": payload["blocked"]
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
