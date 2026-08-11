#!/usr/bin/env ffpython
"""Audit all v5.1 Almost Done glyphs for cleanup notches and overexpansion."""
from __future__ import annotations

import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".font-deps"), str(ROOT / "tools")]
import repair_bold_v4 as v4
import simplify_font as sf

REGULAR = ROOT / "fontforge" / "redrawn.sfd"
BOLD = ROOT / "fontforge" / "bold.sfd"
RAW = ROOT / "fontforge" / "bold-raw.sfd"
DECISIONS = ROOT / "qa" / "bold-manual-decisions.json"
REPORT = ROOT / "qa" / "assets" / "bold-report.csv"
OUTPUT = ROOT / "qa" / "assets" / "bold-v51-indent-audit.json"
SIZES = (128, 256, 512, 1024)
CANONICAL_OVERRIDES = {"at", "d", "q", "uni0432", "uni0443", "beta", "rho"}


def layer_sets(layer):
    return v4.layer_sets(layer, error=0.75, spacing=3.0)


def bbox_union(*sets_groups):
    contours = [contour for group in sets_groups for contour in group if contour]
    if not contours:
        return (-16.0, -16.0, 16.0, 16.0)
    box = sf.bbox_of_sets(contours)
    pad = max(8.0, max(box[2] - box[0], box[3] - box[1]) * 0.04)
    return box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad


def difference_components(reference, candidate, size):
    """Return connected regions where reference has ink and candidate does not."""
    parents, areas, bounds, sums = [], [], [], []

    def find(value):
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    def union(left, right):
        left, right = find(left), find(right)
        if left == right:
            return left
        if areas[left] < areas[right]:
            left, right = right, left
        parents[right] = left
        areas[left] += areas[right]
        bounds[left][0] = min(bounds[left][0], bounds[right][0])
        bounds[left][1] = min(bounds[left][1], bounds[right][1])
        bounds[left][2] = max(bounds[left][2], bounds[right][2])
        bounds[left][3] = max(bounds[left][3], bounds[right][3])
        sums[left][0] += sums[right][0]
        sums[left][1] += sums[right][1]
        return left

    previous = []
    for row in range(size):
        runs = []
        offset = row * size
        column = 0
        while column < size:
            active = reference[offset + column] >= 0.5 and candidate[offset + column] < 0.5
            if not active:
                column += 1
                continue
            start = column
            while column + 1 < size:
                next_column = column + 1
                if not (
                    reference[offset + next_column] >= 0.5
                    and candidate[offset + next_column] < 0.5
                ):
                    break
                column = next_column
            end = column
            identifier = len(parents)
            count = end - start + 1
            parents.append(identifier)
            areas.append(count)
            bounds.append([start, row, end, row])
            sums.append([(start + end) * count / 2.0, row * count])
            runs.append([start, end, identifier])
            column += 1
        prior_index = 0
        for run in runs:
            while prior_index < len(previous) and previous[prior_index][1] < run[0] - 1:
                prior_index += 1
            scan = prior_index
            while scan < len(previous) and previous[scan][0] <= run[1] + 1:
                run[2] = union(run[2], previous[scan][2])
                scan += 1
        previous = runs
    roots = {find(index) for index in range(len(parents))}
    result = []
    for root in roots:
        area = areas[root]
        if area < sf.minimum_region_pixels(size):
            continue
        x0, y0, x1, y1 = bounds[root]
        result.append({
            "area_pixels": area,
            "bbox_pixels": [x0, y0, x1 + 1, y1 + 1],
            "centroid_pixels": [sums[root][0] / area, sums[root][1] / area],
        })
    return sorted(result, key=lambda value: value["area_pixels"], reverse=True)


def normalize_region(region, size, box):
    if not region:
        return None
    x0, y0, x1, y1 = region["bbox_pixels"]
    left, bottom, right, top = box
    width, height = right - left, top - bottom
    cx, cy = region["centroid_pixels"]
    return {
        **region,
        "bbox_normalized": [x0 / size, y0 / size, x1 / size, y1 / size],
        "bbox_font_units": [
            left + x0 / size * width,
            top - y1 / size * height,
            left + x1 / size * width,
            top - y0 / size * height,
        ],
        "centroid_normalized": [cx / size, cy / size],
        "centroid_font_units": [left + cx / size * width, top - cy / size * height],
        "mouth_width_fu": (x1 - x0) / size * width,
        "depth_fu": (y1 - y0) / size * height,
    }


def bbox_iou(left, right):
    if not left or not right:
        return 0.0
    a, b = left["bbox_normalized"], right["bbox_normalized"]
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    union = (
        max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
        + max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
        - intersection
    )
    return intersection / max(union, 1e-9)


def dimensions(box):
    return max(0.0, box[2] - box[0]), max(0.0, box[3] - box[1])


