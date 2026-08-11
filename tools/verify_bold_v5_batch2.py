#!/usr/bin/env ffpython
"""Verify Bold v5.1 promotion, indentation audit, and Batch 2 candidate."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf
import repair_bold_v4 as v4
import refresh_bold_qa as metrics

PROMOTED = ROOT / "fontforge" / "bold.sfd"
CANDIDATE = ROOT / "fontforge" / "bold-v5-batch2.sfd"
REPORT = ROOT / "qa" / "assets" / "bold-v5-batch2.json"
AUDIT = ROOT / "qa" / "assets" / "bold-v51-indent-audit.json"
DECISIONS = ROOT / "qa" / "bold-manual-decisions.json"
CHECKPOINT = ROOT / "checkpoints" / "bold-v5" / "batch-01-promoted" / "manifest.json"
EXPECTED = [
    "uni2010", "quoteleft", "quoteright", "quotesinglbase",
    "quotereversed", "quotedblleft", "quotedblright", "quotedblbase",
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def normalized_points(contour):
    x0, y0, x1, y1 = contour.boundingBox()
    width, height = x1 - x0, y1 - y0
    return [
        (
            round((point.x - x0) / max(width, 1e-9), 5),
            round((point.y - y0) / max(height, 1e-9), 5),
            bool(point.on_curve),
        )
        for point in contour
    ]


def main():
    errors = []
    frozen = load(CHECKPOINT)
    for name, path in {
        "redrawn.sfd": ROOT / "fontforge" / "redrawn.sfd",
        "regular-bold-base.sfd": ROOT / "fontforge" / "regular-bold-base.sfd",
        "bold-raw.sfd": ROOT / "fontforge" / "bold-raw.sfd",
        "bold.sfd": PROMOTED,
    }.items():
        if digest(path) != frozen["artifact_hashes"][name]:
            errors.append("promoted/authoritative source changed: " + name)

    audit = load(AUDIT)
    if audit.get("version") != "bold-v51-indent-audit-v1":
        errors.append("audit version differs")
    if audit.get("glyph_count") != 60 or audit.get("sizes") != [128, 256, 512, 1024]:
        errors.append("audit cohort or sizes differ")
    if set(audit.get("glyphs", {})) != {
        name for name, decision in load(DECISIONS)["decisions"].items()
        if decision["status"] == "almost_done"
    }:
        errors.append("audit does not classify the complete Almost Done cohort")
    allowed = {
        "terminal-indent", "boundary-protrusion", "directional-overexpansion",
        "unmatched-white-patch", "canonical-counter", "no-similar-defect",
    }
    for name, row in audit.get("glyphs", {}).items():
        if not row.get("classifications") or not set(row["classifications"]) <= allowed:
            errors.append(name + " audit classification invalid")

    report = load(REPORT)
    if report.get("version") != "bold-v5-batch2-candidate-v2":
        errors.append("candidate report version differs")
    if report.get("selected") != EXPECTED:
        errors.append("candidate membership/order differs")
    if report.get("candidate_ready") != 8 or report.get("candidate_blocked") != 0:
        errors.append("candidate gate totals differ")
    if report.get("source_sha256") != digest(PROMOTED):
        errors.append("candidate source hash differs")
    if report.get("candidate_sha256") != digest(CANDIDATE):
        errors.append("candidate font hash differs")

    source = fontforge.open(str(PROMOTED))
    regular = fontforge.open(str(ROOT / "fontforge" / "redrawn.sfd"))
    candidate = fontforge.open(str(CANDIDATE))
    try:
        source_names = [glyph.glyphname for glyph in source.glyphs()]
        names = [glyph.glyphname for glyph in candidate.glyphs()]
        if source_names != names or len(names) != 340:
            errors.append("candidate glyph order differs")
        changed = []
        for name in names:
            if source[name].unicode != candidate[name].unicode:
                errors.append(name + " cmap changed")
            if source[name].width != candidate[name].width:
                errors.append(name + " width changed")
            if rf.layer_hash(source[name].foreground) != rf.layer_hash(candidate[name].foreground):
                changed.append(name)
        if sorted(changed) != sorted(EXPECTED):
            errors.append("unexpected candidate changes: " + ",".join(changed))

        for name in EXPECTED:
            item = report["glyphs"][name]
            defects = v4.structural(candidate[name].foreground)
            short, _ = metrics.smoothness_metrics(candidate[name].foreground)
            spikes, _ = metrics.spike_metrics(candidate[name].foreground)
            if any(defects[key] for key in ("intersections", "open_contours", "invalid_handles")):
                errors.append(name + " structural defects")
            if (
                short > item.get("inherited_short_segments", 0)
                or spikes > item.get("inherited_boundary_spikes", 0)
                or item.get("unmatched_white_regions")
            ):
                errors.append(name + " new smoothness/white-region defects")
            if any(item["counters_64_128_256_512"]):
                errors.append(name + " unintended counter")

        box = candidate["uni2010"].foreground.boundingBox()
        if candidate["uni2010"].width != 431:
            errors.append("uni2010 advance differs")
        source_box = source["uni2010"].foreground.boundingBox()
        regular_box = regular["uni2010"].foreground.boundingBox()
        if abs((box[2] - box[0]) - (regular_box[2] - regular_box[0])) > 0.1:
            errors.append("uni2010 did not preserve Regular ink length")
        if abs((box[3] - box[1]) - ((source_box[3] - source_box[1]) + 40.0)) > 0.2:
            errors.append("uni2010 thickness differs")
        if abs((box[0] + box[2]) / 2.0 - (source_box[0] + source_box[2]) / 2.0) > 0.1:
            errors.append("uni2010 center differs")

        for name in EXPECTED[1:]:
            source_contours = list(source[name].foreground)
            candidate_contours = list(candidate[name].foreground)
            if len(source_contours) != len(candidate_contours):
                errors.append(name + " component count differs from existing Bold")
            item = report["glyphs"][name]
            if item["iou_64"] < 0.99 or item["iou_128"] < 0.99:
                errors.append(name + " no longer preserves the existing Bold silhouette")
            if item["new_points"] > item["old_points"]:
                errors.append(name + " cleanup increased point count")
    finally:
        source.close()
        regular.close()
        candidate.close()

    decisions = load(DECISIONS)
    counts = {}
    for decision in decisions["decisions"].values():
        counts[decision["status"]] = counts.get(decision["status"], 0) + 1
    if counts != {"pass": 226, "almost_done": 60, "needs_rework": 54}:
        errors.append("decision totals changed during candidate build")

    print("glyphs=340; audit=60; batch2=8; ready=8; blockers=0; errors={}".format(
        len(errors)
    ))
    for error in errors:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
