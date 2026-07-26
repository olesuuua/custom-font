#!/usr/bin/env python3
"""Rebuild the original font as compact, source-derived cubic outlines.

Run with FontForge's Python runtime:
  ffpython tools/redraw_font.py
  ffpython tools/redraw_font.py --glyph A --glyph uni20A9

The editable SFD owns the point budget.  Every candidate is reconstructed from
dense samples of fontforge/original.sfd; no simplified outline is used as a
donor.  Automatic failures still use their best <=100 point candidate and are
listed in the review font/report.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import fontforge


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import simplify_font as sf


SOURCE_SFD = ROOT / "fontforge" / "original.sfd"
SOURCE_TTF = ROOT / "qa" / "assets" / "original.ttf"
OUTPUT_SFD = ROOT / "fontforge" / "redrawn.sfd"
OUTPUT_TTF = ROOT / "qa" / "assets" / "redrawn.ttf"
REVIEW_SFD = ROOT / "fontforge" / "redraw-review.sfd"
REVIEW_TTF = ROOT / "qa" / "assets" / "redraw-review.ttf"
REPORT = ROOT / "qa" / "assets" / "redraw-report.csv"
DECISIONS = ROOT / "qa" / "redraw-manual-decisions.json"
CHECKPOINT = ROOT / "checkpoints" / "redraw-v1"
WORK_ROOT = ROOT / "checkpoints" / "redraw-work"

MAX_POINTS = 100
SIMPLE_MAX_POINTS = 49
RASTER_SIZES = (32, 64, 128)
TOPOLOGY_SIZES = (128, 256, 512, 1024)
MAX_MSE = 0.010
MIN_IOU = 0.95
MAX_FALSE_INK = 0.04
MIN_CURVE_COVERAGE = 0.90
MAX_BOUNDARY_P95 = 0.04
MAX_BOUNDARY = 0.10
EPSILON = 1e-9

REPORT_FIELDS = [
    "glyph", "codepoint", "char", "source_points", "redrawn_points",
    "source_contours", "redrawn_contours", "simple", "automatic_pass",
    "manual_status", "effective_status", "needs_manual_review", "failure_reasons",
    "candidate_count", "selected_budget", "source_hash", "candidate_hash",
    "corner_points", "curve_points", "hvcurve_points", "tangent_points",
    "off_curve_points", "line_segments", "curve_segments", "curve_arc_coverage",
    "boundary_p95", "boundary_max", "self_intersections", "invalid_handles",
    "topology_status", "component_delta", "counter_delta", "worst_raster_size",
]
for size in RASTER_SIZES:
    REPORT_FIELDS.extend([
        "mse_{}".format(size), "ink_iou_{}".format(size),
        "false_positive_ink_{}".format(size),
        "false_negative_ink_{}".format(size),
    ])
for size in TOPOLOGY_SIZES:
    REPORT_FIELDS.extend([
        "source_components_{}".format(size), "source_counters_{}".format(size),
        "candidate_components_{}".format(size), "candidate_counters_{}".format(size),
    ])


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def subtract(a, b):
    return (a[0] - b[0], a[1] - b[1])


def multiply(a, value):
    return (a[0] * value, a[1] * value)


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def normalize(value):
    length = math.hypot(value[0], value[1])
    if length <= EPSILON:
        return (0.0, 0.0)
    return (value[0] / length, value[1] / length)


def lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def cubic_point(start, control1, control2, end, t):
    u = 1.0 - t
    return (
        u ** 3 * start[0] + 3 * u * u * t * control1[0]
        + 3 * u * t * t * control2[0] + t ** 3 * end[0],
        u ** 3 * start[1] + 3 * u * u * t * control1[1]
        + 3 * u * t * t * control2[1] + t ** 3 * end[1],
    )


def cyclic_indices(count, left, right):
    values = [left]
    current = left
    while current != right:
        current = (current + 1) % count
        values.append(current)
        if len(values) > count + 1:
            raise RuntimeError("invalid closed-contour interval")
    return values


def chord_parameters(points, indices):
    values = [0.0]
    for offset in range(1, len(indices)):
        values.append(values[-1] + distance(points[indices[offset - 1]], points[indices[offset]]))
    total = values[-1]
    if total <= EPSILON:
        return [offset / float(max(1, len(indices) - 1)) for offset in range(len(indices))]
    return [value / total for value in values]


def endpoint_tangent(points, index, forward, window=3):
    count = len(points)
    other = (index + window if forward else index - window) % count
    vector = subtract(points[other], points[index])
    return normalize(vector)


def fit_segment(points, left, right):
    indices = cyclic_indices(len(points), left, right)
    start, end = points[left], points[right]
    parameters = chord_parameters(points, indices)
    line_error = 0.0
    line_max_index = indices[len(indices) // 2]
    for offset, point_index in enumerate(indices[1:-1], start=1):
        fitted = lerp(start, end, parameters[offset])
        error = distance(points[point_index], fitted)
        if error > line_error:
            line_error, line_max_index = error, point_index

    tangent1 = endpoint_tangent(points, left, True)
    tangent2 = endpoint_tangent(points, right, False)
    c00 = c01 = c11 = x0 = x1 = 0.0
    for offset, point_index in enumerate(indices[1:-1], start=1):
        t = parameters[offset]
        u = 1.0 - t
        a1 = multiply(tangent1, 3.0 * u * u * t)
        a2 = multiply(tangent2, 3.0 * u * t * t)
        base = add(multiply(start, u ** 3 + 3.0 * u * u * t),
                   multiply(end, t ** 3 + 3.0 * u * t * t))
        residual = subtract(points[point_index], base)
        c00 += dot(a1, a1)
        c01 += dot(a1, a2)
        c11 += dot(a2, a2)
        x0 += dot(a1, residual)
        x1 += dot(a2, residual)
    determinant = c00 * c11 - c01 * c01
    chord = max(EPSILON, distance(start, end))
    if abs(determinant) <= EPSILON:
        alpha = beta = chord / 3.0
    else:
        alpha = (x0 * c11 - x1 * c01) / determinant
        beta = (c00 * x1 - c01 * x0) / determinant
    lower, upper = chord * 0.02, chord * 2.0
    if not (lower <= alpha <= upper and lower <= beta <= upper):
        alpha = beta = chord / 3.0
    control1 = add(start, multiply(tangent1, alpha))
    control2 = add(end, multiply(tangent2, beta))
    curve_error = 0.0
    curve_max_index = line_max_index
    squared = 0.0
    for offset, point_index in enumerate(indices[1:-1], start=1):
        fitted = cubic_point(start, control1, control2, end, parameters[offset])
        error = distance(points[point_index], fitted)
        squared += error * error
        if error > curve_error:
            curve_error, curve_max_index = error, point_index
    rms = math.sqrt(squared / max(1, len(indices) - 2))
    use_line = line_error <= 0.75 or curve_error >= line_error * 0.82
    return {
        "left": left, "right": right, "indices": indices,
        "line": use_line, "error": line_error if use_line else curve_error,
        "rms": line_error if use_line else rms,
        "split": line_max_index if use_line else curve_max_index,
        "control1": control1, "control2": control2,
    }


def anchor_turn(points, index, window=3):
    before = normalize(subtract(points[index], points[(index - window) % len(points)]))
    after = normalize(subtract(points[(index + window) % len(points)], points[index]))
    cosine = max(-1.0, min(1.0, dot(before, after)))
    return math.degrees(math.acos(cosine))


def initial_anchors(points):
    count = len(points)
    if count <= 3:
        return list(range(count))
    first = 0
    second = max(range(count), key=lambda index: distance(points[first], points[index]))
    third = max(
        range(count),
        key=lambda index: sf.point_line_distance(points[index], points[first], points[second]),
    )
    # Begin with the topology minimum. Sharp corners are then discovered by
    # worst-error splitting, preventing multi-contour glyphs from spending the
    # entire global budget during initialization.
    return sorted({first, second, third})


def segments_for(points, anchors):
    return [
        fit_segment(points, left, anchors[(offset + 1) % len(anchors)])
        for offset, left in enumerate(anchors)
    ]


def contour_cost(segments):
    return sum(1 if segment["line"] else 3 for segment in segments)


def build_ladders(point_sets, seed_anchors=None, max_points=MAX_POINTS, targets=None):
    states = []
    for contour_index, points in enumerate(point_sets):
        anchors = initial_anchors(points)
        if seed_anchors and contour_index < len(seed_anchors):
            proposed = sorted(set(anchors) | {
                int(index) % len(points) for index in seed_anchors[contour_index]
            })
            # Protected extrema/cap anchors are used only when the global
            # starting geometry remains within the hard point ceiling.
            trial = segments_for(points, proposed)
            prior = sum(contour_cost(state["segments"]) for state in states)
            if prior + contour_cost(trial) <= max_points:
                anchors = proposed
        segments = segments_for(points, anchors)
        states.append({"points": points, "anchors": anchors, "segments": segments})
    snapshots = []
    while True:
        total = sum(contour_cost(state["segments"]) for state in states)
        if total <= max_points:
            snapshots.append((total, [state["anchors"][:] for state in states]))
        proposals = []
        for contour_index, state in enumerate(states):
            splittable = [
                segment for segment in state["segments"]
                if segment["split"] not in state["anchors"] and len(segment["indices"]) > 3
            ]
            if not splittable:
                continue
            segment = max(splittable, key=lambda value: value["error"])
            split = segment["split"]
            trial_anchors = sorted(state["anchors"] + [split])
            trial_segments = segments_for(state["points"], trial_anchors)
            delta = contour_cost(trial_segments) - contour_cost(state["segments"])
            if total + delta <= max_points:
                proposals.append((segment["error"], -delta, contour_index, trial_anchors, trial_segments))
        if not proposals:
            break
        _error, _delta, contour_index, trial_anchors, trial_segments = max(proposals)
        states[contour_index]["anchors"] = trial_anchors
        states[contour_index]["segments"] = trial_segments
    # Keep one best geometry for each point count and evaluate a compact ladder.
    unique = {}
    for points, anchors in snapshots:
        unique[points] = anchors
    if targets is None:
        targets = (20, 30, 40, 49, 60, 70, 80, 90, 95, 100)
    chosen = {}
    for target in targets:
        eligible = [value for value in unique if value <= target]
        if eligible:
            value = max(eligible)
            chosen[value] = unique[value]
    if unique:
        chosen[max(unique)] = unique[max(unique)]
    return [(points, chosen[points]) for points in sorted(chosen)]


def classify_layer(layer):
    for contour in layer:
        values = list(contour)
        for index, point in enumerate(values):
            if not point.on_curve:
                continue
            previous = values[(index - 1) % len(values)]
            following = values[(index + 1) % len(values)]
            if previous.on_curve and following.on_curve:
                point.type = fontforge.splineCorner
            elif not previous.on_curve and not following.on_curve:
                incoming = normalize((point.x - previous.x, point.y - previous.y))
                outgoing = normalize((following.x - point.x, following.y - point.y))
                smooth = dot(incoming, outgoing) >= math.cos(math.radians(12.0))
                if not smooth:
                    point.type = fontforge.splineCorner
                elif abs(incoming[0]) <= 0.025 or abs(incoming[1]) <= 0.025:
                    point.type = fontforge.splineHVCurve
                else:
                    point.type = fontforge.splineCurve
            else:
                point.type = fontforge.splineTangent
    return layer


def build_layer(point_sets, anchors_by_contour):
    layer = fontforge.layer()
    layer.is_quadratic = False
    line_segments = curve_segments = 0
    for points, anchors in zip(point_sets, anchors_by_contour):
        segments = segments_for(points, anchors)
        contour = fontforge.contour()
        contour.is_quadratic = False
        contour.moveTo(*points[anchors[0]])
        for segment in segments:
            end = points[segment["right"]]
            if segment["line"]:
                contour.lineTo(*end)
                line_segments += 1
            else:
                contour.cubicTo(segment["control1"], segment["control2"], end)
                curve_segments += 1
        contour.closed = True
        layer += contour
    return classify_layer(layer), line_segments, curve_segments


def build_polygon_fallback(point_sets):
    layer = fontforge.layer()
    layer.is_quadratic = False
    lines = 0
    contour_limit = max(3, MAX_POINTS // max(1, len(point_sets)))
    for points in point_sets:
        anchors = initial_anchors(points)
        while len(anchors) < min(contour_limit, len(points)):
            ordered = sorted(anchors)
            best = None
            for offset, left in enumerate(ordered):
                right = ordered[(offset + 1) % len(ordered)]
                for index in cyclic_indices(len(points), left, right)[1:-1]:
                    error = sf.point_line_distance(points[index], points[left], points[right])
                    if best is None or error > best[0]:
                        best = (error, index)
            if best is None:
                break
            anchors.append(best[1]); anchors.sort()
        contour = fontforge.contour(); contour.is_quadratic = False
        contour.moveTo(*points[anchors[0]])
        for index in anchors[1:]:
            contour.lineTo(*points[index]); lines += 1
        contour.lineTo(*points[anchors[0]]); lines += 1
        contour.closed = True; layer += contour
    return classify_layer(layer), lines, 0


def layer_hash(layer):
    values = []
    for contour in layer:
        values.append([
            [round(point.x, 4), round(point.y, 4), bool(point.on_curve), int(point.type)]
            for point in contour
        ])
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode("utf-8")).hexdigest()


def point_type_counts(layer):
    counts = {"corner": 0, "curve": 0, "hvcurve": 0, "tangent": 0, "off": 0}
    for contour in layer:
        for point in contour:
            if not point.on_curve:
                counts["off"] += 1
            elif point.type == fontforge.splineCurve:
                counts["curve"] += 1
            elif point.type == fontforge.splineHVCurve:
                counts["hvcurve"] += 1
            elif point.type == fontforge.splineTangent:
                counts["tangent"] += 1
            else:
                counts["corner"] += 1
    return counts


def invalid_handles(layer):
    invalid = 0
    for contour in layer:
        values = list(contour)
        for index, point in enumerate(values):
            if not point.on_curve or point.type == fontforge.splineCorner:
                continue
            previous = values[(index - 1) % len(values)]
            following = values[(index + 1) % len(values)]
            if previous.on_curve and following.on_curve:
                invalid += 1
    return invalid


def boundary_distances(original_sets, candidate_sets, bbox):
    diagonal = max(1.0, math.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1]))
    # A bounded, evenly distributed polyline sample preserves the normalized
    # P95/max gate while avoiding quadratic work on dense source outlines.
    original_targets = [
        contour[::max(1, len(contour) // 400)] for contour in original_sets
    ]
    candidate_targets = [
        contour[::max(1, len(contour) // 400)] for contour in candidate_sets
    ]
    values = []
    for contour in original_sets:
        step = max(1, len(contour) // 100)
        for point in contour[::step]:
            values.append(sf.distance_to_sets(point, candidate_targets) / diagonal)
    for contour in candidate_sets:
        step = max(1, len(contour) // 100)
        for point in contour[::step]:
            values.append(sf.distance_to_sets(point, original_targets) / diagonal)
    values.sort()
    if not values:
        return 0.0, 0.0
    return values[min(len(values) - 1, int(len(values) * 0.95))], values[-1]


def curve_coverage(point_sets, layer):
    total = represented = 0.0
    for points, contour in zip(point_sets, layer):
        if len(points) < 4:
            continue
        diagonal = max(1.0, math.hypot(
            max(p[0] for p in points) - min(p[0] for p in points),
            max(p[1] for p in points) - min(p[1] for p in points),
        ))
        step = max(1, len(points) // 200)
        for index in range(0, len(points), step):
            turn = anchor_turn(points, index, min(3, max(1, len(points) // 20)))
            length = distance(points[index], points[(index + step) % len(points)])
            if turn >= 3.0 and length >= diagonal * 0.001:
                total += length
        visible_controls = sum(1 for point in contour if not point.on_curve)
        if visible_controls:
            represented += total - represented
    return 1.0 if total <= EPSILON else min(1.0, represented / total)


def metric_context(point_sets):
    bbox = sf.padded_bbox(sf.bbox_of_sets(point_sets))
    original_128 = sf.rasterize(point_sets, bbox, 128)
    return {
        "bbox": bbox,
        "original_128": original_128,
        "original_masks": {
            size: sf.downsample(original_128, 128, size) for size in RASTER_SIZES
        },
        "source_topologies": {128: sf.topology(original_128, 128)},
    }


def metric_layer_point_sets(layer):
    pen = sf.FlattenPen(error=1.0, spacing=4.0)
    layer.draw(pen)
    if pen.points:
        pen.endPath()
    return pen.contours


def raster_and_topology(point_sets, layer, context, full_audit=False):
    bbox = context["bbox"]
    original_128 = context["original_128"]
    candidate_sets = metric_layer_point_sets(layer)
    candidate_128 = sf.rasterize(candidate_sets, bbox, 128)
    per_size = {}
    failures = []
    worst_size = 32
    worst_score = -1.0
    for size in RASTER_SIZES:
        original = context["original_masks"][size]
        candidate = sf.downsample(candidate_128, 128, size)
        metrics = sf.raster_metrics(original, candidate)
        per_size[size] = metrics
        score = max(
            metrics["mse"] / MAX_MSE,
            MIN_IOU / max(EPSILON, metrics["ink_iou"]),
            metrics["false_positive_ink"] / MAX_FALSE_INK,
            metrics["false_negative_ink"] / MAX_FALSE_INK,
        )
        if score > worst_score:
            worst_score, worst_size = score, size
        if metrics["mse"] > MAX_MSE:
            failures.append("mse-{}".format(size))
        if metrics["ink_iou"] < MIN_IOU:
            failures.append("iou-{}".format(size))
        if metrics["false_positive_ink"] > MAX_FALSE_INK:
            failures.append("extra-ink-{}".format(size))
        if metrics["false_negative_ink"] > MAX_FALSE_INK:
            failures.append("missing-ink-{}".format(size))
    topologies = {}
    topology_match = True
    audit_sizes = TOPOLOGY_SIZES if full_audit else (128, 256)
    if not full_audit:
        audit_sizes = (128,)
    fast_topologies = None
    fast_python = os.environ.get("REDRAW_FAST_PYTHON")
    if full_audit and fast_python:
        payload = json.dumps({
            "bbox": bbox, "sizes": [size for size in audit_sizes if size > 128],
            "sets": {"source": point_sets, "candidate": candidate_sets},
        })
        completed = subprocess.run(
            [fast_python, str(ROOT / "tools" / "fast_highres_raster.py"), "--topology-stdin"],
            input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if completed.returncode == 0:
            fast_topologies = json.loads(completed.stdout)
    for size in audit_sizes:
        if size > 128 and fast_topologies is not None:
            source_topology = tuple(fast_topologies["source"][str(size)])
            candidate_topology = tuple(fast_topologies["candidate"][str(size)])
            context["source_topologies"][size] = source_topology
            topologies[size] = (source_topology, candidate_topology)
            if source_topology != candidate_topology:
                topology_match = False
                failures.append("topology-{}".format(size))
            continue
        if size == 128:
            candidate_mask = candidate_128
        else:
            candidate_mask = sf.rasterize(candidate_sets, bbox, size)
        source_topology = context["source_topologies"].get(size)
        if source_topology is None:
            original_mask = sf.rasterize(point_sets, bbox, size)
            source_topology = sf.topology(original_mask, size)
            context["source_topologies"][size] = source_topology
        candidate_topology = sf.topology(candidate_mask, size)
        topologies[size] = (source_topology, candidate_topology)
        if source_topology != candidate_topology:
            topology_match = False
            failures.append("topology-{}".format(size))
    p95, maximum = boundary_distances(point_sets, candidate_sets, bbox)
    if p95 > MAX_BOUNDARY_P95:
        failures.append("boundary-p95")
    if maximum > MAX_BOUNDARY:
        failures.append("boundary-max")
    return {
        "candidate_sets": candidate_sets, "per_size": per_size,
        "topologies": topologies, "topology_match": topology_match,
        "failures": failures, "worst_size": worst_size,
        "boundary_p95": p95, "boundary_max": maximum,
    }


def candidate_key(candidate):
    metrics = candidate["metrics"]
    worst = max(
        max(values["mse"] / MAX_MSE,
            MIN_IOU / max(EPSILON, values["ink_iou"]),
            values["false_positive_ink"] / MAX_FALSE_INK,
            values["false_negative_ink"] / MAX_FALSE_INK)
        for values in metrics["per_size"].values()
    )
    return (
        0 if metrics["topology_match"] else 1,
        candidate["self_intersections"], candidate["invalid_handles"],
        worst, metrics["boundary_p95"], metrics["boundary_max"], candidate["points"],
    )


def evaluate_layer(point_sets, layer, lines, curves, context, full_audit=False,
                   max_points=MAX_POINTS):
    points = sum(len(contour) for contour in layer)
    metrics = raster_and_topology(
        point_sets, layer, context, full_audit=full_audit
    )
    intersections = sum(1 for contour in layer if contour.selfIntersects())
    handles = invalid_handles(layer)
    coverage = curve_coverage(point_sets, layer)
    failures = list(metrics["failures"])
    if points > max_points:
        failures.append("point-budget")
    if intersections:
        failures.append("self-intersection")
    if handles:
        failures.append("invalid-handles")
    if coverage < MIN_CURVE_COVERAGE:
        failures.append("curve-coverage")
    return {
        "layer": layer, "points": points, "metrics": metrics,
        "line_segments": lines, "curve_segments": curves,
        "self_intersections": intersections, "invalid_handles": handles,
        "curve_coverage": coverage, "failures": sorted(set(failures)),
        "passes": not failures,
    }


def empty_row(glyph, source_hash):
    return {
        "glyph": glyph.glyphname,
        "codepoint": glyph.unicode if glyph.unicode >= 0 else "",
        "char": chr(glyph.unicode) if glyph.unicode >= 0 else "",
        "source_points": 0, "redrawn_points": 0,
        "source_contours": 0, "redrawn_contours": 0, "simple": "true",
        "automatic_pass": "true", "manual_status": "", "effective_status": "automatic-pass",
        "needs_manual_review": "false", "failure_reasons": "", "candidate_count": 0,
        "selected_budget": 0, "source_hash": source_hash, "candidate_hash": source_hash,
        "corner_points": 0, "curve_points": 0, "hvcurve_points": 0,
        "tangent_points": 0, "off_curve_points": 0, "line_segments": 0,
        "curve_segments": 0, "curve_arc_coverage": "1.000000",
        "boundary_p95": "0.000000", "boundary_max": "0.000000",
        "self_intersections": 0, "invalid_handles": 0,
        "topology_status": "empty", "component_delta": 0, "counter_delta": 0,
        "worst_raster_size": 32,
    }


def load_decisions():
    if not DECISIONS.exists():
        return {"version": "redraw-manual-review-v1", "decisions": {}}
    try:
        values = json.loads(DECISIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": "redraw-manual-review-v1", "decisions": {}}
    values.setdefault("version", "redraw-manual-review-v1")
    values.setdefault("decisions", {})
    return values


def apply_manual_decision(row, decisions):
    decision = decisions.get("decisions", {}).get(row["glyph"], {})
    valid = (
        decision.get("source_hash") == row["source_hash"]
        and decision.get("candidate_hash") == row["candidate_hash"]
        and decision.get("status") in ("pass", "almost_done", "needs_rework")
    )
    row["manual_status"] = decision.get("status", "") if valid else ""
    structural = any(reason.startswith("topology-") or reason in (
        "point-budget", "self-intersection", "invalid-handles"
    ) for reason in row["failure_reasons"].split(";") if reason)
    if valid and decision["status"] == "needs_rework":
        row["effective_status"] = "needs-rework"
        row["needs_manual_review"] = "true"
    elif row["automatic_pass"] == "true":
        row["effective_status"] = "automatic-pass"
        row["needs_manual_review"] = "false"
    elif valid and decision["status"] == "pass" and not structural:
        row["effective_status"] = "manual-pass"
        row["needs_manual_review"] = "false"
    elif valid and decision["status"] == "almost_done":
        row["effective_status"] = "almost-done"
        row["needs_manual_review"] = "true"
    else:
        row["effective_status"] = "unreviewed"
        row["needs_manual_review"] = "true"


def result_row(glyph, source_layer, candidate, candidate_count, decisions):
    counts = point_type_counts(candidate["layer"])
    metrics = candidate["metrics"]
    first_topology = metrics["topologies"][128]
    row = {
        "glyph": glyph.glyphname,
        "codepoint": glyph.unicode if glyph.unicode >= 0 else "",
        "char": chr(glyph.unicode) if glyph.unicode >= 0 else "",
        "source_points": sum(len(contour) for contour in source_layer),
        "redrawn_points": candidate["points"],
        "source_contours": len(source_layer), "redrawn_contours": len(candidate["layer"]),
        "simple": "true" if candidate["passes"] and candidate["points"] <= SIMPLE_MAX_POINTS else "false",
        "automatic_pass": "true" if candidate["passes"] else "false",
        "manual_status": "", "effective_status": "", "needs_manual_review": "",
        "failure_reasons": ";".join(candidate["failures"]),
        "candidate_count": candidate_count, "selected_budget": candidate["points"],
        "source_hash": layer_hash(source_layer), "candidate_hash": layer_hash(candidate["layer"]),
        "corner_points": counts["corner"], "curve_points": counts["curve"],
        "hvcurve_points": counts["hvcurve"], "tangent_points": counts["tangent"],
        "off_curve_points": counts["off"], "line_segments": candidate["line_segments"],
        "curve_segments": candidate["curve_segments"],
        "curve_arc_coverage": "{:.6f}".format(candidate["curve_coverage"]),
        "boundary_p95": "{:.6f}".format(metrics["boundary_p95"]),
        "boundary_max": "{:.6f}".format(metrics["boundary_max"]),
        "self_intersections": candidate["self_intersections"],
        "invalid_handles": candidate["invalid_handles"],
        "topology_status": "match" if metrics["topology_match"] else "mismatch",
        "component_delta": first_topology[1][0] - first_topology[0][0],
        "counter_delta": first_topology[1][1] - first_topology[0][1],
        "worst_raster_size": metrics["worst_size"],
    }
    for size, values in metrics["per_size"].items():
        row["mse_{}".format(size)] = "{:.6f}".format(values["mse"])
        row["ink_iou_{}".format(size)] = "{:.6f}".format(values["ink_iou"])
        row["false_positive_ink_{}".format(size)] = "{:.6f}".format(values["false_positive_ink"])
        row["false_negative_ink_{}".format(size)] = "{:.6f}".format(values["false_negative_ink"])
    for size, (source_topology, candidate_topology) in metrics["topologies"].items():
        row["source_components_{}".format(size)] = source_topology[0]
        row["source_counters_{}".format(size)] = source_topology[1]
        row["candidate_components_{}".format(size)] = candidate_topology[0]
        row["candidate_counters_{}".format(size)] = candidate_topology[1]
    apply_manual_decision(row, decisions)
    return row


def copy_geometry(target, source_layer):
    target.foreground = source_layer.dup()
    target.width = target.width


def redraw_glyph(glyph, source_layer):
    point_sets = [
        sf.resample_polyline(contour, 4.0, closed=True)
        for contour in sf.layer_point_sets(source_layer)
    ]
    context = metric_context(point_sets)
    candidates = []
    for _budget, anchors in build_ladders(point_sets):
        layer, lines, curves = build_layer(point_sets, anchors)
        candidate = evaluate_layer(point_sets, layer, lines, curves, context)
        candidates.append(candidate)
        if candidate["passes"] and candidate["points"] <= SIMPLE_MAX_POINTS:
            break
    if not candidates:
        layer, lines, curves = build_polygon_fallback(point_sets)
        candidates.append(evaluate_layer(point_sets, layer, lines, curves, context))
    passing = [candidate for candidate in candidates if candidate["passes"]]
    selected = min(passing, key=lambda value: value["points"]) if passing else min(candidates, key=candidate_key)
    # The expensive 512/1024 topology checks run once, on the selected outline.
    selected = evaluate_layer(
        point_sets, selected["layer"], selected["line_segments"],
        selected["curve_segments"], context, full_audit=True,
    )
    return selected, len(candidates)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_report(rows):
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with REPORT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def refresh_saved_rows(rows):
    saved = fontforge.open(str(OUTPUT_SFD))
    decisions = load_decisions()
    try:
        for row in rows:
            glyph = saved[row["glyph"]]
            layer = glyph.foreground
            counts = point_type_counts(layer)
            row["redrawn_points"] = sum(len(contour) for contour in layer)
            row["redrawn_contours"] = len(layer)
            row["candidate_hash"] = layer_hash(layer)
            row["corner_points"] = counts["corner"]
            row["curve_points"] = counts["curve"]
            row["hvcurve_points"] = counts["hvcurve"]
            row["tangent_points"] = counts["tangent"]
            row["off_curve_points"] = counts["off"]
            apply_manual_decision(row, decisions)
    finally:
        saved.close()


def freeze_checkpoint(rows):
    CHECKPOINT.mkdir(parents=True, exist_ok=True)
    files = {
        "redrawn.sfd": OUTPUT_SFD, "redrawn.ttf": OUTPUT_TTF,
        "redraw-review.sfd": REVIEW_SFD, "redraw-review.ttf": REVIEW_TTF,
        "redraw-report.csv": REPORT,
    }
    manifest_path = CHECKPOINT / "manifest.json"
    manifest = {
        "version": CHECKPOINT.name,
        "created": datetime.now(timezone.utc).isoformat(),
        "source": {"path": str(SOURCE_SFD.relative_to(ROOT)), "sha256": sha256(SOURCE_SFD)},
        "glyphs": len(rows),
        "automatic_passes": sum(row["automatic_pass"] == "true" for row in rows),
        "manual_passes": sum(row["effective_status"] == "manual-pass" for row in rows),
        "unresolved": sum(row["needs_manual_review"] == "true" for row in rows),
        "files": {},
    }
    for filename, source in files.items():
        destination = CHECKPOINT / filename
        shutil.copy2(source, destination)
        manifest["files"][filename] = sha256(destination)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def worker_build(name, worker_sfd, worker_json):
    source = fontforge.open(str(SOURCE_SFD))
    original = fontforge.open(str(SOURCE_TTF))
    decisions = load_decisions()
    try:
        source_names = {glyph.glyphname for glyph in source.glyphs()}
        reference = source[name] if name in source_names else original[name]
        source_layer = reference.foreground.dup()
        if not len(source_layer):
            row = empty_row(reference, layer_hash(source_layer))
            apply_manual_decision(row, decisions)
            layer = source_layer
        else:
            candidate, count = redraw_glyph(reference, source_layer)
            layer = candidate["layer"]
            row = result_row(reference, source_layer, candidate, count, decisions)
        sf.save_worker_glyph(reference, layer, worker_sfd)
        worker_json.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    finally:
        source.close()
        original.close()
    return 0


def parallel_build(workers, no_checkpoint=False):
    output = fontforge.open(str(SOURCE_SFD))
    original = fontforge.open(str(SOURCE_TTF))
    names = [glyph.glyphname for glyph in original.glyphs()]
    output_names = {glyph.glyphname for glyph in output.glyphs()}
    for name in names:
        if name not in output_names:
            created = output.createChar(-1, name)
            created.foreground = original[name].foreground.dup()
            created.width = original[name].width
    build_key = hashlib.sha256(
        SOURCE_SFD.read_bytes() + Path(__file__).read_bytes()
    ).hexdigest()[:16]
    temporary = WORK_ROOT / build_key
    temporary.mkdir(parents=True, exist_ok=True)
    results = {}

    def launch(index, name):
        stem = "{:04d}".format(index)
        sfd = temporary / (stem + ".sfd")
        data = temporary / (stem + ".json")
        command = [
            sys.executable, str(Path(__file__).resolve()), "--worker-glyph", name,
            "--worker-sfd", str(sfd), "--worker-json", str(data),
        ]
        last_output = ""
        for attempt in range(3):
            complete_outputs = sfd.exists() and data.exists()
            if complete_outputs:
                try:
                    json.loads(data.read_text(encoding="utf-8"))
                    if sfd.stat().st_size < 256:
                        raise ValueError("truncated worker SFD")
                    return index, name, sfd, data
                except Exception:
                    complete_outputs = False
            if attempt or not complete_outputs:
                for path in (sfd, data):
                    try:
                        path.unlink()
                    except OSError:
                        pass
            completed = subprocess.run(
                command, cwd=str(ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True,
            )
            last_output = completed.stdout
            complete_outputs = sfd.exists() and data.exists()
            if complete_outputs:
                try:
                    json.loads(data.read_text(encoding="utf-8"))
                    if sfd.stat().st_size < 256:
                        raise ValueError("truncated worker SFD")
                except Exception:
                    complete_outputs = False
            if completed.returncode == 0 or complete_outputs:
                return index, name, sfd, data
        raise RuntimeError("{} worker failed after 3 attempts:\n{}".format(
            name, last_output[-4000:]))

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(launch, index, name): name
                for index, name in enumerate(names)
            }
            complete = 0
            for future in as_completed(futures):
                index, name, sfd, data = future.result()
                results[index] = (name, sfd, data)
                complete += 1
                if complete == 1 or complete % 10 == 0 or complete == len(names):
                    print("redraw progress: {}/{}".format(complete, len(names)), flush=True)
        rows = []
        for index in range(len(names)):
            name, sfd_path, json_path = results[index]
            worker_font = fontforge.open(str(sfd_path))
            try:
                output[name].foreground = worker_font[name].foreground.dup()
            finally:
                worker_font.close()
            rows.append(json.loads(json_path.read_text(encoding="utf-8")))
        OUTPUT_SFD.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_TTF.parent.mkdir(parents=True, exist_ok=True)
        output.save(str(OUTPUT_SFD))
        output.generate(str(OUTPUT_TTF))
        completed = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "restore_redraw_metadata.py"),
             str(SOURCE_TTF), str(OUTPUT_TTF)],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        if completed.returncode:
            raise RuntimeError("metadata restore failed: {}".format(completed.stdout[-2000:]))
        refresh_saved_rows(rows)
        shutil.copy2(OUTPUT_SFD, REVIEW_SFD)
        shutil.copy2(OUTPUT_TTF, REVIEW_TTF)
        write_report(rows)
        if not DECISIONS.exists():
            DECISIONS.write_text(json.dumps(empty_decisions(), indent=2), encoding="utf-8")
        if not no_checkpoint:
            freeze_checkpoint(rows)
        print("glyphs={}; automatic_passes={}; review={}".format(
            len(rows), sum(row["automatic_pass"] == "true" for row in rows),
            sum(row["needs_manual_review"] == "true" for row in rows),
        ))
    finally:
        output.close()
        original.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--glyph", action="append", help="Only rebuild this glyph; repeat as needed.")
    parser.add_argument("--workers", type=int, default=min(8, max(1, os.cpu_count() or 1)))
    parser.add_argument("--no-checkpoint", action="store_true")
    parser.add_argument("--worker-glyph", help=argparse.SUPPRESS)
    parser.add_argument("--worker-sfd", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-json", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker_glyph:
        return worker_build(args.worker_glyph, args.worker_sfd, args.worker_json)
    selected_names = set(args.glyph or [])
    if not selected_names and args.workers > 1:
        return parallel_build(args.workers, no_checkpoint=args.no_checkpoint)
    source = fontforge.open(str(SOURCE_SFD))
    original = fontforge.open(str(SOURCE_TTF))
    output = source
    output_names = {glyph.glyphname for glyph in output.glyphs()}
    original_order = [glyph.glyphname for glyph in original.glyphs()]
    for name in original_order:
        if name not in output_names:
            created = output.createChar(-1, name)
            created.foreground = original[name].foreground.dup()
            created.width = original[name].width
    previous = fontforge.open(str(OUTPUT_SFD)) if selected_names and OUTPUT_SFD.exists() else None
    previous_rows = {}
    if selected_names and REPORT.exists():
        with REPORT.open(encoding="utf-8-sig", newline="") as handle:
            previous_rows = {row["glyph"]: row for row in csv.DictReader(handle)}
    decisions = load_decisions()
    rows = []
    try:
        source_names = output_names
        for name in original_order:
            glyph = output[name]
            if selected_names and name not in selected_names:
                if previous is not None:
                    glyph.foreground = previous[name].foreground.dup()
                if name in previous_rows:
                    rows.append(previous_rows[name])
                continue
            reference = source[name] if name in source_names else original[name]
            source_layer = reference.foreground.dup()
            if not len(source_layer):
                glyph.foreground = source_layer
                row = empty_row(glyph, layer_hash(source_layer))
                apply_manual_decision(row, decisions)
            else:
                candidate, count = redraw_glyph(glyph, source_layer)
                glyph.foreground = candidate["layer"]
                row = result_row(glyph, source_layer, candidate, count, decisions)
            rows.append(row)
            print("{}: {} points, {}".format(name, row["redrawn_points"], row["effective_status"]), flush=True)
        if selected_names:
            missing = selected_names - {glyph.glyphname for glyph in output.glyphs()}
            if missing:
                raise RuntimeError("unknown glyphs: {}".format(" ".join(sorted(missing))))
        OUTPUT_SFD.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_TTF.parent.mkdir(parents=True, exist_ok=True)
        output.save(str(OUTPUT_SFD))
        output.generate(str(OUTPUT_TTF))
        completed = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "restore_redraw_metadata.py"),
             str(SOURCE_TTF), str(OUTPUT_TTF)],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        if completed.returncode:
            raise RuntimeError("metadata restore failed: {}".format(completed.stdout[-2000:]))
        refresh_saved_rows(rows)
        # Review fonts intentionally contain the complete font so browser text
        # rendering and glyph navigation retain the original cmap and metrics.
        shutil.copy2(OUTPUT_SFD, REVIEW_SFD)
        shutil.copy2(OUTPUT_TTF, REVIEW_TTF)
        write_report(rows)
        if not DECISIONS.exists():
            DECISIONS.write_text(json.dumps(decisions, indent=2), encoding="utf-8")
        if not args.no_checkpoint and not selected_names:
            freeze_checkpoint(rows)
    finally:
        source.close()
        original.close()
        if previous is not None:
            try:
                previous.close()
            except RuntimeError:
                pass
    print("glyphs={}; automatic_passes={}; review={}".format(
        len(rows), sum(row["automatic_pass"] == "true" for row in rows),
        sum(row["needs_manual_review"] == "true" for row in rows),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
