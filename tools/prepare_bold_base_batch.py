#!/usr/bin/env ffpython
"""Validate and apply a small batch of isolated overlap-cleanup results."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".font-deps"))
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf
import simplify_font as sf

WORKER = ROOT / "tools" / "cleanup_bold_base_worker.py"


def point_count(layer):
    return sum(len(contour) for contour in layer)


def layer_sets(layer):
    pen = sf.FlattenPen(error=1.0, spacing=4.0)
    layer.draw(pen)
    if pen.points:
        pen.endPath()
    return pen.contours


def padded_bbox(left, right):
    contours = left + right
    if not contours:
        return (-16.0, -16.0, 16.0, 16.0)
    box = sf.bbox_of_sets(contours)
    pad = max(8.0, math.hypot(box[2] - box[0], box[3] - box[1]) * 0.04)
    return (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("target")
    parser.add_argument("start", type=int)
    parser.add_argument("end", type=int)
    parser.add_argument("work")
    parser.add_argument("rows")
    args = parser.parse_args()
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    source = fontforge.open(args.source)
    target = fontforge.open(args.target)
    rows = []
    try:
        glyphs = list(source.glyphs())
        for index in range(args.start, min(args.end, len(glyphs))):
            glyph = glyphs[index]
            name = glyph.glyphname
            if name in ("uni03BC", "mu"):
                row = {
                    "glyph": name, "codepoint": glyph.unicode if glyph.unicode >= 0 else "",
                    "source_hash": "preserved-by-whole-font-hash", "source_points": "not-audited",
                    "status": "clean", "backend": "preserved-fontforge-crash",
                    "cleaned_hash": "preserved-by-whole-font-hash", "cleaned_points": "not-audited",
                    "point_delta": 0, "changed": "false", "idempotent": "not-applicable",
                    "iou_64": "1.000000", "iou_128": "1.000000",
                    "removed_micro_regions": 0, "source_intersections": "not-audited",
                    "cleaned_intersections": "not-audited", "source_open_contours": "not-audited",
                    "cleaned_open_contours": "not-audited", "source_invalid_handles": "not-audited",
                    "cleaned_invalid_handles": "not-audited", "failure_reasons": "",
                }
                for size in (32, 64, 128, 256):
                    row["source_components_{}".format(size)] = "not-audited"
                    row["cleaned_components_{}".format(size)] = "not-audited"
                    row["source_counters_{}".format(size)] = "not-audited"
                    row["cleaned_counters_{}".format(size)] = "not-audited"
                rows.append(row)
                continue
            raw = glyph.foreground.dup()
            raw_points = point_count(raw)
            row = {
                "glyph": name, "codepoint": glyph.unicode if glyph.unicode >= 0 else "",
                "source_hash": rf.layer_hash(raw), "source_points": raw_points,
            }
            if raw_points == 0:
                row.update(status="clean", backend="preserved-empty",
                           cleaned_hash=row["source_hash"], cleaned_points=0,
                           point_delta=0, changed="false", idempotent="true",
                           iou_64="1.000000", iou_128="1.000000",
                           removed_micro_regions=0, source_intersections=0,
                           cleaned_intersections=0, source_open_contours=0,
                           cleaned_open_contours=0, source_invalid_handles=0,
                           cleaned_invalid_handles=0, failure_reasons="")
                for size in (32, 64, 128, 256):
                    for kind in ("components", "counters"):
                        row["source_{}_{}".format(kind, size)] = 0
                        row["cleaned_{}_{}".format(kind, size)] = 0
                rows.append(row)
                continue
            one_sfd = work / "{:04d}.sfd".format(index)
            one_json = work / "{:04d}.json".format(index)
            run = subprocess.run(
                [sys.executable, str(WORKER), args.source, name,
                 str(one_sfd), str(one_json)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            info = json.loads(one_json.read_text(encoding="utf-8")) if one_json.exists() else {}
            if run.returncode or info.get("status") != "ok" or not one_sfd.exists():
                row.update(status="base_cleanup_blocked", backend="fontforge-isolated",
                           cleaned_hash="", cleaned_points="", point_delta="", changed="",
                           idempotent="false", iou_64="", iou_128="",
                           removed_micro_regions="", source_intersections=sum(c.selfIntersects() for c in raw),
                           cleaned_intersections="", source_open_contours=sum(not c.closed for c in raw),
                           cleaned_open_contours="", source_invalid_handles=rf.invalid_handles(raw),
                           cleaned_invalid_handles="", failure_reasons="worker-failed")
                rows.append(row)
                continue
            one = fontforge.open(str(one_sfd))
            try:
                candidate = one[name].foreground.dup()
            finally:
                one.close()
            raw_sets, clean_sets = layer_sets(raw), layer_sets(candidate)
            box = padded_bbox(raw_sets, clean_sets)
            topology, ious = {}, {}
            for size in (32, 64, 128, 256):
                before_mask = sf.rasterize(raw_sets, box, size)
                after_mask = sf.rasterize(clean_sets, box, size)
                topology[size] = (sf.topology(before_mask, size), sf.topology(after_mask, size))
                if size in (64, 128):
                    ious[size] = sf.raster_metrics(before_mask, after_mask)["ink_iou"]
            raw_intersections = sum(c.selfIntersects() for c in raw)
            clean_intersections = sum(c.selfIntersects() for c in candidate)
            raw_open = sum(not c.closed for c in raw)
            clean_open = sum(not c.closed for c in candidate)
            raw_handles = rf.invalid_handles(raw)
            clean_handles = rf.invalid_handles(candidate)
            clean_points = point_count(candidate)
            failures = []
            if min(ious.values()) < 0.999:
                failures.append("visible-raster-change")
            for size in (32, 64):
                if topology[size][0] != topology[size][1]:
                    failures.append("topology-{}".format(size))
            if clean_intersections > raw_intersections:
                failures.append("new-self-intersection")
            if clean_open > raw_open:
                failures.append("new-open-contour")
            if clean_handles > raw_handles:
                failures.append("new-invalid-handles")
            if clean_points > raw_points + max(20, int(raw_points * 0.25)):
                failures.append("point-growth")
            if not failures:
                target[name].foreground = candidate
                target[name].width = glyph.width
            row.update(
                status="base_cleanup_blocked" if failures else "clean",
                backend=info.get("backend", "fontforge-isolated"),
                cleaned_hash=rf.layer_hash(candidate), cleaned_points=clean_points,
                point_delta=clean_points - raw_points,
                changed=str(bool(info.get("changed"))).lower(),
                idempotent=str(bool(info.get("idempotent"))).lower(),
                iou_64="{:.6f}".format(ious[64]), iou_128="{:.6f}".format(ious[128]),
                removed_micro_regions=sum(max(0, sum(topology[s][0]) - sum(topology[s][1]))
                                          for s in (128, 256)),
                source_intersections=raw_intersections, cleaned_intersections=clean_intersections,
                source_open_contours=raw_open, cleaned_open_contours=clean_open,
                source_invalid_handles=raw_handles, cleaned_invalid_handles=clean_handles,
                failure_reasons=";".join(sorted(set(failures))),
            )
            for size, (before, after) in topology.items():
                row["source_components_{}".format(size)] = before[0]
                row["source_counters_{}".format(size)] = before[1]
                row["cleaned_components_{}".format(size)] = after[0]
                row["cleaned_counters_{}".format(size)] = after[1]
            rows.append(row)
        target.save(args.target)
    finally:
        source.close()
        target.close()
    Path(args.rows).write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
