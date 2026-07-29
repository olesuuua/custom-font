#!/usr/bin/env python3
"""Freeze the isolated Bold v5 pilot and manual alternatives."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT = ROOT / "checkpoints" / "bold-v5-pilot"
ARTIFACTS = {
    "bold-v5-pilot.sfd": ROOT / "fontforge" / "bold-v5-pilot.sfd",
    "bold-v5-pilot-manual.sfd": ROOT / "fontforge" / "bold-v5-pilot-manual.sfd",
    "bold-v5-pilot.ttf": ROOT / "qa" / "assets" / "bold-v5-pilot.ttf",
    "bold-v5-pilot-manual.ttf": ROOT / "qa" / "assets" / "bold-v5-pilot-manual.ttf",
    "bold-v5-pilot.json": ROOT / "qa" / "assets" / "bold-v5-pilot.json",
    "bold-report.csv": ROOT / "qa" / "assets" / "bold-report.csv",
    "bold-v41-triage.json": ROOT / "qa" / "assets" / "bold-v41-triage.json",
    "bold-manual-decisions.json": ROOT / "qa" / "bold-manual-decisions.json",
    "bold-ready-to-pass.json": ROOT / "qa" / "bold-ready-to-pass.json",
    "qa-index.html": ROOT / "qa" / "index.html",
    "build_bold_pilot.py": ROOT / "tools" / "build_bold_pilot.py",
    "verify_bold_v41.py": ROOT / "tools" / "verify_bold_v41.py",
    "README.md": ROOT / "README.md",
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if (CHECKPOINT / "manifest.json").exists() and not args.force:
        raise SystemExit("Pilot checkpoint exists; use --force only for intentional replacement")
    CHECKPOINT.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name, source in ARTIFACTS.items():
        if not source.is_file():
            raise SystemExit("Missing pilot artifact: " + str(source))
        target = CHECKPOINT / name
        shutil.copy2(source, target)
        hashes[name] = sha(target)
    report = json.loads((ROOT / "qa" / "assets" / "bold-v5-pilot.json").read_text(encoding="utf-8"))
    manifest = {
        "version": "bold-v5-pilot-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_checkpoint": "checkpoints/bold-v4",
        "qa_checkpoint": "checkpoints/bold-v4.1",
        "selected": report["selected"],
        "accepted": report["accepted"],
        "blocked": report["blocked"],
        "accepted_glyphs": [name for name, row in report["glyphs"].items() if row["status"] == "accepted"],
        "manual_alternative_glyphs": [name for name, row in report["glyphs"].items() if row.get("manual_candidate")],
        "main_bold_unchanged": True,
        "artifact_hashes": hashes,
    }
    (CHECKPOINT / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"frozen={len(hashes)}; accepted={report['accepted']}; blocked={report['blocked']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
