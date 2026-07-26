#!/usr/bin/env ffpython
"""Verify redraw-v5's target-only changes and universal 100-point cap."""

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
import repair_redraw_v5 as v5


def points(glyph):
    return sum(len(contour) for contour in glyph.foreground)


def main():
    v4_dir, v5_dir = ROOT / "checkpoints" / "redraw-v4", ROOT / "checkpoints" / "redraw-v5"
    manifest = json.loads((v5_dir / "manifest.json").read_text(encoding="utf-8"))
    with (v5_dir / "redraw-report.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    base = fontforge.open(str(v4_dir / "redrawn.sfd")); output = fontforge.open(str(v5_dir / "redrawn.sfd"))
    source_tt = TTFont(str(v4_dir / "redrawn.ttf"), recalcBBoxes=False, recalcTimestamp=False)
    output_tt = TTFont(str(v5_dir / "redrawn.ttf"), recalcBBoxes=False, recalcTimestamp=False)
    errors = []
    try:
        if len(rows) != 334 or manifest.get("hard_point_ceiling") != 100: errors.append("invalid manifest")
        if source_tt.getGlyphOrder() != output_tt.getGlyphOrder(): errors.append("glyph order differs")
        if source_tt.getBestCmap() != output_tt.getBestCmap(): errors.append("cmap differs")
        if source_tt["hmtx"].metrics != output_tt["hmtx"].metrics: errors.append("metrics differ")
        changed = []
        for row in rows:
            name = row["glyph"]
            if name in v5.TARGETS and points(output[name]) > 100: errors.append(name + " exceeds 100 points")
            if rf.layer_hash(output[name].foreground) != row["candidate_hash"]: errors.append(name + " hash mismatch")
            differs = rf.layer_hash(base[name].foreground) != rf.layer_hash(output[name].foreground)
            if differs: changed.append(name)
            if differs and name not in v5.TARGETS: errors.append(name + " changed outside target set")
            if name in v5.TARGETS:
                candidate = v4.evaluate(v4.point_sets(fontforge.open(str(ROOT / "fontforge" / "original.sfd"))[name].foreground),
                                        v4.point_sets(fontforge.open(str(ROOT / "fontforge" / "original.sfd"))[name].foreground),
                                        output[name].foreground.dup(), 100, row.get("candidate_quality_v5") or "verify", normalize=False)
                if not differs: errors.append(name + " was not rebuilt")
                if not v5.is_clean(candidate): errors.append(name + " is structurally invalid")
        if set(changed) != v5.TARGETS: errors.append("changed glyph set differs from target set")
    finally:
        base.close(); output.close(); source_tt.close(); output_tt.close()
    print("glyphs={}; changed={}; errors={}".format(len(rows), len(changed), len(errors)))
    for error in errors: print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
