#!/usr/bin/env fontforge
"""Verify the isolated Italic v10 redraw, fidelity report, and QA wiring."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf
import redraw_italic_v10 as v10


def points(layer):
    return sum(len(contour) for contour in layer)


def main() -> int:
    required = [v10.OUTPUT_SFD, v10.OUTPUT_TTF, v10.REPORT, v10.MANIFEST,
                v10.PROGRESS_METRICS,
                v10.FAMILY_REPORT, v10.DECISIONS, ROOT / "qa" / "italic.html"]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError("missing Italic v10 artifacts: " + " ".join(missing))
    manifest = json.loads(v10.MANIFEST.read_text(encoding="utf-8"))
    with v10.REPORT.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    by_name = {row["glyph"]: row for row in rows}
    source = fontforge.open(str(v10.SOURCE_SFD))
    output = fontforge.open(str(v10.OUTPUT_SFD))
    ttf = fontforge.open(str(v10.OUTPUT_TTF))
    predecessor = fontforge.open(str(v10.V9_BASE_SFD))
    v5 = fontforge.open(str(v10.V5_BASE_SFD))
    errors = []
    try:
        source_names = {glyph.glyphname for glyph in source.glyphs()}
        output_names = {glyph.glyphname for glyph in output.glyphs()}
        ttf_names = {glyph.glyphname for glyph in ttf.glyphs()}
        if output_names != source_names or ttf_names != source_names:
            errors.append("glyph inventory differs from v2")
        source_encoding = sorted((glyph.encoding, glyph.glyphname) for glyph in source.glyphs())
        output_encoding = sorted((glyph.encoding, glyph.glyphname) for glyph in output.glyphs())
        if output_encoding != source_encoding:
            errors.append("SFD encoding order differs from v2")
        if len(rows) != 334:
            errors.append(f"report has {len(rows)} rows; expected 334")
        for path, expected in manifest["immutable_hashes"].items():
            actual_path = ROOT / path
            if not actual_path.exists() or v10.sha256(actual_path) != expected:
                errors.append("immutable artifact changed: " + path)
        ceilings = dict(v10.POINT_CEILINGS)
        for name, row in by_name.items():
            editable = points(output[name].foreground)
            actual_ttf = points(ttf[name].foreground)
            if editable != int(row["candidate_points"]):
                errors.append(name + " editable point count differs")
            if actual_ttf != int(row["ttf_points"]):
                errors.append(name + " TTF point count differs")
            ceiling = ceilings.get(name, v10.MAX_POINTS)
            if editable > ceiling:
                errors.append(
                    f"{name} exceeds its {ceiling}-point ceiling "
                    f"({editable} editable points)")
            if rf.layer_hash(source[name].foreground) != row["source_hash"]:
                errors.append(name + " v2 source hash differs")
            if rf.layer_hash(output[name].foreground) != row["editable_hash"]:
                errors.append(name + " editable hash differs")
            candidate_svg = Path(row["candidate_svg"])
            if not candidate_svg.exists():
                errors.append(name + " actual candidate SVG is missing")
            if rf.layer_hash(ttf[name].foreground) != row["candidate_hash"]:
                errors.append(name + " actual TTF outline hash differs")
            if row["automatic_pass"] == "true" and int(row["candidate_points"]):
                if row["failure_reasons"] or row["topology_status"] != "match":
                    errors.append(name + " automatic pass has a recorded failure")
                if int(row["self_intersections"]) or int(row["invalid_handles"]):
                    errors.append(name + " automatic pass is structurally invalid")
            if row["family_status"] == "accepted" and row["automatic_pass"] != "true":
                errors.append(name + " accepted family donor did not pass target gates")
        notdef = output[".notdef"]
        if len(notdef.foreground) or int(notdef.width) != 341:
            errors.append(".notdef is not the blank 341-unit Regular space copy")
        if manifest["point_reduction"] < .70:
            errors.append("family point reduction is below 70%")
        if max(
            int(row["candidate_points"])
            - ceilings.get(row["glyph"], v10.MAX_POINTS)
            for row in rows
        ) > 0:
            errors.append("report point ceiling exceeds the per-glyph budgets")
        expected_changed = set(v10.V10_CHANGED_GLYPHS)
        actual_changed = {
            name for name in output_names
            if name in predecessor and
            rf.layer_hash(output[name].foreground) !=
            rf.layer_hash(predecessor[name].foreground)
        }
        if actual_changed != expected_changed:
            errors.append(
                "v10 changed slots differ: " + ",".join(sorted(actual_changed))
            )
        v5_fraction = v5["seveneighths"].foreground
        v10_fraction = output["seveneighths"].foreground
        contour_signature = lambda contour: [
            (round(point.x, 6), round(point.y, 6), point.on_curve, point.type)
            for point in contour
        ]
        if len(v10_fraction) != 5:
            errors.append("seveneighths does not have the five-contour inventory")
        elif any(
            contour_signature(v5_fraction[index]) != contour_signature(v10_fraction[index])
            for index in (1, 2)
        ):
            errors.append("seveneighths changed the passed v5 seven or slash")
        for name in ("S",):
            if float(by_name[name]["spacing_cv"]) > .20:
                errors.append(name + " centerline redraw spacing is not uniform")
            if int(by_name[name]["self_intersections"]):
                errors.append(name + " centerline redraw self-intersects")
        if int(by_name["uni0416"]["corner_points"]) < 8:
            errors.append("uni0416 junction exception lacks protected corner structure")
        if int(by_name["uni0416"]["candidate_points"]) > 100:
            errors.append("uni0416 was not returned to the 100-point limit")
        if int(by_name["uni0446"]["candidate_points"]) > int(by_name["uni0446"]["source_points"]):
            errors.append("uni0446 exceeds its source point count")
        if by_name["uni042A"]["topology_status"] != "match":
            errors.append("uni042A protected top-detail topology differs")
        progress = json.loads(v10.PROGRESS_METRICS.read_text(encoding="utf-8"))
        if set(progress.get("glyphs", {})) != expected_changed:
            errors.append("progress metrics do not cover exactly the changed glyphs")
        if progress.get("glyphs", {}).get("uni0416", {}).get("meaningful_progress"):
            errors.append("uni0416 must remain explicitly unresolved")
        decisions = json.loads(v10.DECISIONS.read_text(encoding="utf-8")).get("decisions", {})
        if len(decisions) != 334:
            errors.append("v10 review decisions are incomplete")
        almost = {
            name for name, item in decisions.items()
            if item.get("status") == "almost_done"
        }
        if almost != {"S", "g", "uni0416", "uni0446"}:
            errors.append("v10 almost-done decisions differ from the completed review")
        for name, decision in decisions.items():
            row = by_name.get(name, {})
            if decision.get("source_hash") != row.get("source_hash") \
                    or decision.get("candidate_hash") != row.get("candidate_hash"):
                errors.append(name + " review decision is stale")
        curve_geometry_points = sum(
            int(row["off_curve_points"]) + int(row["curve_points"])
            + int(row["hvcurve_points"]) + int(row["tangent_points"])
            for row in rows
        )
        editable_points = sum(int(row["candidate_points"]) for row in rows)
        if curve_geometry_points / max(1, editable_points) < .70:
            errors.append("less than 70% of editable points participate in curve geometry")
        page = (ROOT / "qa" / "italic.html").read_text(encoding="utf-8")
        server = (ROOT / "tools" / "serve_italic_qa.py").read_text(encoding="utf-8")
        if "v2 source" not in page or "v10 TTF outline" not in page:
            errors.append("QA page is not the required v2/v10 comparison")
        if "italic-v10-glyph-report.csv" not in server or "italic-v10-manual-decisions.json" not in server:
            errors.append("QA server is not wired to the v10 report and decisions")
    finally:
        source.close(); output.close(); ttf.close(); predecessor.close(); v5.close()
    print(json.dumps({
        "glyphs": len(rows),
        "editable_points": sum(int(row["candidate_points"]) for row in rows),
        "maximum_editable_points": max(int(row["candidate_points"]) for row in rows),
        "automatic_passes": sum(row["automatic_pass"] == "true" for row in rows),
        "manual_review_required": sum(row["needs_manual_review"] == "true" for row in rows),
        "accepted_family_reuse": sum(row["family_status"] == "accepted" for row in rows),
        "errors": len(errors),
    }, indent=2))
    for error in errors[:100]:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
