#!/usr/bin/env ffpython
"""Verify the updated Regular, cleaned base, raw/final Bold, QA, and checkpoint."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf

REGULAR = ROOT / "fontforge" / "redrawn.sfd"
BASE = ROOT / "fontforge" / "regular-bold-base.sfd"
RAW = ROOT / "fontforge" / "bold-raw.sfd"
BOLD = ROOT / "fontforge" / "bold.sfd"
REPORT = ROOT / "qa" / "assets" / "bold-report.csv"
META = ROOT / "qa" / "assets" / "bold-report.json"
BASE_META = ROOT / "qa" / "assets" / "bold-base-cleanup.json"
DECISIONS = ROOT / "qa" / "bold-manual-decisions.json"
CHECKPOINT = ROOT / "checkpoints" / "bold-v2"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def points(layer):
    return sum(len(contour) for contour in layer)


def main():
    errors = []
    for path in (REGULAR, BASE, RAW, BOLD, REPORT, META, BASE_META, DECISIONS):
        if not path.is_file():
            errors.append("missing " + str(path))
    if errors:
        print("\n".join(errors))
        return 1
    fonts = [fontforge.open(str(path)) for path in (REGULAR, BASE, RAW, BOLD)]
    regular, base, raw, bold = fonts
    try:
        lists = [[glyph.glyphname for glyph in font.glyphs()] for font in fonts]
        if any(len(names) != 340 for names in lists):
            errors.append("all sources must contain 340 glyphs")
        if any(names != lists[0] for names in lists[1:]):
            errors.append("glyph order differs")
        maps = [{glyph.glyphname: glyph.unicode for glyph in font.glyphs()} for font in fonts]
        if any(mapping != maps[0] for mapping in maps[1:]):
            errors.append("Unicode mapping differs")
        if sum(points(glyph.foreground) > 0 for glyph in regular.glyphs()) != 334:
            errors.append("updated Regular must contain 334 outlined glyphs")
        if sum(points(glyph.foreground) > 0 for glyph in base.glyphs()) != 334:
            errors.append("cleaned base must contain 334 outlined glyphs")
        for name in lists[0]:
            if regular[name].width != base[name].width:
                errors.append(name + " base width changed")
        for font in (base, raw, bold):
            if len(font.gpos_lookups) != len(regular.gpos_lookups) or len(font.gsub_lookups) != len(regular.gsub_lookups):
                errors.append("OpenType lookup coverage differs")
        if (bold.familyname, bold.weight, bold.os2_weight, bold.macstyle, bold.italicangle) != (
            "Olesuas Hand", "Bold", 700, 1, 0.0
        ):
            errors.append("Bold metadata differs")
        base_meta = json.loads(BASE_META.read_text(encoding="utf-8-sig"))
        if base_meta["source_sha256"] != digest(REGULAR):
            errors.append("cleaned base does not derive from current Regular")
        if base_meta["base_sha256"] != digest(BASE) or not base_meta["build_ready"]:
            errors.append("cleaned base hash/readiness differs")
        rows = list(csv.DictReader(REPORT.open(encoding="utf-8-sig", newline="")))
        if len(rows) != 340:
            errors.append("QA report must contain 340 rows")
        for row in rows:
            name = row["glyph"]
            if row["regular_hash"] != rf.layer_hash(regular[name].foreground):
                errors.append(name + " updated Regular hash stale")
            if row["base_hash"] != rf.layer_hash(base[name].foreground):
                errors.append(name + " base hash stale")
            if row["bold_hash"] != rf.layer_hash(bold[name].foreground):
                errors.append(name + " Bold hash stale")
        meta = json.loads(META.read_text(encoding="utf-8-sig"))
        if meta["version"] != "bold-metrics-v2":
            errors.append("wrong metrics version")
        if meta["regular_sfd_hash"] != digest(REGULAR) or meta["base_sfd_hash"] != digest(BASE) or meta["bold_sfd_hash"] != digest(BOLD):
            errors.append("QA artifact hashes stale")
    finally:
        for font in fonts:
            font.close()
    decisions = json.loads(DECISIONS.read_text(encoding="utf-8-sig"))
    if decisions.get("version") != "bold-manual-review-v2":
        errors.append("wrong decision document version")
    if (CHECKPOINT / "manifest.json").is_file():
        manifest = json.loads((CHECKPOINT / "manifest.json").read_text(encoding="utf-8"))
        for name, expected in manifest["artifact_hashes"].items():
            path = CHECKPOINT / name
            if not path.is_file() or digest(path) != expected:
                errors.append("checkpoint hash differs: " + name)
    print("glyphs=340; outlined=334; errors={}".format(len(errors)))
    for error in errors[:100]:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
