#!/usr/bin/env ffpython
"""Build raw and editable Bold only from the validated cleaned Regular base."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
UPDATED_REGULAR = ROOT / "fontforge" / "redrawn.sfd"
BASE = ROOT / "fontforge" / "regular-bold-base.sfd"
BASE_REPORT = ROOT / "qa" / "assets" / "bold-base-cleanup.json"
RAW = ROOT / "fontforge" / "bold-raw.sfd"
BOLD = ROOT / "fontforge" / "bold.sfd"
BUILD_INFO = ROOT / "qa" / "assets" / "bold-build.json"
ROUNDTRIP = ROOT / "qa" / "assets" / ".bold-v2-roundtrip.ttf"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def points(layer):
    return sum(len(contour) for contour in layer)


def metadata(font):
    font.fontname = "OlesuasHand-Bold"
    font.familyname = "Olesuas Hand"
    font.fullname = "Olesuas Hand Bold"
    font.weight = "Bold"
    font.version = "001.001"
    font.italicangle = 0
    font.os2_weight = 700
    font.os2_stylemap = 0x20
    font.macstyle = 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if (RAW.exists() or BOLD.exists()) and not args.force:
        raise SystemExit("Refusing to overwrite existing Bold files without --force.")
    report = json.loads(BASE_REPORT.read_text(encoding="utf-8-sig"))
    if not report.get("build_ready"):
        raise SystemExit("Cleaned Bold base has unresolved blockers.")
    if report.get("source_sha256") != sha256(UPDATED_REGULAR):
        raise SystemExit("Cleanup report does not match the updated Regular.")
    if report.get("base_sha256") != sha256(BASE):
        raise SystemExit("Cleaned Bold base does not match its completed cleanup report.")
    font = fontforge.open(str(BASE))
    methods = {}
    try:
        before = {glyph.glyphname: points(glyph.foreground) for glyph in font.glyphs()}
        font.selection.all()
        font.changeWeight(40, "LCG", 0, 0, "retain")
        for glyph in font.glyphs():
            if before[glyph.glyphname] == 0:
                glyph.foreground = fontforge.layer()
                methods[glyph.glyphname] = "preserved-empty"
            else:
                methods[glyph.glyphname] = "raw-fontforge-lcg-retain"
        metadata(font)
        font.generate(str(ROUNDTRIP))
    finally:
        font.close()
    roundtrip = fontforge.open(str(ROUNDTRIP))
    try:
        metadata(roundtrip)
        roundtrip.save(str(RAW))
    finally:
        roundtrip.close()
        if ROUNDTRIP.exists():
            os.unlink(ROUNDTRIP)
    # The editable file starts from the diagnostic result. Post-bold repair
    # operates only on this copy and never on RAW or either Regular source.
    shutil.copy2(RAW, BOLD)
    payload = {
        "version": "bold-build-v2",
        "metrics_version": "bold-metrics-v2",
        "authoritative_regular": "fontforge/redrawn.sfd",
        "cleaned_base": "fontforge/regular-bold-base.sfd",
        "raw_bold": "fontforge/bold-raw.sfd",
        "bold": "fontforge/bold.sfd",
        "authoritative_regular_sha256": sha256(UPDATED_REGULAR),
        "cleaned_base_sha256": sha256(BASE),
        "raw_bold_sha256": sha256(RAW),
        "bold_sha256": sha256(BOLD),
        "stroke_width": 40,
        "type": "LCG",
        "counter_type": "retain",
        "cleanup_methods": methods,
    }
    BUILD_INFO.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("Created raw and editable Bold from validated cleaned base.")


if __name__ == "__main__":
    raise SystemExit(main())