def main():
    decisions = json.loads(DECISIONS.read_text(encoding="utf-8-sig"))["decisions"]
    with REPORT.open(encoding="utf-8-sig", newline="") as handle:
        report_rows = {row["glyph"]: row for row in csv.DictReader(handle)}
    names = [
        name for name, decision in decisions.items()
        if decision.get("status") == "almost_done"
    ]
    if len(names) != 60:
        raise SystemExit("expected 60 Almost Done glyphs, found {}".format(len(names)))
    regular = fontforge.open(str(REGULAR))
    bold = fontforge.open(str(BOLD))
    raw = fontforge.open(str(RAW))
    rows = {}
    try:
        for name in names:
            regular_sets = layer_sets(regular[name].foreground)
            bold_sets = layer_sets(bold[name].foreground)
            raw_sets = layer_sets(raw[name].foreground)
            box = bbox_union(regular_sets, bold_sets, raw_sets)
            missing, added, raster = {}, {}, {}
            for size in SIZES:
                regular_mask = sf.rasterize(regular_sets, box, size)
                bold_mask = sf.rasterize(bold_sets, box, size)
                raw_mask = sf.rasterize(raw_sets, box, size)
                missing_regions = difference_components(raw_mask, bold_mask, size)
                added_regions = difference_components(bold_mask, raw_mask, size)
                missing[str(size)] = normalize_region(
                    missing_regions[0] if missing_regions else None, size, box
                )
                added[str(size)] = normalize_region(
                    added_regions[0] if added_regions else None, size, box
                )
                raster[str(size)] = {
                    "regular_to_bold": sf.raster_metrics(regular_mask, bold_mask),
                    "raw_to_bold": sf.raster_metrics(raw_mask, bold_mask),
                    "bold_ink_pixels": sum(bold_mask),
                }
            regular_box = regular[name].foreground.boundingBox()
            bold_box = bold[name].foreground.boundingBox()
            rw, rh = dimensions(regular_box)
            bw, bh = dimensions(bold_box)
            horizontal = rw >= rh
            regular_long = rw if horizontal else rh
            regular_normal = rh if horizontal else rw
            bold_long = bw if horizontal else bh
            bold_normal = bh if horizontal else bw
            missing_ratio = (
                missing["1024"]["area_pixels"] / max(1.0, raster["1024"]["bold_ink_pixels"])
                if missing["1024"] else 0.0
            )
            added_ratio = (
                added["1024"]["area_pixels"] / max(1.0, raster["1024"]["bold_ink_pixels"])
                if added["1024"] else 0.0
            )
            missing_persistent = bbox_iou(missing["512"], missing["1024"]) >= 0.35
            added_persistent = bbox_iou(added["512"], added["1024"]) >= 0.35
            row = report_rows[name]
            classes = []
            canonical = (
                name in CANONICAL_OVERRIDES
                or int(row.get("required_counters") or 0) > 0
            )
            if canonical:
                classes.append("canonical-counter")
            if (
                int(row.get("unmatched_white_regions") or 0) > 0
                and name not in CANONICAL_OVERRIDES
            ):
                classes.append("unmatched-white-patch")
            longitudinal_ratio = bold_long / max(regular_long, 1e-9)
            normal_gain = bold_normal - regular_normal
            if longitudinal_ratio > 1.18 and normal_gain < 30.0:
                classes.append("directional-overexpansion")
            if missing_persistent and 0 < missing_ratio <= 0.02:
                classes.append("terminal-indent")
            if added_persistent and 0 < added_ratio <= 0.02:
                classes.append("boundary-protrusion")
            if not classes:
                classes.append("no-similar-defect")
            regular_center = [
                (regular_box[0] + regular_box[2]) / 2.0,
                (regular_box[1] + regular_box[3]) / 2.0,
            ]
            bold_center = [
                (bold_box[0] + bold_box[2]) / 2.0,
                (bold_box[1] + bold_box[3]) / 2.0,
            ]
            left_sb = bold_box[0]
            right_sb = bold[name].width - bold_box[2]
            rows[name] = {
                "glyph": name,
                "codepoint": row["codepoint"],
                "classifications": classes,
                "orientation": "horizontal" if horizontal else "vertical",
                "longitudinal_span_ratio": longitudinal_ratio,
                "normal_thickness_gain": normal_gain,
                "centroid_displacement": math.hypot(
                    bold_center[0] - regular_center[0],
                    bold_center[1] - regular_center[1],
                ),
                "centroid_delta": [
                    bold_center[0] - regular_center[0],
                    bold_center[1] - regular_center[1],
                ],
                "sidebearing_balance": abs(left_sb - right_sb),
                "regular_bbox": regular_box,
                "bold_bbox": bold_box,
                "missing_ink_ratio": missing_ratio,
                "protruding_ink_ratio": added_ratio,
                "missing_persistent_512_1024": missing_persistent,
                "protrusion_persistent_512_1024": added_persistent,
                "largest_missing_region": missing,
                "largest_protruding_region": added,
                "raster": raster,
                "regular_overlay_family": "OlesuasRegularQA",
                "bold_overlay_family": "OlesuasBoldQA",
            }
    finally:
        regular.close()
        bold.close()
        raw.close()
    counts = {}
    for row in rows.values():
        for classification in row["classifications"]:
            counts[classification] = counts.get(classification, 0) + 1
    payload = {
        "version": "bold-v51-indent-audit-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "metrics_version": "bold-metrics-v5.1",
        "glyph_count": len(rows),
        "sizes": list(SIZES),
        "area_ratio_limit": 0.02,
        "persistence_bbox_iou": 0.35,
        "canonical_counter_overrides": sorted(CANONICAL_OVERRIDES),
        "classification_counts": counts,
        "glyphs": rows,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"glyphs": len(rows), "classifications": counts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
