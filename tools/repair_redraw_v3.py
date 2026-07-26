#!/usr/bin/env ffpython
"""Freeze-safe, budgeted repair of the redraw-reviewed-v2 queue."""

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


REVIEWED = ROOT / "checkpoints" / "redraw-reviewed-v2"
V3 = ROOT / "checkpoints" / "redraw-v3"
WORK = ROOT / "checkpoints" / "redraw-v3-work"
EXTRA_FIELDS = [
    "previous_review_status", "current_review_status", "changed_in_v3",
    "repaired_structure", "repair_method", "previous_failure_reasons",
    "budget_searched", "selected_budget_v3", "reference_type",
    "cleanup_backend", "cleanup_status", "contour_target", "donor",
    "locked_in_v2", "raw_boundary_p95", "raw_boundary_max", "raw_topology_status",
    "raw_ink_iou_128", "raw_false_positive_ink_128", "raw_false_negative_ink_128",
    "envelope_delta", "stroke_weight_delta",
]

QUOTE_TARGETS = {
    "quoteleft": 1, "quoteright": 1, "quotesinglbase": 1,
    "quotereversed": 1, "quotedblbase": 2,
}
ARROW_DONORS = {
    "arrowdblright": ("arrowdblleft", "mirror-x"),
    "arrowright": ("arrowleft", "mirror-x"),
    "arrowup": ("arrowdown", "mirror-y"),
    "arrowdblup": ("arrowdbldown", "mirror-y"),
}
CLEAN_FAMILIES = (
    "quote", "uni20", "fraction", "one", "two", "three", "four", "five",
    "six", "seven", "eight", "nine", "uni207", "uni208", "arrow", "math",
)
HIGH_RES_TARGETS = {"beta", "gamma", "psi", "uni0439"}
DIRECT_SOURCE_PRIORITY = {"uni2078", "uni2088", *HIGH_RES_TARGETS}


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


def status_for(row: dict, decisions: dict) -> str:
    decision = decisions.get(row["glyph"], {})
    if (decision.get("source_hash") == row["source_hash"]
            and decision.get("candidate_hash") == row["candidate_hash"]):
        return decision.get("status", "needs_rework")
    if row.get("current_review_status"):
        return row["current_review_status"]
    return "automatic_pass" if row.get("automatic_pass", "").lower() == "true" else "needs_rework"


def point_sets(layer):
    return [sf.resample_polyline(contour, 4.0, closed=True)
            for contour in sf.layer_point_sets(layer)]


def budget_targets(old_points: int) -> tuple[list[int], int, bool]:
    if old_points < 50:
        return list(range(10, 81, 10)), 80, False
    if old_points < 90:
        return list(range(50, 101, 10)), 100, False
    return [90, 95, 100], 100, True


def ladder_layers(reference_sets, targets, ceiling, protected=True):
    seeds = v2.protected_anchors(reference_sets) if protected else None
    ladders = rf.build_ladders(reference_sets, seed_anchors=seeds)
    selected = {}
    for target in targets:
        attainable = [(points, anchors) for points, anchors in ladders
                      if points <= target and points <= ceiling]
        if attainable:
            points, anchors = max(attainable, key=lambda item: item[0])
            selected[points] = anchors
    return [(points, rf.build_layer(reference_sets, anchors)[0])
            for points, anchors in sorted(selected.items())]


def bbox_delta(reference_sets, candidate_sets):
    rb, cb = sf.bbox_of_sets(reference_sets), sf.bbox_of_sets(candidate_sets)
    rw, rh = max(1.0, rb[2] - rb[0]), max(1.0, rb[3] - rb[1])
    return max(abs((cb[2] - cb[0]) / rw - 1.0), abs((cb[3] - cb[1]) / rh - 1.0))


def ink_delta(reference_sets, candidate_sets, context):
    bbox = context["bbox"]
    left = sum(sf.rasterize(reference_sets, bbox, 128))
    right = sum(sf.rasterize(candidate_sets, bbox, 128))
    return abs(right / max(1.0, left) - 1.0)


