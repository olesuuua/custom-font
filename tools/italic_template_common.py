#!/usr/bin/env python3
"""Shared geometry and glyph metadata for the italic drawing workflow."""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SFD = ROOT / "fontforge" / "redrawn.sfd"
SOURCE_TTF = ROOT / "qa" / "assets" / "redrawn.ttf"
MANIFEST = ROOT / "output" / "italic-template" / "italic-template-manifest.json"
TEMPLATE_PDF = ROOT / "output" / "pdf" / "olesuas-hand-italic-drawing-template.pdf"

A4_WIDTH = 595.275590551
A4_HEIGHT = 841.88976378
PAGE_COUNT = 12
COLS = 5
ROWS = 6
CELLS_PER_PAGE = COLS * ROWS

PAGE_MARGIN_X = 18.0
GRID_BOTTOM = 22.0
GRID_TOP = 806.0
CELL_WIDTH = (A4_WIDTH - 2 * PAGE_MARGIN_X) / COLS
CELL_HEIGHT = (GRID_TOP - GRID_BOTTOM) / ROWS
LABEL_HEIGHT = 16.0
CELL_PADDING_X = 5.0
CELL_PADDING_Y = 5.0

FONT_UNITS_PER_EM = 1024
FONT_Y_MIN = -479
FONT_Y_MAX = 964
FONT_X_MIN = -128
FONT_X_MAX = 1280
ITALIC_ANGLE = 15.0
AUTO_SIDEBEARING = 50
DRAWING_LAYER = "ITALIC INK"

EMPTY_GLYPHS = (
    ".null",
    "nonmarkingreturn",
    "uni2117",
    "weierstrass",
    "uni2119",
    "uni211A",
)


@dataclass(frozen=True)
class CellGeometry:
    page: int
    row: int
    column: int
    left: float
    bottom: float
    right: float
    top: float
    scale: float
    x_origin: float
    baseline: float

    @property
    def xopp_top(self) -> float:
        return A4_HEIGHT - self.top

    @property
    def xopp_bottom(self) -> float:
        return A4_HEIGHT - self.bottom

    @property
    def xopp_baseline(self) -> float:
        return A4_HEIGHT - self.baseline


def cell_geometry(page: int, row: int, column: int) -> CellGeometry:
    left = PAGE_MARGIN_X + column * CELL_WIDTH
    right = left + CELL_WIDTH
    top = GRID_TOP - row * CELL_HEIGHT
    bottom = top - CELL_HEIGHT
    draw_left = left + CELL_PADDING_X
    draw_right = right - CELL_PADDING_X
    draw_bottom = bottom + CELL_PADDING_Y
    draw_top = top - LABEL_HEIGHT - CELL_PADDING_Y
    scale = min(
        (draw_right - draw_left) / (FONT_X_MAX - FONT_X_MIN),
        (draw_top - draw_bottom) / (FONT_Y_MAX - FONT_Y_MIN),
    )
    x_origin = draw_left - FONT_X_MIN * scale
    baseline = draw_bottom - FONT_Y_MIN * scale
    return CellGeometry(
        page=page,
        row=row,
        column=column,
        left=left,
        bottom=bottom,
        right=right,
        top=top,
        scale=scale,
        x_origin=x_origin,
        baseline=baseline,
    )


def semantic_group(codepoint: int | None, glyph_name: str) -> tuple[int, str]:
    if glyph_name == ".notdef":
        return (0, "missing glyph")
    if codepoint is None:
        return (99, "unencoded")
    name = unicodedata.name(chr(codepoint), "")
    category = unicodedata.category(chr(codepoint))
    if "LATIN" in name and "LETTER" in name:
        return (1, "Latin")
    if "CYRILLIC" in name and "LETTER" in name:
        return (2, "Cyrillic")
    if category.startswith("N"):
        return (3, "numbers and fractions")
    if category.startswith("P"):
        return (4, "punctuation and quotes")
    if category == "Sc":
        return (5, "currency")
    if "ARROW" in name:
        return (6, "arrows")
    if category == "Sm" or any(
        token in name
        for token in ("INTEGRAL", "SUMMATION", "PRODUCT", "INFINITY", "RADICAL")
    ):
        return (7, "mathematics")
    return (8, "symbols")


def semantic_sort_key(codepoint: int | None, glyph_name: str) -> tuple:
    group, _ = semantic_group(codepoint, glyph_name)
    if codepoint is None:
        return (group, -1, glyph_name)
    character = chr(codepoint)
    category = unicodedata.category(character)
    case_rank = 0 if category == "Lu" else 1 if category == "Ll" else 2
    return (group, case_rank, codepoint, glyph_name)


FRIENDLY_OVERRIDES = {
    ".notdef": "missing glyph box",
    " ": "space",
    "!": "exclamation mark !",
    '"': 'quotation mark \"',
    "#": "number sign #",
    "$": "dollar sign $",
    "%": "percent sign %",
    "&": "ampersand &",
    "'": "apostrophe '",
    "(": "left parenthesis (",
    ")": "right parenthesis )",
    "*": "asterisk *",
    "+": "plus sign +",
    ",": "comma ,",
    "-": "hyphen -",
    ".": "full stop .",
    "/": "slash /",
    ":": "colon :",
    ";": "semicolon ;",
    "<": "less-than sign <",
    "=": "equals sign =",
    ">": "greater-than sign >",
    "?": "question mark ?",
    "@": "at sign @",
    "[": "left bracket [",
    "]": "right bracket ]",
    "\\": "backslash \\",
    "_": "underscore _",
    "{": "left brace {",
    "}": "right brace }",
    "|": "vertical bar |",
    "~": "tilde ~",
}


def friendly_label(codepoint: int | None, glyph_name: str) -> str:
    if glyph_name == ".notdef":
        return FRIENDLY_OVERRIDES[glyph_name]
    if codepoint is None:
        return glyph_name.replace("_", " ")
    character = chr(codepoint)
    if character in FRIENDLY_OVERRIDES:
        return FRIENDLY_OVERRIDES[character]
    unicode_name = unicodedata.name(character, "")
    if "LATIN" in unicode_name and "LETTER" in unicode_name:
        return f"{character} - Latin"
    if "CYRILLIC" in unicode_name and "LETTER" in unicode_name:
        return f"{character} - Cyrillic"
    if "GREEK" in unicode_name and "LETTER" in unicode_name:
        return f"{character} - Greek"
    if "DIGIT" in unicode_name and len(character) == 1 and "0" <= character <= "9":
        return character
    if character.isprintable() and not character.isspace():
        readable = unicode_name.lower().replace("leftwards", "left").replace("rightwards", "right")
        return readable or character
    return unicode_name.lower() or glyph_name


def slant_run(delta_y: float) -> float:
    return math.tan(math.radians(ITALIC_ANGLE)) * delta_y
