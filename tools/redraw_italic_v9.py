#!/usr/bin/env fontforge
"""Build the isolated, smooth, low-point Italic v9 review candidate.

Run with FontForge's Python runtime:

    fontforge -lang=py -script tools/redraw_italic_v9.py --workers 8
    fontforge -lang=py -script tools/redraw_italic_v9.py --glyph A
    fontforge -lang=py -script tools/redraw_italic_v9.py --finalize

The accepted v2 font is immutable input.  Candidates never exceed 100 editable
foreground points; dense v2 outlines are never used as candidate fallbacks.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import re
import statistics
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import fontforge


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf
import simplify_font as sf
import repair_redraw_v3 as rv3
import repair_redraw_v4 as rv4


SOURCE_SFD = ROOT / "fontforge" / "italic-v2-review.sfd"
SOURCE_TTF = ROOT / "output" / "font" / "OlesuasHand-Italic-v2-review.ttf"
V8_BASE_SFD = ROOT / "fontforge" / "italic-v8-redraw-review.sfd"
V6_BASE_SFD = ROOT / "fontforge" / "italic-v6-redraw-review.sfd"
V5_BASE_SFD = ROOT / "fontforge" / "italic-v5-redraw-review.sfd"
REGULAR_FINAL_SFD = ROOT / "regular-final.sfd"
OUTPUT_SFD = ROOT / "fontforge" / "italic-v9-redraw-review.sfd"
OUTPUT_TTF = ROOT / "output" / "font" / "OlesuasHand-Italic-v9-redraw-review.ttf"
FINAL_SFD = ROOT / "fontforge" / "italic-v9.sfd"
FINAL_TTF = ROOT / "output" / "font" / "OlesuasHand-Italic-v9.ttf"
WORK = ROOT / "output" / "italic-v9-redraw"
REPORT = WORK / "italic-v9-glyph-report.csv"
MANIFEST = WORK / "manifest.json"
FAMILY_SOURCE = ROOT / "tools" / "italic_v9_families.json"
FAMILY_REPORT = WORK / "family-registry.json"
DECISIONS = ROOT / "qa" / "italic-v9-manual-decisions.json"
PREVIOUS_DECISIONS = ROOT / "qa" / "italic-v8-manual-decisions.json"
TEMPLATE_MANIFEST = ROOT / "output" / "italic-template" / "italic-template-manifest.json"
XOPP_SOURCE = ROOT / "output" / "pdf" / "olesuas-hand-italic-drawing-template.xopp"
SOURCE_SVG_DIR = WORK / "v2-source-glyph-svg"
CANDIDATE_SVG_DIR = WORK / "v9-final-font-glyph-svg"
WORKERS = WORK / "workers"

MAX_POINTS = 100
TARGET_POINT_BUDGETS = (30, 40, 49, 60, 70, 80, 90, 100)
MIN_CURVE_RATIO = 0.70
MAX_UNPROTECTED_SPACING_CV = 0.55
ALGORITHM_CACHE_VERSION = "v9-feedback-directed-detail-allocation-a6"
ORIGINAL_INFINITY_SVG = (
    ROOT / "output" / "italic-import" / "exact-source-glyph-svg" / "269-infinity.svg"
)
SMOOTH_REVIEW = "almost_done"
DETAIL_REVIEW = "needs_rework"
PURE_SMOOTH_REVIEW = "pure_smooth"
V9_CHANGED_GLYPHS = {
    "Beta", "S", "Theta", "Upsilon", "g", "o", "seveneighths",
    "uni0412", "uni0414", "uni0416", "uni042E", "uni0435",
    "uni0437", "uni0446", "uni044E",
}
REVIEW_MODE_OVERRIDES = {
    "D": PURE_SMOOTH_REVIEW,
    **{name: "" for name in (
        "g", "o", "uni0414", "uni042E", "uni0435", "uni0437",
        "uni0446", "uni044E", "Theta", "Upsilon", "S", "uni0412",
        "uni0416", "Beta", "seveneighths",
    )},
}
FORCE_CLEAN_REGULAR = {"uni042D"}

IMMUTABLES = (
    ROOT / "regular-final.sfd",
    ROOT / "bold-final.sfd",
    ROOT / "fontforge" / "italic-review.sfd",
    SOURCE_SFD,
    SOURCE_TTF,
    V8_BASE_SFD,
    V6_BASE_SFD,
    V5_BASE_SFD,
)

REPORT_FIELDS = [
    "index", "glyph", "codepoint", "source_points", "candidate_points",
    "source_contours", "candidate_contours", "automatic_pass",
    "needs_manual_review", "failure_reasons", "selected_method",
    "source_hash", "editable_hash", "candidate_hash", "source_svg", "candidate_svg",
    "ttf_points", "ttf_contours",
    "width", "left_sidebearing", "right_sidebearing", "bounds",
    "corner_points", "curve_points", "hvcurve_points", "tangent_points",
    "off_curve_points", "on_curve_points", "curve_ratio", "curve_geometry_ratio",
    "spacing_cv", "spacing_max_ratio", "protected_spacing_exceptions",
    "small_contours_preserved", "extrema_preserved", "smoothness_status",
    "self_intersections", "invalid_handles", "topology_status",
    "component_delta", "counter_delta", "boundary_p95", "boundary_max",
    "family", "family_donor", "family_transform", "family_status",
    "candidate_count", "processing_seconds", "worst_raster_size",
]
for _size in rf.RASTER_SIZES:
    REPORT_FIELDS.extend([
        f"mse_{_size}", f"ink_iou_{_size}",
        f"false_positive_ink_{_size}", f"false_negative_ink_{_size}",
    ])
for _size in rf.TOPOLOGY_SIZES:
    REPORT_FIELDS.extend([
        f"source_components_{_size}", f"source_counters_{_size}",
        f"candidate_components_{_size}", f"candidate_counters_{_size}",
    ])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def point_count(layer) -> int:
    return sum(len(contour) for contour in layer)


def contour_bbox(contour) -> tuple[float, float, float, float]:
    values = list(contour)
    return (
        min(point.x for point in values), min(point.y for point in values),
        max(point.x for point in values), max(point.y for point in values),
    )


def layer_bbox(layer) -> tuple[float, float, float, float]:
    boxes = [contour_bbox(contour) for contour in layer]
    if not boxes:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        min(box[0] for box in boxes), min(box[1] for box in boxes),
        max(box[2] for box in boxes), max(box[3] for box in boxes),
    )


def segment_counts(layer) -> tuple[int, int]:
    lines = curves = 0
    for contour in layer:
        values = list(contour)
        for index, point in enumerate(values):
            if not point.on_curve:
                continue
            if values[(index - 1) % len(values)].on_curve:
                lines += 1
            else:
                curves += 1
    return lines, curves


def point_type_metrics(layer) -> dict:
    counts = rf.point_type_counts(layer)
    on_curve = sum(counts[key] for key in ("corner", "curve", "hvcurve", "tangent"))
    smooth = counts["curve"] + counts["hvcurve"]
    return {
        "corner_points": counts["corner"],
        "curve_points": counts["curve"],
        "hvcurve_points": counts["hvcurve"],
        "tangent_points": counts["tangent"],
        "off_curve_points": counts["off"],
        "on_curve_points": on_curve,
        "curve_ratio": smooth / max(1, on_curve),
        "curve_geometry_ratio": (
            counts["off"] + counts["curve"] + counts["hvcurve"] + counts["tangent"]
        ) / max(1, counts["off"] + on_curve),
    }


def spacing_metrics(layer) -> dict:
    contour_cvs = []
    maximum_ratio = 1.0
    protected = 0
    for contour in layer:
        anchors = [point for point in contour if point.on_curve]
        if len(anchors) < 2:
            continue
        pairs = list(zip(anchors, anchors[1:] + anchors[:1]))
        lengths = [math.hypot(second.x - first.x, second.y - first.y) for first, second in pairs]
        positive = [value for value in lengths if value > 1e-6]
        if not positive:
            continue
        median = sorted(positive)[len(positive) // 2]
        unprotected = []
        for value, (first, second) in zip(lengths, pairs):
            exceptional = (
                first.type in (fontforge.splineCorner, fontforge.splineTangent)
                or second.type in (fontforge.splineCorner, fontforge.splineTangent)
                or value < median * .55
            )
            if exceptional:
                protected += 1
            elif value > 1e-6:
                unprotected.append(value)
        measured = unprotected or positive
        mean = sum(measured) / len(measured)
        variance = sum((value - mean) ** 2 for value in measured) / len(measured)
        contour_cvs.append(math.sqrt(variance) / max(mean, 1e-6))
        maximum_ratio = max(maximum_ratio, max(measured) / max(min(measured), 1e-6))
    return {
        "spacing_cv": max(contour_cvs, default=0.0),
        "spacing_max_ratio": maximum_ratio,
        "protected_spacing_exceptions": protected,
    }


def turn_angle(points, index, window=2) -> float:
    before = rf.normalize(rf.subtract(points[index], points[(index - window) % len(points)]))
    after = rf.normalize(rf.subtract(points[(index + window) % len(points)], points[index]))
    value = max(-1.0, min(1.0, rf.dot(before, after)))
    return math.degrees(math.acos(value))


def protected_features(points) -> list[tuple[float, int, str]]:
    if len(points) < 6:
        return [(10.0, index, "small-contour") for index in range(len(points))]
    features = []
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    extrema = {xs.index(min(xs)), xs.index(max(xs)), ys.index(min(ys)), ys.index(max(ys))}
    for index in extrema:
        features.append((20.0, index, "extremum"))
    window = min(3, max(1, len(points) // 30))
    for index in range(len(points)):
        angle = turn_angle(points, index, window)
        if angle >= 38.0:
            features.append((8.0 + angle / 20.0, index, "sharp-corner"))
        elif angle >= 18.0:
            features.append((5.0 + angle / 30.0, index, "tight-curve"))
    return sorted(features, reverse=True)


def perimeter(points) -> float:
    return sum(math.dist(first, second) for first, second in zip(points, points[1:] + points[:1]))


def allocate_segments(point_sets, total_segments: int) -> list[int] | None:
    contour_count = len(point_sets)
    if contour_count == 0:
        return []
    if contour_count * 2 > total_segments:
        return None
    allocation = [2] * contour_count
    remaining = total_segments - sum(allocation)
    weights = []
    for points in point_sets:
        length = perimeter(points)
        # Square-root weighting deliberately gives small contours more points
        # per unit length than large outlines.
        weights.append(math.sqrt(max(length, 1.0)))
    while remaining:
        index = max(
            range(contour_count),
            key=lambda item: weights[item] / max(1, allocation[item]),
        )
        allocation[index] += 1
        remaining -= 1
    return allocation


def uniform_anchors(points, count: int, review_mode="") -> list[int]:
    anchors = {int(round(index * len(points) / count)) % len(points) for index in range(count)}
    while len(anchors) < count:
        anchors.add((max(anchors) + 1) % len(points))
    anchors = set(sorted(anchors)[:count])
    features = protected_features(points)
    # Extrema are mandatory.  Only a bounded number of other features may
    # displace uniform anchors; otherwise a dense source recreates its original
    # point bunching in the supposedly even redraw.
    extrema = [item for item in features if item[2] == "extremum"]
    if review_mode == PURE_SMOOTH_REVIEW:
        details = []
    elif review_mode == SMOOTH_REVIEW:
        details = [
            item for item in features
            if item[2] == "sharp-corner" and item[0] >= 12.0
        ][:max(1, count // 8)]
    elif review_mode == DETAIL_REVIEW:
        details = [item for item in features if item[2] != "extremum"][:max(2, count // 2)]
    else:
        details = [item for item in features if item[2] != "extremum"][:max(1, count // 4)]
    for _score, feature, _kind in extrema + details:
        if feature in anchors:
            continue
        replaceable = sorted(
            anchors,
            key=lambda anchor: min(
                (anchor - feature) % len(points), (feature - anchor) % len(points)
            ),
        )
        if not replaceable:
            break
        anchors.remove(replaceable[0])
        anchors.add(feature)
    return sorted(anchors)


def build_uniform_curve_layer(point_sets, allocation, review_mode=""):
    layer = fontforge.layer()
    layer.is_quadratic = False
    curves = 0
    for points, count in zip(point_sets, allocation):
        anchors = uniform_anchors(points, count, review_mode)
        if review_mode == PURE_SMOOTH_REVIEW:
            protected = set()
        elif review_mode == SMOOTH_REVIEW:
            protected = {
                index for score, index, kind in protected_features(points)
                if kind == "sharp-corner" and score >= 12.0
            }
        elif review_mode == DETAIL_REVIEW:
            protected = {
                index for score, index, kind in protected_features(points)
                if kind == "sharp-corner" and score >= 10.0
            }
        else:
            protected = {
                index for score, index, kind in protected_features(points)
                if kind == "sharp-corner" and score >= 10.0
            }
        tangents = {}
        for offset, anchor in enumerate(anchors):
            previous = points[anchors[(offset - 1) % len(anchors)]]
            following = points[anchors[(offset + 1) % len(anchors)]]
            tangents[anchor] = rf.normalize(rf.subtract(following, previous))
        contour = fontforge.contour()
        contour.is_quadratic = False
        contour.moveTo(*points[anchors[0]])
        for offset, left in enumerate(anchors):
            right = anchors[(offset + 1) % len(anchors)]
            segment = rf.fit_segment(points, left, right)
            start, end = points[left], points[right]
            chord = max(1e-6, math.dist(start, end))
            start_length = max(chord * .05, min(chord * .62, math.dist(start, segment["control1"])))
            end_length = max(chord * .05, min(chord * .62, math.dist(end, segment["control2"])))
            if left in protected:
                control1 = segment["control1"]
            else:
                control1 = rf.add(start, rf.multiply(tangents[left], start_length))
            if right in protected:
                control2 = segment["control2"]
            else:
                control2 = rf.subtract(end, rf.multiply(tangents[right], end_length))
            contour.cubicTo(control1, control2, end)
            curves += 1
        contour.closed = True
        on_curve = [point for point in contour if point.on_curve]
        for point, anchor in zip(on_curve, anchors):
            point.type = fontforge.splineCorner if anchor in protected else fontforge.splineCurve
        layer += contour
    return layer, 0, curves


def enrich_candidate(candidate: dict, method: str) -> dict:
    candidate = dict(candidate)
    candidate["method"] = method
    candidate.update(point_type_metrics(candidate["layer"]))
    candidate.update(spacing_metrics(candidate["layer"]))
    return candidate


def uniform_candidates(source_layer, review_mode="") -> tuple[list[dict], list[list[tuple[float, float]]], dict]:
    point_sets = [
        sf.resample_polyline(contour, 4.0, closed=True)
        for contour in sf.layer_point_sets(source_layer)
    ]
    context = rf.metric_context(point_sets)
    candidates = []
    seen = set()
    for budget in TARGET_POINT_BUDGETS:
        allocation = allocate_segments(point_sets, budget // 3)
        if allocation is None:
            continue
        signature = tuple(allocation)
        if signature in seen:
            continue
        seen.add(signature)
        layer, lines, curves = build_uniform_curve_layer(point_sets, allocation, review_mode)
        candidate = rf.evaluate_layer(point_sets, layer, lines, curves, context)
        candidates.append(enrich_candidate(candidate, f"uniform-cubic-{point_count(layer)}"))
    return candidates, point_sets, context


def base_candidates(source_layer, review_mode="") -> tuple[dict, list[list[tuple[float, float]]], dict, int]:
    uniform, point_sets, context = uniform_candidates(source_layer, review_mode)
    regular, regular_count = rf.redraw_glyph(None, source_layer)
    regular = enrich_candidate(regular, "regular-cubic-ladder")
    # v5 always compares the uniform refits with the established structural
    # redraw.  In v4, any structurally valid uniform candidate masked the
    # structural candidate even when it had much worse raster fidelity or had
    # visually lost a stroke.
    candidates = uniform + [regular]
    passing = [candidate for candidate in candidates if candidate["passes"]]
    preferred = [
        candidate for candidate in passing
        if candidate["curve_ratio"] >= MIN_CURVE_RATIO
        and candidate["spacing_cv"] <= MAX_UNPROTECTED_SPACING_CV
    ]
    clean = [
        candidate for candidate in candidates
        if not candidate["self_intersections"] and not candidate["invalid_handles"]
        and len(candidate["layer"]) == len(source_layer)
    ]
    structurally_valid = [candidate for candidate in clean if candidate["metrics"]["topology_match"]]
    regular_clean = [
        regular for _ in (0,)
        if not regular["self_intersections"] and not regular["invalid_handles"]
        and len(regular["layer"]) == len(source_layer)
    ]
    if review_mode in (SMOOTH_REVIEW, PURE_SMOOTH_REVIEW):
        smooth = [candidate for candidate in structurally_valid if candidate["curve_ratio"] >= .75]
        clean_smooth = [candidate for candidate in clean if candidate["curve_ratio"] >= .75]
        pool = smooth or clean_smooth or structurally_valid or clean or regular_clean or preferred or passing or uniform
    elif review_mode == DETAIL_REVIEW:
        # Rework glyphs are fidelity-first.  Prefer topology-complete, clean
        # outlines and compare every redraw method instead of forcing the
        # evenly distributed candidate simply because it exists.
        pool = structurally_valid or clean or regular_clean or passing or candidates
    else:
        pool = preferred or passing
    if pool:
        if review_mode in (SMOOTH_REVIEW, PURE_SMOOTH_REVIEW):
            selected = min(pool, key=lambda value: (
                rf.candidate_key(value), value["spacing_cv"],
                -value["curve_ratio"], value["points"],
            ))
        elif review_mode == DETAIL_REVIEW:
            selected = min(pool, key=lambda value: (
                rf.candidate_key(value), -value["curve_ratio"],
                value["spacing_cv"],
            ))
        else:
            selected = min(pool, key=lambda value: (
                value["points"], value["spacing_cv"], -value["curve_ratio"],
            ))
    else:
        selected = min(candidates, key=lambda value: (
            rf.candidate_key(value), value["spacing_cv"], -value["curve_ratio"],
        ))
    lines, curves = segment_counts(selected["layer"])
    selected_method = selected["method"]
    selected = rf.evaluate_layer(
        point_sets, selected["layer"], lines, curves, context, full_audit=True,
        max_points=MAX_POINTS,
    )
    selected = enrich_candidate(selected, selected_method)
    return selected, point_sets, context, len(candidates) + regular_count


def affine_for(source_box, target_box, operation: str):
    sx0, sy0, sx1, sy1 = source_box
    tx0, ty0, tx1, ty1 = target_box
    sw, sh = max(1e-6, sx1 - sx0), max(1e-6, sy1 - sy0)
    tw, th = max(1e-6, tx1 - tx0), max(1e-6, ty1 - ty0)
    if operation == "flip-x":
        a, b, c, d = -tw / sw, 0.0, 0.0, th / sh
        e, f = tx1 - a * sx0, ty0 - d * sy0
    elif operation == "flip-y":
        a, b, c, d = tw / sw, 0.0, 0.0, -th / sh
        e, f = tx0 - a * sx0, ty1 - d * sy0
    elif operation == "rotate-180":
        a, b, c, d = -tw / sw, 0.0, 0.0, -th / sh
        e, f = tx1 - a * sx0, ty1 - d * sy0
    elif operation == "rotate-cw":
        a, b, c, d = 0.0, -th / sw, tw / sh, 0.0
        e, f = tx0 - c * sy0, ty1 - b * sx0
    elif operation == "rotate-ccw":
        a, b, c, d = 0.0, th / sw, -tw / sh, 0.0
        e, f = tx1 - c * sy0, ty0 - b * sx0
    else:
        a, b, c, d = tw / sw, 0.0, 0.0, th / sh
        e, f = tx0 - a * sx0, ty0 - d * sy0
    return (a, b, c, d, e, f)


def registered_layer(layer, source_box, target_box, operation="register"):
    result = layer.dup()
    result.transform(affine_for(source_box, target_box, operation))
    return rf.classify_layer(result)


def uniform_transform(source_box, target_box, operation="register"):
    sx0, sy0, sx1, sy1 = source_box
    tx0, ty0, tx1, ty1 = target_box
    sw, sh = max(1e-6, sx1 - sx0), max(1e-6, sy1 - sy0)
    tw, th = max(1e-6, tx1 - tx0), max(1e-6, ty1 - ty0)
    scale = min(tw / sw, th / sh)
    if operation == "rotate-180":
        a, d = -scale, -scale
    elif operation == "flip-x":
        a, d = -scale, scale
    elif operation == "flip-y":
        a, d = scale, -scale
    else:
        a, d = scale, scale
    scx, scy = (sx0 + sx1) / 2.0, (sy0 + sy1) / 2.0
    tcx, tcy = (tx0 + tx1) / 2.0, (ty0 + ty1) / 2.0
    return (a, 0.0, 0.0, d, tcx - a * scx, tcy - d * scy)


def uniform_registered_layer(layer, source_box, target_box, operation="register"):
    result = layer.dup()
    result.transform(uniform_transform(source_box, target_box, operation))
    return rf.classify_layer(result)


def contour_area_box(contour):
    box = contour_bbox(contour)
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def principal_boxes(layer, count: int):
    contours = sorted(layer, key=contour_area_box, reverse=True)[:count]
    return sorted((contour_bbox(contour) for contour in contours), key=lambda box: box[0])


def layer_from_contours(contours):
    layer = fontforge.layer()
    layer.is_quadratic = False
    for contour in contours:
        layer += contour.dup()
    return layer


def cubic_oval(box, reverse=False):
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    rx, ry = (x1 - x0) / 2.0, (y1 - y0) / 2.0
    k = 0.5522847498307936
    contour = fontforge.contour()
    contour.is_quadratic = False
    contour.moveTo(cx + rx, cy)
    contour.cubicTo((cx + rx, cy + k * ry), (cx + k * rx, cy + ry), (cx, cy + ry))
    contour.cubicTo((cx - k * rx, cy + ry), (cx - rx, cy + k * ry), (cx - rx, cy))
    contour.cubicTo((cx - rx, cy - k * ry), (cx - k * rx, cy - ry), (cx, cy - ry))
    contour.cubicTo((cx + k * rx, cy - ry), (cx + rx, cy - k * ry), (cx + rx, cy))
    contour.closed = True
    if reverse:
        contour.reverseDirection()
    for point in contour:
        if point.on_curve:
            point.type = fontforge.splineCurve
    return contour


def evaluate_custom(point_sets, context, layer, method, force_curves=False, max_points=MAX_POINTS):
    layer = rf.classify_layer(layer)
    if force_curves:
        for contour in layer:
            for point in contour:
                if point.on_curve:
                    point.type = fontforge.splineCurve
    lines, curves = segment_counts(layer)
    candidate = rf.evaluate_layer(
        point_sets, layer, lines, curves, context, full_audit=True,
        max_points=max_points,
    )
    return enrich_candidate(candidate, method)


def selected_uniform_at(source_layer, budget: int, review_mode=SMOOTH_REVIEW):
    candidates, _sets, _context = uniform_candidates(source_layer, review_mode)
    eligible = [candidate for candidate in candidates if candidate["points"] <= budget]
    structural = [
        candidate for candidate in eligible
        if not candidate["self_intersections"] and not candidate["invalid_handles"]
        and candidate["metrics"]["topology_match"]
    ]
    return max(structural or eligible or candidates, key=lambda candidate: candidate["points"])


def baseline_smoothing_candidate(baseline_layer, point_sets, context, name):
    """Remove local bumps without reintroducing dense-source irregularities."""
    candidates, _baseline_sets, _baseline_context = uniform_candidates(
        baseline_layer, PURE_SMOOTH_REVIEW,
    )
    lower = 60 if name == "six" else 70
    evaluated = []
    for item in candidates:
        if not lower <= item["points"] <= MAX_POINTS:
            continue
        lines, curves = segment_counts(item["layer"])
        value = rf.evaluate_layer(
            point_sets, item["layer"], lines, curves, context,
            full_audit=True, max_points=MAX_POINTS,
        )
        evaluated.append(enrich_candidate(
            value, f"reviewed-baseline-pure-cubic-smoothing-{item['points']}",
        ))
    clean = [
        item for item in evaluated
        if not item["self_intersections"] and not item["invalid_handles"]
        and len(item["layer"]) == len(baseline_layer)
        and item["metrics"]["topology_match"]
    ]
    pool = clean or [
        item for item in evaluated
        if not item["self_intersections"] and not item["invalid_handles"]
        and len(item["layer"]) == len(baseline_layer)
    ]
    if not pool:
        return None
    return min(pool, key=lambda item: (
        rf.candidate_key(item), item["spacing_cv"], -item["curve_ratio"],
        -item["points"],
    ))


def visible_structure_candidate(source_glyph, source_layer, point_sets, context):
    """Rebuild visible ink topology instead of preserving bad overlaps."""
    candidates = []
    union_layer = rv3.isolated_fontforge_union(source_glyph, source_layer)
    if union_layer is not None and len(union_layer):
        union_candidates, _union_sets, _union_context = uniform_candidates(
            union_layer, DETAIL_REVIEW,
        )
        for item in union_candidates:
            if item["points"] < 70:
                continue
            lines, curves = segment_counts(item["layer"])
            candidate = rf.evaluate_layer(
                point_sets, item["layer"], lines, curves, context,
                full_audit=True, max_points=MAX_POINTS,
            )
            candidates.append(enrich_candidate(
                candidate, f"v2-boolean-union-structure-cubic-{point_count(item['layer'])}",
            ))
    components, counters = context["source_topologies"][128]
    contour_count = max(1, int(components) + int(counters))
    visible = rv3.raster_visible_boundary(point_sets, contour_count, size=1024)
    if len(visible) == contour_count:
        dense = [sf.resample_polyline(contour, 2.0, closed=True) for contour in visible]
        for budget in (70, 80, 90, 100):
            allocation = allocate_segments(dense, budget // 3)
            if allocation is None:
                continue
            layer, lines, curves = build_uniform_curve_layer(
                dense, allocation, PURE_SMOOTH_REVIEW,
            )
            candidate = rf.evaluate_layer(
                point_sets, layer, lines, curves, context,
                full_audit=True, max_points=MAX_POINTS,
            )
            candidates.append(enrich_candidate(
                candidate, f"v2-visible-structure-cubic-{point_count(layer)}",
            ))
    clean = [
        item for item in candidates
        if not item["self_intersections"] and not item["invalid_handles"]
    ]
    if not clean:
        return None
    return min(clean, key=lambda item: (
        rf.candidate_key(item), item["spacing_cv"], -item["curve_ratio"],
    ))


def v9_refit_candidate(baseline_layer, allocation, point_sets, context, method,
                       review_mode=PURE_SMOOTH_REVIEW):
    baseline_sets = [
        sf.resample_polyline(contour, 2.0, closed=True)
        for contour in sf.layer_point_sets(baseline_layer)
    ]
    if len(baseline_sets) != len(allocation):
        return None
    layer, _lines, _curves = build_uniform_curve_layer(
        baseline_sets, allocation, review_mode,
    )
    return evaluate_custom(point_sets, context, layer, method)


def v9_shared_beta_candidate(v6_font, point_sets, context):
    return evaluate_custom(
        point_sets, context, v6_font["uni0412"].foreground.dup(),
        "v9-shared-Beta-uni0412-canonical",
    )


def v9_s_candidate(source_glyph, source_layer, point_sets, context):
    candidate = visible_structure_candidate(
        source_glyph, source_layer, point_sets, context,
    )
    if candidate is not None:
        candidate["method"] = "v9-S-visible-structure-terminal-preserving"
    return candidate


def v9_zhe_candidate(v6_font, point_sets, context):
    layer = v6_font["uni0416"].foreground.dup()
    # Pinch only the three sides of the central junction.  The v6 contour is
    # structurally clean; moving these existing nodes inward restores the
    # small connection detail without refitting (and merging) all six strokes.
    adjustments = (
        ((469.02, 422.68), -6.0),
        ((491.93, 417.44), -5.0),
        ((486.23, 394.40), 5.0),
    )
    for contour in layer:
        for point in contour:
            if not point.on_curve:
                continue
            for (target_x, target_y), delta_y in adjustments:
                if math.hypot(point.x - target_x, point.y - target_y) < 2.0:
                    point.y += delta_y
                    point.type = fontforge.splineCorner
                    break
    return evaluate_custom(
        point_sets, context, layer,
        "v9-Zhe-local-junction-pinch-protected-corners",
    )


def align_selected_smooth_nodes(contour, indices):
    size = len(contour)
    on_indices = [index for index, point in enumerate(contour) if point.on_curve]
    for index in indices:
        if index not in on_indices:
            continue
        offset = on_indices.index(index)
        previous_on = contour[on_indices[(offset - 1) % len(on_indices)]]
        current = contour[index]
        following_on = contour[on_indices[(offset + 1) % len(on_indices)]]
        tangent = rf.normalize((following_on.x - previous_on.x,
                                following_on.y - previous_on.y))
        previous_control = contour[(index - 1) % size]
        following_control = contour[(index + 1) % size]
        previous_length = math.hypot(
            current.x - previous_control.x, current.y - previous_control.y,
        )
        following_length = math.hypot(
            following_control.x - current.x, following_control.y - current.y,
        )
        previous_control.x = current.x - tangent[0] * previous_length
        previous_control.y = current.y - tangent[1] * previous_length
        following_control.x = current.x + tangent[0] * following_length
        following_control.y = current.y + tangent[1] * following_length
        current.type = fontforge.splineCurve


def v9_uni042a_candidate(v6_font, point_sets, context):
    layer = v6_font["uni042A"].foreground.dup()
    # Smooth only the upper loop and its counter.  The rest of the v6 outline
    # is deliberately untouched because a whole-contour refit changes overlap
    # winding and produces filled TTF artifacts.
    align_selected_smooth_nodes(layer[0], (0, 3, 6, 67, 70, 73, 76))
    align_selected_smooth_nodes(layer[2], (0, 3, 6))
    return evaluate_custom(
        point_sets, context, layer,
        "v9-uni042A-top-loop-local-handle-alignment",
    )


NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


def xopp_centerlines(target_name):
    """Return the original pressure-aware pen centerlines in font units."""
    raw = XOPP_SOURCE.read_bytes()
    root = ET.fromstring(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
    manifest = json.loads(TEMPLATE_MANIFEST.read_text(encoding="utf-8"))
    entries = {}
    for entry in manifest["glyphs"]:
        entries.setdefault(int(entry["page"]), []).append(entry)
    pages = [element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "page"]
    result = []
    for page_number, page in enumerate(pages, 1):
        for element in page.iter():
            if element.tag.rsplit("}", 1)[-1] != "stroke":
                continue
            values = [float(value) for value in NUMBER_RE.findall(element.text or "")]
            points = list(zip(values[0::2], values[1::2]))
            if not points:
                continue
            center = (
                sum(point[0] for point in points) / len(points),
                sum(point[1] for point in points) / len(points),
            )
            entry = next((item for item in entries.get(page_number, ()) if
                item["cell_xopp"]["left"] <= center[0] <= item["cell_xopp"]["right"] and
                item["cell_xopp"]["top"] <= center[1] <= item["cell_xopp"]["bottom"]
            ), None)
            if entry is None or entry["glyph_name"] != target_name:
                continue
            width_values = [float(value) for value in NUMBER_RE.findall(element.attrib.get("width", ""))]
            if len(width_values) == len(points) + 1:
                widths = [width_values[0] * value for value in width_values[1:]]
            elif len(width_values) == 1:
                widths = width_values * len(points)
            elif len(width_values) >= len(points):
                widths = width_values[-len(points):]
            else:
                widths = [statistics.median(width_values)] * len(points)
            transform = entry["transform"]
            scale = transform["scale_points_per_font_unit"]
            x_origin = transform["xopp_x_origin"]
            baseline = transform["xopp_baseline"]
            result.append((
                [((x - x_origin) / scale, (baseline - y) / scale) for x, y in points],
                [width / scale for width in widths],
            ))
    if not result:
        raise RuntimeError(f"no XOPP centerlines found for {target_name}")
    return result


def smooth_open_points(points, passes):
    values = list(points)
    for _ in range(passes):
        if len(values) < 5:
            break
        values = [values[0]] + [(
            values[index - 1][0] * .25 + values[index][0] * .5 + values[index + 1][0] * .25,
            values[index - 1][1] * .25 + values[index][1] * .5 + values[index + 1][1] * .25,
        ) for index in range(1, len(values) - 1)] + [values[-1]]
    return values


def open_cubic_centerline(points, segment_count, smooth_passes=2, protected=()):
    """Fit one smooth open cubic while retaining extrema and named details."""
    points = smooth_open_points(points, smooth_passes)
    distances = [0.0]
    for first, second in zip(points, points[1:]):
        distances.append(distances[-1] + math.dist(first, second))
    anchors = {0, len(points) - 1}
    for step in range(1, segment_count):
        target = distances[-1] * step / segment_count
        anchors.add(min(range(len(points)), key=lambda index: abs(distances[index] - target)))
    anchors.update({
        min(range(len(points)), key=lambda index: points[index][0]),
        max(range(len(points)), key=lambda index: points[index][0]),
        min(range(len(points)), key=lambda index: points[index][1]),
        max(range(len(points)), key=lambda index: points[index][1]),
    })
    for target in protected:
        anchors.add(min(range(len(points)), key=lambda index: math.dist(points[index], target)))
    anchors = sorted(anchors)
    contour = fontforge.contour()
    contour.is_quadratic = False
    contour.moveTo(*points[anchors[0]])
    for left, right in zip(anchors, anchors[1:]):
        start, end = points[left], points[right]
        window = max(1, min(5, (right - left) // 3))
        before = points[max(0, left - window)]
        after = points[min(len(points) - 1, left + window)]
        tangent1 = rf.normalize((after[0] - before[0], after[1] - before[1]))
        before = points[max(0, right - window)]
        after = points[min(len(points) - 1, right + window)]
        tangent2 = rf.normalize((after[0] - before[0], after[1] - before[1]))
        chord = math.dist(start, end)
        handle = chord / 3.0
        contour.cubicTo(
            (start[0] + tangent1[0] * handle, start[1] + tangent1[1] * handle),
            (end[0] - tangent2[0] * handle, end[1] - tangent2[1] * handle),
            end,
        )
    contour.closed = False
    return contour


def centerline_stroke_candidate(name, point_sets, context, segments, passes,
                                protected=(), width_scale=1.0, method=""):
    strokes = xopp_centerlines(name)
    result = fontforge.layer()
    result.is_quadratic = False
    for index, (points, widths) in enumerate(strokes):
        # Smooth the dense pen trajectory before expansion.  Keeping its full
        # order here prevents a low-segment centerline fit from opening a loop
        # or moving a junction; point reduction happens on the resolved outline.
        points = smooth_open_points(points, passes[index])
        contour = fontforge.contour()
        contour.is_quadratic = False
        contour.moveTo(*points[0])
        for point in points[1:]:
            contour.lineTo(*point)
        contour.closed = False
        center = fontforge.layer()
        center.is_quadratic = False
        center += contour
        # One robust width per handwritten stroke couples both boundaries and
        # removes the independent-boundary wobble that remained in v7.
        outlined = center.stroke("circular", statistics.median(widths) * width_scale)
        for outlined_contour in outlined:
            result += outlined_contour
    scratch = fontforge.font()
    scratch.encoding = "UnicodeFull"
    temporary = scratch.createChar(-1, "centerlineCandidate")
    try:
        temporary.foreground = result
        temporary.removeOverlap()
        temporary.correctDirection()
        result = temporary.foreground.dup()
    finally:
        scratch.close()
    compact = selected_uniform_at(result, MAX_POINTS, PURE_SMOOTH_REVIEW)
    if compact is not None and point_count(compact["layer"]) <= MAX_POINTS:
        result = compact["layer"]
    else:
        result = simplify_preserving_path(result, MAX_POINTS) or result
    return evaluate_custom(point_sets, context, result, method)


def protected_detail_candidate(build_source_layer, point_sets, context,
                               protected_targets, method):
    """Spend the outline budget at explicitly protected local structures."""
    build_sets = [
        sf.resample_polyline(contour, 2.0, closed=True)
        for contour in sf.layer_point_sets(build_source_layer)
    ]
    seeds = [[] for _ in build_sets]
    for target in protected_targets:
        contour_index, point_index, _distance = min(
            (contour_index, point_index, math.dist(point, target))
            for contour_index, points in enumerate(build_sets)
            for point_index, point in enumerate(points)
        )
        seeds[contour_index].append(point_index)
    ladders = rf.build_ladders(
        build_sets, seed_anchors=seeds, max_points=MAX_POINTS,
        targets=(90, 95, 100),
    )
    candidates = []
    for _count, anchors in ladders:
        layer, _lines, _curves = rf.build_layer(build_sets, anchors)
        candidates.append(evaluate_custom(point_sets, context, layer, method))
    clean = [candidate for candidate in candidates if
             not candidate["self_intersections"] and
             not candidate["invalid_handles"] and
             candidate["metrics"]["topology_match"]]
    return max(clean or candidates, key=lambda candidate: candidate["points"])


def exact_donor_candidate(font, donor_name, point_sets, context, method):
    return evaluate_custom(
        point_sets, context, font[donor_name].foreground.dup(), method,
    )


def biased_detail_candidate(build_source_layer, point_sets, context,
                            allocation, protected_targets, method):
    """Move a fixed cubic budget from broad curves to named local details."""
    build_sets = [
        sf.resample_polyline(contour, 2.0, closed=True)
        for contour in sf.layer_point_sets(build_source_layer)
    ]
    if len(build_sets) != len(allocation):
        raise RuntimeError(f"{method}: contour allocation mismatch")
    protected = [[] for _ in build_sets]
    for target in protected_targets:
        contour_index, point_index, _distance = min(
            (contour_index, point_index, math.dist(point, target))
            for contour_index, points in enumerate(build_sets)
            for point_index, point in enumerate(points)
        )
        protected[contour_index].append(point_index)
    anchors_by_contour = []
    for points, count, requested in zip(build_sets, allocation, protected):
        anchors = set(uniform_anchors(points, count, PURE_SMOOTH_REVIEW))
        requested = list(dict.fromkeys(requested))
        for feature in requested:
            if feature in anchors:
                continue
            replaceable = [anchor for anchor in anchors if anchor not in requested]
            if not replaceable:
                break
            # Remove a node in the least important broad-curve region.  This
            # intentionally bunches the transferred budget at the requested
            # crossing, terminal, curl, or decorative join.
            replacement = max(
                replaceable,
                key=lambda anchor: min(
                    min((anchor - item) % len(points), (item - anchor) % len(points))
                    for item in requested
                ),
            )
            anchors.remove(replacement)
            anchors.add(feature)
        anchors_by_contour.append(sorted(anchors))
    layer, _lines, _curves = rf.build_layer(build_sets, anchors_by_contour)
    candidate = evaluate_custom(point_sets, context, layer, method)
    if candidate["points"] > MAX_POINTS:
        raise RuntimeError(f"{method}: biased candidate exceeds point cap")
    return candidate


def seveneighths_v5_smoothed_candidate(v5_font, point_sets, context):
    """Keep v5's seven and slash verbatim; smooth only its 8 contours."""
    result = v5_font["seveneighths"].foreground.dup()
    if len(result) != 5:
        raise RuntimeError("v5 seveneighths contour inventory changed")
    # v5 contour order is: outer 8, slash, seven, lower counter, upper counter.
    # Align handles only at upper-loop nodes. Every on-curve point, the lower
    # loop, the seven, and the slash stay at their exact v5 coordinates.
    # The visible bump is at the top apex (point 18). Align that node only;
    # touching adjacent nodes or the counter changes the intended v5 shape.
    align_selected_smooth_nodes(result[0], (18,))
    candidate = evaluate_custom(
        point_sets, context, result,
        "v9-seveneighths-v5-exact-seven-slash-smoothed-eight",
    )
    if candidate["points"] != 100:
        raise RuntimeError("v9 seveneighths did not preserve the v5 100-point budget")
    return candidate


