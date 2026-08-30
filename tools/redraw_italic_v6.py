#!/usr/bin/env fontforge
"""Build the isolated, smooth, low-point Italic v6 review candidate.

Run with FontForge's Python runtime:

    fontforge -lang=py -script tools/redraw_italic_v6.py --workers 8
    fontforge -lang=py -script tools/redraw_italic_v6.py --glyph A
    fontforge -lang=py -script tools/redraw_italic_v6.py --finalize

The accepted v2 font is immutable input.  Candidates never exceed 100 editable
foreground points; dense v2 outlines are never used as candidate fallbacks.
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
import redraw_font as rf
import simplify_font as sf
import repair_redraw_v3 as rv3
import repair_redraw_v4 as rv4


SOURCE_SFD = ROOT / "fontforge" / "italic-v2-review.sfd"
SOURCE_TTF = ROOT / "output" / "font" / "OlesuasHand-Italic-v2-review.ttf"
V5_BASE_SFD = ROOT / "fontforge" / "italic-v5-redraw-review.sfd"
REGULAR_FINAL_SFD = ROOT / "regular-final.sfd"
OUTPUT_SFD = ROOT / "fontforge" / "italic-v6-redraw-review.sfd"
OUTPUT_TTF = ROOT / "output" / "font" / "OlesuasHand-Italic-v6-redraw-review.ttf"
FINAL_SFD = ROOT / "fontforge" / "italic-v6.sfd"
FINAL_TTF = ROOT / "output" / "font" / "OlesuasHand-Italic-v6.ttf"
WORK = ROOT / "output" / "italic-v6-redraw"
REPORT = WORK / "italic-v6-glyph-report.csv"
MANIFEST = WORK / "manifest.json"
FAMILY_SOURCE = ROOT / "tools" / "italic_v6_families.json"
FAMILY_REPORT = WORK / "family-registry.json"
DECISIONS = ROOT / "qa" / "italic-v6-manual-decisions.json"
TEMPLATE_MANIFEST = ROOT / "output" / "italic-template" / "italic-template-manifest.json"
SOURCE_SVG_DIR = WORK / "v2-source-glyph-svg"
CANDIDATE_SVG_DIR = WORK / "v6-final-font-glyph-svg"
WORKERS = WORK / "workers"

MAX_POINTS = 100
TARGET_POINT_BUDGETS = (30, 40, 49, 60, 70, 80, 90, 100)
MIN_CURVE_RATIO = 0.70
MAX_UNPROTECTED_SPACING_CV = 0.55
ALGORITHM_CACHE_VERSION = "review-guided-cubic-v17-structural-detail-floor"
ORIGINAL_INFINITY_SVG = (
    ROOT / "output" / "italic-import" / "exact-source-glyph-svg" / "269-infinity.svg"
)
SMOOTH_REVIEW = "almost_done"
DETAIL_REVIEW = "needs_rework"
PURE_SMOOTH_REVIEW = "pure_smooth"
REVIEW_MODE_OVERRIDES = {
    "D": PURE_SMOOTH_REVIEW,
    **{name: SMOOTH_REVIEW for name in (
        "Beta", "S", "Upsilon", "infinity", "o", "seveneighths", "six",
        "uni0416", "uni042A", "uni042B", "uni0435", "zero",
    )},
    **{name: DETAIL_REVIEW for name in (
        "Theta", "g", "uni0412", "uni0414", "uni042E", "uni0437",
        "uni0446", "uni044E",
    )},
}
FORCE_CLEAN_REGULAR = {"uni042D"}

IMMUTABLES = (
    ROOT / "regular-final.sfd",
    ROOT / "bold-final.sfd",
    ROOT / "fontforge" / "italic-review.sfd",
    SOURCE_SFD,
    SOURCE_TTF,
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
            value, f"v5-local-pure-cubic-smoothing-{item['points']}",
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
        "version": "olesuas-hand-italic-v6-family-registry-v1",
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
            point_sets, context, v5_base[name].foreground.dup(),
            "v5-reviewed-baseline",
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
        if name == "D":
            family_spec = {
                "family": "user-directed-smooth-letter", "donor": "v5-D-shape",
                "transform": "preserve-geometry+reclassify-smooth-nodes",
            }
            family = evaluate_custom(
                point_sets, context, v5_base[name].foreground.dup(),
                "user-v5-D-preserved-smooth-node-reclassification",
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
            family = degree_candidate(source, v5_base, point_sets, context)
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
            family = angstrom_candidate(source, v5_base, point_sets, context)
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
                v5_base[name].foreground.dup(), point_sets, context, name,
            )
            if smoothed is not None:
                selected = smoothed
                count += 1
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
                print(f"italic v6 redraw: {complete}/{len(names)}", flush=True)
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
        atomic_json(DECISIONS, {"version": "italic-v6-manual-review-v1", "decisions": {}})
    with REPORT.open(encoding="utf-8-sig", newline="") as handle:
        report_rows = list(csv.DictReader(handle))
    migrate_unchanged_decisions(previous_rows, report_rows, selected_names or set(review_names))
    invalidate_changed_decisions(selected_names or set(review_names))
    immutable_after = {str(path.relative_to(ROOT)): sha256(path) for path in IMMUTABLES}
    if immutable_after != immutable_before:
        raise RuntimeError("an immutable source artifact changed during the v6 build")
    source_points = sum(int(row["source_points"] or 0) for row in report_rows)
    candidate_points = sum(int(row["candidate_points"] or 0) for row in report_rows)
    manifest = {
        "format": "olesuas-hand-italic-v6-redraw-v1",
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
        raise RuntimeError("build and review Italic v6 before finalization")
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
            f"Italic v6 review is incomplete or stale for {len(unresolved)} glyphs; "
            f"first: {unresolved[:12]}"
        )
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest["point_reduction"] < .70:
        raise RuntimeError("Italic v6 does not meet the 70% point-reduction release gate")
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