def evaluate(reference_sets, raw_sets, layer, method, reference_type="raw-original",
             cleanup_backend="", cleanup_status="", donor="", full=True):
    reference_context = rf.metric_context(reference_sets)
    candidate = v2.evaluate(reference_sets, layer, reference_context, method, full=full)
    raw_context = reference_context if reference_sets is raw_sets else rf.metric_context(raw_sets)
    raw = candidate if reference_sets is raw_sets else v2.evaluate(
        raw_sets, layer, raw_context, method + ":raw", full=full)
    candidate.update({
        "reference_type": reference_type,
        "cleanup_backend": cleanup_backend,
        "cleanup_status": cleanup_status,
        "donor": donor,
        "raw": raw,
        "envelope_delta": bbox_delta(raw_sets, candidate["metrics"]["candidate_sets"]),
        "stroke_weight_delta": ink_delta(raw_sets, candidate["metrics"]["candidate_sets"], raw_context),
    })
    return candidate


def structural(candidate, contour_target=None):
    return (
        candidate["points"] <= rf.MAX_POINTS
        and not candidate["self_intersections"]
        and not candidate["invalid_handles"]
        and candidate["metrics"]["topology_match"]
        and (contour_target is None or len(candidate["layer"]) == contour_target)
    )


def worst_raster(candidate, raw=False):
    value = candidate["raw"] if raw else candidate
    return v2.raster_score(value)


def selection_key(candidate, contour_target=None):
    invalid = not structural(candidate, contour_target)
    raw = candidate["raw"]
    return (
        1 if invalid else 0,
        candidate["stroke_weight_delta"], candidate["envelope_delta"],
        candidate.get("terminal_p95", 1.0), worst_raster(candidate),
        raw["metrics"]["boundary_p95"], raw["metrics"]["boundary_max"],
        candidate["points"],
    )


def materially_better(candidate, baseline, contour_target=None):
    if not structural(candidate, contour_target):
        return False
    if not structural(baseline, contour_target):
        return True
    old, new = worst_raster(baseline, raw=True), worst_raster(candidate, raw=True)
    raw, base_raw = candidate["raw"]["metrics"], baseline["raw"]["metrics"]
    no_coverage_regression = (
        raw["per_size"][128]["false_negative_ink"]
        <= base_raw["per_size"][128]["false_negative_ink"] + .015
        and raw["boundary_p95"] <= base_raw["boundary_p95"] + .003
    )
    return no_coverage_regression and (
        new <= old * .985
        or raw["boundary_p95"] <= base_raw["boundary_p95"] - .0015
        or candidate["stroke_weight_delta"] <= baseline["stroke_weight_delta"] - .025
    )


def transform_donor(layer, donor_sets, target_sets, operation):
    transformed = layer.dup()
    bbox = sf.bbox_of_sets(donor_sets)
    if operation == "mirror-x":
        transformed.transform((-1.0, 0.0, 0.0, 1.0, bbox[0] + bbox[2], 0.0))
    elif operation == "mirror-y":
        transformed.transform((1.0, 0.0, 0.0, -1.0, 0.0, bbox[1] + bbox[3]))
    mirrored_sets = point_sets(transformed)
    return v2.registered_donor(
        transformed, sf.bbox_of_sets(mirrored_sets), sf.bbox_of_sets(target_sets))


def cleaned_references(source_glyph, source_layer, glyph_work, pathops_python):
    variants = []
    try:
        variants.extend(sf.fontforge_visual_cleanup_variants(
            source_glyph, source_layer.dup(), glyph_work / "full", pathops_python))
    except Exception:
        pass
    if not any(item.get("backend", "").startswith("fontforge-pathops") for item in variants):
        try:
            layer, details = sf.selective_pathops_cleaned_layer(
                source_glyph, source_layer.dup(), glyph_work / "selective", pathops_python)
            variants.append({"layer": layer, "backend": details["backend"],
                             "stable": True, "fallback_safe": True,
                             "passes": details.get("passes", 0)})
        except Exception:
            pass
    unique, result = set(), []
    for item in variants:
        digest = rf.layer_hash(item["layer"])
        if digest in unique or not item.get("stable"):
            continue
        unique.add(digest); result.append(item)
    return result


def isolated_fontforge_union(source_glyph, layer):
    """Boolean-union a candidate in a disposable font, never in original.sfd."""
    scratch = fontforge.font()
    scratch.encoding = "UnicodeFull"
    glyph = scratch.createChar(source_glyph.unicode, source_glyph.glyphname)
    glyph.width = source_glyph.width
    glyph.foreground = layer.dup()
    try:
        glyph.removeOverlap(); glyph.correctDirection()
        glyph.removeOverlap(); glyph.correctDirection()
        return rf.classify_layer(glyph.foreground.dup())
    except (EnvironmentError, RuntimeError, TypeError):
        return None
    finally:
        scratch.close()


