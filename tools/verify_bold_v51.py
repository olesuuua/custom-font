#!/usr/bin/env ffpython
"""Verify the promoted Bold v5.1 source and decision migration."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf
import repair_bold_v4 as v4

FROZEN = ROOT / "checkpoints" / "bold-v5" / "batch-01-review"
CURRENT = ROOT / "fontforge" / "bold.sfd"
DECISIONS = ROOT / "qa" / "bold-manual-decisions.json"
READY = ROOT / "qa" / "bold-ready-to-pass.json"
META = ROOT / "qa" / "assets" / "bold-report.json"
APPROVED = [
    "two", "uni042F", "brokenbar", "minute", "second", "uni21BA", "dagger"
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main():
    errors = []
    old_manifest = load(FROZEN / "manifest.json")
    guarded = {
        "redrawn.sfd": ROOT / "fontforge" / "redrawn.sfd",
        "regular-bold-base.sfd": ROOT / "fontforge" / "regular-bold-base.sfd",
        "bold-raw.sfd": ROOT / "fontforge" / "bold-raw.sfd",
    }
    for name, path in guarded.items():
        if digest(path) != old_manifest["artifact_hashes"][name]:
            errors.append("authoritative source changed: " + name)

    old = fontforge.open(str(FROZEN / "bold.sfd"))
    current = fontforge.open(str(CURRENT))
    try:
        old_names = [glyph.glyphname for glyph in old.glyphs()]
        names = [glyph.glyphname for glyph in current.glyphs()]
        if old_names != names or len(names) != 340:
            errors.append("glyph order differs")
        changed = []
        for name in names:
            if old[name].unicode != current[name].unicode:
                errors.append(name + " cmap changed")
            if old[name].width != current[name].width:
                errors.append(name + " width changed")
            if rf.layer_hash(old[name].foreground) != rf.layer_hash(current[name].foreground):
                changed.append(name)
        if sorted(changed) != sorted(APPROVED):
            errors.append("unexpected changed glyphs: " + ",".join(changed))
        if rf.layer_hash(old["uni2010"].foreground) != rf.layer_hash(current["uni2010"].foreground):
            errors.append("rejected uni2010 changed")
        if current.version != "1.002":
            errors.append("font version is not 1.002")
        for name in APPROVED:
            defects = v4.structural(current[name].foreground)
            if any(defects[key] for key in ("intersections", "open_contours", "invalid_handles")):
                errors.append(name + " has structural defects")
    finally:
        old.close()
        current.close()

    decisions = load(DECISIONS)
    counts = {}
    for decision in decisions.get("decisions", {}).values():
        counts[decision["status"]] = counts.get(decision["status"], 0) + 1
    if decisions.get("version") != "bold-manual-review-v5.1":
        errors.append("decision version differs")
    if decisions.get("metrics_version") != "bold-metrics-v5.1":
        errors.append("decision metrics version differs")
    if counts != {"pass": 226, "almost_done": 60, "needs_rework": 54}:
        errors.append("decision totals differ")
    for name in APPROVED:
        if decisions["decisions"][name]["status"] != "pass":
            errors.append(name + " was not passed")
    if decisions["decisions"]["uni2010"]["status"] != "almost_done":
        errors.append("uni2010 status changed")
    ready = load(READY)
    if ready.get("version") != "bold-ready-to-pass-v5.1":
        errors.append("Ready queue version differs")
    if ready.get("metrics_version") != "bold-metrics-v5.1":
        errors.append("Ready queue metrics version differs")
    if len(ready.get("glyphs", {})) != 11:
        errors.append("Ready queue count differs")
    meta = load(META)
    if meta.get("version") != "bold-metrics-v5.1":
        errors.append("QA metrics version differs")
    if meta.get("bold_sfd_hash") != digest(CURRENT):
        errors.append("QA Bold hash differs")

    print("glyphs=340; promoted=7; pass=226; almost=60; rework=54; errors={}".format(
        len(errors)
    ))
    for error in errors:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
