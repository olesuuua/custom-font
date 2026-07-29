#!/usr/bin/env ffpython
"""Raster-union reconstruction for the final embedded white-patch cohort."""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".font-deps"), str(ROOT / "tools")]

import bold_curve_helpers as curves
import fill_bold_v4_unmatched as fills
import repair_bold_v4 as v4
import simplify_font as sf

BOLD = ROOT / "fontforge" / "bold.sfd"
OLD = ROOT / "checkpoints" / "bold-v3-reviewed" / "bold.sfd"
REGULAR = ROOT / "fontforge" / "redrawn.sfd"
INPUT = ROOT / "qa" / "assets" / "bold-v4-counter-resolution.json"
OUT = ROOT / "qa" / "assets" / "bold-v4-raster-rebuild.json"


def fill_excess(mask, size, required):
    components = fills.enclosed_components(mask, size)
    for component in sorted(components, key=lambda item: item["area"])[:max(0, len(components) - required)]:
        x0, y0, x1, y1 = component["box"]
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                index = y * size + x
                if not mask[index]:
                    mask[index] = 1
    return mask


def boundary_loops(mask, size):
    edges = set()
    for y in range(size):
        for x in range(size):
            if not mask[y * size + x]:
                continue
            if y == 0 or not mask[(y - 1) * size + x]:
                edges.add(((x, y), (x + 1, y)))
            if x + 1 == size or not mask[y * size + x + 1]:
                edges.add(((x + 1, y), (x + 1, y + 1)))
            if y + 1 == size or not mask[(y + 1) * size + x]:
                edges.add(((x + 1, y + 1), (x, y + 1)))
            if x == 0 or not mask[y * size + x - 1]:
                edges.add(((x, y + 1), (x, y)))
    outgoing = defaultdict(list)
    for edge in edges:
        outgoing[edge[0]].append(edge[1])
    loops = []
    while edges:
        start = next(iter(edges))
        edge = start
        loop = [edge[0]]
        for _ in range(len(edges) + 1):
            if edge not in edges:
                break
            edges.remove(edge)
            loop.append(edge[1])
            if edge[1] == start[0]:
                break
            choices = [
                target for target in outgoing[edge[1]]
                if (edge[1], target) in edges
            ]
            if not choices:
                break
            edge = (edge[1], choices[0])
        if len(loop) > 8 and loop[-1] == loop[0]:
            loops.append(loop[:-1])
    return loops


def remove_collinear(points):
    result = []
    for point in points:
        result.append(point)
        while len(result) >= 3:
            a, b, c = result[-3:]
            if (b[0] - a[0]) * (c[1] - b[1]) == (b[1] - a[1]) * (c[0] - b[0]):
                result.pop(-2)
            else:
                break
    return result


def reconstruct(layer, required, curved=True, keep_loops=None):
    sets = v4.layer_sets(layer, error=.35, spacing=1.5)
    box = v4.shared_bbox(sets, sets)
    size = 512
    mask = bytearray(1 if value else 0 for value in sf.rasterize(sets, box, size))
    mask = fill_excess(mask, size, required)
    loops = boundary_loops(mask, size)
    signed = [
        sum(
            loop[i][0] * loop[(i + 1) % len(loop)][1]
            - loop[(i + 1) % len(loop)][0] * loop[i][1]
            for i in range(len(loop))
        ) / 2.0
        for loop in loops
    ]
    if loops:
        outer_sign = 1 if signed[max(range(len(loops)), key=lambda i: abs(signed[i]))] > 0 else -1
        outer_indices = [i for i, area in enumerate(signed) if (1 if area > 0 else -1) == outer_sign]
        hole_indices = sorted(
            [i for i, area in enumerate(signed) if (1 if area > 0 else -1) != outer_sign],
            key=lambda i: abs(signed[i]), reverse=True,
        )[:required]
        loops = [loops[i] for i in outer_indices + hole_indices]
    if keep_loops is not None and len(loops) > keep_loops:
        loops = sorted(
            loops,
            key=lambda loop: abs(sum(
                loop[i][0] * loop[(i + 1) % len(loop)][1]
                - loop[(i + 1) % len(loop)][0] * loop[i][1]
                for i in range(len(loop))
            )), reverse=True,
        )[:keep_loops]
    point_sets = []
    scale_x = (box[2] - box[0]) / size
    scale_y = (box[3] - box[1]) / size
    for loop in loops:
        loop = remove_collinear(loop)
        point_sets.append([
            (box[0] + x * scale_x, box[3] - y * scale_y)
            for x, y in loop
        ])
    if not curved:
        result = fontforge.layer()
        result.is_quadratic = True
        for points in point_sets:
            contour = fontforge.contour()
            contour.is_quadratic = True
            contour.moveTo(*points[0])
            for point in points[1:]:
                contour.lineTo(*point)
            contour.closed = True
            result += contour
        return result
    counts = tuple(
        min(48, max(8, int(sum(
            math.hypot(
                points[(i + 1) % len(points)][0] - points[i][0],
                points[(i + 1) % len(points)][1] - points[i][1],
            )
            for i in range(len(points))
        ) / 24.0)))
        for points in point_sets
    )
    return curves.curve_layer(
        point_sets, counts, tension=.27, corner_angle=48.0
    )


def main():
    bold = fontforge.open(str(BOLD))
    regular = fontforge.open(str(REGULAR))
    old = fontforge.open(str(OLD))
    source = json.loads(INPUT.read_text(encoding="utf-8"))
    rows = {}
    try:
        for name, prior in source["glyphs"].items():
            if prior.get("status") != "blocked":
                continue
            before = old[name].foreground.dup()
            required = v4.required_counters(regular[name].foreground, name)
            regular_metrics = v4.comparison(regular[name].foreground, regular[name].foreground)
            expected_components = regular_metrics[128]["left_topology"][0]
            candidate = reconstruct(before, required, keep_loops=expected_components + required)
            defects = v4.structural(candidate)
            metrics = v4.comparison(regular[name].foreground, candidate)
            actual = max(
                metrics[size]["right_topology"][1] for size in (128, 256, 512)
            )
            if actual > required or defects["intersections"]:
                candidate = reconstruct(before, required, curved=False, keep_loops=expected_components + required)
                defects = v4.structural(candidate)
                metrics = v4.comparison(regular[name].foreground, candidate)
                actual = max(
                    metrics[size]["right_topology"][1] for size in (128, 256, 512)
                )
                if actual > required or defects["intersections"]:
                    rows[name] = {
                        "status": "blocked", "required": required,
                        "actual": actual, "defects": defects,
                    }
                    continue
            bold[name].foreground = candidate
            delta = v4.comparison(before, candidate)
            rows[name] = {
                "status": "ok", "method": "filled-raster-boundary-refit",
                "required": required, "actual": actual,
                "old_points": v4.point_count(before),
                "new_points": v4.point_count(candidate),
                "iou_64": delta[64]["iou"], "iou_128": delta[128]["iou"],
            }
        bold.save(str(BOLD))
    finally:
        bold.close()
        regular.close()
        old.close()
    payload = {
        "version": "bold-v4-raster-rebuild",
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
