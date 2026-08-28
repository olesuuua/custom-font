#!/usr/bin/env ffpython
"""Create final Regular/Bold fonts with corrected spacing and Bold kerning."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import fontforge


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGULAR = ROOT / "fontforge" / "redrawn.sfd"
DEFAULT_BOLD = ROOT / "bold-manual.sfd"
DEFAULT_REGULAR_OUT = ROOT / "regular-final.sfd"
DEFAULT_BOLD_OUT = ROOT / "bold-final.sfd"
DEFAULT_REGULAR_TTF = ROOT / "qa" / "assets" / "regular-final.ttf"
DEFAULT_BOLD_TTF = ROOT / "qa" / "assets" / "bold-final.ttf"
DEFAULT_REPORT = ROOT / "qa" / "assets" / "bold-final-report.json"

REGULAR_SPACE_WIDTH = 341
BOLD_SPACE_WIDTH = 381
KERN_SEPARATION = 153
KERN_MINIMUM = 15
KERN_SUBTABLE = "Bold Final Auto Kern"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def point_count(glyph) -> int:
    return sum(len(contour) for contour in glyph.foreground)


def rounded(value: float) -> float | int:
    value = round(float(value), 6)
    return int(value) if value.is_integer() else value


def add_blank_space(font, width: int) -> None:
    space = font.createChar(0x20, "space")
    space.clear()
    space.width = width


def pair_records(glyph):
    return [record for record in glyph.getPosSub("*") if record[1] == "Pair"]


def add_regular_ohm(font) -> dict:
    """Clone Greek capital Omega into the Unicode compatibility Ohm sign."""
    source = font["uni03A9"]
    if source.unicode != 0x03A9:
        raise RuntimeError("uni03A9 is not encoded as U+03A9")
    if any(glyph.unicode == 0x2126 for glyph in font.glyphs()):
        raise RuntimeError("Regular source already contains U+2126")

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
            1 for glyph in font.glyphs() for record in pair_records(glyph)
            if record[2] == target.glyphname
        ),
    }


def translate_foreground(glyph, translate_x: float) -> None:
    """Translate points without FontForge's transform-time contour rewriting."""
    layer = glyph.foreground
    for contour in layer:
        for point in contour:
            point.x += translate_x
    glyph.foreground = layer


def regular_match(glyph, regular_by_name, regular_by_unicode):
    if glyph.glyphname == "Omega" and "uni03A9" in regular_by_name:
        return regular_by_name["uni03A9"], "compatibility:uni03A9"
    if glyph.glyphname in regular_by_name:
        return regular_by_name[glyph.glyphname], "name"
    if glyph.unicode >= 0 and glyph.unicode in regular_by_unicode:
        return regular_by_unicode[glyph.unicode], "unicode"
    return None, None


def match_bearings(regular, bold):
    regular_by_name = {glyph.glyphname: glyph for glyph in regular.glyphs()}
    regular_by_unicode = {glyph.unicode: glyph for glyph in regular.glyphs() if glyph.unicode >= 0}
    changes = []
    skipped = []

    for glyph in bold.glyphs():
        if glyph.glyphname == "space" or not point_count(glyph):
            continue
        reference, matched_by = regular_match(glyph, regular_by_name, regular_by_unicode)
        if reference is None or not point_count(reference):
            skipped.append({
                "glyph": glyph.glyphname,
                "codepoint": glyph.unicode if glyph.unicode >= 0 else None,
                "reason": "no outlined Regular metrics reference",
            })
            continue

        regular_box = reference.boundingBox()
        bold_box = glyph.boundingBox()
        regular_lsb = regular_box[0]
        regular_rsb = reference.width - regular_box[2]
        before_width = glyph.width
        before_lsb = bold_box[0]
        before_rsb = before_width - bold_box[2]
        # Quantize to the font's 1/1024-em grid. Arbitrary floating offsets can
        # make FontForge collapse nearly coincident control points on save.
        translate_x = round((regular_lsb - bold_box[0]) * 1024) / 1024

        if abs(translate_x) > 1e-9:
            translate_foreground(glyph, translate_x)
        translated_box = glyph.boundingBox()
        target_width = int(round(translated_box[2] + regular_rsb))
        glyph.width = target_width
        final_box = glyph.boundingBox()

        changes.append({
            "glyph": glyph.glyphname,
            "codepoint": glyph.unicode if glyph.unicode >= 0 else None,
            "regular_reference": reference.glyphname,
            "matched_by": matched_by,
            "translate_x": rounded(translate_x),
            "width_before": before_width,
            "width_after": target_width,
            "width_delta": target_width - before_width,
            "regular_lsb": rounded(regular_lsb),
            "regular_rsb": rounded(regular_rsb),
            "bold_lsb_before": rounded(before_lsb),
            "bold_rsb_before": rounded(before_rsb),
            "bold_lsb_after": rounded(final_box[0]),
            "bold_rsb_after": rounded(target_width - final_box[2]),
        })
    return changes, skipped


