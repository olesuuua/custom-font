#!/usr/bin/env ffpython
"""Build an isolated manifest-driven Bold repair candidate."""
from __future__ import annotations

import argparse
import csv
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

REGULAR = ROOT / "fontforge" / "redrawn.sfd"
BASE = ROOT / "fontforge" / "regular-bold-base.sfd"
RAW = ROOT / "fontforge" / "bold-raw.sfd"
SOURCE = ROOT / "fontforge" / "bold.sfd"
PILOT = ROOT / "fontforge" / "bold-v5-pilot.sfd"
PILOT_MANUAL = ROOT / "fontforge" / "bold-v5-pilot-manual.sfd"
TRIAGE = ROOT / "qa" / "assets" / "bold-report.csv"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def point_count(layer):
    return sum(len(contour) for contour in layer)


def rebuilt_layer(layer, contours):
    result = fontforge.layer()
    result.is_quadratic = layer.is_quadratic
    for contour in contours:
        result += contour
    return result


def set_point(contour, index, x, y, point_type=None):
    point = contour[index]
    point.x, point.y = x, y
    if point_type is not None and point.on_curve:
        point.type = point_type


def affine_contour(contour, source_box, target_box):
    result = contour.dup()
    sx = (target_box[2] - target_box[0]) / max(source_box[2] - source_box[0], 1e-9)
    sy = (target_box[3] - target_box[1]) / max(source_box[3] - source_box[1], 1e-9)
    for point in result:
        point.x = target_box[0] + (point.x - source_box[0]) * sx
        point.y = target_box[1] + (point.y - source_box[1]) * sy
    return result, [sx, 0.0, 0.0, sy,
                    target_box[0] - source_box[0] * sx,
                    target_box[1] - source_box[1] * sy]


def repair_brokenbar(donor):
    layer = donor["brokenbar"].foreground.dup()
    contours = [contour.dup() for contour in layer]
    set_point(contours[1], 7, 55.0, 607.0, fontforge.splineCurve)
    set_point(contours[1], 8, 56.0, 611.0)
    return rebuilt_layer(layer, contours), {}


def repair_dagger(donor):
    return donor["dagger"].foreground.dup(), {}


def repair_uni042f(source):
    layer = source["uni042F"].foreground.dup()
    contours = [contour.dup() for contour in layer]
    contour = contours[0]
    set_point(contour, 113, 562.0, 218.0)
    set_point(contour, 114, 570.0, 224.0, fontforge.splineCurve)
    set_point(contour, 115, 575.0, 223.0)
    set_point(contour, 116, 580.0, 222.0, fontforge.splineCurve)
    set_point(contour, 117, 584.0, 220.0)
    set_point(contour, 0, 587.0, 219.0, fontforge.splineCurve)
    set_point(contour, 91, 479.0, 703.0, fontforge.splineCurve)
    return rebuilt_layer(layer, contours), {}


def repair_uni2010(source):
    layer = source["uni2010"].foreground.dup()
    contours = [contour.dup() for contour in layer]
    contour = contours[0]
    set_point(contour, 21, 389.0, 284.0, fontforge.splineCurve)
    set_point(contour, 25, 386.0, 254.0, fontforge.splineCurve)
    set_point(contour, 26, 380.0, 256.0)
    set_point(contour, 27, 367.0, 258.0, fontforge.splineCurve)
    set_point(contour, 28, 360.0, 258.0)
    set_point(contour, 29, 355.0, 258.0, fontforge.splineCurve)
    set_point(contour, 30, 349.0, 258.0)
    set_point(contour, 31, 342.5, 258.0, fontforge.splineCurve)
    set_point(contour, 32, 336.0, 258.0)
    set_point(contour, 33, 335.0, 258.0, fontforge.splineCurve)
    set_point(contour, 34, 318.0, 258.0)
    set_point(contour, 35, 308.0, 258.0, fontforge.splineCurve)
    set_point(contour, 36, 297.0, 258.0)
    set_point(contour, 37, 276.0, 258.0, fontforge.splineCurve)
    set_point(contour, 38, 244.0, 258.0)
    set_point(contour, 39, 211.5, 258.0, fontforge.splineCurve)
    set_point(contour, 40, 195.0, 258.0)
    return rebuilt_layer(layer, contours), {}


