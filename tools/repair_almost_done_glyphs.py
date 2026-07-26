#!/usr/bin/env ffpython
"""Apply the four user-directed almost-done outline repairs."""

from __future__ import annotations

import csv
import json
import math
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf
import repair_redraw_v2 as v2
import repair_redraw_v3 as v3
import repair_redraw_v4 as v4
import simplify_font as sf

NAMES = ("infinity", "uni044B", "second", "onethird")
REVIEWED = ROOT / "checkpoints" / "redraw-reviewed-v4"
CHECKPOINT = ROOT / "checkpoints" / "redraw-targeted-v1"
EXTRA_FIELDS = ("changed_in_targeted_repair", "targeted_repair_note")


def add_layer(target, source):
    for contour in source:
        target += contour.dup()


def contour_bbox(contour):
    layer = fontforge.layer(); layer.is_quadratic = False; layer += contour.dup()
    return sf.bbox_of_sets(sf.layer_point_sets(layer))


def capsule(start, end, radius):
    x0, y0 = start; x1, y1 = end
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux
    k = 4.0 / 3.0
    contour = fontforge.contour()
    contour.moveTo(x0 + nx * radius, y0 + ny * radius)
    contour.lineTo(x1 + nx * radius, y1 + ny * radius)
    contour.cubicTo(
        (x1 + nx * radius + ux * k * radius, y1 + ny * radius + uy * k * radius),
        (x1 - nx * radius + ux * k * radius, y1 - ny * radius + uy * k * radius),
        (x1 - nx * radius, y1 - ny * radius),
    )
    contour.lineTo(x0 - nx * radius, y0 - ny * radius)
    contour.cubicTo(
        (x0 - nx * radius - ux * k * radius, y0 - ny * radius - uy * k * radius),
        (x0 + nx * radius - ux * k * radius, y0 + ny * radius - uy * k * radius),
        (x0 + nx * radius, y0 + ny * radius),
    )
    contour.closed = True
    return contour


def infinity_candidate(source_layer):
    raw = v4.point_sets(source_layer)
    visible = v3.raster_visible_boundary(raw, 3, size=1024)
    candidates = v4.ladder_candidates(
        visible, raw, [80, 90, 100], 100, "smooth-visible-boundary",
        "raster-visible-boundary", "raster-1024", protected=True,
    )
    valid = [candidate for candidate in candidates if v4.structural(candidate, 100, 3)]
    if not valid:
        raise RuntimeError("infinity has no structurally valid smooth refit")
    return max(valid, key=lambda candidate: candidate["points"]), len(candidates)


def uni044b_candidate(current_layer, source_layer):
    layer = fontforge.layer(); layer.is_quadratic = False
    for contour in current_layer:
        bbox = contour_bbox(contour)
        if bbox[0] < 300:
            layer += contour.dup()
    # Match the original right stem envelope, but keep its width constant.
    layer += capsule((373.0, 55.0), (373.0, 534.0), 23.0)
    raw = v4.point_sets(source_layer)
    candidate = v4.evaluate(raw, raw, layer, 100, "uniform-right-stem")
    if not v4.structural(candidate, 100, 4):
        raise RuntimeError("uni044B uniform-stem candidate is structurally invalid")
    return candidate, 1


def second_candidate(source_layer):
    layer = fontforge.layer(); layer.is_quadratic = False
    layer += capsule((35.7, -55.2), (86.8, 109.8), 18.0)
    layer += capsule((127.7, -67.2), (178.8, 97.8), 18.0)
    raw = v4.point_sets(source_layer)
    candidate = v4.evaluate(raw, raw, layer, 100, "straight-uniform-bars")
    if not v4.structural(candidate, 100, 2):
        raise RuntimeError("second straight-bar candidate is structurally invalid")
    return candidate, 1


def onethird_candidate(output, source_layer):
    raw = v4.point_sets(source_layer)
    targets = sorted((sf.bbox_of_sets([contour]) for contour in raw),
                     key=lambda bbox: (bbox[0] + bbox[2]) / 2.0)
    donors = ("one", "slash", "three")
    combined = fontforge.layer(); combined.is_quadratic = False
    for donor, target in zip(donors, targets):
        donor_layer = output[donor].foreground
        donor_bbox = sf.bbox_of_sets(v4.point_sets(donor_layer))
        registered = v2.registered_donor(donor_layer, donor_bbox, target)
        add_layer(combined, registered)
    candidate = v4.evaluate(
        raw, raw, combined, 120, "registered-one-slash-three",
        "raw-original", donor=",".join(donors), transform="component-envelope",
    )
    if not v4.structural(candidate, 120, 3):
        raise RuntimeError("onethird donor candidate is structurally invalid")
    return candidate, 1


def snapshot_current():
    if REVIEWED.exists():
        return
    REVIEWED.mkdir(parents=True)
    artifacts = {
        "redrawn.sfd": rf.OUTPUT_SFD,
        "redrawn.ttf": rf.OUTPUT_TTF,
        "redraw-review.sfd": rf.REVIEW_SFD,
        "redraw-review.ttf": rf.REVIEW_TTF,
        "redraw-report.csv": rf.REPORT,
        "redraw-manual-decisions.json": rf.DECISIONS,
    }
    for name, source in artifacts.items():
        shutil.copy2(source, REVIEWED / name)
    v4.atomic_json(REVIEWED / "manifest.json", {
        "version": "redraw-reviewed-v4",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "artifact_hashes": {name: v4.sha256(REVIEWED / name) for name in artifacts},
    })


