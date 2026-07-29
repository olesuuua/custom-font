#!/usr/bin/env ffpython
"""Verify Bold v4.1 triage, review migration, and isolated v5 pilot."""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

import fontforge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import redraw_font as rf
import repair_bold_v4 as v4

V4 = ROOT / "checkpoints" / "bold-v4"
REGULAR = ROOT / "fontforge" / "redrawn.sfd"
BASE = ROOT / "fontforge" / "regular-bold-base.sfd"
BOLD = ROOT / "fontforge" / "bold.sfd"
PILOT = ROOT / "fontforge" / "bold-v5-pilot.sfd"
MANUAL = ROOT / "fontforge" / "bold-v5-pilot-manual.sfd"
REPORT = ROOT / "qa" / "assets" / "bold-report.csv"
META = ROOT / "qa" / "assets" / "bold-report.json"
TRIAGE = ROOT / "qa" / "assets" / "bold-v41-triage.json"
PILOT_REPORT = ROOT / "qa" / "assets" / "bold-v5-pilot.json"
DECISIONS = ROOT / "qa" / "bold-manual-decisions.json"
READY = ROOT / "qa" / "bold-ready-to-pass.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main():
    errors = []
    manifest = load(V4 / "manifest.json")
    for path, name in ((REGULAR, "redrawn.sfd"), (BASE, "regular-bold-base.sfd"), (BOLD, "bold.sfd")):
        if digest(path) != manifest["artifact_hashes"][name]:
            errors.append(f"authoritative source changed: {name}")
    with REPORT.open(encoding="utf-8-sig", newline="") as handle:
        rows = {row["glyph"]: row for row in csv.DictReader(handle)}
    meta = load(META)
    triage = load(TRIAGE)
    if len(rows) != 340 or meta.get("version") != "bold-metrics-v4.1":
        errors.append("v4.1 report shape or version differs")
    if meta.get("legacy_broad_blocker_count") != 60 or meta.get("legacy_broad_classified_count") != 60:
        errors.append("legacy blocker cohort is not fully classified")
    if any("counter-or-component-blocked" in row["manual_blockers"] + row["hard_warnings"] for row in rows.values()):
        errors.append("generic counter/component blocker remains")
    if triage.get("classified_count") != 60 or triage.get("generic_remaining") != 0:
        errors.append("triage evidence is incomplete")
    decisions = load(DECISIONS)
    statuses = {}
    for decision in decisions.get("decisions", {}).values():
        statuses[decision["status"]] = statuses.get(decision["status"], 0) + 1
    if decisions.get("version") != "bold-manual-review-v4.1" or decisions.get("metrics_version") != "bold-metrics-v4.1":
        errors.append("decision version differs")
    if len(decisions.get("decisions", {})) != 340 or statuses != {"pass": 219, "almost_done": 67, "needs_rework": 54}:
        errors.append("manual decision counts changed")
    overrides = [
        (name, decision)
        for name, decision in decisions.get("decisions", {}).items()
        if decision.get("manual_override")
    ]
    if len(overrides) != 13:
        errors.append("explicit visual-approval override count differs")
    for name, decision in overrides:
        row = rows[name]
        if (
            decision.get("status") != "pass"
            or int(row["bold_self_intersections"])
            or int(row["bold_invalid_handles"])
            or int(row["bold_open_contours"])
        ):
            errors.append(name + " invalid visual-approval override")
    ready = load(READY)
    if ready.get("metrics_version") != "bold-metrics-v4.1" or len(ready.get("glyphs", {})) != 11:
        errors.append("Ready-to-Pass migration differs")
    for name in ready.get("glyphs", {}):
        decision = decisions["decisions"].get(name, {})
        row = rows[name]
        if (
            decision.get("status") not in {"pass", "almost_done"}
            or decision.get("regular_hash") != row["regular_hash"]
            or decision.get("base_hash") != row["base_hash"]
            or decision.get("bold_hash") != row["bold_hash"]
        ):
            errors.append(name + " Ready decision binding differs")
    pilot = load(PILOT_REPORT)
    selected = pilot.get("selected", [])
    if len(selected) > 8 or selected != ["Theta", "brokenbar", "dagger"]:
        errors.append("pilot membership differs")
    if pilot.get("accepted") != 1 or pilot.get("blocked") != 2:
        errors.append("pilot gate totals differ")
    for item in pilot.get("calibration", {}).values():
        if item.get("status") != "ok" or any(abs(value - 1) > 1e-9 for value in item["iou"].values()):
            errors.append("pilot calibration failed")
    if pilot["calibration"]["period"].get("coordinate_delta", 999) > 2:
        errors.append("coordinate orientation calibration failed")

    fonts = [fontforge.open(str(path)) for path in (BOLD, PILOT, MANUAL)]
    source, accepted, manual = fonts
    try:
        names = [[glyph.glyphname for glyph in font.glyphs()] for font in fonts]
        if any(value != names[0] for value in names[1:]) or len(names[0]) != 340:
            errors.append("pilot glyph order differs")
        changed = []
        manual_changed = []
        for name in names[0]:
            if source[name].width != accepted[name].width or source[name].unicode != accepted[name].unicode:
                errors.append(name + " accepted pilot metrics/cmap changed")
            if rf.layer_hash(source[name].foreground) != rf.layer_hash(accepted[name].foreground):
                changed.append(name)
            if rf.layer_hash(source[name].foreground) != rf.layer_hash(manual[name].foreground):
                manual_changed.append(name)
        if changed != ["dagger"]:
            errors.append("accepted pilot changed unexpected glyphs: " + ",".join(changed))
        if sorted(manual_changed) != ["Theta", "brokenbar"]:
            errors.append("manual pilot changed unexpected glyphs: " + ",".join(manual_changed))
        defects = v4.structural(accepted["dagger"].foreground)
        if defects["intersections"] or defects["open_contours"] or defects["invalid_handles"]:
            errors.append("accepted dagger retains structural defects")
    finally:
        for font in fonts:
            font.close()
    print(f"glyphs=340; decisions=340; ready=11; pilot={len(selected)}; accepted={pilot.get('accepted')}; errors={len(errors)}")
    for error in errors:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
