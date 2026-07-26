#!/usr/bin/env ffpython
"""Verify the four targeted almost-done repairs and all unchanged locks."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf
import repair_redraw_v4 as v4
import simplify_font as sf
import repair_almost_done_glyphs as targeted


def main() -> int:
    errors = []
    with rf.REPORT.open(encoding="utf-8-sig", newline="") as handle:
        rows = {row["glyph"]: row for row in csv.DictReader(handle)}
    decisions = json.loads(rf.DECISIONS.read_text(encoding="utf-8"))["decisions"]
    before = fontforge.open(str(targeted.REVIEWED / "redrawn.sfd"))
    output = fontforge.open(str(rf.OUTPUT_SFD))
    review = fontforge.open(str(rf.REVIEW_SFD))
    try:
        if len(list(output.glyphs())) != 334: errors.append("glyph count differs")
        for glyph in output.glyphs():
            name = glyph.glyphname
            if name not in targeted.NAMES:
                if rf.layer_hash(glyph.foreground) != rf.layer_hash(before[name].foreground):
                    errors.append(name + " changed outside targeted repair")
            if rf.layer_hash(glyph.foreground) != rf.layer_hash(review[name].foreground):
                errors.append(name + " review outline differs")
        for name in targeted.NAMES:
            row = rows[name]
            ceiling = 120 if name == "onethird" else 100
            points = sum(len(contour) for contour in output[name].foreground)
            if points > ceiling: errors.append(name + " exceeds point ceiling")
            if points != int(row["redrawn_points"]): errors.append(name + " point count differs")
            if row["topology_status"] != "match": errors.append(name + " topology mismatch")
            if int(row["self_intersections"]) or int(row["invalid_handles"]):
                errors.append(name + " has invalid geometry")
            audit = v4.direction_audit(output[name].foreground)
            if audit["changed"] or not audit["idempotent"] or audit["degenerate"]:
                errors.append(name + " direction is not normalized")
            decision = decisions.get(name, {})
            if (decision.get("status") not in ("pass", "almost_done", "needs_rework")
                    or decision.get("status") != row["current_review_status"]
                    or decision.get("source_hash") != row["source_hash"]
                    or decision.get("candidate_hash") != row["candidate_hash"]):
                errors.append(name + " review decision is stale or unreconciled")
            if row.get("changed_in_targeted_repair") != "true":
                errors.append(name + " missing targeted repair marker")
        if len(output["infinity"].foreground) != 3:
            errors.append("infinity contour count differs")
        if len(output["uni044B"].foreground) != 4:
            errors.append("uni044B contour count differs")
        if len(output["second"].foreground) != 2:
            errors.append("second must contain two bars")
        if len(output["onethird"].foreground) != 3:
            errors.append("onethird contour count differs")
        stem_boxes = [targeted.contour_bbox(contour)
                      for contour in output["uni044B"].foreground]
        stem = max(stem_boxes, key=lambda box: box[0])
        if not (44 <= stem[2] - stem[0] <= 48):
            errors.append("uni044B right stem thickness differs")
    finally:
        before.close(); output.close(); review.close()
    print("glyphs=334; targeted=4; errors={}".format(len(errors)))
    for error in errors: print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