def rebuild_row(source_glyph, candidate, count, old, decisions_payload, note):
    row = rf.result_row(source_glyph, source_glyph.foreground, candidate, count,
                        {"decisions": {}})
    for field in old:
        if field not in row:
            row[field] = old[field]
    row.update({
        "previous_review_status": old.get("current_review_status", "almost_done"),
        "current_review_status": "needs_rework",
        "changed_in_targeted_repair": "true",
        "targeted_repair_note": note,
        "repair_method": candidate.get("method", note),
        "selected_budget_v4": str(candidate["points"]),
        "point_ceiling": "120" if source_glyph.glyphname == "onethird" else "100",
        "direction_result": "normalized",
        "direction_repaired": "true",
        "reference_type_v4": candidate.get("reference_type", "raw-original"),
        "donor_glyphs": candidate.get("donor", ""),
        "donor_transform": candidate.get("donor_transform", ""),
        "high_budget_candidate": "true" if candidate["points"] >= 80 else "false",
    })
    decision = {
        "status": "needs_rework",
        "source_hash": row["source_hash"],
        "candidate_hash": row["candidate_hash"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "previous_review_status": old.get("current_review_status", "almost_done"),
    }
    decisions_payload["decisions"][row["glyph"]] = decision
    rf.apply_manual_decision(row, decisions_payload)
    return row


def main() -> int:
    snapshot_current()
    with rf.REPORT.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle); fields = list(reader.fieldnames or []); rows = list(reader)
    for field in EXTRA_FIELDS:
        if field not in fields: fields.append(field)
    by_name = {row["glyph"]: row for row in rows}
    decisions = json.loads(rf.DECISIONS.read_text(encoding="utf-8"))
    decisions["candidate_version"] = "redraw-targeted-v1"
    source = fontforge.open(str(rf.SOURCE_SFD))
    output = fontforge.open(str(rf.OUTPUT_SFD))
    notes = {
        "infinity": "smooth inside and outside loops",
        "uni044B": "thicker uniform right stem",
        "second": "two straight constant-width angled bars",
        "onethird": "curved one top and smooth donor three",
    }
    counts = {}
    try:
        selected = {}
        selected["infinity"] = infinity_candidate(source["infinity"].foreground)
        selected["uni044B"] = uni044b_candidate(output["uni044B"].foreground,
                                                 source["uni044B"].foreground)
        selected["second"] = second_candidate(source["second"].foreground)
        selected["onethird"] = onethird_candidate(output, source["onethird"].foreground)
        for name, (candidate, count) in selected.items():
            output[name].foreground = candidate["layer"].dup()
            counts[name] = count
        output.save(str(rf.OUTPUT_SFD))
    finally:
        output.close()

    # Reopen the serialized outlines so hashes and metrics match the served font exactly.
    output = fontforge.open(str(rf.OUTPUT_SFD))
    try:
        for name in NAMES:
            raw = v4.point_sets(source[name].foreground)
            ceiling = 120 if name == "onethird" else 100
            candidate = v4.evaluate(raw, raw, output[name].foreground, ceiling,
                                    notes[name])
            by_name[name] = rebuild_row(source[name], candidate, counts[name],
                                        by_name[name], decisions, notes[name])
        output.generate(str(rf.OUTPUT_TTF))
    finally:
        output.close(); source.close()

    tmp = rf.REPORT.with_suffix(rf.REPORT.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(by_name[row["glyph"]] for row in rows)
    tmp.replace(rf.REPORT)
    v4.atomic_json(rf.DECISIONS, decisions)
    shutil.copy2(rf.OUTPUT_SFD, rf.REVIEW_SFD)
    shutil.copy2(rf.OUTPUT_TTF, rf.REVIEW_TTF)

    if CHECKPOINT.exists():
        shutil.rmtree(CHECKPOINT)
    CHECKPOINT.mkdir(parents=True)
    artifacts = {
        "redrawn.sfd": rf.OUTPUT_SFD, "redrawn.ttf": rf.OUTPUT_TTF,
        "redraw-review.sfd": rf.REVIEW_SFD, "redraw-review.ttf": rf.REVIEW_TTF,
        "redraw-report.csv": rf.REPORT, "redraw-manual-decisions.json": rf.DECISIONS,
    }
    for name, path in artifacts.items(): shutil.copy2(path, CHECKPOINT / name)
    v4.atomic_json(CHECKPOINT / "manifest.json", {
        "version": "redraw-targeted-v1",
        "base_version": "redraw-reviewed-v4",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "changed_glyphs": list(NAMES),
        "artifact_hashes": {name: v4.sha256(CHECKPOINT / name) for name in artifacts},
    })
    print(json.dumps({name: {
        "points": by_name[name]["redrawn_points"],
        "iou128": by_name[name]["ink_iou_128"],
        "topology": by_name[name]["topology_status"],
    } for name in NAMES}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
