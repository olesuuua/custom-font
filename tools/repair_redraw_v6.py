#!/usr/bin/env ffpython
"""Targeted v6 smoothing and self-intersection cleanup on top of redraw-v5."""

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
import repair_redraw_v2 as v2
import repair_redraw_v4 as v4
import simplify_font as sf

V5 = ROOT / "checkpoints" / "redraw-v5"
V6 = ROOT / "checkpoints" / "redraw-v6"
WORK = ROOT / "checkpoints" / "redraw-v6-work"
CURVATURE_REVIEW = ROOT / "fontforge" / "curvature-contour-review.sfd"
ALMOST_DONE = {
    "uni0401", "uni041D", "dong", "uni20B4", "oneeighth",
    "threeeighths", "fiveeighths", "uni21BA", "approxequal",
}
PASS_AFTER_CLEANUP = {"daggerdbl", "element"}
SEVEN = "seveneighths"
TARGETS = ALMOST_DONE | PASS_AFTER_CLEANUP | {SEVEN}
CEILING = 100
EXTRA_FIELDS = (
    "changed_in_v6", "v6_smoothing_method", "v6_converted_corners",
    "v6_resulting_curve_points", "v6_donor_glyphs",
)


def atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def point_count(layer) -> int:
    return sum(len(contour) for contour in layer)


def promote_smooth_types(layer):
    promoted = layer.dup()
    for contour in promoted:
        values = list(contour)
        for index, point in enumerate(values):
            if not point.on_curve:
                continue
            previous = values[(index - 1) % len(values)]
            following = values[(index + 1) % len(values)]
            if previous.on_curve or following.on_curve:
                continue
            incoming = rf.normalize((point.x - previous.x, point.y - previous.y))
            outgoing = rf.normalize((following.x - point.x, following.y - point.y))
            if rf.dot(incoming, outgoing) < math.cos(math.radians(32.0)):
                continue
            if abs(incoming[0]) <= .025 or abs(incoming[1]) <= .025:
                point.type = fontforge.splineHVCurve
            else:
                point.type = fontforge.splineCurve
    return promoted


def scratch_cleanup(layer, error):
    scratch = fontforge.font(); scratch.encoding = "UnicodeFull"
    glyph = scratch.createChar(-1, "candidate")
    try:
        glyph.foreground = layer.dup()
        glyph.simplify(error, ("mergelines", "choosehv", "smoothcurves"))
        glyph.removeOverlap(); glyph.correctDirection()
        return promote_smooth_types(glyph.foreground)
    finally:
        scratch.close()


def evaluate(raw_sets, layer, method, normalize=True):
    return v4.evaluate(raw_sets, raw_sets, layer, CEILING, method, normalize=normalize)


def structurally_clean(candidate) -> bool:
    return (
        candidate["points"] <= CEILING
        and not candidate["self_intersections"]
        and not candidate["invalid_handles"]
        and candidate["metrics"]["topology_match"]
        and candidate["direction_valid"]
    )


def raster_score(candidate):
    return v4.raster_score(candidate)


def quality_gate(candidate, baseline) -> bool:
    if not structurally_clean(candidate):
        return False
    new = candidate["metrics"]; old = baseline["metrics"]
    n128, o128 = new["per_size"][128], old["per_size"][128]
    return (
        n128["ink_iou"] >= o128["ink_iou"] - .015
        and n128["false_positive_ink"] <= o128["false_positive_ink"] + .02
        and n128["false_negative_ink"] <= o128["false_negative_ink"] + .02
        and new["boundary_p95"] <= old["boundary_p95"] + .004
    )


def smoothness(candidate):
    counts = rf.point_type_counts(candidate["layer"])
    return counts["corner"], -(counts["curve"] + counts["hvcurve"]), candidate["points"]