def cubicize_matching_segment(layer, contour_index, start_target, end_target,
                              control1, control2):
    """Replace one budgeted line with a cubic without disturbing neighbors."""
    result = fontforge.layer()
    result.is_quadratic = False
    for current_index, source_contour in enumerate(layer):
        values = list(source_contour)
        on_indices = [index for index, point in enumerate(values) if point.on_curve]
        contour = fontforge.contour()
        contour.is_quadratic = False
        first = values[on_indices[0]]
        contour.moveTo(first.x, first.y)
        for position, left_index in enumerate(on_indices):
            right_index = on_indices[(position + 1) % len(on_indices)]
            start, end = values[left_index], values[right_index]
            controls = []
            cursor = (left_index + 1) % len(values)
            while cursor != right_index:
                if not values[cursor].on_curve:
                    controls.append(values[cursor])
                cursor = (cursor + 1) % len(values)
            matched = (
                current_index == contour_index and
                math.dist((start.x, start.y), start_target) < 3.0 and
                math.dist((end.x, end.y), end_target) < 3.0
            )
            if matched:
                contour.cubicTo(control1, control2, (end.x, end.y))
            elif len(controls) >= 2:
                contour.cubicTo(
                    (controls[0].x, controls[0].y),
                    (controls[-1].x, controls[-1].y),
                    (end.x, end.y),
                )
            else:
                contour.lineTo(end.x, end.y)
        contour.closed = True
        result += contour
    return rf.classify_layer(result)