def repair_two(source):
    layer = source["two"].foreground.dup()
    contours = [contour.dup() for contour in layer]
    contour = contours[0]
    updates = {
        49: (450.0, 95.0), 50: (459.0, 99.0),
        51: (469.0, 103.0), 52: (480.0, 108.0),
        53: (493.0, 114.0), 54: (503.0, 108.0),
        55: (509.0, 104.0), 56: (514.0, 98.0),
        57: (519.0, 93.0), 58: (521.0, 86.0),
        59: (523.0, 82.0), 60: (523.0, 78.0),
        61: (523.0, 74.0), 62: (522.0, 68.0),
    }
    for index, (x, y) in updates.items():
        set_point(contour, index, x, y,
                  fontforge.splineCurve if contour[index].on_curve else None)
    return rebuilt_layer(layer, contours), {}


def repair_minute(source):
    layer = source["minute"].foreground.dup()
    source_contour = list(layer)[0]
    contour = fontforge.contour()
    contour.is_quadratic = True
    contour.moveTo(source_contour[0].x, source_contour[0].y)
    for end in (2, 4, 6, 8, 10, 12):
        control, target = source_contour[end - 1], source_contour[end]
        contour.quadraticTo((control.x, control.y), (target.x, target.y))
    contour.quadraticTo((49.0, -59.0), (41.0, -53.0))
    for end in (24, 26, 28):
        control, target = source_contour[end - 1], source_contour[end]
        contour.quadraticTo((control.x, control.y), (target.x, target.y))
    contour.quadraticTo(
        (source_contour[29].x, source_contour[29].y),
        (source_contour[0].x, source_contour[0].y),
    )
    contour.closed = True
    for point in contour:
        if point.on_curve:
            point.type = fontforge.splineCurve
    return rebuilt_layer(layer, [contour]), {}


def repair_second(source, minute_layer):
    layer = source["second"].foreground.dup()
    targets = [contour.boundingBox() for contour in layer]
    template = list(minute_layer)[0]
    source_box = template.boundingBox()
    contours, transforms = [], []
    for target_box in targets:
        contour, transform = affine_contour(template, source_box, target_box)
        contours.append(contour)
        transforms.append(transform)
    return rebuilt_layer(layer, contours), {
        "family_template": "minute",
        "family_transforms": transforms,
    }


def repair_uni21ba(source):
    layer = source["uni21BA"].foreground.dup()
    contours = [contour.dup() for contour in layer]
    contour = contours[0]
    set_point(contour, 40, 342.0, 24.0)
    set_point(contour, 41, 323.0, 24.5, fontforge.splineCurve)
    set_point(contour, 42, 304.0, 24.0)
    return rebuilt_layer(layer, contours), {}


def persistent(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts, key=lambda value: (counts[value], value))


