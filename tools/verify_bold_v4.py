#!/usr/bin/env ffpython
"""Verify Bold v4 smoothness, topology, dots, decisions, and font invariants."""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools")]
import redraw_font as rf

REGULAR = ROOT / "fontforge" / "redrawn.sfd"
BASE = ROOT / "fontforge" / "regular-bold-base.sfd"
RAW = ROOT / "fontforge" / "bold-raw.sfd"
BOLD = ROOT / "fontforge" / "bold.sfd"
REPORT = ROOT / "qa" / "assets" / "bold-report.csv"
META = ROOT / "qa" / "assets" / "bold-report.json"
DECISIONS = ROOT / "qa" / "bold-manual-decisions.json"
READY = ROOT / "qa" / "bold-ready-to-pass.json"
OLD = ROOT / "checkpoints" / "bold-v3-reviewed"
EXPECTED_REGULAR = "5f0c82491cb499a1eec606c905b449129a48479703b12d5d0c56152d68fba418"
EXPECTED_BASE = "9559efcd3007580146ed089a5eec3c4dd76ffd04c64c9d99445fff0d7bdb62e2"
PRIORITY = {"lessequal", "uni208A", "asterisk", "one", "four", "t", "u", "z"}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def points(layer):
    return sum(len(contour) for contour in layer)


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main():
    errors = []
    required = [REGULAR, BASE, RAW, BOLD, REPORT, META, DECISIONS, READY]
    for path in required:
        if not path.is_file():
            errors.append("missing " + str(path))
    if errors:
        print("\n".join(errors))
        return 1
    if digest(REGULAR) != EXPECTED_REGULAR:
        errors.append("authoritative Regular changed")
    if digest(BASE) != EXPECTED_BASE:
        errors.append("cleaned Bold base changed")
    fonts = [fontforge.open(str(path)) for path in (REGULAR, BASE, RAW, BOLD)]
    regular, base, raw, bold = fonts
    try:
        names = [[glyph.glyphname for glyph in font.glyphs()] for font in fonts]
        if any(len(value) != 340 for value in names):
            errors.append("all sources must contain 340 glyphs")
        if any(value != names[0] for value in names[1:]):
            errors.append("glyph order differs")
        maps = [{glyph.glyphname: glyph.unicode for glyph in font.glyphs()} for font in fonts]
        if any(value != maps[0] for value in maps[1:]):
            errors.append("cmap differs")
        if sum(points(glyph.foreground) > 0 for glyph in regular.glyphs()) != 334:
            errors.append("Regular outlined-glyph count differs")
        for name in names[0]:
            if regular[name].width != base[name].width:
                errors.append(name + " cleaned-base advance changed")
            if raw[name].width != bold[name].width:
                errors.append(name + " Bold advance changed")
            if any(contour.selfIntersects() for contour in bold[name].foreground):
                errors.append(name + " has a self-intersection")
        for font in (base, raw, bold):
            if (
                list(font.gpos_lookups) != list(regular.gpos_lookups)
                or list(font.gsub_lookups) != list(regular.gsub_lookups)
            ):
                errors.append("OpenType lookup coverage differs")
        if (bold.familyname, bold.weight, bold.os2_weight, bold.macstyle, bold.italicangle) != (
            "Olesuas Hand", "Bold", 700, 1, 0.0
        ):
            errors.append("Bold metadata differs")
    finally:
        for font in fonts:
            font.close()
    with REPORT.open(encoding="utf-8-sig", newline="") as handle:
        rows = {row["glyph"]: row for row in csv.DictReader(handle)}
    if len(rows) != 340:
        errors.append("QA report must contain 340 rows")
    fields = {
        "unmatched_white_regions", "matched_required_counters",
        "boundary_spikes", "roughness_score",
        "dot_center_delta_x", "dot_center_delta_y", "dot_diameter_ratio",
    }
    if rows and not fields.issubset(next(iter(rows.values()))):
        errors.append("v4 metrics fields missing")
    if sum(int(row["bold_self_intersections"]) for row in rows.values()):
        errors.append("QA reports self-intersections")
    if sum(int(row["unmatched_white_regions"]) for row in rows.values()):
        errors.append("QA reports unmatched white regions")
    for name in PRIORITY:
        if rows[name]["manual_blockers"]:
            errors.append(name + " retains a Pass blocker")
    for name in ("t", "u", "z"):
        if any(int(rows[name]["bold_counters_{}".format(size)]) for size in (256, 512)):
            errors.append(name + " retains a white patch")
    targets = {}
    for filename in ("bold-v4-repairs.json", "bold-v4-residuals.json", "bold-v4-uni0451-dots.json"):
        payload = load(ROOT / "qa" / "assets" / filename)
        for name, item in payload.get("glyphs", {}).items():
            if item.get("dots"):
                targets[name] = item["dots"]
    for name, details in targets.items():
        row = rows[name]
        if (
            abs(float(row["dot_center_delta_x"])) > 1.0
            or abs(float(row["dot_center_delta_y"])) > 1.0
            or abs(float(row["dot_diameter_ratio"]) - 1.0) > .01
        ):
            errors.append(name + " dot placement or diameter differs")
    meta = load(META)
    if (
        meta.get("version") != "bold-metrics-v4"
        or meta.get("regular_sfd_hash") != EXPECTED_REGULAR
        or meta.get("base_sfd_hash") != EXPECTED_BASE
        or meta.get("bold_sfd_hash") != digest(BOLD)
        or meta.get("self_intersection_glyph_count") != 0
        or meta.get("unmatched_white_region_count") != 0
    ):
        errors.append("v4 QA metadata is stale")
    decisions = load(DECISIONS)
    ready = load(READY).get("glyphs", {})
    if (
        decisions.get("version") != "bold-manual-review-v4"
        or decisions.get("metrics_version") != "bold-metrics-v4"
    ):
        errors.append("v4 decision version differs")
    if meta.get("ready_to_pass_count") != len(ready):
        errors.append("Ready-to-Pass count differs")
    for name, entry in ready.items():
        decision = decisions["decisions"].get(name)
        row = rows[name]
        if (
            not decision or decision.get("status") != "almost_done"
            or decision.get("bold_hash") != row["bold_hash"]
            or row["ready_to_pass"] != "true"
            or row["manual_blockers"]
            or min(float(entry["repair_iou_64"]), float(entry["repair_iou_128"])) < .995
        ):
            errors.append(name + " Ready binding differs")
    old_decisions = load(OLD / "bold-manual-decisions.json")["decisions"]
    with (OLD / "bold-report.csv").open(encoding="utf-8-sig", newline="") as handle:
        old_rows = {row["glyph"]: row for row in csv.DictReader(handle)}
    for name, old_decision in old_decisions.items():
        if old_rows[name]["bold_hash"] == rows[name]["bold_hash"]:
            current = decisions["decisions"].get(name)
            if not current or current.get("status") != old_decision.get("status"):
                errors.append(name + " unchanged decision was not preserved")
    print("glyphs=340; outlined=334; ready={}; errors={}".format(len(ready), len(errors)))
    for error in errors[:150]:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