def shared_b_middle_candidate(source_font, point_sets, context):
    candidate = protected_detail_candidate(
        source_font["uni0412"].foreground.dup(), point_sets, context,
        protected_targets=(
            (308, 418), (342, 422), (365, 436),
            (416, 472), (452, 499), (371, 479),
        ),
        method="v9-shared-B-adaptive-protected-middle-detail",
    )
    layer = cubicize_matching_segment(
        candidate["layer"], 1, (366, 436), (416, 472),
        (379, 445), (399, 462),
    )
    return evaluate_custom(
        point_sets, context, layer,
        "v9-shared-B-protected-middle-cubic-join",
    )


def zhe_exaggerated_junction_candidate(v7_font, point_sets, context):
    layer = v7_font["uni0416"].foreground.dup()
    adjustments = (
        ((469.0, 416.7), 10.0),
        ((491.9, 412.4), 10.0),
        ((486.2, 399.4), -10.0),
    )
    for contour in layer:
        for point in contour:
            if not point.on_curve:
                continue
            for target, delta_y in adjustments:
                if math.dist((point.x, point.y), target) < 3.0:
                    point.y += delta_y
                    point.type = fontforge.splineCorner
                    break
    return evaluate_custom(
        point_sets, context, layer,
        "v9-Zhe-separated-protected-junction-corners",
    )


