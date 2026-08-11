#!/usr/bin/env ffpython
"""Refresh derived TTFs and Bold QA metrics without changing bold.sfd."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
import sys
import unicodedata
from collections import deque
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".font-deps"))
sys.path.insert(0, str(ROOT / "tools"))


import redraw_font as rf
import simplify_font as sf

UPDATED_REGULAR_SFD = ROOT / "fontforge" / "redrawn.sfd"
REGULAR_SFD = ROOT / "fontforge" / "regular-bold-base.sfd"
BOLD_SFD = ROOT / "fontforge" / "bold-v6.sfd"
PREVIOUS_BOLD_SFD = ROOT / "fontforge" / "bold.sfd"
RAW_BOLD_SFD = ROOT / "fontforge" / "bold-raw.sfd"
REGULAR_TTF = ROOT / "qa" / "assets" / "redrawn.ttf"
BASE_TTF = ROOT / "qa" / "assets" / "regular-bold-base.ttf"
BOLD_TTF = ROOT / "qa" / "assets" / "bold-v6.ttf"
RAW_BOLD_TTF = ROOT / "qa" / "assets" / "bold-raw.ttf"
REPORT = ROOT / "qa" / "assets" / "bold-report.csv"
META = ROOT / "qa" / "assets" / "bold-report.json"
BUILD_INFO = ROOT / "qa" / "assets" / "bold-build.json"
METRICS_VERSION = "bold-metrics-v6"
ALWAYS_FILL_COUNTERS = {"asterisk", "uni041D", "uni0427", "uni043D"}
FORCE_SOLID_COUNTERS = ALWAYS_FILL_COUNTERS | {"t", "u", "z"}
# Semantic targets for the audited v4 generic-blocker cohort. Zero means that
# persistent raster holes are overlap artifacts rather than required counters.
AUDITED_COUNTER_TARGETS = {
    "hyphen": 0, "two": 0, "seven": 0, "equal": 0,
    "A": 1, "C": 0, "D": 1, "E": 0, "I": 0, "L": 0, "R": 1,
    "c": 0, "g": 1, "h": 0, "i": 0, "q": 1, "r": 0,
    "uni0401": 0, "uni0411": 1, "uni0414": 1, "uni0415": 0,
    "uni041A": 0, "uni041C": 0, "uni041F": 0, "uni0423": 0,
    "uni0431": 1, "uni0432": 2, "asciicircum": 0, "section": 2,
    "plusminus": 0, "uni00B2": 0, "uni00B3": 0, "onequarter": 0,
    "threequarters": 0, "multiply": 0, "Alpha": 1, "Epsilon": 0,
    "Theta": 2, "Pi": 0, "beta": 2, "gamma": 0, "iota": 0,
    "tau": 0, "uni2011": 0, "uni201F": 0, "uni2075": 0,
    "uni2076": 1, "uni2078": 2, "uni207C": 0, "uni20A9": 0,
    "oneeighth": 2, "threeeighths": 2, "seveneighths": 2,
    "notelement": 0, "phi": 2, "uni2086": 1, "uni2088": 2,
    "uni2089": 1, "uni208C": 0, "uni212B": 2,
}
READY_PATH = ROOT / "qa" / "bold-ready-to-pass.json"
V4_REPORT = ROOT / "checkpoints" / "bold-v4" / "bold-report.csv"
RASTER_SIZES = (32, 64, 128)
TOPOLOGY_SIZES = (128, 256, 512)

FIELDS = [
    "glyph", "codepoint", "char", "category", "regular_hash", "base_hash", "bold_hash",
    "previous_bold_hash", "changed_in_version",
    "base_cleanup_status", "bold_cleanup_status", "protection_status", "manual_blockers",
    "blocker_classification", "blocker_detail",
    "persistent_regular_components", "persistent_bold_components", "required_counters",
    "regular_outline_counters", "bold_outline_counters", "semantic_counter_target",
    "ready_to_pass", "intentional_counter_fills",
    "unmatched_white_regions", "matched_required_counters",
    "regular_points", "bold_points", "point_delta", "point_ratio",
    "regular_contours", "bold_contours", "cleanup_method", "repair_method",
    "pre_repair_points", "repair_iou_64", "repair_iou_128",
    "short_segments", "curvature_reversals", "boundary_spikes", "roughness_score",
    "dot_center_delta_x", "dot_center_delta_y", "dot_diameter_ratio",
    "regular_width", "bold_width", "width_delta",
    "regular_lsb", "bold_lsb", "regular_rsb", "bold_rsb",
    "left_expansion", "right_expansion", "outside_advance",
    "expansion_p10", "expansion_median", "expansion_p90", "expansion_spread",
    "regular_self_intersections", "bold_self_intersections",
    "regular_invalid_handles", "bold_invalid_handles",
    "regular_open_contours", "bold_open_contours",
    "regular_validation", "bold_validation",
    "counter_area_ratio", "counter_width_ratio", "hard_warnings", "warnings", "automatic_warning",
]
for size in RASTER_SIZES:
    FIELDS.extend([
        "ink_ratio_{}".format(size), "ink_iou_{}".format(size),
    ])
for size in TOPOLOGY_SIZES:
    FIELDS.extend([
        "regular_components_{}".format(size), "regular_counters_{}".format(size),
        "bold_components_{}".format(size), "bold_counters_{}".format(size),
    ])


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def points(layer):
    return sum(len(contour) for contour in layer)


def layer_sets(layer):
    pen = sf.FlattenPen(error=1.0, spacing=4.0)
    layer.draw(pen)
    if pen.points:
        pen.endPath()
    return pen.contours


def padded_union_bbox(sets_a, sets_b):
    sets = [contour for contour in sets_a + sets_b if contour]
    if not sets:
        return (-16.0, -16.0, 16.0, 16.0)
    box = sf.bbox_of_sets(sets)
    padding = max(8.0, math.hypot(box[2] - box[0], box[3] - box[1]) * 0.04)
    return (box[0] - padding, box[1] - padding, box[2] + padding, box[3] + padding)


def quantile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def boundary_expansion(regular_sets, bold_sets):
    if not regular_sets or not bold_sets:
        return 0.0, 0.0, 0.0
    targets = [
        contour[::max(1, len(contour) // 300)] for contour in bold_sets
    ]
    distances = []
    for contour in regular_sets:
        for point in contour[::max(1, len(contour) // 120)]:
            distances.append(sf.distance_to_sets(point, targets))
    return (
        quantile(distances, 0.10),
        quantile(distances, 0.50),
        quantile(distances, 0.90),
    )


def enclosed_area(mask, size):
    outside = bytearray(size * size)
    queue = deque()
    for x in range(size):
        queue.append(x)
        queue.append((size - 1) * size + x)
    for y in range(size):
        queue.append(y * size)
        queue.append(y * size + size - 1)
    while queue:
        index = queue.popleft()
        if index < 0 or index >= len(mask) or outside[index] or mask[index]:
            continue
        outside[index] = 1
        x, y = index % size, index // size
        if x:
            queue.append(index - 1)
        if x + 1 < size:
            queue.append(index + 1)
        if y:
            queue.append(index - size)
        if y + 1 < size:
            queue.append(index + size)
    return sum(1 for index, value in enumerate(mask) if not value and not outside[index])


def counter_width_proxy(mask, size):
    """Approximate the narrowest counter width from raster run spans."""
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
    enclosed = [not mask[i] and not outside[i] for i in range(size * size)]
    seen = bytearray(size * size)
    widths = []
    for seed, value in enumerate(enclosed):
        if not value or seen[seed]:
            continue
        component, flood = [], deque([seed])
        seen[seed] = 1
        while flood:
            index = flood.popleft(); component.append(index)
            x, y = index % size, index // size
            neighbors = (index - 1 if x else -1, index + 1 if x + 1 < size else -1,
                         index - size if y else -1, index + size if y + 1 < size else -1)
            for neighbor in neighbors:
                if neighbor >= 0 and enclosed[neighbor] and not seen[neighbor]:
                    seen[neighbor] = 1; flood.append(neighbor)
        rows, columns = {}, {}
        for index in component:
            x, y = index % size, index // size
            rows.setdefault(y, []).append(x); columns.setdefault(x, []).append(y)
        horizontal = max((max(v) - min(v) + 1 for v in rows.values()), default=0)
        vertical = max((max(v) - min(v) + 1 for v in columns.values()), default=0)
        widths.append(min(horizontal, vertical))
    return min(widths) if widths else 0

def oncurve_points(layer):
    return [point for contour in layer for point in contour if point.on_curve]


def smoothness_metrics(layer):
    short = 0
    reversals = 0
    for contour in layer:
        anchors = [point for point in contour if point.on_curve]
        if len(anchors) < 2:
            continue
        pairs = zip(anchors, anchors[1:] + ([anchors[0]] if contour.closed else []))
        short += sum(math.hypot(b.x - a.x, b.y - a.y) < 2.0 for a, b in pairs)
        if len(anchors) < 4:
            continue
        signs = []
        triples = zip(anchors, anchors[1:] + anchors[:1], anchors[2:] + anchors[:2])
        for a, b, c in triples:
            cross = (b.x - a.x) * (c.y - b.y) - (b.y - a.y) * (c.x - b.x)
            if abs(cross) > 4.0:
                signs.append(1 if cross > 0 else -1)
        reversals += sum(a != b for a, b in zip(signs, signs[1:] + signs[:1])) if signs else 0
    return short, reversals


def spike_metrics(layer):
    spikes = 0
    anchors_total = 0
    for contour in layer:
        anchors = [point for point in contour if point.on_curve]
        anchors_total += len(anchors)
        if len(anchors) < 3:
            continue
        for index, current in enumerate(anchors):
            previous = anchors[index - 1]
            following = anchors[(index + 1) % len(anchors)]
            left = math.hypot(current.x - previous.x, current.y - previous.y)
            right = math.hypot(following.x - current.x, following.y - current.y)
            if min(left, right) > 18 or not left or not right:
                continue
            dot = ((previous.x - current.x) * (following.x - current.x)
                   + (previous.y - current.y) * (following.y - current.y)) / (left * right)
            angle = math.degrees(math.acos(max(-1.0, min(1.0, dot))))
            if angle < 38:
                spikes += 1
    return spikes, anchors_total
def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default


def repair_index():
    result = {}
    cleanup = load_json(ROOT / "qa" / "assets" / "bold-v3-cleanup.json", {"glyphs": []})
    for row in cleanup.get("glyphs", []):
        if row.get("status") == "ok":
            result[row["glyph"]] = dict(row, source="cleanup")
    geometric = load_json(ROOT / "qa" / "assets" / "bold-v3-geometric-repairs.json", {"glyphs": {}})
    for name, row in geometric.get("glyphs", {}).items():
        result[name] = dict(row, glyph=name, source="geometric")
    targets = load_json(ROOT / "qa" / "assets" / "bold-v3-target-fixes.json", {"glyphs": []})
    for row in targets.get("glyphs", []):
        if row.get("status") == "ok":
            result[row["glyph"]] = dict(row, source="target-cleanup")
    dots = load_json(ROOT / "qa" / "assets" / "bold-v3-dot-repairs.json", {"glyphs": {}})
    for name, row in dots.get("glyphs", {}).items():
        result[name] = dict(row, glyph=name, source="dot-repair")
    dot_protection = load_json(ROOT / "qa" / "assets" / "bold-v3-dot-protection.json", {"glyphs": {}})
    for name, row in dot_protection.get("glyphs", {}).items():
        result[name] = dict(row, glyph=name, source="dot-protection")
    for filename in (
        "bold-v4-repairs.json", "bold-v4-pathops.json",
        "bold-v4-final-cleanup.json", "bold-v4-residuals.json",
        "bold-v4-counter-resolution.json", "bold-v4-raster-rebuild.json",
        "bold-v4-canonical-rings.json", "bold-v4-percent-union.json",
        "bold-v4-uni0451-dots.json", "bold-v4-six-counters.json",
    ):
        payload = load_json(ROOT / "qa" / "assets" / filename, {"glyphs": {}})
        for name, row in payload.get("glyphs", {}).items():
            if row.get("status") == "ok":
                result[name] = dict(row, glyph=name, source="v4")
    v6 = load_json(ROOT / "qa" / "assets" / "bold-v6-repairs.json", {"glyphs": {}})
    for name, row in v6.get("glyphs", {}).items():
        result[name] = dict(row, glyph=name, source="v6")
    return result


def ready_index():
    payload = load_json(READY_PATH, {"glyphs": {}})
    glyphs = payload.get("glyphs", {})
    return glyphs if isinstance(glyphs, dict) else {name: True for name in glyphs}
def category_for(glyph):
    codepoint = glyph.unicode
    name = glyph.glyphname.lower()
    if codepoint < 0:
        return "other"
    category = unicodedata.category(chr(codepoint))
    if "fraction" in name or 0x2150 <= codepoint <= 0x218F:
        return "fractions"
    if category.startswith("L"):
        return "letters"
    if category.startswith("N"):
        return "numerals"
    if category == "Sc":
        return "currency"
    if category.startswith("P"):
        return "punctuation"
    if category.startswith("S") or category.startswith("M"):
        return "mathematics"
    return "other"


def sidebearings(glyph):
    box = glyph.boundingBox()
    if box[2] <= box[0]:
        return 0.0, float(glyph.width), box
    return box[0], glyph.width - box[2], box


def fmt(value):
    return "{:.6f}".format(float(value))


def persistent_count(values):
    """Return the modal raster count, preferring the higher count on a tie."""
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts, key=lambda value: (counts[value], value))


def nested_contour_count(point_sets):
    """Count outline contours genuinely nested inside another contour."""
    nested = 0
    for index, contour in enumerate(point_sets):
        if len(contour) < 3:
            continue
        sample = contour[0]
        depth = sum(
            1 for other_index, other in enumerate(point_sets)
            if other_index != index and len(other) >= 3 and sf.point_in_polygon(sample, other)
        )
        if depth % 2:
            nested += 1
    return nested


def robust_outliers(rows, field):
    by_category = {}
    for row in rows:
        if float(row.get("regular_points") or 0) <= 0:
            continue
        by_category.setdefault(row["category"], []).append(float(row[field]))
    outliers = set()
    for category, values in by_category.items():
        if len(values) < 5:
            continue
        median = statistics.median(values)
        deviations = [abs(value - median) for value in values]
        mad = statistics.median(deviations)
        if mad <= 1e-9:
            continue
        for row in rows:
            if row["category"] == category and abs(float(row[field]) - median) > 3 * mad:
                outliers.add(row["glyph"])
    return outliers


def main():
    if not REGULAR_SFD.is_file() or not BOLD_SFD.is_file():
        raise SystemExit("Regular or Bold SFD is missing")
    REGULAR_TTF.parent.mkdir(parents=True, exist_ok=True)
    updated_regular = fontforge.open(str(UPDATED_REGULAR_SFD))
    regular = fontforge.open(str(REGULAR_SFD))
    bold = fontforge.open(str(BOLD_SFD))
    previous_bold = fontforge.open(str(PREVIOUS_BOLD_SFD))
    raw_bold = fontforge.open(str(RAW_BOLD_SFD))
    build = json.loads(BUILD_INFO.read_text(encoding="utf-8")) if BUILD_INFO.exists() else {}
    methods = build.get("cleanup_methods", {})
    def csv_index(path):
        if not path.exists():
            return {}
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return {row["glyph"]: row for row in csv.DictReader(handle)}
    base_cleanup = csv_index(ROOT / "qa" / "assets" / "bold-base-cleanup.csv")
    bold_cleanup = csv_index(ROOT / "qa" / "assets" / "bold-post-cleanup.csv")
    legacy_v4 = csv_index(V4_REPORT)
    legacy_broad = {
        name for name, item in legacy_v4.items()
        if "counter-or-component-blocked" in item.get("manual_blockers", "")
    }
    protection_path = ROOT / "qa" / "assets" / "bold-protection.json"
    protection_data = json.loads(protection_path.read_text(encoding="utf-8")) if protection_path.exists() else {"glyphs": []}
    protection = {row["glyph"]: row for row in protection_data.get("glyphs", [])}
    repairs = repair_index()
    ready = ready_index()
    dot_targets = {}
    for filename in ("bold-v4-repairs.json", "bold-v4-residuals.json", "bold-v4-uni0451-dots.json"):
        payload = load_json(ROOT / "qa" / "assets" / filename, {"glyphs": {}})
        for name, item in payload.get("glyphs", {}).items():
            if item.get("dots"):
                dot_targets[name] = item["dots"]
    rows = []
    try:
        updated_regular.generate(str(REGULAR_TTF))
        regular.generate(str(BASE_TTF))
        bold.generate(str(BOLD_TTF))
        raw_bold.generate(str(RAW_BOLD_TTF))
        regular_glyphs = list(regular.glyphs())
        updated_by_name = {glyph.glyphname: glyph for glyph in updated_regular.glyphs()}
        bold_by_name = {glyph.glyphname: glyph for glyph in bold.glyphs()}
        previous_by_name = {glyph.glyphname: glyph for glyph in previous_bold.glyphs()}
        regular_names = [glyph.glyphname for glyph in regular_glyphs]
        bold_names = [glyph.glyphname for glyph in bold.glyphs()]
        if set(regular_names) != set(bold_names):
            raise RuntimeError("Regular and Bold glyph sets differ")
        for index, rg in enumerate(regular_glyphs):
            bg = bold_by_name[rg.glyphname]
            rlayer, blayer = rg.foreground, bg.foreground
            rsets, bsets = layer_sets(rlayer), layer_sets(blayer)
            bbox = padded_union_bbox(rsets, bsets)
            rpoints, bpoints = points(rlayer), points(blayer)
            rlsb, rrsb, rbox = sidebearings(rg)
            blsb, brsb, bbox_ink = sidebearings(bg)
            p10, p50, p90 = boundary_expansion(rsets, bsets)
            hard = []
            warnings = []
            rintersections = sum(1 for contour in rlayer if contour.selfIntersects())
            bintersections = sum(1 for contour in blayer if contour.selfIntersects())
            ropen = sum(1 for contour in rlayer if not contour.closed)
            bopen = sum(1 for contour in blayer if not contour.closed)
            rhandles, bhandles = rf.invalid_handles(rlayer), rf.invalid_handles(blayer)
            repair = repairs.get(rg.glyphname, {})
            dot_dx = dot_dy = 0.0
            dot_ratio = 1.0
            targets = dot_targets.get(rg.glyphname, [])
            if targets:
                ellipses = [
                    contour for contour in blayer
                    if sum(point.on_curve for point in contour) == 4
                    and 30 <= contour.boundingBox()[2] - contour.boundingBox()[0] <= 240
                ]
                available = list(ellipses)
                deltas, ratios = [], []
                for target in targets:
                    if not available:
                        break
                    tx, ty = target["center_x"], target["center_y"]
                    contour = min(available, key=lambda item: math.hypot(
                        (item.boundingBox()[0] + item.boundingBox()[2]) / 2.0 - tx,
                        (item.boundingBox()[1] + item.boundingBox()[3]) / 2.0 - ty,
                    ))
                    available.remove(contour)
                    box = contour.boundingBox()
                    cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
                    deltas.append((cx - tx, cy - ty))
                    ratios.append(max(box[2] - box[0], box[3] - box[1]) / target["new_diameter"])
                if deltas:
                    dot_dx = max(deltas, key=lambda item: abs(item[0]))[0]
                    dot_dy = max(deltas, key=lambda item: abs(item[1]))[1]
                    dot_ratio = max(ratios, key=lambda value: abs(value - 1.0))
            short_segments, curvature_reversals = smoothness_metrics(blayer)
            boundary_spikes, anchor_total = spike_metrics(blayer)
            roughness_score = (2 * short_segments + curvature_reversals + 3 * boundary_spikes) / max(1, anchor_total)
            if bool(rpoints) != bool(bpoints):
                hard.append("empty-mismatch")
            if bintersections > rintersections:
                hard.append("new-self-intersection")
            if bhandles > rhandles:
                hard.append("new-invalid-handles")
            if bopen > ropen:
                hard.append("new-open-contour")
            if bpoints > max(rpoints * 3, rpoints + 100):
                warnings.append("high-point-growth")
            if p50 and not 8 <= p50 <= 32:
                warnings.append("median-expansion")
            outside = max(0.0, -bbox_ink[0]) + max(0.0, bbox_ink[2] - bg.width)
            regular_outside = max(0.0, -rbox[0]) + max(0.0, rbox[2] - rg.width)
            if outside > regular_outside + 5:
                warnings.append("outside-advance")
            row = {
                "glyph": rg.glyphname,
                "codepoint": rg.unicode if rg.unicode >= 0 else "",
                "char": chr(rg.unicode) if rg.unicode >= 0 else "",
                "category": category_for(rg),
                "regular_hash": rf.layer_hash(updated_by_name[rg.glyphname].foreground),
                "base_hash": rf.layer_hash(rlayer),
                "bold_hash": rf.layer_hash(blayer),
                "previous_bold_hash": rf.layer_hash(previous_by_name[rg.glyphname].foreground),
                "changed_in_version": "true" if rf.layer_hash(previous_by_name[rg.glyphname].foreground) != rf.layer_hash(blayer) else "false",
                "base_cleanup_status": base_cleanup.get(rg.glyphname, {}).get("status", "unknown"),
                "bold_cleanup_status": "clean" if not (bintersections or bhandles or bopen) else "blocked",
                "protection_status": protection.get(rg.glyphname, {}).get("status", "not-needed"),
                "manual_blockers": "",
                "blocker_classification": "",
                "blocker_detail": "",
                "persistent_regular_components": 0,
                "persistent_bold_components": 0,
                "required_counters": 0,
                "regular_outline_counters": 0,
                "bold_outline_counters": 0,
                "semantic_counter_target": "",
                "ready_to_pass": "true" if rg.glyphname in ready and rf.layer_hash(previous_by_name[rg.glyphname].foreground) == rf.layer_hash(blayer) else "false",
                "intentional_counter_fills": "false",
                "unmatched_white_regions": 0,
                "matched_required_counters": 0,
                "regular_points": rpoints,
                "bold_points": bpoints,
                "point_delta": bpoints - rpoints,
                "point_ratio": fmt(bpoints / max(1, rpoints)) if rpoints else "1.000000",
                "regular_contours": len(rlayer),
                "bold_contours": len(blayer),
                "cleanup_method": methods.get(rg.glyphname, "manual-or-unknown"),
                "repair_method": repair.get("method", repair.get("weight_method", "unchanged")),
                "pre_repair_points": repair.get("old_points", bpoints),
                "repair_iou_64": fmt(repair.get("iou_64", 1.0)),
                "repair_iou_128": fmt(repair.get("iou_128", 1.0)),
                "short_segments": short_segments,
                "curvature_reversals": curvature_reversals,
                "boundary_spikes": boundary_spikes,
                "roughness_score": fmt(roughness_score),
                "dot_center_delta_x": fmt(dot_dx),
                "dot_center_delta_y": fmt(dot_dy),
                "dot_diameter_ratio": fmt(dot_ratio),
                "regular_width": rg.width,
                "bold_width": bg.width,
                "width_delta": bg.width - rg.width,
                "regular_lsb": fmt(rlsb), "bold_lsb": fmt(blsb),
                "regular_rsb": fmt(rrsb), "bold_rsb": fmt(brsb),
                "left_expansion": fmt(rlsb - blsb),
                "right_expansion": fmt(rrsb - brsb),
                "outside_advance": fmt(outside),
                "expansion_p10": fmt(p10), "expansion_median": fmt(p50),
                "expansion_p90": fmt(p90), "expansion_spread": fmt(p90 - p10),
                "regular_self_intersections": rintersections,
                "bold_self_intersections": bintersections,
                "regular_invalid_handles": rhandles,
                "bold_invalid_handles": bhandles,
                "regular_open_contours": ropen, "bold_open_contours": bopen,
                "regular_validation": rg.validate(True),
                "bold_validation": bg.validate(True),
                "counter_area_ratio": "1.000000",
                "counter_width_ratio": "1.000000",
            }
            r_counter_area = b_counter_area = 0
            r_counter_width = b_counter_width = 0
            regular_topology, bold_topology = {}, {}
            r_ink_128 = 0
            for size in sorted(set(RASTER_SIZES + TOPOLOGY_SIZES)):
                rmask = sf.rasterize(rsets, bbox, size) if rsets else bytes(size * size)
                bmask = sf.rasterize(bsets, bbox, size) if bsets else bytes(size * size)
                rt, bt = sf.topology(rmask, size), sf.topology(bmask, size)
                regular_topology[size], bold_topology[size] = rt, bt
                if size in RASTER_SIZES:
                    r_ink = sum(rmask)
                    b_ink = sum(bmask)
                    row["ink_ratio_{}".format(size)] = fmt(b_ink / max(1, r_ink))
                    row["ink_iou_{}".format(size)] = fmt(sf.raster_metrics(rmask, bmask)["ink_iou"])
                if size in TOPOLOGY_SIZES:
                    row["regular_components_{}".format(size)] = rt[0]
                    row["regular_counters_{}".format(size)] = rt[1]
                    row["bold_components_{}".format(size)] = bt[0]
                    row["bold_counters_{}".format(size)] = bt[1]
                    if size == 128:
                        r_ink_128 = sum(rmask)
                        r_counter_area = enclosed_area(rmask, size)
                        b_counter_area = enclosed_area(bmask, size)
                        r_counter_width = counter_width_proxy(rmask, size)
                        b_counter_width = counter_width_proxy(bmask, size)
            force_solid = rg.glyphname in FORCE_SOLID_COUNTERS
            regular_visible_ratio = r_counter_area / max(1, r_ink_128)
            regular_counter_counts = [regular_topology[size][1] for size in TOPOLOGY_SIZES]
            bold_counter_counts = [bold_topology[size][1] for size in TOPOLOGY_SIZES]
            regular_component_counts = [regular_topology[size][0] for size in TOPOLOGY_SIZES]
            bold_component_counts = [bold_topology[size][0] for size in TOPOLOGY_SIZES]
            persistent_regular_components = persistent_count(regular_component_counts)
            persistent_bold_components = persistent_count(bold_component_counts)
            counter_persists = all(value > 0 for value in regular_counter_counts)
            counter_visible = regular_topology.get(64, (0, 0))[1] > 0 or regular_visible_ratio > 0.02
            regular_outline_counters = nested_contour_count(rsets)
            bold_outline_counters = nested_contour_count(bsets)
            semantic_target = AUDITED_COUNTER_TARGETS.get(rg.glyphname)
            geometric_target = min(min(regular_counter_counts), regular_outline_counters) if counter_visible and counter_persists else 0
            required_counters = 0 if force_solid else min(semantic_target, geometric_target) if semantic_target is not None else geometric_target
            persistent_bold_counters = min(bold_counter_counts)
            maximum_bold_counters = max(bold_topology[256][1], bold_topology[512][1])
            unmatched = max(0, maximum_bold_counters - required_counters)
            matched = min(required_counters, persistent_bold_counters)
            intentional_fill = force_solid and regular_topology.get(128, (0, 0))[1] > 0
            row["intentional_counter_fills"] = "true" if intentional_fill else "false"
            row["unmatched_white_regions"] = unmatched
            row["matched_required_counters"] = matched
            row["persistent_regular_components"] = persistent_regular_components
            row["persistent_bold_components"] = persistent_bold_components
            row["required_counters"] = required_counters
            row["regular_outline_counters"] = regular_outline_counters
            row["bold_outline_counters"] = bold_outline_counters
            row["semantic_counter_target"] = "" if semantic_target is None else semantic_target

            classifications = []
            blockers = []
            if row["base_cleanup_status"] == "base_cleanup_blocked":
                blockers.append("contour-cleanup-defect")
                classifications.append("contour-cleanup-defect")
            if bintersections or bhandles or bopen:
                blockers.append("contour-cleanup-defect")
                classifications.append("contour-cleanup-defect")
            if persistent_bold_components < persistent_regular_components:
                blockers.append("visible-component-merged")
                classifications.append("visible-component-merged")
            elif persistent_bold_components > persistent_regular_components:
                blockers.append("unexpected-component-added")
                classifications.append("unexpected-component-added")
            elif regular_component_counts != bold_component_counts and rg.glyphname in legacy_broad:
                classifications.append("metric-false-positive")
                warnings.append("metric-false-positive")
            if unmatched:
                blockers.append("unexpected-component-added")
                classifications.append("unexpected-component-added")
                hard.append("unmatched-white-region")
            if matched < required_counters:
                blockers.append("required-counter-lost")
                classifications.append("required-counter-lost")
                hard.append("required-counter-lost")
            if required_counters and r_counter_area:
                ratio = b_counter_area / r_counter_area
                row["counter_area_ratio"] = fmt(ratio)
                width_ratio = b_counter_width / max(1, r_counter_width)
                row["counter_width_ratio"] = fmt(width_ratio)
                if ratio < 0.75 or width_ratio < 0.70:
                    blockers.append("required-counter-too-small")
                    classifications.append("required-counter-too-small")
                if ratio < 0.75:
                    warnings.append("counter-area")
                if width_ratio < 0.70:
                    warnings.append("counter-width")
            if rg.glyphname in legacy_broad and not classifications:
                classifications.append("metric-false-positive")
                warnings.append("metric-false-positive")
            blockers = sorted(set(blockers))
            classifications = sorted(set(classifications))
            detail = {
                "regular_components": regular_component_counts,
                "bold_components": bold_component_counts,
                "persistent_components": [persistent_regular_components, persistent_bold_components],
                "regular_counters": regular_counter_counts,
                "bold_counters": bold_counter_counts,
                "required_counters": required_counters,
                "outline_counters": [regular_outline_counters, bold_outline_counters],
                "matched_counters": matched,
                "counter_area_ratio": float(row["counter_area_ratio"]),
                "counter_width_ratio": float(row["counter_width_ratio"]),
            }
            row["blocker_classification"] = ";".join(classifications)
            row["blocker_detail"] = json.dumps(detail, sort_keys=True, separators=(",", ":"))
            row["manual_blockers"] = ";".join(blockers)
            hard.extend(blockers)
            row["_hard"] = hard
            row["_warnings"] = warnings
            rows.append(row)

        ink_outliers = robust_outliers(rows, "ink_ratio_128")
        expansion_outliers = robust_outliers(rows, "expansion_median")
        for row in rows:
            if row["glyph"] in ink_outliers:
                row["_warnings"].append("category-ink-outlier")
            if row["glyph"] in expansion_outliers:
                row["_warnings"].append("category-expansion-outlier")
            row["hard_warnings"] = ";".join(sorted(set(row.pop("_hard"))))
            row["warnings"] = ";".join(sorted(set(row.pop("_warnings"))))
            row["automatic_warning"] = "true" if row["hard_warnings"] or row["warnings"] else "false"
    finally:
        updated_regular.close()
        regular.close()
        bold.close()
        previous_bold.close()
        raw_bold.close()

    with REPORT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


    payload = {
        "version": METRICS_VERSION,
        "glyph_count": len(rows),
        "outlined_glyphs": sum(int(row["regular_points"]) > 0 for row in rows),
        "hard_warning_count": sum(bool(row["hard_warnings"]) for row in rows),
        "warning_count": sum(row["automatic_warning"] == "true" for row in rows),
        "ready_to_pass_count": sum(row["ready_to_pass"] == "true" for row in rows),
        "intentional_counter_fill_count": sum(row["intentional_counter_fills"] == "true" for row in rows),
        "unmatched_white_region_count": sum(int(row["unmatched_white_regions"]) for row in rows),
        "self_intersection_glyph_count": sum(int(row["bold_self_intersections"]) > 0 for row in rows),
        "legacy_broad_blocker_count": len(legacy_broad),
        "legacy_broad_classified_count": sum(
            row["glyph"] in legacy_broad and bool(row["blocker_classification"]) for row in rows
        ),
        "regular_sfd_hash": sha256(UPDATED_REGULAR_SFD),
        "base_sfd_hash": sha256(REGULAR_SFD),
        "bold_sfd_hash": sha256(BOLD_SFD),
        "previous_bold_sfd_hash": sha256(PREVIOUS_BOLD_SFD),
        "regular_ttf_hash": sha256(REGULAR_TTF),
        "base_ttf_hash": sha256(BASE_TTF),
        "bold_ttf_hash": sha256(BOLD_TTF),
        "raw_bold_ttf_hash": sha256(RAW_BOLD_TTF),
    }
    payload["build_id"] = hashlib.sha256(
        (payload["regular_ttf_hash"] + payload["bold_ttf_hash"]).encode("ascii")
    ).hexdigest()[:16]
    META.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
