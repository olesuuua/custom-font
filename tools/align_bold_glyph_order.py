#!/usr/bin/env python
"""Align Bold SFD encoding slots to the authoritative Regular glyph order."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGULAR = ROOT / "fontforge" / "redrawn.sfd"
TARGETS = (ROOT / "fontforge" / "bold-raw.sfd", ROOT / "fontforge" / "bold.sfd")


def encodings(text):
    result = {}
    current = None
    for line in text.splitlines():
        if line.startswith("StartChar: "):
            current = line[len("StartChar: "):]
        elif current is not None and line.startswith("Encoding: ") and line != "Encoding: UnicodeFull":
            result[current] = line
            current = None
    return result


def align(path, mapping):
    lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    current = None
    changed = 0
    for index, line in enumerate(lines):
        if line == "Encoding: UnicodeBmp":
            lines[index] = "Encoding: UnicodeFull"
        elif line.startswith("BeginChars: "):
            lines[index] = "BeginChars: 1114121 340"
        elif line.startswith("StartChar: "):
            current = line[len("StartChar: "):]
        elif current is not None and line.startswith("Encoding: ") and current in mapping:
            if line != mapping[current]:
                lines[index] = mapping[current]
                changed += 1
            current = None
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("{}: aligned {} glyph slots".format(path.name, changed))


def main():
    mapping = encodings(REGULAR.read_text(encoding="utf-8"))
    if len(mapping) != 340:
        raise SystemExit("Expected 340 Regular encoding records, found {}".format(len(mapping)))
    for path in TARGETS:
        align(path, mapping)


if __name__ == "__main__":
    main()