def simplify_preserving_path(layer, budget):
    best = None
    for error in (.25, .5, .75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0,
                  8.0, 10.0, 12.0, 16.0, 20.0, 30.0):
        scratch = fontforge.font()
        scratch.encoding = "UnicodeFull"
        glyph = scratch.createChar(-1, "candidate")
        try:
            glyph.foreground = layer.dup()
            glyph.simplify(error, ("mergelines", "choosehv", "smoothcurves"))
            candidate = glyph.foreground.dup()
        finally:
            scratch.close()
        if point_count(candidate) <= budget:
            best = candidate
            break
    return best


def v9_yeru_candidate(v6_font, point_sets, context):
    base = v6_font["uni042B"].foreground
    left = []
    right = []
    split = v6_font["uni042B"].width * .55
    for contour in base:
        box = contour_bbox(contour)
        (right if (box[0] + box[2]) / 2.0 > split else left).append(contour)
    if not left or not right:
        return None
    donor_source = v6_font["l"].foreground.dup()
    left_source = layer_from_contours(left)
    left_candidate = simplify_preserving_path(left_source, 51)
    if left_candidate is None:
        return None
    target_right = layer_from_contours(right)
    fitted = registered_layer(
        donor_source, layer_bbox(donor_source),
        layer_bbox(target_right),
    )
    layer = left_candidate.dup()
    for contour in fitted:
        layer += contour.dup()
    return evaluate_custom(
        point_sets, context, layer, "v9-Yeru-right-stem-from-l",
    )


def forced_quote_candidate(source_font, target_name, point_sets, context):
    comma_doubles = {"quotedblleft", "quotedblright", "quotedblbase", "uni201F"}
    straight_doubles = {"quotedbl", "second"}
    if target_name in comma_doubles or target_name in {
        "quoteleft", "quoteright", "quotereversed",
    }:
        family, donor_name = "comma-quotes", "quotesinglbase"
        doubles = target_name in comma_doubles
    elif target_name in straight_doubles or target_name in {"quotesingle", "minute"}:
        family, donor_name = "straight-quotes", "minute"
        doubles = target_name in straight_doubles
    else:
        return None

    donor_source = source_font[donor_name].foreground.dup()
    donor = selected_uniform_at(donor_source, 48, PURE_SMOOTH_REVIEW)
    boxes = principal_boxes(source_font[target_name].foreground, 2 if doubles else 1)
    candidates = []
    # Orientation is selected against the target glyph rather than inferred
    # from its Unicode name.  This catches the reversed comma quotes that were
    # incorrectly rotated in v4 while keeping straight quote strokes separate.
    for orientation in ("register", "rotate-180", "flip-x", "flip-y"):
        layers = [
            uniform_registered_layer(
                donor["layer"], layer_bbox(donor_source), box, orientation,
            )
            for box in boxes
        ]
        layer = combine_layers(layers)
        if point_count(layer) > MAX_POINTS:
            continue
        candidates.append(evaluate_custom(
            point_sets, context, layer,
            f"user-family:{family}:{donor_name}:{orientation}:{len(boxes)}",
        ))
    if not candidates:
        return None
    clean = [
        candidate for candidate in candidates
        if not candidate["self_intersections"] and not candidate["invalid_handles"]
    ]
    return min(clean or candidates, key=rf.candidate_key)


