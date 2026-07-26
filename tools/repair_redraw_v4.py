#!/usr/bin/env ffpython
"""High-budget repair of the redraw-reviewed-v3 queue."""

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
import repair_redraw_v2 as v2
import repair_redraw_v3 as v3

REVIEWED = ROOT / "checkpoints" / "redraw-reviewed-v3"
V4 = ROOT / "checkpoints" / "redraw-v4"
WORK = ROOT / "checkpoints" / "redraw-v4-work"
FRACTIONS = {"onethird", "twothirds", "oneeighth", "threeeighths", "fiveeighths", "seveneighths"}
FRACTION_DONORS = {
    "onethird": ("uni00B9", "slash", "uni2083"),
    "twothirds": ("uni00B2", "slash", "uni2083"),
    "oneeighth": ("uni00B9", "slash", "uni2088"),
    "threeeighths": ("uni00B3", "slash", "uni2088"),
    "fiveeighths": ("uni2075", "slash", "uni2088"),
    "seveneighths": ("uni2077", "slash", "uni2088"),
}
DIAGONAL_DONORS = {
    "uni2196": ("uni2199", -90), "uni2199": ("uni2196", 90),
    "uni2197": ("uni2198", 90), "uni2198": ("uni2197", -90),
}
CONTOUR_TARGETS = {"Euro": 1, "uni20AA": 2}
EXTRA_FIELDS = [
    "previous_review_status", "current_review_status", "changed_in_v4",
    "repaired_structure", "repair_method", "previous_failure_reasons",
    "budget_searched_v4", "selected_budget_v4", "point_ceiling",
    "direction_audit", "direction_result", "direction_repaired",
    "cleaned_reference_status", "reference_type_v4", "donor_glyphs",
    "donor_transform", "fraction_reconstruction", "high_budget_candidate",
    "locked_in_v3", "raw_ink_iou_128_v4", "raw_false_positive_ink_128_v4",
    "raw_false_negative_ink_128_v4", "raw_boundary_p95_v4", "raw_boundary_max_v4",
]


def atomic_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
    os.replace(tmp, path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def point_sets(layer):
    return [sf.resample_polyline(contour, 4.0, closed=True)
            for contour in sf.layer_point_sets(layer)]


def segment_counts(layer):
    return v2.segment_counts(layer)


def direction_audit(layer):
    sets = sf.layer_point_sets(layer)
    bbox = sf.bbox_of_sets(sets) if sets else (0, 0, 1, 1)
    area_limit = max(1.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) * 1e-5)
    degenerate = sum(abs(sf.signed_area(contour)) <= area_limit for contour in sets)
    scratch = fontforge.font(); scratch.encoding = "UnicodeFull"
    glyph = scratch.createChar(-1, "audit")
    glyph.foreground = layer.dup()
    try:
        before = rf.layer_hash(glyph.foreground)
        glyph.correctDirection(); first_layer = glyph.foreground.dup()
        first = rf.layer_hash(first_layer)
        glyph.correctDirection(); second = rf.layer_hash(glyph.foreground)
        return {
            "changed": before != first, "idempotent": first == second,
            "degenerate": degenerate, "normalized": rf.classify_layer(first_layer),
        }
    except (EnvironmentError, RuntimeError, TypeError):
        return {"changed": False, "idempotent": False, "degenerate": degenerate,
                "normalized": layer.dup()}
    finally:
        scratch.close()


def evaluate(reference_sets, raw_sets, layer, ceiling, method,
             reference_type="raw-original", backend="", donor="", transform="",
             normalize=True):
    audit_before = direction_audit(layer)
    candidate_layer = audit_before["normalized"].dup() if normalize else layer.dup()
    audit_after = direction_audit(candidate_layer)
    lines, curves = segment_counts(candidate_layer)
    context = rf.metric_context(reference_sets)
    candidate = rf.evaluate_layer(reference_sets, candidate_layer, lines, curves, context,
                                  full_audit=True, max_points=ceiling)
    candidate = v2.enrich_candidate(candidate, reference_sets, context, method)
    if reference_sets is raw_sets:
        raw = candidate
    else:
        raw_context = rf.metric_context(raw_sets)
        raw_layer = candidate_layer.dup()
        raw = rf.evaluate_layer(raw_sets, raw_layer, lines, curves, raw_context,
                                full_audit=True, max_points=ceiling)
        raw = v2.enrich_candidate(raw, raw_sets, raw_context, method + ":raw")
    candidate.update({
        "raw": raw, "method": method, "reference_type": reference_type,
        "cleanup_backend": backend, "donor": donor, "donor_transform": transform,
        "direction_changed": audit_before["changed"],
        "direction_valid": audit_after["idempotent"] and not audit_after["changed"]
                           and audit_after["degenerate"] == 0,
        "degenerate_contours": audit_after["degenerate"],
    })
    return candidate


