#!/usr/bin/env python3
"""Build a source-derived curvature review font without changing production.

Run with FontForge's Python runtime:
  ffpython tools/restore_curvature.py
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

import fontforge


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import simplify_font as sf


ORIGINAL_SFD = ROOT / "fontforge" / "original.sfd"
ORIGINAL_TTF = ROOT / "qa" / "assets" / "original.ttf"
BASELINE_SFD = ROOT / "checkpoints" / "simplified-v126" / "simplified.sfd"
BASELINE_TTF = ROOT / "checkpoints" / "simplified-v126" / "simplified.ttf"
BASELINE_REPORT = ROOT / "checkpoints" / "simplified-v126" / "glyph-report.csv"
OUTPUT_SFD = ROOT / "fontforge" / "curvature-candidates.sfd"
OUTPUT_TTF = ROOT / "qa" / "assets" / "curvature-candidates.ttf"
OUTPUT_REPORT = ROOT / "qa" / "assets" / "curvature-report.csv"
REVIEW_SFD = ROOT / "fontforge" / "curvature-review.sfd"
REVIEW_TTF = ROOT / "qa" / "assets" / "curvature-review.ttf"
NORMALIZED_SOURCE_SFD = ROOT / "fontforge" / "cleanup-attempts.sfd"
CURVATURE_V1_CHECKPOINT = ROOT / "checkpoints" / "curvature-v1"

CORNER_RATIO_THRESHOLD = 0.75
MIN_LINE_DEVIATION = 0.6
MIN_RMS_IMPROVEMENT = 0.20
CORNER_ANGLE_DEGREES = 15.0
BUDGETS = (80,)
STRATEGIES = ("complexity", "perimeter", "area", "balanced")

CURVATURE_FIELDS = [
    "curvature_targeted", "curvature_status", "curvature_policy",
    "contour_mapping_status", "curvature_points", "curvature_source_runs",
    "curvature_source_fit_improvement", "curvature_metric_pass",
    "baseline_corner_points", "baseline_curve_points", "baseline_hvcurve_points",
    "baseline_tangent_points", "baseline_off_curve_points", "baseline_corner_ratio",
    "curvature_corner_points", "curvature_curve_points", "curvature_hvcurve_points",
    "curvature_tangent_points", "curvature_off_curve_points", "curvature_corner_ratio",
    "curvature_mse", "curvature_ink_iou", "curvature_false_positive_ink",
    "curvature_false_negative_ink", "curvature_sample_delta",
    "curvature_component_delta", "curvature_counter_delta",
    "curvature_topology_status", "curvature_worst_raster_size",
    "curvature_mse_delta", "curvature_ink_iou_delta",
    "curvature_false_positive_delta", "curvature_false_negative_delta",
    "curvature_sample_delta_change", "curvature_fit_mode",
    "raw_points", "safe_points", "raw_status", "safe_status", "fallback_reason",
    "safe_corner_points", "safe_curve_points", "safe_hvcurve_points",
    "safe_tangent_points", "safe_off_curve_points", "safe_corner_ratio",
    "raw_self_intersections", "baseline_self_intersections",
    "raw_components_512", "raw_counters_512", "raw_components_1024", "raw_counters_1024",
    "baseline_components_512", "baseline_counters_512", "baseline_components_1024", "baseline_counters_1024",
    "original_components_512", "original_counters_512", "original_components_1024", "original_counters_1024",
    "raw_micro_components", "raw_micro_counters", "baseline_micro_components", "baseline_micro_counters",
    "raw_largest_extra_region", "baseline_largest_extra_region",
    "raw_largest_missing_region", "baseline_largest_missing_region",
    "raw_boundary_p95", "baseline_boundary_p95", "raw_boundary_max", "baseline_boundary_max",
    "raw_cusp_count", "baseline_cusp_count",
    "raw_mse", "raw_ink_iou", "raw_false_positive_ink", "raw_false_negative_ink",
    "safe_mse", "safe_ink_iou", "safe_false_positive_ink", "safe_false_negative_ink",
]

COMPLETE_FIELDS = [
    "complete_status", "complete_mapping_mode", "significant_source_runs",
    "represented_curve_runs", "curved_run_coverage", "handle_valid",
    "attempted_curve_counts", "rejection_history", "complete_source_error",
    "complete_audit_status", "complete_audit_reason",
]

RELATIVE_MSE = 0.002
RELATIVE_IOU = 0.015
RELATIVE_INK = 0.015
LOCAL_REGION_RATIO = 1.25
LOCAL_REGION_DELTA = 0.005
BOUNDARY_P95_DELTA = 0.002
BOUNDARY_MAX_DELTA = 0.005
MICRO_REGION_RATIO = 0.0005


def point_type_counts(glyph):
    values = {"corner": 0, "curve": 0, "hvcurve": 0, "tangent": 0, "off": 0}
    for contour in glyph.foreground:
        for point in contour:
            if not point.on_curve:
                values["off"] += 1
            elif point.type == fontforge.splineCurve:
                values["curve"] += 1
            elif point.type == fontforge.splineHVCurve:
                values["hvcurve"] += 1
            elif point.type == fontforge.splineTangent:
                values["tangent"] += 1
            else:
                values["corner"] += 1
    on_curve = sum(values[name] for name in ("corner", "curve", "hvcurve", "tangent"))
    values["total"] = on_curve + values["off"]
    values["ratio"] = values["corner"] / float(max(1, on_curve))
    return values


def outline_signature(glyph):
    return tuple(
        tuple(
            (round(point.x, 4), round(point.y, 4), bool(point.on_curve))
            for point in contour
        )
        for contour in glyph.foreground
    )


def is_target(glyph):
    if glyph.glyphname == ".notdef":
        return False
    counts = point_type_counts(glyph)
    return counts["total"] > 0 and counts["ratio"] >= CORNER_RATIO_THRESHOLD


def contour_stats(points):
    area = sf.signed_area(points)
    bbox = sf.bbox_of_sets([points])
    centroid = (
        sum(point[0] for point in points) / max(1, len(points)),
        sum(point[1] for point in points) / max(1, len(points)),
    )
    return area, bbox, centroid


def contour_match_cost(left, right, scale):
    left_area, left_bbox, left_center = contour_stats(left)
    right_area, right_bbox, right_center = contour_stats(right)
    area_cost = abs(math.log(max(sf.EPSILON, abs(left_area)) / max(sf.EPSILON, abs(right_area))))
    center_cost = sf.distance(left_center, right_center) / max(1.0, scale)
    bbox_cost = sum(abs(a - b) for a, b in zip(left_bbox, right_bbox)) / max(1.0, scale * 4.0)
    winding_cost = 2.0 if left_area * right_area < 0 else 0.0
    return area_cost + center_cost + bbox_cost + winding_cost


def match_contours(original_sets, baseline_sets):
    if len(original_sets) != len(baseline_sets):
        return None, "contour-count-mismatch"
    if not original_sets:
        return [], "empty"
    bbox = sf.bbox_of_sets(original_sets + baseline_sets)
    scale = math.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1])
    pairs = []
    for baseline_index, baseline in enumerate(baseline_sets):
        for original_index, original in enumerate(original_sets):
            pairs.append((contour_match_cost(baseline, original, scale), baseline_index, original_index))
    pairs.sort()
    assigned_baseline = set()
    assigned_original = set()
    mapping = [None] * len(baseline_sets)
    for cost, baseline_index, original_index in pairs:
        if baseline_index in assigned_baseline or original_index in assigned_original:
            continue
        mapping[baseline_index] = original_index
        assigned_baseline.add(baseline_index)
        assigned_original.add(original_index)
    if any(index is None for index in mapping):
        return None, "assignment-failure"
    # Reject only clearly implausible pairings; symmetric contours are allowed.
    for baseline_index, original_index in enumerate(mapping):
        if contour_match_cost(baseline_sets[baseline_index], original_sets[original_index], scale) > 4.0:
            return None, "ambiguous-geometry"
    return [original_sets[index] for index in mapping], "matched"


def fast_removal_order(points):
    """Visvalingam-style removal order; final raster/topology gates provide safety."""
    count = len(points)
    if count <= 3:
        return []
    previous = [(index - 1) % count for index in range(count)]
    following = [(index + 1) % count for index in range(count)]
    active = [True] * count
    versions = [0] * count
    protected = set(sf.seed_indices(points))
    weights = sf.importance_weights(points)

    def score(index):
        if index in protected:
            return float("inf")
        geometric = sf.point_line_distance(points[index], points[previous[index]], points[following[index]])
        return geometric + weights[index] * 4.0

    heap = [(score(index), versions[index], index) for index in range(count)]
    heapq.heapify(heap)
    removed = []
    active_count = count
    while active_count > 3 and heap:
        _value, version, index = heapq.heappop(heap)
        if not active[index] or version != versions[index]:
            continue
        left, right = previous[index], following[index]
        active[index] = False
        following[left] = right
        previous[right] = left
        active_count -= 1
        removed.append(index)
        for neighbor in (left, right):
            versions[neighbor] += 1
            heapq.heappush(heap, (score(neighbor), versions[neighbor], neighbor))
    return removed


def path_parameters(points, path):
    distances = [0.0]
    for index in range(1, len(path)):
        distances.append(distances[-1] + sf.distance(points[path[index - 1]], points[path[index]]))
    total = max(sf.EPSILON, distances[-1])
    return [value / total for value in distances]


def quadratic_point(start, control, end, t):
    mt = 1.0 - t
    return (
        mt * mt * start[0] + 2.0 * mt * t * control[0] + t * t * end[0],
        mt * mt * start[1] + 2.0 * mt * t * control[1] + t * t * end[1],
    )


def line_point(start, end, t):
    return (start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t)


def rms_fit(points, path, control=None):
    start, end = points[path[0]], points[path[-1]]
    errors = []
    for point_index, t in zip(path, path_parameters(points, path)):
        fitted = line_point(start, end, t) if control is None else quadratic_point(start, control, end, t)
        errors.append(sf.distance(points[point_index], fitted) ** 2)
    return math.sqrt(sum(errors) / max(1, len(errors)))


def source_tangent(points, index, forward):
    count = len(points)
    if forward:
        start, end = points[index], points[(index + 1) % count]
    else:
        start, end = points[(index - 1) % count], points[index]
    length = max(sf.EPSILON, sf.distance(start, end))
    return ((end[0] - start[0]) / length, (end[1] - start[1]) / length)


def tangent_intersection(points, start_index, end_index, fallback):
    start, end = points[start_index], points[end_index]
    left = source_tangent(points, start_index, True)
    right = source_tangent(points, end_index, False)
    determinant = left[0] * right[1] - left[1] * right[0]
    if abs(determinant) < 1e-4:
        return fallback
    delta = (end[0] - start[0], end[1] - start[1])
    amount = (delta[0] * right[1] - delta[1] * right[0]) / determinant
    control = (start[0] + amount * left[0], start[1] + amount * left[1])
    samples = [points[index] for index in sf.open_path(points, start_index, end_index)]
    bbox = sf.bbox_of_sets([samples])
    pad = max(2.0, math.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1]) * 0.35)
    if not (bbox[0] - pad <= control[0] <= bbox[2] + pad and bbox[1] - pad <= control[1] <= bbox[3] + pad):
        return fallback
    return control


def tangent_angle(points, index):
    incoming = source_tangent(points, index, False)
    outgoing = source_tangent(points, index, True)
    dot = max(-1.0, min(1.0, incoming[0] * outgoing[0] + incoming[1] * outgoing[1]))
    return math.degrees(math.acos(dot))


def segment_fit(points, path):
    fallback, line_deviation = sf.fit_quadratic(points, path)
    control = tangent_intersection(points, path[0], path[-1], fallback)
    line_rms = rms_fit(points, path)
    curve_rms = rms_fit(points, path, control)
    improvement = 0.0 if line_rms <= sf.EPSILON else 1.0 - curve_rms / line_rms
    use_curve = line_deviation > MIN_LINE_DEVIATION and improvement >= MIN_RMS_IMPROVEMENT
    return control, line_rms, curve_rms, improvement, use_curve


def build_curvature_layer(contours, removal_orders, budget, strategy):
    safe_minimum = sum(max(3, len(contour) - len(order)) for contour, order in zip(contours, removal_orders))
    anchor_budget = max(safe_minimum, budget // 2)
    targets = sf.allocate_contour_targets(contours, removal_orders, anchor_budget, strategy)
    if targets is None:
        return None

    def render(local_targets):
        layer = fontforge.layer()
        layer.is_quadratic = True
        source_runs = 0
        improvements = []
        for contour_index, points in enumerate(contours):
            remove_count = max(0, len(points) - local_targets[contour_index])
            removed = set(removal_orders[contour_index][:remove_count])
            anchors = [index for index in range(len(points)) if index not in removed]
            if len(anchors) < 3:
                return None
            contour = fontforge.contour()
            contour.is_quadratic = True
            contour.moveTo(*points[anchors[0]])
            for offset, left in enumerate(anchors):
                right = anchors[(offset + 1) % len(anchors)]
                path = sf.open_path(points, left, right)
                control, _line_rms, _curve_rms, improvement, use_curve = segment_fit(points, path)
                if use_curve:
                    contour.quadraticTo(control, points[right])
                    source_runs += 1
                    improvements.append(improvement)
                else:
                    contour.lineTo(*points[right])
            contour.closed = True
            layer += contour
        return {
            "layer": layer,
            "points": sum(len(contour) for contour in layer),
            "source_runs": source_runs,
            "fit_improvement": sum(improvements) / max(1, len(improvements)),
            "fit_mode": "{}-quadratic".format(strategy),
        }

    probe = render(targets)
    if probe is None:
        return None
    probe_anchors = max(1, sum(targets))
    curve_rate = probe["source_runs"] / float(probe_anchors)
    desired_anchors = max(safe_minimum, int(budget / max(1.0, 1.0 + curve_rate)))
    targets = sf.allocate_contour_targets(contours, removal_orders, desired_anchors, strategy)
    if targets is None:
        return None
    best = render(targets)
    if best is None:
        return None
    # One correction normally lands within a point or two of the hard cap.
    correction = int((budget - best["points"]) / max(1.0, 1.0 + curve_rate))
    if correction:
        corrected_budget = max(safe_minimum, min(sum(len(contour) for contour in contours), sum(targets) + correction))
        corrected_targets = sf.allocate_contour_targets(
            contours, removal_orders, corrected_budget, strategy
        )
        corrected = render(corrected_targets) if corrected_targets is not None else None
        if corrected is not None and corrected["points"] <= budget:
            best = corrected
    if best["points"] > budget or best["source_runs"] == 0:
        return None
    return best


def angle_between(left, right):
    left_length = max(sf.EPSILON, math.hypot(left[0], left[1]))
    right_length = max(sf.EPSILON, math.hypot(right[0], right[1]))
    dot = (left[0] * right[0] + left[1] * right[1]) / (left_length * right_length)
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def classify_layer(layer):
    """Assign editable FontForge join types from the actual round-tripped geometry."""
    for contour in layer:
        points = list(contour)
        count = len(points)
        for index, point in enumerate(points):
            if not point.on_curve:
                continue
            previous = points[(index - 1) % count]
            following = points[(index + 1) % count]
            previous_vector = (previous.x - point.x, previous.y - point.y)
            following_vector = (following.x - point.x, following.y - point.y)
            aligned = abs(180.0 - angle_between(previous_vector, following_vector)) <= CORNER_ANGLE_DEGREES
            if not aligned:
                point.type = fontforge.splineCorner
            elif not previous.on_curve and not following.on_curve:
                point.type = (
                    fontforge.splineHVCurve
                    if min(abs(previous_vector[0]), abs(previous_vector[1])) < 0.015
                    else fontforge.splineCurve
                )
            elif previous.on_curve != following.on_curve:
                point.type = fontforge.splineTangent
            else:
                point.type = fontforge.splineCorner
    return layer


def metrics_context(original_glyph):
    original_sets = sf.layer_point_sets(original_glyph.foreground)
    bbox = sf.padded_bbox(sf.bbox_of_sets(original_sets))
    original_mask = sf.rasterize(original_sets, bbox)
    original_masks = {
        size: sf.downsample(original_mask, sf.RASTER_SIZE, size) for size in sf.RASTER_SIZES
    }
    topologies = sf.topology_at_sizes(original_sets, bbox)
    return original_sets, bbox, original_mask, original_masks, topologies


def evaluate(layer, context):
    original_sets, bbox, original_mask, original_masks, topologies = context
    return sf.evaluate_candidate(
        layer, original_sets, original_mask, original_masks, bbox,
        sf.persistent_topology(topologies), topologies,
    )


def standard_pass(metrics):
    return metrics["passes"]


def policy_for_baseline(metrics):
    if standard_pass(metrics):
        return "standard"
    if sf.hard_budget_metrics_pass(metrics):
        return "hard-budget"
    return "baseline-exception"


def policy_pass(metrics, policy):
    if policy == "standard":
        return standard_pass(metrics)
    if policy == "hard-budget":
        return sf.hard_budget_metrics_pass(metrics)
    return metrics["component_delta"] == 0 and metrics["counter_delta"] == 0


def baseline_metrics_from_row(row):
    return {
        "mse": float(row["mse"] or 0),
        "ink_iou": float(row["ink_iou"] or 1),
        "false_positive_ink": float(row["false_positive_ink"] or 0),
        "false_negative_ink": float(row["false_negative_ink"] or 0),
        "sample_delta": float(row["sample_delta"] or 0),
        "component_delta": int(row["component_delta"] or 0),
        "counter_delta": int(row["counter_delta"] or 0),
        "topology_status": row.get("topology_status", "pass"),
        "worst_raster_size": int(row.get("worst_raster_size") or 128),
    }


def policy_from_row(row):
    metrics = baseline_metrics_from_row(row)
    standard = (
        metrics["mse"] <= sf.MAX_MSE
        and metrics["ink_iou"] >= sf.MIN_INK_IOU
        and metrics["false_positive_ink"] <= sf.MAX_FALSE_POSITIVE_INK
        and metrics["false_negative_ink"] <= sf.MAX_FALSE_NEGATIVE_INK
        and metrics["component_delta"] == 0
        and metrics["counter_delta"] == 0
    )
    if standard:
        return "standard"
    hard = (
        metrics["mse"] <= sf.HARD_BUDGET_MAX_MSE
        and metrics["ink_iou"] >= sf.HARD_BUDGET_MIN_INK_IOU
        and metrics["false_positive_ink"] <= sf.HARD_BUDGET_MAX_FALSE_POSITIVE_INK
        and metrics["false_negative_ink"] <= sf.HARD_BUDGET_MAX_FALSE_NEGATIVE_INK
        and metrics["component_delta"] == 0
        and metrics["counter_delta"] == 0
    )
    return "hard-budget" if hard else "baseline-exception"


def candidate_key(candidate, policy):
    metrics = candidate["metrics"]
    return (
        not policy_pass(metrics, policy),
        abs(metrics["component_delta"]) + abs(metrics["counter_delta"]),
        metrics["sample_delta"],
        sf.quality_key({"metrics": metrics, "points": candidate["points"]}),
        -candidate["source_runs"],
        candidate["points"],
    )


def format_number(value):
    return "{:.6f}".format(value)


def add_type_fields(row, prefix, counts):
    for name in ("corner", "curve", "hvcurve", "tangent", "off"):
        row["{}_{}_points".format(prefix, "off_curve" if name == "off" else name)] = counts[name]
    row["{}_corner_ratio".format(prefix)] = format_number(counts["ratio"])


def add_curvature_metrics(row, metrics, baseline_metrics):
    row.update({
        "curvature_mse": format_number(metrics["mse"]),
        "curvature_ink_iou": format_number(metrics["ink_iou"]),
        "curvature_false_positive_ink": format_number(metrics["false_positive_ink"]),
        "curvature_false_negative_ink": format_number(metrics["false_negative_ink"]),
        "curvature_sample_delta": format_number(metrics["sample_delta"]),
        "curvature_component_delta": metrics["component_delta"],
        "curvature_counter_delta": metrics["counter_delta"],
        "curvature_topology_status": metrics["topology_status"],
        "curvature_worst_raster_size": metrics["worst_raster_size"],
        "curvature_mse_delta": format_number(metrics["mse"] - baseline_metrics["mse"]),
        "curvature_ink_iou_delta": format_number(metrics["ink_iou"] - baseline_metrics["ink_iou"]),
        "curvature_false_positive_delta": format_number(metrics["false_positive_ink"] - baseline_metrics["false_positive_ink"]),
        "curvature_false_negative_delta": format_number(metrics["false_negative_ink"] - baseline_metrics["false_negative_ink"]),
        "curvature_sample_delta_change": format_number(metrics["sample_delta"] - baseline_metrics["sample_delta"]),
    })


def self_intersection_count(glyph):
    return sum(1 for contour in glyph.foreground if contour.selfIntersects())


def region_areas(mask, size, filled):
    # Scanline run union avoids pushing every pixel in the very large outside
    # background at 1024 px. Connectivity remains four-neighbour equivalent.
    target = bool(filled)
    parent = []
    areas = []
    borders = []

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        left = find(left)
        right = find(right)
        if left == right:
            return left
        if areas[left] < areas[right]:
            left, right = right, left
        parent[right] = left
        areas[left] += areas[right]
        borders[left] = borders[left] or borders[right]
        return left

    previous = []
    for row in range(size):
        offset = row * size
        current = []
        column = 0
        while column < size:
            if (mask[offset + column] >= 0.5) != target:
                column += 1
                continue
            start = column
            column += 1
            while column < size and ((mask[offset + column] >= 0.5) == target):
                column += 1
            end = column - 1
            run_id = len(parent)
            parent.append(run_id)
            areas.append(end - start + 1)
            borders.append(row == 0 or row == size - 1 or start == 0 or end == size - 1)
            current.append((start, end, run_id))
        previous_index = 0
        for start, end, run_id in current:
            while previous_index < len(previous) and previous[previous_index][1] < start:
                previous_index += 1
            match = previous_index
            while match < len(previous) and previous[match][0] <= end:
                union(run_id, previous[match][2])
                match += 1
        previous = current
    return [(areas[index], borders[index]) for index in range(len(parent)) if find(index) == index]


def raw_topology(mask, size):
    components = [area for area, _border in region_areas(mask, size, True)]
    counters = [area for area, border in region_areas(mask, size, False) if not border]
    return components, counters


def largest_diff_region(source, candidate, size, extra):
    if extra:
        diff = [1.0 if right >= 0.5 and left < 0.5 else 0.0 for left, right in zip(source, candidate)]
    else:
        diff = [1.0 if left >= 0.5 and right < 0.5 else 0.0 for left, right in zip(source, candidate)]
    return max([area for area, _border in region_areas(diff, size, True)] or [0])


def boundary_pixels(mask, size):
    values = bytearray(1 if value >= 0.5 else 0 for value in mask)
    result = bytearray(size * size)
    for row in range(1, size - 1):
        offset = row * size
        for column in range(1, size - 1):
            index = offset + column
            value = values[index]
            if values[index - 1] != value or values[index + 1] != value or values[index - size] != value or values[index + size] != value:
                result[index] = 1
    return result


def distance_to_boundary(boundary, size):
    infinity = size * 4.0
    distance_map = [0.0 if value else infinity for value in boundary]
    diagonal = math.sqrt(2.0)
    for row in range(size):
        for column in range(size):
            index = row * size + column
            value = distance_map[index]
            if row:
                value = min(value, distance_map[index - size] + 1.0)
                if column:
                    value = min(value, distance_map[index - size - 1] + diagonal)
                if column + 1 < size:
                    value = min(value, distance_map[index - size + 1] + diagonal)
            if column:
                value = min(value, distance_map[index - 1] + 1.0)
            distance_map[index] = value
    for row in range(size - 1, -1, -1):
        for column in range(size - 1, -1, -1):
            index = row * size + column
            value = distance_map[index]
            if row + 1 < size:
                value = min(value, distance_map[index + size] + 1.0)
                if column:
                    value = min(value, distance_map[index + size - 1] + diagonal)
                if column + 1 < size:
                    value = min(value, distance_map[index + size + 1] + diagonal)
            if column + 1 < size:
                value = min(value, distance_map[index + 1] + 1.0)
            distance_map[index] = value
    return distance_map


def boundary_distance(source, candidate, size):
    source_boundary = boundary_pixels(source, size)
    candidate_boundary = boundary_pixels(candidate, size)
    source_distance = distance_to_boundary(source_boundary, size)
    candidate_distance = distance_to_boundary(candidate_boundary, size)
    values = [source_distance[index] for index, value in enumerate(candidate_boundary) if value]
    values.extend(candidate_distance[index] for index, value in enumerate(source_boundary) if value)
    if not values:
        return 0.0, 0.0
    values.sort()
    diagonal = size * math.sqrt(2.0)
    return values[min(len(values) - 1, int(len(values) * 0.95))] / diagonal, values[-1] / diagonal


def turn_angle(points, index):
    previous = points[(index - 1) % len(points)]
    current = points[index]
    following = points[(index + 1) % len(points)]
    incoming = (current[0] - previous[0], current[1] - previous[1])
    outgoing = (following[0] - current[0], following[1] - current[1])
    return angle_between(incoming, outgoing)


def unsupported_cusps(glyph, source_sets):
    return unsupported_cusps_layer(glyph.foreground, source_sets)


def unsupported_cusps_layer(layer, source_sets):
    count = 0
    for contour in sf.layer_point_sets(layer):
        if len(contour) < 3:
            continue
        for index, point in enumerate(contour):
            if turn_angle(contour, index) <= 45.0:
                continue
            nearest_points = min(source_sets, key=lambda values: min(sf.distance(point, value) for value in values))
            nearest = min(range(len(nearest_points)), key=lambda offset: sf.distance(point, nearest_points[offset]))
            if turn_angle(nearest_points, nearest) < 15.0:
                count += 1
    return count


def complete_highres_reasons(original_glyph, baseline_glyph, candidate_layer):
    source_sets = sf.layer_point_sets(original_glyph.foreground)
    baseline_sets = sf.layer_point_sets(baseline_glyph.foreground)
    candidate_sets = sf.layer_point_sets(candidate_layer)
    bbox = sf.padded_bbox(sf.bbox_of_sets(source_sets + baseline_sets + candidate_sets))
    masks = {}
    topology = {}
    for size in (512, 1024):
        for label, sets in (("source", source_sets), ("baseline", baseline_sets),
                            ("candidate", candidate_sets)):
            masks[(label, size)] = sf.rasterize(sets, bbox, size)
            topology[(label, size)] = raw_topology(masks[(label, size)], size)
    source_ink = max(1, sum(masks[("source", 512)]))
    threshold = source_ink * MICRO_REGION_RATIO
    baseline_micro = tuple(
        sum(area < threshold for area in values)
        for values in topology[("baseline", 512)]
    )
    candidate_micro = tuple(
        sum(area < threshold for area in values)
        for values in topology[("candidate", 512)]
    )
    baseline_extra = largest_diff_region(
        masks[("source", 512)], masks[("baseline", 512)], 512, True) / source_ink
    candidate_extra = largest_diff_region(
        masks[("source", 512)], masks[("candidate", 512)], 512, True) / source_ink
    baseline_missing = largest_diff_region(
        masks[("source", 512)], masks[("baseline", 512)], 512, False) / source_ink
    candidate_missing = largest_diff_region(
        masks[("source", 512)], masks[("candidate", 512)], 512, False) / source_ink
    baseline_p95, baseline_max = boundary_distance(
        masks[("source", 512)], masks[("baseline", 512)], 512)
    candidate_p95, candidate_max = boundary_distance(
        masks[("source", 512)], masks[("candidate", 512)], 512)
    reasons = []
    if any(
        len(topology[("candidate", size)][kind]) > max(
            len(topology[("source", size)][kind]), len(topology[("baseline", size)][kind]))
        for size in (512, 1024) for kind in (0, 1)
    ) or any(right > left for left, right in zip(baseline_micro, candidate_micro)):
        reasons.append("micro-topology")
    if unsupported_cusps_layer(candidate_layer, source_sets) > unsupported_cusps(baseline_glyph, source_sets):
        reasons.append("cusp")
    if ((candidate_extra > baseline_extra * LOCAL_REGION_RATIO
         and candidate_extra - baseline_extra > LOCAL_REGION_DELTA)
            or (candidate_missing > baseline_missing * LOCAL_REGION_RATIO
                and candidate_missing - baseline_missing > LOCAL_REGION_DELTA)):
        reasons.append("local-ink")
    if (candidate_p95 - baseline_p95 > BOUNDARY_P95_DELTA
            or candidate_max - baseline_max > BOUNDARY_MAX_DELTA):
        reasons.append("boundary")
    return reasons


def safe_postprocess(args):
    """Preserve the fitted font as review geometry and emit a gated safe font."""
    print("Starting curvature safety post-process", flush=True)
    if not REVIEW_SFD.exists():
        shutil.copy2(args.output_sfd, REVIEW_SFD)
    if not REVIEW_TTF.exists():
        shutil.copy2(args.output_ttf, REVIEW_TTF)
    # The generated source font contains the complete 334-glyph QA order; the
    # hand-edited SFD intentionally omits a few empty/encoding-only records.
    original = fontforge.open(str(args.original_ttf))
    print("Opened original", flush=True)
    baseline = fontforge.open(str(args.baseline_sfd))
    print("Opened baseline", flush=True)
    review_sfd = fontforge.open(str(REVIEW_SFD))
    print("Opened raw review SFD", flush=True)
    review_ttf = fontforge.open(str(REVIEW_TTF))
    print("Opened raw review TTF", flush=True)
    safe = fontforge.open(str(args.baseline_sfd))
    print("Opened source, baseline, raw review, and safe fonts", flush=True)
    baseline_rows = list(csv.DictReader(args.baseline_report.open(encoding="utf-8-sig")))
    old_rows = {}
    if args.output_report.exists():
        old_rows = {row["glyph"]: row for row in csv.DictReader(args.output_report.open(encoding="utf-8-sig"))}
    rows = []
    try:
        for position, baseline_row in enumerate(baseline_rows, start=1):
            name = baseline_row["glyph"]
            if position <= 3:
                print("Preparing {}".format(name), flush=True)
            row = dict(baseline_row)
            previous = old_rows.get(name, {})
            for field in CURVATURE_FIELDS:
                row[field] = previous.get(field, "")
            targeted = is_target(baseline[name])
            changed = outline_signature(review_sfd[name]) != outline_signature(baseline[name])
            baseline_metrics = baseline_metrics_from_row(baseline_row)
            raw_metrics = dict(baseline_metrics)
            if targeted and changed:
                context = metrics_context(original[name])
                baseline_metrics = evaluate(baseline[name].foreground, context)
                raw_metrics = evaluate(review_ttf[name].foreground, context)
            policy = policy_from_row(baseline_row)
            baseline_counts = point_type_counts(baseline[name])
            raw_counts = point_type_counts(review_sfd[name])
            add_type_fields(row, "baseline", baseline_counts)
            add_type_fields(row, "curvature", raw_counts)
            row["curvature_targeted"] = str(targeted).lower()
            row["curvature_policy"] = policy
            row["raw_points"] = sf.point_count(review_ttf[name])
            row["raw_self_intersections"] = self_intersection_count(review_sfd[name])
            row["baseline_self_intersections"] = self_intersection_count(baseline[name])
            row["raw_mse"] = format_number(raw_metrics["mse"])
            row["raw_ink_iou"] = format_number(raw_metrics["ink_iou"])
            row["raw_false_positive_ink"] = format_number(raw_metrics["false_positive_ink"])
            row["raw_false_negative_ink"] = format_number(raw_metrics["false_negative_ink"])
            fallback = ""
            diagnostics = {}
            if targeted and changed:
                print("  high-resolution checks: {}".format(name), flush=True)
                source_sets = sf.layer_point_sets(original[name].foreground)
                baseline_sets = sf.layer_point_sets(baseline[name].foreground)
                raw_sets = sf.layer_point_sets(review_ttf[name].foreground)
                bbox = sf.padded_bbox(sf.bbox_of_sets(source_sets + baseline_sets + raw_sets))
                masks = {}
                for size in (512, 1024):
                    if size == 512:
                        masks[("source", size)] = sf.rasterize(source_sets, bbox, size)
                    masks[("baseline", size)] = sf.rasterize(baseline_sets, bbox, size)
                    masks[("raw", size)] = sf.rasterize(raw_sets, bbox, size)
                    for label in ("source", "baseline", "raw"):
                        if label == "source" and size == 1024:
                            components, counters = diagnostics[("source", 512)]
                        else:
                            components, counters = raw_topology(masks[(label, size)], size)
                        diagnostics[(label, size)] = (components, counters)
                        row["{}_components_{}".format("original" if label == "source" else label, size)] = len(components)
                        row["{}_counters_{}".format("original" if label == "source" else label, size)] = len(counters)
                source_ink = max(1, sum(masks[("source", 512)]))
                threshold = source_ink * MICRO_REGION_RATIO
                for label in ("baseline", "raw"):
                    components, counters = diagnostics[(label, 512)]
                    row["{}_micro_components".format(label)] = sum(area < threshold for area in components)
                    row["{}_micro_counters".format(label)] = sum(area < threshold for area in counters)
                raw_extra = largest_diff_region(masks[("source", 512)], masks[("raw", 512)], 512, True) / source_ink
                base_extra = largest_diff_region(masks[("source", 512)], masks[("baseline", 512)], 512, True) / source_ink
                raw_missing = largest_diff_region(masks[("source", 512)], masks[("raw", 512)], 512, False) / source_ink
                base_missing = largest_diff_region(masks[("source", 512)], masks[("baseline", 512)], 512, False) / source_ink
                raw_p95, raw_max = boundary_distance(masks[("source", 512)], masks[("raw", 512)], 512)
                base_p95, base_max = boundary_distance(masks[("source", 512)], masks[("baseline", 512)], 512)
                for key, value in (("raw_largest_extra_region", raw_extra), ("baseline_largest_extra_region", base_extra),
                                   ("raw_largest_missing_region", raw_missing), ("baseline_largest_missing_region", base_missing),
                                   ("raw_boundary_p95", raw_p95), ("baseline_boundary_p95", base_p95),
                                   ("raw_boundary_max", raw_max), ("baseline_boundary_max", base_max)):
                    row[key] = format_number(value)
                row["raw_cusp_count"] = unsupported_cusps(review_sfd[name], source_sets)
                row["baseline_cusp_count"] = unsupported_cusps(baseline[name], source_sets)
                if int(row["raw_self_intersections"]) > int(row["baseline_self_intersections"]):
                    fallback = "reject-self-intersection"
                elif raw_metrics["mse"] - baseline_metrics["mse"] > RELATIVE_MSE or baseline_metrics["ink_iou"] - raw_metrics["ink_iou"] > RELATIVE_IOU or raw_metrics["false_positive_ink"] - baseline_metrics["false_positive_ink"] > RELATIVE_INK or raw_metrics["false_negative_ink"] - baseline_metrics["false_negative_ink"] > RELATIVE_INK:
                    fallback = "reject-relative-regression"
                elif any(len(diagnostics[("raw", size)][kind]) > max(len(diagnostics[("source", size)][kind]), len(diagnostics[("baseline", size)][kind])) for size in (512, 1024) for kind in (0, 1)) or int(row["raw_micro_components"]) > int(row["baseline_micro_components"]) or int(row["raw_micro_counters"]) > int(row["baseline_micro_counters"]):
                    fallback = "reject-micro-topology"
                elif int(row["raw_cusp_count"]) > int(row["baseline_cusp_count"]):
                    fallback = "reject-cusp"
                elif not policy_pass(raw_metrics, policy):
                    fallback = "reject-absolute-metric"
                elif ((raw_extra > base_extra * LOCAL_REGION_RATIO and raw_extra - base_extra > LOCAL_REGION_DELTA) or (raw_missing > base_missing * LOCAL_REGION_RATIO and raw_missing - base_missing > LOCAL_REGION_DELTA)):
                    fallback = "reject-local-ink"
                elif raw_p95 - base_p95 > BOUNDARY_P95_DELTA or raw_max - base_max > BOUNDARY_MAX_DELTA:
                    fallback = "reject-boundary"
            if targeted and changed and not fallback:
                safe[name].foreground = review_sfd[name].foreground
                status = "safe-restored"
            elif fallback:
                status = fallback
            elif not targeted:
                status = "not-targeted"
            else:
                status = previous.get("curvature_status", "unchanged")
            row["raw_status"] = previous.get("curvature_status", "restored" if changed else status)
            row["safe_status"] = status
            row["fallback_reason"] = fallback
            row["curvature_status"] = status
            row["curvature_metric_pass"] = str(not fallback).lower()
            row["contour_mapping_status"] = previous.get("contour_mapping_status", "not-needed" if not targeted else "matched")
            row["curvature_source_runs"] = previous.get("curvature_source_runs", raw_counts["off"] if changed else 0)
            row["curvature_source_fit_improvement"] = previous.get("curvature_source_fit_improvement", "0.200000" if changed else "0.000000")
            row["curvature_fit_mode"] = previous.get("curvature_fit_mode", "source-derived-quadratic" if changed else "baseline-fallback")
            add_curvature_metrics(row, raw_metrics, baseline_metrics)
            rows.append(row)
            if position % 10 == 0 or position == len(baseline_rows):
                print("Safety-audited {}/{}".format(position, len(baseline_rows)), flush=True)
        safe.save(str(args.output_sfd))
        safe.generate(str(args.output_ttf))
        sf.restore_horizontal_metrics(args.original_ttf, args.output_ttf, args.pathops_python)
        safe_ttf = fontforge.open(str(args.output_ttf))
        for row in rows:
            name = row["glyph"]
            row["safe_points"] = sf.point_count(safe_ttf[name])
            row["curvature_points"] = row["safe_points"]
            if row["fallback_reason"]:
                row["safe_mse"] = row["mse"]
                row["safe_ink_iou"] = row["ink_iou"]
                row["safe_false_positive_ink"] = row["false_positive_ink"]
                row["safe_false_negative_ink"] = row["false_negative_ink"]
            else:
                row["safe_mse"] = row["raw_mse"]
                row["safe_ink_iou"] = row["raw_ink_iou"]
                row["safe_false_positive_ink"] = row["raw_false_positive_ink"]
                row["safe_false_negative_ink"] = row["raw_false_negative_ink"]
        safe_ttf.close()
        fields = list(baseline_rows[0].keys()) + CURVATURE_FIELDS
        with args.output_report.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    finally:
        for font in (original, baseline, review_sfd, review_ttf, safe):
            try:
                font.close()
            except RuntimeError:
                pass


def refresh_report_types(args):
    """Refresh inexpensive safe point-type fields without rerunning raster QA."""
    rows = list(csv.DictReader(args.output_report.open(encoding="utf-8-sig")))
    safe_sfd = fontforge.open(str(args.output_sfd))
    try:
        for row in rows:
            counts = point_type_counts(safe_sfd[row["glyph"]])
            add_type_fields(row, "safe", counts)
        fields = list(rows[0].keys())
        for field in CURVATURE_FIELDS:
            if field not in fields:
                fields.append(field)
        with args.output_report.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    finally:
        safe_sfd.close()


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def archive_curvature_v1(args):
    files = {
        "curvature-candidates.sfd": args.output_sfd,
        "curvature-candidates.ttf": args.output_ttf,
        "curvature-review.sfd": REVIEW_SFD,
        "curvature-review.ttf": REVIEW_TTF,
        "curvature-report.csv": args.output_report,
    }
    CURVATURE_V1_CHECKPOINT.mkdir(parents=True, exist_ok=True)
    manifest_path = CURVATURE_V1_CHECKPOINT / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for name, expected in manifest["files"].items():
            path = CURVATURE_V1_CHECKPOINT / name
            if not path.exists() or file_sha256(path) != expected:
                raise RuntimeError("curvature-v1 checkpoint differs: {}".format(name))
        return
    manifest = {"files": {}}
    for name, source in files.items():
        if not source.exists():
            continue
        destination = CURVATURE_V1_CHECKPOINT / name
        shutil.copy2(source, destination)
        manifest["files"][name] = file_sha256(destination)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def exact_oncurve_points(glyph):
    contours = []
    for contour in glyph.foreground:
        points = [(float(point.x), float(point.y)) for point in contour if point.on_curve]
        if len(points) >= 3:
            contours.append(points)
    return contours


def nearest_sample(contours, point):
    best = None
    for contour_index, contour in enumerate(contours):
        for point_index, value in enumerate(contour):
            cost = sf.distance(point, value)
            if best is None or cost < best[0]:
                best = (cost, contour_index, point_index)
    return best


def cyclic_indices(count, start, end, forward):
    values = [start]
    current = start
    while current != end and len(values) <= count:
        current = (current + (1 if forward else -1)) % count
        values.append(current)
    return values


def source_path_for_window(source_contours, previous, middle, following):
    mapped = [nearest_sample(source_contours, point) for point in (previous, middle, following)]
    if any(item is None for item in mapped):
        return None
    if len({item[1] for item in mapped}) != 1:
        return None
    contour = source_contours[mapped[0][1]]
    left, center, right = (item[2] for item in mapped)
    forward = cyclic_indices(len(contour), left, right, True)
    backward = cyclic_indices(len(contour), left, right, False)
    options = [path for path in (forward, backward) if center in path]
    if not options:
        return None
    path = min(options, key=len)
    if len(path) < 3 or len(path) > max(12, len(contour) // 3):
        return None
    return [contour[index] for index in path]


def least_squares_control(start, end, samples):
    distances = [0.0]
    for index in range(1, len(samples)):
        distances.append(distances[-1] + sf.distance(samples[index - 1], samples[index]))
    total = max(sf.EPSILON, distances[-1])
    numerator_x = numerator_y = denominator = 0.0
    for point, distance_value in zip(samples, distances):
        t = distance_value / total
        weight = 2.0 * t * (1.0 - t)
        base_x = (1.0 - t) ** 2 * start[0] + t ** 2 * end[0]
        base_y = (1.0 - t) ** 2 * start[1] + t ** 2 * end[1]
        numerator_x += weight * (point[0] - base_x)
        numerator_y += weight * (point[1] - base_y)
        denominator += weight * weight
    if denominator <= sf.EPSILON:
        return None
    return numerator_x / denominator, numerator_y / denominator


def sampled_rms(start, end, samples, control=None):
    distances = [0.0]
    for index in range(1, len(samples)):
        distances.append(distances[-1] + sf.distance(samples[index - 1], samples[index]))
    total = max(sf.EPSILON, distances[-1])
    errors = []
    for point, distance_value in zip(samples, distances):
        t = distance_value / total
        fitted = line_point(start, end, t) if control is None else quadratic_point(start, control, end, t)
        errors.append(sf.distance(point, fitted) ** 2)
    return math.sqrt(sum(errors) / max(1, len(errors)))


def control_is_forward(start, control, end, samples):
    chord = (end[0] - start[0], end[1] - start[1])
    chord_square = chord[0] ** 2 + chord[1] ** 2
    if chord_square <= sf.EPSILON:
        return False
    left_projection = ((control[0] - start[0]) * chord[0] + (control[1] - start[1]) * chord[1]) / chord_square
    right_projection = ((end[0] - control[0]) * chord[0] + (end[1] - control[1]) * chord[1]) / chord_square
    bbox = sf.bbox_of_sets([samples])
    pad = max(2.0, math.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1]) * 0.4)
    return (
        left_projection > 0.02 and right_projection > 0.02
        and bbox[0] - pad <= control[0] <= bbox[2] + pad
        and bbox[1] - pad <= control[1] <= bbox[3] + pad
    )


def layer_handles_valid(layer):
    for contour in layer:
        points = list(contour)
        for index, control in enumerate(points):
            if control.on_curve:
                continue
            previous = points[(index - 1) % len(points)]
            following = points[(index + 1) % len(points)]
            if not previous.on_curve or not following.on_curve:
                return False
            chord = (following.x - previous.x, following.y - previous.y)
            chord_square = chord[0] ** 2 + chord[1] ** 2
            if chord_square <= sf.EPSILON:
                return False
            left = ((control.x - previous.x) * chord[0] + (control.y - previous.y) * chord[1]) / chord_square
            right = ((following.x - control.x) * chord[0] + (following.y - control.y) * chord[1]) / chord_square
            if left <= 0.02 or right <= 0.02:
                return False
    return True


def curve_windows(baseline_points, source_contours):
    windows = []
    count = len(baseline_points)
    for index in range(count):
        previous = baseline_points[(index - 1) % count]
        middle = baseline_points[index]
        following = baseline_points[(index + 1) % count]
        if angle_between((middle[0] - previous[0], middle[1] - previous[1]),
                         (following[0] - middle[0], following[1] - middle[1])) > 55.0:
            continue
        samples = source_path_for_window(source_contours, previous, middle, following)
        if samples is None:
            continue
        control = least_squares_control(previous, following, samples)
        if control is None or not control_is_forward(previous, control, following, samples):
            continue
        line_error = sampled_rms(previous, following, samples)
        curve_error = sampled_rms(previous, following, samples, control)
        deviation = max(sf.point_line_distance(point, previous, following) for point in samples)
        improvement = 0.0 if line_error <= sf.EPSILON else 1.0 - curve_error / line_error
        if deviation < MIN_LINE_DEVIATION or improvement < MIN_RMS_IMPROVEMENT:
            continue
        windows.append({
            "index": index, "control": control, "line_error": line_error,
            "curve_error": curve_error, "improvement": improvement,
            "score": improvement * max(0.25, deviation),
        })
    return windows


def independent_windows(windows, count, limit=None):
    selected = []
    blocked = set()
    for item in sorted(windows, key=lambda value: (-value["score"], value["curve_error"])):
        index = item["index"]
        if index in blocked:
            continue
        selected.append(item)
        blocked.update(((index - 1) % count, index, (index + 1) % count))
        if limit is not None and len(selected) >= limit:
            break
    return selected


def layer_with_windows(contour_points, selected_by_contour, passthrough=()):
    layer = fontforge.layer()
    layer.is_quadratic = True
    for points, selected in zip(contour_points, selected_by_contour):
        controls = {item["index"]: item["control"] for item in selected}
        anchors = [index for index in range(len(points)) if index not in controls]
        if len(anchors) < 2:
            return None
        start = anchors[0]
        contour = fontforge.contour()
        contour.is_quadratic = True
        contour.moveTo(*points[start])
        current = start
        while True:
            next_index = (current + 1) % len(points)
            if next_index in controls:
                endpoint = (next_index + 1) % len(points)
                contour.quadraticTo(controls[next_index], points[endpoint])
                current = endpoint
            else:
                contour.lineTo(*points[next_index])
                current = next_index
            if current == start:
                break
        contour.closed = True
        layer += contour
    for contour in passthrough:
        layer += contour.dup()
    return classify_layer(layer)


def relative_metrics_pass(candidate, baseline):
    return (
        candidate["mse"] - baseline["mse"] <= RELATIVE_MSE
        and baseline["ink_iou"] - candidate["ink_iou"] <= RELATIVE_IOU
        and candidate["false_positive_ink"] - baseline["false_positive_ink"] <= RELATIVE_INK
        and candidate["false_negative_ink"] - baseline["false_negative_ink"] <= RELATIVE_INK
    )


def complete_policy_pass(candidate, baseline, policy):
    if policy != "baseline-exception":
        return policy_pass(candidate, policy)
    return (
        candidate["component_delta"] == baseline["component_delta"]
        and candidate["counter_delta"] == baseline["counter_delta"]
    )


def complete_candidate_for_glyph(name, original_glyph, baseline_glyph, source_glyph, temporary,
                                 highres_required=False):
    context = metrics_context(original_glyph)
    baseline_metrics = evaluate(baseline_glyph.foreground, context)
    policy = policy_for_baseline(baseline_metrics)
    baseline_counts = point_type_counts(baseline_glyph)
    baseline_intersections = sum(1 for contour in baseline_glyph.foreground if contour.selfIntersects())
    if baseline_counts["off"] > 0:
        return {
            "layer": baseline_glyph.foreground.dup(), "metrics": baseline_metrics,
            "status": "already-source-curved", "mapping": "preserved-valid",
            "windows": 0, "available": 0, "attempts": [0], "rejections": [],
            "source_error": 0.0,
        }
    baseline_contours = exact_oncurve_points(baseline_glyph)
    passthrough = [
        contour for contour in baseline_glyph.foreground
        if sum(1 for point in contour if point.on_curve) < 3
    ]
    source_contours = sf.layer_point_sets(source_glyph.foreground)
    mapping, mapping_status = match_contours(source_contours, baseline_contours)
    if mapping is None:
        mapping = source_contours
        mapping_mode = "visible-boundary"
        source_for_contour = [source_contours for _contour in baseline_contours]
    else:
        mapping_mode = "direct-contour"
        source_for_contour = [[contour] for contour in mapping]
    available = [curve_windows(points, sources) for points, sources in zip(baseline_contours, source_for_contour)]
    total_available = sum(len(values) for values in available)
    if not total_available:
        return {"layer": None, "status": "unresolved-no-source-window", "mapping": mapping_mode,
                "windows": 0, "available": 0, "attempts": [], "rejections": ["no-source-window"]}
    maxima = [len(independent_windows(values, len(points))) for values, points in zip(available, baseline_contours)]
    total_maximum = sum(maxima)
    requested_counts = sorted(set([total_maximum, max(1, int(total_maximum * 0.75)),
                                   max(1, int(total_maximum * 0.5)), max(1, int(total_maximum * 0.25)), 1]), reverse=True)
    attempts = []
    rejections = []
    passing = []
    for requested in ([] if highres_required else requested_counts):
        remaining = requested
        selected = []
        for points, values, maximum in zip(baseline_contours, available, maxima):
            share = min(maximum, max(0, int(round(requested * maximum / float(max(1, total_maximum))))))
            share = min(share, remaining)
            chosen = independent_windows(values, len(points), share)
            selected.append(chosen)
            remaining -= len(chosen)
        if remaining:
            for contour_index, (points, values, maximum) in enumerate(zip(baseline_contours, available, maxima)):
                if remaining <= 0:
                    break
                current = len(selected[contour_index])
                chosen = independent_windows(values, len(points), min(maximum, current + remaining))
                remaining -= max(0, len(chosen) - current)
                selected[contour_index] = chosen
        actual_count = sum(len(values) for values in selected)
        if actual_count <= 0 or actual_count in attempts:
            continue
        attempts.append(actual_count)
        layer = layer_with_windows(baseline_contours, selected, passthrough)
        if layer is None:
            rejections.append("{}:build".format(actual_count))
            continue
        actual = sf.roundtrip_candidate(original_glyph, layer, temporary,
                                        "complete-{}-{}".format(sf.glyph_stem(name), actual_count))
        actual = classify_layer(actual)
        if not layer_handles_valid(actual):
            rejections.append("{}:invalid-handle".format(actual_count))
            continue
        if sum(len(contour) for contour in actual) > sf.MAX_POINTS:
            rejections.append("{}:budget".format(actual_count))
            continue
        if sum(1 for contour in actual if contour.selfIntersects()) > baseline_intersections:
            rejections.append("{}:self-intersection".format(actual_count))
            continue
        metrics = evaluate(actual, context)
        if not complete_policy_pass(metrics, baseline_metrics, policy):
            rejections.append("{}:absolute".format(actual_count))
            continue
        if not relative_metrics_pass(metrics, baseline_metrics):
            rejections.append("{}:relative".format(actual_count))
            continue
        source_error = sum(item["curve_error"] for values in selected for item in values) / actual_count
        passing.append({
            "layer": actual, "metrics": metrics, "windows": actual_count,
            "source_error": source_error,
            "source_improvement": min(item["improvement"] for values in selected for item in values),
        })
    if not passing:
        single_options = sorted(
            ((contour_index, item) for contour_index, values in enumerate(available) for item in values),
            key=lambda value: (value[1]["curve_error"], -value[1]["improvement"]),
        )
        for contour_index, item in single_options:
            selected = [[] for _contour in baseline_contours]
            selected[contour_index] = [item]
            label = "single-{}-{}".format(contour_index, item["index"])
            layer = layer_with_windows(baseline_contours, selected, passthrough)
            if layer is None:
                rejections.append(label + ":build")
                continue
            actual = sf.roundtrip_candidate(
                original_glyph, layer, temporary,
                "complete-{}-{}".format(sf.glyph_stem(name), label),
            )
            actual = classify_layer(actual)
            if not layer_handles_valid(actual):
                rejections.append(label + ":invalid-handle")
                continue
            if sum(len(contour) for contour in actual) > sf.MAX_POINTS:
                rejections.append(label + ":budget")
                continue
            if sum(1 for contour in actual if contour.selfIntersects()) > baseline_intersections:
                rejections.append(label + ":self-intersection")
                continue
            metrics = evaluate(actual, context)
            if not complete_policy_pass(metrics, baseline_metrics, policy):
                rejections.append(label + ":absolute")
                continue
            if not relative_metrics_pass(metrics, baseline_metrics):
                rejections.append(label + ":relative")
                continue
            passing.append({
                "layer": actual, "metrics": metrics, "windows": 1,
                "source_error": item["curve_error"],
                "source_improvement": item["improvement"],
            })
            if highres_required:
                reasons = complete_highres_reasons(original_glyph, baseline_glyph, actual)
                if reasons:
                    passing.pop()
                    rejections.append(label + ":highres-" + "+".join(reasons))
                    continue
            break
    if not passing:
        return {"layer": None, "status": "unresolved-quality", "mapping": mapping_mode,
                "windows": 0, "available": total_available, "attempts": attempts,
                "rejections": rejections}
    best = min(passing, key=lambda item: (-item["windows"], item["source_error"],
                                          item["metrics"]["sample_delta"]))
    best.update({
        "status": "safe-restored-full" if best["windows"] >= max(2, total_maximum // 2) else "safe-restored-local",
        "mapping": mapping_mode, "available": total_available, "attempts": attempts,
        "rejections": rejections,
    })
    return best


def complete_restoration(args):
    archive_curvature_v1(args)
    original = fontforge.open(str(args.original_ttf))
    baseline = fontforge.open(str(args.baseline_sfd))
    normalized = fontforge.open(str(args.normalized_source_sfd))
    resume = bool(args.resume_complete and args.output_sfd.exists() and args.output_report.exists())
    safe = fontforge.open(str(args.output_sfd if resume else args.baseline_sfd))
    raw = fontforge.open(str(REVIEW_SFD if resume and REVIEW_SFD.exists() else args.baseline_sfd))
    report_source = args.output_report if resume else args.baseline_report
    baseline_rows = list(csv.DictReader(report_source.open(encoding="utf-8-sig")))
    temporary = Path(tempfile.mkdtemp(prefix="curvature-complete-"))
    rows = []
    unresolved = []
    try:
        targets = [glyph.glyphname for glyph in baseline.glyphs() if is_target(glyph)]
        if len(targets) != 213:
            raise RuntimeError("expected 213 curvature targets, found {}".format(len(targets)))
        target_set = set(targets)
        for position, baseline_row in enumerate(baseline_rows, start=1):
            name = baseline_row["glyph"]
            row = dict(baseline_row)
            previous_status = row.get("complete_status", "")
            if not resume:
                for field in CURVATURE_FIELDS + COMPLETE_FIELDS:
                    row[field] = ""
            baseline_counts = point_type_counts(baseline[name])
            add_type_fields(row, "baseline", baseline_counts)
            explicitly_selected = bool(args.glyph and name in set(args.glyph))
            if resume and previous_status and not previous_status.startswith("unresolved") and not explicitly_selected:
                rows.append(row)
                continue
            if name not in target_set:
                result = {"layer": baseline[name].foreground.dup(), "metrics": baseline_metrics_from_row(row),
                          "status": "not-targeted", "mapping": "not-needed", "windows": 0,
                          "available": 0, "attempts": [0], "rejections": [], "source_error": 0.0}
            else:
                result = complete_candidate_for_glyph(
                    name, original[name], baseline[name], normalized[name], temporary,
                    row.get("complete_audit_status") == "fail",
                )
                if result.get("layer") is None:
                    unresolved.append(name)
                    result["layer"] = baseline[name].foreground.dup()
                else:
                    safe[name].foreground = result["layer"]
                    raw[name].foreground = result["layer"]
            candidate_counts = point_type_counts(safe[name])
            add_type_fields(row, "curvature", candidate_counts)
            add_type_fields(row, "safe", candidate_counts)
            row.update({
                "curvature_targeted": str(name in target_set).lower(),
                "curvature_status": result["status"], "safe_status": result["status"],
                "raw_status": result["status"], "curvature_policy": policy_from_row(row),
                "contour_mapping_status": result["mapping"],
                "curvature_source_runs": result.get("windows", 0),
                "curvature_source_fit_improvement": format_number(
                    result.get("source_improvement", 0.0)),
                "curvature_fit_mode": "adaptive-local-quadratic",
                "curvature_metric_pass": str(result["status"] not in ("unresolved-quality", "unresolved-no-source-window")).lower(),
                "complete_status": result["status"], "complete_mapping_mode": result["mapping"],
                "significant_source_runs": result.get("available", 0),
                "represented_curve_runs": result.get("windows", 0),
                "curved_run_coverage": format_number(result.get("windows", 0) / float(max(1, result.get("available", 0)))),
                "handle_valid": str(result["status"] not in ("unresolved-quality", "unresolved-no-source-window")).lower(),
                "attempted_curve_counts": ";".join(str(value) for value in result.get("attempts", [])),
                "rejection_history": ";".join(result.get("rejections", [])),
                "complete_source_error": format_number(result.get("source_error", 0.0)),
            })
            metrics = result.get("metrics", baseline_metrics_from_row(row))
            add_curvature_metrics(row, metrics, baseline_metrics_from_row(row))
            rows.append(row)
            if position % 10 == 0 or position == len(baseline_rows):
                print("Complete restoration {}/{}; unresolved {}".format(position, len(baseline_rows), len(unresolved)), flush=True)
        args.output_sfd.parent.mkdir(parents=True, exist_ok=True)
        args.output_ttf.parent.mkdir(parents=True, exist_ok=True)
        safe.save(str(args.output_sfd))
        safe.generate(str(args.output_ttf))
        raw.save(str(REVIEW_SFD))
        raw.generate(str(REVIEW_TTF))
        sf.restore_horizontal_metrics(args.original_ttf, args.output_ttf, args.pathops_python)
        sf.restore_horizontal_metrics(args.original_ttf, REVIEW_TTF, args.pathops_python)
        safe_ttf = fontforge.open(str(args.output_ttf))
        for row in rows:
            row["safe_points"] = sf.point_count(safe_ttf[row["glyph"]])
            row["raw_points"] = row["safe_points"]
            row["curvature_points"] = row["safe_points"]
        safe_ttf.close()
        fields = list(baseline_rows[0].keys())
        for field in CURVATURE_FIELDS + COMPLETE_FIELDS:
            if field not in fields:
                fields.append(field)
        with args.output_report.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        if unresolved:
            raise RuntimeError("unresolved curvature targets: {}".format(" ".join(unresolved)))
    finally:
        for font in (original, baseline, normalized, safe, raw):
            try:
                font.close()
            except RuntimeError:
                pass
        shutil.rmtree(str(temporary), ignore_errors=True)


def audit_complete(args):
    """Enforce high-resolution structural and localized-shape gates in place."""
    original = fontforge.open(str(args.original_ttf))
    baseline = fontforge.open(str(args.baseline_sfd))
    candidate_sfd = fontforge.open(str(args.output_sfd))
    candidate_ttf = fontforge.open(str(args.output_ttf))
    rows = list(csv.DictReader(args.output_report.open(encoding="utf-8-sig")))
    failures = []
    try:
        for position, row in enumerate(rows, start=1):
            name = row["glyph"]
            targeted = row.get("curvature_targeted") == "true"
            selected = not args.glyph or name in set(args.glyph)
            if args.resume_complete and row.get("complete_audit_status"):
                selected = False
            changed = outline_signature(candidate_sfd[name]) != outline_signature(baseline[name])
            if not selected:
                continue
            row["complete_audit_status"] = "not-targeted" if not targeted else "pass"
            row["complete_audit_reason"] = ""
            if targeted and changed:
                source_sets = sf.layer_point_sets(original[name].foreground)
                baseline_sets = sf.layer_point_sets(baseline[name].foreground)
                candidate_sets = sf.layer_point_sets(candidate_ttf[name].foreground)
                bbox = sf.padded_bbox(sf.bbox_of_sets(source_sets + baseline_sets + candidate_sets))
                masks = {}
                topology = {}
                for size in (512, 1024):
                    for label, sets in (("source", source_sets), ("baseline", baseline_sets),
                                        ("raw", candidate_sets)):
                        masks[(label, size)] = sf.rasterize(sets, bbox, size)
                        topology[(label, size)] = raw_topology(masks[(label, size)], size)
                        report_label = "original" if label == "source" else label
                        row["{}_components_{}".format(report_label, size)] = len(topology[(label, size)][0])
                        row["{}_counters_{}".format(report_label, size)] = len(topology[(label, size)][1])
                source_ink = max(1, sum(masks[("source", 512)]))
                micro_threshold = source_ink * MICRO_REGION_RATIO
                for label in ("baseline", "raw"):
                    components, counters = topology[(label, 512)]
                    row["{}_micro_components".format(label)] = sum(area < micro_threshold for area in components)
                    row["{}_micro_counters".format(label)] = sum(area < micro_threshold for area in counters)
                candidate_extra = largest_diff_region(
                    masks[("source", 512)], masks[("raw", 512)], 512, True) / source_ink
                baseline_extra = largest_diff_region(
                    masks[("source", 512)], masks[("baseline", 512)], 512, True) / source_ink
                candidate_missing = largest_diff_region(
                    masks[("source", 512)], masks[("raw", 512)], 512, False) / source_ink
                baseline_missing = largest_diff_region(
                    masks[("source", 512)], masks[("baseline", 512)], 512, False) / source_ink
                candidate_p95, candidate_max = boundary_distance(
                    masks[("source", 512)], masks[("raw", 512)], 512)
                baseline_p95, baseline_max = boundary_distance(
                    masks[("source", 512)], masks[("baseline", 512)], 512)
                for key, value in (
                    ("raw_largest_extra_region", candidate_extra),
                    ("baseline_largest_extra_region", baseline_extra),
                    ("raw_largest_missing_region", candidate_missing),
                    ("baseline_largest_missing_region", baseline_missing),
                    ("raw_boundary_p95", candidate_p95),
                    ("baseline_boundary_p95", baseline_p95),
                    ("raw_boundary_max", candidate_max),
                    ("baseline_boundary_max", baseline_max),
                ):
                    row[key] = format_number(value)
                row["raw_self_intersections"] = self_intersection_count(candidate_sfd[name])
                row["baseline_self_intersections"] = self_intersection_count(baseline[name])
                row["raw_cusp_count"] = unsupported_cusps(candidate_sfd[name], source_sets)
                row["baseline_cusp_count"] = unsupported_cusps(baseline[name], source_sets)
                reasons = []
                if int(row["raw_self_intersections"]) > int(row["baseline_self_intersections"]):
                    reasons.append("self-intersection")
                if any(
                    len(topology[("raw", size)][kind]) > max(
                        len(topology[("source", size)][kind]), len(topology[("baseline", size)][kind]))
                    for size in (512, 1024) for kind in (0, 1)
                ) or int(row["raw_micro_components"]) > int(row["baseline_micro_components"]) or int(
                    row["raw_micro_counters"]) > int(row["baseline_micro_counters"]):
                    reasons.append("micro-topology")
                if int(row["raw_cusp_count"]) > int(row["baseline_cusp_count"]):
                    reasons.append("cusp")
                if ((candidate_extra > baseline_extra * LOCAL_REGION_RATIO
                     and candidate_extra - baseline_extra > LOCAL_REGION_DELTA)
                        or (candidate_missing > baseline_missing * LOCAL_REGION_RATIO
                            and candidate_missing - baseline_missing > LOCAL_REGION_DELTA)):
                    reasons.append("local-ink")
                if (candidate_p95 - baseline_p95 > BOUNDARY_P95_DELTA
                        or candidate_max - baseline_max > BOUNDARY_MAX_DELTA):
                    reasons.append("boundary")
                if reasons:
                    row["complete_audit_status"] = "fail"
                    row["complete_audit_reason"] = ";".join(reasons)
                    failures.append("{}:{}".format(name, ",".join(reasons)))
            if position % 10 == 0 or position == len(rows):
                print("Complete audit {}/{}; failures {}".format(position, len(rows), len(failures)), flush=True)
        for row in rows:
            if row.get("complete_audit_status") == "fail":
                row["complete_status"] = "unresolved-audit"
                if "{}:{}".format(row["glyph"], row.get("complete_audit_reason", "")) not in failures:
                    failures.append("{}:{}".format(row["glyph"], row.get("complete_audit_reason", "")))
        fields = list(rows[0].keys())
        for field in CURVATURE_FIELDS + COMPLETE_FIELDS:
            if field not in fields:
                fields.append(field)
        with args.output_report.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        if failures:
            raise RuntimeError("complete curvature audit failures: {}".format(" ".join(failures)))
    finally:
        for font in (original, baseline, candidate_sfd, candidate_ttf):
            try:
                font.close()
            except RuntimeError:
                pass


def build(args):
    original = fontforge.open(str(args.original_sfd))
    baseline = fontforge.open(str(args.baseline_sfd))
    output = fontforge.open(str(args.baseline_sfd))
    baseline_rows = list(csv.DictReader(args.baseline_report.open(encoding="utf-8-sig")))
    rows_by_name = {row["glyph"]: dict(row) for row in baseline_rows}
    decisions = {}
    selected = set(args.glyph or [])
    temporary = Path(tempfile.mkdtemp(prefix="curvature-review-"))
    try:
        names = [glyph.glyphname for glyph in baseline.glyphs()]
        targets = [name for name in names if is_target(baseline[name]) and (not selected or name in selected)]
        print("Curvature targets: {}".format(len(targets)), flush=True)
        for position, name in enumerate(targets, start=1):
            source_glyph = original[name]
            baseline_glyph = baseline[name]
            context = metrics_context(source_glyph)
            baseline_metrics = evaluate(baseline_glyph.foreground, context)
            policy = policy_for_baseline(baseline_metrics)
            matched, mapping_status = match_contours(
                context[0], sf.layer_point_sets(baseline_glyph.foreground)
            )
            decision = {
                "targeted": True, "status": "mapping-failure", "policy": policy,
                "mapping": mapping_status, "candidate": None,
                "baseline_metrics": baseline_metrics,
            }
            if matched is not None:
                removal_orders = [fast_removal_order(contour) for contour in matched]
                candidates = []
                for budget in BUDGETS:
                    for strategy in STRATEGIES:
                        candidate = build_curvature_layer(matched, removal_orders, budget, strategy)
                        if candidate is None:
                            continue
                        original_sets, bbox, original_mask, original_masks, topologies = context
                        candidate["metrics"] = sf.evaluate_candidate(
                            candidate["layer"], original_sets, original_mask, original_masks,
                            bbox, sf.persistent_topology(topologies), None,
                        )
                        candidates.append(candidate)
                if not candidates:
                    decision["status"] = "no-source-curvature"
                else:
                    best = min(candidates, key=lambda item: candidate_key(item, policy))
                    actual = sf.roundtrip_candidate(source_glyph, best["layer"], temporary, "curvature-{}".format(sf.glyph_stem(name)))
                    actual = classify_layer(actual)
                    actual_points = sum(len(contour) for contour in actual)
                    actual_metrics = evaluate(actual, context)
                    best["layer"] = actual
                    best["points"] = actual_points
                    best["metrics"] = actual_metrics
                    if actual_points > sf.MAX_POINTS:
                        decision["status"] = "budget-failure"
                    elif actual_metrics["component_delta"] or actual_metrics["counter_delta"]:
                        decision["status"] = "topology-failure"
                    else:
                        decision["candidate"] = best
                        decision["status"] = (
                            "restored" if policy_pass(actual_metrics, policy) else "metric-failure"
                        )
                        try:
                            output[name].setLayer(actual, 1, ("select_none",))
                        except TypeError:
                            output[name].foreground = actual
            decisions[name] = decision
            if position % 10 == 0 or position == len(targets):
                print("Processed {}/{}".format(position, len(targets)), flush=True)

        args.output_sfd.parent.mkdir(parents=True, exist_ok=True)
        args.output_ttf.parent.mkdir(parents=True, exist_ok=True)
        output.save(str(args.output_sfd))
        output.generate(str(args.output_ttf))
        sf.restore_horizontal_metrics(args.original_ttf, args.output_ttf, args.pathops_python)
        actual_font = fontforge.open(str(args.output_ttf))
        candidate_sfd = fontforge.open(str(args.output_sfd))
        report_rows = []
        for name in names:
            row = rows_by_name[name]
            for field in CURVATURE_FIELDS:
                row[field] = ""
            baseline_counts = point_type_counts(baseline[name])
            candidate_counts = point_type_counts(candidate_sfd[name])
            add_type_fields(row, "baseline", baseline_counts)
            add_type_fields(row, "curvature", candidate_counts)
            row["curvature_points"] = sf.point_count(actual_font[name])
            decision = decisions.get(name)
            if decision is None:
                row.update({
                    "curvature_targeted": "false", "curvature_status": "not-targeted",
                    "contour_mapping_status": "not-needed", "curvature_policy": "baseline",
                    "curvature_metric_pass": "true", "curvature_source_runs": 0,
                    "curvature_source_fit_improvement": "0.000000",
                    "curvature_fit_mode": "baseline",
                })
                row.update({
                    "curvature_mse": row.get("mse", ""),
                    "curvature_ink_iou": row.get("ink_iou", ""),
                    "curvature_false_positive_ink": row.get("false_positive_ink", ""),
                    "curvature_false_negative_ink": row.get("false_negative_ink", ""),
                    "curvature_sample_delta": row.get("sample_delta", ""),
                    "curvature_component_delta": row.get("component_delta", ""),
                    "curvature_counter_delta": row.get("counter_delta", ""),
                    "curvature_topology_status": row.get("topology_status", ""),
                    "curvature_worst_raster_size": row.get("worst_raster_size", ""),
                    "curvature_mse_delta": "0.000000",
                    "curvature_ink_iou_delta": "0.000000",
                    "curvature_false_positive_delta": "0.000000",
                    "curvature_false_negative_delta": "0.000000",
                    "curvature_sample_delta_change": "0.000000",
                })
                report_rows.append(row)
                continue
            else:
                row.update({
                    "curvature_targeted": "true", "curvature_status": decision["status"],
                    "curvature_policy": decision["policy"],
                    "contour_mapping_status": decision["mapping"],
                })
                baseline_metrics = decision["baseline_metrics"]
                actual_metrics = evaluate(actual_font[name].foreground, metrics_context(original[name]))
                candidate = decision["candidate"]
                row["curvature_metric_pass"] = str(
                    policy_pass(actual_metrics, decision["policy"])
                ).lower()
                if candidate:
                    row["curvature_source_runs"] = candidate["source_runs"]
                    row["curvature_source_fit_improvement"] = format_number(candidate["fit_improvement"])
                    row["curvature_fit_mode"] = candidate["fit_mode"]
                else:
                    row["curvature_source_runs"] = 0
                    row["curvature_source_fit_improvement"] = "0.000000"
                    row["curvature_fit_mode"] = "baseline-fallback"
            add_curvature_metrics(row, actual_metrics, baseline_metrics)
            report_rows.append(row)

        fields = list(baseline_rows[0].keys()) + CURVATURE_FIELDS
        with args.output_report.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(report_rows)
        actual_font.close()
        candidate_sfd.close()
    finally:
        for font in (original, baseline, output):
            try:
                font.close()
            except RuntimeError:
                pass
        import shutil
        shutil.rmtree(str(temporary), ignore_errors=True)


def write_existing_report(args):
    """Audit already-generated candidate artifacts without rerunning fitting."""
    original = fontforge.open(str(args.original_sfd))
    baseline = fontforge.open(str(args.baseline_sfd))
    candidate_ttf = fontforge.open(str(args.output_ttf))
    candidate_sfd = fontforge.open(str(args.output_sfd))
    baseline_rows = list(csv.DictReader(args.baseline_report.open(encoding="utf-8-sig")))
    rows = []
    try:
        for position, baseline_row in enumerate(baseline_rows, start=1):
            name = baseline_row["glyph"]
            row = dict(baseline_row)
            for field in CURVATURE_FIELDS:
                row[field] = ""
            baseline_counts = point_type_counts(baseline[name])
            candidate_counts = point_type_counts(candidate_sfd[name])
            targeted = is_target(baseline[name])
            add_type_fields(row, "baseline", baseline_counts)
            add_type_fields(row, "curvature", candidate_counts)
            row["curvature_points"] = sf.point_count(candidate_ttf[name])
            row["curvature_targeted"] = str(targeted).lower()
            changed = outline_signature(candidate_sfd[name]) != outline_signature(baseline[name])
            if not targeted:
                row.update({
                    "curvature_status": "not-targeted", "curvature_policy": "baseline",
                    "contour_mapping_status": "not-needed", "curvature_metric_pass": "true",
                    "curvature_source_runs": 0,
                    "curvature_source_fit_improvement": "0.000000",
                    "curvature_fit_mode": "baseline",
                    "curvature_mse": row.get("mse", ""),
                    "curvature_ink_iou": row.get("ink_iou", ""),
                    "curvature_false_positive_ink": row.get("false_positive_ink", ""),
                    "curvature_false_negative_ink": row.get("false_negative_ink", ""),
                    "curvature_sample_delta": row.get("sample_delta", ""),
                    "curvature_component_delta": row.get("component_delta", ""),
                    "curvature_counter_delta": row.get("counter_delta", ""),
                    "curvature_topology_status": row.get("topology_status", ""),
                    "curvature_worst_raster_size": row.get("worst_raster_size", ""),
                    "curvature_mse_delta": "0.000000",
                    "curvature_ink_iou_delta": "0.000000",
                    "curvature_false_positive_delta": "0.000000",
                    "curvature_false_negative_delta": "0.000000",
                    "curvature_sample_delta_change": "0.000000",
                })
            else:
                context = metrics_context(original[name])
                baseline_metrics = baseline_metrics_from_row(row)
                actual_metrics = evaluate(candidate_ttf[name].foreground, context)
                policy = policy_from_row(row)
                matched, mapping_status = match_contours(
                    context[0], sf.layer_point_sets(baseline[name].foreground)
                )
                if not changed:
                    status = "mapping-failure" if matched is None else "no-source-curvature"
                elif actual_metrics["component_delta"] or actual_metrics["counter_delta"]:
                    status = "topology-failure"
                else:
                    status = "restored" if policy_pass(actual_metrics, policy) else "metric-failure"
                row.update({
                    "curvature_status": status, "curvature_policy": policy,
                    "contour_mapping_status": mapping_status,
                    "curvature_metric_pass": str(policy_pass(actual_metrics, policy)).lower(),
                    "curvature_source_runs": candidate_counts["off"] if changed else 0,
                    # Every emitted run passed the fitter's 20% line-vs-curve
                    # improvement gate; the report-only audit records its floor.
                    "curvature_source_fit_improvement": "0.200000" if changed else "0.000000",
                    "curvature_fit_mode": "source-derived-quadratic" if changed else "baseline-fallback",
                })
                add_curvature_metrics(row, actual_metrics, baseline_metrics)
            rows.append(row)
            if position % 25 == 0 or position == len(baseline_rows):
                print("Audited {}/{}".format(position, len(baseline_rows)), flush=True)
        fields = list(baseline_rows[0].keys()) + CURVATURE_FIELDS
        with args.output_report.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    finally:
        for font in (original, baseline, candidate_ttf, candidate_sfd):
            try:
                font.close()
            except RuntimeError:
                pass


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--original-sfd", type=Path, default=ORIGINAL_SFD)
    value.add_argument("--original-ttf", type=Path, default=ORIGINAL_TTF)
    value.add_argument("--baseline-sfd", type=Path, default=BASELINE_SFD)
    value.add_argument("--baseline-ttf", type=Path, default=BASELINE_TTF)
    value.add_argument("--baseline-report", type=Path, default=BASELINE_REPORT)
    value.add_argument("--normalized-source-sfd", type=Path, default=NORMALIZED_SOURCE_SFD)
    value.add_argument("--output-sfd", type=Path, default=OUTPUT_SFD)
    value.add_argument("--output-ttf", type=Path, default=OUTPUT_TTF)
    value.add_argument("--output-report", type=Path, default=OUTPUT_REPORT)
    value.add_argument("--pathops-python", default=sf.DEFAULT_EXTERNAL_PYTHON)
    value.add_argument("--glyph", action="append")
    value.add_argument("--report-only", action="store_true")
    value.add_argument("--safe-postprocess", action="store_true")
    value.add_argument("--refresh-report-types", action="store_true")
    value.add_argument("--complete-restoration", action="store_true")
    value.add_argument("--resume-complete", action="store_true")
    value.add_argument("--audit-complete", action="store_true")
    return value


if __name__ == "__main__":
    arguments = parser().parse_args()
    if arguments.audit_complete:
        audit_complete(arguments)
    elif arguments.complete_restoration:
        complete_restoration(arguments)
    elif arguments.refresh_report_types:
        refresh_report_types(arguments)
    elif arguments.safe_postprocess:
        safe_postprocess(arguments)
    elif arguments.report_only:
        write_existing_report(arguments)
    else:
        build(arguments)
