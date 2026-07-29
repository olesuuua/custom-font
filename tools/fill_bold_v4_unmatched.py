#!/usr/bin/env ffpython
"""Fill raster-detected Bold-only white components while retaining required counters."""
from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".font-deps"), str(ROOT / "tools")]

import repair_bold_v4 as v4
import simplify_font as sf

BOLD = ROOT / "fontforge" / "bold.sfd"
REGULAR = ROOT / "fontforge" / "redrawn.sfd"
OUT = ROOT / "qa" / "assets" / "bold-v4-white-patch-fills.json"


def enclosed_components(mask, size):
    outside = bytearray(size * size)
    queue = deque()
    for x in range(size):
        queue.extend((x, (size - 1) * size + x))
    for y in range(size):
        queue.extend((y * size, y * size + size - 1))
    while queue:
        index = queue.popleft()
        if index < 0 or index >= len(mask) or outside[index] or mask[index]:
            continue
        outside[index] = 1
        x, y = index % size, index // size
        if x: queue.append(index - 1)
        if x + 1 < size: queue.append(index + 1)
        if y: queue.append(index - size)
        if y + 1 < size: queue.append(index + size)
    enclosed = bytearray(
        not value and not outside[index] for index, value in enumerate(mask)
    )
    seen = bytearray(size * size)
    components = []
    for seed, value in enumerate(enclosed):
        if not value or seen[seed]:
            continue
        flood = deque([seed])
        seen[seed] = 1
        points = []
        while flood:
            index = flood.popleft()
            x, y = index % size, index // size
            points.append((x, y))
            for neighbor in (
                index - 1 if x else -1,
                index + 1 if x + 1 < size else -1,
                index - size if y else -1,
                index + size if y + 1 < size else -1,
            ):
                if neighbor >= 0 and enclosed[neighbor] and not seen[neighbor]:
                    seen[neighbor] = 1
                    flood.append(neighbor)
        components.append({
            "area": len(points),
            "box": (
                min(p[0] for p in points), min(p[1] for p in points),
                max(p[0] for p in points), max(p[1] for p in points),
            ),
        })
    return components


def rectangle(box, raster_box, size, padding=12.0):
    x0, y0, x1, y1 = box
    sx = (raster_box[2] - raster_box[0]) / size
    sy = (raster_box[3] - raster_box[1]) / size
    left = raster_box[0] + (x0 - padding) * sx
    right = raster_box[0] + (x1 + 1 + padding) * sx
    bottom = raster_box[1] + (y0 - padding) * sy
    top = raster_box[1] + (y1 + 1 + padding) * sy
    contour = fontforge.contour()
    contour.is_quadratic = True
    contour.moveTo(left, bottom)
    contour.lineTo(left, top)
    contour.lineTo(right, top)
    contour.lineTo(right, bottom)
    contour.closed = True
    return contour


def actual_counters(regular_layer, bold_layer):
    metrics = v4.comparison(regular_layer, bold_layer)
    return max(metrics[size]["right_topology"][1] for size in (128, 256, 512))


def main():
    regular = fontforge.open(str(REGULAR))
    bold = fontforge.open(str(BOLD))
    rows = {}
    try:
        for glyph in bold.glyphs():
            name = glyph.glyphname
            if not v4.point_count(glyph.foreground):
                continue
            required = v4.required_counters(regular[name].foreground, name)
            before_count = actual_counters(regular[name].foreground, glyph.foreground)
            if before_count <= required:
                continue
            before = glyph.foreground.dup()
            filled = []
            for _pass in range(3):
                sets = v4.layer_sets(glyph.foreground, error=.35, spacing=1.5)
                raster_box = v4.shared_bbox(sets, sets)
                size = 512
                mask = sf.rasterize(sets, raster_box, size)
                components = enclosed_components(mask, size)
                excess = max(0, len(components) - required)
                if not excess:
                    break
                candidates = sorted(components, key=lambda item: item["area"])[:excess]
                layer = glyph.foreground.dup()
                for component in candidates:
                    layer += rectangle(component["box"], raster_box, size)
                    filled.append(component)
                glyph.foreground = layer
            after_count = actual_counters(regular[name].foreground, glyph.foreground)
            if after_count > required:
                glyph.foreground = before
                rows[name] = {
                    "status": "pathops-needed", "required": required,
                    "before": before_count, "after": after_count,
                    "filled_regions": len(filled),
                }
                continue
            metrics = v4.comparison(before, glyph.foreground)
            rows[name] = {
                "status": "ok",
                "method": "raster-white-component-fill",
                "required": required,
                "before": before_count,
                "after": after_count,
                "filled_regions": len(filled),
                "old_points": v4.point_count(before),
                "new_points": v4.point_count(glyph.foreground),
                "iou_64": metrics[64]["iou"],
                "iou_128": metrics[128]["iou"],
            }
        bold.save(str(BOLD))
    finally:
        regular.close()
        bold.close()
    payload = {
        "version": "bold-v4-white-patch-fills",
        "accepted": sum(row.get("status") == "ok" for row in rows.values()),
        "blocked": sum(row.get("status") == "pathops-needed" for row in rows.values()),
        "glyphs": rows,
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "accepted": payload["accepted"], "blocked": payload["blocked"]
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
