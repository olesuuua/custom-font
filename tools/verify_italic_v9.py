#!/usr/bin/env fontforge
"""Verify the isolated Italic v9 redraw, fidelity report, and QA wiring."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf
import redraw_italic_v9 as v9


def points(layer):
    return sum(len(contour) for contour in layer)


def main() -> int:
    required = [v9.OUTPUT_SFD, v9.OUTPUT_TTF, v9.REPORT, v9.MANIFEST,
                v9.FAMILY_REPORT, v9.DECISIONS, ROOT / "qa" / "italic.html"]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError("missing Italic v9 artifacts: " + " ".join(missing))
    manifest = json.loads(v9.MANIFEST.read_text(encoding="utf-8"))
    with v9.REPORT.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    by_name = {row["glyph"]: row for row in rows}
    source = fontforge.open(str(v9.SOURCE_SFD))
    output = fontforge.open(str(v9.OUTPUT_SFD))
    ttf = fontforge.open(str(v9.OUTPUT_TTF))
    predecessor = fontforge.open(str(v9.V8_BASE_SFD))
    v5 = fontforge.open(str(v9.V5_BASE_SFD))
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
            if not actual_path.exists() or v9.sha256(actual_path) != expected:
                errors.append("immutable artifact changed: " + path)
        for name, row in by_name.items():
            editable = points(output[name].foreground)
            actual_ttf = points(ttf[name].foreground)
            if editable != int(row["candidate_points"]):
                errors.append(name + " editable point count differs")
            if actual_ttf != int(row["ttf_points"]):
                errors.append(name + " TTF point count differs")
            if editable > v9.MAX_POINTS:
                errors.append(name + " exceeds 100 editable points")
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
        if max(int(row["candidate_points"]) for row in rows) > 100:
            errors.append("report point ceiling exceeds 100")
        expected_changed = {
            "Beta", "S", "Theta", "Upsilon", "g", "o", "seveneighths",
            "uni0412", "uni0414", "uni0416", "uni042E", "uni0435",
            "uni0437", "uni0446", "uni044E",
        }
        actual_changed = {
            name for name in output_names
            if name in predecessor and
            rf.layer_hash(output[name].foreground) !=
            rf.layer_hash(predecessor[name].foreground)
        }
        if actual_changed != expected_changed:
            errors.append(
                "v9 changed slots differ: " + ",".join(sorted(actual_changed))
            )
        if rf.layer_hash(output["Beta"].foreground) != rf.layer_hash(output["uni0412"].foreground):
            errors.append("Beta and uni0412 do not share the exact v9 outline")
        v5_fraction = v5["seveneighths"].foreground
        v9_fraction = output["seveneighths"].foreground
        contour_signature = lambda contour: [
            (round(point.x, 6), round(point.y, 6), point.on_curve, point.type)
            for point in contour
        ]
        if any(
            contour_signature(v5_fraction[index]) != contour_signature(v9_fraction[index])
            for index in (1, 2, 3, 4)
        ):
            errors.append("seveneighths changed a v5 contour outside the upper outer 8")
        v5_oncurves = [
            (round(point.x, 6), round(point.y, 6))
            for contour in v5_fraction for point in contour if point.on_curve
        ]
        v9_oncurves = [
            (round(point.x, 6), round(point.y, 6))
            for contour in v9_fraction for point in contour if point.on_curve
        ]
        if v5_oncurves != v9_oncurves:
            errors.append("seveneighths changed a v5 on-curve coordinate")
        for name in ("S",):
            if float(by_name[name]["spacing_cv"]) > .20:
                errors.append(name + " centerline redraw spacing is not uniform")
            if int(by_name[name]["self_intersections"]):
                errors.append(name + " centerline redraw self-intersects")
        if int(by_name["uni0416"]["corner_points"]) < 3:
            errors.append("uni0416 junction has no protected corner structure")
        if by_name["uni042A"]["topology_status"] != "match":
            errors.append("uni042A protected top-detail topology differs")
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
        if "v2 source" not in page or "v9 TTF outline" not in page:
            errors.append("QA page is not the required v2/v9 comparison")
        if "italic-v9-glyph-report.csv" not in server or "italic-v9-manual-decisions.json" not in server:
            errors.append("QA server is not wired to the v9 report and decisions")
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
