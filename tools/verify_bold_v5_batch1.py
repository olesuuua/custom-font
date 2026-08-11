#!/usr/bin/env ffpython
"""Verify the isolated Bold v5 Batch 1 candidate and immutable review state."""
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
import refresh_bold_qa as qa

SOURCE = ROOT / "fontforge" / "bold.sfd"
CANDIDATE = ROOT / "fontforge" / "bold-v5-batch1.sfd"
REPORT = ROOT / "qa" / "assets" / "bold-v5-batch1.json"
DECISIONS = ROOT / "qa" / "bold-manual-decisions.json"
BATCH0 = ROOT / "checkpoints" / "bold-v5" / "batch-00-source" / "manifest.json"
EXPECTED = [
    "brokenbar",
    "dagger",
    "uni042F",
    "uni2010",
    "two",
    "minute",
    "second",
    "uni21BA",
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def normalized_points(contour):
    x0, y0, x1, y1 = contour.boundingBox()
    width = x1 - x0
    height = y1 - y0
    return [
        (
            round((point.x - x0) / width, 6),
            round((point.y - y0) / height, 6),
            bool(point.on_curve),
        )
        for point in contour
    ]


def main():
    errors = []
    frozen = load(BATCH0)
    artifact_hashes = frozen["artifact_hashes"]
    guarded = {
        "redrawn.sfd": ROOT / "fontforge" / "redrawn.sfd",
        "regular-bold-base.sfd": ROOT / "fontforge" / "regular-bold-base.sfd",
        "bold-raw.sfd": ROOT / "fontforge" / "bold-raw.sfd",
        "bold.sfd": SOURCE,
    }
    for name, path in guarded.items():
        if digest(path) != artifact_hashes[name]:
            errors.append("authoritative source changed: " + name)

    decisions = load(DECISIONS)
    counts = {}
    for decision in decisions.get("decisions", {}).values():
        status = decision["status"]
        counts[status] = counts.get(status, 0) + 1
    if len(decisions.get("decisions", {})) != 340:
        errors.append("manual decision total changed")
    if counts != {"pass": 219, "almost_done": 67, "needs_rework": 54}:
        errors.append("manual decision counts changed")

    report = load(REPORT)
    if report.get("version") != "bold-v5-batch1-candidate-v1":
        errors.append("candidate report version differs")
    if report.get("selected") != EXPECTED:
        errors.append("candidate membership/order differs")
    if report.get("candidate_ready") != 8 or report.get("candidate_blocked") != 0:
        errors.append("candidate gate totals differ")
    if report.get("source_sha256") != digest(SOURCE):
        errors.append("candidate report source hash differs")
    if report.get("candidate_sha256") != digest(CANDIDATE):
        errors.append("candidate report font hash differs")

    source = fontforge.open(str(SOURCE))
    candidate = fontforge.open(str(CANDIDATE))
    try:
        source_names = [glyph.glyphname for glyph in source.glyphs()]
        candidate_names = [glyph.glyphname for glyph in candidate.glyphs()]
        if source_names != candidate_names or len(source_names) != 340:
            errors.append("glyph order differs")
        changed = []
        for name in source_names:
            if source[name].unicode != candidate[name].unicode:
                errors.append(name + " cmap changed")
            if source[name].width != candidate[name].width:
                errors.append(name + " advance width changed")
            if rf.layer_hash(source[name].foreground) != rf.layer_hash(candidate[name].foreground):
                changed.append(name)
        if sorted(changed) != sorted(EXPECTED):
            errors.append("changed glyph set differs: " + ",".join(changed))

        for name in EXPECTED:
            item = report["glyphs"][name]
            defects = v4.structural(candidate[name].foreground)
            if any(defects[key] for key in ("intersections", "open_contours", "invalid_handles")):
                errors.append(name + " has structural defects")
            short_segments, _ = qa.smoothness_metrics(candidate[name].foreground)
            boundary_spikes, _ = qa.spike_metrics(candidate[name].foreground)
            if short_segments or boundary_spikes:
                errors.append(name + " has short segments or boundary spikes")
            if not item.get("blocker_free") or item.get("unmatched_white_regions"):
                errors.append(name + " is not blocker-free")
            for size, components, counters in zip(
                (64, 128, 256, 512),
                item["components_64_128_256_512"],
                item["counters_64_128_256_512"],
            ):
                topology = v4.comparison(candidate[name].foreground, candidate[name].foreground)[size]["right_topology"]
                if topology[0] != components or topology[1] != counters:
                    errors.append(f"{name} topology report differs at {size}px")

        minute = list(candidate["minute"].foreground)[0]
        second = list(candidate["second"].foreground)
        if len(second) != 2:
            errors.append("second does not contain two template contours")
        elif any(normalized_points(part) != normalized_points(minute) for part in second):
            errors.append("second is not built from exact affine minute copies")
    finally:
        source.close()
        candidate.close()

    print(
        "glyphs=340; batch1=8; ready={}; blockers={}; decisions=340; errors={}".format(
            report.get("candidate_ready"),
            report.get("candidate_blocked"),
            len(errors),
        )
    )
    for error in errors:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
