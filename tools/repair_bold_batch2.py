#!/usr/bin/env ffpython
"""Build the isolated Bold v5 Batch 2 hyphen and quote-family candidate."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".font-deps"), str(ROOT / "tools")]
import redraw_font as rf
import repair_bold_v4 as v4
import refresh_bold_qa as metrics

SOURCE = ROOT / "fontforge" / "bold.sfd"
REGULAR = ROOT / "fontforge" / "redrawn.sfd"
BASE = ROOT / "fontforge" / "regular-bold-base.sfd"
RAW = ROOT / "fontforge" / "bold-raw.sfd"
AUDIT = ROOT / "qa" / "assets" / "bold-v51-indent-audit.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def point_count(layer):
    return sum(len(contour) for contour in layer)


def make_layer(is_quadratic, contours):
    layer = fontforge.layer()
    layer.is_quadratic = is_quadratic
    for contour in contours:
        layer += contour
    return layer


def transform_contour(contour, matrix):
    xx, xy, yx, yy, dx, dy = matrix
    result = contour.dup()
    for point in result:
        x, y = point.x, point.y
        point.x = xx * x + xy * y + dx
        point.y = yx * x + yy * y + dy
    return result


def fit_contour(contour, source_box, target_box):
    sx = (target_box[2] - target_box[0]) / max(source_box[2] - source_box[0], 1e-9)
    sy = (target_box[3] - target_box[1]) / max(source_box[3] - source_box[1], 1e-9)
    matrix = [
        sx, 0.0, 0.0, sy,
        target_box[0] - source_box[0] * sx,
        target_box[1] - source_box[1] * sy,
    ]
    return transform_contour(contour, matrix), matrix


def fit_regular_layer(regular_layer, bold_layer, target_boxes=None):
    """Preserve each Regular contour exactly up to an affine weight expansion."""
    templates = list(regular_layer)
    targets = list(bold_layer)
    if len(templates) != len(targets):
        raise ValueError("Regular/Bold contour count differs")
    if target_boxes is None:
        target_boxes = [contour.boundingBox() for contour in targets]
    if len(target_boxes) != len(templates):
        raise ValueError("target box count differs")
    contours, transforms = [], []
    for template, target_box in zip(templates, target_boxes):
        fitted, matrix = fit_contour(template, template.boundingBox(), target_box)
        contours.append(fitted)
        transforms.append(matrix)
    return make_layer(regular_layer.is_quadratic, contours), transforms

def persistent(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts, key=lambda value: (counts[value], value))


def evaluate(name, before, candidate, regular_layer, expected_components):
    defects = v4.structural(candidate)
    delta = v4.comparison(before, candidate)
    topology = v4.comparison(regular_layer, candidate)
    components = [topology[size]["right_topology"][0] for size in (64, 128, 256, 512)]
    counters = [topology[size]["right_topology"][1] for size in (64, 128, 256, 512)]
    short, reversals = metrics.smoothness_metrics(candidate)
    inherited_short, _ = metrics.smoothness_metrics(before)
    spikes, anchors = metrics.spike_metrics(candidate)
    inherited_spikes, _ = metrics.spike_metrics(before)
    blocker_free = (
        not defects["intersections"]
        and not defects["open_contours"]
        and not defects["invalid_handles"]
        and persistent(components[1:]) == expected_components
        and all(value == 0 for value in counters)
        and short <= inherited_short
        and spikes <= inherited_spikes
    )
    return {
        "status": "candidate-ready" if blocker_free else "candidate-blocked",
        "blocker_free": blocker_free,
        "iou_64": delta[64]["iou"],
        "iou_128": delta[128]["iou"],
        "iou_256": delta[256]["iou"],
        "iou_512": delta[512]["iou"],
        "old_points": point_count(before),
        "new_points": point_count(candidate),
        "defects": defects,
        "components_64_128_256_512": components,
        "counters_64_128_256_512": counters,
        "expected_components": expected_components,
        "required_counters": 0,
        "unmatched_white_regions": 0,
        "short_segments": short,
        "inherited_short_segments": inherited_short,
        "curvature_reversals": reversals,
        "boundary_spikes": spikes,
        "inherited_boundary_spikes": inherited_spikes,
        "on_curve_anchors": anchors,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=ROOT / "tools" / "bold-v5-batch2.json")
    parser.add_argument("--output", type=Path, default=ROOT / "fontforge" / "bold-v5-batch2.sfd")
    parser.add_argument("--ttf", type=Path, default=ROOT / "qa" / "assets" / "bold-v5-batch2.ttf")
    parser.add_argument("--report", type=Path, default=ROOT / "qa" / "assets" / "bold-v5-batch2.json")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    selected = [item["glyph"] for item in manifest["glyphs"]]
    if len(selected) != 8 or len(set(selected)) != 8:
        raise SystemExit("Batch 2 must contain exactly eight unique glyphs")
    for path, expected, label in (
        (SOURCE, manifest["source_sha256"], "promoted Bold"),
        (REGULAR, manifest["regular_sha256"], "Regular"),
        (BASE, manifest["base_sha256"], "cleaned base"),
        (RAW, manifest["raw_sha256"], "raw Bold"),
    ):
        if digest(path) != expected:
            raise SystemExit("{} hash differs".format(label))
    for path in (args.output, args.ttf, args.report):
        if path.exists() and not args.force:
            raise SystemExit("{} exists; use --force to rebuild".format(path))

    shutil.copy2(SOURCE, args.output)
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    source = fontforge.open(str(SOURCE))
    regular = fontforge.open(str(REGULAR))
    candidate = fontforge.open(str(args.output))
    rows = {}
    try:
        family = {}
        extras = {}
        quote_names = [
            "quoteleft", "quoteright", "quotesinglbase", "quotereversed",
            "quotedblleft", "quotedblright", "quotedblbase",
        ]
        for name in quote_names:
            family[name] = v4.scratch_cleanup(
                source, name, source[name].foreground, 0.5
            )
            extras[name] = {
                "family_template": "existing-bold-" + name,
                "shape_source": "existing-bold-weighted-from-authoritative-regular",
                "shape_preservation": "conservative-simplification-only",
                "simplify_error": 0.5,
            }

        regular_box = regular["uni2010"].foreground.boundingBox()
        bold_box = source["uni2010"].foreground.boundingBox()
        center_x = (bold_box[0] + bold_box[2]) / 2.0
        center_y = (bold_box[1] + bold_box[3]) / 2.0
        source_width = bold_box[2] - bold_box[0]
        source_thickness = bold_box[3] - bold_box[1]
        target_width = regular_box[2] - regular_box[0]
        target_thickness = source_thickness + 40.0
        horizontal_scale = target_width / source_width
        vertical_scale = target_thickness / source_thickness
        hyphen = source["uni2010"].foreground.dup()
        hyphen.transform((
            horizontal_scale, 0.0, 0.0, vertical_scale,
            center_x - horizontal_scale * center_x,
            center_y - vertical_scale * center_y,
        ))
        family["uni2010"] = v4.scratch_cleanup(source, "uni2010", hyphen, 0.5)
        extras["uni2010"] = {
            "target_advance": 431,
            "target_center_x": center_x,
            "target_ink_length": target_width,
            "target_thickness": target_thickness,
            "family_template": "existing-bold-uni2010",
            "shape_source": "existing-bold",
            "shape_preservation": "regular-width-bold-silhouette-y-plus-40",
            "horizontal_scale": horizontal_scale,
            "vertical_scale": vertical_scale,
            "simplify_error": 0.5,
        }

        methods = {item["glyph"]: item["method"] for item in manifest["glyphs"]}
        for name in selected:
            before = source[name].foreground.dup()
            candidate[name].foreground = family[name]
            expected_components = 2 if name.startswith("quotedbl") else 1
            row = evaluate(
                name, before, family[name], regular[name].foreground, expected_components
            )
            row.update({
                "glyph": name,
                "method": methods[name],
                "audit": audit["glyphs"][name],
                **extras.get(name, {}),
            })
            rows[name] = row
        candidate.save(str(args.output))
        persisted = fontforge.open(str(args.output))
        try:
            for name in selected:
                row = evaluate(
                    name,
                    source[name].foreground,
                    persisted[name].foreground,
                    regular[name].foreground,
                    2 if name.startswith("quotedbl") else 1,
                )
                row.update({
                    "glyph": name,
                    "method": methods[name],
                    "audit": audit["glyphs"][name],
                    **extras.get(name, {}),
                })
                rows[name] = row
        finally:
            persisted.close()
        candidate.generate(str(args.ttf), flags=("opentype",))
    finally:
        source.close()
        regular.close()
        candidate.close()

    payload = {
        "version": "bold-v5-batch2-candidate-v2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "batch": 2,
        "source_sha256": manifest["source_sha256"],
        "candidate": str(args.output.relative_to(ROOT)).replace("\\", "/"),
        "candidate_sha256": digest(args.output),
        "candidate_ttf_sha256": digest(args.ttf),
        "build_id": digest(args.ttf)[:16],
        "selected": selected,
        "candidate_ready": sum(row["blocker_free"] for row in rows.values()),
        "candidate_blocked": sum(not row["blocker_free"] for row in rows.values()),
        "glyphs": rows,
    }
    args.report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "selected": len(selected),
        "candidate_ready": payload["candidate_ready"],
        "candidate_blocked": payload["candidate_blocked"],
    }, sort_keys=True))
    return 0 if not payload["candidate_blocked"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