def structural(candidate, ceiling, contour_target=None):
    return (candidate["points"] <= ceiling and not candidate["self_intersections"]
            and not candidate["invalid_handles"] and candidate["metrics"]["topology_match"]
            and candidate["direction_valid"]
            and (contour_target is None or len(candidate["layer"]) == contour_target))


def raster_score(candidate):
    return v2.raster_score(candidate["raw"])


def selection_key(candidate, ceiling, contour_target=None):
    raw = candidate["raw"]["metrics"]
    return (0 if structural(candidate, ceiling, contour_target) else 1,
            raster_score(candidate), raw["boundary_p95"], raw["boundary_max"],
            candidate["terminal_p95"], candidate["points"])


def materially_better(candidate, baseline, ceiling, contour_target=None):
    if not structural(candidate, ceiling, contour_target): return False
    if not structural(baseline, ceiling, contour_target): return True
    new, old = candidate["raw"]["metrics"], baseline["raw"]["metrics"]
    n128, o128 = new["per_size"][128], old["per_size"][128]
    coverage_ok = (n128["false_positive_ink"] <= o128["false_positive_ink"] + .02
                   and n128["false_negative_ink"] <= o128["false_negative_ink"] + .02)
    return coverage_ok and (raster_score(candidate) <= raster_score(baseline) * .98
                            or new["boundary_p95"] <= old["boundary_p95"] - .0015
                            or n128["ink_iou"] >= o128["ink_iou"] + .02)


def budget_targets(old_points, fraction=False):
    values = [80, 90, 100, 110, 120] if fraction else [80, 90, 100]
    result = [value for value in values if value > old_points]
    if not result: result = [120 if fraction else 100]
    return result, 120 if fraction else 100


def ladder_candidates(reference_sets, raw_sets, targets, ceiling, method,
                      reference_type="raw-original", backend="", protected=True):
    seeds = v2.protected_anchors(reference_sets) if protected else None
    ladders = rf.build_ladders(reference_sets, seed_anchors=seeds,
                               max_points=ceiling, targets=targets)
    chosen = {}
    for target in targets:
        eligible = [(points, anchors) for points, anchors in ladders if points <= target]
        if eligible:
            points, anchors = max(eligible, key=lambda item: item[0]); chosen[points] = anchors
    result = []
    for points, anchors in sorted(chosen.items()):
        layer = rf.build_layer(reference_sets, anchors)[0]
        result.append(evaluate(reference_sets, raw_sets, layer, ceiling,
                               "{}:{}".format(method, points), reference_type, backend))
    return result


def rotate_registered(layer, donor_sets, target_sets, degrees):
    transformed = layer.dup(); bbox = sf.bbox_of_sets(donor_sets)
    cx, cy = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
    angle = math.radians(degrees); cosine, sine = math.cos(angle), math.sin(angle)
    transformed.transform((1, 0, 0, 1, -cx, -cy))
    transformed.transform((cosine, sine, -sine, cosine, 0, 0))
    transformed.transform((1, 0, 0, 1, cx, cy))
    rotated_sets = point_sets(transformed)
    return v2.registered_donor(transformed, sf.bbox_of_sets(rotated_sets),
                               sf.bbox_of_sets(target_sets))


def component_bboxes(source_sets, size=512):
    bbox = sf.padded_bbox(sf.bbox_of_sets(source_sets)); mask = sf.rasterize(source_sets, bbox, size)
    filled = {(x, y) for y in range(size) for x in range(size) if mask[y * size + x] >= .5}
    components = []
    while filled:
        seed = filled.pop(); stack = [seed]; values = [seed]
        while stack:
            x, y = stack.pop()
            for neighbor in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
                if neighbor in filled:
                    filled.remove(neighbor); stack.append(neighbor); values.append(neighbor)
        components.append(values)
    components.sort(key=len, reverse=True); components = components[:3]
    left, bottom, right, top = bbox; width, height = right-left, top-bottom
    result = []
    for values in components:
        xs, ys = [v[0] for v in values], [v[1] for v in values]
        result.append((left + min(xs)/size*width, top-(max(ys)+1)/size*height,
                       left + (max(xs)+1)/size*width, top-min(ys)/size*height))
    return sorted(result, key=lambda value: (value[0]+value[2])/2.0)


