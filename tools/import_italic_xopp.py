#!/usr/bin/env python3
"""Turn a completed Xournal++ italic template into compact glyph SVGs and a font.

Run the full build with FontForge's Python runtime, for example:

    ffpython tools/import_italic_xopp.py drawings.xopp --svg-pages exported-svg

Normal Python may use --extract-only to test XOPP parsing and SVG extraction.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".font-deps"))
PATHOPS_DIRECTORY = ROOT / ".font-deps" / "pathops"
_PATHOPS_DLL_HANDLE = None
if hasattr(os, "add_dll_directory") and PATHOPS_DIRECTORY.exists():
    _PATHOPS_DLL_HANDLE = os.add_dll_directory(str(PATHOPS_DIRECTORY))

from fontTools.pens.svgPathPen import SVGPathPen

from italic_template_common import (
    A4_HEIGHT,
    A4_WIDTH,
    AUTO_SIDEBEARING,
    DRAWING_LAYER,
    FONT_X_MAX,
    FONT_X_MIN,
    FONT_Y_MAX,
    FONT_Y_MIN,
    ITALIC_ANGLE,
    MANIFEST,
    PAGE_COUNT,
    SOURCE_SFD,
)


NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
DEFAULT_FFPYTHON = Path(r"C:\Program Files\FontForgeBuilds\bin\ffpython.exe")
pathops = None


def require_pathops():
    global pathops
    if pathops is None:
        import importlib
        pathops = importlib.import_module("pathops")
    return pathops


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def numeric(value: str) -> float:
    match = NUMBER_RE.search(value or "")
    if not match:
        raise ValueError(f"numeric value expected, got {value!r}")
    return float(match.group(0))


def load_xopp(path: Path) -> ET.Element:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return ET.fromstring(raw)


def is_black(color: str) -> bool:
    value = (color or "").strip().lower()
    named = {"black", "#000000", "#000000ff"}
    if value in named:
        return True
    if value.startswith("#") and len(value) in (7, 9):
        try:
            rgb = tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))
        except ValueError:
            return False
        return max(rgb) <= 38
    return False


def parse_points(text: str) -> list[tuple[float, float]]:
    values = [float(item) for item in NUMBER_RE.findall(text or "")]
    if len(values) % 2:
        raise ValueError("stroke has an odd coordinate count")
    return list(zip(values[0::2], values[1::2]))


def parse_widths(value: str, count: int) -> list[float]:
    values = [float(item) for item in NUMBER_RE.findall(value or "")]
    if not values:
        raise ValueError("stroke is missing width data")
    if len(values) == 1:
        return [max(values[0], 0.01)] * count
    if len(values) == count + 1:
        base = max(values[0], 0.01)
        pressure = values[1:]
        return [max(base * sample, 0.01) for sample in pressure]
    if len(values) == count:
        return [max(sample, 0.01) for sample in values]
    samples = values[1:] if len(values) > 2 else values
    result = []
    for index in range(count):
        position = index * (len(samples) - 1) / max(count - 1, 1)
        left = int(math.floor(position))
        right = min(left + 1, len(samples) - 1)
        fraction = position - left
        result.append(max(samples[left] * (1 - fraction) + samples[right] * fraction, 0.01))
    return result


def page_and_ink_layers(root: ET.Element) -> list[tuple[ET.Element, ET.Element]]:
    pages = [element for element in root.iter() if local_name(element.tag) == "page"]
    selected = []
    for page_number, page in enumerate(pages, 1):
        width = numeric(page.attrib.get("width", "0"))
        height = numeric(page.attrib.get("height", "0"))
        if abs(width - A4_WIDTH) > 1.0 or abs(height - A4_HEIGHT) > 1.0:
            raise ValueError(
                f"page {page_number} is {width:.3f} x {height:.3f} pt; expected unchanged A4"
            )
        layers = [child for child in page if local_name(child.tag) == "layer"]
        ink = [layer for layer in layers if layer.attrib.get("name", "").strip() == DRAWING_LAYER]
        if len(ink) != 1:
            names = [layer.attrib.get("name", "") for layer in layers]
            raise ValueError(
                f"page {page_number} needs exactly one layer named {DRAWING_LAYER!r}; found {names}"
            )
        selected.append((page, ink[0]))
    if len(selected) != PAGE_COUNT:
        raise ValueError(f"expected {PAGE_COUNT} Xournal++ pages, found {len(selected)}")
    return selected


def load_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != "olesuas-hand-italic-template-v1":
        raise ValueError("unsupported or missing italic template manifest format")
    if data.get("drawable_glyph_count") != 334 or len(data.get("glyphs", [])) != 334:
        raise ValueError("manifest must contain exactly 334 drawable glyphs")
    return data


def entry_for_point(entries_by_page: dict[int, list[dict]], page: int, x: float, y: float) -> dict | None:
    for entry in entries_by_page[page]:
        cell = entry["cell_xopp"]
        if cell["left"] <= x <= cell["right"] and cell["top"] <= y <= cell["bottom"]:
            return entry
    return None


def transform_stroke(points, widths, entry):
    transform = entry["transform"]
    scale = transform["scale_points_per_font_unit"]
    x_origin = transform["xopp_x_origin"]
    baseline = transform["xopp_baseline"]
    transformed = [((x - x_origin) / scale, (baseline - y) / scale) for x, y in points]
    transformed_widths = [width / scale for width in widths]
    return transformed, transformed_widths


def circle_path(x: float, y: float, radius: float) -> pathops.Path:
    k = 0.5522847498307936
    path = pathops.Path()
    path.moveTo(x + radius, y)
    path.cubicTo(x + radius, y + k * radius, x + k * radius, y + radius, x, y + radius)
    path.cubicTo(x - k * radius, y + radius, x - radius, y + k * radius, x - radius, y)
    path.cubicTo(x - radius, y - k * radius, x - k * radius, y - radius, x, y - radius)
    path.cubicTo(x + k * radius, y - radius, x + radius, y - k * radius, x + radius, y)
    path.close()
    return path


def expanded_stroke(points: list[tuple[float, float]], widths: list[float]) -> list[pathops.Path]:
    if not points:
        return []
    if len(points) == 1:
        return [circle_path(points[0][0], points[0][1], widths[0] / 2)]
    shapes = []
    for index, (start, end) in enumerate(zip(points, points[1:])):
        if math.dist(start, end) < 0.01:
            continue
        segment = pathops.Path()
        segment.moveTo(*start)
        segment.lineTo(*end)
        width = (widths[index] + widths[min(index + 1, len(widths) - 1)]) / 2
        segment.stroke(width, pathops.LineCap.ROUND_CAP, pathops.LineJoin.ROUND_JOIN, 4.0)
        segment.convertConicsToQuads(0.20)
        shapes.append(segment)
    if not shapes:
        shapes.append(circle_path(points[0][0], points[0][1], widths[0] / 2))
    return shapes


def rectangle_path(left: float, bottom: float, right: float, top: float) -> pathops.Path:
    path = pathops.Path()
    path.moveTo(left, bottom)
    path.lineTo(right, bottom)
    path.lineTo(right, top)
    path.lineTo(left, top)
    path.close()
    return path


def union_and_clip(shapes: list[pathops.Path]) -> pathops.Path:
    if not shapes:
        return pathops.Path()
    unioned = pathops.Path()
    pathops.union(shapes, unioned.getPen(), fix_winding=True, keep_starting_points=False)
    clipped = pathops.Path()
    clip = rectangle_path(FONT_X_MIN, FONT_Y_MIN, FONT_X_MAX, FONT_Y_MAX)
    pathops.intersection([unioned], [clip], clipped.getPen(), fix_winding=True, keep_starting_points=False)
    return clipped


def path_to_svg(path: pathops.Path, destination: Path):
    pen = SVGPathPen(None)
    path.draw(pen)
    commands = pen.getCommands()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        "\n".join(
            (
                '<?xml version="1.0" encoding="UTF-8"?>',
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{FONT_X_MIN} {-FONT_Y_MAX} {FONT_X_MAX - FONT_X_MIN} {FONT_Y_MAX - FONT_Y_MIN}">',
                '  <g transform="scale(1,-1)" fill="#000000" stroke="none" fill-rule="nonzero">',
                f'    <path d="{commands}"/>',
                "  </g>",
                "</svg>",
            )
        ),
        encoding="utf-8",
    )


def extract_glyph_svgs(xopp: Path, manifest: dict, output_dir: Path) -> dict:
    require_pathops()
    root = load_xopp(xopp)
    pages = page_and_ink_layers(root)
    entries_by_page = defaultdict(list)
    for entry in manifest["glyphs"]:
        entries_by_page[entry["page"]].append(entry)

    strokes_by_glyph = defaultdict(list)
    warnings = []
    ignored_nonblack = 0
    for page_number, (_, layer) in enumerate(pages, 1):
        for element in layer.iter():
            if local_name(element.tag) != "stroke":
                continue
            if element.attrib.get("tool", "pen") != "pen":
                warnings.append(f"page {page_number}: ignored non-pen object")
                continue
            if not is_black(element.attrib.get("color", "")):
                ignored_nonblack += 1
                continue
            points = parse_points(element.text or "")
            if not points:
                continue
            widths = parse_widths(element.attrib.get("width", ""), len(points))
            centroid_x = sum(point[0] for point in points) / len(points)
            centroid_y = sum(point[1] for point in points) / len(points)
            entry = entry_for_point(entries_by_page, page_number, centroid_x, centroid_y)
            if entry is None:
                warnings.append(
                    f"page {page_number}: ignored black stroke outside drawable cells at {centroid_x:.1f},{centroid_y:.1f}"
                )
                continue
            transformed, transformed_widths = transform_stroke(points, widths, entry)
            strokes_by_glyph[entry["glyph_name"]].append((transformed, transformed_widths))

    records = {}
    for entry in manifest["glyphs"]:
        glyph_name = entry["glyph_name"]
        shapes = []
        for points, widths in strokes_by_glyph.get(glyph_name, []):
            shapes.extend(expanded_stroke(points, widths))
        path = union_and_clip(shapes)
        svg_path = output_dir / f"{entry['index']:03d}-{glyph_name}.svg"
        if shapes:
            path_to_svg(path, svg_path)
        records[glyph_name] = {
            "index": entry["index"],
            "page": entry["page"],
            "row": entry["row"],
            "column": entry["column"],
            "stroke_count": len(strokes_by_glyph.get(glyph_name, [])),
            "svg": str(svg_path.resolve()) if shapes else "",
            "status": "extracted" if shapes else "missing",
        }
    return {
        "glyphs": records,
        "warnings": warnings,
        "ignored_nonblack_strokes": ignored_nonblack,
        "completed_glyphs": sum(record["status"] == "extracted" for record in records.values()),
    }


def natural_key(path: Path):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def validate_svg_pages(directory: Path) -> dict:
    files = sorted(directory.glob("*.svg"), key=natural_key)
    if len(files) != PAGE_COUNT:
        raise ValueError(f"expected {PAGE_COUNT} exported page SVGs, found {len(files)} in {directory}")
    pages = []
    for index, path in enumerate(files, 1):
        root = ET.parse(path).getroot()
        width = numeric(root.attrib.get("width", str(A4_WIDTH)))
        height = numeric(root.attrib.get("height", str(A4_HEIGHT)))
        if abs(width - A4_WIDTH) > 2 or abs(height - A4_HEIGHT) > 2:
            raise ValueError(f"SVG page {index} has unexpected size {width:.2f} x {height:.2f}")
        black_shapes = 0
        for element in root.iter():
            style = element.attrib.get("style", "")
            color = element.attrib.get("stroke", "") or element.attrib.get("fill", "")
            if is_black(color) or "stroke:rgb(0%,0%,0%)" in style or "fill:rgb(0%,0%,0%)" in style:
                black_shapes += 1
        pages.append({"page": index, "file": str(path.resolve()), "black_shape_count": black_shapes})
    return {"pages": pages, "status": "validated"}


def layer_point_count(layer) -> int:
    return sum(len(contour) for contour in layer)


def topology_signature(layer) -> tuple[int, int]:
    closed = sum(1 for contour in layer if contour.closed)
    return len(layer), closed


def simplify_glyph(glyph) -> dict:
    glyph.removeOverlap()
    glyph.correctDirection()
    base = glyph.foreground.dup()
    base_topology = topology_signature(base)
    candidates = []
    for error in (0.5, 0.8, 1.1, 1.5, 2.0, 2.8, 3.8):
        glyph.foreground = base.dup()
        glyph.simplify(error, ("mergelines", "choosehv", "smoothcurves", "setstarttoextremum"))
        glyph.removeOverlap()
        glyph.correctDirection()
        points = layer_point_count(glyph.foreground)
        topology = topology_signature(glyph.foreground)
        candidates.append((error, points, topology, glyph.foreground.dup()))
    matching = [candidate for candidate in candidates if candidate[2] == base_topology]
    preferred = [candidate for candidate in matching if candidate[1] <= 80]
    if preferred:
        chosen = preferred[0]
    elif matching:
        chosen = min(matching, key=lambda candidate: candidate[1])
    else:
        raise ValueError("simplification changed contour topology for every candidate")
    if chosen[1] > 120:
        raise ValueError(f"outline still has {chosen[1]} points after guarded simplification")
    glyph.foreground = chosen[3]
    return {
        "raw_points": layer_point_count(base),
        "points": chosen[1],
        "simplify_error": chosen[0],
        "contours": chosen[2][0],
    }


def build_font(manifest: dict, extraction: dict, output_sfd: Path, output_ttf: Path,
               cleaned_svg_dir: Path, allow_partial: bool) -> dict:
    try:
        import fontforge
        import psMat
    except ImportError as exc:
        raise RuntimeError("font construction must run with FontForge ffpython") from exc

    missing = [name for name, record in extraction["glyphs"].items() if record["status"] == "missing"]
    if missing and not allow_partial:
        raise ValueError(f"{len(missing)} drawable glyphs have no ink; first missing: {missing[:12]}")

    font = fontforge.open(str(SOURCE_SFD))
    reports = {}
    try:
        for lookup in list(font.gpos_lookups):
            font.removeLookup(lookup)
        for entry in manifest["glyphs"]:
            name = entry["glyph_name"]
            record = extraction["glyphs"][name]
            if record["status"] == "missing":
                reports[name] = dict(record, status="preserved-regular-partial")
                continue
            glyph = font[name]
            glyph.clear()
            glyph.importOutlines(record["svg"], scale=False, correctdir=True)
            quality = simplify_glyph(glyph)
            bounds = glyph.boundingBox()
            ink_width = bounds[2] - bounds[0]
            glyph.transform(psMat.translate(AUTO_SIDEBEARING - bounds[0], 0))
            width_value = int(round(ink_width + 2 * AUTO_SIDEBEARING))
            glyph.width = width_value
            cleaned_svg = cleaned_svg_dir / f"{entry['index']:03d}-{name}.svg"
            cleaned_svg.parent.mkdir(parents=True, exist_ok=True)
            # The PathOps SVG is already expanded, unioned, clipped, and standalone.
            # Keep that stable artifact; FontForge's Windows glyph.export() can
            # invalidate the owning font during a long batch.
            shutil.copy2(record["svg"], cleaned_svg)
            reports[name] = dict(
                record,
                status="imported",
                cleaned_svg=str(cleaned_svg.resolve()),
                width=width_value,
                left_sidebearing=AUTO_SIDEBEARING,
                **quality,
            )

        font.fontname = "OlesuasHand-Italic"
        font.familyname = "Olesuas Hand"
        font.fullname = "Olesuas Hand Italic"
        font.weight = "Regular"
        font.italicangle = -ITALIC_ANGLE
        font.os2_weight = 400
        font.macstyle = 2
        font.os2_stylemap = 1
        font.version = "1.001"
        output_sfd.parent.mkdir(parents=True, exist_ok=True)
        output_ttf.parent.mkdir(parents=True, exist_ok=True)
        font.save(str(output_sfd))
        font.generate(str(output_ttf), flags=("opentype",))
    finally:
        font.close()

    from fontTools.ttLib import TTFont

    built = TTFont(output_ttf)
    built["post"].italicAngle = -ITALIC_ANGLE
    built["hhea"].caretSlopeRise = 1000
    built["hhea"].caretSlopeRun = int(round(math.tan(math.radians(ITALIC_ANGLE)) * 1000))
    built.save(output_ttf, reorderTables=False)
    built.close()
    return {"glyphs": reports, "output_sfd": str(output_sfd.resolve()), "output_ttf": str(output_ttf.resolve())}


def run_fontforge_worker(ffpython: Path, manifest_path: Path, extraction_path: Path,
                         work: Path, output_sfd: Path, output_ttf: Path,
                         allow_partial: bool) -> dict:
    if not ffpython.exists():
        raise FileNotFoundError(f"FontForge Python runtime not found: {ffpython}")
    worker_report = work / "fontforge-build-report.json"
    command = [
        str(ffpython), str(Path(__file__).resolve()),
        "--fontforge-worker",
        "--manifest", str(manifest_path),
        "--extraction-report", str(extraction_path),
        "--work", str(work),
        "--output-sfd", str(output_sfd),
        "--output-ttf", str(output_ttf),
        "--fontforge-report", str(worker_report),
    ]
    if allow_partial:
        command.append("--allow-partial")
    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    if completed.returncode:
        raise RuntimeError(
            "FontForge worker failed\nSTDOUT:\n{}\nSTDERR:\n{}".format(completed.stdout, completed.stderr)
        )
    return json.loads(worker_report.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("xopp", type=Path, nargs="?")
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--svg-pages", type=Path)
    parser.add_argument("--work", type=Path, default=ROOT / "output" / "italic-import")
    parser.add_argument("--output-sfd", type=Path, default=ROOT / "fontforge" / "italic.sfd")
    parser.add_argument("--output-ttf", type=Path, default=ROOT / "output" / "font" / "OlesuasHand-Italic.ttf")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--extract-only", action="store_true")
    parser.add_argument("--ffpython", type=Path, default=DEFAULT_FFPYTHON)
    parser.add_argument("--fontforge-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--extraction-report", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--fontforge-report", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    args.work.mkdir(parents=True, exist_ok=True)
    if args.fontforge_worker:
        if not args.extraction_report or not args.fontforge_report:
            raise ValueError("FontForge worker needs --extraction-report and --fontforge-report")
        extraction = json.loads(args.extraction_report.read_text(encoding="utf-8"))
        result = build_font(
            manifest,
            extraction,
            args.output_sfd,
            args.output_ttf,
            args.work / "cleaned-glyph-svg",
            args.allow_partial,
        )
        args.fontforge_report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"fontforge_report": str(args.fontforge_report.resolve())}, indent=2))
        return
    if args.xopp is None:
        raise ValueError("the completed .xopp file is required")
    extraction = extract_glyph_svgs(args.xopp, manifest, args.work / "expanded-glyph-svg")
    report = {"format": "olesuas-hand-italic-import-v1", "extraction": extraction}
    extraction_path = args.work / "italic-extraction-report.json"
    extraction_path.write_text(json.dumps(extraction, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.svg_pages:
        report["svg_cross_check"] = validate_svg_pages(args.svg_pages)
    elif not args.extract_only:
        raise ValueError("--svg-pages is required for a full build")
    if not args.extract_only:
        report["font"] = run_fontforge_worker(
            args.ffpython,
            args.manifest,
            extraction_path,
            args.work,
            args.output_sfd,
            args.output_ttf,
            args.allow_partial,
        )
    report_path = args.work / "italic-import-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path.resolve()), "completed_glyphs": extraction["completed_glyphs"]}, indent=2))


if __name__ == "__main__":
    main()
