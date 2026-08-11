#!/usr/bin/env ffpython
"""Promote the seven user-approved Bold v5 Batch 1 glyphs."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf

SOURCE = ROOT / "fontforge" / "bold.sfd"
CANDIDATE = ROOT / "fontforge" / "bold-v5-batch1.sfd"
CHECKPOINT = ROOT / "checkpoints" / "bold-v5" / "batch-01-review" / "manifest.json"
APPROVED = [
    "two",
    "uni042F",
    "brokenbar",
    "minute",
    "second",
    "uni21BA",
    "dagger",
]
REJECTED = "uni2010"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    manifest = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
    hashes = manifest["artifact_hashes"]
    if digest(SOURCE) != hashes["bold.sfd"]:
        raise SystemExit("bold.sfd is not the frozen Batch 1 source")
    if digest(CANDIDATE) != hashes["bold-v5-batch1.sfd"]:
        raise SystemExit("Batch 1 candidate hash differs from its checkpoint")

    source = fontforge.open(str(SOURCE))
    candidate = fontforge.open(str(CANDIDATE))
    try:
        names = [glyph.glyphname for glyph in source.glyphs()]
        if names != [glyph.glyphname for glyph in candidate.glyphs()] or len(names) != 340:
            raise SystemExit("candidate glyph order differs")
        rejected_hash = rf.layer_hash(source[REJECTED].foreground)
        before = {name: rf.layer_hash(source[name].foreground) for name in names}
        for name in APPROVED:
            if source[name].width != candidate[name].width:
                raise SystemExit(name + " candidate width differs")
            if source[name].unicode != candidate[name].unicode:
                raise SystemExit(name + " candidate Unicode differs")
            source[name].foreground = candidate[name].foreground.dup()
        source.version = "1.002"
        source.save(str(SOURCE))
    finally:
        source.close()
        candidate.close()

    promoted = fontforge.open(str(SOURCE))
    try:
        changed = [
            name for name in names
            if rf.layer_hash(promoted[name].foreground) != before[name]
        ]
        if sorted(changed) != sorted(APPROVED):
            raise SystemExit("unexpected promoted glyph set: " + ",".join(changed))
        if rf.layer_hash(promoted[REJECTED].foreground) != rejected_hash:
            raise SystemExit("rejected uni2010 changed")
        if promoted.version != "1.002":
            raise SystemExit("Bold version was not updated to 1.002")
    finally:
        promoted.close()
    print("promoted={}; version=1.002; rejected_unchanged={}".format(
        len(APPROVED), REJECTED
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
