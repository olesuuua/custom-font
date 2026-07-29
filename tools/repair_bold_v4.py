#!/usr/bin/env ffpython
"""Apply Bold v4 smoothness, white-patch, intersection, and dot repairs."""
from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".font-deps"), str(ROOT / "tools")]

import bold_curve_helpers as curves
import redraw_font as rf
import simplify_font as sf

REGULAR = ROOT / "fontforge" / "redrawn.sfd"
BASE = ROOT / "fontforge" / "regular-bold-base.sfd"
BOLD = ROOT / "fontforge" / "bold.sfd"
OLD_BOLD = ROOT / "checkpoints" / "bold-v3-reviewed" / "bold.sfd"
OUT = ROOT / "qa" / "assets" / "bold-v4-repairs.json"
DOT_SOURCE = ROOT / "qa" / "assets" / "bold-v3-geometric-repairs.json"

FORCE_SOLID = {
    "asterisk", "t", "u", "z", "uni041D", "uni0427", "uni043D"
}
PRIORITY_COUNTS = {
    "lessequal": (10, 6),
    "uni208A": (12,),
    "asterisk": (18,),
    "one": (10,),
    "four": (12,),
    "t": (18,),
    "u": (20,),
    "z": (18,),
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def point_count(layer):
    return sum(len(contour) for contour in layer)


def layer_sets(layer, error=0.75, spacing=3.0):
    pen = sf.FlattenPen(error=error, spacing=spacing)
    layer.draw(pen)
    if pen.points:
        pen.endPath()
    return pen.contours


def shared_bbox(left, right):
    contours = [c for c in left + right if c]
    if not contours:
        return (-16.0, -16.0, 16.0, 16.0)
    box = sf.bbox_of_sets(contours)
    pad = max(8.0, math.hypot(box[2] - box[0], box[3] - box[1]) * .04)
    return box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad


def comparison(left, right):
    left_sets, right_sets = layer_sets(left), layer_sets(right)
    box = shared_bbox(left_sets, right_sets)
    result = {}
    for size in (64, 128, 256, 512):
        lm = sf.rasterize(left_sets, box, size) if left_sets else bytes(size * size)
        rm = sf.rasterize(right_sets, box, size) if right_sets else bytes(size * size)
        result[size] = {
            "iou": sf.raster_metrics(lm, rm)["ink_iou"],
            "left_topology": sf.topology(lm, size),
            "right_topology": sf.topology(rm, size),
        }
    return result


def enclosed_area(mask, size):
    from collections import deque
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
    return sum(not value and not outside[i] for i, value in enumerate(mask))


def required_counters(layer, name):
    if name in FORCE_SOLID:
        return 0
    sets = layer_sets(layer)
    if not sets:
        return 0
    box = shared_bbox(sets, sets)
    masks = {s: sf.rasterize(sets, box, s) for s in (64, 128)}
    counters64 = sf.topology(masks[64], 64)[1]
    counters128 = sf.topology(masks[128], 128)[1]
    visible_area = enclosed_area(masks[128], 128) / max(1, sum(masks[128]))
    return counters128 if counters64 or visible_area > .02 else 0


def structural(layer):
    return {
        "intersections": sum(contour.selfIntersects() for contour in layer),
        "open_contours": sum(not contour.closed for contour in layer),
        "invalid_handles": rf.invalid_handles(layer),
        "points": point_count(layer),
    }


def scratch_cleanup(font, name, layer, simplify_error):
    scratch = fontforge.font()
    scratch.encoding = "UnicodeFull"
    scratch.ascent, scratch.descent = font.ascent, font.descent
    source = font[name]
    glyph = scratch.createChar(source.unicode, name)
    glyph.width = source.width
    glyph.foreground = layer.dup()
    glyph.correctDirection()
    glyph.removeOverlap()
    glyph.correctDirection()
    if simplify_error:
        glyph.simplify(
            simplify_error,
            ("mergelines", "choosehv", "smoothcurves", "setstarttoextremum"),
        )
        glyph.correctDirection()
    result = glyph.foreground.dup()
    scratch.close()
    return result


def contour_area(contour):
    points = [(p.x, p.y) for p in contour if p.on_curve]
    if len(points) < 3:
        return 0.0
    return abs(sum(
        points[i][0] * points[(i + 1) % len(points)][1]
        - points[(i + 1) % len(points)][0] * points[i][1]
        for i in range(len(points))
    )) / 2.0


def drop_extra_holes(layer, required):
    contours = list(layer)
    holes = [
        (contour_area(contour), index)
        for index, contour in enumerate(contours)
        if contour.isClockwise() == 0
    ]
    remove = {
        index for _area, index in sorted(holes)[:max(0, len(holes) - required)]
    }
    result = fontforge.layer()
    result.is_quadratic = layer.is_quadratic
    for index, contour in enumerate(contours):
        if index not in remove:
            result += contour.dup()
    return result, len(remove)


def generic_candidate(font, regular, name, layer):
    required = required_counters(regular[name].foreground, name)
    candidates = []
    for error in (0, .5, 1.0, 2.0, 3.0):
        try:
            cleaned = scratch_cleanup(font, name, layer, error)
            cleaned, removed = drop_extra_holes(cleaned, required)
            cleaned = scratch_cleanup(font, name, cleaned, 0)
        except Exception:
            continue
        defects = structural(cleaned)
        metrics = comparison(regular[name].foreground, cleaned)
        component_ok = all(
            metrics[size]["left_topology"][0]
            == metrics[size]["right_topology"][0]
            for size in (128, 256, 512)
        )
        counters = [metrics[size]["right_topology"][1] for size in (128, 256, 512)]
        counter_ok = (
            all(value == 0 for value in counters)
            if name in FORCE_SOLID
            else all(value <= required for value in counters)
        )
        if (
            not defects["intersections"]
            and not defects["open_contours"]
            and not defects["invalid_handles"]
            and component_ok and counter_ok
        ):
            candidates.append((defects["points"], -removed, error, cleaned, metrics, removed))
    return min(candidates, key=lambda item: (item[0], item[1], item[2])) if candidates else None


def refit_priority(font, name, layer):
    sets = layer_sets(layer, error=.45, spacing=2.0)
    if name == "lessequal":
        sets = sorted(sets, key=lambda pts: max(p[1] for p in pts), reverse=True)
    counts = PRIORITY_COUNTS[name]
    if len(sets) != len(counts):
        return layer
    candidate = curves.curve_layer(
        sets, counts, tension=.28, corner_angle=48.0,
        force_smooth_indices=() if name in {"lessequal", "uni208A", "asterisk"} else (0,),
    )
    return scratch_cleanup(font, name, candidate, .5)


def ellipse(cx, cy, diameter):
    radius = diameter / 2.0
    contour = fontforge.contour()
    contour.is_quadratic = True
    contour.moveTo(cx, cy + radius)
    contour.quadraticTo((cx + radius, cy + radius), (cx + radius, cy))
    contour.quadraticTo((cx + radius, cy - radius), (cx, cy - radius))
    contour.quadraticTo((cx - radius, cy - radius), (cx - radius, cy))
    contour.quadraticTo((cx - radius, cy + radius), (cx, cy + radius))
    contour.closed = True
    for point in contour:
        if point.on_curve:
            point.type = fontforge.splineCurve
    return contour


def raster_centroid(contour):
    sets = layer_sets(contour, error=.35, spacing=1.5)
    box = shared_bbox(sets, sets)
    size = 512
    mask = sf.rasterize(sets, box, size)
    points = [(i % size, i // size) for i, value in enumerate(mask) if value]
    if not points:
        b = contour.boundingBox()
        return (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
    x = sum(p[0] for p in points) / len(points)
    y = sum(p[1] for p in points) / len(points)
    return (
        box[0] + (x + .5) * (box[2] - box[0]) / size,
        box[1] + (y + .5) * (box[3] - box[1]) / size,
    )


def dot_contours(layer):
    result = []
    for index, contour in enumerate(layer):
        box = contour.boundingBox()
        width, height = box[2] - box[0], box[3] - box[1]
        if (
            contour.isClockwise() == 1 and 8 <= width <= 220 and 8 <= height <= 220
            and .35 <= width / max(height, 1e-6) <= 2.8
        ):
            result.append((index, contour))
    return result


def repair_dots(regular, bold, names):
    rows = {}
    for name in names:
        current = list(bold[name].foreground)
        current_dots = [
            (index, contour) for index, contour in enumerate(current)
            if sum(point.on_curve for point in contour) == 4
            and 40 <= contour.boundingBox()[2] - contour.boundingBox()[0] <= 220
        ]
        regular_dots = dot_contours(regular[name].foreground)
        if not current_dots or not regular_dots:
            continue
        available = set(index for index, _contour in regular_dots)
        replacements = {}
        details = []
        for current_index, current_contour in current_dots:
            current_box = current_contour.boundingBox()
            current_center = (
                (current_box[0] + current_box[2]) / 2.0,
                (current_box[1] + current_box[3]) / 2.0,
            )
            choices = []
            for regular_index, regular_contour in regular_dots:
                if regular_index not in available:
                    continue
                target = raster_centroid(regular_contour)
                choices.append((math.hypot(
                    target[0] - current_center[0], target[1] - current_center[1]
                ), regular_index, regular_contour, target))
            if not choices:
                continue
            _distance, regular_index, _regular_contour, target = min(choices)
            available.remove(regular_index)
            old_diameter = max(
                current_box[2] - current_box[0], current_box[3] - current_box[1]
            )
            diameter = old_diameter * .95
            replacements[current_index] = ellipse(target[0], target[1], diameter)
            details.append({
                "center_x": target[0], "center_y": target[1],
                "old_diameter": old_diameter, "new_diameter": diameter,
            })
        if not replacements:
            continue
        result = fontforge.layer()
        result.is_quadratic = True
        for index, contour in enumerate(current):
            result += replacements.get(index, contour).dup()
        bold[name].foreground = result
        rows[name] = {
            "method": "regular-ink-centroid-four-anchor-dot-95pct",
            "dots": details,
        }
    return rows


def main():
    regular = fontforge.open(str(REGULAR))
    base = fontforge.open(str(BASE))
    bold = fontforge.open(str(BOLD))
    old = fontforge.open(str(OLD_BOLD))
    before_hash = digest(BOLD)
    rows = {}
    try:
        targets = set(PRIORITY_COUNTS)
        for glyph in bold.glyphs():
            name = glyph.glyphname
            if not point_count(glyph.foreground):
                continue
            metrics = comparison(regular[name].foreground, glyph.foreground)
            new_counter = any(
                metrics[size]["right_topology"][1]
                > metrics[size]["left_topology"][1]
                for size in (128, 256, 512)
            )
            intersections = structural(glyph.foreground)["intersections"]
            if new_counter or intersections:
                targets.add(name)
        for name in sorted(targets):
            original = bold[name].foreground.dup()
            selected = generic_candidate(bold, regular, name, original)
            if selected is None:
                rows[name] = {"status": "blocked", "method": "no-safe-cleanup"}
                continue
            _points, _removed_sort, error, candidate, _metrics, removed = selected
            if name in PRIORITY_COUNTS:
                refitted = refit_priority(bold, name, candidate)
                refit_defects = structural(refitted)
                if not (
                    refit_defects["intersections"]
                    or refit_defects["open_contours"]
                    or refit_defects["invalid_handles"]
                ):
                    candidate = refitted
            final = scratch_cleanup(bold, name, candidate, 0)
            defects = structural(final)
            if defects["intersections"] or defects["open_contours"] or defects["invalid_handles"]:
                rows[name] = {"status": "blocked", "method": "post-refit-defect"}
                continue
            old_metrics = comparison(old[name].foreground, final)
            bold[name].foreground = final
            rows[name] = {
                "status": "ok",
                "method": (
                    "geometric-refit-remove-overlap"
                    if name in PRIORITY_COUNTS else "remove-overlap-fill-unmatched"
                ),
                "simplify_error": error,
                "removed_hole_contours": removed,
                "old_points": point_count(original),
                "new_points": point_count(final),
                "iou_64": old_metrics[64]["iou"],
                "iou_128": old_metrics[128]["iou"],
            }
        dot_names = json.loads(DOT_SOURCE.read_text(encoding="utf-8"))["dot_glyphs"]
        dot_rows = repair_dots(regular, bold, dot_names)
        for name, row in dot_rows.items():
            previous = rows.get(name, {})
            row.update({
                "status": "ok",
                "old_points": previous.get("old_points", point_count(old[name].foreground)),
                "new_points": point_count(bold[name].foreground),
            })
            old_metrics = comparison(old[name].foreground, bold[name].foreground)
            row["iou_64"], row["iou_128"] = (
                old_metrics[64]["iou"], old_metrics[128]["iou"]
            )
            rows[name] = row
        bold.save(str(BOLD))
    finally:
        regular.close()
        base.close()
        bold.close()
        old.close()
    payload = {
        "version": "bold-v4-repairs",
        "bold_before_sha256": before_hash,
        "bold_sha256": digest(BOLD),
        "changed_count": sum(row.get("status") == "ok" for row in rows.values()),
        "blocked_count": sum(row.get("status") == "blocked" for row in rows.values()),
        "glyphs": rows,
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "changed": payload["changed_count"],
        "blocked": payload["blocked_count"],
        "bold_sha256": payload["bold_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
