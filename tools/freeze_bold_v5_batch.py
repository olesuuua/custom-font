#!/usr/bin/env python3
"""Freeze immutable source and review checkpoints for Bold v5 batches."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_DIR = ROOT / "checkpoints" / "bold-v5"

SOURCE_ARTIFACTS = {
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
    "bold-v5-batch1.json": ROOT / "tools" / "bold-v5-batch1.json",
}

BATCH1_ARTIFACTS = {
    **SOURCE_ARTIFACTS,
    "bold-v5-batch1.sfd": ROOT / "fontforge" / "bold-v5-batch1.sfd",
    "bold-v5-batch1.ttf": ROOT / "qa" / "assets" / "bold-v5-batch1.ttf",
    "bold-v5-batch1-report.json": ROOT / "qa" / "assets" / "bold-v5-batch1.json",
    "qa-index.html": ROOT / "qa" / "index.html",
    "repair_bold_batch.py": ROOT / "tools" / "repair_bold_batch.py",
    "verify_bold_v5_batch1.py": ROOT / "tools" / "verify_bold_v5_batch1.py",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("source", "batch1"), required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    target = BASE_DIR / ("batch-00-source" if args.stage == "source" else "batch-01-review")
    manifest_path = target / "manifest.json"
    if manifest_path.exists() and not args.force:
        raise SystemExit("{} exists; use --force only for intentional replacement".format(target))
    artifacts = SOURCE_ARTIFACTS if args.stage == "source" else BATCH1_ARTIFACTS
    target.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name, source in artifacts.items():
        if not source.is_file():
            raise SystemExit("Missing checkpoint artifact: " + str(source))
        destination = target / name
        shutil.copy2(source, destination)
        hashes[name] = digest(destination)
    review = json.loads((ROOT / "qa" / "bold-manual-decisions.json").read_text(encoding="utf-8-sig"))
    counts = {}
    for decision in review["decisions"].values():
        status = decision["status"]
        counts[status] = counts.get(status, 0) + 1
    value = {
        "version": "bold-v5-{}-v1".format(args.stage),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": "c2fb36015f76d8ba290c9ef2b646579526ddc181",
        "stage": args.stage,
        "main_bold_unchanged": True,
        "decision_counts": counts,
        "artifact_hashes": hashes,
    }
    manifest_path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("stage={}; frozen={}".format(args.stage, len(hashes)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
