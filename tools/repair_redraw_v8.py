#!/usr/bin/env ffpython
"""Build redraw-v8 with equal-arc cubic anchors and a shared smooth outer eight."""

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
import repair_redraw_v7 as v7
import simplify_font as sf

V7 = ROOT / "checkpoints" / "redraw-v7"
V8 = ROOT / "checkpoints" / "redraw-v8"
WORK = ROOT / "checkpoints" / "redraw-v8-work"
TARGETS = {
    "uni0401", "uni041D", "dong", "uni20B4", "uni20BA",
    "oneeighth", "threeeighths", "fiveeighths", "seveneighths",
}
FRACTIONS = {"oneeighth", "threeeighths", "fiveeighths", "seveneighths"}
FULL_LETTERS = {"uni0401", "dong", "uni20B4", "uni20BA"}
CEILING = 100
EXTRA_FIELDS = (
    "changed_in_v8", "on_curve_points", "control_points",
    "v8_rebuild_method", "v8_equal_spacing", "v8_spacing_ratio",
    "v8_smooth_curve_points", "v8_preserved_components",
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
    return v7.contour_layer(contours)


def contour_bbox(contour):
    return v7.contour_bbox(contour)


def cyclic_values(values, left, right):
    result = []
    index = (left + 1) % len(values)
    while index != right:
        result.append(values[index])
        index = (index + 1) % len(values)
        if len(result) > len(values):
            raise RuntimeError("invalid cyclic contour interval")
    return result


def cubicize_layer(layer):
    """Rebuild every segment as cubic without changing its fitted geometry."""
    result = fontforge.layer()
    result.is_quadratic = False
    for source_contour in layer:
        values = list(source_contour)
        on_indices = [index for index, point in enumerate(values) if point.on_curve]
        if len(on_indices) < 3:
            raise RuntimeError("cubic contour requires at least three on-curve anchors")
        contour = fontforge.contour()
        contour.is_quadratic = False
        first = values[on_indices[0]]
        contour.moveTo(first.x, first.y)
        for position, left_index in enumerate(on_indices):
            right_index = on_indices[(position + 1) % len(on_indices)]
            start = values[left_index]
            end = values[right_index]
            controls = [point for point in cyclic_values(values, left_index, right_index)
                        if not point.on_curve]
            if len(controls) >= 2:
                control1 = (controls[0].x, controls[0].y)
                control2 = (controls[-1].x, controls[-1].y)
            else:
                delta = (end.x - start.x, end.y - start.y)
                control1 = (start.x + delta[0] / 3.0, start.y + delta[1] / 3.0)
                control2 = (start.x + 2.0 * delta[0] / 3.0,
                            start.y + 2.0 * delta[1] / 3.0)
            contour.cubicTo(control1, control2, (end.x, end.y))
        contour.closed = True
        result += contour
    return rf.classify_layer(result)


def equal_fit(reference_layer, counts, maximum_turn):
    point_sets = v4.point_sets(reference_layer)
    if len(point_sets) != len(counts):
        raise RuntimeError("equal-fit contour allocation mismatch")
    anchors = [
        v7.evenly_spaced_indices(points, count)
        for points, count in zip(point_sets, counts)
    ]
    fitted, _lines, _curves = rf.build_layer(point_sets, anchors)
    fitted = cubicize_layer(fitted)
    if maximum_turn is not None:
        fitted, _converted = v7.aligned_smooth_handles(
            fitted, maximum_turn=maximum_turn
        )
    return fitted


def contour_segments(contour):
    values = list(contour)
    on_indices = [index for index, point in enumerate(values) if point.on_curve]
    result = []
    for position, left_index in enumerate(on_indices):
        right_index = on_indices[(position + 1) % len(on_indices)]
        start = values[left_index]
        end = values[right_index]
        controls = [point for point in cyclic_values(values, left_index, right_index)
                    if not point.on_curve]
        result.append((start, controls, end))
    return result


def cubic_point(start, control1, control2, end, t):
    u = 1.0 - t
    return (
        u ** 3 * start[0] + 3 * u * u * t * control1[0]
        + 3 * u * t * t * control2[0] + t ** 3 * end[0],
        u ** 3 * start[1] + 3 * u * u * t * control1[1]
        + 3 * u * t * t * control2[1] + t ** 3 * end[1],
    )


def segment_length(segment):
    start, controls, end = segment
    start_xy = (start.x, start.y)
    end_xy = (end.x, end.y)
    if len(controls) < 2:
        return rf.distance(start_xy, end_xy)
    control1 = (controls[0].x, controls[0].y)
    control2 = (controls[-1].x, controls[-1].y)
    total = 0.0
    previous = start_xy
    for step in range(1, 21):
        current = cubic_point(start_xy, control1, control2, end_xy, step / 20.0)
        total += rf.distance(previous, current)
        previous = current
    return total


def spacing_ratio(layer, curved_only=True):
    ratios = []
    for contour in layer:
        lengths = []
        for start, controls, end in contour_segments(contour):
            if curved_only and (
                start.type == fontforge.splineCorner
                or end.type == fontforge.splineCorner
            ):
                continue
            length = segment_length((start, controls, end))
            if length > 1e-4:
                lengths.append(length)
        if len(lengths) >= 2:
            ratios.append(max(lengths) / min(lengths))
    return max(ratios) if ratios else 1.0


def evaluate(reference_sets, layer, method, normalize=True):
    return v4.evaluate(
        reference_sets, reference_sets, layer, CEILING, method,
        normalize=normalize,
    )


def structurally_clean(candidate):
    return (
        candidate["points"] <= CEILING
        and not candidate["self_intersections"]
        and not candidate["invalid_handles"]
        and candidate["metrics"]["topology_match"]
        and candidate["direction_valid"]
        and candidate["degenerate_contours"] == 0
    )


def quality_gate(candidate, baseline, fraction=False):
    if not structurally_clean(candidate):
        return False
    new = candidate["metrics"]
    old = baseline["metrics"]
    n128 = new["per_size"][128]
    o128 = old["per_size"][128]
    iou_tolerance = .09 if fraction else .05
    ink_tolerance = .08 if fraction else .055
    return (
        n128["ink_iou"] >= o128["ink_iou"] - iou_tolerance
        and n128["false_positive_ink"]
        <= o128["false_positive_ink"] + ink_tolerance
        and n128["false_negative_ink"]
        <= o128["false_negative_ink"] + ink_tolerance
        and new["boundary_p95"] <= old["boundary_p95"] + .010
    )


def rebuild_letter(name, base):
    current = base[name].foreground
    if name == "uni0401":
        records = sorted(
            [(len(contour), index, contour.dup())
             for index, contour in enumerate(current)],
            reverse=True,
        )
        main_index = records[0][1]
        main = contour_layer([current[main_index]])
        rebuilt_main = equal_fit(main, (20,), None)
        contours = []
        for index, contour in enumerate(current):
            contours.extend(list(rebuilt_main) if index == main_index else [contour.dup()])
        return contour_layer(contours), "equal-arc-main:20", "accents"
    allocations = {
        "dong": ((7, 6, 20), 24.0),
        "uni20B4": ((33,), 24.0),
        "uni20BA": ((33,), 44.0),
    }
    counts, maximum_turn = allocations[name]
    return (
        equal_fit(current, counts, maximum_turn),
        "equal-arc-full:{}:turn-{:.0f}".format(
            "-".join(map(str, counts)), maximum_turn
        ),
        "",
    )


def redistribute_connector(layer):
    result = fontforge.layer()
    result.is_quadratic = False
    changed = 0
    for source_contour in layer:
        values = list(source_contour)
        on_indices = [index for index, point in enumerate(values) if point.on_curve]
        eligible = [
            position for position, index in enumerate(on_indices)
            if 280 <= values[index].x <= 670 and 320 <= values[index].y <= 470
        ]
        groups = []
        current = []
        for position in eligible:
            if current and position != current[-1] + 1:
                groups.append(current)
                current = []
            current.append(position)
        if current:
            groups.append(current)
        connector_groups = [group for group in groups if len(group) >= 4]
        connector_positions = set()
        smooth_positions = set()
        for group in connector_groups:
            connector_positions.update(group)
            smooth_positions.update(group[1:-1])
            anchors = [values[on_indices[position]] for position in group]
            originals = [(point.x, point.y) for point in anchors]
            distances = [0.0]
            for left, right in zip(originals, originals[1:]):
                distances.append(distances[-1] + rf.distance(left, right))
            total = distances[-1]
            for offset in range(1, len(anchors) - 1):
                target = total * offset / (len(anchors) - 1)
                segment = 0
                while segment + 1 < len(distances) and distances[segment + 1] < target:
                    segment += 1
                span = max(1e-9, distances[segment + 1] - distances[segment])
                local = (target - distances[segment]) / span
                left, right = originals[segment], originals[segment + 1]
                anchors[offset].x = left[0] + (right[0] - left[0]) * local
                anchors[offset].y = left[1] + (right[1] - left[1]) * local
                changed += 1

        contour = fontforge.contour()
        contour.is_quadratic = False
        first = values[on_indices[0]]
        contour.moveTo(first.x, first.y)
        desired_types = [first.type]
        for position, left_index in enumerate(on_indices):
            right_position = (position + 1) % len(on_indices)
            right_index = on_indices[right_position]
            start = values[left_index]
            end = values[right_index]
            controls = [point for point in cyclic_values(values, left_index, right_index)
                        if not point.on_curve]
            same_connector_run = any(
                position in group and right_position in group
                and right_position == position + 1
                for group in connector_groups
            )
            if same_connector_run:
                chord = rf.distance((start.x, start.y), (end.x, end.y))
                if position in smooth_positions:
                    previous_position = (position - 1) % len(on_indices)
                    previous = values[on_indices[previous_position]]
                    outgoing = rf.normalize((end.x - previous.x, end.y - previous.y))
                else:
                    outgoing = rf.normalize((end.x - start.x, end.y - start.y))
                if right_position in smooth_positions:
                    following_position = (right_position + 1) % len(on_indices)
                    following = values[on_indices[following_position]]
                    incoming = rf.normalize((following.x - start.x, following.y - start.y))
                else:
                    incoming = rf.normalize((end.x - start.x, end.y - start.y))
                control1 = (start.x + outgoing[0] * chord / 3.0,
                            start.y + outgoing[1] * chord / 3.0)
                control2 = (end.x - incoming[0] * chord / 3.0,
                            end.y - incoming[1] * chord / 3.0)
                contour.cubicTo(control1, control2, (end.x, end.y))
            elif len(controls) >= 2:
                contour.cubicTo(
                    (controls[0].x, controls[0].y),
                    (controls[-1].x, controls[-1].y),
                    (end.x, end.y),
                )
            else:
                contour.lineTo(end.x, end.y)
            desired_types.append(
                fontforge.splineCurve if right_position in smooth_positions
                else end.type
            )
        contour.closed = True
        on_curves = [point for point in contour if point.on_curve]
        for point, point_type in zip(on_curves, desired_types):
            point.type = point_type
        result += contour
    if changed < 2:
        raise RuntimeError("uni041D connector did not expose an equal-spacing run")
    return result, "equal-arc-middle-connector-cubic:{}".format(changed), "stems"


def sort_denominator(contours):
    records = sorted(
        contours,
        key=lambda contour: -(
            (contour_bbox(contour)[2] - contour_bbox(contour)[0])
            * (contour_bbox(contour)[3] - contour_bbox(contour)[1])
        ),
    )
    return [records[0]] + sorted(
        records[1:],
        key=lambda contour: (
            contour_bbox(contour)[1] + contour_bbox(contour)[3]
        ) / 2.0,
    )


def build_outer_master(base):
    _numerator, _bar, denominator = v7.split_fraction(
        base["threeeighths"].foreground
    )
    outer = sort_denominator(denominator)[0]
    point_sets = v4.point_sets(contour_layer([outer]))
    return v7.curve_layer(
        point_sets, (8,), tension=.39, corner_angle=180.0,
        force_smooth_indices=(0,),
    )


def rebuild_fraction(name, base, outer_master):
    numerator, bar, denominator = v7.split_fraction(base[name].foreground)
    denominator = sort_denominator(denominator)
    old_outer = denominator[0]
    inner = denominator[1:]
    registered_outer = v4.registered_to_bbox(
        outer_master, contour_bbox(old_outer)
    )
    numerator_layer = contour_layer([numerator])
    preserved = "bar,inner-counters,numerator"
    if name == "threeeighths":
        numerator_layer = v6.scratch_cleanup(numerator_layer, 4.0)
        preserved = "bar,inner-counters"
    combined = contour_layer(
        list(numerator_layer) + [bar.dup()]
        + list(registered_outer) + [contour.dup() for contour in inner]
    )
    method = "shared-outer-eight:8:tension-0.39"
    if name == "threeeighths":
        method += ":compact-three-4.0"
    return combined, method, preserved


def patch_metadata(reference, target):
    completed = subprocess.run(
        [
            sys.executable, str(ROOT / "tools" / "restore_redraw_metadata.py"),
            str(reference), str(target),
        ],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if completed.returncode:
        raise RuntimeError("metadata restore failed: " + completed.stdout[-2000:])


def publish_to_canonical():
    copies = {
        V8 / "redrawn.sfd": ROOT / "fontforge" / "redrawn.sfd",
        V8 / "redraw-review.sfd": ROOT / "fontforge" / "redraw-review.sfd",
        V8 / "redrawn.ttf": ROOT / "qa" / "assets" / "redrawn.ttf",
        V8 / "redraw-review.ttf": ROOT / "qa" / "assets" / "redraw-review.ttf",
        V8 / "redraw-report.csv": ROOT / "qa" / "assets" / "redraw-report.csv",
        V8 / "redraw-manual-decisions.json": ROOT / "qa" / "redraw-manual-decisions.json",
    }
    for source, target in copies.items():
        shutil.copy2(source, target)
    index = ROOT / "qa" / "index.html"
    content = index.read_text(encoding="utf-8")
    content = re.sub(
        r'assets/redrawn\.ttf\?v=[^"]+',
        "assets/redrawn.ttf?v=redraw-v8",
        content,
    )
    content = content.replace(
        '<div class="metric"><span>SFD points</span>',
        '<div class="metric"><span>Serialized points</span>',
    )
    content = content.replace(
        'elements.points.textContent = `${row.source_points} → ${row.redrawn_points}${row.simple === "true" ? " · simple" : ""}`;',
        'elements.points.textContent = `${row.source_points} → ${row.redrawn_points} total = ${row.on_curve_points} anchors + ${row.control_points} controls${row.simple === "true" ? " · simple" : ""}`;',
    )
    index.write_text(content, encoding="utf-8", newline="\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if V8.exists() and not args.force:
        raise SystemExit("redraw-v8 exists; use --force")
    WORK.mkdir(parents=True, exist_ok=True)

    with (V7 / "redraw-report.csv").open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        old_rows = {row["glyph"]: row for row in reader}
    for field in EXTRA_FIELDS:
        if field not in fields:
            fields.append(field)
    decision_source = ROOT / "qa" / "redraw-manual-decisions.json"
    decisions_payload = json.loads(decision_source.read_text(encoding="utf-8"))
    decisions = decisions_payload["decisions"]
    source = fontforge.open(str(ROOT / "fontforge" / "original.sfd"))
    base = fontforge.open(str(V7 / "redrawn.sfd"))
    output = base
    order = [glyph.glyphname for glyph in output.glyphs()]
    methods = {}
    preserved_by_name = {}
    selected_by_name = {}
    outer_master = build_outer_master(base)
    try:
        for name in order:
            if name not in TARGETS:
                continue
            raw_sets = v4.point_sets(source[name].foreground)
            baseline = evaluate(
                raw_sets, base[name].foreground.dup(), "v7-baseline",
                normalize=False,
            )
            if name in FULL_LETTERS:
                layer, method, preserved = rebuild_letter(name, base)
            elif name == "uni041D":
                layer, method, preserved = redistribute_connector(
                    base[name].foreground
                )
            else:
                layer, method, preserved = rebuild_fraction(
                    name, base, outer_master
                )
            candidate = evaluate(raw_sets, layer, method, normalize=False)
            if not quality_gate(candidate, baseline, name in FRACTIONS):
                metrics = candidate["metrics"]["per_size"][128]
                raise RuntimeError(
                    "{} failed v8 gate: points={} self={} topology={} iou={:.4f}".format(
                        name, candidate["points"], candidate["self_intersections"],
                        candidate["metrics"]["topology_match"], metrics["ink_iou"],
                    )
                )
            output[name].foreground = candidate["layer"].dup()
            methods[name] = method
            preserved_by_name[name] = preserved
            selected_by_name[name] = candidate
            counts = rf.point_type_counts(candidate["layer"])
            print(
                "v8 {}: {} total = {} anchors + {} controls; curves {}; corners {}; {}".format(
                    name, candidate["points"],
                    counts["corner"] + counts["curve"] + counts["hvcurve"]
                    + counts["tangent"],
                    counts["off"], counts["curve"] + counts["hvcurve"],
                    counts["corner"], method,
                ),
                flush=True,
            )
        output.save(str(WORK / "redrawn.sfd"))
        output.generate(str(WORK / "redrawn.ttf"))
    finally:
        source.close()
        output.close()

    patch_metadata(V7 / "redrawn.ttf", WORK / "redrawn.ttf")

    saved = fontforge.open(str(WORK / "redrawn.sfd"))
    original = fontforge.open(str(ROOT / "fontforge" / "original.sfd"))
    previous = fontforge.open(str(V7 / "redrawn.sfd"))
    new_rows = []
    now = datetime.now(timezone.utc).isoformat()
    try:
        for name in order:
            old = dict(old_rows[name])
            if name not in TARGETS:
                counts = rf.point_type_counts(saved[name].foreground)
                old["on_curve_points"] = str(counts["corner"] + counts["curve"] + counts["hvcurve"] + counts["tangent"] )
                old["control_points"] = str(counts["off"] )
                old["changed_in_v8"] = "false"
                new_rows.append(old)
                continue
            raw_sets = v4.point_sets(original[name].foreground)
            final_candidate = evaluate(
                raw_sets, saved[name].foreground.dup(),
                methods[name] + ":serialized", normalize=False,
            )
            if not structurally_clean(final_candidate):
                raise RuntimeError(name + " became invalid after SFD serialization")
            row = rf.result_row(
                original[name], original[name].foreground, final_candidate, 1,
                {"decisions": {}},
            )
            for field, value in old.items():
                row.setdefault(field, value)
            counts = rf.point_type_counts(saved[name].foreground)
            on_curve = (
                counts["corner"] + counts["curve"] + counts["hvcurve"]
                + counts["tangent"]
            )
            ratio = spacing_ratio(
                saved[name].foreground,
                curved_only=True,
            )
            row.update({
                "changed_in_v8": "true",
                "on_curve_points": str(on_curve),
                "control_points": str(counts["off"]),
                "v8_rebuild_method": methods[name],
                "v8_equal_spacing": "true",
                "v8_spacing_ratio": "{:.6f}".format(ratio),
                "v8_smooth_curve_points": str(counts["curve"] + counts["hvcurve"]),
                "v8_preserved_components": preserved_by_name[name],
                "current_review_status": "needs_rework",
                "point_ceiling": str(CEILING),
                "repair_method": methods[name],
            })
            decisions[name] = {
                "status": "needs_rework",
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
        "version": "redraw-manual-review-v8",
        "candidate_version": "redraw-v8",
    })
    decision_path = WORK / "redraw-manual-decisions.json"
    atomic_json(decision_path, decisions_payload)

    if V8.exists():
        shutil.rmtree(V8)
    V8.mkdir(parents=True)
    artifacts = {
        "redrawn.sfd": WORK / "redrawn.sfd",
        "redrawn.ttf": WORK / "redrawn.ttf",
        "redraw-review.sfd": review_sfd,
        "redraw-review.ttf": review_ttf,
        "redraw-report.csv": report,
        "redraw-manual-decisions.json": decision_path,
    }
    for filename, path in artifacts.items():
        shutil.copy2(path, V8 / filename)
    manifest = {
        "version": "redraw-v8",
        "base_version": "redraw-v7",
        "created_at": now,
        "glyph_count": len(new_rows),
        "hard_point_ceiling": CEILING,
        "point_budget_definition": "on-curve anchors plus off-curve controls",
        "target_glyphs": sorted(TARGETS),
        "changed_glyphs": sorted(TARGETS),
        "repair_methods": methods,
        "preserved_components": preserved_by_name,
        "artifact_hashes": {
            name: sha256(V8 / name) for name in artifacts
        },
    }
    atomic_json(V8 / "manifest.json", manifest)
    publish_to_canonical()
    print(json.dumps({"changed": len(TARGETS), "version": "redraw-v8"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
