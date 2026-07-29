#!/usr/bin/env ffpython
"""Remove the final extra counter from superscript/subscript six."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".font-deps"), str(ROOT / "tools")]
import raster_rebuild_bold_v4 as raster
import repair_bold_v4 as v4

BOLD = ROOT / "fontforge" / "bold.sfd"
OLD = ROOT / "checkpoints" / "bold-v3-reviewed" / "bold.sfd"
OUT = ROOT / "qa" / "assets" / "bold-v4-six-counters.json"


def main():
    bold = fontforge.open(str(BOLD))
    old = fontforge.open(str(OLD))
    rows = {}
    for name in ("uni2076", "uni2086"):
        before = bold[name].foreground.dup()
        candidate = raster.reconstruct(
            old[name].foreground, required=1, keep_loops=2
        )
        bold[name].foreground = candidate
        metrics = v4.comparison(before, candidate)
        rows[name] = {
            "status": "ok", "method": "single-counter-raster-boundary-refit",
            "old_points": v4.point_count(before),
            "new_points": v4.point_count(candidate),
            "iou_64": metrics[64]["iou"], "iou_128": metrics[128]["iou"],
        }
    bold.save(str(BOLD))
    bold.close()
    old.close()
    OUT.write_text(
        json.dumps({"version": "bold-v4-six-counters", "glyphs": rows},
                   indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"changed": 2}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
