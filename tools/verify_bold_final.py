#!/usr/bin/env ffpython
"""Verify final spacing, space glyphs, kerning, and QA character coverage."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import fontforge


ROOT = Path(__file__).resolve().parents[1]
REGULAR_SOURCE = ROOT / "fontforge" / "redrawn.sfd"
BOLD_SOURCE = ROOT / "bold-manual.sfd"
REGULAR_FINAL = ROOT / "regular-final.sfd"
BOLD_FINAL = ROOT / "bold-final.sfd"
REGULAR_TTF = ROOT / "qa" / "assets" / "regular-final.ttf"
BOLD_TTF = ROOT / "qa" / "assets" / "bold-final.ttf"
REPORT = ROOT / "qa" / "assets" / "bold-final-report.json"
SPECIMENS = ROOT / "qa" / "assets" / "bold-final-specimens.json"
SUBTABLE = "Bold Final Auto Kern"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def points(glyph):
    return [[(point.x, point.y, point.on_curve) for point in contour] for contour in glyph.foreground]


def assert_blank_space(font, width):
    space = next((glyph for glyph in font.glyphs() if glyph.unicode == 0x20), None)
    assert space is not None, "U+0020 is missing"
    assert space.glyphname == "space", space.glyphname
    assert space.width == width, (space.width, width)
    assert not len(space.foreground) and not space.references


def regular_reference(glyph, regular):
    if glyph.glyphname == "Omega":
        return regular["uni03A9"]
    try:
        return regular[glyph.glyphname]
    except TypeError:
        if glyph.unicode >= 0:
            try:
                return regular[glyph.unicode]
            except TypeError:
                return None
    return None


def pair_records(glyph):
    return sorted(record for record in glyph.getPosSub("*") if record[1] == "Pair")


def incoming_pairs(font, glyph_name):
    return sorted(
        (glyph.glyphname, record[0], *record[3:])
        for glyph in font.glyphs()
        for record in glyph.getPosSub("*")
        if record[1] == "Pair" and record[2] == glyph_name
    )


def assert_regular_ohm(font):
    omega = font["uni03A9"]
    ohm = font["Omega"]
    assert omega.unicode == 0x03A9
    assert ohm.unicode == 0x2126
    assert omega.foreground == ohm.foreground
    assert omega.width == ohm.width
    assert omega.vwidth == ohm.vwidth
    assert omega.anchorPoints == ohm.anchorPoints
    assert omega.references == ohm.references
    assert pair_records(omega) == pair_records(ohm)
    assert incoming_pairs(font, omega.glyphname) == incoming_pairs(font, ohm.glyphname)


def main() -> int:
    for path in (REGULAR_SOURCE, BOLD_SOURCE, REGULAR_FINAL, BOLD_FINAL, REGULAR_TTF, BOLD_TTF, REPORT, SPECIMENS):
        assert path.is_file(), path
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    assert report["source_hashes"] == {
        "regular": sha256(REGULAR_SOURCE),
        "bold": sha256(BOLD_SOURCE),
    }
    assert report["output_hashes"] == {
        "regular_sfd": sha256(REGULAR_FINAL),
        "bold_sfd": sha256(BOLD_FINAL),
        "regular_ttf": sha256(REGULAR_TTF),
        "bold_ttf": sha256(BOLD_TTF),
    }

    regular = fontforge.open(str(REGULAR_FINAL))
    bold = fontforge.open(str(BOLD_FINAL))
    bold_source = fontforge.open(str(BOLD_SOURCE))
    regular_ttf = fontforge.open(str(REGULAR_TTF))
    bold_ttf = fontforge.open(str(BOLD_TTF))
    try:
        assert_blank_space(regular, 341)
        assert_blank_space(bold, 381)
        assert_blank_space(regular_ttf, 341)
        assert_blank_space(bold_ttf, 381)
        assert_regular_ohm(regular)
        assert_regular_ohm(regular_ttf)

        changes = {row["glyph"]: row for row in report["spacing"]["changes"]}
        for glyph_name, row in changes.items():
            final_glyph = bold[glyph_name]
            source_glyph = bold_source[glyph_name]
            reference = regular_reference(final_glyph, regular)
            assert reference is not None, glyph_name
            final_box = final_glyph.boundingBox()
            reference_box = reference.boundingBox()
            assert abs(final_box[0] - reference_box[0]) <= 0.01, glyph_name
            assert abs((final_glyph.width - final_box[2]) - (reference.width - reference_box[2])) <= 1.01, glyph_name

            source_points = points(source_glyph)
            final_points = points(final_glyph)
            assert len(source_points) == len(final_points), glyph_name
            dx = float(row["translate_x"])
            for source_contour, final_contour in zip(source_points, final_points):
                assert len(source_contour) == len(final_contour), glyph_name
                for source_point, final_point in zip(source_contour, final_contour):
                    assert source_point[2] == final_point[2], glyph_name
                    assert abs(source_point[1] - final_point[1]) <= 0.01, glyph_name
                    assert abs((final_point[0] - source_point[0]) - dx) <= 0.02, glyph_name

        kern_lookup = next(name for name in bold.gpos_lookups if "kern" in name.lower())
        assert bold.getLookupSubtables(kern_lookup) == (SUBTABLE,)
        empty_names = {glyph.glyphname for glyph in bold.glyphs() if not len(glyph.foreground)}
        pairs = []
        for glyph in bold.glyphs():
            for positioning in glyph.getPosSub(SUBTABLE):
                if len(positioning) > 5 and positioning[1] == "Pair":
                    pairs.append((glyph.glyphname, positioning[2], positioning[5]))
        assert len(pairs) == report["kerning"]["pair_count"]
        assert all(value <= -15 for _, _, value in pairs)
        assert all(left not in empty_names and right not in empty_names for left, right, _ in pairs)

        ttf_kern_lookups = [name for name in bold_ttf.gpos_lookups if "kern" in name.lower()]
        assert len(ttf_kern_lookups) == 1
        ttf_pairs = []
        for subtable in bold_ttf.getLookupSubtables(ttf_kern_lookups[0]):
            for glyph in bold_ttf.glyphs():
                for positioning in glyph.getPosSub(subtable):
                    if len(positioning) > 5 and positioning[1] == "Pair":
                        ttf_pairs.append((glyph.glyphname, positioning[2], positioning[5]))
        assert set(ttf_pairs) == set(pairs)

        specimen_text = "\n".join(
            section["text"] for section in json.loads(SPECIMENS.read_text(encoding="utf-8"))["sections"]
        )
        regular_codepoints = {glyph.unicode for glyph in regular.glyphs() if glyph.unicode >= 0}
        bold_codepoints = {glyph.unicode for glyph in bold.glyphs() if glyph.unicode >= 0}
        used = {ord(character) for character in specimen_text if character not in "\r\n"}
        assert not (used - regular_codepoints), "Regular specimen gaps: {}".format(sorted(used - regular_codepoints))
        assert not (used - bold_codepoints), "Bold specimen gaps: {}".format(sorted(used - bold_codepoints))
    finally:
        regular.close()
        bold.close()
        bold_source.close()
        regular_ttf.close()
        bold_ttf.close()

    print(json.dumps({
        "matched_glyphs": report["spacing"]["matched_glyph_count"],
        "kern_pairs": report["kerning"]["pair_count"],
        "space_widths": report["space"],
        "specimen_codepoints": len(used),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
