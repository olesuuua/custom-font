#!/usr/bin/env ffpython
"""Repair only the reviewed-v1 redraw queue and publish redraw-v2."""

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
from datetime import datetime, timezone
from pathlib import Path

import fontforge


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf
import simplify_font as sf


REVIEWED = ROOT / "checkpoints" / "redraw-reviewed-v1"
V2 = ROOT / "checkpoints" / "redraw-v2"
EXTRA_FIELDS = [
    "previous_review_status", "current_review_status", "changed_in_v2",
    "repaired_structure", "repair_method", "previous_failure_reasons",
    "terminal_boundary_p95", "terminal_boundary_max", "locked_in_v1",
]


DIGIT_DONORS = {
    "uni2070": "zero", "uni00B9": "one", "uni00B2": "two", "uni00B3": "three",
    "uni2074": "four", "uni2075": "five", "uni2076": "six", "uni2077": "seven",
    "uni2078": "eight", "uni2079": "nine", "uni2080": "zero", "uni2081": "one",
    "uni2082": "two", "uni2083": "three", "uni2084": "four", "uni2085": "five",
    "uni2086": "six", "uni2087": "seven", "uni2088": "eight", "uni2089": "nine",
}
RELATED_DONORS = {
    **DIGIT_DONORS,
    "uni207A": "plus", "uni208A": "plus", "uni207B": "minus", "uni208B": "minus",
    "uni207C": "equal", "uni208C": "equal", "uni207D": "parenleft",
    "uni208D": "parenleft", "uni207E": "parenright", "uni208E": "parenright",
    "quotedblbase": "quotedblleft", "guilsinglleft": "guillemotleft",
    "guilsinglright": "guillemotright", "arrowleft": "arrowdblleft",
    "arrowright": "arrowdblright", "arrowboth": "arrowdblboth",
}
DETAILED_SOURCE_REFITS = {"beta", "gamma", "psi", "uni0439", "uni2075", "uni2085", "uni20A6"}


def atomic_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bool_text(value) -> bool:
    return str(value).lower() == "true"


def segment_counts(layer):
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


