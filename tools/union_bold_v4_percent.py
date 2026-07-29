#!/usr/bin/env ffpython
"""Union the geometric percent rings and slash into canonical topology."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".font-deps"), str(ROOT / "tools")]

import repair_bold_v4 as v4
import simplify_font as sf

BOLD = ROOT / "fontforge" / "bold.sfd"
REGULAR = ROOT / "fontforge" / "redrawn.sfd"
WORK = ROOT / "qa" / "bold-v4-percent-union"
OUT = ROOT / "qa" / "assets" / "bold-v4-percent-union.json"


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    source = WORK / "source.ttf"
    clean_path = WORK / "clean.ttf"
    report_path = WORK / "pathops.json"
    bold = fontforge.open(str(BOLD))
    regular = fontforge.open(str(REGULAR))
    before = bold["percent"].foreground.dup()
    bold.generate(str(source), flags=("opentype",))
    result = subprocess.run([
        sf.DEFAULT_EXTERNAL_PYTHON, str(ROOT / "tools" / "pathops_cleanup.py"),
        str(source), "percent", str(clean_path), str(report_path),
    ], cwd=str(ROOT), capture_output=True, text=True)
    if result.returncode:
        raise SystemExit("Percent PathOps failed: " + result.stderr)
    clean = fontforge.open(str(clean_path))
    candidate = clean["percent"].foreground.dup()
    clean.close()
    metrics = v4.comparison(regular["percent"].foreground, candidate)
    actual = max(metrics[size]["right_topology"][1] for size in (256, 512))
    defects = v4.structural(candidate)
    if actual > 2 or defects["intersections"]:
        raise SystemExit("Percent union did not retain exactly two canonical counters")
    bold["percent"].foreground = candidate
    bold.save(str(BOLD))
    bold.close()
    regular.close()
    delta = v4.comparison(before, candidate)
    payload = {
        "version": "bold-v4-percent-union",
        "glyphs": {"percent": {
            "status": "ok", "method": "skia-pathops-geometric-percent-union",
            "required": 2, "actual": actual,
            "old_points": v4.point_count(before),
            "new_points": v4.point_count(candidate),
            "iou_64": delta[64]["iou"], "iou_128": delta[128]["iou"],
        }},
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"actual_counters": actual}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
