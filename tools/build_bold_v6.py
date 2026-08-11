#!/usr/bin/env ffpython
"""Build the accumulated Bold v6 candidate without changing approved bold.sfd."""
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
BATCH2 = ROOT / "fontforge" / "bold-v5-batch2.sfd"
REGULAR = ROOT / "fontforge" / "regular-bold-base.sfd"
RAW = ROOT / "fontforge" / "bold-raw.sfd"
DECISIONS = ROOT / "checkpoints" / "bold-v6" / "revision-00-source" / "bold-manual-decisions.json"
OUTPUT = ROOT / "fontforge" / "bold-v6.sfd"
REPORT = ROOT / "qa" / "assets" / "bold-v6-repairs.json"

QUOTE_NAMES = [
    "quoteleft", "quoteright", "quotesinglbase", "quotereversed",
    "quotedblleft", "quotedblright", "quotedblbase", "uni201F",
]
IDENTITY_QUOTES = {"quoteleft", "quotedblleft"}
COUNTER_DONORS = {"d": 1, "uni0432": 2, "uni0443": 1, "beta": 2, "rho": 1}
PERSISTENCE_BLOCKED = {"arrowupdn", "z"}
WHITE_PATCH_TARGETS = {
    "D": 1, "L": 0, "h": 0, "iota": 0, "notelement": 0,
    "uni0412": 2, "uni0414": 1,
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def point_count(layer):
    return sum(len(contour) for contour in layer)


def make_layer(quadratic, contours):
    layer = fontforge.layer()
    layer.is_quadratic = quadratic
    for contour in contours:
        layer += contour
    return layer


def fit_contour(contour, target_box, rotate=False):
    source_box = contour.boundingBox()
    result = contour.dup()
    source_width = max(source_box[2] - source_box[0], 1e-9)
    source_height = max(source_box[3] - source_box[1], 1e-9)
    target_width = target_box[2] - target_box[0]
    target_height = target_box[3] - target_box[1]
    for point in result:
        x = (point.x - source_box[0]) / source_width
        y = (point.y - source_box[1]) / source_height
        if rotate:
            x, y = 1.0 - x, 1.0 - y
        point.x = target_box[0] + x * target_width
        point.y = target_box[1] + y * target_height
    return result


def quote_prototype():
    """Low-point U+201C contour with the upper indentation removed."""
    contour = fontforge.contour()
    contour.is_quadratic = True
    contour.moveTo(182.0, 581.0)
    contour.quadraticTo((182.0, 561.0), (163.0, 545.5))
    contour.quadraticTo((144.0, 530.0), (121.0, 530.0))
    contour.quadraticTo((95.09, 530.0), (73.5, 542.5))
    contour.quadraticTo((49.0, 556.68), (49.0, 589.0))
    contour.quadraticTo((49.0, 610.0), (69.0, 635.0))
    contour.quadraticTo((76.0, 648.0), (96.0, 672.0))
    contour.quadraticTo((99.0, 667.0), (109.0, 675.0))
    contour.quadraticTo((119.0, 685.0), (126.0, 699.0))
    contour.quadraticTo((136.0, 710.0), (148.0, 710.0))
    contour.quadraticTo((157.0, 710.0), (165.0, 704.5))
    contour.quadraticTo((173.0, 699.0), (173.0, 690.0))
    contour.quadraticTo((173.0, 671.0), (146.0, 633.0))
    contour.quadraticTo((182.0, 614.0), (182.0, 581.0))
    contour.closed = True
    return contour


def repair_uni2010(layer):
    """Remove the tiny round kink while retaining the approved v5 silhouette."""
    points = list(list(layer)[0])
    contour = fontforge.contour()
    contour.is_quadratic = True
    contour.moveTo(points[0].x, points[0].y)
    for control, end in ((1, 2), (3, 4), (5, 6), (7, 8)):
        contour.quadraticTo((points[control].x, points[control].y), (points[end].x, points[end].y))
    contour.lineTo(points[9].x, points[9].y)
    contour.quadraticTo((points[10].x, points[10].y), (points[11].x, points[11].y))
    contour.quadraticTo((points[12].x, points[12].y), (points[13].x, points[13].y))
    for control, end in ((15, 16), (17, 18), (19, 20), (21, 22), (23, 24), (25, 26), (27, 28)):
        contour.quadraticTo((points[control].x, points[control].y), (points[end].x, points[end].y))
    # Replace the upward backtracking bay (old points 29-33) with one shallow,
    # tangent-continuous lower edge. This fills the visible bottom-right notch
    # while retaining the handwritten terminal above it.
    contour.quadraticTo((236.0, 243.5), (points[0].x, points[0].y))
    contour.closed = True
    return make_layer(True, [contour])


def contour_area(contour):
    return v4.contour_area(contour)


def keep_outer_and_holes(layer, hole_count):
    contours = list(layer)
    if not contours:
        return layer.dup()
    outer = max(range(len(contours)), key=lambda index: contour_area(contours[index]))
    holes = sorted((index for index in range(len(contours)) if index != outer), key=lambda index: contour_area(contours[index]), reverse=True)[:hole_count]
    keep = {outer, *holes}
    return make_layer(layer.is_quadratic, [contour.dup() for index, contour in enumerate(contours) if index in keep])


def structural_ok(layer):
    defects = v4.structural(layer)
    return not any(defects[key] for key in ("intersections", "open_contours", "invalid_handles"))


def conservative_cleanup(font, regular, name):
    before = font[name].foreground.dup()
    candidate = v4.scratch_cleanup(font, name, before, 0.5)
    if not structural_ok(candidate):
        return None
    comparison = v4.comparison(before, candidate)
    if comparison[64]["iou"] < 0.995 or comparison[128]["iou"] < 0.995:
        return None
    topology = v4.comparison(regular[name].foreground, candidate)
    before_topology = v4.comparison(regular[name].foreground, before)
    for size in (64, 128, 256, 512):
        if topology[size]["right_topology"] != before_topology[size]["right_topology"]:
            return None
    before_short, _ = metrics.smoothness_metrics(before)
    after_short, _ = metrics.smoothness_metrics(candidate)
    before_spikes, _ = metrics.spike_metrics(before)
    after_spikes, _ = metrics.spike_metrics(candidate)
    if after_short > before_short or after_spikes > before_spikes:
        return None
    if point_count(candidate) >= point_count(before):
        return None
    return candidate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--report", type=Path, default=REPORT)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    for path in (SOURCE, BATCH2, REGULAR, RAW, DECISIONS):
        if not path.is_file():
            raise SystemExit("missing input: " + str(path))
    if args.output.exists() and not args.force:
        raise SystemExit(str(args.output) + " exists; use --force")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE, args.output)

    decisions = json.loads(DECISIONS.read_text(encoding="utf-8-sig"))["decisions"]
    nonpassed = {name for name, decision in decisions.items() if decision.get("status") != "pass"}
    source = fontforge.open(str(SOURCE))
    batch2 = fontforge.open(str(BATCH2))
    regular = fontforge.open(str(REGULAR))
    raw = fontforge.open(str(RAW))
    candidate = fontforge.open(str(args.output))
    methods = {}
    try:
        candidate.version = "1.003"
        candidate["uni2010"].foreground = repair_uni2010(batch2["uni2010"].foreground)
        methods["uni2010"] = "local-bottom-notch-fill-refit"

        prototype = quote_prototype()
        for name in QUOTE_NAMES:
            target_font = batch2 if name != "uni201F" else source
            target_boxes = [contour.boundingBox() for contour in target_font[name].foreground]
            expected = 1 if name in {"quoteleft", "quoteright", "quotesinglbase", "quotereversed"} else 2
            if len(target_boxes) < expected:
                raise RuntimeError(name + " lacks target components")
            contours = [fit_contour(prototype, target_boxes[index], rotate=name not in IDENTITY_QUOTES) for index in range(expected)]
            candidate[name].foreground = make_layer(True, contours)
            methods[name] = "shared-u201c-smooth-prototype"

        for name, required_holes in COUNTER_DONORS.items():
            donor = v4.scratch_cleanup(raw, name, raw[name].foreground, 1.0)
            donor = keep_outer_and_holes(donor, required_holes)
            if structural_ok(donor):
                candidate[name].foreground = donor
                methods[name] = "cleaned-raw-required-counter-donor"

        for name, required_holes in WHITE_PATCH_TARGETS.items():
            cleaned = keep_outer_and_holes(source[name].foreground, required_holes)
            if structural_ok(cleaned):
                candidate[name].foreground = cleaned
                methods[name] = "remove-unmatched-micro-contours"

        protected = {"uni2010", *QUOTE_NAMES, *COUNTER_DONORS, *WHITE_PATCH_TARGETS}
        for name in sorted(nonpassed - protected - PERSISTENCE_BLOCKED):
            cleaned = conservative_cleanup(source, regular, name)
            if cleaned is not None:
                candidate[name].foreground = cleaned
                methods[name] = "conservative-topology-preserving-refit"
        candidate.save(str(args.output))
    finally:
        source.close(); batch2.close(); regular.close(); raw.close(); candidate.close()

    source = fontforge.open(str(SOURCE))
    candidate = fontforge.open(str(args.output))
    rows = {}
    try:
        for glyph in candidate.glyphs():
            name = glyph.glyphname
            old_hash = rf.layer_hash(source[name].foreground)
            new_hash = rf.layer_hash(glyph.foreground)
            if old_hash == new_hash:
                continue
            before, after = source[name].foreground, glyph.foreground
            comparison = v4.comparison(before, after)
            rows[name] = {
                "glyph": name,
                "method": methods.get(name, "fontforge-persisted-change"),
                "previous_bold_hash": old_hash,
                "bold_hash": new_hash,
                "old_points": point_count(before),
                "new_points": point_count(after),
                "iou_64": comparison[64]["iou"],
                "iou_128": comparison[128]["iou"],
                "defects": v4.structural(after),
            }
    finally:
        source.close(); candidate.close()

    payload = {
        "version": "bold-v6-repairs-r1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": digest(SOURCE),
        "candidate_sha256": digest(args.output),
        "changed_count": len(rows),
        "changed_glyphs": sorted(rows),
        "glyphs": rows,
    }
    args.report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "changed": len(rows),
        "quotes": sum(name in rows for name in QUOTE_NAMES),
        "nonpassed_cleanups": sum(row["method"] == "conservative-topology-preserving-refit" for row in rows.values()),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
