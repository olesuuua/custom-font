#!/usr/bin/env ffpython
"""Finalize v4 PathOps candidates and targeted solid-outline fallbacks."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".font-deps"), str(ROOT / "tools")]

import bold_curve_helpers as curves
import repair_bold_v4 as v4

BOLD = ROOT / "fontforge" / "bold.sfd"
WORK = ROOT / "qa" / "bold-v4-pathops-work"
PATHOPS_REPORT = ROOT / "qa" / "assets" / "bold-v4-pathops.json"
OUT = ROOT / "qa" / "assets" / "bold-v4-final-cleanup.json"


def largest_only(layer):
    contours = list(layer)
    if not contours:
        return layer
    largest = max(contours, key=v4.contour_area)
    result = fontforge.layer()
    result.is_quadratic = layer.is_quadratic
    result += largest.dup()
    return result


def refit(layer, preferred=None):
    sets = v4.layer_sets(layer, error=.4, spacing=1.8)
    if not sets:
        return layer
    counts = []
    for points in sets:
        counts.append(min(30, max(8, len(points) // 7)))
    if preferred and len(sets) == 1:
        counts = [preferred]
    return curves.curve_layer(
        sets, counts, tension=.27, corner_angle=50.0,
        force_smooth_indices=(),
    )


def pathops_candidate(name):
    directories = sorted(WORK.glob("*-" + name.replace("/", "_")))
    if not directories:
        return None
    path = directories[0] / "clean.ttf"
    if not path.exists():
        return None
    font = fontforge.open(str(path))
    result = font[name].foreground.dup()
    font.close()
    return result


def clean_candidate(font, name, candidate, preferred=None, solid=False):
    if solid:
        candidate = largest_only(candidate)
    defects = v4.structural(candidate)
    if defects["intersections"]:
        candidate = refit(candidate, preferred)
        try:
            candidate = v4.scratch_cleanup(font, name, candidate, .5)
        except Exception:
            pass
    if solid:
        candidate = largest_only(candidate)
    return candidate


def main():
    bold = fontforge.open(str(BOLD))
    pathops = json.loads(PATHOPS_REPORT.read_text(encoding="utf-8"))
    rows = {}
    try:
        for name, status in pathops["glyphs"].items():
            if status.get("status") != "blocked":
                continue
            candidate = pathops_candidate(name)
            if candidate is None:
                continue
            before = bold[name].foreground.dup()
            candidate = clean_candidate(
                bold, name, candidate,
                preferred=18 if name == "z" else None,
                solid=name in v4.FORCE_SOLID,
            )
            defects = v4.structural(candidate)
            if defects["intersections"] or defects["open_contours"] or defects["invalid_handles"]:
                rows[name] = {"status": "blocked", "defects": defects}
                continue
            bold[name].foreground = candidate
            metrics = v4.comparison(before, candidate)
            rows[name] = {
                "status": "ok",
                "method": "pathops-visual-fallback-refit",
                "old_points": v4.point_count(before),
                "new_points": v4.point_count(candidate),
                "iou_64": metrics[64]["iou"],
                "iou_128": metrics[128]["iou"],
            }
        for name, preferred in (("one", 10), ("z", 18)):
            before = bold[name].foreground.dup()
            source = pathops_candidate(name) if name == "z" else before
            candidate = clean_candidate(
                bold, name, source or before, preferred=preferred, solid=True
            )
            defects = v4.structural(candidate)
            if not (
                defects["intersections"] or defects["open_contours"]
                or defects["invalid_handles"]
            ):
                bold[name].foreground = candidate
                metrics = v4.comparison(before, candidate)
                rows[name] = {
                    "status": "ok",
                    "method": "geometric-solid-terminal-refit",
                    "old_points": v4.point_count(before),
                    "new_points": v4.point_count(candidate),
                    "iou_64": metrics[64]["iou"],
                    "iou_128": metrics[128]["iou"],
                }
        bold.save(str(BOLD))
    finally:
        bold.close()
    payload = {
        "version": "bold-v4-final-cleanup",
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