def raster_visible_boundary(point_sets_value, contour_count, size=512):
    """Trace the non-zero-winding visible union and retain its largest loops."""
    bbox = sf.padded_bbox(sf.bbox_of_sets(point_sets_value))
    mask = sf.rasterize(point_sets_value, bbox, size)
    filled = {(x, y) for y in range(size) for x in range(size)
              if mask[y * size + x] >= .5}
    edges = set()
    for x, y in filled:
        if (x, y - 1) not in filled: edges.add(((x, y), (x + 1, y)))
        if (x + 1, y) not in filled: edges.add(((x + 1, y), (x + 1, y + 1)))
        if (x, y + 1) not in filled: edges.add(((x + 1, y + 1), (x, y + 1)))
        if (x - 1, y) not in filled: edges.add(((x, y + 1), (x, y)))
    outgoing = {}
    for start, end in edges:
        outgoing.setdefault(start, []).append(end)
    loops = []
    remaining = set(edges)
    while remaining:
        start, current = next(iter(remaining))
        remaining.remove((start, current))
        loop = [start, current]
        while current != start and len(loop) <= len(edges) + 1:
            choices = [end for end in outgoing.get(current, []) if (current, end) in remaining]
            if not choices: break
            end = choices[0]
            remaining.remove((current, end)); current = end; loop.append(current)
        if current == start and len(loop) >= 5:
            loop = loop[:-1]
            compact = []
            for value in loop:
                if len(compact) >= 2:
                    a, b = compact[-2], compact[-1]
                    if (b[0] - a[0]) * (value[1] - b[1]) == (b[1] - a[1]) * (value[0] - b[0]):
                        compact[-1] = value; continue
                compact.append(value)
            loops.append(compact)
    def area(values):
        return abs(sum(values[i][0] * values[(i + 1) % len(values)][1]
                       - values[(i + 1) % len(values)][0] * values[i][1]
                       for i in range(len(values))))
    loops = sorted(loops, key=area, reverse=True)[:contour_count]
    left, bottom, right, top = bbox
    width, height = right - left, top - bottom
    return [[(left + x / size * width, top - y / size * height) for x, y in loop]
            for loop in loops]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--glyph", action="append")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--pathops-python", default=sf.DEFAULT_EXTERNAL_PYTHON)
    args = parser.parse_args(argv)
    if not (REVIEWED / "manifest.json").is_file():
        raise SystemExit("Run tools/freeze_redraw_review_v2.py first")
    if V3.exists() and not args.force:
        raise SystemExit("redraw-v3 checkpoint exists; use --force")

    frozen = json.loads((REVIEWED / "manifest.json").read_text(encoding="utf-8"))
    locked_names, repair_names = set(frozen["locked_glyphs"]), set(frozen["repair_queue"])
    selected = set(args.glyph or repair_names)
    if selected - repair_names:
        raise SystemExit("Not in repair queue: " + ", ".join(sorted(selected - repair_names)))
    with (REVIEWED / "redraw-report.csv").open(encoding="utf-8-sig", newline="") as handle:
        old_rows = {row["glyph"]: row for row in csv.DictReader(handle)}
    decision_payload = json.loads((REVIEWED / "redraw-manual-decisions.json").read_text(encoding="utf-8"))
    decisions = decision_payload["decisions"]

    source = fontforge.open(str(rf.SOURCE_SFD))
    source_ttf = fontforge.open(str(rf.SOURCE_TTF))
    reviewed = fontforge.open(str(REVIEWED / "redrawn.sfd"))
    output = fontforge.open(str(REVIEWED / "redrawn.sfd"))
    order = [glyph.glyphname for glyph in source_ttf.glyphs()]
    source_names = {glyph.glyphname for glyph in source.glyphs()}
    rows, changed_names, repaired_names, methods = [], [], [], {}
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        for index, name in enumerate(order, 1):
            old = dict(old_rows[name])
            previous = status_for(old, decisions)
            if name in locked_names or name not in selected or not len(reviewed[name].foreground):
                old.update({
                    "previous_review_status": previous, "current_review_status": previous,
                    "changed_in_v3": "false", "repaired_structure": "false",
                    "repair_method": "locked-reviewed-v2" if name in locked_names else "unchanged-reviewed-v2",
                    "budget_searched": "", "selected_budget_v3": old.get("redrawn_points", "0"),
                    "reference_type": "raw-original", "cleanup_backend": "",
                    "cleanup_status": "not-run", "contour_target": QUOTE_TARGETS.get(name, ""),
                    "donor": "", "locked_in_v2": "true" if name in locked_names else "false",
                    "previous_failure_reasons": old.get("failure_reasons", ""),
                    "raw_boundary_p95": old.get("boundary_p95", ""),
                    "raw_boundary_max": old.get("boundary_max", ""),
                    "raw_topology_status": old.get("topology_status", ""),
                    "raw_ink_iou_128": old.get("ink_iou_128", ""),
                    "raw_false_positive_ink_128": old.get("false_positive_ink_128", ""),
                    "raw_false_negative_ink_128": old.get("false_negative_ink_128", ""),
                    "envelope_delta": "", "stroke_weight_delta": "",
                })
                rows.append(old); continue

            reference_glyph = source[name] if name in source_names else source_ttf[name]
            source_layer = reference_glyph.foreground.dup()
            raw_sets = point_sets(source_layer)
            baseline_layer = reviewed[name].foreground.dup()
            baseline = evaluate(raw_sets, raw_sets, baseline_layer, "reviewed-v2")
            candidates = [baseline]
            targets, ceiling, cleaned_first = budget_targets(int(old.get("redrawn_points") or 0))
            contour_target = QUOTE_TARGETS.get(name)

            if not cleaned_first or name in DIRECT_SOURCE_PRIORITY:
                for budget, layer in ladder_layers(raw_sets, targets, ceiling):
                    candidates.append(evaluate(raw_sets, raw_sets, layer,
                                               "source-fit:{}".format(budget)))

            should_clean = cleaned_first or name in QUOTE_TARGETS or any(
                name.startswith(prefix) for prefix in CLEAN_FAMILIES)
            if should_clean:
                for variant in cleaned_references(
                        reference_glyph, source_layer, WORK / name, args.pathops_python):
                    cleaned_sets = point_sets(variant["layer"])
                    if not cleaned_sets:
                        continue
                    for budget, layer in ladder_layers(cleaned_sets, targets, ceiling):
                        candidates.append(evaluate(
                            cleaned_sets, raw_sets, layer, "cleaned-source-fit:{}".format(budget),
                            "cleaned-original", variant.get("backend", "unknown"),
                            "stable" if variant.get("stable") else "unstable"))
                        if contour_target is not None:
                            union = isolated_fontforge_union(reference_glyph, layer)
                            if (union is not None
                                    and sum(len(contour) for contour in union) <= rf.MAX_POINTS):
                                candidates.append(evaluate(
                                    cleaned_sets, raw_sets, union,
                                    "cleaned-source-fit:{}+union".format(budget),
                                    "cleaned-original", variant.get("backend", "unknown"),
                                    "stable" if variant.get("stable") else "unstable"))

            if contour_target is not None:
                visible_sets = raster_visible_boundary(raw_sets, contour_target)
                if len(visible_sets) == contour_target:
                    for budget, layer in ladder_layers(visible_sets, targets, ceiling, protected=False):
                        candidates.append(evaluate(
                            visible_sets, raw_sets, layer,
                            "raster-visible-boundary-fit:{}".format(budget),
                            "raster-visible-boundary", "nonzero-raster", "stable"))

            donor_spec = ARROW_DONORS.get(name)
            if donor_spec and donor_spec[0] in reviewed:
                donor_name, operation = donor_spec
                donor_source = source[donor_name] if donor_name in source_names else source_ttf[donor_name]
                donor = transform_donor(
                    reviewed[donor_name].foreground, point_sets(donor_source.foreground),
                    raw_sets, operation)
                if sum(len(contour) for contour in donor) <= rf.MAX_POINTS:
                    candidates.append(evaluate(raw_sets, raw_sets, donor,
                                               "donor-{}:{}".format(operation, donor_name),
                                               donor=donor_name))

            if name in {"uni2078", "uni2088"}:
                # The v2 donor was visibly underweight. Prefer the strongest valid
                # direct source fit that improves IoU and stays within the envelope.
                eligible = [c for c in candidates
                            if (c["method"].startswith("source-fit:")
                                or c["method"].startswith("cleaned-source-fit:"))
                            and structural(c, contour_target)
                            and c["envelope_delta"] <= .05
                            and c["raw"]["metrics"]["per_size"][128]["false_negative_ink"] < .20
                            and c["raw"]["metrics"]["per_size"][128]["ink_iou"]
                            > baseline["raw"]["metrics"]["per_size"][128]["ink_iou"]]
                chosen = min(eligible, key=lambda c: (
                    -c["raw"]["metrics"]["per_size"][128]["ink_iou"],
                    c["stroke_weight_delta"], c["points"])) if eligible else baseline
            elif name == "arrowdblright":
                eligible = [c for c in candidates if c.get("donor") == "arrowdblleft"
                            and structural(c, contour_target)]
                chosen = min(eligible, key=lambda c: selection_key(c, contour_target)) if eligible else baseline
            else:
                eligible = [c for c in candidates[1:] if materially_better(c, baseline, contour_target)]
                chosen = min(eligible, key=lambda c: selection_key(c, contour_target)) if eligible else baseline

            if chosen is baseline:
                chosen["method"] = "reviewed-v2"
                selected_layer = baseline_layer
                changed = False
            else:
                selected_layer = chosen["layer"]
                changed = rf.layer_hash(selected_layer) != rf.layer_hash(baseline_layer)
            output[name].foreground = selected_layer.dup()
            before_bad = (int(old.get("self_intersections") or 0) > 0
                          or int(old.get("invalid_handles") or 0) > 0
                          or old.get("topology_status") == "mismatch"
                          or (contour_target is not None and int(old.get("redrawn_contours") or 0) != contour_target))
            repaired = changed and before_bad and structural(chosen, contour_target)
            row = rf.result_row(reference_glyph, source_layer, chosen, len(candidates), {"decisions": {}})
            raw_metrics = chosen["raw"]["metrics"]
            row.update({
                "previous_review_status": previous,
                "current_review_status": "needs_rework" if changed else previous,
                "changed_in_v3": "true" if changed else "false",
                "repaired_structure": "true" if repaired else "false",
                "repair_method": chosen["method"],
                "previous_failure_reasons": old.get("failure_reasons", ""),
                "budget_searched": ",".join(map(str, targets)),
                "selected_budget_v3": chosen["points"],
                "reference_type": chosen["reference_type"],
                "cleanup_backend": chosen["cleanup_backend"],
                "cleanup_status": chosen["cleanup_status"] or ("not-run" if not should_clean else "not-selected"),
                "contour_target": contour_target if contour_target is not None else "",
                "donor": chosen["donor"], "locked_in_v2": "false",
                "raw_boundary_p95": "{:.6f}".format(raw_metrics["boundary_p95"]),
                "raw_boundary_max": "{:.6f}".format(raw_metrics["boundary_max"]),
                "raw_topology_status": "match" if raw_metrics["topology_match"] else "mismatch",
                "raw_ink_iou_128": "{:.6f}".format(raw_metrics["per_size"][128]["ink_iou"]),
                "raw_false_positive_ink_128": "{:.6f}".format(raw_metrics["per_size"][128]["false_positive_ink"]),
                "raw_false_negative_ink_128": "{:.6f}".format(raw_metrics["per_size"][128]["false_negative_ink"]),
                "envelope_delta": "{:.6f}".format(chosen["envelope_delta"]),
                "stroke_weight_delta": "{:.6f}".format(chosen["stroke_weight_delta"]),
            })
            rows.append(row); methods[name] = chosen["method"]
            if changed: changed_names.append(name)
            if repaired: repaired_names.append(name)
            if args.verbose:
                for candidate in candidates:
                    print("candidate {} {} p={} valid={} iou={:.4f} fn={:.4f} env={:.4f}".format(
                        name, candidate["method"], candidate["points"],
                        structural(candidate, contour_target),
                        candidate["raw"]["metrics"]["per_size"][128]["ink_iou"],
                        candidate["raw"]["metrics"]["per_size"][128]["false_negative_ink"],
                        candidate["envelope_delta"]), flush=True)
                    if not structural(candidate, contour_target):
                        print("  invalid self={} handles={} topo={} contours={}/{}".format(
                            candidate["self_intersections"], candidate["invalid_handles"],
                            candidate["metrics"]["topology_match"], len(candidate["layer"]),
                            contour_target if contour_target is not None else "any"), flush=True)
            print("v3 {}/{} {}: {} {} points{}".format(
                index, len(order), name, chosen["method"], chosen["points"],
                " changed" if changed else " fallback"), flush=True)

        output.save(str(rf.OUTPUT_SFD)); output.generate(str(rf.OUTPUT_TTF))
    finally:
        for font in (source, source_ttf, reviewed, output):
            try: font.close()
            except RuntimeError: pass

    completed = subprocess.run([
        sys.executable, str(ROOT / "tools" / "restore_redraw_metadata.py"),
        str(rf.SOURCE_TTF), str(rf.OUTPUT_TTF)], cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if completed.returncode:
        raise RuntimeError("metadata restore failed: " + completed.stdout[-2000:])

    saved = fontforge.open(str(rf.OUTPUT_SFD))
    try:
        by_name = {row["glyph"]: row for row in rows}
        now = datetime.now(timezone.utc).isoformat()
        for name in order:
            row, layer = by_name[name], saved[name].foreground
            digest = rf.layer_hash(layer)
            frozen_hash = (frozen.get("repair_queue", {}).get(name, {})
                           .get("candidate_hash"))
            serialized_change = bool(frozen_hash and digest != frozen_hash)
            if serialized_change and name not in changed_names:
                changed_names.append(name)
                row["changed_in_v3"] = "true"
                row["current_review_status"] = "needs_rework"
                if row.get("repair_method") == "reviewed-v2":
                    row["repair_method"] = "reviewed-v2-reserialized"
            row["candidate_hash"] = digest
            row["redrawn_points"] = sum(len(contour) for contour in layer)
            counts = rf.point_type_counts(layer)
            row.update({"corner_points": counts["corner"], "curve_points": counts["curve"],
                        "hvcurve_points": counts["hvcurve"], "tangent_points": counts["tangent"],
                        "off_curve_points": counts["off"]})
            if name in changed_names:
                decisions[name] = {"status": "needs_rework", "source_hash": row["source_hash"],
                                   "candidate_hash": digest, "updated_at": now,
                                   "previous_review_status": row["previous_review_status"]}
            rf.apply_manual_decision(row, {"decisions": decisions})
            if name not in changed_names:
                row["current_review_status"] = status_for(row, decisions)
    finally:
        saved.close()

    decision_payload.update({"version": "redraw-manual-review-v3", "candidate_version": "redraw-v3"})
    atomic_json(rf.DECISIONS, decision_payload)
    shutil.copy2(rf.OUTPUT_SFD, rf.REVIEW_SFD); shutil.copy2(rf.OUTPUT_TTF, rf.REVIEW_TTF)
    fields = list(dict.fromkeys(rf.REPORT_FIELDS + EXTRA_FIELDS))
    with rf.REPORT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)

    if V3.exists(): shutil.rmtree(V3)
    V3.mkdir(parents=True)
    artifacts = {"redrawn.sfd": rf.OUTPUT_SFD, "redrawn.ttf": rf.OUTPUT_TTF,
                 "redraw-review.sfd": rf.REVIEW_SFD, "redraw-review.ttf": rf.REVIEW_TTF,
                 "redraw-report.csv": rf.REPORT, "redraw-manual-decisions.json": rf.DECISIONS}
    for name, path in artifacts.items(): shutil.copy2(path, V3 / name)
    manifest = {
        "version": "redraw-v3", "base_version": "redraw-reviewed-v2",
        "created_at": datetime.now(timezone.utc).isoformat(), "glyph_count": len(rows),
        "locked_glyph_count": len(locked_names), "repair_queue_count": len(repair_names),
        "changed_glyphs": changed_names, "repaired_structure_glyphs": repaired_names,
        "repair_methods": methods,
        "unresolved_glyphs": [row["glyph"] for row in rows if row["needs_manual_review"] == "true"],
        "artifact_hashes": {name: sha256(V3 / name) for name in artifacts},
    }
    atomic_json(V3 / "manifest.json", manifest)
    print(json.dumps({"changed": len(changed_names), "repaired_structure": len(repaired_names),
                      "unresolved": len(manifest["unresolved_glyphs"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
