#!/usr/bin/env ffpython
"""Build the isolated Bold v5 pilot from the frozen v4 source."""
from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".font-deps"), str(ROOT / "tools")]
import bold_curve_helpers as curves
import redraw_font as rf
import repair_bold_v4 as v4
import raster_rebuild_bold_v4 as raster_backend
import simplify_font as sf

SOURCE = ROOT / "checkpoints" / "bold-v4" / "bold.sfd"
REGULAR = ROOT / "fontforge" / "redrawn.sfd"
CANDIDATE = ROOT / "fontforge" / "bold-v5-pilot.sfd"
MANUAL_CANDIDATE = ROOT / "fontforge" / "bold-v5-pilot-manual.sfd"
PILOT_TTF = ROOT / "qa" / "assets" / "bold-v5-pilot.ttf"
MANUAL_TTF = ROOT / "qa" / "assets" / "bold-v5-pilot-manual.ttf"
REPORT = ROOT / "qa" / "assets" / "bold-v5-pilot.json"
TRIAGE = ROOT / "qa" / "assets" / "bold-report.csv"
WORK = ROOT / "qa" / "bold-v5-pilot-work"
PRIORITY = ("E", "I", "Theta", "plusminus", "seven", "uni041C", "brokenbar", "dagger")
CALIBRATION = ("asterisk", "o", "exclam", "period")


def point_count(layer):
    return sum(len(contour) for contour in layer)


def persistent(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts, key=lambda value: (counts[value], value))


