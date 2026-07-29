#!/usr/bin/env ffpython
"""Clean and compact one Almost Done Bold glyph in an isolated process."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".font-deps"))
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf
import simplify_font as sf


def points(layer):
    return sum(len(contour) for contour in layer)


def sets(layer):
    pen = sf.FlattenPen(error=1.0, spacing=4.0)
    layer.draw(pen)
    if pen.points:
        pen.endPath()
    return pen.contours


def bbox(left, right):
    contours = left + right
    box = sf.bbox_of_sets(contours)
    pad = max(8.0, math.hypot(box[2] - box[0], box[3] - box[1]) * .04)
    return box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad


def clean_metrics(reference, candidate):
    reference_sets, candidate_sets = sets(reference), sets(candidate)
    shared = bbox(reference_sets, candidate_sets)
    result = {"per_size": {}}
    for size in (64, 128, 256, 512):
        before = sf.rasterize(reference_sets, shared, size)
        after = sf.rasterize(candidate_sets, shared, size)
        result["per_size"][str(size)] = {
            "iou": sf.raster_metrics(before, after)["ink_iou"],
            "before_topology": sf.topology(before, size),
            "after_topology": sf.topology(after, size),
        }
    result.update(
        points=points(candidate),
        intersections=sum(contour.selfIntersects() for contour in candidate),
        open_contours=sum(not contour.closed for contour in candidate),
        invalid_handles=rf.invalid_handles(candidate),
    )
    return result


def candidate_layer(source_font, name, layer, simplify_error=None):
    scratch = fontforge.font()
    scratch.encoding = "UnicodeFull"
    scratch.ascent = source_font.ascent
    scratch.descent = source_font.descent
    source = source_font[name]
    glyph = scratch.createChar(source.unicode, name)
    glyph.width = source.width
    glyph.foreground = layer.dup()
    if simplify_error is None:
        glyph.correctDirection()
        glyph.removeOverlap()
        glyph.correctDirection()
    else:
        glyph.simplify(
            simplify_error,
            ("mergelines", "choosehv", "smoothcurves", "setstarttoextremum"),
        )
        glyph.correctDirection()
    result = glyph.foreground.dup()
    scratch.close()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("bold")
    parser.add_argument("glyph")
    parser.add_argument("output")
    parser.add_argument("report")
    args = parser.parse_args()
    payload = {"glyph": args.glyph, "status": "blocked"}
    font = fontforge.open(args.bold)
    output_font = None
    try:
        source = font[args.glyph]
        original = source.foreground.dup()
        original_points = points(original)
        cleaned = candidate_layer(font, args.glyph, original)
        candidates = [("remove-overlap", cleaned)]
        for error in (.25, .5, .75, 1.0, 1.5, 2.0, 3.0):
            candidates.append((
                "remove-overlap-smooth-{:.2f}".format(error),
                candidate_layer(font, args.glyph, cleaned, error),
            ))
        evaluated = []
        for method, layer in candidates:
            metrics = clean_metrics(original, layer)
            topology_ok = all(
                metrics["per_size"][str(size)]["before_topology"]
                == metrics["per_size"][str(size)]["after_topology"]
                for size in (128, 256, 512)
            )
            iou_gate = min(
                metrics["per_size"]["64"]["iou"],
                metrics["per_size"]["128"]["iou"],
            )
            point_ceiling = max(original_points + 60, int(math.ceil(original_points * 1.6)))
            metrics["accepted"] = (
                metrics["intersections"] == 0
                and metrics["open_contours"] == 0
                and metrics["invalid_handles"] == 0
                and topology_ok
                and iou_gate >= .995
                and metrics["points"] <= point_ceiling
            )
            evaluated.append((method, layer, metrics))
        accepted = [item for item in evaluated if item[2]["accepted"]]
        if accepted:
            method, selected, metrics = min(
                accepted,
                key=lambda item: (
                    item[2]["points"],
                    -min(item[2]["per_size"]["64"]["iou"],
                         item[2]["per_size"]["128"]["iou"]),
                ),
            )
            output_font = fontforge.font()
            output_font.encoding = "UnicodeFull"
            output_font.ascent = font.ascent
            output_font.descent = font.descent
            glyph = output_font.createChar(source.unicode, args.glyph)
            glyph.width = source.width
            glyph.foreground = selected
            output_font.save(args.output)
            payload.update(
                status="ok",
                method=method,
                old_points=original_points,
                new_points=metrics["points"],
                iou_64=metrics["per_size"]["64"]["iou"],
                iou_128=metrics["per_size"]["128"]["iou"],
                candidates=[
                    {"method": item[0], **item[2]} for item in evaluated
                ],
            )
        else:
            payload.update(
                error="no safe compact cleanup candidate",
                old_points=original_points,
                candidates=[
                    {"method": item[0], **item[2]} for item in evaluated
                ],
            )
    except Exception as error:
        payload["error"] = "{}: {}".format(type(error).__name__, error)
    finally:
        if output_font is not None:
            output_font.close()
        font.close()
        Path(args.report).write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    return 0 if payload["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
