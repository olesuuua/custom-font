#!/usr/bin/env python3
"""Build the per-contour source-derived curvature review candidate."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from itertools import product
from pathlib import Path

import fontforge
import psMat


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import restore_curvature as rc
import simplify_font as sf


BASELINE_SFD = ROOT / "checkpoints" / "simplified-v126" / "simplified.sfd"
BASELINE_REPORT = ROOT / "checkpoints" / "simplified-v126" / "glyph-report.csv"
ORIGINAL_TTF = ROOT / "qa" / "assets" / "original.ttf"
NORMALIZED_SFD = ROOT / "fontforge" / "cleanup-attempts.sfd"
PREVIOUS_SFD = ROOT / "fontforge" / "curvature-candidates.sfd"
PREVIOUS_TTF = ROOT / "qa" / "assets" / "curvature-candidates.ttf"
PREVIOUS_REVIEW_SFD = ROOT / "fontforge" / "curvature-review.sfd"
PREVIOUS_REVIEW_TTF = ROOT / "qa" / "assets" / "curvature-review.ttf"
PREVIOUS_REPORT = ROOT / "qa" / "assets" / "curvature-report.csv"
ARCHIVE = ROOT / "checkpoints" / "curvature-v2"

OUTPUT_SFD = ROOT / "fontforge" / "curvature-contour-candidates.sfd"
OUTPUT_TTF = ROOT / "qa" / "assets" / "curvature-contour-candidates.ttf"
REVIEW_SFD = ROOT / "fontforge" / "curvature-contour-review.sfd"
REVIEW_TTF = ROOT / "qa" / "assets" / "curvature-contour-review.ttf"
OUTPUT_REPORT = ROOT / "qa" / "assets" / "curvature-contour-report.csv"
OUTPUT_DETAILS = ROOT / "qa" / "assets" / "curvature-contour-details.json"
OUTPUT_BUILD = ROOT / "qa" / "assets" / "curvature-build.json"
MANUAL_DECISIONS = ROOT / "qa" / "curvature-manual-decisions.json"
FAST_RASTER = ROOT / "tools" / "fast_highres_raster.py"
WORK_CHECKPOINT = ROOT / "checkpoints" / "contour-restoration-work"
CONTOUR_ARCHIVE = ROOT / "checkpoints" / "curvature-v3-pre-terminal"

MIN_ARC_LENGTH_RATIO = 0.015
ARC_COVERAGE_REQUIRED = 0.80
CONTOUR_IMPROVEMENT_REQUIRED = 0.001
CONTOUR_ERROR_TOLERANCE = 0.004
REDUNDANT_ANCHOR_DEVIATION = 2.1
BEAM_WIDTH = 64
MAX_FINALISTS = 2
TERMINAL_TURN_MINIMUM = 75.0
TERMINAL_ADJACENT_RUN_RATIO = 1.5
TERMINAL_P95_LIMIT = 0.08
TERMINAL_MAX_LIMIT = 0.18

ARROW_GLYPHS = {
    "arrowleft", "arrowup", "arrowright", "arrowdown", "arrowboth",
    "arrowupdn", "uni2196", "uni2199", "uni21A6", "arrowdblleft",
    "arrowdblup", "arrowdblright",
}

REPORT_FIELDS = [
    "contour_status", "required_curved_contours", "covered_curved_contours",
    "required_curve_arcs", "covered_curve_arcs", "arc_length_coverage",
    "minimum_contour_improvement", "visible_off_curve_points",
    "freed_straight_points", "added_curve_points", "allocation_method",
    "residual_source_error", "unresolved_reason", "contour_mapping_mode",
    "contour_points", "contour_mse", "contour_ink_iou",
    "contour_false_positive_ink", "contour_false_negative_ink",
    "contour_component_delta", "contour_counter_delta", "contour_audit_status",
    "manual_review_status", "locked_source_sha256", "processing_priority",
    "processing_sequence", "required_terminal_arcs", "covered_terminal_arcs",
    "terminal_arc_coverage", "terminal_boundary_p95", "terminal_boundary_max",
    "terminal_dimension_error", "terminal_unsupported_corners",
    "terminal_correction_method", "altered_contours", "terminal_rejection_history",
]


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def archive_v2():
    files = {
        "curvature-candidates.sfd": PREVIOUS_SFD,
        "curvature-candidates.ttf": PREVIOUS_TTF,
        "curvature-review.sfd": PREVIOUS_REVIEW_SFD,
        "curvature-review.ttf": PREVIOUS_REVIEW_TTF,
        "curvature-report.csv": PREVIOUS_REPORT,
    }
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    manifest_path = ARCHIVE / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for filename, expected in manifest.get("files", {}).items():
            path = ARCHIVE / filename
            if not path.exists() or sha256(path) != expected:
                raise RuntimeError("curvature-v2 archive differs: {}".format(filename))
        return manifest
    manifest = {"version": "curvature-v2", "files": {}}
    for filename, source in files.items():
        if not source.exists():
            raise RuntimeError("missing curvature-v2 source artifact: {}".format(source))
        destination = ARCHIVE / filename
        shutil.copy2(source, destination)
        manifest["files"][filename] = sha256(destination)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def archive_contour_v1():
    """Freeze the diagnostic build that preceded terminal-aware restoration."""
    files = {
        "curvature-contour-review.sfd": REVIEW_SFD,
        "curvature-contour-review.ttf": REVIEW_TTF,
        "curvature-contour-report.csv": OUTPUT_REPORT,
        "curvature-contour-details.json": OUTPUT_DETAILS,
        "curvature-build.json": OUTPUT_BUILD,
    }
    manifest_path = CONTOUR_ARCHIVE / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for filename, expected in manifest.get("files", {}).items():
            path = CONTOUR_ARCHIVE / filename
            if not path.exists() or sha256(path) != expected:
                raise RuntimeError("pre-terminal archive differs: {}".format(filename))
        return manifest
    missing = [str(path) for path in files.values() if not path.exists()]
    if missing:
        raise RuntimeError("missing pre-terminal artifacts: {}".format(" ".join(missing)))
    CONTOUR_ARCHIVE.mkdir(parents=True, exist_ok=True)
    manifest = {"version": "curvature-v3-pre-terminal", "files": {}}
    for filename, source in files.items():
        destination = CONTOUR_ARCHIVE / filename
        shutil.copy2(source, destination)
        manifest["files"][filename] = sha256(destination)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def load_manual_decisions():
    values = json.loads(MANUAL_DECISIONS.read_text(encoding="utf-8"))
    approved = set(values.get("approved_v2_lock", []))
    minor = set(values.get("minor_refinement", []))
    derived = values.get("derived_approved", {})
    overlap = approved & minor
    if overlap:
        raise RuntimeError("manual decision overlap: {}".format(" ".join(sorted(overlap))))
    return values, approved, minor, derived


def layer_hash(layer):
    digest = hashlib.sha256()
    for contour in layer:
        digest.update(b"contour\0")
        for point in contour:
            digest.update(("{:.6f},{:.6f},{};".format(
                float(point.x), float(point.y), int(bool(point.on_curve))
            )).encode("ascii"))
    return digest.hexdigest()


def processing_priority(name, glyph):
    if name in ARROW_GLYPHS:
        return 2, "arrows"
    codepoint = int(getattr(glyph, "unicode", -1))
    if 0 <= codepoint <= 0x10FFFF and unicodedata.category(chr(codepoint)).startswith("L"):
        return 0, "letters"
    return 1, "symbols"


def ordered_work_rows(rows, baseline, target_set):
    indexed = list(enumerate(rows))
    return sorted(indexed, key=lambda item: (
        processing_priority(item[1]["glyph"], baseline[item[1]["glyph"]])[0]
        if item[1]["glyph"] in target_set else 3,
        item[0],
    ))


def contour_records(glyph):
    records = []
    passthrough = []
    for layer_index, contour in enumerate(glyph.foreground):
        points = [(float(point.x), float(point.y)) for point in contour if point.on_curve]
        if len(points) >= 3:
            records.append({
                "layer_index": layer_index,
                "points": points,
                "baseline_off": sum(1 for point in contour if not point.on_curve),
                "contour": contour.dup(),
            })
        else:
            passthrough.append(contour.dup())
    return records, passthrough


def independent_mapping(baseline_sets, source_sets):
    if not baseline_sets:
        return [], "empty"
    if not source_sets:
        return None, "missing-source"
    matched, status = rc.match_contours(source_sets, baseline_sets)
    if matched is not None:
        return matched, "direct"
    bbox = sf.bbox_of_sets(source_sets + baseline_sets)
    scale = math.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1])
    mapping = []
    used = set()
    for baseline in baseline_sets:
        ranked = sorted(
            (rc.contour_match_cost(baseline, source, scale), index, source)
            for index, source in enumerate(source_sets)
        )
        unused = [item for item in ranked if item[1] not in used]
        chosen = (unused or ranked)[0]
        if chosen[0] > 6.0:
            return None, "ambiguous-visible-boundary"
        used.add(chosen[1])
        mapping.append(chosen[2])
    return mapping, "visible-boundary"


def merge_windows(*groups):
    selected = {}
    for group in groups:
        for item in group:
            item = dict(item)
            item.setdefault("point_delta", 0)
            key = (item["index"], item.get("mode", "replacement"))
            current = selected.get(key)
            if current is None or (item["curve_error"], -item["improvement"]) < (
                    current["curve_error"], -current["improvement"]):
                selected[key] = item
    return list(selected.values())


def source_path_for_edge(source_contour, start, end):
    left = rc.nearest_sample([source_contour], start)
    right = rc.nearest_sample([source_contour], end)
    if left is None or right is None:
        return None
    forward = rc.cyclic_indices(len(source_contour), left[2], right[2], True)
    backward = rc.cyclic_indices(len(source_contour), left[2], right[2], False)
    path = min((forward, backward), key=len)
    if len(path) < 3 or len(path) > max(12, len(source_contour) // 2):
        return None
    return [source_contour[index] for index in path]


def three_anchor_windows(points, source_contour):
    if len(points) != 3:
        return []
    windows = []
    for index in range(3):
        previous = points[index]
        following = points[(index + 1) % 3]
        samples = source_path_for_edge(source_contour, previous, following)
        if samples is None:
            continue
        control = rc.least_squares_control(previous, following, samples)
        if control is None or not rc.control_is_forward(previous, control, following, samples):
            continue
        line_error = rc.sampled_rms(previous, following, samples)
        curve_error = rc.sampled_rms(previous, following, samples, control)
        deviation = max(sf.point_line_distance(point, previous, following) for point in samples)
        improvement = 0.0 if line_error <= sf.EPSILON else 1.0 - curve_error / line_error
        if deviation < rc.MIN_LINE_DEVIATION or improvement < rc.MIN_RMS_IMPROVEMENT:
            continue
        windows.append({
            "index": index, "control": control, "line_error": line_error,
            "curve_error": curve_error, "improvement": improvement,
            "score": improvement * max(0.25, deviation), "mode": "edge",
            "point_delta": 1,
        })
    return windows


def edge_curve_windows(points, source_contour):
    windows = []
    for index, start in enumerate(points):
        end = points[(index + 1) % len(points)]
        samples = source_path_for_edge(source_contour, start, end)
        if samples is None:
            continue
        control = rc.least_squares_control(start, end, samples)
        if control is None or not rc.control_is_forward(start, control, end, samples):
            continue
        line_error = rc.sampled_rms(start, end, samples)
        curve_error = rc.sampled_rms(start, end, samples, control)
        deviation = max(sf.point_line_distance(point, start, end) for point in samples)
        improvement = 0.0 if line_error <= sf.EPSILON else 1.0 - curve_error / line_error
        if deviation < rc.MIN_LINE_DEVIATION or improvement < rc.MIN_RMS_IMPROVEMENT:
            continue
        windows.append({
            "index": index, "control": control, "line_error": line_error,
            "curve_error": curve_error, "improvement": improvement,
            "score": improvement * max(0.25, deviation), "mode": "edge",
            "point_delta": 1,
        })
    return windows


def source_path_through(source_contour, anchors):
    mapped = [rc.nearest_sample([source_contour], point) for point in anchors]
    if any(value is None for value in mapped):
        return None
    indices = [value[2] for value in mapped]
    forward = rc.cyclic_indices(len(source_contour), indices[0], indices[-1], True)
    backward = rc.cyclic_indices(len(source_contour), indices[0], indices[-1], False)
    options = [path for path in (forward, backward)
               if all(index in path for index in indices[1:-1])]
    if not options:
        return None
    path = min(options, key=len)
    if len(path) < 4 or len(path) > max(18, len(source_contour) // 2):
        return None
    return [source_contour[index] for index in path]


def long_curve_windows(points, source_contour):
    """Replace two adjacent anchors with one source-fitted quadratic control."""
    windows = []
    count = len(points)
    if count < 5:
        return windows
    for index in range(count):
        start = points[(index - 1) % count]
        end = points[(index + 2) % count]
        samples = source_path_through(
            source_contour, [start, points[index], points[(index + 1) % count], end])
        if samples is None:
            continue
        control = rc.least_squares_control(start, end, samples)
        if control is None or not rc.control_is_forward(start, control, end, samples):
            continue
        line_error = rc.sampled_rms(start, end, samples)
        curve_error = rc.sampled_rms(start, end, samples, control)
        deviation = max(sf.point_line_distance(point, start, end) for point in samples)
        improvement = 0.0 if line_error <= sf.EPSILON else 1.0 - curve_error / line_error
        if deviation < rc.MIN_LINE_DEVIATION or improvement < rc.MIN_RMS_IMPROVEMENT:
            continue
        windows.append({
            "index": index, "control": control, "line_error": line_error,
            "curve_error": curve_error, "improvement": improvement,
            "score": improvement * max(0.25, deviation), "mode": "long",
            "point_delta": -1,
        })
    return windows


def straight_removal_windows(points, source_contour):
    removals = []
    for index in range(len(points)):
        previous = points[(index - 1) % len(points)]
        middle = points[index]
        following = points[(index + 1) % len(points)]
        baseline_deviation = sf.point_line_distance(middle, previous, following)
        chord = (following[0] - previous[0], following[1] - previous[1])
        chord_square = chord[0] ** 2 + chord[1] ** 2
        projection = 0.0 if chord_square <= sf.EPSILON else (
            (middle[0] - previous[0]) * chord[0]
            + (middle[1] - previous[1]) * chord[1]
        ) / chord_square
        if baseline_deviation <= 0.1 and 0.0 < projection < 1.0:
            removals.append({
                "index": index, "mode": "remove", "point_delta": -1,
                "line_error": baseline_deviation, "curve_error": baseline_deviation,
                "improvement": 0.0, "score": -baseline_deviation,
            })
            continue
        samples = rc.source_path_for_window([source_contour], previous, middle, following)
        if samples is None:
            # Exact collinearity in v126 is a safe fallback when correspondence
            # sampling spans too many high-detail source points.  Raster and
            # per-contour gates still validate the resulting removal.
            if baseline_deviation > REDUNDANT_ANCHOR_DEVIATION:
                continue
            removals.append({
                "index": index, "mode": "remove", "point_delta": -1,
                "line_error": baseline_deviation, "curve_error": baseline_deviation,
                "improvement": 0.0, "score": -baseline_deviation,
            })
            continue
        deviation = max(sf.point_line_distance(point, previous, following) for point in samples)
        if deviation > rc.MIN_LINE_DEVIATION:
            continue
        removals.append({
            "index": index, "mode": "remove", "point_delta": -1,
            "line_error": rc.sampled_rms(previous, following, samples),
            "curve_error": rc.sampled_rms(previous, following, samples),
            "improvement": 0.0, "score": -deviation,
        })
    return removals


def line_intersection(start, left_vector, end, right_vector):
    determinant = left_vector[0] * right_vector[1] - left_vector[1] * right_vector[0]
    if abs(determinant) <= sf.EPSILON:
        return None
    delta = (end[0] - start[0], end[1] - start[1])
    left_scale = (delta[0] * right_vector[1] - delta[1] * right_vector[0]) / determinant
    right_scale = (delta[0] * left_vector[1] - delta[1] * left_vector[0]) / determinant
    if left_scale <= 0 or right_scale <= 0:
        return None
    return start[0] + left_vector[0] * left_scale, start[1] + left_vector[1] * left_scale


def tangent_refine_windows(points, source_contour, windows):
    refined = []
    count = len(points)
    for item in windows:
        index = item["index"]
        if item.get("mode") == "edge":
            start = points[index]
            end = points[(index + 1) % count]
            samples = source_path_for_edge(source_contour, start, end)
        elif item.get("mode") == "long":
            start = points[(index - 1) % count]
            end = points[(index + 2) % count]
            samples = source_path_through(
                source_contour,
                [start, points[index], points[(index + 1) % count], end],
            )
        else:
            start = points[(index - 1) % count]
            end = points[(index + 1) % count]
            samples = rc.source_path_for_window(
                [source_contour], start, points[index], end)
        if samples is None or len(samples) < 3:
            refined.append(item)
            continue
        width = max(1.0, sf.distance(start, end))
        depth = max(sf.point_line_distance(point, start, end) for point in samples)
        incoming = (samples[1][0] - samples[0][0], samples[1][1] - samples[0][1])
        outgoing = (samples[-1][0] - samples[-2][0], samples[-1][1] - samples[-2][1])
        turn = rc.angle_between(incoming, outgoing)
        local_turns = [
            rc.angle_between(
                (samples[offset][0] - samples[offset - 1][0],
                 samples[offset][1] - samples[offset - 1][1]),
                (samples[offset + 1][0] - samples[offset][0],
                 samples[offset + 1][1] - samples[offset][1]),
            )
            for offset in range(1, len(samples) - 1)
        ]
        if item.get("mode") == "edge":
            left_run = sf.distance(points[(index - 1) % count], start)
            right_run = sf.distance(end, points[(index + 2) % count])
        elif item.get("mode") == "long":
            left_run = sf.distance(points[(index - 2) % count], start)
            right_run = sf.distance(end, points[(index + 3) % count])
        else:
            left_run = sf.distance(points[(index - 2) % count], start)
            right_run = sf.distance(end, points[(index + 2) % count])
        item = dict(item)
        item.update({
            "terminal": (
                turn >= TERMINAL_TURN_MINIMUM
                and max(local_turns or [0.0]) <= 35.0
                and min(left_run, right_run) >= width * TERMINAL_ADJACENT_RUN_RATIO
                and depth >= width * 0.10
            ),
            "source_turn": turn, "source_width": width,
            "source_depth": depth,
        })
        left = (samples[1][0] - samples[0][0], samples[1][1] - samples[0][1])
        right = (samples[-2][0] - samples[-1][0], samples[-2][1] - samples[-1][1])
        control = line_intersection(start, left, end, right)
        if control is None or not rc.control_is_forward(start, control, end, samples):
            refined.append(item)
            continue
        curve_error = rc.sampled_rms(start, end, samples, control)
        improvement = 0.0 if item["line_error"] <= sf.EPSILON else 1.0 - curve_error / item["line_error"]
        if improvement < rc.MIN_RMS_IMPROVEMENT:
            refined.append(item)
            continue
        value = dict(item)
        value.update({
            "control": control, "curve_error": curve_error,
            "improvement": improvement,
            "score": improvement * max(0.25, item["score"] / max(sf.EPSILON, item["improvement"])),
            "tangent_fitted": True,
        })
        refined.append(value)
    return refined


def cyclic_gap(left, right, count):
    return min((right - left) % count, (left - right) % count)


def terminal_arc_profile(points, source_contour, ordered):
    """Recognize a smooth cap joining two substantial straight-side runs."""
    if source_contour is None or not ordered or len(points) < 5:
        return {"terminal": False}
    count = len(points)
    first = ordered[0]
    last = ordered[-1]
    before = (first - 1) % count
    after = (last + 1) % count
    middle = ordered[len(ordered) // 2]
    mapped = [rc.nearest_sample([source_contour], points[index])
              for index in (before, middle, after)]
    if any(value is None for value in mapped):
        return {"terminal": False}
    source_indices = [value[2] for value in mapped]
    forward = rc.cyclic_indices(len(source_contour), source_indices[0], source_indices[2], True)
    backward = rc.cyclic_indices(len(source_contour), source_indices[0], source_indices[2], False)
    paths = [path for path in (forward, backward) if source_indices[1] in path]
    if not paths:
        return {"terminal": False}
    path = min(paths, key=len)
    samples = [source_contour[index] for index in path]
    if len(samples) < 3:
        return {"terminal": False}
    incoming = (samples[1][0] - samples[0][0], samples[1][1] - samples[0][1])
    outgoing = (samples[-1][0] - samples[-2][0], samples[-1][1] - samples[-2][1])
    turn = rc.angle_between(incoming, outgoing)
    width = max(1.0, sf.distance(samples[0], samples[-1]))
    depth = max(sf.point_line_distance(point, samples[0], samples[-1]) for point in samples)
    left_run = sf.distance(points[before], points[(before - 1) % count])
    right_run = sf.distance(points[after], points[(after + 1) % count])
    terminal = (
        turn >= TERMINAL_TURN_MINIMUM
        and min(left_run, right_run) >= width * TERMINAL_ADJACENT_RUN_RATIO
        and depth >= width * 0.10
    )
    return {
        "terminal": terminal,
        "source_turn": turn,
        "source_width": width,
        "source_depth": depth,
        "source_samples": len(samples),
    }


def cluster_arcs(windows, count, points, source_contour=None):
    indices = sorted({item["index"] for item in windows})
    if not indices:
        return []
    groups = [[indices[0]]]
    for index in indices[1:]:
        if index - groups[-1][-1] <= 2:
            groups[-1].append(index)
        else:
            groups.append([index])
    if len(groups) > 1 and (groups[0][0] + count - groups[-1][-1]) <= 2:
        groups[0] = groups[-1] + groups[0]
        groups.pop()
    diagonal = max(1.0, math.hypot(
        sf.bbox_of_sets([points])[2] - sf.bbox_of_sets([points])[0],
        sf.bbox_of_sets([points])[3] - sf.bbox_of_sets([points])[1],
    ))
    arcs = []
    for arc_index, indices in enumerate(groups):
        ordered = sorted(indices, key=lambda value: (value - indices[0]) % count)
        length = sum(
            sf.distance(points[index], points[(index + 1) % count])
            for index in ordered
        )
        if length < diagonal * MIN_ARC_LENGTH_RATIO:
            continue
        profile = terminal_arc_profile(points, source_contour, ordered)
        arcs.append({"id": arc_index, "indices": set(indices), "length": length, **profile})
    terminal_indices = sorted({item["index"] for item in windows if item.get("terminal")})
    terminal_groups = []
    for index in terminal_indices:
        if terminal_groups and index - terminal_groups[-1][-1] <= 2:
            terminal_groups[-1].append(index)
        else:
            terminal_groups.append([index])
    if len(terminal_groups) > 1 and (
            terminal_groups[0][0] + count - terminal_groups[-1][-1]) <= 2:
        terminal_groups[0] = terminal_groups[-1] + terminal_groups[0]
        terminal_groups.pop()
    for indices in terminal_groups:
        relevant = [item for item in windows if item.get("terminal")
                    and item["index"] in indices]
        expanded = {
            value % count for index in indices for value in (index - 1, index, index + 1)
        }
        arcs.append({
            "id": len(arcs), "indices": expanded,
            "length": sum(sf.distance(points[index], points[(index + 1) % count])
                          for index in expanded),
            "terminal": True,
            "source_turn": max(item.get("source_turn", 0.0) for item in relevant),
            "source_width": max(item.get("source_width", 1.0) for item in relevant),
            "source_depth": max(item.get("source_depth", 0.0) for item in relevant),
        })
    return arcs


def item_coverage(item, arc, count):
    covered = {
        index for index in arc["indices"]
        if cyclic_gap(index, item["index"], count) <= 1
    }
    return covered


def compatible(item, selected, count):
    def omitted(value):
        mode = value.get("mode", "replacement")
        if mode == "edge":
            return set()
        if mode == "long":
            return {value["index"], (value["index"] + 1) % count}
        return {value["index"]}

    def required(value):
        mode = value.get("mode", "replacement")
        if mode == "edge":
            return {value["index"], (value["index"] + 1) % count}
        if mode == "long":
            return {(value["index"] - 1) % count, (value["index"] + 2) % count}
        return {(value["index"] - 1) % count, (value["index"] + 1) % count}

    for current in selected:
        if omitted(item) & (omitted(current) | required(current)):
            return False
        if omitted(current) & required(item):
            return False
        item_edge = item.get("mode") == "edge"
        current_edge = current.get("mode") == "edge"
        if item_edge and current_edge:
            if item["index"] == current["index"]:
                return False
            continue
        if item_edge != current_edge:
            edge = item if item_edge else current
            replacement = current if item_edge else item
            if replacement["index"] in (edge["index"], (edge["index"] + 1) % count):
                return False
            continue
        if cyclic_gap(item["index"], current["index"], count) <= 1:
            return False
    return True


def arc_alternatives(arc, windows, count):
    relevant = [item for item in windows if item_coverage(item, arc, count)]
    if not relevant:
        return []
    orders = [
        sorted(relevant, key=lambda item: (-item["score"], item["curve_error"])),
        sorted(relevant, key=lambda item: (item["curve_error"], -item["score"])),
        sorted(relevant, key=lambda item: (item.get("mode") != "edge", item["curve_error"])),
        sorted(relevant, key=lambda item: (item.get("mode") == "edge", item["curve_error"])),
        sorted(relevant, key=lambda item: item["index"]),
        sorted(relevant, key=lambda item: item["index"], reverse=True),
    ]
    alternatives = []
    required = int(math.ceil(len(arc["indices"]) * ARC_COVERAGE_REQUIRED))
    for order in orders:
        for offset in range(min(4, len(order))):
            rotated = order[offset:] + order[:offset]
            chosen = []
            covered = set()
            for item in rotated:
                if not compatible(item, chosen, count):
                    continue
                gain = item_coverage(item, arc, count) - covered
                if not gain:
                    continue
                chosen.append(item)
                covered.update(gain)
                if len(covered) >= required:
                    break
            if len(covered) < required:
                continue
            if arc.get("terminal") and sum(
                    item.get("mode") != "remove" for item in chosen) < 2:
                continue
            signature = tuple(sorted((item["index"], item.get("mode", "replacement"))
                                     for item in chosen))
            if any(value[0] == signature for value in alternatives):
                continue
            error = sum(item["curve_error"] for item in chosen)
            alternatives.append((signature, chosen, covered, error))
    # Retain lean variants when an item is redundant for the 80% requirement.
    # They are important because a nearby straight anchor may need to be removed
    # to pay for an edge control elsewhere in the glyph.
    expanded = list(alternatives)
    for _signature, chosen, _covered, _error in list(alternatives):
        for dropped in range(len(chosen)):
            trial = chosen[:dropped] + chosen[dropped + 1:]
            covered = set()
            for item in trial:
                covered.update(item_coverage(item, arc, count))
            if len(covered) < required:
                continue
            if arc.get("terminal") and sum(
                    item.get("mode") != "remove" for item in trial) < 2:
                continue
            signature = tuple(sorted((item["index"], item.get("mode", "replacement"))
                                     for item in trial))
            if any(value[0] == signature for value in expanded):
                continue
            expanded.append((signature, trial, covered,
                             sum(item["curve_error"] for item in trial)))
    alternatives = expanded
    alternatives.sort(key=lambda value: (len(value[1]), value[3], value[0]))
    return alternatives[:20]


def build_layer(records, selected_by_contour, passthrough, preserve_by_contour=None):
    layer = fontforge.layer()
    layer.is_quadratic = True
    preserve_by_contour = preserve_by_contour or [False] * len(records)
    for record, selected, preserve in zip(records, selected_by_contour, preserve_by_contour):
        if preserve:
            layer += record["contour"].dup()
            continue
        points = record["points"]
        replacements = {item["index"]: item["control"] for item in selected
                        if item.get("mode", "replacement") == "replacement"}
        long_replacements = {item["index"]: item["control"] for item in selected
                             if item.get("mode") == "long"}
        removals = {item["index"] for item in selected if item.get("mode") == "remove"}
        edges = {item["index"]: item["control"] for item in selected
                 if item.get("mode") == "edge"}
        omitted = set(replacements) | removals | set(long_replacements)
        omitted.update((index + 1) % len(points) for index in long_replacements)
        anchors = [index for index in range(len(points)) if index not in omitted]
        if len(anchors) < 2:
            return None
        start = anchors[0]
        contour = fontforge.contour()
        contour.is_quadratic = True
        contour.moveTo(*points[start])
        current = start
        while True:
            if current in edges:
                endpoint = (current + 1) % len(points)
                if endpoint in omitted:
                    return None
                contour.quadraticTo(edges[current], points[endpoint])
                current = endpoint
            else:
                next_index = (current + 1) % len(points)
                if next_index in omitted:
                    endpoint = (next_index + (2 if next_index in long_replacements else 1)) % len(points)
                    if next_index in long_replacements:
                        contour.quadraticTo(long_replacements[next_index], points[endpoint])
                    elif next_index in replacements:
                        contour.quadraticTo(replacements[next_index], points[endpoint])
                    else:
                        contour.lineTo(*points[endpoint])
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
    return rc.classify_layer(layer)


def contour_error(source, candidate):
    bbox = sf.bbox_of_sets([source, candidate])
    return sf.sampled_delta([source], [candidate], bbox)


def candidate_contour_improvements(records, mapped_source, baseline_glyph, layer):
    baseline_sets = [record["points"] for record in records]
    candidate_sets = sf.layer_point_sets(layer)
    if len(candidate_sets) != len(baseline_sets):
        return None, None
    candidate_sets, _mapping_status = independent_mapping(baseline_sets, candidate_sets)
    if candidate_sets is None:
        return None, None
    improvements = []
    residuals = []
    for baseline, candidate, source in zip(baseline_sets, candidate_sets, mapped_source):
        baseline_error = contour_error(source, baseline)
        candidate_error = contour_error(source, candidate)
        residuals.append(candidate_error)
        if baseline_error <= sf.EPSILON:
            improvements.append(0.0 if candidate_error <= baseline_error + sf.EPSILON else -1.0)
        else:
            improvements.append((baseline_error - candidate_error) / baseline_error)
    return improvements, residuals


def policy_pass(metrics, baseline_metrics):
    if rc.standard_pass(baseline_metrics):
        return rc.standard_pass(metrics), "standard"
    return sf.hard_budget_metrics_pass(metrics), "hard-budget"


def complete_highres_reasons_fast(original_glyph, baseline_glyph, candidate_layer):
    source_sets = sf.layer_point_sets(original_glyph.foreground)
    baseline_sets = sf.layer_point_sets(baseline_glyph.foreground)
    candidate_sets = sf.layer_point_sets(candidate_layer)
    bbox = sf.padded_bbox(sf.bbox_of_sets(source_sets + baseline_sets + candidate_sets))
    work = Path(tempfile.mkdtemp(prefix="curvature-raster-"))
    try:
        payload = {
            "bbox": bbox, "sizes": [512, 1024],
            "sets": {"source": source_sets, "baseline": baseline_sets,
                     "candidate": candidate_sets},
        }
        input_path = work / "input.json"
        input_path.write_text(json.dumps(payload), encoding="utf-8")
        result = subprocess.run(
            [sf.DEFAULT_EXTERNAL_PYTHON, str(FAST_RASTER), str(input_path), str(work / "masks")],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode:
            raise RuntimeError("fast raster failed: {}".format(result.stderr.strip()))
        manifest = json.loads((work / "masks" / "manifest.json").read_text(encoding="utf-8"))
        masks = {(label, size): (work / "masks" / "{}-{}.mask".format(label, size)).read_bytes()
                 for label in ("source", "baseline", "candidate") for size in (512, 1024)}
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
    topology = {(label, size): rc.raw_topology(masks[(label, size)], size)
                for label in ("source", "baseline", "candidate") for size in (512, 1024)}
    source_ink = max(1, sum(masks[("source", 512)]))
    threshold = source_ink * rc.MICRO_REGION_RATIO
    baseline_micro = tuple(sum(area < threshold for area in values)
                           for values in topology[("baseline", 512)])
    candidate_micro = tuple(sum(area < threshold for area in values)
                            for values in topology[("candidate", 512)])
    baseline_extra = rc.largest_diff_region(
        masks[("source", 512)], masks[("baseline", 512)], 512, True) / source_ink
    candidate_extra = rc.largest_diff_region(
        masks[("source", 512)], masks[("candidate", 512)], 512, True) / source_ink
    baseline_missing = rc.largest_diff_region(
        masks[("source", 512)], masks[("baseline", 512)], 512, False) / source_ink
    candidate_missing = rc.largest_diff_region(
        masks[("source", 512)], masks[("candidate", 512)], 512, False) / source_ink
    baseline_p95, baseline_max = rc.boundary_distance(
        masks[("source", 512)], masks[("baseline", 512)], 512)
    candidate_p95, candidate_max = rc.boundary_distance(
        masks[("source", 512)], masks[("candidate", 512)], 512)
    reasons = []
    if any(len(topology[("candidate", size)][kind]) > max(
            len(topology[("source", size)][kind]), len(topology[("baseline", size)][kind]))
           for size in (512, 1024) for kind in (0, 1)) or any(
            right > left for left, right in zip(baseline_micro, candidate_micro)):
        reasons.append("micro-topology")
    if rc.unsupported_cusps_layer(candidate_layer, source_sets) > rc.unsupported_cusps(
            baseline_glyph, source_sets):
        reasons.append("cusp")
    if ((candidate_extra > baseline_extra * rc.LOCAL_REGION_RATIO
         and candidate_extra - baseline_extra > rc.LOCAL_REGION_DELTA)
            or (candidate_missing > baseline_missing * rc.LOCAL_REGION_RATIO
                and candidate_missing - baseline_missing > rc.LOCAL_REGION_DELTA)):
        reasons.append("local-ink")
    if (candidate_p95 - baseline_p95 > rc.BOUNDARY_P95_DELTA
            or candidate_max - baseline_max > rc.BOUNDARY_MAX_DELTA):
        reasons.append("boundary")
    return reasons


def selected_coverage(arcs_by_contour, selected_by_contour, records, preserve_by_contour=None):
    required = covered = 0
    terminal_required = terminal_covered = 0
    terminal_fit = []
    terminal_dimension = []
    terminal_unsupported_corners = 0
    length_required = length_covered = 0.0
    contour_covered = []
    arc_details = []
    preserve_by_contour = preserve_by_contour or [False] * len(records)
    for contour_index, (arcs, selected, record, preserve) in enumerate(zip(
            arcs_by_contour, selected_by_contour, records, preserve_by_contour)):
        contour_ok = True
        for arc in arcs:
            required += 1
            is_terminal = bool(arc.get("terminal"))
            if is_terminal:
                terminal_required += 1
            length_required += arc["length"]
            indices = set(arc["indices"]) if preserve else set()
            if not preserve:
                for item in selected:
                    if item.get("mode") == "remove":
                        continue
                    indices.update(item_coverage(item, arc, len(record["points"])))
            ratio = len(indices) / float(max(1, len(arc["indices"])))
            ok = ratio >= ARC_COVERAGE_REQUIRED
            if ok:
                covered += 1
                length_covered += arc["length"] * min(1.0, ratio)
            if is_terminal:
                curve_items = [item for item in selected if item.get("mode") != "remove"
                               and item_coverage(item, arc, len(record["points"]))]
                if preserve:
                    terminal_covered += int(ok)
                    terminal_fit.append(0.0)
                    terminal_dimension.append(0.0)
                elif ok and len(curve_items) >= 2:
                    terminal_covered += 1
                    width = max(1.0, float(arc.get("source_width", 1.0)))
                    fit = max([item.get("curve_error", 0.0) for item in curve_items] or [0.0]) / width
                    terminal_fit.append(fit)
                    controls = [item.get("control") for item in curve_items if item.get("control")]
                    if controls:
                        estimated_depth = max(
                            sf.point_line_distance(
                                control,
                                record["points"][(item["index"] - 1) % len(record["points"])],
                                record["points"][(item["index"] + 1) % len(record["points"])],
                            ) * 0.5
                            for item, control in ((value, value.get("control")) for value in curve_items)
                            if control is not None
                        )
                        source_depth = max(1.0, float(arc.get("source_depth", 1.0)))
                        terminal_dimension.append(abs(estimated_depth - source_depth) / source_depth)
                else:
                    terminal_unsupported_corners += 1
            contour_ok = contour_ok and ok
            arc_details.append({
                "contour": contour_index, "arc": arc["id"],
                "required_indices": sorted(arc["indices"]),
                "covered_indices": sorted(indices), "coverage": ratio, "covered": ok,
                "terminal": is_terminal,
                "source_turn": arc.get("source_turn", 0.0),
                "source_width": arc.get("source_width", 0.0),
                "source_depth": arc.get("source_depth", 0.0),
            })
        contour_covered.append(contour_ok if arcs else None)
    return {
        "required_arcs": required, "covered_arcs": covered,
        "coverage": length_covered / max(sf.EPSILON, length_required),
        "contour_covered": contour_covered, "arcs": arc_details,
        "required_terminal_arcs": terminal_required,
        "covered_terminal_arcs": terminal_covered,
        "terminal_coverage": terminal_covered / float(max(1, terminal_required)),
        "terminal_boundary_p95": max(terminal_fit or [0.0]),
        "terminal_boundary_max": max(terminal_fit or [0.0]) * math.sqrt(2.0),
        "terminal_dimension_error": max(terminal_dimension or [0.0]),
        "terminal_unsupported_corners": terminal_unsupported_corners,
    }


def terminal_metrics_pass(coverage):
    if coverage.get("required_terminal_arcs", 0) == 0:
        return True
    return (
        coverage.get("covered_terminal_arcs", 0) == coverage.get("required_terminal_arcs", 0)
        and coverage.get("terminal_boundary_p95", 1.0) <= TERMINAL_P95_LIMIT
        and coverage.get("terminal_boundary_max", 1.0) <= TERMINAL_MAX_LIMIT
        and coverage.get("terminal_dimension_error", 1.0) <= 0.10
        and coverage.get("terminal_unsupported_corners", 1) == 0
    )


def candidate_states(alternatives_by_arc, arc_locations):
    states = [([], 0.0)]
    for alternatives, location in zip(alternatives_by_arc, arc_locations):
        expanded = []
        contour_index, count = location
        for selected, cost in states:
            existing = [item for current_contour, item in selected if current_contour == contour_index]
            for _signature, items, _covered, error in alternatives:
                if any(not compatible(item, existing, count) for item in items):
                    continue
                expanded.append((selected + [(contour_index, item) for item in items], cost + error))
        expanded.sort(key=lambda value: (value[1], len(value[0])))
        buckets = {}
        signatures = set()
        for selected, cost in expanded:
            signature = tuple(sorted((contour, item["index"], item.get("mode", "replacement"))
                                     for contour, item in selected))
            if signature in signatures:
                continue
            signatures.add(signature)
            delta = sum(item.get("point_delta", 0) for _contour, item in selected)
            bucket = buckets.setdefault(delta, [])
            if len(bucket) < 4:
                bucket.append((selected, cost))
        # Keep point-allocation diversity throughout the beam.  Edge controls
        # cost a point while replacement controls free one, so temporarily
        # expensive states must survive long enough to be offset on later arcs.
        deduped = []
        for delta in sorted(buckets, key=lambda value: (abs(value), value)):
            deduped.extend(buckets[delta])
        deduped = deduped[:BEAM_WIDTH]
        states = deduped
        if not states:
            break
    return states


def visible_off_curve_points(layer):
    return sum(
        sum(1 for point in contour if not point.on_curve)
        for contour in layer if sum(1 for point in contour if point.on_curve) >= 3
    )


def target_names(baseline):
    return [glyph.glyphname for glyph in baseline.glyphs() if rc.is_target(glyph)]


def infer_selected_windows(layer, records, windows_by_contour):
    """Associate existing quadratic controls with the source-derived windows.

    This lets the archived v2 candidate enter the new search without granting it
    any coverage merely because it contains an off-curve point.  A control only
    counts when it is close to a window independently derived from source shape.
    """
    contours = [contour for contour in layer if sum(point.on_curve for point in contour) >= 3]
    if len(contours) != len(records):
        return None
    selected = []
    for contour, record, windows in zip(contours, records, windows_by_contour):
        controls = [(point.x, point.y) for point in contour if not point.on_curve]
        bbox = sf.bbox_of_sets([record["points"]])
        diagonal = max(1.0, math.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1]))
        available = list(windows)
        chosen = []
        for control in controls:
            if not available:
                break
            item = min(available, key=lambda value: sf.distance(control, value["control"]))
            if sf.distance(control, item["control"]) > max(3.0, diagonal * 0.08):
                continue
            chosen.append(item)
            available.remove(item)
        selected.append(chosen)
    return selected


def structurally_safe_manual_layer(original_glyph, baseline_glyph, layer, skip_highres=False):
    reasons = []
    if sum(len(contour) for contour in layer) > sf.MAX_POINTS:
        reasons.append("budget")
    proxy = type("Glyph", (), {"foreground": layer})()
    if rc.self_intersection_count(proxy) > rc.self_intersection_count(baseline_glyph):
        reasons.append("self-intersection")
    if not rc.layer_handles_valid(layer) and rc.layer_handles_valid(baseline_glyph.foreground):
        reasons.append("invalid-handle")
    metrics = rc.evaluate(layer, rc.metrics_context(original_glyph))
    if metrics.get("component_delta", 0) or metrics.get("counter_delta", 0):
        reasons.append("topology")
    if not skip_highres:
        highres = complete_highres_reasons_fast(original_glyph, baseline_glyph, layer)
        reasons.extend(value for value in highres if value in ("micro-topology", "cusp"))
    return sorted(set(reasons)), metrics


def manual_approved_result(name, original_glyph, baseline_glyph, approved_glyph, approved_row):
    if (not str(approved_row.get("safe_status", "")).startswith("safe-restored")
            and approved_row.get("safe_status") != "already-source-curved"):
        raise RuntimeError("manual approval {} is not safe in curvature-v2".format(name))
    if approved_row.get("complete_audit_status") not in ("", "pass"):
        raise RuntimeError("manual approval {} lacks a passing archived audit".format(name))
    layer = rc.classify_layer(approved_glyph.foreground.dup())
    reasons, metrics = structurally_safe_manual_layer(
        original_glyph, baseline_glyph, layer, skip_highres=True)
    if reasons:
        raise RuntimeError("manual approval {} failed structural gates: {}".format(
            name, " ".join(reasons)))
    return {
        "status": "manual-approved", "reason": "quality-override-only",
        "layer": layer, "metrics": metrics, "allocation": "locked-curvature-v2",
        "manual_status": "approved-v2-lock", "locked_source": layer_hash(layer),
        "required_contours": 0, "coverage": {
            "required_arcs": 0, "covered_arcs": 0, "coverage": 1.0,
            "contour_covered": [], "arcs": [], "required_terminal_arcs": 0,
            "covered_terminal_arcs": 0, "terminal_coverage": 1.0,
            "terminal_boundary_p95": 0.0, "terminal_boundary_max": 0.0,
            "terminal_dimension_error": 0.0, "terminal_unsupported_corners": 0,
        },
        "details": {"manual_review": "approved-v2-lock", "quality_override": True},
    }


def derive_scaled_glyph(donor_glyph, target_glyph):
    layer = rc.classify_layer(donor_glyph.foreground.dup())
    donor_box = layer.boundingBox()
    target_box = target_glyph.foreground.boundingBox()
    donor_width = max(1.0, donor_box[2] - donor_box[0])
    donor_height = max(1.0, donor_box[3] - donor_box[1])
    target_width = max(1.0, target_box[2] - target_box[0])
    target_height = max(1.0, target_box[3] - target_box[1])
    scale = min(target_width / donor_width, target_height / donor_height)
    layer.transform(psMat.scale(scale))
    scaled_box = layer.boundingBox()
    target_center = (target_box[0] + target_box[2]) * 0.5
    scaled_center = (scaled_box[0] + scaled_box[2]) * 0.5
    layer.transform(psMat.translate(
        target_center - scaled_center,
        target_box[1] - scaled_box[1],
    ))
    return rc.classify_layer(layer), scale


def derived_approved_result(name, original_glyph, baseline_glyph, donor_glyph):
    layer, scale = derive_scaled_glyph(donor_glyph, baseline_glyph)
    reasons, metrics = structurally_safe_manual_layer(original_glyph, baseline_glyph, layer)
    if reasons:
        raise RuntimeError("derived approval {} failed structural gates: {}".format(
            name, " ".join(reasons)))
    return {
        "status": "manual-approved", "reason": "derived-quality-override-only",
        "layer": layer, "metrics": metrics, "allocation": "scaled-approved-donor",
        "manual_status": "derived-approved", "locked_source": layer_hash(layer),
        "required_contours": 0, "coverage": {
            "required_arcs": 0, "covered_arcs": 0, "coverage": 1.0,
            "contour_covered": [], "arcs": [], "required_terminal_arcs": 0,
            "covered_terminal_arcs": 0, "terminal_coverage": 1.0,
            "terminal_boundary_p95": 0.0, "terminal_boundary_max": 0.0,
            "terminal_dimension_error": 0.0, "terminal_unsupported_corners": 0,
        },
        "details": {"manual_review": "derived-approved", "donor": "Z", "scale": scale},
    }


def analyze_and_fit(name, original_glyph, normalized_glyph, baseline_glyph,
                    previous_glyph, previous_row, temporary, allow_previous_seed=True,
                    minor_focus=None):
    records, passthrough = contour_records(baseline_glyph)
    baseline_sets = [record["points"] for record in records]
    normalized_sets = sf.layer_point_sets(normalized_glyph.foreground)
    original_sets = sf.layer_point_sets(original_glyph.foreground)
    normalized_map, normalized_status = independent_mapping(baseline_sets, normalized_sets)
    original_map, original_status = independent_mapping(baseline_sets, original_sets)
    if normalized_map is None and original_map is None:
        return {
            "status": "unresolved-quality", "reason": "contour-mapping",
            "layer": baseline_glyph.foreground.dup(), "details": {
                "mapping": {"normalized": normalized_status, "original": original_status},
            },
        }
    # The cleaned outline is the contour-correspondence reference.  The original
    # remains the raster/visual truth, but a visible-boundary match may reorder
    # near-coincident counters and must not silently replace a direct normalized
    # mapping for per-contour source-error calculations.
    fit_map = normalized_map or original_map
    mapping_mode = "direct" if normalized_status == "direct" and original_status == "direct" else "visible-boundary"
    windows_by_contour = []
    removals_by_contour = []
    arcs_by_contour = []
    baseline_errors = []
    preserve_by_contour = []
    for index, record in enumerate(records):
        groups = []
        if normalized_map is not None:
            groups.append(tangent_refine_windows(
                record["points"], normalized_map[index],
                rc.curve_windows(record["points"], [normalized_map[index]]),
            ))
            groups.append(tangent_refine_windows(
                record["points"], normalized_map[index],
                edge_curve_windows(record["points"], normalized_map[index]),
            ))
            groups.append(tangent_refine_windows(
                record["points"], normalized_map[index],
                long_curve_windows(record["points"], normalized_map[index]),
            ))
        if original_map is not None and (original_status == "direct" or normalized_map is None):
            groups.append(tangent_refine_windows(
                record["points"], original_map[index],
                rc.curve_windows(record["points"], [original_map[index]]),
            ))
            groups.append(tangent_refine_windows(
                record["points"], original_map[index],
                edge_curve_windows(record["points"], original_map[index]),
            ))
            groups.append(tangent_refine_windows(
                record["points"], original_map[index],
                long_curve_windows(record["points"], original_map[index]),
            ))
        windows = merge_windows(*groups)
        arcs = cluster_arcs(
            windows, len(record["points"]), record["points"], fit_map[index])
        windows_by_contour.append(windows)
        removals_by_contour.append(straight_removal_windows(record["points"], fit_map[index]))
        arcs_by_contour.append(arcs)
        error = contour_error(fit_map[index], record["points"])
        baseline_errors.append(error)
        preserve_by_contour.append(bool(
            record["baseline_off"] and (not arcs or error <= CONTOUR_ERROR_TOLERANCE)
        ))

    if minor_focus is not None:
        preserve_by_contour = [
            bool(index != minor_focus or not arcs)
            for index, arcs in enumerate(arcs_by_contour)
        ]

    required_contours = sum(bool(arcs) for arcs in arcs_by_contour)
    required_arcs = sum(len(arcs) for arcs in arcs_by_contour)
    if required_arcs == 0:
        return {
            "status": "source-straight", "reason": "no-material-source-arc",
            "layer": baseline_glyph.foreground.dup(), "metrics": rc.evaluate(
                baseline_glyph.foreground, rc.metrics_context(original_glyph)),
            "coverage": {"required_arcs": 0, "covered_arcs": 0, "coverage": 1.0,
                         "contour_covered": [None] * len(records), "arcs": []},
            "improvements": [0.0] * len(records), "residuals": baseline_errors,
            "required_contours": 0, "mapping_mode": mapping_mode,
            "selected": [[] for _record in records], "preserve": preserve_by_contour,
            "details": {"mapping": {"normalized": normalized_status, "original": original_status}},
        }

    alternatives_by_arc = []
    arc_locations = []
    missing = []
    for contour_index, (record, windows, arcs, preserve) in enumerate(zip(
            records, windows_by_contour, arcs_by_contour, preserve_by_contour)):
        if preserve:
            continue
        for arc in arcs:
            alternatives = arc_alternatives(arc, windows, len(record["points"]))
            if not alternatives:
                missing.append((contour_index, arc["id"]))
            else:
                alternatives_by_arc.append(alternatives)
                arc_locations.append((contour_index, len(record["points"])))
    if missing:
        return {
            "status": "unresolved-budget", "reason": "unrepresentable-arcs",
            "layer": baseline_glyph.foreground.dup(), "required_contours": required_contours,
            "details": {"missing_arcs": missing,
                        "mapping": {"normalized": normalized_status, "original": original_status}},
        }

    states = candidate_states(alternatives_by_arc, arc_locations)
    if not states and all(preserve_by_contour):
        states = [([], 0.0)]
    context = rc.metrics_context(original_glyph)
    baseline_metrics = rc.evaluate(baseline_glyph.foreground, context)
    baseline_intersections = rc.self_intersection_count(baseline_glyph)
    source_sets_for_cusps = sf.layer_point_sets(original_glyph.foreground)
    baseline_cusps = rc.unsupported_cusps(baseline_glyph, source_sets_for_cusps)
    lowres = []
    rejection_counts = {}
    rejection_evidence = {}

    def reject(reason, evidence=None):
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        if evidence is not None and reason not in rejection_evidence:
            rejection_evidence[reason] = evidence

    # Reuse a successful archived shape only after proving that its controls map
    # to source-derived windows and that every required arc is covered.  It is a
    # search seed, not an exemption from any new gate.
    previous_layer = rc.classify_layer(previous_glyph.foreground.dup())
    previous_selected = (
        infer_selected_windows(previous_layer, records, windows_by_contour)
        if allow_previous_seed else None
    )
    if previous_selected is not None:
        previous_coverage = selected_coverage(
            arcs_by_contour, previous_selected, records, preserve_by_contour)
        if (previous_coverage["covered_arcs"] == previous_coverage["required_arcs"]
                and terminal_metrics_pass(previous_coverage)):
            improvements, residuals = candidate_contour_improvements(
                records, fit_map, baseline_glyph, previous_layer)
            if improvements is not None and all(
                preserve_by_contour[index] or not arcs
                or baseline_errors[index] <= CONTOUR_ERROR_TOLERANCE
                or improvements[index] >= CONTOUR_IMPROVEMENT_REQUIRED
                for index, arcs in enumerate(arcs_by_contour)
            ):
                metrics = rc.evaluate(previous_layer, context)
                accepted, policy = policy_pass(metrics, baseline_metrics)
                if (accepted and rc.relative_metrics_pass(metrics, baseline_metrics)
                        and rc.layer_handles_valid(previous_layer)
                        and rc.self_intersection_count(type(
                            "Glyph", (), {"foreground": previous_layer})()) <= baseline_intersections):
                    lowres.append({
                        "layer": previous_layer, "selected": previous_selected,
                        "coverage": previous_coverage, "improvements": improvements,
                        "residuals": residuals, "metrics": metrics, "policy": policy,
                        "cost": -1.0, "allocation": "archived-v2-seed",
                        "highres_cached": previous_row.get("complete_audit_status") == "pass",
                    })

    for state_items, state_cost in states:
        selected = [[] for _record in records]
        for contour_index, item in state_items:
            selected[contour_index].append(item)
        baseline_point_total = sum(len(contour) for contour in baseline_glyph.foreground)
        edge_count = sum(item.get("mode") == "edge" for values in selected for item in values)
        long_count = sum(item.get("mode") == "long" for values in selected for item in values)
        needed = max(0, baseline_point_total + edge_count - long_count - sf.MAX_POINTS)
        removal_choices = sorted(
            ((item["line_error"], contour_index, item)
             for contour_index, values in enumerate(removals_by_contour)
             for item in values),
            key=lambda value: value[0],
        )
        removed_per_contour = [0] * len(records)
        for _error, contour_index, item in removal_choices:
            if needed <= 0:
                break
            if len(records[contour_index]["points"]) - removed_per_contour[contour_index] <= 3:
                continue
            count = len(records[contour_index]["points"])
            trial_values = [value for value in selected[contour_index]
                            if compatible(item, [value], count)]
            trial_values.append(item)
            trial_selected = list(selected)
            trial_selected[contour_index] = trial_values
            trial_coverage = selected_coverage(
                arcs_by_contour, trial_selected, records, preserve_by_contour)
            if trial_coverage["covered_arcs"] != trial_coverage["required_arcs"]:
                continue
            selected[contour_index] = trial_values
            removed_per_contour[contour_index] += 1
            needed -= 1
        if needed:
            reject("budget-no-straight-anchor")
            continue
        layer = build_layer(records, selected, passthrough, preserve_by_contour)
        if layer is None:
            reject("build")
            continue
        actual = sf.roundtrip_candidate(
            original_glyph, layer, temporary,
            "contour-{}-{}".format(sf.glyph_stem(name), len(lowres)),
        )
        actual = rc.classify_layer(actual)
        if sum(len(contour) for contour in actual) > sf.MAX_POINTS:
            reject("budget")
            continue
        if rc.self_intersection_count(type("Glyph", (), {"foreground": actual})()) > baseline_intersections:
            reject("self-intersection")
            continue
        if not rc.layer_handles_valid(actual):
            reject("invalid-handle")
            continue
        if rc.unsupported_cusps_layer(actual, source_sets_for_cusps) > baseline_cusps:
            reject("cusp")
            continue
        coverage = selected_coverage(arcs_by_contour, selected, records, preserve_by_contour)
        if coverage["covered_arcs"] != coverage["required_arcs"]:
            reject("arc-coverage")
            continue
        if not terminal_metrics_pass(coverage):
            reject("terminal-shape", {
                "required": coverage.get("required_terminal_arcs", 0),
                "covered": coverage.get("covered_terminal_arcs", 0),
                "p95": coverage.get("terminal_boundary_p95", 0.0),
                "maximum": coverage.get("terminal_boundary_max", 0.0),
                "dimension_error": coverage.get("terminal_dimension_error", 0.0),
            })
            continue
        improvements, residuals = candidate_contour_improvements(
            records, fit_map, baseline_glyph, actual)
        if improvements is None:
            reject("contour-order")
            continue
        contour_ok = all(
            preserve_by_contour[index] or not arcs
            or baseline_errors[index] <= CONTOUR_ERROR_TOLERANCE
            or improvements[index] >= CONTOUR_IMPROVEMENT_REQUIRED
            for index, arcs in enumerate(arcs_by_contour)
        )
        if not contour_ok:
            reject("contour-improvement", {
                "improvements": improvements, "residuals": residuals,
                "baseline_errors": baseline_errors,
            })
            continue
        metrics = rc.evaluate(actual, context)
        accepted, policy = policy_pass(metrics, baseline_metrics)
        if not accepted:
            reject("absolute-metric")
            continue
        if not rc.relative_metrics_pass(metrics, baseline_metrics):
            reject("relative-metric")
            continue
        lowres.append({
            "layer": actual, "selected": selected, "coverage": coverage,
            "improvements": improvements, "residuals": residuals,
            "metrics": metrics, "policy": policy, "cost": state_cost,
            "allocation": "per-arc-beam-search",
        })
    lowres.sort(key=lambda item: (
        -item["coverage"].get("terminal_coverage", 1.0),
        -item["coverage"]["coverage"], not item.get("highres_cached", False),
        -min(item["improvements"] or [0]), item["metrics"]["sample_delta"], item["cost"],
    ))
    for candidate in lowres[:MAX_FINALISTS]:
        reasons = [] if candidate.get("highres_cached") else complete_highres_reasons_fast(
            original_glyph, baseline_glyph, candidate["layer"])
        if reasons:
            reject("highres-" + "+".join(reasons))
            continue
        candidate.update({
            "status": "complete", "reason": "", "required_contours": required_contours,
            "mapping_mode": mapping_mode, "preserve": preserve_by_contour,
            "details": {
                "mapping": {"normalized": normalized_status, "original": original_status},
                "arcs": candidate["coverage"]["arcs"], "rejections": rejection_counts,
                "rejection_evidence": rejection_evidence,
            },
        })
        return candidate
    if lowres:
        unresolved_result = dict(lowres[0])
        unresolved_result.update({
            "status": "unresolved-quality", "reason": "no-safe-complete-candidate",
            "required_contours": required_contours, "mapping_mode": mapping_mode,
        })
    else:
        unresolved_result = {
            "status": "unresolved-quality", "reason": "no-safe-complete-candidate",
            "layer": baseline_glyph.foreground.dup(), "required_contours": required_contours,
            "mapping_mode": mapping_mode,
        }
    unresolved_result["details"] = {
        "mapping": {"normalized": normalized_status, "original": original_status},
        "arcs": unresolved_result.get("coverage", {}).get("arcs", []),
        "rejections": rejection_counts,
        "rejection_evidence": rejection_evidence,
    }
    return unresolved_result


def add_result_fields(row, result, layer):
    coverage = result.get("coverage", {})
    improvements = result.get("improvements", [])
    residuals = result.get("residuals", [])
    contour_covered = coverage.get("contour_covered", [])
    selected = [item for values in result.get("selected", []) for item in values]
    freed = sum(item.get("mode") in ("remove", "long") for item in selected)
    added = sum(item.get("mode") == "edge" for item in selected)
    row.update({
        "contour_status": result["status"],
        "required_curved_contours": result.get("required_contours", 0),
        "covered_curved_contours": sum(value is True for value in contour_covered),
        "required_curve_arcs": coverage.get("required_arcs", 0),
        "covered_curve_arcs": coverage.get("covered_arcs", 0),
        "arc_length_coverage": "{:.6f}".format(coverage.get("coverage", 0.0)),
        "minimum_contour_improvement": "{:.6f}".format(min(improvements or [0.0])),
        "visible_off_curve_points": visible_off_curve_points(layer),
        "freed_straight_points": freed,
        "added_curve_points": added,
        "allocation_method": result.get("allocation", "per-arc-beam-search"),
        "residual_source_error": "{:.6f}".format(max(residuals or [0.0])),
        "unresolved_reason": result.get("reason", ""),
        "contour_mapping_mode": result.get("mapping_mode", "not-needed"),
        "contour_points": sum(len(contour) for contour in layer),
        "contour_audit_status": "pass" if result["status"] in ("complete", "source-straight") else "fail",
        "manual_review_status": result.get("manual_status", ""),
        "locked_source_sha256": result.get("locked_source", ""),
        "processing_priority": result.get("processing_priority", ""),
        "processing_sequence": result.get("processing_sequence", ""),
        "required_terminal_arcs": coverage.get("required_terminal_arcs", 0),
        "covered_terminal_arcs": coverage.get("covered_terminal_arcs", 0),
        "terminal_arc_coverage": "{:.6f}".format(coverage.get("terminal_coverage", 1.0)),
        "terminal_boundary_p95": "{:.6f}".format(coverage.get("terminal_boundary_p95", 0.0)),
        "terminal_boundary_max": "{:.6f}".format(coverage.get("terminal_boundary_max", 0.0)),
        "terminal_dimension_error": "{:.6f}".format(coverage.get("terminal_dimension_error", 0.0)),
        "terminal_unsupported_corners": coverage.get("terminal_unsupported_corners", 0),
        "terminal_correction_method": (
            "multi-quadratic-source-fit" if coverage.get("required_terminal_arcs", 0)
            else result.get("allocation", "")
        ),
        "altered_contours": result.get("altered_contours", ""),
        "terminal_rejection_history": json.dumps(
            result.get("details", {}).get("rejections", {}), sort_keys=True),
    })
    if result["status"] == "manual-approved":
        row["contour_audit_status"] = "pass"
    metrics = result.get("metrics")
    if metrics:
        row.update({
            "contour_mse": "{:.6f}".format(metrics["mse"]),
            "contour_ink_iou": "{:.6f}".format(metrics["ink_iou"]),
            "contour_false_positive_ink": "{:.6f}".format(metrics["false_positive_ink"]),
            "contour_false_negative_ink": "{:.6f}".format(metrics["false_negative_ink"]),
            "contour_component_delta": metrics["component_delta"],
            "contour_counter_delta": metrics["counter_delta"],
        })


def write_csv(path, rows, baseline_fields):
    fields = list(baseline_fields)
    for field in REPORT_FIELDS:
        if field not in fields:
            fields.append(field)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build(args):
    archive_manifest = archive_v2()
    pre_terminal_archive = archive_contour_v1()
    manual_values, approved_names, minor_names, derived_names = load_manual_decisions()
    original = fontforge.open(str(ORIGINAL_TTF))
    normalized = fontforge.open(str(NORMALIZED_SFD))
    baseline = fontforge.open(str(BASELINE_SFD))
    previous = fontforge.open(str(PREVIOUS_SFD))
    approved = fontforge.open(str(ARCHIVE / "curvature-candidates.ttf"))
    checkpoint_state = WORK_CHECKPOINT / "state.json"
    checkpoint_sfd = WORK_CHECKPOINT / "review.sfd"
    resume_state = None
    if args.resume and checkpoint_state.exists() and checkpoint_sfd.exists():
        resume_state = json.loads(checkpoint_state.read_text(encoding="utf-8"))
    review = fontforge.open(str(checkpoint_sfd if resume_state else BASELINE_SFD))
    safe = fontforge.open(str(BASELINE_SFD))
    baseline_rows = list(csv.DictReader(BASELINE_REPORT.open(encoding="utf-8-sig")))
    previous_rows = {
        row["glyph"]: row for row in csv.DictReader(
            (ARCHIVE / "curvature-report.csv").open(encoding="utf-8-sig"))
    }
    selected_names = set(args.glyph or [])
    targets = target_names(baseline)
    if len(targets) != 213:
        raise RuntimeError("expected 213 targets, found {}".format(len(targets)))
    target_set = set(targets)
    unknown = selected_names - target_set
    if unknown:
        raise RuntimeError("selected glyphs are not curvature targets: {}".format(" ".join(sorted(unknown))))
    decision_names = approved_names | minor_names | set(derived_names)
    unknown_decisions = decision_names - target_set
    if unknown_decisions:
        raise RuntimeError("manual decisions are not curvature targets: {}".format(
            " ".join(sorted(unknown_decisions))))
    temporary = Path(tempfile.mkdtemp(prefix="contour-curvature-"))
    rows = list(resume_state.get("rows", [])) if resume_state else []
    details = (resume_state.get("details") if resume_state else None) or {
        "version": 2, "targets": {}, "archive": archive_manifest,
        "pre_terminal_archive": pre_terminal_archive,
        "manual_decisions": manual_values,
    }
    unresolved = list(resume_state.get("unresolved", [])) if resume_state else []
    completed_names = {row["glyph"] for row in rows}
    if resume_state:
        for row in rows:
            name = row["glyph"]
            if name in approved_names:
                result = manual_approved_result(
                    name, original[name], baseline[name], approved[name],
                    previous_rows.get(name, {}))
                result["processing_priority"] = row.get("processing_priority", "")
                result["processing_sequence"] = row.get("processing_sequence", "")
                review[name].foreground = result["layer"]
                add_result_fields(row, result, result["layer"])
            elif name in derived_names:
                donor_name = derived_names[name]["donor"]
                result = derived_approved_result(
                    name, original[name], baseline[name], approved[donor_name])
                result["processing_priority"] = row.get("processing_priority", "")
                result["processing_sequence"] = row.get("processing_sequence", "")
                review[name].foreground = result["layer"]
                add_result_fields(row, result, result["layer"])
        for row in rows:
            if row.get("contour_status") in ("complete", "source-straight", "manual-approved"):
                safe[row["glyph"]].foreground = review[row["glyph"]].foreground.dup()
    try:
        work_rows = ordered_work_rows(baseline_rows, baseline, target_set)
        for position, (_source_position, baseline_row) in enumerate(work_rows, start=1):
            name = baseline_row["glyph"]
            if name in completed_names:
                continue
            row = dict(baseline_row)
            for field in REPORT_FIELDS:
                row[field] = ""
            if name not in target_set or (selected_names and name not in selected_names):
                status = "not-targeted" if name not in target_set else "not-selected"
                result = {"status": status, "reason": "", "layer": baseline[name].foreground.dup()}
            else:
                priority_number, priority_name = processing_priority(name, baseline[name])
                print("Fitting {} [{}]".format(name, priority_name), flush=True)
                if name in approved_names:
                    result = manual_approved_result(
                        name, original[name], baseline[name], approved[name],
                        previous_rows.get(name, {}))
                elif name in derived_names:
                    donor_name = derived_names[name]["donor"]
                    result = derived_approved_result(
                        name, original[name], baseline[name], approved[donor_name])
                else:
                    fit_baseline = previous[name] if name in minor_names else baseline[name]
                    if name in minor_names:
                        contour_count = len(contour_records(fit_baseline)[0])
                        attempts = [analyze_and_fit(
                            name, original[name], normalized[name], fit_baseline,
                            previous[name], previous_rows.get(name, {}), temporary,
                            allow_previous_seed=False, minor_focus=focus,
                        ) for focus in range(contour_count)]
                        completed_attempts = [value for value in attempts
                                              if value["status"] == "complete"]
                        if completed_attempts:
                            result = min(completed_attempts, key=lambda value: (
                                value.get("metrics", {}).get("sample_delta", 1.0),
                                max(value.get("residuals", [1.0]) or [1.0]),
                                sum(len(contour) for contour in value["layer"]),
                            ))
                        else:
                            result = min(attempts, key=lambda value: (
                                value.get("reason", "") != "no-safe-complete-candidate",
                                sum(len(contour) for contour in value["layer"]),
                            ))
                    else:
                        result = analyze_and_fit(
                            name, original[name], normalized[name], fit_baseline,
                            previous[name], previous_rows.get(name, {}), temporary,
                        )
                    if name in minor_names and result["status"] == "complete":
                        if layer_hash(result["layer"]) == layer_hash(previous[name].foreground):
                            result["status"] = "unresolved-quality"
                            result["reason"] = "minor-refinement-produced-no-change"
                        else:
                            structural, _metrics = structurally_safe_manual_layer(
                                original[name], baseline[name], result["layer"])
                            if structural:
                                result["status"] = "unresolved-quality"
                                result["reason"] = "minor-refinement-structural-{}".format(
                                    "+".join(structural))
                            else:
                                result["manual_status"] = "minor-refined"
                result["processing_priority"] = priority_name
                result["processing_sequence"] = position
                review[name].foreground = result["layer"]
                details["targets"][name] = result.get("details", {})
                details["targets"][name].update({
                    "status": result["status"], "reason": result.get("reason", ""),
                    "required_contours": result.get("required_contours", 0),
                })
                if result["status"] in ("complete", "source-straight", "manual-approved"):
                    safe[name].foreground = result["layer"]
                else:
                    unresolved.append(name)
            add_result_fields(row, result, result["layer"])
            rows.append(row)
            if position % 10 == 0 or position == len(baseline_rows):
                WORK_CHECKPOINT.mkdir(parents=True, exist_ok=True)
                review.save(str(checkpoint_sfd))
                checkpoint_state.write_text(json.dumps({
                    "rows": rows, "details": details, "unresolved": unresolved,
                }, indent=2, sort_keys=True), encoding="utf-8")
                print("Contour restoration {}/{}; unresolved {}".format(
                    position, len(work_rows), len(unresolved)), flush=True)

        row_order = {row["glyph"]: index for index, row in enumerate(baseline_rows)}
        rows.sort(key=lambda value: row_order[value["glyph"]])

        REVIEW_SFD.parent.mkdir(parents=True, exist_ok=True)
        REVIEW_TTF.parent.mkdir(parents=True, exist_ok=True)
        review.save(str(REVIEW_SFD))
        review.generate(str(REVIEW_TTF))
        sf.restore_horizontal_metrics(ORIGINAL_TTF, REVIEW_TTF, sf.DEFAULT_EXTERNAL_PYTHON)
        write_csv(OUTPUT_REPORT, rows, baseline_rows[0].keys())
        OUTPUT_DETAILS.write_text(json.dumps(details, indent=2, sort_keys=True), encoding="utf-8")

        if not unresolved:
            safe.save(str(OUTPUT_SFD))
            safe.generate(str(OUTPUT_TTF))
            sf.restore_horizontal_metrics(ORIGINAL_TTF, OUTPUT_TTF, sf.DEFAULT_EXTERNAL_PYTHON)
            build_manifest = {
                "version": "contour-curvature-v2-terminal",
                "fonts": {
                    "original": sha256(ORIGINAL_TTF),
                    "baseline": sha256(ROOT / "checkpoints" / "simplified-v126" / "simplified.ttf"),
                    "candidate": sha256(OUTPUT_TTF),
                    "review": sha256(REVIEW_TTF),
                },
                "report": sha256(OUTPUT_REPORT), "details": sha256(OUTPUT_DETAILS),
            }
            OUTPUT_BUILD.write_text(json.dumps(build_manifest, indent=2, sort_keys=True), encoding="utf-8")
        else:
            # Emit a reviewable safe font even while promotion remains blocked.
            # Every unresolved glyph is still the exact v126 fallback in `safe`.
            safe.save(str(OUTPUT_SFD))
            safe.generate(str(OUTPUT_TTF))
            sf.restore_horizontal_metrics(ORIGINAL_TTF, OUTPUT_TTF, sf.DEFAULT_EXTERNAL_PYTHON)
            OUTPUT_BUILD.write_text(json.dumps({
                "version": "contour-curvature-v2-terminal-diagnostic",
                "blocked": True, "fallback_count": len(unresolved),
                "unresolved": unresolved,
                "fonts": {
                    "original": sha256(ORIGINAL_TTF),
                    "baseline": sha256(ROOT / "checkpoints" / "simplified-v126" / "simplified.ttf"),
                    "candidate": sha256(OUTPUT_TTF),
                    "review": sha256(REVIEW_TTF),
                },
                "report": sha256(OUTPUT_REPORT), "details": sha256(OUTPUT_DETAILS),
            }, indent=2, sort_keys=True), encoding="utf-8")
            if not args.allow_unresolved:
                raise RuntimeError("unresolved contour targets: {}".format(" ".join(unresolved)))
    finally:
        for font in (original, normalized, baseline, previous, approved, review, safe):
            try:
                font.close()
            except RuntimeError:
                pass
        shutil.rmtree(str(temporary), ignore_errors=True)
    return unresolved


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--archive-only", action="store_true")
    value.add_argument("--glyph", action="append")
    value.add_argument("--allow-unresolved", action="store_true")
    value.add_argument("--resume", action="store_true")
    value.add_argument("--refresh-report", action="store_true")
    value.add_argument("--assemble-safe", action="store_true")
    return value


def assemble_safe_from_report():
    rows = list(csv.DictReader(OUTPUT_REPORT.open(encoding="utf-8-sig")))
    baseline = fontforge.open(str(BASELINE_SFD))
    review = fontforge.open(str(REVIEW_SFD))
    safe = fontforge.open(str(BASELINE_SFD))
    try:
        unresolved = []
        for row in rows:
            name = row["glyph"]
            if row.get("contour_status") in ("complete", "source-straight", "manual-approved"):
                safe[name].foreground = review[name].foreground.dup()
            elif row.get("contour_status", "").startswith("unresolved"):
                unresolved.append(name)
        safe.save(str(OUTPUT_SFD))
        safe.generate(str(OUTPUT_TTF))
        sf.restore_horizontal_metrics(ORIGINAL_TTF, OUTPUT_TTF, sf.DEFAULT_EXTERNAL_PYTHON)
        manifest = json.loads(OUTPUT_BUILD.read_text(encoding="utf-8"))
        manifest.update({
            "version": "contour-curvature-v2-terminal-diagnostic",
            "blocked": bool(unresolved), "fallback_count": len(unresolved),
            "unresolved": unresolved,
        })
        manifest.setdefault("fonts", {})["candidate"] = sha256(OUTPUT_TTF)
        manifest["fonts"]["review"] = sha256(REVIEW_TTF)
        manifest["report"] = sha256(OUTPUT_REPORT)
        manifest["details"] = sha256(OUTPUT_DETAILS)
        OUTPUT_BUILD.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    finally:
        for font in (baseline, review, safe):
            try:
                font.close()
            except RuntimeError:
                pass


def refresh_report_allocations():
    rows = list(csv.DictReader(OUTPUT_REPORT.open(encoding="utf-8-sig")))
    baseline = fontforge.open(str(BASELINE_SFD))
    review = fontforge.open(str(REVIEW_SFD))
    try:
        for row in rows:
            name = row["glyph"]
            if row.get("contour_status") in ("not-targeted", "not-selected"):
                continue
            baseline_points = sum(len(contour) for contour in baseline[name].foreground)
            review_points = sum(len(contour) for contour in review[name].foreground)
            baseline_off = sum(not point.on_curve for contour in baseline[name].foreground for point in contour)
            review_off = sum(not point.on_curve for contour in review[name].foreground for point in contour)
            added = max(0, review_off - baseline_off)
            freed = max(0, baseline_points + added - review_points)
            row["added_curve_points"] = str(added)
            row["freed_straight_points"] = str(freed)
        write_csv(OUTPUT_REPORT, rows, rows[0].keys())
        unresolved = [row["glyph"] for row in rows
                      if row.get("contour_status", "").startswith("unresolved")]
        OUTPUT_BUILD.write_text(json.dumps({
            "version": "contour-curvature-v1-diagnostic",
            "blocked": bool(unresolved), "unresolved": unresolved,
            "fonts": {
                "original": sha256(ORIGINAL_TTF),
                "baseline": sha256(ROOT / "checkpoints" / "simplified-v126" / "simplified.ttf"),
                "review": sha256(REVIEW_TTF),
            },
            "report": sha256(OUTPUT_REPORT), "details": sha256(OUTPUT_DETAILS),
        }, indent=2, sort_keys=True), encoding="utf-8")
    finally:
        baseline.close()
        review.close()


if __name__ == "__main__":
    arguments = parser().parse_args()
    archive_v2()
    archive_contour_v1()
    if arguments.assemble_safe:
        assemble_safe_from_report()
    elif arguments.refresh_report:
        refresh_report_allocations()
    elif not arguments.archive_only:
        build(arguments)
