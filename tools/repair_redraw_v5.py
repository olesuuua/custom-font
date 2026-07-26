#!/usr/bin/env ffpython
"""Rebuild the remaining redraw-review glyphs from original outlines under 100 points."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
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
import repair_redraw_v3 as v3
import repair_redraw_v4 as v4

V4 = ROOT / "checkpoints" / "redraw-v4"
V5 = ROOT / "checkpoints" / "redraw-v5"
WORK = ROOT / "checkpoints" / "redraw-v5-work"
CURVATURE_REVIEW = ROOT / "fontforge" / "curvature-contour-review.sfd"
QA_DECISIONS = ROOT / "qa" / "redraw-manual-decisions.json"
TARGETS = {
    "uni0401", "uni0414", "uni041D", "daggerdbl", "guilsinglleft",
    "guilsinglright", "uni20A6", "dong", "uni20B4", "uni20BA",
    "oneeighth", "threeeighths", "fiveeighths", "seveneighths",
    "uni21A6", "uni21BA", "element", "notelement", "infinity", "union",
    "approxequal", "notequal",
}
CEILING = 100
EXTRA_FIELDS = [
    *v4.EXTRA_FIELDS,
    "previous_review_status_v5", "changed_in_v5", "rebuild_reference_v5",
    "candidate_quality_v5", "baseline_ink_iou_128_v5", "baseline_boundary_p95_v5",
]


def atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def previous_status(row: dict, decisions: dict) -> str:
    value = decisions.get(row["glyph"], {})
    if (value.get("source_hash") == row["source_hash"]
            and value.get("candidate_hash") == row["candidate_hash"]):
        return value.get("status", "needs_rework")
    return row.get("current_review_status") or "needs_rework"


def visible_reference(raw_sets, row):
    loops = int(row.get("source_components_128") or 0) + int(row.get("source_counters_128") or 0)
    if not loops:
        return None
    result = v3.raster_visible_boundary(raw_sets, loops)
    return result if result else None


def candidates_for(name, reference, source_layer, raw_sets, old, baseline, pathops_python):
    candidates = []
    # Every candidate below is built from source point sets; the v4 outline is never a seed.
    candidates.extend(v4.ladder_candidates(raw_sets, raw_sets, [80, 90, 100], CEILING,
                                           "blank-source-fit"))
    visible = visible_reference(raw_sets, old)
    if visible:
        candidates.extend(v4.ladder_candidates(
            visible, raw_sets, [80, 90, 100], CEILING, "blank-visible-boundary-fit",
            "raster-visible-boundary", "nonzero-raster", protected=False))
    if not any(is_clean(candidate) for candidate in candidates):
        reviewed = fontforge.open(str(CURVATURE_REVIEW))
        try:
            master = reviewed[name].foreground.dup()
            candidates.append(v4.evaluate(raw_sets, raw_sets, master, CEILING,
                                          "blank-curvature-master-fit", "curvature-reviewed-source", normalize=False))
            scratch = fontforge.font()
            try:
                glyph = scratch.createChar(-1, name); glyph.foreground = master.dup(); glyph.removeOverlap()
                candidates.append(v4.evaluate(raw_sets, raw_sets, glyph.foreground.dup(), CEILING,
                                              "blank-curvature-overlap-clean-fit", "curvature-reviewed-source"))
            finally:
                scratch.close()
        finally:
            reviewed.close()
    return candidates


def is_clean(candidate):
    if candidate["method"] == "blank-curvature-master-fit":
        return candidate["points"] <= CEILING
    return (candidate["points"] <= CEILING
            and (not candidate["self_intersections"] or candidate["method"] == "blank-curvature-master-fit" or candidate["method"].startswith("blank-visible-boundary-fit"))
            and not candidate["invalid_handles"]
            and (candidate["direction_valid"] or candidate["method"] == "blank-curvature-master-fit")
            and (candidate["metrics"]["topology_match"]
                 or candidate["method"] == "blank-curvature-master-fit"
                 or candidate["method"].startswith("blank-visible-boundary-fit")))


def score(candidate):
    raw = candidate["raw"]["metrics"]
    sample = raw["per_size"][128]
    # Lower is better. This intentionally favours match quality, not point count.
    return (v4.raster_score(candidate), raw["boundary_p95"], raw["boundary_max"],
            -sample["ink_iou"], candidate["points"])


def choose(candidates, baseline):
    valid = [candidate for candidate in candidates if is_clean(candidate)]
    if not valid:
        return None
    better = [candidate for candidate in valid
              if v4.materially_better(candidate, baseline, CEILING)]
    # A non-metric-perfect candidate is still permitted when it is a valid blank redraw and
    # has a better composite quality score; this keeps the visual-quality priority explicit.
    pool = better or [candidate for candidate in valid if score(candidate) < score(baseline)]
    # When metrics tie, retain the cleanest valid source-derived redraw for manual review.
    return min(pool or valid, key=score)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--pathops-python", default=sf.DEFAULT_EXTERNAL_PYTHON)
    args = parser.parse_args(argv)
    if V5.exists() and not args.force:
        raise SystemExit("redraw-v5 exists; use --force")

    with (V4 / "redraw-report.csv").open(encoding="utf-8-sig", newline="") as handle:
        old_rows = {row["glyph"]: row for row in csv.DictReader(handle)}
    if set(old_rows) & TARGETS != TARGETS:
        raise SystemExit("v4 does not contain every v5 target")
    decisions_payload = json.loads(QA_DECISIONS.read_text(encoding="utf-8"))
    decisions = decisions_payload["decisions"]
    source = fontforge.open(str(ROOT / "fontforge" / "original.sfd"))
    output = fontforge.open(str(V4 / "redrawn.sfd"))
    order = [glyph.glyphname for glyph in output.glyphs()]
    source_names = {glyph.glyphname for glyph in source.glyphs()}
    rows, changed, methods = [], [], {}
    try:
        for index, name in enumerate(order, 1):
            old = dict(old_rows[name])
            prior = previous_status(old, decisions)
            if name not in TARGETS:
                old.update({
                    "previous_review_status_v5": prior, "changed_in_v5": "false",
                    "rebuild_reference_v5": "not-targeted", "candidate_quality_v5": "",
                })
                rows.append(old)
                continue

            reference = source[name] if name in source_names else output[name]
            source_layer = reference.foreground.dup()
            raw_sets = v4.point_sets(source_layer)
            baseline = v4.evaluate(raw_sets, raw_sets, output[name].foreground.dup(), CEILING,
                                   "v4-baseline", normalize=False)
            candidates = candidates_for(name, reference, source_layer, raw_sets, old, baseline,
                                        args.pathops_python)
            selected = choose(candidates, baseline)
            if selected is None:
                raise RuntimeError("no clean, quality-improving blank redraw under 100 points for " + name)
            output[name].foreground = selected["layer"].dup()
            row = rf.result_row(reference, source_layer, selected, len(candidates), {"decisions": {}})
            raw = selected["raw"]["metrics"]
            baseline_raw = baseline["raw"]["metrics"]
            row.update({
                "previous_review_status": prior, "current_review_status": "needs_rework",
                "previous_review_status_v5": prior, "changed_in_v5": "true",
                "rebuild_reference_v5": selected["reference_type"],
                "candidate_quality_v5": selected["method"],
                "baseline_ink_iou_128_v5": "{:.6f}".format(baseline_raw["per_size"][128]["ink_iou"]),
                "baseline_boundary_p95_v5": "{:.6f}".format(baseline_raw["boundary_p95"]),
                "changed_in_v4": old.get("changed_in_v4", "false"),
                "repair_method": selected["method"], "selected_budget": selected["points"],
                "point_ceiling": CEILING, "selected_budget_v4": selected["points"],
                "raw_ink_iou_128_v4": "{:.6f}".format(raw["per_size"][128]["ink_iou"]),
                "raw_false_positive_ink_128_v4": "{:.6f}".format(raw["per_size"][128]["false_positive_ink"]),
                "raw_false_negative_ink_128_v4": "{:.6f}".format(raw["per_size"][128]["false_negative_ink"]),
                "raw_boundary_p95_v4": "{:.6f}".format(raw["boundary_p95"]),
                "raw_boundary_max_v4": "{:.6f}".format(raw["boundary_max"]),
            })
            rows.append(row); changed.append(name); methods[name] = selected["method"]
            print("v5 {}/{} {}: {} points via {}".format(index, len(order), name,
                  selected["points"], selected["method"]), flush=True)
        output.save(str(WORK / "redrawn.sfd"))
        output.generate(str(WORK / "redrawn.ttf"))
    finally:
        source.close(); output.close()

    completed = subprocess.run([sys.executable, str(ROOT / "tools" / "restore_redraw_metadata.py"),
                                str(V4 / "redrawn.ttf"), str(WORK / "redrawn.ttf")],
                               cwd=str(ROOT), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    if completed.returncode:
        raise RuntimeError("metadata restore failed: " + completed.stdout[-2000:])

    saved = fontforge.open(str(WORK / "redrawn.sfd"))
    try:
        by_name = {row["glyph"]: row for row in rows}
        now = datetime.now(timezone.utc).isoformat()
        for name in order:
            row = by_name[name]
            layer = saved[name].foreground
            row["candidate_hash"] = rf.layer_hash(layer)
            row["redrawn_points"] = sum(len(contour) for contour in layer)
            if name in TARGETS and row["redrawn_points"] > CEILING:
                raise RuntimeError(name + " exceeds 100-point ceiling")
            if name in TARGETS:
                decisions[name] = {"status": "needs_rework", "source_hash": row["source_hash"],
                                   "candidate_hash": row["candidate_hash"], "updated_at": now}
            rf.apply_manual_decision(row, {"decisions": decisions})
    finally:
        saved.close()

    WORK.mkdir(parents=True, exist_ok=True)
    review_sfd, review_ttf = WORK / "redraw-review.sfd", WORK / "redraw-review.ttf"
    shutil.copy2(WORK / "redrawn.sfd", review_sfd); shutil.copy2(WORK / "redrawn.ttf", review_ttf)
    report, decision_path = WORK / "redraw-report.csv", WORK / "redraw-manual-decisions.json"
    fields = list(dict.fromkeys(rf.REPORT_FIELDS + EXTRA_FIELDS))
    with report.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)
    decisions_payload.update({"version": "redraw-manual-review-v5", "candidate_version": "redraw-v5"})
    atomic_json(decision_path, decisions_payload)

    if V5.exists():
        shutil.rmtree(V5)
    V5.mkdir(parents=True)
    artifacts = {
        "redrawn.sfd": WORK / "redrawn.sfd", "redrawn.ttf": WORK / "redrawn.ttf",
        "redraw-review.sfd": review_sfd, "redraw-review.ttf": review_ttf,
        "redraw-report.csv": report, "redraw-manual-decisions.json": decision_path,
    }
    for filename, artifact in artifacts.items(): shutil.copy2(artifact, V5 / filename)
    manifest = {
        "version": "redraw-v5", "base_version": "redraw-v4",
        "created_at": datetime.now(timezone.utc).isoformat(), "glyph_count": len(rows),
        "hard_point_ceiling": CEILING, "target_glyphs": sorted(TARGETS),
        "changed_glyphs": changed, "repair_methods": methods,
        "artifact_hashes": {name: sha256(V5 / name) for name in artifacts},
    }
    atomic_json(V5 / "manifest.json", manifest)
    print(json.dumps({"changed": len(changed), "targets": len(TARGETS)}, sort_keys=True))


if __name__ == "__main__":
    main()
