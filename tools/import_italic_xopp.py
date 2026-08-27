#!/usr/bin/env python3
"""Turn a completed Xournal++ italic template into compact glyph SVGs and a font.

Run the full build with FontForge's Python runtime, for example:

    ffpython tools/import_italic_xopp.py drawings.xopp --svg-pages exported-svg

Normal Python may use --extract-only to test XOPP parsing and SVG extraction.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
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
from fontTools.pens.transformPen import TransformPen
from fontTools.svgLib.path import parse_path

from fast_highres_raster import rasterize as rasterize_contours
from fast_highres_raster import topology as raster_topology

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
TARGETED_REPAIR_VERSION = "v1"
TARGETED_REPAIRS = {
    "at": {"kind": "continuous-strokes", "targets": None},
    "braceleft": {"kind": "continuous-joined-strokes", "targets": [24.226]},
    "uni212B": {"kind": "component-thickness", "targets": [20.08, 19.12, 22.75, 20.20]},
}

OUTLINE_MODES = ("exact", "xopp-compact", "compare")
COMPACT_METHODS = ("xopp-polyline", "xopp-cubic")
COMPACT_TOLERANCES = (0.35, 0.55, 0.8, 1.15, 1.6, 2.25, 3.2, 4.5, 6.4)
COMPACT_BUDGETS = (80, 100, 120, 160, 240, 360, 500, 750, 1000, 1500)
RASTER_SIZES = (32, 64, 128)
TOPOLOGY_SIZES = (128, 256, 512, 1024)
MAX_MSE = 0.010
MIN_INK_IOU = 0.95
MAX_FALSE_INK = 0.04
MAX_BOUNDARY_P95 = 0.04
MAX_BOUNDARY = 0.10


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
        # Xournal++ commonly drops a layer name when a PDF is annotated.  A
        # single layer is still unambiguous, so accept it and report the
        # fallback from extract_glyph_svgs().
        if not ink and len(layers) == 1 and not layers[0].attrib.get("name", "").strip():
            ink = layers
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
    """Union stroke outlines without clipping their italic overhang.

    The historical name remains for compatibility with the synthetic tests.
    Template-cell assignment already isolates glyphs.  Clipping here damaged
    outlines whose round stroke caps extended beyond the guide rectangle.
    """
    if not shapes:
        return pathops.Path()
    unioned = pathops.Path()
    pathops.union(shapes, unioned.getPen(), fix_winding=True, keep_starting_points=False)
    return unioned


def path_to_svg(path: pathops.Path, destination: Path):
    paths_to_svg([path], destination)


def paths_to_svg(paths: list[pathops.Path], destination: Path):
    commands = []
    for path in paths:
        pen = SVGPathPen(None)
        path.draw(pen)
        if pen.getCommands():
            commands.append(f'    <path d="{pen.getCommands()}"/>')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        "\n".join(
            (
                '<?xml version="1.0" encoding="UTF-8"?>',
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{FONT_X_MIN} {-FONT_Y_MAX} {FONT_X_MAX - FONT_X_MIN} {FONT_Y_MAX - FONT_Y_MIN}">',
                '  <g transform="scale(1,-1)" fill="#000000" stroke="none" fill-rule="nonzero">',
                *commands,
                "  </g>",
                "</svg>",
            )
        ),
        encoding="utf-8",
    )


def svg_path_to_path(commands: str, transform=None) -> pathops.Path:
    result = pathops.Path()
    pen = result.getPen()
    if transform is not None:
        pen = TransformPen(pen, transform)
    parse_path(commands, pen)
    return result


def contour_count(path: pathops.Path) -> int:
    pen = SVGPathPen(None)
    path.draw(pen)
    return len(re.findall(r"[Mm]", pen.getCommands()))


def path_counter_count(path: pathops.Path) -> int:
    contours, current = [], []

    class EndpointPen:
        def moveTo(self, point):
            nonlocal current
            if current:
                contours.append(current)
            current = [point]
        def lineTo(self, point): current.append(point)
        def curveTo(self, *points): current.append(points[-1])
        def qCurveTo(self, *points): current.append(next(point for point in reversed(points) if point is not None))
        def closePath(self):
            nonlocal current
            if current:
                contours.append(current)
                current = []
        def endPath(self): self.closePath()
        def addComponent(self, glyphName, transformation): pass

    path.draw(EndpointPen())
    if current:
        contours.append(current)
    areas = [sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(points, points[1:] + points[:1])
    ) / 2 for points in contours if len(points) >= 3]
    if not areas:
        return 0
    outer_sign = 1 if max(areas, key=lambda value: abs(value)) > 0 else -1
    return sum(1 for area in areas if area * outer_sign < 0)


def variable_width_outline(points: list[tuple[float, float]], widths: list[float]) -> pathops.Path:
    """Expand one complete centreline without adding a cap at every sample."""
    filtered_points = []
    filtered_widths = []
    for point, width in zip(points, widths):
        if filtered_points and math.dist(point, filtered_points[-1]) < 0.01:
            filtered_widths[-1] = width
        else:
            filtered_points.append(point)
            filtered_widths.append(width)
    if len(filtered_points) == 1:
        return circle_path(*filtered_points[0], filtered_widths[0] / 2)
    left, right = [], []
    for index, ((x, y), width) in enumerate(zip(filtered_points, filtered_widths)):
        before = filtered_points[max(index - 1, 0)]
        after = filtered_points[min(index + 1, len(filtered_points) - 1)]
        tx, ty = after[0] - before[0], after[1] - before[1]
        length = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / length, tx / length
        radius = width / 2
        left.append((x + nx * radius, y + ny * radius))
        right.append((x - nx * radius, y - ny * radius))
    body = pathops.Path()
    body.moveTo(*left[0])
    for point in left[1:]:
        body.lineTo(*point)
    for point in reversed(right):
        body.lineTo(*point)
    body.close()
    return union_and_clip([
        body,
        circle_path(*filtered_points[0], filtered_widths[0] / 2),
        circle_path(*filtered_points[-1], filtered_widths[-1] / 2),
    ])


def _point_segment_distance_3d(point, start, end) -> float:
    delta = tuple(end[index] - start[index] for index in range(3))
    length_sq = sum(value * value for value in delta)
    if length_sq <= 1e-12:
        return math.sqrt(sum((point[index] - start[index]) ** 2 for index in range(3)))
    ratio = max(0.0, min(1.0, sum(
        (point[index] - start[index]) * delta[index] for index in range(3)
    ) / length_sq))
    return math.sqrt(sum(
        (point[index] - (start[index] + ratio * delta[index])) ** 2 for index in range(3)
    ))


def pressure_rdp(points, widths, tolerance):
    """RDP in x/y/pressure space, retaining endpoints and pressure extrema."""
    if len(points) <= 2:
        return list(points), list(widths)
    pressure_scale = 0.75
    values = [(point[0], point[1], widths[index] * pressure_scale) for index, point in enumerate(points)]
    keep = {0, len(points) - 1}
    for index in range(1, len(widths) - 1):
        before = widths[index] - widths[index - 1]
        after = widths[index + 1] - widths[index]
        if before * after <= 0 and max(abs(before), abs(after)) >= max(0.15, tolerance * 0.15):
            keep.add(index)

    def reduce(left, right):
        if right <= left + 1:
            return
        distance, split = max(
            (_point_segment_distance_3d(values[index], values[left], values[right]), index)
            for index in range(left + 1, right)
        )
        protected = [index for index in keep if left < index < right]
        if protected:
            split = max(protected, key=lambda index: min(index - left, right - index))
            distance = tolerance + 1
        if distance > tolerance:
            keep.add(split)
            reduce(left, split)
            reduce(split, right)

    reduce(0, len(points) - 1)
    indices = sorted(keep)
    return [points[index] for index in indices], [widths[index] for index in indices]


def _cubic_point(start, control1, control2, end, ratio):
    inverse = 1.0 - ratio
    return (
        inverse ** 3 * start[0] + 3 * inverse * inverse * ratio * control1[0]
        + 3 * inverse * ratio * ratio * control2[0] + ratio ** 3 * end[0],
        inverse ** 3 * start[1] + 3 * inverse * inverse * ratio * control1[1]
        + 3 * inverse * ratio * ratio * control2[1] + ratio ** 3 * end[1],
    )


def _fit_open_segment(points, widths, left, right):
    subset = points[left:right + 1]
    distances = [0.0]
    for first, second in zip(subset, subset[1:]):
        distances.append(distances[-1] + math.dist(first, second))
    total = distances[-1]
    parameters = [value / total for value in distances] if total > 1e-9 else [
        index / max(1, len(subset) - 1) for index in range(len(subset))
    ]

    def unit(vector):
        length = math.hypot(*vector)
        return (vector[0] / length, vector[1] / length) if length > 1e-9 else (1.0, 0.0)

    start, end = points[left], points[right]
    tangent1 = unit((points[min(left + 2, right)][0] - start[0], points[min(left + 2, right)][1] - start[1]))
    tangent2 = unit((points[max(right - 2, left)][0] - end[0], points[max(right - 2, left)][1] - end[1]))
    c00 = c01 = c11 = x0 = x1 = 0.0
    for offset, point in enumerate(subset[1:-1], 1):
        ratio = parameters[offset]
        inverse = 1.0 - ratio
        a1 = (3 * inverse * inverse * ratio * tangent1[0], 3 * inverse * inverse * ratio * tangent1[1])
        a2 = (3 * inverse * ratio * ratio * tangent2[0], 3 * inverse * ratio * ratio * tangent2[1])
        base = (
            start[0] * (inverse ** 3 + 3 * inverse * inverse * ratio)
            + end[0] * (ratio ** 3 + 3 * inverse * ratio * ratio),
            start[1] * (inverse ** 3 + 3 * inverse * inverse * ratio)
            + end[1] * (ratio ** 3 + 3 * inverse * ratio * ratio),
        )
        residual = (point[0] - base[0], point[1] - base[1])
        c00 += a1[0] ** 2 + a1[1] ** 2
        c01 += a1[0] * a2[0] + a1[1] * a2[1]
        c11 += a2[0] ** 2 + a2[1] ** 2
        x0 += a1[0] * residual[0] + a1[1] * residual[1]
        x1 += a2[0] * residual[0] + a2[1] * residual[1]
    determinant = c00 * c11 - c01 * c01
    chord = max(1e-9, math.dist(start, end))
    if abs(determinant) <= 1e-9:
        alpha = beta = chord / 3
    else:
        alpha = (x0 * c11 - x1 * c01) / determinant
        beta = (c00 * x1 - c01 * x0) / determinant
    if not (chord * 0.01 <= alpha <= chord * 3 and chord * 0.01 <= beta <= chord * 3):
        alpha = beta = chord / 3
    control1 = (start[0] + tangent1[0] * alpha, start[1] + tangent1[1] * alpha)
    control2 = (end[0] + tangent2[0] * beta, end[1] + tangent2[1] * beta)
    worst_error, split = 0.0, (left + right) // 2
    for offset, point in enumerate(subset[1:-1], 1):
        ratio = parameters[offset]
        fitted = _cubic_point(start, control1, control2, end, ratio)
        geometry_error = math.dist(point, fitted)
        pressure_error = abs(widths[left + offset] - (
            widths[left] + (widths[right] - widths[left]) * ratio
        )) * 0.75
        error = max(geometry_error, pressure_error)
        if error > worst_error:
            worst_error, split = error, left + offset
    return (start, control1, control2, end), worst_error, split


def pressure_cubic_samples(points, widths, tolerance):
    """Fit open cubic runs, then sample them for variable-width expansion."""
    if len(points) <= 2:
        return list(points), list(widths)
    segments = []

    def fit(left, right):
        cubic, error, split = _fit_open_segment(points, widths, left, right)
        if error <= tolerance or right <= left + 2:
            segments.append((left, right, cubic))
            return
        fit(left, split)
        fit(split, right)

    fit(0, len(points) - 1)
    output_points, output_widths = [], []
    spacing = max(2.0, tolerance * 2.0)
    for left, right, cubic in segments:
        control_length = sum(math.dist(first, second) for first, second in zip(cubic, cubic[1:]))
        samples = max(2, min(24, int(math.ceil(control_length / spacing))))
        for index in range(samples + 1):
            if output_points and index == 0:
                continue
            ratio = index / samples
            output_points.append(_cubic_point(*cubic, ratio))
            output_widths.append(widths[left] + (widths[right] - widths[left]) * ratio)
    return output_points, output_widths


class _FlattenPathPen:
    def __init__(self, steps=12):
        self.steps = steps
        self.contours = []
        self.current = []
        self.position = None

    def moveTo(self, point):
        if self.current:
            self.contours.append(self.current)
        self.current = [point]
        self.position = point

    def lineTo(self, point):
        self.current.append(point)
        self.position = point

    def curveTo(self, *points):
        start = self.position
        control1, control2, end = points
        for index in range(1, self.steps + 1):
            self.current.append(_cubic_point(start, control1, control2, end, index / self.steps))
        self.position = end

    def qCurveTo(self, *points):
        values = [point for point in points if point is not None]
        if len(values) != 2:
            for point in values:
                self.lineTo(point)
            return
        control, end = values
        start = self.position
        cubic1 = (start[0] + 2 * (control[0] - start[0]) / 3, start[1] + 2 * (control[1] - start[1]) / 3)
        cubic2 = (end[0] + 2 * (control[0] - end[0]) / 3, end[1] + 2 * (control[1] - end[1]) / 3)
        self.curveTo(cubic1, cubic2, end)

    def closePath(self):
        if self.current:
            self.contours.append(self.current)
        self.current = []
        self.position = None

    def endPath(self):
        self.closePath()

    def addComponent(self, *_args):
        raise ValueError("compact paths cannot contain components")


def flattened_contours(path, steps=12):
    pen = _FlattenPathPen(steps)
    path.draw(pen)
    if pen.current:
        pen.endPath()
    return pen.contours


def path_point_count(path) -> int:
    count = 0

    class CountPen:
        def moveTo(self, _point):
            nonlocal count; count += 1
        def lineTo(self, _point):
            nonlocal count; count += 1
        def curveTo(self, *points):
            nonlocal count; count += len(points)
        def qCurveTo(self, *points):
            nonlocal count; count += len([point for point in points if point is not None])
        def closePath(self): pass
        def endPath(self): pass
        def addComponent(self, *_args): pass

    path.draw(CountPen())
    return count


def _raster_metrics(reference: bytes, candidate: bytes) -> dict:
    reference_ink = sum(reference)
    candidate_ink = sum(candidate)
    intersection = sum(1 for first, second in zip(reference, candidate) if first and second)
    union = sum(1 for first, second in zip(reference, candidate) if first or second)
    false_positive = sum(1 for first, second in zip(reference, candidate) if not first and second)
    false_negative = sum(1 for first, second in zip(reference, candidate) if first and not second)
    return {
        "mse": (false_positive + false_negative) / max(1, len(reference)),
        "ink_iou": intersection / max(1, union),
        "false_positive_ink": false_positive / max(1, candidate_ink),
        "false_negative_ink": false_negative / max(1, reference_ink),
    }


def _downsample_mask(mask: bytes, source_size: int, target_size: int) -> list[float]:
    factor = source_size // target_size
    area = float(factor * factor)
    return [
        sum(
            mask[(row * factor + dy) * source_size + column * factor + dx]
            for dy in range(factor) for dx in range(factor)
        ) / area
        for row in range(target_size) for column in range(target_size)
    ]


def _boundary_pixels(mask: bytes, size: int) -> list[int]:
    result = []
    for row in range(size):
        for column in range(size):
            offset = row * size + column
            if not mask[offset]:
                continue
            if row in (0, size - 1) or column in (0, size - 1) or any(
                not mask[(row + dy) * size + column + dx]
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1))
            ):
                result.append(offset)
    return result


def _distance_to_boundary(source: list[int], target: list[int], size: int) -> list[float]:
    if not source:
        return []
    if not target:
        return [1.0] * len(source)
    # Chamfer distance transform, normalized to the fixed comparison viewport.
    infinity = size * 4.0
    distances = [infinity] * (size * size)
    for offset in target:
        distances[offset] = 0.0
    diagonal = math.sqrt(2.0)
    for row in range(size):
        for column in range(size):
            offset = row * size + column
            value = distances[offset]
            if column: value = min(value, distances[offset - 1] + 1)
            if row:
                value = min(value, distances[offset - size] + 1)
                if column: value = min(value, distances[offset - size - 1] + diagonal)
                if column + 1 < size: value = min(value, distances[offset - size + 1] + diagonal)
            distances[offset] = value
    for row in range(size - 1, -1, -1):
        for column in range(size - 1, -1, -1):
            offset = row * size + column
            value = distances[offset]
            if column + 1 < size: value = min(value, distances[offset + 1] + 1)
            if row + 1 < size:
                value = min(value, distances[offset + size] + 1)
                if column: value = min(value, distances[offset + size - 1] + diagonal)
                if column + 1 < size: value = min(value, distances[offset + size + 1] + diagonal)
            distances[offset] = value
    return [distances[offset] / size for offset in source]


def compare_paths(reference, candidate, full_topology=False) -> dict:
    bbox = (FONT_X_MIN, FONT_Y_MIN, FONT_X_MAX, FONT_Y_MAX)
    reference_contours = flattened_contours(reference)
    candidate_contours = flattened_contours(candidate)
    result = {}
    masks = {}
    for size in RASTER_SIZES:
        raster_size = size * 4 if size == 32 else size
        reference_raw = rasterize_contours(reference_contours, bbox, raster_size)
        candidate_raw = rasterize_contours(candidate_contours, bbox, raster_size)
        reference_mask = _downsample_mask(reference_raw, raster_size, size) if raster_size != size else reference_raw
        candidate_mask = _downsample_mask(candidate_raw, raster_size, size) if raster_size != size else candidate_raw
        masks[size] = (reference_mask, candidate_mask)
        for key, value in _raster_metrics(reference_mask, candidate_mask).items():
            result[f"{key}_{size}"] = round(value, 6)
    reference_boundary = _boundary_pixels(masks[128][0], 128)
    candidate_boundary = _boundary_pixels(masks[128][1], 128)
    boundary = sorted(
        _distance_to_boundary(reference_boundary, candidate_boundary, 128)
        + _distance_to_boundary(candidate_boundary, reference_boundary, 128)
    )
    result["boundary_p95"] = round(boundary[min(len(boundary) - 1, int(len(boundary) * .95))], 6) if boundary else 0.0
    result["boundary_max"] = round(boundary[-1], 6) if boundary else 0.0
    topologies = {}
    topology_match = True
    topology_sizes = TOPOLOGY_SIZES if full_topology else (128,)
    for size in topology_sizes:
        reference_topology = raster_topology(rasterize_contours(reference_contours, bbox, size), size)
        candidate_topology = raster_topology(rasterize_contours(candidate_contours, bbox, size), size)
        topologies[str(size)] = {"reference": reference_topology, "candidate": candidate_topology}
        topology_match = topology_match and reference_topology == candidate_topology
    result["topologies"] = topologies
    result["topology_status"] = "match" if topology_match else "mismatch"
    reference_bounds = path_bounds(reference)
    candidate_bounds = path_bounds(candidate)
    tolerance = max(
        8.0,
        (reference_bounds[2] - reference_bounds[0]) * .015,
        (reference_bounds[3] - reference_bounds[1]) * .015,
    )
    result["bounds_drift"] = round(max(abs(first - second) for first, second in zip(reference_bounds, candidate_bounds)), 4)
    result["bounds_status"] = "pass" if result["bounds_drift"] <= tolerance else "fail"
    result["passes"] = bool(
        topology_match and result["bounds_status"] == "pass"
        and all(result[f"mse_{size}"] <= MAX_MSE for size in RASTER_SIZES)
        and all(result[f"ink_iou_{size}"] >= MIN_INK_IOU for size in RASTER_SIZES)
        and all(result[f"false_positive_ink_{size}"] <= MAX_FALSE_INK for size in RASTER_SIZES)
        and all(result[f"false_negative_ink_{size}"] <= MAX_FALSE_INK for size in RASTER_SIZES)
        and result["boundary_p95"] <= MAX_BOUNDARY_P95
        and result["boundary_max"] <= MAX_BOUNDARY
    )
    return result


def compact_budget(points: int) -> int:
    return next((budget for budget in COMPACT_BUDGETS if points <= budget), points)


def build_compact_candidates(strokes, reference, glyph_name, work, index) -> dict:
    started = time.perf_counter()
    candidates = []
    for method in COMPACT_METHODS:
        for tolerance in COMPACT_TOLERANCES:
            shapes = []
            centerline_points = 0
            for points, widths in strokes:
                if method == "xopp-polyline":
                    reduced_points, reduced_widths = pressure_rdp(points, widths, tolerance)
                else:
                    reduced_points, reduced_widths = pressure_cubic_samples(points, widths, tolerance)
                centerline_points += len(reduced_points)
                shapes.append(variable_width_outline(reduced_points, reduced_widths))
            candidate = union_and_clip(shapes)
            points = path_point_count(candidate)
            metrics = compare_paths(reference, candidate, full_topology=False)
            record = {
                "method": method,
                "tolerance": tolerance,
                "centerline_points": centerline_points,
                "final_points": points,
                "selected_budget": compact_budget(points),
                "candidate_bounds": path_bounds(candidate),
                **metrics,
            }
            candidates.append((record, candidate))
    reference_points = path_point_count(reference)
    passing = []
    for record, path in sorted(candidates, key=lambda value: value[0]["final_points"]):
        if not record["passes"]:
            continue
        verified = compare_paths(reference, path, full_topology=True)
        record.update(verified)
        if record["passes"] and record["final_points"] < reference_points:
            passing.append((record, path))
            break
    selected = min(passing, key=lambda value: (value[0]["final_points"], value[0]["boundary_p95"])) if passing else None
    candidate_directory = work / "xopp-candidate-glyph-svg"
    comparison_directory = work / "comparison-glyph-svg"
    comparison_directory.mkdir(parents=True, exist_ok=True)
    summaries = []
    for record, path in candidates:
        # Comparison mode can expose every method/tolerance without making it authoritative.
        summaries.append(record)
    if selected:
        record, path = selected
        destination = candidate_directory / f"{index:03d}-{glyph_name}.svg"
        path_to_svg(path, destination)
        selected_record = dict(record, svg=str(destination.resolve()), fallback_reason="")
    else:
        selected_record = {
            "method": "exact-fallback", "tolerance": 0.0,
            "centerline_points": sum(len(points) for points, _ in strokes),
            "final_points": reference_points, "selected_budget": reference_points,
            "svg": "", "fallback_reason": "no smaller compact candidate passed every fidelity gate",
            "passes": True, "topology_status": "reference", "bounds_drift": 0.0,
            "boundary_p95": 0.0, "boundary_max": 0.0,
            "candidate_bounds": path_bounds(reference),
        }
    selected_record["processing_seconds"] = round(time.perf_counter() - started, 4)
    return {"selected": selected_record, "candidates": summaries}


def constant_width_outline(points: list[tuple[float, float]], width: float) -> pathops.Path:
    """Stroke one complete sampled centreline with one cap at each true end."""
    clean = []
    for point in points:
        if not clean or math.dist(point, clean[-1]) >= 0.01:
            clean.append(point)
    if len(clean) == 1:
        return circle_path(*clean[0], width / 2)
    path = pathops.Path()
    path.moveTo(*clean[0])
    for point in clean[1:]:
        path.lineTo(*point)
    path.stroke(width, pathops.LineCap.ROUND_CAP, pathops.LineJoin.ROUND_JOIN, 4.0)
    path.convertConicsToQuads(0.20)
    return path


def join_stroke_fragments(strokes):
    """Join fragments by closest endpoints, preserving every sampled point."""
    remaining = [(list(points), list(widths)) for points, widths in strokes]
    points, widths = remaining.pop(0)
    while remaining:
        candidates = []
        for index, (other_points, other_widths) in enumerate(remaining):
            candidates.extend([
                (math.dist(points[-1], other_points[0]), index, False, False),
                (math.dist(points[-1], other_points[-1]), index, False, True),
                (math.dist(points[0], other_points[-1]), index, True, False),
                (math.dist(points[0], other_points[0]), index, True, True),
            ])
        _, index, prepend, reverse = min(candidates)
        other_points, other_widths = remaining.pop(index)
        if reverse:
            other_points.reverse()
            other_widths.reverse()
        if prepend:
            points = other_points + points
            widths = other_widths + widths
        else:
            points.extend(other_points)
            widths.extend(other_widths)
    return points, widths


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def path_bounds(path: pathops.Path) -> list[float]:
    return [round(float(value), 4) for value in path.bounds]


def outside_safe_bounds(bounds: list[float]) -> bool:
    return (
        bounds[0] < FONT_X_MIN or bounds[1] < FONT_Y_MIN
        or bounds[2] > FONT_X_MAX or bounds[3] > FONT_Y_MAX
    )


def extract_glyph_svgs(xopp: Path, manifest: dict, work: Path, svg_pages: Path,
                        outline_mode: str = "exact", compact_glyphs=None) -> dict:
    """Match exported SVG paths to XOPP strokes, then build exact canonical glyphs."""
    if outline_mode not in OUTLINE_MODES:
        raise ValueError(f"outline mode must be one of {OUTLINE_MODES}")
    require_pathops()
    root = load_xopp(xopp)
    pages = page_and_ink_layers(root)
    page_files = sorted(svg_pages.glob("*.svg"), key=natural_key)
    if len(page_files) != PAGE_COUNT:
        raise ValueError(f"expected {PAGE_COUNT} exported page SVGs, found {len(page_files)} in {svg_pages}")
    entries_by_page = defaultdict(list)
    for entry in manifest["glyphs"]:
        entries_by_page[entry["page"]].append(entry)

    strokes_by_glyph = defaultdict(list)
    exact_paths_by_glyph = defaultdict(list)
    warnings = []
    for page_number, (_, layer) in enumerate(pages, 1):
        if not layer.attrib.get("name", "").strip():
            warnings.append(
                f"page {page_number}: accepted the sole unnamed layer as {DRAWING_LAYER!r}"
            )
    ignored_nonblack = 0
    outside_cell_points = 0
    matching = []
    for page_number, ((_, layer), page_file) in enumerate(zip(pages, page_files), 1):
        stroke_elements = [element for element in layer.iter() if local_name(element.tag) == "stroke"]
        svg_root = ET.parse(page_file).getroot()
        svg_elements = [element for element in svg_root.iter() if local_name(element.tag) == "path" and element.attrib.get("d")]
        if len(svg_elements) != len(stroke_elements):
            raise ValueError(
                f"page {page_number}: {len(stroke_elements)} XOPP strokes but {len(svg_elements)} exported SVG paths"
            )
        page_max_delta = 0.0
        for stroke_index, (element, svg_element) in enumerate(zip(stroke_elements, svg_elements), 1):
            if element.attrib.get("tool", "pen") != "pen":
                raise ValueError(f"page {page_number} path {stroke_index}: non-pen object cannot be matched safely")
            points = parse_points(element.text or "")
            if not points:
                raise ValueError(f"page {page_number} path {stroke_index}: empty XOPP stroke")
            widths = parse_widths(element.attrib.get("width", ""), len(points))
            exact_page_path = svg_path_to_path(svg_element.attrib["d"])
            svg_bounds = path_bounds(exact_page_path)
            point_bounds = [
                min(point[0] for point in points), min(point[1] for point in points),
                max(point[0] for point in points), max(point[1] for point in points),
            ]
            delta = max(
                abs((svg_bounds[0] + svg_bounds[2]) / 2 - (point_bounds[0] + point_bounds[2]) / 2),
                abs((svg_bounds[1] + svg_bounds[3]) / 2 - (point_bounds[1] + point_bounds[3]) / 2),
            )
            page_max_delta = max(page_max_delta, delta)
            if delta > 2.0:
                raise ValueError(
                    f"page {page_number} path {stroke_index}: SVG/XOPP bounds centres differ by {delta:.3f} pt"
                )
            if not is_black(element.attrib.get("color", "")):
                ignored_nonblack += 1
                continue
            centroid_x = sum(point[0] for point in points) / len(points)
            centroid_y = sum(point[1] for point in points) / len(points)
            entry = entry_for_point(entries_by_page, page_number, centroid_x, centroid_y)
            if entry is None:
                warnings.append(
                    f"page {page_number}: ignored black stroke outside drawable cells at {centroid_x:.1f},{centroid_y:.1f}"
                )
                continue
            cell = entry["cell_xopp"]
            escaped = sum(
                not (cell["left"] <= x <= cell["right"] and cell["top"] <= y <= cell["bottom"])
                for x, y in points
            )
            if escaped:
                outside_cell_points += escaped
                warnings.append(
                    f"page {page_number}: {entry['glyph_name']} has {escaped} stroke points outside its cell"
                )
            transformed, transformed_widths = transform_stroke(points, widths, entry)
            strokes_by_glyph[entry["glyph_name"]].append((transformed, transformed_widths))
            transform = entry["transform"]
            scale = transform["scale_points_per_font_unit"]
            exact_font_path = svg_path_to_path(
                svg_element.attrib["d"],
                (1 / scale, 0, 0, -1 / scale,
                 -transform["xopp_x_origin"] / scale, transform["xopp_baseline"] / scale),
            )
            exact_paths_by_glyph[entry["glyph_name"]].append(exact_font_path)
        matching.append({
            "page": page_number, "xopp_strokes": len(stroke_elements),
            "svg_paths": len(svg_elements), "max_bounds_center_delta_pt": round(page_max_delta, 4),
            "status": "matched",
        })

    thicknesses = {
        name: statistics.median(width for _, widths in strokes for width in widths)
        for name, strokes in strokes_by_glyph.items() if strokes
    }
    population = list(thicknesses.values())
    thickness_median = statistics.median(population)
    thickness_mad = statistics.median(abs(value - thickness_median) for value in population)
    lower_threshold = thickness_median - 3 * thickness_mad
    upper_threshold = thickness_median + 3 * thickness_mad

    records = {}
    compact_completed = 0
    compact_total = sum(
        compact_glyphs is None or entry["glyph_name"] in compact_glyphs
        for entry in manifest["glyphs"]
        if entry["glyph_name"] in exact_paths_by_glyph
    ) if outline_mode in ("xopp-compact", "compare") else 0
    for entry in manifest["glyphs"]:
        glyph_name = entry["glyph_name"]
        before = thicknesses.get(glyph_name)
        flagged = before is not None and (before < lower_threshold or before > upper_threshold)
        exact_paths = exact_paths_by_glyph.get(glyph_name, [])
        canonical_path = union_and_clip(exact_paths)
        final_path = canonical_path
        adjustment = "none"
        per_stroke = [statistics.median(widths) for _, widths in strokes_by_glyph.get(glyph_name, [])]
        if glyph_name in TARGETED_REPAIRS and exact_paths:
            repair = TARGETED_REPAIRS[glyph_name]
            strokes = strokes_by_glyph[glyph_name]
            repaired = []
            if repair["kind"] == "continuous-joined-strokes":
                points, widths = join_stroke_fragments(strokes)
                target = repair["targets"][0]
                repaired = [constant_width_outline(points, target)]
            else:
                for stroke_index, (points, widths) in enumerate(strokes):
                    target = repair["targets"][stroke_index] if repair["targets"] else statistics.median(widths)
                    repaired.append(constant_width_outline(points, target))
            final_path = union_and_clip(repaired)
            adjustment = f"{TARGETED_REPAIR_VERSION}:{repair['kind']}"
        source_svg_path = work / "exact-source-glyph-svg" / f"{entry['index']:03d}-{glyph_name}.svg"
        canonical_svg_path = work / "canonical-glyph-svg" / f"{entry['index']:03d}-{glyph_name}.svg"
        svg_path = work / "targeted-overrides" / TARGETED_REPAIR_VERSION / f"{entry['index']:03d}-{glyph_name}.svg" if adjustment != "none" else canonical_svg_path
        if exact_paths:
            paths_to_svg(exact_paths, source_svg_path)
            path_to_svg(canonical_path, canonical_svg_path)
            if adjustment != "none":
                path_to_svg(final_path, svg_path)
        compact = None
        selected_svg = svg_path
        compact_selected_for_scope = compact_glyphs is None or glyph_name in compact_glyphs
        if exact_paths and outline_mode in ("xopp-compact", "compare") and compact_selected_for_scope:
            compact_strokes = strokes_by_glyph.get(glyph_name, [])
            if adjustment != "none":
                repair = TARGETED_REPAIRS[glyph_name]
                if repair["kind"] == "continuous-joined-strokes":
                    joined_points, _ = join_stroke_fragments(compact_strokes)
                    compact_strokes = [(joined_points, [repair["targets"][0]] * len(joined_points))]
                else:
                    compact_strokes = [
                        (points, [repair["targets"][stroke_index] if repair["targets"] else statistics.median(widths)] * len(points))
                        for stroke_index, (points, widths) in enumerate(compact_strokes)
                    ]
            compact = build_compact_candidates(
                compact_strokes, final_path, glyph_name, work, entry["index"]
            )
            compact_completed += 1
            if compact_completed == 1 or compact_completed % 10 == 0 or compact_completed == compact_total:
                print(
                    f"italic compact progress: {compact_completed}/{compact_total}",
                    file=sys.stderr, flush=True,
                )
            compact_selected = compact["selected"]
            if compact_selected.get("svg"):
                if outline_mode == "xopp-compact":
                    selected_svg = Path(compact_selected["svg"])
        bounds = path_bounds(final_path) if exact_paths else []
        records[glyph_name] = {
            "index": entry["index"],
            "codepoint": entry.get("codepoint"),
            "page": entry["page"],
            "row": entry["row"],
            "column": entry["column"],
            "stroke_count": len(strokes_by_glyph.get(glyph_name, [])),
            "source_centerline_points": sum(
                len(points) for points, _ in strokes_by_glyph.get(glyph_name, [])
            ),
            "exact_path_count": len(exact_paths),
            "canonical_contours": contour_count(canonical_path) if exact_paths else 0,
            "exact_points": path_point_count(final_path) if exact_paths else 0,
            "final_contours": contour_count(final_path) if exact_paths else 0,
            "canonical_counters": path_counter_count(canonical_path) if exact_paths else 0,
            "final_counters": path_counter_count(final_path) if exact_paths else 0,
            "source_svg": str(source_svg_path.resolve()) if exact_paths else "",
            "canonical_svg": str(canonical_svg_path.resolve()) if exact_paths else "",
            "svg": str(selected_svg.resolve()) if exact_paths else "",
            "exact_svg": str(svg_path.resolve()) if exact_paths else "",
            "compact_svg": compact["selected"].get("svg", "") if compact else "",
            "outline_mode": outline_mode,
            "selected_method": compact["selected"]["method"] if compact else "exact-svg",
            "compact": compact,
            "source_hash": sha256_path(source_svg_path) if exact_paths else "",
            "canonical_hash": sha256_path(canonical_svg_path) if exact_paths else "",
            "candidate_hash": sha256_path(selected_svg) if exact_paths else "",
            "bounds": bounds,
            "candidate_bounds": compact["selected"].get("candidate_bounds", bounds) if compact and outline_mode == "xopp-compact" else bounds,
            "outside_safe_bounds": outside_safe_bounds(bounds) if bounds else False,
            "thickness_before": round(before, 4) if before is not None else None,
            "thickness_after": round(statistics.median(
                TARGETED_REPAIRS[glyph_name]["targets"] or per_stroke
            ), 4) if adjustment != "none" else (round(before, 4) if before is not None else None),
            "thickness_outlier": bool(flagged),
            "normalization_factor": 1.0,
            "per_stroke_thickness": [round(value, 4) for value in per_stroke],
            "targeted_adjustment": adjustment,
            "status": "extracted" if exact_paths else "missing",
        }
    return {
        "outline_mode": outline_mode,
        "compact_scope": sorted(compact_glyphs) if compact_glyphs is not None else "all",
        "glyphs": records,
        "warnings": warnings,
        "ignored_nonblack_strokes": ignored_nonblack,
        "outside_cell_points": outside_cell_points,
        "svg_xopp_matching": matching,
        "thickness": {
            "median": round(thickness_median, 4),
            "mad": round(thickness_mad, 4),
            "lower_threshold": round(lower_threshold, 4),
            "upper_threshold": round(upper_threshold, 4),
            "outlier_count": sum(record["thickness_outlier"] for record in records.values()),
            "automatic_normalization": "disabled",
        },
        "completed_glyphs": sum(record["status"] == "extracted" for record in records.values()),
    }


def compact_comparison_report(extraction: dict) -> dict:
    records = [record for record in extraction["glyphs"].values() if record.get("compact")]
    selected = [record["compact"]["selected"] for record in records]
    compacted = [record for record in selected if record["method"] != "exact-fallback"]
    exact_points = sum(record.get("exact_points", 0) for record in records)
    selected_points = sum(record["final_points"] for record in selected)
    methods = {}
    for method in COMPACT_METHODS:
        candidates = [
            candidate for record in records for candidate in record["compact"]["candidates"]
            if candidate["method"] == method
        ]
        methods[method] = {
            "candidates": len(candidates),
            "passing_candidates": sum(bool(candidate["passes"]) for candidate in candidates),
            "best_selected_glyphs": sum(record["method"] == method for record in selected),
        }
    compact_rate = len(compacted) / max(1, len(records))
    reduction = 1.0 - selected_points / max(1, exact_points)
    return {
        "format": "olesuas-hand-italic-compact-comparison-v1",
        "glyphs": len(records),
        "compacted_glyphs": len(compacted),
        "exact_fallback_glyphs": len(records) - len(compacted),
        "compact_rate": round(compact_rate, 6),
        "exact_points": exact_points,
        "selected_points": selected_points,
        "point_reduction": round(reduction, 6),
        "processing_seconds": round(sum(record.get("processing_seconds", 0) for record in selected), 4),
        "methods": methods,
        "acceptance": {
            "minimum_compact_rate": 0.90,
            "minimum_point_reduction": 0.70,
            "all_selected_candidates_pass": all(record.get("passes") for record in selected),
            "recommended_for_default": bool(
                compact_rate >= .90 and reduction >= .70
                and all(record.get("passes") for record in selected)
            ),
        },
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
    base_bounds = tuple(float(value) for value in glyph.boundingBox())
    candidates = []
    for error in (0.5, 0.8, 1.1, 1.5, 2.0, 2.8, 3.8):
        glyph.foreground = base.dup()
        glyph.simplify(error, ("mergelines", "choosehv", "smoothcurves", "setstarttoextremum"))
        glyph.removeOverlap()
        glyph.correctDirection()
        points = layer_point_count(glyph.foreground)
        topology = topology_signature(glyph.foreground)
        bounds = tuple(float(value) for value in glyph.boundingBox())
        tolerance = max(8.0, (base_bounds[2] - base_bounds[0]) * 0.015, (base_bounds[3] - base_bounds[1]) * 0.015)
        envelope_safe = all(abs(actual - original) <= tolerance for actual, original in zip(bounds, base_bounds))
        candidates.append((error, points, topology, glyph.foreground.dup(), bounds, envelope_safe))
        if topology == base_topology and envelope_safe and points <= 80:
            break
    matching = [candidate for candidate in candidates if candidate[2] == base_topology and candidate[5]]
    preferred = [candidate for candidate in matching if candidate[1] <= 80]
    if preferred:
        chosen = preferred[0]
    elif matching:
        chosen = min(matching, key=lambda candidate: candidate[1])
    else:
        # Topology is more important than point count. Keep the post-overlap
        # base and send this glyph to manual QA rather than aborting 334 slots.
        chosen = (0.0, layer_point_count(base), base_topology, base.dup(), base_bounds, True)
    glyph.foreground = chosen[3]
    return {
        "raw_points": layer_point_count(base),
        "points": chosen[1],
        "simplify_error": chosen[0],
        "contours": chosen[2][0],
        "topology_status": "guarded-fallback" if chosen[0] == 0 else "preserved",
        "point_budget_warning": chosen[1] > 120,
        "bounds_before_simplify": [round(value, 4) for value in base_bounds],
        "bounds_after_simplify": [round(value, 4) for value in chosen[4]],
    }


def export_final_font_svgs(ttf_path: Path, manifest: dict, reports: dict, destination: Path):
    """Export the outlines actually stored in glyf, after the full TTF round trip."""
    from fontTools.ttLib import TTFont

    font = TTFont(ttf_path, recalcBBoxes=True, recalcTimestamp=False)
    glyph_set = font.getGlyphSet()
    glyf = font["glyf"]
    destination.mkdir(parents=True, exist_ok=True)
    try:
        for entry in manifest["glyphs"]:
            name = entry["glyph_name"]
            if name not in glyph_set:
                continue
            pen = SVGPathPen(glyph_set)
            glyph_set[name].draw(pen)
            commands = pen.getCommands()
            svg_path = destination / f"{entry['index']:03d}-{name}.svg"
            svg_path.write_text("\n".join((
                '<?xml version="1.0" encoding="UTF-8"?>',
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{FONT_X_MIN} {-FONT_Y_MAX} {FONT_X_MAX - FONT_X_MIN} {FONT_Y_MAX - FONT_Y_MIN}">',
                '  <g transform="scale(1,-1)" fill="#000000" stroke="none" fill-rule="nonzero">',
                f'    <path d="{commands}"/>', "  </g>", "</svg>",
            )), encoding="utf-8")
            glyph = glyf[name]
            contour_total = glyph.numberOfContours if not glyph.isComposite() else -1
            ttf_counters = 0
            if contour_total > 0:
                coordinates, end_points, _ = glyph.getCoordinates(glyf)
                start = 0
                areas = []
                for end in end_points:
                    points = list(coordinates[start:end + 1])
                    start = end + 1
                    areas.append(sum(
                        first[0] * second[1] - second[0] * first[1]
                        for first, second in zip(points, points[1:] + points[:1])
                    ) / 2)
                outer_sign = 1 if max(areas, key=lambda value: abs(value)) > 0 else -1
                ttf_counters = sum(1 for area in areas if area * outer_sign < 0)
            report = reports.get(name, {})
            report["final_font_svg"] = str(svg_path.resolve())
            report["candidate_hash"] = sha256_json({
                "final_font_svg": sha256_path(svg_path), "width": font["hmtx"].metrics[name][0]
            })
            report["contours"] = contour_total
            report["ttf_counters"] = ttf_counters
            report["points"] = len(glyph.getCoordinates(glyf)[0]) if contour_total >= 0 else "composite"
            report["topology_status"] = "ttf-round-trip"
    finally:
        font.close()


def build_font(manifest: dict, extraction: dict, output_sfd: Path, output_ttf: Path,
               cleaned_svg_dir: Path, allow_partial: bool) -> dict:
    try:
        import fontforge
        import psMat
    except ImportError as exc:
        raise RuntimeError("font construction must run with FontForge ffpython") from exc

    missing = [name for name, record in extraction["glyphs"].items() if record["status"] == "missing"]
    blocking_missing = [name for name in missing if name != ".notdef"]
    if blocking_missing and not allow_partial:
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
                if name == ".notdef":
                    fallback_hash = hashlib.sha256(str(font[name].foreground).encode("utf-8")).hexdigest()
                    reports[name] = dict(
                        record,
                        status="preserved-regular-notdef",
                        source_hash=fallback_hash,
                        candidate_hash=fallback_hash,
                        width=int(font[name].width),
                        left_sidebearing=int(round(font[name].left_side_bearing)),
                        points=layer_point_count(font[name].foreground),
                        contours=topology_signature(font[name].foreground)[0],
                    )
                    continue
                reports[name] = dict(record, status="preserved-regular-partial")
                continue
            try:
                glyph = font[name]
            except TypeError:
                glyph = font.createChar(entry.get("codepoint", -1) or -1, name)
            glyph.clear()
            glyph.importOutlines(record["svg"], scale=False, correctdir=False)
            # FontForge's SVG importer anchors the viewBox at the font's typo
            # ascender on Windows. Re-align the imported outline to the
            # manifest-space lower bound so the drawing baseline is retained.
            imported_bounds = glyph.boundingBox()
            import_y_correction = record.get("candidate_bounds", record["bounds"])[1] - imported_bounds[1]
            glyph.transform(psMat.translate(0, import_y_correction))
            quality = {
                "raw_points": layer_point_count(glyph.foreground),
                "points": layer_point_count(glyph.foreground),
                "simplify_error": 0.0,
                "contours": topology_signature(glyph.foreground)[0],
                "topology_status": "exact-no-simplification",
                "point_budget_warning": False,
                "bounds_before_simplify": [round(float(value), 4) for value in glyph.boundingBox()],
                "bounds_after_simplify": [round(float(value), 4) for value in glyph.boundingBox()],
            }
            bounds = glyph.boundingBox()
            ink_width = bounds[2] - bounds[0]
            glyph.transform(psMat.translate(AUTO_SIDEBEARING - bounds[0], 0))
            width_value = int(round(ink_width + 2 * AUTO_SIDEBEARING))
            glyph.width = width_value
            cleaned_svg = cleaned_svg_dir / f"{entry['index']:03d}-{name}.svg"
            cleaned_svg.parent.mkdir(parents=True, exist_ok=True)
            # Keep the exact import artifact separately; actual TTF outlines are
            # exported after generation below.
            shutil.copy2(record["svg"], cleaned_svg)
            reports[name] = dict(
                record,
                status="imported",
                cleaned_svg=str(cleaned_svg.resolve()),
                width=width_value,
                left_sidebearing=AUTO_SIDEBEARING,
                right_sidebearing=AUTO_SIDEBEARING,
                import_y_correction=round(import_y_correction, 4),
                candidate_hash=sha256_json({
                    "expanded_svg_hash": record["candidate_hash"],
                    "width": width_value,
                    "import_y_correction": round(import_y_correction, 4),
                    "quality": quality,
                }),
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
    source = TTFont(ROOT / "qa" / "assets" / "redrawn.ttf")
    built["post"].italicAngle = -ITALIC_ANGLE
    built["hhea"].caretSlopeRise = 1000
    built["hhea"].caretSlopeRun = int(round(math.tan(math.radians(ITALIC_ANGLE)) * 1000))
    for field in ("ascent", "descent", "lineGap"):
        setattr(built["hhea"], field, getattr(source["hhea"], field))
    for field in (
        "sTypoAscender", "sTypoDescender", "sTypoLineGap", "usWinAscent", "usWinDescent"
    ):
        setattr(built["OS/2"], field, getattr(source["OS/2"], field))
    built.save(output_ttf, reorderTables=False)
    source.close()
    built.close()
    export_final_font_svgs(output_ttf, manifest, reports, cleaned_svg_dir.parent / "final-font-glyph-svg")
    return {"glyphs": reports, "output_sfd": str(output_sfd.resolve()), "output_ttf": str(output_ttf.resolve())}


def structural_compatibility(manifest: dict) -> dict:
    """Validate stable family structure while treating historical hashes as drift signals."""
    from fontTools.ttLib import TTFont

    source_ttf = ROOT / manifest["source_ttf"]
    font = TTFont(source_ttf, recalcBBoxes=False, recalcTimestamp=False)
    current_order = font.getGlyphOrder()
    current_cmap = font.getBestCmap()
    upm = font["head"].unitsPerEm
    metrics = {
        "hhea_ascent": font["hhea"].ascent,
        "hhea_descent": font["hhea"].descent,
        "hhea_line_gap": font["hhea"].lineGap,
        "typo_ascender": font["OS/2"].sTypoAscender,
        "typo_descender": font["OS/2"].sTypoDescender,
        "win_ascent": font["OS/2"].usWinAscent,
        "win_descent": font["OS/2"].usWinDescent,
    }
    font.close()
    if upm != manifest["units_per_em"]:
        raise ValueError(f"source UPM changed from {manifest['units_per_em']} to {upm}")
    expected = [entry["glyph_name"] for entry in manifest["glyphs"]] + manifest.get("empty_glyphs", [])
    expected_set = set(expected)
    current_set = set(current_order)
    current_only = sorted(current_set - expected_set)
    if current_only:
        raise ValueError(f"current source has glyphs absent from the drawing template: {current_only[:12]}")
    cmap_conflicts = []
    for entry in manifest["glyphs"]:
        codepoint = entry.get("codepoint")
        if codepoint is not None and codepoint in current_cmap and current_cmap[codepoint] != entry["glyph_name"]:
            cmap_conflicts.append((codepoint, current_cmap[codepoint], entry["glyph_name"]))
    if cmap_conflicts:
        raise ValueError(f"source cmap conflicts with template: {cmap_conflicts[:8]}")
    current_sfd = ROOT / manifest["source_sfd"]
    ttf_hash = sha256_path(source_ttf)
    sfd_hash = sha256_path(current_sfd)
    return {
        "status": "compatible-with-drift" if (
            ttf_hash != manifest.get("source_ttf_sha256")
            or sfd_hash != manifest.get("source_sfd_sha256")
        ) else "exact",
        "units_per_em": upm,
        "current_glyph_count": len(current_order),
        "template_family_glyph_count": len(expected_set),
        "template_only_glyphs": sorted(expected_set - current_set),
        "current_only_glyphs": current_only,
        "source_ttf_hash": ttf_hash,
        "template_source_ttf_hash": manifest.get("source_ttf_sha256", ""),
        "source_sfd_hash": sfd_hash,
        "template_source_sfd_hash": manifest.get("source_sfd_sha256", ""),
        "vertical_metrics": metrics,
    }


def write_geometry_report(extraction: dict, font_report: dict, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "index", "glyph", "codepoint", "status", "stroke_count", "exact_path_count", "canonical_contours",
        "final_contours", "canonical_counters", "final_counters", "ttf_counters", "per_stroke_thickness", "targeted_adjustment", "thickness_before", "thickness_after",
        "thickness_outlier", "normalization_factor", "bounds", "outside_safe_bounds",
        "width", "left_sidebearing", "right_sidebearing", "raw_points", "points", "contours",
        "topology_status", "point_budget_warning", "source_hash", "candidate_hash", "source_svg", "candidate_svg",
        "outline_mode", "selected_method", "source_centerline_points", "compact_centerline_points",
        "compact_final_points", "selected_budget", "fit_tolerance", "processing_seconds",
        "fallback_reason", "compact_passes", "compact_topology_status", "bounds_drift",
        "boundary_p95", "boundary_max", "compact_svg", "exact_svg",
    ]
    for size in RASTER_SIZES:
        fields.extend([
            f"mse_{size}", f"ink_iou_{size}", f"false_positive_ink_{size}",
            f"false_negative_ink_{size}",
        ])
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for name, record in sorted(extraction["glyphs"].items(), key=lambda item: item[1]["index"]):
            built = font_report.get("glyphs", {}).get(name, {})
            compact = (record.get("compact") or {}).get("selected", {})
            writer.writerow({
                "index": record["index"], "glyph": name, "codepoint": record.get("codepoint"), "status": built.get("status", record["status"]),
                "stroke_count": record["stroke_count"], "exact_path_count": record.get("exact_path_count", 0),
                "canonical_contours": record.get("canonical_contours", 0), "final_contours": record.get("final_contours", 0),
                "canonical_counters": record.get("canonical_counters", 0), "final_counters": record.get("final_counters", 0),
                "ttf_counters": built.get("ttf_counters", 0),
                "per_stroke_thickness": json.dumps(record.get("per_stroke_thickness", [])),
                "targeted_adjustment": record.get("targeted_adjustment", "none"), "thickness_before": record["thickness_before"],
                "thickness_after": record["thickness_after"], "thickness_outlier": str(record["thickness_outlier"]).lower(),
                "normalization_factor": record["normalization_factor"], "bounds": json.dumps(record["bounds"]),
                "outside_safe_bounds": str(record["outside_safe_bounds"]).lower(), "width": built.get("width", ""),
                "left_sidebearing": built.get("left_sidebearing", ""), "right_sidebearing": built.get("right_sidebearing", ""),
                "raw_points": built.get("raw_points", ""), "points": built.get("points", ""),
                "contours": built.get("contours", ""), "topology_status": built.get("topology_status", "fallback" if name == ".notdef" else ""),
                "point_budget_warning": str(built.get("point_budget_warning", False)).lower(), "source_hash": built.get("source_hash", record["source_hash"]),
                "candidate_hash": built.get("candidate_hash", record["candidate_hash"]), "source_svg": record["source_svg"],
                "candidate_svg": built.get("final_font_svg", record["svg"]),
                "outline_mode": record.get("outline_mode", "exact"),
                "selected_method": record.get("selected_method", "exact-svg"),
                "source_centerline_points": record.get("source_centerline_points", ""),
                "compact_centerline_points": compact.get("centerline_points", ""),
                "compact_final_points": compact.get("final_points", ""),
                "selected_budget": compact.get("selected_budget", ""),
                "fit_tolerance": compact.get("tolerance", ""),
                "processing_seconds": compact.get("processing_seconds", ""),
                "fallback_reason": compact.get("fallback_reason", ""),
                "compact_passes": str(compact.get("passes", "")).lower(),
                "compact_topology_status": compact.get("topology_status", ""),
                "bounds_drift": compact.get("bounds_drift", ""),
                "boundary_p95": compact.get("boundary_p95", ""),
                "boundary_max": compact.get("boundary_max", ""),
                "compact_svg": record.get("compact_svg", ""),
                "exact_svg": record.get("exact_svg", ""),
                **{
                    key: compact.get(key, "")
                    for size in RASTER_SIZES
                    for key in (
                        f"mse_{size}", f"ink_iou_{size}", f"false_positive_ink_{size}",
                        f"false_negative_ink_{size}",
                    )
                },
            })


def finalize_review_font(manifest: dict, decisions_path: Path, review_sfd: Path, review_ttf: Path,
                         final_sfd: Path, final_ttf: Path) -> dict:
    decisions = json.loads(decisions_path.read_text(encoding="utf-8")).get("decisions", {})
    report_path = ROOT / "output" / "italic-import" / "italic-glyph-report.csv"
    with report_path.open(encoding="utf-8-sig", newline="") as handle:
        current = {row["glyph"]: row for row in csv.DictReader(handle)}
    names = [entry["glyph_name"] for entry in manifest["glyphs"]]
    unresolved = [
        name for name in names
        if decisions.get(name, {}).get("status") != "pass"
        or decisions.get(name, {}).get("source_hash") != current.get(name, {}).get("source_hash")
        or decisions.get(name, {}).get("candidate_hash") != current.get(name, {}).get("candidate_hash")
    ]
    if unresolved:
        raise ValueError(f"release blocked: {len(unresolved)} glyphs are not passed; first: {unresolved[:12]}")
    final_sfd.parent.mkdir(parents=True, exist_ok=True)
    final_ttf.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(review_sfd, final_sfd)
    shutil.copy2(review_ttf, final_ttf)
    return {"status": "released", "passed_glyphs": len(names), "sfd": str(final_sfd.resolve()), "ttf": str(final_ttf.resolve())}


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
    parser.add_argument("--outline-mode", choices=OUTLINE_MODES, default="exact")
    parser.add_argument(
        "--compact-glyph", action="append",
        help="Limit compact/compare candidate generation to named glyphs; repeat for diagnostics.",
    )
    parser.add_argument("--work", type=Path, default=ROOT / "output" / "italic-import")
    parser.add_argument("--output-sfd", type=Path, default=ROOT / "fontforge" / "italic-review.sfd")
    parser.add_argument("--output-ttf", type=Path, default=ROOT / "output" / "font" / "OlesuasHand-Italic-review.ttf")
    parser.add_argument("--finalize", action="store_true")
    parser.add_argument("--decisions", type=Path, default=ROOT / "qa" / "italic-manual-decisions.json")
    parser.add_argument("--final-sfd", type=Path, default=ROOT / "fontforge" / "italic.sfd")
    parser.add_argument("--final-ttf", type=Path, default=ROOT / "output" / "font" / "OlesuasHand-Italic.ttf")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--extract-only", action="store_true")
    parser.add_argument("--ffpython", type=Path, default=DEFAULT_FFPYTHON)
    parser.add_argument("--fontforge-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--extraction-report", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--fontforge-report", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    if args.finalize:
        try:
            result = finalize_review_font(
                manifest, args.decisions, args.output_sfd, args.output_ttf, args.final_sfd, args.final_ttf
            )
        except (OSError, ValueError) as error:
            parser.error(str(error))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
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
    compatibility = structural_compatibility(manifest)
    if args.svg_pages is None:
        raise ValueError("--svg-pages is required; exported SVG pages are the authoritative geometry")
    extraction = extract_glyph_svgs(
        args.xopp, manifest, args.work, args.svg_pages, outline_mode=args.outline_mode,
        compact_glyphs=set(args.compact_glyph) if args.compact_glyph else None,
    )
    report = {
        "format": "olesuas-hand-italic-import-v2",
        "compatibility": compatibility,
        "extraction": extraction,
    }
    extraction_path = args.work / "italic-extraction-report.json"
    extraction_path.write_text(json.dumps(extraction, ensure_ascii=False, indent=2), encoding="utf-8")
    comparison = None
    if args.outline_mode in ("xopp-compact", "compare"):
        comparison = compact_comparison_report(extraction)
        comparison_path = args.work / "italic-compact-comparison.json"
        comparison_path.write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
        report["compact_comparison"] = comparison
    report["svg_source"] = validate_svg_pages(args.svg_pages)
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
        write_geometry_report(extraction, report["font"], args.work / "italic-glyph-report.csv")
        if args.work.resolve() == (ROOT / "output" / "italic-import").resolve():
            subprocess.run(
                [sys.executable, str(ROOT / "tools" / "generate_italic_static_qa.py")],
                cwd=str(ROOT), check=True,
            )
        build_manifest = {
            "format": (
                "olesuas-hand-italic-build-v2-exact-svg"
                if args.outline_mode == "exact" else "olesuas-hand-italic-build-v3-xopp-compact"
            ),
            "review_status": "awaiting-manual-review",
            "outline_mode": args.outline_mode,
            "geometry_source": (
                "XOPP centerlines fidelity-gated against 12 exported SVG pages"
                if args.outline_mode == "xopp-compact" else "12 exported SVG pages"
            ),
            "svg_xopp_matching": extraction["svg_xopp_matching"],
            "automatic_thickness_normalization": False,
            "fontforge_overlap_removal": False,
            "simplification": args.outline_mode == "xopp-compact",
            "compact_comparison": comparison,
            "targeted_repair_version": TARGETED_REPAIR_VERSION,
            "targeted_repairs": TARGETED_REPAIRS,
            "glyph_count": len(manifest["glyphs"]),
            "drawn_glyph_count": extraction["completed_glyphs"],
            "regular_fallback_glyphs": [".notdef"],
            "thickness": extraction["thickness"],
            "compatibility": compatibility,
            "output_sfd": report["font"]["output_sfd"],
            "output_ttf": report["font"]["output_ttf"],
            "geometry_report": str((args.work / "italic-glyph-report.csv").resolve()),
        }
        (args.work / "italic-build-manifest.json").write_text(
            json.dumps(build_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    report_path = args.work / "italic-import-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path.resolve()), "completed_glyphs": extraction["completed_glyphs"]}, indent=2))


if __name__ == "__main__":
    main()
