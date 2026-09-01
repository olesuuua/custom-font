#!/usr/bin/env fontforge
"""Promote the approved manual Italic v4 into immutable final artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import fontforge


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "italic-manual-fixed-v4.sfd"
DEFAULT_OUTPUT = ROOT / "italic-final.sfd"
DEFAULT_TTF = ROOT / "qa" / "assets" / "italic-final.ttf"
DEFAULT_REPORT = ROOT / "qa" / "assets" / "italic-final-report.json"
SPACE_WIDTH = 341


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pair_records(glyph):
    return [record for record in glyph.getPosSub("*") if record[1] == "Pair"]


def add_blank_space(font) -> dict:
    existing = next((glyph for glyph in font.glyphs() if glyph.unicode == 0x20), None)
    if existing is not None:
        raise RuntimeError("Italic source unexpectedly already contains U+0020 SPACE")
    space = font.createChar(0x20, "space")
    space.clear()
    space.width = SPACE_WIDTH
    return {"glyph": space.glyphname, "codepoint": "U+0020", "width": space.width}


def add_ohm(font) -> dict:
    source = font["uni03A9"]
    if source.unicode != 0x03A9:
        raise RuntimeError("uni03A9 is not encoded as U+03A9")
    if any(glyph.unicode == 0x2126 for glyph in font.glyphs()):
        raise RuntimeError("Italic source unexpectedly already contains U+2126 OHM SIGN")

    outgoing = pair_records(source)
    incoming = []
    for glyph in font.glyphs():
        for record in pair_records(glyph):
            if record[2] == source.glyphname:
                incoming.append((glyph.glyphname, record))

    font.selection.select(source.glyphname)
    font.copy()
    target = font.createChar(0x2126, "Omega")
    font.selection.select(target.glyphname)
    font.paste()

    existing_outgoing = set(pair_records(target))
    for record in outgoing:
        if record not in existing_outgoing:
            target.addPosSub(record[0], record[2], *record[3:])
        if record[2] == source.glyphname:
            target.addPosSub(record[0], target.glyphname, *record[3:])
    for glyph_name, record in incoming:
        font[glyph_name].addPosSub(record[0], target.glyphname, *record[3:])

    target.width = source.width
    target.vwidth = source.vwidth
    return {
        "glyph": target.glyphname,
        "codepoint": "U+2126",
        "source_glyph": source.glyphname,
        "source_codepoint": "U+03A9",
        "width": target.width,
        "outgoing_pair_count": len(pair_records(target)),
        "incoming_pair_count": sum(
            1
            for glyph in font.glyphs()
            for record in pair_records(glyph)
            if record[2] == target.glyphname
        ),
    }


def font_summary(path: Path) -> dict:
    font = fontforge.open(str(path))
    try:
        glyphs = list(font.glyphs())
        lookups = [name for name in font.gpos_lookups if "kern" in name.lower()]
        subtables = [
            subtable
            for lookup in lookups
            for subtable in font.getLookupSubtables(lookup)
        ]
        pairs = [
            (glyph.glyphname, record[2], record[5])
            for glyph in glyphs
            for record in pair_records(glyph)
            if len(record) > 5
        ]
        return {
            "glyph_count": len(glyphs),
            "outlined_glyph_count": sum(
                bool(len(glyph.foreground) or glyph.references) for glyph in glyphs
            ),
            "empty_glyph_count": sum(
                not len(glyph.foreground) and not glyph.references for glyph in glyphs
            ),
            "kerning_lookups": lookups,
            "kerning_subtables": subtables,
            "kerning_pair_count": len(pairs),
            "kerning_minimum": max((value for _, _, value in pairs), default=0),
            "kerning_maximum_magnitude": min((value for _, _, value in pairs), default=0),
            "validation": font.validate(),
            "version": font.version,
        }
    finally:
        font.close()


def kerning_pairs(path: Path) -> list[dict]:
    font = fontforge.open(str(path))
    try:
        pairs = [
            {"left": glyph.glyphname, "right": record[2], "value": record[5]}
            for glyph in font.glyphs()
            for record in pair_records(glyph)
            if len(record) > 5
        ]
        pairs.sort(key=lambda row: (row["left"], row["right"]))
        return pairs
    finally:
        font.close()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--ttf", type=Path, default=DEFAULT_TTF)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    ttf = args.ttf.resolve()
    report = args.report.resolve()
    outputs = (output, ttf, report)

    if not source.is_file():
        raise FileNotFoundError(source)
    if source in outputs:
        raise RuntimeError("final outputs must not overwrite the manual v4 source")
    existing = [path for path in outputs if path.exists()]
    if existing and not args.force:
        raise FileExistsError(
            "outputs already exist; pass --force: " + ", ".join(map(str, existing))
        )
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)

    source_hash = sha256(source)
    source_summary = font_summary(source)
    shutil.copyfile(source, output)
    font = fontforge.open(str(output))
    try:
        space = add_blank_space(font)
        ohm = add_ohm(font)
        font.save(str(output))
        font.generate(str(ttf), flags=("opentype",))
    finally:
        font.close()

    if sha256(source) != source_hash:
        raise RuntimeError("manual v4 source changed during finalization")

    output_summary = font_summary(output)
    ttf_summary = font_summary(ttf)
    if output_summary["kerning_pair_count"] != ttf_summary["kerning_pair_count"]:
        raise RuntimeError("SFD and TTF kerning pair counts differ")
    pairs = kerning_pairs(output)

    payload = {
        "version": "italic-final-release-v1",
        "source": str(source),
        "source_hash": source_hash,
        "output_hashes": {"sfd": sha256(output), "ttf": sha256(ttf)},
        "source_summary": source_summary,
        "output_summary": output_summary,
        "ttf_summary": ttf_summary,
        "space": space,
        "ohm": ohm,
        "kerning": {
            "pair_count": len(pairs),
            "minimum": 15,
            "only_closer": True,
            "touch": False,
            "sfd_lookups": output_summary["kerning_lookups"],
            "sfd_subtables": output_summary["kerning_subtables"],
            "ttf_subtables": ttf_summary["kerning_subtables"],
            "pairs": pairs,
        },
    }
    payload["build_id"] = hashlib.sha256(
        (payload["output_hashes"]["sfd"] + payload["output_hashes"]["ttf"]).encode("ascii")
    ).hexdigest()[:16]
    report.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "build_id": payload["build_id"],
        "glyphs": output_summary["glyph_count"],
        "kern_pairs": output_summary["kerning_pair_count"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
