#!/usr/bin/env ffpython
"""Union raster white-patch fill bridges with Skia PathOps."""
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
INPUT = ROOT / "qa" / "assets" / "bold-v4-white-patch-fills.json"
WORK = ROOT / "qa" / "bold-v4-white-union-work"
OUT = ROOT / "qa" / "assets" / "bold-v4-white-union.json"


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    source_ttf = WORK / "source.ttf"
    bold = fontforge.open(str(BOLD))
    regular = fontforge.open(str(REGULAR))
    bold.generate(str(source_ttf), flags=("opentype",))
    source = json.loads(INPUT.read_text(encoding="utf-8"))
    names = [
        name for name, row in source["glyphs"].items()
        if row.get("status") == "pathops-needed"
    ]
    rows = {}
    try:
        for index, name in enumerate(names):
            glyph_dir = WORK / "{:03d}-{}".format(index, name.replace("/", "_"))
            glyph_dir.mkdir(exist_ok=True)
            output = glyph_dir / "clean.ttf"
            report_path = glyph_dir / "report.json"
            result = subprocess.run(
                [
                    sf.DEFAULT_EXTERNAL_PYTHON,
                    str(ROOT / "tools" / "pathops_cleanup.py"),
                    str(source_ttf), name, str(output), str(report_path),
                ],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            if result.returncode or not output.exists():
                rows[name] = {"status": "blocked", "method": "pathops-failed"}
                continue
            cleaned = fontforge.open(str(output))
            candidate = cleaned[name].foreground.dup()
            cleaned.close()
            required = v4.required_counters(regular[name].foreground, name)
            metrics = v4.comparison(regular[name].foreground, candidate)
            actual = max(
                metrics[size]["right_topology"][1] for size in (128, 256, 512)
            )
            defects = v4.structural(candidate)
            if actual > required or defects["intersections"]:
                rows[name] = {
                    "status": "blocked", "method": "pathops-union-rejected",
                    "required": required, "actual": actual, "defects": defects,
                }
                continue
            before = bold[name].foreground.dup()
            bold[name].foreground = candidate
            delta = v4.comparison(before, candidate)
            rows[name] = {
                "status": "ok", "method": "skia-pathops-white-fill-union",
                "required": required, "actual": actual,
                "old_points": v4.point_count(before),
                "new_points": v4.point_count(candidate),
                "iou_64": delta[64]["iou"], "iou_128": delta[128]["iou"],
            }
        bold.save(str(BOLD))
    finally:
        bold.close()
        regular.close()
    payload = {
        "version": "bold-v4-white-union",
        "accepted": sum(row.get("status") == "ok" for row in rows.values()),
        "blocked": sum(row.get("status") == "blocked" for row in rows.values()),
        "glyphs": rows,
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "accepted": payload["accepted"], "blocked": payload["blocked"]
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