def protected_anchors(point_sets):
    """Reserve extrema plus shoulders so round stroke caps cannot collapse to wedges."""
    result = []
    for points in point_sets:
        count = len(points)
        if count < 8:
            result.append(list(range(count)))
            continue
        extrema = {
            min(range(count), key=lambda i: points[i][0]),
            max(range(count), key=lambda i: points[i][0]),
            min(range(count), key=lambda i: points[i][1]),
            max(range(count), key=lambda i: points[i][1]),
        }
        window = max(2, min(8, count // 32))
        anchors = set(extrema)
        for index in extrema:
            anchors.add((index - window) % count)
            anchors.add((index + window) % count)
        # Local U-turns identify round stroke terminals that are not global
        # x/y extrema (the lower-left cap in uni20A6 is the key example).
        turn_window = max(2, min(10, count // 40))
        separation = max(4, count // 18)
        ranked = sorted(
            range(count), key=lambda index: rf.anchor_turn(points, index, turn_window), reverse=True
        )
        selected_turns = []
        for index in ranked:
            if rf.anchor_turn(points, index, turn_window) < 18.0:
                break
            if any(min((index - prior) % count, (prior - index) % count) < separation for prior in selected_turns):
                continue
            selected_turns.append(index)
            anchors.update({index, (index - window) % count, (index + window) % count})
            if len(selected_turns) >= 5:
                break
        result.append(sorted(anchors))
    return result


def terminal_distances(source_sets, candidate_sets, bbox):
    diagonal = max(1.0, math.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1]))
    values = []
    for points, anchors in zip(source_sets, protected_anchors(source_sets)):
        for index in anchors:
            values.append(sf.distance_to_sets(points[index], candidate_sets) / diagonal)
    values.sort()
    if not values:
        return 0.0, 0.0
    return values[min(len(values) - 1, int(len(values) * .95))], values[-1]


def enrich_candidate(candidate, source_sets, context, method):
    p95, maximum = terminal_distances(
        source_sets, candidate["metrics"]["candidate_sets"], context["bbox"]
    )
    candidate["terminal_p95"] = p95
    candidate["terminal_max"] = maximum
    candidate["method"] = method
    return candidate


def repair_key(candidate):
    invalid_structure = (
        candidate["points"] > rf.MAX_POINTS
        or candidate["self_intersections"] > 0
        or candidate["invalid_handles"] > 0
    )
    metrics = candidate["metrics"]
    raster = max(
        max(
            values["mse"] / rf.MAX_MSE,
            rf.MIN_IOU / max(rf.EPSILON, values["ink_iou"]),
            values["false_positive_ink"] / rf.MAX_FALSE_INK,
            values["false_negative_ink"] / rf.MAX_FALSE_INK,
        )
        for values in metrics["per_size"].values()
    )
    return (
        1 if invalid_structure else 0,
        1 if not metrics["topology_match"] else 0,
        candidate.get("terminal_p95", 1.0), candidate.get("terminal_max", 1.0),
        raster, metrics["boundary_p95"], metrics["boundary_max"], candidate["points"],
    )


def raster_score(candidate):
    return max(
        max(
            values["mse"] / rf.MAX_MSE,
            rf.MIN_IOU / max(rf.EPSILON, values["ink_iou"]),
            values["false_positive_ink"] / rf.MAX_FALSE_INK,
            values["false_negative_ink"] / rf.MAX_FALSE_INK,
        )
        for values in candidate["metrics"]["per_size"].values()
    )


def promotion_is_material(candidate, baseline):
    candidate_structure = (
        candidate["points"] > rf.MAX_POINTS or candidate["self_intersections"]
        or candidate["invalid_handles"]
    )
    baseline_structure = (
        baseline["points"] > rf.MAX_POINTS or baseline["self_intersections"]
        or baseline["invalid_handles"]
    )
    if baseline_structure and not candidate_structure and candidate["metrics"]["topology_match"]:
        return True
    candidate_topology = candidate["metrics"]["topology_match"]
    baseline_topology = baseline["metrics"]["topology_match"]
    if candidate_topology and not baseline_topology and not candidate_structure:
        return True
    if candidate_structure or not candidate_topology:
        return False
    candidate_raster, baseline_raster = raster_score(candidate), raster_score(baseline)
    if candidate.get("method", "").startswith("family-donor:") and candidate_raster <= baseline_raster * .90:
        return True
    terminal_gain = baseline["terminal_p95"] - candidate["terminal_p95"]
    boundary_ok = candidate["metrics"]["boundary_p95"] <= max(
        baseline["metrics"]["boundary_p95"] * 1.10, baseline["metrics"]["boundary_p95"] + .001
    )
    terminal_repair = terminal_gain >= .001 and candidate_raster <= baseline_raster * 1.05 and boundary_ok
    similarity_repair = candidate_raster <= baseline_raster * .97 and (
        candidate["terminal_p95"] <= baseline["terminal_p95"] + .002
    )
    return terminal_repair or similarity_repair


def evaluate(source_sets, layer, context, method, full=True):
    layer = rf.classify_layer(layer.dup())
    lines, curves = segment_counts(layer)
    candidate = rf.evaluate_layer(source_sets, layer, lines, curves, context, full_audit=full)
    return enrich_candidate(candidate, source_sets, context, method)


def compact_refit(reference_sets, source_sets, context, method):
    seeds = protected_anchors(reference_sets)
    anchors = []
    for points, protected in zip(reference_sets, seeds):
        anchors.append(sorted(set(rf.initial_anchors(points)) | set(protected)))
    layer, _lines, _curves = rf.build_layer(reference_sets, anchors)
    if sum(len(contour) for contour in layer) > rf.MAX_POINTS:
        anchors = [rf.initial_anchors(points) for points in reference_sets]
        layer, _lines, _curves = rf.build_layer(reference_sets, anchors)
    if sum(len(contour) for contour in layer) > rf.MAX_POINTS:
        return None
    return evaluate(source_sets, layer, context, method, full=True)


def detailed_refit(reference_sets, source_sets, context, method):
    ladders = rf.build_ladders(reference_sets, seed_anchors=protected_anchors(reference_sets))
    if not ladders:
        return None
    _budget, anchors = ladders[-1]
    layer, _lines, _curves = rf.build_layer(reference_sets, anchors)
    return evaluate(source_sets, layer, context, method, full=True)


def registered_donor(layer, donor_bbox, target_bbox):
    source_width = max(1.0, donor_bbox[2] - donor_bbox[0])
    source_height = max(1.0, donor_bbox[3] - donor_bbox[1])
    target_width = max(1.0, target_bbox[2] - target_bbox[0])
    target_height = max(1.0, target_bbox[3] - target_bbox[1])
    sx, sy = target_width / source_width, target_height / source_height
    dx = target_bbox[0] - donor_bbox[0] * sx
    dy = target_bbox[1] - donor_bbox[1] * sy
    transformed = layer.dup()
    transformed.transform((sx, 0.0, 0.0, sy, dx, dy))
    return rf.classify_layer(transformed)


def current_status(row, decisions):
    decision = decisions.get(row["glyph"], {})
    if (decision.get("source_hash") == row["source_hash"]
            and decision.get("candidate_hash") == row["candidate_hash"]):
        return decision.get("status", "needs_rework")
    return "automatic_pass" if bool_text(row["automatic_pass"]) else "needs_rework"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--glyph", action="append", help="repair only selected queue glyph(s)")
    parser.add_argument("--force", action="store_true", help="replace an existing redraw-v2 checkpoint")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if not (REVIEWED / "manifest.json").is_file():
        raise SystemExit("Run tools/freeze_redraw_review.py first")
    if V2.exists() and not args.force:
        raise SystemExit("redraw-v2 checkpoint exists; use --force to rebuild")

    manifest_v1 = json.loads((REVIEWED / "manifest.json").read_text(encoding="utf-8"))
    locked_names = set(manifest_v1["locked_glyphs"])
    repair_names = set(manifest_v1["repair_queue"])
    selected = set(args.glyph or repair_names)
    unknown = selected - repair_names
    if unknown:
        raise SystemExit("Not in repair queue: " + ", ".join(sorted(unknown)))

    with (REVIEWED / "redraw-report.csv").open(encoding="utf-8-sig", newline="") as handle:
        old_rows = {row["glyph"]: row for row in csv.DictReader(handle)}
    decision_payload = json.loads((REVIEWED / "redraw-manual-decisions.json").read_text(encoding="utf-8"))
    decisions = decision_payload["decisions"]

    source = fontforge.open(str(rf.SOURCE_SFD))
    source_ttf = fontforge.open(str(rf.SOURCE_TTF))
    output = fontforge.open(str(REVIEWED / "redrawn.sfd"))
    reviewed = fontforge.open(str(REVIEWED / "redrawn.sfd"))
    source_names = {glyph.glyphname for glyph in source.glyphs()}
    output_names = {glyph.glyphname for glyph in output.glyphs()}
    rows = []
    changed_names = []
    repaired_structure = []
    methods = {}
    try:
        order = [glyph.glyphname for glyph in source_ttf.glyphs()]
        for index, name in enumerate(order, start=1):
            old = dict(old_rows[name])
            previous_status = current_status(old, decisions)
            if name in locked_names or name not in selected or not len(reviewed[name].foreground):
                row = old
                row.update({
                    "previous_review_status": previous_status,
                    "current_review_status": previous_status,
                    "changed_in_v2": "false", "repaired_structure": "false",
                    "repair_method": "locked-reviewed-v1" if name in locked_names else "unchanged-reviewed-v1",
                    "previous_failure_reasons": old.get("failure_reasons", ""),
                    "terminal_boundary_p95": "", "terminal_boundary_max": "",
                    "locked_in_v1": "true" if name in locked_names else "false",
                })
                rows.append(row)
                continue

            reference = source[name] if name in source_names else source_ttf[name]
            source_layer = reference.foreground.dup()
            source_sets = [
                sf.resample_polyline(contour, 4.0, closed=True)
                for contour in sf.layer_point_sets(source_layer)
            ]
            context = rf.metric_context(source_sets)
            baseline_layer = reviewed[name].foreground.dup()
            baseline = evaluate(source_sets, baseline_layer, context, "reviewed-v1")
            candidates = [baseline]

            # FontForge overlap cleanup is a candidate, never an automatic promotion.
            glyph = output[name]
            glyph.foreground = baseline_layer.dup()
            try:
                glyph.removeOverlap()
                glyph.correctDirection()
                cleaned_layer = glyph.foreground.dup()
                cleaned_points = sum(len(contour) for contour in cleaned_layer)
                if cleaned_points <= rf.MAX_POINTS:
                    candidates.append(evaluate(source_sets, cleaned_layer, context, "remove-overlap"))
                else:
                    cleaned_sets = [
                        sf.resample_polyline(contour, 4.0, closed=True)
                        for contour in sf.layer_point_sets(cleaned_layer)
                    ]
                    compact = compact_refit(cleaned_sets, source_sets, context, "remove-overlap-compact-refit")
                    if compact:
                        candidates.append(compact)
                    if name == "uni20A6":
                        for error in (0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0):
                            glyph.foreground = cleaned_layer.dup()
                            try:
                                glyph.simplify(error, ("mergelines", "choosehv", "smoothcurves"))
                                glyph.removeOverlap(); glyph.correctDirection()
                                simplified_layer = glyph.foreground.dup()
                                if sum(len(contour) for contour in simplified_layer) <= rf.MAX_POINTS:
                                    candidates.append(evaluate(
                                        source_sets, simplified_layer, context,
                                        "remove-overlap-fontforge-simplify:{}".format(error),
                                    ))
                            except (EnvironmentError, RuntimeError, TypeError):
                                pass
                        detailed = detailed_refit(
                            cleaned_sets, source_sets, context, "remove-overlap-detailed-refit"
                        )
                        if detailed:
                            candidates.append(detailed)
            except (EnvironmentError, RuntimeError, TypeError):
                pass
            finally:
                glyph.foreground = baseline_layer.dup()

            terminal_fit = compact_refit(source_sets, source_sets, context, "terminal-aware-source-fit")
            if terminal_fit:
                candidates.append(terminal_fit)
            if name in DETAILED_SOURCE_REFITS:
                detailed = detailed_refit(source_sets, source_sets, context, "terminal-aware-detailed-fit")
                if detailed:
                    candidates.append(detailed)
                    glyph.foreground = detailed["layer"].dup()
                    try:
                        glyph.removeOverlap(); glyph.correctDirection()
                        repaired_layer = glyph.foreground.dup()
                        if sum(len(contour) for contour in repaired_layer) <= rf.MAX_POINTS:
                            candidates.append(evaluate(
                                source_sets, repaired_layer, context,
                                "terminal-aware-detailed-fit+remove-overlap",
                            ))
                        else:
                            if name == "uni20A6":
                                for error in (1.0, 2.0, 4.0, 8.0, 12.0, 20.0):
                                    glyph.foreground = repaired_layer.dup()
                                    try:
                                        glyph.simplify(error, ("mergelines", "choosehv", "smoothcurves"))
                                        glyph.removeOverlap(); glyph.correctDirection()
                                        simplified = glyph.foreground.dup()
                                        if sum(len(contour) for contour in simplified) <= rf.MAX_POINTS:
                                            candidates.append(evaluate(
                                                source_sets, simplified, context,
                                                "terminal-aware-overlap-simplify:{}".format(error),
                                            ))
                                    except (EnvironmentError, RuntimeError, TypeError):
                                        pass
                            repaired_sets = [
                                sf.resample_polyline(contour, 4.0, closed=True)
                                for contour in sf.layer_point_sets(repaired_layer)
                            ]
                            recompressed = detailed_refit(
                                repaired_sets, source_sets, context,
                                "terminal-aware-detailed-fit+overlap-refit",
                            )
                            if recompressed:
                                candidates.append(recompressed)
                    except (EnvironmentError, RuntimeError, TypeError):
                        pass
                    finally:
                        glyph.foreground = baseline_layer.dup()

            donor_name = RELATED_DONORS.get(name)
            if donor_name in output_names and donor_name in locked_names:
                donor_layer = reviewed[donor_name].foreground.dup()
                donor_sets = sf.layer_point_sets(source[donor_name].foreground if donor_name in source_names else source_ttf[donor_name].foreground)
                if donor_sets:
                    donor_bbox = sf.bbox_of_sets(donor_sets)
                    target_bbox = sf.bbox_of_sets(source_sets)
                    donor = registered_donor(donor_layer, donor_bbox, target_bbox)
                    if sum(len(contour) for contour in donor) <= rf.MAX_POINTS:
                        candidates.append(evaluate(source_sets, donor, context, "family-donor:" + donor_name))

            if args.verbose:
                for candidate in candidates:
                    print("candidate {} {} points={} topology={} self={} handles={} raster={:.3f} iou128={:.4f} terminal={:.4f}".format(
                        name, candidate["method"], candidate["points"],
                        candidate["metrics"]["topology_match"], candidate["self_intersections"],
                        candidate["invalid_handles"], raster_score(candidate),
                        candidate["metrics"]["per_size"][128]["ink_iou"],
                        candidate["terminal_p95"],
                    ), flush=True)

            promotable = [candidate for candidate in candidates[1:] if promotion_is_material(candidate, baseline)]
            if promotable:
                chosen = min(promotable, key=repair_key)
                chosen_layer = chosen["layer"]
                changed = rf.layer_hash(chosen_layer) != rf.layer_hash(baseline_layer)
            else:
                chosen = baseline
                chosen["method"] = "reviewed-v1"
                chosen_layer = baseline_layer
                changed = False
            glyph.foreground = chosen_layer.dup()
            structural_before = bool(
                int(old.get("self_intersections") or 0)
                or int(old.get("invalid_handles") or 0)
                or old.get("topology_status") == "mismatch"
                or int(old.get("redrawn_points") or 0) > rf.MAX_POINTS
            )
            structural_after = (
                chosen["points"] > rf.MAX_POINTS or chosen["self_intersections"]
                or chosen["invalid_handles"] or not chosen["metrics"]["topology_match"]
            )
            repaired = changed and structural_before and not structural_after
            row = rf.result_row(reference, source_layer, chosen, len(candidates), {"decisions": {}})
            method = chosen["method"]
            row.update({
                "previous_review_status": previous_status,
                "current_review_status": "needs_rework" if changed else previous_status,
                "changed_in_v2": "true" if changed else "false",
                "repaired_structure": "true" if repaired else "false",
                "repair_method": method,
                "previous_failure_reasons": old.get("failure_reasons", ""),
                "terminal_boundary_p95": "{:.6f}".format(chosen["terminal_p95"]),
                "terminal_boundary_max": "{:.6f}".format(chosen["terminal_max"]),
                "locked_in_v1": "false",
            })
            rows.append(row)
            methods[name] = method
            if changed:
                changed_names.append(name)
            if repaired:
                repaired_structure.append(name)
            print("repair {}/{} {}: {} -> {}, {} points".format(
                index, len(order), name, previous_status, row["current_review_status"], chosen["points"]
            ), flush=True)

        rf.OUTPUT_SFD.parent.mkdir(parents=True, exist_ok=True)
        rf.OUTPUT_TTF.parent.mkdir(parents=True, exist_ok=True)
        output.save(str(rf.OUTPUT_SFD))
        output.generate(str(rf.OUTPUT_TTF))
    finally:
        for font in (source, source_ttf, output, reviewed):
            try:
                font.close()
            except RuntimeError:
                pass

    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "restore_redraw_metadata.py"),
         str(rf.SOURCE_TTF), str(rf.OUTPUT_TTF)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if completed.returncode:
        raise RuntimeError("metadata restore failed: " + completed.stdout[-2000:])

    # SFD serialization rounds coordinates; hashes and decisions must use saved outlines.
    saved = fontforge.open(str(rf.OUTPUT_SFD))
    try:
        by_name = {row["glyph"]: row for row in rows}
        for name in order:
            row = by_name[name]
            layer = saved[name].foreground
            saved_hash = rf.layer_hash(layer)
            row["candidate_hash"] = saved_hash
            row["redrawn_points"] = sum(len(contour) for contour in layer)
            counts = rf.point_type_counts(layer)
            row.update({
                "corner_points": counts["corner"], "curve_points": counts["curve"],
                "hvcurve_points": counts["hvcurve"], "tangent_points": counts["tangent"],
                "off_curve_points": counts["off"],
            })
            if name in changed_names:
                decisions[name] = {
                    "status": "needs_rework", "source_hash": row["source_hash"],
                    "candidate_hash": saved_hash, "updated_at": datetime.now(timezone.utc).isoformat(),
                    "previous_review_status": row["previous_review_status"],
                }
            elif name in decisions:
                row["current_review_status"] = decisions[name]["status"]
            rf.apply_manual_decision(row, {"decisions": decisions})
    finally:
        saved.close()

    decision_payload["version"] = "redraw-manual-review-v2"
    decision_payload["candidate_version"] = "redraw-v2"
    atomic_json(rf.DECISIONS, decision_payload)
    shutil.copy2(rf.OUTPUT_SFD, rf.REVIEW_SFD)
    shutil.copy2(rf.OUTPUT_TTF, rf.REVIEW_TTF)
    fields = rf.REPORT_FIELDS + EXTRA_FIELDS
    with rf.REPORT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)

    if V2.exists():
        shutil.rmtree(V2)
    V2.mkdir(parents=True)
    artifacts = {
        "redrawn.sfd": rf.OUTPUT_SFD, "redrawn.ttf": rf.OUTPUT_TTF,
        "redraw-review.sfd": rf.REVIEW_SFD, "redraw-review.ttf": rf.REVIEW_TTF,
        "redraw-report.csv": rf.REPORT, "redraw-manual-decisions.json": rf.DECISIONS,
    }
    for name, path in artifacts.items():
        shutil.copy2(path, V2 / name)
    v2_manifest = {
        "version": "redraw-v2", "created_at": datetime.now(timezone.utc).isoformat(),
        "base_version": "redraw-reviewed-v1", "glyph_count": len(rows),
        "locked_glyph_count": len(locked_names), "repair_queue_count": len(repair_names),
        "changed_glyphs": changed_names, "repaired_structure_glyphs": repaired_structure,
        "repair_methods": methods,
        "unresolved_glyphs": [row["glyph"] for row in rows if row["needs_manual_review"] == "true"],
        "artifact_hashes": {name: sha256(V2 / name) for name in artifacts},
    }
    atomic_json(V2 / "manifest.json", v2_manifest)
    print(json.dumps({
        "changed": len(changed_names), "repaired_structure": len(repaired_structure),
        "unresolved": len(v2_manifest["unresolved_glyphs"]),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
