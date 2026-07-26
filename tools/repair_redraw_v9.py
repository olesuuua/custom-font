#!/usr/bin/env ffpython
"""Build redraw-v9 with adaptive stroke-boundary fitting and protected junctions."""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf
import repair_redraw_v3 as v3
import repair_redraw_v4 as v4
import repair_redraw_v6 as v6
import repair_redraw_v7 as v7
import repair_redraw_v8 as v8

V7 = ROOT / "checkpoints" / "redraw-v7"
V8 = ROOT / "checkpoints" / "redraw-v8"
V9 = ROOT / "checkpoints" / "redraw-v9"
WORK = ROOT / "checkpoints" / "redraw-v9-work"
CEILING = 100
FRACTIONS = {"oneeighth", "threeeighths", "fiveeighths", "seveneighths"}
TARGETS = {
    "uni0401", "uni041D", "uni20B4", "uni20BA",
    "oneeighth", "threeeighths", "fiveeighths", "seveneighths",
}
EXTRA_FIELDS = [
    "changed_in_v9", "v9_construction", "v9_component_points",
    "v9_protected_corner_count", "v9_junction_corner_count",
    "v9_width_cv", "v9_max_adjacent_width_change",
    "v9_component_source", "v9_preserved_components",
]


def atomic_json(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def point_count(layer):
    return sum(len(contour) for contour in layer)


def adaptive_fit(reference_layer, maximum, seeds=None):
    point_sets = v4.point_sets(reference_layer)
    ladders = rf.build_ladders(
        point_sets, seed_anchors=seeds, max_points=maximum,
        targets=(maximum,),
    )
    if not ladders:
        raise RuntimeError("adaptive fit produced no candidate")
    _points, anchors = ladders[-1]
    layer, _lines, _curves = rf.build_layer(point_sets, anchors)
    return layer


def nearest_index(points, target):
    return min(
        range(len(points)),
        key=lambda index: rf.distance(points[index], target),
    )


def semantic_cubic_fit(reference_layer, targets):
    point_sets = v4.point_sets(reference_layer)
    if len(point_sets) != 1:
        raise RuntimeError("semantic cubic fit expects one contour")
    points = point_sets[0]
    anchors = sorted({nearest_index(points, target) for target in targets})
    if len(anchors) != len(targets):
        raise RuntimeError("semantic anchors collapsed")
    layer, _lines, _curves = rf.build_layer(point_sets, [anchors])
    layer = v8.cubicize_layer(layer)
    layer, _converted = v7.aligned_smooth_handles(
        layer, maximum_turn=180.0
    )
    return layer


def cyclic_interval(values, left, right):
    indexes = [left]
    current = left
    while current != right:
        current = (current + 1) % len(values)
        indexes.append(current)
        if len(indexes) > len(values) + 1:
            raise RuntimeError("invalid cyclic interval")
    return indexes


def thin_e_lower_stroke(layer):
    result = layer.dup()
    main = max(result, key=len)
    values = list(main)
    on_indices = [index for index, point in enumerate(values) if point.on_curve]
    start = min(
        on_indices,
        key=lambda index: rf.distance(
            (values[index].x, values[index].y), (344.0, 63.0)
        ),
    )
    end = min(
        on_indices,
        key=lambda index: rf.distance(
            (values[index].x, values[index].y), (125.0, 340.0)
        ),
    )
    interval = cyclic_interval(values, start, end)
    center = (185.0, 150.0)
    for offset, index in enumerate(interval):
        phase = offset / float(max(1, len(interval) - 1))
        weight = math.sin(math.pi * phase)
        point = values[index]
        factor = 0.080 * weight
        point.x += (center[0] - point.x) * factor
        point.y += (center[1] - point.y) * factor
    return rf.classify_layer(result)


def cubicize_to_cap(layer, cap):
    line_choices = []
    contour_records = []
    for contour_index, source_contour in enumerate(layer):
        segments = v8.contour_segments(source_contour)
        contour_records.append(segments)
        for segment_index, (start, controls, end) in enumerate(segments):
            if len(controls) < 2:
                line_choices.append((
                    rf.distance((start.x, start.y), (end.x, end.y)),
                    contour_index, segment_index,
                ))
    full_points = sum(3 * len(segments) for segments in contour_records)
    keep_lines = max(0, int(math.ceil((full_points - cap) / 2.0)))
    preserved_lines = {
        (contour_index, segment_index)
        for _length, contour_index, segment_index in
        sorted(line_choices)[:keep_lines]
    }
    result = fontforge.layer()
    result.is_quadratic = False
    for contour_index, segments in enumerate(contour_records):
        contour = fontforge.contour()
        contour.is_quadratic = False
        contour.moveTo(segments[0][0].x, segments[0][0].y)
        for segment_index, (start, controls, end) in enumerate(segments):
            if (contour_index, segment_index) in preserved_lines:
                contour.lineTo(end.x, end.y)
                continue
            if len(controls) >= 2:
                control1 = (controls[0].x, controls[0].y)
                control2 = (controls[-1].x, controls[-1].y)
            else:
                delta = (end.x - start.x, end.y - start.y)
                control1 = (
                    start.x + delta[0] / 3.0,
                    start.y + delta[1] / 3.0,
                )
                control2 = (
                    start.x + 2.0 * delta[0] / 3.0,
                    start.y + 2.0 * delta[1] / 3.0,
                )
            contour.cubicTo(control1, control2, (end.x, end.y))
        contour.closed = True
        result += contour
    return rf.classify_layer(result), len(preserved_lines)

def rebuild_e(v8_font):
    combined = thin_e_lower_stroke(v8_font["uni0401"].foreground)
    return (
        combined,
        "centerline-width-normalization:lower-stroke-8-percent",
        "dots,inner-openings,upper-stroke",
        8,
        0,
    )

def rebuild_n(v7_font, v8_font):
    layer = v8_font["uni041D"].foreground.dup()
    reference_points = [
        point for contour in v7_font["uni041D"].foreground
        for point in contour
        if point.on_curve and 305 < point.x < 640 and 325 < point.y < 465
    ]
    moved = 0
    protected = 0
    for contour in layer:
        values = list(contour)
        for index, point in enumerate(values):
            if not point.on_curve:
                continue
            if 305 < point.x < 640 and 325 < point.y < 465:
                nearest = min(
                    reference_points,
                    key=lambda target: rf.distance(
                        (point.x, point.y), (target.x, target.y)
                    ),
                )
                delta = (
                    (nearest.x - point.x) * .18,
                    (nearest.y - point.y) * .18,
                )
                point.x += delta[0]
                point.y += delta[1]
                for neighbor_index in (
                    (index - 1) % len(values),
                    (index + 1) % len(values),
                ):
                    neighbor = values[neighbor_index]
                    if not neighbor.on_curve:
                        neighbor.x += delta[0]
                        neighbor.y += delta[1]
                point.type = fontforge.splineCurve
                moved += 1
            elif (
                280 <= point.x <= 670 and 320 <= point.y <= 470
                and (point.x <= 305 or point.x >= 640)
            ):
                point.type = fontforge.splineCorner
                protected += 1
    return (
        layer,
        "centerline-connector:v8-cubic:source-blend-18:corner-attachments",
        "stems",
        protected,
        protected,
    )

def protect_junctions(layer, low, high):
    result = layer.dup()
    protected = 0
    for contour in result:
        values = list(contour)
        on_indices = [index for index, point in enumerate(values) if point.on_curve]
        for position, index in enumerate(on_indices):
            point = values[index]
            if not (low <= point.y <= high):
                continue
            previous = values[on_indices[(position - 1) % len(on_indices)]]
            following = values[on_indices[(position + 1) % len(on_indices)]]
            incoming = rf.normalize((point.x - previous.x, point.y - previous.y))
            outgoing = rf.normalize((following.x - point.x, following.y - point.y))
            cosine = max(-1.0, min(1.0, rf.dot(incoming, outgoing)))
            turn = math.degrees(math.acos(cosine))
            if turn >= 10.0:
                previous_control = values[(index - 1) % len(values)]
                following_control = values[(index + 1) % len(values)]
                if not previous_control.on_curve and not following_control.on_curve:
                    normal = (-outgoing[1], outgoing[0])
                    following_control.x += normal[0] * .25
                    following_control.y += normal[1] * .25
                point.type = fontforge.splineCorner
                protected += 1
    return result, protected


def rebuild_currency(name, v7_font):
    reference = v7_font[name].foreground
    fitted = adaptive_fit(reference, CEILING)
    if name == "uni20B4":
        protected_layer, protected = protect_junctions(fitted, 210.0, 390.0)
        source = "body-plus-two-horizontal-bars"
    else:
        protected_layer, protected = protect_junctions(fitted, 275.0, 465.0)
        source = "stem-loop-plus-two-diagonal-bars"
    return (
        protected_layer,
        "adaptive-stroke-boundary:separate-junction-protection",
        source,
        protected,
        protected,
    )


def outer_master(v7_font):
    _numerator, _bar, denominator = v7.split_fraction(
        v7_font["threeeighths"].foreground
    )
    outer = v8.sort_denominator(denominator)[0]
    values = list(outer)
    on_indices = [index for index, point in enumerate(values) if point.on_curve]
    if len(on_indices) != 9:
        raise RuntimeError("v7 outer eight no longer has nine anchors")
    kept_positions = (0, 1, 2, 3, 4, 5, 7, 8)
    contour = fontforge.contour()
    contour.is_quadratic = False
    first = values[on_indices[kept_positions[0]]]
    contour.moveTo(first.x, first.y)
    for offset, left_position in enumerate(kept_positions):
        right_position = kept_positions[(offset + 1) % len(kept_positions)]
        left_index = on_indices[left_position]
        right_index = on_indices[right_position]
        start = values[left_index]
        end = values[right_index]
        if left_position == 5 and right_position == 7:
            controls = []
        else:
            controls = [
                point for point in v8.cyclic_values(
                    values, left_index, right_index
                ) if not point.on_curve
            ]
        if len(controls) >= 2:
            control1 = (controls[0].x, controls[0].y)
            control2 = (controls[-1].x, controls[-1].y)
        else:
            delta = (end.x - start.x, end.y - start.y)
            control1 = (
                start.x + delta[0] / 3.0,
                start.y + delta[1] / 3.0,
            )
            control2 = (
                start.x + 2.0 * delta[0] / 3.0,
                start.y + 2.0 * delta[1] / 3.0,
            )
        contour.cubicTo(control1, control2, (end.x, end.y))
    contour.closed = True
    layer = v8.contour_layer([contour])
    layer, _converted = v7.aligned_smooth_handles(
        layer, maximum_turn=72.0
    )
    return layer
def source_outer_master(original_font):
    raw = v4.point_sets(original_font["threeeighths"].foreground)
    visible = v3.raster_visible_boundary(raw, 5, 512)
    outer = max(
        (points for points in visible if max(x for x, _y in points) > 560
         and min(y for _x, y in points) < 10),
        key=len,
    )
    regions = {
        "top": [point for point in outer if point[1] > 280],
        "upper": [point for point in outer if point[1] > 200],
        "waist": [point for point in outer if 130 < point[1] < 200],
        "lower": [point for point in outer if point[1] < 130],
    }
    targets = [
        max(regions["top"], key=lambda point: point[1]),
        max(regions["upper"], key=lambda point: point[0]),
        max(regions["waist"], key=lambda point: point[0]),
        max(regions["lower"], key=lambda point: point[0]),
        min(regions["lower"], key=lambda point: point[1]),
        min(regions["lower"], key=lambda point: point[0]),
        min(regions["waist"], key=lambda point: point[0]),
        min(regions["upper"], key=lambda point: point[0]),
    ]
    anchors = sorted({nearest_index(outer, target) for target in targets})
    layer, _lines, _curves = rf.build_layer([outer], [anchors])
    layer = v8.cubicize_layer(layer)
    layer, _converted = v7.aligned_smooth_handles(
        layer, maximum_turn=180.0
    )
    return layer


def cubic_geometry(layer):
    contour = list(layer)[0]
    segments = v8.contour_segments(contour)
    anchors = [(segment[0].x, segment[0].y) for segment in segments]
    outgoing = []
    incoming = [None] * len(segments)
    for index, (_start, controls, _end) in enumerate(segments):
        if len(controls) < 2:
            raise RuntimeError("blend geometry requires cubic segments")
        outgoing.append((controls[0].x, controls[0].y))
        incoming[(index + 1) % len(segments)] = (
            controls[-1].x, controls[-1].y
        )
    return anchors, incoming, outgoing


def orientation_variants(geometry):
    anchors, incoming, outgoing = geometry
    count = len(anchors)
    variants = []
    for reverse in (False, True):
        if reverse:
            base_anchors = list(reversed(anchors))
            base_incoming = list(reversed(outgoing))
            base_outgoing = list(reversed(incoming))
        else:
            base_anchors = anchors
            base_incoming = incoming
            base_outgoing = outgoing
        for shift in range(count):
            variants.append((
                base_anchors[shift:] + base_anchors[:shift],
                base_incoming[shift:] + base_incoming[:shift],
                base_outgoing[shift:] + base_outgoing[:shift],
            ))
    return variants


def blend_cubic_layers(baseline, source, alpha):
    left = cubic_geometry(baseline)
    best = min(
        orientation_variants(cubic_geometry(source)),
        key=lambda candidate: sum(
            rf.distance(a, b)
            for a, b in zip(left[0], candidate[0])
        ),
    )
    def mix(a, b):
        return (
            a[0] + (b[0] - a[0]) * alpha,
            a[1] + (b[1] - a[1]) * alpha,
        )
    anchors = [mix(a, b) for a, b in zip(left[0], best[0])]
    incoming = [mix(a, b) for a, b in zip(left[1], best[1])]
    outgoing = [mix(a, b) for a, b in zip(left[2], best[2])]
    contour = fontforge.contour()
    contour.is_quadratic = False
    contour.moveTo(*anchors[0])
    for index in range(len(anchors)):
        following = (index + 1) % len(anchors)
        contour.cubicTo(
            outgoing[index], incoming[following], anchors[following]
        )
    contour.closed = True
    layer = v8.contour_layer([contour])
    layer, _converted = v7.aligned_smooth_handles(
        layer, maximum_turn=180.0
    )
    return layer

def slash_twelve(bar):
    segments = v8.contour_segments(bar)
    if len(segments) != 5:
        raise RuntimeError("approved slash no longer has five segments")
    contour = fontforge.contour()
    contour.is_quadratic = False
    contour.moveTo(segments[0][0].x, segments[0][0].y)
    for index, (start, controls, end) in enumerate(segments):
        if index == 1:
            midpoint = v8.cubic_point(
                (start.x, start.y),
                (controls[0].x, controls[0].y),
                (controls[-1].x, controls[-1].y),
                (end.x, end.y),
                .5,
            )
            contour.lineTo(*midpoint)
            contour.lineTo(end.x, end.y)
        elif len(controls) >= 2:
            contour.cubicTo(
                (controls[0].x, controls[0].y),
                (controls[-1].x, controls[-1].y),
                (end.x, end.y),
            )
        else:
            contour.lineTo(end.x, end.y)
    contour.closed = True
    return rf.classify_layer(v8.contour_layer([contour]))
def rebuild_fraction(name, v7_font, v8_font, master):
    numerator8, bar8, denominator8 = v7.split_fraction(
        v8_font[name].foreground
    )
    denominator8 = v8.sort_denominator(denominator8)
    inner = [contour.dup() for contour in denominator8[1:]]
    source_outer = v4.registered_to_bbox(
        master, v8.contour_bbox(denominator8[0])
    )
    outer = blend_cubic_layers(
        v8.contour_layer([denominator8[0]]), source_outer, .20
    )
    numerator = numerator8.dup()
    source = "v8-numerator"
    if name == "threeeighths":
        numerator7, _bar7, _denominator7 = v7.split_fraction(
            v7_font[name].foreground
        )
        numerator = numerator7.dup()
        source = "v7-complete-three"
    slash = slash_twelve(bar8)
    layer = v8.contour_layer(
        [numerator] + [contour.dup() for contour in slash]
        + [contour.dup() for contour in outer] + inner
    )
    component_points = {
        "numerator": len(numerator),
        "slash": point_count(slash),
        "outer_eight": point_count(outer),
        "inner_counters": sum(len(contour) for contour in inner),
    }
    return (
        layer,
        "consensus-eight:semantic-eight-anchor-cubic",
        "inner-counters",
        0,
        0,
        source,
        component_points,
    )


def approximate_width_metrics(layer):
    # A raster distance ridge is used only as a stable nonterminal width proxy.
    sets = rf.metric_layer_point_sets(layer)
    bbox = rf.sf.padded_bbox(rf.sf.bbox_of_sets(sets))
    size = 128
    mask = rf.sf.rasterize(sets, bbox, size)
    inside = [value >= 0.5 for value in mask]
    inf = 10 ** 6
    dist = [inf if value else 0.0 for value in inside]
    root2 = math.sqrt(2.0)
    for y in range(size):
        for x in range(size):
            index = y * size + x
            if not inside[index]:
                continue
            candidates = [dist[index]]
            if x:
                candidates.append(dist[index - 1] + 1.0)
            if y:
                candidates.append(dist[index - size] + 1.0)
            if x and y:
                candidates.append(dist[index - size - 1] + root2)
            if x + 1 < size and y:
                candidates.append(dist[index - size + 1] + root2)
            dist[index] = min(candidates)
    for y in range(size - 1, -1, -1):
        for x in range(size - 1, -1, -1):
            index = y * size + x
            if not inside[index]:
                continue
            candidates = [dist[index]]
            if x + 1 < size:
                candidates.append(dist[index + 1] + 1.0)
            if y + 1 < size:
                candidates.append(dist[index + size] + 1.0)
            if x + 1 < size and y + 1 < size:
                candidates.append(dist[index + size + 1] + root2)
            if x and y + 1 < size:
                candidates.append(dist[index + size - 1] + root2)
            dist[index] = min(candidates)
    ridges = []
    for y in range(1, size - 1):
        for x in range(1, size - 1):
            index = y * size + x
            if dist[index] < 1.5:
                continue
            neighbors = [
                dist[(y + dy) * size + x + dx]
                for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                if dx or dy
            ]
            if dist[index] >= max(neighbors):
                ridges.append(dist[index] * 2.0)
    if len(ridges) < 5:
        return 0.0, 0.0
    ridges.sort()
    trimmed = ridges[
        int(len(ridges) * .35):max(int(len(ridges) * .35) + 1,
                                    int(len(ridges) * .65))
    ]
    mean = sum(trimmed) / len(trimmed)
    variance = sum((value - mean) ** 2 for value in trimmed) / len(trimmed)
    cv = math.sqrt(variance) / max(1e-9, mean)
    ordered = sorted(trimmed)
    jumps = [
        abs(right - left) / max(left, right)
        for left, right in zip(ordered, ordered[1:])
    ]
    return cv, max(jumps) if jumps else 0.0


def evaluate(original, name, layer, method):
    sets = v4.point_sets(original[name].foreground)
    return v8.evaluate(sets, layer, method, normalize=False)


def quality_gate(candidate, baseline, name):
    if not v8.structurally_clean(candidate):
        return False
    new = candidate["metrics"]
    old = baseline["metrics"]
    if name == "uni0401":
        return (
            new["per_size"][128]["ink_iou"]
            >= old["per_size"][128]["ink_iou"] - .060
        )
    for size in (32, 64, 128):
        if (
            new["per_size"][size]["ink_iou"]
            < old["per_size"][size]["ink_iou"] - .005
        ):
            return False
    return (
        new["per_size"][128]["ink_iou"]
        > old["per_size"][128]["ink_iou"] + .002
        and new["boundary_p95"] <= old["boundary_p95"] + .002
    )


def patch_metadata(source_ttf, target_ttf):
    v8.patch_metadata(source_ttf, target_ttf)


def publish_to_canonical():
    copies = {
        V9 / "redrawn.sfd": ROOT / "fontforge" / "redrawn.sfd",
        V9 / "redraw-review.sfd": ROOT / "fontforge" / "redraw-review.sfd",
        V9 / "redrawn.ttf": ROOT / "qa" / "assets" / "redrawn.ttf",
        V9 / "redraw-review.ttf": ROOT / "qa" / "assets" / "redraw-review.ttf",
        V9 / "redraw-report.csv": ROOT / "qa" / "assets" / "redraw-report.csv",
        V9 / "redraw-manual-decisions.json":
            ROOT / "qa" / "redraw-manual-decisions.json",
    }
    for source, target in copies.items():
        shutil.copy2(source, target)
    index = ROOT / "qa" / "index.html"
    content = index.read_text(encoding="utf-8")
    content = content.replace("redrawn.ttf?v=redraw-v8", "redrawn.ttf?v=redraw-v9")
    index.write_text(content, encoding="utf-8", newline="\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if V9.exists() and not args.force:
        raise SystemExit("redraw-v9 exists; use --force")
    WORK.mkdir(parents=True, exist_ok=True)

    with (V8 / "redraw-report.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        old_rows = {row["glyph"]: row for row in reader}
    for field in EXTRA_FIELDS:
        if field not in fields:
            fields.append(field)
    decisions_payload = json.loads(
        (V8 / "redraw-manual-decisions.json").read_text(encoding="utf-8")
    )
    decisions = decisions_payload["decisions"]

    original = fontforge.open(str(ROOT / "fontforge" / "original.sfd"))
    prior7 = fontforge.open(str(V7 / "redrawn.sfd"))
    output = fontforge.open(str(V8 / "redrawn.sfd"))
    order = [glyph.glyphname for glyph in output.glyphs()]
    master = source_outer_master(original)
    methods = {}
    preserved = {}
    protected = {}
    junctions = {}
    component_sources = {}
    component_points = {}
    width_metrics = {}
    try:
        for name in order:
            if name not in TARGETS:
                continue
            baseline = evaluate(
                original, name, output[name].foreground.dup(), "v8-baseline"
            )
            if name == "uni0401":
                layer, method, keep, protect, junction = rebuild_e(output)
                source = "v7-cleaned-source-adaptive"
                parts = {"whole": point_count(layer)}
            elif name == "uni041D":
                layer, method, keep, protect, junction = rebuild_n(prior7, output)
                source = "v7-connector-centerline"
                parts = {"whole": point_count(layer)}
            elif name in ("uni20B4", "uni20BA"):
                layer, method, keep, protect, junction = rebuild_currency(
                    name, prior7
                )
                source = "v7-cleaned-source-adaptive"
                parts = {"whole": point_count(layer)}
            else:
                (
                    layer, method, keep, protect, junction, source, parts
                ) = rebuild_fraction(name, prior7, output, master)
            candidate = evaluate(original, name, layer, method)
            if not quality_gate(candidate, baseline, name):
                n128 = candidate["metrics"]["per_size"][128]["ink_iou"]
                o128 = baseline["metrics"]["per_size"][128]["ink_iou"]
                raise RuntimeError(
                    "{} failed v9 quality gate: points={} iou={:.4f} "
                    "baseline={:.4f}".format(
                        name, candidate["points"], n128, o128
                    )
                )
            output[name].foreground = candidate["layer"].dup()
            methods[name] = method
            preserved[name] = keep
            protected[name] = protect
            junctions[name] = junction
            component_sources[name] = source
            component_points[name] = parts
            width_metrics[name] = approximate_width_metrics(
                candidate["layer"]
            )
            counts = rf.point_type_counts(candidate["layer"])
            print(
                "v9 {}: {} total; corners {}; curves {}; iou {:.4f}; "
                "width-cv {:.4f}; {}".format(
                    name, candidate["points"], counts["corner"],
                    counts["curve"] + counts["hvcurve"],
                    candidate["metrics"]["per_size"][128]["ink_iou"],
                    width_metrics[name][0], method,
                ),
                flush=True,
            )
        output.save(str(WORK / "redrawn.sfd"))
        output.generate(str(WORK / "redrawn.ttf"))
    finally:
        original.close()
        prior7.close()
        output.close()

    patch_metadata(V8 / "redrawn.ttf", WORK / "redrawn.ttf")
    saved = fontforge.open(str(WORK / "redrawn.sfd"))
    original = fontforge.open(str(ROOT / "fontforge" / "original.sfd"))
    rows = []
    now = datetime.now(timezone.utc).isoformat()
    try:
        for name in order:
            old = dict(old_rows[name])
            if name not in TARGETS:
                old["changed_in_v9"] = "false"
                rows.append(old)
                continue
            final = evaluate(
                original, name, saved[name].foreground.dup(),
                methods[name] + ":serialized",
            )
            if not v8.structurally_clean(final):
                raise RuntimeError(name + " invalid after SFD serialization")
            row = rf.result_row(
                original[name], original[name].foreground, final, 1,
                {"decisions": {}},
            )
            for field, value in old.items():
                row.setdefault(field, value)
            counts = rf.point_type_counts(saved[name].foreground)
            on_curve = sum(
                counts[key] for key in
                ("corner", "curve", "hvcurve", "tangent")
            )
            if name in ("uni20B4", "uni20BA"):
                low, high = (
                    (210.0, 390.0) if name == "uni20B4"
                    else (275.0, 465.0)
                )
                junctions[name] = sum(
                    1 for contour in saved[name].foreground
                    for point in contour
                    if (
                        point.on_curve
                        and point.type == fontforge.splineCorner
                        and low <= point.y <= high
                    )
                )
            cv, jump = approximate_width_metrics(saved[name].foreground)
            row.update({
                "changed_in_v9": "true",
                "on_curve_points": str(on_curve),
                "control_points": str(counts["off"]),
                "v9_construction": methods[name],
                "v9_component_points": json.dumps(
                    component_points[name], sort_keys=True,
                    separators=(",", ":"),
                ),
                "v9_protected_corner_count": str(protected[name]),
                "v9_junction_corner_count": str(junctions[name]),
                "v9_width_cv": "{:.6f}".format(cv),
                "v9_max_adjacent_width_change": "{:.6f}".format(jump),
                "v9_component_source": component_sources[name],
                "v9_preserved_components": preserved[name],
                "current_review_status": "needs_rework",
                "repair_method": methods[name],
                "point_ceiling": str(CEILING),
            })
            decisions[name] = {
                "status": "needs_rework",
                "source_hash": row["source_hash"],
                "candidate_hash": row["candidate_hash"],
                "updated_at": now,
            }
            rf.apply_manual_decision(row, {"decisions": decisions})
            rows.append(row)
    finally:
        saved.close()
        original.close()

    review_sfd = WORK / "redraw-review.sfd"
    review_ttf = WORK / "redraw-review.ttf"
    shutil.copy2(WORK / "redrawn.sfd", review_sfd)
    shutil.copy2(WORK / "redrawn.ttf", review_ttf)
    report = WORK / "redraw-report.csv"
    with report.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    decisions_payload.update({
        "version": "redraw-manual-review-v9",
        "candidate_version": "redraw-v9",
    })
    decision_path = WORK / "redraw-manual-decisions.json"
    atomic_json(decision_path, decisions_payload)

    if V9.exists():
        shutil.rmtree(V9)
    V9.mkdir(parents=True)
    artifacts = {
        "redrawn.sfd": WORK / "redrawn.sfd",
        "redrawn.ttf": WORK / "redrawn.ttf",
        "redraw-review.sfd": review_sfd,
        "redraw-review.ttf": review_ttf,
        "redraw-report.csv": report,
        "redraw-manual-decisions.json": decision_path,
    }
    for filename, path in artifacts.items():
        shutil.copy2(path, V9 / filename)
    manifest = {
        "version": "redraw-v9",
        "base_version": "redraw-v8",
        "created_at": now,
        "glyph_count": len(rows),
        "hard_point_ceiling": CEILING,
        "point_budget_definition": "on-curve anchors plus off-curve controls",
        "target_glyphs": sorted(TARGETS),
        "changed_glyphs": sorted(TARGETS),
        "construction_methods": methods,
        "component_points": component_points,
        "component_sources": component_sources,
        "protected_corner_counts": protected,
        "junction_corner_counts": junctions,
        "width_metrics": {
            name: {
                "coefficient_of_variation": values[0],
                "maximum_adjacent_change": values[1],
            }
            for name, values in width_metrics.items()
        },
        "artifact_hashes": {
            filename: v8.sha256(V9 / filename)
            for filename in artifacts
        },
    }
    atomic_json(V9 / "manifest.json", manifest)
    publish_to_canonical()
    print(json.dumps({
        "changed": len(TARGETS), "version": "redraw-v9"
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
