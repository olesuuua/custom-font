#!/usr/bin/env python3
"""Restore glyph-independent metadata after FontForge redraw generation."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".font-deps"))

from fontTools.ttLib import TTFont


def main():
    source_path, target_path = sys.argv[1:3]
    source = TTFont(source_path, recalcBBoxes=False, recalcTimestamp=False)
    target = TTFont(target_path, recalcBBoxes=False, recalcTimestamp=False)
    try:
        if source.getGlyphOrder() != target.getGlyphOrder():
            raise RuntimeError("generated glyph order differs from source")
        for tag in ("cmap", "name", "OS/2", "post", "hhea", "hmtx", "kern", "GDEF", "GPOS", "GSUB"):
            if tag in source:
                target[tag] = source[tag]
            elif tag in target and tag in ("kern", "GDEF", "GPOS", "GSUB"):
                del target[tag]
        target.save(target_path, reorderTables=False)
    finally:
        source.close()
        target.close()


if __name__ == "__main__":
    main()
