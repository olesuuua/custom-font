#!/usr/bin/env ffpython
"""Restore two four-anchor ink-centroid dots after uni0451 raster cleanup."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".font-deps"), str(ROOT / "tools")]
import repair_bold_v4 as v4

BOLD = ROOT / "fontforge" / "bold.sfd"
REGULAR = ROOT / "fontforge" / "redrawn.sfd"
OUT = ROOT / "qa" / "assets" / "bold-v4-uni0451-dots.json"


def center(contour):
    box = contour.boundingBox()
    return (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0


def main():
    bold = fontforge.open(str(BOLD))
    regular = fontforge.open(str(REGULAR))
    before = bold["uni0451"].foreground.dup()
    current = list(before)
    current_dots = sorted(
        [(index, contour) for index, contour in enumerate(current)
         if contour.boundingBox()[1] > 500],
        key=lambda item: center(item[1])[0],
    )
    regular_dots = sorted(
        [contour for contour in regular["uni0451"].foreground
         if contour.boundingBox()[1] > 500 and v4.contour_area(contour) > 10],
        key=lambda contour: center(contour)[0],
    )
    if len(current_dots) != 2 or len(regular_dots) != 2:
        raise SystemExit("uni0451 dot components could not be identified")
    replacements = {}
    details = []
    for (index, _current), source in zip(current_dots, regular_dots):
        target = v4.raster_centroid(source)
        box = source.boundingBox()
        diameter = (max(box[2] - box[0], box[3] - box[1]) + 40.0) * .95
        replacements[index] = v4.ellipse(target[0], target[1], diameter)
        details.append({
            "center_x": target[0], "center_y": target[1],
            "new_diameter": diameter,
        })
    result = fontforge.layer()
    result.is_quadratic = True
    for index, contour in enumerate(current):
        result += replacements.get(index, contour).dup()
    bold["uni0451"].foreground = result
    bold.save(str(BOLD))
    bold.close()
    regular.close()
    metrics = v4.comparison(before, result)
    payload = {
        "version": "bold-v4-uni0451-dots",
        "glyphs": {"uni0451": {
            "status": "ok",
            "method": "two-regular-ink-centroid-four-anchor-dots-95pct",
            "dots": details,
            "old_points": v4.point_count(before),
            "new_points": v4.point_count(result),
            "iou_64": metrics[64]["iou"], "iou_128": metrics[128]["iou"],
        }},
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"dots": 2}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
