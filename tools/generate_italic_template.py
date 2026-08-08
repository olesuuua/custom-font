#!/usr/bin/env python3
"""Generate the Olesuas Hand italic drawing PDF and cell manifest."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".font-deps"))

from fontTools.pens.basePen import BasePen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont as ReportLabTTFont
from reportlab.pdfgen import canvas

from italic_template_common import (
    A4_HEIGHT,
    A4_WIDTH,
    AUTO_SIDEBEARING,
    CELL_HEIGHT,
    CELL_WIDTH,
    CELLS_PER_PAGE,
    COLS,
    DRAWING_LAYER,
    EMPTY_GLYPHS,
    FONT_X_MAX,
    FONT_X_MIN,
    FONT_Y_MAX,
    FONT_Y_MIN,
    GRID_BOTTOM,
    GRID_TOP,
    ITALIC_ANGLE,
    LABEL_HEIGHT,
    MANIFEST,
    PAGE_COUNT,
    PAGE_MARGIN_X,
    ROWS,
    SOURCE_SFD,
    SOURCE_TTF,
    TEMPLATE_PDF,
    cell_geometry,
    friendly_label,
    semantic_group,
    semantic_sort_key,
    slant_run,
)


class ReportLabPathPen(BasePen):
    def __init__(self, glyph_set, path):
        super().__init__(glyph_set)
        self.path = path

    def _moveTo(self, point):
        self.path.moveTo(*point)

    def _lineTo(self, point):
        self.path.lineTo(*point)

    def _curveToOne(self, control1, control2, point):
        self.path.curveTo(*(control1 + control2 + point))

    def _qCurveToOne(self, control, point):
        start = self._getCurrentPoint()
        c1 = (
            start[0] + (control[0] - start[0]) * 2.0 / 3.0,
            start[1] + (control[1] - start[1]) * 2.0 / 3.0,
        )
        c2 = (
            point[0] + (control[0] - point[0]) * 2.0 / 3.0,
            point[1] + (control[1] - point[1]) * 2.0 / 3.0,
        )
        self.path.curveTo(*(c1 + c2 + point))

    def _closePath(self):
        self.path.close()

    def _endPath(self):
        pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def register_label_font() -> str:
    candidates = (
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for path in candidates:
        if path.exists():
            pdfmetrics.registerFont(ReportLabTTFont("TemplateLabels", str(path)))
            return "TemplateLabels"
    return "Helvetica"


def fit_font_size(text: str, font_name: str, maximum: float, width: float) -> float:
    size = maximum
    while size > 5.2 and pdfmetrics.stringWidth(text, font_name, size) > width:
        size -= 0.25
    return size


def glyph_inventory(font: TTFont) -> tuple[list[dict], list[str]]:
    cmap = font.getBestCmap()
    reverse_cmap = {name: codepoint for codepoint, name in cmap.items()}
    glyph_set = font.getGlyphSet()
    drawable = []
    empty = []
    for order, glyph_name in enumerate(font.getGlyphOrder()):
        pen = BoundsPen(glyph_set)
        glyph_set[glyph_name].draw(pen)
        if pen.bounds is None:
            empty.append(glyph_name)
            continue
        codepoint = reverse_cmap.get(glyph_name)
        group_order, group_name = semantic_group(codepoint, glyph_name)
        drawable.append(
            {
                "glyph_name": glyph_name,
                "codepoint": codepoint,
                "character": chr(codepoint) if codepoint is not None else "",
                "label": friendly_label(codepoint, glyph_name),
                "group": group_name,
                "group_order": group_order,
                "source_order": order,
                "source_bounds": list(pen.bounds),
            }
        )
    drawable.sort(key=lambda item: semantic_sort_key(item["codepoint"], item["glyph_name"]))
    return drawable, empty


def draw_guides(pdf: canvas.Canvas, geometry):
    pdf.saveState()
    clip = pdf.beginPath()
    clip.rect(
        geometry.left + 1,
        geometry.bottom + 1,
        CELL_WIDTH - 2,
        CELL_HEIGHT - LABEL_HEIGHT - 2,
    )
    pdf.clipPath(clip, stroke=0, fill=0)

    metric_colors = {
        FONT_Y_MAX: 0.79,
        0: 0.65,
        FONT_Y_MIN: 0.79,
        512: 0.86,
        730: 0.86,
    }
    for metric, gray in metric_colors.items():
        y = geometry.baseline + metric * geometry.scale
        pdf.setStrokeGray(gray)
        pdf.setLineWidth(0.32 if metric else 0.55)
        pdf.line(geometry.left + 3, y, geometry.right - 3, y)

    pdf.setStrokeGray(0.87)
    pdf.setLineWidth(0.3)
    y0 = geometry.baseline + FONT_Y_MIN * geometry.scale
    y1 = geometry.baseline + FONT_Y_MAX * geometry.scale
    for base_x in (
        geometry.x_origin - 500 * geometry.scale,
        geometry.x_origin,
        geometry.x_origin + 500 * geometry.scale,
        geometry.x_origin + 1000 * geometry.scale,
    ):
        pdf.line(base_x + slant_run(FONT_Y_MIN) * geometry.scale, y0,
                 base_x + slant_run(FONT_Y_MAX) * geometry.scale, y1)
    pdf.restoreState()


def draw_ghost(pdf: canvas.Canvas, glyph_set, glyph_name: str, geometry):
    path = pdf.beginPath()
    glyph_set[glyph_name].draw(ReportLabPathPen(glyph_set, path))
    pdf.saveState()
    clip = pdf.beginPath()
    clip.rect(
        geometry.left + 1,
        geometry.bottom + 1,
        CELL_WIDTH - 2,
        CELL_HEIGHT - LABEL_HEIGHT - 2,
    )
    pdf.clipPath(clip, stroke=0, fill=0)
    pdf.translate(geometry.x_origin, geometry.baseline)
    pdf.scale(geometry.scale, geometry.scale)
    pdf.setFillGray(0.84)
    pdf.setStrokeGray(0.84)
    pdf.drawPath(path, stroke=0, fill=1, fillMode=0)
    pdf.restoreState()


def draw_cell(pdf, label_font, glyph_set, entry, page, row, column):
    geometry = cell_geometry(page, row, column)
    pdf.setStrokeGray(0.72)
    pdf.setLineWidth(0.45)
    pdf.rect(geometry.left, geometry.bottom, CELL_WIDTH, CELL_HEIGHT, stroke=1, fill=0)
    pdf.setStrokeGray(0.84)
    pdf.line(
        geometry.left,
        geometry.top - LABEL_HEIGHT,
        geometry.right,
        geometry.top - LABEL_HEIGHT,
    )
    draw_guides(pdf, geometry)
    draw_ghost(pdf, glyph_set, entry["glyph_name"], geometry)

    label = entry["label"]
    size = fit_font_size(label, label_font, 7.6, CELL_WIDTH - 8)
    pdf.setFillGray(0.35)
    pdf.setFont(label_font, size)
    pdf.drawCentredString(
        (geometry.left + geometry.right) / 2,
        geometry.top - LABEL_HEIGHT + 4.3,
        label,
    )
    return geometry


def draw_instructions(pdf, label_font):
    left = PAGE_MARGIN_X
    right = A4_WIDTH - PAGE_MARGIN_X
    top = GRID_TOP - CELL_HEIGHT - 14
    bottom = GRID_BOTTOM + 10
    pdf.setStrokeGray(0.72)
    pdf.setLineWidth(0.6)
    pdf.roundRect(left, bottom, right - left, top - bottom, 7, stroke=1, fill=0)
    pdf.setFillGray(0.24)
    pdf.setFont(label_font, 13)
    pdf.drawString(left + 14, top - 24, "Xournal++ drawing instructions")
    lines = [
        "1. Open this PDF with File > Annotate PDF.",
        f"2. Rename the top drawing layer exactly: {DRAWING_LAYER}",
        "3. Draw only with a solid black pressure-sensitive pen.",
        "4. Keep every page at its original A4 size and do not move the background.",
        "5. Draw inside each cell; the pale glyph is the upright Regular reference.",
        f"6. Follow the {int(ITALIC_ANGLE)} degree rails for the intended italic slant.",
        "7. Save the journal as .xopp, then export every page as SVG.",
        "8. Return the .xopp and the complete SVG page set together.",
        "",
        "The cleanup tool reads only the named ink layer, cuts cells from fixed coordinates,",
        "reconstructs pressure strokes, simplifies the outlines, and produces FontForge-ready SVGs.",
    ]
    pdf.setFont(label_font, 9.5)
    y = top - 50
    for line in lines:
        pdf.drawString(left + 14, y, line)
        y -= 16


def build_manifest(drawable, empty, source_font: TTFont) -> dict:
    entries = []
    for index, entry in enumerate(drawable):
        page = index // CELLS_PER_PAGE + 1
        position = index % CELLS_PER_PAGE
        row, column = divmod(position, COLS)
        geometry = cell_geometry(page, row, column)
        item = dict(entry)
        item.update(
            index=index,
            page=page,
            row=row,
            column=column,
            cell_pdf={
                "left": geometry.left,
                "bottom": geometry.bottom,
                "right": geometry.right,
                "top": geometry.top,
            },
            cell_xopp={
                "left": geometry.left,
                "top": geometry.xopp_top,
                "right": geometry.right,
                "bottom": geometry.xopp_bottom,
            },
            transform={
                "scale_points_per_font_unit": geometry.scale,
                "pdf_x_origin": geometry.x_origin,
                "pdf_baseline": geometry.baseline,
                "xopp_x_origin": geometry.x_origin,
                "xopp_baseline": geometry.xopp_baseline,
                "font_x_from_xopp": "(x - xopp_x_origin) / scale",
                "font_y_from_xopp": "(xopp_baseline - y) / scale",
            },
            safe_font_bounds={
                "x_min": FONT_X_MIN,
                "y_min": FONT_Y_MIN,
                "x_max": FONT_X_MAX,
                "y_max": FONT_Y_MAX,
            },
        )
        entries.append(item)
    return {
        "format": "olesuas-hand-italic-template-v1",
        "source_sfd": str(SOURCE_SFD.relative_to(ROOT)).replace("\\", "/"),
        "source_ttf": str(SOURCE_TTF.relative_to(ROOT)).replace("\\", "/"),
        "source_sfd_sha256": file_sha256(SOURCE_SFD),
        "source_ttf_sha256": file_sha256(SOURCE_TTF),
        "units_per_em": source_font["head"].unitsPerEm,
        "page": {"width_points": A4_WIDTH, "height_points": A4_HEIGHT, "format": "A4 portrait"},
        "grid": {
            "pages": PAGE_COUNT,
            "columns": COLS,
            "rows": ROWS,
            "cells_per_page": CELLS_PER_PAGE,
            "cell_width_points": CELL_WIDTH,
            "cell_height_points": CELL_HEIGHT,
        },
        "drawing": {
            "layer_name": DRAWING_LAYER,
            "ink": "solid black pressure-sensitive pen",
            "italic_angle_degrees": ITALIC_ANGLE,
            "auto_sidebearing_units": AUTO_SIDEBEARING,
        },
        "drawable_glyph_count": len(entries),
        "empty_glyph_count": len(empty),
        "empty_glyphs": empty,
        "glyphs": entries,
    }


def write_manifest_files(manifest: dict, json_path: Path):
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_path = json_path.with_suffix(".csv")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("index", "page", "row", "column", "group", "label", "glyph_name", "codepoint", "character"))
        for item in manifest["glyphs"]:
            writer.writerow(
                (
                    item["index"], item["page"], item["row"], item["column"], item["group"],
                    item["label"], item["glyph_name"], item["codepoint"] if item["codepoint"] is not None else "",
                    item["character"],
                )
            )


def generate(pdf_path: Path, manifest_path: Path):
    source_font = TTFont(SOURCE_TTF, recalcBBoxes=False, recalcTimestamp=False)
    drawable, empty = glyph_inventory(source_font)
    if len(drawable) != 334:
        raise RuntimeError(f"expected 334 outlined glyphs, found {len(drawable)}")
    if tuple(empty) != EMPTY_GLYPHS:
        raise RuntimeError(f"empty glyph set differs: {empty}")
    manifest = build_manifest(drawable, empty, source_font)
    write_manifest_files(manifest, manifest_path)

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    label_font = register_label_font()
    glyph_set = source_font.getGlyphSet()
    pdf = canvas.Canvas(str(pdf_path), pagesize=(A4_WIDTH, A4_HEIGHT), pageCompression=1)
    pdf.setTitle("Olesuas Hand Italic Drawing Template")
    pdf.setAuthor("Olesya")
    for page in range(1, PAGE_COUNT + 1):
        pdf.setFillGray(0.25)
        pdf.setFont(label_font, 10)
        pdf.drawString(PAGE_MARGIN_X, A4_HEIGHT - 19, "Olesuas Hand Italic - drawing template")
        pdf.drawRightString(A4_WIDTH - PAGE_MARGIN_X, A4_HEIGHT - 19, f"Page {page} / {PAGE_COUNT}")
        page_entries = [item for item in manifest["glyphs"] if item["page"] == page]
        for item in page_entries:
            draw_cell(pdf, label_font, glyph_set, item, page, item["row"], item["column"])
        if page == PAGE_COUNT:
            draw_instructions(pdf, label_font)
        pdf.setFillGray(0.46)
        pdf.setFont(label_font, 6.5)
        pdf.drawString(PAGE_MARGIN_X, 9, "Return: saved .xopp + one SVG export per page")
        pdf.drawRightString(A4_WIDTH - PAGE_MARGIN_X, 9, f"Grid {COLS} x {ROWS} | slant {int(ITALIC_ANGLE)} degrees")
        pdf.showPage()
    pdf.save()
    source_font.close()
    print(json.dumps({"pdf": str(pdf_path), "manifest": str(manifest_path), "glyphs": len(drawable), "pages": PAGE_COUNT}, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, default=TEMPLATE_PDF)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    args = parser.parse_args()
    generate(args.pdf, args.manifest)


if __name__ == "__main__":
    main()