def rebuild_kerning(font):
    lookup = next((name for name in font.gpos_lookups if "kern" in name.lower()), None)
    if lookup is None:
        raise RuntimeError("Bold source has no GPOS kerning lookup")
    for subtable in font.getLookupSubtables(lookup):
        font.removeLookupSubtable(subtable)
    font.addLookupSubtable(lookup, KERN_SUBTABLE)

    eligible = sorted(
        glyph.glyphname for glyph in font.glyphs()
        if glyph.unicode >= 0x20 and glyph.glyphname != "space" and point_count(glyph)
    )
    font.autoKern(
        KERN_SUBTABLE,
        KERN_SEPARATION,
        eligible,
        eligible,
        minKern=KERN_MINIMUM,
        onlyCloser=True,
        touch=False,
    )

    pairs = []
    for glyph in font.glyphs():
        for positioning in glyph.getPosSub(KERN_SUBTABLE):
            if len(positioning) > 5 and positioning[1] == "Pair":
                pairs.append({
                    "left": glyph.glyphname,
                    "right": positioning[2],
                    "value": positioning[5],
                })
    pairs.sort(key=lambda row: (row["left"], row["right"]))
    return lookup, eligible, pairs


def install_kerning(font, pairs):
    lookup = next((name for name in font.gpos_lookups if "kern" in name.lower()), None)
    if lookup is None:
        raise RuntimeError("Bold source has no GPOS kerning lookup")
    for subtable in font.getLookupSubtables(lookup):
        font.removeLookupSubtable(subtable)
    font.addLookupSubtable(lookup, KERN_SUBTABLE)
    for pair in pairs:
        font[pair["left"]].addPosSub(KERN_SUBTABLE, pair["right"], pair["value"])
    return lookup


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regular", type=Path, default=DEFAULT_REGULAR)
    parser.add_argument("--bold", type=Path, default=DEFAULT_BOLD)
    parser.add_argument("--regular-output", type=Path, default=DEFAULT_REGULAR_OUT)
    parser.add_argument("--bold-output", type=Path, default=DEFAULT_BOLD_OUT)
    parser.add_argument("--regular-ttf", type=Path, default=DEFAULT_REGULAR_TTF)
    parser.add_argument("--bold-ttf", type=Path, default=DEFAULT_BOLD_TTF)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    inputs = (args.regular.resolve(), args.bold.resolve())
    outputs = (
        args.regular_output.resolve(), args.bold_output.resolve(),
        args.regular_ttf.resolve(), args.bold_ttf.resolve(), args.report.resolve(),
    )
    for path in inputs:
        if not path.is_file():
            raise FileNotFoundError(path)
    if any(path in inputs for path in outputs):
        raise RuntimeError("output paths must not overwrite either source SFD")
    existing = [path for path in outputs if path.exists()]
    if existing and not args.force:
        raise FileExistsError("outputs already exist; pass --force: {}".format(", ".join(map(str, existing))))
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)

    source_hashes = {"regular": sha256(inputs[0]), "bold": sha256(inputs[1])}
    regular = fontforge.open(str(inputs[0]))
    bold = fontforge.open(str(inputs[1]))
    (ROOT / "tmp").mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="bold-final-kern-", dir=str(ROOT / "tmp"))
    kerning_source = Path(temporary.name) / "bold-kerning.sfd"
    shutil.copyfile(inputs[1], kerning_source)
    kerning_font = fontforge.open(str(kerning_source))
    try:
        add_blank_space(regular, REGULAR_SPACE_WIDTH)
        regular_ohm = add_regular_ohm(regular)
        add_blank_space(bold, BOLD_SPACE_WIDTH)
        add_blank_space(kerning_font, BOLD_SPACE_WIDTH)
        changes, skipped = match_bearings(regular, bold)
        match_bearings(regular, kerning_font)
        _, eligible, pairs = rebuild_kerning(kerning_font)
        lookup = install_kerning(bold, pairs)

        regular.save(str(outputs[0]))
        bold.save(str(outputs[1]))
        regular.generate(str(outputs[2]), flags=("opentype",))
        bold.generate(str(outputs[3]), flags=("opentype",))
    finally:
        regular.close()
        bold.close()
        kerning_font.close()
        temporary.cleanup()

    if {"regular": sha256(inputs[0]), "bold": sha256(inputs[1])} != source_hashes:
        raise RuntimeError("a source SFD changed during finalization")

    payload = {
        "version": "bold-final-metrics-v1",
        "source_hashes": source_hashes,
        "output_hashes": {
            "regular_sfd": sha256(outputs[0]),
            "bold_sfd": sha256(outputs[1]),
            "regular_ttf": sha256(outputs[2]),
            "bold_ttf": sha256(outputs[3]),
        },
        "space": {"regular_width": REGULAR_SPACE_WIDTH, "bold_width": BOLD_SPACE_WIDTH},
        "regular_ohm": regular_ohm,
        "spacing": {
            "matched_glyph_count": len(changes),
            "skipped_glyph_count": len(skipped),
            "changes": changes,
            "skipped": skipped,
        },
        "kerning": {
            "lookup": lookup,
            "subtable": KERN_SUBTABLE,
            "separation": KERN_SEPARATION,
            "minimum": KERN_MINIMUM,
            "only_closer": True,
            "touch": False,
            "eligible_glyph_count": len(eligible),
            "eligible_glyphs": eligible,
            "pair_count": len(pairs),
            "pairs": pairs,
        },
    }
    payload["build_id"] = hashlib.sha256(
        (payload["output_hashes"]["regular_ttf"] + payload["output_hashes"]["bold_ttf"]).encode("ascii")
    ).hexdigest()[:16]
    args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "build_id": payload["build_id"],
        "matched_glyphs": len(changes),
        "skipped_glyphs": len(skipped),
        "kern_pairs": len(pairs),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
