#!/usr/bin/env python
"""Freeze the reproducible Bold v2 sources, builds, reports, and hashes."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT = ROOT / "checkpoints" / "bold-v2"
ARTIFACTS = {
    "redrawn.sfd": ROOT / "fontforge" / "redrawn.sfd",
    "regular-bold-base.sfd": ROOT / "fontforge" / "regular-bold-base.sfd",
    "bold-raw.sfd": ROOT / "fontforge" / "bold-raw.sfd",
    "bold.sfd": ROOT / "fontforge" / "bold.sfd",
    "redrawn.ttf": ROOT / "qa" / "assets" / "redrawn.ttf",
    "regular-bold-base.ttf": ROOT / "qa" / "assets" / "regular-bold-base.ttf",
    "bold-raw.ttf": ROOT / "qa" / "assets" / "bold-raw.ttf",
    "bold.ttf": ROOT / "qa" / "assets" / "bold.ttf",
    "bold-base-cleanup.csv": ROOT / "qa" / "assets" / "bold-base-cleanup.csv",
    "bold-base-cleanup.json": ROOT / "qa" / "assets" / "bold-base-cleanup.json",
    "bold-build.json": ROOT / "qa" / "assets" / "bold-build.json",
    "bold-protection.json": ROOT / "qa" / "assets" / "bold-protection.json",
    "bold-post-cleanup.csv": ROOT / "qa" / "assets" / "bold-post-cleanup.csv",
    "bold-post-cleanup.json": ROOT / "qa" / "assets" / "bold-post-cleanup.json",
    "bold-report.csv": ROOT / "qa" / "assets" / "bold-report.csv",
    "bold-report.json": ROOT / "qa" / "assets" / "bold-report.json",
    "bold-manual-decisions.json": ROOT / "qa" / "bold-manual-decisions.json",
}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    CHECKPOINT.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name, source in ARTIFACTS.items():
        if not source.is_file():
            raise SystemExit("Missing checkpoint artifact: {}".format(source))
        target = CHECKPOINT / name
        shutil.copy2(source, target)
        hashes[name] = sha256(target)
    manifest = {
        "version": "bold-v2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "fontforge/redrawn.sfd",
        "cleaned_base": "fontforge/regular-bold-base.sfd",
        "settings": {
            "weight_change": 40,
            "type": "LCG",
            "counter_type": "retain",
            "base_cleanup": ["correct-direction", "remove-overlap", "correct-direction", "remove-overlap-audit"],
            "metrics_version": "bold-metrics-v2",
        },
        "artifact_hashes": hashes,
    }
    (CHECKPOINT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print("Frozen {} artifacts in {}".format(len(hashes), CHECKPOINT))


if __name__ == "__main__":
    raise SystemExit(main())
