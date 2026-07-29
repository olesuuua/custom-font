#!/usr/bin/env ffpython
"""Apply geometric punctuation, dot, intentional-hole, and weight repairs."""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".font-deps"))
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf
import repair_redraw_v4 as v4
import bold_curve_helpers as curves
import simplify_font as sf

BASE = ROOT / "fontforge" / "regular-bold-base.sfd"
BOLD = ROOT / "fontforge" / "bold.sfd"
REPORT = ROOT / "qa" / "assets" / "bold-v3-geometric-repairs.json"
HOLE_POLICY = ROOT / "qa" / "assets" / "bold-v3-hole-policy.json"
GEOMETRIC = {
    "quotesingle": (7, .34, True),
    "comma": (8, .30, False),
    "parenleft": (8, .31, True),
    "parenright": (8, .31, True),
}
ALWAYS_FILL = {"asterisk", "uni041D", "uni0427", "uni043D"}
WEIGHT_TARGETS = {
    "eight": (1.40, 1.50),
    "Z": (1.45, 1.55),
    "Zeta": (1.45, 1.55),
    "uni20A6": (1.35, 1.45),
    "uni2116": (1.35, 1.45),
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def point_count(layer):
    return sum(len(contour) for contour in layer)


def contour_bbox(contour):
    values = list(contour)
    return (
        min(point.x for point in values), min(point.y for point in values),
        max(point.x for point in values), max(point.y for point in values),
    )


def center(box):
    return (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0


def layer_sets(layer):
    pen = sf.FlattenPen(error=1.0, spacing=4.0)
    layer.draw(pen)
    if pen.points:
        pen.endPath()
    return pen.contours


def union_bbox(left, right):
    box = sf.bbox_of_sets(left + right)
    pad = max(8.0, math.hypot(box[2] - box[0], box[3] - box[1]) * .04)
    return box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad


def raster_comparison(reference, candidate):
    left, right = layer_sets(reference), layer_sets(candidate)
    box = union_bbox(left, right)
    result = {}
    for size in (32, 64, 128, 256, 512):
        before = sf.rasterize(left, box, size)
        after = sf.rasterize(right, box, size)
        result[size] = {
            "iou": sf.raster_metrics(before, after)["ink_iou"],
            "ink_ratio": sum(after) / max(1, sum(before)),
            "before_topology": sf.topology(before, size),
            "after_topology": sf.topology(after, size),
        }
    return result


def structural(layer):
    return {
        "points": point_count(layer),
        "intersections": sum(contour.selfIntersects() for contour in layer),
        "open_contours": sum(not contour.closed for contour in layer),
        "invalid_handles": rf.invalid_handles(layer),
    }


def registered_expand(layer, amount_x=20.0, amount_y=20.0):
    box = sf.bbox_of_sets(layer_sets(layer))
    width, height = box[2] - box[0], box[3] - box[1]
    result = layer.dup()
    result.transform((
        (width + 2 * amount_x) / max(width, 1e-9), 0,
        0, (height + 2 * amount_y) / max(height, 1e-9),
        box[0] - amount_x - box[0] * (width + 2 * amount_x) / max(width, 1e-9),
        box[1] - amount_y - box[1] * (height + 2 * amount_y) / max(height, 1e-9),
    ))
    return result


def refit_geometric(layer, anchors, tension, force_smooth):
    point_sets = v4.point_sets(layer)
    if len(point_sets) != 1:
        raise RuntimeError("geometric refit expects one contour")
    return curves.curve_layer(
        point_sets, (anchors,), tension=tension,
        corner_angle=42.0,
        force_smooth_indices=(0,) if force_smooth else (),
    )


def ellipse(cx, cy, diameter):
    radius = diameter / 2.0
    handle = radius * 0.5522847498307936
    contour = fontforge.contour()
    contour.is_quadratic = False
    contour.moveTo(cx, cy + radius)
    contour.cubicTo(
        (cx + handle, cy + radius), (cx + radius, cy + handle),
        (cx + radius, cy),
    )
    contour.cubicTo(
        (cx + radius, cy - handle), (cx + handle, cy - radius),
        (cx, cy - radius),
    )
    contour.cubicTo(
        (cx - handle, cy - radius), (cx - radius, cy - handle),
        (cx - radius, cy),
    )
    contour.cubicTo(
        (cx - radius, cy + handle), (cx - handle, cy + radius),
        (cx, cy + radius),
    )
    contour.closed = True
    for point in contour:
        if point.on_curve:
            point.type = fontforge.splineCurve
    return contour


def box_contains(outer, inner, margin=0.0):
    return (
        outer[0] - margin <= inner[0] and outer[1] - margin <= inner[1]
        and outer[2] + margin >= inner[2] and outer[3] + margin >= inner[3]
    )


def overlaps(left, right, margin=0.0):
    return not (
        left[2] + margin < right[0] or right[2] + margin < left[0]
        or left[3] + margin < right[1] or right[3] + margin < left[1]
    )


def dot_contours(layer):
    contours = list(layer)
    boxes = [contour_bbox(contour) for contour in contours]
    result = []
    for index, (contour, box) in enumerate(zip(contours, boxes)):
        width, height = box[2] - box[0], box[3] - box[1]
        if not (10 <= width <= 180 and 10 <= height <= 180):
            continue
        if not (.50 <= width / max(height, 1e-9) <= 2.0):
            continue
        if contour.isClockwise() != 1:
            continue
        if any(
            other != index and box_contains(box, boxes[other], 1.0)
            for other in range(len(contours))
        ):
            continue
        if any(
            other != index and overlaps(box, boxes[other], -2.0)
            for other in range(len(contours))
        ):
            continue
        result.append((index, box))
    return result


def replace_dots(base_layer, bold_layer):
    base_dots = dot_contours(base_layer)
    if not base_dots:
        return bold_layer.dup(), []
    bold_contours = list(bold_layer)
    bold_boxes = [contour_bbox(contour) for contour in bold_contours]
    remove = set()
    replacements = []
    for _base_index, box in base_dots:
        cx, cy = center(box)
        width, height = box[2] - box[0], box[3] - box[1]
        choices = [
            (math.hypot(center(candidate)[0] - cx, center(candidate)[1] - cy), index)
            for index, candidate in enumerate(bold_boxes)
            if index not in remove
        ]
        if not choices:
            continue
        distance, match = min(choices)
        if distance > max(100.0, max(width, height)):
            continue
        remove.add(match)
        replacements.append(ellipse(cx, cy, max(width, height) + 40.0))
    result = fontforge.layer()
    result.is_quadratic = False
    for index, contour in enumerate(bold_contours):
        if index not in remove:
            preserved = contour.dup()
            preserved.is_quadratic = False
            result += preserved
    for contour in replacements:
        result += contour
    return result, [
        {"center": center(box), "base_box": box}
        for _index, box in base_dots[:len(replacements)]
    ]


def remove_holes(layer):
    result = fontforge.layer()
    result.is_quadratic = False
    removed = 0
    for contour in layer:
        if contour.isClockwise() == 0:
            removed += 1
        else:
            preserved = contour.dup()
            preserved.is_quadratic = False
            result += preserved
    return result, removed


def cleanup_candidate(font, name, layer, simplify_error=None):
    scratch = fontforge.font()
    scratch.encoding = "UnicodeFull"
    scratch.ascent = font.ascent
    scratch.descent = font.descent
    source = font[name]
    glyph = scratch.createChar(source.unicode, name)
    glyph.width = source.width
    glyph.foreground = layer.dup()
    glyph.correctDirection()
    glyph.removeOverlap()
    glyph.correctDirection()
    if simplify_error is not None:
        glyph.simplify(
            simplify_error,
            ("mergelines", "choosehv", "smoothcurves", "setstarttoextremum"),
        )
        glyph.correctDirection()
    result = glyph.foreground.dup()
    scratch.close()
    return result


def scale_outer_contours(layer, factor):
    result = fontforge.layer()
    result.is_quadratic = False
    for contour in layer:
        candidate = contour.dup()
        candidate.is_quadratic = False
        if contour.isClockwise() == 1:
            box = contour_bbox(contour)
            cx, cy = center(box)
            candidate.transform((
                factor, 0, 0, factor,
                cx - factor * cx, cy - factor * cy,
            ))
        result += candidate
    return result


def choose_weight_candidate(font, base_layer, current_layer, name, target):
    candidates = []
    for factor in (1.01, 1.02, 1.03, 1.04, 1.05, 1.06, 1.08, 1.10, 1.12):
        expanded = scale_outer_contours(current_layer, factor)
        variants = [("outer-scale-{:.2f}".format(factor), expanded)]
        for error in (None, .5, 1.0, 2.0):
            try:
                cleaned = cleanup_candidate(font, name, expanded, error)
            except Exception:
                continue
            variants.append((
                "outer-scale-{:.2f}-cleanup-{}".format(
                    factor, "raw" if error is None else "{:.1f}".format(error)
                ),
                cleaned,
            ))
        for method, layer in variants:
            comparison = raster_comparison(base_layer, layer)
            structure = structural(layer)
            topology_ok = all(
                comparison[size]["after_topology"][0]
                == comparison[size]["before_topology"][0]
                and comparison[size]["after_topology"][1]
                >= comparison[size]["before_topology"][1]
                for size in (128, 256, 512)
            )
            ratio = comparison[128]["ink_ratio"]
            if (
                structure["intersections"] == 0
                and structure["open_contours"] == 0
                and structure["invalid_handles"] == 0
                and topology_ok
            ):
                candidates.append((method, layer, comparison, structure, ratio))
    if not candidates:
        return None
    midpoint = sum(target) / 2.0
    in_range = [item for item in candidates if target[0] <= item[4] <= target[1]]
    pool = in_range or candidates
    return min(pool, key=lambda item: (
        0 if target[0] <= item[4] <= target[1] else 1,
        abs(item[4] - midpoint),
        item[3]["points"],
    ))


def main():
    regular_hash = digest(ROOT / "fontforge" / "redrawn.sfd")
    base_hash = digest(BASE)
    before_hash = digest(BOLD)
    base = fontforge.open(str(BASE))
    bold = fontforge.open(str(BOLD))
    rows = {}
    dot_names = []
    try:
        for name, (anchors, tension, smooth) in GEOMETRIC.items():
            old = bold[name].foreground.dup()
            expanded = registered_expand(base[name].foreground, 20.0, 20.0)
            candidate = refit_geometric(expanded, anchors, tension, smooth)
            metrics = raster_comparison(old, candidate)
            structure = structural(candidate)
            if structure["intersections"] or structure["open_contours"] or structure["invalid_handles"]:
                raise RuntimeError("{} geometric reconstruction is invalid".format(name))
            bold[name].foreground = candidate
            rows[name] = {
                "method": "geometric-refit-{}-anchors".format(anchors),
                "old_points": point_count(old), "new_points": point_count(candidate),
                "iou_64": metrics[64]["iou"], "iou_128": metrics[128]["iou"],
            }

        for glyph in bold.glyphs():
            name = glyph.glyphname
            if name in GEOMETRIC or name in ALWAYS_FILL:
                continue
            candidate, dots = replace_dots(
                base[name].foreground, glyph.foreground
            )
            if not dots:
                continue
            structure = structural(candidate)
            if structure["intersections"] or structure["open_contours"] or structure["invalid_handles"]:
                continue
            old = glyph.foreground.dup()
            comparison = raster_comparison(old, candidate)
            glyph.foreground = candidate
            dot_names.append(name)
            rows[name] = {
                "method": "four-anchor-isolated-dot",
                "dot_count": len(dots),
                "old_points": point_count(old), "new_points": point_count(candidate),
                "iou_64": comparison[64]["iou"],
                "iou_128": comparison[128]["iou"],
            }

        for name in ALWAYS_FILL:
            old = bold[name].foreground.dup()
            candidate, removed = remove_holes(old)
            if removed:
                bold[name].foreground = candidate
            comparison = raster_comparison(old, candidate)
            rows[name] = {
                "method": "intentional-filled-holes",
                "removed_holes": removed,
                "old_points": point_count(old), "new_points": point_count(candidate),
                "iou_64": comparison[64]["iou"],
                "iou_128": comparison[128]["iou"],
            }

        for name, target in WEIGHT_TARGETS.items():
            old = bold[name].foreground.dup()
            selected = choose_weight_candidate(
                bold, base[name].foreground, old, name, target
            )
            if selected is None:
                rows.setdefault(name, {}).update(
                    weight_method="no-safe-thicker-candidate",
                    target_ink_ratio=target,
                )
                continue
            method, candidate, comparison, structure, ratio = selected
            bold[name].foreground = candidate
            previous = rows.get(name, {})
            previous.update(
                weight_method=method,
                target_ink_ratio=target,
                achieved_ink_ratio_128=ratio,
                old_points=point_count(old),
                new_points=structure["points"],
                iou_64=comparison[64]["iou"],
                iou_128=comparison[128]["iou"],
            )
            rows[name] = previous

        bold.save(str(BOLD))
    finally:
        base.close()
        bold.close()

    # The QA calculation uses this explicit policy instead of treating every
    # tiny high-resolution white speck as a required Bold counter.
    policy = {
        "version": "bold-v3-hole-policy",
        "always_fill": sorted(ALWAYS_FILL),
        "automatic_rule": {
            "absent_at_sizes": [32, 64],
            "maximum_span_pixels_at_128": 2,
            "maximum_counter_area_to_ink_ratio": .02,
        },
    }
    HOLE_POLICY.write_text(
        json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    payload = {
        "version": "bold-v3-geometric-repairs",
        "authoritative_regular_sha256": regular_hash,
        "cleaned_base_sha256": base_hash,
        "bold_before_sha256": before_hash,
        "bold_sha256": digest(BOLD),
        "dot_glyphs": sorted(dot_names),
        "glyphs": rows,
    }
    REPORT.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "changed_count": len(rows),
        "dot_glyph_count": len(dot_names),
        "bold_sha256": payload["bold_sha256"],
    }, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
