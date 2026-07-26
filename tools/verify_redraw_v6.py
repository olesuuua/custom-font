#!/usr/bin/env ffpython
"""Verify redraw-v6 targeted smoothing, cleanup, statuses, and metadata."""

import csv
import json
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".font-deps")); sys.path.insert(0, str(ROOT / "tools"))
from fontTools.ttLib import TTFont
import redraw_font as rf
import repair_redraw_v4 as v4
import repair_redraw_v6 as v6


def points(glyph):
    return sum(len(contour) for contour in glyph.foreground)


def main():
    errors = []
    with (v6.V6 / "redraw-report.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = {row["glyph"]: row for row in csv.DictReader(handle)}
    decisions = json.loads((v6.V6 / "redraw-manual-decisions.json").read_text(
        encoding="utf-8"))["decisions"]
    base = fontforge.open(str(v6.V5 / "redrawn.sfd"))
    output = fontforge.open(str(v6.V6 / "redrawn.sfd"))
    source = fontforge.open(str(ROOT / "fontforge" / "original.sfd"))
    base_tt = TTFont(str(v6.V5 / "redrawn.ttf"), recalcBBoxes=False, recalcTimestamp=False)
    output_tt = TTFont(str(v6.V6 / "redrawn.ttf"), recalcBBoxes=False, recalcTimestamp=False)
    changed = set()
    try:
        if len(rows) != 334: errors.append("glyph count differs")
        if base_tt.getGlyphOrder() != output_tt.getGlyphOrder(): errors.append("glyph order differs")
        if base_tt.getBestCmap() != output_tt.getBestCmap(): errors.append("cmap differs")
        if base_tt["hmtx"].metrics != output_tt["hmtx"].metrics: errors.append("metrics differ")
        for name, row in rows.items():
            differs = rf.layer_hash(base[name].foreground) != rf.layer_hash(output[name].foreground)
            if differs: changed.add(name)
            if differs and name not in v6.TARGETS:
                errors.append(name + " changed outside v6 targets")
            if points(output[name]) != int(row["redrawn_points"]):
                errors.append(name + " point count/report mismatch")
            if rf.layer_hash(output[name].foreground) != row["candidate_hash"]:
                errors.append(name + " hash mismatch")
            if name not in v6.TARGETS:
                continue
            if points(output[name]) > v6.CEILING:
                errors.append(name + " exceeds point ceiling")
            raw = v4.point_sets(source[name].foreground)
            candidate = v6.evaluate(raw, output[name].foreground.dup(),
                                    row.get("v6_smoothing_method") or "verify", normalize=False)
            if not v6.structurally_clean(candidate):
                errors.append(name + " is structurally invalid")
            expected = "pass" if name in v6.PASS_AFTER_CLEANUP else "needs_rework"
            decision = decisions.get(name, {})
            if (decision.get("status") != expected
                    or decision.get("source_hash") != row["source_hash"]
                    or decision.get("candidate_hash") != row["candidate_hash"]):
                errors.append(name + " review decision mismatch")
            if row.get("changed_in_v6") != "true":
                errors.append(name + " missing v6 marker")
        if changed != v6.TARGETS:
            errors.append("changed glyph set differs from v6 targets")
    finally:
        base.close(); output.close(); source.close(); base_tt.close(); output_tt.close()
    print("glyphs=334; changed={}; errors={}".format(len(changed), len(errors)))
    for error in errors:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
