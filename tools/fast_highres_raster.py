#!/usr/bin/env python3
"""Rasterize flattened font contours quickly for curvature structural QA."""

import argparse
import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw


def signed_area(points):
    return sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    ) / 2.0


def rasterize(contours, bbox, size):
    left, bottom, right, top = bbox
    width = max(1.0, right - left)
    height = max(1.0, top - bottom)
    transformed = [
        [((x - left) / width * size, (top - y) / height * size) for x, y in contour]
        for contour in contours if len(contour) >= 3
    ]
    if not transformed:
        return bytes(size * size)
    # Match simplify_font.rasterize's non-zero winding rule exactly. Bytearray
    # slice fills make this substantially faster than assigning every pixel.
    mask = bytearray(size * size)
    for row in range(size):
        y = row + .5
        intersections = []
        for contour in transformed:
            for index, start in enumerate(contour):
                end = contour[(index + 1) % len(contour)]
                if (start[1] <= y < end[1]) or (end[1] <= y < start[1]):
                    ratio = (y - start[1]) / (end[1] - start[1])
                    winding = 1 if end[1] > start[1] else -1
                    intersections.append((start[0] + (end[0] - start[0]) * ratio, winding))
        intersections.sort(key=lambda item: item[0])
        winding = 0
        span_start = None
        for x, delta in intersections:
            old = winding; winding += delta
            if old == 0 and winding != 0:
                span_start = x
            elif old != 0 and winding == 0 and span_start is not None:
                x0 = max(0, int(math.ceil(span_start - .5)))
                x1 = min(size, int(math.ceil(x - .5)))
                if x1 > x0:
                    offset = row * size + x0
                    mask[offset:offset + x1 - x0] = b"\x01" * (x1 - x0)
                span_start = None
    return bytes(mask)


def minimum_region_pixels(size):
    return max(2, int(math.ceil((float(size) / 128.0) ** 2)))


def count_regions(mask, size, filled):
    target = 1 if filled else 0
    parents, areas, borders = [], [], []

    def find(value):
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    def union(left, right):
        left, right = find(left), find(right)
        if left == right:
            return left
        if areas[left] < areas[right]:
            left, right = right, left
        parents[right] = left
        areas[left] += areas[right]
        borders[left] = borders[left] or borders[right]
        return left

    previous = []
    for row in range(size):
        runs = []
        offset = row * size
        column = 0
        while column < size:
            value = 1 if mask[offset + column] else 0
            if value != target:
                column += 1
                continue
            start = column
            column += 1
            while column < size and (1 if mask[offset + column] else 0) == target:
                column += 1
            end = column - 1
            identifier = len(parents)
            parents.append(identifier); areas.append(end - start + 1)
            borders.append(row == 0 or row == size - 1 or start == 0 or end == size - 1)
            runs.append([start, end, identifier])
        prior = 0
        for run in runs:
            while prior < len(previous) and previous[prior][1] < run[0]:
                prior += 1
            scan = prior
            while scan < len(previous) and previous[scan][0] <= run[1]:
                run[2] = union(run[2], previous[scan][2]); scan += 1
        previous = runs
    threshold = minimum_region_pixels(size)
    roots = {find(index) for index in range(len(parents))}
    valid = [root for root in roots if areas[root] >= threshold]
    return len(valid), sum(1 for root in valid if borders[root])


def topology(mask, size):
    components, _ = count_regions(mask, size, True)
    empty, border = count_regions(mask, size, False)
    return components, max(0, empty - border)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", nargs="?")
    parser.add_argument("output", nargs="?")
    parser.add_argument("--topology-stdin", action="store_true")
    args = parser.parse_args()
    if args.topology_stdin:
        payload = json.load(sys.stdin)
        values = {}
        for label, contours in payload["sets"].items():
            values[label] = {
                str(size): topology(rasterize(contours, payload["bbox"], size), size)
                for size in payload["sizes"]
            }
        json.dump(values, sys.stdout, separators=(",", ":"))
        return
    if not args.input or not args.output:
        parser.error("input and output are required unless --topology-stdin is used")
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    files = {}
    for label, contours in payload["sets"].items():
        for size in payload["sizes"]:
            path = output / "{}-{}.mask".format(label, size)
            path.write_bytes(rasterize(contours, payload["bbox"], size))
            files["{}:{}".format(label, size)] = str(path)
    (output / "manifest.json").write_text(json.dumps(files), encoding="utf-8")


if __name__ == "__main__":
    main()
