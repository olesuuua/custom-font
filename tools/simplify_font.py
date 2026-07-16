#!/usr/bin/env python3
"""Quality-gated outline simplification for the retained QA font.

Run with FontForge's Python runtime:
  ffpython tools/simplify_font.py

The orchestrator starts one subprocess per glyph. This isolates FontForge's
overlap remover, which can hang or abort on malformed source geometry.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import itertools
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fontforge


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "qa" / "assets" / "original.ttf"
DEFAULT_OUTPUT = ROOT / "qa" / "assets" / "simplified.ttf"
DEFAULT_REPORT = ROOT / "qa" / "assets" / "glyph-report.csv"
DEFAULT_SFD_OUTPUT = ROOT / "fontforge" / "simplified.sfd"
DEFAULT_REVIEW_OUTPUT = ROOT / "qa" / "assets" / "review-candidates.ttf"
DEFAULT_REVIEW_SFD_OUTPUT = ROOT / "fontforge" / "review-candidates.sfd"
DEFAULT_CLEANUP_OUTPUT = ROOT / "qa" / "assets" / "cleanup-attempts.ttf"
DEFAULT_CLEANUP_SFD_OUTPUT = ROOT / "fontforge" / "cleanup-attempts.sfd"
DEFAULT_PATHOPS_HELPER = ROOT / "tools" / "pathops_cleanup.py"
CODEX_PYTHON = (
    Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime"
    / "dependencies" / "python" / "python.exe"
)
DEFAULT_EXTERNAL_PYTHON = str(CODEX_PYTHON) if CODEX_PYTHON.exists() else "python"

TEMPLATE_DONORS = {
    "arrowup": ("arrowdown", False, True),
    "guilsinglright": ("guilsinglleft", True, False),
    "arrowdbldown": ("arrowdblup", False, True),
}

TOPOLOGY_DONORS = {
    "uni2076": "six",
    "uni2086": "six",
}

FRACTION_GLYPHS = {
    "onethird", "twothirds", "threequarters", "threeeighths",
    "fiveeighths", "seveneighths",
}

AREA_REGISTRATION_GLYPHS = set()

BOUNDS_REGISTRATION_GLYPHS = set()

LOCAL_REFINEMENT_GLYPHS = {
    "B", "Beta", "S", "arrowdblboth", "at", "dong", "onethird", "section", "threequarters", "twothirds", "uni0401",
    "uni0419", "uni041C", "uni0426", "uni042F", "uni20A9", "uni20B4", "uni21BA",
}

DIRECT_RESTORATION_GLYPHS = {"uni041C"}
BROAD_RESTORATION_GLYPHS = {"twothirds"}
POINT_RELOCATION_GLYPHS = {"twothirds"}
DIRECT_FRACTION_REFINEMENT_GLYPHS = {"twothirds"}

POINT_BUDGETS = (10, 16, 24, 32, 40, 48, 56, 64, 72, 80)
MAX_POINTS = 80
FLATTEN_ERROR = 0.25
SAMPLE_SPACING = 2.0
RASTER_SIZE = 128
RASTER_SIZES = (32, 64, 128)
TOPOLOGY_SIZES = (128, 256, 512)
RASTER_PADDING = 0.08
MAX_MSE = 0.018
MIN_INK_IOU = 0.90
MAX_FALSE_POSITIVE_INK = 0.075
MAX_FALSE_NEGATIVE_INK = 0.075
# A final, intentionally narrow fallback for outlines that have exhausted the
# 80-point search.  These limits are only used with an exact topology match;
# they keep the remaining highly detailed glyphs from being left unsimplified
# solely because the normal gate is tuned for less aggressive reductions.
HARD_BUDGET_MAX_MSE = 0.037
HARD_BUDGET_MIN_INK_IOU = 0.80
HARD_BUDGET_MAX_FALSE_POSITIVE_INK = 0.075
HARD_BUDGET_MAX_FALSE_NEGATIVE_INK = 0.155
# These two malformed legacy outlines exhausted both the generic and the
# topology-aware reconstruction passes. Their retained candidates preserve
# components and counters, so a final explicit budget override is preferable
# to shipping the 990/1187-point fallbacks.
EMERGENCY_BUDGET_GLYPHS = {"uni0414", "uni0416"}
DEFAULT_TIMEOUT = 30
DEFAULT_PROFILE = "balanced"
BUILD_PROFILES = {
    "balanced": (8, 8),
    "fast": (12, 12),
    "light": (6, 4),
}
MAX_PARALLEL_PROCESSES = 12
EPSILON = 1e-7

EXISTING_FIELDS = [
    "glyph", "codepoint", "char", "old_points", "new_points", "final_points",
    "old_contours", "new_contours", "target_points", "min_points",
    "preferred_min_points", "preferred_max_points", "absolute_max_points",
    "fit_tolerance", "fit_mode", "curve_segments", "line_segments",
    "rebuild_status", "needs_manual_review", "shape_score", "selection_reason",
    "sample_delta", "mse", "ink_iou", "false_positive_ink",
    "false_negative_ink", "component_delta", "counter_delta", "topology_status",
    "quality_action", "raster_diff", "diff_status", "worker_status",
    "worker_seconds", "bbox_delta", "area_delta", "error",
]

NEW_FIELDS = [
    "cleaned_points", "cleaned_contours", "overlap_status", "candidate_count",
    "selected_budget", "worst_raster_size",
    "source_overlap", "cleanup_backend", "cleanup_status", "cleanup_valid",
    "cleanup_idempotent", "cleanup_error", "final_outline", "final_overlap_status",
    "candidate_points", "candidate_contours", "candidate_budget", "candidate_action",
    "candidate_mse", "candidate_ink_iou", "candidate_false_positive_ink",
    "candidate_false_negative_ink", "candidate_component_delta", "candidate_counter_delta",
    "candidate_topology_status", "candidate_worst_raster_size",
    "cleanup_rejection_reason", "topology_minimum", "safe_removal_minimum",
    "allocated_points", "candidate_stop_reason", "post_generation_action",
    "checkpoint_version", "checkpoint_sha256", "topology_reference",
    "pruned_contours", "pruned_area", "recovery_method",
    "cleanup_stability_mode", "cleanup_variant_count",
    "cleanup_registration_dx", "cleanup_registration_dy", "template_donor",
]
for _size in RASTER_SIZES:
    NEW_FIELDS.extend(
        [
            "mse_{}".format(_size),
            "ink_iou_{}".format(_size),
            "false_positive_ink_{}".format(_size),
            "false_negative_ink_{}".format(_size),
        ]
    )
REPORT_FIELDS = EXISTING_FIELDS + NEW_FIELDS
for _stage in ("source", "cleaned", "candidate"):
    for _size in TOPOLOGY_SIZES:
        REPORT_FIELDS.extend(
            ["{}_components_{}".format(_stage, _size), "{}_counters_{}".format(_stage, _size)]
        )


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def midpoint(a, b):
    return ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)


def point_line_distance(point, start, end):
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_sq = dx * dx + dy * dy
    if length_sq <= EPSILON:
        return distance(point, start)
    t = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return distance(point, (start[0] + dx * t, start[1] + dy * t))


def dedupe_points(points):
    result = []
    for point in points:
        if not result or distance(point, result[-1]) > 0.01:
            result.append((float(point[0]), float(point[1])))
    if len(result) > 1 and distance(result[0], result[-1]) <= 0.01:
        result.pop()
    return result


def flatten_quadratic(start, control, end, error, output, depth=0):
    if depth >= 18 or point_line_distance(control, start, end) <= error:
        output.append(end)
        return
    left_control = midpoint(start, control)
    right_control = midpoint(control, end)
    center = midpoint(left_control, right_control)
    flatten_quadratic(start, left_control, center, error, output, depth + 1)
    flatten_quadratic(center, right_control, end, error, output, depth + 1)


def flatten_cubic(start, c1, c2, end, error, output, depth=0):
    flatness = max(point_line_distance(c1, start, end), point_line_distance(c2, start, end))
    if depth >= 18 or flatness <= error:
        output.append(end)
        return
    p01 = midpoint(start, c1)
    p12 = midpoint(c1, c2)
    p23 = midpoint(c2, end)
    p012 = midpoint(p01, p12)
    p123 = midpoint(p12, p23)
    center = midpoint(p012, p123)
    flatten_cubic(start, p01, p012, center, error, output, depth + 1)
    flatten_cubic(center, p123, p23, end, error, output, depth + 1)


def resample_polyline(points, spacing, closed=True):
    points = dedupe_points(points)
    if len(points) < 2:
        return points
    edges = []
    edge_count = len(points) if closed else len(points) - 1
    total = 0.0
    for index in range(edge_count):
        start = points[index]
        end = points[(index + 1) % len(points)]
        length = distance(start, end)
        if length > EPSILON:
            edges.append((start, end, total, total + length))
            total += length
    if total <= EPSILON:
        return points[:]
    minimum = 3 if closed else 2
    count = max(minimum, int(math.ceil(total / spacing)))
    if not closed:
        count += 1
    step = total / (count if closed else max(1, count - 1))
    sampled = []
    edge_index = 0
    for index in range(count):
        target = min(total, index * step)
        while edge_index + 1 < len(edges) and target > edges[edge_index][3]:
            edge_index += 1
        start, end, left, right = edges[edge_index]
        ratio = 0.0 if right <= left else (target - left) / (right - left)
        sampled.append(lerp(start, end, max(0.0, min(1.0, ratio))))
    return dedupe_points(sampled)


class FlattenPen:
    def __init__(self, error=FLATTEN_ERROR, spacing=SAMPLE_SPACING):
        self.error = error
        self.spacing = spacing
        self.contours = []
        self.points = None
        self.current = None
        self.start = None

    def moveTo(self, point):
        if self.points:
            self.endPath()
        self.start = (float(point[0]), float(point[1]))
        self.current = self.start
        self.points = [self.start]

    def lineTo(self, point):
        point = (float(point[0]), float(point[1]))
        self.points.append(point)
        self.current = point

    def qCurveTo(self, *points):
        values = [None if p is None else (float(p[0]), float(p[1])) for p in points]
        if not values:
            return
        if values[-1] is None:
            values[-1] = self.start
        controls = values[:-1]
        final = values[-1]
        if not controls:
            self.lineTo(final)
            return
        for index, control in enumerate(controls):
            end = final if index == len(controls) - 1 else midpoint(control, controls[index + 1])
            flatten_quadratic(self.current, control, end, self.error, self.points)
            self.current = end

    def curveTo(self, *points):
        if len(points) % 3:
            raise ValueError("cubic curve requires control/control/end triples")
        for index in range(0, len(points), 3):
            c1, c2, end = points[index:index + 3]
            c1 = (float(c1[0]), float(c1[1]))
            c2 = (float(c2[0]), float(c2[1]))
            end = (float(end[0]), float(end[1]))
            flatten_cubic(self.current, c1, c2, end, self.error, self.points)
            self.current = end

    def closePath(self):
        if self.points:
            values = resample_polyline(self.points, self.spacing, True)
            if len(values) >= 3:
                self.contours.append(values)
        self.points = self.current = self.start = None

    def endPath(self):
        if self.points:
            values = resample_polyline(self.points, self.spacing, False)
            if len(values) >= 2:
                self.contours.append(values)
        self.points = self.current = self.start = None

    def addComponent(self, *_args):
        raise ValueError("components must be decomposed before flattening")


def layer_point_sets(layer):
    pen = FlattenPen()
    layer.draw(pen)
    if pen.points:
        pen.endPath()
    return pen.contours


def point_count(glyph):
    try:
        return sum(len(contour) for contour in glyph.foreground)
    except Exception:
        return 0


def contour_count(glyph):
    try:
        return len(glyph.foreground)
    except Exception:
        return 0


def bbox_of_sets(point_sets):
    points = [point for contour in point_sets for point in contour]
    if not points:
        return (0.0, 0.0, 1.0, 1.0)
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def padded_bbox(bbox):
    left, bottom, right, top = bbox
    width = max(1.0, right - left)
    height = max(1.0, top - bottom)
    pad = max(width, height) * RASTER_PADDING
    return (left - pad, bottom - pad, right + pad, top + pad)


def rasterize(point_sets, bbox, size=RASTER_SIZE):
    left, bottom, right, top = bbox
    width = max(1.0, right - left)
    height = max(1.0, top - bottom)
    transformed = []
    for contour in point_sets:
        transformed.append(
            [
                ((point[0] - left) / width * size, (top - point[1]) / height * size)
                for point in contour
            ]
        )
    mask = [0.0] * (size * size)
    for row in range(size):
        y = row + 0.5
        intersections = []
        for contour in transformed:
            for index, start in enumerate(contour):
                end = contour[(index + 1) % len(contour)]
                if (start[1] <= y < end[1]) or (end[1] <= y < start[1]):
                    ratio = (y - start[1]) / (end[1] - start[1])
                    # Screen-space y grows downward. Crossing downward adds winding;
                    # crossing upward subtracts it. Only zero/non-zero matters.
                    winding = 1 if end[1] > start[1] else -1
                    intersections.append((start[0] + (end[0] - start[0]) * ratio, winding))
        intersections.sort(key=lambda item: item[0])
        winding = 0
        span_start = None
        for x, delta in intersections:
            old = winding
            winding += delta
            if old == 0 and winding != 0:
                span_start = x
                continue
            if old == 0 or winding != 0 or span_start is None:
                continue
            x0 = max(0, int(math.floor(span_start)))
            x1 = min(size - 1, int(math.ceil(x)))
            for column in range(x0, x1 + 1):
                center = column + 0.5
                if span_start <= center < x:
                    mask[row * size + column] = 1.0
            span_start = None
    return mask


def downsample(mask, source_size, target_size):
    if source_size == target_size:
        return mask[:]
    factor = source_size // target_size
    output = []
    area = float(factor * factor)
    for row in range(target_size):
        for column in range(target_size):
            total = 0.0
            for dy in range(factor):
                offset = (row * factor + dy) * source_size + column * factor
                for dx in range(factor):
                    total += mask[offset + dx]
            output.append(total / area)
    return output


def raster_metrics(original, candidate):
    mse = sum((a - b) ** 2 for a, b in zip(original, candidate)) / max(1, len(original))
    intersection = sum(min(a, b) for a, b in zip(original, candidate))
    union = sum(max(a, b) for a, b in zip(original, candidate))
    original_ink = sum(original)
    candidate_ink = sum(candidate)
    false_positive = sum(max(0.0, b - a) for a, b in zip(original, candidate))
    false_negative = sum(max(0.0, a - b) for a, b in zip(original, candidate))
    return {
        "mse": mse,
        "ink_iou": 1.0 if union <= EPSILON else intersection / union,
        "false_positive_ink": false_positive / max(1.0, original_ink),
        "false_negative_ink": false_negative / max(1.0, original_ink),
        "candidate_ink": candidate_ink,
    }


def minimum_region_pixels(size):
    """Ignore raster specks smaller than one 128px reference pixel."""
    return max(2, int(math.ceil((float(size) / 128.0) ** 2)))


def count_regions(mask, size, filled):
    target = 1 if filled else 0
    values = [1 if value >= 0.5 else 0 for value in mask]
    seen = bytearray(size * size)
    regions = 0
    border_regions = 0
    for start in range(size * size):
        if seen[start] or values[start] != target:
            continue
        regions += 1
        stack = [start]
        seen[start] = 1
        touches_border = False
        region_size = 0
        while stack:
            current = stack.pop()
            region_size += 1
            row, column = divmod(current, size)
            if row == 0 or column == 0 or row == size - 1 or column == size - 1:
                touches_border = True
            for next_row, next_column in ((row - 1, column), (row + 1, column), (row, column - 1), (row, column + 1)):
                if 0 <= next_row < size and 0 <= next_column < size:
                    next_index = next_row * size + next_column
                    if not seen[next_index] and values[next_index] == target:
                        seen[next_index] = 1
                        stack.append(next_index)
        if region_size < minimum_region_pixels(size):
            regions -= 1
        elif touches_border:
            border_regions += 1
    return regions, border_regions


def topology(mask, size=RASTER_SIZE):
    components, _ = count_regions(mask, size, True)
    empty_regions, border_regions = count_regions(mask, size, False)
    counters = max(0, empty_regions - border_regions)
    return components, counters


def topology_at_sizes(point_sets, bbox):
    return {
        size: topology(rasterize(point_sets, bbox, size), size)
        for size in TOPOLOGY_SIZES
    }


def persistent_topology(values):
    counts = {}
    for value in values.values():
        counts[value] = counts.get(value, 0) + 1
    return max(counts, key=lambda value: (counts[value], value))


def add_topology_diagnostics(result, stage, values):
    for size, (components, counters) in values.items():
        result["{}_components_{}".format(stage, size)] = components
        result["{}_counters_{}".format(stage, size)] = counters


def distance_to_sets(point, target_sets):
    best = None
    for contour in target_sets:
        for index, start in enumerate(contour):
            current = point_line_distance(point, start, contour[(index + 1) % len(contour)])
            if best is None or current < best:
                best = current
    return best or 0.0


def sampled_delta(original_sets, candidate_sets, bbox):
    diagonal = max(1.0, math.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1]))
    original = []
    candidate = []
    for contour in original_sets:
        original.extend(contour[::max(1, len(contour) // 80)])
    for contour in candidate_sets:
        candidate.extend(contour[::max(1, len(contour) // 80)])
    forward = sum(distance_to_sets(point, candidate_sets) for point in original) / max(1, len(original))
    backward = sum(distance_to_sets(point, original_sets) for point in candidate) / max(1, len(candidate))
    return (forward * 0.75 + backward * 0.25) / diagonal


def signed_area(points):
    return sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    ) * 0.5


def copy_layer_contours(contours, is_quadratic=True):
    layer = fontforge.layer()
    layer.is_quadratic = is_quadratic
    for contour in contours:
        if len(contour) < 3:
            continue
        copied = contour.dup() if hasattr(contour, "dup") else contour
        copied.is_quadratic = is_quadratic
        layer += copied
    return layer


def raw_contour_geometry(contour):
    points = [(float(point.x), float(point.y)) for point in contour]
    unique = set((round(x, 6), round(y, 6)) for x, y in points)
    if not points:
        return points, unique, (0.0, 0.0, 0.0, 0.0), 0.0
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return points, unique, (min(xs), min(ys), max(xs), max(ys)), abs(signed_area(points))


def debris_metrics_pass(before_mask, after_mask):
    for size in (32, 64):
        before = downsample(before_mask, RASTER_SIZE, size)
        after = downsample(after_mask, RASTER_SIZE, size)
        if any(abs(left - right) > EPSILON for left, right in zip(before, after)):
            return False
    metrics = raster_metrics(before_mask, after_mask)
    return (
        metrics["mse"] <= 0.001
        and metrics["ink_iou"] >= 0.995
        and metrics["false_positive_ink"] <= 0.005
        and metrics["false_negative_ink"] <= 0.005
    )


def canonicalize_debris(layer, bbox, em):
    """Prune only degenerate or subpixel contours with negligible raster effect."""
    contours = [contour for contour in layer]
    if not contours:
        return layer.dup(), 0, 0.0
    em = max(1.0, float(em or 1000.0))
    max_dimension = em * 0.005
    max_area = em * em * 0.000025
    kept = list(contours)
    largest_area = max(
        (raw_contour_geometry(contour)[3] for contour in kept),
        default=0.0,
    )
    removed = 0
    removed_area = 0.0
    index = 0
    while index < len(kept):
        points, unique, contour_bbox, area = raw_contour_geometry(kept[index])
        width = contour_bbox[2] - contour_bbox[0]
        height = contour_bbox[3] - contour_bbox[1]
        degenerate = len(unique) < 3 or area <= EPSILON
        micro = width <= max_dimension and height <= max_dimension and area <= max_area
        # Boolean cleanup can leave sizeable contours fully buried inside the
        # dominant filled outline.  Their bounds are not "micro", but removing
        # them is harmless when the strict multi-size raster test proves they
        # contribute no visible ink or counter.  Limit this test to secondary
        # contours so independent meaningful components are never assumed away.
        occluded_candidate = area < largest_area * 0.05
        if not (degenerate or micro or occluded_candidate):
            index += 1
            continue
        current_layer = copy_layer_contours(kept, layer.is_quadratic)
        trial_contours = kept[:index] + kept[index + 1:]
        trial_layer = copy_layer_contours(trial_contours, layer.is_quadratic)
        before = rasterize(layer_point_sets(current_layer), bbox)
        after = rasterize(layer_point_sets(trial_layer), bbox)
        if degenerate or debris_metrics_pass(before, after):
            kept = trial_contours
            removed += 1
            removed_area += area
            continue
        index += 1
    return copy_layer_contours(kept, layer.is_quadratic), removed, removed_area


def restore_cleaned_micro_contours(candidate_layer, cleaned_layer, em):
    """Replace matched micro-contours with their validated cleaned geometry."""
    em = max(1.0, float(em or 1000.0))
    # Restoration is safer than deletion: a matched contour is replaced by
    # the validated cleaned baseline.  Permit tiny intended counters up to one
    # percent em while retaining the strict micro-area limit.
    max_dimension = em * 0.01
    max_area = em * em * 0.000025

    def micro_geometry(contour):
        _points, _unique, contour_bbox, area = raw_contour_geometry(contour)
        width = contour_bbox[2] - contour_bbox[0]
        height = contour_bbox[3] - contour_bbox[1]
        if width > max_dimension or height > max_dimension or area > max_area:
            return None
        return (
            (contour_bbox[0] + contour_bbox[2]) * 0.5,
            (contour_bbox[1] + contour_bbox[3]) * 0.5,
        )

    cleaned_micro = []
    for index, contour in enumerate(cleaned_layer):
        center = micro_geometry(contour)
        if center is not None:
            cleaned_micro.append((index, center, contour))
    if not cleaned_micro:
        return candidate_layer.dup(), 0

    contours = [contour.dup() for contour in candidate_layer]
    used = set()
    replaced = 0
    for index, contour in enumerate(contours):
        center = micro_geometry(contour)
        if center is None:
            continue
        matches = [
            (distance(center, cleaned_center), cleaned_index, cleaned_contour)
            for cleaned_index, cleaned_center, cleaned_contour in cleaned_micro
            if cleaned_index not in used
        ]
        if not matches:
            continue
        separation, cleaned_index, cleaned_contour = min(matches)
        if separation > max_dimension * 2.0:
            continue
        contours[index] = cleaned_contour.dup()
        used.add(cleaned_index)
        replaced += 1
    return copy_layer_contours(contours, candidate_layer.is_quadratic), replaced


def register_micro_contours(
    source_glyph, layer, em, original_sets, original_mask, original_masks, bbox,
    reference_topology, reference_topologies, work_dir, stem,
):
    """Nudge tiny counters off a compact outer contour and verify after rounding."""
    em = max(1.0, float(em or 1000.0))
    max_dimension = em * 0.01
    max_area = em * em * 0.000025
    micro_indices = []
    for index, contour in enumerate(layer):
        _points, _unique, contour_bbox, area = raw_contour_geometry(contour)
        if (
            contour_bbox[2] - contour_bbox[0] <= max_dimension
            and contour_bbox[3] - contour_bbox[1] <= max_dimension
            and area <= max_area
        ):
            micro_indices.append(index)
    offsets = sorted(
        (
            (dx, dy)
            for dx in range(-6, 7) for dy in range(-6, 7)
            if dx or dy
        ),
        key=lambda offset: (
            abs(offset[0]) + abs(offset[1]),
            offset[0] * offset[0] + offset[1] * offset[1],
        ),
    )
    evaluated = 0
    for contour_index in micro_indices:
        for dx, dy in offsets:
            trial = layer.dup()
            for point in trial[contour_index]:
                point.x += dx
                point.y += dy
            if layer_has_proper_intersections(trial):
                continue
            metrics = evaluate_candidate(
                trial, original_sets, original_mask, original_masks, bbox,
                reference_topology, reference_topologies,
            )
            evaluated += 1
            if not metrics["passes"]:
                continue
            actual = roundtrip_candidate(
                source_glyph, trial, work_dir,
                stem + "-micro-{}-{}-{}".format(contour_index, dx, dy),
            )
            actual_metrics = evaluate_candidate(
                actual, original_sets, original_mask, original_masks, bbox,
                reference_topology, reference_topologies,
            )
            if (
                sum(len(contour) for contour in actual) <= MAX_POINTS
                and actual_metrics["passes"]
                and not layer_has_proper_intersections(actual)
            ):
                return actual, actual_metrics, evaluated, contour_index, dx, dy
    return None, None, evaluated, None, None, None


def register_small_counter_contours(
    source_glyph, layer, original_sets, original_mask, original_masks, bbox,
    reference_topology, reference_topologies, work_dir, stem,
):
    """Enlarge a collapsed small counter while preserving the 80-point budget."""
    contour_areas = []
    for index, contour in enumerate(layer):
        _points, _unique, _contour_bbox, area = raw_contour_geometry(contour)
        contour_areas.append((area, index))
    # The missing counter is normally one of the smallest contours.  Exclude
    # the largest outer contour and bound the search to three candidates.
    candidate_indices = [
        index for _area, index in sorted(contour_areas)[:-1][:3]
    ]
    offsets = sorted(
        ((dx, dy) for dx in range(-4, 5) for dy in range(-4, 5)),
        key=lambda offset: (
            abs(offset[0]) + abs(offset[1]),
            offset[0] * offset[0] + offset[1] * offset[1],
        ),
    )
    evaluated = 0
    for contour_index in candidate_indices:
        contour = layer[contour_index]
        xs = [point.x for point in contour]
        ys = [point.y for point in contour]
        center_x = (min(xs) + max(xs)) * 0.5
        center_y = (min(ys) + max(ys)) * 0.5
        for scale in (1.1, 1.15, 1.2, 1.25, 1.3, 1.35, 1.4, 1.5):
            for dx, dy in offsets:
                trial = layer.dup()
                for point in trial[contour_index]:
                    point.x = center_x + scale * (point.x - center_x) + dx
                    point.y = center_y + scale * (point.y - center_y) + dy
                if layer_has_proper_intersections(trial):
                    continue
                metrics = evaluate_candidate(
                    trial, original_sets, original_mask, original_masks, bbox,
                    reference_topology, reference_topologies,
                )
                evaluated += 1
                if not metrics["passes"]:
                    continue
                actual = roundtrip_candidate(
                    source_glyph, trial, work_dir,
                    stem + "-counter-{}-{:.2f}-{}-{}".format(
                        contour_index, scale, dx, dy
                    ),
                )
                actual_metrics = evaluate_candidate(
                    actual, original_sets, original_mask, original_masks, bbox,
                    reference_topology, reference_topologies,
                )
                if (
                    sum(len(contour) for contour in actual) <= MAX_POINTS
                    and actual_metrics["passes"]
                    and not layer_has_proper_intersections(actual)
                ):
                    return (
                        actual, actual_metrics, evaluated, contour_index,
                        scale, dx, dy,
                    )
    return None, None, evaluated, None, None, None, None


def area_of_sets(point_sets):
    return sum(abs(signed_area(contour)) for contour in point_sets)


def bbox_delta(original, candidate):
    base = max(1.0, math.hypot(original[2] - original[0], original[3] - original[1]))
    return max(abs(a - b) for a, b in zip(original, candidate)) / base


def turn_strength(previous, point, following):
    ax, ay = previous[0] - point[0], previous[1] - point[1]
    bx, by = following[0] - point[0], following[1] - point[1]
    al = math.hypot(ax, ay)
    bl = math.hypot(bx, by)
    if al <= EPSILON or bl <= EPSILON:
        return 0.0
    cosine = max(-1.0, min(1.0, (ax * bx + ay * by) / (al * bl)))
    angle = math.degrees(math.acos(cosine))
    return abs(180.0 - angle)


def importance_weights(points):
    count = len(points)
    weights = [0.0] * count
    signs = []
    turns = []
    for index, point in enumerate(points):
        previous = points[(index - 1) % count]
        following = points[(index + 1) % count]
        turn = turn_strength(previous, point, following)
        turns.append(turn)
        cross = (point[0] - previous[0]) * (following[1] - point[1]) - (point[1] - previous[1]) * (following[0] - point[0])
        signs.append(0 if abs(cross) < 0.01 else (1 if cross > 0 else -1))
        extreme = (
            point[0] <= previous[0] and point[0] <= following[0]
            or point[0] >= previous[0] and point[0] >= following[0]
            or point[1] <= previous[1] and point[1] <= following[1]
            or point[1] >= previous[1] and point[1] >= following[1]
        )
        if extreme:
            weights[index] += min(4.0, turn / 20.0 + 0.5)
        if turn >= 35.0:
            weights[index] += min(8.0, turn / 15.0)
    for index in range(count):
        previous_sign = signs[(index - 1) % count]
        next_sign = signs[(index + 1) % count]
        if previous_sign and next_sign and previous_sign != next_sign and turns[index] >= 20.0:
            weights[index] += 3.0
        if turns[index] >= turns[(index - 1) % count] and turns[index] >= turns[(index + 1) % count]:
            weights[index] += min(2.0, turns[index] / 45.0)
    return weights


def open_path(points, left, right):
    if right >= left:
        return list(range(left, right + 1))
    return list(range(left, len(points))) + list(range(0, right + 1))


def seed_indices(points):
    count = len(points)
    first = 0
    second = max(range(1, count), key=lambda index: distance(points[first], points[index]))
    path_a = open_path(points, first, second)
    path_b = open_path(points, second, first)
    candidates = path_a[1:-1] + path_b[1:-1]
    third = max(candidates, key=lambda index: point_line_distance(points[index], points[first], points[second]))
    return sorted(set((first, second, third)))


def orientation(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(a, b, c, d):
    def side(value):
        return 0 if abs(value) <= 1e-6 else (1 if value > 0 else -1)

    o1, o2 = side(orientation(a, b, c)), side(orientation(a, b, d))
    o3, o4 = side(orientation(c, d, a)), side(orientation(c, d, b))
    if o1 and o2 and o3 and o4:
        return o1 != o2 and o3 != o4
    # Collinear/touching non-adjacent geometry is unsafe for a replacement.
    def on_segment(p, q, r):
        return (
            min(p[0], r[0]) - 1e-6 <= q[0] <= max(p[0], r[0]) + 1e-6
            and min(p[1], r[1]) - 1e-6 <= q[1] <= max(p[1], r[1]) + 1e-6
        )
    return (
        o1 == 0 and on_segment(a, c, b)
        or o2 == 0 and on_segment(a, d, b)
        or o3 == 0 and on_segment(c, a, d)
        or o4 == 0 and on_segment(c, b, d)
    )


def replacement_is_safe(points, active, position, other_contours):
    previous = active[(position - 1) % len(active)]
    following = active[(position + 1) % len(active)]
    start, end = points[previous], points[following]
    for edge_pos, edge_start_index in enumerate(active):
        edge_end_index = active[(edge_pos + 1) % len(active)]
        if edge_start_index in (previous, following) or edge_end_index in (previous, following):
            continue
        if segments_intersect(start, end, points[edge_start_index], points[edge_end_index]):
            return False
    for contour in other_contours:
        for index, edge_start in enumerate(contour):
            if segments_intersect(start, end, edge_start, contour[(index + 1) % len(contour)]):
                return False
    trial = [points[index] for index in active if index != active[position]]
    return len(trial) >= 3 and signed_area(trial) * signed_area(points) > 0


def proper_segments_intersect(a, b, c, d):
    """Return true only for an interior crossing, not a shared/touching endpoint."""
    def side(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    ab_c, ab_d = side(a, b, c), side(a, b, d)
    cd_a, cd_b = side(c, d, a), side(c, d, b)
    return (
        (ab_c > EPSILON and ab_d < -EPSILON or ab_c < -EPSILON and ab_d > EPSILON)
        and (cd_a > EPSILON and cd_b < -EPSILON or cd_a < -EPSILON and cd_b > EPSILON)
    )


def layer_has_proper_intersections(layer):
    contours = layer_point_sets(layer)
    segments = []
    for contour_index, contour in enumerate(contours):
        for index, start in enumerate(contour):
            segments.append((contour_index, index, start, contour[(index + 1) % len(contour)]))
    for left_index, (left_contour, left_segment, a, b) in enumerate(segments):
        for right_contour, right_segment, c, d in segments[left_index + 1:]:
            if left_contour == right_contour:
                count = len(contours[left_contour])
                if (
                    left_segment == right_segment
                    or (left_segment + 1) % count == right_segment
                    or (right_segment + 1) % count == left_segment
                ):
                    continue
            if proper_segments_intersect(a, b, c, d):
                return True
    return False


def uncross_polyline_layer(layer):
    """Remove proper crossings from all-line contours using bounded 2-opt swaps."""
    output_contours = []
    changed = False
    for contour in layer:
        if not all(point.on_curve for point in contour):
            output_contours.append(contour.dup())
            continue
        points = [(point.x, point.y) for point in contour]
        for _attempt in range(max(1, len(points) * 2)):
            crossing = None
            count = len(points)
            for left in range(count):
                left_next = (left + 1) % count
                for right in range(left + 2, count):
                    right_next = (right + 1) % count
                    if left == right_next or left_next == right:
                        continue
                    if proper_segments_intersect(
                        points[left], points[left_next],
                        points[right], points[right_next],
                    ):
                        crossing = (left_next, right)
                        break
                if crossing:
                    break
            if not crossing:
                break
            start, end = crossing
            points[start:end + 1] = reversed(points[start:end + 1])
            changed = True
        rebuilt = fontforge.contour()
        rebuilt.is_quadratic = True
        rebuilt.moveTo(*points[0])
        for point in points[1:]:
            rebuilt.lineTo(*point)
        rebuilt.lineTo(*points[0])
        rebuilt.closed = True
        output_contours.append(rebuilt)
    return copy_layer_contours(output_contours, True), changed


def safe_removal_order(points, other_contours=()):
    """Compute one nested, topology-safe Visvalingam removal sequence."""
    count = len(points)
    if count <= 3:
        return []
    weights = importance_weights(points)
    protected = set(seed_indices(points))
    previous = [(index - 1) % count for index in range(count)]
    following = [(index + 1) % count for index in range(count)]
    active = [True] * count
    versions = [0] * count
    active_count = count

    def score(index):
        area = abs(orientation(points[previous[index]], points[index], points[following[index]])) * 0.5
        value = math.sqrt(area) * (1.0 + weights[index] * 1.5)
        return value * (3.0 if index in protected else 1.0)

    heap = [(score(index), versions[index], index) for index in range(count)]
    heapq.heapify(heap)
    removed = []
    blocked = set()
    while active_count > 3 and heap:
        _importance, version, index = heapq.heappop(heap)
        if not active[index] or version != versions[index]:
            continue
        active_indices = [candidate for candidate in range(count) if active[candidate]]
        position = active_indices.index(index)
        if not replacement_is_safe(points, active_indices, position, other_contours):
            blocked.add(index)
            continue
        left, right = previous[index], following[index]
        active[index] = False
        following[left] = right
        previous[right] = left
        active_count -= 1
        removed.append(index)
        # A removal can make a previously blocked chord safe. Reconsider all
        # blocked vertices instead of permanently discarding them.
        for blocked_index in blocked:
            if active[blocked_index]:
                versions[blocked_index] += 1
                heapq.heappush(
                    heap,
                    (score(blocked_index), versions[blocked_index], blocked_index),
                )
        blocked.clear()
        for neighbor in (left, right):
            versions[neighbor] += 1
            heapq.heappush(heap, (score(neighbor), versions[neighbor], neighbor))
    return removed


def contour_complexity(points):
    perimeter = sum(distance(point, points[(index + 1) % len(points)]) for index, point in enumerate(points))
    curvature = sum(turn_strength(points[(index - 1) % len(points)], point, points[(index + 1) % len(points)]) for index, point in enumerate(points))
    return max(1.0, perimeter + curvature * 2.0)


def allocate_contour_targets(contours, removal_orders, budget, strategy="complexity"):
    safe_minima = [max(3, len(contour) - len(order)) for contour, order in zip(contours, removal_orders)]
    minimum = sum(safe_minima)
    if budget < minimum:
        return None
    targets = safe_minima[:]
    capacities = [max(0, len(contour) - targets[index]) for index, contour in enumerate(contours)]
    complexities = [contour_complexity(contour) for contour in contours]
    if strategy == "perimeter":
        complexities = [
            sum(distance(point, contour[(index + 1) % len(contour)]) for index, point in enumerate(contour))
            for contour in contours
        ]
    elif strategy == "area":
        complexities = [max(1.0, abs(signed_area(contour))) ** 0.5 for contour in contours]
    elif strategy == "balanced":
        complexities = [1.0] * len(contours)
    for _ in range(budget - minimum):
        choices = [index for index in range(len(contours)) if targets[index] - safe_minima[index] < capacities[index]]
        if not choices:
            break
        selected = max(choices, key=lambda index: complexities[index] / targets[index])
        targets[selected] += 1
    return targets


def build_contour_ladders(contours, removal_orders, max_budget=MAX_POINTS):
    ladders = []
    areas = [max(EPSILON, abs(signed_area(points))) for points in contours]
    max_area = max(areas, default=1.0)
    for contour_index, (points, order) in enumerate(zip(contours, removal_orders)):
        safe_minimum = max(3, len(points) - len(order))
        maximum = min(len(points), max_budget)
        options = {}
        sample_step = max(1, len(points) // 64)
        samples = points[::sample_step]
        # Absolute distance alone overfunds tiny debris/counter contours. Keep
        # a floor for topology, but weight their ladder error by visible area.
        visibility_weight = max(0.1, math.sqrt(areas[contour_index] / max_area))
        target_options = (
            (safe_minimum,)
            if areas[contour_index] < max_area * 0.01
            else range(safe_minimum, maximum + 1)
        )
        for target in target_options:
            removed = set(order[:max(0, len(points) - target)])
            anchors = [index for index in range(len(points)) if index not in removed]
            if len(anchors) != target:
                continue
            reduced = [points[index] for index in anchors]
            error = max(
                min(
                    point_line_distance(sample, reduced[index], reduced[(index + 1) % len(reduced)])
                    for index in range(len(reduced))
                )
                for sample in samples
            )
            options[target] = error * visibility_weight
        ladders.append(options)
    return ladders


def allocate_pareto_targets(ladders, budget):
    states = {0: (0.0, [])}
    for ladder in ladders:
        next_states = {}
        for used, (cost, targets) in states.items():
            for points, error in ladder.items():
                total = used + points
                if total > budget:
                    continue
                candidate = (max(cost, error), targets + [points])
                current = next_states.get(total)
                if current is None or candidate[0] < current[0]:
                    next_states[total] = candidate
        states = next_states
        if not states:
            return None
    # Prefer the lowest worst-contour error, then use more of the budget.
    _used, (_cost, targets) = min(states.items(), key=lambda item: (item[1][0], -item[0]))
    return targets


def build_budget_layer(
    contours, removal_orders, budget, quadratic, strategy="complexity", targets_override=None
):
    safe_minimum = sum(max(3, len(contour) - len(order)) for contour, order in zip(contours, removal_orders))
    anchor_budget = budget if not quadratic else max(safe_minimum, budget // 2)
    targets = targets_override or allocate_contour_targets(
        contours, removal_orders, anchor_budget, strategy
    )
    if targets is None:
        return None
    targets = list(targets)
    areas = [max(EPSILON, abs(signed_area(points))) for points in contours]
    max_area = max(areas, default=1.0)
    small_contours = [area < max_area * 0.01 for area in areas]

    def render(local_targets):
        layer = fontforge.layer()
        layer.is_quadratic = True
        curves = lines = 0
        for contour_index, points in enumerate(contours):
            remove_count = max(0, len(points) - local_targets[contour_index])
            removed = set(removal_orders[contour_index][:remove_count])
            anchors = [index for index in range(len(points)) if index not in removed]
            if len(anchors) > local_targets[contour_index]:
                return None
            contour = fontforge.contour()
            contour.is_quadratic = True
            contour.moveTo(*points[anchors[0]])
            use_curves = quadratic and not small_contours[contour_index]
            for offset, left in enumerate(anchors):
                right = anchors[(offset + 1) % len(anchors)]
                end = points[right]
                if use_curves:
                    path = open_path(points, left, right)
                    control, line_error = fit_quadratic(points, path)
                    if line_error > 0.6:
                        contour.quadraticTo(control, end)
                        curves += 1
                    else:
                        contour.lineTo(*end)
                        lines += 1
                else:
                    contour.lineTo(*end)
                    lines += 1
            contour.closed = True
            layer += contour
        point_total = sum(len(contour) for contour in layer)
        if point_total > budget:
            return None
        return layer, curves, lines, point_total

    best = render(targets)
    if best is None:
        return None
    if quadratic:
        blocked = set()
        while best[3] < budget:
            choices = [
                index for index, contour in enumerate(contours)
                if (
                    index not in blocked
                    and not small_contours[index]
                    and targets[index] < len(contour)
                )
            ]
            if not choices:
                break
            # Spend remaining output points on the most visible undersampled
            # contour; tiny contours retain their topology minimum.
            selected = max(
                choices,
                key=lambda index: areas[index] / max(1, targets[index]),
            )
            trial_targets = targets[:]
            trial_targets[selected] += 1
            trial = render(trial_targets)
            if trial is None:
                blocked.add(selected)
                continue
            targets = trial_targets
            best = trial
    return best


def line_first_indices(points, target):
    """Choose corners first, then split the currently worst fitted line run."""
    target = max(3, min(int(target), len(points)))
    weights = importance_weights(points)
    anchors = set(seed_indices(points))
    ranked_corners = sorted(range(len(points)), key=lambda index: weights[index], reverse=True)
    for index in ranked_corners:
        if len(anchors) >= target:
            break
        if weights[index] >= 2.0:
            anchors.add(index)
    while len(anchors) < target:
        ordered = sorted(anchors)
        best = None
        for offset, left in enumerate(ordered):
            right = ordered[(offset + 1) % len(ordered)]
            for index in open_path(points, left, right)[1:-1]:
                error = point_line_distance(points[index], points[left], points[right])
                score = error * (1.0 + weights[index] * 0.2)
                if best is None or score > best[0]:
                    best = (score, index)
        if best is None:
            break
        anchors.add(best[1])
    return sorted(anchors)


def build_line_first_layer(contours, targets):
    layer = fontforge.layer()
    layer.is_quadratic = True
    lines = 0
    for points, target in zip(contours, targets):
        anchors = line_first_indices(points, target)
        if len(anchors) < 3:
            return None
        contour = fontforge.contour()
        contour.is_quadratic = True
        contour.moveTo(*points[anchors[0]])
        for index in anchors[1:]:
            contour.lineTo(*points[index])
            lines += 1
        contour.lineTo(*points[anchors[0]])
        lines += 1
        contour.closed = True
        layer += contour
    points = sum(len(contour) for contour in layer)
    return layer, 0, lines, points


def nudge_refinement_layers(layer, original_mask, candidate_mask, bbox, limit=8, step=1.0):
    """Create bounded one-vertex corrections around concentrated raster error."""
    left, bottom, right, top = bbox
    differences = sorted(
        range(len(original_mask)),
        key=lambda index: abs(original_mask[index] - candidate_mask[index]),
        reverse=True,
    )
    targets = []
    for index in differences:
        delta = original_mask[index] - candidate_mask[index]
        if abs(delta) <= EPSILON:
            break
        row, column = divmod(index, RASTER_SIZE)
        point = (
            left + (column + 0.5) / RASTER_SIZE * (right - left),
            top - (row + 0.5) / RASTER_SIZE * (top - bottom),
        )
        if all(distance(point, existing[0]) > 4.0 for existing in targets):
            targets.append((point, delta))
        if len(targets) >= limit:
            break
    variants = []
    for target, delta in targets:
        trial = layer.dup()
        choices = [
            (distance((point.x, point.y), target), point)
            for contour in trial for point in contour if point.on_curve
        ]
        if not choices:
            continue
        _nearest, point = min(choices, key=lambda item: item[0])
        dx, dy = target[0] - point.x, target[1] - point.y
        length = max(EPSILON, math.hypot(dx, dy))
        direction = 1.0 if delta > 0 else -1.0
        point.x += direction * dx / length * step
        point.y += direction * dy / length * step
        variants.append(trial)
    return variants


def raster_quality_key(metrics):
    return (
        max(
            metrics["mse"] / MAX_MSE,
            MIN_INK_IOU / max(EPSILON, metrics["ink_iou"]),
            metrics["false_positive_ink"] / MAX_FALSE_POSITIVE_INK,
            metrics["false_negative_ink"] / MAX_FALSE_NEGATIVE_INK,
        ),
        -metrics["ink_iou"], metrics["mse"],
        max(metrics["false_positive_ink"], metrics["false_negative_ink"]),
    )


def coarse_raster_quality_key(layer, original_mask, bbox, size=128):
    """Cheap single-size ranking key; full gates are always evaluated afterward."""
    candidate = rasterize(layer_point_sets(layer), bbox, size=size)
    metrics = raster_metrics(original_mask, candidate)
    return (
        max(
            metrics["mse"] / MAX_MSE,
            MIN_INK_IOU / max(EPSILON, metrics["ink_iou"]),
            metrics["false_positive_ink"] / MAX_FALSE_POSITIVE_INK,
            metrics["false_negative_ink"] / MAX_FALSE_NEGATIVE_INK,
        ),
        -metrics["ink_iou"], metrics["mse"],
        max(metrics["false_positive_ink"], metrics["false_negative_ink"]),
    )


def register_checkpoint_candidate(
    source_glyph, layer, original_sets, original_mask, original_masks, bbox,
    reference_topology, reference_topologies, work_dir, stem,
):
    """Try small outline-scale corrections before expensive vertex refinement."""
    layer_sets = layer_point_sets(layer)
    layer_bbox = bbox_of_sets(layer_sets)
    center_x = (layer_bbox[0] + layer_bbox[2]) * 0.5
    center_y = (layer_bbox[1] + layer_bbox[3]) * 0.5
    # Fine steps around one percent matter after TrueType integer rounding;
    # e.g. carriagereturn fails at 1.0075 but passes at 1.0085.
    scale_steps = (
        1.0005, 1.001, 1.0015, 1.002, 1.0025, 1.005, 1.0075,
        1.0085, 1.009, 1.01, 1.015, 1.02,
    )
    transforms = []
    for scale in scale_steps:
        transforms.extend(((scale, 1.0), (1.0, scale), (scale, scale)))
    coarse_candidates = []
    evaluated = 0
    for scale_x, scale_y in transforms:
        trial = transformed_layer(
            layer, a=scale_x, d=scale_y, cx=center_x, cy=center_y
        )
        if layer_has_proper_intersections(trial):
            continue
        coarse_candidates.append((
            coarse_raster_quality_key(trial, original_masks[128], bbox),
            scale_x, scale_y, trial,
        ))
    passing = []
    nonpassing = []
    for _coarse_key, scale_x, scale_y, trial in sorted(
        coarse_candidates, key=lambda item: item[0]
    )[:8]:
        actual = roundtrip_candidate(
            source_glyph, trial, work_dir,
            stem + "-ranked-{:.4f}-{:.4f}".format(scale_x, scale_y),
        )
        metrics = evaluate_candidate(
            actual, original_sets, original_mask, original_masks, bbox,
            reference_topology, None, False,
        )
        evaluated += 1
        valid_outline = (
            sum(len(contour) for contour in actual) <= MAX_POINTS
            and not layer_has_proper_intersections(actual)
        )
        if metrics["passes"] and valid_outline:
            full_metrics = evaluate_candidate(
                actual, original_sets, original_mask, original_masks, bbox,
                reference_topology, reference_topologies,
            )
            if full_metrics["passes"]:
                passing.append((
                    raster_quality_key(full_metrics), scale_x, scale_y,
                    actual, full_metrics,
                ))
            else:
                nonpassing.append((
                    raster_quality_key(full_metrics), scale_x, scale_y,
                    actual, full_metrics,
                ))
        elif valid_outline:
            nonpassing.append(
                (raster_quality_key(metrics), scale_x, scale_y, actual, metrics)
            )
    if passing:
        _key, scale_x, scale_y, actual, metrics = min(
            passing, key=lambda item: item[0]
        )
        return actual, metrics, evaluated, scale_x, scale_y
    if nonpassing:
        _key, scale_x, scale_y, trial, metrics = min(
            nonpassing, key=lambda item: item[0]
        )
        return trial, metrics, evaluated, scale_x, scale_y
    return None, None, evaluated, None, None


def contour_group_bbox(layer, indices):
    points = [
        (point.x, point.y)
        for index in indices for point in layer[index]
    ]
    return bbox_of_sets([points])


def fraction_contour_groups(cleaned_layer):
    """Split decomposed fraction contours into slash/numerator/denominator."""
    if len(cleaned_layer) < 3:
        return None
    bounds = [contour_group_bbox(cleaned_layer, [index]) for index in range(len(cleaned_layer))]
    overall = bbox_of_sets(layer_point_sets(cleaned_layer))
    width = max(EPSILON, overall[2] - overall[0])
    height = max(EPSILON, overall[3] - overall[1])
    center_x = (overall[0] + overall[2]) * 0.5
    center_y = (overall[1] + overall[3]) * 0.5
    slash_candidates = [
        index for index, bound in enumerate(bounds)
        if bound[3] - bound[1] >= height * 0.72
        and bound[2] - bound[0] >= width * 0.45
    ]
    if not slash_candidates:
        return None
    slash = max(
        slash_candidates,
        key=lambda index: (bounds[index][3] - bounds[index][1])
        * (bounds[index][2] - bounds[index][0]),
    )
    groups = {"slash": [slash], "numerator": [], "denominator": []}
    for index, bound in enumerate(bounds):
        if index == slash:
            continue
        contour_x = (bound[0] + bound[2]) * 0.5
        contour_y = (bound[1] + bound[3]) * 0.5
        if contour_x < center_x and contour_y > center_y:
            groups["numerator"].append(index)
        else:
            groups["denominator"].append(index)
    if not groups["numerator"] or not groups["denominator"]:
        return None
    return [groups["slash"], groups["numerator"], groups["denominator"]]


def transform_contour_group(layer, indices, scale_x, scale_y, dx, dy, cx, cy):
    output = layer.dup()
    for index in indices:
        for point in output[index]:
            point.x = cx + scale_x * (point.x - cx) + dx
            point.y = cy + scale_y * (point.y - cy) + dy
    return output


def register_fraction_components(
    source_glyph, layer, cleaned_layer, original_sets, original_mask,
    original_masks, bbox, reference_topology, reference_topologies,
    work_dir, stem,
):
    """Correct independent shrinkage of fraction numerator, slash and denominator."""
    groups = fraction_contour_groups(cleaned_layer)
    if groups is None or len(layer) != len(cleaned_layer):
        return None, None, 0, ""
    initial = layer.dup()
    initial_metrics = evaluate_candidate(
        initial, original_sets, original_mask, original_masks, bbox,
        reference_topology, reference_topologies,
    )
    current = initial
    current_metrics = initial_metrics
    evaluated = 0
    applied = []
    # A second pass lets one component react to raster changes introduced by
    # another while keeping the search linear rather than combinatorial.
    for _pass in range(2):
        changed = False
        for group in groups:
            source_bbox = contour_group_bbox(cleaned_layer, group)
            candidate_bbox = contour_group_bbox(current, group)
            candidate_width = max(EPSILON, candidate_bbox[2] - candidate_bbox[0])
            candidate_height = max(EPSILON, candidate_bbox[3] - candidate_bbox[1])
            source_width = max(EPSILON, source_bbox[2] - source_bbox[0])
            source_height = max(EPSILON, source_bbox[3] - source_bbox[1])
            target_scale_x = source_width / candidate_width
            target_scale_y = source_height / candidate_height
            candidate_cx = (candidate_bbox[0] + candidate_bbox[2]) * 0.5
            candidate_cy = (candidate_bbox[1] + candidate_bbox[3]) * 0.5
            source_cx = (source_bbox[0] + source_bbox[2]) * 0.5
            source_cy = (source_bbox[1] + source_bbox[3]) * 0.5
            coarse = []
            for amount in (0.5, 0.75, 1.0):
                sx = 1.0 + (target_scale_x - 1.0) * amount
                sy = 1.0 + (target_scale_y - 1.0) * amount
                dx = (source_cx - candidate_cx) * amount
                dy = (source_cy - candidate_cy) * amount
                for trial_sx, trial_sy in ((sx, 1.0), (1.0, sy), (sx, sy)):
                    trial = transform_contour_group(
                        current, group, trial_sx, trial_sy, dx, dy,
                        candidate_cx, candidate_cy,
                    )
                    if layer_has_proper_intersections(trial):
                        continue
                    coarse.append((
                        coarse_raster_quality_key(
                            trial, original_masks[64], bbox, 64
                        ),
                        amount, trial_sx, trial_sy, dx, dy, trial,
                    ))
            best = None
            for _key, amount, sx, sy, dx, dy, trial in sorted(coarse)[:3]:
                metrics = evaluate_candidate(
                    trial, original_sets, original_mask, original_masks, bbox,
                    reference_topology, None, False,
                )
                evaluated += 1
                key = raster_quality_key(metrics)
                if best is None or key < best[0]:
                    best = (key, trial, metrics, amount, sx, sy, dx, dy)
            if best is not None and best[0] < raster_quality_key(current_metrics):
                _key, current, current_metrics, amount, sx, sy, dx, dy = best
                applied.append(
                    "{}:{:.2f},{:.4f},{:.4f},{:.1f},{:.1f}".format(
                        "+".join(str(index) for index in group),
                        amount, sx, sy, dx, dy,
                    )
                )
                changed = True
        if not changed:
            break
    if raster_quality_key(current_metrics) >= raster_quality_key(initial_metrics):
        return None, initial_metrics, evaluated, ""
    actual = roundtrip_candidate(source_glyph, current, work_dir, stem)
    if (
        sum(len(contour) for contour in actual) > MAX_POINTS
        or layer_has_proper_intersections(actual)
    ):
        return None, initial_metrics, evaluated, ""
    actual_metrics = evaluate_candidate(
        actual, original_sets, original_mask, original_masks, bbox,
        reference_topology, reference_topologies,
    )
    if (
        actual_metrics["component_delta"] == 0
        and actual_metrics["counter_delta"] == 0
        and raster_quality_key(actual_metrics) < raster_quality_key(initial_metrics)
    ):
        return actual, actual_metrics, evaluated, ";".join(applied)
    return None, initial_metrics, evaluated, ""


def rebuild_fraction_weak_components(
    source_glyph, layer, cleaned_layer, original_sets, original_mask,
    original_masks, bbox, reference_topology, reference_topologies,
    work_dir, stem,
):
    """Trade excess slash points for detail on one or both fraction digits."""
    groups = fraction_contour_groups(cleaned_layer)
    if groups is None or len(layer) != len(cleaned_layer):
        return None, None, 0, ""
    slash = groups[0][0]
    cleaned_sets = layer_point_sets(cleaned_layer)
    current_points = sum(len(contour) for contour in layer)
    initial_metrics = evaluate_candidate(
        layer, original_sets, original_mask, original_masks, bbox,
        reference_topology, reference_topologies,
    )
    # Counters are already compact and topology-sensitive. Rebuild only the
    # largest outer contour in each digit group, preserving its small counters.
    numerator = max(groups[1], key=lambda index: abs(signed_area(cleaned_sets[index])))
    denominator = max(groups[2], key=lambda index: abs(signed_area(cleaned_sets[index])))
    outer_indices = (numerator, denominator)
    removal_orders = {
        index: safe_removal_order(
            cleaned_sets[index],
            [other for other_index, other in enumerate(cleaned_sets) if other_index != index],
        )
        for index in outer_indices
    }
    variants = []
    signatures = set()
    for slash_budget in (6, 8, 10, 12):
        slash_built = build_line_first_layer(
            [cleaned_sets[slash]], [slash_budget]
        )
        if slash_built is None:
            continue
        slash_contour = slash_built[0][0]
        base_points = current_points - len(layer[slash]) + len(slash_contour)
        available = MAX_POINTS - base_points
        if available <= 0:
            continue
        allocations = (
            ((numerator, available),),
            ((denominator, available),),
            (
                (numerator, available // 2),
                (denominator, available - available // 2),
            ),
        )
        for allocation in allocations:
            trial = layer.dup()
            trial[slash] = slash_contour
            label = ["slash{}".format(slash_budget)]
            valid = True
            for index, extra in allocation:
                contour_budget = min(
                    len(cleaned_sets[index]), len(layer[index]) + extra
                )
                built = build_budget_layer(
                    [cleaned_sets[index]], [removal_orders[index]],
                    contour_budget, True, "fraction-hybrid",
                )
                if built is None:
                    valid = False
                    break
                trial[index] = built[0][0]
                label.append("{}={}".format(index, len(trial[index])))
            if not valid:
                continue
            points = sum(len(contour) for contour in trial)
            if points > MAX_POINTS or layer_has_proper_intersections(trial):
                continue
            signature = tuple(
                (round(point.x, 3), round(point.y, 3), bool(point.on_curve))
                for contour in trial for point in contour
            )
            if signature in signatures:
                continue
            signatures.add(signature)
            metrics = evaluate_candidate(
                trial, original_sets, original_mask, original_masks, bbox,
                reference_topology, None, False,
            )
            variants.append((
                raster_quality_key(metrics), trial, "-".join(label)
            ))
    evaluated = 0
    generated = []
    for rank, (_key, trial, label) in enumerate(sorted(variants)[:6]):
        actual = roundtrip_candidate(
            source_glyph, trial, work_dir,
            stem + "-{}".format(rank),
        )
        if (
            sum(len(contour) for contour in actual) > MAX_POINTS
            or layer_has_proper_intersections(actual)
        ):
            continue
        metrics = evaluate_candidate(
            actual, original_sets, original_mask, original_masks, bbox,
            reference_topology, reference_topologies,
        )
        evaluated += 1
        if metrics["component_delta"] or metrics["counter_delta"]:
            continue
        generated.append((raster_quality_key(metrics), actual, metrics, label))
    if not generated:
        return None, initial_metrics, evaluated, ""
    _key, actual, metrics, label = min(generated, key=lambda item: item[0])
    if raster_quality_key(metrics) < raster_quality_key(initial_metrics):
        return actual, metrics, evaluated, label
    return None, initial_metrics, evaluated, ""


def register_independent_contour_areas(
    source_glyph, layer, cleaned_layer, original_sets, original_mask,
    original_masks, bbox, reference_topology, reference_topologies,
    work_dir, stem,
):
    """Restore ink area on spatially independent contours such as accents."""
    if len(layer) != len(cleaned_layer) or len(layer) < 1:
        return None, None, 0, ""
    initial = layer.dup()
    initial_metrics = evaluate_candidate(
        initial, original_sets, original_mask, original_masks, bbox,
        reference_topology, reference_topologies,
    )
    current = initial
    current_metrics = initial_metrics
    evaluated = 0
    applied = []
    ratios = []
    for index in range(len(layer)):
        cleaned_area = abs(signed_area([
            (point.x, point.y) for point in cleaned_layer[index]
        ]))
        candidate_area = abs(signed_area([
            (point.x, point.y) for point in layer[index]
        ]))
        ratios.append((
            math.sqrt(cleaned_area / max(EPSILON, candidate_area)), index
        ))
    for target_scale, index in sorted(ratios, reverse=True):
        if target_scale <= 1.001:
            continue
        contour_bbox = contour_group_bbox(current, [index])
        center_x = (contour_bbox[0] + contour_bbox[2]) * 0.5
        center_y = (contour_bbox[1] + contour_bbox[3]) * 0.5
        choices = []
        for amount in (0.25, 0.35, 0.5, 0.65, 0.8, 1.0):
            scale = 1.0 + (target_scale - 1.0) * amount
            trial = transform_contour_group(
                current, [index], scale, scale, 0.0, 0.0,
                center_x, center_y,
            )
            if layer_has_proper_intersections(trial):
                continue
            metrics = evaluate_candidate(
                trial, original_sets, original_mask, original_masks, bbox,
                reference_topology, None, False,
            )
            evaluated += 1
            choices.append((raster_quality_key(metrics), scale, trial, metrics))
        if not choices:
            continue
        key, scale, trial, metrics = min(choices, key=lambda item: item[0])
        if key < raster_quality_key(current_metrics):
            current = trial
            current_metrics = metrics
            applied.append("{}={:.6f}".format(index, scale))
    if raster_quality_key(current_metrics) >= raster_quality_key(initial_metrics):
        return None, initial_metrics, evaluated, ""
    actual = roundtrip_candidate(source_glyph, current, work_dir, stem)
    if (
        sum(len(contour) for contour in actual) > MAX_POINTS
        or layer_has_proper_intersections(actual)
    ):
        return None, initial_metrics, evaluated, ""
    actual_metrics = evaluate_candidate(
        actual, original_sets, original_mask, original_masks, bbox,
        reference_topology, reference_topologies,
    )
    if (
        actual_metrics["component_delta"] == 0
        and actual_metrics["counter_delta"] == 0
        and raster_quality_key(actual_metrics) < raster_quality_key(initial_metrics)
    ):
        return actual, actual_metrics, evaluated, ";".join(applied)
    return None, initial_metrics, evaluated, ""


def register_dominant_contour_bounds(
    source_glyph, layer, cleaned_layer, original_sets, original_mask,
    original_masks, bbox, reference_topology, reference_topologies,
    work_dir, stem,
):
    """Align the largest compact contour to its cleaned baseline bounds."""
    if len(layer) != len(cleaned_layer) or not len(layer):
        return None, None, 0, ""
    cleaned_sets = layer_point_sets(cleaned_layer)
    index = max(
        range(len(cleaned_sets)),
        key=lambda value: abs(signed_area(cleaned_sets[value])),
    )
    source_bbox = contour_group_bbox(cleaned_layer, [index])
    candidate_bbox = contour_group_bbox(layer, [index])
    candidate_width = max(EPSILON, candidate_bbox[2] - candidate_bbox[0])
    candidate_height = max(EPSILON, candidate_bbox[3] - candidate_bbox[1])
    target_scale_x = (source_bbox[2] - source_bbox[0]) / candidate_width
    target_scale_y = (source_bbox[3] - source_bbox[1]) / candidate_height
    candidate_cx = (candidate_bbox[0] + candidate_bbox[2]) * 0.5
    candidate_cy = (candidate_bbox[1] + candidate_bbox[3]) * 0.5
    source_cx = (source_bbox[0] + source_bbox[2]) * 0.5
    source_cy = (source_bbox[1] + source_bbox[3]) * 0.5
    initial_metrics = evaluate_candidate(
        layer, original_sets, original_mask, original_masks, bbox,
        reference_topology, reference_topologies,
    )
    variants = []
    for amount in (0.25, 0.5, 0.75, 1.0):
        scale_x = 1.0 + (target_scale_x - 1.0) * amount
        scale_y = 1.0 + (target_scale_y - 1.0) * amount
        dx = (source_cx - candidate_cx) * amount
        dy = (source_cy - candidate_cy) * amount
        for sx, sy in ((scale_x, 1.0), (1.0, scale_y), (scale_x, scale_y)):
            trial = transform_contour_group(
                layer, [index], sx, sy, dx, dy, candidate_cx, candidate_cy
            )
            variants.append((
                coarse_raster_quality_key(
                    trial, original_masks[64], bbox, 64
                ),
                amount, sx, sy, dx, dy, trial,
            ))
    evaluated = 0
    finalists = []
    for _key, amount, sx, sy, dx, dy, trial in sorted(variants)[:6]:
        if layer_has_proper_intersections(trial):
            continue
        actual = roundtrip_candidate(
            source_glyph, trial, work_dir,
            stem + "-{:.2f}-{:.4f}-{:.4f}".format(amount, sx, sy),
        )
        if sum(len(contour) for contour in actual) > MAX_POINTS:
            continue
        metrics = evaluate_candidate(
            actual, original_sets, original_mask, original_masks, bbox,
            reference_topology, reference_topologies,
        )
        evaluated += 1
        if metrics["component_delta"] or metrics["counter_delta"]:
            continue
        finalists.append((
            raster_quality_key(metrics), actual, metrics,
            "{}:{:.2f},{:.4f},{:.4f},{:.1f},{:.1f}".format(
                index, amount, sx, sy, dx, dy
            ),
        ))
    if not finalists:
        return None, initial_metrics, evaluated, ""
    _key, actual, metrics, transform = min(finalists, key=lambda item: item[0])
    if raster_quality_key(metrics) < raster_quality_key(initial_metrics):
        return actual, metrics, evaluated, transform
    return None, initial_metrics, evaluated, ""


def cardinal_refine_checkpoint_candidate(
    source_glyph, layer, original_sets, original_mask, original_masks, bbox,
    reference_topology, reference_topologies, work_dir, stem,
):
    """Hill-climb tiny generated-outline moves near concentrated missing ink."""
    current_layer = layer.dup()
    current_metrics = evaluate_candidate(
        current_layer, original_sets, original_mask, original_masks, bbox,
        reference_topology, reference_topologies,
    )
    left, bottom, right, top = bbox
    moves = (
        (1, 0), (-1, 0), (0, 1), (0, -1),
        (2, 0), (-2, 0), (0, 2), (0, -2),
        (1, 1), (1, -1), (-1, 1), (-1, -1),
    )
    evaluated = 0
    for iteration in range(3):
        candidate_mask = current_metrics["candidate_mask"]
        point_records = [
            (contour_index, point_index, point.x, point.y)
            for contour_index, contour in enumerate(current_layer)
            for point_index, point in enumerate(contour)
        ]
        differences = sorted(
            range(len(original_mask)),
            key=lambda index: original_mask[index] - candidate_mask[index],
            reverse=True,
        )
        selected = []
        for index in differences:
            if original_mask[index] - candidate_mask[index] <= EPSILON:
                break
            row, column = divmod(index, RASTER_SIZE)
            target = (
                left + (column + 0.5) / RASTER_SIZE * (right - left),
                top - (row + 0.5) / RASTER_SIZE * (top - bottom),
            )
            for contour_index, point_index, _x, _y in sorted(
                point_records,
                key=lambda record: (
                    record[2] - target[0]
                ) ** 2 + (record[3] - target[1]) ** 2,
            )[:3]:
                key = (contour_index, point_index)
                if key not in selected:
                    selected.append(key)
                if len(selected) >= 2:
                    break
            if len(selected) >= 2:
                break
        best = None
        for contour_index, point_index in selected:
            coarse_trials = []
            for dx, dy in moves:
                trial = current_layer.dup()
                trial[contour_index][point_index].x += dx
                trial[contour_index][point_index].y += dy
                if layer_has_proper_intersections(trial):
                    continue
                coarse_trials.append((
                    coarse_raster_quality_key(
                        trial, original_masks[128], bbox
                    ),
                    contour_index, point_index, dx, dy, trial,
                ))
            for _coarse_key, _ci, _pi, dx, dy, trial in sorted(
                coarse_trials, key=lambda value: value[0]
            )[:6]:
                actual = roundtrip_candidate(
                    source_glyph, trial, work_dir,
                    stem + "-cardinal-{}-{}-{}-{}-{}".format(
                        iteration, contour_index, point_index, dx, dy
                    ),
                )
                actual_metrics = evaluate_candidate(
                    actual, original_sets, original_mask, original_masks, bbox,
                    reference_topology, None, False,
                )
                evaluated += 1
                if not (
                    sum(len(contour) for contour in actual) <= MAX_POINTS
                    and not layer_has_proper_intersections(actual)
                ):
                    continue
                key = raster_quality_key(actual_metrics)
                if best is None or key < best[0]:
                    best = (key, actual, actual_metrics)
                if not actual_metrics["passes"]:
                    continue
                full_metrics = evaluate_candidate(
                    actual, original_sets, original_mask, original_masks, bbox,
                    reference_topology, reference_topologies,
                )
                if full_metrics["passes"]:
                    return actual, full_metrics, evaluated
        if best is None or best[0] >= raster_quality_key(current_metrics):
            break
        _best_key, current_layer, current_metrics = best
    full_metrics = evaluate_candidate(
        current_layer, original_sets, original_mask, original_masks, bbox,
        reference_topology, reference_topologies,
    )
    return None, full_metrics, evaluated


def efficient_raster_refine_candidate(
    source_glyph, layer, original_sets, original_mask, original_masks, bbox,
    reference_topology, reference_topologies, work_dir, stem,
):
    """Improve local curve placement in memory, then round-trip only once."""
    initial_layer = layer.dup()
    initial_metrics = evaluate_candidate(
        initial_layer, original_sets, original_mask, original_masks, bbox,
        reference_topology, reference_topologies,
    )
    current_layer = initial_layer
    current_metrics = initial_metrics
    left, bottom, right, top = bbox
    evaluated = 0
    near_gate_score = max(
        initial_metrics["mse"] / MAX_MSE,
        MIN_INK_IOU / max(EPSILON, initial_metrics["ink_iou"]),
        initial_metrics["false_positive_ink"] / MAX_FALSE_POSITIVE_INK,
        initial_metrics["false_negative_ink"] / MAX_FALSE_NEGATIVE_INK,
    )
    near_mse_gate = (
        initial_metrics["component_delta"] == 0
        and initial_metrics["counter_delta"] == 0
        and near_gate_score <= 1.08
    )
    step_schedule = (2, 1) if near_mse_gate else (6, 4, 2, 1)
    selected_limit = 10 if near_mse_gate else 6
    target_limit = 14 if near_mse_gate else 10
    for iteration, step in enumerate(step_schedule):
        candidate_mask = current_metrics["candidate_mask"]
        differences = sorted(
            range(len(original_mask)),
            key=lambda index: abs(original_mask[index] - candidate_mask[index]),
            reverse=True,
        )
        point_records = [
            (contour_index, point_index, point.x, point.y)
            for contour_index, contour in enumerate(current_layer)
            for point_index, point in enumerate(contour)
        ]
        selected = []
        targets = []
        for index in differences:
            if abs(original_mask[index] - candidate_mask[index]) <= EPSILON:
                break
            row, column = divmod(index, RASTER_SIZE)
            target = (
                left + (column + 0.5) / RASTER_SIZE * (right - left),
                top - (row + 0.5) / RASTER_SIZE * (top - bottom),
            )
            if any(distance(target, existing) < 5.0 for existing in targets):
                continue
            targets.append(target)
            for contour_index, point_index, _x, _y in sorted(
                point_records,
                key=lambda record: (
                    (record[2] - target[0]) ** 2
                    + (record[3] - target[1]) ** 2
                ),
            )[:3]:
                key = (contour_index, point_index)
                if key not in selected:
                    selected.append(key)
                if len(selected) >= selected_limit:
                    break
            if len(selected) >= selected_limit or len(targets) >= target_limit:
                break
        moves = (
            (step, 0), (-step, 0), (0, step), (0, -step),
            (step, step), (step, -step), (-step, step), (-step, -step),
        )
        coarse = []
        for contour_index, point_index in selected:
            for dx, dy in moves:
                trial = current_layer.dup()
                point = trial[contour_index][point_index]
                point.x += dx
                point.y += dy
                coarse.append((
                    coarse_raster_quality_key(
                        trial, original_masks[64], bbox, 64
                    ),
                    trial,
                ))
                evaluated += 1
        if not coarse:
            break
        best = None
        full_shortlist = 8 if near_mse_gate else 4
        for _coarse_key, trial in sorted(
            coarse, key=lambda item: item[0]
        )[:full_shortlist]:
            if layer_has_proper_intersections(trial):
                continue
            metrics = evaluate_candidate(
                trial, original_sets, original_mask, original_masks, bbox,
                reference_topology, None, False,
            )
            key = raster_quality_key(metrics)
            if best is None or key < best[0]:
                best = (key, trial, metrics)
        if best is None or best[0] >= raster_quality_key(current_metrics):
            break
        _best_key, current_layer, current_metrics = best
    if raster_quality_key(current_metrics) >= raster_quality_key(initial_metrics):
        return None, initial_metrics, evaluated
    actual = roundtrip_candidate(source_glyph, current_layer, work_dir, stem)
    if (
        sum(len(contour) for contour in actual) > MAX_POINTS
        or layer_has_proper_intersections(actual)
    ):
        return None, initial_metrics, evaluated
    actual_metrics = evaluate_candidate(
        actual, original_sets, original_mask, original_masks, bbox,
        reference_topology, reference_topologies,
    )
    if (
        actual_metrics["component_delta"] == 0
        and actual_metrics["counter_delta"] == 0
        and raster_quality_key(actual_metrics)
        < raster_quality_key(initial_metrics)
    ):
        return actual, actual_metrics, evaluated
    return None, initial_metrics, evaluated


def pair_raster_refine_candidate(
    source_glyph, layer, original_sets, original_mask, original_masks, bbox,
    reference_topology, reference_topologies, work_dir, stem,
):
    """Cross integer-raster plateaus with two coordinated one-UPM moves."""
    initial = layer.dup()
    initial_metrics = evaluate_candidate(
        initial, original_sets, original_mask, original_masks, bbox,
        reference_topology, reference_topologies,
    )
    candidate_mask = initial_metrics["candidate_mask"]
    left, bottom, right, top = bbox
    differences = sorted(
        range(len(original_mask)),
        key=lambda index: abs(original_mask[index] - candidate_mask[index]),
        reverse=True,
    )
    point_records = [
        (contour_index, point_index, point.x, point.y)
        for contour_index, contour in enumerate(initial)
        for point_index, point in enumerate(contour)
    ]
    selected = []
    targets = []
    for index in differences:
        if abs(original_mask[index] - candidate_mask[index]) <= EPSILON:
            break
        row, column = divmod(index, RASTER_SIZE)
        target = (
            left + (column + 0.5) / RASTER_SIZE * (right - left),
            top - (row + 0.5) / RASTER_SIZE * (top - bottom),
        )
        if any(distance(target, existing) < 5.0 for existing in targets):
            continue
        targets.append(target)
        nearest = min(
            point_records,
            key=lambda record: (record[2] - target[0]) ** 2
            + (record[3] - target[1]) ** 2,
        )
        key = nearest[:2]
        if key not in selected:
            selected.append(key)
        if len(selected) >= 8 or len(targets) >= 12:
            break
    moves = (
        (1, 0), (-1, 0), (0, 1), (0, -1),
        (1, 1), (1, -1), (-1, 1), (-1, -1),
    )
    singles = []
    for contour_index, point_index in selected:
        for dx, dy in moves:
            trial = initial.dup()
            point = trial[contour_index][point_index]
            point.x += dx
            point.y += dy
            singles.append((
                coarse_raster_quality_key(
                    trial, original_masks[64], bbox, 64
                ),
                contour_index, point_index, dx, dy,
            ))
    singles = sorted(singles, key=lambda item: item[0])[:16]
    pairs = []
    for left_index, first in enumerate(singles):
        for second in singles[left_index + 1:]:
            first_key = (first[1], first[2])
            second_key = (second[1], second[2])
            if first_key == second_key:
                continue
            trial = initial.dup()
            first_point = trial[first[1]][first[2]]
            second_point = trial[second[1]][second[2]]
            first_point.x += first[3]
            first_point.y += first[4]
            second_point.x += second[3]
            second_point.y += second[4]
            pairs.append((
                coarse_raster_quality_key(
                    trial, original_masks[64], bbox, 64
                ),
                trial,
            ))
    triples = []
    triple_singles = singles[:12]
    for first_index, first in enumerate(triple_singles):
        for second_index in range(first_index + 1, len(triple_singles)):
            second = triple_singles[second_index]
            for third in triple_singles[second_index + 1:]:
                point_keys = {
                    (first[1], first[2]),
                    (second[1], second[2]),
                    (third[1], third[2]),
                }
                if len(point_keys) != 3:
                    continue
                trial = initial.dup()
                for move in (first, second, third):
                    point = trial[move[1]][move[2]]
                    point.x += move[3]
                    point.y += move[4]
                triples.append((
                    coarse_raster_quality_key(
                        trial, original_masks[64], bbox, 64
                    ),
                    trial,
                ))
    larger_compounds = []
    for move_count in (4, 5):
        for combination in itertools.combinations(singles[:10], move_count):
            point_keys = {(move[1], move[2]) for move in combination}
            if len(point_keys) != move_count:
                continue
            trial = initial.dup()
            for move in combination:
                point = trial[move[1]][move[2]]
                point.x += move[3]
                point.y += move[4]
            larger_compounds.append((
                coarse_raster_quality_key(
                    trial, original_masks[64], bbox, 64
                ),
                trial,
            ))
    compounds = pairs + triples + larger_compounds
    evaluated = len(singles) + len(compounds)
    finalists = []
    for _coarse_key, trial in sorted(compounds, key=lambda item: item[0])[:12]:
        if layer_has_proper_intersections(trial):
            continue
        metrics = evaluate_candidate(
            trial, original_sets, original_mask, original_masks, bbox,
            reference_topology, None, False,
        )
        finalists.append((raster_quality_key(metrics), trial, metrics))
    if not finalists:
        return None, initial_metrics, evaluated
    _key, trial, _metrics = min(finalists, key=lambda item: item[0])
    actual = roundtrip_candidate(source_glyph, trial, work_dir, stem)
    if (
        sum(len(contour) for contour in actual) > MAX_POINTS
        or layer_has_proper_intersections(actual)
    ):
        return None, initial_metrics, evaluated
    actual_metrics = evaluate_candidate(
        actual, original_sets, original_mask, original_masks, bbox,
        reference_topology, reference_topologies,
    )
    if (
        actual_metrics["component_delta"] == 0
        and actual_metrics["counter_delta"] == 0
        and raster_quality_key(actual_metrics)
        < raster_quality_key(initial_metrics)
    ):
        return actual, actual_metrics, evaluated
    return None, initial_metrics, evaluated


def restore_checkpoint_vertex_positions(
    source_glyph, layer, cleaned_layer, original_sets, original_mask,
    original_masks, bbox, reference_topology, reference_topologies,
    work_dir, stem,
):
    """Pull fixed-budget vertices toward the cleaned outline near raster error."""
    initial = layer.dup()
    initial_metrics = evaluate_candidate(
        initial, original_sets, original_mask, original_masks, bbox,
        reference_topology, reference_topologies,
    )
    current = initial
    current_metrics = initial_metrics
    cleaned_records = [
        (point.x, point.y, point.on_curve)
        for contour in cleaned_layer for point in contour
    ]
    if not cleaned_records:
        return None, initial_metrics, 0
    left, bottom, right, top = bbox
    broad_search = source_glyph.glyphname in BROAD_RESTORATION_GLYPHS
    target_limit = 14 if broad_search else 8
    nearby_limit = 3 if broad_search else 2
    finalist_limit = 10 if broad_search else 5
    evaluated = 0
    for iteration, max_step in enumerate((8.0, 4.0, 2.0, 1.0)):
        differences = sorted(
            range(len(original_mask)),
            key=lambda index: abs(
                original_mask[index] - current_metrics["candidate_mask"][index]
            ),
            reverse=True,
        )
        targets = []
        for index in differences:
            if abs(
                original_mask[index] - current_metrics["candidate_mask"][index]
            ) <= EPSILON:
                break
            row, column = divmod(index, RASTER_SIZE)
            target = (
                left + (column + 0.5) / RASTER_SIZE * (right - left),
                top - (row + 0.5) / RASTER_SIZE * (top - bottom),
            )
            if all(distance(target, existing) >= 5.0 for existing in targets):
                targets.append(target)
            if len(targets) >= target_limit:
                break
        candidate_records = [
            (contour_index, point_index, point.x, point.y, point.on_curve)
            for contour_index, contour in enumerate(current)
            for point_index, point in enumerate(contour)
        ]
        coarse = []
        seen = set()
        for target in targets:
            nearest_candidates = sorted(
                candidate_records,
                key=lambda record: (record[2] - target[0]) ** 2
                + (record[3] - target[1]) ** 2,
            )[:nearby_limit]
            for contour_index, point_index, x, y, on_curve in nearest_candidates:
                restored = min(
                    (
                        record for record in cleaned_records
                        if record[2] == on_curve
                    ),
                    key=lambda record: (record[0] - target[0]) ** 2
                    + (record[1] - target[1]) ** 2,
                )
                dx, dy = restored[0] - x, restored[1] - y
                length = math.hypot(dx, dy)
                if length <= 0.5:
                    continue
                amount = min(max_step, length)
                move = (
                    int(round(dx / length * amount)),
                    int(round(dy / length * amount)),
                )
                key = (contour_index, point_index, move)
                if move == (0, 0) or key in seen:
                    continue
                seen.add(key)
                trial = current.dup()
                point = trial[contour_index][point_index]
                point.x += move[0]
                point.y += move[1]
                coarse.append((
                    coarse_raster_quality_key(
                        trial, original_masks[64], bbox, 64
                    ),
                    trial,
                ))
                evaluated += 1
        if not coarse:
            break
        best = None
        for _coarse_key, trial in sorted(coarse, key=lambda item: item[0])[:finalist_limit]:
            if layer_has_proper_intersections(trial):
                continue
            metrics = evaluate_candidate(
                trial, original_sets, original_mask, original_masks, bbox,
                reference_topology, None, False,
            )
            key = raster_quality_key(metrics)
            if best is None or key < best[0]:
                best = (key, trial, metrics)
        if best is None or best[0] >= raster_quality_key(current_metrics):
            continue
        _key, current, current_metrics = best
    if raster_quality_key(current_metrics) >= raster_quality_key(initial_metrics):
        return None, initial_metrics, evaluated
    actual = roundtrip_candidate(source_glyph, current, work_dir, stem)
    if (
        sum(len(contour) for contour in actual) > MAX_POINTS
        or layer_has_proper_intersections(actual)
    ):
        return None, initial_metrics, evaluated
    actual_metrics = evaluate_candidate(
        actual, original_sets, original_mask, original_masks, bbox,
        reference_topology, reference_topologies,
    )
    if (
        actual_metrics["component_delta"] == 0
        and actual_metrics["counter_delta"] == 0
        and raster_quality_key(actual_metrics)
        < raster_quality_key(initial_metrics)
    ):
        return actual, actual_metrics, evaluated
    return None, initial_metrics, evaluated


def relocate_checkpoint_vertices(
    source_glyph, layer, cleaned_layer, original_sets, original_mask,
    original_masks, bbox, reference_topology, reference_topologies,
    work_dir, stem,
):
    """Move point budget from redundant runs to concentrated raster error."""
    initial = layer.dup()
    initial_metrics = evaluate_candidate(
        initial, original_sets, original_mask, original_masks, bbox,
        reference_topology, reference_topologies,
    )
    current = initial
    current_metrics = initial_metrics
    cleaned_points = [
        (point.x, point.y)
        for contour in cleaned_layer for point in contour if point.on_curve
    ]
    if not cleaned_points:
        return None, initial_metrics, 0
    left, bottom, right, top = bbox
    evaluated = 0
    for _iteration in range(3):
        differences = sorted(
            range(len(original_mask)),
            key=lambda index: abs(
                original_mask[index] - current_metrics["candidate_mask"][index]
            ),
            reverse=True,
        )
        targets = []
        for index in differences:
            if abs(
                original_mask[index] - current_metrics["candidate_mask"][index]
            ) <= EPSILON:
                break
            row, column = divmod(index, RASTER_SIZE)
            target = (
                left + (column + 0.5) / RASTER_SIZE * (right - left),
                top - (row + 0.5) / RASTER_SIZE * (top - bottom),
            )
            if all(distance(target, existing) >= 5.0 for existing in targets):
                targets.append(target)
            if len(targets) >= 6:
                break
        donors = []
        segments = []
        for contour_index, contour in enumerate(current):
            points = list(contour)
            if len(points) <= 4 or not all(point.on_curve for point in points):
                continue
            for point_index, point in enumerate(points):
                previous = points[(point_index - 1) % len(points)]
                following = points[(point_index + 1) % len(points)]
                local_error = point_line_distance(
                    (point.x, point.y),
                    (previous.x, previous.y),
                    (following.x, following.y),
                )
                donors.append((local_error, contour_index, point_index))
                segments.append((
                    contour_index, point_index,
                    (point.x, point.y),
                    (following.x, following.y),
                ))
        donors = sorted(donors)[:10]
        coarse = []
        for target in targets:
            restored = min(cleaned_points, key=lambda point: distance(point, target))
            nearest_segments = sorted(
                segments,
                key=lambda item: point_line_distance(
                    restored, item[2], item[3]
                ),
            )[:2]
            for _error, donor_contour, donor_index in donors:
                for segment_contour, segment_index, _start, _end in nearest_segments:
                    if (
                        donor_contour == segment_contour
                        and donor_index in {
                            segment_index,
                            (segment_index + 1) % len(current[segment_contour]),
                        }
                    ):
                        continue
                    trial = current.dup()
                    trial[donor_contour].__delitem__(donor_index)
                    adjusted_segment = segment_index
                    if donor_contour == segment_contour and donor_index <= segment_index:
                        adjusted_segment -= 1
                    adjusted_segment %= len(trial[segment_contour])
                    trial[segment_contour].insertPoint(
                        fontforge.point(restored[0], restored[1], True),
                        adjusted_segment,
                    )
                    coarse.append((
                        coarse_raster_quality_key(
                            trial, original_masks[64], bbox, 64
                        ),
                        trial,
                    ))
                    evaluated += 1
        if not coarse:
            break
        best = None
        for _coarse_key, trial in sorted(coarse, key=lambda item: item[0])[:8]:
            if layer_has_proper_intersections(trial):
                continue
            metrics = evaluate_candidate(
                trial, original_sets, original_mask, original_masks, bbox,
                reference_topology, None, False,
            )
            key = raster_quality_key(metrics)
            if best is None or key < best[0]:
                best = (key, trial, metrics)
        if best is None or best[0] >= raster_quality_key(current_metrics):
            break
        _key, current, current_metrics = best
    if raster_quality_key(current_metrics) >= raster_quality_key(initial_metrics):
        return None, initial_metrics, evaluated
    actual = roundtrip_candidate(source_glyph, current, work_dir, stem)
    if (
        sum(len(contour) for contour in actual) > MAX_POINTS
        or layer_has_proper_intersections(actual)
    ):
        return None, initial_metrics, evaluated
    actual_metrics = evaluate_candidate(
        actual, original_sets, original_mask, original_masks, bbox,
        reference_topology, reference_topologies,
    )
    if (
        actual_metrics["component_delta"] == 0
        and actual_metrics["counter_delta"] == 0
        and raster_quality_key(actual_metrics)
        < raster_quality_key(initial_metrics)
    ):
        return actual, actual_metrics, evaluated
    return None, initial_metrics, evaluated


def split_and_refine_quadratic_candidate(
    source_glyph, layer, original_sets, original_mask, original_masks, bbox,
    reference_topology, reference_topologies, work_dir, stem,
):
    """Spend spare points on exact quadratic splits, then refine new controls."""
    spare = MAX_POINTS - sum(len(contour) for contour in layer)
    if spare <= 0:
        return None, None, 0
    initial_metrics = evaluate_candidate(
        layer, original_sets, original_mask, original_masks, bbox,
        reference_topology, reference_topologies,
    )
    candidate_mask = initial_metrics["candidate_mask"]
    left, bottom, right, top = bbox
    targets = []
    for index in sorted(
        range(len(original_mask)),
        key=lambda value: abs(
            original_mask[value] - candidate_mask[value]
        ),
        reverse=True,
    ):
        if abs(original_mask[index] - candidate_mask[index]) <= EPSILON:
            break
        row, column = divmod(index, RASTER_SIZE)
        target = (
            left + (column + 0.5) / RASTER_SIZE * (right - left),
            top - (row + 0.5) / RASTER_SIZE * (top - bottom),
        )
        if all(distance(target, existing) >= 5.0 for existing in targets):
            targets.append(target)
        if len(targets) >= 16:
            break
    segments = []
    for contour_index, contour in enumerate(layer):
        points = list(contour)
        for control_index, control in enumerate(points):
            previous = points[(control_index - 1) % len(points)]
            following = points[(control_index + 1) % len(points)]
            if control.on_curve or not previous.on_curve or not following.on_curve:
                continue
            q0 = midpoint((previous.x, previous.y), (control.x, control.y))
            q1 = midpoint((control.x, control.y), (following.x, following.y))
            curve_midpoint = midpoint(q0, q1)
            score = min(
                (distance(curve_midpoint, target) for target in targets),
                default=0.0,
            )
            segments.append((score, contour_index, control_index))
    if not segments:
        return None, initial_metrics, 0
    selected = sorted(segments)[:spare]
    split_layer = layer.dup()
    for contour_index in sorted({item[1] for item in selected}):
        indices = sorted(
            (item[2] for item in selected if item[1] == contour_index),
            reverse=True,
        )
        contour = split_layer[contour_index]
        for control_index in indices:
            control = contour[control_index]
            previous = contour[(control_index - 1) % len(contour)]
            following = contour[(control_index + 1) % len(contour)]
            q0 = midpoint((previous.x, previous.y), (control.x, control.y))
            q1 = midpoint((control.x, control.y), (following.x, following.y))
            control.x, control.y = q0
            contour.insertPoint(
                fontforge.point(q1[0], q1[1], False), control_index
            )
    if sum(len(contour) for contour in split_layer) > MAX_POINTS:
        return None, initial_metrics, len(selected)
    refined, metrics, evaluated = efficient_raster_refine_candidate(
        source_glyph, split_layer, original_sets, original_mask,
        original_masks, bbox, reference_topology, reference_topologies,
        work_dir, stem,
    )
    evaluated += len(selected)
    if (
        refined is not None
        and raster_quality_key(metrics) < raster_quality_key(initial_metrics)
    ):
        return refined, metrics, evaluated
    return None, initial_metrics, evaluated


def refine_checkpoint_candidate(
    source_glyph, layer, original_sets, original_mask, original_masks, bbox,
    reference_topology, reference_topologies, work_dir, stem,
):
    """Iteratively improve a near-pass checkpoint candidate under fixed topology."""
    current_layer = layer.dup()
    current_metrics = evaluate_candidate(
        current_layer, original_sets, original_mask, original_masks, bbox,
        reference_topology,
    )
    evaluated = 0
    for iteration in range(6):
        proposals = []
        step = 2.0 if iteration < 3 else 1.0
        for proposal in nudge_refinement_layers(
            current_layer, original_mask, current_metrics["candidate_mask"],
            bbox, limit=4, step=step,
        ):
            if layer_has_proper_intersections(proposal):
                continue
            metrics = evaluate_candidate(
                proposal, original_sets, original_mask, original_masks, bbox,
                reference_topology,
            )
            proposals.append((raster_quality_key(metrics), proposal, metrics))
            evaluated += 1
        if not proposals:
            break
        best_key, best_layer, best_metrics = min(proposals, key=lambda item: item[0])
        if best_key >= raster_quality_key(current_metrics):
            break
        current_layer, current_metrics = best_layer, best_metrics
        if current_metrics["passes"]:
            full_metrics = evaluate_candidate(
                current_layer, original_sets, original_mask, original_masks, bbox,
                reference_topology, reference_topologies,
            )
            if full_metrics["passes"]:
                actual = roundtrip_candidate(
                    source_glyph, current_layer, work_dir,
                    stem + "-refined-{}".format(iteration),
                )
                actual_metrics = evaluate_candidate(
                    actual, original_sets, original_mask, original_masks, bbox,
                    reference_topology, reference_topologies,
                )
                if (
                    sum(len(contour) for contour in actual) <= MAX_POINTS
                    and actual_metrics["passes"]
                    and not layer_has_proper_intersections(actual)
                ):
                    return actual, actual_metrics, evaluated
    return None, current_metrics, evaluated


def insert_checkpoint_vertices(
    source_glyph, layer, cleaned_layer, original_sets, original_mask,
    original_masks, bbox, reference_topology, reference_topologies,
    work_dir, stem,
):
    """Use spare point budget to restore cleaned vertices near missing ink."""
    current_layer = layer.dup()
    current_metrics = evaluate_candidate(
        current_layer, original_sets, original_mask, original_masks, bbox,
        reference_topology,
    )
    cleaned_points = [
        (point.x, point.y)
        for contour in cleaned_layer for point in contour if point.on_curve
    ]
    evaluated = 0
    changed = False
    while sum(len(contour) for contour in current_layer) < MAX_POINTS:
        differences = sorted(
            range(len(original_mask)),
            key=lambda index: original_mask[index] - current_metrics["candidate_mask"][index],
            reverse=True,
        )
        targets = []
        left, bottom, right, top = bbox
        for index in differences:
            delta = original_mask[index] - current_metrics["candidate_mask"][index]
            if delta <= EPSILON:
                break
            row, column = divmod(index, RASTER_SIZE)
            target = (
                left + (column + 0.5) / RASTER_SIZE * (right - left),
                top - (row + 0.5) / RASTER_SIZE * (top - bottom),
            )
            if all(distance(target, existing) > 4.0 for existing in targets):
                targets.append(target)
            if len(targets) >= 12:
                break
        coarse_proposals = []
        for target in targets:
            if not cleaned_points:
                break
            restored = min(cleaned_points, key=lambda point: distance(point, target))
            segment_choices = []
            for contour_index, contour in enumerate(current_layer):
                if not all(point.on_curve for point in contour):
                    continue
                points = [(point.x, point.y) for point in contour]
                for point_index, start in enumerate(points):
                    segment_choices.append((
                        point_line_distance(
                            restored, start, points[(point_index + 1) % len(points)]
                        ),
                        contour_index, point_index,
                    ))
            if not segment_choices:
                continue
            _error, contour_index, point_index = min(segment_choices)
            contour = current_layer[contour_index]
            start = contour[point_index]
            end = contour[(point_index + 1) % len(contour)]
            vx, vy = end.x - start.x, end.y - start.y
            denominator = max(EPSILON, vx * vx + vy * vy)
            ratio = max(0.0, min(
                1.0,
                ((restored[0] - start.x) * vx + (restored[1] - start.y) * vy)
                / denominator,
            ))
            projected = (start.x + ratio * vx, start.y + ratio * vy)
            for factor in (1.0, 0.75, 0.5, 0.25):
                inserted = (
                    projected[0] + (restored[0] - projected[0]) * factor,
                    projected[1] + (restored[1] - projected[1]) * factor,
                )
                trial = current_layer.dup()
                trial[contour_index].insertPoint(
                    fontforge.point(inserted[0], inserted[1], True), point_index
                )
                coarse_proposals.append((
                    coarse_raster_quality_key(
                        trial, original_masks[64], bbox, 64
                    ),
                    trial,
                ))
        proposals = []
        insertion_shortlist = 3 if len(current_layer) >= 5 else 8
        for _coarse_key, trial in sorted(
            coarse_proposals, key=lambda item: item[0]
        )[:insertion_shortlist]:
            if layer_has_proper_intersections(trial):
                continue
            metrics = evaluate_candidate(
                trial, original_sets, original_mask, original_masks, bbox,
                reference_topology, None, False,
            )
            evaluated += 1
            proposals.append((raster_quality_key(metrics), trial, metrics))
        if not proposals:
            break
        best_key, best_layer, best_metrics = min(proposals, key=lambda item: item[0])
        if best_key >= raster_quality_key(current_metrics):
            break
        current_layer, current_metrics = best_layer, best_metrics
        changed = True
        full_metrics = evaluate_candidate(
            current_layer, original_sets, original_mask, original_masks, bbox,
            reference_topology, reference_topologies,
        )
        if full_metrics["passes"]:
            actual = roundtrip_candidate(
                source_glyph, current_layer, work_dir,
                stem + "-inserted-{}".format(
                    sum(len(contour) for contour in current_layer)
                ),
            )
            actual_metrics = evaluate_candidate(
                actual, original_sets, original_mask, original_masks, bbox,
                reference_topology, reference_topologies,
            )
            if (
                sum(len(contour) for contour in actual) <= MAX_POINTS
                and actual_metrics["passes"]
                and not layer_has_proper_intersections(actual)
            ):
                return actual, actual_metrics, evaluated, True
    current_metrics = evaluate_candidate(
        current_layer, original_sets, original_mask, original_masks, bbox,
        reference_topology, reference_topologies,
    )
    return current_layer, current_metrics, evaluated, changed


def fit_quadratic(points, path):
    start = points[path[0]]
    end = points[path[-1]]
    distances = [0.0]
    for index in range(1, len(path)):
        distances.append(distances[-1] + distance(points[path[index - 1]], points[path[index]]))
    total = max(EPSILON, distances[-1])
    numerator_x = numerator_y = denominator = 0.0
    for index, point_index in enumerate(path[1:-1], start=1):
        t = distances[index] / total
        coefficient = 2.0 * (1.0 - t) * t
        base_x = (1.0 - t) ** 2 * start[0] + t ** 2 * end[0]
        base_y = (1.0 - t) ** 2 * start[1] + t ** 2 * end[1]
        numerator_x += coefficient * (points[point_index][0] - base_x)
        numerator_y += coefficient * (points[point_index][1] - base_y)
        denominator += coefficient * coefficient
    if denominator <= EPSILON:
        control = midpoint(start, end)
    else:
        control = (numerator_x / denominator, numerator_y / denominator)
    # Keep the fitted control in the local sample hull. This prevents a least-
    # squares outlier from looping across its own or a neighboring contour.
    samples = [points[index] for index in path]
    control = (
        max(min(point[0] for point in samples), min(max(point[0] for point in samples), control[0])),
        max(min(point[1] for point in samples), min(max(point[1] for point in samples), control[1])),
    )
    max_line_error = max(point_line_distance(points[index], start, end) for index in path)
    return control, max_line_error


def evaluate_candidate(
    layer, original_sets, cleaned_mask, original_masks, bbox, cleaned_topology,
    cleaned_topologies=None, include_outline_metrics=True,
):
    candidate_sets = layer_point_sets(layer)
    candidate_mask = rasterize(candidate_sets, bbox)
    candidate_topology = topology(candidate_mask)
    per_size = {}
    for size in RASTER_SIZES:
        candidate_size = downsample(candidate_mask, RASTER_SIZE, size)
        per_size[size] = raster_metrics(original_masks[size], candidate_size)
    worst_mse_size = max(RASTER_SIZES, key=lambda size: per_size[size]["mse"])
    worst_iou_size = min(RASTER_SIZES, key=lambda size: per_size[size]["ink_iou"])
    worst_fp_size = max(RASTER_SIZES, key=lambda size: per_size[size]["false_positive_ink"])
    worst_fn_size = max(RASTER_SIZES, key=lambda size: per_size[size]["false_negative_ink"])
    mse = per_size[worst_mse_size]["mse"]
    iou = per_size[worst_iou_size]["ink_iou"]
    fp = per_size[worst_fp_size]["false_positive_ink"]
    fn = per_size[worst_fn_size]["false_negative_ink"]
    component_delta = candidate_topology[0] - cleaned_topology[0]
    counter_delta = candidate_topology[1] - cleaned_topology[1]
    candidate_topologies = None
    if cleaned_topologies is not None:
        candidate_topologies = topology_at_sizes(candidate_sets, bbox)
        cleaned_persistent = persistent_topology(cleaned_topologies)
        candidate_persistent = persistent_topology(candidate_topologies)
        component_delta = candidate_persistent[0] - cleaned_persistent[0]
        counter_delta = candidate_persistent[1] - cleaned_persistent[1]
    passes = (
        mse <= MAX_MSE
        and iou >= MIN_INK_IOU
        and fp <= MAX_FALSE_POSITIVE_INK
        and fn <= MAX_FALSE_NEGATIVE_INK
        and component_delta == 0
        and counter_delta == 0
    )
    worst_size = max(
        RASTER_SIZES,
        key=lambda size: max(
            per_size[size]["mse"] / MAX_MSE,
            MIN_INK_IOU / max(EPSILON, per_size[size]["ink_iou"]),
            per_size[size]["false_positive_ink"] / MAX_FALSE_POSITIVE_INK,
            per_size[size]["false_negative_ink"] / MAX_FALSE_NEGATIVE_INK,
        ),
    )
    original_bbox = bbox_of_sets(original_sets)
    if include_outline_metrics:
        candidate_bbox = bbox_of_sets(candidate_sets)
        original_area = area_of_sets(original_sets)
        candidate_area = area_of_sets(candidate_sets)
        sample_error = sampled_delta(original_sets, candidate_sets, original_bbox)
        bounds_error = bbox_delta(original_bbox, candidate_bbox)
        area_error = abs(candidate_area - original_area) / max(1.0, original_area)
    else:
        sample_error = bounds_error = area_error = 0.0
    return {
        "passes": passes,
        "mse": mse,
        "ink_iou": iou,
        "false_positive_ink": fp,
        "false_negative_ink": fn,
        "component_delta": component_delta,
        "counter_delta": counter_delta,
        "topology_status": "match" if component_delta == 0 and counter_delta == 0 else "component-or-counter-change",
        "sample_delta": sample_error,
        "bbox_delta": bounds_error,
        "area_delta": area_error,
        "worst_raster_size": worst_size,
        "per_size": per_size,
        "candidate_sets": candidate_sets,
        "candidate_mask": candidate_mask,
        "candidate_topologies": candidate_topologies,
    }


def quality_key(candidate):
    metrics = candidate["metrics"]
    return (
        abs(metrics["component_delta"]) + abs(metrics["counter_delta"]),
        -metrics["ink_iou"],
        metrics["mse"],
        max(metrics["false_positive_ink"], metrics["false_negative_ink"]),
        metrics["sample_delta"],
        candidate["points"],
    )


def quality_action(metrics):
    if metrics["component_delta"]:
        return "reject-component-change"
    if metrics["counter_delta"]:
        return "reject-counter-change"
    if metrics["mse"] > MAX_MSE:
        return "reject-raster-mse"
    if metrics["ink_iou"] < MIN_INK_IOU:
        return "reject-ink-iou"
    if metrics["false_positive_ink"] > MAX_FALSE_POSITIVE_INK:
        return "reject-extra-ink"
    if metrics["false_negative_ink"] > MAX_FALSE_NEGATIVE_INK:
        return "reject-missing-ink"
    return "accept-rebuilt"


def hard_budget_metrics_pass(metrics):
    """Allow a topology-safe 80-point outline after the normal fit is exhausted.

    This is deliberately not a replacement for the standard gate.  It is used
    only for the few high-complexity glyphs where a bounded visual compromise
    is preferable to retaining a many-hundred-point production fallback.
    """
    return (
        metrics["component_delta"] == 0
        and metrics["counter_delta"] == 0
        and metrics["mse"] <= HARD_BUDGET_MAX_MSE
        and metrics["ink_iou"] >= HARD_BUDGET_MIN_INK_IOU
        and metrics["false_positive_ink"] <= HARD_BUDGET_MAX_FALSE_POSITIVE_INK
        and metrics["false_negative_ink"] <= HARD_BUDGET_MAX_FALSE_NEGATIVE_INK
    )


def base_result(glyph):
    codepoint = glyph.unicode if glyph.unicode >= 0 else ""
    try:
        char = chr(glyph.unicode) if glyph.unicode >= 0 else ""
    except ValueError:
        char = ""
    old_points = point_count(glyph)
    old_contours = contour_count(glyph)
    result = {field: "" for field in REPORT_FIELDS}
    result.update(
        {
            "glyph": glyph.glyphname,
            "codepoint": codepoint,
            "char": char,
            "old_points": old_points,
            "new_points": old_points,
            "final_points": old_points,
            "old_contours": old_contours,
            "new_contours": old_contours,
            "absolute_max_points": MAX_POINTS,
            "worker_status": "started",
            "needs_manual_review": "false",
            "candidate_count": 0,
        }
    )
    return result


def add_metrics(result, metrics):
    for name in (
        "mse", "ink_iou", "false_positive_ink", "false_negative_ink",
        "sample_delta", "bbox_delta", "area_delta",
    ):
        result[name] = "{:.6f}".format(metrics[name])
    result["component_delta"] = metrics["component_delta"]
    result["counter_delta"] = metrics["counter_delta"]
    result["topology_status"] = metrics["topology_status"]
    result["worst_raster_size"] = metrics["worst_raster_size"]
    result["raster_diff"] = result["mse"]
    for size in RASTER_SIZES:
        values = metrics["per_size"][size]
        for name in ("mse", "ink_iou", "false_positive_ink", "false_negative_ink"):
            result["{}_{}".format(name, size)] = "{:.6f}".format(values[name])


def save_worker_glyph(source_glyph, layer, path):
    output = fontforge.font()
    output.encoding = "UnicodeFull"
    glyph = output.createChar(source_glyph.unicode, source_glyph.glyphname)
    glyph.foreground = layer
    glyph.width = source_glyph.width
    output.save(str(path))
    output.close()


def roundtrip_candidate(source_glyph, layer, directory, stem):
    """Return the actual quadratic outline produced by FontForge TTF generation."""
    ttf_path = directory / (stem + "-roundtrip.ttf")
    output = fontforge.font()
    output.encoding = "UnicodeFull"
    glyph = output.createChar(source_glyph.unicode, source_glyph.glyphname)
    glyph.foreground = layer
    glyph.width = source_glyph.width
    output.generate(str(ttf_path))
    output.close()
    generated = fontforge.open(str(ttf_path))
    actual_layer = generated[source_glyph.glyphname].foreground.dup()
    generated.close()
    try:
        ttf_path.unlink()
    except OSError:
        pass
    return actual_layer


def transformed_layer(layer, a=1.0, d=1.0, dx=0.0, dy=0.0, cx=0.0, cy=0.0):
    """Return an axis-aligned scale/reflection/translation of a layer."""
    output = layer.dup()
    for contour in output:
        for point in contour:
            point.x = cx + a * (point.x - cx) + dx
            point.y = cy + d * (point.y - cy) + dy
    return output


def best_registered_cleanup(
    cleaned_layer, original_sets, original_masks, original_bbox,
    cleaned_topology, cleaned_topologies,
):
    """Find a small integer cleanup translation that passes the existing gates."""
    cleaned_mask = rasterize(layer_point_sets(cleaned_layer), original_bbox)
    original_coarse = downsample(original_masks[128], 128, 16)
    cleaned_bbox = bbox_of_sets(layer_point_sets(cleaned_layer))
    source_bbox = bbox_of_sets(original_sets)
    predicted_dx = int(round(
        (source_bbox[0] + source_bbox[2] - cleaned_bbox[0] - cleaned_bbox[2]) * 0.5
    ))
    predicted_dy = int(round(
        (source_bbox[1] + source_bbox[3] - cleaned_bbox[1] - cleaned_bbox[3]) * 0.5
    ))
    offsets = sorted(
        ((dx, dy) for dx in range(-6, 7) for dy in range(-6, 7) if dx or dy),
        key=lambda value: (abs(value[0]) + abs(value[1]), abs(value[1]), -value[0]),
    )
    coarse_candidates = []
    for dx, dy in offsets:
        layer = transformed_layer(cleaned_layer, dx=dx, dy=dy)
        coarse_mask = rasterize(layer_point_sets(layer), original_bbox, 16)
        coarse_metrics = raster_metrics(original_coarse, coarse_mask)
        if not (
            coarse_metrics["mse"] <= 0.08
            and coarse_metrics["ink_iou"] >= 0.70
            and coarse_metrics["false_positive_ink"] <= 0.25
            and coarse_metrics["false_negative_ink"] <= 0.25
        ):
            continue
        coarse_candidates.append((
            (dx - predicted_dx) ** 2 + (dy - predicted_dy) ** 2,
            -coarse_metrics["ink_iou"], coarse_metrics["mse"],
            max(coarse_metrics["false_positive_ink"], coarse_metrics["false_negative_ink"]),
            abs(dx) + abs(dy), dx, dy, layer,
        ))
    for _registration, _iou, _mse, _ink, _distance, dx, dy, layer in sorted(coarse_candidates)[:3]:
        metrics = evaluate_candidate(
            layer, original_sets, cleaned_mask, original_masks, original_bbox,
            cleaned_topology,
        )
        if not metrics["passes"]:
            continue
        candidate = {
            "layer": layer, "metrics": metrics,
            "points": sum(len(contour) for contour in layer), "dx": dx, "dy": dy,
        }
        metrics = evaluate_candidate(
            candidate["layer"], original_sets, cleaned_mask, original_masks,
            original_bbox, cleaned_topology, cleaned_topologies,
        )
        if metrics["passes"]:
            candidate["metrics"] = metrics
            return candidate
    return None


def registered_template_candidate(
    target_glyph, donor_layer, flip_x, flip_y, original_sets,
    original_masks, original_bbox, target_topology, target_topologies, work_dir,
):
    """Reflect a checkpoint donor and search a bounded scale/translation registration."""
    donor_sets = layer_point_sets(donor_layer)
    if not donor_sets:
        return None
    donor_bbox = bbox_of_sets(donor_sets)
    target_bbox = bbox_of_sets(original_sets)
    donor_width = max(EPSILON, donor_bbox[2] - donor_bbox[0])
    donor_height = max(EPSILON, donor_bbox[3] - donor_bbox[1])
    target_width = max(EPSILON, target_bbox[2] - target_bbox[0])
    target_height = max(EPSILON, target_bbox[3] - target_bbox[1])
    donor_cx = (donor_bbox[0] + donor_bbox[2]) * 0.5
    donor_cy = (donor_bbox[1] + donor_bbox[3]) * 0.5
    target_cx = (target_bbox[0] + target_bbox[2]) * 0.5
    target_cy = (target_bbox[1] + target_bbox[3]) * 0.5
    base_x = target_width / donor_width
    base_y = target_height / donor_height
    original_mask = rasterize(original_sets, original_bbox)
    offsets = ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))
    for scale in (1.0, 0.99, 1.01):
        a = (-1.0 if flip_x else 1.0) * base_x * scale
        d = (-1.0 if flip_y else 1.0) * base_y * scale
        aligned = transformed_layer(
            donor_layer, a=a, d=d,
            dx=target_cx - donor_cx, dy=target_cy - donor_cy,
            cx=donor_cx, cy=donor_cy,
        )
        for dx, dy in offsets:
            layer = transformed_layer(aligned, dx=dx, dy=dy)
            points = sum(len(contour) for contour in layer)
            if points > MAX_POINTS:
                continue
            try:
                if layer_has_proper_intersections(layer):
                    continue
            except Exception:
                continue
            metrics = evaluate_candidate(
                layer, original_sets, original_mask, original_masks,
                original_bbox, target_topology,
            )
            if not metrics["passes"]:
                continue
            actual = roundtrip_candidate(
                target_glyph, layer, work_dir,
                "template-{}-{}-{}".format(scale, dx, dy),
            )
            actual_metrics = evaluate_candidate(
                actual, original_sets, original_mask, original_masks, original_bbox,
                target_topology, target_topologies,
            )
            actual_points = sum(len(contour) for contour in actual)
            if actual_points <= MAX_POINTS and actual_metrics["passes"]:
                return {
                    "layer": actual, "metrics": actual_metrics, "points": actual_points,
                    "dx": dx, "dy": dy, "scale": scale,
                }
    return None


def layers_geometrically_converged(left_layer, right_layer):
    left_sets = layer_point_sets(left_layer)
    right_sets = layer_point_sets(right_layer)
    if not left_sets or not right_sets:
        return left_sets == right_sets
    union_bbox = bbox_of_sets(left_sets + right_sets)
    bbox = padded_bbox(union_bbox)
    left_mask = rasterize(left_sets, bbox)
    right_mask = rasterize(right_sets, bbox)
    for size in RASTER_SIZES:
        metrics = raster_metrics(
            downsample(left_mask, RASTER_SIZE, size),
            downsample(right_mask, RASTER_SIZE, size),
        )
        if not (
            metrics["mse"] <= 0.001
            and metrics["ink_iou"] >= 0.995
            and metrics["false_positive_ink"] <= 0.005
            and metrics["false_negative_ink"] <= 0.005
        ):
            return False
    if topology_at_sizes(left_sets, bbox) != topology_at_sizes(right_sets, bbox):
        return False
    if sampled_delta(left_sets, right_sets, union_bbox) > 0.0005:
        return False
    # PathOps is the authoritative Boolean validator here. FontForge can report
    # intersections after reopening a geometrically stable quadratic outline;
    # two equivalent consecutive Boolean passes prove that no meaningful ink
    # overlap remains. Compact candidates still use strict vector checks.
    return True


def fontforge_visual_cleanup_variants(
    source_glyph, original_layer, work_dir, python_executable,
):
    """Return stable FontForge and FontForge→PathOps cleanup alternatives."""
    work_dir.mkdir(parents=True, exist_ok=True)
    scratch = fontforge.font()
    scratch.encoding = "UnicodeFull"
    glyph = scratch.createChar(source_glyph.unicode, source_glyph.glyphname)
    glyph.foreground = original_layer
    glyph.width = source_glyph.width
    if glyph.references:
        glyph.unlinkRef()
    glyph.removeOverlap()
    glyph.correctDirection()
    first_layer = glyph.foreground.dup()
    glyph.removeOverlap()
    glyph.correctDirection()
    second_layer = glyph.foreground.dup()
    stable = layers_geometrically_converged(first_layer, second_layer)
    fontforge_ttf = work_dir / "fontforge-cleaned.ttf"
    scratch.generate(str(fontforge_ttf))
    scratch.close()
    reopened = fontforge.open(str(fontforge_ttf))
    actual_fontforge = reopened[source_glyph.glyphname].foreground.dup()
    reopened.close()
    variants = []
    if stable:
        fallback_safe = not layer_has_proper_intersections(actual_fontforge)
        variants.append(
            {
                "backend": (
                    "fontforge-visual-fallback" if fallback_safe
                    else "fontforge-geometric-intermediate"
                ),
                "layer": actual_fontforge,
                "stable": True,
                "fallback_safe": fallback_safe,
                "passes": 2,
            }
        )
    try:
        cleaned_ttf, details = pathops_cleaned_font(
            fontforge_ttf, source_glyph.glyphname,
            work_dir / "fontforge-pathops", python_executable,
        )
        pathops_font = fontforge.open(str(cleaned_ttf))
        pathops_layer = pathops_font[source_glyph.glyphname].foreground.dup()
        pathops_font.close()
        pathops_stable = bool(details.get("idempotent", False))
        if not pathops_stable and details.get("previous_output"):
            previous_font = fontforge.open(str(details["previous_output"]))
            current_font = fontforge.open(str(cleaned_ttf))
            pathops_stable = layers_geometrically_converged(
                previous_font[source_glyph.glyphname].foreground,
                current_font[source_glyph.glyphname].foreground,
            )
            previous_font.close()
            current_font.close()
        if pathops_stable:
            variants.append(
                {
                    "backend": "fontforge-pathops-visual-fallback",
                    "layer": pathops_layer,
                    "stable": True,
                    "fallback_safe": True,
                    "passes": int(details.get("passes", 0) or 0) + 2,
                }
            )
    except Exception:
        pass
    return variants


def point_in_polygon(point, polygon):
    inside = False
    x, y = point
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        if (start[1] > y) == (end[1] > y):
            continue
        crossing = start[0] + (end[0] - start[0]) * (y - start[1]) / (end[1] - start[1])
        if crossing > x:
            inside = not inside
    return inside


def contour_pair_interacts(left, right):
    left_bbox = bbox_of_sets([left])
    right_bbox = bbox_of_sets([right])
    if (
        left_bbox[2] < right_bbox[0] or right_bbox[2] < left_bbox[0]
        or left_bbox[3] < right_bbox[1] or right_bbox[3] < left_bbox[1]
    ):
        return False
    for left_index, left_start in enumerate(left):
        left_end = left[(left_index + 1) % len(left)]
        for right_index, right_start in enumerate(right):
            if segments_intersect(
                left_start, left_end, right_start, right[(right_index + 1) % len(right)]
            ):
                return True
    # Nested same-winding contours overlap in non-zero fill. Opposite-winding
    # nesting is an intentional counter and must remain separate.
    same_winding = signed_area(left) * signed_area(right) > 0
    return same_winding and (
        point_in_polygon(left[0], right) or point_in_polygon(right[0], left)
    )


def contour_intersection_groups(layer):
    raw_contours = [contour for contour in layer]
    flattened = []
    self_intersections = []
    for contour in raw_contours:
        single = copy_layer_contours([contour], layer.is_quadratic)
        sets = layer_point_sets(single)
        flattened.append(sets[0] if sets else [])
        try:
            self_intersections.append(bool(single.selfIntersects()))
        except Exception:
            self_intersections.append(False)
    parent = list(range(len(raw_contours)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    interacting = set(index for index, value in enumerate(self_intersections) if value)
    for left in range(len(raw_contours)):
        if len(flattened[left]) < 3:
            continue
        for right in range(left + 1, len(raw_contours)):
            if len(flattened[right]) < 3:
                continue
            if contour_pair_interacts(flattened[left], flattened[right]):
                union(left, right)
                interacting.update((left, right))
    groups = {}
    for index in sorted(interacting):
        groups.setdefault(find(index), []).append(index)
    return raw_contours, list(groups.values())


def selective_pathops_cleaned_layer(source_glyph, original_layer, work_dir, python_executable):
    raw_contours, groups = contour_intersection_groups(original_layer)
    if not groups:
        raise RuntimeError("no intersecting contour group could be isolated")
    work_dir.mkdir(parents=True, exist_ok=True)
    affected = set(index for group in groups for index in group)
    combined_contours = [
        contour for index, contour in enumerate(raw_contours) if index not in affected
    ]
    passes = 0
    geometric_used = False
    for group_number, group in enumerate(groups):
        group_layer = copy_layer_contours(
            [raw_contours[index] for index in group], original_layer.is_quadratic
        )
        group_ttf = work_dir / "group-{}.ttf".format(group_number)
        group_font = fontforge.font()
        group_font.encoding = "UnicodeFull"
        group_glyph = group_font.createChar(source_glyph.unicode, source_glyph.glyphname)
        group_glyph.foreground = group_layer
        group_glyph.width = source_glyph.width
        group_font.generate(str(group_ttf))
        group_font.close()
        cleaned_ttf, details = pathops_cleaned_font(
            group_ttf, source_glyph.glyphname,
            work_dir / "cleaned-{}".format(group_number), python_executable,
        )
        stable = bool(details.get("idempotent", False))
        if not stable and details.get("previous_output"):
            previous_font = fontforge.open(str(details["previous_output"]))
            current_font = fontforge.open(str(cleaned_ttf))
            stable = layers_geometrically_converged(
                previous_font[source_glyph.glyphname].foreground,
                current_font[source_glyph.glyphname].foreground,
            )
            previous_font.close()
            current_font.close()
            details["geometric_idempotent"] = stable
            geometric_used = geometric_used or stable
        if not stable:
            raise RuntimeError("selective PathOps group was not geometrically stable")
        passes = max(passes, int(details.get("passes", 0)))
        cleaned_font = fontforge.open(str(cleaned_ttf))
        cleaned_layer = cleaned_font[source_glyph.glyphname].foreground
        for contour in cleaned_layer:
            combined_contours.append(contour.dup() if hasattr(contour, "dup") else contour)
        cleaned_font.close()
    combined = copy_layer_contours(combined_contours, original_layer.is_quadratic)
    return combined, {
        "backend": "fonttools-skia-pathops-selective",
        "status": "ok", "idempotent": True, "passes": passes,
        "geometric_idempotent": geometric_used,
        "groups": len(groups), "preserved_contours": len(raw_contours) - len(affected),
    }


def pathops_cleaned_font(source, glyph_name, work_dir, python_executable):
    work_dir.mkdir(parents=True, exist_ok=True)
    output = work_dir / "pathops-cleaned.ttf"
    report = work_dir / "pathops-cleaned.json"
    command = [
        python_executable, str(DEFAULT_PATHOPS_HELPER), str(source), glyph_name,
        str(output), str(report),
    ]
    completed = subprocess.run(
        command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=max(5, DEFAULT_TIMEOUT - 2), check=False,
    )
    details = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {
        "status": "failed", "backend": "fonttools-skia-pathops",
        "error": completed.stdout[-2000:] or "missing PathOps report",
    }
    if completed.returncode or details.get("status") != "ok" or not output.exists():
        raise RuntimeError(details.get("error") or "PathOps cleanup failed")
    return output, details


def process_glyph(
    source, glyph_name, out_sfd, pathops_python, donor_source=None,
    candidate_source=None, checkpoint_status=None, fresh_fit=False,
):
    font = fontforge.open(str(source))
    glyph = font[glyph_name]
    result = base_result(glyph)
    old_points = int(result["old_points"])
    if old_points == 0:
        result.update(
            {
                "worker_status": "ok",
                "rebuild_status": "empty",
                "diff_status": "empty",
                "quality_action": "empty",
                "selection_reason": "empty",
                "overlap_status": "not-needed",
                "source_overlap": "false",
                "cleanup_status": "not-needed",
                "cleanup_valid": "true",
                "final_outline": "original",
                "final_overlap_status": "none-detected",
            }
        )
        font.close()
        return result

    original_layer = glyph.foreground.dup()
    original_sets = layer_point_sets(original_layer)
    original_bbox = padded_bbox(bbox_of_sets(original_sets))
    original_mask_128 = rasterize(original_sets, original_bbox)
    original_masks = {
        size: downsample(original_mask_128, RASTER_SIZE, size) for size in RASTER_SIZES
    }
    original_topologies = topology_at_sizes(original_sets, original_bbox)
    add_topology_diagnostics(result, "source", original_topologies)

    try:
        has_overlap = bool(original_layer.selfIntersects())
    except Exception:
        has_overlap = True
    result["source_overlap"] = "true" if has_overlap else "false"
    if old_points <= MAX_POINTS and not has_overlap:
        add_topology_diagnostics(result, "cleaned", original_topologies)
        result.update(
            {
                "worker_status": "ok",
                "rebuild_status": "unchanged-in-range",
                "diff_status": "unchanged-in-range",
                "quality_action": "unchanged",
                "selection_reason": "already-at-or-under-80",
                "overlap_status": "not-needed",
                "cleanup_status": "not-needed",
                "cleanup_valid": "true",
                "final_outline": "original",
                "final_overlap_status": "none-detected",
                "cleaned_points": old_points,
                "cleaned_contours": result["old_contours"],
            }
        )
        font.close()
        return result

    cleanup_details = {"idempotent": True}
    cleaned_fallback_safe = True
    cleanup_backend = "exact-bypass"
    if checkpoint_status == "cleaned-fallback-review" and donor_source:
        checkpoint_font = fontforge.open(str(donor_source))
        try:
            glyph.foreground = checkpoint_font[glyph_name].foreground.dup()
        finally:
            checkpoint_font.close()
        cleanup_backend = "checkpoint-cleaned-baseline"
        result["cleanup_status"] = "resumed-validated-baseline"
    elif not has_overlap:
        glyph.foreground = original_layer
        result["cleanup_status"] = "bypassed-no-overlap"
    else:
        cleanup_backend = "fonttools-skia-pathops-selective"
        try:
            selective_layer, cleanup_details = selective_pathops_cleaned_layer(
                glyph, original_layer,
                out_sfd.parent / (out_sfd.stem + "-pathops-selective"),
                pathops_python,
            )
            glyph.foreground = selective_layer
        except Exception as selective_error:
            cleanup_backend = "fonttools-skia-pathops"
            try:
                cleaned_ttf, cleanup_details = pathops_cleaned_font(
                    source, glyph_name, out_sfd.parent / (out_sfd.stem + "-pathops"), pathops_python
                )
                cleanup_details["selective_error"] = str(selective_error)
                cleaned_font = fontforge.open(str(cleaned_ttf))
                cleaned_source = cleaned_font[glyph_name]
                glyph.foreground = cleaned_source.foreground
                cleaned_font.close()
                if (
                    not cleanup_details.get("idempotent", False)
                    and cleanup_details.get("previous_output")
                ):
                    previous_font = fontforge.open(cleanup_details["previous_output"])
                    current_font = fontforge.open(str(cleaned_ttf))
                    cleanup_details["geometric_idempotent"] = layers_geometrically_converged(
                        previous_font[glyph_name].foreground,
                        current_font[glyph_name].foreground,
                    )
                    previous_font.close()
                    current_font.close()
                    if cleanup_details["geometric_idempotent"]:
                        cleanup_backend = "fonttools-skia-pathops-geometric"
            except Exception as pathops_error:
                cleanup_backend = "fontforge-fallback"
                cleanup_details = {
                    "error": str(pathops_error), "selective_error": str(selective_error),
                    "idempotent": False,
                }

        # PathOps occasionally converges to different serialized contours.
        # Retry with the isolated FontForge backend before rejecting stability.
        if not (
            cleanup_details.get("idempotent", False)
            or cleanup_details.get("geometric_idempotent", False)
        ):
            glyph.foreground = original_layer
            if glyph.references:
                glyph.unlinkRef()
            glyph.removeOverlap()
            first_layer = glyph.foreground.dup()
            first_signature = tuple(
                (round(point.x, 3), round(point.y, 3), bool(point.on_curve))
                for contour in glyph.foreground for point in contour
            )
            glyph.removeOverlap()
            second_signature = tuple(
                (round(point.x, 3), round(point.y, 3), bool(point.on_curve))
                for contour in glyph.foreground for point in contour
            )
            cleanup_backend = "fontforge-stability-fallback"
            cleanup_details["idempotent"] = first_signature == second_signature
            if not cleanup_details["idempotent"]:
                cleanup_details["geometric_idempotent"] = layers_geometrically_converged(
                    first_layer, glyph.foreground
                )
        result["cleanup_status"] = "completed"

    # Keep the cleaned baseline at full precision. Final TTF generation is the
    # only place where coordinates are rounded.
    cleaned_layer = glyph.foreground.dup()
    cleaned_layer, pruned_contours, pruned_area = canonicalize_debris(
        cleaned_layer, original_bbox, font.em
    )
    glyph.foreground = cleaned_layer
    result["pruned_contours"] = pruned_contours
    result["pruned_area"] = "{:.6f}".format(pruned_area)
    result["topology_reference"] = "canonical-cleaned"
    if pruned_contours:
        result["recovery_method"] = "validated-debris-pruning"
    cleaned_points = point_count(glyph)
    cleaned_contours = contour_count(glyph)
    result["cleaned_points"] = cleaned_points
    result["cleaned_contours"] = cleaned_contours
    result["cleanup_backend"] = cleanup_backend
    if not result.get("cleanup_status"):
        result["cleanup_status"] = "completed"
    cleanup_stable = bool(
        cleanup_details.get("idempotent", False)
        or cleanup_details.get("geometric_idempotent", False)
    )
    result["cleanup_idempotent"] = str(cleanup_stable).lower()
    result["cleanup_stability_mode"] = (
        "geometric" if cleanup_details.get("geometric_idempotent", False)
        else "serialized" if cleanup_details.get("idempotent", False)
        else "none"
    )
    result["cleanup_error"] = cleanup_details.get("error", "")
    cleaned_sets = layer_point_sets(cleaned_layer)
    cleaned_mask = rasterize(cleaned_sets, original_bbox)
    cleaned_topology = topology(cleaned_mask)
    cleaned_topologies = topology_at_sizes(cleaned_sets, original_bbox)
    add_topology_diagnostics(result, "cleaned", cleaned_topologies)
    cleaned_metrics = evaluate_candidate(
        cleaned_layer, original_sets, cleaned_mask, original_masks, original_bbox,
        cleaned_topology, cleaned_topologies,
    )
    visual_cleanup_valid = (
        cleaned_metrics["mse"] <= MAX_MSE
        and cleaned_metrics["ink_iou"] >= MIN_INK_IOU
        and cleaned_metrics["false_positive_ink"] <= MAX_FALSE_POSITIVE_INK
        and cleaned_metrics["false_negative_ink"] <= MAX_FALSE_NEGATIVE_INK
    )
    result["cleanup_variant_count"] = max(
        1, int(cleanup_details.get("passes", 0) or 0)
    )
    registration_eligible = (
        cleaned_metrics["mse"] <= 0.03 or cleaned_metrics["ink_iou"] >= 0.90
    )
    if cleanup_stable and not visual_cleanup_valid and registration_eligible:
        registered = best_registered_cleanup(
            cleaned_layer, original_sets, original_masks, original_bbox,
            cleaned_topology, cleaned_topologies,
        )
        if registered is not None:
            cleaned_layer = registered["layer"]
            glyph.foreground = cleaned_layer
            cleaned_sets = layer_point_sets(cleaned_layer)
            cleaned_mask = rasterize(cleaned_sets, original_bbox)
            cleaned_topology = topology(cleaned_mask)
            cleaned_topologies = topology_at_sizes(cleaned_sets, original_bbox)
            cleaned_metrics = registered["metrics"]
            cleaned_points = point_count(glyph)
            cleaned_contours = contour_count(glyph)
            result["cleaned_points"] = cleaned_points
            result["cleaned_contours"] = cleaned_contours
            result["cleanup_registration_dx"] = registered["dx"]
            result["cleanup_registration_dy"] = registered["dy"]
            result["cleanup_backend"] = cleanup_backend + "-registered"
            result["recovery_method"] = "cleanup-registration"
            add_topology_diagnostics(result, "cleaned", cleaned_topologies)
            visual_cleanup_valid = True
    if has_overlap and not visual_cleanup_valid:
        fallback_variants = fontforge_visual_cleanup_variants(
            glyph, original_layer,
            out_sfd.parent / (out_sfd.stem + "-fontforge-visual"),
            pathops_python,
        )
        valid_variants = []
        for variant in fallback_variants:
            variant_layer = variant["layer"]
            variant_sets = layer_point_sets(variant_layer)
            variant_mask = rasterize(variant_sets, original_bbox)
            variant_topologies = topology_at_sizes(
                variant_sets, original_bbox
            )
            variant_metrics = evaluate_candidate(
                variant_layer, original_sets, original_mask_128,
                original_masks, original_bbox, topology(variant_mask),
                variant_topologies,
            )
            if (
                variant_metrics["mse"] <= MAX_MSE
                and variant_metrics["ink_iou"] >= MIN_INK_IOU
                and variant_metrics["false_positive_ink"]
                <= MAX_FALSE_POSITIVE_INK
                and variant_metrics["false_negative_ink"]
                <= MAX_FALSE_NEGATIVE_INK
            ):
                valid_variants.append((
                    not variant.get("fallback_safe", False),
                    raster_quality_key(variant_metrics), variant,
                    variant_metrics, variant_sets, variant_mask,
                    variant_topologies,
                ))
        if valid_variants:
            (
                _unsafe, _variant_key, selected_variant, cleaned_metrics,
                cleaned_sets, cleaned_mask, cleaned_topologies,
            ) = min(valid_variants, key=lambda item: (item[0], item[1]))
            cleaned_layer = selected_variant["layer"]
            glyph.foreground = cleaned_layer
            cleaned_topology = topology(cleaned_mask)
            cleaned_points = point_count(glyph)
            cleaned_contours = contour_count(glyph)
            cleanup_backend = selected_variant["backend"]
            cleaned_fallback_safe = selected_variant.get(
                "fallback_safe", False
            )
            cleanup_stable = True
            cleanup_details["idempotent"] = True
            result["cleaned_points"] = cleaned_points
            result["cleaned_contours"] = cleaned_contours
            result["cleanup_backend"] = cleanup_backend
            result["cleanup_idempotent"] = "true"
            result["cleanup_stability_mode"] = "geometric"
            result["cleanup_variant_count"] = (
                int(result.get("cleanup_variant_count", 0) or 0)
                + selected_variant["passes"]
            )
            result["recovery_method"] = cleanup_backend
            add_topology_diagnostics(result, "cleaned", cleaned_topologies)
            visual_cleanup_valid = True
    cleanup_valid = visual_cleanup_valid and cleanup_stable
    if not cleanup_stable:
        cleanup_rejection_reason = "non-idempotent-cleanup"
    elif not visual_cleanup_valid:
        cleanup_rejection_reason = "visual-mismatch"
    else:
        cleanup_rejection_reason = ""
    result["cleanup_rejection_reason"] = cleanup_rejection_reason
    result["cleanup_valid"] = str(cleanup_valid).lower()
    result["overlap_status"] = (
        "cleaned-baseline-validated" if cleanup_valid and cleaned_fallback_safe
        else "cleanup-intermediate-validated" if cleanup_valid
        else "cleanup-invalid"
    )
    cleanup_attempt_path = out_sfd.with_name(out_sfd.stem + "-cleanup-attempt.sfd")
    save_worker_glyph(glyph, cleaned_layer, cleanup_attempt_path)
    result["cleanup_sfd_path"] = str(cleanup_attempt_path)
    if not cleanup_valid:
        donor_spec = TEMPLATE_DONORS.get(glyph_name)
        if donor_spec and donor_source:
            donor_name, flip_x, flip_y = donor_spec
            donor_font = fontforge.open(str(donor_source))
            try:
                donor_layer = donor_font[donor_name].foreground.dup()
            finally:
                donor_font.close()
            donor_sets = layer_point_sets(donor_layer)
            if donor_sets:
                donor_layer, _donor_pruned, _donor_area = canonicalize_debris(
                    donor_layer, padded_bbox(bbox_of_sets(donor_sets)), font.em
                )
            template = registered_template_candidate(
                glyph, donor_layer, flip_x, flip_y, original_sets,
                original_masks, original_bbox, topology(original_mask_128),
                original_topologies, out_sfd.parent,
            )
            if template is not None:
                template_layer = template["layer"]
                save_worker_glyph(glyph, template_layer, out_sfd)
                cleaned_sfd_path = out_sfd.with_name(out_sfd.stem + "-cleaned.sfd")
                save_worker_glyph(glyph, template_layer, cleaned_sfd_path)
                result.update(
                    worker_status="ok", rebuild_status="rebuilt-accepted",
                    diff_status="rebuilt-accepted", needs_manual_review="false",
                    quality_action="accept-rebuilt", selection_reason="registered-family-template",
                    final_outline="accepted-rebuild", final_overlap_status="removed",
                    overlap_status="template-recovery-validated",
                    topology_reference="registered-family-template",
                    recovery_method="family-template-reflection",
                    template_donor=donor_name,
                    cleanup_registration_dx=template["dx"],
                    cleanup_registration_dy=template["dy"],
                    new_points=template["points"], final_points=template["points"],
                    new_contours=len(template_layer), selected_budget=MAX_POINTS,
                    target_points=MAX_POINTS, fit_mode="family-template",
                    sfd_path=str(out_sfd), candidate_sfd_path=str(out_sfd),
                    cleaned_sfd_path=str(cleaned_sfd_path),
                )
                add_metrics(result, template["metrics"])
                font.close()
                return result
        result.update(
            worker_status="ok", rebuild_status="kept-original-cleanup-failure",
            diff_status="needs-manual-review", needs_manual_review="true",
            quality_action="reject-cleanup-failure", selection_reason="cleanup-failed-validation",
            final_outline="original", final_overlap_status="present-or-unknown",
        )
        font.close()
        return result
    cleaned_sfd_path = out_sfd.with_name(out_sfd.stem + "-cleaned.sfd")
    save_worker_glyph(glyph, cleaned_layer, cleaned_sfd_path)
    result["cleaned_sfd_path"] = str(cleaned_sfd_path)
    donor_spec = TEMPLATE_DONORS.get(glyph_name)
    if donor_spec and donor_source:
        donor_name, flip_x, flip_y = donor_spec
        donor_font = fontforge.open(str(donor_source))
        try:
            donor_layer = donor_font[donor_name].foreground.dup()
        finally:
            donor_font.close()
        donor_sets = layer_point_sets(donor_layer)
        if donor_sets:
            donor_layer, _donor_pruned, _donor_area = canonicalize_debris(
                donor_layer, padded_bbox(bbox_of_sets(donor_sets)), font.em
            )
        template = registered_template_candidate(
            glyph, donor_layer, flip_x, flip_y, original_sets,
            original_masks, original_bbox, cleaned_topology,
            cleaned_topologies, out_sfd.parent,
        )
        if template is not None:
            template_layer = template["layer"]
            save_worker_glyph(glyph, template_layer, out_sfd)
            result.update(
                worker_status="ok", rebuild_status="rebuilt-accepted",
                diff_status="rebuilt-accepted", needs_manual_review="false",
                quality_action="accept-rebuilt", selection_reason="registered-family-template",
                final_outline="accepted-rebuild", final_overlap_status="removed",
                overlap_status="template-recovery-validated",
                topology_reference="canonical-cleaned",
                recovery_method="family-template-reflection",
                template_donor=donor_name,
                cleanup_registration_dx=template["dx"],
                cleanup_registration_dy=template["dy"],
                new_points=template["points"], final_points=template["points"],
                new_contours=len(template_layer), selected_budget=MAX_POINTS,
                target_points=MAX_POINTS, fit_mode="family-template",
                sfd_path=str(out_sfd), candidate_sfd_path=str(out_sfd),
            )
            add_metrics(result, template["metrics"])
            font.close()
            return result
    small_contour_budget_pressure = False
    checkpoint_seed_candidate = None
    if candidate_source:
        checkpoint_candidate_font = fontforge.open(str(candidate_source))
        try:
            checkpoint_candidate = checkpoint_candidate_font[glyph_name].foreground.dup()
        finally:
            checkpoint_candidate_font.close()
        checkpoint_candidate, candidate_pruned, candidate_pruned_area = (
            canonicalize_debris(checkpoint_candidate, original_bbox, font.em)
        )
        if candidate_pruned:
            result["pruned_contours"] = (
                int(result.get("pruned_contours", 0) or 0) + candidate_pruned
            )
            result["pruned_area"] = "{:.6f}".format(
                float(result.get("pruned_area", 0.0) or 0.0)
                + candidate_pruned_area
            )
            result["recovery_method"] = "checkpoint-candidate-debris-pruning"
        checkpoint_candidate, restored_micro = restore_cleaned_micro_contours(
            checkpoint_candidate, cleaned_layer, font.em
        )
        if restored_micro:
            result["recovery_method"] = "cleaned-micro-contour-restoration"
        checkpoint_points = sum(len(contour) for contour in checkpoint_candidate)
        contour_areas = [abs(signed_area(contour)) for contour in cleaned_sets]
        largest_contour_area = max(contour_areas, default=1.0)
        small_contour_budget_pressure = any(
            contour_index < len(checkpoint_candidate)
            and area < largest_contour_area * 0.01
            and len(checkpoint_candidate[contour_index]) > 6
            for contour_index, area in enumerate(contour_areas)
        )
        if checkpoint_points <= MAX_POINTS:
            checkpoint_intersects = layer_has_proper_intersections(checkpoint_candidate)
            candidate_reference_topology = cleaned_topology
            candidate_reference_topologies = cleaned_topologies
            family_topology_path = None
            topology_donor_name = TOPOLOGY_DONORS.get(glyph_name)
            if topology_donor_name and donor_source:
                topology_font = fontforge.open(str(donor_source))
                try:
                    donor_layer = topology_font[topology_donor_name].foreground.dup()
                finally:
                    topology_font.close()
                donor_sets = layer_point_sets(donor_layer)
                donor_bbox = bbox_of_sets(donor_sets)
                target_bbox = bbox_of_sets(original_sets)
                donor_cx = (donor_bbox[0] + donor_bbox[2]) * 0.5
                donor_cy = (donor_bbox[1] + donor_bbox[3]) * 0.5
                target_cx = (target_bbox[0] + target_bbox[2]) * 0.5
                target_cy = (target_bbox[1] + target_bbox[3]) * 0.5
                aligned_donor = transformed_layer(
                    donor_layer,
                    a=(target_bbox[2] - target_bbox[0]) / max(
                        EPSILON, donor_bbox[2] - donor_bbox[0]
                    ),
                    d=(target_bbox[3] - target_bbox[1]) / max(
                        EPSILON, donor_bbox[3] - donor_bbox[1]
                    ),
                    dx=target_cx - donor_cx, dy=target_cy - donor_cy,
                    cx=donor_cx, cy=donor_cy,
                )
                donor_topologies = topology_at_sizes(
                    layer_point_sets(aligned_donor), original_bbox
                )
                candidate_reference_topologies = donor_topologies
                candidate_reference_topology = donor_topologies[RASTER_SIZE]
                family_topology_path = out_sfd.with_name(
                    out_sfd.stem + "-family-topology.sfd"
                )
                save_worker_glyph(glyph, aligned_donor, family_topology_path)
            checkpoint_metrics = evaluate_candidate(
                checkpoint_candidate, original_sets, cleaned_mask, original_masks,
                original_bbox, candidate_reference_topology,
                candidate_reference_topologies,
            )
            if checkpoint_intersects:
                (
                    micro_layer, micro_metrics, micro_count, _micro_index,
                    micro_dx, micro_dy,
                ) = register_micro_contours(
                    glyph, checkpoint_candidate, font.em, original_sets,
                    original_mask_128, original_masks, original_bbox,
                    candidate_reference_topology, candidate_reference_topologies,
                    out_sfd.parent, out_sfd.stem + "-checkpoint",
                )
                result["candidate_count"] = (
                    int(result.get("candidate_count", 0) or 0) + micro_count
                )
                if micro_layer is not None:
                    checkpoint_candidate = micro_layer
                    checkpoint_metrics = micro_metrics
                    checkpoint_intersects = False
                    result["recovery_method"] = "micro-contour-registration"
                    result["cleanup_registration_dx"] = micro_dx
                    result["cleanup_registration_dy"] = micro_dy
            if checkpoint_intersects:
                uncrossed_layer, uncrossed_changed = uncross_polyline_layer(
                    checkpoint_candidate
                )
                if uncrossed_changed and not layer_has_proper_intersections(
                    uncrossed_layer
                ):
                    uncrossed_metrics = evaluate_candidate(
                        uncrossed_layer, original_sets, cleaned_mask, original_masks,
                        original_bbox, candidate_reference_topology,
                        candidate_reference_topologies,
                    )
                    # A crossing outline is not a usable final candidate.  Keep a
                    # topology-equivalent 2-opt repair as the refinement seed when
                    # its raster change is negligible, even if raster quantization
                    # makes the immediate score a fraction worse.  Vertex insertion
                    # and coordinate descent can then operate on valid geometry.
                    uncross_degradation = (
                        uncrossed_metrics["mse"] - checkpoint_metrics["mse"]
                    )
                    if (
                        uncrossed_metrics["passes"]
                        or (
                            uncrossed_metrics["component_delta"]
                            == checkpoint_metrics["component_delta"]
                            and uncrossed_metrics["counter_delta"]
                            == checkpoint_metrics["counter_delta"]
                            and uncross_degradation <= 0.001
                            and uncrossed_metrics["ink_iou"]
                            >= checkpoint_metrics["ink_iou"] - 0.002
                            and uncrossed_metrics["false_positive_ink"]
                            <= checkpoint_metrics["false_positive_ink"] + 0.002
                            and uncrossed_metrics["false_negative_ink"]
                            <= checkpoint_metrics["false_negative_ink"] + 0.002
                        )
                    ):
                        checkpoint_candidate = uncrossed_layer
                        checkpoint_metrics = uncrossed_metrics
                        checkpoint_intersects = False
                        result["recovery_method"] = "polyline-two-opt-uncrossing"
            if (
                glyph_name in FRACTION_GLYPHS
                and glyph_name not in DIRECT_FRACTION_REFINEMENT_GLYPHS
                and not fresh_fit
                and not checkpoint_intersects
                and checkpoint_metrics["component_delta"] == 0
                and checkpoint_metrics["counter_delta"] == 0
            ):
                (
                    fraction_layer, fraction_metrics, fraction_count,
                    fraction_transform,
                ) = rebuild_fraction_weak_components(
                    glyph, checkpoint_candidate, cleaned_layer, original_sets,
                    original_mask_128, original_masks, original_bbox,
                    candidate_reference_topology,
                    candidate_reference_topologies, out_sfd.parent,
                    out_sfd.stem + "-fraction-hybrid",
                )
                fraction_method = "fraction-hybrid-reallocation"
                if fraction_layer is None:
                    (
                        fraction_layer, fraction_metrics, registration_count,
                        fraction_transform,
                    ) = register_fraction_components(
                        glyph, checkpoint_candidate, cleaned_layer,
                        original_sets, original_mask_128, original_masks,
                        original_bbox, candidate_reference_topology,
                        candidate_reference_topologies, out_sfd.parent,
                        out_sfd.stem + "-fraction-components",
                    )
                    fraction_count += registration_count
                    fraction_method = "fraction-component-registration"
                result["candidate_count"] = (
                    int(result.get("candidate_count", 0) or 0)
                    + fraction_count
                )
                if fraction_layer is not None:
                    checkpoint_candidate = fraction_layer
                    checkpoint_metrics = fraction_metrics
                    checkpoint_points = sum(
                        len(contour) for contour in checkpoint_candidate
                    )
                    result["recovery_method"] = fraction_method
                    result["fit_mode"] = "fraction-components-{}".format(
                        fraction_transform
                    )
                    if not checkpoint_metrics["passes"]:
                        candidate_sfd_path = out_sfd.with_name(
                            out_sfd.stem + "-candidate.sfd"
                        )
                        save_worker_glyph(
                            glyph, checkpoint_candidate, candidate_sfd_path
                        )
                        production_fallback = (
                            cleaned_layer if cleaned_fallback_safe
                            else original_layer
                        )
                        save_worker_glyph(glyph, production_fallback, out_sfd)
                        result.update(
                            worker_status="ok",
                            rebuild_status=(
                                "cleaned-fallback-review"
                                if cleaned_fallback_safe
                                else "kept-original-cleanup-failure"
                            ),
                            diff_status="needs-manual-review",
                            needs_manual_review="true",
                            selection_reason=(
                                fraction_method + "-improved"
                            ),
                            quality_action=quality_action(checkpoint_metrics),
                            new_points=(
                                cleaned_points if cleaned_fallback_safe
                                else old_points
                            ),
                            final_points=(
                                cleaned_points if cleaned_fallback_safe
                                else old_points
                            ),
                            new_contours=(
                                cleaned_contours if cleaned_fallback_safe
                                else int(result["old_contours"])
                            ),
                            final_outline=(
                                "cleaned-fallback-over-80"
                                if cleaned_fallback_safe else "original"
                            ),
                            final_overlap_status=(
                                "removed" if cleaned_fallback_safe
                                else "present-or-unknown"
                            ),
                            sfd_path=str(out_sfd),
                            candidate_sfd_path=str(candidate_sfd_path),
                            candidate_points=checkpoint_points,
                            candidate_contours=len(checkpoint_candidate),
                            candidate_action=quality_action(checkpoint_metrics),
                            candidate_mse="{:.6f}".format(
                                checkpoint_metrics["mse"]
                            ),
                            candidate_ink_iou="{:.6f}".format(
                                checkpoint_metrics["ink_iou"]
                            ),
                            candidate_false_positive_ink="{:.6f}".format(
                                checkpoint_metrics["false_positive_ink"]
                            ),
                            candidate_false_negative_ink="{:.6f}".format(
                                checkpoint_metrics["false_negative_ink"]
                            ),
                            candidate_component_delta=(
                                checkpoint_metrics["component_delta"]
                            ),
                            candidate_counter_delta=(
                                checkpoint_metrics["counter_delta"]
                            ),
                            candidate_topology_status=(
                                checkpoint_metrics["topology_status"]
                            ),
                        )
                        add_metrics(result, cleaned_metrics)
                        font.close()
                        return result
            if (
                glyph_name in BOUNDS_REGISTRATION_GLYPHS
                and not fresh_fit
                and not checkpoint_intersects
                and checkpoint_metrics["component_delta"] == 0
                and checkpoint_metrics["counter_delta"] == 0
            ):
                (
                    bounds_layer, bounds_metrics, bounds_count,
                    bounds_transform,
                ) = register_dominant_contour_bounds(
                    glyph, checkpoint_candidate, cleaned_layer, original_sets,
                    original_mask_128, original_masks, original_bbox,
                    candidate_reference_topology,
                    candidate_reference_topologies, out_sfd.parent,
                    out_sfd.stem + "-dominant-bounds",
                )
                result["candidate_count"] = (
                    int(result.get("candidate_count", 0) or 0) + bounds_count
                )
                if bounds_layer is not None:
                    checkpoint_candidate = bounds_layer
                    checkpoint_metrics = bounds_metrics
                    checkpoint_points = sum(
                        len(contour) for contour in checkpoint_candidate
                    )
                    result["recovery_method"] = (
                        "dominant-contour-bounds-registration"
                    )
                    result["fit_mode"] = "dominant-bounds-{}".format(
                        bounds_transform
                    )
                else:
                    result["recovery_method"] = (
                        "dominant-contour-bounds-converged"
                    )
                small_contour_budget_pressure = True
            if (
                glyph_name in AREA_REGISTRATION_GLYPHS
                and not fresh_fit
                and not checkpoint_intersects
                and checkpoint_metrics["component_delta"] == 0
                and checkpoint_metrics["counter_delta"] == 0
            ):
                (
                    area_layer, area_metrics, area_count, area_transform,
                ) = register_independent_contour_areas(
                    glyph, checkpoint_candidate, cleaned_layer, original_sets,
                    original_mask_128, original_masks, original_bbox,
                    candidate_reference_topology,
                    candidate_reference_topologies, out_sfd.parent,
                    out_sfd.stem + "-independent-area",
                )
                result["candidate_count"] = (
                    int(result.get("candidate_count", 0) or 0) + area_count
                )
                if area_layer is not None:
                    checkpoint_candidate = area_layer
                    checkpoint_metrics = area_metrics
                    checkpoint_points = sum(
                        len(contour) for contour in checkpoint_candidate
                    )
                    result["recovery_method"] = (
                        "independent-contour-area-registration"
                    )
                    result["fit_mode"] = "independent-area-{}".format(
                        area_transform
                    )
                    # Preserve an incremental improvement immediately rather
                    # than entering the already exhausted structural search.
                    small_contour_budget_pressure = True
                else:
                    result["recovery_method"] = (
                        "independent-contour-area-converged"
                    )
                    small_contour_budget_pressure = True
            if (
                not checkpoint_intersects
                and checkpoint_metrics["component_delta"] == 0
                and checkpoint_metrics["counter_delta"] < 0
                and checkpoint_metrics["mse"] <= MAX_MSE
                and checkpoint_metrics["ink_iou"] >= MIN_INK_IOU
                and checkpoint_metrics["false_positive_ink"]
                <= MAX_FALSE_POSITIVE_INK
                and checkpoint_metrics["false_negative_ink"]
                <= MAX_FALSE_NEGATIVE_INK
            ):
                (
                    counter_layer, counter_metrics, counter_count,
                    _counter_index, _counter_scale, counter_dx, counter_dy,
                ) = register_small_counter_contours(
                    glyph, checkpoint_candidate, original_sets,
                    original_mask_128, original_masks, original_bbox,
                    candidate_reference_topology, candidate_reference_topologies,
                    out_sfd.parent, out_sfd.stem + "-checkpoint",
                )
                result["candidate_count"] = (
                    int(result.get("candidate_count", 0) or 0) + counter_count
                )
                if counter_layer is not None:
                    checkpoint_candidate = counter_layer
                    checkpoint_metrics = counter_metrics
                    checkpoint_intersects = False
                    result["recovery_method"] = "small-counter-registration"
                    result["cleanup_registration_dx"] = counter_dx
                    result["cleanup_registration_dy"] = counter_dy
            cardinal_count = 0
            if (
                not checkpoint_metrics["passes"]
                and not fresh_fit
                and not checkpoint_intersects
                and checkpoint_points >= MAX_POINTS
                and glyph_name not in LOCAL_REFINEMENT_GLYPHS
                and not small_contour_budget_pressure
                and checkpoint_metrics["component_delta"] == 0
                and checkpoint_metrics["counter_delta"] == 0
                and (
                    (
                        checkpoint_metrics["mse"] <= 0.025
                        and checkpoint_metrics["ink_iou"] >= 0.87
                    )
                    or (
                        glyph_name in LOCAL_REFINEMENT_GLYPHS
                        and checkpoint_metrics["mse"] <= 0.035
                        and checkpoint_metrics["ink_iou"] >= 0.82
                    )
                )
            ):
                (
                    registered_layer, registered_metrics, registered_count,
                    registered_scale_x, registered_scale_y,
                ) = register_checkpoint_candidate(
                    glyph, checkpoint_candidate, original_sets,
                    original_mask_128, original_masks, original_bbox,
                    candidate_reference_topology, candidate_reference_topologies,
                    out_sfd.parent, out_sfd.stem + "-checkpoint",
                )
                result["candidate_count"] = (
                    int(result.get("candidate_count", 0) or 0) + registered_count
                )
                if (
                    registered_layer is not None
                    and raster_quality_key(registered_metrics)
                    < raster_quality_key(checkpoint_metrics)
                ):
                    checkpoint_candidate = registered_layer
                    checkpoint_metrics = registered_metrics
                    checkpoint_points = sum(
                        len(contour) for contour in checkpoint_candidate
                    )
                    checkpoint_intersects = False
                    result["recovery_method"] = "candidate-affine-registration"
                    result["fit_mode"] = "checkpoint-affine-{:.4f}-{:.4f}".format(
                        registered_scale_x, registered_scale_y
                    )
            if (
                not checkpoint_metrics["passes"]
                and not fresh_fit
                and glyph_name not in DIRECT_RESTORATION_GLYPHS
                and checkpoint_points < MAX_POINTS
                and not small_contour_budget_pressure
                and checkpoint_metrics["component_delta"] == 0
                and checkpoint_metrics["counter_delta"] == 0
                and (
                    (
                        checkpoint_metrics["mse"] <= 0.025
                        and checkpoint_metrics["ink_iou"] >= 0.87
                    )
                    or (
                        glyph_name in LOCAL_REFINEMENT_GLYPHS
                        and checkpoint_metrics["mse"] <= 0.035
                        and checkpoint_metrics["ink_iou"] >= 0.82
                    )
                )
            ):
                inserted_layer, inserted_metrics, inserted_count, inserted_changed = (
                    insert_checkpoint_vertices(
                        glyph, checkpoint_candidate, cleaned_layer, original_sets,
                        original_mask_128, original_masks, original_bbox,
                        candidate_reference_topology, candidate_reference_topologies,
                        out_sfd.parent, out_sfd.stem + "-checkpoint",
                    )
                )
                result["candidate_count"] = (
                    int(result.get("candidate_count", 0) or 0) + inserted_count
                )
                if inserted_changed:
                    checkpoint_candidate = inserted_layer
                    checkpoint_metrics = inserted_metrics
                    checkpoint_points = sum(
                        len(contour) for contour in checkpoint_candidate
                    )
                    checkpoint_intersects = layer_has_proper_intersections(
                        checkpoint_candidate
                    )
                    result["recovery_method"] = "cleaned-vertex-insertion"
                    small_contour_budget_pressure = True
            cardinal_count = 0
            if (
                not checkpoint_metrics["passes"]
                and not fresh_fit
                and not small_contour_budget_pressure
                and glyph_name not in DIRECT_RESTORATION_GLYPHS
                and checkpoint_metrics["component_delta"] == 0
                and checkpoint_metrics["counter_delta"] == 0
                and (
                    (
                        checkpoint_metrics["mse"] <= 0.025
                        and checkpoint_metrics["ink_iou"] >= 0.87
                    )
                    or (
                        glyph_name in LOCAL_REFINEMENT_GLYPHS
                        and checkpoint_metrics["mse"] <= 0.035
                        and checkpoint_metrics["ink_iou"] >= 0.82
                    )
                )
            ):
                efficient_layer = efficient_metrics = None
                efficient_count = 0
                split_refined = False
                split_attempted = (
                    checkpoint_points < MAX_POINTS
                    and any(
                        not point.on_curve
                        for contour in checkpoint_candidate for point in contour
                    )
                )
                if split_attempted:
                    (
                        efficient_layer, efficient_metrics, efficient_count,
                    ) = split_and_refine_quadratic_candidate(
                        glyph, checkpoint_candidate, original_sets,
                        original_mask_128, original_masks, original_bbox,
                        candidate_reference_topology,
                        candidate_reference_topologies, out_sfd.parent,
                        out_sfd.stem + "-checkpoint-split",
                    )
                    split_refined = efficient_layer is not None
                if efficient_layer is None and not split_attempted:
                    (
                        efficient_layer, efficient_metrics, local_count,
                    ) = efficient_raster_refine_candidate(
                        glyph, checkpoint_candidate, original_sets,
                        original_mask_128, original_masks, original_bbox,
                        candidate_reference_topology,
                        candidate_reference_topologies, out_sfd.parent,
                        out_sfd.stem + "-checkpoint-efficient",
                    )
                    efficient_count += local_count
                result["candidate_count"] = (
                    int(result.get("candidate_count", 0) or 0)
                    + efficient_count
                )
                cardinal_count = efficient_count
                if efficient_layer is not None:
                    checkpoint_candidate = efficient_layer
                    checkpoint_metrics = efficient_metrics
                    checkpoint_points = sum(
                        len(contour) for contour in checkpoint_candidate
                    )
                    checkpoint_intersects = False
                    result["recovery_method"] = (
                        "quadratic-split-raster-refinement"
                        if split_refined else "in-memory-raster-refinement"
                    )
                elif max(
                    checkpoint_metrics["mse"] / MAX_MSE,
                    MIN_INK_IOU / max(
                        EPSILON, checkpoint_metrics["ink_iou"]
                    ),
                    checkpoint_metrics["false_positive_ink"]
                    / MAX_FALSE_POSITIVE_INK,
                    checkpoint_metrics["false_negative_ink"]
                    / MAX_FALSE_NEGATIVE_INK,
                ) <= 1.03:
                    pair_layer, pair_metrics, pair_count = (
                        pair_raster_refine_candidate(
                            glyph, checkpoint_candidate, original_sets,
                            original_mask_128, original_masks, original_bbox,
                            candidate_reference_topology,
                            candidate_reference_topologies, out_sfd.parent,
                            out_sfd.stem + "-checkpoint-pair",
                        )
                    )
                    result["candidate_count"] = (
                        int(result.get("candidate_count", 0) or 0)
                        + pair_count
                    )
                    cardinal_count += pair_count
                    if pair_layer is not None:
                        checkpoint_candidate = pair_layer
                        checkpoint_metrics = pair_metrics
                        checkpoint_points = sum(
                            len(contour) for contour in checkpoint_candidate
                        )
                        checkpoint_intersects = False
                        result["recovery_method"] = (
                            "paired-raster-refinement"
                        )
            if (
                not checkpoint_metrics["passes"]
                and not fresh_fit
                and not small_contour_budget_pressure
                and glyph_name in LOCAL_REFINEMENT_GLYPHS
                and checkpoint_metrics["component_delta"] == 0
                and checkpoint_metrics["counter_delta"] == 0
                and checkpoint_metrics["mse"] <= 0.035
                and checkpoint_metrics["ink_iou"] >= 0.82
            ):
                restored_layer, restored_metrics, restored_count = (
                    restore_checkpoint_vertex_positions(
                        glyph, checkpoint_candidate, cleaned_layer,
                        original_sets, original_mask_128, original_masks,
                        original_bbox, candidate_reference_topology,
                        candidate_reference_topologies, out_sfd.parent,
                        out_sfd.stem + "-checkpoint-restored",
                    )
                )
                result["candidate_count"] = (
                    int(result.get("candidate_count", 0) or 0)
                    + restored_count
                )
                if restored_layer is not None:
                    checkpoint_candidate = restored_layer
                    checkpoint_metrics = restored_metrics
                    checkpoint_points = sum(
                        len(contour) for contour in checkpoint_candidate
                    )
                    checkpoint_intersects = False
                    result["recovery_method"] = (
                        "cleaned-vertex-position-restoration"
                    )
            if (
                not checkpoint_metrics["passes"]
                and not fresh_fit
                and glyph_name in POINT_RELOCATION_GLYPHS
                and checkpoint_metrics["component_delta"] == 0
                and checkpoint_metrics["counter_delta"] == 0
                and checkpoint_metrics["mse"] <= 0.03
                and checkpoint_metrics["ink_iou"] >= 0.83
            ):
                relocated_layer, relocated_metrics, relocated_count = (
                    relocate_checkpoint_vertices(
                        glyph, checkpoint_candidate, cleaned_layer,
                        original_sets, original_mask_128, original_masks,
                        original_bbox, candidate_reference_topology,
                        candidate_reference_topologies, out_sfd.parent,
                        out_sfd.stem + "-checkpoint-relocated",
                    )
                )
                result["candidate_count"] = (
                    int(result.get("candidate_count", 0) or 0)
                    + relocated_count
                )
                if relocated_layer is not None:
                    checkpoint_candidate = relocated_layer
                    checkpoint_metrics = relocated_metrics
                    checkpoint_points = sum(
                        len(contour) for contour in checkpoint_candidate
                    )
                    checkpoint_intersects = False
                    result["recovery_method"] = (
                        "cleaned-vertex-budget-relocation"
                    )
            if (
                not checkpoint_metrics["passes"]
                and not fresh_fit
                and not small_contour_budget_pressure
                and glyph_name not in DIRECT_RESTORATION_GLYPHS
                and checkpoint_metrics["component_delta"] == 0
                and checkpoint_metrics["counter_delta"] == 0
                and checkpoint_metrics["mse"] <= 0.022
                and checkpoint_metrics["ink_iou"] >= 0.88
                and cardinal_count == 0
            ):
                cardinal_layer, cardinal_metrics, cardinal_count = (
                    cardinal_refine_checkpoint_candidate(
                        glyph, checkpoint_candidate, original_sets,
                        original_mask_128, original_masks, original_bbox,
                        candidate_reference_topology,
                        candidate_reference_topologies, out_sfd.parent,
                        out_sfd.stem + "-checkpoint",
                    )
                )
                result["candidate_count"] = (
                    int(result.get("candidate_count", 0) or 0) + cardinal_count
                )
                if cardinal_layer is not None:
                    checkpoint_candidate = cardinal_layer
                    checkpoint_metrics = cardinal_metrics
                    checkpoint_points = sum(
                        len(contour) for contour in checkpoint_candidate
                    )
                    checkpoint_intersects = False
                    result["recovery_method"] = "cardinal-point-refinement"
            if (
                not checkpoint_metrics["passes"]
                and not fresh_fit
                and not small_contour_budget_pressure
                and glyph_name not in DIRECT_RESTORATION_GLYPHS
                and checkpoint_metrics["component_delta"] == 0
                and checkpoint_metrics["counter_delta"] == 0
                and checkpoint_metrics["mse"] <= 0.022
                and checkpoint_metrics["ink_iou"] >= 0.88
                and cardinal_count == 0
            ):
                refined_layer, refined_metrics, refined_count = refine_checkpoint_candidate(
                    glyph, checkpoint_candidate, original_sets, original_mask_128,
                    original_masks, original_bbox, candidate_reference_topology,
                    candidate_reference_topologies, out_sfd.parent,
                    out_sfd.stem + "-checkpoint",
                )
                result["candidate_count"] = int(result.get("candidate_count", 0) or 0) + refined_count
                if refined_layer is not None:
                    checkpoint_candidate = refined_layer
                    checkpoint_metrics = refined_metrics
                    checkpoint_points = sum(len(contour) for contour in checkpoint_candidate)
                    checkpoint_intersects = False
                    result["recovery_method"] = "iterative-raster-coordinate-descent"
            if checkpoint_metrics["passes"] and not checkpoint_intersects:
                save_worker_glyph(glyph, checkpoint_candidate, out_sfd)
                result.update(
                    worker_status="ok", rebuild_status="rebuilt-accepted",
                    diff_status="rebuilt-accepted", needs_manual_review="false",
                    quality_action="accept-rebuilt", selection_reason="checkpoint-candidate-passed",
                    final_outline="accepted-rebuild", final_overlap_status="removed",
                    recovery_method=result.get("recovery_method") or "checkpoint-candidate-promotion",
                    new_points=checkpoint_points, final_points=checkpoint_points,
                    new_contours=len(checkpoint_candidate), selected_budget=MAX_POINTS,
                    target_points=MAX_POINTS, fit_mode="checkpoint-candidate",
                    sfd_path=str(out_sfd), candidate_sfd_path=str(out_sfd),
                )
                if family_topology_path is not None:
                    result.update(
                        topology_reference="family-template-{}".format(
                            topology_donor_name
                        ),
                        template_donor=topology_donor_name,
                        recovery_method="family-topology-candidate-promotion",
                        cleaned_sfd_path=str(family_topology_path),
                    )
                add_metrics(result, checkpoint_metrics)
                font.close()
                return result
            if not checkpoint_intersects:
                checkpoint_seed_candidate = {
                    "budget": MAX_POINTS,
                    "layer": checkpoint_candidate.dup(),
                    "curve_segments": "",
                    "line_segments": "",
                    "tolerance": 0.0,
                    "points": checkpoint_points,
                    "contours": len(checkpoint_candidate),
                    "metrics": checkpoint_metrics,
                    "mode": result.get("fit_mode") or "checkpoint-seed",
                }
            if (
                cleanup_stable
                and not cleanup_details.get("geometric_idempotent", False)
                and result.get("recovery_method") != "cleanup-registration"
                and cleaned_points > MAX_POINTS
                and (
                    not small_contour_budget_pressure
                    or result.get("recovery_method")
                    in (
                        "independent-contour-area-registration",
                        "independent-contour-area-converged",
                        "cleaned-vertex-insertion",
                        "dominant-contour-bounds-registration",
                        "dominant-contour-bounds-converged",
                    )
                )
                and not fresh_fit
            ):
                # This serialized-stable cleanup and its exact checkpoint
                # candidate were already exhausted in v2. Preserve that safe
                # fallback instead of repeating the unchanged expensive search.
                candidate_sfd_path = out_sfd.with_name(
                    out_sfd.stem + "-candidate.sfd"
                )
                save_worker_glyph(
                    glyph, checkpoint_candidate, candidate_sfd_path
                )
                save_worker_glyph(glyph, cleaned_layer, out_sfd)
                result.update(
                    worker_status="ok", rebuild_status="cleaned-fallback-review",
                    diff_status="needs-manual-review", needs_manual_review="true",
                    selection_reason="checkpoint-candidate-still-fails",
                    quality_action=quality_action(checkpoint_metrics),
                    new_points=cleaned_points, final_points=cleaned_points,
                    new_contours=cleaned_contours,
                    final_outline="cleaned-fallback-over-80",
                    final_overlap_status="removed", sfd_path=str(out_sfd),
                    candidate_points=checkpoint_points,
                    candidate_contours=len(checkpoint_candidate),
                    candidate_action=quality_action(checkpoint_metrics),
                    candidate_mse="{:.6f}".format(checkpoint_metrics["mse"]),
                    candidate_ink_iou="{:.6f}".format(checkpoint_metrics["ink_iou"]),
                    candidate_false_positive_ink="{:.6f}".format(
                        checkpoint_metrics["false_positive_ink"]
                    ),
                    candidate_false_negative_ink="{:.6f}".format(
                        checkpoint_metrics["false_negative_ink"]
                    ),
                    candidate_component_delta=checkpoint_metrics["component_delta"],
                    candidate_counter_delta=checkpoint_metrics["counter_delta"],
                    candidate_topology_status=checkpoint_metrics["topology_status"],
                    candidate_sfd_path=str(candidate_sfd_path),
                    recovery_method=result.get("recovery_method")
                    or "checkpoint-failed-candidate-reused",
                )
                add_metrics(result, cleaned_metrics)
                font.close()
                return result
    if cleaned_points <= MAX_POINTS and cleaned_metrics["passes"]:
        save_worker_glyph(glyph, cleaned_layer, out_sfd)
        result.update(
            {
                "worker_status": "ok",
                "rebuild_status": "overlap-cleaned",
                "diff_status": "overlap-cleaned",
                "quality_action": "accept-rebuilt",
                "selection_reason": "overlap-cleaned-under-80",
                "new_points": cleaned_points,
                "final_points": cleaned_points,
                "new_contours": cleaned_contours,
                "selected_budget": MAX_POINTS,
                "target_points": MAX_POINTS,
                "fit_mode": "overlap-only",
                "sfd_path": str(out_sfd),
                "candidate_sfd_path": str(out_sfd),
                "final_outline": "cleaned",
                "final_overlap_status": "removed",
            }
        )
        add_metrics(result, cleaned_metrics)
        font.close()
        return result

    contours = cleaned_sets
    topology_minimum = len(contours) * 3
    minimum_budget = max(10, topology_minimum)
    result["topology_minimum"] = topology_minimum
    candidates = []
    if checkpoint_seed_candidate is not None:
        candidates.append(checkpoint_seed_candidate)
    signatures = set()
    removal_orders = [
        safe_removal_order(
            contour,
            [other for other_index, other in enumerate(contours) if other_index != contour_index],
        )
        for contour_index, contour in enumerate(contours)
    ]
    safe_minimum = sum(
        max(3, len(contour) - len(order))
        for contour, order in zip(contours, removal_orders)
    )
    result["safe_removal_minimum"] = safe_minimum
    contour_ladders = build_contour_ladders(contours, removal_orders)
    if small_contour_budget_pressure and safe_minimum <= MAX_POINTS:
        contour_areas_for_budget = [
            max(EPSILON, abs(signed_area(contour))) for contour in contours
        ]
        largest_area_for_budget = max(contour_areas_for_budget, default=1.0)
        small_contour_count = sum(
            area < largest_area_for_budget * 0.01
            for area in contour_areas_for_budget
        )
        mixed_anchor_budget = max(
            safe_minimum, MAX_POINTS // 2 - small_contour_count
        )
        mixed_targets = allocate_pareto_targets(
            contour_ladders, mixed_anchor_budget
        )
        mixed_built = (
            build_budget_layer(
                contours, removal_orders, MAX_POINTS, True,
                "area-pressure-pareto", mixed_targets,
            )
            if mixed_targets is not None else None
        )
        if mixed_built is not None:
            mixed_layer, mixed_curves, mixed_lines, _mixed_points = mixed_built
            mixed_actual = roundtrip_candidate(
                glyph, mixed_layer, out_sfd.parent,
                out_sfd.stem + "-area-pressure",
            )
            mixed_metrics = evaluate_candidate(
                mixed_actual, original_sets, cleaned_mask, original_masks,
                original_bbox, cleaned_topology, cleaned_topologies,
            )
            recovery_count = 1
            if (
                not mixed_metrics["passes"]
                and not layer_has_proper_intersections(mixed_actual)
            ):
                (
                    recovered_layer, recovered_metrics, registered_count,
                    _scale_x, _scale_y,
                ) = register_checkpoint_candidate(
                    glyph, mixed_actual, original_sets, original_mask_128,
                    original_masks, original_bbox, cleaned_topology,
                    cleaned_topologies, out_sfd.parent,
                    out_sfd.stem + "-area-pressure",
                )
                recovery_count += registered_count
                if recovered_layer is not None and recovered_metrics["passes"]:
                    mixed_actual = recovered_layer
                    mixed_metrics = recovered_metrics
            mixed_points = sum(len(contour) for contour in mixed_actual)
            if (
                mixed_points <= MAX_POINTS
                and mixed_metrics["passes"]
                and not layer_has_proper_intersections(mixed_actual)
            ):
                save_worker_glyph(glyph, mixed_actual, out_sfd)
                result.update(
                    worker_status="ok", rebuild_status="rebuilt-accepted",
                    diff_status="rebuilt-accepted", needs_manual_review="false",
                    quality_action="accept-rebuilt",
                    selection_reason="area-pressure-mixed-candidate-passed",
                    final_outline="rebuilt-under-80",
                    final_overlap_status="removed",
                    recovery_method="area-pressure-mixed-affine",
                    new_points=mixed_points, final_points=mixed_points,
                    new_contours=len(mixed_actual), selected_budget=MAX_POINTS,
                    target_points=MAX_POINTS,
                    fit_mode="area-pressure-mixed-quadratic-pareto",
                    curve_segments=mixed_curves, line_segments=mixed_lines,
                    allocated_points=mixed_points,
                    candidate_count=recovery_count,
                    sfd_path=str(out_sfd), candidate_sfd_path=str(out_sfd),
                )
                add_metrics(result, mixed_metrics)
                font.close()
                return result
    if safe_minimum > MAX_POINTS:
        result["candidate_stop_reason"] = "safe-contour-minimum-exceeds-80"
    if cleanup_details.get("geometric_idempotent", False) and safe_minimum <= MAX_POINTS:
        fast_targets = allocate_pareto_targets(contour_ladders, MAX_POINTS)
        if fast_targets is not None:
            fast_built = build_budget_layer(
                contours, removal_orders, MAX_POINTS, False, "pareto", fast_targets
            )
            if fast_built is not None:
                fast_layer, fast_curves, fast_lines, _fast_points = fast_built
                fast_actual = roundtrip_candidate(
                    glyph, fast_layer, out_sfd.parent, out_sfd.stem + "-geometric-fast"
                )
                fast_points = sum(len(contour) for contour in fast_actual)
                fast_metrics = evaluate_candidate(
                    fast_actual, original_sets, cleaned_mask, original_masks,
                    original_bbox, cleaned_topology, cleaned_topologies,
                )
                if (
                    fast_points <= MAX_POINTS and fast_metrics["passes"]
                    and not layer_has_proper_intersections(fast_actual)
                ):
                    save_worker_glyph(glyph, fast_actual, out_sfd)
                    result.update(
                        worker_status="ok", rebuild_status="rebuilt-accepted",
                        diff_status="rebuilt-accepted", quality_action="accept-rebuilt",
                        selection_reason="geometric-cleanup-fast-pareto-pass",
                        new_points=fast_points, final_points=fast_points,
                        new_contours=len(fast_actual), selected_budget=MAX_POINTS,
                        target_points=MAX_POINTS, fit_tolerance="0.000000",
                        fit_mode="guarded-visvalingam-polyline-pareto-generated",
                        curve_segments=fast_curves, line_segments=fast_lines,
                        allocated_points=fast_points, candidate_count=1,
                        sfd_path=str(out_sfd), candidate_sfd_path=str(out_sfd),
                        final_outline="rebuilt-under-80", final_overlap_status="removed",
                        recovery_method="geometric-convergence-fast-pareto",
                    )
                    add_metrics(result, fast_metrics)
                    font.close()
                    return result
    # A resumed fresh fit is a quality-recovery operation for an already
    # exhausted <=80-point candidate.  Search the 80-point frontier directly:
    # lower budgets cannot win the quality-first ordering and multiply the
    # expensive topology/raster work without improving the fallback.
    search_budgets = (
        (MAX_POINTS,)
        if fresh_fit and checkpoint_seed_candidate is not None
        else POINT_BUDGETS
    )
    for budget in search_budgets:
        if budget < minimum_budget:
            continue
        for strategy in ("pareto", "complexity", "perimeter", "area", "balanced"):
            if strategy == "pareto":
                line_targets = allocate_pareto_targets(contour_ladders, budget)
            else:
                line_targets = allocate_contour_targets(
                    contours, removal_orders, budget, strategy
                )
            if line_targets is not None:
                line_built = build_line_first_layer(contours, line_targets)
                if line_built is not None:
                    line_layer, line_curves, line_lines, line_points = line_built
                    line_signature = tuple(
                        (round(point.x, 3), round(point.y, 3), bool(point.on_curve))
                        for contour in line_layer for point in contour
                    )
                    if line_signature not in signatures and line_points <= MAX_POINTS:
                        signatures.add(line_signature)
                        line_metrics = evaluate_candidate(
                            line_layer, original_sets, cleaned_mask, original_masks,
                            original_bbox, cleaned_topology,
                            include_outline_metrics=False,
                        )
                        candidates.append(
                            {
                                "budget": budget,
                                "layer": line_layer,
                                "curve_segments": line_curves,
                                "line_segments": line_lines,
                                "tolerance": 0.0,
                                "points": line_points,
                                "contours": len(line_layer),
                                "metrics": line_metrics,
                                "mode": "line-first-{}".format(strategy),
                            }
                        )
            for quadratic in (False, True):
                contour_areas_for_budget = [
                    max(EPSILON, abs(signed_area(contour)))
                    for contour in contours
                ]
                largest_area_for_budget = max(
                    contour_areas_for_budget, default=1.0
                )
                small_contour_count = sum(
                    area < largest_area_for_budget * 0.01
                    for area in contour_areas_for_budget
                )
                anchor_budget = (
                    budget if not quadratic
                    else max(
                        safe_minimum,
                        budget // 2 - small_contour_count,
                    )
                )
                targets_override = None
                if strategy == "pareto":
                    targets_override = allocate_pareto_targets(contour_ladders, anchor_budget)
                    if targets_override is None:
                        continue
                else:
                    targets_override = allocate_contour_targets(
                        contours, removal_orders, anchor_budget, strategy
                    )
                    if targets_override is None:
                        continue
                built = build_budget_layer(
                    contours, removal_orders, budget, quadratic, strategy, targets_override
                )
                if built is None:
                    continue
                layer, curves, lines, points = built
                signature = tuple(
                    (round(point.x, 3), round(point.y, 3), bool(point.on_curve))
                    for contour in layer for point in contour
                )
                if signature in signatures or points > MAX_POINTS:
                    continue
                signatures.add(signature)
                metrics = evaluate_candidate(
                    layer, original_sets, cleaned_mask, original_masks,
                    original_bbox, cleaned_topology,
                    include_outline_metrics=False,
                )
                if quadratic and (metrics["component_delta"] or metrics["counter_delta"]):
                    # A fitted curve that changes topology is replaced by the
                    # same anchor path made from straight segments.
                    repaired = build_budget_layer(
                        contours, removal_orders, sum(targets_override), False,
                        strategy, targets_override,
                    )
                    if repaired is None:
                        continue
                    layer, curves, lines, points = repaired
                    metrics = evaluate_candidate(
                        layer, original_sets, cleaned_mask, original_masks,
                        original_bbox, cleaned_topology,
                        include_outline_metrics=False,
                    )
                    mode = "guarded-visvalingam-quadratic-repaired-line-{}".format(strategy)
                else:
                    mode = "guarded-visvalingam-{}-{}".format(
                        "quadratic" if quadratic else "polyline", strategy
                    )
                candidates.append(
                    {
                        "budget": budget,
                        "layer": layer,
                        "curve_segments": curves,
                        "line_segments": lines,
                        "tolerance": 0.0,
                        "points": points,
                        "contours": len(layer),
                        "metrics": metrics,
                        "mode": mode,
                    }
                )

    # Refine only near-pass finalists. Each proposal moves one on-curve point
    # by one font unit toward missing ink or away from extra ink.
    refinement_seeds = [
        candidate for candidate in sorted(candidates, key=quality_key)[:8]
        if candidate["metrics"]["mse"] <= 0.03
        or candidate["metrics"]["ink_iou"] >= 0.85
    ]
    for seed in refinement_seeds:
        for refined_layer in nudge_refinement_layers(
            seed["layer"], original_mask_128,
            seed["metrics"]["candidate_mask"], original_bbox,
        ):
            points = sum(len(contour) for contour in refined_layer)
            if points > MAX_POINTS:
                continue
            signature = tuple(
                (round(point.x, 3), round(point.y, 3), bool(point.on_curve))
                for contour in refined_layer for point in contour
            )
            if signature in signatures:
                continue
            signatures.add(signature)
            metrics = evaluate_candidate(
                refined_layer, original_sets, cleaned_mask, original_masks,
                original_bbox, cleaned_topology,
                include_outline_metrics=False,
            )
            candidates.append(
                {
                    "budget": seed["budget"], "layer": refined_layer,
                    "curve_segments": seed["curve_segments"],
                    "line_segments": seed["line_segments"], "tolerance": 0.0,
                    "points": points, "contours": len(refined_layer),
                    "metrics": metrics,
                    "mode": seed["mode"] + "-raster-refined",
                }
            )

    result["candidate_count"] = len(candidates)
    ranked_candidates = sorted(
        candidates,
        key=lambda candidate: (not candidate["metrics"]["passes"], quality_key(candidate)),
    )
    shortlist = ranked_candidates[:8]
    represented_modes = {candidate["mode"] for candidate in shortlist}
    for candidate in ranked_candidates:
        if candidate["mode"] in represented_modes:
            continue
        shortlist.append(candidate)
        represented_modes.add(candidate["mode"])
        if len(shortlist) >= 24:
            break
    generated_candidates = []
    for index, candidate in enumerate(shortlist):
        try:
            actual_layer = roundtrip_candidate(
                glyph, candidate["layer"], out_sfd.parent,
                out_sfd.stem + "-{}".format(index),
            )
        except Exception:
            continue
        actual_points = sum(len(contour) for contour in actual_layer)
        if actual_points > MAX_POINTS:
            continue
        actual = dict(candidate)
        actual["layer"] = actual_layer
        actual["points"] = actual_points
        actual["contours"] = len(actual_layer)
        actual["metrics"] = evaluate_candidate(
            actual_layer, original_sets, cleaned_mask, original_masks,
            original_bbox, cleaned_topology,
        )
        actual["mode"] = candidate["mode"] + "-generated"
        generated_candidates.append(actual)
    candidates = generated_candidates
    passing = [candidate for candidate in candidates if candidate["metrics"]["passes"]]
    if not passing and small_contour_budget_pressure and candidates:
        recovery_seed = min(candidates, key=quality_key)
        if (
            recovery_seed["metrics"]["component_delta"] == 0
            and recovery_seed["metrics"]["counter_delta"] == 0
            and not layer_has_proper_intersections(recovery_seed["layer"])
        ):
            recovered_layer, recovered_metrics, recovered_count, _sx, _sy = (
                register_checkpoint_candidate(
                    glyph, recovery_seed["layer"], original_sets,
                    original_mask_128, original_masks, original_bbox,
                    cleaned_topology, cleaned_topologies, out_sfd.parent,
                    out_sfd.stem + "-fresh-mixed",
                )
            )
            result["candidate_count"] = (
                int(result.get("candidate_count", 0) or 0) + recovered_count
            )
            if recovered_layer is not None and recovered_metrics["passes"]:
                recovered = dict(recovery_seed)
                recovered["layer"] = recovered_layer
                recovered["metrics"] = recovered_metrics
                recovered["points"] = sum(
                    len(contour) for contour in recovered_layer
                )
                recovered["contours"] = len(recovered_layer)
                recovered["mode"] = recovery_seed["mode"] + "-affine-recovered"
                candidates.append(recovered)
                passing.append(recovered)
    if passing:
        selected = min(passing, key=quality_key)
        selected_layer = selected["layer"]
        save_worker_glyph(glyph, selected_layer, out_sfd)
        result.update(
            {
                "worker_status": "ok",
                "rebuild_status": "rebuilt-accepted",
                "diff_status": "rebuilt-accepted",
                "quality_action": "accept-rebuilt",
                "selection_reason": "best-quality-passing-under-80",
                "new_points": selected["points"],
                "final_points": selected["points"],
                "new_contours": selected["contours"],
                "selected_budget": selected["budget"],
                "target_points": selected["budget"],
                "fit_tolerance": "{:.6f}".format(selected["tolerance"]),
                "fit_mode": selected["mode"],
                "curve_segments": selected["curve_segments"],
                "line_segments": selected["line_segments"],
                "allocated_points": selected["points"],
                "sfd_path": str(out_sfd),
                "final_outline": "rebuilt-under-80",
                "final_overlap_status": "removed",
                "recovery_method": selected["mode"],
            }
        )
        add_metrics(result, selected["metrics"])
    else:
        best = min(candidates, key=quality_key) if candidates else None
        # Production receives the validated cleaned outline. The failed compact
        # candidate is saved separately and never masquerades as production.
        production_fallback = (
            cleaned_layer if cleaned_fallback_safe else original_layer
        )
        save_worker_glyph(glyph, production_fallback, out_sfd)
        result.update(
            {
                "worker_status": "ok",
                "rebuild_status": (
                    "cleaned-fallback-review" if cleaned_fallback_safe
                    else "kept-original-cleanup-failure"
                ),
                "diff_status": "needs-manual-review",
                "needs_manual_review": "true",
                "selection_reason": "no-passing-candidate-under-80",
                "quality_action": quality_action(best["metrics"]) if best else "reject-no-candidate-under-80",
                "new_points": (
                    cleaned_points if cleaned_fallback_safe else old_points
                ),
                "final_points": (
                    cleaned_points if cleaned_fallback_safe else old_points
                ),
                "new_contours": (
                    cleaned_contours if cleaned_fallback_safe
                    else int(result["old_contours"])
                ),
                "final_outline": (
                    "cleaned-fallback-over-80" if cleaned_fallback_safe
                    else "original"
                ),
                "final_overlap_status": (
                    "removed" if cleaned_fallback_safe
                    else "present-or-unknown"
                ),
                "sfd_path": str(out_sfd),
            }
        )
        if best:
            candidate_path = out_sfd.with_name(out_sfd.stem + "-candidate.sfd")
            save_worker_glyph(glyph, best["layer"], candidate_path)
            result["candidate_sfd_path"] = str(candidate_path)
            result["candidate_points"] = best["points"]
            result["candidate_contours"] = best["contours"]
            result["candidate_budget"] = best["budget"]
            result["candidate_action"] = quality_action(best["metrics"])
            result["candidate_mse"] = "{:.6f}".format(best["metrics"]["mse"])
            result["candidate_ink_iou"] = "{:.6f}".format(best["metrics"]["ink_iou"])
            result["candidate_false_positive_ink"] = "{:.6f}".format(best["metrics"]["false_positive_ink"])
            result["candidate_false_negative_ink"] = "{:.6f}".format(best["metrics"]["false_negative_ink"])
            result["candidate_component_delta"] = best["metrics"]["component_delta"]
            result["candidate_counter_delta"] = best["metrics"]["counter_delta"]
            result["candidate_topology_status"] = best["metrics"]["topology_status"]
            result["candidate_worst_raster_size"] = best["metrics"]["worst_raster_size"]
            result["allocated_points"] = best["points"]
            candidate_topologies = topology_at_sizes(
                best["metrics"]["candidate_sets"], original_bbox
            )
            add_topology_diagnostics(result, "candidate", candidate_topologies)
            result["selected_budget"] = ""
            result["target_points"] = ""
            result["fit_tolerance"] = "{:.6f}".format(best["tolerance"])
            result["fit_mode"] = best["mode"]
            result["curve_segments"] = best["curve_segments"]
            result["line_segments"] = best["line_segments"]
            add_metrics(result, cleaned_metrics)
        elif not result.get("candidate_stop_reason"):
            result["candidate_stop_reason"] = "no-buildable-candidate"
    font.close()
    return result


def worker_main(args):
    started = time.monotonic()
    result = None
    try:
        result = process_glyph(
            Path(args.source), args.worker_glyph, Path(args.worker_sfd), args.pathops_python,
            Path(args.worker_base) if args.worker_base else None,
            Path(args.worker_review_base) if args.worker_review_base else None,
            args.worker_checkpoint_status,
            args.fresh_fit,
        )
    except Exception as exc:
        result = {field: "" for field in REPORT_FIELDS}
        result.update(
            {
                "glyph": args.worker_glyph,
                "worker_status": "error",
                "rebuild_status": "kept-original-error",
                "diff_status": "needs-manual-review",
                "needs_manual_review": "true",
                "overlap_status": "failed",
                "cleanup_status": "failed",
                "cleanup_valid": "false",
                "final_outline": "original",
                "final_overlap_status": "present-or-unknown",
                "quality_action": "error",
                "selection_reason": "worker-error",
                "error": str(exc),
            }
        )
        traceback.print_exc()
    result["worker_seconds"] = "{:.3f}".format(time.monotonic() - started)
    Path(args.worker_json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result.get("worker_status") != "error" else 1


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


CHECKPOINT_FILES = {
    "simplified.ttf": "output",
    "simplified.sfd": "sfd_output",
    "glyph-report.csv": "report",
    "review-candidates.ttf": "review_output",
    "review-candidates.sfd": "review_sfd_output",
    "cleanup-attempts.ttf": "cleanup_output",
    "cleanup-attempts.sfd": "cleanup_sfd_output",
}


def read_checkpoint(directory, source):
    directory = Path(directory).resolve()
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError("checkpoint manifest is missing: {}".format(manifest_path))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_source = str(manifest.get("source", {}).get("sha256", "")).lower()
    if expected_source and sha256(source).lower() != expected_source:
        raise RuntimeError("checkpoint source hash does not match original.ttf")
    paths = {}
    for filename, key in CHECKPOINT_FILES.items():
        path = directory / filename
        expected = str(manifest.get("files", {}).get(filename, "")).lower()
        if not path.exists() or (expected and sha256(path).lower() != expected):
            raise RuntimeError("checkpoint file failed validation: {}".format(path))
        paths[key] = path
    paths["directory"] = directory
    paths["manifest"] = manifest
    paths["manifest_sha256"] = sha256(manifest_path)
    return paths


def write_checkpoint(args, destination):
    destination = Path(destination).resolve()
    source = Path(args.source).resolve()
    sources = {
        filename: Path(getattr(args, key)).resolve()
        for filename, key in CHECKPOINT_FILES.items()
    }
    for path in sources.values():
        if not path.exists():
            raise RuntimeError("cannot checkpoint missing artifact: {}".format(path))
    if destination.exists():
        read_checkpoint(destination, source)
        print("Checkpoint already exists and is valid: {}".format(destination))
        return 0
    temporary = destination.with_name(destination.name + ".next")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    hashes = {}
    for filename, path in sources.items():
        target = temporary / filename
        shutil.copy2(str(path), str(target))
        hashes[filename] = sha256(target).upper()
    rows = list(csv.DictReader(sources["glyph-report.csv"].open(encoding="utf-8-sig")))
    manifest = {
        "version": destination.name,
        "created": time.strftime("%Y-%m-%d"),
        "source": {"path": "qa/assets/original.ttf", "sha256": sha256(source).upper()},
        "glyphs": len(rows),
        "resolved_glyphs": sum(str(row.get("needs_manual_review", "")).lower() == "false" for row in rows),
        "review_glyphs": sum(str(row.get("needs_manual_review", "")).lower() == "true" for row in rows),
        "files": hashes,
    }
    (temporary / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(str(temporary), str(destination))
    print("Checkpoint written: {}".format(destination))
    return 0


def glyph_stem(name):
    return name.encode("utf-8").hex()


def run_one_worker(
    script, source, glyph_name, temp_dir, timeout, pathops_python,
    donor_source, candidate_source, checkpoint_status, fresh_fit,
):
    stem = glyph_stem(glyph_name)
    json_path = temp_dir / (stem + ".json")
    sfd_path = temp_dir / (stem + ".sfd")
    command = [
        sys.executable, str(script), "--worker", "--source", str(source),
        "--worker-glyph", glyph_name, "--worker-json", str(json_path),
        "--worker-sfd", str(sfd_path), "--pathops-python", pathops_python,
        "--worker-base", str(donor_source),
        "--worker-review-base", str(candidate_source),
        "--worker-checkpoint-status", str(checkpoint_status or ""),
    ]
    if fresh_fit:
        command.append("--fresh-fit")
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "glyph": glyph_name,
            "worker_status": "timeout",
            "rebuild_status": "kept-original-overlap-timeout",
            "diff_status": "needs-manual-review",
            "needs_manual_review": "true",
            "overlap_status": "timeout",
            "cleanup_status": "timeout",
            "cleanup_valid": "false",
            "final_outline": "original",
            "final_overlap_status": "present-or-unknown",
            "quality_action": "timeout",
            "selection_reason": "worker-timeout",
            "worker_seconds": "{:.3f}".format(time.monotonic() - started),
            "error": "worker exceeded {} seconds".format(timeout),
        }
    if json_path.exists():
        result = json.loads(json_path.read_text(encoding="utf-8"))
    else:
        result = {
            "glyph": glyph_name,
            "worker_status": "error",
            "rebuild_status": "kept-original-error",
            "diff_status": "needs-manual-review",
            "needs_manual_review": "true",
            "overlap_status": "failed",
            "cleanup_status": "failed",
            "cleanup_valid": "false",
            "final_outline": "original",
            "final_overlap_status": "present-or-unknown",
            "quality_action": "error",
            "selection_reason": "missing-worker-result",
            "worker_seconds": "{:.3f}".format(time.monotonic() - started),
            "error": completed.stdout[-4000:],
        }
    if completed.returncode and not result.get("error"):
        result["error"] = completed.stdout[-4000:]
    if sfd_path.exists():
        result["sfd_path"] = str(sfd_path)
    return result


def fill_missing_metadata(result, metadata):
    row = {field: result.get(field, "") for field in REPORT_FIELDS}
    for name, value in metadata.items():
        if row.get(name, "") == "":
            row[name] = value
    if row["final_points"] == "":
        row["final_points"] = metadata["old_points"]
    if row["new_points"] == "":
        row["new_points"] = row["final_points"]
    if row["new_contours"] == "":
        row["new_contours"] = metadata["old_contours"]
    if row["absolute_max_points"] == "":
        row["absolute_max_points"] = MAX_POINTS
    return row


def merge_glyph(font, result, path_key="sfd_path"):
    path = result.get(path_key)
    if not path or not os.path.exists(path):
        return False
    rebuilt = fontforge.open(path)
    name = result["glyph"]
    font[name].foreground = rebuilt[name].foreground
    rebuilt.close()
    return True


def write_report(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def refresh_actual_counts(rows, final_path, review_path, selected=None):
    final = fontforge.open(str(final_path))
    for row in rows:
        if selected is not None and row["glyph"] not in selected:
            continue
        actual = point_count(final[row["glyph"]])
        row["final_points"] = actual
        row["new_points"] = actual
        row["new_contours"] = contour_count(final[row["glyph"]])
    final.close()
    review = fontforge.open(str(review_path))
    for row in rows:
        if selected is not None and row["glyph"] not in selected:
            continue
        if row.get("candidate_sfd_path") or row.get("candidate_points"):
            row["candidate_points"] = point_count(review[row["glyph"]])
            row["candidate_contours"] = contour_count(review[row["glyph"]])
    review.close()


def refresh_actual_metrics(
    rows, source_path, final_path, review_path, topology_path=None, selected=None
):
    source = fontforge.open(str(source_path))
    final = fontforge.open(str(final_path))
    topology_font = fontforge.open(str(topology_path or source_path))
    for row in rows:
        if selected is not None and row["glyph"] not in selected:
            continue
        name = row["glyph"]
        original_sets = layer_point_sets(source[name].foreground)
        if not original_sets:
            continue
        bbox = padded_bbox(bbox_of_sets(original_sets))
        original_mask = rasterize(original_sets, bbox)
        original_masks = {
            size: downsample(original_mask, RASTER_SIZE, size) for size in RASTER_SIZES
        }
        topology_sets = layer_point_sets(topology_font[name].foreground)
        topology_mask = rasterize(topology_sets, bbox)
        reference_topologies = topology_at_sizes(topology_sets, bbox)
        metrics = evaluate_candidate(
            final[name].foreground, original_sets, original_mask, original_masks,
            bbox, topology(topology_mask), reference_topologies,
        )
        add_metrics(row, metrics)
        row["topology_reference"] = "canonical-cleaned"
        add_topology_diagnostics(row, "cleaned", reference_topologies)
    source.close()
    final.close()
    topology_font.close()


def audit_glyph(name, has_candidate, source, final, review, topology_font):
    update = {
        "final_points": point_count(final[name]),
        "new_points": point_count(final[name]),
        "new_contours": contour_count(final[name]),
    }
    if has_candidate:
        update["candidate_points"] = point_count(review[name])
        update["candidate_contours"] = contour_count(review[name])
    original_sets = layer_point_sets(source[name].foreground)
    if not original_sets:
        return update
    bbox = padded_bbox(bbox_of_sets(original_sets))
    original_mask = rasterize(original_sets, bbox)
    original_masks = {
        size: downsample(original_mask, RASTER_SIZE, size) for size in RASTER_SIZES
    }
    topology_sets = layer_point_sets(topology_font[name].foreground)
    topology_mask = rasterize(topology_sets, bbox)
    reference_topologies = topology_at_sizes(topology_sets, bbox)
    metrics = evaluate_candidate(
        final[name].foreground, original_sets, original_mask, original_masks,
        bbox, topology(topology_mask), reference_topologies,
    )
    add_metrics(update, metrics)
    add_topology_diagnostics(update, "cleaned", reference_topologies)
    if has_candidate:
        candidate_metrics = evaluate_candidate(
            review[name].foreground, original_sets, original_mask, original_masks,
            bbox, topology(topology_mask), reference_topologies,
        )
        update["candidate_action"] = quality_action(candidate_metrics)
        update["candidate_mse"] = "{:.6f}".format(candidate_metrics["mse"])
        update["candidate_ink_iou"] = "{:.6f}".format(candidate_metrics["ink_iou"])
        update["candidate_false_positive_ink"] = "{:.6f}".format(
            candidate_metrics["false_positive_ink"]
        )
        update["candidate_false_negative_ink"] = "{:.6f}".format(
            candidate_metrics["false_negative_ink"]
        )
        update["candidate_component_delta"] = candidate_metrics["component_delta"]
        update["candidate_counter_delta"] = candidate_metrics["counter_delta"]
        update["candidate_topology_status"] = candidate_metrics["topology_status"]
        update["candidate_worst_raster_size"] = candidate_metrics["worst_raster_size"]
        add_topology_diagnostics(
            update, "candidate", candidate_metrics["candidate_topologies"]
        )
    return update


def audit_worker_main(args):
    request = json.loads(Path(args.audit_request).read_text(encoding="utf-8"))
    source_path = Path(args.source).resolve()
    topology_path = Path(args.audit_topology).resolve()
    source = fontforge.open(str(source_path))
    final = fontforge.open(str(Path(args.audit_final).resolve()))
    review = fontforge.open(str(Path(args.audit_review).resolve()))
    topology_font = source if topology_path == source_path else fontforge.open(str(topology_path))
    updates = {}
    try:
        for item in request:
            name = item["glyph"]
            updates[name] = audit_glyph(
                name, bool(item.get("has_candidate")), source, final, review,
                topology_font,
            )
    finally:
        source.close()
        final.close()
        review.close()
        if topology_font is not source:
            topology_font.close()
    Path(args.audit_result).write_text(
        json.dumps(updates, ensure_ascii=False), encoding="utf-8"
    )
    return 0


def partition_audit_rows(rows, selected, workers):
    indexed = [
        (index, row) for index, row in enumerate(rows)
        if selected is None or row["glyph"] in selected
    ]
    if not indexed:
        return []
    chunk_count = min(max(1, workers), len(indexed))
    chunks = [[] for _ in range(chunk_count)]
    weights = [0] * chunk_count

    def row_weight(row):
        values = []
        for field in ("old_points", "cleaned_points", "final_points", "candidate_points"):
            try:
                values.append(int(float(row.get(field) or 0)))
            except (TypeError, ValueError):
                values.append(0)
        return max(values[:3] or [1]) + values[3]

    for index, row in sorted(indexed, key=lambda item: (-row_weight(item[1]), item[0])):
        target = min(range(chunk_count), key=lambda chunk: (weights[chunk], chunk))
        chunks[target].append((index, row))
        weights[target] += row_weight(row)
    for chunk in chunks:
        chunk.sort(key=lambda item: item[0])
    return [[row for _index, row in chunk] for chunk in chunks]


def run_one_audit_worker(
    script, source, final, review, topology_path, rows, temp_dir, chunk_index, timeout
):
    request_path = temp_dir / "audit-{:02d}-request.json".format(chunk_index)
    result_path = temp_dir / "audit-{:02d}-result.json".format(chunk_index)
    request = [
        {"glyph": row["glyph"], "has_candidate": bool(row.get("candidate_points"))}
        for row in rows
    ]
    request_path.write_text(json.dumps(request), encoding="utf-8")
    command = [
        sys.executable, str(script), "--audit-worker", "--source", str(source),
        "--audit-final", str(final), "--audit-review", str(review),
        "--audit-topology", str(topology_path), "--audit-request", str(request_path),
        "--audit-result", str(result_path),
    ]
    completed = subprocess.run(
        command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=max(120, timeout * max(1, len(rows))), check=False,
    )
    if completed.returncode or not result_path.exists():
        raise RuntimeError(
            "audit chunk {} failed: {}".format(chunk_index, completed.stdout[-4000:])
        )
    return json.loads(result_path.read_text(encoding="utf-8"))


def run_parallel_audit(
    rows, source, final, review, topology_path, selected, workers, script,
    temp_dir, timeout,
):
    chunks = partition_audit_rows(rows, selected, workers)
    if not chunks:
        return
    started = time.monotonic()
    updates = {}
    audited = 0
    print(
        "Auditing {} glyphs in {} isolated processes...".format(
            sum(len(chunk) for chunk in chunks), len(chunks)
        ),
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=len(chunks)) as executor:
        futures = {
            executor.submit(
                run_one_audit_worker, script, source, final, review, topology_path,
                chunk, temp_dir, index, timeout,
            ): (index, len(chunk))
            for index, chunk in enumerate(chunks)
        }
        completed_chunks = 0
        for future in as_completed(futures):
            index, count = futures[future]
            chunk_updates = future.result()
            updates.update(chunk_updates)
            audited += count
            completed_chunks += 1
            print(
                "[audit {}/{}] chunk {} / glyphs {} / elapsed {:.1f}s".format(
                    completed_chunks, len(chunks), index + 1, audited,
                    time.monotonic() - started,
                ),
                flush=True,
            )
    for row in rows:
        if row["glyph"] in updates:
            row.update(updates[row["glyph"]])


def restore_horizontal_metrics(source, target, python_executable):
    helper = ROOT / "tools" / "restore_metadata.py"
    completed = subprocess.run(
        [python_executable, str(helper), str(source), str(target)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=60, check=False,
    )
    if completed.returncode:
        raise RuntimeError("metadata restore failed: {}".format(completed.stdout[-2000:]))


def row_passes_gates(row):
    try:
        return (
            int(row["final_points"]) <= MAX_POINTS
            and float(row["mse"]) <= MAX_MSE
            and float(row["ink_iou"]) >= MIN_INK_IOU
            and float(row["false_positive_ink"]) <= MAX_FALSE_POSITIVE_INK
            and float(row["false_negative_ink"]) <= MAX_FALSE_NEGATIVE_INK
            and int(row["component_delta"]) == 0
            and int(row["counter_delta"]) == 0
        )
    except (KeyError, TypeError, ValueError):
        return False


def row_passes_hard_budget(row):
    try:
        return (
            int(row["final_points"]) <= MAX_POINTS
            and float(row["mse"]) <= HARD_BUDGET_MAX_MSE
            and float(row["ink_iou"]) >= HARD_BUDGET_MIN_INK_IOU
            and float(row["false_positive_ink"]) <= HARD_BUDGET_MAX_FALSE_POSITIVE_INK
            and float(row["false_negative_ink"]) <= HARD_BUDGET_MAX_FALSE_NEGATIVE_INK
            and int(row["component_delta"]) == 0
            and int(row["counter_delta"]) == 0
        )
    except (KeyError, TypeError, ValueError):
        return False


def row_passes_emergency_budget(row):
    try:
        return (
            row["glyph"] in EMERGENCY_BUDGET_GLYPHS
            and int(row["final_points"]) <= MAX_POINTS
            and float(row["ink_iou"]) >= 0.55
            and int(row["component_delta"]) == 0
            and int(row["counter_delta"]) == 0
        )
    except (KeyError, TypeError, ValueError):
        return False


def candidate_row_passes_gates(row):
    try:
        return (
            int(row["candidate_points"]) <= MAX_POINTS
            and float(row["candidate_mse"]) <= MAX_MSE
            and float(row["candidate_ink_iou"]) >= MIN_INK_IOU
            and float(row["candidate_false_positive_ink"]) <= MAX_FALSE_POSITIVE_INK
            and float(row["candidate_false_negative_ink"]) <= MAX_FALSE_NEGATIVE_INK
            and int(row["candidate_component_delta"]) == 0
            and int(row["candidate_counter_delta"]) == 0
        )
    except (KeyError, TypeError, ValueError):
        return False


def candidate_row_passes_hard_budget(row):
    try:
        return (
            int(row["candidate_points"]) <= MAX_POINTS
            and float(row["candidate_mse"]) <= HARD_BUDGET_MAX_MSE
            and float(row["candidate_ink_iou"]) >= HARD_BUDGET_MIN_INK_IOU
            and float(row["candidate_false_positive_ink"])
            <= HARD_BUDGET_MAX_FALSE_POSITIVE_INK
            and float(row["candidate_false_negative_ink"])
            <= HARD_BUDGET_MAX_FALSE_NEGATIVE_INK
            and int(row["candidate_component_delta"]) == 0
            and int(row["candidate_counter_delta"]) == 0
        )
    except (KeyError, TypeError, ValueError):
        return False


def candidate_row_passes_emergency_budget(row):
    try:
        return (
            row["glyph"] in EMERGENCY_BUDGET_GLYPHS
            and int(row["candidate_points"]) <= MAX_POINTS
            and float(row["candidate_ink_iou"]) >= 0.55
            and int(row["candidate_component_delta"]) == 0
            and int(row["candidate_counter_delta"]) == 0
        )
    except (KeyError, TypeError, ValueError):
        return False


def promote_post_generation_passes(
    rows, source, next_output, next_review, next_sfd, python_executable, selected=None
):
    promoted = [
        row for row in rows
        if row["rebuild_status"] == "cleaned-fallback-review"
        and (selected is None or row["glyph"] in selected)
        and row.get("candidate_points")
        and (
            candidate_row_passes_gates(row)
            or candidate_row_passes_hard_budget(row)
            or candidate_row_passes_emergency_budget(row)
        )
    ]
    if not promoted:
        return 0
    font = fontforge.open(str(next_output))
    review = fontforge.open(str(next_review))
    for row in promoted:
        font[row["glyph"]].foreground = review[row["glyph"]].foreground
        row.update(
            rebuild_status="rebuilt-accepted", diff_status="rebuilt-accepted",
            needs_manual_review="false", quality_action="accept-rebuilt",
            selection_reason=(
                "post-generation-candidate-passed-gates"
                if candidate_row_passes_gates(row)
                else "topology-safe-hard-budget-promotion"
                if candidate_row_passes_hard_budget(row)
                else "emergency-topology-safe-budget-promotion"
            ),
            selected_budget=row["candidate_budget"], target_points=row["candidate_budget"],
            final_outline="rebuilt-under-80", final_overlap_status="removed",
            post_generation_action="promoted",
        )
    review.close()
    retry_sfd = next_sfd.with_name(next_sfd.stem + ".promote" + next_sfd.suffix)
    retry_ttf = next_output.with_name(next_output.stem + ".promote" + next_output.suffix)
    font.save(str(retry_sfd))
    font.generate(str(retry_ttf))
    font.close()
    restore_horizontal_metrics(source, retry_ttf, python_executable)
    os.replace(retry_sfd, next_sfd)
    os.replace(retry_ttf, next_output)
    return len(promoted)


def demote_post_generation_failures(
    rows, results, source, next_output, next_sfd, python_executable, selected=None
):
    failed = [
        row for row in rows
        if row["rebuild_status"] in ("rebuilt-accepted", "overlap-cleaned")
        and (selected is None or row["glyph"] in selected)
        and not (
            row_passes_gates(row)
            or row_passes_hard_budget(row)
            or row_passes_emergency_budget(row)
        )
    ]
    if not failed:
        return 0
    font = fontforge.open(str(next_output))
    for row in failed:
        result = results[row["glyph"]]
        cleaned_path = result.get("cleaned_sfd_path")
        if not cleaned_path or not os.path.exists(cleaned_path):
            raise RuntimeError("missing cleaned fallback for {}".format(row["glyph"]))
        row["candidate_points"] = row["final_points"]
        row["candidate_contours"] = row["new_contours"]
        row["candidate_budget"] = row["selected_budget"] or row["target_points"]
        row["candidate_action"] = quality_action(
            {
                "component_delta": int(row["component_delta"]),
                "counter_delta": int(row["counter_delta"]),
                "mse": float(row["mse"]),
                "ink_iou": float(row["ink_iou"]),
                "false_positive_ink": float(row["false_positive_ink"]),
                "false_negative_ink": float(row["false_negative_ink"]),
            }
        )
        for source_name, target_name in (
            ("mse", "candidate_mse"), ("ink_iou", "candidate_ink_iou"),
            ("false_positive_ink", "candidate_false_positive_ink"),
            ("false_negative_ink", "candidate_false_negative_ink"),
            ("component_delta", "candidate_component_delta"),
            ("counter_delta", "candidate_counter_delta"),
            ("topology_status", "candidate_topology_status"),
            ("worst_raster_size", "candidate_worst_raster_size"),
        ):
            row[target_name] = row[source_name]
        rebuilt = fontforge.open(cleaned_path)
        font[row["glyph"]].foreground = rebuilt[row["glyph"]].foreground
        rebuilt.close()
        row.update(
            rebuild_status="cleaned-fallback-review",
            diff_status="needs-manual-review", needs_manual_review="true",
            selection_reason="post-generation-candidate-failed-gates",
            quality_action=row["candidate_action"], selected_budget="", target_points="",
            final_outline="cleaned-fallback-over-80", final_overlap_status="removed",
            post_generation_action="demoted",
        )
    retry_sfd = next_sfd.with_name(next_sfd.stem + ".retry" + next_sfd.suffix)
    retry_ttf = next_output.with_name(next_output.stem + ".retry" + next_output.suffix)
    font.save(str(retry_sfd))
    font.generate(str(retry_ttf))
    font.close()
    restore_horizontal_metrics(source, retry_ttf, python_executable)
    os.replace(retry_sfd, next_sfd)
    os.replace(retry_ttf, next_output)
    return len(failed)


def promote_checkpoint_candidates_only(
    args, checkpoint, source, output, report, sfd_output, review_output,
    review_sfd_output, cleanup_output, cleanup_sfd_output,
):
    """Promote already-audited checkpoint candidates without rerunning fitting."""
    rows = list(csv.DictReader(checkpoint["report"].open(encoding="utf-8-sig")))
    selected = {
        row["glyph"] for row in rows
        if str(row.get("needs_manual_review", "")).lower() == "true"
    }
    for destination, source_path in (
        (output, checkpoint["output"]),
        (sfd_output, checkpoint["sfd_output"]),
        (review_output, checkpoint["review_output"]),
        (review_sfd_output, checkpoint["review_sfd_output"]),
        (cleanup_output, checkpoint["cleanup_output"]),
        (cleanup_sfd_output, checkpoint["cleanup_sfd_output"]),
    ):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
    promoted = promote_post_generation_passes(
        rows, source, output, review_output, sfd_output, args.pathops_python,
        selected,
    )
    audit_temp = Path(tempfile.mkdtemp(prefix="custom-font-promote-audit-"))
    try:
        run_parallel_audit(
            rows, source, output, review_output, source, selected,
            args.audit_workers, Path(__file__).resolve(), audit_temp, args.timeout,
        )
    finally:
        shutil.rmtree(audit_temp, ignore_errors=True)
    write_report(report, rows)
    print("Checkpoint-only promotions: {}".format(promoted))
    print("Manual review glyphs: {}".format(sum(
        str(row.get("needs_manual_review", "")).lower() == "true" for row in rows
    )))
    return 0


def orchestrator_main(args):
    build_started = time.monotonic()
    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    report = Path(args.report).resolve()
    sfd_output = Path(args.sfd_output).resolve()
    review_output = Path(args.review_output).resolve()
    review_sfd_output = Path(args.review_sfd_output).resolve()
    cleanup_output = Path(args.cleanup_output).resolve()
    cleanup_sfd_output = Path(args.cleanup_sfd_output).resolve()
    if args.write_checkpoint:
        return write_checkpoint(args, args.write_checkpoint)
    if args.review_only and not args.resume:
        raise SystemExit("--review-only requires --resume")
    checkpoint = read_checkpoint(args.resume, source) if args.resume else None
    checkpoint_rows = []
    checkpoint_by_glyph = {}
    if checkpoint:
        checkpoint_rows = list(
            csv.DictReader(checkpoint["report"].open(encoding="utf-8-sig"))
        )
        checkpoint_by_glyph = {row["glyph"]: row for row in checkpoint_rows}
    if args.promote_hard_budget_only:
        if not checkpoint:
            raise SystemExit("--promote-hard-budget-only requires --resume")
        return promote_checkpoint_candidates_only(
            args, checkpoint, source, output, report, sfd_output, review_output,
            review_sfd_output, cleanup_output, cleanup_sfd_output,
        )
    if args.refresh_report_only:
        rows = list(csv.DictReader(report.open(encoding="utf-8")))
        audit_temp = Path(tempfile.mkdtemp(prefix="custom-font-audit-"))
        try:
            run_parallel_audit(
                rows, source, output, review_output, source, None,
                args.audit_workers, Path(__file__).resolve(), audit_temp, args.timeout,
            )
            next_report = report.with_name(report.stem + ".next" + report.suffix)
            write_report(next_report, rows)
            os.replace(next_report, report)
        finally:
            shutil.rmtree(audit_temp, ignore_errors=True)
        print("Refreshed actual production and candidate metrics: {}".format(report))
        return 0
    source_hash = sha256(source)
    font = fontforge.open(str(source))
    glyphs = list(font.glyphs())
    glyph_order = [glyph.glyphname for glyph in glyphs]
    if args.glyph:
        selected = set(args.glyph)
    elif args.review_only:
        selected = {
            row["glyph"] for row in checkpoint_rows
            if str(row.get("needs_manual_review", "")).lower() == "true"
        }
    else:
        selected = set(glyph_order)
    unknown = sorted(selected.difference(glyph_order))
    if unknown:
        font.close()
        raise SystemExit("Unknown glyph(s): {}".format(", ".join(unknown)))
    metadata = {}
    for glyph in glyphs:
        codepoint = glyph.unicode if glyph.unicode >= 0 else ""
        metadata[glyph.glyphname] = {
            "glyph": glyph.glyphname,
            "codepoint": codepoint,
            "char": chr(glyph.unicode) if glyph.unicode >= 0 else "",
            "old_points": point_count(glyph),
            "old_contours": contour_count(glyph),
        }
    font.close()

    created_temp = Path(tempfile.mkdtemp(prefix="custom-font-simplify-"))
    script = Path(__file__).resolve()
    results = {}
    try:
        names = [name for name in glyph_order if name in selected]
        print(
            "Profile {}: {} glyph workers / {} audit workers".format(
                args.profile, args.workers, args.audit_workers
            ),
            flush=True,
        )
        print("Processing {} glyphs with {} workers...".format(len(names), args.workers), flush=True)
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(
                    run_one_worker, script, source, name, created_temp, args.timeout,
                    args.pathops_python, checkpoint["output"] if checkpoint else source,
                    checkpoint["review_output"] if checkpoint else source,
                    checkpoint_by_glyph.get(name, {}).get("rebuild_status", "")
                    if checkpoint else "",
                    args.fresh_fit,
                ): name
                for name in names
            }
            completed_count = 0
            timeout_count = 0
            error_count = 0
            worker_phase_started = time.monotonic()
            for future in as_completed(futures):
                name = futures[future]
                completed_count += 1
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "glyph": name,
                        "worker_status": "error",
                        "rebuild_status": "kept-original-error",
                        "diff_status": "needs-manual-review",
                        "needs_manual_review": "true",
                        "quality_action": "error",
                        "selection_reason": "orchestrator-error",
                        "error": str(exc),
                    }
                results[name] = result
                timeout_count += result.get("worker_status") == "timeout"
                error_count += result.get("worker_status") == "error"
                print(
                    "[{}/{}] {}: {} / elapsed {:.1f}s / timeouts {} / errors {}".format(
                        completed_count, len(names), name, result.get("rebuild_status"),
                        time.monotonic() - worker_phase_started, timeout_count, error_count,
                    ),
                    flush=True,
                )

        final_base = checkpoint["sfd_output"] if checkpoint else source
        final_font = fontforge.open(str(final_base))
        rows = []
        accepted = 0
        for name in glyph_order:
            if name not in selected:
                if checkpoint and name in checkpoint_by_glyph:
                    rows.append(dict(checkpoint_by_glyph[name]))
                    continue
                result = {
                    "glyph": name, "worker_status": "not-selected",
                    "rebuild_status": "not-selected", "diff_status": "not-selected",
                    "quality_action": "unchanged", "selection_reason": "not-selected",
                    "overlap_status": "not-run", "cleanup_status": "not-run",
                    "final_outline": "original", "final_overlap_status": "not-checked",
                    "needs_manual_review": "false",
                }
            else:
                result = results[name]
                if checkpoint:
                    if (
                        result.get("worker_status") in ("timeout", "error")
                        and name in checkpoint_by_glyph
                    ):
                        worker_failure = result
                        result = dict(checkpoint_by_glyph[name])
                        result.update(
                            worker_status=worker_failure.get("worker_status", "error"),
                            worker_seconds=worker_failure.get("worker_seconds", ""),
                            error=worker_failure.get("error", ""),
                            selection_reason="checkpoint-preserved-after-{}".format(
                                worker_failure.get("worker_status", "error")
                            ),
                        )
                        result["_checkpoint_preserved"] = True
                    result["checkpoint_version"] = checkpoint["manifest"].get("version", "")
                    result["checkpoint_sha256"] = checkpoint["manifest_sha256"]
                    if result.get("rebuild_status", "").startswith("kept-original"):
                        result["final_outline"] = "checkpoint-fallback"
                        result["selection_reason"] = "checkpoint-fallback-" + result.get("selection_reason", "")
            if result.get("rebuild_status") in (
                "rebuilt-accepted", "overlap-cleaned", "cleaned-fallback-review"
            ) and not result.get("_checkpoint_preserved"):
                if merge_glyph(final_font, result):
                    accepted += 1
                else:
                    result["rebuild_status"] = "kept-original-error"
                    result["diff_status"] = "needs-manual-review"
                    result["needs_manual_review"] = "true"
                    result["quality_action"] = "error"
                    result["error"] = "accepted worker output could not be merged"
            rows.append(fill_missing_metadata(result, metadata[name]))

        output.parent.mkdir(parents=True, exist_ok=True)
        report.parent.mkdir(parents=True, exist_ok=True)
        sfd_output.parent.mkdir(parents=True, exist_ok=True)
        review_output.parent.mkdir(parents=True, exist_ok=True)
        review_sfd_output.parent.mkdir(parents=True, exist_ok=True)
        cleanup_output.parent.mkdir(parents=True, exist_ok=True)
        cleanup_sfd_output.parent.mkdir(parents=True, exist_ok=True)
        next_output = output.with_name(output.stem + ".next" + output.suffix)
        next_report = report.with_name(report.stem + ".next" + report.suffix)
        next_sfd = sfd_output.with_name(sfd_output.stem + ".next" + sfd_output.suffix)
        next_review = review_output.with_name(review_output.stem + ".next" + review_output.suffix)
        next_review_sfd = review_sfd_output.with_name(
            review_sfd_output.stem + ".next" + review_sfd_output.suffix
        )
        next_cleanup = cleanup_output.with_name(cleanup_output.stem + ".next" + cleanup_output.suffix)
        next_cleanup_sfd = cleanup_sfd_output.with_name(
            cleanup_sfd_output.stem + ".next" + cleanup_sfd_output.suffix
        )
        final_font.save(str(next_sfd))
        final_font.generate(str(next_output))
        final_font.close()

        review_base = checkpoint["review_sfd_output"] if checkpoint else source
        review_font = fontforge.open(str(review_base))
        for name in glyph_order:
            result = results.get(name, {})
            if result.get("candidate_sfd_path"):
                if not merge_glyph(review_font, result, "candidate_sfd_path"):
                    result["cleanup_error"] = "review candidate could not be merged"
            elif result.get("rebuild_status") in ("rebuilt-accepted", "overlap-cleaned"):
                merge_glyph(review_font, result)
        review_font.save(str(next_review_sfd))
        review_font.generate(str(next_review))
        review_font.close()

        cleanup_base = checkpoint["cleanup_sfd_output"] if checkpoint else source
        cleanup_font = fontforge.open(str(cleanup_base))
        for name in glyph_order:
            result = results.get(name, {})
            if result.get("cleanup_sfd_path"):
                merge_glyph(cleanup_font, result, "cleanup_sfd_path")
        cleanup_font.save(str(next_cleanup_sfd))
        cleanup_font.generate(str(next_cleanup))
        cleanup_font.close()
        topology_font = fontforge.open(str(final_base))
        for name in names:
            result = results.get(name, {})
            if result.get("cleaned_sfd_path"):
                merge_glyph(topology_font, result, "cleaned_sfd_path")
        next_topology = created_temp / "topology-baseline.ttf"
        topology_font.generate(str(next_topology))
        topology_font.close()
        restore_horizontal_metrics(source, next_output, args.pathops_python)
        restore_horizontal_metrics(source, next_review, args.pathops_python)
        restore_horizontal_metrics(source, next_cleanup, args.pathops_python)
        metric_selection = selected if checkpoint else None
        run_parallel_audit(
            rows, source, next_output, next_review, next_topology,
            metric_selection, args.audit_workers, script, created_temp, args.timeout,
        )
        promoted = promote_post_generation_passes(
            rows, source, next_output, next_review, next_sfd, args.pathops_python,
            metric_selection,
        )
        if promoted:
            run_parallel_audit(
                rows, source, next_output, next_review, next_topology,
                metric_selection, args.audit_workers, script, created_temp, args.timeout,
            )
            print("Post-generation promotions: {}".format(promoted), flush=True)
        demoted = demote_post_generation_failures(
            rows, results, source, next_output, next_sfd, args.pathops_python,
            metric_selection,
        )
        if demoted:
            run_parallel_audit(
                rows, source, next_output, next_review, next_topology,
                metric_selection, args.audit_workers, script, created_temp, args.timeout,
            )
            print("Post-generation demotions: {}".format(demoted), flush=True)
        write_report(next_report, rows)
        if sha256(source) != source_hash:
            raise RuntimeError("source font changed during build")
        os.replace(next_output, output)
        os.replace(next_report, report)
        os.replace(next_sfd, sfd_output)
        os.replace(next_review, review_output)
        os.replace(next_review_sfd, review_sfd_output)
        os.replace(next_cleanup, cleanup_output)
        os.replace(next_cleanup_sfd, cleanup_sfd_output)
        review = sum(str(row["needs_manual_review"]).lower() == "true" for row in rows)
        print("Accepted changed glyphs: {}".format(accepted))
        print("Manual review glyphs: {}".format(review))
        print("Font: {}".format(output))
        print("FontForge source: {}".format(sfd_output))
        print("Review candidates: {}".format(review_output))
        print("Review FontForge source: {}".format(review_sfd_output))
        print("Cleanup attempts: {}".format(cleanup_output))
        print("Cleanup FontForge source: {}".format(cleanup_sfd_output))
        print("Report: {}".format(report))
        print("Total elapsed: {:.1f}s".format(time.monotonic() - build_started))
    finally:
        if args.keep_temp:
            print("Temporary files: {}".format(created_temp))
        else:
            shutil.rmtree(created_temp, ignore_errors=True)
    return 0


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--source", default=str(DEFAULT_SOURCE))
    value.add_argument("--output", default=str(DEFAULT_OUTPUT))
    value.add_argument("--report", default=str(DEFAULT_REPORT))
    value.add_argument("--sfd-output", default=str(DEFAULT_SFD_OUTPUT))
    value.add_argument("--review-output", default=str(DEFAULT_REVIEW_OUTPUT))
    value.add_argument("--review-sfd-output", default=str(DEFAULT_REVIEW_SFD_OUTPUT))
    value.add_argument("--cleanup-output", default=str(DEFAULT_CLEANUP_OUTPUT))
    value.add_argument("--cleanup-sfd-output", default=str(DEFAULT_CLEANUP_SFD_OUTPUT))
    value.add_argument(
        "--pathops-python", default=os.environ.get("PATHOPS_PYTHON", DEFAULT_EXTERNAL_PYTHON),
        help="Regular CPython executable used for the FontTools/PathOps helper.",
    )
    value.add_argument("--glyph", action="append", help="Only process this glyph name; repeat as needed.")
    value.add_argument(
        "--write-checkpoint", metavar="DIRECTORY",
        help="Freeze the current generated artifacts and exit.",
    )
    value.add_argument(
        "--resume", metavar="DIRECTORY",
        help="Use a validated checkpoint as the production and report base.",
    )
    value.add_argument(
        "--review-only", action="store_true",
        help="With --resume, process only rows currently marked for review.",
    )
    value.add_argument(
        "--profile", choices=sorted(BUILD_PROFILES), default=DEFAULT_PROFILE,
        help="Concurrency profile; explicit worker flags override its values.",
    )
    value.add_argument("--workers", type=int, default=None)
    value.add_argument("--audit-workers", type=int, default=None)
    value.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    value.add_argument("--keep-temp", action="store_true")
    value.add_argument(
        "--fresh-fit", action="store_true",
        help="Bypass a resumed failed candidate and rerun structural fitting.",
    )
    value.add_argument(
        "--refresh-report-only", action="store_true",
        help="Recalculate report counts and metrics from the generated TTF files.",
    )
    value.add_argument(
        "--promote-hard-budget-only", action="store_true",
        help="Promote topology-safe checkpoint candidates without fitting workers.",
    )
    value.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    value.add_argument("--worker-glyph", help=argparse.SUPPRESS)
    value.add_argument("--worker-json", help=argparse.SUPPRESS)
    value.add_argument("--worker-sfd", help=argparse.SUPPRESS)
    value.add_argument("--worker-base", help=argparse.SUPPRESS)
    value.add_argument("--worker-review-base", help=argparse.SUPPRESS)
    value.add_argument("--worker-checkpoint-status", help=argparse.SUPPRESS)
    value.add_argument("--audit-worker", action="store_true", help=argparse.SUPPRESS)
    value.add_argument("--audit-request", help=argparse.SUPPRESS)
    value.add_argument("--audit-result", help=argparse.SUPPRESS)
    value.add_argument("--audit-final", help=argparse.SUPPRESS)
    value.add_argument("--audit-review", help=argparse.SUPPRESS)
    value.add_argument("--audit-topology", help=argparse.SUPPRESS)
    return value


def main():
    args = parser().parse_args()
    if args.audit_worker:
        required = (
            args.audit_request, args.audit_result, args.audit_final,
            args.audit_review, args.audit_topology,
        )
        if not all(required):
            raise SystemExit("audit worker arguments are incomplete")
        return audit_worker_main(args)
    if args.worker:
        if not args.worker_glyph or not args.worker_json or not args.worker_sfd:
            raise SystemExit("worker arguments are incomplete")
        return worker_main(args)
    profile_workers, profile_audit_workers = BUILD_PROFILES[args.profile]
    args.workers = profile_workers if args.workers is None else args.workers
    args.audit_workers = (
        profile_audit_workers if args.audit_workers is None else args.audit_workers
    )
    for label, value in (("workers", args.workers), ("audit-workers", args.audit_workers)):
        if not 1 <= value <= MAX_PARALLEL_PROCESSES:
            raise SystemExit("--{} must be between 1 and {}".format(label, MAX_PARALLEL_PROCESSES))
    return orchestrator_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
