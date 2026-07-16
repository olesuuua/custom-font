#!/usr/bin/env python3
"""Restore immutable glyph order and horizontal metrics after FontForge output."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".font-deps"))

from fontTools.ttLib import TTFont


def main():
    source_path, target_path = sys.argv[1:3]
    source = TTFont(source_path, recalcBBoxes=False, recalcTimestamp=False)
    target = TTFont(target_path, recalcBBoxes=False, recalcTimestamp=False)
    order = source.getGlyphOrder()
    if target.getGlyphOrder() != order:
        raise RuntimeError("generated glyph order differs from source")
    target["hmtx"].metrics = dict(source["hmtx"].metrics)
    target.save(target_path, reorderTables=False)
    source.close()
    target.close()


if __name__ == "__main__":
    main()
