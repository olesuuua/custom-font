#!/usr/bin/env python
"""Archive the reviewed Bold v2 state before any Bold v3 outline changes."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "checkpoints" / "bold-v2-reviewed"
ARTIFACTS = {
    "bold.sfd": ROOT / "fontforge" / "bold.sfd",
    "bold.ttf": ROOT / "qa" / "assets" / "bold.ttf",
    "bold-report.csv": ROOT / "qa" / "assets" / "bold-report.csv",
    "bold-report.json": ROOT / "qa" / "assets" / "bold-report.json",
    "bold-manual-decisions.json": ROOT / "qa" / "bold-manual-decisions.json",
    "bold-build.json": ROOT / "qa" / "assets" / "bold-build.json",
    "bold-protection.json": ROOT / "qa" / "assets" / "bold-protection.json",
    "bold-post-cleanup.csv": ROOT / "qa" / "assets" / "bold-post-cleanup.csv",
    "bold-post-cleanup.json": ROOT / "qa" / "assets" / "bold-post-cleanup.json",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if TARGET.exists():
        raise SystemExit("Refusing to replace existing archive: {}".format(TARGET))
    TARGET.mkdir(parents=True)
    hashes = {}
    for name, source in ARTIFACTS.items():
        target = TARGET / name
        shutil.copy2(source, target)
        hashes[name] = digest(target)
    manifest = {
        "version": "bold-v2-reviewed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "authoritative_regular_sha256": digest(ROOT / "fontforge" / "redrawn.sfd"),
        "cleaned_base_sha256": digest(ROOT / "fontforge" / "regular-bold-base.sfd"),
        "artifact_hashes": hashes,
    }
    (TARGET / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print("Archived reviewed Bold v2 in {}".format(TARGET))


if __name__ == "__main__":
    main()
