#!/usr/bin/env python3
"""Verify the italic template and exercise the Xournal++ round trip."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".font-deps"))

from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

from import_italic_xopp import (
    DEFAULT_FFPYTHON,
    extract_glyph_svgs,
    run_fontforge_worker,
    structural_compatibility,
    validate_svg_pages,
)
from italic_template_common import (
    A4_HEIGHT,
    A4_WIDTH,
    AUTO_SIDEBEARING,
    DRAWING_LAYER,
    EMPTY_GLYPHS,
    ITALIC_ANGLE,
    MANIFEST,
    PAGE_COUNT,
    SOURCE_TTF,
    TEMPLATE_PDF,
)


def load_and_verify_manifest() -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    errors = []
    glyphs = manifest.get("glyphs", [])
    names = [entry["glyph_name"] for entry in glyphs]
    cells = [(entry["page"], entry["row"], entry["column"]) for entry in glyphs]
    if manifest.get("drawable_glyph_count") != 334 or len(glyphs) != 334:
        errors.append("manifest does not contain 334 drawable glyphs")
    if len(set(names)) != 334:
        errors.append("manifest glyph names are not unique")
    if len(set(cells)) != 334:
        errors.append("manifest page cells are not unique")
    if tuple(manifest.get("empty_glyphs", [])) != EMPTY_GLYPHS:
        errors.append("manifest empty glyph list differs")
    if max(entry["page"] for entry in glyphs) != PAGE_COUNT:
        errors.append("manifest does not span 12 pages")

    # Exact source hashes are historical provenance, not a build invariant.
    # Current family structure may be a strict subset when template-era glyph
    # slots are being restored by the Italic drawings.
    compatibility = structural_compatibility(manifest)
    if compatibility["current_only_glyphs"]:
        errors.append("current source contains glyphs absent from the template")
    if errors:
        raise AssertionError("; ".join(errors))
    return manifest


def verify_pdf():
    if PdfReader is None:
        raise RuntimeError("pypdf is unavailable in this Python runtime")
    reader = PdfReader(TEMPLATE_PDF)
    if len(reader.pages) != PAGE_COUNT:
        raise AssertionError(f"PDF has {len(reader.pages)} pages, expected {PAGE_COUNT}")
    for number, page in enumerate(reader.pages, 1):
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        if abs(width - A4_WIDTH) > 0.02 or abs(height - A4_HEIGHT) > 0.02:
            raise AssertionError(f"PDF page {number} is not exact A4")
    last_text = reader.pages[-1].extract_text() or ""
    if DRAWING_LAYER not in last_text or "Xournal++ drawing instructions" not in last_text:
        raise AssertionError("final PDF page is missing the drawing instructions")


def make_synthetic_xopp(manifest: dict, destination: Path, glyph_count: int = 8):
    root = ET.Element("xournal", creator="Codex synthetic italic test", fileversion="4")
    ET.SubElement(root, "title").text = "Synthetic Olesuas Hand italic import test"
    by_page = {}
    for entry in manifest["glyphs"][:glyph_count]:
        by_page.setdefault(entry["page"], []).append(entry)
    for page_number in range(1, PAGE_COUNT + 1):
        page = ET.SubElement(root, "page", width=f"{A4_WIDTH:.8f}", height=f"{A4_HEIGHT:.8f}")
        ET.SubElement(page, "background", type="solid", color="#ffffffff", style="plain")
        layer = ET.SubElement(page, "layer", name=DRAWING_LAYER)
        for entry in by_page.get(page_number, []):
            transform = entry["transform"]
            x = transform["xopp_x_origin"] + 180 * transform["scale_points_per_font_unit"]
            baseline = transform["xopp_baseline"]
            scale = transform["scale_points_per_font_unit"]
            points = [
                (x, baseline - 100 * scale),
                (x + 90 * scale, baseline - 520 * scale),
                (x + 190 * scale, baseline - 120 * scale),
            ]
            stroke = ET.SubElement(layer, "stroke", tool="pen", color="#000000ff", width="2.4 0.8 1.0 0.9")
            stroke.text = " ".join(f"{coordinate:.5f}" for point in points for coordinate in point)
        if page_number == 1:
            gray = ET.SubElement(layer, "stroke", tool="pen", color="#808080ff", width="2.0")
            gray.text = "10 10 20 20"
    xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    destination.write_bytes(gzip.compress(xml))


def make_synthetic_svg_pages(directory: Path, xopp: Path):
    directory.mkdir(parents=True, exist_ok=True)
    root = ET.fromstring(gzip.decompress(xopp.read_bytes()))
    pages = [element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "page"]
    for page, page_element in enumerate(pages, 1):
        paths = []
        for stroke in (element for element in page_element.iter() if element.tag.rsplit("}", 1)[-1] == "stroke"):
            values = [float(value) for value in (stroke.text or "").split()]
            points = list(zip(values[0::2], values[1::2]))
            x = (min(point[0] for point in points) + max(point[0] for point in points)) / 2
            y = (min(point[1] for point in points) + max(point[1] for point in points)) / 2
            paths.append(f'<path d="M{x-1:.5f} {y-1:.5f}L{x+1:.5f} {y-1:.5f}L{x+1:.5f} {y+1:.5f}L{x-1:.5f} {y+1:.5f}Z"/>')
        (directory / f"page-{page:02d}.svg").write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{A4_WIDTH}pt" height="{A4_HEIGHT}pt" '
            f'viewBox="0 0 {A4_WIDTH} {A4_HEIGHT}">{"".join(paths)}</svg>',
            encoding="utf-8",
        )


def synthetic_round_trip(manifest: dict, full_fontforge: bool) -> dict:
    with tempfile.TemporaryDirectory(prefix="italic-workflow-") as temporary:
        temporary_path = Path(temporary)
        xopp = temporary_path / "synthetic.xopp"
        svg_pages = temporary_path / "svg-pages"
        make_synthetic_xopp(manifest, xopp)
        make_synthetic_svg_pages(svg_pages, xopp)
        extraction = extract_glyph_svgs(xopp, manifest, temporary_path / "work", svg_pages)
        if extraction["completed_glyphs"] != 8:
            raise AssertionError(f"synthetic extraction completed {extraction['completed_glyphs']} glyphs, expected 8")
        validate_svg_pages(svg_pages)
        result = {"completed_glyphs": extraction["completed_glyphs"], "fontforge": "not requested"}
        if full_fontforge:
            output_sfd = temporary_path / "italic-test.sfd"
            output_ttf = temporary_path / "italic-test.ttf"
            extraction_path = temporary_path / "extraction.json"
            extraction_path.write_text(json.dumps(extraction, ensure_ascii=False, indent=2), encoding="utf-8")
            build = run_fontforge_worker(
                DEFAULT_FFPYTHON,
                MANIFEST,
                extraction_path,
                temporary_path,
                output_sfd,
                output_ttf,
                allow_partial=True,
            )
            font = TTFont(output_ttf)
            if len(font.getGlyphOrder()) != 334:
                raise AssertionError("partial synthetic font does not preserve the 334 Regular source slots")
            if font["post"].italicAngle != -ITALIC_ANGLE:
                raise AssertionError("synthetic italic font has the wrong italic angle")
            if font["hhea"].caretSlopeRun != 268:
                raise AssertionError("synthetic italic font has the wrong caret slope")
            if "GPOS" in font:
                raise AssertionError("synthetic italic font unexpectedly retains Regular kerning")
            first_name = manifest["glyphs"][0]["glyph_name"]
            glyph_set = font.getGlyphSet()
            pen = BoundsPen(glyph_set)
            glyph_set[first_name].draw(pen)
            width, left = font["hmtx"][first_name]
            if pen.bounds is None or abs(left - AUTO_SIDEBEARING) > 2:
                raise AssertionError("synthetic italic glyph does not have the auto-fit left sidebearing")
            if abs(width - ((pen.bounds[2] - pen.bounds[0]) + 2 * AUTO_SIDEBEARING)) > 3:
                raise AssertionError("synthetic italic glyph does not have the auto-fit advance width")
            font.close()
            result.update(fontforge="passed", output=build["output_ttf"])
        return result


def verify_review_build(manifest: dict) -> dict:
    review_ttf = ROOT / "output" / "font" / "OlesuasHand-Italic-review.ttf"
    if not review_ttf.exists():
        return {"status": "not built"}
    regular = TTFont(SOURCE_TTF)
    italic = TTFont(review_ttf)
    expected_names = {entry["glyph_name"] for entry in manifest["glyphs"]} | set(manifest["empty_glyphs"])
    if set(italic.getGlyphOrder()) != expected_names:
        raise AssertionError("review font glyph inventory differs from the template family inventory")
    glyph_set = italic.getGlyphSet()
    report_path = ROOT / "output" / "italic-import" / "italic-glyph-report.csv"
    with report_path.open(encoding="utf-8-sig", newline="") as handle:
        report = {row["glyph"]: row for row in csv.DictReader(handle)}
    if len(report) != 334:
        raise AssertionError("geometry report does not cover all 334 review slots")
    outlined = []
    empty = []
    for name in italic.getGlyphOrder():
        pen = BoundsPen(glyph_set)
        glyph_set[name].draw(pen)
        (outlined if pen.bounds else empty).append(name)
        row = report.get(name)
        if row and pen.bounds and row["bounds"] not in ("", "[]"):
            source_bounds = json.loads(row["bounds"])
            source_width = source_bounds[2] - source_bounds[0]
            actual_width = pen.bounds[2] - pen.bounds[0]
            if abs(source_width - actual_width) > 2.5 or abs(source_bounds[1] - pen.bounds[1]) > 2.5 or abs(source_bounds[3] - pen.bounds[3]) > 2.5:
                raise AssertionError(f"{name} changed envelope during the TTF round trip")
    if len(outlined) != 334 or set(empty) != set(manifest["empty_glyphs"]):
        raise AssertionError(f"review outline inventory is {len(outlined)} outlined / {len(empty)} empty")
    for field in ("ascent", "descent", "lineGap"):
        if getattr(italic["hhea"], field) != getattr(regular["hhea"], field):
            raise AssertionError(f"review hhea {field} differs from Regular")
    for field in ("sTypoAscender", "sTypoDescender", "sTypoLineGap", "usWinAscent", "usWinDescent"):
        if getattr(italic["OS/2"], field) != getattr(regular["OS/2"], field):
            raise AssertionError(f"review OS/2 {field} differs from Regular")
    names = {
        name_id: {record.toUnicode() for record in italic["name"].names if record.nameID == name_id}
        for name_id in (1, 2, 4, 6)
    }
    expected_name_values = {
        1: {"Olesuas Hand"}, 2: {"Italic"}, 4: {"Olesuas Hand Italic"}, 6: {"OlesuasHand-Italic"}
    }
    if names != expected_name_values:
        raise AssertionError(f"review font naming is incorrect: {names}")
    if italic["OS/2"].fsSelection & 1 == 0 or italic["OS/2"].fsSelection & 64:
        raise AssertionError("review font Italic/Regular style bits are incorrect")
    if italic["head"].macStyle & 2 == 0 or italic["post"].italicAngle != -ITALIC_ANGLE:
        raise AssertionError("review font macStyle or italicAngle is incorrect")
    if italic["hhea"].caretSlopeRun != 268 or "GPOS" in italic:
        raise AssertionError("review font caret slope or kerning state is incorrect")
    mismatched_topology = [
        name for name, row in report.items()
        if name != ".notdef" and row["canonical_contours"] != row["contours"]
    ]
    if mismatched_topology:
        raise AssertionError(f"TTF contour topology changed: {mismatched_topology[:12]}")
    if any(float(row["normalization_factor"]) != 1.0 for row in report.values()):
        raise AssertionError("automatic whole-glyph thickness normalization is still active")
    for name in ("K", "uni041A"):
        row = report[name]
        if int(row["canonical_counters"]) < 1 or row["canonical_counters"] != row["ttf_counters"]:
            raise AssertionError(f"{name} lost a counter")
    if report["dong"]["canonical_counters"] != report["dong"]["ttf_counters"] or int(report["dong"]["ttf_counters"]) < 1:
        raise AssertionError("dong lost its loop")
    expected_repairs = {
        "at": "v1:continuous-strokes",
        "braceleft": "v1:continuous-joined-strokes",
        "uni212B": "v1:component-thickness",
    }
    if any(report[name]["targeted_adjustment"] != repair for name, repair in expected_repairs.items()):
        raise AssertionError("targeted repair metadata is incomplete")
    if report["uni2015"]["targeted_adjustment"] != "none" or float(report["uni2015"]["thickness_before"]) < 24:
        raise AssertionError("uni2015 should remain exact and is not a thin-source outlier")
    regular.close()
    italic.close()
    return {"status": "passed", "glyphs": 340, "outlined": 334, "empty_controls": 6}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-fontforge", action="store_true")
    args = parser.parse_args()
    manifest = load_and_verify_manifest()
    verify_pdf()
    synthetic = synthetic_round_trip(manifest, args.full_fontforge)
    review_build = verify_review_build(manifest)
    print(json.dumps({
        "status": "passed", "manifest_glyphs": 334, "pdf_pages": PAGE_COUNT,
        "compatibility": structural_compatibility(manifest), "synthetic": synthetic,
        "review_build": review_build,
    }, indent=2))


if __name__ == "__main__":
    main()
