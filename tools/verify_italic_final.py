#!/usr/bin/env fontforge
"""Verify the immutable Italic release master, TTF, coverage, and kerning."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import fontforge


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "italic-manual-fixed-v4.sfd"
FINAL = ROOT / "italic-final.sfd"
TTF = ROOT / "qa" / "assets" / "italic-final.ttf"
REPORT = ROOT / "qa" / "assets" / "italic-final-report.json"
SPECIMENS = ROOT / "qa" / "assets" / "bold-final-specimens.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pair_records(glyph):
    return sorted(record for record in glyph.getPosSub("*") if record[1] == "Pair")


def incoming_pairs(font, glyph_name):
    return sorted(
        (glyph.glyphname, *record[3:])
        for glyph in font.glyphs()
        for record in pair_records(glyph)
        if record[2] == glyph_name
    )


def outgoing_pairs(glyph):
    return sorted((record[2], *record[3:]) for record in pair_records(glyph))


def all_pairs(font):
    return sorted(
        (glyph.glyphname, record[2], record[5])
        for glyph in font.glyphs()
        for record in pair_records(glyph)
        if len(record) > 5
    )


def assert_blank_space(font):
    matches = [glyph for glyph in font.glyphs() if glyph.unicode == 0x20]
    assert len(matches) == 1
    space = matches[0]
    assert space.glyphname == "space"
    assert space.width == 341
    assert not len(space.foreground) and not space.references


def assert_ohm(font):
    omega = font["uni03A9"]
    ohm = font["Omega"]
    assert omega.unicode == 0x03A9
    assert ohm.unicode == 0x2126
    assert omega.foreground == ohm.foreground
    assert omega.width == ohm.width
    assert omega.vwidth == ohm.vwidth
    assert omega.anchorPoints == ohm.anchorPoints
    assert omega.references == ohm.references
    assert outgoing_pairs(omega) == outgoing_pairs(ohm)
    assert incoming_pairs(font, omega.glyphname) == incoming_pairs(font, ohm.glyphname)


def main() -> int:
    for path in (SOURCE, FINAL, TTF, REPORT, SPECIMENS):
        assert path.is_file(), path
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    assert report["source_hash"] == sha256(SOURCE)
    assert report["output_hashes"] == {"sfd": sha256(FINAL), "ttf": sha256(TTF)}

    source = fontforge.open(str(SOURCE))
    final = fontforge.open(str(FINAL))
    ttf = fontforge.open(str(TTF))
    try:
        source_names = {glyph.glyphname for glyph in source.glyphs()}
        final_names = {glyph.glyphname for glyph in final.glyphs()}
        assert final_names - source_names == {"space", "Omega"}
        for glyph_name in source_names:
            before = source[glyph_name]
            after = final[glyph_name]
            assert before.foreground == after.foreground, glyph_name
            assert before.references == after.references, glyph_name
            assert before.anchorPoints == after.anchorPoints, glyph_name
            assert before.width == after.width, glyph_name
            assert before.vwidth == after.vwidth, glyph_name
            original_pairs = pair_records(before)
            retained_pairs = [record for record in pair_records(after) if record[2] != "Omega"]
            assert original_pairs == retained_pairs, glyph_name

        assert_blank_space(final)
        assert_blank_space(ttf)
        assert_ohm(final)
        assert_ohm(ttf)

        final_pairs = all_pairs(final)
        ttf_pairs = all_pairs(ttf)
        expected_pairs = report["output_summary"]["kerning_pair_count"]
        assert report["kerning"]["pair_count"] == expected_pairs
        assert len(final_pairs) == expected_pairs
        assert len(ttf_pairs) == expected_pairs
        assert final_pairs == ttf_pairs
        assert final_pairs == sorted(
            (row["left"], row["right"], row["value"])
            for row in report["kerning"]["pairs"]
        )
        assert all(value <= -15 for _, _, value in final_pairs)
        empty_names = {
            glyph.glyphname for glyph in final.glyphs()
            if not len(glyph.foreground) and not glyph.references
        }
        assert all(
            left not in empty_names and right not in empty_names
            for left, right, _ in final_pairs
        )

        specimen_text = "\n".join(
            section["text"]
            for section in json.loads(SPECIMENS.read_text(encoding="utf-8"))["sections"]
        )
        codepoints = {glyph.unicode for glyph in final.glyphs() if glyph.unicode >= 0}
        used = {ord(character) for character in specimen_text if character not in "\r\n"}
        assert not (used - codepoints), "Italic specimen gaps: {}".format(sorted(used - codepoints))

        assert len(list(source.glyphs())) == 340
        assert len(list(final.glyphs())) == len(list(ttf.glyphs())) == 342
        assert final.fontname == "OlesuasHand-Italic"
        assert final.fullname == "Olesuas Hand Italic"
        assert final.familyname == "Olesuas Hand"
        assert final.italicangle == -15
    finally:
        source.close()
        final.close()
        ttf.close()

    print(json.dumps({
        "glyphs": report["output_summary"]["glyph_count"],
        "kern_pairs": report["output_summary"]["kerning_pair_count"],
        "specimen_codepoints": len(used),
        "ttf_subtables": len(report["ttf_summary"]["kerning_subtables"]),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
