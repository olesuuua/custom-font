#!/usr/bin/env ffpython
"""Clean one Regular glyph in an isolated FontForge process."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("glyph")
    parser.add_argument("output")
    parser.add_argument("report")
    args = parser.parse_args()
    result = {"glyph": args.glyph, "status": "failed", "backend": "fontforge-isolated"}
    source = scratch = None
    try:
        source = fontforge.open(args.source)
        original = source[args.glyph]
        scratch = fontforge.font()
        scratch.encoding = "UnicodeFull"
        scratch.ascent = source.ascent
        scratch.descent = source.descent
        target = scratch.createChar(original.unicode, original.glyphname)
        target.width = original.width
        target.foreground = original.foreground.dup()
        before_hash = rf.layer_hash(target.foreground)
        target.correctDirection()
        target.removeOverlap()
        target.correctDirection()
        first_hash = rf.layer_hash(target.foreground)
        target.removeOverlap()
        target.correctDirection()
        second_hash = rf.layer_hash(target.foreground)
        scratch.save(args.output)
        result.update(
            status="ok",
            before_hash=before_hash,
            cleaned_hash=second_hash,
            changed=before_hash != second_hash,
            idempotent=first_hash == second_hash,
            width=target.width,
        )
    except Exception as error:
        result["error"] = "{}: {}".format(type(error).__name__, error)
    finally:
        for font in (scratch, source):
            if font is not None:
                try:
                    font.close()
                except RuntimeError:
                    pass
        Path(args.report).write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
