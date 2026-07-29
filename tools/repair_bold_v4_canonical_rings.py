#!/usr/bin/env ffpython
"""Geometrically rebuild the canonical counters in o and percent."""
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
OLD = ROOT / "checkpoints" / "bold-v3-reviewed" / "bold.sfd"
OUT = ROOT / "qa" / "assets" / "bold-v4-canonical-rings.json"


def oval(box, hole=False):
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    rx, ry = (x1 - x0) / 2.0, (y1 - y0) / 2.0
    contour = fontforge.contour()
    contour.is_quadratic = True
    contour.moveTo(cx, cy + ry)
    contour.quadraticTo((cx + rx, cy + ry), (cx + rx, cy))
    contour.quadraticTo((cx + rx, cy - ry), (cx, cy - ry))
    contour.quadraticTo((cx - rx, cy - ry), (cx - rx, cy))
    contour.quadraticTo((cx - rx, cy + ry), (cx, cy + ry))
    contour.closed = True
    if hole:
        contour.reverseDirection()
    return contour


def main():
    bold = fontforge.open(str(BOLD))
    old = fontforge.open(str(OLD))
    rows = {}
    try:
        name = "o"
        before = bold[name].foreground.dup()
        contours = list(old[name].foreground)
        outer = max(contours, key=v4.contour_area)
        inner = min(contours, key=v4.contour_area)
        layer = fontforge.layer()
        layer.is_quadratic = True
        layer += oval(outer.boundingBox())
        layer += oval(inner.boundingBox(), hole=True)
        bold[name].foreground = layer
        rows[name] = {"method": "geometric-oval-ring", "status": "ok"}

        name = "percent"
        before_percent = bold[name].foreground.dup()
        contours = list(old[name].foreground)
        outers = sorted(
            [contour for contour in contours if contour.isClockwise() == 1],
            key=v4.contour_area, reverse=True,
        )
        holes = sorted(
            [contour for contour in contours if contour.isClockwise() == 0],
            key=v4.contour_area, reverse=True,
        )
        slash = max(outers, key=lambda contour: contour.boundingBox()[3] - contour.boundingBox()[1])
        rings = [contour for contour in outers if contour is not slash]
        slash_sets = v4.layer_sets(slash, error=.35, spacing=1.5)
        slash_refit = curves.curve_layer(
            slash_sets, (10,), tension=.27, corner_angle=48.0
        )
        layer = fontforge.layer()
        layer.is_quadratic = True
        for contour in slash_refit:
            converted = contour.dup()
            converted.is_quadratic = True
            layer += converted
        for outer_contour, hole_contour in zip(
            sorted(rings, key=lambda c: c.boundingBox()[1], reverse=True),
            sorted(holes, key=lambda c: c.boundingBox()[1], reverse=True),
        ):
            layer += oval(outer_contour.boundingBox())
            layer += oval(hole_contour.boundingBox(), hole=True)
        bold[name].foreground = layer
        rows[name] = {"method": "two-geometric-rings-plus-refit-slash", "status": "ok"}
        bold.save(str(BOLD))
    finally:
        bold.close()
        old.close()
    for name, before in (("o", before), ("percent", before_percent)):
        current = fontforge.open(str(BOLD))
        metrics = v4.comparison(before, current[name].foreground)
        rows[name].update(
            old_points=v4.point_count(before),
            new_points=v4.point_count(current[name].foreground),
            iou_64=metrics[64]["iou"], iou_128=metrics[128]["iou"],
        )
        current.close()
    OUT.write_text(
        json.dumps({"version": "bold-v4-canonical-rings", "glyphs": rows},
                   indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"changed": 2}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