def forced_round_mark_candidate(source_font, target_name, point_sets, context):
    donor_source = source_font["periodcentered"].foreground.dup()
    donor, _sets, _context, _count = base_candidates(donor_source)
    target_box = principal_boxes(source_font[target_name].foreground, 1)[0]
    layer = registered_layer(donor["layer"], layer_bbox(donor_source), target_box)
    return evaluate_custom(
        point_sets, context, layer,
        "user-family:round-marks:periodcentered:register",
    )


def semicolon_candidate(source_font, point_sets, context):
    source_layer = source_font["semicolon"].foreground
    contours = sorted(source_layer, key=contour_area_box, reverse=True)
    tail_source = layer_from_contours(contours[:1])
    tail = selected_uniform_at(tail_source, 60, SMOOTH_REVIEW)
    donor_source = source_font["periodcentered"].foreground.dup()
    dot, _sets, _context, _count = base_candidates(donor_source)
    dot_box = contour_bbox(contours[1])
    dot_layer = registered_layer(dot["layer"], layer_bbox(donor_source), dot_box)
    return evaluate_custom(
        point_sets, context, combine_layers([tail["layer"], dot_layer]),
        "user-composite:semicolon-tail+periodcentered",
    )


def zero_candidate(source_layer, point_sets, context):
    candidates, _sets, _context = uniform_candidates(source_layer, PURE_SMOOTH_REVIEW)
    eligible = [
        candidate for candidate in candidates
        if candidate["points"] <= 70 and len(candidate["layer"]) == 2
        and candidate["metrics"]["topology_match"]
        and not candidate["self_intersections"] and not candidate["invalid_handles"]
    ]
    selected = min(eligible or candidates, key=rf.candidate_key)
    return evaluate_custom(
        point_sets, context, selected["layer"],
        "user-source-shaped-smooth-zero",
    )


def high_detail_smooth_candidate(source_layer, point_sets, context, name):
    candidates, _sets, _context = uniform_candidates(
        source_layer, PURE_SMOOTH_REVIEW,
    )
    eligible = [
        candidate for candidate in candidates
        if candidate["points"] >= 90
        and len(candidate["layer"]) == len(source_layer)
        and not candidate["self_intersections"] and not candidate["invalid_handles"]
    ]
    selected = min(eligible or candidates, key=rf.candidate_key)
    return evaluate_custom(
        point_sets, context, selected["layer"],
        f"user-high-detail-pure-smooth:{name}",
    )


def point_sets_bbox(point_sets):
    points = [point for contour in point_sets for point in contour]
    return (
        min(point[0] for point in points), min(point[1] for point in points),
        max(point[0] for point in points), max(point[1] for point in points),
    )


