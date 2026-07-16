#!/usr/bin/env python3
"""Remove overlap from one glyph with FontTools/Skia PathOps."""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".font-deps"))
PATHOPS_DIRECTORY = ROOT / ".font-deps" / "pathops"
_PATHOPS_DLL_HANDLE = None
if hasattr(os, "add_dll_directory") and PATHOPS_DIRECTORY.exists():
    _PATHOPS_DLL_HANDLE = os.add_dll_directory(str(PATHOPS_DIRECTORY))

from fontTools.ttLib import TTFont
from fontTools.ttLib.removeOverlaps import removeOverlaps


def glyph_bytes(font, name):
    glyph = font["glyf"][name]
    return glyph.compile(font["glyf"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("glyph")
    parser.add_argument("output")
    parser.add_argument("report")
    args = parser.parse_args()

    result = {"backend": "fonttools-skia-pathops", "status": "failed"}
    try:
        font = TTFont(args.source, recalcBBoxes=False, recalcTimestamp=False)
        metrics = font["hmtx"][args.glyph]
        before = glyph_bytes(font, args.glyph)
        removeOverlaps(font, glyphNames=[args.glyph], removeHinting=True, ignoreErrors=False)
        font["hmtx"][args.glyph] = metrics
        after = glyph_bytes(font, args.glyph)
        first_changed = before != after
        passes = 1
        idempotent = False
        previous_output = Path(args.output).with_name(Path(args.output).stem + ".previous.ttf")
        for passes in range(2, 6):
            previous = after
            snapshot = Path(args.output).with_name(Path(args.output).stem + ".snapshot.ttf")
            font.save(snapshot, reorderTables=False)
            shutil.copy2(snapshot, previous_output)
            snapshot.unlink()
            removeOverlaps(font, glyphNames=[args.glyph], removeHinting=True, ignoreErrors=False)
            font["hmtx"][args.glyph] = metrics
            after = glyph_bytes(font, args.glyph)
            if previous == after:
                idempotent = True
                break
        result.update(
            status="ok",
            changed=first_changed,
            idempotent=idempotent,
            passes=passes,
            previous_output=str(previous_output.resolve()) if previous_output.exists() else "",
        )
        font.save(args.output, reorderTables=False)
        font.close()
    except Exception as exc:
        result["error"] = "{}: {}".format(type(exc).__name__, exc)
    Path(args.report).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