def registered_to_bbox(layer, target_bbox):
    sets = point_sets(layer); return v2.registered_donor(
        layer, sf.bbox_of_sets(sets), target_bbox)


def map_sets_to_bbox(sets, target_bbox):
    source_bbox = sf.bbox_of_sets(sets)
    sx = (target_bbox[2] - target_bbox[0]) / max(1.0, source_bbox[2] - source_bbox[0])
    sy = (target_bbox[3] - target_bbox[1]) / max(1.0, source_bbox[3] - source_bbox[1])
    return [[(target_bbox[0] + (x - source_bbox[0]) * sx,
              target_bbox[1] + (y - source_bbox[1]) * sy) for x, y in contour]
            for contour in sets]


def combine_layers(layers):
    combined = fontforge.layer(); combined.is_quadratic = False
    for layer in layers:
        for contour in layer: combined += contour
    return rf.classify_layer(combined)


def status_for(row, decisions):
    decision = decisions.get(row["glyph"], {})
    if (decision.get("source_hash") == row["source_hash"]
            and decision.get("candidate_hash") == row["candidate_hash"]):
        return decision.get("status", "needs_rework")
    return "automatic_pass" if row.get("automatic_pass") == "true" else "needs_rework"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--pathops-python", default=sf.DEFAULT_EXTERNAL_PYTHON)
    args = parser.parse_args(argv)
    if not (REVIEWED / "manifest.json").is_file():
        raise SystemExit("Run tools/freeze_redraw_review_v3.py first")
    if V4.exists() and not args.force: raise SystemExit("redraw-v4 exists; use --force")
    frozen = json.loads((REVIEWED / "manifest.json").read_text(encoding="utf-8"))
    locked, repair = set(frozen["locked_glyphs"]), set(frozen["repair_queue"])
    with (REVIEWED / "redraw-report.csv").open(encoding="utf-8-sig", newline="") as handle:
        old_rows = {row["glyph"]: row for row in csv.DictReader(handle)}
    decisions_payload = json.loads((REVIEWED / "redraw-manual-decisions.json").read_text(encoding="utf-8"))
    decisions = decisions_payload["decisions"]
    source = fontforge.open(str(rf.SOURCE_SFD)); source_ttf = fontforge.open(str(rf.SOURCE_TTF))
    output = fontforge.open(str(REVIEWED / "redrawn.sfd"))
    source_names = {glyph.glyphname for glyph in source.glyphs()}
    order = [glyph.glyphname for glyph in source_ttf.glyphs()]
    WORK.mkdir(parents=True, exist_ok=True)
    prepared = {}

    def prepare(name):
        if name in prepared: return prepared[name]
        old = old_rows[name]; reference = source[name] if name in source_names else source_ttf[name]
        source_layer = reference.foreground.dup(); raw_sets = point_sets(source_layer)
        targets, ceiling = budget_targets(int(old.get("redrawn_points") or 0), name in FRACTIONS)
        baseline = evaluate(raw_sets, raw_sets, output[name].foreground.dup(), ceiling,
                            "reviewed-v3", normalize=False)
        candidates = [baseline]
        candidates += ladder_candidates(raw_sets, raw_sets, targets, ceiling, "source-fit")
        baseline_bad = not baseline["direction_valid"] or baseline["self_intersections"] \
            or old.get("topology_status") == "mismatch"
        should_clean = (baseline_bad or int(old.get("redrawn_points") or 0) >= 100
                        or name in FRACTIONS or name.startswith("uni20") or name.startswith("uni21")
                        or name in {"beta", "uni041D", "arrowdblboth", "Euro"})
        if should_clean:
            for variant in v3.cleaned_references(reference, source_layer, WORK/name,
                                                  args.pathops_python):
                cleaned_sets = point_sets(variant["layer"])
                if cleaned_sets:
                    candidates += ladder_candidates(
                        cleaned_sets, raw_sets, targets, ceiling, "cleaned-source-fit",
                        "cleaned-original", variant.get("backend", "unknown"))
        contour_target = CONTOUR_TARGETS.get(name)
        if contour_target:
            visible = v3.raster_visible_boundary(raw_sets, contour_target)
            if len(visible) == contour_target:
                candidates += ladder_candidates(visible, raw_sets, targets, ceiling,
                                                 "visible-boundary-fit", "raster-visible-boundary",
                                                 "nonzero-raster", protected=False)
        elif baseline_bad:
            visible_count = (int(old.get("source_components_1024") or 0)
                             + int(old.get("source_counters_1024") or 0))
            if visible_count:
                visible = v3.raster_visible_boundary(raw_sets, visible_count)
                if len(visible) == visible_count:
                    candidates += ladder_candidates(
                        visible, raw_sets, targets, ceiling, "visible-boundary-fit",
                        "raster-visible-boundary", "nonzero-raster", protected=False)
        prepared[name] = (reference, source_layer, raw_sets, targets, ceiling,
                          contour_target, baseline, candidates)
        return prepared[name]

    # Build non-recursive direct masters for both diagonal-arrow pairs.
    diagonal_masters = {}
    for name in DIAGONAL_DONORS:
        _r, _sl, _sets, _t, ceiling, target, baseline, candidates = prepare(name)
        direct = [candidate for candidate in candidates[1:]
                  if structural(candidate, ceiling, target)]
        diagonal_masters[name] = min(direct, key=lambda c: selection_key(c, ceiling, target)) \
            if direct else baseline

    rows, changed_names, repaired_names, methods = [], [], [], {}
    try:
        for index, name in enumerate(order, 1):
            old = dict(old_rows[name]); previous = status_for(old, decisions)
            if name in locked or name not in repair or not len(output[name].foreground):
                old.update({
                    "previous_review_status": previous, "current_review_status": previous,
                    "changed_in_v4": "false", "repaired_structure": "false",
                    "repair_method": "locked-reviewed-v3" if name in locked else "unchanged-reviewed-v3",
                    "previous_failure_reasons": old.get("failure_reasons", ""),
                    "budget_searched_v4": "", "selected_budget_v4": old.get("redrawn_points", "0"),
                    "point_ceiling": 120 if name in FRACTIONS else 100,
                    "direction_audit": "warning-recorded" if direction_audit(output[name].foreground)["changed"] else "ok",
                    "direction_result": "locked", "direction_repaired": "false",
                    "cleaned_reference_status": "not-run", "reference_type_v4": "raw-original",
                    "donor_glyphs": "", "donor_transform": "", "fraction_reconstruction": "none",
                    "high_budget_candidate": "false", "locked_in_v3": "true" if name in locked else "false",
                })
                rows.append(old); continue

            reference, source_layer, raw_sets, targets, ceiling, contour_target, baseline, candidates = prepare(name)
            before_bad = not structural(baseline, ceiling, contour_target)

            if name in DIAGONAL_DONORS:
                donor_name, degrees = DIAGONAL_DONORS[name]
                master = diagonal_masters[donor_name]
                donor_layer = rotate_registered(master["layer"], master["metrics"]["candidate_sets"],
                                                raw_sets, degrees)
                visible_target = v3.raster_visible_boundary(raw_sets, 1)
                donor_reference = visible_target if len(visible_target) == 1 else raw_sets
                donor = evaluate(donor_reference, raw_sets, donor_layer, ceiling,
                                 "paired-diagonal-donor", donor=donor_name,
                                 transform="rotate({})+register".format(degrees),
                                 reference_type=("raster-visible-boundary"
                                                 if donor_reference is not raw_sets else "raw-original"))
                candidates.append(donor)
                if structural(donor, ceiling, contour_target): chosen = donor
                else:
                    valid = [c for c in candidates[1:] if structural(c, ceiling, contour_target)]
                    chosen = min(valid, key=lambda c: selection_key(c, ceiling, contour_target)) if valid else baseline
            else:
                if name in FRACTIONS:
                    boxes = component_bboxes(raw_sets)
                    donor_names = list(FRACTION_DONORS[name])
                    if name == "seveneighths":
                        seven_audit = direction_audit(output["uni2077"].foreground)
                        if seven_audit["degenerate"] or not seven_audit["idempotent"]:
                            donor_names[0] = "seven"
                    if len(boxes) == 3:
                        combined_sets = []
                        for donor_name, bbox in zip(donor_names, boxes):
                            donor_row = old_rows[donor_name]
                            loop_count = (int(donor_row.get("candidate_components_128") or 0)
                                          + int(donor_row.get("candidate_counters_128") or 0))
                            donor_visible = v3.raster_visible_boundary(
                                point_sets(output[donor_name].foreground), max(1, loop_count))
                            combined_sets.extend(map_sets_to_bbox(donor_visible, bbox))
                        donor_candidates = ladder_candidates(
                            combined_sets, raw_sets, targets, ceiling, "fraction-donor-fit",
                            "registered-small-form-donors", protected=False)
                        for candidate in donor_candidates:
                            candidate["donor"] = ",".join(donor_names)
                            candidate["donor_transform"] = "component-envelope-registration"
                        candidates.extend(donor_candidates)
                    valid = [c for c in candidates[1:]
                             if materially_better(c, baseline, ceiling, contour_target)]
                    if valid:
                        best = min(valid, key=lambda c: selection_key(c, ceiling, contour_target))
                        donor_valid = [c for c in valid
                                       if c["method"].startswith("fraction-donor-fit")
                                       and raster_score(c) <= raster_score(best) * 1.02
                                       and c["raw"]["metrics"]["boundary_p95"]
                                       <= best["raw"]["metrics"]["boundary_p95"] + .002]
                        chosen = (min(donor_valid, key=lambda c: selection_key(c, ceiling, contour_target))
                                  if donor_valid else best)
                    else:
                        chosen = baseline
                else:
                    valid = [c for c in candidates[1:]
                             if materially_better(c, baseline, ceiling, contour_target)]
                    chosen = min(valid, key=lambda c: selection_key(c, ceiling, contour_target)) if valid else baseline

            if chosen is baseline:
                selected_layer = output[name].foreground.dup(); changed = False
                chosen["method"] = "reviewed-v3"
            else:
                selected_layer = chosen["layer"].dup()
                changed = rf.layer_hash(selected_layer) != rf.layer_hash(output[name].foreground)
                if changed: output[name].foreground = selected_layer
            repaired = changed and before_bad and structural(chosen, ceiling, contour_target)
            row = rf.result_row(reference, source_layer, chosen, len(candidates), {"decisions": {}})
            raw = chosen["raw"]["metrics"]; audit_before = direction_audit(baseline["layer"])
            row.update({
                "previous_review_status": previous,
                "current_review_status": "needs_rework" if changed else previous,
                "changed_in_v4": "true" if changed else "false",
                "repaired_structure": "true" if repaired else "false",
                "repair_method": chosen["method"], "previous_failure_reasons": old.get("failure_reasons", ""),
                "budget_searched_v4": ",".join(map(str, targets)), "selected_budget_v4": chosen["points"],
                "point_ceiling": ceiling,
                "direction_audit": "changed" if audit_before["changed"] else "ok",
                "direction_result": "normalized" if chosen["direction_valid"] else "fallback-warning",
                "direction_repaired": "true" if changed and audit_before["changed"] and chosen["direction_valid"] else "false",
                "cleaned_reference_status": chosen["cleanup_backend"] or "not-selected",
                "reference_type_v4": chosen["reference_type"], "donor_glyphs": chosen["donor"],
                "donor_transform": chosen["donor_transform"],
                "fraction_reconstruction": "small-form-donor" if chosen["method"].startswith("fraction-donor") else "source-fit",
                "high_budget_candidate": "true" if chosen["points"] >= 80 else "false",
                "locked_in_v3": "false",
                "raw_ink_iou_128_v4": "{:.6f}".format(raw["per_size"][128]["ink_iou"]),
                "raw_false_positive_ink_128_v4": "{:.6f}".format(raw["per_size"][128]["false_positive_ink"]),
                "raw_false_negative_ink_128_v4": "{:.6f}".format(raw["per_size"][128]["false_negative_ink"]),
                "raw_boundary_p95_v4": "{:.6f}".format(raw["boundary_p95"]),
                "raw_boundary_max_v4": "{:.6f}".format(raw["boundary_max"]),
            })
            rows.append(row); methods[name] = chosen["method"]
            if changed: changed_names.append(name)
            if repaired: repaired_names.append(name)
            print("v4 {}/{} {}: {} {} points{}".format(index, len(order), name,
                  chosen["method"], chosen["points"], " changed" if changed else " fallback"), flush=True)
            if args.verbose:
                for candidate in candidates:
                    print("  {} p={} valid={} iou={:.4f} self={} dir={}".format(
                        candidate["method"], candidate["points"], structural(candidate, ceiling, contour_target),
                        candidate["raw"]["metrics"]["per_size"][128]["ink_iou"],
                        candidate["self_intersections"], candidate["direction_valid"]), flush=True)
        output.save(str(rf.OUTPUT_SFD)); output.generate(str(rf.OUTPUT_TTF))
    finally:
        source.close(); source_ttf.close(); output.close()

    completed = subprocess.run([sys.executable, str(ROOT/"tools"/"restore_redraw_metadata.py"),
                                str(rf.SOURCE_TTF), str(rf.OUTPUT_TTF)], cwd=str(ROOT),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if completed.returncode: raise RuntimeError("metadata restore failed: " + completed.stdout[-2000:])
    saved = fontforge.open(str(rf.OUTPUT_SFD)); by_name = {row["glyph"]: row for row in rows}
    try:
        now = datetime.now(timezone.utc).isoformat()
        for name in order:
            row, layer = by_name[name], saved[name].foreground; digest = rf.layer_hash(layer)
            old_hash = frozen.get("repair_queue", {}).get(name, {}).get("candidate_hash")
            actually_changed = bool(old_hash and digest != old_hash)
            if actually_changed and name not in changed_names: changed_names.append(name)
            if actually_changed:
                row["changed_in_v4"] = "true"; row["current_review_status"] = "needs_rework"
                decisions[name] = {"status": "needs_rework", "source_hash": row["source_hash"],
                                   "candidate_hash": digest, "updated_at": now,
                                   "previous_review_status": row["previous_review_status"]}
            row["candidate_hash"] = digest; row["redrawn_points"] = sum(len(c) for c in layer)
            counts = rf.point_type_counts(layer)
            row.update({"corner_points": counts["corner"], "curve_points": counts["curve"],
                        "hvcurve_points": counts["hvcurve"], "tangent_points": counts["tangent"],
                        "off_curve_points": counts["off"]})
            rf.apply_manual_decision(row, {"decisions": decisions})
            if not actually_changed: row["current_review_status"] = status_for(row, decisions)
    finally:
        saved.close()
    decisions_payload.update({"version": "redraw-manual-review-v4", "candidate_version": "redraw-v4"})
    atomic_json(rf.DECISIONS, decisions_payload)
    shutil.copy2(rf.OUTPUT_SFD, rf.REVIEW_SFD); shutil.copy2(rf.OUTPUT_TTF, rf.REVIEW_TTF)
    fields = list(dict.fromkeys(rf.REPORT_FIELDS + EXTRA_FIELDS))
    with rf.REPORT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)
    if V4.exists(): shutil.rmtree(V4)
    V4.mkdir(parents=True)
    artifacts = {"redrawn.sfd": rf.OUTPUT_SFD, "redrawn.ttf": rf.OUTPUT_TTF,
                 "redraw-review.sfd": rf.REVIEW_SFD, "redraw-review.ttf": rf.REVIEW_TTF,
                 "redraw-report.csv": rf.REPORT, "redraw-manual-decisions.json": rf.DECISIONS}
    for filename, path in artifacts.items(): shutil.copy2(path, V4/filename)
    manifest = {"version": "redraw-v4", "base_version": "redraw-reviewed-v3",
                "created_at": datetime.now(timezone.utc).isoformat(), "glyph_count": len(rows),
                "locked_glyph_count": len(locked), "repair_queue_count": len(repair),
                "changed_glyphs": changed_names, "repaired_structure_glyphs": repaired_names,
                "repair_methods": methods,
                "unresolved_glyphs": [r["glyph"] for r in rows if r["needs_manual_review"] == "true"],
                "artifact_hashes": {n: sha256(V4/n) for n in artifacts}}
    atomic_json(V4/"manifest.json", manifest)
    print(json.dumps({"changed": len(changed_names), "repaired_structure": len(repaired_names),
                      "unresolved": len(manifest["unresolved_glyphs"])}, sort_keys=True))
    return 0


if __name__ == "__main__": raise SystemExit(main())
