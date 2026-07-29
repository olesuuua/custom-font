#!/usr/bin/env ffpython
"""Apply a bounded batch of protected one-glyph SFDs to editable Bold."""

import argparse
import json
from pathlib import Path

import fontforge


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("bold")
    parser.add_argument("manifest")
    args = parser.parse_args()
    items = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    bold = fontforge.open(args.bold)
    try:
        for item in items:
            candidate = fontforge.open(item["sfd"])
            try:
                name = item["glyph"]
                bold[name].foreground = candidate[name].foreground.dup()
            finally:
                candidate.close()
        bold.save(args.bold)
    finally:
        bold.close()


if __name__ == "__main__":
    raise SystemExit(main())