def original_infinity_candidate(point_sets, context):
    scratch = fontforge.font()
    try:
        glyph = scratch.createChar(-1, "infinity.original")
        glyph.importOutlines(str(ORIGINAL_INFINITY_SVG), scale=False, correctdir=False)
        exact_layer = glyph.foreground.dup()
    finally:
        scratch.close()
    raw = rv4.point_sets(exact_layer)
    visible = rv3.raster_visible_boundary(raw, 3, size=1024)
    exact_context = rf.metric_context(raw)
    candidates = []
    for budget in (49, 60, 70, 80):
        allocation = allocate_segments(visible, budget // 3)
        if allocation is None:
            continue
        layer, lines, curves = build_uniform_curve_layer(
            visible, allocation, PURE_SMOOTH_REVIEW,
        )
        candidate = rf.evaluate_layer(
            raw, layer, lines, curves, exact_context, full_audit=True,
            max_points=MAX_POINTS,
        )
        candidates.append(enrich_candidate(candidate, f"original-svg-uniform-{point_count(layer)}"))
    valid = [
        candidate for candidate in candidates
        if len(candidate["layer"]) == 3 and not candidate["self_intersections"]
        and not candidate["invalid_handles"]
    ]
    if not valid:
        raise RuntimeError("original SVG infinity has no structurally valid smooth refit")
    exact = min(valid, key=lambda candidate: (rf.candidate_key(candidate), -candidate["curve_ratio"]))
    registered = uniform_registered_layer(
        exact["layer"], layer_bbox(exact["layer"]), point_sets_bbox(point_sets),
    )
    return evaluate_custom(
        point_sets, context, registered,
        "user-original-svg-smooth-infinity-registered-to-v2",
    )


def phi_candidate(source_font, point_sets, context):
    donor_source = source_font["uni0424"].foreground.dup()
    donor, _sets, _context, _count = base_candidates(donor_source)
    layer = uniform_registered_layer(
        donor["layer"], layer_bbox(donor_source), layer_bbox(source_font["Phi"].foreground),
    )
    return evaluate_custom(point_sets, context, layer, "user-family:Phi:uni0424:uniform-scale")


def degree_candidate(source_font, v4_font, point_sets, context):
    source = v4_font["degree"].foreground
    # The four tiny contours at the top are source artifacts.  The intended
    # degree mark is the two largest contours; refit those with smooth cubic
    # tangents rather than replacing the hand-drawn ring with a perfect oval.
    intended = layer_from_contours(sorted(source, key=contour_area_box, reverse=True)[:2])
    return evaluate_custom(
        point_sets, context, intended,
        "user-clean-degree-preserved-v4-ring-two-contours",
    )


def angstrom_candidate(source_font, v4_font, point_sets, context):
    target = source_font["uni212B"].foreground
    contours = sorted(target, key=contour_area_box, reverse=True)
    a_source = source_font["A"].foreground.dup()
    a, _sets, _context, _count = base_candidates(a_source)
    transform = uniform_transform(layer_bbox(a_source), contour_bbox(contours[0]))
    a_layer = a["layer"].dup()
    a_layer.transform(transform)
    degree_source = source_font["degree"].foreground.dup()
    degree_sets = [
        sf.resample_polyline(contour, 4.0, closed=True)
        for contour in sf.layer_point_sets(degree_source)
    ]
    degree = degree_candidate(
        source_font, v4_font, degree_sets, rf.metric_context(degree_sets),
    )
    ring_target = layer_from_contours(contours[1:])
    ring = uniform_registered_layer(
        degree["layer"], layer_bbox(degree["layer"]), layer_bbox(ring_target),
    )
    candidate = evaluate_custom(
        point_sets, context, combine_layers([a_layer, ring]),
        "user-composite:uni212B:uniform-scaled-A+degree",
        max_points=1000,
    )
    candidate["editable_layer"] = ring
    candidate["component_references"] = [("A", transform)]
    return candidate


def clustered_boxes(layer, count: int) -> list[tuple[float, float, float, float]]:
    boxes = [contour_bbox(contour) for contour in layer]
    if count <= 1 or len(boxes) <= 1:
        return [layer_bbox(layer)]
    ordered = sorted(boxes, key=lambda box: ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))
    groups = [[] for _ in range(count)]
    for index, box in enumerate(ordered):
        groups[min(count - 1, index * count // len(ordered))].append(box)
    result = []
    for group in groups:
        if not group:
            continue
        result.append((
            min(box[0] for box in group), min(box[1] for box in group),
            max(box[2] for box in group), max(box[3] for box in group),
        ))
    return result


def combine_layers(layers):
    result = fontforge.layer()
    result.is_quadratic = False
    for layer in layers:
        for contour in layer:
            result += contour
    return rf.classify_layer(result)


def family_candidate(source_font, target_name: str, spec: dict, point_sets, context):
    donor_name = spec["donor"]
    if donor_name not in source_font:
        return None
    donor_source = source_font[donor_name].foreground.dup()
    if not len(donor_source):
        return None
    donor, _sets, _context, _count = base_candidates(donor_source)
    operation = spec.get("transform", "register")
    target_layer = source_font[target_name].foreground
    if operation == "repeat-components":
        donor_contours = max(1, len(donor_source))
        repeat_count = max(2, min(4, round(len(target_layer) / donor_contours)))
        boxes = clustered_boxes(target_layer, repeat_count)
        layers = [
            registered_layer(donor["layer"], layer_bbox(donor_source), box)
            for box in boxes
        ]
        layer = combine_layers(layers)
    else:
        layer = registered_layer(
            donor["layer"], layer_bbox(donor_source), layer_bbox(target_layer), operation,
        )
    if point_count(layer) > MAX_POINTS:
        return None
    lines, curves = segment_counts(layer)
    candidate = rf.evaluate_layer(
        point_sets, layer, lines, curves, context, full_audit=True,
        max_points=MAX_POINTS,
    )
    return enrich_candidate(candidate, f"family:{spec['family']}:{donor_name}:{operation}")


def fraction_candidate(source_font, target_name: str, donors: list[str], point_sets, context):
    if any(name not in source_font for name in donors):
        return None
    boxes = clustered_boxes(source_font[target_name].foreground, len(donors))
    if len(boxes) != len(donors):
        return None
    layers = []
    for donor_name, box in zip(donors, boxes):
        donor_source = source_font[donor_name].foreground.dup()
        donor, _sets, _context, _count = base_candidates(donor_source)
        layers.append(registered_layer(donor["layer"], layer_bbox(donor_source), box))
    layer = combine_layers(layers)
    if point_count(layer) > MAX_POINTS:
        return None
    lines, curves = segment_counts(layer)
    candidate = rf.evaluate_layer(
        point_sets, layer, lines, curves, context, full_audit=True,
        max_points=MAX_POINTS,
    )
    return enrich_candidate(candidate, "family:fractions:" + ",".join(donors))


def canonical_contour(points, operation="register"):
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    width, height = max(1e-6, x1 - x0), max(1e-6, y1 - y0)
    values = []
    step = max(1, len(points) // 48)
    for x, y in points[::step]:
        nx, ny = (x - x0) / width, (y - y0) / height
        if operation == "flip-x": nx = 1 - nx
        elif operation == "flip-y": ny = 1 - ny
        elif operation == "rotate-180": nx, ny = 1 - nx, 1 - ny
        values.append((round(nx, 3), round(ny, 3)))
    if not values:
        return ()
    rotations = [tuple(values[index:] + values[:index]) for index in range(len(values))]
    reverse = list(reversed(values))
    rotations.extend(tuple(reverse[index:] + reverse[:index]) for index in range(len(reverse)))
    return min(rotations)


def normalized_signature(layer, operation="register") -> str:
    contours = [canonical_contour(points, operation) for points in sf.layer_point_sets(layer)]
    payload = json.dumps(sorted(contours), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_family_registry(source_font, review_names: list[str]) -> dict:
    source = json.loads(FAMILY_SOURCE.read_text(encoding="utf-8"))
    targets = {}
    for family in source["families"]:
        for name, transform in family["members"].items():
            targets[name] = {
                "family": family["name"], "donor": family["donor"],
                "transform": transform, "origin": "seeded",
            }
    signatures = {}
    discovered = []
    for name in review_names:
        if name not in source_font or not len(source_font[name].foreground):
            continue
        layer = source_font[name].foreground
        identity = normalized_signature(layer)
        if name not in targets and identity in signatures:
            donor = signatures[identity]
            targets[name] = {
                "family": "detected-reused-outline", "donor": donor,
                "transform": "register", "origin": "normalized-v2-match",
            }
            discovered.append({"glyph": name, "donor": donor, "transform": "register"})
        signatures.setdefault(identity, name)
        for operation in ("flip-x", "flip-y", "rotate-180"):
            signature = normalized_signature(layer, operation)
            if name not in targets and signature in signatures:
                donor = signatures[signature]
                targets[name] = {
                    "family": "detected-reused-outline", "donor": donor,
                    "transform": operation, "origin": "normalized-v2-match",
                }
                discovered.append({"glyph": name, "donor": donor, "transform": operation})
                break
    return {
        "version": "olesuas-hand-italic-v9-family-registry-v1",
        "seed_source": str(FAMILY_SOURCE.relative_to(ROOT)),
        "targets": targets,
        "fractions": source.get("fractions", {}),
        "discovered": discovered,
    }


def empty_row(name: str, source_layer, width: int) -> dict:
    digest = rf.layer_hash(source_layer)
    return {
        "glyph": name, "source_points": point_count(source_layer),
        "candidate_points": 0, "source_contours": len(source_layer),
        "candidate_contours": 0, "automatic_pass": "true",
        "needs_manual_review": "false", "failure_reasons": "",
        "selected_method": "empty", "source_hash": digest,
        "candidate_hash": rf.layer_hash(fontforge.layer()), "width": width,
        "left_sidebearing": 0, "right_sidebearing": width,
        "bounds": "[]", "corner_points": 0, "curve_points": 0,
        "hvcurve_points": 0, "tangent_points": 0, "off_curve_points": 0,
        "on_curve_points": 0, "curve_ratio": "1.000000",
        "curve_geometry_ratio": "1.000000",
        "spacing_cv": "0.000000", "spacing_max_ratio": "1.000000",
        "protected_spacing_exceptions": 0, "small_contours_preserved": "true",
        "extrema_preserved": "true", "smoothness_status": "empty",
        "self_intersections": 0, "invalid_handles": 0,
        "topology_status": "empty", "component_delta": 0, "counter_delta": 0,
        "boundary_p95": "0.000000", "boundary_max": "0.000000",
        "family": "", "family_donor": "", "family_transform": "",
        "family_status": "not-applicable", "candidate_count": 0,
        "processing_seconds": "0.0000", "worst_raster_size": 32,
    }


def candidate_row(glyph, source_layer, candidate, candidate_count, family_spec,
                  family_status, elapsed: float) -> dict:
    metrics = candidate["metrics"]
    point_types = point_type_metrics(candidate["layer"])
    spacing = spacing_metrics(candidate["layer"])
    source_box = layer_bbox(source_layer)
    candidate_box = layer_bbox(candidate["layer"])
    extrema_delta = max(abs(first - second) for first, second in zip(source_box, candidate_box))
    first_topology = metrics["topologies"].get(128, ((0, 0), (0, 0)))
    row = {
        "glyph": glyph.glyphname, "codepoint": glyph.unicode if glyph.unicode >= 0 else "",
        "source_points": point_count(source_layer), "candidate_points": candidate["points"],
        "source_contours": len(source_layer), "candidate_contours": len(candidate["layer"]),
        "automatic_pass": str(candidate["passes"]).lower(),
        "needs_manual_review": str(not candidate["passes"]).lower(),
        "failure_reasons": ";".join(candidate["failures"]),
        "selected_method": candidate["method"],
        "source_hash": rf.layer_hash(source_layer),
        "candidate_hash": rf.layer_hash(candidate["layer"]),
        "width": int(glyph.width),
        "left_sidebearing": int(round(glyph.left_side_bearing)),
        "right_sidebearing": int(round(glyph.right_side_bearing)),
        "bounds": json.dumps([round(value, 4) for value in source_box]),
        **{key: point_types[key] for key in (
            "corner_points", "curve_points", "hvcurve_points", "tangent_points",
            "off_curve_points", "on_curve_points",
        )},
        "curve_ratio": f"{point_types['curve_ratio']:.6f}",
        "curve_geometry_ratio": f"{point_types['curve_geometry_ratio']:.6f}",
        "spacing_cv": f"{spacing['spacing_cv']:.6f}",
        "spacing_max_ratio": f"{spacing['spacing_max_ratio']:.6f}",
        "protected_spacing_exceptions": spacing["protected_spacing_exceptions"],
        "small_contours_preserved": str(len(source_layer) == len(candidate["layer"])).lower(),
        "extrema_preserved": str(extrema_delta <= 4.0).lower(),
        "smoothness_status": (
            "preferred" if point_types["curve_ratio"] >= MIN_CURVE_RATIO
            and spacing["spacing_cv"] <= MAX_UNPROTECTED_SPACING_CV else "review"
        ),
        "self_intersections": candidate["self_intersections"],
        "invalid_handles": candidate["invalid_handles"],
        "topology_status": "match" if metrics["topology_match"] else "mismatch",
        "component_delta": first_topology[1][0] - first_topology[0][0],
        "counter_delta": first_topology[1][1] - first_topology[0][1],
        "boundary_p95": f"{metrics['boundary_p95']:.6f}",
        "boundary_max": f"{metrics['boundary_max']:.6f}",
        "family": family_spec.get("family", "") if family_spec else "",
        "family_donor": family_spec.get("donor", "") if family_spec else "",
        "family_transform": family_spec.get("transform", "") if family_spec else "",
        "family_status": family_status,
        "candidate_count": candidate_count,
        "processing_seconds": f"{elapsed:.4f}",
        "worst_raster_size": metrics["worst_size"],
    }
    for size, values in metrics["per_size"].items():
        row[f"mse_{size}"] = f"{values['mse']:.6f}"
        row[f"ink_iou_{size}"] = f"{values['ink_iou']:.6f}"
        row[f"false_positive_ink_{size}"] = f"{values['false_positive_ink']:.6f}"
        row[f"false_negative_ink_{size}"] = f"{values['false_negative_ink']:.6f}"
    for size, (source_topology, target_topology) in metrics["topologies"].items():
        row[f"source_components_{size}"] = source_topology[0]
        row[f"source_counters_{size}"] = source_topology[1]
        row[f"candidate_components_{size}"] = target_topology[0]
        row[f"candidate_counters_{size}"] = target_topology[1]
    return row


def save_worker_glyph(source_glyph, layer, destination: Path, width=None):
    worker = fontforge.font()
    worker.encoding = "UnicodeFull"
    worker.em = 1000
    glyph = worker.createChar(source_glyph.unicode, source_glyph.glyphname)
    glyph.foreground = layer
    glyph.width = source_glyph.width if width is None else width
    worker.save(str(destination))
    worker.close()


def worker_build(name: str, destination_sfd: Path, destination_json: Path) -> int:
    import time
    started = time.perf_counter()
    source = fontforge.open(str(SOURCE_SFD))
    v8_base = fontforge.open(str(V8_BASE_SFD))
    v6_base = fontforge.open(str(V6_BASE_SFD))
    v5_base = fontforge.open(str(V5_BASE_SFD))
    regular = fontforge.open(str(REGULAR_FINAL_SFD))
    registry = json.loads(FAMILY_REPORT.read_text(encoding="utf-8"))
    try:
        glyph = source[name]
        source_layer = glyph.foreground.dup()
        if name == ".notdef":
            empty = regular["space"].foreground.dup()
            row = empty_row(name, source_layer, int(regular["space"].width))
            row["selected_method"] = "regular-space-copy"
            row["source_hash"] = rf.layer_hash(source_layer)
            row["candidate_hash"] = rf.layer_hash(empty)
            save_worker_glyph(glyph, empty, destination_sfd, width=int(regular["space"].width))
            destination_json.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
            return 0
        if not len(source_layer):
            row = empty_row(name, source_layer, int(glyph.width))
            save_worker_glyph(glyph, source_layer, destination_sfd)
            destination_json.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
            return 0
        review_mode = ""
        if DECISIONS.exists():
            try:
                review_mode = json.loads(DECISIONS.read_text(encoding="utf-8")) \
                    .get("decisions", {}).get(name, {}).get("status", "")
            except (OSError, ValueError):
                review_mode = ""
        review_mode = REVIEW_MODE_OVERRIDES.get(name, review_mode)
        selected, point_sets, context, count = base_candidates(source_layer, review_mode)
        baseline = evaluate_custom(
            point_sets, context, v8_base[name].foreground.dup(),
            "v8-reviewed-baseline",
        )
        if review_mode == DETAIL_REVIEW:
            structural = visible_structure_candidate(
                glyph, source_layer, point_sets, context,
            )
            if structural is not None:
                selected = structural
                count += 1
            else:
                selected = min((selected, baseline), key=rf.candidate_key)
                count += 1
        if name in FORCE_CLEAN_REGULAR:
            regular_candidate, regular_count = rf.redraw_glyph(None, source_layer)
            lines, curves = segment_counts(regular_candidate["layer"])
            regular_candidate = rf.evaluate_layer(
                point_sets, regular_candidate["layer"], lines, curves, context,
                full_audit=True, max_points=MAX_POINTS,
            )
            selected = enrich_candidate(regular_candidate, "clean-regular-structural-fallback")
            count += regular_count
        family_spec = registry["targets"].get(name)
        family_status = "not-applicable"
        family = None
        if name == "g":
            family_spec = {
                "family": "v9-detail-biased-lowercase-g", "donor": "v2-g",
                "transform": "redistribute-to-crossing+lower-stroke-width",
            }
            family = biased_detail_candidate(
                source_layer, point_sets, context,
                allocation=(11, 6, 7, 3, 2, 2, 2),
                protected_targets=(
                    (285, 70), (315, 45), (345, 15),
                    (320, -20), (285, -65), (250, -110),
                ),
                method="v9-g-protected-lower-crossing-and-overlap-detail",
            )
            family_status = "user-directed-exception"
        elif name == "o":
            family_spec = {
                "family": "latin-cyrillic-small-o", "donor": "uni043E",
                "transform": "exact-outline-copy",
            }
            family = exact_donor_candidate(
                v8_base, "uni043E", point_sets, context,
                "v9-latin-o-from-cyrillic-small-o",
            )
            family_status = "user-directed-reuse"
        elif name == "uni0414":
            family_spec = {
                "family": "latin-cyrillic-capital-D", "donor": "D",
                "transform": "exact-outline-copy",
            }
            family = exact_donor_candidate(
                v8_base, "D", point_sets, context,
                "v9-cyrillic-De-from-Latin-D",
            )
            family_status = "user-directed-reuse"
        elif name == "uni042E":
            family_spec = {
                "family": "v9-detail-biased-capital-Yu", "donor": "v2-uni042E",
                "transform": "reduce-O+protect-horizontal-and-left-decorations",
            }
            family = protected_detail_candidate(
                source_layer, point_sets, context,
                protected_targets=(
                    (55, 720), (150, 770), (285, 740),
                    (250, 470), (315, 455), (390, 450),
                    (285, 80), (180, 20), (55, 170),
                ),
                method="v9-capital-Yu-detail-biased-left-and-horizontal",
            )
            family_status = "user-directed-exception"
        elif name == "uni0435":
            family_spec = {
                "family": "latin-cyrillic-small-e", "donor": "e",
                "transform": "exact-outline-copy",
            }
            family = exact_donor_candidate(
                v8_base, "e", point_sets, context,
                "v9-cyrillic-Ie-from-Latin-e",
            )
            family_status = "user-directed-reuse"
        elif name == "uni0437":
            family_spec = {
                "family": "v9-detail-biased-small-Ze", "donor": "v2-uni0437",
                "transform": "reduce-loop+protect-crossing-above-loop",
            }
            family = biased_detail_candidate(
                source_layer, point_sets, context,
                allocation=(25, 8),
                protected_targets=(
                    (255, 105), (290, 75), (325, 45),
                    (335, 10), (300, -20),
                ),
                method="v9-small-Ze-protected-crossing-above-loop",
            )
            family_status = "user-directed-exception"
        elif name == "uni0446":
            family_spec = {
                "family": "v9-detail-biased-small-Tse", "donor": "v2-uni0446",
                "transform": "protect-loop-end+bottom-terminal",
            }
            family = biased_detail_candidate(
                source_layer, point_sets, context,
                allocation=(27, 6),
                protected_targets=(
                    (390, -70), (430, -75), (452, -5),
                    (480, -95), (420, 10),
                ),
                method="v9-small-Tse-protected-loop-end-and-bottom",
            )
            family_status = "user-directed-exception"
        elif name == "uni044E":
            family_spec = {
                "family": "v9-detail-biased-small-Yu", "donor": "v2-uni044E",
                "transform": "reduce-O+protect-left-and-crossing-topology",
            }
            family = protected_detail_candidate(
                source_layer, point_sets, context,
                protected_targets=(
                    (230, 460), (295, 575), (275, 500),
                    (335, 365), (355, 330), (315, 315),
                    (150, 180), (80, 45),
                ),
                method="v9-small-Yu-protected-left-crossings-and-O",
            )
            family_status = "user-directed-exception"
        elif name == "Theta":
            family_spec = {
                "family": "v9-detail-biased-Theta", "donor": "v2-Theta",
                "transform": "reduce-main-curves+protect-top-curl",
            }
            family = biased_detail_candidate(
                source_layer, point_sets, context,
                allocation=(10, 8, 15),
                protected_targets=(
                    (350, 700), (410, 735), (485, 730),
                    (455, 660), (400, 610), (365, 575),
                ),
                method="v9-Theta-budget-moved-from-O-to-top-curl",
            )
            family_status = "user-directed-exception"
        elif name == "Upsilon":
            family_spec = {
                "family": "greek-cyrillic-capital-Upsilon-U", "donor": "uni0423",
                "transform": "exact-outline-copy",
            }
            family = exact_donor_candidate(
                v8_base, "uni0423", point_sets, context,
                "v9-Upsilon-from-Cyrillic-U",
            )
            family_status = "user-directed-reuse"
        elif name in {"Beta", "uni0412"}:
            family_spec = {
                "family": "latin-greek-cyrillic-capital-B",
                "donor": "B",
                "transform": "exact-outline-copy",
            }
            family = exact_donor_candidate(
                v8_base, "B", point_sets, context,
                "v9-Beta-and-Cyrillic-Ve-from-Latin-B",
            )
            family_status = "user-directed-reuse"
        elif name == "S":
            family_spec = {
                "family": "v9-detail-biased-S", "donor": "v8-S",
                "transform": "reduce-main-curve+protect-both-decorative-terminals",
            }
            family = biased_detail_candidate(
                v8_base["S"].foreground.dup(), point_sets, context,
                allocation=(28,),
                protected_targets=(
                    (550, 790), (525, 730), (500, 665),
                    (105, 185), (55, 235), (110, 120), (190, 35),
                ),
                method="v9-S-budget-moved-from-main-curve-to-terminals",
            )
            family_status = "user-directed-exception"
        elif name == "six":
            family_spec = {
                "family": "v9-centerline-stroke-rebuild", "donor": "XOPP-six-stroke",
                "transform": "smooth-centerline+coupled-uniform-width",
            }
            family = centerline_stroke_candidate(
                "six", point_sets, context,
                segments=(12,), passes=(6,),
                protected=(((369, 462), (168, 185), (91, 100)),),
                method="v9-six-centerline-coupled-uniform-width",
            )
            family_status = "user-directed-exception"
        elif name == "uni0416":
            family_spec = {
                "family": "v9-detail-biased-Zhe", "donor": "v2-uni0416",
                "transform": "reduce-main-curves+protect-central-crossing",
            }
            family = v9_zhe_candidate(v6_base, point_sets, context)
            family_status = "user-directed-exception"
        elif name == "seveneighths":
            family_spec = {
                "family": "v5-selected-seven-eighths",
                "donor": "v5-seveneighths",
                "transform": "exact-seven-and-slash+smooth-8",
            }
            family = seveneighths_v5_smoothed_candidate(
                v5_base, point_sets, context,
            )
            family_status = "user-directed-exception"
        elif name == "uni042A":
            family_spec = {
                "family": "v9-protected-detail-refit", "donor": "v2-uni042A",
                "transform": "adaptive-budget+protected-top-detail",
            }
            family = protected_detail_candidate(
                source_layer, point_sets, context,
                protected_targets=(
                    (143, 636), (8, 700), (200, 748), (308, 639),
                    (410, 748), (440, 732), (410, 645), (300, 610),
                ),
                method="v9-uni042A-adaptive-protected-top-detail",
            )
            family_status = "user-directed-exception"
        elif name == "uni042B":
            family_spec = {
                "family": "v9-Yeru-composition", "donor": "uni042B-left,l-right",
                "transform": "preserve-left+registered-l",
            }
            family = v9_yeru_candidate(v8_base, point_sets, context)
            family_status = "user-directed-reuse"
        elif name == "D":
            family_spec = {
                "family": "user-directed-smooth-letter", "donor": "v7-D-shape",
                "transform": "preserve-geometry+reclassify-smooth-nodes",
            }
            family = evaluate_custom(
                point_sets, context, v8_base[name].foreground.dup(),
                "user-v7-D-preserved-smooth-node-reclassification",
            )
            family_status = "user-directed-exception"
        elif name in {
            "quoteleft", "quoteright", "quotereversed", "quotedblleft",
            "quotedblright", "quotedblbase", "uni201F", "quotedbl",
            "quotesingle", "minute", "second",
        }:
            comma_family = name in {
                "quoteleft", "quoteright", "quotereversed", "quotedblleft",
                "quotedblright", "quotedblbase", "uni201F",
            }
            family_spec = {
                "family": "comma-quotes" if comma_family else "straight-quotes",
                "donor": "quotesinglbase" if comma_family else "minute",
                "transform": "target-tested-orientation-and-repeat",
            }
            family = forced_quote_candidate(source, name, point_sets, context)
            family_status = "user-directed-reuse"
        elif name in {"period", "bullet", "uni2219"}:
            family_spec = {
                "family": "user-directed-round-marks", "donor": "periodcentered",
                "transform": "register",
            }
            family = forced_round_mark_candidate(source, name, point_sets, context)
            family_status = "user-directed-reuse"
        elif name == "semicolon":
            family_spec = {
                "family": "user-directed-semicolon", "donor": "periodcentered",
                "transform": "tail-plus-registered-dot",
            }
            family = semicolon_candidate(source, point_sets, context)
            family_status = "user-directed-reuse"
        elif name == "zero":
            family_spec = {
                "family": "user-directed-zero", "donor": "v2-zero-shape",
                "transform": "pure-smooth-source-refit",
            }
            family = zero_candidate(source_layer, point_sets, context)
            family_status = "user-directed-exception"
        elif name == "infinity":
            family_spec = {
                "family": "user-directed-infinity", "donor": "original-svg",
                "transform": "smooth-cubic-refit",
            }
            family = original_infinity_candidate(point_sets, context)
            family_status = "user-directed-exception"
        elif name == "degree":
            family_spec = {
                "family": "user-directed-degree", "donor": "degree",
                "transform": "remove-four-artifact-contours+smooth-refit",
            }
            family = degree_candidate(source, v8_base, point_sets, context)
            family_status = "user-directed-exception"
        elif name == "Phi":
            family_spec = {
                "family": "latin-cyrillic-phi", "donor": "uni0424",
                "transform": "uniform-scale",
            }
            family = phi_candidate(source, point_sets, context)
            family_status = "user-directed-reuse"
        elif name == "uni212B":
            family_spec = {
                "family": "user-directed-angstrom", "donor": "A,degree",
                "transform": "uniform-scale+compose",
            }
            family = angstrom_candidate(source, v8_base, point_sets, context)
            family_status = "user-directed-reuse"
        elif name in registry.get("fractions", {}):
            donors = registry["fractions"][name]
            family_spec = {
                "family": "fractions", "donor": ",".join(donors),
                "transform": "component-envelope-registration",
            }
            family = fraction_candidate(source, name, donors, point_sets, context)
        elif family_spec:
            family = family_candidate(source, name, family_spec, point_sets, context)
        user_directed = family_status.startswith("user-directed")
        if family is not None:
            count += 1
            if user_directed:
                selected = family
            elif family["passes"] and (
                not selected["passes"] or family["points"] <= selected["points"]
            ):
                selected = family
                family_status = "accepted"
            else:
                family_status = "rejected-by-target-gates"
        elif family_spec:
            family_status = "unavailable-or-over-budget"
        if review_mode == SMOOTH_REVIEW:
            smoothed = baseline_smoothing_candidate(
                v8_base[name].foreground.dup(), point_sets, context, name,
            )
            if smoothed is not None:
                selected = smoothed
                count += 1
        if name not in V9_CHANGED_GLYPHS:
            # v9 is an isolated feedback round. Prior review tags must not
            # silently re-run legacy repair branches on unchanged v8 glyphs.
            selected = baseline
            family_spec = None
            family_status = "v8-preserved-unchanged"
        row = candidate_row(
            glyph, source_layer, selected, count, family_spec, family_status,
            time.perf_counter() - started,
        )
        if selected.get("component_references"):
            row["component_references"] = selected["component_references"]
        save_worker_glyph(glyph, selected.get("editable_layer", selected["layer"]), destination_sfd)
        destination_json.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
        return 0
    finally:
        source.close()
        v8_base.close()
        v6_base.close()
        v5_base.close()
        regular.close()


def launch_worker(index: int, name: str):
    cache_key = hashlib.sha256(
        ALGORITHM_CACHE_VERSION.encode("ascii") + FAMILY_SOURCE.read_bytes() + SOURCE_SFD.read_bytes()
    ).hexdigest()[:16]
    worker_root = WORKERS / cache_key
    worker_root.mkdir(parents=True, exist_ok=True)
    sfd = worker_root / f"{index:04d}-{name}.sfd"
    data = worker_root / f"{index:04d}-{name}.json"
    if sfd.exists() and data.exists():
        try:
            json.loads(data.read_text(encoding="utf-8"))
            if sfd.stat().st_size > 256:
                return index, name, sfd, data
        except (OSError, ValueError):
            pass
    command = [
        "fontforge", "-lang=py", "-script", str(Path(__file__).resolve()),
        "--worker-glyph", name, "--worker-sfd", str(sfd), "--worker-json", str(data),
    ]
    completed = subprocess.run(
        command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if completed.returncode or not sfd.exists() or not data.exists():
        raise RuntimeError(f"{name} worker failed:\n{completed.stdout[-5000:]}")
    return index, name, sfd, data


def empty_svg(path: Path) -> None:
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="-10 0 1020 1443"></svg>\n',
        encoding="utf-8",
    )


def normalize_review_svg_viewbox(path: Path, width: int) -> None:
    text = path.read_text(encoding="utf-8")
    start = text.find('viewBox="')
    if start < 0:
        raise RuntimeError("exported SVG has no viewBox: " + str(path))
    value_start = start + len('viewBox="')
    value_end = text.find('"', value_start)
    replacement = f'-10 0 {max(20, int(width) + 20)} 1443'
    path.write_text(text[:value_start] + replacement + text[value_end:], encoding="utf-8")


def export_review_svgs(review_names: list[str], index_by_name: dict[str, int]) -> None:
    SOURCE_SVG_DIR.mkdir(parents=True, exist_ok=True)
    CANDIDATE_SVG_DIR.mkdir(parents=True, exist_ok=True)
    source = fontforge.open(str(SOURCE_SFD))
    candidate = fontforge.open(str(OUTPUT_TTF))
    try:
        for name in review_names:
            filename = f"{index_by_name[name]:03d}-{name}.svg"
            source_path = SOURCE_SVG_DIR / filename
            candidate_path = CANDIDATE_SVG_DIR / filename
            if len(source[name].foreground):
                source[name].export(str(source_path))
                normalize_review_svg_viewbox(source_path, source[name].width)
            else: empty_svg(source_path)
            if name in candidate and len(candidate[name].foreground):
                candidate[name].export(str(candidate_path))
                normalize_review_svg_viewbox(candidate_path, candidate[name].width)
            else: empty_svg(candidate_path)
    finally:
        source.close()
        candidate.close()


def write_report(rows: list[dict], review_names: list[str], index_by_name: dict[str, int]) -> None:
    by_name = {row["glyph"]: row for row in rows}
    output = []
    codepoints = {
        entry["glyph_name"]: entry.get("codepoint", "")
        for entry in json.loads(TEMPLATE_MANIFEST.read_text(encoding="utf-8"))["glyphs"]
    }
    editable = fontforge.open(str(OUTPUT_SFD))
    ttf = fontforge.open(str(OUTPUT_TTF))
    try:
        for name in review_names:
            row = by_name[name]
            filename = f"{index_by_name[name]:03d}-{name}.svg"
            source_svg = SOURCE_SVG_DIR / filename
            candidate_svg = CANDIDATE_SVG_DIR / filename
            row["index"] = index_by_name[name]
            row["codepoint"] = codepoints.get(name, row.get("codepoint", ""))
            row["source_svg"] = str(source_svg.resolve())
            row["candidate_svg"] = str(candidate_svg.resolve())
            editable_layer = editable[name].foreground
            actual_types = point_type_metrics(editable_layer)
            actual_spacing = spacing_metrics(editable_layer)
            row["candidate_points"] = point_count(editable_layer)
            row["candidate_contours"] = len(editable_layer)
            row["editable_hash"] = rf.layer_hash(editable_layer)
            for key in (
                "corner_points", "curve_points", "hvcurve_points", "tangent_points",
                "off_curve_points", "on_curve_points",
            ):
                row[key] = actual_types[key]
            row["curve_ratio"] = f"{actual_types['curve_ratio']:.6f}"
            row["curve_geometry_ratio"] = f"{actual_types['curve_geometry_ratio']:.6f}"
            row["spacing_cv"] = f"{actual_spacing['spacing_cv']:.6f}"
            row["spacing_max_ratio"] = f"{actual_spacing['spacing_max_ratio']:.6f}"
            row["protected_spacing_exceptions"] = actual_spacing["protected_spacing_exceptions"]
            row["candidate_hash"] = rf.layer_hash(ttf[name].foreground) if name in ttf else rf.layer_hash(fontforge.layer())
            row["ttf_points"] = point_count(ttf[name].foreground) if name in ttf else 0
            row["ttf_contours"] = len(ttf[name].foreground) if name in ttf else 0
            output.append(row)
    finally:
        editable.close(); ttf.close()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with REPORT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=REPORT_FIELDS,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(output)


def migrate_unchanged_decisions(previous_rows: dict[str, dict], report_rows: list[dict], changed_names: set[str]) -> None:
    if not DECISIONS.exists() or not previous_rows:
        return
    values = json.loads(DECISIONS.read_text(encoding="utf-8"))
    decisions = values.setdefault("decisions", {})
    for row in report_rows:
        name = row["glyph"]
        old = previous_rows.get(name)
        decision = decisions.get(name)
        if not old or not decision:
            continue
        was_valid = (
            decision.get("source_hash") == old.get("source_hash")
            and decision.get("candidate_hash") == old.get("candidate_hash")
        )
        outline_unchanged = (
            old.get("source_hash") == row.get("source_hash")
            and old.get("editable_hash") == row.get("editable_hash")
        )
        if was_valid and outline_unchanged:
            decision["source_hash"] = row["source_hash"]
            decision["candidate_hash"] = row["candidate_hash"]
            decision["hash_migrated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(DECISIONS, values)


def invalidate_changed_decisions(changed_names: set[str]) -> None:
    if not changed_names or not DECISIONS.exists():
        return
    values = json.loads(DECISIONS.read_text(encoding="utf-8"))
    decisions = values.setdefault("decisions", {})
    for name in changed_names:
        decisions.pop(name, None)
    atomic_json(DECISIONS, values)


def build(selected_names: set[str], workers: int) -> int:
    immutable_before = {str(path.relative_to(ROOT)): sha256(path) for path in IMMUTABLES}
    template = json.loads(TEMPLATE_MANIFEST.read_text(encoding="utf-8"))
    review_names = [entry["glyph_name"] for entry in template["glyphs"]]
    index_by_name = {entry["glyph_name"]: int(entry["index"]) for entry in template["glyphs"]}
    source = fontforge.open(str(SOURCE_SFD))
    try:
        all_names = [glyph.glyphname for glyph in source.glyphs()]
        registry = build_family_registry(source, review_names)
    finally:
        source.close()
    atomic_json(FAMILY_REPORT, registry)
    names = [name for name in all_names if not selected_names or name in selected_names]
    if selected_names - set(all_names):
        raise RuntimeError("unknown glyphs: " + " ".join(sorted(selected_names - set(all_names))))
    previous_rows = {}
    if REPORT.exists():
        with REPORT.open(encoding="utf-8-sig", newline="") as handle:
            previous_rows = {row["glyph"]: row for row in csv.DictReader(handle)}
    results = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(launch_worker, index, name): name
            for index, name in enumerate(names)
        }
        complete = 0
        for future in as_completed(futures):
            index, name, sfd, data = future.result()
            results[name] = (sfd, data)
            complete += 1
            if complete == 1 or complete % 10 == 0 or complete == len(names):
                print(f"italic v9 redraw: {complete}/{len(names)}", flush=True)
    output = fontforge.open(str(OUTPUT_SFD if selected_names and OUTPUT_SFD.exists() else SOURCE_SFD))
    rows = []
    try:
        for name in names:
            sfd, data = results[name]
            row_data = json.loads(data.read_text(encoding="utf-8"))
            worker = fontforge.open(str(sfd))
            try:
                output[name].foreground = worker[name].foreground.dup()
                output[name].width = worker[name].width
            finally:
                worker.close()
            output[name].references = ()
            for reference in row_data.get("component_references", []):
                output[name].addReference(reference[0], tuple(reference[1]))
            rows.append(row_data)
        if selected_names:
            rows.extend(dict(row) for name, row in previous_rows.items() if name not in selected_names)
        OUTPUT_SFD.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_TTF.parent.mkdir(parents=True, exist_ok=True)
        output.save(str(OUTPUT_SFD))
        output.generate(str(OUTPUT_TTF))
    finally:
        output.close()
    # OUTPUT_SFD is cloned directly from the v2 SFD, so FontForge retains the
    # family naming, style flags, encoding, vertical metrics, and lookup state.
    # Do not copy hmtx from the v2 TTF here: that would incorrectly restore the
    # old 374-unit .notdef advance over the requested 341-unit space width.
    export_review_svgs(review_names, index_by_name)
    write_report(rows, review_names, index_by_name)
    if not DECISIONS.exists():
        if PREVIOUS_DECISIONS.exists():
            shutil.copy2(PREVIOUS_DECISIONS, DECISIONS)
        else:
            atomic_json(DECISIONS, {"version": "italic-v9-manual-review-v1", "decisions": {}})
    with REPORT.open(encoding="utf-8-sig", newline="") as handle:
        report_rows = list(csv.DictReader(handle))
    changed_for_review = selected_names or V9_CHANGED_GLYPHS
    migrate_unchanged_decisions(previous_rows, report_rows, changed_for_review)
    invalidate_changed_decisions(changed_for_review)
    immutable_after = {str(path.relative_to(ROOT)): sha256(path) for path in IMMUTABLES}
    if immutable_after != immutable_before:
        raise RuntimeError("an immutable source artifact changed during the v9 build")
    source_points = sum(int(row["source_points"] or 0) for row in report_rows)
    candidate_points = sum(int(row["candidate_points"] or 0) for row in report_rows)
    manifest = {
        "format": "olesuas-hand-italic-v9-redraw-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_sfd": str(SOURCE_SFD.relative_to(ROOT)),
        "output_sfd": str(OUTPUT_SFD.relative_to(ROOT)),
        "output_ttf": str(OUTPUT_TTF.relative_to(ROOT)),
        "glyphs": len(report_rows), "max_points": MAX_POINTS,
        "source_points": source_points, "candidate_points": candidate_points,
        "point_reduction": 1 - candidate_points / max(1, source_points),
        "automatic_passes": sum(row["automatic_pass"] == "true" for row in report_rows),
        "manual_review_required": sum(row["needs_manual_review"] == "true" for row in report_rows),
        "family_candidates_accepted": sum(row["family_status"] == "accepted" for row in report_rows),
        "immutable_hashes": immutable_after,
        "artifact_hashes": {
            str(OUTPUT_SFD.relative_to(ROOT)): sha256(OUTPUT_SFD),
            str(OUTPUT_TTF.relative_to(ROOT)): sha256(OUTPUT_TTF),
            str(REPORT.relative_to(ROOT)): sha256(REPORT),
            str(FAMILY_REPORT.relative_to(ROOT)): sha256(FAMILY_REPORT),
        },
        "release_eligible": False,
    }
    atomic_json(MANIFEST, manifest)
    print(json.dumps({
        "glyphs": len(report_rows), "source_points": source_points,
        "candidate_points": candidate_points,
        "point_reduction": manifest["point_reduction"],
        "automatic_passes": manifest["automatic_passes"],
        "manual_review_required": manifest["manual_review_required"],
    }, indent=2))
    return 0


def finalize() -> int:
    if not REPORT.exists() or not OUTPUT_SFD.exists() or not OUTPUT_TTF.exists():
        raise RuntimeError("build and review Italic v9 before finalization")
    decisions = json.loads(DECISIONS.read_text(encoding="utf-8")).get("decisions", {})
    with REPORT.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    unresolved = []
    for row in rows:
        decision = decisions.get(row["glyph"], {})
        if not (
            decision.get("status") == "pass"
            and decision.get("source_hash") == row["source_hash"]
            and decision.get("candidate_hash") == row["candidate_hash"]
        ):
            unresolved.append(row["glyph"])
    if unresolved:
        raise RuntimeError(
            f"Italic v9 review is incomplete or stale for {len(unresolved)} glyphs; "
            f"first: {unresolved[:12]}"
        )
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest["point_reduction"] < .70:
        raise RuntimeError("Italic v9 does not meet the 70% point-reduction release gate")
    shutil.copy2(OUTPUT_SFD, FINAL_SFD)
    shutil.copy2(OUTPUT_TTF, FINAL_TTF)
    manifest["release_eligible"] = True
    manifest["finalized_at"] = datetime.now(timezone.utc).isoformat()
    manifest["final_artifacts"] = {
        str(FINAL_SFD.relative_to(ROOT)): sha256(FINAL_SFD),
        str(FINAL_TTF.relative_to(ROOT)): sha256(FINAL_TTF),
    }
    atomic_json(MANIFEST, manifest)
    print(f"finalized {FINAL_SFD} and {FINAL_TTF}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--glyph", action="append", help="Rebuild only this glyph; repeat as needed")
    parser.add_argument("--workers", type=int, default=min(8, max(1, os.cpu_count() or 1)))
    parser.add_argument("--finalize", action="store_true")
    parser.add_argument("--worker-glyph", help=argparse.SUPPRESS)
    parser.add_argument("--worker-sfd", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-json", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker_glyph:
        return worker_build(args.worker_glyph, args.worker_sfd, args.worker_json)
    if args.finalize:
        return finalize()
    return build(set(args.glyph or []), args.workers)


if __name__ == "__main__":
    raise SystemExit(main())