def select_candidate(name, candidates, baseline):
    valid = [candidate for candidate in candidates if quality_gate(candidate, baseline)]
    if not valid:
        raise RuntimeError(name + " has no structurally clean non-regressing v6 candidate")
    if name == SEVEN:
        donor = [candidate for candidate in valid
                 if candidate["method"].startswith("uni2077-guided-top-bar-thin")]
        if not donor:
            raise RuntimeError("seveneighths has no valid registered-seven candidate")
        return min(donor, key=lambda item: (
            raster_score(item), item["metrics"]["boundary_p95"], item["points"]
        ))
    if name in PASS_AFTER_CLEANUP:
        return min(valid, key=lambda item: (raster_score(item), item["metrics"]["boundary_p95"],
                                            item["points"]))
    baseline_smooth = smoothness(baseline)
    smoother = [candidate for candidate in valid if smoothness(candidate) < baseline_smooth]
    if not smoother:
        raise RuntimeError(name + " has no candidate with improved curve/corner structure")
    return min(smoother, key=lambda item: (
        smoothness(item), raster_score(item), item["metrics"]["boundary_p95"]
    ))


def contour_bbox(contour):
    layer = fontforge.layer(); layer.is_quadratic = False; layer += contour.dup()
    return sf.bbox_of_sets(v4.point_sets(layer))


def seven_donor_candidates(output, current_layer, raw_sets):
    records = sorted(
        [(contour_bbox(contour), contour.dup()) for contour in current_layer],
        key=lambda item: (item[0][0] + item[0][2]) / 2.0,
    )
    if len(records) != 5:
        raise RuntimeError("seveneighths must have five contours")
    numerator_bbox, numerator = records[0]
    top = numerator_bbox[3]
    height = numerator_bbox[3] - numerator_bbox[1]
    threshold = top - height * .24
    candidates = []
    for factor in (.65, .72, .80, .88):
        thinned = numerator.dup()
        for point in thinned:
            if point.y >= threshold:
                point.y = top - (top - point.y) * factor
        combined = fontforge.layer(); combined.is_quadratic = False
        combined += thinned
        for _bbox, contour in records[1:]:
            combined += contour
        combined = promote_smooth_types(combined)
        candidate = evaluate(
            raw_sets, combined,
            "uni2077-guided-top-bar-thin:{:.2f}".format(factor),
        )
        candidate["donor"] = "uni2077"
        candidates.append(candidate)
    return candidates


def build_candidates(name, output, curvature, raw_sets):
    current = output[name].foreground.dup()
    candidates = []
    for error in (.25, .5, 1.0, 1.5, 2.0, 3.0):
        cleaned = scratch_cleanup(current, error)
        candidate = evaluate(raw_sets, cleaned, "smoothcurves:{:.2f}".format(error))
        candidates.append(candidate)
        if candidate["points"] > CEILING or candidate["self_intersections"]:
            reference = v4.point_sets(cleaned)
            candidates.extend(v4.ladder_candidates(
                reference, raw_sets, [80, 90, 100], CEILING,
                "smoothcurves-refit:{:.2f}".format(error),
            ))
    if name in curvature:
        candidate = evaluate(raw_sets, promote_smooth_types(curvature[name].foreground),
                             "curvature-review-donor")
        candidates.append(candidate)
    if name == SEVEN:
        candidates.extend(seven_donor_candidates(output, current, raw_sets))
    return candidates