def evaluate(name, before, candidate, regular_layer, row):
    defects = v4.structural(candidate)
    delta = v4.comparison(before, candidate)
    topology = v4.comparison(regular_layer, candidate)
    components = [
        topology[size]["right_topology"][0] for size in (64, 128, 256, 512)
    ]
    counters = [
        topology[size]["right_topology"][1] for size in (64, 128, 256, 512)
    ]
    expected_components = int(row["persistent_bold_components"] or 0)
    required_counters = int(row["required_counters"] or 0)
    short, reversals = metrics.smoothness_metrics(candidate)
    spikes, anchors = metrics.spike_metrics(candidate)
    blocker_free = (
        not defects["intersections"]
        and not defects["open_contours"]
        and not defects["invalid_handles"]
        and persistent(components[1:]) == expected_components
        and all(value == required_counters for value in counters[1:])
        and short == 0
        and spikes == 0
    )
    ready_iou = delta[64]["iou"] >= .995 and delta[128]["iou"] >= .995
    return {
        "status": "candidate-ready" if blocker_free else "candidate-blocked",
        "blocker_free": blocker_free,
        "ready_iou": ready_iou,
        "iou_64": delta[64]["iou"],
        "iou_128": delta[128]["iou"],
        "iou_256": delta[256]["iou"],
        "iou_512": delta[512]["iou"],
        "old_points": point_count(before),
        "new_points": point_count(candidate),
        "defects": defects,
        "components_64_128_256_512": components,
        "expected_components": expected_components,
        "counters_64_128_256_512": counters,
        "required_counters": required_counters,
        "user_required_counters": 0,
        "unmatched_white_regions": max(0, persistent(counters[1:]) - required_counters),
        "short_segments": short,
        "curvature_reversals": reversals,
        "boundary_spikes": spikes,
        "on_curve_anchors": anchors,
        "regular_to_candidate": {
            str(size): {
                "components": topology[size]["right_topology"][0],
                "counters": topology[size]["right_topology"][1],
            }
            for size in (64, 128, 256, 512)
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=ROOT / "tools" / "bold-v5-batch1.json")
    parser.add_argument("--output", type=Path, default=ROOT / "fontforge" / "bold-v5-batch1.sfd")
    parser.add_argument("--ttf", type=Path, default=ROOT / "qa" / "assets" / "bold-v5-batch1.ttf")
    parser.add_argument("--report", type=Path, default=ROOT / "qa" / "assets" / "bold-v5-batch1.json")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    selected = [item["glyph"] for item in manifest["glyphs"]]
    if len(selected) != len(set(selected)) or len(selected) > int(manifest["maximum_glyphs"]):
        raise SystemExit("Invalid or oversized batch manifest")
    checks = [
        (SOURCE, manifest["source_sha256"], "Bold source"),
        (REGULAR, manifest["regular_sha256"], "Regular source"),
        (BASE, manifest["base_sha256"], "cleaned base"),
        (RAW, manifest["raw_sha256"], "raw Bold"),
    ]
    for path, expected, label in checks:
        actual = sha256(path)
        if actual != expected:
            raise SystemExit("{} hash differs: {} != {}".format(label, actual, expected))
    for path in (args.output, args.ttf, args.report):
        if path.exists() and not args.force:
            raise SystemExit("{} exists; use --force to rebuild from the frozen source".format(path))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.ttf.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE, args.output)
    with TRIAGE.open(encoding="utf-8-sig", newline="") as handle:
        triage = {row["glyph"]: row for row in csv.DictReader(handle)}
    candidate = fontforge.open(str(args.output))
    source = fontforge.open(str(SOURCE))
    regular = fontforge.open(str(REGULAR))
    pilot = fontforge.open(str(PILOT))
    pilot_manual = fontforge.open(str(PILOT_MANUAL))
    rows = {}
    try:
        repaired = {}
        for item in manifest["glyphs"]:
            name = item["glyph"]
            before = source[name].foreground.dup()
            if name == "brokenbar":
                layer, extra = repair_brokenbar(pilot_manual)
            elif name == "dagger":
                layer, extra = repair_dagger(pilot)
            elif name == "uni042F":
                layer, extra = repair_uni042f(source)
            elif name == "uni2010":
                layer, extra = repair_uni2010(source)
            elif name == "two":
                layer, extra = repair_two(source)
            elif name == "minute":
                layer, extra = repair_minute(source)
            elif name == "second":
                layer, extra = repair_second(source, repaired["minute"])
            elif name == "uni21BA":
                layer, extra = repair_uni21ba(source)
            else:
                raise RuntimeError("No repair backend for " + name)
            candidate[name].foreground = layer
            repaired[name] = layer.dup()
            row = evaluate(name, before, layer, regular[name].foreground, triage[name])
            row.update({
                "glyph": name,
                "codepoint": item["codepoint"],
                "method": item["method"],
                "donor_source": item["donor_source"],
                "discarded_artifact_contours": 0,
                "local_repair_region": item.get("local_repair_region"),
                **extra,
            })
            rows[name] = row
        candidate.save(str(args.output))
        candidate.generate(str(args.ttf), flags=("opentype",))
    finally:
        for font in (candidate, source, regular, pilot, pilot_manual):
            font.close()
    payload = {
        "version": "bold-v5-batch1-candidate-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "batch": 1,
        "source": str(SOURCE.relative_to(ROOT)).replace("\\", "/"),
        "source_sha256": manifest["source_sha256"],
        "candidate": str(args.output.relative_to(ROOT)).replace("\\", "/"),
        "candidate_sha256": sha256(args.output),
        "candidate_ttf_sha256": sha256(args.ttf),
        "build_id": sha256(args.ttf)[:16],
        "selected": selected,
        "candidate_ready": sum(row["blocker_free"] for row in rows.values()),
        "candidate_blocked": sum(not row["blocker_free"] for row in rows.values()),
        "ready_iou": sum(row["ready_iou"] for row in rows.values()),
        "glyphs": rows,
    }
    args.report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "selected": len(selected),
        "candidate_ready": payload["candidate_ready"],
        "candidate_blocked": payload["candidate_blocked"],
        "ready_iou": payload["ready_iou"],
    }, sort_keys=True))
    return 0 if not payload["candidate_blocked"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
