#!/usr/bin/env ffpython
"""Resolve remaining Bold v4 intersections with per-glyph Skia PathOps."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".font-deps"), str(ROOT / "tools")]

import redraw_font as rf
import repair_bold_v4 as v4
import simplify_font as sf

BOLD = ROOT / "fontforge" / "bold.sfd"
REGULAR = ROOT / "fontforge" / "redrawn.sfd"
WORK = ROOT / "qa" / "bold-v4-pathops-work"
OUT = ROOT / "qa" / "assets" / "bold-v4-pathops.json"


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    source_ttf = WORK / "source.ttf"
    bold = fontforge.open(str(BOLD))
    regular = fontforge.open(str(REGULAR))
    bold.generate(str(source_ttf), flags=("opentype",))
    names = [
        glyph.glyphname for glyph in bold.glyphs()
        if any(contour.selfIntersects() for contour in glyph.foreground)
    ]
    rows = {}
    try:
        for index, name in enumerate(names):
            glyph_dir = WORK / "{:03d}-{}".format(index, name.replace("/", "_"))
            glyph_dir.mkdir(exist_ok=True)
            output_ttf = glyph_dir / "clean.ttf"
            report_path = glyph_dir / "report.json"
            result = subprocess.run(
                [
                    sf.DEFAULT_EXTERNAL_PYTHON,
                    str(ROOT / "tools" / "pathops_cleanup.py"),
                    str(source_ttf), name, str(output_ttf), str(report_path),
                ],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            report = (
                json.loads(report_path.read_text(encoding="utf-8"))
                if report_path.exists() else {}
            )
            if result.returncode or report.get("status") != "ok":
                rows[name] = {"status": "blocked", "method": "pathops-failed", **report}
                continue
            cleaned_font = fontforge.open(str(output_ttf))
            candidate = cleaned_font[name].foreground.dup()
            cleaned_font.close()
            required = v4.required_counters(regular[name].foreground, name)
            candidate, removed = v4.drop_extra_holes(candidate, required)
            defects = v4.structural(candidate)
            metrics = v4.comparison(regular[name].foreground, candidate)
            component_ok = all(
                metrics[size]["left_topology"][0]
                == metrics[size]["right_topology"][0]
                for size in (128, 256, 512)
            )
            counter_ok = all(
                metrics[size]["right_topology"][1] <= required
                for size in (128, 256, 512)
            )
            if (
                defects["intersections"] or defects["open_contours"]
                or defects["invalid_handles"] or not component_ok or not counter_ok
            ):
                rows[name] = {
                    "status": "blocked",
                    "method": "pathops-structure-rejected",
                    "defects": defects,
                    "component_ok": component_ok,
                    "counter_ok": counter_ok,
                }
                continue
            before = bold[name].foreground.dup()
            bold[name].foreground = candidate
            old_metrics = v4.comparison(before, candidate)
            rows[name] = {
                "status": "ok",
                "method": "skia-pathops-fill-unmatched",
                "removed_hole_contours": removed,
                "old_points": v4.point_count(before),
                "new_points": v4.point_count(candidate),
                "iou_64": old_metrics[64]["iou"],
                "iou_128": old_metrics[128]["iou"],
                "idempotent": report.get("idempotent", False),
            }
        bold.save(str(BOLD))
    finally:
        bold.close()
        regular.close()
    payload = {
        "version": "bold-v4-pathops",
        "attempted": len(names),
        "accepted": sum(row.get("status") == "ok" for row in rows.values()),
        "blocked": sum(row.get("status") == "blocked" for row in rows.values()),
        "glyphs": rows,
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "attempted": payload["attempted"],
        "accepted": payload["accepted"],
        "blocked": payload["blocked"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
