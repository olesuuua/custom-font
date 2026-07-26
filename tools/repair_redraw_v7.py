#!/usr/bin/env ffpython
"""Build redraw-v7 with curve-first target rebuilds and shared fraction parts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / ".font-deps"))
from fontTools.ttLib import TTFont

import redraw_font as rf
import repair_redraw_v4 as v4
import repair_redraw_v6 as v6
import simplify_font as sf

V6 = ROOT / "checkpoints" / "redraw-v6"
V7 = ROOT / "checkpoints" / "redraw-v7"
WORK = ROOT / "checkpoints" / "redraw-v7-work"
TARGETS = {
    "uni0414", "uni0401", "uni041D", "dong", "uni20BA",
    "oneeighth", "threeeighths", "fiveeighths", "seveneighths",
}
FRACTIONS = {"oneeighth", "threeeighths", "fiveeighths", "seveneighths"}
FULL_REFITS = {"uni0401", "dong", "uni20BA"}
CEILING = 100
EXTRA_FIELDS = (
    "changed_in_v7", "v7_rebuild_method", "v7_previous_corner_points",
    "v7_resulting_corner_points", "v7_resulting_curve_points",
    "v7_donor_glyphs", "v7_shared_fraction_bar",
    "v7_shared_denominator_eight",
)


def atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def point_count(layer) -> int:
    return sum(len(contour) for contour in layer)


def contour_layer(contours):
    layer = fontforge.layer()
    layer.is_quadratic = False
    for contour in contours:
        layer += contour.dup()
    return layer


def contour_bbox(contour):
    xs = [point.x for point in contour]
    ys = [point.y for point in contour]
    return min(xs), min(ys), max(xs), max(ys)


def contour_sets(contours):
    return v4.point_sets(contour_layer(contours))


def evenly_spaced_indices(points, count):
    count = max(3, min(count, len(points)))
    distances = [0.0]
    for index in range(1, len(points) + 1):
        first = points[index - 1]
        second = points[index % len(points)]
        distances.append(distances[-1] + rf.distance(first, second))
    total = distances[-1]
    result = []
    cursor = 0
    for step in range(count):
        target = total * step / count
        while cursor + 1 < len(distances) and distances[cursor + 1] <= target:
            cursor += 1
        result.append(cursor % len(points))
    return sorted(set(result))


def curve_contour(points, anchor_count, tension=.28, corner_angle=38.0,
                  force_smooth=False):
    indices = evenly_spaced_indices(points, anchor_count)
    anchors = [points[index] for index in indices]
    count = len(anchors)
    if count < 3:
        raise RuntimeError("curve contour requires at least three anchors")
    incoming = []
    outgoing = []
    smooth = []
    for index, source_index in enumerate(indices):
        previous = anchors[(index - 1) % count]
        current = anchors[index]
        following = anchors[(index + 1) % count]
        before = rf.normalize(rf.subtract(current, previous))
        after = rf.normalize(rf.subtract(following, current))
        turn = rf.anchor_turn(points, source_index, min(4, max(1, len(points) // 30)))
        is_smooth = force_smooth or turn < corner_angle
        if is_smooth:
            tangent = rf.normalize(rf.subtract(following, previous))
            incoming.append(tangent)
            outgoing.append(tangent)
        else:
            incoming.append(before)
            outgoing.append(after)
        smooth.append(is_smooth)

    contour = fontforge.contour()
    contour.is_quadratic = False
    contour.moveTo(*anchors[0])
    for index in range(count):
        following = (index + 1) % count
        chord = rf.distance(anchors[index], anchors[following])
        control1 = rf.add(anchors[index], rf.multiply(outgoing[index], chord * tension))
        control2 = rf.subtract(
            anchors[following], rf.multiply(incoming[following], chord * tension)
        )
        contour.cubicTo(control1, control2, anchors[following])
    contour.closed = True
    values = list(contour)
    on_curves = [point for point in values if point.on_curve]
    for point, is_smooth in zip(on_curves, smooth):
        point.type = fontforge.splineCurve if is_smooth else fontforge.splineCorner
    return contour


def curve_layer(point_sets, counts, tension=.28, corner_angle=38.0,
                force_smooth_indices=()):
    if len(point_sets) != len(counts):
        raise RuntimeError("contour/count mismatch")
    layer = fontforge.layer()
    layer.is_quadratic = False
    forced = set(force_smooth_indices)
    for index, (points, count) in enumerate(zip(point_sets, counts)):
        layer += curve_contour(
            points, count, tension=tension, corner_angle=corner_angle,
            force_smooth=index in forced,
        )
    return layer


def evaluate(reference_sets, layer, method, normalize=True):
    return v4.evaluate(reference_sets, reference_sets, layer, CEILING, method,
                       normalize=normalize)


def structurally_clean(candidate):
    return (
        candidate["points"] <= CEILING
        and not candidate["self_intersections"]
        and not candidate["invalid_handles"]
        and candidate["metrics"]["topology_match"]
        and candidate["direction_valid"]
        and candidate["degenerate_contours"] == 0
    )


def quality_gate(candidate, baseline, explicit_fraction=False):
    if not structurally_clean(candidate):
        return False
    new = candidate["metrics"]
    old = baseline["metrics"]
    n128, o128 = new["per_size"][128], old["per_size"][128]
    if explicit_fraction:
        return (
            n128["ink_iou"] >= o128["ink_iou"] - .080
            and n128["false_positive_ink"] <= o128["false_positive_ink"] + .070
            and n128["false_negative_ink"] <= o128["false_negative_ink"] + .060
            and new["boundary_p95"] <= old["boundary_p95"] + .010
        )
    return (
        n128["ink_iou"] >= o128["ink_iou"] - .025
        and n128["false_positive_ink"] <= o128["false_positive_ink"] + .030
        and n128["false_negative_ink"] <= o128["false_negative_ink"] + .030
        and new["boundary_p95"] <= old["boundary_p95"] + .007
    )


def smoothness_key(candidate):
    counts = rf.point_type_counts(candidate["layer"])
    curves = counts["curve"] + counts["hvcurve"]
    return (-curves, counts["corner"], v4.raster_score(candidate),
            candidate["metrics"]["boundary_p95"], candidate["points"])


def select_candidate(name, candidates, baseline):
    valid = [candidate for candidate in candidates
             if quality_gate(candidate, baseline, name in FRACTIONS)]
    if not valid:
        diagnostics = [
            (candidate["method"], candidate["points"],
             candidate["self_intersections"], candidate["invalid_handles"],
             candidate["metrics"]["topology_match"])
            for candidate in candidates
        ]
        raise RuntimeError("{} has no valid curve-first candidate: {}".format(
            name, diagnostics
        ))
    baseline_types = rf.point_type_counts(baseline["layer"])
    baseline_curves = baseline_types["curve"] + baseline_types["hvcurve"]
    improved = [
        candidate for candidate in valid
        if (rf.point_type_counts(candidate["layer"])["curve"]
            + rf.point_type_counts(candidate["layer"])["hvcurve"]) > baseline_curves
        and rf.point_type_counts(candidate["layer"])["corner"] < baseline_types["corner"]
    ]
    if not improved:
        raise RuntimeError(name + " has no candidate with genuinely improved point types")
    return min(improved, key=smoothness_key)


def aligned_smooth_handles(layer, predicate=None, maximum_turn=55.0):
    result = layer.dup()
    converted = 0
    for contour in result:
        values = list(contour)
        on_indices = [index for index, point in enumerate(values) if point.on_curve]
        for position, index in enumerate(on_indices):
            point = values[index]
            if predicate is not None and not predicate(point):
                continue
            immediate_previous = values[(index - 1) % len(values)]
            immediate_following = values[(index + 1) % len(values)]
            if immediate_previous.on_curve or immediate_following.on_curve:
                continue
            previous_on = values[on_indices[(position - 1) % len(on_indices)]]
            following_on = values[on_indices[(position + 1) % len(on_indices)]]
            incoming = rf.normalize((point.x - previous_on.x, point.y - previous_on.y))
            outgoing = rf.normalize((following_on.x - point.x, following_on.y - point.y))
            cosine = max(-1.0, min(1.0, rf.dot(incoming, outgoing)))
            if math.degrees(math.acos(cosine)) > maximum_turn:
                continue
            tangent = rf.normalize((
                following_on.x - previous_on.x, following_on.y - previous_on.y
            ))
            incoming_length = rf.distance(
                (point.x, point.y), (immediate_previous.x, immediate_previous.y)
            )
            outgoing_length = rf.distance(
                (point.x, point.y), (immediate_following.x, immediate_following.y)
            )
            immediate_previous.x = point.x - tangent[0] * incoming_length
            immediate_previous.y = point.y - tangent[1] * incoming_length
            immediate_following.x = point.x + tangent[0] * outgoing_length
            immediate_following.y = point.y + tangent[1] * outgoing_length
            point.type = fontforge.splineCurve
            converted += 1
    return result, converted


def full_refit_candidates(name, reference_layer, metric_sets):
    shape_sets = v4.point_sets(reference_layer)
    candidates = []
    selected_turns = {"uni0401": (44.0,), "dong": (44.0,), "uni20BA": (32.0,)}
    for maximum_turn in selected_turns[name]:
        aligned, converted = aligned_smooth_handles(
            reference_layer, maximum_turn=maximum_turn
        )
        method = "curve-handle-refit:aligned-{}:{:.0f}".format(
            converted, maximum_turn
        )
        candidates.append(evaluate(metric_sets, aligned, method, normalize=False))
    return shape_sets, candidates


def smooth_connector(layer):
    result = layer.dup()
    converted = 0
    for contour in result:
        values = list(contour)
        on_indices = [index for index, point in enumerate(values) if point.on_curve]
        for position, index in enumerate(on_indices):
            point = values[index]
            if not (280 <= point.x <= 650 and 320 <= point.y <= 470):
                continue
            previous_index = on_indices[(position - 1) % len(on_indices)]
            following_index = on_indices[(position + 1) % len(on_indices)]
            previous_on = values[previous_index]
            following_on = values[following_index]
            immediate_previous = values[(index - 1) % len(values)]
            immediate_following = values[(index + 1) % len(values)]
            if immediate_previous.on_curve or immediate_following.on_curve:
                continue
            tangent = rf.normalize((
                following_on.x - previous_on.x, following_on.y - previous_on.y
            ))
            incoming_length = rf.distance(
                (point.x, point.y), (immediate_previous.x, immediate_previous.y)
            )
            outgoing_length = rf.distance(
                (point.x, point.y), (immediate_following.x, immediate_following.y)
            )
            immediate_previous.x = point.x - tangent[0] * incoming_length
            immediate_previous.y = point.y - tangent[1] * incoming_length
            immediate_following.x = point.x + tangent[0] * outgoing_length
            immediate_following.y = point.y + tangent[1] * outgoing_length
            point.type = fontforge.splineCurve
            converted += 1
    return result, converted


def split_fraction(layer):
    records = [(contour_bbox(contour), contour.dup()) for contour in layer]
    bar_index = max(
        range(len(records)),
        key=lambda index: records[index][0][3] - records[index][0][1],
    )
    bar = records[bar_index][1]
    remaining = [record for index, record in enumerate(records) if index != bar_index]
    numerator = [contour for bbox, contour in remaining
                 if (bbox[0] + bbox[2]) / 2.0 < 300]
    denominator = [contour for bbox, contour in remaining
                   if (bbox[0] + bbox[2]) / 2.0 >= 300]
    if len(numerator) != 1 or len(denominator) != 3:
        raise RuntimeError("unexpected fraction contour layout")
    denominator.sort(key=lambda contour: (
        -(contour_bbox(contour)[2] - contour_bbox(contour)[0])
        * (contour_bbox(contour)[3] - contour_bbox(contour)[1])
    ))
    return numerator[0], bar, denominator


def registered_sets(point_sets, target_bbox):
    source_bbox = sf.bbox_of_sets(point_sets)
    sx = (target_bbox[2] - target_bbox[0]) / max(1.0, source_bbox[2] - source_bbox[0])
    sy = (target_bbox[3] - target_bbox[1]) / max(1.0, source_bbox[3] - source_bbox[1])
    return [[
        (target_bbox[0] + (x - source_bbox[0]) * sx,
         target_bbox[1] + (y - source_bbox[1]) * sy)
        for x, y in contour
    ] for contour in point_sets]


def shared_eight_candidates(base):
    _numerator, _bar, denominator = split_fraction(base["threeeighths"].foreground)
    denominator.sort(key=lambda contour: (
        -(contour_bbox(contour)[2] - contour_bbox(contour)[0])
        * (contour_bbox(contour)[3] - contour_bbox(contour)[1])
    ))
    outer = contour_layer([denominator[0]])
    counter_sets = contour_sets(denominator[1:])
    candidates = []
    smooth_counters = curve_layer(
        counter_sets, (4, 4), tension=.39, corner_angle=180.0,
        force_smooth_indices=(0, 1),
    )
    counter_converted = sum(rf.point_type_counts(smooth_counters)[key]
                            for key in ("curve", "hvcurve"))
    for maximum_turn in (32.0,):
        smooth_outer, outer_converted = aligned_smooth_handles(
            outer, maximum_turn=maximum_turn
        )
        layer = contour_layer(list(smooth_outer) + list(smooth_counters))
        candidates.append((
            "retained-{}-{}".format(outer_converted, counter_converted),
            maximum_turn, layer,
        ))
    return candidates


def clean_seven_layer(bbox, bar_ratio=.17, diagonal=.40):
    left, bottom, right, top = bbox
    width, height = right - left, top - bottom
    contour = fontforge.contour()
    contour.is_quadratic = False
    contour.moveTo(left + .05 * width, top)
    contour.lineTo(right - .04 * width, top)
    contour.lineTo(right - .04 * width, top - bar_ratio * height)
    contour.cubicTo(
        (left + .80 * width, top - .24 * height),
        (left + .55 * width, bottom + .38 * height),
        (left + diagonal * width, bottom + .08 * height),
    )
    contour.cubicTo(
        (left + .37 * width, bottom + .02 * height),
        (left + .29 * width, bottom),
        (left + .20 * width, bottom + .02 * height),
    )
    contour.cubicTo(
        (left + .34 * width, bottom + .36 * height),
        (left + .56 * width, top - .28 * height),
        (left + .68 * width, top - bar_ratio * height),
    )
    contour.lineTo(left + .05 * width, top - bar_ratio * height)
    contour.closed = True
    return contour_layer([contour])


def fraction_candidates(name, base, source, bar, eights):
    numerator, old_bar, old_denominator = split_fraction(base[name].foreground)
    numerator_bbox = contour_bbox(numerator)
    registered_bar = v4.registered_to_bbox(contour_layer([bar]), contour_bbox(old_bar))
    denominator_bbox = sf.bbox_of_sets(contour_sets(old_denominator))
    numerator_layers = []
    if name == "seveneighths":
        for bar_ratio in (.15, .17, .19):
            for diagonal in (.38, .42):
                numerator_layers.append((
                    "clean-seven-{:.2f}-{:.2f}".format(bar_ratio, diagonal),
                    clean_seven_layer(numerator_bbox, bar_ratio, diagonal),
                ))
        existing_seven = contour_layer([numerator])
        for maximum_turn in (32.0, 44.0, 60.0, 90.0):
            aligned, converted = aligned_smooth_handles(
                existing_seven, maximum_turn=maximum_turn
            )
            numerator_layers.append((
                "smooth-seven-{}-{:.0f}".format(converted, maximum_turn), aligned
            ))
    else:
        numerator_layer = contour_layer([numerator])
        if name == "threeeighths":
            numerator_sets = contour_sets([numerator])
            for anchor_count in (10, 11):
                for tension in (.18, .22):
                    numerator_layers.append((
                        "curve-three-{}-{:.2f}".format(anchor_count, tension),
                        curve_layer(
                            numerator_sets, (anchor_count,), tension=tension,
                            corner_angle=42.0,
                        ),
                    ))
            for error in (5.0, 8.0):
                compact = v6.scratch_cleanup(numerator_layer, error)
                compact, converted = aligned_smooth_handles(
                    compact, maximum_turn=32.0
                )
                numerator_layers.append((
                    "compact-three-{:.2f}-{}".format(error, converted),
                    compact,
                ))
        for maximum_turn in (32.0, 44.0, 60.0):
            aligned, converted = aligned_smooth_handles(
                numerator_layer, maximum_turn=maximum_turn
            )
            numerator_layers.append((
                "retained-{}-{:.0f}".format(converted, maximum_turn), aligned
            ))
    raw_sets = v4.point_sets(source[name].foreground)
    candidates = []
    for eight_method, eight_turn, eight_layer in eights:
        registered_eight = v4.registered_to_bbox(eight_layer, denominator_bbox)
        for numerator_method, numerator_layer in numerator_layers:
            combined = contour_layer(
                list(numerator_layer) + list(registered_bar) + list(registered_eight)
            )
            if point_count(combined) > CEILING:
                continue
            method = "fraction-shared-bar-eight:{}:{}:{:.0f}".format(
                numerator_method, eight_method, eight_turn
            )
            candidate = evaluate(raw_sets, combined, method)
            candidate["donor"] = (
                "threeeighths-bar,threeeighths-eight-master"
                + (",uni2077" if name == "seveneighths" else "")
            )
            candidates.append(candidate)
    return raw_sets, candidates


def patch_d_metrics(path):
    font = TTFont(str(path), recalcBBoxes=False, recalcTimestamp=False)
    try:
        font["hmtx"].metrics["uni0414"] = font["hmtx"].metrics["D"]
        font.save(str(path), reorderTables=False)
    finally:
        font.close()


def publish_to_canonical():
    copies = {
        V7 / "redrawn.sfd": ROOT / "fontforge" / "redrawn.sfd",
        V7 / "redraw-review.sfd": ROOT / "fontforge" / "redraw-review.sfd",
        V7 / "redrawn.ttf": ROOT / "qa" / "assets" / "redrawn.ttf",
        V7 / "redraw-review.ttf": ROOT / "qa" / "assets" / "redraw-review.ttf",
        V7 / "redraw-report.csv": ROOT / "qa" / "assets" / "redraw-report.csv",
        V7 / "redraw-manual-decisions.json": ROOT / "qa" / "redraw-manual-decisions.json",
    }
    for source, target in copies.items():
        shutil.copy2(source, target)
    index = ROOT / "qa" / "index.html"
    content = index.read_text(encoding="utf-8")
    content = re.sub(
        r'assets/redrawn\.ttf\?v=[^"]+',
        "assets/redrawn.ttf?v=redraw-v7",
        content,
    )
    index.write_text(content, encoding="utf-8", newline="\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if V7.exists() and not args.force:
        raise SystemExit("redraw-v7 exists; use --force")
    WORK.mkdir(parents=True, exist_ok=True)

    with (V6 / "redraw-report.csv").open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        old_rows = {row["glyph"]: row for row in reader}
    for field in EXTRA_FIELDS:
        if field not in fields:
            fields.append(field)
    decisions_payload = json.loads(
        (V6 / "redraw-manual-decisions.json").read_text(encoding="utf-8")
    )
    decisions = decisions_payload["decisions"]
    source = fontforge.open(str(ROOT / "fontforge" / "original.sfd"))
    output = fontforge.open(str(V6 / "redrawn.sfd"))
    order = [glyph.glyphname for glyph in output.glyphs()]
    selected_by_name = {}
    methods = {}
    donors = {}
    try:
        three_numerator, shared_bar, _three_denominator = split_fraction(
            output["threeeighths"].foreground
        )
        del three_numerator
        eights = shared_eight_candidates(output)
        for name in order:
            if name not in TARGETS:
                continue
            baseline_sets = v4.point_sets(source[name].foreground)
            baseline = evaluate(
                baseline_sets, output[name].foreground.dup(), "v6-baseline",
                normalize=False,
            )
            if name == "uni0414":
                donor_sets = v4.point_sets(output["D"].foreground)
                candidate = evaluate(
                    donor_sets, output["D"].foreground.dup(), "latin-D-exact",
                    normalize=False,
                )
                if candidate["points"] > CEILING:
                    raise RuntimeError("Latin D exceeds v7 point ceiling")
                selected = candidate
                donors[name] = "D"
            elif name == "uni041D":
                connector, converted = smooth_connector(output[name].foreground)
                if converted < 2:
                    raise RuntimeError("uni041D connector did not expose enough smooth anchors")
                candidate = evaluate(
                    baseline_sets, connector,
                    "localized-middle-connector:{}".format(converted),
                    normalize=False,
                )
                selected = select_candidate(name, [candidate], baseline)
            elif name in FULL_REFITS:
                _shape_sets, candidates = full_refit_candidates(
                    name, output[name].foreground, baseline_sets)
                selected = select_candidate(name, candidates, baseline)
            else:
                _raw_sets, candidates = fraction_candidates(
                    name, output, source, shared_bar, eights
                )
                selected = select_candidate(name, candidates, baseline)
                donors[name] = selected.get("donor", "")
            output[name].foreground = selected["layer"].dup()
            if name == "uni0414":
                output[name].width = output["D"].width
            selected_by_name[name] = selected
            methods[name] = selected["method"]
            counts = rf.point_type_counts(selected["layer"])
            print(
                "v7 {}: {} points; {} curves; {} corners; {}".format(
                    name, selected["points"],
                    counts["curve"] + counts["hvcurve"], counts["corner"],
                    selected["method"],
                ),
                flush=True,
            )
        output.save(str(WORK / "redrawn.sfd"))
        output.generate(str(WORK / "redrawn.ttf"))
    finally:
        source.close()
        output.close()

    restored = subprocess.run(
        [
            sys.executable, str(ROOT / "tools" / "restore_redraw_metadata.py"),
            str(V6 / "redrawn.ttf"), str(WORK / "redrawn.ttf"),
        ],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if restored.returncode:
        raise RuntimeError("metadata restore failed: " + restored.stdout[-2000:])
    patch_d_metrics(WORK / "redrawn.ttf")

    saved = fontforge.open(str(WORK / "redrawn.sfd"))
    original = fontforge.open(str(ROOT / "fontforge" / "original.sfd"))
    previous = fontforge.open(str(V6 / "redrawn.sfd"))
    new_rows = []
    now = datetime.now(timezone.utc).isoformat()
    try:
        for name in order:
            old = dict(old_rows[name])
            if name not in TARGETS:
                old["changed_in_v7"] = "false"
                new_rows.append(old)
                continue
            selected = selected_by_name[name]
            reference_sets = (
                v4.point_sets(saved["D"].foreground)
                if name == "uni0414"
                else v4.point_sets(original[name].foreground)
            )
            final_candidate = evaluate(
                reference_sets, saved[name].foreground.dup(),
                methods[name] + ":serialized", normalize=False,
            )
            row = rf.result_row(
                original[name], original[name].foreground, final_candidate, 1,
                {"decisions": {}},
            )
            for field, value in old.items():
                row.setdefault(field, value)
            before = rf.point_type_counts(previous[name].foreground)
            after = rf.point_type_counts(saved[name].foreground)
            status = "pass" if name == "uni0414" else "needs_rework"
            row.update({
                "changed_in_v7": "true",
                "v7_rebuild_method": methods[name],
                "v7_previous_corner_points": str(before["corner"]),
                "v7_resulting_corner_points": str(after["corner"]),
                "v7_resulting_curve_points": str(after["curve"] + after["hvcurve"]),
                "v7_donor_glyphs": donors.get(name, ""),
                "v7_shared_fraction_bar": "true" if name in FRACTIONS else "false",
                "v7_shared_denominator_eight": "true" if name in FRACTIONS else "false",
                "current_review_status": status,
                "point_ceiling": str(CEILING),
                "repair_method": methods[name],
            })
            decisions[name] = {
                "status": status,
                "source_hash": row["source_hash"],
                "candidate_hash": row["candidate_hash"],
                "updated_at": now,
            }
            rf.apply_manual_decision(row, {"decisions": decisions})
            new_rows.append(row)
    finally:
        saved.close()
        original.close()
        previous.close()

    review_sfd = WORK / "redraw-review.sfd"
    review_ttf = WORK / "redraw-review.ttf"
    shutil.copy2(WORK / "redrawn.sfd", review_sfd)
    shutil.copy2(WORK / "redrawn.ttf", review_ttf)
    report = WORK / "redraw-report.csv"
    with report.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(new_rows)
    decisions_payload.update({
        "version": "redraw-manual-review-v7",
        "candidate_version": "redraw-v7",
    })
    decision_path = WORK / "redraw-manual-decisions.json"
    atomic_json(decision_path, decisions_payload)

    if V7.exists():
        shutil.rmtree(V7)
    V7.mkdir(parents=True)
    artifacts = {
        "redrawn.sfd": WORK / "redrawn.sfd",
        "redrawn.ttf": WORK / "redrawn.ttf",
        "redraw-review.sfd": review_sfd,
        "redraw-review.ttf": review_ttf,
        "redraw-report.csv": report,
        "redraw-manual-decisions.json": decision_path,
    }
    for filename, path in artifacts.items():
        shutil.copy2(path, V7 / filename)
    manifest = {
        "version": "redraw-v7",
        "base_version": "redraw-v6",
        "created_at": now,
        "glyph_count": len(new_rows),
        "hard_point_ceiling": CEILING,
        "target_glyphs": sorted(TARGETS),
        "changed_glyphs": sorted(TARGETS),
        "metric_exceptions": {"uni0414": "copied from D"},
        "repair_methods": methods,
        "donor_glyphs": donors,
        "artifact_hashes": {
            name: sha256(V7 / name) for name in artifacts
        },
    }
    atomic_json(V7 / "manifest.json", manifest)
    publish_to_canonical()
    print(json.dumps({"changed": len(TARGETS), "version": "redraw-v7"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