def coordinate_centroid(layer, size=512):
    sets = v4.layer_sets(layer, error=.35, spacing=1.5)
    box = v4.shared_bbox(sets, sets)
    mask = sf.rasterize(sets, box, size)
    ink = [(index % size, index // size) for index, value in enumerate(mask) if value]
    if not ink:
        return (0.0, 0.0)
    px = sum(item[0] + .5 for item in ink) / len(ink)
    py = sum(item[1] + .5 for item in ink) / len(ink)
    return (
        box[0] + px * (box[2] - box[0]) / size,
        box[3] - py * (box[3] - box[1]) / size,
    )


def calibration(font):
    rows = {}
    for name in CALIBRATION:
        layer = font[name].foreground
        metrics = v4.comparison(layer, layer)
        result = {
            "iou": {str(size): metrics[size]["iou"] for size in (64, 128, 256, 512)},
            "topology": {str(size): metrics[size]["left_topology"] for size in (64, 128, 256, 512)},
            "status": "ok",
        }
        if any(abs(value - 1.0) > 1e-9 for value in result["iou"].values()):
            raise RuntimeError(f"Calibration IoU failed for {name}")
        rows[name] = result
    period = font["period"].foreground
    box = period.boundingBox()
    raster_center = coordinate_centroid(period)
    outline_center = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
    delta = math.hypot(raster_center[0] - outline_center[0], raster_center[1] - outline_center[1])
    rows["period"]["coordinate_delta"] = delta
    if delta > 2.0:
        raise RuntimeError(f"Coordinate orientation calibration failed: {delta:.3f}")
    return rows


def copied_layer(contours, quadratic):
    result = fontforge.layer()
    result.is_quadratic = quadratic
    for contour in contours:
        result += contour.dup()
    return result


def geometric_candidates(font, name, before):
    results = []
    closed = before.dup()
    for contour in closed:
        contour.closed = True
    try:
        results.append(("geometric-close-and-union", v4.scratch_cleanup(font, name, closed, .5)))
    except Exception:
        pass
    if name == "Theta":
        contours = list(before)
        for index in range(len(contours)):
            candidate = copied_layer(
                [contour for current, contour in enumerate(contours) if current != index],
                before.is_quadratic,
            )
            try:
                results.append((f"geometric-drop-contour-{index}", v4.scratch_cleanup(font, name, candidate, .5)))
            except Exception:
                pass
    return results


def fontforge_candidates(font, name, before):
    results = []
    for error in (0, .5, 1.0, 2.0):
        try:
            results.append((f"fontforge-overlap-{error:g}", v4.scratch_cleanup(font, name, before, error)))
        except Exception:
            pass
    return results


def pathops_candidate(source_ttf, name, glyph_dir):
    output_ttf = glyph_dir / "pathops.ttf"
    report_path = glyph_dir / "pathops.json"
    result = subprocess.run(
        [sf.DEFAULT_EXTERNAL_PYTHON, str(ROOT / "tools" / "pathops_cleanup.py"),
         str(source_ttf), name, str(output_ttf), str(report_path)],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    if result.returncode or not output_ttf.exists():
        return None
    cleaned = fontforge.open(str(output_ttf))
    layer = cleaned[name].foreground.dup()
    cleaned.close()
    return ("skia-pathops", layer)


def evaluate(name, before, candidate, regular_layer, row):
    defects = v4.structural(candidate)
    delta = v4.comparison(before, candidate)
    topology = v4.comparison(regular_layer, candidate)
    candidate_components = persistent([topology[size]["right_topology"][0] for size in (128, 256, 512)])
    required = int(row["required_counters"] or 0)
    counter_counts = [topology[size]["right_topology"][1] for size in (128, 256, 512)]
    expected_components = int(row["persistent_regular_components"] or 0)
    iou64, iou128 = delta[64]["iou"], delta[128]["iou"]
    short, reversals = (0, 0)
    spikes = 0
    for contour in candidate:
        anchors = [point for point in contour if point.on_curve]
        pairs = zip(anchors, anchors[1:] + ([anchors[0]] if contour.closed else []))
        short += sum(math.hypot(b.x - a.x, b.y - a.y) < 2 for a, b in pairs)
    accepted = (
        not defects["intersections"] and not defects["open_contours"]
        and not defects["invalid_handles"]
        and candidate_components == expected_components
        and all(value == required for value in counter_counts)
        and iou64 >= .995 and iou128 >= .995
        and short == 0 and spikes == 0
    )
    return {
        "accepted": accepted,
        "defects": defects,
        "iou_64": iou64,
        "iou_128": iou128,
        "components": candidate_components,
        "expected_components": expected_components,
        "counters": counter_counts,
        "required_counters": required,
        "short_segments": short,
        "curvature_reversals": reversals,
        "boundary_spikes": spikes,
        "old_points": point_count(before),
        "new_points": point_count(candidate),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("auto", "fontforge", "pathops", "geometric", "raster"), default="auto")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if CANDIDATE.exists() and not args.force:
        raise SystemExit("Pilot candidate exists; use --force only to rebuild it from frozen v4")
    if not SOURCE.is_file():
        raise SystemExit("Frozen v4 source is missing")
    shutil.copy2(SOURCE, CANDIDATE)
    shutil.copy2(SOURCE, MANUAL_CANDIDATE)
    WORK.mkdir(parents=True, exist_ok=True)
    with TRIAGE.open(encoding="utf-8-sig", newline="") as handle:
        triage = {row["glyph"]: row for row in csv.DictReader(handle)}
    selected = [name for name in PRIORITY if triage[name]["manual_blockers"]]
    if len(selected) > 8:
        raise RuntimeError("Pilot exceeds eight glyphs")
    font = fontforge.open(str(CANDIDATE))
    manual_font = fontforge.open(str(MANUAL_CANDIDATE))
    regular = fontforge.open(str(REGULAR))
    source_ttf = WORK / "source.ttf"
    font.generate(str(source_ttf), flags=("opentype",))
    report_rows = {}
    calibration_rows = calibration(font)
    try:
        for name in selected:
            before = font[name].foreground.dup()
            candidates = []
            if args.backend in ("auto", "fontforge"):
                candidates.extend(fontforge_candidates(font, name, before))
            if args.backend in ("auto", "geometric"):
                candidates.extend(geometric_candidates(font, name, before))
            if args.backend in ("auto", "pathops"):
                glyph_dir = WORK / name
                glyph_dir.mkdir(exist_ok=True)
                pathops = pathops_candidate(source_ttf, name, glyph_dir)
                if pathops:
                    candidates.append(pathops)
            if args.backend in ("auto", "raster"):
                required = int(triage[name]["required_counters"] or 0)
                try:
                    candidates.append(("raster-boundary-refit", raster_backend.reconstruct(before, required)))
                except Exception:
                    pass
            evaluated = []
            for method, candidate in candidates:
                result = evaluate(name, before, candidate, regular[name].foreground, triage[name])
                evaluated.append((method, candidate, result))
            accepted = [item for item in evaluated if item[2]["accepted"]]
            if accepted:
                method, candidate, result = max(accepted, key=lambda item: (min(item[2]["iou_64"], item[2]["iou_128"]), -item[2]["new_points"]))
                font[name].foreground = candidate
                result = dict(result, status="accepted", method=method)
            else:
                viable = [
                    item for item in evaluated
                    if not item[2]["defects"]["intersections"]
                    and not item[2]["defects"]["open_contours"]
                    and not item[2]["defects"]["invalid_handles"]
                    and item[2]["components"] == item[2]["expected_components"]
                    and all(value == item[2]["required_counters"] for value in item[2]["counters"])
                ]
                manual = None
                if viable:
                    manual = max(viable, key=lambda item: (min(item[2]["iou_64"], item[2]["iou_128"]), -item[2]["short_segments"], -item[2]["new_points"]))
                    manual_font[name].foreground = manual[1]
                result = {
                    "status": "blocked",
                    "method": "none",
                    "manual_candidate": dict(manual[2], method=manual[0]) if manual else None,
                    "attempts": [dict(item[2], method=item[0]) for item in evaluated],
                }
            report_rows[name] = result
        font.save(str(CANDIDATE))
        font.generate(str(PILOT_TTF), flags=("opentype",))
        manual_font.save(str(MANUAL_CANDIDATE))
        manual_font.generate(str(MANUAL_TTF), flags=("opentype",))
    finally:
        font.close()
        manual_font.close()
        regular.close()
    payload = {
        "version": "bold-v5-pilot-v1",
        "source": str(SOURCE.relative_to(ROOT)).replace("\\", "/"),
        "candidate": str(CANDIDATE.relative_to(ROOT)).replace("\\", "/"),
        "manual_candidate": str(MANUAL_CANDIDATE.relative_to(ROOT)).replace("\\", "/"),
        "backend": args.backend,
        "selected": selected,
        "accepted": sum(item.get("status") == "accepted" for item in report_rows.values()),
        "blocked": sum(item.get("status") == "blocked" for item in report_rows.values()),
        "calibration": calibration_rows,
        "glyphs": report_rows,
    }
    REPORT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"selected": selected, "accepted": payload["accepted"], "blocked": payload["blocked"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
