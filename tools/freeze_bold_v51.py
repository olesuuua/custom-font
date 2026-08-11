#!/usr/bin/env python3
"""Freeze the promoted Bold v5.1 source and review state."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "checkpoints" / "bold-v5" / "batch-01-promoted"
ARTIFACTS = {
    "redrawn.sfd": ROOT / "fontforge" / "redrawn.sfd",
    "regular-bold-base.sfd": ROOT / "fontforge" / "regular-bold-base.sfd",
    "bold-raw.sfd": ROOT / "fontforge" / "bold-raw.sfd",
    "bold.sfd": ROOT / "fontforge" / "bold.sfd",
    "redrawn.ttf": ROOT / "qa" / "assets" / "redrawn.ttf",
    "regular-bold-base.ttf": ROOT / "qa" / "assets" / "regular-bold-base.ttf",
    "bold-raw.ttf": ROOT / "qa" / "assets" / "bold-raw.ttf",
    "bold.ttf": ROOT / "qa" / "assets" / "bold.ttf",
    "bold-report.csv": ROOT / "qa" / "assets" / "bold-report.csv",
    "bold-report.json": ROOT / "qa" / "assets" / "bold-report.json",
    "bold-manual-decisions.json": ROOT / "qa" / "bold-manual-decisions.json",
    "bold-ready-to-pass.json": ROOT / "qa" / "bold-ready-to-pass.json",
    "promote_bold_v51.py": ROOT / "tools" / "promote_bold_v51.py",
    "migrate_bold_v51_decisions.py": ROOT / "tools" / "migrate_bold_v51_decisions.py",
    "verify_bold_v51.py": ROOT / "tools" / "verify_bold_v51.py",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="refresh this generated checkpoint in place",
    )
    args = parser.parse_args()
    manifest_path = TARGET / "manifest.json"
    if manifest_path.exists() and not args.force:
        raise SystemExit(str(TARGET) + " exists; refusing to overwrite")
    TARGET.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name, source in ARTIFACTS.items():
        if not source.is_file():
            raise SystemExit("missing artifact: " + str(source))
        destination = TARGET / name
        shutil.copy2(source, destination)
        hashes[name] = digest(destination)
    payload = {
        "version": "bold-v5.1-promoted-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": "c2fb36015f76d8ba290c9ef2b646579526ddc181",
        "font_version": "1.002",
        "promoted_glyphs": [
            "two", "uni042F", "brokenbar", "minute", "second", "uni21BA", "dagger"
        ],
        "rejected_unchanged": ["uni2010"],
        "decision_counts": {"pass": 226, "almost_done": 60, "needs_rework": 54},
        "artifact_hashes": hashes,
    }
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print("frozen={}; target={}".format(len(hashes), TARGET))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