def copy_old_fields(row, old):
    for field, value in old.items():
        row.setdefault(field, value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if V6.exists() and not args.force:
        raise SystemExit("redraw-v6 exists; use --force")
    WORK.mkdir(parents=True, exist_ok=True)
    with (V5 / "redraw-report.csv").open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    for field in EXTRA_FIELDS:
        if field not in fields:
            fields.append(field)
    old_rows = {row["glyph"]: row for row in rows}
    decisions_payload = json.loads((V5 / "redraw-manual-decisions.json").read_text(encoding="utf-8"))
    decisions = decisions_payload["decisions"]
    source = fontforge.open(str(ROOT / "fontforge" / "original.sfd"))
    output = fontforge.open(str(V5 / "redrawn.sfd"))
    curvature = fontforge.open(str(CURVATURE_REVIEW))
    order = [glyph.glyphname for glyph in output.glyphs()]
    new_rows, methods = [], {}
    try:
        for name in order:
            old = dict(old_rows[name])
            if name not in TARGETS:
                old["changed_in_v6"] = "false"
                new_rows.append(old)
                continue
            raw_sets = v4.point_sets(source[name].foreground)
            baseline = evaluate(raw_sets, output[name].foreground.dup(), "v5-baseline",
                                normalize=False)
            candidates = build_candidates(name, output, curvature, raw_sets)
            selected = select_candidate(name, candidates, baseline)
            before_counts = rf.point_type_counts(output[name].foreground)
            after_counts = rf.point_type_counts(selected["layer"])
            output[name].foreground = selected["layer"].dup()
            row = rf.result_row(source[name], source[name].foreground, selected,
                                len(candidates), {"decisions": {}})
            copy_old_fields(row, old)
            row.update({
                "changed_in_v6": "true",
                "v6_smoothing_method": selected["method"],
                "v6_converted_corners": str(max(0, before_counts["corner"] - after_counts["corner"])),
                "v6_resulting_curve_points": str(after_counts["curve"] + after_counts["hvcurve"]),
                "v6_donor_glyphs": selected.get("donor", ""),
                "current_review_status": "pass" if name in PASS_AFTER_CLEANUP else "needs_rework",
                "point_ceiling": str(CEILING),
                "repair_method": selected["method"],
            })
            new_rows.append(row); methods[name] = selected["method"]
            print("v6 {}: {} points, corners {}->{}, curves {}->{}".format(
                name, selected["points"], before_counts["corner"], after_counts["corner"],
                before_counts["curve"] + before_counts["hvcurve"],
                after_counts["curve"] + after_counts["hvcurve"],
            ), flush=True)
        output.save(str(WORK / "redrawn.sfd"))
        output.generate(str(WORK / "redrawn.ttf"))
    finally:
        source.close(); output.close(); curvature.close()

    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "restore_redraw_metadata.py"),
         str(V5 / "redrawn.ttf"), str(WORK / "redrawn.ttf")],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if completed.returncode:
        raise RuntimeError("metadata restore failed: " + completed.stdout[-2000:])

    saved = fontforge.open(str(WORK / "redrawn.sfd"))
    try:
        by_name = {row["glyph"]: row for row in new_rows}
        now = datetime.now(timezone.utc).isoformat()
        for name in order:
            row = by_name[name]; layer = saved[name].foreground
            row["candidate_hash"] = rf.layer_hash(layer)
            row["redrawn_points"] = str(point_count(layer))
            if name in TARGETS:
                status = "pass" if name in PASS_AFTER_CLEANUP else "needs_rework"
                decisions[name] = {
                    "status": status, "source_hash": row["source_hash"],
                    "candidate_hash": row["candidate_hash"], "updated_at": now,
                }
            rf.apply_manual_decision(row, {"decisions": decisions})
    finally:
        saved.close()

    review_sfd = WORK / "redraw-review.sfd"
    review_ttf = WORK / "redraw-review.ttf"
    shutil.copy2(WORK / "redrawn.sfd", review_sfd)
    shutil.copy2(WORK / "redrawn.ttf", review_ttf)
    report = WORK / "redraw-report.csv"
    with report.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(new_rows)
    decisions_payload.update({
        "version": "redraw-manual-review-v6", "candidate_version": "redraw-v6",
    })
    decision_path = WORK / "redraw-manual-decisions.json"
    atomic_json(decision_path, decisions_payload)

    if V6.exists():
        shutil.rmtree(V6)
    V6.mkdir(parents=True)
    artifacts = {
        "redrawn.sfd": WORK / "redrawn.sfd", "redrawn.ttf": WORK / "redrawn.ttf",
        "redraw-review.sfd": review_sfd, "redraw-review.ttf": review_ttf,
        "redraw-report.csv": report, "redraw-manual-decisions.json": decision_path,
    }
    for filename, path in artifacts.items():
        shutil.copy2(path, V6 / filename)
    manifest = {
        "version": "redraw-v6", "base_version": "redraw-v5",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "glyph_count": len(new_rows), "hard_point_ceiling": CEILING,
        "target_glyphs": sorted(TARGETS), "changed_glyphs": sorted(TARGETS),
        "repair_methods": methods,
        "artifact_hashes": {name: sha256(V6 / name) for name in artifacts},
    }
    atomic_json(V6 / "manifest.json", manifest)
    print(json.dumps({"changed": len(TARGETS), "targets": len(TARGETS)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
