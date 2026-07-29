#!/usr/bin/env ffpython
"""Choose the heaviest safe weight for one glyph from the cleaned Bold base."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import deque
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".font-deps"))
sys.path.insert(0, str(ROOT / "tools"))
import simplify_font as sf


def sets(layer):
    pen = sf.FlattenPen(error=1.0, spacing=4.0)
    layer.draw(pen)
    if pen.points:
        pen.endPath()
    return pen.contours


def box_for(a, b):
    contours = a + b
    if not contours:
        return (-16.0, -16.0, 16.0, 16.0)
    box = sf.bbox_of_sets(contours)
    pad = max(8.0, math.hypot(box[2] - box[0], box[3] - box[1]) * 0.04)
    return box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad


def enclosed_area(mask, size):
    outside = bytearray(size * size)
    queue = deque()
    for x in range(size):
        queue.append(x)
        queue.append((size - 1) * size + x)
    for y in range(size):
        queue.append(y * size)
        queue.append(y * size + size - 1)
    while queue:
        index = queue.popleft()
        if outside[index] or mask[index]:
            continue
        outside[index] = 1
        x, y = index % size, index // size
        if x:
            queue.append(index - 1)
        if x + 1 < size:
            queue.append(index + 1)
        if y:
            queue.append(index - size)
        if y + 1 < size:
            queue.append(index + size)
    return sum(not mask[i] and not outside[i] for i in range(size * size))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("glyph")
    parser.add_argument("output")
    parser.add_argument("report")
    args = parser.parse_args()
    result = {"glyph": args.glyph, "status": "blocked"}
    source = fontforge.open(args.base)
    try:
        original = source[args.glyph]
        original_layer = original.foreground.dup()
        original_sets = sets(original_layer)
        if not original_sets:
            result.update(status="ok", weight=0, method="preserved-empty")
            return_code = 0
        else:
            selected = None
            selected_metrics = None
            for weight in (40, 38, 36, 34, 32, 30, 28, 26, 24, 22, 20, 18, 16, 14, 12, 10):
                scratch = fontforge.font()
                scratch.encoding = "UnicodeFull"
                scratch.ascent = source.ascent
                scratch.descent = source.descent
                glyph = scratch.createChar(original.unicode, original.glyphname)
                glyph.width = original.width
                glyph.foreground = original_layer.dup()
                scratch.selection.all()
                scratch.changeWeight(weight, "LCG", 0, 0, "retain")
                candidate = glyph.foreground.dup()
                candidate_sets = sets(candidate)
                bbox = box_for(original_sets, candidate_sets)
                safe = True
                metrics = {}
                for size in (128, 256, 512):
                    before = sf.rasterize(original_sets, bbox, size)
                    after = sf.rasterize(candidate_sets, bbox, size)
                    bt, at = sf.topology(before, size), sf.topology(after, size)
                    before_area = enclosed_area(before, size)
                    after_area = enclosed_area(after, size)
                    ratio = after_area / before_area if before_area else 1.0
                    metrics[str(size)] = {
                        "base_components": bt[0], "bold_components": at[0],
                        "base_counters": bt[1], "bold_counters": at[1],
                        "counter_area_ratio": ratio,
                    }
                    if at[0] < bt[0] or at[1] < bt[1] or (before_area and ratio < 0.75):
                        safe = False
                scratch.close()
                if safe:
                    selected = candidate
                    selected_metrics = metrics
                    result.update(status="ok", weight=weight,
                                  method="raw-plus40" if weight == 40 else "adaptive-counter-component-protection",
                                  metrics=metrics)
                    break
            if selected is None:
                result.update(error="No bold candidate retained components and 75% counter area")
                return_code = 1
            else:
                out = fontforge.font()
                out.encoding = "UnicodeFull"
                out.ascent = source.ascent
                out.descent = source.descent
                glyph = out.createChar(original.unicode, original.glyphname)
                glyph.width = original.width
                glyph.foreground = selected
                out.save(args.output)
                out.close()
                return_code = 0
    except Exception as error:
        result["error"] = "{}: {}".format(type(error).__name__, error)
        return_code = 1
    finally:
        source.close()
        Path(args.report).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
